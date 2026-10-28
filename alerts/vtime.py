"""Date and time helpers. Everything is in Cairo time."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Africa/Cairo")

# Weekday numbers follow JavaScript's getDay(): 0 = Sunday ... 6 = Saturday.
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(TZ)


def today_cairo(now: datetime | None = None) -> str:
    return _now(now).date().isoformat()


def now_cairo_hhmm(now: datetime | None = None) -> str:
    return _now(now).strftime("%H:%M")


def compact_to_iso(s: str) -> str:
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def iso_to_compact(s: str) -> str:
    return s.replace("-", "")


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def weekday_of(iso: str) -> int:
    return (_d(iso).weekday() + 1) % 7


def add_days(iso: str, n: int) -> str:
    return (_d(iso) + timedelta(days=n)).isoformat()


def pretty_date(iso: str) -> str:
    d = _d(iso)
    return f"{WEEKDAYS[weekday_of(iso)]} {d.day} {MONTHS[d.month - 1]}"


def pretty_date_long(iso: str) -> str:
    d = _d(iso)
    return f"{WEEKDAY_NAMES[weekday_of(iso)]} {d.day} {MONTHS[d.month - 1]}"


def parse_vox_time(label: str) -> int | None:
    """'7:45pm' -> minutes after midnight. Shows before 5am count as the same cinema
    night, so 12:30am becomes 24:30 and sorts after 11pm."""
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", str(label).strip().lower())
    if not m:
        return None
    h = int(m.group(1)) % 12
    if m.group(3) == "pm":
        h += 12
    mins = h * 60 + int(m.group(2))
    if mins < 5 * 60:
        mins += 24 * 60
    return mins


def parse_hhmm(s: str) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s))
    if not m:
        return None
    mins = int(m.group(1)) * 60 + int(m.group(2))
    if mins < 5 * 60:
        mins += 24 * 60
    return mins


def pretty_time(mins: int) -> str:
    m = ((mins % 1440) + 1440) % 1440
    h = m // 60
    ampm = "PM" if h >= 12 else "AM"
    h = h % 12 or 12
    return f"{h}:{m % 60:02d} {ampm}"


def label_to_iso(label: str, today_iso: str) -> str:
    """'Wed 16 Dec' -> ISO date, picking the year that keeps it near today."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", label)
    if not m:
        return today_iso
    months = [x.lower() for x in MONTHS]
    mon = months.index(m.group(2).lower()) if m.group(2).lower() in months else None
    if mon is None:
        return today_iso
    year = int(today_iso[:4])
    iso = f"{year}-{mon + 1:02d}-{int(m.group(1)):02d}"
    if iso < today_iso and (_d(today_iso) - _d(iso)).days > 60:
        iso = f"{year + 1}{iso[4:]}"
    return iso
