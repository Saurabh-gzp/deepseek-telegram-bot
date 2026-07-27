"""Quick smoke test for DeepSeekClient — no bot."""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from deepseek_client import DeepSeekClient, RULES

TOKEN = os.getenv("DEEPSEEK_TOKEN", "")

print("→ Init client…")
c = DeepSeekClient(TOKEN, workdir=ROOT)
print("✓ WASM & solver ready")

print("→ list_chats()…")
chats = c.list_chats()
print(f"  got {len(chats)} chats")

print("→ create_chat()…")
sid = c.create_chat()
print(f"  session_id: {sid}")
assert sid, "Failed to create session (token might be invalid)"

print("→ chat_stream() with instant + no thinking…")
parent = None
answer_buf = ""
think_buf = ""
for ev in c.chat_stream(sid, parent, "Say hi in 5 words only.",
                        model_type='default', thinking=False, search=False):
    if ev['type'] == 'msg_id':
        parent = ev['id']
    elif ev['type'] == 'think':
        think_buf += ev['text']
    elif ev['type'] == 'answer':
        answer_buf += ev['text']
    elif ev['type'] == 'error':
        print(f"  ERROR: {ev['msg']}"); sys.exit(1)

print(f"  think ({len(think_buf)} chars): {think_buf[:120]!r}")
print(f"  answer ({len(answer_buf)} chars): {answer_buf[:200]!r}")
assert answer_buf, "No answer received!"

print("→ delete_chat()…")
ok = c.delete_chat(sid)
print(f"  deleted: {ok}")

print("\n✅ ALL TESTS PASSED")
