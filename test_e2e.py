"""
End-to-end integration tests for v4 — real DeepSeek + real TTS + real Whisper.
Also tests: URL fetch (with real trafilatura), YouTube (if available).
"""
import sys, os, asyncio, tempfile
sys.path.insert(0, os.path.dirname(__file__))
os.environ["STATE_FILE"] = "e2e_state.json"
if os.path.exists("e2e_state.json"): os.unlink("e2e_state.json")

PASS = FAIL = 0
def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name} — {detail}")


async def main():
    print("\n===== 1. Real DeepSeek chat =====")
    from deepseek_client import DeepSeekClient
    c = DeepSeekClient(os.getenv("DEEPSEEK_TOKEN", ""),
                       workdir=os.path.dirname(__file__))
    sid = c.create_chat()
    ok("session created", sid is not None)

    answer = ""
    for ev in c.chat_stream(sid, None, "Reply with just the word: banana",
                            model_type='default', thinking=False):
        if ev['type'] == 'answer': answer += ev['text']
    ok("DeepSeek streamed answer", "banana" in answer.lower(),
       f"got: {answer!r}")

    print("\n===== 2. Real TTS → OGG-Opus =====")
    from tts import synthesize_ogg
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg"); tmp.close()
    try:
        await synthesize_ogg("Hello world, this is a test.", tmp.name,
                             prefer_female=True)
        size = os.path.getsize(tmp.name)
        with open(tmp.name, "rb") as f: header = f.read(4)
        ok("TTS produced OGG", header == b"OggS", f"header: {header!r}")
        ok("TTS size reasonable", 2000 < size < 500000, f"size: {size}")

        # Hinglish
        await synthesize_ogg("Namaste bhai, kaise ho aap? Aaj kya karna hai?",
                              tmp.name, prefer_female=False)
        size2 = os.path.getsize(tmp.name)
        ok("Hinglish TTS works", size2 > 2000)
    finally:
        try: os.unlink(tmp.name)
        except: pass

    print("\n===== 3. Whisper STT =====")
    from stt import transcribe
    # Generate a known TTS in English, transcribe it
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg"); tmp.close()
    try:
        await synthesize_ogg("The quick brown fox jumps over the lazy dog.",
                              tmp.name, prefer_female=True)
        text, lang = await asyncio.to_thread(transcribe, tmp.name)
        ok("Whisper transcribed", "fox" in text.lower() or "quick" in text.lower(),
           f"got: {text!r}")
        ok("English detected", lang == "en", f"got: {lang}")
    finally:
        try: os.unlink(tmp.name)
        except: pass

    print("\n===== 4. URL fetch =====")
    from urlfetch import fetch_url_text, is_youtube
    ok("YouTube id extract",
       is_youtube("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk")

    # Try a real fetch (example.com is public and simple)
    try:
        text, title = await asyncio.to_thread(fetch_url_text, "https://example.com")
        ok("URL fetch works", len(text) > 10, f"text: {text[:100]!r}")
    except Exception as e:
        ok("URL fetch works", False, f"error: {e}")

    print("\n===== 5. Markdown converter =====")
    from md2tg import md_to_tg_html
    out = md_to_tg_html("**bold**\n- item1\n- item2\n\n```py\ncode\n```")
    ok("multi-feature md",
       "<b>bold</b>" in out and "• item1" in out and "<pre>" in out)

    print("\n===== 6. Personas =====")
    from personas import PERSONAS, wrap_prompt
    ok("all personas load", len(PERSONAS) >= 9)
    ok("wrap non-default", "System instruction" in wrap_prompt("q", "coder"))

    print("\n===== 7. Cleanup DeepSeek session =====")
    c.delete_chat(sid)
    ok("session deleted", True)

    print(f"\n{'='*60}\nE2E RESULTS: {PASS} passed, {FAIL} failed\n{'='*60}")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(main())
