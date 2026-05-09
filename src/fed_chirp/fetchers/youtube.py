"""YouTube transcript fetcher.

Some regional Fed speakers (Bostic in particular) increasingly publish
appearances as YouTube live streams or external video instead of written
speech transcripts. This module pulls the auto-generated captions via
`youtube-transcript-api` so those events flow through the same scoring
pipeline as text speeches.

Quality caveat: auto-captions on live streams are often unavailable
(disabled by the uploader) and even when present can garble the
prescriptive language the rubric depends on. Treat this as a best-effort
augmentation; the speech-likeness filter and rubric `excluded` path are
the safety net.
"""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import parse_qs, urlparse

from .federalreserve import Speech, SpeechRef


_YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")
_LIVE_PATH_RE = re.compile(r"^/live/([A-Za-z0-9_\-]{6,})")
_EMBED_PATH_RE = re.compile(r"^/embed/([A-Za-z0-9_\-]{6,})")

# Auto-caption segments are tiny (a few words each), so we chunk them into
# paragraph-sized blocks before storing. Otherwise the speech-likeness
# filter's link-density gate trips on the high short-line ratio.
_PARAGRAPH_CHAR_TARGET = 500


def _segments_to_paragraphs(transcript) -> str:
    paragraphs: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for seg in transcript:
        text = seg.text.strip()
        if not text:
            continue
        buf.append(text)
        buf_len += len(text) + 1
        if buf_len >= _PARAGRAPH_CHAR_TARGET:
            paragraphs.append(" ".join(buf))
            buf = []
            buf_len = 0
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs)


def is_youtube_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in _YT_HOSTS


def extract_video_id(url: str) -> str | None:
    """Pull the 11-ish-character YouTube video id from any common URL form."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if (u.hostname or "") not in _YT_HOSTS:
        return None
    if u.hostname == "youtu.be":
        return u.path.lstrip("/").split("/", 1)[0] or None
    if u.path == "/watch":
        return parse_qs(u.query).get("v", [None])[0]
    m = _LIVE_PATH_RE.match(u.path) or _EMBED_PATH_RE.match(u.path)
    return m.group(1) if m else None


class CaptionsUnavailable(Exception):
    """Captions are disabled, missing, or the video isn't accessible.

    Distinct from ValueError so callers can downgrade these to INFO logs
    — they're expected for many live streams and not actionable.
    """


def fetch_transcript(ref: SpeechRef) -> Speech:
    """Fetch YouTube auto-captions and wrap them as a Speech.

    Raises ValueError if the URL isn't YouTube or the video id can't be
    parsed. Raises CaptionsUnavailable if the video exists but has no
    accessible transcript (the common case for many Fed live streams).
    """
    # Imported here so the optional dependency only loads when needed.
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
    )

    vid = extract_video_id(ref.url)
    if vid is None:
        raise ValueError(f"Cannot parse YouTube video id from {ref.url}")

    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(vid)
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
        raise CaptionsUnavailable(
            f"{type(exc).__name__} for {ref.url}"
        ) from exc

    body = _segments_to_paragraphs(transcript)
    if not body.strip():
        raise ValueError(f"YouTube captions empty for {ref.url}")

    title = ref.title or f"YouTube transcript ({vid})"
    return Speech(
        url=ref.url,
        speaker_key=ref.speaker_key,
        speech_date=ref.pub_date,
        title=title,
        location="YouTube",
        body=body,
    )
