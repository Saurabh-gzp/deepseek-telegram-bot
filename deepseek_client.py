"""
DeepSeek client library — refactored from the original terminal script.
All I/O is programmatic (no prints) so it can be embedded in a Telegram bot.
"""
import json
import os
import subprocess
import time as _time
import urllib.request
import uuid
from typing import Optional, Iterator, List, Tuple, Dict, Any

import requests


def login_with_credentials(email: str, password: str) -> Optional[str]:
    """
    Login to DeepSeek with email/password, return session token or None.
    Uses Android API endpoint as in v5.2 terminal client.
    """
    url = "https://chat.deepseek.com/api/v0/users/login"
    login_headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'DeepSeek/2.0.2 (Android; API)',
        'Origin': 'https://chat.deepseek.com',
        'Referer': 'https://chat.deepseek.com/'
    }
    data = {
        'email': email,
        'password': password,
        'device_id': str(uuid.uuid4()),
        'os': 'Android'
    }
    try:
        resp = requests.post(url, headers=login_headers, json=data, timeout=20)
        if resp.status_code == 200:
            result = resp.json()
            if result.get('code') == 0 and 'data' in result:
                token = result['data'].get('biz_data', {}).get('user', {}).get('token')
                if token:
                    return token
        return None
    except Exception:
        return None


def validate_token(token: str, workdir: str = ".") -> bool:
    """Quick validation: try to fetch chat list with token."""
    try:
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'X-Client-Platform': 'web',
            'X-Client-Version': '2.0.2',
            'Origin': 'https://chat.deepseek.com',
            'Referer': 'https://chat.deepseek.com/'
        }
        r = requests.get("https://chat.deepseek.com/api/v0/chat_session/fetch_page",
                         headers=headers, timeout=15)
        if r.status_code == 200:
            j = r.json()
            # biz_code 0 means valid
            if j.get('code') == 0:
                return True
        if r.status_code in (401, 403):
            return False
        return False
    except Exception:
        return False

WASM_FILENAME = "sha3_wasm_bg.7b9ca65ddd.wasm"
JS_SOLVER_FILENAME = "pow_solver.js"
WASM_URL = "https://raw.githubusercontent.com/xtekky/deepseek4free/main/dsk/wasm/sha3_wasm_bg.7b9ca65ddd.wasm"

RULES = {
    'default': {
        'supports_thinking': True, 'supports_search': True, 'supports_files': True,
        'name': 'INSTANT',
        'desc': 'Fast, standard responses (Search & Files supported)',
        'emoji': '🚀',
    },
    'expert': {
        'supports_thinking': True, 'supports_search': False, 'supports_files': False,
        'name': 'EXPERT',
        'desc': 'Complex reasoning & coding (Search & Files BLOCKED)',
        'emoji': '💎',
    },
    'vision': {
        'supports_thinking': True, 'supports_search': False, 'supports_files': True,
        'name': 'VISION',
        'desc': 'Image/document understanding (Search BLOCKED)',
        'emoji': '👁️',
    },
}

JS_SOLVER_CODE = r"""
const fs = require('fs');
const WASM_PATH = 'sha3_wasm_bg.7b9ca65ddd.wasm';
async function main() {
  const input = process.argv[2];
  if (!input) process.exit(1);
  const config = JSON.parse(input);
  const wasmBuffer = fs.readFileSync(WASM_PATH);
  const wasmModule = await WebAssembly.compile(wasmBuffer);
  const instance = await WebAssembly.instantiate(wasmModule, {});
  const mem = instance.exports.memory;
  const prefix = `${config.salt}_${config.expire_at}_`;
  function writeString(str) {
    const encoded = Buffer.from(str, 'utf-8');
    const length = encoded.length;
    const ptr = instance.exports.__wbindgen_export_0(length, 1);
    const view = new Uint8Array(mem.buffer);
    for (let i = 0; i < length; i++) view[ptr + i] = encoded[i];
    return { ptr, length };
  }
  const retptr = instance.exports.__wbindgen_add_to_stack_pointer(-16);
  try {
    const challengeInfo = writeString(config.challenge);
    const prefixInfo = writeString(prefix);
    instance.exports.wasm_solve(retptr, challengeInfo.ptr, challengeInfo.length,
      prefixInfo.ptr, prefixInfo.length, config.difficulty);
    const view = new Int32Array(mem.buffer);
    const status = view[retptr / 4];
    if (status === 0) process.exit(1);
    const floatView = new Float64Array(mem.buffer);
    const value = floatView[(retptr + 8) / 8];
    const answer = Math.floor(value);
    const result = {
      algorithm: config.algorithm, challenge: config.challenge, salt: config.salt,
      answer: answer, signature: config.signature, target_path: config.target_path
    };
    console.log(Buffer.from(JSON.stringify(result)).toString('base64'));
  } finally {
    instance.exports.__wbindgen_add_to_stack_pointer(16);
  }
}
main().catch(err => { console.error(err); process.exit(1); });
"""


