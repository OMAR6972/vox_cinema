"""Decides what a tracked movie should say, given the showtimes VOX has right now.

The rules (see the design notes in VOX-ALERTS-GUIDE.md):
  * Nothing happens unless something is NEW to this watch (not in its "seen" notebook).
  * The movie's very first showtimes always send a message ("opening"), even if the chosen day is not out.
  * If days were chosen, new showtimes on other days stay silent until a chosen day gets some.
  * A message only ever shows what is new, never relists older times.
"""
from __future__ import annotations

from .vtime import parse_hhmm, weekday_of


def has_day_pref(w) -> bool:
    return len(w.get("dates") or []) + len(w.get("weekdays") or []) > 0


def day_wanted(w, iso) -> bool:
    if not has_day_pref(w):
        return True
    return iso in (w.get("dates") or []) or weekday_of(iso) in (w.get("weekdays") or [])


def _prefs(w):
    return [m for m in (parse_hhmm(t) for t in (w.get("times") or [])) if m is not None]


def distance(w, s):
    prefs = _prefs(w)
    if not prefs:
        return None
    return min(abs(s["min"] - p) for p in prefs)


def _same_exp(a, b):
    return a.strip().lower() == b.strip().lower()


def sessions_for_watch(snapshot, w, today):
    """snapshot = {"sessions": {date: [session, ...]}}"""
    out = []
    for date, lst in (snapshot.get("sessions") or {}).items():
        if date < today:
            continue
        for s in lst:
            if s["slug"] != w["slug"]:
                continue
            if w.get("cinemas") and s["cinema"] not in w["cinemas"]:
                continue
            if w.get("experiences") and not any(_same_exp(e, s["exp"]) for e in w["experiences"]):
                continue
            if not s["available"] and not w.get("includeSoldOut"):
                continue
            out.append(s)
    by_key = {}
    for s in out:
        key = (s["date"], s["cinema"], s["exp"], s["min"])
        prev = by_key.get(key)
        if prev is None or (not prev["available"] and s["available"]):
            by_key[key] = s
    kept = {id(v) for v in by_key.values()}
    return sorted((s for s in out if id(s) in kept), key=lambda s: (s["date"], s["min"]))


def _group_by_date(lst):
    m = {}
    for s in lst:
        m.setdefault(s["date"], []).append(s)
    return m


def day_block(w, date, lst):
    """How one day's times are laid out. `lst` holds only the times that should be shown."""
    prefs = _prefs(w)
    flex = float(w.get("flex", 30) if w.get("flex") is not None else 30)
    sessions = [dict(s, dist=distance(w, s)) for s in lst]
    if not prefs:
        return {"date": date, "mode": "all", "sessions": sorted(sessions, key=lambda s: s["min"])}
    matches = sorted((s for s in sessions if s["dist"] <= flex), key=lambda s: (s["dist"], s["min"]))
    if matches:
        others = sorted((s for s in sessions if s["dist"] > flex), key=lambda s: s["min"])
        return {"date": date, "mode": "match", "matches": matches, "others": others}
    return {"date": date, "mode": "closest",
            "sessions": sorted(sessions, key=lambda s: (s["dist"], s["min"]))}


def evaluate(w, sessions, seen=frozenset(), intro=False, opening=False):
    """Returns None when there is nothing to say, else a plan for the formatter.

    kinds: waiting / intro (start of tracking), opening (first showtimes ever),
           new (new showtimes), added (a few more showtimes on days that already had some).
    """
    by_date = _group_by_date(sessions)
    all_dates = sorted(by_date)
    pref = has_day_pref(w)
    base = {"watch": w, "intro": intro, "opening": opening, "dayPref": pref}

    if intro:
        if not sessions:
            return dict(base, kind="waiting")
        dates = [d for d in all_dates if day_wanted(w, d)]
        return dict(base, kind="intro", yourDayOut=bool(dates),
                    days=[day_block(w, d, by_date[d]) for d in dates])

    fresh = [s for s in sessions if s["id"] not in seen]
    if not fresh:
        return None
    fresh_by = _group_by_date(fresh)
    fresh_dates = sorted(fresh_by)

    if opening:
        wanted = [d for d in all_dates if day_wanted(w, d)] if pref else []
        others = [d for d in all_dates if d not in wanted]
        return dict(base, kind="opening",
                    yourDay=[day_block(w, d, by_date[d]) for d in wanted],
                    days=[day_block(w, d, by_date[d]) for d in others])

    wanted_fresh = [d for d in fresh_dates if day_wanted(w, d)]
    if not wanted_fresh:
        return None  # a day was chosen and none of the new times are on it: stay silent
    seen_dates = {s["date"] for s in sessions if s["id"] in seen}
    added = all(d in seen_dates for d in wanted_fresh)
    new_exps = []
    if added:
        had = {s["exp"] for s in sessions if s["id"] in seen and s["date"] in wanted_fresh}
        new_exps = sorted({s["exp"] for s in fresh if s["date"] in wanted_fresh} - had)
    return dict(base, kind="added" if added else "new", newExps=new_exps, freshDates=wanted_fresh,
                weekRelease=len(wanted_fresh) >= 4,
                days=[day_block(w, d, fresh_by[d]) for d in wanted_fresh])
