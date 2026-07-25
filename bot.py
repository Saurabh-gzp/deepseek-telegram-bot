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
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      BotCommand, LinkPreviewOptions)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from deepseek_client import DeepSeekClient, RULES
from md2tg import md_to_tg_html, strip_incomplete_markers, safe_for_telegram
from personas import PERSONAS, get_persona, wrap_prompt
from urlfetch import extract_urls, is_youtube, fetch_url_text, fetch_youtube_transcript

# ---------- Config ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_TOKEN = os.getenv("DEEPSEEK_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
WORKDIR = os.path.dirname(os.path.abspath(__file__))
ENABLE_HEALTH = os.getenv("ENABLE_HEALTH", "0") == "1"

if not TELEGRAM_TOKEN or not DEEPSEEK_TOKEN or not OWNER_ID:
    raise SystemExit(
        "❌ Missing required env vars.\n"
        "Set TELEGRAM_TOKEN, DEEPSEEK_TOKEN, and OWNER_ID.\n"
        "Copy .env.example → .env and fill in the values, or set them in "
        "your host's dashboard (Render / Railway / etc.)."
    )

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

ds = DeepSeekClient(DEEPSEEK_TOKEN, workdir=WORKDIR)

# In-memory response cache per user (for regenerate/tts/file/quick actions)
LAST: Dict[int, dict] = {}
# Chat history (last N exchanges) per user for /export
HISTORY: Dict[int, List[dict]] = {}
MAX_HISTORY = 100


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

STATE: Dict[int, UserState] = {}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
            for k, v in raw.items():
                v = {kk: vv for kk, vv in v.items()
                     if kk in UserState.__dataclass_fields__}
                STATE[int(k)] = UserState(**v)
            log.info("Loaded state for %d user(s)", len(STATE))
        except Exception as e:
            log.warning("state load failed: %s", e)

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({str(k): asdict(v) for k, v in STATE.items()}, f, indent=2)
    except Exception as e:
        log.warning("state save failed: %s", e)

def get_state(uid: int) -> UserState:
    if uid not in STATE:
        STATE[uid] = UserState()
    return STATE[uid]

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


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
    lines.append("👇 <i>Message/voice/photo/file/URL bhejo — sab handle karta hoon.</i>")
    return "\n".join(lines)


def main_menu_kb(s: UserState) -> InlineKeyboardMarkup:
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
            InlineKeyboardButton("🔁 Regen", callback_data="rsp:regen"),
        ])
        rows.append([
            InlineKeyboardButton("⚡ Quick actions", callback_data="rsp:actions"),
        ])
    rows.append([
        InlineKeyboardButton("🆕 New", callback_data="cmd:new"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmd:refresh"),
        InlineKeyboardButton("❓ Help", callback_data="cmd:help"),
    ])
    return InlineKeyboardMarkup(rows)


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


HELP_TEXT = (
"<b>🤖 DeepSeek Bot — Complete Guide</b>\n\n"
"<b>Input types (sab handle karta hoon):</b>\n"
"📝 Text — normal chat\n"
"🎤 Voice — Whisper transcribe → DeepSeek\n"
"🖼 Photo — Vision mode me analyze\n"
"📎 Document — upload + question caption\n"
"🔗 URL — auto fetch aur summarize\n"
"▶️ YouTube link — transcript se summary\n\n"
"<b>Modes:</b>\n"
"🚀 Instant — fast, supports search+files\n"
"💎 Expert — deep reasoning\n"
"👁 Vision — images/docs\n\n"
"<b>Personas 🎭 (9 options):</b>\n"
"Default, Tutor, Coder, Dost, Writer, Translator, "
"Comedian, Scientist, Startup Coach, Health Info\n\n"
"<b>Response buttons (har jawab pe):</b>\n"
"🔊 <b>Speak</b> — voice message me sun lo\n"
"📄 <b>File</b> — .md file me download\n"
"🔁 <b>Regen</b> — same sawaal, alag jawab\n"
"⚡ <b>Quick actions</b> — Translate/Summarize/Rephrase/Explain/Continue\n\n"
"<b>Toggles:</b>\n"
"🧠 Think — reasoning chain\n"
"🌐 Search — real-time web\n"
"🔊 Voice-reply — sab replies voice me bhi\n"
"👤 Male/Female voice\n"
"🔗 URL fetch — automatic vs manual\n\n"
"<b>Chat mgmt:</b>\n"
"🆕 New · 📁 My Chats · 🗑 Delete · 💥 Wipe · 📤 Export .md\n\n"
"<b>Long responses:</b>\n"
"3800–10000 chars → multi-bubble\n"
"10000+ → auto .md file\n\n"
"<b>Slash commands (sirf 2):</b>\n"
"/start — menu\n/help — ye page\n\n"
"<i>💡 Tip: Simply text likho, main sab detect karta hoon.</i>"
)


# ---------- Utility ----------
async def send_menu(target, s: UserState, edit: bool = False):
    text = status_text(s)
    kb = main_menu_kb(s)
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


def record_history(uid: int, role: str, text: str):
    lst = HISTORY.setdefault(uid, [])
    lst.append({"role": role, "text": text, "ts": time.time()})
    if len(lst) > MAX_HISTORY:
        del lst[:len(lst) - MAX_HISTORY]


# ---------- Slash commands ----------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Personal bot."); return
    await send_menu(update, get_state(update.effective_user.id))

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="cmd:refresh")]])
    await update.message.reply_html(HELP_TEXT, reply_markup=kb)


