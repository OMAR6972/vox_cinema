from datetime import datetime, timezone
from pathlib import Path

from alerts import formatter as F
from alerts.engine import Engine
from alerts.matcher import evaluate, sessions_for_watch
from alerts.vox import parse_coming_soon, parse_showtimes_page
from alerts.vtime import parse_hhmm, parse_vox_time, pretty_time, weekday_of

FIX = Path(__file__).parent / "fixtures_alerts"
html = lambda n: (FIX / n).read_text(encoding="utf-8")
NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)  # 12:00 Cairo


def sess(i, date, mins, slug="m", cinema="C", exp="Standard", available=True):
    return {"id": i, "slug": slug, "title": slug.title(), "date": date, "cinema": cinema, "exp": exp,
            "min": mins, "time": pretty_time(mins), "available": available, "url": f"https://x/{i}"}


def test_time_helpers():
    assert parse_vox_time("7:45pm") == 19 * 60 + 45
    assert parse_vox_time("12:30am") == 24 * 60 + 30
    assert parse_hhmm("00:15") == 24 * 60 + 15
    assert pretty_time(24 * 60 + 30) == "12:30 AM"
    assert weekday_of("2026-10-28") == 3  # Wednesday


def test_parse_fixtures():
    p = parse_showtimes_page(html("showtimes_20261028.html"))
    assert p.looks_right and p.sessions and "clayface" in p.movies
    assert p.dates == sorted(p.dates)
    assert parse_coming_soon(html("comingsoon.html"))[0]["slug"] == "the-mongoose"
    d = parse_showtimes_page(html("showtimes_20261217.html"))
    assert all(s["date"] == "2026-12-17" for s in d.sessions)



class FakeTG:
    def __init__(self):
        self.sent, self.queue, self.photos, self.username = [], [], [], "bot"

    def send(self, chat, text, buttons=None, silent=False):
        self.sent.append((chat, text, buttons))

    def send_photo(self, chat, path, caption_html=None, silent=True):
        self.photos.append((chat, str(path), caption_html or ""))

    def updates(self, offset):
        q, self.queue = self.queue, []
        return q

    def answer_callback(self, *a, **k):
        pass

class FakeVox:
    def __init__(self):
        self.days = {"2026-10-28": [sess("a", "2026-10-28", 19 * 60, slug="dune", cinema="Almaza")]}

    def __call__(self, path):
        if path.startswith("/movies/comingsoon"):
            return "<html></html>"
        d = path.split("d=")[1] if "d=" in path else "2026-10-28"
        d = d if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:]}"
        items = ""
        for s in self.days.get(d, []):
            items += (f'<article class="movie-compare" data-slug="{s["slug"]}"><div class="movie-hero"><h2>{s["title"]}</h2></div>'
                      f'<div class="dates"><h3>{s["cinema"]}</h3><ol class="showtimes"><li><strong>{s["exp"]}</strong><ol>'
                      f'<li data-id="{s["id"]}"><a class="showtime" href="/booking/{s["id"]}">{s["time"].replace(" ", "").lower()}</a></li>'
                      f'</ol></li></ol></div></article>')
        tabs = "".join(f'<li><a href="/showtimes?d={x.replace("-", "")}">{x}</a></li>' for x in sorted(self.days))
        return (f'<nav class="date-filter"><ol>{tabs}</ol></nav>'
                f'<form class="auto-submit"><input name="d" value="{d.replace("-", "")}"></form>{items}')




def make(cfg, state=None, vox=None, tg=None):
    return Engine(cfg, state or {}, tg or FakeTG(), vox or FakeVox(), owner_chat_id="111", now=NOW, sleep=lambda s: None)


def CFG():
    return {"settings": {"summaryTime": "23:59"},
            "people": [{"id": "me", "name": "Omar", "owner": True},
                       {"id": "f", "name": "Sara", "invite": "tok"}],
            "watches": [{"id": "w1", "slug": "dune", "title": "Dune", "recipients": ["me", "f"], "cinemas": ["Almaza"]}]}


