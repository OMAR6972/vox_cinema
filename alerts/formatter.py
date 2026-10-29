"""Turns plans and events into Telegram (HTML) messages."""
from __future__ import annotations

import re
from html import escape

from .vox import BASE, movie_url
from .vtime import WEEKDAY_NAMES, add_days, parse_hhmm, pretty_date, pretty_date_long, pretty_time

MAX_DAYS = 8


def esc(s) -> str:
    return escape(str(s), quote=False)


def b(s):
    return f"<b>{esc(s)}</b>"


def i(s):
    return f"<i>{esc(s)}</i>"


def link(label, url):
    return f'<a href="{escape(url, quote=True)}">{esc(label)}</a>'


def first_name(p) -> str:
    if not p:
        return "there"
    if p.get("isGroup"):
        return p.get("name") or "everyone"
    parts = (p.get("name") or "").strip().split()
    return parts[0] if parts else "there"


def describe_watch(w) -> str:
    bits = []
    if w.get("dates"):
        bits.append(", ".join(pretty_date(d) for d in w["dates"]))
    if w.get("weekdays"):
        bits.append("every " + " / ".join(WEEKDAY_NAMES[d] for d in w["weekdays"]))
    if w.get("times"):
        bits.append("around " + " / ".join(pretty_time(parse_hhmm(t)) for t in w["times"]))
    bits.append(", ".join(w["cinemas"]) if w.get("cinemas") else "any VOX cinema")
    if w.get("experiences"):
        bits.append(", ".join(w["experiences"]))
    return " · ".join(bits)


