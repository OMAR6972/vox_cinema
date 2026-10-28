"""Minimal Telegram client: send messages, read replies, answer buttons."""
from __future__ import annotations

import re

import requests


class Telegram:
    def __init__(self, token: str, session=None):
        self.token = token
        self.http = session or requests
        self.username = None

    def api(self, method, body=None, timeout=30):
        r = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=body or {}, timeout=timeout)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method}: {j.get('description') or r.status_code}")
        return j["result"]

    def start(self):
        self.username = self.api("getMe")["username"]
        try:
            self.api("setMyCommands", {"commands": [
                {"command": "status", "description": "What am I watching for you?"},
                {"command": "help", "description": "How this works"},
            ]})
        except RuntimeError:
            pass

    def send(self, chat_id, html, buttons=None):
        text = html
        if len(text) > 4000:
            text = re.sub(r"<[^>]*$", "", text[:3990]) + "…"
        body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True}}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        return self.api("sendMessage", body)

    def updates(self, offset):
        return self.api("getUpdates", {"offset": offset, "timeout": 0,
                                       "allowed_updates": ["message", "callback_query"]})

    def answer_callback(self, qid, text="Done"):
        try:
            self.api("answerCallbackQuery", {"callback_query_id": qid, "text": text})
        except RuntimeError:
            pass

    def invite_link(self, token, group=False):
        if not self.username:
            return None
        return f"https://t.me/{self.username}?{'startgroup' if group else 'start'}={token}"