def test_matcher_rules():
    w = {"slug": "m", "title": "M", "weekdays": [5], "times": ["20:00"], "flex": 30}  # Fridays, around 8 PM
    fri, sat = "2026-12-18", "2026-12-19"
    # first showtimes ever: always a message, even when the chosen day is not out
    p = evaluate(w, [sess("a", sat, 600)], opening=True)
    assert p["kind"] == "opening" and p["yourDay"] == []
    # later, new times on another day stay silent
    assert evaluate(w, [sess("a", sat, 600), sess("b", sat, 700)], seen={"a"}) is None
    # your day shows up: message with only the new times, closest to 8 PM marked
    p = evaluate(w, [sess("a", sat, 600), sess("b", fri, 20 * 60), sess("c", fri, 13 * 60)], seen={"a"})
    assert p["kind"] == "new" and [d["date"] for d in p["days"]] == [fri] and p["days"][0]["mode"] == "match"
    # extra time on a day that already had showtimes = "added", and only the new time is listed
    s = [sess("b", fri, 20 * 60), sess("c", fri, 13 * 60, exp="GOLD")]
    p = evaluate(w, s, seen={"b"})
    assert p["kind"] == "added" and p["newExps"] == ["GOLD"]
    assert [x["id"] for x in p["days"][0]["sessions"]] == ["c"]
    # nothing new: nothing sent
    assert evaluate(w, s, seen={"b", "c"}) is None
    # no day chosen: every new day counts
    assert evaluate({"slug": "m", "title": "M"}, [sess("a", sat, 600)], seen=set())["kind"] == "new"
    # nothing near your time on your day: closest first
    far = evaluate(w, [sess("z", fri, 13 * 60)], seen=set())
    assert far["days"][0]["mode"] == "closest"


def test_filters_and_dedupe():
    snap = {"sessions": {"2026-10-28": [sess("a", "2026-10-28", 600, exp="IMAX", available=False),
                                        sess("b", "2026-10-28", 600, exp="IMAX"),
                                        sess("c", "2026-10-28", 700, cinema="Other")]}}
    w = {"slug": "m", "cinemas": ["C"], "experiences": ["imax"]}
    out = sessions_for_watch(snap, w, "2026-10-28")
    assert [s["id"] for s in out] == ["b"]
    w2 = dict(w, includeSoldOut=True)
    assert [s["id"] for s in sessions_for_watch(snap, w2, "2026-10-28")] == ["b"]


def test_messages_text():
    w = {"slug": "m", "title": "M & Co", "weekdays": [3], "times": ["19:30"], "includeSoldOut": True}
    wed = "2026-10-28"
    s = [sess("a", wed, 19 * 60 + 30), sess("b", wed, 21 * 60, available=False)]
    txt = F.render_watch_message(evaluate(w, s, seen=set()))
    assert "NEW SHOWTIMES ON YOUR DAY" in txt and "M &amp; Co" in txt and "exactly your time" in txt
    assert "<s>9:00 PM</s>" in txt and "sold out" not in txt and "🆕" not in txt and "Why you got this" in txt
    # a time that cannot be booked has no link at all
    assert 'href="https://x/b"' not in txt and 'href="https://x/a"' in txt
    op = F.render_watch_message(evaluate(w, [sess("c", "2026-10-29", 600)], opening=True))
    assert "FIRST SHOWTIMES ARE OUT" in op and "is not out yet" in op and "stay silent" in op
    added = F.render_watch_message(evaluate(w, [sess("a", wed, 600), sess("n", wed, 700, exp="GOLD")], seen={"a"}))
    assert "NEW SHOWTIMES ADDED" in added and "GOLD showtimes" in added and "10:00 AM" not in added
    wait = F.render_watch_message(evaluate(w, [], intro=True))
    assert "NOW TRACKING" in wait and "No showtimes are out for it yet." in wait and "first showtimes" in wait


