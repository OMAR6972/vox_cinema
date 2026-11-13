"""One stateless run: read VOX, read replies, message the right people, save state."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import discord as D
from . import formatter as F
from . import scene
from .matcher import evaluate, sessions_for_watch
from .vox import (parse_coming_soon, parse_filters, parse_listing_titles, parse_posters, parse_showtimes_page,
                  showtimes_path)
from .vtime import add_days, now_cairo_hhmm, today_cairo

log = logging.getLogger("vox_alerts")
ROOT = Path(__file__).resolve().parent.parent

HELP = ("🎬 <b>VOX Alerts</b>\nI watch VOX Cinemas Egypt and message you the moment the movies you track "
        "get showtimes, plus one daily roundup of what's new on VOX.\n\n/status - what I'm tracking for you\n"
        "Use the buttons under a message to say you booked, to stop tracking a movie, or to track a new one.")
LEGACY_NEWS = ("newWeek", "newMovies", "comingSoon")


def migrate_config(config) -> bool:
    """Old configs had four news switches. News now lives in the daily roundup only, so anyone who had
    any news switch on gets the roundup. Returns True when something changed."""
    changed = False
    s = config.setdefault("settings", {})
    if "newWindowHours" in s:
        del s["newWindowHours"]
        changed = True
    for p in config.get("people", []):
        a = p.get("alerts")
        if a is not None and any(k in a for k in LEGACY_NEWS):
            on = a.get("dailySummary", True) or any(a.get(k) for k in LEGACY_NEWS)
            p["alerts"] = {"dailySummary": bool(on)}
            changed = True
    for w in config.get("watches", []):
        if "mode" in w:
            del w["mode"]
            changed = True
    return changed


def roundup_on(p) -> bool:
    return bool((p.get("alerts") or {}).get("dailySummary", True))


def empty_roundup():
    return {"dates": [], "weekMovies": [], "newMovies": [], "coming": []}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def default_state():
    return {"baselineDone": False, "dates": [], "knownMovies": {}, "comingSoon": [], "watches": {},
            "links": {}, "roundup": empty_roundup(), "summary": {"lastSentDate": None}, "offset": 0,
            "failures": 0, "warned": False, "lastCheck": None, "lastError": None, "lastRun": None}


class Engine:
    def __init__(self, config, state, telegram, fetch, owner_chat_id=None, discord_url=None, now=None,
                 sleep=time.sleep, delay=0.4, root=None, scene_fetch=None):
        self.config = config
        self.config_dirty = migrate_config(config)  # True when the bot must save config.json
        self.state = {**default_state(), **state}
        self.tg = telegram
        self.fetch = fetch           # fetch(path) -> html
        self.scene_fetch = scene_fetch   # fetch(url, xhr=False) -> html; None switches Scene off
        self.scene_done = None       # slugs whose Scene showtimes were read this run (None = Scene not read at all)
        self.owner_chat_id = owner_chat_id
        self.discord_url = discord_url
        self.now = now or datetime.now(timezone.utc)
        self.sleep = sleep
        self.delay = delay
        self.root = Path(root) if root else ROOT
        self.sent = 0
        for stale in ("firstSeen", "changes"):  # old "is it 🆕" memory, no longer used
            self.state.pop(stale, None)
        self._notified = set()       # (person, movie) already messaged in this run

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

    def poster_path(self, slug):
        for f in sorted((self.root / "posters").glob(f"{slug}.*")):
            if f.is_file():
                return f
        return None

    def deliver(self, p, html, buttons=None, photo=None, discord_html=None, silent=False):
        """photo = (slug, title): the poster is sent first, silently, then the message itself."""
        if p.get("active") is False:
            return False
        ok = False
        cid = self.chat_id(p)
        if cid:
            try:
                path = self.poster_path(photo[0]) if photo else None
                if path and hasattr(self.tg, "send_photo"):
                    try:
                        self.tg.send_photo(cid, path, F.b(photo[1]))
                    except Exception as e:  # a missing poster must never block the message
                        log.info("poster not sent: %s", e)
                self.tg.send(cid, html, buttons, **({"silent": True} if silent else {}))
                ok = True
            except Exception as e:  # one person's failure must not block the others
                log.warning("could not message %s on Telegram: %s", p.get("name"), e)
        if p.get("discord") and self.discord_url:
            text = discord_html or html
            image = (self.state.get("posters") or {}).get(photo[0]) if photo else None
            try:
                try:
                    D.send(self.discord_url, text, **({"image": image} if image else {}))
                except RuntimeError:
                    if not image:
                        raise
                    D.send(self.discord_url, text)
                ok = True
            except Exception as e:
                log.warning("could not post to Discord: %s", e)
        self.sent += ok
        return ok

    def tell_admin(self, actor, text):
        """Tell Omar when a friend changes something with a button (not when he does it himself)."""
        if actor.get("owner"):
            return
        admin = (self.config.get("settings") or {}).get("adminPersonId")
        p = self.person(admin) or next((x for x in self.people if x.get("owner")), None)
        if p:
            self.deliver(p, F.esc(text))

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

    def _watch(self, wid):
        return next((x for x in self.config.get("watches", []) if x["id"] == wid), None)

    def title_for(self, slug):
        for lst in getattr(self, "snapshot", {"sessions": {}})["sessions"].values():
            for s in lst:
                if s["slug"] == slug:
                    return s["title"]
        for m in self.state.get("comingSoon", []) + self.state.get("nowShowing", []):
            if m["slug"] == slug:
                return m["title"]
        sc = (self.state.get("scene", {}).get("listing") or {}).get(slug)
        if sc and sc.get("title"):
            return sc["title"]
        return (self.state["knownMovies"].get(slug) or {}).get("title") or slug.replace("-", " ").title()

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
                    self.tg.send(chat["id"], f"✅ Hi {F.esc(name)}, you're connected! I'll message you here when the movies you track drop.\n\n/status to see what I'm tracking.")
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
            action, _, arg = (q.get("data") or "").partition(":")
            chat_id = q["message"]["chat"]["id"]
            p = self._by_chat(chat_id)
            if p and action in ("booked", "stop"):
                self._stop(p, arg, action, chat_id, q["id"])
            elif p and action == "track":
                self._track(p, arg, chat_id, q["id"])
            elif p and action == "roundup" and arg == "off":
                self._roundup_off(p, chat_id, q["id"])
            else:
                self.tg.answer_callback(q["id"], "Done")

    def _stop(self, p, wid, action, chat_id, qid):
        w = self._watch(wid)
        if not w:
            return self.tg.answer_callback(qid, "Done")
        ws = self.state["watches"].setdefault(wid, {})
        stopped = ws.setdefault("stopped", [])
        already = p["id"] in stopped
        if not already:
            stopped.append(p["id"])
            if p["id"] in (w.get("recipients") or []):
                w["recipients"] = [r for r in w["recipients"] if r != p["id"]]  # the panel shows them unticked
                self.config_dirty = True
            else:  # reached it through "all watches": mute them for this movie in the bot's own memory
                ws.setdefault("booked", [])
                if p["id"] not in ws["booked"]:
                    ws["booked"].append(p["id"])
            self.tell_admin(p, f"ℹ️ {p.get('name')} {'booked' if action == 'booked' else 'stopped tracking'} {w['title']}.")
        self.tg.answer_callback(qid, "Done")
        if not already:  # tapped several times while waiting: answer once
            self.tg.send(chat_id, F.esc(F.booked_text(w["title"]) if action == "booked" else F.stopped_text(w["title"])))

    def _track(self, p, slug, chat_id, qid):
        watches = self.config.setdefault("watches", [])
        if any(w["slug"] == slug and w.get("active", True) and p["id"] in (w.get("recipients") or []) for w in watches):
            return self.tg.answer_callback(qid, "You already track it")
        title = self.title_for(slug)
        wid = f"{slug}-{p['id'][:6]}"
        w = next((x for x in watches if x["id"] == wid), None)
        nonce = hashlib.sha1(f"{wid}{self.now.isoformat()}".encode()).hexdigest()[:12]
        if w:  # tracked before, stopped, and now wants it back: say hello again
            w["active"] = True
            w["recipients"] = list(dict.fromkeys((w.get("recipients") or []) + [p["id"]]))
            w["introNonce"] = nonce
        else:
            watches.append({"id": wid, "slug": slug, "title": title, "recipients": [p["id"]], "cinemas": [], "dates": [],
                            "weekdays": [], "times": [], "flex": 30, "experiences": [], "includeSoldOut": False,
                            "active": True, "intro": True})
        self.state["watches"].setdefault(wid, {}).pop("stopped", None)
        self.config_dirty = True
        self.tell_admin(p, f"ℹ️ {p.get('name')} is now tracking {title}.")
        self.tg.answer_callback(qid, "Tracking ✅")

    def _roundup_off(self, p, chat_id, qid):
        was_on = roundup_on(p)
        if was_on:
            p["alerts"] = {**(p.get("alerts") or {}), "dailySummary": False}
            self.config_dirty = True
            self.tell_admin(p, f"ℹ️ {p.get('name')} turned off Daily roundup.")
        self.tg.answer_callback(qid, "Done")
        if was_on:
            self.tg.send(chat_id, F.esc(F.ROUNDUP_OFF_TEXT))

    def status_text(self, p):
        mine = [w for w in self.config.get("watches", []) if w.get("active", True) and p["id"] in w.get("recipients", [])
                and p["id"] not in self.state["watches"].get(w["id"], {}).get("booked", [])]
        lines = ["👀 <b>I'm tracking for you:</b>\n" + "\n".join(f"• {F.b(w['title'])}\n  {F.i(F.describe_watch(w))}" for w in mine)] if mine \
            else ["I'm not tracking any movie for you right now."]
        lines.append("☀️ Daily roundup: " + ("on" if roundup_on(p) else "off"))
        return "\n\n".join(lines)

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
        posters = st.setdefault("posters", {})
        try:
            soon_html = self.fetch("/movies/comingsoon")
            posters.update(parse_posters(soon_html))
            lst = parse_coming_soon(soon_html)
            if lst:
                known = {m["slug"] for m in st["comingSoon"]}
                if st["comingSoon"]:
                    new_coming = [m for m in lst if m["slug"] not in known]
                st["comingSoon"] = lst
        except Exception as e:
            log.info("coming soon skipped: %s", e)

        # posters + titles for movies now showing (one extra page, only when a poster is missing)
        try:
            if any(sl not in posters for sl in movies):
                now_html = self.fetch("/movies/whatson")
                posters.update(parse_posters(now_html))
                st["nowShowing"] = parse_listing_titles(now_html)
        except Exception as e:
            log.info("posters skipped: %s", e)
        prev_dates = set(st["dates"])
        new_dates = [] if first_run else [d for d in home.dates if d >= today and d not in prev_dates]
        new_slugs = []
        for lst in sessions.values():
            for s in lst:
                if s["slug"] not in st["knownMovies"]:
                    st["knownMovies"][s["slug"]] = {"firstSeen": self.now.isoformat(timespec="seconds"), "title": s["title"]}
                    if not first_run:
                        new_slugs.append(s["slug"])
        st.setdefault("trackingSince", self.now.isoformat(timespec="seconds"))
        st["dates"] = [d for d in home.dates if d >= today]
        st["baselineDone"] = True
        st["lastCheck"] = self.now.isoformat(timespec="seconds")
        st["lastError"] = None
        new_scene = self.crawl_scene(sessions, first_run, today)
        self.snapshot = {"sessions": sessions, "movies": movies}

        # what the next daily roundup will say (only after the very first run, which just learns what is out)
        if first_run:
            return
        rd = st.setdefault("roundup", empty_roundup())
        allv = [s for lst in sessions.values() for s in lst if not s["id"].startswith("scene:")]  # the new week is VOX only
        if new_dates:
            rd["dates"] = sorted(set(rd["dates"]) | set(new_dates))
            have = {m["slug"] for m in rd["weekMovies"]}
            for s in allv:
                if s["date"] in new_dates and s["slug"] not in have:
                    rd["weekMovies"].append({"slug": s["slug"], "title": s["title"]})
                    have.add(s["slug"])
            rd["weekMovies"].sort(key=lambda m: m["title"].lower())
        have = {m["slug"] for m in rd["newMovies"]}
        for slug in dict.fromkeys(new_slugs):
            if slug in have:
                continue
            ss = sorted((s for s in allv if s["slug"] == slug), key=lambda s: (s["date"], s["min"]))
            rd["newMovies"].append({"slug": slug, "title": ss[0]["title"], "firstDate": ss[0]["date"],
                                    "experiences": list(dict.fromkeys(s["exp"] for s in ss))})
        for m in new_scene:
            if m["slug"] not in have:
                rd["newMovies"].append(m)
                have.add(m["slug"])
        have = {m["slug"] for m in rd["coming"]}
        for m in new_coming:
            if m["slug"] not in have:
                rd["coming"].append({"slug": m["slug"], "title": m["title"], "release": m.get("release")})

    # ---- Scene Cinemas (Cairo Festival City, District 5) ----
    def crawl_scene(self, sessions, first_run, today):
        """Reads each Scene branch's movie list, plus showtimes for the movies somebody tracks. Adds them to `sessions`.
        Returns the Scene movies that are brand new (for the daily roundup). Never raises: if a branch is down,
        VOX alerts still go out and nothing from that branch is marked as read."""
        self.scene_ok, self.scene_done = set(), set()
        F.OFF_VOX.clear()
        if not self.scene_fetch:
            return []
        st = self.state
        sc = st.setdefault("scene", {"baseline": False, "known": [], "listing": {}})
        raw_lists, errors = {}, []
        for br in scene.BRANCHES:
            try:
                lst = scene.parse_listing(self.scene_fetch(scene.home_url(br), False))
                if not lst:
                    raise RuntimeError("its home page lists no movies. The page layout may have changed.")
                raw_lists[br] = lst
                self.scene_ok.add(br)
            except Exception as e:
                log.warning("Scene %s skipped: %s", br, e)
                errors.append(f"{br}: {e}")
        if errors:
            st["sceneError"] = f"{self.now.isoformat(timespec='seconds')} - " + "; ".join(errors)
        else:
            st.pop("sceneError", None)
        if not raw_lists:
            self._keep_scene_listing()
            return []
        by_norm, by_title = {}, {}
        for lst in sessions.values():
            for s in lst:
                by_norm[scene.norm(s["slug"])] = s["slug"]
                by_title[scene.norm(s["title"])] = s["slug"]
        for slug, m in list(st["knownMovies"].items()) + [(m["slug"], m) for m in st["comingSoon"] + st.get("nowShowing", [])]:
            by_norm.setdefault(scene.norm(slug), slug)
            if (m or {}).get("title"):
                by_title.setdefault(scene.norm(m["title"]), slug)
        canon = {}
        for br, lst in raw_lists.items():
            for raw, title in lst:
                slug = by_norm.get(scene.norm(raw)) or by_title.get(scene.norm(title)) or scene.norm(raw)
                c = canon.setdefault(slug, {"title": title, "raws": {}})
                c["raws"].setdefault(br, raw)
        for br in scene.BRANCHES:       # a branch we could not read keeps what we knew about it
            if br not in raw_lists:
                for slug, m in (sc.get("listing") or {}).items():
                    if br in (m.get("raws") or {}):
                        c = canon.setdefault(slug, {"title": m["title"], "raws": {}})
                        c["raws"].setdefault(br, m["raws"][br])
        sc["listing"] = canon
        self._set_off_vox(sessions)
        # showtimes only for the movies somebody tracks
        wanted = {w["slug"] for w in self.config.get("watches", []) if w.get("active", True)}
        for slug in wanted & set(canon):
            for br, raw in canon[slug]["raws"].items():
                if br not in self.scene_ok:
                    continue
                try:
                    self.sleep(self.delay)
                    title, dates = scene.parse_movie_page(self.scene_fetch(scene.public_movie_url(br, raw), False), raw)
                    for d in dates:
                        if d < today:
                            continue
                        self.sleep(self.delay)
                        sessions.setdefault(d, []).extend(scene.parse_fragment(
                            self.scene_fetch(scene.movie_url(br, raw, d), True), d, slug, canon[slug]["title"] or title, raw, br))
                    self.scene_done.add((slug, br))
                except Exception as e:
                    log.warning("Scene %s showtimes for %s skipped: %s", br, slug, e)
        # brand-new movies for the roundup (a movie is new when no site we read had it before)
        known = set(sc["known"])
        fresh = []
        if sc["baseline"] and not first_run:
            for slug, m in canon.items():
                if slug in known or slug in st["knownMovies"] or slug in {x["slug"] for x in st["comingSoon"]}:
                    continue
                fresh.append({"slug": slug, "title": m["title"], "firstDate": None, "experiences": [],
                              "source": " & ".join(scene.BRANCHES[b]["name"] for b in scene.BRANCHES if b in m["raws"])})
        for m in fresh:
            st["knownMovies"].setdefault(m["slug"], {"firstSeen": self.now.isoformat(timespec="seconds"),
                                                     "title": m["title"], "source": "scene"})
        sc["known"] = sorted(known | set(canon))
        sc["baseline"] = True
        return fresh

    def _set_off_vox(self, sessions=None):
        """Movies VOX does not list get a Scene page link and site name in messages."""
        if sessions is not None:
            vox_now = {s["slug"] for lst in sessions.values() for s in lst if not s["id"].startswith("scene:")}
        else:
            vox_now = set()
        vox_now |= {m["slug"] for m in self.state.get("nowShowing", []) + self.state["comingSoon"]}
        for slug, m in (self.state.get("scene", {}).get("listing") or {}).items():
            if slug not in vox_now and m.get("raws"):
                br = next(iter(m["raws"]))
                names = " & ".join(scene.BRANCHES[b]["name"] for b in m["raws"])
                F.OFF_VOX[slug] = (names, scene.public_movie_url(br, m["raws"][br]))

    def _keep_scene_listing(self):
        """No Scene branch could be read: keep showing its movies as they were last time."""
        self._set_off_vox()

    def scene_ready(self, slug, branch):
        """True when this run read everything branch `branch` has for the movie (or the branch doesn't have it)."""
        if branch not in self.scene_ok:
            return False
        m = (self.state.get("scene", {}).get("listing") or {}).get(slug)
        return (slug, branch) in self.scene_done or not m or branch not in m["raws"]

    # ---- tracked movies ----
    def process_watch(self, w):
        st = self.state["watches"].setdefault(w["id"], {})
        today = today_cairo(self.now)
        sessions = sessions_for_watch(self.snapshot, w, today)
        if st.get("stopped"):
            st["stopped"] = [x for x in st["stopped"] if x not in (w.get("recipients") or [])]  # re-added in the panel
        booked = st.setdefault("booked", [])
        first_look = "seen" not in st
        nonce = w.get("introNonce")
        resend = bool(nonce and st.get("introNonce") != nonce)  # "send current times now" pressed in the panel
        intro = bool((first_look and w.get("intro")) or resend)
        if resend:
            st["introNonce"] = nonce
        fkey = json.dumps([sorted(w.get("cinemas") or []), sorted(w.get("experiences") or []), bool(w.get("includeSoldOut"))])
        seeded = st.get("sceneSeeded")
        seeded = ["cfc"] if seeded is True else list(seeded or [])
        ready = [br for br in scene.BRANCHES if self.scene_ready(w["slug"], br)]
        if first_look and not intro:
            st["filters"] = fkey
            st["seen"] = {s["id"]: s["date"] for s in sessions}
            st["everHadSessions"] = bool(sessions)
            st["sceneSeeded"] = ready
            return None
        st.setdefault("seen", {})
        fresh_branches = [br for br in ready if br not in seeded]
        if fresh_branches:
            # a Scene branch is new for this watch: what it already shows is not "new", record it silently once
            st["sceneSeeded"] = sorted(set(seeded) | set(fresh_branches))
            if not first_look:
                for sx in sessions:
                    if any(sx["id"].startswith(f"scene:{br}:") for br in fresh_branches):
                        st["seen"][sx["id"]] = sx["date"]
        # Changing the cinema, screen types or sold-out switch changes WHICH showings count. Everything that now
        # matches would look "new", so record it silently instead (the Send now button still lists it all).
        if st.get("filters") is None:
            st["filters"] = fkey
        elif st["filters"] != fkey:
            st["filters"] = fkey
            if not intro:
                for sx in sessions:
                    st["seen"][sx["id"]] = sx["date"]
                st["everHadSessions"] = bool(sessions) or st.get("everHadSessions", False)
                return None
        opening = (not intro) and st.get("everHadSessions", True) is False and bool(sessions)
        plan = evaluate(w, sessions, seen=set(st["seen"]), intro=intro, opening=opening)
        if plan:
            text = F.render_watch_message(plan)
            stop = {"text": f"🔕 Stop tracking {w['title']}", "callback_data": f"stop:{w['id']}"}
            buttons = [[stop]] if intro else [[{"text": "✅ I booked it", "callback_data": f"booked:{w['id']}"}, stop]]
            everyone = [x["id"] for x in self.people if x.get("allWatches")]
            for pid in list(dict.fromkeys(list(w.get("recipients", [])) + everyone)):
                p = self.person(pid)
                if not p or (pid in booked and not intro) or (pid, w["slug"]) in self._notified:
                    continue
                self._notified.add((pid, w["slug"]))
                self.deliver(p, text, buttons, photo=(w["slug"], w["title"]))
        for s in sessions:
            st["seen"][s["id"]] = s["date"]
        if first_look:
            st["sceneSeeded"] = ready
        st["seen"] = {k: v for k, v in st["seen"].items() if v >= today}
        if sessions:
            st["everHadSessions"] = True
        return plan

    # ---- the daily roundup ----
    def maybe_daily_roundup(self, today):
        s = self.state["summary"]
        at = (self.config.get("settings") or {}).get("summaryTime", "18:00")
        if s.get("lastSentDate") == today or now_cairo_hhmm(self.now) < at:
            return
        s["lastSentDate"] = today
        rd, self.state["roundup"] = self.state.get("roundup") or empty_roundup(), empty_roundup()
        if not (rd["dates"] or rd["newMovies"] or rd["coming"]):
            return
        dates = set(self.state["dates"])
        through, d = None, today
        while d in dates:
            through, d = d, add_days(d, 1)
        for p in self.people:
            if p.get("active") is False or not roundup_on(p):
                continue
            tracked = {w["slug"] for w in self.config.get("watches", []) if w.get("active", True)
                       and (p["id"] in (w.get("recipients") or []) or p.get("allWatches"))}
            tg_blocks = F.render_roundup(rd, p, tracked, today, through)
            dc_blocks = F.render_roundup(rd, p, tracked, today, through, links=True)
            for n, (blk, dc) in enumerate(zip(tg_blocks, dc_blocks)):
                self.deliver(p, blk["html"], blk["buttons"], discord_html=dc["html"], silent=n > 0)

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
            self.crawl()
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
        for w in list(self.config.get("watches", [])):
            if not w.get("active", True):
                continue
            if w.get("dates") and not w.get("weekdays") and all(d < today for d in w["dates"]):
                continue
            self.process_watch(w)
        for wid in [k for k in st["watches"] if k not in configured]:
            del st["watches"][wid]
        self.maybe_daily_roundup(today)
        return st

    def movies_doc(self):
        """What the control panel shows: every movie VOX or Scene lists now or soon, and where each one was found."""
        out, seen = [], set()
        sess = self.snapshot["sessions"]
        showing = {}
        for lst in sess.values():
            for s in lst:
                if s["id"].startswith("scene:"):
                    continue
                showing.setdefault(s["slug"], {"title": s["title"], "cinemas": set(), "exps": set()})
                showing[s["slug"]]["cinemas"].add(s["cinema"])
                showing[s["slug"]]["exps"].add(s["exp"])
        posters = self.state.get("posters", {})
        listing = (self.state.get("scene", {}).get("listing") or {})
        on_vox = set(showing) | {m["slug"] for m in self.state.get("nowShowing", [])} | {m["slug"] for m in self.state["comingSoon"]}
        known = self.state["knownMovies"]

        def entry(slug, title, status, **extra):
            sources = (["vox"] if slug in on_vox else []) + (["scene"] if slug in listing else [])
            e = {"slug": slug, "title": title, "status": status, "poster": posters.get(slug), "sources": sources}
            if slug in listing:
                e["sceneCinemas"] = [scene.BRANCHES[b]["name"] for b in listing[slug]["raws"]]
            if slug not in on_vox and (known.get(slug) or {}).get("source", "vox") == "vox" and slug in known:
                e["goneFrom"] = ["vox"]   # VOX used to list it and no longer does
            e.update(extra)
            return e

        for slug, v in sorted(showing.items(), key=lambda kv: kv[1]["title"].lower()):
            out.append(entry(slug, v["title"], "now"))
            seen.add(slug)
        for m in self.state.get("nowShowing", []):
            if m["slug"] not in seen:
                out.append(entry(m["slug"], m["title"], "now"))
                seen.add(m["slug"])
        for m in self.state["comingSoon"]:
            if m["slug"] not in seen:
                out.append(entry(m["slug"], m["title"], "soon", release=m.get("release")))
                seen.add(m["slug"])
        for slug, m in sorted(listing.items(), key=lambda kv: kv[1]["title"].lower()):
            if slug not in seen:
                out.append(entry(slug, m["title"], "now"))
                seen.add(slug)
        cinemas = sorted({c for v in showing.values() for c in v["cinemas"] if c} | ({scene.BRANCHES[b]["name"] for m in listing.values() for b in m["raws"]}))
        exps = sorted({e for v in showing.values() for e in v["exps"] if e})
        return {"movies": out, "cinemas": cinemas, "experiences": exps}
