"""Word-level diff between two FOMC statements.

FOMC statements are short (~400 words) and the canonical Fed-watcher
exercise is reading the diff vs the previous statement. We tokenize on
whitespace+punctuation, run difflib at the token level, and emit either
ANSI-colored output for the terminal or `<ins>/<del>` markup for HTML
(which Gmail/most webmail render with strikethrough/underline).
"""

from __future__ import annotations

import difflib
import html as _html
import re

# Reset / red / green ANSI for terminal rendering.
_ANSI_RESET = "\x1b[0m"
_ANSI_RED = "\x1b[31m"   # deletions
_ANSI_GREEN = "\x1b[32m"  # insertions

# Token regex: a "word" or a single non-whitespace char. Whitespace runs are
# preserved as their own tokens so the rendered diff keeps the original
# layout reasonably intact.
_TOKEN_RE = re.compile(r"\s+|[A-Za-z0-9'’‘]+|[^\s\w]")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def render_html(prev: str, curr: str) -> str:
    """Return an HTML string: deletions wrapped in <del>, insertions in <ins>.

    Whitespace tokens are emitted as-is (escaped) so the result preserves the
    visible word layout in an HTML email body.
    """
    a, b = tokenize(prev), tokenize(curr)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old = "".join(a[i1:i2])
        new = "".join(b[j1:j2])
        if tag == "equal":
            parts.append(_html.escape(new))
        elif tag == "delete":
            parts.append(f"<del style=\"color:#c0392b\">{_html.escape(old)}</del>")
        elif tag == "insert":
            parts.append(f"<ins style=\"color:#1e7e34;text-decoration:none;"
                         f"background:#e8f6ec\">{_html.escape(new)}</ins>")
        elif tag == "replace":
            parts.append(f"<del style=\"color:#c0392b\">{_html.escape(old)}</del>")
            parts.append(f"<ins style=\"color:#1e7e34;text-decoration:none;"
                         f"background:#e8f6ec\">{_html.escape(new)}</ins>")
    return "".join(parts)


def render_ansi(prev: str, curr: str) -> str:
    """Same diff as render_html, but ANSI-colored for terminal output."""
    a, b = tokenize(prev), tokenize(curr)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old = "".join(a[i1:i2])
        new = "".join(b[j1:j2])
        if tag == "equal":
            parts.append(new)
        elif tag == "delete":
            parts.append(f"{_ANSI_RED}{old}{_ANSI_RESET}")
        elif tag == "insert":
            parts.append(f"{_ANSI_GREEN}{new}{_ANSI_RESET}")
        elif tag == "replace":
            parts.append(f"{_ANSI_RED}{old}{_ANSI_RESET}")
            parts.append(f"{_ANSI_GREEN}{new}{_ANSI_RESET}")
    return "".join(parts)
