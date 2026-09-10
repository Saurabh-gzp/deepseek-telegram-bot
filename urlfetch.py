"""
URL & YouTube fetch/summarize helpers.

YouTube transcripts use a 3-layer fallback chain, because cloud hosts
(Render / AWS / GCP ...) get their IPs blocked by YouTube:

  Layer 1  youtube-transcript-api  (fast, official lib; honors YT_PROXY)
  Layer 2  yt-dlp captions         (manual subs first, then auto-captions;
                                    honors YT_PROXY + YT_COOKIES)
  Layer 3  Invidious mirrors       (public instances; best-effort)

Env vars (all optional):
  YT_PROXY     proxy URL, e.g. http://user:pass@host:port or socks5://host:1080
               (also falls back to standard HTTP(S)_PROXY env vars)
  YT_COOKIES   path to a Netscape cookies.txt, OR the raw file contents,
               OR base64 of the file — used by yt-dlp to pass the bot check
  YT_MIRRORS   comma-separated Invidious bases to override the default list
"""
import base64
import html as _html
import json as _json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

log = logging.getLogger("urlfetch")

URL_RE = re.compile(r"https?://[^\s<>()]+")
YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)

# Languages we prefer, in order. Hinglish audience: English first, then Hindi.
_LANG_PREFS = ("en", "hi", "en-US", "en-GB", "en-orig", "hi-Latn")

# How long a fetched transcript stays in the in-memory cache (6 h) and how
# many videos we keep — same video re-sent later should not re-hit YouTube.
_CACHE_TTL = 6 * 3600
_CACHE_MAX = 100
_CACHE: dict[str, Tuple[float, str]] = {}

_DEFAULT_MIRRORS = [
    "https://inv.nadeko.net",
    "https://invidious.f5.si",
    "https://yewtu.be",
    "https://iv.melmac.space",
    "https://invidious.privacyredirect.com",
    "https://invidious.jing.rocks",
]

_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}

_BLOCKED_HINT = (
    "YouTube is blocking this server's IP (cloud/datacenter IPs are blocked). "
    "Fix: on Render add env var YT_PROXY=http://user:pass@host:port "
    "(a proxy/VPN exit not hosted on a cloud IP), and/or YT_COOKIES "
    "(export cookies.txt from a signed-in browser), then redeploy."
)


class YouTubeFetchError(RuntimeError):
    """All transcript sources failed — message contains the reason + fix."""


# --------------------------------------------------------------------------
# URL helpers (unchanged API)
# --------------------------------------------------------------------------

def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def is_youtube(url: str) -> Optional[str]:
    m = YT_RE.search(url)
    return m.group(1) if m else None


def fetch_url_text(url: str) -> Tuple[str, str]:
    """
    Returns (extracted_text, title).
    Uses trafilatura — good at stripping boilerplate.
    """
    import trafilatura
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise RuntimeError("Could not download page")
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=True,
        favor_recall=True,
    ) or ""
    md = trafilatura.extract_metadata(downloaded)
    title = ""
    if md:
        title = (md.title or "").strip()
    return text.strip(), title


# --------------------------------------------------------------------------
# Config from environment
# --------------------------------------------------------------------------

