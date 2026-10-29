"""One stateless run: read VOX, read replies, message the right people, save state."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import discord as D
from . import formatter as F
from .matcher import evaluate, sessions_for_watch
from .vox import (parse_coming_soon, parse_filters, parse_showtimes_page, showtimes_path)
from .vtime import add_days, now_cairo_hhmm, today_cairo

log = logging.getLogger("vox_alerts")
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ALERTS = {"newWeek": True, "newMovies": True, "comingSoon": False, "dailySummary": True}
HELP = ("🎬 <b>VOX Alerts</b>\nI watch VOX Cinemas Egypt and message you the moment the movies you picked "
        "get showtimes, new dates, or open for booking.\n\n/status - what I'm watching for you\n"
        "Use the buttons under an alert to say you booked, or to stop that one.")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def default_state():
    return {"baselineDone": False, "dates": [], "knownMovies": {}, "comingSoon": [], "watches": {},
            "links": {}, "changes": [], "summary": {"lastSentDate": None}, "offset": 0,
            "failures": 0, "warned": False, "lastCheck": None, "lastError": None, "lastRun": None}


class Engine:
    def __init__(self, config, state, telegram, fetch, owner_chat_id=None, discord_url=None, now=None, sleep=time.sleep, delay=0.4):
        self.config = config
        self.state = {**default_state(), **state}
        self.tg = telegram
        self.fetch = fetch           # fetch(path) -> html
        self.owner_chat_id = owner_chat_id
        self.discord_url = discord_url
        self.now = now or datetime.now(timezone.utc)
        self.sleep = sleep
        self.delay = delay
        self.sent = 0

    # ---- people ----
    @property
    def people(self):
        return self.config.get("people", [])

    def person(self, pid):
        return next((p for p in self.people if p["id"] == pid), None)

    def chat_id(self, p):
        if p.get("owner") and self.owner_chat_id:
            return self.owner_chat_id
        return p.get("chatId") or self.state["links"].get(p["id"])

    def deliver(self, p, html, buttons=None):
        if p.get("active") is False:
            return False
        ok = False
        cid = self.chat_id(p)
        if cid:
            try:
                self.tg.send(cid, html, buttons)
                ok = True
            except Exception as e:  # one person's failure must not block the others
                log.warning("could not message %s on Telegram: %s", p.get("name"), e)
        if p.get("discord") and self.discord_url:
            try:
                D.send(self.discord_url, html)
                ok = True
            except Exception as e:
                log.warning("could not post to Discord: %s", e)
        self.sent += ok
        return ok

    # ---- telegram replies ----
    def handle_updates(self):
        for u in self.tg.updates(self.state["offset"]):
            self.state["offset"] = u["update_id"] + 1
            try:
                self.handle_update(u)
            except Exception as e:
                log.warning("update failed: %s", e)

    def _by_chat(self, chat_id):
        for p in self.people:
            if str(self.chat_id(p)) == str(chat_id):
                return p
        return None

    def handle_update(self, u):
        msg = u.get("message")
        if msg and msg.get("text"):
            chat = msg["chat"]
            parts = msg["text"].strip().split()
            cmd = parts[0].split("@")[0].lower()
            arg = parts[1] if len(parts) > 1 else None
            if cmd == "/start":
                p = next((x for x in self.people if arg and x.get("invite") == arg), None)
                if p:
                    self.state["links"][p["id"]] = chat["id"]
                    name = F.first_name(p)
                    self.tg.send(chat["id"], f"✅ Hi {F.esc(name)}, you're connected! I'll message you here when your movies drop.\n\n/status to see what I'm watching.")
                elif self._by_chat(chat["id"]):
                    self.tg.send(chat["id"], HELP)
                else:
                    self.tg.send(chat["id"], "Hi! Ask the person who runs this bot for your personal invite link.")
            elif cmd == "/status":
                p = self._by_chat(chat["id"])
                self.tg.send(chat["id"], self.status_text(p) if p else "I don't know you yet. Ask for your invite link.")
            elif cmd.startswith("/"):
                self.tg.send(chat["id"], HELP)
        q = u.get("callback_query")
        if q:
            action, _, wid = (q.get("data") or "").partition(":")
            p = self._by_chat(q["message"]["chat"]["id"])
            w = next((x for x in self.config.get("watches", []) if x["id"] == wid), None)
            if p and w and action in ("booked", "stop"):
                ws = self.state["watches"].setdefault(wid, {})
                ws.setdefault("booked", [])
                if p["id"] not in ws["booked"]:
                    ws["booked"].append(p["id"])
                text = (f"🎉 Enjoy the movie! I won't ping you about {w['title']} again." if action == "booked"
                        else f"🔕 Okay, no more alerts about {w['title']} for you.")
                self.tg.answer_callback(q["id"], "Done")
                self.tg.send(q["message"]["chat"]["id"], F.esc(text))
            else:
                self.tg.answer_callback(q["id"], "Done")

    def status_text(self, p):
        mine = [w for w in self.config.get("watches", []) if w.get("active", True) and p["id"] in w.get("recipients", [])
                and p["id"] not in self.state["watches"].get(w["id"], {}).get("booked", [])]
        if not mine:
            return "I'm not watching any movie for you right now."
        return "👀 <b>I'm watching for you:</b>\n" + "\n".join(f"• {F.b(w['title'])}\n  {F.i(F.describe_watch(w))}" for w in mine)

    # ---- crawl ----
    def crawl(self):
        st = self.state
        today = today_cairo(self.now)
        first_run = not st["baselineDone"]
        home_html = self.fetch(showtimes_path())
        home = parse_showtimes_page(home_html)
        if not home.looks_right or not home.dates:
            raise RuntimeError("The VOX showtimes page looks different from what the bot expects (no date list found). The site layout may have changed.")
        sessions = {home.date: home.sessions}
        movies = dict(home.movies)
        for d in home.dates:
            if d == home.date or d < today:
                continue
            self.sleep(self.delay)
            page = parse_showtimes_page(self.fetch(showtimes_path(d)))
            sessions[d] = page.sessions
            movies.update(page.movies)

        new_coming = []
        try:
            lst = parse_coming_soon(self.fetch("/movies/comingsoon"))
            if lst:
                known = {m["slug"] for m in st["comingSoon"]}
                if st["comingSoon"]:
                    new_coming = [m for m in lst if m["slug"] not in known]
                st["comingSoon"] = lst
        except Exception as e:
            log.info("coming soon skipped: %s", e)

        prev_dates = set(st["dates"])
        new_dates = [] if first_run else [d for d in home.dates if d >= today and d not in prev_dates]
        new_slugs = []
        for lst in sessions.values():
            for s in lst:
                if s["slug"] not in st["knownMovies"]:
                    st["knownMovies"][s["slug"]] = {"firstSeen": self.now.isoformat(timespec="seconds"), "title": s["title"]}
                    if not first_run:
                        new_slugs.append(s["slug"])
        st["dates"] = [d for d in home.dates if d >= today]
        st["baselineDone"] = True
        st["lastCheck"] = self.now.isoformat(timespec="seconds")
        st["lastError"] = None

        allv = [s for lst in sessions.values() for s in lst]
        movie_events = []
        for slug in dict.fromkeys(new_slugs):
            ss = sorted((s for s in allv if s["slug"] == slug), key=lambda s: (s["date"], s["min"]))
            movie_events.append({"slug": slug, "title": ss[0]["title"], "firstDate": ss[0]["date"],
                                 "experiences": list(dict.fromkeys(s["exp"] for s in ss))})
        date_event = None
        if new_dates:
            date_event = {"dates": new_dates,
                          "movies": list(dict.fromkeys(s["title"] for s in allv if s["date"] in new_dates))}
        self.snapshot = {"sessions": sessions, "movies": movies}
        return {"first_run": first_run, "date_event": date_event, "movie_events": movie_events,
                "new_coming": [] if first_run else new_coming, "filters": parse_filters(home_html)}

    # ---- watches ----
    def process_watch(self, w):
        st = self.state["watches"].setdefault(w["id"], {})
        today = today_cairo(self.now)
        sessions = sessions_for_watch(self.snapshot, w, today)
        booked = st.setdefault("booked", [])
        first_look = "seen" not in st
        nonce = w.get("introNonce")
        resend = bool(nonce and st.get("introNonce") != nonce)  # "send current times now" pressed in the panel
        intro = bool((first_look and w.get("intro")) or resend)
        if resend:
            st["introNonce"] = nonce
        if first_look and not intro:
            st["seen"] = {s["id"]: s["date"] for s in sessions}
            st["everHadSessions"] = bool(sessions)
            return None
        st.setdefault("seen", {})
        wv = dict(w, everHadSessions=st.get("everHadSessions", True))
        plan = evaluate(wv, sessions, seen=set(st["seen"]), intro=intro)
        if plan and plan["kind"] != "silent":
            everyone = [x["id"] for x in self.people if x.get("allWatches")]
            for pid in list(dict.fromkeys(list(w.get("recipients", [])) + everyone)):
                p = self.person(pid)
                if not p or (pid in booked and not intro):
                    continue
                buttons = None if intro else [[{"text": "✅ I booked it", "callback_data": f"booked:{w['id']}"},
                                               {"text": "🔕 Stop this one", "callback_data": f"stop:{w['id']}"}]]
                self.deliver(p, F.render_watch_message(plan, p), buttons)
        for s in sessions:
            st["seen"][s["id"]] = s["date"]
        st["seen"] = {k: v for k, v in st["seen"].items() if v >= today}
        if sessions:
            st["everHadSessions"] = True
        return plan

    def broadcast(self, flag, render):
        for p in self.people:
            if p.get("active") is False:
                continue
            alerts = {**DEFAULT_ALERTS, **(p.get("alerts") or {})}
            if alerts.get(flag):
                self.deliver(p, render(p))

    def add_change(self, **c):
        ch = self.state["changes"]
        ch.append({"at": self.now.isoformat(timespec="seconds"), **c})
        cutoff = self.now.timestamp() - 3 * 86400
        self.state["changes"] = [x for x in ch if datetime.fromisoformat(x["at"]).timestamp() > cutoff]

    def summary_for(self, p, today):
        since = self.now.timestamp() - 86400
        recent = [c for c in self.state["changes"] if datetime.fromisoformat(c["at"]).timestamp() > since]
        dates = set(self.state["dates"])
        through, d = None, today
        while d in dates:
            through, d = d, add_days(d, 1)
        titles = sorted({s["title"] for lst in self.snapshot["sessions"].values() for s in lst})
        return {
            "today": today,
            "newDates": sorted({d for c in recent if c["type"] == "dates" for d in c["dates"]}),
            "newMovies": list(dict.fromkeys(c["title"] for c in recent if c["type"] == "movie")),
            "comingSoon": list(dict.fromkeys(c["title"] for c in recent if c["type"] == "coming")),
            "showingThrough": through,
            "bookable": titles,
            "myWatches": [w["title"] for w in self.config.get("watches", []) if w.get("active", True)
                          and p["id"] in w.get("recipients", [])
                          and p["id"] not in self.state["watches"].get(w["id"], {}).get("booked", [])],
        }

    def maybe_daily_summary(self, today):
        s = self.state["summary"]
        at = (self.config.get("settings") or {}).get("summaryTime", "12:00")
        if s.get("lastSentDate") == today or now_cairo_hhmm(self.now) < at:
            return
        s["lastSentDate"] = today
        self.broadcast("dailySummary", lambda p: F.render_daily_summary(self.summary_for(p, today), p))

    def warn_admin(self, text):
        admin = (self.config.get("settings") or {}).get("adminPersonId")
        p = self.person(admin) or next((x for x in self.people if x.get("owner")), None)
        if p:
            self.deliver(p, F.esc(text))

    # ---- the run ----
    def run(self):
        st = self.state
        st["lastRun"] = self.now.isoformat(timespec="seconds")
        try:
            self.handle_updates()
        except Exception as e:
            log.warning("could not read Telegram replies: %s", e)
        try:
            res = self.crawl()
        except Exception as e:
            st["failures"] += 1
            st["lastError"] = f"{self.now.isoformat(timespec='seconds')} - {e}"
            log.error("VOX check failed: %s", e)
            if st["failures"] >= 5 and not st["warned"]:
                st["warned"] = True
                self.warn_admin(f"⚠️ The VOX checker has failed {st['failures']} times in a row.\nLast error: {e}\nI'll keep trying and tell you when it's back.")
            return st
        if st["failures"] >= 5 and st["warned"]:
            self.warn_admin("✅ VOX checks are working again.")
        st["failures"], st["warned"] = 0, False

        today = today_cairo(self.now)
        configured = {w["id"] for w in self.config.get("watches", [])}
        for w in self.config.get("watches", []):
            if not w.get("active", True):
                continue
            if w.get("dates") and not w.get("weekdays") and all(d < today for d in w["dates"]):
                continue
            self.process_watch(w)
        for wid in [k for k in st["watches"] if k not in configured]:
            del st["watches"][wid]

        opened = {e["title"] for e in res["movie_events"]}
        de = res["date_event"]
        if de:
            self.add_change(type="dates", dates=de["dates"])
            if not all(m in opened for m in de["movies"]):
                self.broadcast("newWeek", lambda p: F.render_new_dates(de, p))
        for ev in res["movie_events"]:
            self.add_change(type="movie", title=ev["title"])
            self.broadcast("newMovies", lambda p, ev=ev: F.render_new_movie(ev, p))
        for ev in res["new_coming"]:
            self.add_change(type="coming", title=ev["title"])
            self.broadcast("comingSoon", lambda p, ev=ev: F.render_coming_soon(ev, p))
        self.maybe_daily_summary(today)
        return st

    def movies_doc(self):
        """What the control panel shows: every movie VOX lists now or soon."""
        out, seen = [], set()
        sess = self.snapshot["sessions"]
        showing = {}
        for lst in sess.values():
            for s in lst:
                showing.setdefault(s["slug"], {"title": s["title"], "cinemas": set(), "exps": set()})
                showing[s["slug"]]["cinemas"].add(s["cinema"])
                showing[s["slug"]]["exps"].add(s["exp"])
        for slug, v in sorted(showing.items(), key=lambda kv: kv[1]["title"].lower()):
            out.append({"slug": slug, "title": v["title"], "status": "now"})
            seen.add(slug)
        for m in self.state["comingSoon"]:
            if m["slug"] not in seen:
                out.append({"slug": m["slug"], "title": m["title"], "status": "soon", "release": m.get("release")})
        cinemas = sorted({c for v in showing.values() for c in v["cinemas"] if c})
        exps = sorted({e for v in showing.values() for e in v["exps"] if e})
        return {"movies": out, "cinemas": cinemas, "experiences": exps}
