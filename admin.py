"""
admin.py — owner-only control panel.

Covers: user management (approve / block / unblock / list), DeepSeek key pool
management (add / list / remove), global stats, broadcast, and read-only
inspection of any single user's DeepSeek conversation.

Everything here is gated by is_admin() in bot.py before dispatch.
"""
import asyncio
import html
import logging
import time
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, BadRequest, TimedOut, NetworkError

import db
from token_pool import POOL

log = logging.getLogger("admin")

PAGE = 6

# Telegram allows roughly 30 messages/second to different chats. We stay well
# under it: bursts of BROADCAST_BATCH then a pause, plus a small per-message
# delay. RetryAfter is always obeyed.
BROADCAST_RATE = 20          # messages per second (ceiling)
BROADCAST_BATCH = 20         # pause after this many
BROADCAST_PAUSE = 1.2        # seconds between batches


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="adm:users:active:0"),
         InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("🔑 DeepSeek keys", callback_data="adm:keys"),
         InlineKeyboardButton("📣 Broadcast", callback_data="adm:bc:ask")],
        [InlineKeyboardButton("🚪 Access mode", callback_data="adm:mode")],
        [InlineKeyboardButton("🔙 Back to bot", callback_data="cmd:refresh")],
    ])


def users_kb(users: List[dict], status: str, page: int,
             total: int) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        uid = u["_id"]
        name = (u.get("first_name") or u.get("username") or str(uid))[:18]
        badge = {"active": "✅", "blocked": "🚫", "pending": "⏳"}.get(
            u.get("status"), "•")
        rows.append([InlineKeyboardButton(
            f"{badge} {name} · {u.get('msg_count', 0)} msgs",
            callback_data=f"adm:u:{uid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "⬅️", callback_data=f"adm:users:{status}:{page-1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton(
            "➡️", callback_data=f"adm:users:{status}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("✅ Active", callback_data="adm:users:active:0"),
        InlineKeyboardButton("⏳ Pending", callback_data="adm:users:pending:0"),
        InlineKeyboardButton("🚫 Blocked", callback_data="adm:users:blocked:0"),
    ])
    rows.append([InlineKeyboardButton("🔙 Admin menu", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def user_detail_kb(uid: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status != db.ACTIVE:
        rows.append([InlineKeyboardButton("✅ Approve",
                                          callback_data=f"adm:approve:{uid}")])
    if status != db.BLOCKED:
        rows.append([InlineKeyboardButton("🚫 Block",
                                          callback_data=f"adm:block:{uid}")])
    else:
        rows.append([InlineKeyboardButton("♻️ Unblock",
                                          callback_data=f"adm:approve:{uid}")])
    rows.append([
        InlineKeyboardButton("💬 View chat", callback_data=f"adm:chat:{uid}:0"),
        InlineKeyboardButton("🧹 Clear chat", callback_data=f"adm:clear:{uid}"),
    ])
    rows.append([InlineKeyboardButton("✉️ Message user",
                                      callback_data=f"adm:dm:{uid}")])
    rows.append([InlineKeyboardButton("🔙 Users",
                                      callback_data="adm:users:active:0")])
    return InlineKeyboardMarkup(rows)


def keys_kb(tokens: List[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{'🟢' if t.get('healthy', True) else '🔴'} {t['label']} · "
        f"{t.get('uses', 0)} uses",
        callback_data=f"adm:key:{t['label']}")] for t in tokens]
    rows.append([InlineKeyboardButton("➕ Add key", callback_data="adm:key:add")])
    rows.append([InlineKeyboardButton("🔙 Admin menu", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

async def stats_text() -> str:
    s = await db.global_stats()
    cfg = await db.get_config()
    mode = "🌍 Open to everyone" if cfg["access_mode"] == db.MODE_OPEN \
        else "🔒 Invite only"
    return (
        "📊 <b>Bot statistics</b>\n\n"
        f"<b>Users</b>\n"
        f"• Total: <b>{s['total']}</b>\n"
        f"• Active: <b>{s['active']}</b>  ·  Pending: <b>{s['pending']}</b>  "
        f"·  Blocked: <b>{s['blocked']}</b>\n"
        f"• Active in last 24h: <b>{s['active_24h']}</b>\n\n"
        f"<b>Usage</b>\n"
        f"• Messages handled: <b>{s['messages']:,}</b>\n"
        f"• Characters in: <b>{s['chars_in']:,}</b>\n"
        f"• Characters out: <b>{s['chars_out']:,}</b>\n"
        f"• Turns stored right now: <b>{s['turns_stored']:,}</b>\n\n"
        f"<b>Capacity</b>\n"
        f"• DeepSeek keys: <b>{POOL.size}</b> "
        f"(<b>{POOL.free_count}</b> free, <b>{POOL.busy_count}</b> in use)\n"
        f"• Simultaneous users supported: <b>{POOL.size or 0}</b>\n\n"
        f"<b>Access:</b> {mode}"
    )


async def keys_text() -> str:
    tokens = await db.list_tokens()
    if not tokens:
        return ("🔑 <b>DeepSeek keys</b>\n\n"
                "<i>No keys yet. Add one — each key lets one more person use "
                "the bot at the same time.</i>")
    lines = ["🔑 <b>DeepSeek keys</b>\n"]
    for t in tokens:
        dot = "🟢" if t.get("healthy", True) else "🔴"
        lines.append(f"{dot} <b>{html.escape(t['label'])}</b> — "
                     f"{t.get('uses', 0)} uses")
        if t.get("last_error"):
            lines.append(f"   <i>{html.escape(t['last_error'][:80])}</i>")
    lines.append(f"\n<b>{len(tokens)}</b> key(s) → <b>{len(tokens)}</b> "
                 f"user(s) can talk to DeepSeek at once.")
    lines.append(f"Currently free: <b>{POOL.free_count}</b>")
    return "\n".join(lines)


async def user_detail_text(uid: int) -> Tuple[str, str]:
    u = await db.get_user(uid)
    if not u:
        return "User not found.", db.PENDING
    st = u.get("status", db.PENDING)
    badge = {"active": "✅ Active", "blocked": "🚫 Blocked",
             "pending": "⏳ Pending approval"}.get(st, st)
    joined = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(u.get("joined_at", 0)))
    seen = time.strftime("%Y-%m-%d %H:%M",
                         time.localtime(u.get("last_seen", 0)))
    uname = f"@{u['username']}" if u.get("username") else "—"
    turns = len(await db.get_history(uid, limit=500))
    txt = (
        f"👤 <b>{html.escape(u.get('first_name') or str(uid))}</b>\n\n"
        f"• ID: <code>{uid}</code>\n"
        f"• Username: {html.escape(uname)}\n"
        f"• Status: <b>{badge}</b>\n"
        f"• Joined: {joined}\n"
        f"• Last seen: {seen}\n\n"
        f"• Messages: <b>{u.get('msg_count', 0):,}</b>\n"
        f"• Chars in/out: {u.get('chars_in', 0):,} / {u.get('chars_out', 0):,}\n"
        f"• Stored turns: <b>{turns}</b> <i>(auto-deleted after 24h)</i>"
    )
    return txt, st


async def user_chat_text(uid: int, page: int = 0,
                         per_page: int = 6) -> Tuple[str, bool]:
    """Read-only transcript view for the admin. Returns (text, has_more)."""
    rows = await db.get_history(uid, limit=200)
    if not rows:
        return ("💬 <i>No stored conversation for this user.</i>\n"
                "<i>History is temporary and clears every night.</i>"), False

    start = page * per_page
    chunk = rows[start:start + per_page]
    if not chunk:
        return "💬 <i>No more messages.</i>", False

    out = [f"💬 <b>Conversation</b> — page {page + 1}\n"]
    for r in chunk:
        who = "👤 User" if r["role"] == "user" else "🤖 DeepSeek"
        ts = time.strftime("%H:%M", time.localtime(r["ts"]))
        body = html.escape(r["text"][:600])
        if len(r["text"]) > 600:
            body += " <i>…</i>"
        out.append(f"<b>{who}</b> <i>{ts}</i>\n{body}\n")
    return "\n".join(out), (start + per_page) < len(rows)


# --------------------------------------------------------------------------
# Broadcast
# --------------------------------------------------------------------------

async def broadcast(bot, text: str, sender_id: int,
                    progress_cb=None) -> dict:
    """
    Send `text` to every active user, respecting Telegram's rate limits.

    Users who blocked the bot are automatically marked blocked in the DB so
    the next broadcast skips them.
    """
    ids = await db.all_active_ids()
    ids = [i for i in ids if i != sender_id]
    stats = {"total": len(ids), "sent": 0, "failed": 0, "blocked": 0}
    delay = 1.0 / max(1, BROADCAST_RATE)

    for i, uid in enumerate(ids, 1):
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML",
                                   disable_notification=True)
            stats["sent"] += 1
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 3)) + 0.5
            log.warning("broadcast flood wait %.1fs", wait)
            await asyncio.sleep(wait)
            try:
                await bot.send_message(chat_id=uid, text=text,
                                       parse_mode="HTML",
                                       disable_notification=True)
                stats["sent"] += 1
            except Exception:
                stats["failed"] += 1
        except Forbidden:
            # user blocked the bot / deleted the chat
            stats["blocked"] += 1
            await db.set_user_status(uid, db.BLOCKED)
        except (BadRequest, TimedOut, NetworkError):
            stats["failed"] += 1
        except Exception as e:
            log.debug("broadcast to %s failed: %s", uid, e)
            stats["failed"] += 1

        await asyncio.sleep(delay)
        if i % BROADCAST_BATCH == 0:
            await asyncio.sleep(BROADCAST_PAUSE)
            if progress_cb:
                await progress_cb(i, stats)

    return stats
