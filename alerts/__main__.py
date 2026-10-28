"""python -m alerts  ->  one run."""
from __future__ import annotations

import logging
import os
import sys

from check_showtimes import VOX_HEADERS, fetch_page

from .engine import ROOT, Engine, read_json, write_json
from .tg import Telegram
from .vox import BASE


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1
    config = read_json(ROOT / "config.json", {"settings": {}, "people": [], "watches": []})
    state = read_json(ROOT / "alerts-state.json", {})
    tg = Telegram(token)
    tg.start()
    eng = Engine(config, state, tg, lambda path: fetch_page(BASE + path, VOX_HEADERS),
                 owner_chat_id=os.environ.get("TELEGRAM_CHAT_ID"))
    try:
        eng.run()
    finally:
        eng.state.pop("lastRun", None)
        eng.state.pop("lastCheck", None)  # would change every run and flood the repo with commits
        write_json(ROOT / "alerts-state.json", eng.state)
        if hasattr(eng, "snapshot"):
            doc = eng.movies_doc()
            old = read_json(ROOT / "movies.json", {})
            if doc != {k: old.get(k) for k in doc}:
                write_json(ROOT / "movies.json", {**doc, "bot": tg.username})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