def ago(secs) -> str:
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{max(1, secs // 60)} min ago"
    if secs < 48 * 3600:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


def _is_new(s, ctx) -> bool:
    ts = s.get("ts")
    return bool(ctx and ts and ctx["now"] - ts < ctx["window"])


def _age_line(sessions, ctx):
    """'🕒 2 new, added 35 min ago · 8 first seen 3 days ago' so you know how fresh a day is."""
    if not ctx or not sessions:
        return None
    news = [s for s in sessions if _is_new(s, ctx)]
    olds = [s for s in sessions if not _is_new(s, ctx)]
    parts = []
    if news:
        parts.append(f"{len(news)} new, added {ago(ctx['now'] - max(s['ts'] for s in news))}")
    known = [s["ts"] for s in olds if s.get("ts")]
    base = [s for s in olds if not s.get("ts")]
    if known:
        parts.append(f"{len(known)} first seen {ago(ctx['now'] - min(known))}")
    if base:
        since = (ctx.get("since") or "")[:10]
        parts.append(f"{len(base)} out before I started tracking" + (f" on {pretty_date(since)}" if since else "") + ", so no exact first-seen time")
    return i("🕒 " + " · ".join(parts))


def _group(lst, key):
    m = {}
    for x in lst:
        m.setdefault(key(x), []).append(x)
    return m


def _compact_times(lst, ctx=None):
    out = []
    for exp, ss in _group(sorted(lst, key=lambda s: s["min"]), lambda s: s["exp"]).items():
        parts = [link(s["time"], s["url"]) + ("" if s["available"] else " (sold out)")
                 + (" 🆕" if _is_new(s, ctx) else "") for s in ss]
        out.append(f"{esc(exp)}: " + " · ".join(parts))
    return out


def _session_line(s, multi, reason=""):
    where = f" · {s['cinema']}" if multi else ""
    sold = "" if s["available"] else " (sold out)"
    main = f"{b(s['time'])} {esc(s['exp'])}{esc(where)}{sold}"
    return f"{main}{' ' + i(reason) if reason else ''} → {link('Book', s['url'])}"


def date_ranges(dates) -> str:
    parts, start, prev = [], None, None
    for d in sorted(set(dates)):
        if prev and add_days(prev, 1) == d:
            prev = d
            continue
        if start:
            parts.append(pretty_date(start) if start == prev else f"{pretty_date(start)} → {pretty_date(prev)}")
        start = prev = d
    if start:
        parts.append(pretty_date(start) if start == prev else f"{pretty_date(start)} → {pretty_date(prev)}")
    return ", ".join(parts)


def _day_block(w, block, ctx=None):
    lines = [f"📅 {b(pretty_date_long(block['date']))}"]
    allv = block["matches"] + block["others"] if block["mode"] == "match" else block["sessions"]
    multi = len({s["cinema"] for s in allv}) > 1
    wanted = " / ".join(pretty_time(parse_hhmm(t)) for t in (w.get("times") or []))
    mark = lambda s: " 🆕" if _is_new(s, ctx) else ""
    if block["mode"] == "match":
        for n, s in enumerate(block["matches"][:4]):
            reason = ""
            if n == 0:
                reason = "exactly your time" if s["dist"] == 0 else f"closest to your {wanted}"
            lines.append(f"🎯 {_session_line(s, multi, reason)}{mark(s)}")
        if block["others"]:
            lines.append(i("Other times that day:"))
            for cinema, ss in _group(block["others"], lambda s: s["cinema"]).items():
                if multi:
                    lines.append(f"📍 {esc(cinema)}")
                lines.extend("   " + l for l in _compact_times(ss, ctx))
    elif block["mode"] == "closest":
        lines.append(f"Nothing near {esc(wanted)} 😕 Here's everything that day, closest first:")
        for s in block["sessions"][:10]:
            lines.append(f"• {_session_line(s, multi)}{mark(s)}")
        if len(block["sessions"]) > 10:
            lines.append(i(f"…and {len(block['sessions']) - 10} more"))
    else:
        for cinema, ss in _group(block["sessions"], lambda s: s["cinema"]).items():
            lines.append(f"📍 {esc(cinema)}")
            lines.extend("   " + l for l in _compact_times(ss, ctx))
        if block.get("already"):
            lines.append(i(f"+ {block['already']} other time{'' if block['already'] == 1 else 's'} that day, nothing new there"))
    age = _age_line(allv, ctx)
    if age:
        lines.append(age)
    return "\n".join(lines)


def render_watch_message(plan, person) -> str:
    w = plan["watch"]
    name = first_name(person)
    title = b(w["title"])
    out = []
    ctx = plan.get("ctx")
    kind = plan["kind"]
    if plan["intro"]:
        out.append(f"👋 Hey {esc(name)}! I'll be watching {title} for you.")
        out.append(i(describe_watch(w)))

    if kind == "waiting":
        out.append("No showtimes for it yet. The second booking opens, you'll hear from me 🍿")
    elif kind in ("yourday", "update"):
        day_names = " & ".join(pretty_date_long(d["date"]) for d in plan["days"])
        if plan["intro"]:
            out.append(f"\nGood news, it's already out for {b(day_names)}:")
        elif plan["opening"]:
            out.append(f"🚨 {esc(name)}! Booking just OPENED for {title}, and your day is in: {b(day_names)} 🎯")
        elif kind == "update":
            out.append(f"🆕 {esc(name)}, new {title} times were just added for {b(day_names)}:")
        else:
            out.append(f"🎬 {esc(name)}! {title} is out for {b(day_names)} 🎯")
        for d in plan["days"][:MAX_DAYS]:
            out.append("\n" + _day_block(w, d, ctx))
        if plan.get("otherDays"):
            out.append(f"\n{i('Also out:')} {date_ranges(plan['otherDays'])}")
        out.append(f"\n{link('All ' + w['title'] + ' times on VOX', movie_url(w['slug'], plan['days'][0]['date']))}")
        if not plan["intro"]:
            out.append("Go grab the good seats 🍿")
    elif kind == "notyet":
        wanted_txt = " / ".join([pretty_date_long(d) for d in (w.get("dates") or [])]
                                + [WEEKDAY_NAMES[d] for d in (w.get("weekdays") or [])])
        fresh = [] if plan["intro"] else [d for d in plan["days"] if d["isNew"]]
        older = [d for d in plan["days"] if d not in fresh]
        if plan["intro"]:
            out.append(f"\n{b(wanted_txt)} isn't out yet. I'll message you the moment it is.")
        elif plan["opening"]:
            out.append(f"🚨 Heads up {esc(name)}! Booking just OPENED for {title}, but {b(wanted_txt)} isn't out yet. I'll tell you the moment it is.")
        else:
            out.append(f"🎬 Heads up {esc(name)}! New {title} dates just dropped, but {b(wanted_txt)} isn't out yet. I'll tell you the moment it is.")
        if fresh:
            out.append(f"\n🆕 {b('Just released:')}")
            multi = len({s["cinema"] for d in fresh for s in d["sessions"]}) > 1
            groups = []
            for d in fresh:
                lines = []
                for cinema, ss in _group(d["sessions"], lambda s: s["cinema"]).items():
                    times = " · ".join(s["time"] + (f" ({s['exp']})" if s["exp"] != "Standard" else "")
                                       for s in sorted(ss, key=lambda s: s["min"]))
                    lines.append(f"   {esc(cinema) + ': ' if multi else ''}{esc(times)}")
                body = "\n".join(lines)
                if groups and groups[-1]["body"] == body and add_days(groups[-1]["dates"][-1], 1) == d["date"]:
                    groups[-1]["dates"].append(d["date"])
                else:
                    groups.append({"dates": [d["date"]], "body": body})
            for g in groups[:MAX_DAYS]:
                out.append(f"📅 {b(date_ranges(g['dates']))}\n{g['body']}")
            if len(groups) > MAX_DAYS:
                out.append(i("…plus more days on the VOX site"))
        if older:
            out.append(f"\n{'Already out' if fresh else 'Showing right now'}: {date_ranges([d['date'] for d in older])}")
        link_date = (fresh[0] if fresh else plan["days"][0])["date"]
        out.append(f"\n{link('See times & book', movie_url(w['slug'], link_date))}")
    elif kind == "schedule":
        if plan["intro"]:
            out.append("\nHere's what's out right now:")
        elif plan["opening"]:
            out.append(f"🚨 {esc(name)}! Booking just OPENED for {title}!")
        else:
            out.append(f"🎬 {esc(name)}! New {title} times just dropped:")
        for d in plan["days"][:MAX_DAYS]:
            out.append("\n" + _day_block(w, d, ctx))
        if len(plan["days"]) > MAX_DAYS:
            out.append(i(f"\n…plus {len(plan['days']) - MAX_DAYS} more days on the VOX site"))
        first = plan["days"][0]["date"] if plan["days"] else None
        out.append(f"\n{link('See times & book', movie_url(w['slug'], first))}")
        if not plan["intro"]:
            out.append("Go grab the good seats 🍿")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def render_new_dates(ev, person) -> str:
    movies = ", ".join(esc(m) for m in ev["movies"][:12])
    if len(ev["movies"]) > 12:
        movies += f", +{len(ev['movies']) - 12} more"
    n = len(ev["movies"])
    return "\n".join([
        f"📅 {esc(first_name(person))}! VOX just dropped new dates: {b(date_ranges(ev['dates']))}",
        f"🍿 {n} movie{'' if n == 1 else 's'}: {movies}",
        link("See all showtimes", f"{BASE}/showtimes?d={ev['dates'][0].replace('-', '')}"),
    ])


def render_new_movie(ev, person) -> str:
    exps = f" ({', '.join(ev['experiences'])})" if ev.get("experiences") else ""
    return "\n".join([
        f"🚨 {esc(first_name(person))}! Booking just opened for {b(ev['title'])}",
        f"First shows from {b(pretty_date_long(ev['firstDate']))}{esc(exps)}",
        link("See times & book", movie_url(ev["slug"], ev["firstDate"])),
    ])


def render_coming_soon(ev, person) -> str:
    rel = f" (out {esc(ev['release'])})" if ev.get("release") else ""
    return f"🎞️ New on VOX Coming Soon: {b(ev['title'])}{rel}\n{link('Details', BASE + '/movies/' + ev['slug'])}"


def render_daily_summary(sum_, person) -> str:
    out = [f"☀️ {b('VOX roundup · ' + pretty_date(sum_['today']))}"]
    if sum_["newDates"]:
        out.append(f"📅 New dates: {date_ranges(sum_['newDates'])}")
    if sum_["newMovies"]:
        out.append("🚨 Booking opened: " + ", ".join(b(m) for m in sum_["newMovies"]))
    if sum_["comingSoon"]:
        out.append("🎞️ Added to Coming Soon: " + ", ".join(esc(m) for m in sum_["comingSoon"]))
    if not (sum_["newDates"] or sum_["newMovies"] or sum_["comingSoon"]):
        out.append("Nothing new in the last 24 hours.")
    if sum_.get("showingThrough"):
        out.append(f"🗓️ Regular showtimes are out through {b(pretty_date_long(sum_['showingThrough']))}")
    if sum_["bookable"]:
        bk = sum_["bookable"]
        out.append(f"🍿 Bookable now ({len(bk)}): " + ", ".join(esc(m) for m in bk[:20]) + ("…" if len(bk) > 20 else ""))
    if sum_["myWatches"]:
        out.append("👀 I'm watching for you: " + ", ".join(esc(m) for m in sum_["myWatches"]))
    return "\n".join(out)
