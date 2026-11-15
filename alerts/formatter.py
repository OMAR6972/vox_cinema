"""Turns plans and events into Telegram (HTML) messages. Discord gets the same text converted to markdown."""
from __future__ import annotations

import re
from html import escape

from .vox import BASE, movie_url as _vox_url

# Movies VOX does not list (only another cinema has them): slug -> (site name, page link). Set by the engine each run.
OFF_VOX = {}


def movie_url(slug, iso=None):
    return OFF_VOX[slug][1] if slug in OFF_VOX else _vox_url(slug, iso)


def site_of(slug):
    return OFF_VOX[slug][0] if slug in OFF_VOX else "VOX"
from .vtime import WEEKDAY_NAMES, add_days, parse_hhmm, pretty_date, pretty_date_long, pretty_time

MAX_DAYS = 8
PAGE = 20  # movies per roundup message


def esc(s) -> str:
    return escape(str(s), quote=False)


def b(s):
    return f"<b>{esc(s)}</b>"


def i(s):
    return f"<i>{esc(s)}</i>"


def strike(s):
    return f"<s>{esc(s)}</s>"


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
    bits.append(", ".join(w["cinemas"]) if w.get("cinemas") else "any cinema")
    if w.get("experiences"):
        bits.append(", ".join(w["experiences"]))
    return " · ".join(bits)


def wanted_text(w) -> str:
    """'Fri 18 Dec' / 'Friday' - the days the person chose."""
    return " / ".join([pretty_date_long(d) for d in (w.get("dates") or [])]
                      + [WEEKDAY_NAMES[d] for d in (w.get("weekdays") or [])])


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


def _group(lst, key):
    m = {}
    for x in lst:
        m.setdefault(key(x), []).append(x)
    return m


def _time_cell(s):
    """A bookable time is a link. A time that cannot be booked is struck through, with no link and nothing added."""
    return link(s["time"], s["url"]) if s["available"] else strike(s["time"])


def _compact_times(lst):
    out = []
    for exp, ss in _group(sorted(lst, key=lambda s: s["min"]), lambda s: s["exp"]).items():
        out.append(f"{esc(exp)}: " + " · ".join(_time_cell(s) for s in ss))
    return out


def _match_line(s, multi, reason=""):
    where = f" · {s['cinema']}" if multi else ""
    t = b(s["time"]) if s["available"] else strike(s["time"])
    main = f"{t} {esc(s['exp'])}{esc(where)}"
    tail = f" → {link('Book', s['url'])}" if s["available"] else ""
    return f"{main}{' ' + i(reason) if reason else ''}{tail}"


def day_block(w, block, everything=False):
    """One '📅 day' block. everything=True means 'all that day', otherwise it lists only the new times."""
    lines = [f"📅 {b(pretty_date_long(block['date']))}"]
    allv = block["matches"] + block["others"] if block["mode"] == "match" else block["sessions"]
    multi = len({s["cinema"] for s in allv}) > 1
    wanted = " / ".join(pretty_time(parse_hhmm(t)) for t in (w.get("times") or []))
    if block["mode"] == "match":
        lines.append(b("⭐ Closest to your time"))
        for n, s in enumerate(block["matches"][:4]):
            reason = ""
            if n == 0:
                reason = "exactly your time" if s["dist"] == 0 else f"closest to your {wanted}"
            lines.append(_match_line(s, multi, reason))
        rest = block["matches"][4:] + block["others"]
        if rest:
            lines.append(b("🕒 Other times that day"))
            for cinema, ss in _group(sorted(rest, key=lambda s: s["min"]), lambda s: s["cinema"]).items():
                if multi:
                    lines.append(f"📍 {esc(cinema)}")
                lines.extend("   " + l for l in _compact_times(ss))
    elif block["mode"] == "closest":
        what = "everything that day" if everything else "the new times that day"
        lines.append(f"Nothing near {esc(wanted)} 😕 Here's {what}, closest first:")
        for s in block["sessions"][:10]:
            lines.append(f"• {_match_line(s, multi)}")
        if len(block["sessions"]) > 10:
            lines.append(i(f"…and {len(block['sessions']) - 10} more"))
    else:
        for cinema, ss in _group(block["sessions"], lambda s: s["cinema"]).items():
            lines.append(f"📍 {esc(cinema)}")
            lines.extend("   " + l for l in _compact_times(ss))
    return "\n".join(lines)


def _who(plan):
    """Which site(s) the showtimes in this message came from, so the wording is right for Scene too."""
    names = set()
    for blk in (plan.get("days") or []) + (plan.get("yourDay") or []):
        for key in ("sessions", "matches", "others"):
            for s in blk.get(key) or []:
                names.add("Scene" if str(s.get("id", "")).startswith("scene:") else "VOX")
    return " and ".join(sorted(names, reverse=True)) or "VOX"


