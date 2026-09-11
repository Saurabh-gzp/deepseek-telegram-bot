"""Comprehensive offline tests for bot v4."""
import asyncio, sys, os, time
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STATE_FILE"] = "test_state.json"
if os.path.exists("test_state.json"): os.unlink("test_state.json")

import bot
from bot import (STATE, LAST, OWNER_ID, get_state, RULES,
                 start_cmd, help_cmd, on_button, on_text, on_media, on_voice,
                 MAX_TG_MSG, FILE_THRESHOLD)

# --- v6: DeepSeek access moved behind an async key pool + MongoDB. These
# tests stay offline, so both are stubbed out here, once, before any handler
# runs. install_fakes() then just swaps in the per-test client mock. ---
from contextlib import asynccontextmanager
import bot as _bot
import db as _db_mod

# Real db implementations — _install_stubs() below replaces some of them on
# the shared module object; section 19 needs the originals back.
_REAL_ADD_TURN = _db_mod.add_turn
_REAL_CLEAR_ALL = _db_mod.clear_all_sessions

_FAKE_TURNS = []          # rows record_history() would have written
_CLIENT = MagicMock()     # current stand-in for a DeepSeekClient


class _FakeLease:
    label = "test"
    token = "t"

    @property
    def client(self):
        return _CLIENT


@asynccontextmanager
async def _fake_acquire(uid, timeout=None):
    yield _FakeLease()


async def _fake_ds_op(uid, method, *a, timeout=60.0):
    return getattr(_CLIENT, method)(*a)


async def _noop(*a, **k):
    return None


async def _async_ret(v):
    return v


async def _fake_list_tokens(*a, **k):
    return []


async def _fake_turn(uid, role, text):
    _FAKE_TURNS.append((uid, role, text))


async def _fake_hist(uid, limit=100):
    return [{"role": r, "text": t, "ts": 0}
            for u, r, t in _FAKE_TURNS if u == uid][-limit:]


async def _fake_gate(update):
    """Offline stand-in for the real access gate: owner only."""
    uid = update.effective_user.id
    if uid == OWNER_ID:
        _bot.USERS[uid] = {"status": "active", "role": "admin"}
        return True
    await update.message.reply_text("🔒 This bot is invite-only.")
    return False


def _install_stubs():
    _bot.POOL.acquire = _fake_acquire
    type(_bot.POOL).size = property(lambda self: 1)
    _bot.ds_op = _fake_ds_op
    _bot.gate = _fake_gate
    _bot.db.add_turn = _fake_turn
    _bot.db.get_history = _fake_hist
    for name in ("set_user_field", "bump_usage", "upsert_user",
                 "clear_history", "mark_token", "set_user_status",
                 "bump_token_use", "clear_all_sessions"):
        setattr(_bot.db, name, _noop)
    _bot.db.list_tokens = _fake_list_tokens
    _bot.save_settings = _noop
    _bot.save_session = _noop


_install_stubs()


def install_fakes(client):
    """Point the pool's lease at `client` for the next block of assertions."""
    global _CLIENT
    _CLIENT = client
    _install_stubs()
from personas import PERSONAS

PASS = FAIL = 0
def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name} — {detail}")


def make_bubble():
    m = MagicMock()
    m.message_id = 1000; m.chat_id = OWNER_ID
    m.edit_text = AsyncMock(); m.reply_html = AsyncMock(); m.reply_text = AsyncMock()
    m.edit_message_reply_markup = AsyncMock()
    return m


def mk_ctx():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.send_message = AsyncMock(side_effect=lambda **k: make_bubble())
    ctx.bot.send_voice = AsyncMock()
    ctx.bot.send_audio = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.bot.get_file = AsyncMock()
    ctx.args = []; ctx.user_data = {}
    return ctx


def mk_update(text=None, doc=None, photo=None, caption=None, voice=None, audio=None,
              uid=OWNER_ID):
    u = MagicMock()
    u.effective_user.id = uid; u.effective_user.first_name = "user"
    u.effective_chat.id = uid
    u.message = MagicMock()
    u.message.text = text; u.message.document = doc; u.message.photo = photo
    u.message.caption = caption; u.message.voice = voice; u.message.audio = audio
    u.message.message_id = 500
    u.message.reply_html = AsyncMock()
    u.message.reply_text = AsyncMock(side_effect=lambda *a, **k: make_bubble())
    return u


def mk_query(data, uid=OWNER_ID):
    u = MagicMock(); q = MagicMock()
    q.data = data; q.from_user.id = uid
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = MagicMock(); q.message.chat_id = uid
    q.message.reply_html = AsyncMock(); q.message.reply_text = AsyncMock()
    u.callback_query = q
    u.effective_user.id = uid; u.effective_chat.id = uid; u.message = None
    return u, q


