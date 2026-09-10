"""
DeepSeek Telegram Bot — v4 (Power Edition)

Slash commands: /start, /help (only)
Everything else: inline buttons + smart message handling.

Bug fixes vs v3:
  • Voice replies as ONE proper OGG-Opus voice message (no more 2x)
  • Whisper `small` model + Hinglish initial prompt → far better STT quality

New features:
  • 🎭 Personas (Tutor, Coder, Dost, Writer, Translator, Comedian, Scientist,
      Startup Coach, Health Info)
  • 🔗 URL summarize — paste any http(s) link → fetches & summarizes
  • ▶️ YouTube summarize — paste YT link → transcript → summary
  • ⚡ Quick actions on responses: Translate/Summarize/Rephrase/Explain/Continue
  • 📤 Export chat as .md
  • 📊 Stats (message count, tokens estimate)
  • 🩺 Health check server (for Render Web Service)
  • 📄 Long response auto-file
  • Per-response buttons: 🔊 Speak · 📄 File · 🔁 Regen · ⚡ Actions
  • Robust error handling + retry
"""
import asyncio
import html
import logging
import os
import re
import secrets
import signal
import socket
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      BotCommand, BotCommandScopeChat, LinkPreviewOptions)
from telegram.error import BadRequest, Conflict, NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from deepseek_client import DeepSeekClient, RULES, login_with_credentials, login_with_credentials_ex
from md2tg import md_to_tg_html, strip_incomplete_markers, safe_for_telegram
from personas import PERSONAS, get_persona, wrap_prompt
from progress import Progress, Waiter
from urlfetch import (extract_urls, is_youtube, fetch_url_text,
                      fetch_youtube_transcript, youtube_enabled)

import admin as adm
import db
from scheduler import nightly_wipe_loop
from token_pool import POOL

# ---------- Config ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_TOKEN = os.getenv("DEEPSEEK_TOKEN")   # optional: seeds the key pool
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
WORKDIR = os.path.dirname(os.path.abspath(__file__))
ENABLE_HEALTH = os.getenv("ENABLE_HEALTH", "0") == "1"

if not TELEGRAM_TOKEN or not OWNER_ID:
    raise SystemExit(
        "❌ Missing required env vars.\n"
        "Set TELEGRAM_TOKEN and OWNER_ID (DeepSeek keys are managed from the\n"
        "in-bot admin panel, or seed one with DEEPSEEK_TOKEN).\n"
        "Copy .env.example → .env and fill in the values, or set them in "
        "your host's dashboard (Render / Railway / etc.)."
    )

CONCURRENCY = int(os.getenv("CONCURRENCY", "16"))
# Max DeepSeek requests per user inside RATE_WINDOW seconds (0 disables)
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "20"))
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))
# How long a user waits for a free DeepSeek key before being told to retry
KEY_WAIT_TIMEOUT = float(os.getenv("KEY_WAIT_TIMEOUT", "180"))
# Single-poller guard: how long to wait for a previous deploy to exit, and
# whether to forcibly take the lease if it will not.
LOCK_WAIT = float(os.getenv("LOCK_WAIT", "75"))
FORCE_POLL = os.getenv("FORCE_POLL", "0") == "1"
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"

MAX_TG_MSG = 3800
FILE_THRESHOLD = 10000
THINK_TICKER_WORDS = 18
URL_TEXT_CAP = 40000  # cap fetched web text chars

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dsbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# In-memory response cache per user (for regenerate/tts/file/quick actions)
LAST: Dict[int, dict] = {}
# Cooperative cancellation: user ids that pressed /cancel or the Stop button
CANCELLED: set = set()
# Sliding window of request timestamps per user, for rate limiting
_RATE: Dict[int, List[float]] = {}
MAX_HISTORY = 100
# Admins waiting to type something (broadcast text, a key, a DM)
PENDING_INPUT: Dict[int, dict] = {}
# Users we already pinged the owner about (avoid repeat spam)
PENDING_NOTIFIED: Dict[int, bool] = {}


# ---------- State ----------
@dataclass
class UserState:
    session_id: Optional[str] = None
    parent_msg_id: Optional[str] = None
    model_type: str = "default"
    thinking: bool = False
    search: bool = False
    voice_reply: bool = False
    tts_female: bool = True
    auto_urls: bool = True   # auto-fetch URLs from messages
    persona: str = "default"
    attached_files: List[List[str]] = field(default_factory=list)
    msg_count: int = 0
    total_chars_in: int = 0
    total_chars_out: int = 0

# Write-through cache: uid -> UserState mirrored in MongoDB.
STATE: Dict[int, UserState] = {}
# Cached user documents (status/role) so hot paths avoid a round-trip.
USERS: Dict[int, dict] = {}

# One lock per user. With concurrent_updates>1 two messages could otherwise
# interleave and corrupt parent_msg_id (DeepSeek's conversation pointer).
_LOCKS: Dict[int, asyncio.Lock] = {}

def user_lock(uid: int) -> asyncio.Lock:
    lk = _LOCKS.get(uid)
    if lk is None:
        lk = _LOCKS[uid] = asyncio.Lock()
    return lk


def _state_from_doc(doc: dict) -> UserState:
    st = UserState()
    for k, v in (doc.get("settings") or {}).items():
        if hasattr(st, k):
            setattr(st, k, v)
    st.session_id = doc.get("session_id")
    st.parent_msg_id = doc.get("parent_msg_id")
    st.attached_files = doc.get("attached_files") or []
    st.msg_count = doc.get("msg_count", 0)
    st.total_chars_in = doc.get("chars_in", 0)
    st.total_chars_out = doc.get("chars_out", 0)
    return st


async def load_user(uid: int, *, username: str = "",
                    first_name: str = "") -> dict:
    """Fetch (or create) the Mongo user doc and hydrate the local caches."""
    doc = await db.upsert_user(uid, username=username, first_name=first_name)
    USERS[uid] = doc
    STATE[uid] = _state_from_doc(doc)
    return doc


async def save_settings(uid: int) -> None:
    """Persist the toggle/persona block for one user."""
    st = STATE.get(uid)
    if not st:
        return
    await db.set_user_field(uid, settings={
        "model_type": st.model_type, "thinking": st.thinking,
        "search": st.search, "voice_reply": st.voice_reply,
        "tts_female": st.tts_female, "auto_urls": st.auto_urls,
        "persona": st.persona,
    })


async def save_session(uid: int) -> None:
    """Persist the DeepSeek conversation pointer for one user."""
    st = STATE.get(uid)
    if not st:
        return
    await db.set_user_field(uid, session_id=st.session_id,
                            parent_msg_id=st.parent_msg_id,
                            attached_files=st.attached_files)


def get_state(uid: int) -> UserState:
    if uid not in STATE:
        STATE[uid] = UserState()
    return STATE[uid]


async def ds_op(uid: int, method: str, *args, timeout: float = 60.0):
    """
    Run a short DeepSeek call on a pooled key.

    The key is held only for the duration of the call, so quick operations
    (create/list/delete/upload) never block a long-running chat stream for
    longer than necessary. Returns None when no key is available.
    """
    async with POOL.acquire(uid, timeout=timeout) as lease:
        if lease is None:
            return None
        try:
            return await asyncio.to_thread(getattr(lease.client, method), *args)
        except Exception as e:
            log.warning("DeepSeek %s failed on key %s: %s",
                        method, lease.label, e)
            await db.mark_token(lease.label, healthy=True, error=str(e))
            return None


def user_status(uid: int) -> str:
    if uid == OWNER_ID:
        return db.ACTIVE
    return (USERS.get(uid) or {}).get("status", db.PENDING)


def is_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    return (USERS.get(uid) or {}).get("role") == "admin"


def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


async def notify_owner(bot, text: str):
    """Notify owner about account failures — fire-and-forget."""
    try:
        await bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML", disable_notification=False)
    except Exception as e:
        log.warning("notify_owner failed: %s", e)


def _parse_email_account_input(text: str):
    """
    Parse admin input for email account.
    Supports:
      email password
      email:password
      label email password
      label=email=password
    Returns (label, email, password) or (None, None, None) on failure.
    """
    import re
    raw = text.strip()
    # Normalize separators: = -> space, : -> space, , -> space
    # But keep email's @ and dots
    # First try split by whitespace
    parts = re.split(r'[\s,]+', raw)
    # Remove empty
    parts = [p.strip().strip('=:\'"') for p in parts if p.strip()]
    if len(parts) == 2:
        email, password = parts
        if "@" in email and len(password) >= 3:
            label = email.split("@")[0][:16] + "_" + str(int(time.time()) % 10000)
            label = re.sub(r'[^a-zA-Z0-9_]', '_', label)
            return label, email, password
    elif len(parts) == 3:
        label, email, password = parts
        if "@" in email and len(password) >= 3:
            label = re.sub(r'[^a-zA-Z0-9_\-]', '_', label)[:32]
            return label, email, password
    # try colon/equal parsing like "label = email = password" or "email:password"
    # fallback: if text contains '=', split
    if "=" in raw:
        eq_parts = [p.strip() for p in raw.split("=")]
        eq_parts = [p for p in eq_parts if p]
        if len(eq_parts) == 2 and "@" in eq_parts[0]:
            email, password = eq_parts
            label = email.split("@")[0][:16] + "_" + str(int(time.time()) % 10000)
            label = re.sub(r'[^a-zA-Z0-9_]', '_', label)
            return label, email, password
        if len(eq_parts) == 3:
            label, email, password = eq_parts
            if "@" in email:
                label = re.sub(r'[^a-zA-Z0-9_\-]', '_', label)[:32]
                return label, email, password
    return None, None, None


# ---------- UI ----------
def status_text(s: UserState) -> str:
    r = RULES[s.model_type]
    p = get_persona(s.persona)
    think = "ON" if s.thinking else "OFF"
    if not r['supports_search']: search = "BLOCKED"
    elif s.search and not s.attached_files: search = "ON"
    else: search = "OFF"
    sess = (s.session_id[:8] + "…") if s.session_id else "none"

    lines = [
        f"🤖 <b>DeepSeek Bot</b> · <i>{p['emoji']} {p['name']}</i>",
        f"{r['emoji']} Mode: <b>{r['name']}</b>  |  🧠 Think: <b>{think}</b>  |  🌐 Search: <b>{search}</b>",
        f"🔊 Voice: <b>{'ON' if s.voice_reply else 'OFF'}</b> ({'♀' if s.tts_female else '♂'})  |  🔗 URL fetch: <b>{'ON' if s.auto_urls else 'OFF'}</b>",
        f"🆔 Session: <code>{sess}</code>",
    ]
    if s.attached_files:
        names = ", ".join(f[1] for f in s.attached_files)
        lines.append(f"📎 Attached: <b>{html.escape(names)}</b>")
    lines.append("")
    lines.append("👇 <i>Send a message, voice note, photo, file or URL — I handle them all.</i>")
    return "\n".join(lines)


