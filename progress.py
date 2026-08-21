"""
progress.py — animated, self-deleting progress bars for long operations.

Design goals:
  • Every operation that can take >1s shows a live progress bar.
  • The bar animates (so the user knows the bot is alive, not hung).
  • When the work finishes the bar DELETES ITSELF and the real result is
    delivered — no leftover "⏳ Uploading…" clutter in the chat.
  • Fast operations never flicker: nothing is sent for the first `show_after`
    seconds, so sub-second work produces no message at all.
  • Telegram flood limits are respected (RetryAfter honoured, edits throttled).

Usage
-----
    async with Progress(bot, chat_id, "📤 File upload",
                        steps=["Download", "Upload", "Parse"]) as p:
        await p.step(0)
        ...
        await p.step(1, "2.3 MB")
        ...
    # bar deleted automatically here

On error the bar turns into a red error message and is KEPT:

    async with Progress(...) as p:
        raise RuntimeError("boom")     # -> "❌ boom" stays in chat
"""
import asyncio
import html
import logging
import time
from typing import Optional, Sequence

from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError

log = logging.getLogger("progress")

FILLED = "▰"
EMPTY = "▱"
BAR_LEN = 10

# Icons for the step checklist
ICON_DONE = "✅"
ICON_NOW = "⏳"
ICON_TODO = "▫️"

_DOTS = ["", ".", "..", "..."]


def bar_str(pct: float) -> str:
    """Render a 10-cell progress bar for a 0-100 percentage."""
    pct = max(0.0, min(100.0, pct))
    n = int(round(pct / 100.0 * BAR_LEN))
    return FILLED * n + EMPTY * (BAR_LEN - n)


