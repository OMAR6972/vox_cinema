"""Send an alert to a Discord channel through a webhook (a private link Discord gives you)."""
from __future__ import annotations

import re
import time

import requests


def to_discord(html: str) -> str:
    """Telegram-style HTML -> Discord markdown."""
    t = re.sub(r"<a href=\"([^\"]+)\">(.*?)</a>", r"[\2](<\1>)", html, flags=re.S)
    t = re.sub(r"</?b>", "**", t)
    t = re.sub(r"</?i>", "_", t)
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    if len(t) > 1900:
        t = t[:1890].rsplit("\n", 1)[0] + "\n…"
    return t


def send(webhook_url: str, html: str, http=requests, sleep=time.sleep):
    body = {"content": to_discord(html), "allowed_mentions": {"parse": []}}
    for _ in range(3):
        r = http.post(webhook_url, json=body, timeout=20)
        if r.status_code == 429:
            try:
                sleep(min(float(r.json().get("retry_after", 2)), 10))
            except Exception:
                sleep(2)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord said {r.status_code}")
        return
    raise RuntimeError("Discord kept rate-limiting")