# ---------- Button handler ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not is_owner(q.from_user.id):
        if q: await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    s = get_state(q.from_user.id)

    # --- mode/toggles ---
    if data.startswith("mode:"):
        m = data.split(":", 1)[1]
        if m in RULES:
            s.model_type = m
            r = RULES[m]
            if not r['supports_search']: s.search = False
            if not r['supports_files']: s.attached_files = []
            save_state()
        await send_menu(q, s, edit=True); return

    if data == "toggle:think":
        s.thinking = not s.thinking; save_state(); await send_menu(q, s, edit=True); return
    if data == "toggle:voice":
        s.voice_reply = not s.voice_reply; save_state(); await send_menu(q, s, edit=True); return
    if data == "toggle:gender":
        s.tts_female = not s.tts_female; save_state(); await send_menu(q, s, edit=True); return
    if data == "toggle:urls":
        s.auto_urls = not s.auto_urls; save_state(); await send_menu(q, s, edit=True); return

    if data == "toggle:search":
        if not RULES[s.model_type]['supports_search']:
            await q.answer("Search blocked in this mode", show_alert=True); return
        if s.attached_files:
            await q.answer("Files attached — detach first", show_alert=True); return
        s.search = not s.search; save_state(); await send_menu(q, s, edit=True); return

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
            s.persona = key; save_state()
            await q.answer(f"Persona: {PERSONAS[key]['name']}")
        await send_menu(q, s, edit=True); return

    # --- commands ---
    if data in ("cmd:refresh", "cmd:menu"):
        try: await send_menu(q, s, edit=True)
        except: await q.message.reply_html(status_text(s), reply_markup=main_menu_kb(s))
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
        s.attached_files = []; save_state()
        await q.answer("Detached"); await send_menu(q, s, edit=True); return

    if data == "cmd:new":
        sid = await asyncio.to_thread(ds.create_chat)
        if sid:
            s.session_id = sid; s.parent_msg_id = None; s.attached_files = []
            save_state(); await q.answer("New chat started")
        else: await q.answer("Failed", show_alert=True)
        try: await send_menu(q, s, edit=True)
        except: await q.message.reply_html(status_text(s), reply_markup=main_menu_kb(s))
        return

    if data == "cmd:stats":
        stats = (
            f"📊 <b>Your stats</b>\n\n"
            f"Messages sent: <b>{s.msg_count}</b>\n"
            f"Chars sent: <b>{s.total_chars_in:,}</b>\n"
            f"Chars received: <b>{s.total_chars_out:,}</b>\n"
            f"Persona: <b>{get_persona(s.persona)['name']}</b>\n"
            f"Mode: <b>{RULES[s.model_type]['name']}</b>\n"
            f"History cached: <b>{len(HISTORY.get(q.from_user.id, []))}</b> msgs"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="cmd:refresh")]])
        try: await q.edit_message_text(stats, parse_mode="HTML", reply_markup=kb)
        except BadRequest: pass
        return

    if data == "cmd:export":
        hist = HISTORY.get(q.from_user.id, [])
        if not hist:
            await q.answer("No history yet", show_alert=True); return
        content = "# DeepSeek Chat Export\n\n"
        for h in hist:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h['ts']))
            role = "👤 You" if h['role'] == 'user' else "🤖 Bot"
            content += f"### {role} — {ts}\n\n{h['text']}\n\n---\n\n"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md",
                                          encoding="utf-8") as f:
            f.write(content); path = f.name
        try:
            with open(path, "rb") as fh:
                await ctx.bot.send_document(chat_id=q.message.chat_id, document=fh,
                    filename=f"chat_export_{int(time.time())}.md",
                    caption=f"📤 Exported {len(hist)} messages")
        finally:
            try: os.unlink(path)
            except: pass
        return

    # --- chats list ---
    if data.startswith("chats:"):
        page = int(data.split(":", 1)[1])
        chats = await asyncio.to_thread(ds.list_chats)
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
        chats = ctx.user_data.get('chat_list') or await asyncio.to_thread(ds.list_chats)
        if idx >= len(chats):
            await q.answer("Out of range", show_alert=True); return
        c = chats[idx]
        s.session_id = c['id']
        _, last = await asyncio.to_thread(ds.get_history, c['id'])
        s.parent_msg_id = last
        m = c.get('model_type', 'default')
        if m in RULES: s.model_type = m
        save_state()
        await q.answer(f"Switched: {(c.get('title') or 'Untitled')[:25]}")
        await send_menu(q, s, edit=True); return

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
            ok = await asyncio.to_thread(ds.delete_chat, s.session_id)
            if ok:
                s.session_id = None; s.parent_msg_id = None; s.attached_files = []
                save_state(); await q.answer("Deleted")
            else: await q.answer("Failed", show_alert=True)
        await send_menu(q, s, edit=True); return

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
        ok = await asyncio.to_thread(ds.delete_all_chats)
        for st in STATE.values():
            st.session_id = None; st.parent_msg_id = None
        save_state()
        await q.answer("Wiped" if ok else "Failed", show_alert=True)
        await send_menu(q, s, edit=True); return

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
        await _send_response_file(ctx, q.message.chat_id,
                                   last.get('prompt', ''), last['raw_answer'])
        return

    if data == "rsp:regen":
        last = LAST.get(q.from_user.id)
        if not last or not last.get('prompt'):
            await q.answer("Nothing to regenerate", show_alert=True); return
        await q.answer("Regenerating…")
        s.parent_msg_id = last.get('parent_before')
        save_state()
        await _process_prompt_chat(
            ctx=ctx, chat_id=q.message.chat_id, user_id=q.from_user.id,
            prompt=last['prompt'], reply_to_msg_id=None, is_regen=True,
        )
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


