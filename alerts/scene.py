"""Reads Scene Cinemas (Cairo Festival City) pages and turns them into plain data.

Scene is organised per movie: the cinema's home page lists its movies, a movie page has a date strip, and each date's
showtimes come from a small XHR fragment. Sessions come out in the same shape the VOX reader produces, so the
rest of the engine does not care which site a showtime came from.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .vtime import parse_vox_time, pretty_time

# Scene has two branches, each with its own site. The name is what messages and the panel call the cinema.
BRANCHES = {
    "cfc": {"base": "https://cfc.scenecinemas.com", "name": "Scene CFC"},
    "district5": {"base": "https://district5.scenecinemas.com", "name": "Scene District 5"},
}
CINEMAS = [b["name"] for b in BRANCHES.values()]
SOURCE = "scene"


def norm(slug: str) -> str:
    """Scene writes resident_evil, VOX writes resident-evil: compare them without caring about the separator."""
    return re.sub(r"[^a-z0-9]+", "-", str(slug).lower()).strip("-")


def home_url(branch: str) -> str:
    return BRANCHES[branch]["base"] + "/"


def movie_url(branch: str, raw_slug: str, iso: str | None = None) -> str:
    base = f"{BRANCHES[branch]['base']}/movie-details/{raw_slug}.html"
    return base + (f"?business_day={iso[8:10]}-{iso[5:7]}-{iso[0:4]}&ajax=1" if iso else "")


def public_movie_url(branch: str, raw_slug: str) -> str:
    return f"{BRANCHES[branch]['base']}/movie-details/{raw_slug}.html"


def parse_listing(html: str) -> list:
    """[(raw_slug, title)] for every movie linked from the cinema's home page."""
    movies = {}
    for link in BeautifulSoup(html, "html.parser").select('a[href*="/movie-details/"]'):
        m = re.search(r"/movie-details/([^/?]+)\.html", link.get("href", ""))
        if not m:
            continue
        slug = m[1]
        image = link.find("img", alt=True)
        title = (link.get("title", "").strip() or (image["alt"].strip() if image else "")
                 or link.get_text(" ", strip=True) or slug)
        if slug not in movies or (movies[slug] == slug and title != slug):
            movies[slug] = title
    return list(movies.items())


def parse_movie_page(html: str, raw_slug: str):
    """(title, [iso dates the movie page offers])."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".movie-name")
    title = heading.get_text(" ", strip=True) if heading else raw_slug.replace("_", " ").replace("-", " ").title()
    dates = set()
    for li in soup.select("li.calanderdays[id]"):
        m = re.match(r"data-(\d{2})-(\d{2})-(\d{4})$", li["id"])
        if m:
            dates.add(f"{m[3]}-{m[2]}-{m[1]}")
    return title, sorted(dates)


def parse_fragment(html: str, iso: str, slug: str, title: str, raw_slug: str, branch: str = "cfc") -> list:
    """Sessions in one date's XHR fragment. An empty body means nothing is scheduled (yet)."""
    if not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    labels = soup.find_all("span", class_=lambda c: bool(c) and any(
        x.startswith("ex_") and not x.endswith("_content") for x in c.split()))
    for span in labels:
        exp = span.get_text(strip=True)
        box = span.find_parent("div")
        if not exp or box is None:
            continue
        for a in box.select("ul li a"):
            label = a.get_text(strip=True)
            mins = parse_vox_time(label)
            if mins is None:
                continue
            href = (a.get("href") or "").strip()
            sold = "showtime_soldout" in (a.get("class") or []) or href.lower().startswith("javascript:")
            out.append({
                # Scene gives showtimes no stable id, so the identity is where, when and which screen type
                "id": f"scene:{branch}:{iso}:{mins}:{norm(exp)}:{slug}",
                "slug": slug, "title": title, "date": iso, "cinema": BRANCHES[branch]["name"], "exp": exp,
                "min": mins, "time": pretty_time(mins),
                "available": not sold,
                "url": public_movie_url(branch, raw_slug) if (sold or not href.startswith("http")) else href,
            })
    return out