async def run():
    print("\n===== 1. Slash commands =====")
    STATE.clear()
    u = mk_update(); await start_cmd(u, mk_ctx())
    ok("/start replies", u.message.reply_html.called)
    u2 = mk_update(uid=99); await start_cmd(u2, mk_ctx())
    ok("non-owner blocked", u2.message.reply_text.called)

    print("\n===== 2. All toggles =====")
    STATE.clear(); s = get_state(OWNER_ID)
    for tog, attr in [("toggle:think", "thinking"), ("toggle:voice", "voice_reply"),
                       ("toggle:gender", "tts_female"), ("toggle:urls", "auto_urls")]:
        init = getattr(s, attr)
        u, q = mk_query(tog); await on_button(u, mk_ctx())
        ok(f"{tog} flips {attr}", getattr(s, attr) != init)

    print("\n===== 3. Personas =====")
    STATE.clear(); s = get_state(OWNER_ID)
    ok("persona default", s.persona == "default")
    for k in ["tutor", "coder", "friend", "writer"]:
        u, q = mk_query(f"persona:{k}"); await on_button(u, mk_ctx())
        ok(f"persona → {k}", s.persona == k)

    # persona menu
    u, q = mk_query("personas:0"); await on_button(u, mk_ctx())
    ok("persona menu opens", q.edit_message_text.called)

    print("\n===== 4. Modes (removed in new DeepSeek app) =====")
    STATE.clear()
    # DeepSeek removed Instant/Expert/Vision — legacy mode: callbacks must
    # land on 'default' and the menu must show Think/Search, no mode buttons.
    for m in ["expert", "vision", "default"]:
        u, q = mk_query(f"mode:{m}"); await on_button(u, mk_ctx())
        ok(f"legacy mode:{m} → default", get_state(OWNER_ID).model_type == "default")
    kb = bot.main_menu_kb(get_state(OWNER_ID), OWNER_ID)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    ok("no mode buttons", not any("Instant" in l or "Expert" in l or "Vision" in l for l in labels))
    ok("Think toggle present", any("Think" in l for l in labels))
    ok("Search toggle present", any("Search" in l for l in labels))

    print("\n===== 5. Session mgmt (mocked) =====")
    STATE.clear()
    md = MagicMock(); install_fakes(md)
    if True:
        md.create_chat.return_value = "sess-1"
        md.list_chats.return_value = [{"id": "s1", "title": "T", "model_type": "expert"}]
        md.get_history.return_value = ([], "p1")
        md.delete_chat.return_value = True
        md.delete_all_chats.return_value = True

        u, q = mk_query("cmd:new"); await on_button(u, mk_ctx())
        # v7.4: cmd:new is a lazy state reset — session is created later by the
        # streaming account itself (cross-account sessions were invalid).
        ok("new chat (lazy reset)", get_state(OWNER_ID).session_id is None
           and get_state(OWNER_ID).session_key is None)
        u, q = mk_query("chats:0"); c = mk_ctx(); await on_button(u, c)
        ok("chats list", c.user_data.get('chat_list'))
        # v7.5.1: empty list must EDIT a "No cloud chats" panel — the old
        # second q.answer(..., show_alert=True) was ignored by Telegram
        # (one answer per callback query) → button looked dead.
        _orig_list = md.list_chats.return_value
        md.list_chats.return_value = []
        u, q = mk_query("chats:0"); await on_button(u, mk_ctx())
        ok("chats empty → panel edit", q.edit_message_text.called
           and "No cloud chats" in (q.edit_message_text.call_args[0][0]
                                    if q.edit_message_text.call_args else ""))
        md.list_chats.return_value = _orig_list
        u, q = mk_query("switch:0"); c = mk_ctx()
        c.user_data['chat_list'] = md.list_chats.return_value
        await on_button(u, c)
        ok("switch works", get_state(OWNER_ID).session_id == "s1")
        u, q = mk_query("cmd:delete_yes"); await on_button(u, mk_ctx())
        ok("delete", get_state(OWNER_ID).session_id is None)

        # v7.5: wipe = pool-wide — every account in the tokens collection,
        # not just the caller's leased key (app: Data controls → Delete all).
        wipe_res = {"total": 3, "wiped": ["a", "b", "c"], "failed": []}
        wipe_calls = []
        async def fake_wipe():
            wipe_calls.append(1); return wipe_res
        _bot.POOL.wipe_all_accounts = fake_wipe
        cleared = []
        async def fake_clear_all():
            cleared.append(1); return 2
        _bot.db.clear_all_sessions = fake_clear_all
        STATE[OWNER_ID].session_id = "live-sess"
        STATE[OWNER_ID].session_key = "acc-b"
        u, q = mk_query("cmd:wipe_ask"); await on_button(u, mk_ctx())
        ok("wipe ask renders", q.edit_message_text.called)
        u, q = mk_query("cmd:wipe_yes"); await on_button(u, mk_ctx())
        ok("wipe calls pool-wide wipe", bool(wipe_calls))
        ok("wipe clears ALL users' sessions (db)", bool(cleared))
        st = get_state(OWNER_ID)
        ok("wipe resets requester session", st.session_id is None
           and st.session_key is None and st.parent_msg_id is None)
        ok("wipe reports result", q.edit_message_text.called
           and "3/3" in (q.edit_message_text.call_args[0][0]
                         if q.edit_message_text.call_args else ""))

    print("\n===== 6. Text streaming + history =====")
    STATE.clear(); _FAKE_TURNS.clear()
    def gen(*a, **k):
        yield {'type': 'msg_id', 'id': 'r1'}
        yield {'type': 'answer', 'text': 'Hello **world**!'}
    md = MagicMock(); install_fakes(md)
    if True:
        md.create_chat.return_value = "s"
        md.chat_stream = gen
        u = mk_update(text="hi"); await on_text(u, mk_ctx())
        ok("LAST cached", LAST.get(OWNER_ID) is not None)
        ok("history recorded", len([t for t in _FAKE_TURNS if t[0] == OWNER_ID]) == 2)
        ok("msg_count incremented", get_state(OWNER_ID).msg_count == 1)

    print("\n===== 7. Long response → file =====")
    STATE.clear()
    big = "A" * (FILE_THRESHOLD + 2000)
    def gen_big(*a, **k):
        yield {'type': 'msg_id', 'id': 'rb'}
        for i in range(0, len(big), 500):
            yield {'type': 'answer', 'text': big[i:i+500]}
    md = MagicMock(); install_fakes(md)
    if True:
        sfile = AsyncMock()
        _orig_sfile = _bot._send_response_file
        _bot._send_response_file = sfile
        md.create_chat.return_value = "sb"; md.chat_stream = gen_big
        u = mk_update(text="big"); await on_text(u, mk_ctx())
        ok("file sent for huge", sfile.called)
        _bot._send_response_file = _orig_sfile

    print("\n===== 8. Response actions =====")
    STATE.clear(); LAST.clear()
    LAST[OWNER_ID] = {'prompt': 'q', 'raw_answer': 'answer text',
                       'session_id': 's', 'parent': 'p', 'parent_before': 'p0'}

    tts_called = {}
    async def fake_tts(ctx, chat_id, text, female): tts_called['text'] = text
    with patch("bot._send_tts", side_effect=fake_tts):
        u, q = mk_query("rsp:speak"); await on_button(u, mk_ctx())
    ok("speak → TTS", tts_called.get('text') == 'answer text')

    file_called = {}
    async def fake_file(ctx, chat_id, prompt, answer): file_called['a'] = answer
    with patch("bot._send_response_file", side_effect=fake_file):
        u, q = mk_query("rsp:file"); await on_button(u, mk_ctx())
    ok("file button", file_called.get('a') == 'answer text')

    regen = {}
    async def fake_proc(*, ctx, chat_id, user_id, prompt, reply_to_msg_id,
                         is_regen=False, is_quick_action=False):
        regen['prompt'] = prompt; regen['is_regen'] = is_regen
    with patch("bot._process_prompt_chat", side_effect=fake_proc):
        u, q = mk_query("rsp:regen"); await on_button(u, mk_ctx())
    ok("regen reuses prompt", regen.get('prompt') == 'q')
    ok("regen flag set", regen.get('is_regen') is True)

    # quick actions
    for action in ["tr_en", "tr_hi", "summarize", "rephrase", "explain", "continue"]:
        called = {}
        async def cap(*, ctx, chat_id, user_id, prompt, reply_to_msg_id,
                       is_regen=False, is_quick_action=False):
            called['prompt'] = prompt; called['qa'] = is_quick_action
        with patch("bot._process_prompt_chat", side_effect=cap):
            u, q = mk_query(f"qa:{action}"); await on_button(u, mk_ctx())
        ok(f"quick action {action}",
           called.get('qa') is True and 'answer text' in (called.get('prompt') or ''))

    # actions menu open/close
    u, q = mk_query("rsp:actions"); await on_button(u, mk_ctx())
    ok("actions menu opens", q.edit_message_reply_markup.called)
    u, q = mk_query("rsp:back"); await on_button(u, mk_ctx())
    ok("actions menu closes", q.edit_message_reply_markup.called)

    print("\n===== 9. Voice input =====")
    STATE.clear()
    v = MagicMock(); v.file_id = "vfid"
    u = mk_update(voice=v); ctx = mk_ctx()
    tg = AsyncMock(); tg.download_to_drive = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=tg)
    proc = {}
    async def fake_pp(*, ctx, chat_id, user_id, prompt, reply_to_msg_id,
                       is_regen=False, is_quick_action=False):
        proc['prompt'] = prompt
    import stt
    with patch.object(stt, "transcribe", return_value=("Hello test", "en")), \
         patch("bot._process_prompt_chat", side_effect=fake_pp):
        await on_voice(u, ctx)
    ok("voice → transcribe → prompt", proc.get('prompt') == "Hello test")

    print("\n===== 10. Voice-reply auto-TTS =====")
    STATE.clear(); s = get_state(OWNER_ID); s.voice_reply = True
    tts_sent = {}
    async def fake_tts2(ctx, chat_id, text, female): tts_sent['t'] = text
    def gv(*a, **k):
        yield {'type': 'msg_id', 'id': 'rv'}
        yield {'type': 'answer', 'text': 'Voice response'}
    md = MagicMock(); install_fakes(md)
    _bot._send_tts = fake_tts2
    if True:
        md.create_chat.return_value = "sv"; md.chat_stream = gv
        u = mk_update(text="say hi"); await on_text(u, mk_ctx())
    ok("voice_reply auto TTS", tts_sent.get('t') == 'Voice response')

    print("\n===== 11. Thinking OFF regression =====")
    STATE.clear(); s = get_state(OWNER_ID); s.thinking = False
    edits = []
    class B:
        message_id = 42
        async def edit_text(self, text, **kw): edits.append(text)
    tb = B()
    def gl(*a, **k):
        yield {'type': 'msg_id', 'id': 'rl'}
        yield {'type': 'think', 'text': 'LEAK_TEXT'}
        yield {'type': 'answer', 'text': 'clean'}
    ctx = mk_ctx()
    async def _send(**k): return tb
    ctx.bot.send_message = AsyncMock(side_effect=_send)
    md = MagicMock(); install_fakes(md)
    if True:
        md.create_chat.return_value = "sl"; md.chat_stream = gl
        u = mk_update(text="hi"); await on_text(u, ctx)
    ok("no thinking leak", "LEAK_TEXT" not in "\n".join(edits))

    print("\n===== 12. URL detection & auto-fetch =====")
    STATE.clear(); s = get_state(OWNER_ID); s.auto_urls = True
    from urlfetch import extract_urls, is_youtube
    ok("URL detected",
       extract_urls("check https://example.com plz") == ["https://example.com"])
    ok("YouTube detected",
       is_youtube("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    ok("YouTube long URL",
       is_youtube("https://www.youtube.com/watch?v=abcDEFghi01") == "abcDEFghi01")
    ok("no YouTube in normal URL",
       is_youtube("https://example.com") is None)

    print("\n===== 13. URL flow calls fetch + prompt =====")
    STATE.clear(); s = get_state(OWNER_ID); s.auto_urls = True
    fetched = {}
    def fake_fetch(url):
        fetched['url'] = url
        return ("This is the article content about AI. " * 5, "Test Article")
    proc_called = {}
    async def fake_p(*, ctx, chat_id, user_id, prompt, reply_to_msg_id,
                      is_regen=False, is_quick_action=False):
        proc_called['prompt'] = prompt

    # Direct module attribute swap (patch+to_thread has mock semantics issue)
    orig_fetch = bot.fetch_url_text
    orig_pp = bot._process_prompt_chat
    bot.fetch_url_text = fake_fetch
    bot._process_prompt_chat = fake_p
    try:
        u = mk_update(text="summarize https://example.com pls")
        await on_text(u, mk_ctx())
    finally:
        bot.fetch_url_text = orig_fetch
        bot._process_prompt_chat = orig_pp

    ok("URL fetched", fetched.get('url') == 'https://example.com')
    ok("prompt has article content",
       "article content about AI" in (proc_called.get('prompt') or ''))

    print("\n===== 14. URL fetch OFF → normal flow =====")
    STATE.clear(); s = get_state(OWNER_ID); s.auto_urls = False
    urlfetch_called = {}
    def would_fetch(url):
        urlfetch_called['x'] = 1
        return ("", "")
    proc_p = {}
    async def fake_p2(*, ctx, chat_id, user_id, prompt, reply_to_msg_id,
                       is_regen=False, is_quick_action=False):
        proc_p['prompt'] = prompt
    with patch("bot.fetch_url_text", side_effect=would_fetch), \
         patch("bot._process_prompt_chat", side_effect=fake_p2):
        u = mk_update(text="here is url https://example.com and my q")
        await on_text(u, mk_ctx())
    ok("URL fetch skipped when OFF", urlfetch_called.get('x') is None)
    ok("original text used", "https://example.com" in (proc_p.get('prompt') or ''))

    print("\n===== 15. Markdown converter =====")
    from md2tg import md_to_tg_html
    out = md_to_tg_html("**bold** *it* `code` [ln](https://x.com)")
    ok("md bold", "<b>bold</b>" in out)
    ok("md italic", "<i>it</i>" in out)
    ok("md code", "<code>code</code>" in out)
    ok("md link", '<a href="https://x.com">ln</a>' in out)

    print("\n===== 16. TTS module cleanup =====")
    from tts import _pick_voice, _clean_for_speech, VOICES
    ok("hindi voice for hindi text",
       "hi-IN" in _pick_voice("Namaste bhai kaise ho"))
    ok("english voice for english",
       "en-US" in _pick_voice("Hello world today is Monday"))
    c = _clean_for_speech("**bold** and `code` and [link](https://x.com)")
    ok("cleanup strips markdown", "**" not in c and "`" not in c)
    ok("cleanup preserves words", "bold" in c and "code" in c and "link" in c)

    print("\n===== 17. Personas structure =====")
    ok("9+ personas defined", len(PERSONAS) >= 9)
    ok("default persona exists", "default" in PERSONAS)
    for k, p in PERSONAS.items():
        assert 'name' in p and 'emoji' in p and 'desc' in p
    ok("all personas have name/emoji/desc", True)

    from personas import wrap_prompt
    wrapped = wrap_prompt("hello", "coder")
    ok("wrap adds system instruction",
       "System instruction" in wrapped and "hello" in wrapped)
    ok("default persona → no wrap", wrap_prompt("x", "default") == "x")

    print("\n===== 18. App-parity: account switch, edit→regen, think block =====")
    from bot import _think_block_html

    # --- 🧠 collapsible thinking block ---
    blk = _think_block_html("step 1: soch raha hoon\nstep 2: answer", 1200)
    ok("think block expandable",
       blk.startswith("<blockquote expandable>") and "Thinking" in blk)
    esc = _think_block_html("a<b>&c", 500)
    ok("think block escapes html", "&lt;b&gt;" in esc and "a<b>" not in esc)
    ok("think block skips when no room", _think_block_html("abc", 100) == "")
    ok("think block skips when empty", _think_block_html("   ", 500) == "")

    # --- 🔄 account switch ---
    STATE.clear(); s = get_state(OWNER_ID)
    s.session_id = "sess123"; s.session_key = "key1"
    async def fake_switch(uid):
        return "key2"
    _bot.POOL.switch_for = fake_switch
    _bot.POOL.label_index = lambda l: 2
    _bot.POOL.current_label = lambda uid: "key2"
    u, q = mk_query("acct:switch"); await on_button(u, mk_ctx())
    ok("switch drops session + repins key",
       s.session_id is None and s.session_key == "key2" and s.parent_msg_id is None)
    ok("switch confirms", q.answer.called and "#2" in str(q.answer.call_args))
    ok("switch re-renders menu", q.edit_message_text.called)
    # nothing to switch to → graceful refusal
    async def fake_switch_none(uid):
        return None
    _bot.POOL.switch_for = fake_switch_none
    u, q = mk_query("acct:switch"); await on_button(u, mk_ctx())
    ok("switch refused gracefully",
       q.answer.called and not q.edit_message_text.called)
    for attr in ("switch_for", "label_index", "current_label"):
        try: delattr(_bot.POOL, attr)
        except AttributeError: pass

    # --- ✏️ edit message → regen ---
    STATE.clear(); s = get_state(OWNER_ID)
    s.last_prompt_msg_id = 555
    called = {}
    async def fake_proc(**kw):
        called.update(kw)
    with patch.object(_bot, "_process_prompt_chat", side_effect=fake_proc):
        u = mk_update(text="edited prompt text")
        u.edited_message = u.message
        u.edited_message.message_id = 555
        await _bot.on_edited(u, mk_ctx())
    ok("edited last prompt → regen with new text",
       called.get('prompt') == "edited prompt text"
       and called.get('is_regen') is True
       and called.get('reply_to_msg_id') == 555)
    called.clear()
    u = mk_update(text="edit of an older message")
    u.edited_message = u.message
    u.edited_message.message_id = 42
    with patch.object(_bot, "_process_prompt_chat", side_effect=fake_proc):
        await _bot.on_edited(u, mk_ctx())
    ok("older message edit → fresh answer as reply (koi edit ignore nahi)",
       called.get('prompt') == "edit of an older message"
       and called.get('reply_to_msg_id') == 42
       and 'is_regen' not in called and 'edit_regen' not in called)
    called.clear()
    s.last_prompt_msg_id = 42   # real flow: normal prompt path isko set karta hai
    u = mk_update(text="second edit of same message")
    u.edited_message = u.message
    u.edited_message.message_id = 42
    with patch.object(_bot, "_process_prompt_chat", side_effect=fake_proc):
        await _bot.on_edited(u, mk_ctx())
    ok("ab wahi message dobara edit → in-place regen",
       called.get('prompt') == "second edit of same message"
       and called.get('is_regen') is True and called.get('edit_regen') is True)
    called.clear()
    u = mk_update(text="unknown message edit")
    u.edited_message = u.message
    u.edited_message.message_id = 777
    with patch.object(_bot, "_process_prompt_chat", side_effect=fake_proc):
        await _bot.on_edited(u, mk_ctx())
    ok("unknown message edit bhi jawab deta hai",
       called.get('prompt') == "unknown message edit"
       and 'is_regen' not in called)

    # normal prompts record their message id for future edits
    STATE.clear()
    with patch.object(_bot, "_process_prompt_chat_inner", side_effect=fake_proc):
        u = mk_update(text="normal prompt")
        u.message.message_id = 901
        await on_text(u, mk_ctx())
    ok("last prompt msg id tracked",
       get_state(OWNER_ID).last_prompt_msg_id == 901)

    print("\n===== 19. v7.7.1: draft-bubble fix + history auto-delete protection =====")

    # --- native draft: a draft that was EVER sent must ALWAYS be cleared ---
    # (old bug: first draft update OK, a later one fails → on=False → clear
    #  skipped → Telegram kept showing the stuck "..." bubble forever)
    STATE.clear()
    draft_calls = []
    async def draft_fn(**kw):
        draft_calls.append(kw.get("text"))
        if len(draft_calls) == 1:
            return None               # first update lands → draft exists client-side
        raise BadRequest("flood-ish")  # every later update fails
    edits = []
    class B19:
        message_id = 77
        async def edit_text(self, text, **kw): edits.append(text)
    tb19 = B19()
    ctx = mk_ctx()
    ctx.bot.send_message_draft = AsyncMock(side_effect=draft_fn)
    async def _send19(**k): return tb19
    ctx.bot.send_message = AsyncMock(side_effect=_send19)
    def gl19(*a, **k):
        yield {'type': 'msg_id', 'id': 'rd'}
        yield {'type': 'answer', 'text': 'draft answer text'}
    md = MagicMock(); install_fakes(md)
    md.create_chat.return_value = "sd"; md.chat_stream = gl19
    u = mk_update(text="draft test")
    with patch.dict(os.environ, {"NATIVE_STREAM": "1"}):   # opt-in mode
        await on_text(u, ctx)
    ok("draft used during stream (opt-in mode)", len(draft_calls) >= 1
       and any(t for t in draft_calls if t))
    ok("draft ALWAYS cleared at end (no stuck '...')",
       draft_calls[-1] is None)
    ok("fallback answer still delivered",
       any("draft answer text" in e for e in edits))

    # --- default: NATIVE_STREAM OFF — no draft bubble at all ---
    # (the draft bubble needs an explicit clear call to know the answer
    #  finished; under flood it lingers as a stuck "..." and delays buttons.
    #  Classic streaming puts the live text IN the answer bubble instead.)
    STATE.clear()
    draft_calls2 = []
    async def draft_fn2(**kw):
        draft_calls2.append(kw.get("text")); return None
    edits2 = []
    class B19b:
        message_id = 78
        async def edit_text(self, text, **kw): edits2.append(text)
    ctx2 = mk_ctx()
    ctx2.bot.send_message_draft = AsyncMock(side_effect=draft_fn2)
    tb19b = B19b()
    async def _send19b(**k): return tb19b
    ctx2.bot.send_message = AsyncMock(side_effect=_send19b)
    def gl19b(*a, **k):
        yield {'type': 'msg_id', 'id': 'rd2'}
        yield {'type': 'answer', 'text': 'classic answer text'}
    md = MagicMock(); install_fakes(md)
    md.create_chat.return_value = "sd2"; md.chat_stream = gl19b
    os.environ.pop("NATIVE_STREAM", None)     # default → OFF
    u = mk_update(text="classic test"); await on_text(u, ctx2)
    ok("default: zero draft calls (no phantom '...' possible)",
       not draft_calls2)
    ok("default: live text streams in the answer bubble itself",
       any("classic answer text" in e for e in edits2))

    # per-chat persistent draft id (stale draft from a dead request stays
    # reachable/clearable for the next request)
    from bot import _draft_id_for, DRAFT_IDS
    id1 = _draft_id_for(OWNER_ID)
    ok("draft id persistent per chat", _draft_id_for(OWNER_ID) == id1
       and DRAFT_IDS.get(OWNER_ID) == id1)
    ok("draft ids bounded map", len(DRAFT_IDS) < 5000)

    # --- db: admins + daily users exempt from auto-delete ---
    from datetime import datetime, timedelta, timezone
    class _Cur:
        def __init__(self, docs): self._d = list(docs)
        def __aiter__(self): return self
        async def __anext__(self):
            if self._d: return self._d.pop(0)
            raise StopAsyncIteration
    class _R:
        def __init__(self, **kw): self.__dict__.update(kw)
    class _FakeUsers:
        def __init__(self): self.docs = {}
        async def find_one(self, q, p=None): return self.docs.get(q["_id"])
        def find(self, q, p=None):
            cutoff = q["$or"][1]["last_seen"]["$gte"]
            return _Cur([d for d in self.docs.values()
                         if d.get("role") == "admin"
                         or d.get("last_seen", 0) >= cutoff])
        async def update_many(self, q, s):
            nin = q.get("_id", {}).get("$nin", []); n = 0
            for uid, d in self.docs.items():
                if uid not in nin:
                    d.update(s["$set"]); n += 1
            return _R(modified_count=n)
    class _FakeHist:
        def __init__(self): self.rows = []
        async def insert_one(self, doc): self.rows.append(doc)
        async def update_many(self, q, s):
            nin = q["uid"]["$nin"]; n = 0
            for r in self.rows:
                if r["uid"] not in nin and "expires_at" not in r:
                    r["expires_at"] = s["$set"]["expires_at"]; n += 1
            return _R(modified_count=n)
        async def delete_many(self, q):
            nin = q["uid"]["$nin"]; lt = q["expires_at"]["$lt"]
            keep = [r for r in self.rows
                    if r["uid"] in nin or "expires_at" not in r
                    or r["expires_at"] >= lt]
            deleted = len(self.rows) - len(keep)
            self.rows = keep
            return _R(deleted_count=deleted)
    fu, fh = _FakeUsers(), _FakeHist()
    orig_dbs = _db_mod._db
    now = time.time()
    fu.docs = {
        1: {"_id": 1, "role": "admin", "last_seen": now - 999999},  # admin (purana bhi)
        2: {"_id": 2, "role": "user", "last_seen": now - 3600},     # daily user
        3: {"_id": 3, "role": "user", "last_seen": now - 5 * 86400},# inactive
    }
    _db_mod._db = type("DB", (), {"users": fu, "history": fh})()
    try:
        await _REAL_ADD_TURN(1, "user", "admin turn")
        await _REAL_ADD_TURN(2, "user", "daily turn")
        await _REAL_ADD_TURN(3, "user", "inactive turn")
        by = {r["text"]: r for r in fh.rows}
        ok("admin turn: no TTL (never auto-deleted)",
           "expires_at" not in by["admin turn"])
        ok("daily-user turn: no TTL",
           "expires_at" not in by["daily turn"])
        ok("inactive turn: TTL laga",
           "expires_at" in by["inactive turn"])
        prot = await _db_mod.protected_uids()
        ok("protected = admin + daily-active", set(prot) == {1, 2})
        # fail-open: DB down → chat KEEP karo, delete mat karo
        class _Boom:
            async def find_one(self, *a, **k): raise RuntimeError("db down")
        _db_mod._db = type("DB", (), {"users": _Boom(), "history": fh})()
        ok("is_protected fail-open on db error",
           await _db_mod.is_protected_uid(42) is True)
        _db_mod._db = type("DB", (), {"users": fu, "history": fh})()
        # inactive user ka expired turn delete ho, protected kabhi nahi
        for r in fh.rows:
            if "expires_at" in r:
                r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=10)
        removed = await _db_mod.wipe_expired_history(prot)
        ok("expired inactive turn deleted, protected safe",
           removed == 1 and len(fh.rows) == 2
           and all(r["uid"] in (1, 2) for r in fh.rows))
        det = await _REAL_CLEAR_ALL(protected=prot)
        ok("session reset skips protected users",
           det == 1 and "session_id" not in fu.docs[1]
           and fu.docs[3].get("session_id") is None)
        # user 2 churn ho gaya → uske immortal turns ko expiry backfill hoti hai
        fu.docs[2]["last_seen"] = now - 5 * 86400
        prot2 = await _db_mod.protected_uids()
        bf = await _db_mod.backfill_turn_expiry(prot2)
        ok("churned user ke turns ko expiry backfill",
           bf == 1 and all("expires_at" in r for r in fh.rows if r["uid"] == 2))
        ok("admin ke turns phir bhi immortal",
           all("expires_at" not in r for r in fh.rows if r["uid"] == 1))
    finally:
        _db_mod._db = orig_dbs

    # --- scheduler: nightly pass protected-aware hai ---
    import scheduler as sched
    called = {}
    async def f_prot(): return [1, 2]
    async def f_backfill(prot): called["bf"] = list(prot); return 3
    async def f_wipe(prot): called["wipe"] = list(prot); return 7
    async def f_clear(protected=None): called["clear"] = list(protected or []); return 5
    o_p, o_b = _db_mod.protected_uids, _db_mod.backfill_turn_expiry
    o_w, o_c = _db_mod.wipe_expired_history, _db_mod.clear_all_sessions
    _db_mod.protected_uids, _db_mod.backfill_turn_expiry = f_prot, f_backfill
    _db_mod.wipe_expired_history, _db_mod.clear_all_sessions = f_wipe, f_clear
    sent = {}
    class _Bot19:
        async def send_message(self, **k): sent["t"] = str(k.get("text"))
    try:
        st = await sched.run_nightly_once(_Bot19(), OWNER_ID)
    finally:
        _db_mod.protected_uids, _db_mod.backfill_turn_expiry = o_p, o_b
        _db_mod.wipe_expired_history, _db_mod.clear_all_sessions = o_w, o_c
    ok("nightly: protected list wipe+clear tak pahuncha",
       called.get("wipe") == [1, 2] and called.get("clear") == [1, 2])
    ok("nightly: stats sahi", st["removed"] == 7 and st["protected"] == 2)
    ok("nightly: owner report protection batati hai",
       "safe" in sent.get("t", "").lower()
       and "protected" in sent.get("t", "").lower())

    print("\n===== 20. v7.7.3: edited-message crash fix + app-parity edit→regen =====")

    # --- on_text must NEVER process edited_message updates (old bug: PTB's
    # filters.TEXT also matches edits via effective_message → on_text ran
    # with update.message=None → 'NoneType' object has no attribute 'text',
    # aur on_edited kabhi chala hi nahi) ---
    STATE.clear()
    called.clear()
    with patch.object(_bot, "_process_prompt_chat", side_effect=fake_proc):
        u = mk_update(text="x")
        u.message = None
        u.edited_message = MagicMock()
        u.edited_message.text = "edited text"
        crashed = False
        try:
            await on_text(u, mk_ctx())
        except AttributeError:
            crashed = True
    ok("on_text: edited update → no crash, no processing",
       not crashed and not called)

    # --- nightly cleanup ab DEFAULT OFF (user ne mana kiya tha) ---
    from scheduler import nightly_enabled
    _old_nc = os.environ.pop("NIGHTLY_CLEANUP", None)
    try:
        ok("nightly cleanup: default OFF (koi auto-delete nahi)",
           nightly_enabled() is False)
        os.environ["NIGHTLY_CLEANUP"] = "0"
        ok("nightly cleanup: =0 → OFF", nightly_enabled() is False)
        os.environ["NIGHTLY_CLEANUP"] = "1"
        ok("nightly cleanup: sirf explicit opt-in par ON",
           nightly_enabled() is True)
    finally:
        os.environ.pop("NIGHTLY_CLEANUP", None)
        if _old_nc is not None:
            os.environ["NIGHTLY_CLEANUP"] = _old_nc

    # --- edit→regen: answer bubble IN-PLACE replace hota hai (app parity) ---
    STATE.clear()
    s = get_state(OWNER_ID)
    s.last_prompt_msg_id = 555
    s.last_answer_msg_id = 999
    edit_calls = []
    sent_new = []
    async def fake_edit20(**kw):
        edit_calls.append(kw); return True
    def gl20(*a, **k):
        yield {'type': 'msg_id', 'id': 'rd20'}
        yield {'type': 'answer', 'text': 'edited fresh answer'}
    md = MagicMock(); install_fakes(md)
    md.create_chat.return_value = "s20"; md.chat_stream = gl20
    ctx20 = mk_ctx()
    ctx20.bot.edit_message_text = AsyncMock(side_effect=fake_edit20)
    async def _send20(**k):
        sent_new.append(k); return make_bubble()
    ctx20.bot.send_message = AsyncMock(side_effect=_send20)
    amended = {}
    async def fake_amend(uid, **kw):
        amended.update(kw); amended["uid"] = uid
    _o_amend = _db_mod.amend_last_turns
    _db_mod.amend_last_turns = fake_amend
    u = mk_update(text="edited prompt v2")
    u.edited_message = u.message
    u.edited_message.message_id = 555
    try:
        await _bot.on_edited(u, ctx20)
    finally:
        _db_mod.amend_last_turns = _o_amend
    ok("edit→regen: purana answer bubble in-place replace",
       any(c.get("message_id") == 999
           and "edited fresh answer" in str(c.get("text", ""))
           for c in edit_calls))
    ok("edit→regen: koi naya reply bubble nahi bana", not sent_new)
    ok("edit→regen: history amend (edited prompt + fresh answer)",
       amended.get("user_text") == "edited prompt v2"
       and amended.get("assistant_text") == "edited fresh answer"
       and amended.get("uid") == OWNER_ID)
    ok("edit→regen: answer bubble id yaad rakhi",
       get_state(OWNER_ID).last_answer_msg_id == 999)

    # --- fallback: purana bubble gayab ho to naya reply bubble, no crash ---
    STATE.clear()
    s = get_state(OWNER_ID)
    s.last_prompt_msg_id = 556
    s.last_answer_msg_id = 888
    async def boom_edit20(**kw):
        raise BadRequest("message to edit not found")
    sent_fb = []
    ctx_fb = mk_ctx()
    ctx_fb.bot.edit_message_text = AsyncMock(side_effect=boom_edit20)
    async def _sendfb(**k):
        sent_fb.append(k); return make_bubble()
    ctx_fb.bot.send_message = AsyncMock(side_effect=_sendfb)
    def gl_fb(*a, **k):
        yield {'type': 'msg_id', 'id': 'rdfb'}
        yield {'type': 'answer', 'text': 'fallback answer'}
    md = MagicMock(); install_fakes(md)
    md.create_chat.return_value = "sfb"; md.chat_stream = gl_fb
    _db_mod.amend_last_turns = fake_amend
    u = mk_update(text="edited prompt v3")
    u.edited_message = u.message
    u.edited_message.message_id = 556
    crashed = False
    try:
        await _bot.on_edited(u, ctx_fb)
    except Exception:
        crashed = True
    finally:
        _db_mod.amend_last_turns = _o_amend
    ok("edit→regen: dead bubble par fallback reply bubble",
       not crashed and any(k.get("reply_to_message_id") == 556 for k in sent_fb))

    # --- db.amend_last_turns: sirf TEXT badalta hai, ts/role/position safe ---
    class _H20:
        def __init__(self, rows): self.rows = list(rows); self.updates = []
        async def find_one(self, q, sort=None):
            rows = [r for r in self.rows
                    if r["role"] == q["role"] and r["uid"] == q["uid"]]
            return rows[-1] if rows else None
        async def update_one(self, q, sup):
            self.updates.append(q["_id"])
            for r in self.rows:
                if r["_id"] == q["_id"]:
                    r["text"] = sup["$set"]["text"]
    h20 = _H20([
        {"_id": "a", "uid": 7, "role": "user", "text": "old q", "ts": 1},
        {"_id": "b", "uid": 7, "role": "assistant", "text": "old a", "ts": 2},
    ])
    orig_dbs20 = _db_mod._db
    _db_mod._db = type("DB", (), {"history": h20})()
    try:
        await _db_mod.amend_last_turns(7, user_text="new q",
                                       assistant_text="new a")
        by = {r["_id"]: r for r in h20.rows}
        ok("amend: user+assistant text swap in place",
           by["a"]["text"] == "new q" and by["b"]["text"] == "new a")
        ok("amend: ts/role/position untouched",
           by["a"]["ts"] == 1 and by["a"]["role"] == "user"
           and by["b"]["ts"] == 2 and by["b"]["role"] == "assistant")
        await _db_mod.amend_last_turns(99, user_text="no rows for this uid")
        ok("amend: missing rows → no-op", len(h20.updates) == 2)
    finally:
        _db_mod._db = orig_dbs20

    print(f"\n{'='*50}\nRESULTS: {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(run())
