"""
otp_auth.py — DeepSeek OTP login support (new in app v2.6.x).

Reverse-engineered from DeepSeek Android APK v2.6.2 (com.deepseek.chat):

  EMAIL OTP
    POST /api/v0/users/create_email_verification_code
      {email, locale, shumei_verification?, hcaptcha_token?, turnstile_token,
       device_id, scenario}
      scenario is a STRING: "register" | "reset_password" (NOT "login")
      NOTE: DeepSeek's server wants an anti-bot proof with this call. The
      official app supplies it INVISIBLY via the on-device Shumei risk SDK
      (com.ishumei.sdk) — that's why no captcha is ever visible in the app.
      Server-side we cannot mint a Shumei rid or a Turnstile token, so
      request_email_otp() usually gets RECAPTCHA_VERIFY_FAILED from flagged
      IPs. It accepts an optional turnstile_token (solve it elsewhere and
      pass it in). From a clean residential IP (DS_PROXY) it may pass
      without one.
    POST /api/v0/users/check_email_code
      {email, email_verification_code} -> {biz_code 0} on success

  SMS OTP (no captcha required — only an optional PoW "ticket")
    POST /api/v0/users/create_guest_challenge
      {target_path} -> {guest_challenge: {algorithm: "DeepSeekHashV1",
        challenge, salt, signature, difficulty, expire_at, ...}}
      The client solves the hashcash-style PoW and sends the solution as
      "ticket". Reuses the Node WASM solver already bundled for chat PoW.
    POST /api/v0/users/create_sms_verification_code
      {mobile_number, ticket?, locale, shumei_verification?, hcaptcha_token?,
       device_id, os: "android", scenario: "login"}
    POST /api/v0/users/login_by_mobile_sms
      {mobile_number, sms_verification_code, area_code, device_id,
       os: "android"} -> {biz_data: {user: "<session token>"}} on success

  RISK DEVICE
    POST /api/v0/users/login from a flagged IP/device returns
    {biz_code: 11, biz_msg: "RISK_DEVICE_DETECTED"}. The official app then
    falls back to OTP verification of account ownership. login_with_otp_flow()
    below drives that recovery: password login -> on risk_device, OTP verify
    -> retry password login.
"""
import json
import os
import subprocess
import uuid
from typing import Optional, Tuple, Dict, Any

import requests

BASE = "https://chat.deepseek.com/api/v0"


def app_headers(version: str = "2.6.2") -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": f"DeepSeek/{version} Android/34",
        "Referer": "https://chat.deepseek.com",
        "x-client-platform": "android",
        "x-client-version": version,
        "x-client-locale": "en_US",
        "x-client-bundle-id": "com.deepseek.chat",
    }


def _proxies() -> Optional[dict]:
    p = (os.getenv("DS_PROXY") or os.getenv("HTTPS_PROXY")
         or os.getenv("https_proxy") or os.getenv("HTTP_PROXY")
         or os.getenv("http_proxy") or "").strip()
    return {"http": p, "https": p} if p else None


def _biz(resp: requests.Response) -> Tuple[int, str, Any]:
    """Return (biz_code, biz_msg, biz_data) from a DeepSeek envelope."""
    try:
        j = resp.json()
    except Exception:
        return -1, f"HTTP {resp.status_code} non-JSON", None
    d = j.get("data") or {}
    return d.get("biz_code", -1), d.get("biz_msg") or "", d.get("biz_data")


# ---------------------------------------------------------------- PoW ticket
def solve_guest_pow(target_path: str, workdir: str = ".",
                   timeout: int = 60) -> Optional[str]:
    """
    Fetch a DeepSeekHashV1 guest challenge for target_path and solve it.
    Returns the base64 "ticket" to send as `ticket`, or None on failure.
    """
    try:
        r = requests.post(f"{BASE}/users/create_guest_challenge",
                          headers=app_headers(),
                          json={"target_path": target_path},
                          timeout=20, proxies=_proxies())
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        cfg = (r.json().get("data") or {}).get("biz_data", {}).get("guest_challenge") or {}
    except Exception:
        return None
    if not cfg.get("challenge"):
        return None
    # Reuse the Node WASM solver (same wire format as the chat PoW solver).
    js_path = os.path.join(workdir, "pow_solver.js")
    if not os.path.exists(js_path):
        # solver not bundled — caller can still proceed without a ticket
        return None
    try:
        proc = subprocess.run(["node", js_path, json.dumps(cfg)],
                              capture_output=True, text=True,
                              cwd=workdir, timeout=timeout)
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out or None


