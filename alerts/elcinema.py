"""Reads cinema pages on elcinema.com (Sun City, City Stars) and turns them into plain data.

elcinema is organised per cinema: one page per cinema shows every movie with its showtimes for ONE day, and a
date dropdown loads the other days through a small POST. A movie row may carry an orange "Buy tickets" button
(a link to /en/booking/<movie>/<cinema>/). Many movies have no button, some get one later, and some cinemas
(City Stars) do not sell online at all, so the button is tracked separately from the showtimes.
Sessions come out in the same shape the VOX reader produces, plus two flags: `nobook` (no Buy tickets button)
and `noOnline` (the cinema does not sell tickets online on elcinema at all).
"""
from __future__ import annotations

import re
import time

from bs4 import BeautifulSoup

from .vtime import pretty_time

BASE = "https://elcinema.com"
SOURCE = "elc"
# key -> elcinema cinema id and the name messages and the panel use
THEATERS = {
    "sun-city": {"id": 3101119, "name": "Sun City Cinema"},
    # Sun City's VIP halls are a separate cinema on elcinema. To people it is the same Sun City, so it shares the
    # name and its showings are told apart by the screen type "VIP".
    "sun-city-vip": {"id": 3101257, "name": "Sun City Cinema", "exp": "VIP", "label": "Sun City Cinema (VIP)"},
    "city-stars": {"id": 3101050, "name": "City Stars Cinema"},
}
CINEMAS = list(dict.fromkeys(t["name"] for t in THEATERS.values()))


def theater_url(tid) -> str:
    return f"{BASE}/en/theater/{tid}/"


def parse_time(label: str) -> int | None:
    """'10:15 am' / '01:15 pm' / '12:30 midnight' -> minutes after midnight.
    Shows after midnight (labelled 'midnight', or am before 5) belong to the same cinema night, so 12:30 midnight
    becomes 24:30 and sorts after 11 pm, exactly like the VOX reader."""
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm|midnight|noon)?$", str(label).strip().lower())
    if not m:
        return None
    h, mi, tag = int(m[1]) % 12, int(m[2]), m[3]
    mins = (h + 12 if tag in ("pm", "noon") else h) * 60 + mi
    if tag == "midnight" or mins < 5 * 60:
        mins += 24 * 60
    return mins


def parse_theater_page(html: str) -> dict:
    """{dates: [iso], online: bool, csrf: token}. The dates are the days the dropdown offers."""
    soup = BeautifulSoup(html, "html.parser")
    dates = sorted({o.get("value", "") for o in soup.select("#theater-showtimes-date-selector option")
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", o.get("value", ""))})
    online = "online tickets purchasing is currently not an available service" not in soup.get_text(" ", strip=True).lower()
    tag = soup.select_one('meta[name="csrf-token"]')
    return {"dates": dates, "online": online, "csrf": tag.get("content") if tag else None}


def parse_day(html: str) -> list:
    """The movies one day's fragment (or page) lists:
    [{title, work, button (booking URL or None), times: [(experience, minutes)]}]."""
    soup = BeautifulSoup(html or "", "html.parser")
    out, seen = [], set()
    for thumb in soup.select(".thumbnail-wrapper"):
        row = thumb.find_parent("div", class_="row")
        link = row.select_one('h3 a[href*="/work/"]') if row else None
        if not link:
            continue
        m = re.search(r"/work/(\d+)", link.get("href", ""))
        if not m or m[1] in seen:
            continue
        seen.add(m[1])
        buy = row.select_one('a[href*="/booking/"]')
        times = []
        for slide in row.select("div.swiper-slide"):
            head = slide.select_one("h6 strong, h6")
            exp = head.get_text(" ", strip=True) if head else "Standard"
            for cell in slide.select("table.showtimes td strong"):   # includes the rows hidden behind "More (N)"
                mins = parse_time(cell.get_text(" ", strip=True))
                if mins is not None:
                    times.append((exp or "Standard", mins))
        href = buy.get("href", "") if buy else ""
        out.append({"title": link.get_text(" ", strip=True), "work": m[1],
                    "button": (BASE + href if href.startswith("/") else href) or None, "times": times})
    return out


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def sessions_for(movie: dict, iso: str, key: str, slug: str, online: bool = True) -> list:
    th = THEATERS[key]
    out, ids = [], set()
    for exp, mins in movie["times"]:
        exp = th.get("exp") or exp
        sid = f"elc:{key}:{iso}:{mins}:{norm(exp)}:{slug}"
        if sid in ids:
            continue
        ids.add(sid)
        out.append({
            "place": th.get("label", th["name"]),   # what messages call this place (VIP is told apart from regular)
            "id": sid, "slug": slug, "title": movie["title"], "date": iso, "cinema": th["name"], "exp": exp,
            "min": mins, "time": pretty_time(mins),
            "available": True,                      # elcinema never says "sold out"
            "nobook": not movie["button"],          # no orange Buy tickets button on elcinema (yet)
            "noOnline": not online,                 # the cinema does not sell tickets online on elcinema at all
            "url": movie["button"] or theater_url(th["id"]),
        })
    return out


class Client:
    """Talks to elcinema like a browser: open the cinema page (cookie + token), then POST for each day.
    Injectable in tests: the engine only needs .theater(id) -> html and .day(id, iso) -> html."""

    def __init__(self, session_factory=None, sleep=time.sleep):
        self._factory = session_factory
        self._sleep = sleep
        self._session = None
        self._csrf = {}

    def _s(self):
        if self._session is None:
            if self._factory:
                self._session = self._factory()
            else:
                from curl_cffi import requests as cffi
                self._session = cffi.Session(impersonate="chrome124")
        return self._session

    def _go(self, call, what):
        last = None
        for n in range(3):
            try:
                r = call()
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")
                return r.text
            except Exception as e:  # noqa: BLE001 - curl_cffi raises its own errors
                last = e
                if n < 2:
                    self._sleep(2 ** n)
        raise RuntimeError(f"elcinema {what} failed: {last}")

    def theater(self, tid) -> str:
        html = self._go(lambda: self._s().get(theater_url(tid), timeout=20), f"cinema {tid}")
        self._csrf[tid] = parse_theater_page(html)["csrf"]
        return html

    def day(self, tid, iso) -> str:
        headers = {"X-CSRF-Token": self._csrf.get(tid) or "", "X-Requested-With": "XMLHttpRequest",
                   "Content-Type": "application/x-www-form-urlencoded", "Referer": theater_url(tid),
                   "Accept": "text/html, */*; q=0.01"}
        return self._go(lambda: self._s().post(f"{BASE}/en/theater/ajax_show", data={"date": iso, "id": str(tid)},
                                              headers=headers, timeout=20), f"day {iso} for cinema {tid}")
