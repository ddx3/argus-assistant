"""Shared test settings.

The app runs on Windows and macOS. Some tests are about Windows by definition: WASAPI,
named mutexes, Win32 windows. They are marked `windows_only` and are skipped on other
systems rather than failing: a skip is visible in the report and honestly says "not checked
here", while a failure would make people look for a breakage where there is none.

What is checked on macOS is in `tests/test_mac_*.py` and in `test_portability.py`; they
also run on Windows, on stand-in systems (see `docs/macos.md`).
"""

from __future__ import annotations

import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "windows_only: a test about Windows (WASAPI, mutexes, Win32 windows); skipped on other systems",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="Windows only")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def release_signer(monkeypatch):
    """Our own release key instead of the real one: signed build manifests for the update tests.

    `signer.manifest(...)` returns a release manifest (like `latest.json`) together with an
    archive "content" whose hash and size are signed in it; `signer.tampered(...)` is the same
    with a corrupted field.
    """
    import base64
    import hashlib
    from types import SimpleNamespace

    from argus import ed25519, update_trust

    seed = bytes(range(1, 33))
    monkeypatch.setattr(update_trust, "TRUSTED_KEYS", (base64.b64encode(ed25519.public_key(seed)).decode(),))

    def manifest(platform="win", version="1.0+new", build=2000, zip_name=None, payload=b"zip-bytes" * 100):
        if zip_name is None:
            zip_name = "Argus-mac.zip" if platform == "mac" else "Argus-win.zip"
        info = {
            "version": version, "zip": zip_name, "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload), "build": build,
        }
        return SimpleNamespace(info=update_trust.sign_manifest(info, platform, seed), payload=payload)

    return SimpleNamespace(seed=seed, manifest=manifest)


# The address the tests use to talk to the local server: exactly the one the window and the
# hotkeys use. The guard (`argus/ui/guard.py`) accepts no other names in `Host`, while
# `TestClient` defaults to `testserver`, and `httpx.ASGITransport` uses whatever is written in
# `base_url` (`test`).
LOCAL_BASE_URL = "http://127.0.0.1:8420"


def _has_local_guard(app) -> bool:
    """The application was built with `ui.server.build`: the local server guard is installed
    on it.
    """
    from argus.ui.guard import LocalGuard

    return any(getattr(m, "cls", None) is LocalGuard for m in getattr(app, "user_middleware", ()))


@pytest.fixture(autouse=True)
def local_app_speaks_from_its_own_host(monkeypatch):
    """The test clients of the guarded app connect from `127.0.0.1:8420`, not from `testserver`.

    This is substituted only where the address is not set by hand: the guard tests themselves
    set a foreign `Host` and `Origin` through request headers. Other apps (the account server,
    bare `FastAPI()` instances in tests of individual routes) are not affected: they have no
    guard, and the address does not matter to them.
    """
    from urllib.parse import urljoin

    import httpx
    from starlette.testclient import TestClient

    real_sync = TestClient.__init__
    real_async = httpx.AsyncClient.__init__

    def sync_init(self, app, base_url="http://testserver", *args, **kwargs):
        if str(base_url).rstrip("/") == "http://testserver" and _has_local_guard(app):
            base_url = LOCAL_BASE_URL
        real_sync(self, app, base_url, *args, **kwargs)

    def async_init(self, *args, transport=None, base_url="", **kwargs):
        asgi = isinstance(transport, httpx.ASGITransport)
        app = getattr(transport, "app", None) if asgi else None
        unset = str(base_url).rstrip("/") in ("", "http://test", "http://testserver")
        if app is not None and unset and _has_local_guard(app):
            base_url = LOCAL_BASE_URL
        real_async(self, *args, transport=transport, base_url=base_url, **kwargs)

    real_ws = TestClient.websocket_connect

    def ws_connect(self, url, subprotocols=None, **kwargs):
        # A `TestClient` socket always builds the address from `ws://testserver`, not from `base_url`.
        if _has_local_guard(self.app):
            url = urljoin("ws://" + LOCAL_BASE_URL.removeprefix("http://"), url)
        return real_ws(self, url, subprotocols, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", sync_init)
    monkeypatch.setattr(TestClient, "websocket_connect", ws_connect)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", async_init)