# ---------------------------------------------------------------- hCaptcha
# DeepSeek shows an hCaptcha image challenge ("Verify account" screen) before
# sending the email OTP on risky logins. The app solves it in-app; server-side
# we use a solving service (CapSolver / 2captcha style).
#
# REQUIRED (Render env vars):
#   HCAPTCHA_SITEKEY  — DeepSeek's hCaptcha sitekey. Not bundled in the APK;
#                       grab it once via HTTP Canary when the challenge appears
#                       (widget loads js.hcaptcha.com with
#                       host=<sitekey>.android-sdk.hcaptcha.com).
#   CAPSOLVER_KEY     — API key from capsolver.com (pay-per-solve, ~$1-3/1000).
#
# Flow: solve_hcaptcha() -> token -> request_email_otp(..., hcaptcha_token=token)

CAPSOLVER_API = "https://api.capsolver.com"


def solve_hcaptcha(sitekey: Optional[str] = None,
                   api_key: Optional[str] = None,
                   page_url: str = "https://chat.deepseek.com",
                   timeout: int = 120) -> Tuple[Optional[str], str]:
    """
    Solve DeepSeek's hCaptcha via CapSolver. Returns (token, reason).
    """
    import time as _time
    sitekey = sitekey or os.getenv("HCAPTCHA_SITEKEY", "")
    api_key = api_key or os.getenv("CAPSOLVER_KEY", "")
    if not sitekey:
        return None, "no_sitekey"
    if not api_key:
        return None, "no_capsolver_key"
    try:
        r = requests.post(f"{CAPSOLVER_API}/createTask", json={
            "clientKey": api_key,
            "task": {
                "type": "HCaptchaTask",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "isInvisible": False,
            },
        }, timeout=20)
        task_id = (r.json() or {}).get("taskId")
    except Exception as e:
        return None, f"network: {type(e).__name__}"
    if not task_id:
        return None, "task_create_failed"
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        _time.sleep(5)
        try:
            r = requests.post(f"{CAPSOLVER_API}/getTaskResult", json={
                "clientKey": api_key, "taskId": task_id,
            }, timeout=20)
            j = r.json() or {}
        except Exception:
            continue
        if j.get("status") == "ready":
            token = (j.get("solution") or {}).get("gRecaptchaResponse")
            return (token, "ok") if token else (None, "no_token")
        if j.get("status") == "failed":
            return None, str(j.get("errorDescription") or j.get("errorCode") or "failed")
    return None, "timeout"


def request_email_otp_with_captcha(email: str, device_id: Optional[str] = None,
                                   scenario: str = "register",
                                   workdir: str = ".") -> Tuple[bool, str]:
    """
    Full email-OTP send: solve hCaptcha via CapSolver, then request the code.
    Returns (ok, reason). Reasons: 'ok', 'no_sitekey', 'no_capsolver_key',
    'captcha_failed:<reason>', 'too_frequent:<secs>', ...
    """
    token, reason = solve_hcaptcha()
    if not token:
        if reason in ("no_sitekey", "no_capsolver_key"):
            return False, reason
        return False, f"captcha_failed:{reason}"
    return request_email_otp(email, device_id=device_id, scenario=scenario,
                             turnstile_token="", hcaptcha_token=token)


