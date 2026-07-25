"""
STT module v2 — better quality with configurable model size + Hinglish support.
"""
import os
import logging
from typing import Optional, Tuple

log = logging.getLogger("stt")

_MODEL = None
_MODEL_SIZE = os.getenv("WHISPER_SIZE", "small")  # small = ~500MB, good for Hinglish

# Bias Whisper toward Hinglish transcription
INITIAL_PROMPT = (
    "Ye Hinglish conversation hai — Hindi aur English mix. "
    "User bhai, kya, hai, karna, chahiye, matlab, bilkul, samjho jaise "
    "words use karta hai."
)


def _get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model: %s (first time downloads ~500MB)", _MODEL_SIZE)
        _MODEL = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8")
        log.info("Whisper loaded")
    return _MODEL


def transcribe(audio_path: str, language: Optional[str] = None) -> Tuple[str, str]:
    """
    Returns (text, detected_language).
    language=None → auto-detect (biased toward Hindi/English by initial prompt).
    """
    model = _get_model()
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
    parts = []
    for s in segments:
        parts.append(s.text)
    text = "".join(parts).strip()
    return text, info.language