def _blocks(w, blocks, everything=False):
    out = [day_block(w, blk, everything) for blk in blocks[:MAX_DAYS]]
    if len(blocks) > MAX_DAYS:
        out.append(i(f"…plus {len(blocks) - MAX_DAYS} more days on the cinema site"))
    return out


def _why(text):
    return "ℹ️ " + i("Why you got this: " + text)


def render_watch_message(plan) -> str:
    """The message for one tracked movie."""
    w = plan["watch"]
    title = w["title"]
    kind = plan["kind"]
    out = []

    if kind in ("waiting", "intro"):
        out += [f"👋 {b('NOW TRACKING')}", b(title), "", "You'll get a message when this movie:"]
        if kind == "waiting":
            out.append("• gets its first showtimes")
        out.append(f"• gets new showtimes ({esc(describe_watch(w))})")
        out += ["", f"📅 {b('RIGHT NOW')}"]
        if kind == "waiting":
            out.append("No showtimes are out for it yet.")
        elif plan["dayPref"] and not plan["yourDayOut"]:
            out.append(f"{b(wanted_text(w))} is not out yet. I'll message you the moment it is.")
        else:
            for blk in _blocks(w, plan["days"], True):
                out += ["", blk]
        out += ["", _why("tracking was just set up for you.")]

    elif kind == "opening":
        out += [f"🚨 {b('FIRST SHOWTIMES ARE OUT')}", b(title), ""]
        filt = " (matching your cinema and screen settings)" if (w.get("cinemas") or w.get("experiences")) else ""
        out.append(f"{_who(plan)} just put the first showtimes up for this movie{filt}.")
        if plan["dayPref"]:
            out += ["", f"📌 {b('YOUR DAY')}"]
            if plan["yourDay"]:
                for d in plan["yourDay"][:MAX_DAYS]:
                    out += [day_block(w, d, True), ""]
                out.pop()
            else:
                out.append(f"{b(wanted_text(w))} is not out yet. I'll message you the moment it is.")
        if plan["days"]:
            dates = [d["date"] for d in plan["days"]]
            out += ["", "📅 " + b("WHAT'S OUT NOW") + f" ({esc(date_ranges(dates))})"]
            for blk in _blocks(w, plan["days"], True):
                out += ["", blk]
        link_date = (plan["yourDay"] or plan["days"])[0]["date"]
        out += ["", link(f"All {title} times on {site_of(w['slug'])}", movie_url(w["slug"], link_date))]
        why = f"you're tracking {title}, and a movie's first showtimes always notify you"
        if plan["dayPref"]:
            why += f", even if your day isn't out yet. After this I'll stay silent until {wanted_text(w)} has showtimes."
        else:
            why += "."
        out += ["", _why(why)]

    else:  # new / added
        dates = plan["freshDates"]
        span = date_ranges(dates)
        if kind == "added":
            out += [f"➕ {b('NEW SHOWTIMES ADDED')}", b(title) + (f" · {esc(span)}" if len(dates) == 1 else ""), ""]
            what = (" & ".join(plan["newExps"]) + " showtimes") if plan["newExps"] else "more showtimes"
            out.append(f"{_who(plan)} just added {esc(what)}. These are the only changes. I'm not repeating times you already know.")
        elif plan["dayPref"]:
            out += [f"🎯 {b('NEW SHOWTIMES ON YOUR DAY')}", b(title) + f" · {esc(span)}", ""]
            if plan["weekRelease"]:
                out.append(f"{_who(plan)} just released its new week. Everything below is new.")
            else:
                out.append(f"{_who(plan)} just released showtimes for your day. Everything below is new.")
            if w.get("times"):
                wanted = " / ".join(pretty_time(parse_hhmm(t)) for t in w["times"])
                out.append(f"You asked for around {esc(wanted)}.")
        else:
            head = f"NEW WEEK FOR {title.upper()}" if plan["weekRelease"] else f"NEW SHOWTIMES FOR {title.upper()}"
            out += [f"🎬 {b(head)}", esc(span), ""]
            out.append(_who(plan) + " just released " + ("its new week" if plan["weekRelease"] else "new showtimes") + ". Everything below is new.")
        for blk in _blocks(w, plan["days"]):
            out += ["", blk]
        out += ["", link(f"All {title} times on {site_of(w['slug'])}", movie_url(w["slug"], dates[0]))]
        if plan["dayPref"]:
            why = f"you're tracking {title} for {describe_watch(w)}, and "
            why += "this addition is on your day." if kind == "added" else "its showtimes for that day just came out."
        else:
            why = f"you're tracking {title} with no specific day, so you get every new showtime."
        out += ["", _why(why)]

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------------------------------------------------------------- the daily roundup

def _numbered(n, text):
    return f"{n}. {text}"


