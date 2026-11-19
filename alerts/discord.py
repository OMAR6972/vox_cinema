"""Send an alert to a Discord channel through a webhook (a private link Discord gives you)."""
from __future__ import annotations

import re
import time

import requests

from .textsplit import split_message


def to_discord(html: str) -> str:
    """Telegram-style HTML -> Discord markdown."""
    t = re.sub(r"<a href=\"([^\"]+)\">(.*?)</a>", r"[\2](<\1>)", html, flags=re.S)
    t = re.sub(r"</?b>", "**", t)
    t = re.sub(r"</?i>", "_", t)
    t = re.sub(r"</?s>", "~~", t)
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    return t


def send(webhook_url: str, html: str, http=requests, sleep=time.sleep, image=None):
    """Discord allows 2000 characters a message: a longer alert goes out as several, the poster on the first."""
    for n, piece in enumerate(split_message(to_discord(html), 1900)):
        _post(webhook_url, piece, http, sleep, image if n == 0 else None)


def _post(webhook_url, content, http, sleep, image):
    body = {"content": content, "allowed_mentions": {"parse": []}}
    if image:
        body["embeds"] = [{"image": {"url": image}}]
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
