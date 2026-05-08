"""Extract text from a Federal Reserve press-conference transcript PDF."""

from __future__ import annotations

import io

import requests
from pypdf import PdfReader

USER_AGENT = "fed-chirp/0.1 (+https://github.com/local; personal monitor)"
REQUEST_TIMEOUT = 30


def fetch_pdf_text(url: str, *, drop_first_page: bool = True) -> str:
    """Download a PDF and return its concatenated text.

    Press-conference transcript PDFs from federalreserve.gov begin with a
    title/disclaimer page; `drop_first_page=True` skips it so the body
    starts at "CHAIR POWELL:" instead of "Transcript of Chair Powell's...".
    """
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))

    pages = reader.pages[1:] if drop_first_page and len(reader.pages) > 1 else reader.pages
    chunks: list[str] = []
    for page in pages:
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)