def test_migration_turns_news_into_roundup():
    from alerts.engine import migrate_config
    cfg = {"settings": {"newWindowHours": 6},
           "people": [{"id": "a", "alerts": {"newWeek": False, "newMovies": True, "comingSoon": False, "dailySummary": False}},
                      {"id": "b", "alerts": {"newWeek": False, "newMovies": False, "comingSoon": False, "dailySummary": False}}],
           "watches": [{"id": "w", "mode": "mine"}]}
    assert migrate_config(cfg)
    assert cfg["people"][0]["alerts"] == {"dailySummary": True} and cfg["people"][1]["alerts"] == {"dailySummary": False}
    assert "newWindowHours" not in cfg["settings"] and "mode" not in cfg["watches"][0]
    assert not migrate_config(cfg)


def test_first_run_is_silent_then_new_showtime_alerts():
    vox, tg = FakeVox(), FakeTG()
    st = make(CFG(), vox=vox, tg=tg).run()
    assert tg.sent == []
    vox.days["2026-10-28"].append(sess("b", "2026-10-28", 21 * 60, slug="dune", cinema="Almaza"))
    tg.queue = [{"update_id": 1, "message": {"text": "/start tok", "chat": {"id": 222}}}]
    st = make(CFG(), st, vox, tg).run()
    assert st["links"]["f"] == 222
    got = [(str(c), t, b) for c, t, b in tg.sent if "9:00 PM" in t]
    assert sorted(c for c, *_ in got) == ["111", "222"]
    assert [x["callback_data"] for x in got[0][2][0]] == ["booked:w1", "stop:w1"]
    assert "Why you got this" in got[0][1]


def test_stop_removes_person_from_the_movie_in_config_and_tells_owner_once():
    vox, tg = FakeVox(), FakeTG()
    cfg = CFG()
    st = make(cfg, vox=vox, tg=tg).run()
    tg.queue = [{"update_id": 1, "message": {"text": "/start tok", "chat": {"id": 222}}}]
    st = make(cfg, st, vox, tg).run()
    tg.sent.clear()
    tg.queue = [{"update_id": i, "callback_query": {"id": f"q{i}", "data": "stop:w1", "message": {"chat": {"id": 222}}}}
                for i in range(2, 6)]
    eng = make(cfg, st, vox, tg)
    eng.run()
    assert eng.config["watches"][0]["recipients"] == ["me"] and eng.config_dirty
    texts = [(str(c), t) for c, t, _ in tg.sent]
    assert len([1 for c, t in texts if c == "222" and "won't get any more alerts" in t]) == 1
    assert len([1 for c, t in texts if c == "111" and "Sara stopped tracking Dune" in t]) == 1
    # a new showtime now reaches only Omar
    tg.sent.clear()
    vox.days["2026-10-28"].append(sess("c", "2026-10-28", 22 * 60, slug="dune", cinema="Almaza"))
    make(eng.config, eng.state, vox, tg).run()
    assert [str(c) for c, t, _ in tg.sent if "10:00 PM" in t] == ["111"]


def test_new_tracking_says_hello_and_poster_comes_first(tmp_path):
    (tmp_path / "posters").mkdir()
    (tmp_path / "posters" / "dune.jpg").write_bytes(b"\xff\xd8\xff0")
    vox, tg = FakeVox(), FakeTG()
    cfg = CFG()
    st = make(cfg, vox=vox, tg=tg).run()
    cfg["watches"].append({"id": "w2", "slug": "dune", "title": "Dune", "intro": True, "recipients": ["me"]})
    Engine(cfg, st, tg, vox, owner_chat_id="111", now=NOW, sleep=lambda s: None, root=tmp_path).run()
    assert any("NOW TRACKING" in t for _, t, _ in tg.sent)
    assert tg.photos and tg.photos[0][1].endswith("dune.jpg") and "Dune" in tg.photos[0][2]