def main_menu_kb(s: UserState, uid: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{'🟢' if s.model_type=='default' else '⚪'} Instant",
                              callback_data="mode:default"),
         InlineKeyboardButton(f"{'🟢' if s.model_type=='expert' else '⚪'} Expert",
                              callback_data="mode:expert"),
         InlineKeyboardButton(f"{'🟢' if s.model_type=='vision' else '⚪'} Vision",
                              callback_data="mode:vision")],
        [InlineKeyboardButton(f"🧠 Think: {'ON' if s.thinking else 'OFF'}",
                              callback_data="toggle:think"),
         InlineKeyboardButton(f"🌐 Search: {'ON' if s.search else 'OFF'}",
                              callback_data="toggle:search")],
        [InlineKeyboardButton(f"🔊 Voice: {'ON' if s.voice_reply else 'OFF'}",
                              callback_data="toggle:voice"),
         InlineKeyboardButton(f"👤 {'♀ Female' if s.tts_female else '♂ Male'}",
                              callback_data="toggle:gender"),
         InlineKeyboardButton(f"🔗 URL:{'ON' if s.auto_urls else 'OFF'}",
                              callback_data="toggle:urls")],
        [InlineKeyboardButton(f"🎭 Persona: {get_persona(s.persona)['name']}",
                              callback_data="personas:0")],
        [InlineKeyboardButton("🆕 New Chat", callback_data="cmd:new"),
         InlineKeyboardButton("📁 My Chats", callback_data="chats:0"),
         InlineKeyboardButton("📤 Export", callback_data="cmd:export")],
        [InlineKeyboardButton("📊 Stats", callback_data="cmd:stats"),
         InlineKeyboardButton("🗑 Delete", callback_data="cmd:delete_ask"),
         InlineKeyboardButton("💥 Wipe All", callback_data="cmd:wipe_ask")],
    ]
    if s.attached_files:
        rows.append([InlineKeyboardButton("📎 Detach Files", callback_data="cmd:detach")])
    rows.append([
        InlineKeyboardButton("❓ Help", callback_data="cmd:help"),
        InlineKeyboardButton("🔄 Refresh", callback_data="cmd:refresh"),
    ])
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin panel",
                                          callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def personas_kb(current: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for key, p in PERSONAS.items():
        label = f"{'✓ ' if key == current else ''}{p['emoji']} {p['name']}"
        row.append(InlineKeyboardButton(label, callback_data=f"persona:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Menu", callback_data="cmd:refresh")])
    return InlineKeyboardMarkup(rows)


def response_footer_kb(has_text: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if has_text:
        rows.append([
            InlineKeyboardButton("🔊 Speak", callback_data="rsp:speak"),
            InlineKeyboardButton("📄 File", callback_data="rsp:file"),
            InlineKeyboardButton("🔁 Regen", callback_data="rsp:regenmenu"),
        ])
        rows.append([
            InlineKeyboardButton("⚡ Quick actions", callback_data="rsp:actions"),
        ])
    rows.append([
        InlineKeyboardButton("🆕 New Chat", callback_data="cmd:new"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmd:refresh"),
        InlineKeyboardButton("❓ Help", callback_data="cmd:help"),
    ])
    return InlineKeyboardMarkup(rows)


def stop_kb() -> InlineKeyboardMarkup:
    """Shown while DeepSeek is generating (mirrors the app's stop button)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Stop", callback_data="rsp:stop")],
    ])


def regen_menu_kb() -> InlineKeyboardMarkup:
    """Regenerate options — mirrors the app's 'More concise' / 'Add details'."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Fresh retry", callback_data="rsp:regen"),
         InlineKeyboardButton("✂️ More concise", callback_data="rsp:regen_low")],
        [InlineKeyboardButton("📖 Add details", callback_data="rsp:regen_high")],
        [InlineKeyboardButton("🔙 Back", callback_data="rsp:back")],
    ])


def quick_actions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Translate → English", callback_data="qa:tr_en"),
         InlineKeyboardButton("🌐 Translate → Hindi", callback_data="qa:tr_hi")],
        [InlineKeyboardButton("📝 Summarize", callback_data="qa:summarize"),
         InlineKeyboardButton("🔄 Rephrase", callback_data="qa:rephrase")],
        [InlineKeyboardButton("💡 Explain simpler", callback_data="qa:explain"),
         InlineKeyboardButton("➕ Continue", callback_data="qa:continue")],
        [InlineKeyboardButton("🔙 Back", callback_data="rsp:back")],
    ])