def ensure_pow_files(workdir: str = "."):
    wasm_path = os.path.join(workdir, WASM_FILENAME)
    js_path = os.path.join(workdir, JS_SOLVER_FILENAME)
    if not os.path.exists(wasm_path):
        urllib.request.urlretrieve(WASM_URL, wasm_path)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(JS_SOLVER_CODE)


class DeepSeekClient:
    BASE = "https://chat.deepseek.com/api/v0"

    def __init__(self, token: str, workdir: str = "."):
        self.token = token
        self.workdir = workdir
        ensure_pow_files(workdir)
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'X-Client-Platform': 'web',
            'X-Client-Version': '2.0.2',
            'Origin': 'https://chat.deepseek.com',
            'Referer': 'https://chat.deepseek.com/',
        }

    # ---------- PoW ----------
    def _solve_pow(self, config: dict) -> Optional[str]:
        input_json = json.dumps(config)
        try:
            result = subprocess.run(
                ['node', JS_SOLVER_FILENAME, input_json],
                capture_output=True, text=True, cwd=self.workdir, timeout=15,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "Node.js not found. The DeepSeek PoW WASM solver requires "
                "Node.js (>=18). On Render, use runtime: docker with the "
                "provided Dockerfile (which installs Node)."
            )
        out = result.stdout.strip()
        return out or None

    def _pow_header(self, target_path: str) -> Optional[str]:
        r = requests.post(f"{self.BASE}/chat/create_pow_challenge",
                          headers=self.headers, json={'target_path': target_path}, timeout=30)
        if r.status_code == 200:
            cfg = r.json().get('data', {}).get('biz_data', {}).get('challenge', {})
            return self._solve_pow(cfg)
        return None

    # ---------- Session mgmt ----------
    def list_chats(self) -> List[Dict[str, Any]]:
        r = requests.get(f"{self.BASE}/chat_session/fetch_page", headers=self.headers, timeout=30)
        if r.status_code == 200:
            return r.json().get('data', {}).get('biz_data', {}).get('chat_sessions', [])
        return []

    def get_history(self, sess_id: str) -> Tuple[List[dict], Optional[str]]:
        r = requests.get(f"{self.BASE}/chat/history_messages",
                         headers=self.headers, params={'chat_session_id': sess_id}, timeout=30)
        if r.status_code == 200:
            d = r.json().get('data', {}).get('biz_data', {})
            return d.get('chat_messages', []), d.get('chat_session', {}).get('current_message_id')
        return [], None

    def create_chat(self) -> Optional[str]:
        r = requests.post(f"{self.BASE}/chat_session/create",
                          headers=self.headers, json={'character_id': None}, timeout=30)
        if r.status_code == 200:
            return r.json()['data']['biz_data']['chat_session']['id']
        return None

    def delete_chat(self, sess_id: str) -> bool:
        r = requests.post(f"{self.BASE}/chat_session/delete",
                          headers=self.headers, json={'chat_session_id': sess_id}, timeout=30)
        return r.status_code == 200

    def delete_all_chats(self) -> bool:
        r = requests.post(f"{self.BASE}/chat_session/delete_all",
                          headers=self.headers, json={}, timeout=30)
        return r.status_code == 200

    # ---------- File upload ----------
    # Terminal statuses returned by /file/fetch_files. Anything not in this set
    # means DeepSeek is still working on the file, so we keep polling.
    _TERMINAL_OK = {'SUCCESS'}
    _TERMINAL_FAIL = {
        'FAILED',
        'CONTENT_EMPTY',    # image/PDF parsed but no text could be extracted (OCR found nothing)
        'CONTENT_TOO_LONG',
        'UNSUPPORTED',
        'AUDIT_FAILED',     # content moderation rejected the file
        'AUDIT_BLOCKED',
        'PARSE_FAILED',
        'EXPIRED',
    }

    # Human-readable reasons surfaced to the bot UI
    _FAIL_REASON = {
        'CONTENT_EMPTY':
            "DeepSeek could not extract any text from this image.\n"
            "Its file upload is OCR-only — the image must contain clearly "
            "readable text. Blank or purely graphical images will not work.",
        'CONTENT_TOO_LONG': "File is too large — it exceeds DeepSeek's content limit.",
        'UNSUPPORTED': "DeepSeek does not support this file format.",
        'AUDIT_FAILED': "DeepSeek's content moderation rejected this file.",
        'AUDIT_BLOCKED': "DeepSeek's content moderation blocked this file.",
        'PARSE_FAILED': "DeepSeek could not parse this file (it may be corrupt).",
        'FAILED': "File processing failed on DeepSeek's side.",
        'EXPIRED': "The upload expired — please send the file again.",
        'TIMEOUT': "DeepSeek did not finish parsing in time. Try again shortly.",
        'UPLOAD_HTTP': "The upload request was rejected by the DeepSeek server.",
        'POW': "Could not solve the security challenge (PoW). Check that Node.js is installed.",
    }

    def upload_file_ex(self, file_path: str, mime_type: str = None,
                       wait_for_ready: bool = True, poll_timeout: float = 90.0
                       ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Upload file to DeepSeek. Returns (file_id, file_name, error_reason).

        error_reason is None on success, otherwise a human-readable string.

        DeepSeek requires:
          - multipart field name = "text" (not "file")
          - polling until status == "SUCCESS" before ref_file_ids can be used
        """
        pow_resp = self._pow_header('/api/v0/file/upload_file')
        if not pow_resp:
            return None, None, self._FAIL_REASON['POW']
        h = self.headers.copy()
        h['x-ds-pow-response'] = pow_resp
        h.pop('Content-Type', None)

        # Detect mime by extension if not supplied
        if not mime_type:
            ext = os.path.splitext(file_path)[1].lower()
            mime_type = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
                '.pdf': 'application/pdf',
                '.txt': 'text/plain', '.md': 'text/plain',
                '.csv': 'text/csv',
                '.py': 'text/plain', '.js': 'text/plain',
                '.html': 'text/html', '.json': 'application/json',
                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            }.get(ext, 'application/octet-stream')

        with open(file_path, 'rb') as f:
            # FIELD NAME MUST BE "text" — this is the DeepSeek requirement
            files = {'text': (os.path.basename(file_path), f, mime_type)}
            r = requests.post(f"{self.BASE}/file/upload_file",
                              headers=h, files=files, timeout=120)
            if r.status_code != 200:
                return None, None, (
                    f"{self._FAIL_REASON['UPLOAD_HTTP']} (HTTP {r.status_code})")
            body = r.json()
            biz = body.get('data', {}).get('biz_data')
            if not biz:
                msg = body.get('msg') or body.get('data', {}).get('biz_msg') or ''
                return None, None, (
                    self._FAIL_REASON['UPLOAD_HTTP'] + (f" — {msg}" if msg else ""))
            fid = biz.get('id')
            fname = biz.get('file_name')

        if not fid:
            return None, None, self._FAIL_REASON['UPLOAD_HTTP']
        if not wait_for_ready:
            return fid, fname, None

        # ---- Poll until we hit a TERMINAL status ----
        # BUGFIX: the old loop only stopped on SUCCESS/FAILED, so statuses like
        # CONTENT_EMPTY (very common for images with no readable text) spun the
        # loop for the full timeout and then returned None with no explanation.
        status = biz.get('status')
        deadline = _time.time() + poll_timeout
        delay = 0.5
        while (status not in self._TERMINAL_OK
               and status not in self._TERMINAL_FAIL
               and _time.time() < deadline):
            _time.sleep(delay)
            delay = min(delay * 1.3, 3.0)   # gentle backoff, avoids hammering
            try:
                rs = requests.get(f"{self.BASE}/file/fetch_files",
                                  headers=self.headers,
                                  params={'file_ids': fid}, timeout=15)
                if rs.status_code == 200:
                    files_arr = rs.json().get('data', {}).get('biz_data', {}).get('files', [])
                    if files_arr:
                        status = files_arr[0].get('status')
            except Exception:
                # transient network hiccup — keep trying until the deadline
                continue

        if status in self._TERMINAL_OK:
            return fid, fname, None

        if status in self._TERMINAL_FAIL:
            return None, None, self._FAIL_REASON.get(
                status, f"DeepSeek rejected the file (status: {status}).")

        return None, None, self._FAIL_REASON['TIMEOUT']

    # ---------- Chat streaming ----------
    def chat_stream(self, sess_id: str, parent_msg_id: Optional[str], prompt: str,
                    model_type: str = 'default', thinking: bool = True, search: bool = False,
                    file_ids: Optional[List[str]] = None) -> Iterator[Dict[str, Any]]:
        """
        Yields events:
          {'type': 'think', 'text': str}    - thinking chunk
          {'type': 'answer', 'text': str}   - answer chunk
          {'type': 'msg_id', 'id': str}     - final assistant message id
          {'type': 'error', 'msg': str}
        """
        pow_resp = self._pow_header('/api/v0/chat/completion')
        if not pow_resp:
            yield {'type': 'error', 'msg': 'Failed to solve PoW challenge'}
            return

        h = self.headers.copy()
        h['x-ds-pow-response'] = pow_resp

        if file_ids:
            search = False

        payload = {
            'chat_session_id': sess_id,
            'parent_message_id': parent_msg_id,
            'prompt': prompt,
            'stream': True,
            'ref_file_ids': file_ids or [],
            'thinking_enabled': thinking,
            'search_enabled': search,
            'model_type': model_type,
        }

        r = requests.post(f"{self.BASE}/chat/completion", headers=h, json=payload,
                          stream=True, timeout=300)
        if r.status_code != 200:
            yield {'type': 'error', 'msg': f'HTTP {r.status_code}: {r.text[:200]}'}
            return

        # If server responded with non-SSE JSON (e.g. business error), surface it
        ct = r.headers.get('content-type', '')
        if 'event-stream' not in ct and 'application/json' in ct:
            try:
                body = r.json()
                biz_msg = body.get('data', {}).get('biz_msg') or body.get('msg') \
                          or 'Unknown API error'
                yield {'type': 'error', 'msg': f'DeepSeek: {biz_msg}'}
            except Exception:
                yield {'type': 'error', 'msg': f'Unexpected response: {r.text[:200]}'}
            return

        active_type = "RESPONSE"

        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore')
            if not line.startswith('data:'):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                data = json.loads(body)
            except Exception:
                continue

            def _emit(txt):
                if not txt:
                    return
                if active_type in ('FINISHED', 'END', 'DONE'):
                    return
                yield {'type': 'think' if active_type == 'THINK' else 'answer', 'text': txt}

            if isinstance(data, dict) and "v" in data and "p" not in data and "o" not in data:
                # Bare content chunk — could be dict with response or a plain string continuation
                v = data["v"]
                if isinstance(v, dict) and "response" in v:
                    resp = v["response"]
                    if "message_id" in resp:
                        yield {'type': 'msg_id', 'id': resp["message_id"]}
                    if resp.get("fragments"):
                        frag = resp["fragments"][0]
                        active_type = frag.get("type", "RESPONSE")
                        yield from _emit(frag.get("content", ""))
                elif isinstance(v, str):
                    yield from _emit(v)

            elif isinstance(data, dict) and "p" in data and data.get("o") == "APPEND":
                path = data.get("p", "")
                val = data.get("v", "")
                if "fragments" in path and isinstance(val, list) and val:
                    active_type = val[0].get("type", "RESPONSE")
                    yield from _emit(val[0].get("content", ""))
                elif path == "response/fragments/-1/content":
                    if isinstance(val, str):
                        yield from _emit(val)
            # SET / BATCH / other patch ops are ignored (metadata like status=FINISHED)