def test_roundup_sections_buttons_and_track_button():
    vox, tg = FakeVox(), FakeTG()
    cfg = CFG()
    st = make(cfg, vox=vox, tg=tg).run()          # first run only learns what is out
    assert tg.sent == []
    # a new week: one movie Omar tracks (dune) and a brand-new movie (odyssey)
    vox.days["2026-10-30"] = [sess("n1", "2026-10-30", 600, slug="odyssey", cinema="Almaza"),
                              sess("n2", "2026-10-30", 700, slug="dune", cinema="Almaza")]
    st = make(cfg, st, vox, tg).run()             # summary time not reached yet: nothing in the roundup
    assert not any("ROUNDUP" in t for _, t, _ in tg.sent)
    tg.sent.clear()
    cfg["settings"]["summaryTime"] = "12:00"      # it is 12:00 Cairo in this test
    eng = Engine(cfg, st, tg, vox, owner_chat_id="111", now=NOW, sleep=lambda s: None)
    eng.run()
    mine = [(t, b) for c, t, b in tg.sent if str(c) == "111"]
    allt = "\n".join(t for t, _ in mine)
    assert "VOX DAILY ROUNDUP" in allt and "Why you got this" in allt
    assert "NEW WEEK" in allt and "NEW MOVIES" in allt and "Odyssey" in allt and "you track it" in allt
    btns = [x for _, b in mine if b for row in b for x in row]
    assert any(x.get("callback_data") == "track:odyssey" for x in btns)
    assert not any(x.get("callback_data") == "track:dune" for x in btns)
    assert any(x.get("callback_data") == "roundup:off" for x in btns)
    assert eng.state["roundup"] == {"dates": [], "weekMovies": [], "newMovies": [], "coming": []}
    # Sara taps Track on the new movie, then turns the roundup off
    tg.sent.clear()
    tg.queue = [{"update_id": 9, "callback_query": {"id": "q", "data": "track:odyssey", "message": {"chat": {"id": 222}}}},
                {"update_id": 10, "callback_query": {"id": "q2", "data": "roundup:off", "message": {"chat": {"id": 222}}}}]
    eng.state["links"]["f"] = 222
    eng2 = Engine(eng.config, eng.state, tg, vox, owner_chat_id="111", now=NOW, sleep=lambda s: None)
    eng2.run()
    assert any(w["slug"] == "odyssey" and w["recipients"] == ["f"] for w in eng2.config["watches"])
    assert eng2.person("f")["alerts"]["dailySummary"] is False and eng2.config_dirty
    assert any("Daily roundup turned off" in t for _, t, _ in tg.sent)
    assert any("Sara is now tracking" in t for _, t, _ in tg.sent)
    assert any("NOW TRACKING" in t and "Odyssey" in t for c, t, _ in tg.sent if str(c) == "222")