def _fmt_elapsed(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    m, s = divmod(int(sec), 60)
    return f"{m}m {s:02d}s"


class Progress:
    """Animated progress message that cleans up after itself."""

    def __init__(self, bot, chat_id: int, title: str,
                 steps: Optional[Sequence[str]] = None,
                 reply_to: Optional[int] = None,
                 interval: float = 2.0,
                 show_after: float = 0.6):
        self.bot = bot
        self.chat_id = chat_id
        self.title = title
        self.steps = [str(s) for s in steps] if steps else []
        self.reply_to = reply_to
        self.interval = max(1.2, interval)   # never hammer Telegram
        self.show_after = show_after

        self.msg = None                  # telegram.Message once shown
        self._task: Optional[asyncio.Task] = None
        self._idx = 0                    # current step index
        self._note = ""                  # free-text sub-status
        self._pct = 0.0                  # displayed percentage
        self._t0 = time.monotonic()
        self._tick = 0
        self._last_render = ""
        self._closed = False

    # ---------- public API ----------

    async def __aenter__(self) -> "Progress":
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._animate())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            await self.done()
        else:
            await self.fail(f"{exc}")
        return False   # never swallow exceptions

    async def step(self, index: int, note: str = ""):
        """Move to step `index` (0-based) with an optional note."""
        self._idx = max(0, min(index, max(0, len(self.steps) - 1)))
        self._note = note
        # jump the bar to the start of this step so movement feels responsive
        if self.steps:
            self._pct = max(self._pct, self._idx / len(self.steps) * 100.0)
        await self._render(force=True)

    async def note(self, text: str):
        """Update the sub-status line without changing the step."""
        self._note = text
        await self._render(force=True)

    async def set_pct(self, pct: float, note: str = ""):
        """Manually drive the percentage (for byte-accurate progress)."""
        self._pct = max(0.0, min(100.0, pct))
        if note:
            self._note = note
        await self._render(force=True)

    async def done(self, final_text: Optional[str] = None):
        """Finish: delete the bar (or replace it with `final_text`)."""
        if self._closed:
            return
        self._closed = True
        await self._stop_task()
        if self.msg is None:
            if final_text:
                await self._safe_send(final_text)
            return
        if final_text:
            await self._safe_edit(final_text)
        else:
            await self._safe_delete()

    async def fail(self, message: str, keep: bool = True):
        """Turn the bar into an error message (kept in chat by default)."""
        if self._closed:
            return
        self._closed = True
        await self._stop_task()
        text = f"❌ {html.escape(str(message))[:900]}"
        if not keep:
            await self._safe_delete()
            return
        if self.msg is None:
            await self._safe_send(text)
        else:
            await self._safe_edit(text)

    # ---------- internals ----------

    async def _stop_task(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _animate(self):
        """Background loop: creep the bar forward and re-render."""
        try:
            await asyncio.sleep(self.show_after)
            while True:
                self._tick += 1
                self._creep()
                await self._render()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:           # never let the animator kill the job
            log.debug("progress animator stopped: %s", e)

    def _creep(self):
        """Asymptotically approach the end of the current step."""
        if self.steps:
            n = len(self.steps)
            lo = self._idx / n * 100.0
            hi = (self._idx + 1) / n * 100.0
            ceiling = hi - (hi - lo) * 0.12      # never quite complete the step
        else:
            lo, ceiling = 0.0, 92.0
        if self._pct < lo:
            self._pct = lo
        self._pct += (ceiling - self._pct) * 0.28

    def _body(self) -> str:
        dots = _DOTS[self._tick % len(_DOTS)]
        elapsed = _fmt_elapsed(time.monotonic() - self._t0)
        pct = int(self._pct)

        lines = [f"<b>{html.escape(self.title)}</b>", ""]
        lines.append(f"<code>{bar_str(self._pct)}</code>  {pct}%")

        if self.steps:
            lines.append("")
            for i, label in enumerate(self.steps):
                if i < self._idx:
                    icon, style = ICON_DONE, "{}"
                elif i == self._idx:
                    icon, style = ICON_NOW, "<b>{}</b>"
                else:
                    icon, style = ICON_TODO, "<i>{}</i>"
                txt = style.format(html.escape(label))
                if i == self._idx:
                    txt += dots
                lines.append(f"{icon} {txt}")

        if self._note:
            lines.append("")
            lines.append(f"<i>{html.escape(self._note)[:200]}</i>")

        lines.append("")
        lines.append(f"<i>⏱ {elapsed}</i>")
        return "\n".join(lines)

    async def _render(self, force: bool = False):
        if self._closed:
            return
        # Don't create the message before show_after has elapsed — this keeps
        # fast operations completely silent.
        if self.msg is None and (time.monotonic() - self._t0) < self.show_after:
            return
        text = self._body()
        if not force and text == self._last_render:
            return
        self._last_render = text
        if self.msg is None:
            self.msg = await self._safe_send(text)
        else:
            await self._safe_edit(text)

    async def _safe_send(self, text: str):
        for attempt in range(3):
            try:
                return await self.bot.send_message(
                    chat_id=self.chat_id, text=text, parse_mode="HTML",
                    reply_to_message_id=self.reply_to,
                    disable_notification=True,
                )
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 0.5)
            except BadRequest as e:
                # reply target vanished → resend without the reply
                if self.reply_to is not None:
                    self.reply_to = None
                    continue
                log.debug("progress send failed: %s", e)
                return None
            except (TimedOut, NetworkError):
                await asyncio.sleep(1.0)
            except Exception as e:
                log.debug("progress send failed: %s", e)
                return None
        return None

    async def _safe_edit(self, text: str):
        if self.msg is None:
            return
        for attempt in range(3):
            try:
                await self.msg.edit_text(text, parse_mode="HTML")
                return
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 0.5)
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                log.debug("progress edit failed: %s", e)
                return
            except (TimedOut, NetworkError):
                await asyncio.sleep(1.0)
            except Exception as e:
                log.debug("progress edit failed: %s", e)
                return

    async def _safe_delete(self):
        if self.msg is None:
            return
        try:
            await self.msg.delete()
        except Exception as e:
            log.debug("progress delete failed: %s", e)
        finally:
            self.msg = None


class Waiter:
    """
    Lightweight animator for an EXISTING message (used for the chat placeholder
    while we wait for DeepSeek's first token).

    Unlike Progress it never sends or deletes anything — it just animates a
    message that the caller already owns and will overwrite with real content.

        w = Waiter(placeholder, "DeepSeek is thinking")
        await w.start()
        ...
        await w.stop()      # caller then edits `placeholder` with the answer
    """

    def __init__(self, msg, label: str = "Processing", interval: float = 2.0):
        self.msg = msg
        self.label = label
        self.interval = max(1.2, interval)
        self._task: Optional[asyncio.Task] = None
        self._t0 = time.monotonic()
        self._pct = 0.0
        self._tick = 0
        self.stopped = False

    def update_label(self, new_label: str):
        self.label = new_label

    async def start(self):
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self.stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _loop(self):
        try:
            await asyncio.sleep(0.8)
            while not self.stopped:
                self._tick += 1
                self._pct += (92.0 - self._pct) * 0.22
                dots = _DOTS[self._tick % len(_DOTS)]
                elapsed = _fmt_elapsed(time.monotonic() - self._t0)
                text = (f"<code>{bar_str(self._pct)}</code>  "
                        f"{int(self._pct)}%\n"
                        f"<i>{html.escape(self.label)}{dots}</i>  "
                        f"<i>· ⏱ {elapsed}</i>")
                try:
                    await self.msg.edit_text(text, parse_mode="HTML")
                except RetryAfter as e:
                    await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 0.5)
                except BadRequest:
                    pass
                except (TimedOut, NetworkError):
                    pass
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("waiter stopped: %s", e)