# ---------- Voice input ----------
async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    msg = update.message
    voice = msg.voice or msg.audio
    if not voice: return

    status = await msg.reply_text("🎤 Sun raha hoon…")

    tg_file = await ctx.bot.get_file(voice.file_id)
    if msg.voice:
        suffix = ".ogg"
    else:
        mt = getattr(voice, 'mime_type', '') or ''
        suffix = "." + (mt.split('/')[-1] or 'mp3')

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        from stt import transcribe
        text, lang = await asyncio.to_thread(transcribe, tmp_path)
    except Exception as e:
        log.exception("STT failed")
        await status.edit_text(f"❌ STT failed: {html.escape(str(e))}",
                                parse_mode="HTML")
        return
    finally:
        try: os.unlink(tmp_path)
        except: pass

    if not text:
        await status.edit_text("🎤 Couldn't detect speech. Try again clearly.")
        return

    await status.edit_text(
        f"🗣 <b>You said</b> <i>({lang})</i>:\n{html.escape(text)}",
        parse_mode="HTML")
    await _process_prompt_chat(
        ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
        prompt=text, reply_to_msg_id=msg.message_id,
    )


# ---------- Media ----------
async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
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
    status = await msg.reply_text(f"⏳ Uploading '{html.escape(file_name)}'…",
                                    parse_mode="HTML")

    tg_file = await ctx.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix="_" + file_name) as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        await status.edit_text(
            f"⏳ Uploading '{html.escape(file_name)}'…\n"
            f"<i>Parse ho raha hai, thoda ruko…</i>", parse_mode="HTML")
        fid, fname = await asyncio.to_thread(ds.upload_file, tmp_path)
    finally:
        try: os.unlink(tmp_path)
        except: pass

    if not fid:
        await status.edit_text(
            "❌ Upload failed or file couldn't be parsed by DeepSeek.\n"
            "<i>Try a different format (txt/pdf/jpg/png).</i>",
            parse_mode="HTML")
        return

    s.attached_files.append([fid, fname]); save_state()

    if caption:
        await status.edit_text(
            f"✅ Attached '{html.escape(fname)}'. Processing…", parse_mode="HTML")
        await _process_prompt_chat(
            ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
            prompt=caption, reply_to_msg_id=msg.message_id,
        )
    else:
        await status.edit_text(
            f"✅ Attached: <b>{html.escape(fname)}</b>\nAb sawaal likho.",
            parse_mode="HTML")


