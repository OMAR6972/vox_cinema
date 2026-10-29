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


def test_ladder_exact_then_closest_then_notyet():
    w = {"slug": "m", "title": "M", "weekdays": [3], "times": ["19:30"], "flex": 30}
    exact = [sess("a", "2026-10-28", 19 * 60 + 30), sess("b", "2026-10-28", 22 * 60)]
    plan = evaluate(w, exact)
    assert plan["kind"] == "yourday" and plan["days"][0]["mode"] == "match"
    far = [sess("c", "2026-10-28", 14 * 60)]
    assert evaluate(w, far)["days"][0]["mode"] == "closest"
    other_day = [sess("d", "2026-10-29", 19 * 60 + 30)]
    assert evaluate(w, other_day)["kind"] == "notyet"
    assert evaluate(dict(w, mode="mine"), other_day)["kind"] == "silent"
    assert evaluate(w, exact, seen={"a", "b"}) is None


def test_filters_and_dedupe():
    snap = {"sessions": {"2026-10-28": [sess("a", "2026-10-28", 600, exp="IMAX", available=False),
                                        sess("b", "2026-10-28", 600, exp="IMAX"),
                                        sess("c", "2026-10-28", 700, cinema="Other")]}}
    w = {"slug": "m", "cinemas": ["C"], "experiences": ["imax"]}
    out = sessions_for_watch(snap, w, "2026-10-28")
    assert [s["id"] for s in out] == ["b"]
    w2 = dict(w, includeSoldOut=True)
    assert [s["id"] for s in sessions_for_watch(snap, w2, "2026-10-28")] == ["b"]


def test_messages_are_valid_html_ish():
    w = {"slug": "m", "title": "M & Co", "weekdays": [3], "times": ["19:30"]}
    plan = evaluate(w, [sess("a", "2026-10-28", 19 * 60 + 30)], intro=True)
    txt = F.render_watch_message(plan, {"name": "Omar Samir"})
    assert "Hey Omar!" in txt and "M &amp; Co" in txt and "exactly your time" in txt


class FakeTG:
    def __init__(self):
        self.sent, self.queue, self.username = [], [], "bot"

    def send(self, chat, text, buttons=None):
        self.sent.append((chat, text, buttons))

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


CFG = {"settings": {"summaryTime": "23:00"},
       "people": [{"id": "me", "name": "Omar", "owner": True, "alerts": {"newWeek": True, "newMovies": True}},
                  {"id": "f", "name": "Sara", "invite": "tok", "alerts": {}}],
       "watches": [{"id": "w1", "slug": "dune", "title": "Dune", "recipients": ["me", "f"], "cinemas": ["Almaza"]}]}


def test_first_run_is_silent_then_new_showtime_alerts_and_booked_stops():
    vox, tg = FakeVox(), FakeTG()
    st = make(CFG, vox=vox, tg=tg).run()
    assert tg.sent == []
    vox.days["2026-10-28"].append(sess("b", "2026-10-28", 21 * 60, slug="dune", cinema="Almaza"))
    tg.queue = [{"update_id": 1, "message": {"text": "/start tok", "chat": {"id": 222}}}]
    eng = make(CFG, st, vox, tg)
    st = eng.run()
    chats = [c for c, *_ in tg.sent]
    assert "111" in map(str, chats) and st["links"]["f"] == 222
    assert any("9:00 PM" in t for _, t, _ in tg.sent)
    tg.sent.clear()
    tg.queue = [{"update_id": 2, "callback_query": {"id": "q", "data": "booked:w1", "message": {"chat": {"id": 111}}}}]
    vox.days["2026-10-28"].append(sess("c", "2026-10-28", 22 * 60, slug="dune", cinema="Almaza"))
    st = make(CFG, st, vox, tg).run()
    assert st["watches"]["w1"]["booked"] == ["me"]
    assert [str(c) for c, t, _ in tg.sent if "10:00 PM" in t] == ["222"]


def test_new_watch_with_intro_and_new_dates_event():
    vox, tg = FakeVox(), FakeTG()
    st = make(CFG, vox=vox, tg=tg).run()
    cfg = dict(CFG, watches=CFG["watches"] + [{"id": "w2", "slug": "dune", "title": "Dune", "intro": True, "recipients": ["me"]}])
    vox.days["2026-10-30"] = [sess("n", "2026-10-30", 600, slug="dune", cinema="Almaza")]
    make(cfg, st, vox, tg).run()
    texts = [t for _, t, _ in tg.sent]
    assert any("I'll be watching" in t for t in texts)
    assert any("new dates" in t for t in texts)


