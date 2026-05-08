"""Scrape the FOMC meeting calendar from federalreserve.gov.

Page structure (consistent across years):

    <a id="42828">2026 FOMC Meetings</a>
      ...
      <div class="fomc-meeting__month ...">January</div>
      <div class="fomc-meeting__date ...">27-28</div>
      ...
      <div class="fomc-meeting__month ...">March</div>
      <div class="fomc-meeting__date ...">17-18*</div>     # * marks SEP meetings

We take the SECOND day of each multi-day meeting (the day policy is set,
which is also when the statement and press conference are released).

Since 2019 every FOMC meeting has been followed by a press conference, so
we mark `has_press_conf=True` for all meetings.
"""

from __future__ import annotations

import datetime as dt
import re

import requests
from bs4 import BeautifulSoup

CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
USER_AGENT = "fed-chirp/0.1 (+https://github.com/local; personal monitor)"
REQUEST_TIMEOUT = 30

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

_YEAR_HEADER_RE = re.compile(r"^(\d{4})\s+FOMC Meetings$")


def fetch_meetings() -> list[tuple[dt.date, bool]]:
    """Return a list of `(meeting_date, has_press_conf)` covering all
    years currently visible on the calendar page.
    """
    resp = requests.get(
        CALENDAR_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    out: list[tuple[dt.date, bool]] = []

    # Each year section is anchored by <a id="..."><year> FOMC Meetings</a>
    # within an <h4>. Scan the document linearly: track the current year as
    # we encounter each header, then collect every (month, date) pair until
    # the next header.
    current_year: int | None = None
    months = soup.select("div.fomc-meeting__month")
    dates = soup.select("div.fomc-meeting__date")

    # Walk the entire DOM in document order so year headers and meetings
    # interleave correctly. Use a unified node list.
    targets = soup.find_all(
        lambda tag: (
            (tag.name == "a" and tag.get("id") and _YEAR_HEADER_RE.match(
                (tag.get_text() or "").strip())) or
            (tag.name == "div" and tag.get("class") and (
                "fomc-meeting__month" in tag["class"]
                or "fomc-meeting__date" in tag["class"]
            ))
        )
    )

    pending_month: str | None = None
    for tag in targets:
        if tag.name == "a":
            m = _YEAR_HEADER_RE.match((tag.get_text() or "").strip())
            if m:
                current_year = int(m.group(1))
                pending_month = None
            continue
        classes = tag.get("class", [])
        if "fomc-meeting__month" in classes:
            # The month name is inside a nested <strong>.
            pending_month = tag.get_text(strip=True).strip("*")
        elif "fomc-meeting__date" in classes:
            if pending_month is None or current_year is None:
                continue
            d = _parse_meeting_end_day(tag.get_text(strip=True))
            if d is None:
                continue
            month_num = _MONTHS.get(pending_month)
            if month_num is None:
                pending_month = None
                continue
            try:
                meeting_date = dt.date(current_year, month_num, d)
            except ValueError:
                pending_month = None
                continue
            out.append((meeting_date, True))   # all post-2019 meetings have pressers
            pending_month = None

    # Dedupe (in case a year appears twice on the page) and sort.
    out = sorted(set(out), key=lambda x: x[0])
    return out


def _parse_meeting_end_day(s: str) -> int | None:
    """Parse '27-28', '17-18*', '8-9*', or single '15' into the end day."""
    s = s.strip().rstrip("*")
    nums = re.findall(r"\d+", s)
    if not nums:
        return None
    # Pick the LAST plausible day-of-month
    for raw in reversed(nums):
        n = int(raw)
        if 1 <= n <= 31:
            return n
    return None
