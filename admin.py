"""
admin.py — owner-only control panel.

Covers: user management (approve / block / unblock / list), DeepSeek account pool
management (add via email/pass or token / list / remove / refresh), global stats,
broadcast, and read-only inspection of any single user's DeepSeek conversation.

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

BROADCAST_RATE = 20
BROADCAST_BATCH = 20
BROADCAST_PAUSE = 1.2


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="adm:users:active:0"),
         InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("➕ Invite a user", callback_data="adm:inv:menu")],
        [InlineKeyboardButton("🔑 DeepSeek Accounts", callback_data="adm:accounts"),
         InlineKeyboardButton("📣 Broadcast", callback_data="adm:bc:ask")],
        [InlineKeyboardButton("🚪 Access mode", callback_data="adm:mode")],
        [InlineKeyboardButton("🔙 Back to bot", callback_data="cmd:refresh")],
    ])


def invite_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Create invite link",
                              callback_data="adm:inv:new:1:0")],
        [InlineKeyboardButton("🔗 Multi-use (10)",
                              callback_data="adm:inv:new:10:0"),
         InlineKeyboardButton("⏱ 24h link",
                              callback_data="adm:inv:new:1:24")],
        [InlineKeyboardButton("🆔 Add user by ID",
                              callback_data="adm:inv:byid")],
        [InlineKeyboardButton("📋 Active links", callback_data="adm:inv:list")],
        [InlineKeyboardButton("🔙 Admin menu", callback_data="adm:menu")],
    ])


def invite_created_kb(link: str, code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share this link",
                              switch_inline_query=(
                                  f"Join me on this AI bot: {link}"))],
        [InlineKeyboardButton("🗑 Revoke", callback_data=f"adm:inv:rv:{code}"),
         InlineKeyboardButton("📋 Active links", callback_data="adm:inv:list")],
        [InlineKeyboardButton("🔙 Invite menu", callback_data="adm:inv:menu")],
    ])


def invite_list_kb(invites: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for inv in invites[:10]:
        left = inv.get("max_uses", 1) - inv.get("uses", 0)
        rows.append([InlineKeyboardButton(
            f"🔗 {inv['_id'][:10]}… · {left} left",
            callback_data=f"adm:inv:show:{inv['_id']}")])
    rows.append([InlineKeyboardButton("🔙 Invite menu",
                                      callback_data="adm:inv:menu")])
    return InlineKeyboardMarkup(rows)


def invite_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


async def invite_menu_text() -> str:
    active = await db.list_invites()
    live = [i for i in active if db.invite_state(i) == "ok"]
    return (
        "➕ <b>Invite a user</b>\n\n"
        "Two ways to let someone in:\n\n"
        "🔗 <b>Invite link</b> — send them a link. When they open it they are "
        "approved automatically, no action needed from you.\n\n"
        "🆔 <b>Add by ID</b> — if you already know their Telegram numeric ID, "
        "approve them right now. They get a message telling them they're in.\n\n"
        f"<b>{len(live)}</b> link(s) currently active."
    )


async def invite_detail_text(inv: dict, bot_username: str) -> str:
    state = db.invite_state(inv)
    badge = {"ok": "🟢 Active", "revoked": "🗑 Revoked",
             "expired": "⌛ Expired", "used_up": "✅ Fully used"}[state]
    link = invite_link(bot_username, inv["_id"])
    created = time.strftime("%Y-%m-%d %H:%M",
                            time.localtime(inv.get("created_at", 0)))
    lines = [
        "🔗 <b>Invite link</b>\n",
        f"<code>{html.escape(link)}</code>\n",
        f"• Status: <b>{badge}</b>",
        f"• Used: <b>{inv.get('uses', 0)}</b> / {inv.get('max_uses', 1)}",
        f"• Created: {created}",
    ]
    exp = inv.get("expires_at") or 0
    if exp:
        lines.append("• Expires: " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(exp)))
    else:
        lines.append("• Expires: never")
    if inv.get("used_by"):
        lines.append(f"• Joined via this link: "
                     f"{', '.join(f'<code>{u}</code>' for u in inv['used_by'][:10])}")
    lines.append("\n<i>Tap and hold the link above to copy it.</i>")
    return "\n".join(lines)


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
    rows.append([
        InlineKeyboardButton("➕ Invite a user", callback_data="adm:inv:menu"),
        InlineKeyboardButton("🔙 Admin menu", callback_data="adm:menu"),
    ])
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


# New enhanced accounts keyboard
def accounts_kb(tokens: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tokens:
        email = t.get("email", "")
        atype = t.get("auth_type", "token")
        dot = "🟢" if t.get("healthy", True) else "🔴"
        if email and atype == "email":
            label = f"{dot} {t['label']} · {email[:20]} · {t.get('uses', 0)} uses"
        else:
            label = f"{dot} {t['label']} · {t.get('uses', 0)} uses"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm:acc:{t['label']}")])
    rows.append([InlineKeyboardButton("➕ Add Email Account", callback_data="adm:acc:add_email")])
    rows.append([InlineKeyboardButton("➕ Add Token (manual)", callback_data="adm:key:add")])
    rows.append([
        InlineKeyboardButton("🔄 Refresh All", callback_data="adm:acc:refresh_all"),
        InlineKeyboardButton("📊 Pool Status", callback_data="adm:acc:pool")
    ])
    rows.append([InlineKeyboardButton("🔙 Admin menu", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def account_detail_kb(label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Token", callback_data=f"adm:acc:refresh:{label}")],
        [InlineKeyboardButton("🗑 Delete Account", callback_data=f"adm:acc:del:{label}")],
        [InlineKeyboardButton("🔙 Accounts", callback_data="adm:accounts")],
    ])


# Keep old keys_kb for backward compatibility alias
def keys_kb(tokens: List[dict]) -> InlineKeyboardMarkup:
    return accounts_kb(tokens)


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

async def stats_text() -> str:
    s = await db.global_stats()
    cfg = await db.get_config()
    mode = "🌍 Open to everyone" if cfg["access_mode"] == db.MODE_OPEN \
        else "🔒 Invite only"
    # pool info
    tokens = await db.list_tokens()
    healthy = len([t for t in tokens if t.get("healthy", True)])
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
        f"<b>Capacity (DeepSeek Accounts)</b>\n"
        f"• Total accounts: <b>{len(tokens)}</b> (healthy: <b>{healthy}</b>)\n"
        f"• Pool size: <b>{POOL.size}</b> "
        f"(<b>{POOL.free_count}</b> free, <b>{POOL.busy_count}</b> busy, <b>{POOL.waiting_count}</b> queued)\n"
        f"• Simultaneous users supported: <b>{POOL.size or 0}</b>\n"
        f"• 1 account = 1 concurrent user. Queue = line-by-line FIFO.\n\n"
        f"<b>Access:</b> {mode}"
    )


async def accounts_text() -> str:
    tokens = await db.list_tokens()
    if not tokens:
        return (
            "🔑 <b>DeepSeek Accounts</b>\n\n"
            "<i>No accounts yet.</i>\n\n"
            "• <b>➕ Add Email Account</b> → email + password bhejo, token auto-generate hoga aur expire pe auto-refresh bhi.\n"
            "• <b>➕ Add Token</b> → manual token (old method).\n\n"
            "Har account = 1 simultaneous user. 2 accounts = 2 log ek sath chat kar sakte hain. 10 accounts = 10 log ek sath.\n"
            "Jab saare busy honge to baki users ko <i>⏳ Analysing your request...</i> dikhega aur turn-by-turn line me handle hoga."
        )
    lines = ["🔑 <b>DeepSeek Accounts — Pool Status</b>\n"]
    lines.append(f"Pool: <b>{POOL.size}</b> total, <b>{POOL.free_count}</b> free, <b>{POOL.busy_count}</b> busy, <b>{POOL.waiting_count}</b> waiting\n")
    # busy map
    bm = POOL.busy_map()
    if bm:
        lines.append("<b>Busy:</b>")
        for lab, uid in bm.items():
            lines.append(f"  • <code>{html.escape(lab)}</code> → user <code>{uid}</code>")
        lines.append("")
    for t in tokens:
        dot = "🟢" if t.get("healthy", True) else "🔴"
        label = t['label']
        uses = t.get('uses', 0)
        email = t.get("email", "")
        atype = t.get("auth_type", "token")
        last_err = t.get("last_error", "")
        # token preview
        tok = t.get("token", "")
        tok_prev = (tok[:10] + "…") if len(tok) > 12 else tok
        if atype == "email" and email:
            lines.append(f"{dot} <b>{html.escape(label)}</b> — 📧 <code>{html.escape(email)}</code> — {uses} uses — <code>{html.escape(tok_prev)}</code>")
        else:
            lines.append(f"{dot} <b>{html.escape(label)}</b> — 🔑 <code>{html.escape(tok_prev)}</code> — {uses} uses")
        if last_err:
            lines.append(f"   <i>⚠️ {html.escape(last_err[:90])}</i>")
        # last check time
        lc = t.get("last_check", 0)
        if lc:
            lines.append(f"   <i>last check: {time.strftime('%Y-%m-%d %H:%M', time.localtime(lc))}</i>")
    lines.append(f"\n<b>{len(tokens)}</b> account(s) → <b>{len(tokens)}</b> simultaneous user(s).")
    lines.append("Tap an account to manage (refresh/delete).")
    return "\n".join(lines)


async def keys_text() -> str:
    # alias for backward compatibility
    return await accounts_text()


async def account_detail_text(label: str) -> str:
    doc = await db.get_token_doc(label)
    if not doc:
        return f"❌ Account <b>{html.escape(label)}</b> not found."
    dot = "🟢 Healthy" if doc.get("healthy", True) else "🔴 Unhealthy"
    email = doc.get("email", "")
    atype = doc.get("auth_type", "token")
    uses = doc.get("uses", 0)
    added = time.strftime("%Y-%m-%d %H:%M", time.localtime(doc.get("added_at", 0)))
    last_check = doc.get("last_check", 0)
    last_login = doc.get("last_login", 0)
    tok = doc.get("token", "")
    tok_prev = (tok[:16] + "..." + tok[-6:]) if len(tok) > 22 else tok
    last_err = doc.get("last_error", "") or "—"
    # busy?
    bm = POOL.busy_map()
    busy = bm.get(label)
    busy_txt = f"⏳ Busy with user <code>{busy}</code>" if busy else "💤 Free"
    lines = [
        f"🔑 <b>Account: {html.escape(label)}</b>\n",
        f"• Status: <b>{dot}</b> — {busy_txt}",
        f"• Type: <b>{html.escape(atype)}</b>",
    ]
    if email:
        lines.append(f"• Email: <code>{html.escape(email)}</code>")
        # mask password
        pwd = doc.get("password", "")
        if pwd:
            masked = pwd[:2] + "•"*max(3, len(pwd)-4) + pwd[-2:] if len(pwd)>4 else "•"*len(pwd)
            lines.append(f"• Password: <code>{html.escape(masked)}</code> (stored)")
    lines.append(f"• Token: <code>{html.escape(tok_prev)}</code> ({len(tok)} chars)")
    lines.append(f"• Uses: <b>{uses}</b>")
    lines.append(f"• Added: {added}")
    if last_login:
        lines.append(f"• Last login/refresh: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_login))}")
    if last_check:
        lines.append(f"• Last health check: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_check))}")
    lines.append(f"• Last error: <i>{html.escape(last_err[:200])}</i>")
    lines.append("")
    if atype == "email" and email:
        lines.append("✅ Auto-refresh enabled (email/pass se token expire pe auto-renew hoga).")
    else:
        lines.append("⚠️ Manual token — expire pe aapko manually refresh karna hoga ya email/pass add karo.")
    lines.append("\n<i>Use buttons below to refresh or delete.</i>")
    return "\n".join(lines)


async def pool_status_text() -> str:
    tokens = await db.list_tokens()
    healthy = [t for t in tokens if t.get("healthy", True)]
    return (
        "📊 <b>Pool Status — Live</b>\n\n"
        f"• Total accounts (DB): <b>{len(tokens)}</b>\n"
        f"• Healthy: <b>{len(healthy)}</b>  • Unhealthy: <b>{len(tokens)-len(healthy)}</b>\n"
        f"• Pool size (active): <b>{POOL.size}</b>\n"
        f"• Free: <b>{POOL.free_count}</b>  • Busy: <b>{POOL.busy_count}</b>  • Queued: <b>{POOL.waiting_count}</b>\n\n"
        f"<b>How queuing works:</b>\n"
        f"• {POOL.size} accounts = {POOL.size} users ek sath response pa sakte hain.\n"
        f"• Usse zyada users ne bheja to sabko <i>⏳ Analysing your request ...</i> dikhega.\n"
        f"• Har user ka request line-by-line FIFO queue me jayega.\n"
        f"• Jaise hi koi account free hoga, next queued user ka answer start hoga.\n\n"
        f"Busy map: <code>{html.escape(str(POOL.busy_map()))}</code>"
    )


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