def test_discord_markdown_and_delivery():
    from alerts.discord import to_discord
    assert to_discord('<b>Hi</b> <a href="https://x/y">Book</a> &amp; <i>go</i>') == "**Hi** [Book](<https://x/y>) & _go_"
    sent = []
    cfg = {"settings": {}, "people": [{"id": "d", "name": "Chan", "isGroup": True, "discord": True}], "watches": []}
    e = Engine(cfg, {}, FakeTG(), FakeVox(), discord_url="https://hook", now=NOW, sleep=lambda s: None)
    import alerts.engine as eng
    eng.D.send = lambda url, html, **k: sent.append((url, html))
    assert e.deliver(cfg["people"][0], "<b>x</b>") and sent == [("https://hook", "<b>x</b>")]


def test_all_watches_person_gets_every_movie():
    vox, tg = FakeVox(), FakeTG()
    cfg = {"settings": {}, "people": [{"id": "me", "name": "Omar", "owner": True},
                                      {"id": "d", "name": "Chan", "discord": True, "isGroup": True, "allWatches": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    vox.days["2026-10-28"].append(sess("b", "2026-10-28", 21 * 60, slug="dune", cinema="Almaza"))
    e = Engine(cfg, st, tg, vox, owner_chat_id="111", discord_url="https://hook", now=NOW, sleep=lambda s: None)
    got = []
    import alerts.engine as eng
    eng.D.send = lambda url, html, **k: got.append(html)
    e.run()
    assert any("9:00 PM" in h for h in got)


def test_send_current_times_button_resends_once():
    vox, tg = FakeVox(), FakeTG()
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    assert tg.sent == []
    cfg["watches"][0]["introNonce"] = "abc"
    st = make(cfg, st, vox, tg).run()
    assert any("I'll be watching" in t for _, t, _ in tg.sent)
    n = len(tg.sent)
    make(cfg, st, vox, tg).run()
    assert len(tg.sent) == n


def test_first_seen_marks_only_recent_and_labels_old():
    vox, tg = FakeVox(), FakeTG()
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    assert st["firstSeen"]["a"][1] == 0 and "trackingSince" in st
    vox.days["2026-10-28"].append(sess("b", "2026-10-28", 21 * 60, slug="dune", cinema="Almaza"))
    from datetime import timedelta
    st = Engine(cfg, st, tg, vox, owner_chat_id="111", now=NOW + timedelta(hours=1), sleep=lambda s: None).run()
    assert st["firstSeen"]["b"][1] == int((NOW + timedelta(hours=1)).timestamp())
    msg = [t for _, t, _ in tg.sent if "times just dropped" in t][0]
    assert "🆕" in msg and "9:00 PM" in msg and "7:00 PM" not in msg and "other times were already out" in msg
    # 8 hours later, with an intro resend: nothing is "new" any more, and the ages are spelled out
    cfg["watches"][0]["introNonce"] = "x"
    tg.sent.clear()
    Engine(cfg, st, tg, vox, owner_chat_id="111", now=NOW + timedelta(hours=9), sleep=lambda s: None).run()
    msg = [t for _, t, _ in tg.sent if "I'll be watching" in t][0]
    assert "🆕" not in msg and "first seen 8 h ago" in msg and "already out when I started tracking" in msg


def test_posters_and_single_stop_reply():
    from alerts.vox import parse_posters
    html_ = (Path(__file__).parent / "fixtures" / "vox_comingsoon.html").read_text(encoding="utf-8")
    pos = parse_posters(html_)
    assert pos["sense-and-sensibility"].startswith("https://assets.voxcinemas.com/posters/")
    vox, tg = FakeVox(), FakeTG()
    cfg = {"settings": {"summaryTime": "23:59"}, "people": [{"id": "me", "name": "Omar", "owner": True}],
           "watches": [{"id": "w", "slug": "dune", "title": "Dune", "recipients": ["me"]}]}
    st = make(cfg, vox=vox, tg=tg).run()
    tg.sent.clear()
    tg.queue = [{"update_id": i, "callback_query": {"id": f"q{i}", "data": "stop:w", "message": {"chat": {"id": 111}}}} for i in range(1, 5)]
    make(cfg, st, vox, tg).run()
    assert len([t for _, t, _ in tg.sent if "no more alerts" in t]) == 1