def render_roundup(rd, person, tracked, today, through, links=False):
    """Returns a list of messages: [{"html", "buttons"}]. Telegram buttons carry See times / Track;
    for Discord (links=True) the movie names themselves are links and there are no buttons.
    `tracked` is the set of slugs this person already tracks."""
    blocks = []

    def name(slug, title):
        return link(title, movie_url(slug)) if links else b(title)

    def pages(items):
        return [items[k:k + PAGE] for k in range(0, len(items), PAGE)] or []

    # --- new week
    if rd.get("dates"):
        movies = rd.get("weekMovies") or []
        new_slugs = {m["slug"] for m in (rd.get("newMovies") or [])}
        head = [f"━━ 🗓 {b('NEW WEEK')} ━━",
                f"VOX released a new week: {b(date_ranges(rd['dates']))}."]
        if through:
            head.append(f"Showtimes are now out through {b(pretty_date_long(through))}.")
        n = 0
        for chunk_no, chunk in enumerate(pages(movies) or [[]]):
            lines = list(head) if chunk_no == 0 else []
            if chunk_no == 0 and movies:
                lines += ["", f"{len(movies)} movie{'' if len(movies) == 1 else 's'} {'has' if len(movies) == 1 else 'have'} showtimes this week:"]
            btn = []
            for m in chunk:
                n += 1
                tag = ""
                if m["slug"] in tracked:
                    tag = " ⭐ you track it, I already sent you its times"
                elif m["slug"] in new_slugs:
                    tag = " 🆕 first showtimes, see “New movies” below"
                lines.append(_numbered(n, name(m["slug"], m["title"]) + esc(tag)))
                row = [{"text": f"🎟 {m['title']} times", "url": movie_url(m["slug"])}]
                if m["slug"] not in tracked and m["slug"] not in new_slugs:
                    row.append({"text": f"➕ Track {m['title']}", "callback_data": f"track:{m['slug']}"})
                btn.append(row)
            blocks.append({"html": "\n".join(lines), "buttons": None if links else btn})

    # --- new movies (first showtimes) that this person does not track
    new_movies = [m for m in (rd.get("newMovies") or []) if m["slug"] not in tracked]
    for chunk_no, chunk in enumerate(pages(new_movies)):
        lines = []
        if chunk_no == 0:
            lines += [f"━━ 🎬 {b('NEW MOVIES')} ━━", "Movies that just got their first showtimes at VOX or Scene. You don't track these.", ""]
        btn = []
        for k, m in enumerate(chunk, chunk_no * PAGE + 1):
            exps = f" · {', '.join(m['experiences'])}" if m.get("experiences") else ""
            lines.append(_numbered(k, name(m["slug"], m["title"])))
            if m.get("source"):
                lines.append(f"   Found at {esc(m['source'])}, showtimes not read yet")
            else:
                lines.append(f"   From {esc(pretty_date_long(m['firstDate']))}{esc(exps)}")
            btn.append([{"text": f"🎟 {m['title']} times", "url": movie_url(m["slug"])},
                        {"text": f"➕ Track {m['title']}", "callback_data": f"track:{m['slug']}"}])
        blocks.append({"html": "\n".join(lines), "buttons": None if links else btn})

    # --- coming soon
    coming = [m for m in (rd.get("coming") or []) if m["slug"] not in tracked]
    for chunk_no, chunk in enumerate(pages(coming)):
        lines = []
        if chunk_no == 0:
            lines += [f"━━ 🎞 {b('ADDED TO COMING SOON')} ━━", "New titles on VOX's Coming Soon page.", ""]
        btn = []
        for k, m in enumerate(chunk, chunk_no * PAGE + 1):
            url = f"{BASE}/movies/{m['slug']}"
            lines.append(_numbered(k, link(m["title"], url) if links else b(m["title"])))
            if m.get("release"):
                lines.append(f"   Out {esc(m['release'])}")
            btn.append([{"text": f"ℹ️ {m['title']} details", "url": url},
                        {"text": f"➕ Track {m['title']}", "callback_data": f"track:{m['slug']}"}])
        blocks.append({"html": "\n".join(lines), "buttons": None if links else btn})

    if not blocks:
        return []
    top = (f"☀️ {b('VOX DAILY ROUNDUP')} · {esc(pretty_date(today))}\n\n"
           + _why("this is your daily summary of what's new on VOX. Sections with nothing new are left out."))
    blocks[0]["html"] = top + "\n\n" + blocks[0]["html"]
    if not links:
        last = blocks[-1]
        last["buttons"] = (last["buttons"] or []) + [[{"text": "🔕 Turn off daily roundup", "callback_data": "roundup:off"}]]
    return blocks


# ---------------------------------------------------------------- short replies

def stopped_text(title):
    return f"🔕 Stopped. You won't get any more alerts about {title}."


def booked_text(title):
    return f"🎉 Enjoy the movie! I won't ping you about {title} again."


ROUNDUP_OFF_TEXT = "🔕 Daily roundup turned off. You won't get it anymore. Ask Omar if you want it back."
