"""Send HTML alert emails via Gmail SMTP."""

from __future__ import annotations

import datetime as dt
import html
import os
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..analysis.deltas import AlertDecision, Baseline
from ..analysis.fomc_deltas import FomcAlertDecision
from ..fetchers.federalreserve import Speaker
from ..fetchers.fomc import DOC_STATEMENT, doc_type_label
from . import diff as _diff

GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 587


@dataclass
class AlertItem:
    """Speech alert: deviation from speaker's trailing 90d baseline."""
    speaker: Speaker
    speech_url: str
    speech_date: dt.date
    title: str
    location: str
    score: float
    label: str
    rationale: str
    key_quotes: list[str]
    baseline: Baseline
    decision: AlertDecision


@dataclass
class FomcAlertItem:
    """FOMC document alert: deviation from prior doc of same kind."""
    doc_type: str
    speech_url: str
    speech_date: dt.date
    title: str
    score: float
    label: str
    rationale: str
    key_quotes: list[str]
    decision: FomcAlertDecision
    body: str | None = None        # current doc body, for diff rendering
    prior_body: str | None = None  # prior doc body, for diff rendering
    diff_notes: list[str] | None = None  # auto-annotated explanation bullets


def send_alerts(
    items: list[AlertItem | FomcAlertItem],
    *,
    dry_run: bool = False,
) -> str | None:
    """Send a single digest email containing all alerts.

    Returns the rendered HTML body (useful for dry-run inspection).
    Returns None when items is empty.
    """
    if not items:
        return None

    sender = os.environ.get("GMAIL_SENDER")
    recipient = os.environ.get("ALERT_RECIPIENT")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not (sender and recipient and (password or dry_run)):
        raise RuntimeError(
            "Email env vars missing: need GMAIL_SENDER, ALERT_RECIPIENT, GMAIL_APP_PASSWORD"
        )

    subject = _subject(items)
    body_html = _body(items)

    if dry_run:
        print("--- DRY RUN: would send email ---")
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print(body_html)
        return body_html

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(_plain(items), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP(GMAIL_HOST, GMAIL_PORT) as smtp:
        smtp.starttls()
        smtp.login(sender, password)
        smtp.sendmail(sender, [recipient], msg.as_string())

    return body_html


def _subject(items: list[AlertItem | FomcAlertItem]) -> str:
    if len(items) == 1:
        it = items[0]
        sign = "+" if it.decision.delta >= 0 else "−"
        if isinstance(it, FomcAlertItem):
            label = doc_type_label(it.doc_type)
            return f"Fed Chirp — {label} shift ({sign}{abs(it.decision.delta):.2f})"
        last = it.speaker.name.split()[-1]
        return f"Fed Chirp — {last} tone shift ({sign}{abs(it.decision.delta):.2f})"
    return f"Fed Chirp — {len(items)} tone shifts"


def _body(items: list[AlertItem | FomcAlertItem]) -> str:
    blocks: list[str] = []
    for i in items:
        if isinstance(i, FomcAlertItem):
            blocks.append(_fomc_block(i))
        else:
            blocks.append(_speech_block(i))
    return _PAGE.format(content="\n".join(blocks))


def _speech_block(it: AlertItem) -> str:
    sign_class = "hawk" if it.decision.delta > 0 else "dove"
    direction = "more hawkish" if it.decision.delta > 0 else "more dovish"
    quotes = "".join(
        f'<li>"{html.escape(q)}"</li>' for q in it.key_quotes
    )
    z = (
        f"z = {it.decision.z_score:+.2f}"
        if it.decision.z_score is not None
        else "z = n/a"
    )
    return f"""
    <div class="alert">
      <h3>{html.escape(it.speaker.name)} — score {it.score:+.2f}
          <span class="{sign_class}">({direction})</span></h3>
      <p class="meta">
        {it.speech_date.isoformat()} &middot;
        delta vs 90d baseline = {it.decision.delta:+.2f} ({z}, n={it.baseline.n}) &middot;
        baseline mean {it.baseline.mean:+.2f}
      </p>
      <p><strong>{html.escape(it.title)}</strong>
         &mdash; <span class="muted">{html.escape(it.location)}</span></p>
      <p>{html.escape(it.rationale)}</p>
      <ul class="quotes">{quotes}</ul>
      <p><a href="{html.escape(it.speech_url)}">{html.escape(it.speech_url)}</a></p>
    </div>"""


def _fomc_block(it: FomcAlertItem) -> str:
    sign_class = "hawk" if it.decision.delta > 0 else "dove"
    direction = "more hawkish" if it.decision.delta > 0 else "more dovish"
    quotes = "".join(
        f'<li>"{html.escape(q)}"</li>' for q in it.key_quotes
    )
    label = doc_type_label(it.doc_type)
    prior_meta = ""
    if it.decision.prior_score is not None and it.decision.prior_date is not None:
        prior_meta = (
            f" &middot; prior {label} {it.decision.prior_date.isoformat()} "
            f"scored {it.decision.prior_score:+.2f}"
        )

    notes_html = ""
    if it.diff_notes:
        items_md = "".join(
            f'<li>{_render_inline_md(n)}</li>' for n in it.diff_notes
        )
        notes_html = f"""
      <p class="meta">What changed vs the previous statement:</p>
      <ul class="diff-notes">{items_md}</ul>"""

    diff_html = ""
    if (
        it.doc_type == DOC_STATEMENT
        and it.body is not None
        and it.prior_body is not None
    ):
        diff_html = f"""
      <p class="meta">Word-diff vs previous statement:</p>
      <div class="diff">{_diff.render_html(it.prior_body, it.body)}</div>"""

    return f"""
    <div class="alert">
      <h3>{html.escape(label)} — score {it.score:+.2f}
          <span class="{sign_class}">({direction})</span></h3>
      <p class="meta">
        {it.speech_date.isoformat()} &middot;
        Δ vs prior {label} = {it.decision.delta:+.2f}{prior_meta}
      </p>
      <p><strong>{html.escape(it.title)}</strong></p>
      <p>{html.escape(it.rationale)}</p>
      <ul class="quotes">{quotes}</ul>{notes_html}{diff_html}
      <p><a href="{html.escape(it.speech_url)}">{html.escape(it.speech_url)}</a></p>
    </div>"""


def _render_inline_md(text: str) -> str:
    """Render a tiny subset of markdown inline: **bold** and *italic*.
    Everything else is HTML-escaped first to avoid injection from the model.
    """
    import re
    safe = html.escape(text)
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", safe)
    return safe


def _plain(items: list[AlertItem | FomcAlertItem]) -> str:
    parts = []
    for it in items:
        if isinstance(it, FomcAlertItem):
            label = doc_type_label(it.doc_type)
            parts.append(
                f"{label} — score {it.score:+.2f}\n"
                f"  {it.speech_date.isoformat()}: {it.title}\n"
                f"  Δ vs prior = {it.decision.delta:+.2f}\n"
                f"  {it.rationale}\n"
                f"  {it.speech_url}\n"
            )
        else:
            z = (
                f"{it.decision.z_score:+.2f}"
                if it.decision.z_score is not None
                else "n/a"
            )
            parts.append(
                f"{it.speaker.name} — score {it.score:+.2f}\n"
                f"  {it.speech_date.isoformat()}: {it.title}\n"
                f"  delta vs 90d = {it.decision.delta:+.2f} (z={z}, n={it.baseline.n})\n"
                f"  {it.rationale}\n"
                f"  {it.speech_url}\n"
            )
    return "\n".join(parts)


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"/>
<style>
 body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        max-width: 720px; margin: 1.5rem auto; padding: 0 1rem; color: #222; }}
 .alert {{ border-left: 4px solid #ddd; padding: 0.5rem 1rem; margin: 1.5rem 0; }}
 h3 {{ margin: 0 0 0.4rem; font-size: 1.05rem; }}
 .hawk {{ color: #c0392b; font-weight: 600; }}
 .dove {{ color: #2e86c1; font-weight: 600; }}
 .meta {{ color: #777; font-size: 0.85rem; margin: 0 0 0.7rem; }}
 .muted {{ color: #999; }}
 .quotes {{ font-size: 0.92rem; color: #555; }}
 .quotes li {{ margin: 0.2rem 0; }}
 .diff-notes {{ font-size: 0.92rem; color: #333; padding-left: 1.2rem; }}
 .diff-notes li {{ margin: 0.4rem 0; line-height: 1.4; }}
 .diff {{ background: #fafafa; padding: 0.6rem 0.8rem; border-radius: 4px;
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: 0.85rem; line-height: 1.5; white-space: pre-wrap; }}
 a {{ color: #1f6feb; word-break: break-all; }}
</style></head>
<body>
{content}
<p class="meta">Generated by fed-chirp.</p>
</body></html>
"""
