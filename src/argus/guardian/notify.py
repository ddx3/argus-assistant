"""Telling a person. Three channels: a Markdown file (always), e-mail and Telegram (when configured).

Everything is redacted on the way out. A message is short on purpose: what happened, how sure the
guardian is, and the one command that moves things forward. The details live in `show <id>`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from argus.guardian.config import Config
from argus.guardian.redact import redact

log = logging.getLogger("argus.guardian")


class Notifier(Protocol):
    def send(self, title: str, body: str) -> None: ...


class FileNotifier:
    def __init__(self, home: Path) -> None:
        self.dir = home / "reports"
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, title: str, body: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
        text = redact(f"# {title}\n\n{body}\n")
        (self.dir / f"{stamp}-{slug}.md").write_text(text, encoding="utf-8")
        with (self.dir / "latest.md").open("w", encoding="utf-8") as fh:
            fh.write(text)


def _telegram_post(url: str, data: bytes) -> None:
    urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=20).read()


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, post: Callable[[str, bytes], None] = _telegram_post) -> None:
        self.token, self.chat_id, self._post = token, chat_id, post

    def send(self, title: str, body: str) -> None:
        text = redact(f"{title}\n\n{body}")[:3900]
        payload = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        try:
            self._post(f"https://api.telegram.org/bot{self.token}/sendMessage", payload)
        except Exception as e:  # noqa: BLE001 - a failed message must not stop the cycle; the file report still exists
            log.debug("telegram message not sent: %s", e)


def _resend_post(key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "User-Agent": "argus-guardian/1"})        # without a User-Agent the provider's firewall refuses the call
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read() or b"{}")


class EmailNotifier:
    """A plain-text e-mail through the same sending service the server uses for its own mail."""

    def __init__(self, key: str, sender: str, to: str, post: Callable[[str, dict], dict | None] = _resend_post) -> None:
        self.key, self.sender, self.to, self._post = key, sender, to, post
        self.last_error = ""
        self.last_id = ""

    def send(self, title: str, body: str) -> None:
        payload = {"from": self.sender, "to": [self.to], "subject": redact(title)[:150], "text": redact(body)[:20000]}
        try:
            answer = self._post(self.key, payload) or {}
            self.last_id, self.last_error = str(answer.get("id", "")), ""
        except Exception as e:  # noqa: BLE001 - a failed e-mail must not stop the cycle; the file report still exists
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("guardian: e-mail not sent: %s", e)


class Composite:
    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = notifiers

    def send(self, title: str, body: str) -> None:
        for n in self.notifiers:
            n.send(title, body)


def resend_key(cfg: Config) -> str:
    """The sending key: from the environment, or from a one-line file (never from guardian.toml)."""
    key = os.environ.get("GUARDIAN_RESEND_KEY", "").strip()
    if key:
        return key
    path = Path(cfg.resend_key_file).expanduser() if cfg.resend_key_file else None
    if path is not None and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def build_notifier(cfg: Config) -> Notifier:
    notifiers: list[Notifier] = [FileNotifier(cfg.home)]
    token, chat = os.environ.get("GUARDIAN_TELEGRAM_TOKEN", ""), os.environ.get("GUARDIAN_TELEGRAM_CHAT", "")
    if token and chat:
        notifiers.append(TelegramNotifier(token, chat))
    key = resend_key(cfg)
    if cfg.email_to and cfg.email_from and key:
        notifiers.append(EmailNotifier(key, cfg.email_from, cfg.email_to))
    return Composite(*notifiers)


__all__ = ["Composite", "EmailNotifier", "FileNotifier", "Notifier", "TelegramNotifier", "build_notifier", "resend_key"]
