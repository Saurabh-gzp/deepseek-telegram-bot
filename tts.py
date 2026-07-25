"""
TTS module v2 — edge-tts → converts to OGG-Opus for proper Telegram voice messages.
"""
import os
import asyncio
import logging
import tempfile
import re

log = logging.getLogger("tts")

VOICES = {
    "hindi_male":     "hi-IN-MadhurNeural",
    "hindi_female":   "hi-IN-SwaraNeural",
    "english_male":   "en-US-GuyNeural",
    "english_female": "en-US-JennyNeural",
}

_DEV_RE = re.compile(r"[\u0900-\u097F]")
_HINGLISH_WORDS = {
    "hai", "kya", "bhai", "hoon", "kro", "kar", "mera", "tera", "aap", "tum",
    "acha", "nhi", "krna", "kyu", "haan", "toh", "abhi", "bata", "batao",
    "chahiye", "karna", "rahe", "raha", "rahi", "ho", "hu", "matlab",
}


def _pick_voice(text: str, prefer_female: bool = False) -> str:
    if _DEV_RE.search(text):
        return VOICES["hindi_female" if prefer_female else "hindi_male"]
    words = set(re.findall(r"[a-z]+", text.lower()))
    if len(words & _HINGLISH_WORDS) >= 2:
        return VOICES["hindi_female" if prefer_female else "hindi_male"]
    return VOICES["english_female" if prefer_female else "english_male"]


def _clean_for_speech(text: str) -> str:
    """Strip markdown noise so TTS doesn't say 'star star bold star star'."""
    t = text
    t = re.sub(r"```[\s\S]*?```", " (code block skipped) ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[*_~#>|]", "", t)
    t = re.sub(r"[─━—]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Empty response."


async def _mp3_to_ogg_opus(mp3_path: str, ogg_path: str):
    """Convert MP3 to OGG-Opus for Telegram voice messages."""
    import av
    inp = av.open(mp3_path)
    out = av.open(ogg_path, mode='w', format='ogg')
    try:
        out_stream = out.add_stream('libopus', rate=48000)
        out_stream.layout = 'mono'
        resampler = av.audio.resampler.AudioResampler(
            format='s16', layout='mono', rate=48000
        )
        for frame in inp.decode(audio=0):
            for rframe in resampler.resample(frame):
                for pkt in out_stream.encode(rframe):
                    out.mux(pkt)
        for pkt in out_stream.encode():
            out.mux(pkt)
    finally:
        out.close()
        inp.close()


async def synthesize_ogg(text: str, out_ogg_path: str,
                         prefer_female: bool = False,
                         voice: str = None) -> str:
    """
    Synthesize TTS and output an OGG-Opus file that Telegram accepts as voice.
    Returns out_ogg_path.
    """
    import edge_tts

    if not voice:
        voice = _pick_voice(text, prefer_female)

    clean = _clean_for_speech(text)
    if len(clean) > 4000:
        clean = clean[:4000] + " ... (truncated)"

    # Step 1: edge-tts → mp3
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        mp3_path = tmp.name
    try:
        c = edge_tts.Communicate(clean, voice)
        await c.save(mp3_path)
        # Step 2: mp3 → ogg-opus
        await _mp3_to_ogg_opus(mp3_path, out_ogg_path)
        return out_ogg_path
    finally:
        try: os.unlink(mp3_path)
        except: pass