def _yt_proxy() -> Optional[str]:
    """Proxy URL for YouTube traffic, or None."""
    for key in ("YT_PROXY", "HTTPS_PROXY", "https_proxy",
                "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return None


_cookie_tmpfile: Optional[str] = None


def _yt_cookiefile() -> Optional[str]:
    """
    yt-dlp cookiefile path from YT_COOKIES, or None.
    Accepts: an existing file path, raw Netscape cookies.txt content,
    or base64 of that file. The temp file is created lazily and cached.
    """
    global _cookie_tmpfile
    if _cookie_tmpfile:
        return _cookie_tmpfile
    raw = (os.getenv("YT_COOKIES") or "").strip()
    if not raw:
        return None
    # 1) it may simply be a path
    p = Path(raw)
    if p.is_file():
        _cookie_tmpfile = str(p)
        return _cookie_tmpfile
    # 2) base64?
    content = None
    try:
        decoded = base64.b64decode(re.sub(r"\s+", "", raw), validate=True)
        text = decoded.decode("utf-8", "replace")
        if "cookie" in text.lower() or "\t" in text:
            content = text
    except Exception:
        pass
    # 3) raw Netscape content
    if content is None and ("# Netscape" in raw or "\t" in raw):
        content = raw
    if content is None:
        log.warning("YT_COOKIES set but not recognised as path/base64/content")
        return None
    fd, path = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    _cookie_tmpfile = path
    log.info("YT_COOKIES written to %s", path)
    return _cookie_tmpfile


def _yt_mirrors() -> list[str]:
    raw = (os.getenv("YT_MIRRORS") or "").strip()
    if raw:
        bases = [b.strip().rstrip("/") for b in raw.split(",") if b.strip()]
        return bases or list(_DEFAULT_MIRRORS)
    return list(_DEFAULT_MIRRORS)


# --------------------------------------------------------------------------
# Caption parsers (json3 / WebVTT / srv XML)
# --------------------------------------------------------------------------

def _json3_to_text(raw: str) -> str:
    try:
        data = _json.loads(raw)
    except Exception:
        return ""
    parts: list[str] = []
    for ev in data.get("events") or []:
        segs = ev.get("segs") or []
        t = "".join(s.get("utf8", "") for s in segs)
        t = t.replace("\u200b", "").replace("\n", " ").strip()
        if t and (not parts or t != parts[-1]):
            parts.append(t)
    return " ".join(parts)


_VTT_JUNK = re.compile(r"<[^>]+>")


def _vtt_to_text(raw: str) -> str:
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if (not line or line == "WEBVTT"
                or line.startswith(("Kind:", "Language:", "NOTE", "STYLE",
                                    "REGION", "FYI "))) \
                or "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = _VTT_JUNK.sub("", line)
        line = _html.unescape(line).strip()
        if line and (not parts or line != parts[-1]):
            parts.append(line)
    return " ".join(parts)


def _srv_to_text(raw: str) -> str:
    texts = re.findall(r"<text[^>]*>(.*?)</text>", raw, re.S)
    parts: list[str] = []
    for t in texts:
        t = _html.unescape(re.sub(r"<[^>]+>", "", t))
        t = t.replace("\n", " ").strip()
        if t and (not parts or t != parts[-1]):
            parts.append(t)
    return " ".join(parts)


def _caption_body_to_text(body: str) -> str:
    """Parse any caption body format into plain text."""
    s = body.lstrip()
    if s.startswith("{"):
        return _json3_to_text(s)
    if s.startswith("WEBVTT"):
        return _vtt_to_text(s)
    if "<text" in s:
        return _srv_to_text(s)
    return _vtt_to_text(s)  # last resort


def _pick_track(tracks: dict) -> Optional[list]:
    """Pick the best track URL list from a {lang: [track, ...]} dict."""
    if not tracks:
        return None

    def score(code: str) -> int:
        code = (code or "").lower()
        for i, pref in enumerate(_LANG_PREFS):
            if code == pref.lower():
                return i
        if code.startswith("en"):
            return 50
        if code.startswith("hi"):
            return 51
        return 90

    best_code, best_score = None, 10_000
    for code in tracks:
        s = score(code)
        if s < best_score:
            best_code, best_score = code, s
    if best_code is None:
        return None
    return tracks[best_code] or None


def _track_url(track_list: list) -> Optional[str]:
    """From a track's format list prefer json3 > vtt > srv3/srv1."""
    fmt_rank = {"json3": 0, "vtt": 1, "srv3": 2, "srv1": 3, "srv2": 4}
    best, best_rank = None, 100
    for t in track_list:
        rank = fmt_rank.get((t.get("ext") or "").lower(), 50)
        if rank < best_rank and t.get("url"):
            best, best_rank = t["url"], rank
    return best


# --------------------------------------------------------------------------
# Layer 1 — youtube-transcript-api
# --------------------------------------------------------------------------

def _via_yta(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    kwargs = {}
    proxy = _yt_proxy()
    if proxy:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            kwargs["proxy_config"] = GenericProxyConfig(
                http_url=proxy, https_url=proxy)
        except ImportError:
            log.warning("proxy configured but youtube-transcript-api too old")
    api = YouTubeTranscriptApi(**kwargs)

    # Iterate the transcript list ourselves so that any available language
    # can be used (api.fetch() with a fixed language list raises
    # NoTranscriptFound when e.g. the video only has 'en-IN').
    entries = list(api.list(video_id))
    if not entries:
        raise RuntimeError("no transcripts listed")

    def score(t) -> int:
        code = (t.language_code or "").lower()
        for i, pref in enumerate(_LANG_PREFS):
            if code == pref.lower():
                return i
        if code.startswith("en"):
            return 50
        if code.startswith("hi"):
            return 51
        return 90

    entries.sort(key=score)
    fetched = entries[0].fetch()
    parts = [e.text if hasattr(e, "text") else e["text"] for e in fetched]
    return " ".join(parts)


# --------------------------------------------------------------------------
# Layer 2 — yt-dlp captions
# --------------------------------------------------------------------------

def _via_ytdlp(video_id: str) -> str:
    import yt_dlp

    class _QuietLog:
        """Swallow yt-dlp's stderr noise (errors are re-raised as exceptions)."""
        def debug(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLog(),
        "socket_timeout": 20,
        "retries": 2,
        # Multiple clients — if one is blocked yt-dlp falls through.
        "extractor_args": {"youtube": {
            "player_client": ["web_safari", "tv", "ios", "android_vr"]}},
    }
    proxy = _yt_proxy()
    if proxy:
        opts["proxy"] = proxy
    cookiefile = _yt_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False)

    # Manual subtitles first (better quality), then auto-generated.
    tracks = info.get("subtitles") or {}
    track = _pick_track(tracks)
    if not track:
        tracks = info.get("automatic_captions") or {}
        track = _pick_track(tracks)
    if not track:
        raise RuntimeError("yt-dlp: no subtitle tracks for this video")
    url = _track_url(track)
    if not url:
        raise RuntimeError("yt-dlp: track has no URL")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(url, headers=_HDRS, timeout=20, proxies=proxies)
    r.raise_for_status()
    text = _caption_body_to_text(r.text)
    if len(text) < 30:
        raise RuntimeError("yt-dlp: caption body empty")
    return text


# --------------------------------------------------------------------------
# Layer 3 — Invidious mirrors
# --------------------------------------------------------------------------

def _via_mirrors(video_id: str) -> str:
    proxies = None  # mirrors are public; go direct even if YT_PROXY is set
    last_err = "no mirror reachable"
    for base in _yt_mirrors()[:5]:
        try:
            r = requests.get(f"{base}/api/v1/captions/{video_id}",
                             headers=_HDRS, timeout=7, proxies=proxies)
            if r.status_code != 200:
                last_err = f"{base} -> HTTP {r.status_code}"
                continue
            caps = (r.json() or {}).get("captions") or []
            if not caps:
                last_err = f"{base} -> no captions"
                continue

            # try en/hi first, then whatever exists
            def score(c):
                code = (c.get("languageCode") or "").lower()
                for i, pref in enumerate(_LANG_PREFS):
                    if code == pref.lower():
                        return i
                return 80

            for c in sorted(caps, key=score):
                code = c.get("languageCode") or ""
                if not code:
                    continue
                r2 = requests.get(
                    f"{base}/api/v1/captions/{video_id}?lang={code}",
                    headers=_HDRS, timeout=10, proxies=proxies)
                if r2.status_code == 200 and len(r2.text) > 100:
                    text = _caption_body_to_text(r2.text)
                    if len(text) > 30:
                        return text
            last_err = f"{base} -> captions listed but bodies empty"
        except Exception as e:
            last_err = f"{base} -> {type(e).__name__}"
    raise RuntimeError(f"mirrors failed ({last_err})")


# --------------------------------------------------------------------------
# Cache + orchestrator
# --------------------------------------------------------------------------

def _cache_get(video_id: str) -> Optional[str]:
    ent = _CACHE.get(video_id)
    if ent and time.time() - ent[0] < _CACHE_TTL:
        return ent[1]
    if ent:
        _CACHE.pop(video_id, None)
    return None


def _cache_put(video_id: str, text: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[video_id] = (time.time(), text)


def _is_no_captions_error(e: Exception) -> bool:
    """Video genuinely has no captions — retrying elsewhere is pointless."""
    name = type(e).__name__
    msg = str(e)
    return ("TranscriptsDisabled" in name or "VideoUnavailable" in name
            or "NoTranscriptFound" in name and "language" in msg.lower()
            or "is_private" in msg or "private video" in msg.lower())


def fetch_youtube_transcript(video_id: str) -> Tuple[str, str]:
    """
    Returns (transcript_text, video_id).

    Tries: youtube-transcript-api -> yt-dlp -> Invidious mirrors.
    Raises YouTubeFetchError with an actionable message when all fail.
    """
    cached = _cache_get(video_id)
    if cached is not None:
        return cached, video_id

    problems: list[str] = []

    # --- Layer 1: youtube-transcript-api (fast path) ---
    try:
        text = _via_yta(video_id)
        if len(text) >= 30:
            _cache_put(video_id, text)
            return text, video_id
        problems.append("transcript-api: empty result")
    except Exception as e:
        if _is_no_captions_error(e):
            # Real "no captions" — do not hammer the other layers.
            raise YouTubeFetchError(
                "This video has no transcripts/captions (disabled by the "
                "uploader or the video is private/unavailable).") from e
        log.debug("yta failed for %s: %s: %s",
                  video_id, type(e).__name__, str(e)[:120])
        problems.append(f"transcript-api: {type(e).__name__}")
        # one quick retry — transient blocks sometimes clear
        time.sleep(1.5)
        try:
            text = _via_yta(video_id)
            if len(text) >= 30:
                _cache_put(video_id, text)
                return text, video_id
        except Exception as e2:
            problems.append(f"transcript-api retry: {type(e2).__name__}")

    # --- Layer 2: yt-dlp ---
    try:
        text = _via_ytdlp(video_id)
        _cache_put(video_id, text)
        return text, video_id
    except Exception as e:
        log.debug("yt-dlp failed for %s: %s: %s",
                  video_id, type(e).__name__, str(e)[:120])
        problems.append(f"yt-dlp: {type(e).__name__}")

    # --- Layer 3: Invidious mirrors ---
    try:
        text = _via_mirrors(video_id)
        _cache_put(video_id, text)
        return text, video_id
    except Exception as e:
        log.debug("mirrors failed for %s: %s", video_id, str(e)[:120])
        problems.append(str(e))

    blocked = any(k in " ".join(problems)
                  for k in ("RequestBlocked", "IpBlocked", "TooManyRequests",
                            "YouTubeRequestFailed"))
    if blocked:
        detail = (_BLOCKED_HINT + "\n\nDetails: " + "; ".join(problems))
    else:
        detail = ("Could not fetch the YouTube transcript. Details: "
                  + "; ".join(problems))
    raise YouTubeFetchError(detail)