# ---------------------------------------------------------------- Email OTP
def request_email_otp(email: str, device_id: Optional[str] = None,
                      scenario: str = "register",
                      turnstile_token: str = "",
                      hcaptcha_token: str = "") -> Tuple[bool, str]:
    """
    Send an email verification code. Returns (ok, message/reason).
    Reasons: 'ok', 'captcha_required', 'too_frequent', 'api_changed', ...
    """
    device_id = device_id or str(uuid.uuid4())
    payload = {
        "email": email,
        "locale": "en_US",
        "turnstile_token": turnstile_token or "",
        "device_id": device_id,
        "scenario": scenario,
    }
    if hcaptcha_token:
        payload["hcaptcha_token"] = hcaptcha_token
    try:
        r = requests.post(f"{BASE}/users/create_email_verification_code",
                          headers=app_headers(), json=payload,
                          timeout=25, proxies=_proxies())
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code == 422:
        return False, "api_changed"
    if r.status_code == 429:
        return False, "rate_limited"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    code, msg, data = _biz(r)
    if code == 0:
        return True, "ok"
    if msg in ("RECAPTCHA_VERIFY_FAILED", "CAPTCHA_VERIFY_FAILED"):
        return False, "captcha_required"
    if msg == "EMAIL_REQUEST_TOO_FREQUENT":
        secs = (data or {}).get("send_window_secs", 60)
        return False, f"too_frequent:{secs}"
    return False, msg or f"biz_code={code}"


def verify_email_otp(email: str, code: str) -> Tuple[bool, str]:
    """Verify an email OTP. Returns (ok, reason)."""
    try:
        r = requests.post(f"{BASE}/users/check_email_code",
                          headers=app_headers(),
                          json={"email": email, "email_verification_code": code.strip()},
                          timeout=25, proxies=_proxies())
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    biz_code, msg, _ = _biz(r)
    if biz_code == 0:
        return True, "ok"
    return False, msg or f"biz_code={biz_code}"


# ---------------------------------------------------------------- SMS OTP
def request_sms_otp(mobile_number: str, area_code: str = "86",
                    device_id: Optional[str] = None,
                    workdir: str = ".") -> Tuple[bool, str]:
    """
    Send an SMS verification code (scenario "login"). Solves the PoW guest
    challenge first; proceeds even without a ticket (server often accepts).
    Returns (ok, reason).
    """
    device_id = device_id or str(uuid.uuid4())
    target = "/api/v0/users/create_sms_verification_code"
    ticket = solve_guest_pow(target, workdir=workdir)
    payload = {
        "mobile_number": mobile_number.strip(),
        "locale": "en_US",
        "device_id": device_id,
        "os": "android",
        "scenario": "login",
    }
    if ticket:
        payload["ticket"] = ticket
    try:
        r = requests.post(f"{BASE}/users/create_sms_verification_code",
                          headers=app_headers(), json=payload,
                          timeout=25, proxies=_proxies())
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code == 422:
        return False, "api_changed"
    if r.status_code == 429:
        return False, "rate_limited"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    biz_code, msg, data = _biz(r)
    if biz_code == 0:
        return True, "ok"
    if msg == "SMS_REQUEST_TOO_FREQUENT":
        secs = (data or {}).get("send_window_secs", 60)
        return False, f"too_frequent:{secs}"
    return False, msg or f"biz_code={biz_code}"


def login_with_sms_otp(mobile_number: str, area_code: str, code: str,
                       device_id: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Complete SMS-OTP login. Returns (token, reason); reason 'ok' on success.
    """
    device_id = device_id or str(uuid.uuid4())
    payload = {
        "mobile_number": mobile_number.strip(),
        "sms_verification_code": code.strip(),
        "area_code": area_code.strip(),
        "device_id": device_id,
        "os": "android",
    }
    try:
        r = requests.post(f"{BASE}/users/login_by_mobile_sms",
                          headers=app_headers(), json=payload,
                          timeout=25, proxies=_proxies())
    except Exception as e:
        return None, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    biz_code, msg, data = _biz(r)
    if biz_code == 0 and isinstance(data, dict):
        user = data.get("user")
        token = user if isinstance(user, str) else (user or {}).get("token")
        if token:
            return token, "ok"
        return None, "token_missing"
    return None, msg or f"biz_code={biz_code}"
