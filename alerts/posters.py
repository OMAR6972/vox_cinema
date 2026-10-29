"""Keeps a copy of each movie poster inside the repo so the control page can always show it."""
from __future__ import annotations

import re
from pathlib import Path

MAX_BYTES = 700_000


def local_name(slug: str, url: str) -> str:
    m = re.search(r"\.(jpe?g|png|webp)(?:\?|$)", url or "", re.I)
    return f"posters/{slug}.{(m.group(1).lower() if m else 'jpg')}"


def sync_posters(root: Path, movies: list, download, limit: int = 15, keep=()) -> int:
    """download(url) -> bytes. Fills movie['poster'] with a repo path when a local copy exists."""
    (root / "posters").mkdir(exist_ok=True)
    fetched = 0
    for m in movies:
        url = m.get("poster")
        if not url or url.startswith("posters/"):
            continue
        rel = local_name(m["slug"], url)
        path = root / rel
        if not path.exists() and fetched < limit:
            try:
                data = download(url)
                if data and len(data) <= MAX_BYTES:
                    path.write_bytes(data)
                    fetched += 1
            except Exception:
                pass
        if path.exists():
            m["remote"] = url
            m["poster"] = rel
    # tidy up: delete posters of movies VOX no longer lists (unless one of your watches still uses it)
    wanted = {m["slug"] for m in movies} | set(keep)
    for f in (root / "posters").iterdir():
        if f.is_file() and f.stem not in wanted:
            f.unlink()
    return fetched


def looks_like_image(b: bytes) -> bool:
    return b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")


def cffi_download(url: str, base: str) -> bytes:
    from curl_cffi import requests as cffi
    r = cffi.get(url, impersonate="chrome124", timeout=20, headers={"Referer": base + "/"})
    if r.status_code != 200 or not looks_like_image(r.content):
        raise RuntimeError(f"not an image ({r.status_code})")
    return r.content