def test_discord_gets_text_and_poster_link():
    vox, tg = FakeVox(), FakeTG()
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True},
           {"id": "d", "name": "Chan", "discord": True, "isGroup": True, "allWatches": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    st.setdefault("posters", {})["dune"] = "https://img/dune.jpg"
    vox.days["2026-10-28"].append(sess("b", "2026-10-28", 21 * 60, slug="dune", cinema="Almaza"))
    got = []
    import alerts.engine as eng
    eng.D.send = lambda url, html, **k: got.append((html, k))
    Engine(cfg, st, tg, vox, owner_chat_id="111", discord_url="https://hook", now=NOW, sleep=lambda s: None).run()
    assert any("9:00 PM" in h and k.get("image") == "https://img/dune.jpg" for h, k in got)


def test_send_current_times_button_resends_once():
    vox, tg = FakeVox(), FakeTG()
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    assert tg.sent == []
    cfg["watches"][0]["introNonce"] = "abc"
    st = make(cfg, st, vox, tg).run()
    assert any("NOW TRACKING" in t for _, t, _ in tg.sent)
    n = len(tg.sent)
    make(cfg, st, vox, tg).run()
    assert len(tg.sent) == n


def test_changing_cinema_does_not_flood_but_send_now_lists_everything():
    vox, tg = FakeVox(), FakeTG()
    vox.days["2026-10-28"].append(sess("x1", "2026-10-28", 20 * 60, slug="dune", cinema="Alex"))
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"], "cinemas": ["Almaza"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    cfg["watches"][0]["cinemas"] = ["Alex"]
    make(cfg, st, vox, tg).run()
    assert tg.sent == []
    cfg["watches"][0]["introNonce"] = "go"
    make(cfg, st, vox, tg).run()
    assert any("8:00 PM" in t for _, t, _ in tg.sent)


def test_first_showtimes_of_a_tracked_movie_always_message():
    vox, tg = FakeVox(), FakeTG()
    vox.days["2026-10-28"] = []
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"], "weekdays": [5]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    assert tg.sent == []
    vox.days["2026-10-28"] = [sess("a", "2026-10-28", 600, slug="dune", cinema="Almaza")]  # a Wednesday; you chose Fridays
    st = make(cfg, st, vox, tg).run()
    msg = [t for _, t, _ in tg.sent][0]
    assert "FIRST SHOWTIMES ARE OUT" in msg and "is not out yet" in msg
    tg.sent.clear()
    vox.days["2026-10-28"].append(sess("b", "2026-10-28", 700, slug="dune", cinema="Almaza"))
    make(cfg, st, vox, tg).run()
    assert tg.sent == []  # silent until your day (Friday) has showtimes



def test_discord_markdown_and_delivery():
    from alerts.discord import to_discord
    assert to_discord('<b>Hi</b> <a href="https://x/y">Book</a> &amp; <i>go</i>') == "**Hi** [Book](<https://x/y>) & _go_"
    sent = []
    cfg = {"settings": {}, "people": [{"id": "d", "name": "Chan", "isGroup": True, "discord": True}], "watches": []}
    e = Engine(cfg, {}, FakeTG(), FakeVox(), discord_url="https://hook", now=NOW, sleep=lambda s: None)
    import alerts.engine as eng
    eng.D.send = lambda url, html, **k: sent.append((url, html))
    assert e.deliver(cfg["people"][0], "<b>x</b>") and sent == [("https://hook", "<b>x</b>")]



def test_poster_copy(tmp_path):
    from alerts.posters import looks_like_image, sync_posters
    jpg = b"\xff\xd8\xff" + b"0" * 50
    assert looks_like_image(jpg) and not looks_like_image(b"<html>")
    movies = [{"slug": "dune", "poster": "https://x/p/a.jpg"}, {"slug": "bad", "poster": "https://x/p/b.png"}]
    def dl(u):
        if "b.png" in u:
            raise RuntimeError("403")
        return jpg
    assert sync_posters(tmp_path, movies, dl) == 1
    assert movies[0]["poster"] == "posters/dune.jpg" and (tmp_path / "posters/dune.jpg").exists()
    assert movies[1]["poster"].startswith("https://")


def test_old_posters_are_deleted(tmp_path):
    from alerts.posters import sync_posters
    (tmp_path / "posters").mkdir()
    (tmp_path / "posters/gone.jpg").write_bytes(b"x")
    (tmp_path / "posters/kept.jpg").write_bytes(b"x")
    (tmp_path / "posters/live.jpg").write_bytes(b"x")
    sync_posters(tmp_path, [{"slug": "live", "poster": "https://x/live.jpg"}], lambda u: b"", keep=["kept"])
    assert sorted(p.name for p in (tmp_path / "posters").iterdir()) == ["kept.jpg", "live.jpg"]


