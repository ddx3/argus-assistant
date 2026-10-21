"""Interface language: which one is chosen and how it reaches the pages.

The pages are written in English. For the other languages, a dictionary and a small
script (`static/i18n/engine.js`) that replaces the ready-made strings are inserted into
every HTML page when it is served. The insertion is done by a single layer over all
responses of the app, not in every route: several people add and change pages, and a
call of its own in each of them would one day be forgotten.

The language is taken from the setting `channel.assist.interface.language`. The value
"auto" (the default) means the Windows language, and if we do not have it, English.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

from argus.live import live

log = logging.getLogger(__name__)

# The order is as in the settings list. The first three are those the project was written
# for first. The rest go by the number of people who use desktop helper apps and work
# worse in an English interface than in their native one.
SUPPORTED = ("ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl")

# Windows languages that have no translation of their own but whose speakers usually read
# Russian: that is closer than English.
CIS_AS_RU = {"be", "kk", "ky", "tg", "uz", "tk", "hy", "az", "ka", "mo", "os", "ab"}

AUTO = "auto"
STATIC = live(Path(__file__).parent / "static")


def _windows_ui_language() -> str:
    """The Windows interface language as two letters ("ru"); empty if it could not be
    determined.
    """
    try:
        import ctypes
        import locale

        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
        name = locale.windows_locale.get(lang_id, "")
        return name.split("_")[0].lower()
    except Exception:  # noqa: BLE001 — not Windows or no permissions: the environment will decide
        return ""


def _mac_ui_language() -> str:
    """The first language from the "Languages & Region" list in macOS, two letters. Empty
    means it could not be determined.

    An app launched from Finder does not get `LANG`, so the language cannot be guessed from
    environment variables: we ask the system.
    """
    try:
        import Foundation

        languages = Foundation.NSLocale.preferredLanguages()
        if languages:
            return str(languages[0]).replace("_", "-").split("-")[0].lower()
    except Exception:  # noqa: BLE001 — no PyObjC: we will ask `defaults`
        pass
    try:
        import re
        import subprocess

        out = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=5,
        ).stdout
        found = re.search(r'"?([A-Za-z]{2,3})(?:[-_][A-Za-z0-9]+)*"?\s*[,)]', out)
        return found.group(1).lower() if found else ""
    except Exception:  # noqa: BLE001 — not macOS
        return ""


def system_language() -> str:
    code = _windows_ui_language()
    if not code and sys.platform == "darwin":
        code = _mac_ui_language()
    if not code:
        code = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").split("_")[0].split(".")[0].lower()
    return code


def resolve(setting: str | None, system: str | None = None) -> str:
    """The language of the pages: from the setting, and for "auto" — from the system language."""
    value = (setting or AUTO).strip().lower()
    if value in SUPPORTED:
        return value
    code = (system if system is not None else system_language()).lower()
    if code in SUPPORTED:
        return code
    if code in CIS_AS_RU:
        return "ru"
    return "en"


def setting_of(app) -> str:
    """What is written in the settings of this copy."""
    try:
        from argus.assist import settings as assist_settings

        return assist_settings.read(app.cfg).interface.language
    except Exception:  # noqa: BLE001 — the setting is bad: the page must not disappear
        log.exception("interface language not read — falling back to auto-detection")
        return AUTO


@lru_cache(maxsize=32)
def _dictionary(lang: str, stamp: float) -> str:
    path = STATIC / "i18n" / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # Strings only: the dictionary is inserted into the page, and anything extra there is
    # pointless.
    clean = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}
    return json.dumps(clean, ensure_ascii=False).replace("</", "<\\/")


def dictionary_json(lang: str) -> str | None:
    """The language dictionary as JSON to embed in the page. None — there is no dictionary."""
    path = STATIC / "i18n" / f"{lang}.json"
    if lang == "en" or not path.exists():
        return None
    try:
        return _dictionary(lang, path.stat().st_mtime)
    except Exception:  # noqa: BLE001 — a broken dictionary must not take the page down
        log.exception("dictionary not read: %s", lang)
        return None


def snippet(lang: str) -> str:
    """What to insert into `<head>`: the language, the dictionary and the engine. For English —
    only the language.
    """
    marker = f"<script>window.ARGUS_LANG={json.dumps(lang)};</script>"
    data = dictionary_json(lang)
    if data is None:
        return marker
    engine = (STATIC / "i18n" / "engine.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    return f"{marker}<script>window.ARGUS_I18N={data};</script><script>{engine}</script>"


_HEAD = re.compile(r"<head[^>]*>", re.IGNORECASE)


_OPEN, _CLOSE = "<!--argus-i18n-->", "<!--/argus-i18n-->"
_BLOCK = re.compile(re.escape(_OPEN) + r".*?" + re.escape(_CLOSE), re.DOTALL)


def strip(html: str) -> str:
    """A page without the inserted translation layer: what lies in the file.

    Needed by tests that compare the served page with the file, and by nobody else: people
    need the layer.
    """
    return _BLOCK.sub("", html, count=1)


def inject(html: str, lang: str) -> str:
    """Insert the language and the dictionary first in `<head>`, before any page code."""
    if _OPEN in html:             # already inserted
        return html
    block = _OPEN + snippet(lang) + _CLOSE
    m = _HEAD.search(html)
    if m:
        return html[: m.end()] + block + html[m.end():]
    return block + html


class Middleware:
    """An ASGI layer: adds the language and the dictionary to all HTML responses of the app.

    Not `BaseHTTPMiddleware`: it wraps streaming responses too (screen frames go as an
    endless `multipart`), and wrapping breaks them. Here only a response of type
    `text/html` is buffered, and everything else passes untouched.
    """

    def __init__(self, app, argus) -> None:
        self.app = app
        self.argus = argus

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = None
        chunks: list[bytes] = []

        async def wrapper(message):
            nonlocal start
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                if headers.get(b"content-type", b"").startswith(b"text/html") and message.get("status") == 200:
                    start = message           # hold it until the end of the body
                    return
                await send(message)
                return
            if message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                try:
                    lang = resolve(setting_of(self.argus))
                    body = inject(body.decode("utf-8"), lang).encode("utf-8")
                except Exception:  # noqa: BLE001 — the page matters more than the translation
                    log.exception("language was not injected into the page")
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, wrapper)


def install(web, argus) -> None:
    """Attach the layer to the FastAPI application."""
    web.add_middleware(Middleware, argus=argus)
