"""Reads VOX Cinemas Egypt pages and turns them into plain data."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .vtime import compact_to_iso, iso_to_compact, parse_vox_time, pretty_time, today_cairo

BASE = "https://egy.voxcinemas.com"


@dataclass
class ShowtimesPage:
    date: str
    dates: list
    sessions: list
    movies: dict
    looks_right: bool


def _tag_children(el):
    return [c for c in el.children if getattr(c, "name", None)]


def _parse_date_tabs(soup, current_iso):
    dates = []
    for li in soup.select("nav.date-filter ol > li"):
        a = li.find("a", href=True)
        if a:
            m = re.search(r"[?&]d=(\d{8})", a["href"])
            if m:
                dates.append(compact_to_iso(m.group(1)))
            elif re.search("today", a.get_text(), re.I):
                dates.append(today_cairo())
        elif current_iso:
            dates.append(current_iso)
    return sorted(set(dates))


def _parse_cinema_blocks(container, slug, title, date):
    sessions = []
    cinema = None
    for el in _tag_children(container):
        if el.name == "h3":
            cinema = el.get_text().strip()
        elif el.name == "ol" and "showtimes" in (el.get("class") or []):
            for exp_li in [c for c in _tag_children(el) if c.name == "li"]:
                strongs = [c for c in _tag_children(exp_li) if c.name == "strong"]
                exp = strongs[0].get_text().strip() if strongs else ""
                exp = exp or "Standard"
                for s_li in exp_li.select("li[data-id]"):
                    sid = s_li.get("data-id")
                    link = s_li.select_one("a.showtime")
                    node = link or s_li.select_one(".showtime")
                    label = node.get_text().strip() if node else ""
                    mins = parse_vox_time(label)
                    if not sid or mins is None:
                        continue
                    sessions.append({
                        "id": sid, "slug": slug, "title": title, "date": date, "cinema": cinema,
                        "exp": exp, "min": mins, "time": pretty_time(mins),
                        "available": link is not None,
                        "url": (link.get("href") if link else None) or f"{BASE}/booking/{sid}",
                    })
    return sessions


def parse_showtimes_page(html: str) -> ShowtimesPage:
    """The all-movies page (/showtimes or /showtimes?d=YYYYMMDD)."""
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.select_one('form.auto-submit input[name="d"]')
    d = inp.get("value") if inp else None
    date = compact_to_iso(d) if d and re.fullmatch(r"\d{8}", d) else today_cairo()
    dates = _parse_date_tabs(soup, date)
    sessions, movies = [], {}
    for art in soup.select("article.movie-compare"):
        slug = art.get("data-slug")
        if not slug:
            continue
        h2 = art.select_one(".movie-hero h2")
        title = h2.get_text().strip() if h2 and h2.get_text().strip() else slug
        movies[slug] = title
        for box in art.select("div.dates"):
            sessions.extend(_parse_cinema_blocks(box, slug, title, date))
    looks_right = bool(soup.select("nav.date-filter") or soup.select("section.showtimes"))
    return ShowtimesPage(date, dates, sessions, movies, looks_right)


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        if x["slug"] not in seen:
            seen.add(x["slug"])
            out.append(x)
    return out


def parse_filters(html: str) -> dict:
    """Cinemas and bookable movies from the quick-filter form found on most pages."""
    soup = BeautifulSoup(html, "html.parser")

    def read(name):
        out = []
        for i in soup.select(f'input[name="{name}"]'):
            label = i.find_parent("label")
            span = label.find("span") if label else None
            out.append({"slug": i.get("value"), "name": span.get_text().strip() if span else ""})
        return _dedupe([x for x in out if x["slug"]])

    return {"cinemas": read("c"), "movies": read("m")}


def parse_coming_soon(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select('h3 > a[href^="/movies/"]'):
        slug = a["href"].split("/movies/", 1)[1]
        slug = re.split(r"[?#]", slug)[0]
        if not slug or slug in ("whatson", "comingsoon"):
            continue
        clone = BeautifulSoup(str(a), "html.parser")
        for c in clone.select(".classification"):
            c.decompose()
        title = clone.get_text().strip()
        h3 = a.find_parent("h3")
        p = h3.find_next_sibling("p") if h3 else None
        m = re.search(r"Release Date:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})", p.get_text()) if p else None
        out.append({"slug": slug, "title": title, "release": m.group(1) if m else None})
    return _dedupe(out)


def parse_posters(html: str) -> dict:
    """slug -> poster image URL, from a VOX movie listing (whatson / comingsoon)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for art in soup.select("article.movie-summary[data-slug]"):
        img = art.select_one("img.poster")
        if img and img.get("src"):
            out[art["data-slug"]] = img["src"]
    return out


def parse_listing_titles(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    return [{"slug": a["data-slug"], "title": a.get("data-title") or a["data-slug"]}
            for a in soup.select("article.movie-summary[data-slug]")]


def showtimes_path(iso: str | None = None) -> str:
    return f"/showtimes?d={iso_to_compact(iso)}" if iso else "/showtimes"


def movie_url(slug: str, iso: str | None = None) -> str:
    return f"{BASE}/movies/{slug}" + (f"?d={iso_to_compact(iso)}" if iso else "") + "#showtimes"
