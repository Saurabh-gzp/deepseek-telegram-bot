"""
URL & YouTube fetch/summarize helpers.
"""
import re
import logging
from typing import Optional, Tuple

log = logging.getLogger("urlfetch")

URL_RE = re.compile(r"https?://[^\s<>()]+")
YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


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


def fetch_youtube_transcript(video_id: str) -> Tuple[str, str]:
    """
    Returns (transcript_text, video_id).
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    # Try multiple language priorities
    try:
        api = YouTubeTranscriptApi()
        # new API (>=1.0.0)
        entries = api.fetch(video_id, languages=['en', 'hi', 'en-US', 'en-GB'])
        parts = [e.text if hasattr(e, 'text') else e['text'] for e in entries]
    except (AttributeError, TypeError):
        # older API
        entries = YouTubeTranscriptApi.get_transcript(
            video_id, languages=['en', 'hi', 'en-US', 'en-GB'])
        parts = [e['text'] for e in entries]
    text = " ".join(parts)
    return text, video_id