# ---------- Text ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    text = update.message.text
    s = get_state(update.effective_user.id)

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
    status = await msg.reply_text(
        f"🔗 Fetching {'YouTube' if yt_id else 'page'}…")
    try:
        if yt_id:
            text, _ = await asyncio.to_thread(fetch_youtube_transcript, yt_id)
            source_desc = f"YouTube video: {url}"
        else:
            text, title = await asyncio.to_thread(fetch_url_text, url)
            source_desc = f"URL: {url}" + (f"\nTitle: {title}" if title else "")

        if not text or len(text) < 50:
            await status.edit_text(f"❌ Couldn't extract useful content from {url}")
            return False

        if len(text) > URL_TEXT_CAP:
            text = text[:URL_TEXT_CAP] + "\n\n[…truncated]"

        await status.edit_text(
            f"✅ Fetched {len(text):,} chars. Asking DeepSeek…")

        # Build a good prompt
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
        await _process_prompt_chat(
            ctx=ctx, chat_id=update.effective_chat.id, user_id=update.effective_user.id,
            prompt=prompt, reply_to_msg_id=msg.message_id,
        )
        return True
    except Exception as e:
        log.warning("URL fetch failed: %s", e)
        try:
            await status.edit_text(
                f"⚠️ Couldn't fetch URL ({html.escape(str(e))}). "
                "Processing message as-is…", parse_mode="HTML")
        except: pass
        return False


# ---------- Send helpers ----------
async def _send_tts(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                     female: bool):
    from tts import synthesize_ogg
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f:
        ogg_path = f.name
    try:
        await synthesize_ogg(text, ogg_path, prefer_female=female)
        with open(ogg_path, "rb") as fh:
            await ctx.bot.send_voice(chat_id=chat_id, voice=fh)
    except Exception as e:
        log.exception("TTS failed")
        await ctx.bot.send_message(chat_id=chat_id, text=f"❌ TTS failed: {e}")
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

    s = get_state(user_id)

    if not s.session_id:
        sid = await asyncio.to_thread(ds.create_chat)
        if not sid:
            await ctx.bot.send_message(chat_id=chat_id, text="❌ Session creation failed")
            return
        s.session_id = sid; s.parent_msg_id = None; save_state()

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
        record_history(user_id, "user", prompt)

    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    placeholder = await ctx.bot.send_message(
        chat_id=chat_id, text="⏳ …",
        reply_to_message_id=reply_to_msg_id,
    )

    think_buf = ""
    answer_started = False
    messages = [placeholder]
    current_text = ""
    last_edit = 0.0
    edit_interval = 1.3

    async def stream_iter():
        loop = asyncio.get_event_loop()
        gen = ds.chat_stream(
            s.session_id, s.parent_msg_id, final_prompt,
            model_type=mode, thinking=thinking_on, search=search_on,
            file_ids=file_ids,
        )
        while True:
            ev = await loop.run_in_executor(None, next, gen, None)
            if ev is None: break
            yield ev

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
    try:
        got_any = False
        async for ev in stream_iter():
            got_any = True
            if ev['type'] == 'msg_id':
                s.parent_msg_id = ev['id']; continue
            if ev['type'] == 'error':
                await safe_edit(messages[-1], f"❌ {html.escape(ev['msg'])}",
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
                await safe_edit(messages[-1], txt)

        if not got_any:
            await safe_edit(messages[-1], "❌ No response from DeepSeek.",
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
            record_history(user_id, "assistant", full_answer)

        s.total_chars_out += len(full_answer); save_state()

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
        s.attached_files = []; save_state()

    except Exception as e:
        log.exception("stream error")
        try:
            await safe_edit(messages[-1], f"❌ Error: {html.escape(str(e))}",
                             kb=response_footer_kb(has_text=False))
        except: pass


# ---------- Post-init ----------
async def _post_init(app):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Menu kholo"),
            BotCommand("help", "Guide"),
        ])
        await app.bot.send_message(
            chat_id=OWNER_ID,
            text="🚀 <b>Bot v4 LIVE!</b>\n\n"
                 "✨ New: Personas 🎭, URL/YouTube summarize 🔗, "
                 "Quick actions ⚡, Export 📤\n\n"
                 "Fixes: voice quality upgraded, single-voice-message bug fixed.\n\n"
                 "/start for menu.",
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("post_init: %s", e)


def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


async def _run_with_health():
    """Run bot polling + tiny HTTP server (for Render Web Service)."""
    from health import start_health_server
    load_state()
    app = build_app()
    runner = await start_health_server()
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES,
                                          drop_pending_updates=True)
        log.info("Bot + health running…")
        while True:
            await asyncio.sleep(3600)
    finally:
        try: await app.updater.stop()
        except: pass
        try: await app.stop()
        except: pass
        try: await app.shutdown()
        except: pass
        try: await runner.cleanup()
        except: pass


def main():
    if ENABLE_HEALTH:
        asyncio.run(_run_with_health())
    else:
        load_state()
        app = build_app()
        log.info("Bot starting (polling only)…")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