def chats_kb(chats: list, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    rows = []
    start, end = page * per_page, page * per_page + per_page
    for i, c in enumerate(chats[start:end], start=start):
        title = (c.get('title') or 'Untitled')[:36]
        rows.append([InlineKeyboardButton(f"💬 {title}", callback_data=f"switch:{i}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"chats:{page-1}"))
    if end < len(chats): nav.append(InlineKeyboardButton("➡️", callback_data=f"chats:{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="cmd:refresh")])
    return InlineKeyboardMarkup(rows)


_YT_HELP_LINE = ("▶️ YouTube link — summary from transcript\n\n" if youtube_enabled() else
                 "▶️ YouTube link — ❌ host pe YT_PROXY chahiye (YouTube cloud IPs block karta hai)\n\n")

HELP_TEXT = (
"<b>🤖 DeepSeek Bot — Complete Guide</b>\n\n"
"<b>Input types (all supported):</b>\n"
"📝 Text — normal chat\n"
"🎤 Voice — Whisper transcribe → DeepSeek\n"
"🖼 Photo — OCR text extraction\n"
"📎 Document — upload + question caption\n"
"🔗 URL — auto fetch and summarize\n"
+ _YT_HELP_LINE +
"<b>Modes (V4.1 era):</b>\n"
"🚀 Instant — fast daily chat (search+files supported)\n"
"💎 Expert — deep reasoning for complex tasks (no search/files)\n"
"👁 Vision — image/document understanding\n\n"
"<b>Personas 🎭 (9 options):</b>\n"
"Default, Tutor, Coder, Dost, Writer, Translator, "
"Comedian, Scientist, Startup Coach, Health Info\n\n"
"<b>Response buttons (on every reply):</b>\n"
"🔊 <b>Speak</b> — listen as a voice message\n"
"📄 <b>File</b> — download as a .md file\n"
"🔁 <b>Regen</b> — retry menu: fresh answer / ✂️ more concise / 📖 more details\n"
"⚡ <b>Quick actions</b> — Translate/Summarize/Rephrase/Explain/Continue\n"
"⏹ <b>Stop</b> — appears while generating (also /cancel)\n\n"
"<b>Toggles:</b>\n"
"🧠 Think — reasoning chain\n"
"🌐 Search — real-time web\n"
"🔊 Voice-reply — also send every reply as audio\n"
"👤 Male/Female voice\n"
"🔗 URL fetch — automatic vs manual\n\n"
"<b>Chat mgmt:</b>\n"
"🆕 New · 📁 My Chats · 🗑 Delete · 💥 Wipe · 📤 Export .md\n\n"
"<b>Long responses:</b>\n"
"3800–10000 chars → multi-bubble\n"
"10000+ → auto .md file\n\n"
"<b>Privacy:</b>\n"
"Your chats are private — no other user can see them.\n"
"All conversations are deleted automatically every night.\n\n"
"<b>Slash commands:</b>\n"
"/start — menu\n/help — this page\n/cancel — stop the running task\n\n"
"<i>💡 Tip: just type normally — input type is detected automatically.</i>"
)


# ---------- Utility ----------
async def send_menu(target, s: UserState, edit: bool = False, uid: int = 0):
    text = status_text(s)
    kb = main_menu_kb(s, uid)
    try:
        if edit:
            await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            msg = target.message if hasattr(target, "message") and target.message else target
            await msg.reply_html(text, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("send_menu: %s", e)


def _ticker_view(think_buf: str) -> str:
    words = think_buf.split()
    tail = " ".join(words[-THINK_TICKER_WORDS:]).replace("\n", " ")
    if len(tail) > 240: tail = "…" + tail[-240:]
    return f"🤔 <i>{html.escape(tail) if tail else '…'}</i> ▍"


async def record_history(uid: int, role: str, text: str):
    """Store one turn. Rows carry a 24h TTL and are wiped nightly."""
    try:
        await db.add_turn(uid, role, text)
    except Exception as e:
        log.warning("history write failed: %s", e)


# ---------- Rate limiting ----------
def rate_check(uid: int) -> Optional[int]:
    """
    Sliding-window limiter. Returns None if allowed, else seconds to wait.

    Protects the single shared DeepSeek account: bursts of requests look like
    abuse upstream and can get the session token throttled or banned.
    """
    if RATE_LIMIT <= 0:
        return None
    now = time.time()
    hits = _RATE.setdefault(uid, [])
    cutoff = now - RATE_WINDOW
    while hits and hits[0] < cutoff:
        hits.pop(0)
    if len(hits) >= RATE_LIMIT:
        return max(1, int(hits[0] + RATE_WINDOW - now) + 1)
    hits.append(now)
    return None


# ---------- Slash commands ----------
async def gate(update: Update) -> bool:
    """
    Access control for every entry point.

    Loads/creates the user, then decides:
      • owner/admin  -> always allowed
      • blocked      -> silently refused
      • active       -> allowed
      • pending      -> allowed only when access mode is open, otherwise the
                        owner gets an approval request
    Returns True when the update may proceed.
    """
    u = update.effective_user
    if u is None:
        return False
    uid = u.id
    doc = USERS.get(uid)
    if doc is None:
        doc = await load_user(uid, username=u.username or "",
                              first_name=u.first_name or "")
    else:
        asyncio.create_task(db.upsert_user(
            uid, username=u.username or "", first_name=u.first_name or ""))

    if uid == OWNER_ID:
        if doc.get("status") != db.ACTIVE or doc.get("role") != "admin":
            await db.set_user_field(uid, status=db.ACTIVE, role="admin")
            doc["status"], doc["role"] = db.ACTIVE, "admin"
        return True

    status = doc.get("status", db.PENDING)
    if status == db.BLOCKED:
        return False
    if status == db.ACTIVE:
        return True

    # pending
    cfg = await db.get_config()
    if cfg.get("access_mode") == db.MODE_OPEN:
        await db.set_user_status(uid, db.ACTIVE)
        doc["status"] = db.ACTIVE
        USERS[uid] = doc
        return True

    await _request_approval(update, u)
    return False


async def _request_approval(update: Update, u) -> None:
    """Tell the user they need approval and ping the owner once."""
    try:
        await update.effective_message.reply_html(
            "🔒 <b>This bot is invite-only</b>\n\n"
            "Your request has been sent to the admin. "
            "You'll get a message here once you're approved.")
    except Exception:
        pass
    if PENDING_NOTIFIED.get(u.id):
        return
    PENDING_NOTIFIED[u.id] = True
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"adm:approve:{u.id}"),
        InlineKeyboardButton("🚫 Block", callback_data=f"adm:block:{u.id}"),
    ]])
    try:
        await update.get_bot().send_message(
            chat_id=OWNER_ID,
            text=("👋 <b>New access request</b>\n\n"
                  f"• Name: {html.escape(u.first_name or '—')}\n"
                  f"• Username: @{html.escape(u.username or '—')}\n"
                  f"• ID: <code>{u.id}</code>"),
            parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.warning("approval ping failed: %s", e)


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # /start <code> — someone followed an invite link
    code = (ctx.args[0].strip() if getattr(ctx, "args", None) else "")
    if code and uid != OWNER_ID:
        if await _try_redeem(update, ctx, uid, code):
            return   # _try_redeem already replied on failure

    if not await gate(update):
        return
    await send_menu(update, get_state(uid), uid=uid)


async def _try_redeem(update: Update, ctx, uid: int, code: str) -> bool:
    """
    Redeem an invite code. Returns True when the caller should stop
    (the code was bad and the user has been told why).
    """
    u = update.effective_user
    if uid not in USERS:
        await load_user(uid, username=u.username or "",
                        first_name=u.first_name or "")

    # Already in? The link is just a no-op.
    if user_status(uid) == db.ACTIVE:
        return False
    if user_status(uid) == db.BLOCKED:
        return True   # stay silent for blocked users

    result = await db.redeem_invite(code, uid)
    if result == "ok":
        await db.set_user_status(uid, db.ACTIVE)
        USERS.setdefault(uid, {})["status"] = db.ACTIVE
        PENDING_NOTIFIED.pop(uid, None)
        await update.message.reply_html(
            "🎉 <b>Welcome! Your invite has been accepted.</b>\n\n"
            "You now have full access. Here's your menu:")
        try:
            await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=("🔗 <b>Invite link used</b>\n\n"
                      f"• Name: {html.escape(u.first_name or '—')}\n"
                      f"• Username: @{html.escape(u.username or '—')}\n"
                      f"• ID: <code>{uid}</code>"),
                parse_mode="HTML", disable_notification=True)
        except Exception:
            pass
        return False   # fall through so the menu is sent

    reason = {
        "invalid": "This invite link is not valid.",
        "revoked": "This invite link has been revoked.",
        "expired": "This invite link has expired.",
        "used_up": "This invite link has already been used up.",
    }.get(result, "This invite link cannot be used.")
    await update.message.reply_html(
        f"❌ <b>{reason}</b>\n\n"
        "<i>Ask the admin for a fresh link.</i>")
    return True


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await load_user(uid)
    if not is_admin(uid):
        return
    await update.message.reply_html(await adm.stats_text(),
                                    reply_markup=adm.admin_menu_kb())

async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stop whatever DeepSeek request is currently streaming."""
    uid = update.effective_user.id
    if not await gate(update): return
    if PENDING_INPUT.pop(uid, None):
        await update.message.reply_text("Cancelled.")
        return
    if user_lock(uid).locked():
        CANCELLED.add(uid)
        await update.message.reply_text("⏹ Stopping the current request…")
    else:
        await update.message.reply_text("Nothing is running right now.")


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update): return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="cmd:refresh")]])
    await update.message.reply_html(HELP_TEXT, reply_markup=kb)


# ---------- Button handler ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    if not await gate(update):
        await q.answer("You don't have access to this bot.", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    s = get_state(q.from_user.id)

    # --- admin panel (owner/admins only) ---
    if data.startswith("adm:"):
        if not is_admin(q.from_user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await handle_admin(q, ctx, data)
        return

    # --- mode/toggles ---
    if data.startswith("mode:"):
        m = data.split(":", 1)[1]
        if m in RULES:
            s.model_type = m
            r = RULES[m]
            if not r['supports_search']: s.search = False
            if not r['supports_files']: s.attached_files = []
            await save_settings(q.from_user.id)
        await send_menu(q, s, edit=True, uid=q.from_user.id); return

    if data == "toggle:think":
        s.thinking = not s.thinking; await save_settings(q.from_user.id); await send_menu(q, s, edit=True, uid=q.from_user.id); return
    if data == "toggle:voice":
        s.voice_reply = not s.voice_reply; await save_settings(q.from_user.id); await send_menu(q, s, edit=True, uid=q.from_user.id); return
    if data == "toggle:gender":
        s.tts_female = not s.tts_female; await save_settings(q.from_user.id); await send_menu(q, s, edit=True, uid=q.from_user.id); return
    if data == "toggle:urls":
        s.auto_urls = not s.auto_urls; await save_settings(q.from_user.id); await send_menu(q, s, edit=True, uid=q.from_user.id); return

    if data == "toggle:search":
        if not RULES[s.model_type]['supports_search']:
            await q.answer("Search blocked in this mode", show_alert=True); return
        if s.attached_files:
            await q.answer("Files attached — detach first", show_alert=True); return
        s.search = not s.search; await save_settings(q.from_user.id); await send_menu(q, s, edit=True, uid=q.from_user.id); return

    # --- personas ---
    if data.startswith("personas:"):
        try:
            await q.edit_message_text(
                "🎭 <b>Choose a persona:</b>\n<i>Ye AI ka style change karega.</i>",
                parse_mode="HTML", reply_markup=personas_kb(s.persona))
        except BadRequest: pass
        return

    if data.startswith("persona:"):
        key = data.split(":", 1)[1]
        if key in PERSONAS:
            s.persona = key; await save_settings(q.from_user.id)
            await q.answer(f"Persona: {PERSONAS[key]['name']}")
        await send_menu(q, s, edit=True, uid=q.from_user.id); return

    # --- commands ---
    if data in ("cmd:refresh", "cmd:menu"):
        try: await send_menu(q, s, edit=True, uid=q.from_user.id)
        except: await q.message.reply_html(status_text(s), reply_markup=main_menu_kb(s, q.from_user.id))
        return

    if data == "cmd:help":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu",
                                                          callback_data="cmd:refresh")]])
        try:
            await q.edit_message_text(HELP_TEXT, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            await q.message.reply_html(HELP_TEXT, reply_markup=kb)
        return

    if data == "cmd:detach":
        s.attached_files = []; await save_session(q.from_user.id)
        await q.answer("Detached"); await send_menu(q, s, edit=True, uid=q.from_user.id); return

    if data == "cmd:new":
        sid = await ds_op(q.from_user.id, 'create_chat')
        if sid:
            s.session_id = sid; s.parent_msg_id = None; s.attached_files = []
            await save_session(q.from_user.id); await q.answer("New chat started")
        else: await q.answer("Failed", show_alert=True)
        try: await send_menu(q, s, edit=True, uid=q.from_user.id)
        except: await q.message.reply_html(status_text(s), reply_markup=main_menu_kb(s, q.from_user.id))
        return

    if data == "cmd:stats":
        stats = (
            f"📊 <b>Your stats</b>\n\n"
            f"Messages sent: <b>{s.msg_count}</b>\n"
            f"Chars sent: <b>{s.total_chars_in:,}</b>\n"
            f"Chars received: <b>{s.total_chars_out:,}</b>\n"
            f"Persona: <b>{get_persona(s.persona)['name']}</b>\n"
            f"Mode: <b>{RULES[s.model_type]['name']}</b>\n"
            f"Stored turns: <b>{len(await db.get_history(q.from_user.id, limit=500))}</b> "
            f"<i>(cleared nightly)</i>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="cmd:refresh")]])
        try: await q.edit_message_text(stats, parse_mode="HTML", reply_markup=kb)
        except BadRequest: pass
        return

    if data == "cmd:export":
        hist = await db.get_history(q.from_user.id, limit=500)
        if not hist:
            await q.answer("No history yet", show_alert=True); return
        async with Progress(ctx.bot, q.message.chat_id, "📤 Chat export",
                            steps=["Building Markdown", "Sending file"]) as p:
            await p.step(0, f"{len(hist)} messages")
            content = "# DeepSeek Chat Export\n\n"
            for h in hist:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h['ts']))
                role = "👤 You" if h['role'] == 'user' else "🤖 Bot"
                content += f"### {role} — {ts}\n\n{h['text']}\n\n---\n\n"
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md",
                                              encoding="utf-8") as f:
                f.write(content); path = f.name
            try:
                await p.step(1, f"{len(content):,} chars")
                with open(path, "rb") as fh:
                    await ctx.bot.send_document(
                        chat_id=q.message.chat_id, document=fh,
                        filename=f"chat_export_{int(time.time())}.md",
                        caption=f"📤 Exported {len(hist)} messages")
            finally:
                try: os.unlink(path)
                except: pass
        return

    # --- chats list ---
    if data.startswith("chats:"):
        page = int(data.split(":", 1)[1])
        await q.answer("Loading…")
        chats = await ds_op(q.from_user.id, 'list_chats') or []
        ctx.user_data['chat_list'] = chats
        if not chats:
            await q.answer("No chats found", show_alert=True); return
        text = f"<b>📁 Your DeepSeek Chats</b>\nTotal: {len(chats)} · Page {page+1}"
        try:
            await q.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=chats_kb(chats, page))
        except BadRequest:
            await q.message.reply_html(text, reply_markup=chats_kb(chats, page))
        return

    if data.startswith("switch:"):
        idx = int(data.split(":", 1)[1])
        chats = ctx.user_data.get('chat_list') or await ds_op(q.from_user.id, 'list_chats') or []
        if idx >= len(chats):
            await q.answer("Out of range", show_alert=True); return
        c = chats[idx]
        s.session_id = c['id']
        hist = await ds_op(q.from_user.id, 'get_history', c['id'])
        last = hist[1] if hist else None
        s.parent_msg_id = last
        m = c.get('model_type', 'default')
        if m in RULES: s.model_type = m
        await save_session(q.from_user.id); await save_settings(q.from_user.id)
        await q.answer(f"Switched: {(c.get('title') or 'Untitled')[:25]}")
        await send_menu(q, s, edit=True, uid=q.from_user.id); return

    if data == "cmd:delete_ask":
        if not s.session_id:
            await q.answer("No active session", show_alert=True); return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data="cmd:delete_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="cmd:refresh"),
        ]])
        try:
            await q.edit_message_text(
                f"🗑 Delete current session?\n<code>{s.session_id}</code>",
                parse_mode="HTML", reply_markup=kb)
        except BadRequest: pass
        return

    if data == "cmd:delete_yes":
        if s.session_id:
            ok = await ds_op(q.from_user.id, 'delete_chat', s.session_id)
            if ok:
                s.session_id = None; s.parent_msg_id = None; s.attached_files = []
                await save_session(q.from_user.id); await q.answer("Deleted")
            else: await q.answer("Failed", show_alert=True)
        await send_menu(q, s, edit=True, uid=q.from_user.id); return

    if data == "cmd:wipe_ask":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💥 YES", callback_data="cmd:wipe_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="cmd:refresh"),
        ]])
        try:
            await q.edit_message_text(
                "⚠️ <b>ALL cloud chats will be permanently deleted.</b>",
                parse_mode="HTML", reply_markup=kb)
        except BadRequest: pass
        return

    if data == "cmd:wipe_yes":
        ok = await ds_op(q.from_user.id, 'delete_all_chats')
        s.session_id = None; s.parent_msg_id = None; s.attached_files = []
        await save_session(q.from_user.id)
        await db.clear_history(q.from_user.id)
        await q.answer("Wiped" if ok else "Failed", show_alert=True)
        await send_menu(q, s, edit=True, uid=q.from_user.id); return

    # --- Response actions ---
    if data == "rsp:speak":
        last = LAST.get(q.from_user.id)
        if not last or not last.get('raw_answer'):
            await q.answer("No response cached", show_alert=True); return
        await q.answer("Generating voice…")
        await _send_tts(ctx, q.message.chat_id, last['raw_answer'], s.tts_female)
        return

    if data == "rsp:file":
        last = LAST.get(q.from_user.id)
        if not last or not last.get('raw_answer'):
            await q.answer("No response cached", show_alert=True); return
        await q.answer("Sending file…")
        async with Progress(ctx.bot, q.message.chat_id, "📄 Building file",
                            steps=["Building Markdown", "Sending"]) as p:
            await p.step(0, f"{len(last['raw_answer']):,} chars")
            await p.step(1)
            await _send_response_file(ctx, q.message.chat_id,
                                       last.get('prompt', ''), last['raw_answer'])
        return

    if data == "rsp:regen":
        last = LAST.get(q.from_user.id)
        if not last or not last.get('prompt'):
            await q.answer("Nothing to regenerate", show_alert=True); return
        await q.answer("Regenerating…")
        s.parent_msg_id = last.get('parent_before')
        await save_session(q.from_user.id)
        await _process_prompt_chat(
            ctx=ctx, chat_id=q.message.chat_id, user_id=q.from_user.id,
            prompt=last['prompt'], reply_to_msg_id=None, is_regen=True,
        )
        return

    if data == "rsp:regenmenu":
        if not LAST.get(q.from_user.id):
            await q.answer("Nothing to regenerate", show_alert=True); return
        try:
            await q.edit_message_reply_markup(reply_markup=regen_menu_kb())
        except BadRequest: pass
        return

    if data in ("rsp:regen_low", "rsp:regen_high"):
        # Mirrors the DeepSeek app's regenerate verbosity options:
        #   low  = "More concise"  high = "Add details"
        # (the web API has no verbosity param, so it is emulated via style
        # instructions appended to the original prompt)
        last = LAST.get(q.from_user.id)
        if not last or not last.get('prompt'):
            await q.answer("Nothing to regenerate", show_alert=True); return
        await q.answer("Regenerating…")
        s.parent_msg_id = last.get('parent_before')
        await save_session(q.from_user.id)
        style = ("\n\n(IMPORTANT: Answer much more concisely than before — "
                 "shorter, only the key points.)"
                 if data == "rsp:regen_low" else
                 "\n\n(IMPORTANT: Answer in more depth than before — add "
                 "details, examples and explanations.)")
        await _process_prompt_chat(
            ctx=ctx, chat_id=q.message.chat_id, user_id=q.from_user.id,
            prompt=last['prompt'] + style, reply_to_msg_id=None, is_regen=True,
        )
        return

    if data == "rsp:stop":
        if user_lock(q.from_user.id).locked():
            CANCELLED.add(q.from_user.id)
            await q.answer("Stopping…")
        else:
            await q.answer("Nothing is running")
        return

    if data == "rsp:actions":
        try:
            await q.edit_message_reply_markup(reply_markup=quick_actions_kb())
        except BadRequest: pass
        return

    if data == "rsp:back":
        try:
            await q.edit_message_reply_markup(reply_markup=response_footer_kb(True))
        except BadRequest: pass
        return

    if data.startswith("qa:"):
        last = LAST.get(q.from_user.id)
        if not last or not last.get('raw_answer'):
            await q.answer("No response", show_alert=True); return
        action = data.split(":", 1)[1]
        prompts = {
            "tr_en": f"Translate the following to natural English:\n\n{last['raw_answer']}",
            "tr_hi": f"Translate the following to natural Hindi (Devanagari):\n\n{last['raw_answer']}",
            "summarize": f"Summarize the following in 3-5 bullet points:\n\n{last['raw_answer']}",
            "rephrase": f"Rephrase the following in different words, same meaning:\n\n{last['raw_answer']}",
            "explain": f"Explain the following in simpler terms, like I'm a beginner:\n\n{last['raw_answer']}",
            "continue": f"Continue where this left off:\n\n{last['raw_answer']}",
        }
        prompt = prompts.get(action)
        if not prompt:
            await q.answer("Unknown action"); return
        await q.answer(f"Running {action}…")
        await _process_prompt_chat(
            ctx=ctx, chat_id=q.message.chat_id, user_id=q.from_user.id,
            prompt=prompt, reply_to_msg_id=None, is_quick_action=True,
        )
        return


# ---------- Admin panel ----------
async def _bot_username(ctx) -> str:
    """Cached bot username, needed to build t.me invite links."""
    uname = ctx.bot_data.get("bot_username")
    if not uname:
        me = await ctx.bot.get_me()
        uname = me.username
        ctx.bot_data["bot_username"] = uname
    return uname


async def _safe_edit_q(q, text: str, kb=None):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            try:
                await q.message.reply_html(text, reply_markup=kb)
            except Exception:
                pass


async def handle_admin(q, ctx, data: str):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    admin_id = q.from_user.id

    if action == "menu":
        await _safe_edit_q(q, await adm.stats_text(), adm.admin_menu_kb())
        return

    if action == "stats":
        await _safe_edit_q(q, await adm.stats_text(), adm.admin_menu_kb())
        return

    # ----- users -----
    if action == "users":
        status = parts[2] if len(parts) > 2 else db.ACTIVE
        page = int(parts[3]) if len(parts) > 3 else 0
        total = await db.count_users(status)
        users = await db.list_users(status, skip=page * adm.PAGE,
                                    limit=adm.PAGE)
        label = {"active": "Active", "pending": "Pending",
                 "blocked": "Blocked"}.get(status, status)
        txt = (f"👥 <b>{label} users</b> — {total} total\n\n"
               + ("<i>Nobody here yet.</i>" if not users else
                  "<i>Tap a user to manage them.</i>"))
        await _safe_edit_q(q, txt, adm.users_kb(users, status, page, total))
        return

    if action == "u":
        uid = int(parts[2])
        txt, st = await adm.user_detail_text(uid)
        await _safe_edit_q(q, txt, adm.user_detail_kb(uid, st))
        return

    if action in ("approve", "block"):
        uid = int(parts[2])
        new_status = db.ACTIVE if action == "approve" else db.BLOCKED
        await db.set_user_status(uid, new_status)
        if uid in USERS:
            USERS[uid]["status"] = new_status
        PENDING_NOTIFIED.pop(uid, None)
        await q.answer("Approved" if action == "approve" else "Blocked")
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text=("✅ <b>You're approved!</b>\n\nSend /start to begin."
                      if action == "approve" else
                      "🚫 <b>Your access has been revoked.</b>"),
                parse_mode="HTML")
        except Exception:
            pass
        txt, st = await adm.user_detail_text(uid)
        await _safe_edit_q(q, txt, adm.user_detail_kb(uid, st))
        return

    if action == "chat":
        uid = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        txt, more = await adm.user_chat_text(uid, page)
        rows = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                "⬅️", callback_data=f"adm:chat:{uid}:{page-1}"))
        if more:
            nav.append(InlineKeyboardButton(
                "➡️", callback_data=f"adm:chat:{uid}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("🔙 User",
                                          callback_data=f"adm:u:{uid}")])
        await _safe_edit_q(q, txt, InlineKeyboardMarkup(rows))
        return

    if action == "clear":
        uid = int(parts[2])
        n = await db.clear_history(uid)
        await q.answer(f"Cleared {n} turns")
        txt, st = await adm.user_detail_text(uid)
        await _safe_edit_q(q, txt, adm.user_detail_kb(uid, st))
        return

    if action == "dm":
        uid = int(parts[2])
        PENDING_INPUT[admin_id] = {"kind": "dm", "target": uid}
        await _safe_edit_q(
            q, f"✉️ <b>Send the message for user <code>{uid}</code></b>\n\n"
               "<i>Type it now, or /cancel to abort.</i>")
        return

    # ----- invites -----
    if action == "inv":
        sub = parts[2] if len(parts) > 2 else "menu"

        if sub == "menu":
            await _safe_edit_q(q, await adm.invite_menu_text(),
                               adm.invite_menu_kb())
            return

        if sub == "new":
            max_uses = int(parts[3]) if len(parts) > 3 else 1
            hours = int(parts[4]) if len(parts) > 4 else 0
            code = secrets.token_urlsafe(9)
            await db.create_invite(code, admin_id, max_uses=max_uses,
                                   expires_in_h=hours)
            uname = await _bot_username(ctx)
            inv = await db.get_invite(code)
            await _safe_edit_q(q, await adm.invite_detail_text(inv, uname),
                               adm.invite_created_kb(
                                   adm.invite_link(uname, code), code))
            return

        if sub == "list":
            invites = await db.list_invites()
            live = [i for i in invites if db.invite_state(i) == "ok"]
            if not live:
                await _safe_edit_q(
                    q, "📋 <b>No active invite links.</b>\n\n"
                       "<i>Create one from the invite menu.</i>",
                    adm.invite_menu_kb())
                return
            await _safe_edit_q(
                q, f"📋 <b>Active invite links</b> — {len(live)}\n\n"
                   "<i>Tap one to see or revoke it.</i>",
                adm.invite_list_kb(live))
            return

        if sub == "show":
            code = ":".join(parts[3:])
            inv = await db.get_invite(code)
            if not inv:
                await q.answer("Link not found", show_alert=True)
                return
            uname = await _bot_username(ctx)
            await _safe_edit_q(q, await adm.invite_detail_text(inv, uname),
                               adm.invite_created_kb(
                                   adm.invite_link(uname, code), code))
            return

        if sub == "rv":
            code = ":".join(parts[3:])
            await db.revoke_invite(code)
            await q.answer("Link revoked")
            await _safe_edit_q(q, await adm.invite_menu_text(),
                               adm.invite_menu_kb())
            return

        if sub == "byid":
            PENDING_INPUT[admin_id] = {"kind": "adduser"}
            await _safe_edit_q(
                q,
                "🆔 <b>Add a user by Telegram ID</b>\n\n"
                "Send the numeric ID now — for example <code>123456789</code>.\n"
                "You can send several at once, separated by spaces or commas.\n\n"
                "<i>They can find their ID via @userinfobot. "
                "/cancel to abort.</i>")
            return

    # ----- access mode -----
    if action == "mode":
        cfg = await db.get_config()
        cur = cfg.get("access_mode", db.MODE_INVITE)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                ("🔒 " if cur == db.MODE_INVITE else "") + "Invite only",
                callback_data="adm:setmode:invite")],
            [InlineKeyboardButton(
                ("🌍 " if cur == db.MODE_OPEN else "") + "Open to everyone",
                callback_data="adm:setmode:open")],
            [InlineKeyboardButton("🔙 Admin menu", callback_data="adm:menu")],
        ])
        await _safe_edit_q(
            q,
            "🚪 <b>Access mode</b>\n\n"
            "🔒 <b>Invite only</b> — new people wait for your approval.\n"
            "🌍 <b>Open</b> — anyone who presses /start gets in immediately.\n\n"
            f"Currently: <b>{'Invite only' if cur == db.MODE_INVITE else 'Open'}</b>",
            kb)
        return

    if action == "setmode":
        mode = db.MODE_OPEN if parts[2] == "open" else db.MODE_INVITE
        await db.set_config(access_mode=mode)
        await q.answer("Access mode updated")
        await _safe_edit_q(q, await adm.stats_text(), adm.admin_menu_kb())
        return

    # ----- NEW: DeepSeek Accounts -----
    if action in ("accounts", "keys"):
        await _safe_edit_q(q, await adm.accounts_text(),
                           adm.accounts_kb(await db.list_tokens()))
        return

    if action == "acc":
        sub = parts[2] if len(parts) > 2 else ""
        # adm:acc:add_email
        if sub == "add_email":
            PENDING_INPUT[admin_id] = {"kind": "add_email_account"}
            await _safe_edit_q(
                q,
                "📧 <b>Add DeepSeek Email Account</b>\n\n"
                "Send in one message:\n"
                "<code>email password</code>\n"
                "or <code>label email password</code>\n\n"
                "Examples:\n"
                "<code>user@example.com mypass123</code>\n"
                "<code>myacc2 myemail@gmail.com mypass123</code>\n\n"
                "Bot email se login karke token auto-fetch karega.\n"
                "Token expire pe bot <i>auto-refresh</i> karega, aapko manual kuch nahi karna.\n\n"
                "<b>Note:</b> 1 account = 1 simultaneous user. 10 accounts = 10 users ek sath.\n"
                "Queue me <i>Analysing your request...</i> dikhega line-by-line.\n\n"
                "<i>/cancel to abort.</i>")
            return
        if sub == "refresh_all":
            await q.answer("Checking all accounts…")
            try:
                async def notify(lbl, email, err):
                    try:
                        await ctx.bot.send_message(chat_id=admin_id, text=f"⚠️ <b>Account failed:</b> <code>{html.escape(lbl)}</code> ({html.escape(email)})\n<i>{html.escape(err[:200])}</i>", parse_mode="HTML")
                    except: pass
                res = await POOL.health_check_all(notify_callback=notify)
                txt = (f"🔄 <b>Health check done</b>\n\n"
                       f"• Total: <b>{res['total']}</b>\n"
                       f"• Healthy: <b>{res['healthy']}</b>\n"
                       f"• Refreshed: <b>{res['refreshed']}</b>\n"
                       f"• Failed: <b>{res['failed']}</b>\n")
                if res['errors']:
                    txt += "\n<b>Errors:</b>\n" + "\n".join(f"• {html.escape(e[:120])}" for e in res['errors'][:5])
                await _safe_edit_q(q, txt + "\n\n" + await adm.accounts_text(), adm.accounts_kb(await db.list_tokens()))
            except Exception as e:
                await _safe_edit_q(q, f"❌ Health check failed: {html.escape(str(e)[:200])}", adm.accounts_kb(await db.list_tokens()))
            return
        if sub == "pool":
            await _safe_edit_q(q, await adm.pool_status_text(), InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh All", callback_data="adm:acc:refresh_all")],
                [InlineKeyboardButton("🔙 Accounts", callback_data="adm:accounts")]
            ]))
            return
        # adm:acc:<label> -> show detail
        if sub and sub not in ("add_email", "refresh", "del", "refresh_all", "pool"):
            label = ":".join(parts[2:])
            # If label contains colon from original label, handle correctly: parts[2] is label without colon? labels don't contain colon, so ok
            # Check if it's detail view
            if await db.get_token_doc(label):
                await _safe_edit_q(q, await adm.account_detail_text(label), adm.account_detail_kb(label))
                return
        # adm:acc:refresh:<label>
        if sub == "refresh":
            label = ":".join(parts[3:])
            await q.answer("Refreshing…")
            try:
                success, msg = await POOL.refresh_account(label)
                if success:
                    await q.answer("Refreshed ✅")
                    await _safe_edit_q(q, f"✅ <b>Refreshed:</b> <code>{html.escape(label)}</code>\n{html.escape(msg)}\n\n" + await adm.account_detail_text(label), adm.account_detail_kb(label))
                else:
                    await _safe_edit_q(q, f"❌ <b>Refresh failed:</b> <code>{html.escape(label)}</code>\n<i>{html.escape(msg)}</i>\n\n" + await adm.account_detail_text(label), adm.account_detail_kb(label))
                    # notify admin
                    try:
                        await ctx.bot.send_message(chat_id=admin_id, text=f"⚠️ Refresh failed for <code>{html.escape(label)}</code>\n<i>{html.escape(msg)}</i>", parse_mode="HTML")
                    except: pass
            except Exception as e:
                await _safe_edit_q(q, f"❌ Error: {html.escape(str(e)[:200])}", adm.account_detail_kb(label))
            return
        if sub == "del":
            label = ":".join(parts[3:])
            await db.remove_token(label)
            n = await POOL.reload()
            await q.answer(f"Removed · pool = {n}")
            await _safe_edit_q(q, await adm.accounts_text(),
                               adm.accounts_kb(await db.list_tokens()))
            return

    # ----- keys (legacy) -----
    if action == "keys":
        await _safe_edit_q(q, await adm.accounts_text(),
                           adm.accounts_kb(await db.list_tokens()))
        return

    if action == "key":
        which = parts[2] if len(parts) > 2 else ""
        if which == "add":
            PENDING_INPUT[admin_id] = {"kind": "addkey"}
            await _safe_edit_q(
                q,
                "🔑 <b>Add a DeepSeek key (manual token)</b>\n\n"
                "Send it as:\n<code>label = token</code>\n\n"
                "Example:\n<code>acct2 = abc123...</code>\n\n"
                "Tip: Email account better hai — expire pe auto-refresh. Use <b>➕ Add Email Account</b> instead.\n"
                "<i>Each key/account adds one more simultaneous user. "
                "/cancel to abort.</i>")
            return
        label = ":".join(parts[2:])
        # If it's actually an account label, show account detail
        if await db.get_token_doc(label):
            await _safe_edit_q(q, await adm.account_detail_text(label), adm.account_detail_kb(label))
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Remove this key",
                                  callback_data=f"adm:acc:del:{label}")],
            [InlineKeyboardButton("🔙 Accounts", callback_data="adm:accounts")],
        ])
        await _safe_edit_q(q, f"🔑 <b>{html.escape(label)}</b>\n\n"
                              "<i>Removing a key reduces how many people can "
                              "chat at the same time.</i>", kb)
        return

    if action == "keydel":
        label = ":".join(parts[2:])
        await db.remove_token(label)
        n = await POOL.reload()
        await q.answer(f"Removed · pool = {n}")
        await _safe_edit_q(q, await adm.accounts_text(),
                           adm.accounts_kb(await db.list_tokens()))
        return

    # ----- broadcast -----
    if action == "bc" and len(parts) > 2 and parts[2] == "ask":
        n = await db.count_users(db.ACTIVE)
        PENDING_INPUT[admin_id] = {"kind": "broadcast"}
        await _safe_edit_q(
            q,
            f"📣 <b>Broadcast to {n} active user(s)</b>\n\n"
            "Send the message now. HTML formatting is supported.\n"
            "<i>/cancel to abort.</i>")
        return

async def handle_admin_input(update: Update, ctx, pending: dict) -> bool:
    """Consume a typed admin input (key / broadcast / DM). Returns True if used."""
    uid = update.effective_user.id
    text_raw = (update.message.text or "").strip()
    kind = pending.get("kind")
    PENDING_INPUT.pop(uid, None)

    if kind == "add_email_account":
        label, email, password = _parse_email_account_input(text_raw)
        if not label or not email or not password:
            await update.message.reply_html(
                "❌ <b>Galat format.</b>\n\n"
                "Use: <code>email password</code> ya <code>label email password</code>\n"
                "Example: <code>user@example.com mypass123</code>\n"
                "Ya: <code>myacc myemail@gmail.com mypass</code>")
            return True
        # Check duplicate label
        existing = await db.get_token_doc(label)
        if existing:
            await update.message.reply_html(f"❌ Label <b>{label}</b> already exists. Try different label.")
            return True
        await update.message.reply_html(f"⏳ <b>Logging in {email}...</b>\n<i>DeepSeek se token fetch kar raha hu, thoda wait...</i>")
        try:
            token, reason = await asyncio.to_thread(login_with_credentials_ex, email, password)
        except Exception as e:
            await update.message.reply_html(f"❌ Login exception: <code>{html.escape(str(e)[:200])}</code>")
            return True
        if not token:
            if reason == "bot_blocked":
                await update.message.reply_html(
                    f"🛡 <b>DeepSeek ne server ka login request block kiya</b>\n"
                    f"Email: <code>{html.escape(email)}</code>\n\n"
                    "DeepSeek ka anti-bot cloud-server IPs (Render etc.) pe challenge "
                    "deta hai — ye credentials ka problem <b>nahi</b> hai.\n\n"
                    "<b>✅ Pakka fix — Add Token (manual):</b>\n"
                    "1. Apne phone/PC browser me <code>chat.deepseek.com</code> pe login karo\n"
                    "2. Browser me DevTools/Console kholo aur type karo:\n"
                    "<code>localStorage.getItem(\"userToken\")</code>\n"
                    "3. Jo lambi string aaye usko copy karo\n"
                    "4. Admin panel → ➕ <b>Add Token (manual)</b> → paste karo\n\n"
                    "<i>Ya Render pe <b>DS_PROXY</b> env var me residential proxy "
                    "daalo, phir email login bhi chalega.</i>")
            elif reason == "api_changed":
                await update.message.reply_html(
                    f"❌ <b>Login failed for {html.escape(email)}</b>\n"
                    "DeepSeek ne login API change kar di hai — bot update chahiye. "
                    "Filhal ➕ <b>Add Token (manual)</b> use karo.")
            else:
                await update.message.reply_html(
                    f"❌ <b>Login failed for {html.escape(email)}</b>\n"
                    f"Reason: <code>{html.escape(str(reason)[:200])}</code>\n"
                    "<i>Wrong email/password ho sakta hai, ya ➕ Add Token (manual) use karo.</i>")
            # Notify owner about failure
            try:
                await ctx.bot.send_message(
                    chat_id=OWNER_ID,
                    text=(f"⚠️ <b>Account add failed</b>\nEmail: <code>{html.escape(email)}</code>\n"
                          f"Reason: <code>{html.escape(str(reason)[:150])}</code>"),
                    parse_mode="HTML")
            except Exception: pass
            return True
        ok = await db.add_email_account(label, email, password, token)
        if not ok:
            await update.message.reply_html(f"❌ DB error — label <b>{html.escape(label)}</b> already exists.")
            return True
        n = await POOL.reload()
        await update.message.reply_html(
            f"✅ <b>Account added!</b> <code>{html.escape(label)}</code>\n"
            f"📧 {html.escape(email)}\n"
            f"🔑 Token: <code>{html.escape(token[:12])}…</code> ({len(token)} chars)\n\n"
            f"Pool ab <b>{n}</b> account(s) → <b>{n}</b> users ek sath chat kar sakte hain.\n"
            f"{'🟢 Auto-refresh enabled' if email else ''}"
        )
        # Send pool status
        try:
            await update.message.reply_html(await adm.accounts_text(), reply_markup=adm.accounts_kb(await db.list_tokens()))
        except: pass
        return True

    if kind == "addkey":
        if "=" not in text_raw:
            await update.message.reply_html(
                "❌ Wrong format. Use <code>label = token</code>")
            return True
        label, token = text_raw.split("=", 1)
        label, token = label.strip(), token.strip()
        if not label or not token:
            await update.message.reply_html("❌ Label and token are required.")
            return True
        ok = await db.add_token(token, label)
        if not ok:
            await update.message.reply_html(
                f"❌ A key labelled <b>{html.escape(label)}</b> already exists.")
            return True
        n = await POOL.reload()
        await update.message.reply_html(
            f"✅ Key <b>{html.escape(label)}</b> added.\n\n"
            f"Pool is now <b>{n}</b> key(s) → <b>{n}</b> user(s) can chat "
            f"simultaneously.")
        return True

    if kind == "adduser":
        raw = text_raw.replace(",", " ").split()
        ids = []
        for tok in raw:
            tok = tok.strip().lstrip("@")
            if tok.isdigit():
                ids.append(int(tok))
        if not ids:
            await update.message.reply_html(
                "❌ No valid numeric IDs found.\n"
                "<i>Send digits only, e.g. <code>123456789</code>.</i>")
            return True

        added, already, failed = [], [], []
        for tid in ids:
            existing = await db.get_user(tid)
            if existing and existing.get("status") == db.ACTIVE:
                already.append(tid)
                continue
            await db.upsert_user(tid, status=db.ACTIVE)
            await db.set_user_status(tid, db.ACTIVE)
            if tid in USERS:
                USERS[tid]["status"] = db.ACTIVE
            PENDING_NOTIFIED.pop(tid, None)
            added.append(tid)
            try:
                await ctx.bot.send_message(
                    chat_id=tid,
                    text=("🎉 <b>You've been given access to this bot!</b>\n\n"
                          "Send /start to open the menu and begin chatting."),
                    parse_mode="HTML")
            except Exception:
                failed.append(tid)

        lines = []
        if added:
            lines.append(f"✅ Approved <b>{len(added)}</b> user(s): "
                         + ", ".join(f"<code>{i}</code>" for i in added))
        if already:
            lines.append(f"ℹ️ Already active: "
                         + ", ".join(f"<code>{i}</code>" for i in already))
        if failed:
            lines.append(
                f"\n⚠️ Could not notify {', '.join(str(i) for i in failed)} — "
                "Telegram only lets the bot message people who have opened it "
                "at least once. They're approved; ask them to press /start.")
        await update.message.reply_html("\n".join(lines))
        return True

    if kind == "dm":
        target = pending.get("target")
        try:
            await ctx.bot.send_message(
                chat_id=target,
                text=f"✉️ <b>Message from admin</b>\n\n{text_raw}",
                parse_mode="HTML")
            await update.message.reply_html("✅ Sent.")
        except Exception as e:
            await update.message.reply_html(
                f"❌ Could not deliver: {html.escape(str(e))[:150]}")
        return True

    if kind == "broadcast":
        async with Progress(ctx.bot, update.effective_chat.id,
                            "📣 Broadcasting",
                            steps=["Collecting recipients", "Sending"]) as p:
            await p.step(0)
            await p.step(1)

            async def cb(done, st):
                await p.note(f"{done} sent · {st['failed']} failed")

            stats = await adm.broadcast(ctx.bot, text_raw, uid, progress_cb=cb)
        await update.message.reply_html(
            "📣 <b>Broadcast finished</b>\n\n"
            f"• Recipients: <b>{stats['total']}</b>\n"
            f"• Delivered: <b>{stats['sent']}</b>\n"
            f"• Blocked the bot: <b>{stats['blocked']}</b>\n"
            f"• Failed: <b>{stats['failed']}</b>")
        return True

    return False

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update): return
    msg = update.message
    voice = msg.voice or msg.audio
    if not voice: return

    if msg.voice:
        suffix = ".ogg"
    else:
        mt = getattr(voice, 'mime_type', '') or ''
        suffix = "." + (mt.split('/')[-1] or 'mp3')

    dur = getattr(voice, 'duration', 0) or 0
    tmp_path = None
    text = lang = None

    async with Progress(
        ctx.bot, update.effective_chat.id, "🎤 Voice message",
        steps=["Downloading audio", "Transcribing (Whisper)", "Sending to DeepSeek"],
        reply_to=msg.message_id,
    ) as p:
        try:
            await p.step(0, f"{dur}s audio" if dur else "")
            tg_file = await ctx.bot.get_file(voice.file_id)
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

            await p.step(1, f"Whisper '{os.getenv('WHISPER_SIZE', 'small')}' "
                            f"model — pehli baar model download hoga (~500MB)")
            from stt import transcribe
            text, lang = await asyncio.to_thread(transcribe, tmp_path)
        except Exception as e:
            log.exception("STT failed")
            await p.done(f"❌ <b>Could not understand the audio</b>\n\n"
                         f"{html.escape(str(e))[:300]}\n\n"
                         f"<i>💡 Is faster-whisper installed? "
                         f"Check requirements.txt.</i>")
            return
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except: pass

        if not text or not text.strip():
            await p.done("🎤 <b>No speech detected.</b>\n\n"
                         "<i>Speak clearly, reduce background noise, "
                         "and record for longer than a second.</i>")
            return

        await p.step(2, text[:80])

    # transcript stays as a permanent record; the bar is gone
    await msg.reply_html(
        f"🗣 <b>You said</b> <i>({lang})</i>:\n{html.escape(text)}")
    await _process_prompt_chat(
        ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
        prompt=text, reply_to_msg_id=msg.message_id,
    )


# ---------- Media ----------
async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update): return
    s = get_state(update.effective_user.id)
    msg = update.message

    if not RULES[s.model_type]['supports_files']:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("→ Instant", callback_data="mode:default"),
            InlineKeyboardButton("→ Vision", callback_data="mode:vision"),
        ]])
        await msg.reply_html(
            f"❌ Files not supported in <b>{RULES[s.model_type]['name']}</b> mode.",
            reply_markup=kb)
        return

    doc = msg.document or (msg.photo[-1] if msg.photo else None)
    if not doc: return

    file_name = getattr(doc, 'file_name', None) or f"photo_{doc.file_unique_id}.jpg"
    caption = (msg.caption or "").strip()
    is_photo = bool(msg.photo)
    size_mb = (getattr(doc, 'file_size', 0) or 0) / 1_048_576

    tmp_path = None
    fid = fname = err = None

    async with Progress(
        ctx.bot, update.effective_chat.id,
        f"📤 {'Photo' if is_photo else 'File'}: {file_name[:40]}",
        steps=["Downloading from Telegram", "Uploading to DeepSeek", "Parsing / OCR"],
        reply_to=msg.message_id,
    ) as p:
        try:
            await p.step(0, f"{size_mb:.1f} MB" if size_mb else "")
            tg_file = await ctx.bot.get_file(doc.file_id)
            with tempfile.NamedTemporaryFile(delete=False,
                                             suffix="_" + file_name) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

            await p.step(1, "Sending to DeepSeek servers")
            await p.step(2, "Reading text from the image (OCR)"
                            if is_photo else "Parsing document")
            res = await ds_op(update.effective_user.id, 'upload_file_ex', tmp_path,
                              timeout=KEY_WAIT_TIMEOUT)
            if res is None:
                fid, fname, err = None, None, (
                    "No DeepSeek key was free in time. Please try again.")
            else:
                fid, fname, err = res
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except: pass

        if not fid:
            tip = (
                "\n\n<b>What works ✅</b>\n"
                "Screenshots · document scans · handwritten notes · bills · "
                "receipts · anything with readable writing\n\n"
                "<b>What doesn't ❌</b>\n"
                "Selfies · scenery · pets · memes · logos · plain graphics\n\n"
                "<i>💡 Sending it as a <b>File</b> instead of a photo stops "
                "Telegram compressing it, which helps blurry or small text.</i>"
            ) if is_photo else (
                "\n\n<i>💡 txt / pdf / docx / csv work best. "
                "Scanned PDFs need a text layer.</i>"
            )
            # keep the failure visible instead of deleting the bar
            await p.done(f"❌ <b>Upload failed</b>\n\n"
                         f"{html.escape(err or 'Unknown error')}{tip}")
            return
        # success → bar auto-deletes on context exit

    s.attached_files.append([fid, fname]); await save_session(update.effective_user.id)

    if caption:
        await _process_prompt_chat(
            ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
            prompt=caption, reply_to_msg_id=msg.message_id,
        )
    else:
        await msg.reply_html(
            f"✅ Attached: <b>{html.escape(fname)}</b>\nNow ask your question.")


# ---------- Text ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update): return
    uid = update.effective_user.id

    # An admin may be mid-flow typing a key / broadcast / DM
    pending = PENDING_INPUT.get(uid)
    if pending and is_admin(uid):
        if await handle_admin_input(update, ctx, pending):
            return

    text = update.message.text
    s = get_state(uid)

    # URL detection
    if s.auto_urls:
        urls = extract_urls(text)
        if urls:
            handled = await _try_url_summarize(ctx, update, urls[0], text)
            if handled:
                return

    await _process_prompt_chat(
        ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
        prompt=text, reply_to_msg_id=update.message.message_id,
    )


async def _try_url_summarize(ctx, update, url: str, original_text: str) -> bool:
    """Fetch URL/YT, then feed content to DeepSeek. Returns True if handled."""
    msg = update.message
    yt_id = is_youtube(url)
    if yt_id and not youtube_enabled():
        # YouTube blocks cloud IPs and no YT_PROXY/YT_COOKIES is configured —
        # silently treat the link as normal text instead of failing loudly.
        log.info("YouTube fetch disabled (no YT_PROXY/YT_COOKIES) — %s treated as text", url[:60])
        return False
    kind = "YouTube" if yt_id else "Web page"
    prompt = None

    async with Progress(
        ctx.bot, update.effective_chat.id, f"🔗 Reading {kind}",
        steps=(["Fetching transcript", "Cleaning text", "Sending to DeepSeek"]
               if yt_id else
               ["Downloading page", "Extracting article", "Sending to DeepSeek"]),
        reply_to=msg.message_id,
    ) as p:
        try:
            await p.step(0, url[:70])
            if yt_id:
                text, _ = await asyncio.to_thread(fetch_youtube_transcript, yt_id)
                source_desc = f"YouTube video: {url}"
            else:
                text, title = await asyncio.to_thread(fetch_url_text, url)
                source_desc = f"URL: {url}" + (f"\nTitle: {title}" if title else "")

            if not text or len(text) < 50:
                await p.done(
                    f"❌ <b>No content found</b>\n\n"
                    f"{html.escape(url[:100])}\n\n"
                    + ("<i>💡 This video has transcripts/captions disabled, "
                       "or it is private.</i>" if yt_id else
                       "<i>💡 The page is JavaScript-rendered or requires a "
                       "login — the bot cannot read its text.</i>"))
                return False

            await p.step(1, f"{len(text):,} characters mile")
            if len(text) > URL_TEXT_CAP:
                text = text[:URL_TEXT_CAP] + "\n\n[…truncated]"
                await p.note(f"{URL_TEXT_CAP:,} chars tak trim kiya")

            user_intent = original_text.replace(url, "").strip()
            if not user_intent or user_intent.lower() in {"summarize", "summary", "sum"}:
                instruction = "Summarize the content clearly with key points."
            else:
                instruction = user_intent

            prompt = (
                f"[Content from {source_desc}]:\n\n"
                f"{text}\n\n"
                f"---\n\n{instruction}"
            )
            await p.step(2, instruction[:70])
        except Exception as e:
            log.warning("URL fetch failed: %s", e)
            err_txt = html.escape(str(e))
            extra = ""
            if "blocking this server" in err_txt or "YT_PROXY" in err_txt:
                extra = ("\n\n<i>💡 Owner fix: set the <b>YT_PROXY</b> env var "
                         "on the host (Render → Environment) — cloud IPs are "
                         "blocked by YouTube.</i>")
            await p.done(
                f"⚠️ <b>{kind} fetch failed</b>\n\n"
                f"{err_txt[:400]}\n\n"
                f"<i>Falling back to treating this as a normal question…</i>"
                + extra)
            return False

    await _process_prompt_chat(
        ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
        prompt=prompt, reply_to_msg_id=msg.message_id,
    )
    return True


# ---------- Send helpers ----------
async def _send_tts(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                     female: bool):
    from tts import synthesize_ogg
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f:
        ogg_path = f.name

    n_chars = len(text)
    async with Progress(
        ctx.bot, chat_id, "🔊 Generating voice",
        steps=["Cleaning text", "Generating speech", "Sending to Telegram"],
    ) as p:
        try:
            await p.step(0, f"{n_chars:,} characters")
            await p.step(1, f"{'Female' if female else 'Male'} voice "
                            f"· edge-tts")
            await synthesize_ogg(text, ogg_path, prefer_female=female)

            await p.step(2)
            with open(ogg_path, "rb") as fh:
                await ctx.bot.send_voice(chat_id=chat_id, voice=fh)
            # bar deletes itself here; the voice note is the result
        except Exception as e:
            log.exception("TTS failed")
            await p.done(f"❌ <b>Voice generation failed</b>\n\n"
                         f"{html.escape(str(e))[:300]}")
        finally:
            try: os.unlink(ogg_path)
            except: pass


async def _send_response_file(ctx, chat_id: int, prompt: str, answer: str):
    content = ""
    if prompt:
        content += f"# Question\n\n{prompt[:5000]}"
        if len(prompt) > 5000: content += "\n[…prompt truncated]"
        content += "\n\n---\n\n"
    content += f"# Answer\n\n{answer}\n"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md",
                                       encoding="utf-8") as f:
        f.write(content); path = f.name
    try:
        with open(path, "rb") as fh:
            await ctx.bot.send_document(
                chat_id=chat_id, document=fh,
                filename=f"response_{int(time.time())}.md",
                caption=f"📄 Full response ({len(answer):,} chars)")
    finally:
        try: os.unlink(path)
        except: pass


# ---------- Core streaming ----------
async def _process_prompt_chat(*, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                user_id: int, prompt: str,
                                reply_to_msg_id: Optional[int],
                                is_regen: bool = False,
                                is_quick_action: bool = False):
    if not prompt or not prompt.strip():
        await ctx.bot.send_message(chat_id=chat_id, text="Empty prompt."); return

    wait = rate_check(user_id)
    if wait:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(f"🐢 <b>Slow down a little</b>\n\n"
                  f"Limit is {RATE_LIMIT} requests per {RATE_WINDOW}s — this "
                  f"protects the shared DeepSeek token from being throttled.\n"
                  f"<i>Try again in {wait}s.</i>"),
            parse_mode="HTML")
        return

    lock = user_lock(user_id)
    if lock.locked():
        await ctx.bot.send_message(
            chat_id=chat_id,
            text="⏳ <i>Your previous request is still running — this one is "
                 "queued and will start right after.</i>", parse_mode="HTML")
    CANCELLED.discard(user_id)
    async with lock:
        await _process_prompt_chat_inner(
            ctx=ctx, chat_id=chat_id, user_id=user_id, prompt=prompt,
            reply_to_msg_id=reply_to_msg_id, is_regen=is_regen,
            is_quick_action=is_quick_action)


async def _process_prompt_chat_inner(*, ctx: ContextTypes.DEFAULT_TYPE,
                                      chat_id: int, user_id: int, prompt: str,
                                      reply_to_msg_id: Optional[int],
                                      is_regen: bool = False,
                                      is_quick_action: bool = False):
    s = get_state(user_id)

    if not s.session_id:
        sid = await ds_op(user_id, 'create_chat')
        if not sid:
            await ctx.bot.send_message(chat_id=chat_id, text="❌ Session creation failed")
            return
        s.session_id = sid; s.parent_msg_id = None; await save_session(user_id)

    parent_before = s.parent_msg_id
    thinking_on = bool(s.thinking)
    search_on = bool(s.search) and RULES[s.model_type]['supports_search']
    mode = s.model_type
    file_ids = [f[0] for f in s.attached_files]

    # Wrap with persona
    final_prompt = wrap_prompt(prompt, s.persona)

    # Update stats
    if not is_regen:
        s.msg_count += 1
    s.total_chars_in += len(prompt)

    # Record user turn in history (original prompt, not persona-wrapped)
    if not is_regen and not is_quick_action:
        await record_history(user_id, "user", prompt)

    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    placeholder = await ctx.bot.send_message(
        chat_id=chat_id, text="⏳ …",
        reply_to_message_id=reply_to_msg_id,
    )
    # Determine initial waiter label — if queue, show "Analysing your request" as user requested
    initial_label = "Parsing files, DeepSeek is thinking" if file_ids else "DeepSeek is thinking"
    # If all slots busy, show queue message immediately (as per requirement: line-by-line queue)
    if await POOL.would_wait():
        queued = POOL.waiting_count + 1
        initial_label = f"⏳ Analysing your request ...  (Queue: {queued} waiting, {POOL.size} slots busy)"
    waiter = Waiter(placeholder, initial_label, kb=stop_kb())
    await waiter.start()

    # Background task to update waiter label with live queue position while we wait for a slot
    async def _queue_updater():
        while not waiter.stopped:
            try:
                if await POOL.would_wait():
                    q = POOL.waiting_count
                    # Show line-by-line queue position
                    waiter.update_label(f"⏳ Analysing your request ...  (Queue position: {q+1} | {POOL.busy_count}/{POOL.size} busy)")
                else:
                    # slot freed -> show thinking
                    waiter.update_label("Parsing files, DeepSeek is thinking" if file_ids else "DeepSeek is thinking")
            except Exception:
                pass
            await asyncio.sleep(1.8)
    queue_task = asyncio.create_task(_queue_updater())

    think_buf = ""
    answer_started = False
    messages = [placeholder]
    current_text = ""
    last_edit = 0.0
    edit_interval = 1.3

    def stream_iter(client):
        """Bridge the blocking generator onto the event loop."""
        async def _gen():
            loop = asyncio.get_event_loop()
            gen = client.chat_stream(
                s.session_id, s.parent_msg_id, final_prompt,
                model_type=mode, thinking=thinking_on, search=search_on,
                file_ids=file_ids,
            )
            while True:
                ev = await loop.run_in_executor(None, next, gen, None)
                if ev is None: break
                yield ev
        return _gen()

    async def safe_edit(msg, text: str, kb=None):
        try:
            await msg.edit_text(text, parse_mode="HTML",
                                 link_preview_options=LinkPreviewOptions(is_disabled=True),
                                 reply_markup=kb)
        except BadRequest as e:
            err = str(e).lower()
            if "not modified" in err: return
            log.warning("HTML edit failed: %s; falling back to plain", e)
            plain = re.sub(r"<[^>]+>", "", text)
            plain = plain.replace("&lt;", "<").replace("&gt;", ">") \
                         .replace("&amp;", "&").replace("&quot;", '"') \
                         .replace("&#x27;", "'").replace("▍", "")
            try:
                await msg.edit_text(plain[:MAX_TG_MSG], reply_markup=kb)
            except Exception as e2:
                log.debug("plain fallback: %s", e2)

    full_answer = ""
    # Hold a DeepSeek key for the entire stream. N keys => N conversations can
    # run at the same time; everyone else queues here until one frees up.
    # Queue display is line-by-line FIFO as requested.
    async with POOL.acquire(user_id, timeout=KEY_WAIT_TIMEOUT) as lease:
      if lease is None:
        try:
            queue_task.cancel()
            try: await queue_task
            except: pass
        except: pass
        await waiter.stop()
        if POOL.size == 0:
            oops = ("❌ <b>No DeepSeek key/account configured</b>\n\n"
                    "<i>Admin panel → 🔑 DeepSeek Accounts → ➕ Add Email Account se ek account jodo.</i>")
        else:
            oops = ("🕒 <b>All slots are busy — queue timed out</b>\n\n"
                    f"This bot has <b>{POOL.size}</b> DeepSeek account(s), so "
                    f"<b>{POOL.size}</b> chat(s) can run at once.\n"
                    f"<i>({POOL.waiting_count} users still waiting) Please send your message again in a moment.</i>")
        await safe_edit(messages[-1], oops,
                        kb=response_footer_kb(has_text=False))
        return
      # Got a slot — stop queue updater, keep waiter for thinking phase (it will be stopped on first token)
      try:
          queue_task.cancel()
          try: await queue_task
          except: pass
      except: pass
      # Ensure waiter shows thinking now that we have a slot
      waiter.update_label("Parsing files, DeepSeek is thinking" if file_ids else "DeepSeek is thinking")
      # User may have pressed ⏹ Stop while queued in the slot line — bail out
      # BEFORE firing any DeepSeek request (saves quota, mirrors app behaviour).
      if user_id in CANCELLED:
          CANCELLED.discard(user_id)
          await waiter.stop()
          await safe_edit(messages[-1], "⏹ <i>Stopped.</i>",
                          kb=response_footer_kb(has_text=False))
          return
      try:
          got_any = False
          async for ev in stream_iter(lease.client):
              if user_id in CANCELLED:
                  CANCELLED.discard(user_id)
                  await waiter.stop()
                  await safe_edit(messages[-1],
                                   (md_to_tg_html(current_text) + "\n\n<i>⏹ Stopped.</i>")
                                   if current_text else "⏹ <i>Stopped.</i>",
                                   kb=response_footer_kb(has_text=bool(current_text)))
                  return
              if not got_any:
                  # first event from DeepSeek → stop the waiting animation so it
                  # can't overwrite the streaming answer
                  await waiter.stop()
              got_any = True
              if ev['type'] == 'msg_id':
                  s.parent_msg_id = ev['id']; continue
              if ev['type'] == 'error':
                  err_msg = ev['msg']
                  # Try auto-refresh if it's an auth/token error and account has email
                  is_auth_err = any(x in err_msg.lower() for x in ["401","403","unauthorized","token","expired","session","auth"])
                  if is_auth_err and lease:
                      try:
                          # Attempt refresh via pool
                          refreshed = await POOL.report_failure(lease.label, err_msg, notify_callback=lambda l,e,err: notify_owner(ctx.bot, f"⚠️ <b>Account error</b>\n<code>{html.escape(l)}</code> ({html.escape(e)})\n<i>{html.escape(err[:200])}</i>"))
                          if refreshed:
                              await safe_edit(messages[-1], f"🔄 <b>Token expired, auto-refreshed!</b>\n<i>Retrying your request...</i>",
                                              kb=None)
                              # Note: we don't auto-retry here to avoid loops, user can resend
                          else:
                              # Notify admin
                              try:
                                  await notify_owner(ctx.bot, f"⚠️ <b>DeepSeek account failed</b>\nLabel: <code>{html.escape(lease.label)}</code>\nError: <i>{html.escape(err_msg[:250])}</i>\n<i>Admin panel → Accounts → Refresh or check email/pass</i>")
                              except: pass
                      except Exception as ne:
                          log.warning("report_failure error: %s", ne)
                  await safe_edit(messages[-1], f"❌ {html.escape(err_msg)}",
                                  kb=response_footer_kb(has_text=False))
                  return
              if ev['type'] == 'think':
                  if answer_started or not thinking_on: continue
                  think_buf += ev['text']
              elif ev['type'] == 'answer':
                  if not answer_started:
                      answer_started = True
                      current_text = ""
                  current_text += ev['text']
                  full_answer += ev['text']

                  if len(current_text) > MAX_TG_MSG:
                      cut = MAX_TG_MSG
                      tail = current_text[:cut]
                      nl = tail.rfind("\n"); sp = tail.rfind(" ")
                      if nl > cut - 400: cut = nl
                      elif sp > cut - 200: cut = sp
                      sealed = current_text[:cut]
                      remainder = current_text[cut:].lstrip()

                      sealed_html = safe_for_telegram(md_to_tg_html(sealed), MAX_TG_MSG)
                      await safe_edit(messages[-1], sealed_html, kb=None)
                      new_bubble = await ctx.bot.send_message(chat_id=chat_id, text="⏳ …")
                      messages.append(new_bubble)
                      current_text = remainder
                      last_edit = 0.0

              now = asyncio.get_event_loop().time()
              if now - last_edit > edit_interval:
                  last_edit = now
                  if answer_started:
                      partial = strip_incomplete_markers(current_text)
                      txt = md_to_tg_html(partial) + " ▍"
                  else:
                      if thinking_on and think_buf:
                          txt = _ticker_view(think_buf)
                      else:
                          txt = "⏳ <i>thinking…</i>"
                  txt = safe_for_telegram(txt, MAX_TG_MSG)
                  await safe_edit(messages[-1], txt, kb=stop_kb())

          if not got_any:
              await waiter.stop()
              await safe_edit(messages[-1],
                               "❌ <b>No response from DeepSeek.</b>\n\n"
                               "<i>💡 Your token may have expired, or DeepSeek "
                               "servers are busy. Try 'New Chat'.</i>",
                               kb=response_footer_kb(has_text=False))
              return

          # Cache
          LAST[user_id] = {
              'prompt': prompt, 'raw_answer': full_answer,
              'session_id': s.session_id, 'parent': s.parent_msg_id,
              'parent_before': parent_before,
          }
          # Track bot response in history (skip regen since it replaces)
          if not is_regen and not is_quick_action:
              await record_history(user_id, "assistant", full_answer)

          s.total_chars_out += len(full_answer)
          await save_session(user_id)
          await db.bump_usage(user_id, messages=0 if is_regen else 1,
                              chars_in=len(prompt), chars_out=len(full_answer))

          total_len = len(full_answer)

          if total_len > FILE_THRESHOLD:
              preview_html = md_to_tg_html(full_answer[:2500]) + "\n\n<i>… (see file below)</i>"
              preview_html = safe_for_telegram(preview_html, MAX_TG_MSG)
              await safe_edit(messages[0], preview_html, kb=None)
              for extra in messages[1:]:
                  try: await ctx.bot.delete_message(chat_id=chat_id,
                                                      message_id=extra.message_id)
                  except: pass
              await _send_response_file(ctx, chat_id, prompt, full_answer)
              await ctx.bot.send_message(
                  chat_id=chat_id,
                  text=f"📄 Full response ({total_len:,} chars) attached above.",
                  reply_markup=response_footer_kb(has_text=True),
              )
          else:
              if not answer_started:
                  final = "⚠️ Model didn't produce an answer."
              else:
                  final = md_to_tg_html(current_text) if current_text else "(empty)"
              final = safe_for_telegram(final, MAX_TG_MSG)
              await safe_edit(messages[-1], final,
                               kb=response_footer_kb(has_text=answer_started))

          # Auto-TTS if voice_reply is ON
          if s.voice_reply and answer_started and full_answer.strip():
              await _send_tts(ctx, chat_id, full_answer, s.tts_female)

          # Detach files after send
          s.attached_files = []; await save_session(user_id)

      except Exception as e:
          log.exception("stream error")
          # Check if it's auth-like and try refresh
          try:
              if lease and any(x in str(e).lower() for x in ["401","403","token","expired","auth"]):
                  await POOL.report_failure(lease.label, str(e), notify_callback=lambda l,em,er: notify_owner(ctx.bot, f"⚠️ <b>Account exception</b>\n<code>{html.escape(l)}</code>\n<i>{html.escape(er[:200])}</i>"))
                  try:
                      await notify_owner(ctx.bot, f"⚠️ <b>Runtime error on account {html.escape(lease.label)}</b>\n<code>{html.escape(str(e)[:300])}</code>")
                  except: pass
          except: pass
          try:
              await safe_edit(messages[-1], f"❌ Error: {html.escape(str(e))}",
                               kb=response_footer_kb(has_text=False))
          except: pass
      finally:
          # guarantee the animator never outlives the request
          try:
              queue_task.cancel()
              try: await queue_task
              except: pass
          except: pass
          await waiter.stop()


# ---------- Post-init ----------
async def health_check_loop(bot):
    """Periodic health check for DeepSeek accounts — auto-refreshes expired tokens."""
    # Wait a bit after boot
    await asyncio.sleep(60)
    while True:
        try:
            # Check every 6 hours
            async def notify(lbl, email, err):
                try:
                    await bot.send_message(chat_id=OWNER_ID,
                        text=f"⚠️ <b>Account auto-check failed</b>\n<code>{html.escape(lbl)}</code> ({html.escape(email)})\n<i>{html.escape(err[:250])}</i>\n\nAdmin panel → 🔑 DeepSeek Accounts → Refresh",
                        parse_mode="HTML")
                except Exception as ne:
                    log.warning("health notify failed: %s", ne)
            res = await POOL.health_check_all(notify_callback=notify)
            if res["failed"] > 0 or res["refreshed"] > 0:
                log.info("Health check: %s", res)
                # Also send summary to owner if something happened
                if res["refreshed"] > 0:
                    try:
                        await bot.send_message(chat_id=OWNER_ID,
                            text=f"🔄 <b>Health check — {res['refreshed']} account(s) refreshed</b>\nHealthy: {res['healthy']}/{res['total']} · Failed: {res['failed']}",
                            parse_mode="HTML", disable_notification=True)
                    except: pass
        except Exception as e:
            log.warning("health_check_loop error: %s", e)
        await asyncio.sleep(6 * 3600)  # 6 hours


async def _post_init(app):
    log.info("post_init: connecting to MongoDB…")
    try:
        await db.connect()
        # Make sure the owner exists and is an admin
        await db.upsert_user(OWNER_ID, status=db.ACTIVE, role="admin")
        USERS[OWNER_ID] = await db.get_user(OWNER_ID)

        # Seed the pool from DEEPSEEK_TOKEN the first time only
        if DEEPSEEK_TOKEN and not await db.list_tokens():
            await db.add_token(DEEPSEEK_TOKEN, "primary")
            log.info("Seeded key pool from DEEPSEEK_TOKEN")
        n_keys = await POOL.reload()
        log.info("DeepSeek key pool: %d key(s)", n_keys)

        app.bot_data["wipe_task"] = asyncio.create_task(
            nightly_wipe_loop(app.bot, OWNER_ID))
        app.bot_data["health_task"] = asyncio.create_task(
            health_check_loop(app.bot))
        log.info("Health check loop started (6h interval)")

        await app.bot.set_my_commands([
            BotCommand("start", "Open the menu"),
            BotCommand("help", "Usage guide"),
            BotCommand("cancel", "Stop the running task"),
        ])
        await app.bot.set_my_commands([
            BotCommand("start", "Open the menu"),
            BotCommand("help", "Usage guide"),
            BotCommand("cancel", "Stop the running task"),
            BotCommand("admin", "Admin panel"),
        ], scope=BotCommandScopeChat(chat_id=OWNER_ID))
        # Only greet on a genuinely new deployment, not on every restart —
        # hosts like Render restart often and the message became spam.
        stamp = os.path.join(WORKDIR, ".last_boot_notice")
        version = "v7.2-app-parity"
        seen = ""
        try:
            with open(stamp, encoding="utf-8") as f:
                seen = f.read().strip()
        except Exception:
            pass
        if seen != version:
            await app.bot.send_message(
                chat_id=OWNER_ID,
                text="🚀 <b>Bot v7 is live — email accounts & queue</b>\n\n"
                     "👥 Users, approvals, blocking, broadcast\n"
                     "🔑 DeepSeek accounts via email/pass — auto-refresh!\n"
                     "⏳ Queue: 1 account=1 user, others wait line-by-line (FIFO)\n"
                     "🗄 MongoDB · auto health check every 6h\n\n"
                     "/admin → 🔑 DeepSeek Accounts for management.",
                parse_mode="HTML",
            )
            try:
                with open(stamp, "w", encoding="utf-8") as f:
                    f.write(version)
            except Exception:
                pass
    except Exception as e:
        log.exception("post_init failed: %s", e)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Catch-all: without this a single unhandled exception kills the update
    silently and the user is left staring at a dead chat."""
    err = ctx.error
    log.exception("Unhandled exception", exc_info=err)

    if isinstance(err, Conflict):
        # Two pollers on one token. Telegram hands each getUpdates call to a
        # random one, so messages appear to be answered intermittently.
        log.error(
            "Conflict: another process is polling with this bot token. "
            "Only one instance may run. Check for an old Render deploy, a "
            "second service, or a local copy still running. This instance is "
            "%s.", INSTANCE_ID)
        return
    if isinstance(err, (TimedOut, NetworkError)):
        return   # transient, python-telegram-bot retries by itself

    chat_id = None
    if isinstance(update, Update):
        if update.effective_chat:
            chat_id = update.effective_chat.id
        if update.callback_query:
            try: await update.callback_query.answer("Something went wrong — try again",
                                                     show_alert=True)
            except Exception: pass
    if chat_id is None or chat_id != OWNER_ID:
        return
    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=("⚠️ <b>Something went wrong</b>\n\n"
                  f"<code>{html.escape(type(err).__name__)}: "
                  f"{html.escape(str(err))[:250]}</code>\n\n"
                  "<i>The bot is still running — please try again. "
                  "If this keeps happening, press /start.</i>"),
            parse_mode="HTML")
    except Exception:
        pass


def build_app():
    app = (ApplicationBuilder()
           .token(TELEGRAM_TOKEN)
           .concurrent_updates(CONCURRENCY)   # buttons no longer block on a
           .post_init(_post_init)             # slow DeepSeek reply
           .build())
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


async def _claim_polling_lock() -> bool:
    """
    Make sure we are the only poller.

    A redeploy overlaps with the dying instance for a few seconds, so we retry
    for a while before giving up. If another *live* instance holds the lease we
    refuse to start polling rather than fighting it — that fight is what
    produces 'Conflict: terminated by other getUpdates request' and makes the
    bot answer only every other message.
    """
    deadline = time.time() + LOCK_WAIT
    while True:
        if await db.acquire_instance_lock(INSTANCE_ID):
            log.info("Polling lock acquired (instance %s)", INSTANCE_ID)
            return True
        holder = await db.instance_lock_holder() or {}
        age = time.time() - holder.get("heartbeat", 0)
        if time.time() >= deadline:
            if FORCE_POLL:
                log.warning("Forcing takeover from %s (idle %.0fs)",
                            holder.get("owner"), age)
                await db.acquire_instance_lock(INSTANCE_ID, force=True)
                return True
            log.error(
                "Another instance (%s) is already polling, last seen %.0fs "
                "ago. Not starting a second poller — stop the other copy, or "
                "set FORCE_POLL=1 to take over.", holder.get("owner"), age)
            return False
        log.info("Waiting for the previous instance to exit (%s, idle %.0fs)…",
                 holder.get("owner"), age)
        await asyncio.sleep(3)


async def _lock_heartbeat():
    """Keep the lease alive; stop the process if we lose it."""
    while True:
        await asyncio.sleep(db.LOCK_REFRESH)
        try:
            if not await db.refresh_instance_lock(INSTANCE_ID):
                log.error("Lost the polling lock — another instance took "
                          "over. Shutting this one down.")
                os.kill(os.getpid(), signal.SIGTERM)
                return
        except Exception as e:
            log.warning("lock heartbeat failed: %s", e)


async def _run_with_health():
    """Run bot polling + tiny HTTP server (for Render Web Service)."""
    from health import start_health_server

    # Render sends SIGTERM on redeploy. Releasing the lease here means the
    # replacement instance starts polling immediately instead of waiting out
    # the lock TTL.
    stopping = asyncio.Event()

    def _on_signal(*_a):
        log.info("Shutdown signal received — releasing polling lock.")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            pass

    app = build_app()
    runner = await start_health_server()
    try:
        await app.initialize()
        # Application.initialize() does not invoke post_init on this manual
        # start path (only run_polling does), so call it ourselves.
        await _post_init(app)

        # Refuse to become a second poller (the cause of Conflict errors).
        if not await _claim_polling_lock():
            log.error("Staying up to serve /health, but NOT polling Telegram.")
            while True:
                await asyncio.sleep(3600)

        app.bot_data["lock_task"] = asyncio.create_task(_lock_heartbeat())
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES,
                                          drop_pending_updates=True)
        log.info("Bot + health running…")
        await stopping.wait()
    finally:
        for key in ("wipe_task", "health_task", "lock_task"):
            t = app.bot_data.get(key)
            if t:
                t.cancel()
                try: await t
                except (asyncio.CancelledError, Exception): pass
        try: await db.release_instance_lock(INSTANCE_ID)
        except Exception: pass
        try: await app.updater.stop()
        except: pass
        try: await app.stop()
        except: pass
        try: await app.shutdown()
        except: pass
        try: await runner.cleanup()
        except: pass
        try: await db.close()
        except: pass


def main():
    if ENABLE_HEALTH:
        asyncio.run(_run_with_health())
    else:
        app = build_app()
        log.info("Bot starting (polling only)…")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
