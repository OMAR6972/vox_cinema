"""Decides what a watch should say, given current showtimes.

The ladder:
  1. your day + your time exist         -> exact match (+ other times that day)
  2. your day exists, your time doesn't -> every time that day, closest first
  3. your day isn't out yet             -> every day that IS out
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


def day_block(w, date, lst, seen, intro=False):
    prefs = _prefs(w)
    flex = float(w.get("flex", 30) if w.get("flex") is not None else 30)
    sessions = [dict(s, dist=distance(w, s), isNew=s["id"] not in seen) for s in lst]
    if not prefs:
        ordered = sorted(sessions, key=lambda s: s["min"])
        fresh = [s for s in ordered if s["isNew"]]
        if not intro and fresh and len(fresh) < len(ordered):
            return {"date": date, "mode": "all", "sessions": fresh, "already": len(ordered) - len(fresh)}
        return {"date": date, "mode": "all", "sessions": ordered}
    matches = sorted((s for s in sessions if s["dist"] <= flex), key=lambda s: (s["dist"], s["min"]))
    if matches:
        others = sorted((s for s in sessions if s["dist"] > flex), key=lambda s: s["min"])
        return {"date": date, "mode": "match", "matches": matches, "others": others}
    return {"date": date, "mode": "closest",
            "sessions": sorted(sessions, key=lambda s: (s["dist"], s["min"]))}


def evaluate(w, sessions, seen=frozenset(), intro=False):
    """Returns None when there's nothing to say, else a plan for the formatter."""
    fresh = [s for s in sessions if s["id"] not in seen]
    if not intro and not fresh:
        return None
    by_date = _group_by_date(sessions)
    all_dates = sorted(by_date)
    fresh_dates = sorted({s["date"] for s in fresh})
    opening = w.get("everHadSessions") is False and len(sessions) > 0 and not intro
    base = {"watch": w, "opening": opening, "intro": intro}

    if not sessions:
        return dict(base, kind="waiting") if intro else None

    if has_day_pref(w):
        wanted_fresh = [d for d in fresh_dates if day_wanted(w, d)]
        wanted_all = [d for d in all_dates if day_wanted(w, d)]
        to_show = wanted_all if intro else wanted_fresh
        if to_show:
            days = [day_block(w, d, by_date[d], seen, intro) for d in to_show]
            update = (not intro) and all(any(s["id"] in seen for s in by_date[d]) for d in to_show)
            other = [d for d in (all_dates if intro else fresh_dates) if not day_wanted(w, d)]
            return dict(base, kind="update" if update else "yourday", days=days, otherDays=other)
        if intro or w.get("mode") != "mine":
            return dict(base, kind="notyet",
                        days=[{"date": d, "sessions": by_date[d], "isNew": d in fresh_dates} for d in all_dates],
                        freshDates=fresh_dates)
        return dict(base, kind="silent")

    show = all_dates if intro else fresh_dates
    return dict(base, kind="schedule", days=[day_block(w, d, by_date[d], seen, intro) for d in show])
