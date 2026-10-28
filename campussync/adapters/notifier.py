"""Push notifications (Telegram). Mail notifications always go out through the mail adapter;
this adds an instant phone ping for people who switch it on in their settings.

Setup: create a bot with @BotFather, put its token in .env as TELEGRAM_BOT_TOKEN, message the
bot once, then read your chat id from https://api.telegram.org/bot<token>/getUpdates and paste it
in Settings. Without a token, messages are only written to the notification log."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable


class NotificationService:
    def __init__(self, settings, store, clock):
        self.store, self.clock = store, clock
        self.channel = str(settings.get("adapters.notifier", "telegram"))
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.sender: Callable[[str, str], tuple[bool, str]] | None = None  # tests can swap this

    @property
    def live(self) -> bool:
        return self.channel == "telegram" and (bool(self.token) or self.sender is not None)

    def status(self) -> str:
        if self.channel != "telegram":
            return "off"
        return "Telegram bot connected" if self.token else "no TELEGRAM_BOT_TOKEN (messages are only logged)"

    def push(self, person_id: str, text: str, force: bool = False) -> dict | None:
        pr = self.store.prefs(person_id)
        chat = str(pr.get("telegram_chat_id") or "").strip()
        if self.channel != "telegram" or not chat or not (pr.get("notify_telegram") or force):
            return None
        entry = {"id": self.store.new_id("N"), "person_id": person_id, "channel": "telegram", "text": text[:1000],
                 "at": self.clock().isoformat(timespec="minutes"), "status": "logged", "error": ""}
        if self.live:
            ok, err = (self.sender or self._send)(chat, text)
            entry["status"], entry["error"] = ("sent", "") if ok else ("failed", err[:200])
        else:
            entry["status"] = "logged (no bot token)"
        self.store.log_notification(entry)
        return entry

    def log_for(self, person_id: str) -> list[dict]:
        return [n for n in reversed(self.store.data["notifications"]) if n["person_id"] == person_id]

    def _send(self, chat_id: str, text: str) -> tuple[bool, str]:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode() or "{}")
                return bool(data.get("ok")), str(data.get("description", ""))
        except Exception as e:  # network blocked, bad token, etc.
            return False, f"{type(e).__name__}: {e}"
