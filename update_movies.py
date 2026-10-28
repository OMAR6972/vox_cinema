"""Refresh movies.json (the list the control panel shows) from VOX Cinemas Egypt.

Run by the "Update Movies" workflow. Writes:
  movies.json  every movie VOX lists now or soon, flagged when it has showtimes at Almaza.
Optionally, when panel.json has "autoAddNew": true, adds a watch to watches.json for every
movie that appeared since the last run, so new releases are covered without any clicking.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from check_showtimes import VOX_HEADERS, fetch_page, parse_listing_vox

ROOT = Path(__file__).parent
BASE = "https://egy.voxcinemas.com"
CINEMA = "city-centre-almaza"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def collect(fetch=fetch_page) -> "list[dict]":
    now = dict(parse_listing_vox(fetch(BASE + "/movies/whatson", VOX_HEADERS)))
    soon = dict(parse_listing_vox(fetch(BASE + "/movies/comingsoon", VOX_HEADERS)))
    almaza = {slug for slug, _ in parse_listing_vox(fetch(f"{BASE}/showtimes?c={CINEMA}", VOX_HEADERS))}
    movies = {}
    for slug, title in soon.items():
        movies[slug] = {"slug": slug, "title": title, "status": "soon"}
    for slug, title in now.items():
        movies[slug] = {"slug": slug, "title": title, "status": "now"}
    for slug in almaza:
        movies.setdefault(slug, {"slug": slug, "title": slug.replace("-", " ").title(), "status": "now"})
    for slug, movie in movies.items():
        movie["almaza"] = slug in almaza
    return sorted(movies.values(), key=lambda m: (m["status"] != "now", not m["almaza"], m["title"].lower()))


def add_new_watches(watches_doc: dict, new_slugs: "list[str]", panel: dict) -> int:
    existing = {w.get("id") for w in watches_doc.get("watches", [])}
    channels = panel.get("channels") or ["telegram"]
    heads_up = panel.get("headsUp", True)
    added = 0
    for slug in new_slugs:
        wid = f"{slug}-almaza"
        if wid in existing:
            continue
        watch = {"id": wid, "movie": {"vox": [slug]}}
        if heads_up:
            watch["match"] = [slug]
        watch["cinemas"] = [CINEMA]
        watch["channels"] = channels
        watches_doc.setdefault("watches", []).append(watch)
        added += 1
    return added


def main() -> int:
    movies = collect()
    if not movies:
        print("VOX listing came back empty; leaving movies.json untouched", file=sys.stderr)
        return 1
    old = read_json(ROOT / "movies.json", {})
    old_by_slug = {m["slug"]: m for m in old.get("movies", [])}
    today = datetime.now(timezone.utc).date().isoformat()
    for movie in movies:
        movie["firstSeen"] = old_by_slug.get(movie["slug"], {}).get("firstSeen", today)
    doc = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "movies": movies}
    (ROOT / "movies.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"movies.json: {len(movies)} movies")

    panel = read_json(ROOT / "panel.json", {})
    if panel.get("autoAddNew") and old_by_slug:
        new_slugs = [m["slug"] for m in movies if m["slug"] not in old_by_slug]
        if new_slugs:
            watches_doc = read_json(ROOT / "watches.json", {"watches": []})
            added = add_new_watches(watches_doc, new_slugs, panel)
            if added:
                (ROOT / "watches.json").write_text(json.dumps(watches_doc, indent=2) + "\n", encoding="utf-8")
                print(f"watches.json: added {added} new movie watch(es): {', '.join(new_slugs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
