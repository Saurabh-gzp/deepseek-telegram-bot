"""
STT module v3 — RAM-safe Whisper for small hosts (Render free tier = 512MB).

Changes vs v2:
  • Default model `base` (~150MB int8) instead of `small` (~500MB).
    `small` cannot fit in a 512MB container — the process gets OOM-killed
    mid-request. `base` keeps good Hinglish quality at a third of the RAM.
  • WHISPER_SIZE env still overrides (tiny/base/small/medium).
  • The model unloads after STT_IDLE_UNLOAD_SEC (default 600s) of
    inactivity, so RAM goes back to the rest of the bot.
  • On MemoryError: automatic fallback chain (small→base→tiny) and one
    retry, so a voice message never crashes the request.
"""
import gc
import os
import logging
import threading
from typing import Optional, Tuple

log = logging.getLogger("stt")

_FALLBACKS = {"medium": "small", "small": "base", "base": "tiny", "tiny": None}


def _env_size() -> str:
    s = (os.getenv("WHISPER_SIZE") or "base").strip().lower()
    return s if s in _FALLBACKS else "base"


MODEL_SIZE = _env_size()
IDLE_UNLOAD_SEC = int(os.getenv("STT_IDLE_UNLOAD_SEC", "600"))

_MODEL = None
_model_lock = threading.Lock()
_unload_timer: Optional[threading.Timer] = None

# Bias Whisper toward Hinglish transcription
INITIAL_PROMPT = (
    "Ye Hinglish conversation hai — Hindi aur English mix. "
    "User bhai, kya, hai, karna, chahiye, matlab, bilkul, samjho jaise "
    "words use karta hai."
)


def _load(size: str):
    from faster_whisper import WhisperModel
    log.info("Loading Whisper model: %s", size)
    return WhisperModel(size, device="cpu", compute_type="int8")


def _get_model():
    global _MODEL
    with _model_lock:
        if _MODEL is None:
            _MODEL = _load(MODEL_SIZE)
            log.info("Whisper loaded (%s)", MODEL_SIZE)
    return _MODEL


def _unload_now():
    global _MODEL
    with _model_lock:
        if _MODEL is not None:
            log.info("Whisper idle %ss — unloading model to free RAM", IDLE_UNLOAD_SEC)
            _MODEL = None
            gc.collect()


def _schedule_unload():
    global _unload_timer
    if IDLE_UNLOAD_SEC <= 0:
        return
    with _model_lock:
        if _unload_timer is not None:
            _unload_timer.cancel()
    t = threading.Timer(IDLE_UNLOAD_SEC, _unload_now)
    t.daemon = True
    with _model_lock:
        _unload_timer = t
    t.start()


def _run(model, audio_path: str, language: Optional[str]) -> Tuple[str, str]:
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=400,
            speech_pad_ms=200,
        ),
        initial_prompt=INITIAL_PROMPT,
        temperature=0.0,
        condition_on_previous_text=False,
    )
    # NOTE: segments is a LAZY generator — OOM can surface during iteration,
    # which is exactly why the caller wraps this in try/except.
    text = "".join(s.text for s in segments).strip()
    return text, info.language


def transcribe(audio_path: str, language: Optional[str] = None) -> Tuple[str, str]:
    """
    Returns (text, detected_language).
    language=None → auto-detect (biased toward Hindi/English by initial prompt).
    """
    global MODEL_SIZE, _MODEL
    try:
        model = _get_model()
        result = _run(model, audio_path, language)
        _schedule_unload()
        return result
    except (MemoryError, RuntimeError, OSError) as e:
        # Only treat *memory-shaped* errors as OOM; re-raise everything else.
        if not isinstance(e, MemoryError) and not any(
                k in str(e).lower() for k in ("memory", "alloc", "mmap")):
            raise
        nxt = _FALLBACKS.get(MODEL_SIZE)
        if not nxt:
            _schedule_unload()
            raise
        log.warning("Whisper '%s' out of memory (%s: %s) — falling back to '%s'",
                    MODEL_SIZE, type(e).__name__, str(e)[:120], nxt)
        with _model_lock:
            _MODEL = None
            gc.collect()
            MODEL_SIZE = nxt
        try:
            model = _get_model()
            result = _run(model, audio_path, language)
            return result
        finally:
            _schedule_unload()
