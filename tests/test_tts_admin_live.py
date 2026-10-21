"""Voiceover through a real server: an edit reaches the pipeline and the route.

The same lesson as in `test_assist_admin_live.py`: what must be checked is not the
presence of a route but that data arrives through it, and that an edit in the admin page
actually turns on and configures the `Watcher`, rather than only being written to a file.

Characters (`channel.tts.characters`) cannot be created through `/admin/apply`: it is not
a registered `Setting` but a field that `argus clone-voice` writes directly through
`SettingsStore` (see `cli.py`). Here the same path is reproduced: `argus.local.json` with
an already "cloned" character is put on disk before the server comes up, exactly as it
happens in reality when `clone-voice` ran before `argus run`.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from argus.app import Argus
from argus.brain.base import Answer
from argus.core.config import Config
from argus.core.settings_store import LOCAL_NAME
from argus.tts import client as tts_client
from argus.tts.settings import TTSSettings, VoiceProfile
from argus.ui.admin import _character_options
from argus.ui.server import build


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start(tmp_path, monkeypatch, *, seed_character: bool):
    import uvicorn

    async def fake_synthesize(text, profile, settings, api_key):
        return f"clip-for-{profile.voice_id}".encode()

    monkeypatch.setattr(tts_client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    if seed_character:
        (tmp_path / LOCAL_NAME).write_text(
            json.dumps({"channel": {"tts": {"characters": {"demo": {"voice_id": "v1"}}}}}),
            encoding="utf-8",
        )

    # `Config.load()`, not a bare `Config()` + `cfg.root =`: only `.load()` merges
    # `argus.local.json` into the config at startup (`core/config.py`), exactly as `argus run`
    # does. The character from `seed_character` must be visible to the panel already at build
    # time (`ui/admin.py::sections()` reads `app.cfg` once, before the server comes up); with a
    # bare `Config()` the edit on disk would stay unread until the first `/admin/apply`.
    (tmp_path / "argus.toml").write_text("", encoding="utf-8")
    cfg = Config.load(tmp_path / "argus.toml")
    cfg.ui.admin = True
    app = Argus(cfg, with_screen=False, with_brain=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        build(app), host="127.0.0.1", port=port,
        log_level="critical", access_log=False, ws="auto",
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("the server did not come up")

    return app, port, server, task


@pytest.fixture
async def live(tmp_path, monkeypatch):
    """A server on a free port, with no characters: edits go to a temporary folder."""
    app, port, server, task = await _start(tmp_path, monkeypatch, seed_character=False)
    yield app, port, tmp_path
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


@pytest.fixture
async def live_with_voice(tmp_path, monkeypatch):
    """The same thing, but with an already "cloned" character `demo` (voice_id=v1)."""
    app, port, server, task = await _start(tmp_path, monkeypatch, seed_character=True)
    yield app, port, tmp_path
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


async def client(port):
    import httpx

    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20)


async def _wait_for(predicate, tries: int = 50) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Namesakes: two different voices with the same name
# --------------------------------------------------------------------------


async def test_save_character_disambiguates_same_named_voices_in_one_run(tmp_path):
    """Regression: `argus sync-voices` against a live ElevenLabs account found two different
    voices both literally named "Hoffmann", and the second quietly overwrote the
    first under the same key. The same `cfg`, the same process, with no intermediate
    `Config.load()`: exactly what the `sync_voices()` loop over the list of voices does."""
    from argus.cli import _save_character

    (tmp_path / "argus.toml").write_text("", encoding="utf-8")
    cfg = Config.load(tmp_path / "argus.toml")

    first = _save_character(cfg, "voice-aaa1", "Hoffmann")
    second = _save_character(cfg, "voice-bbb2", "Hoffmann")

    assert first != second
    from argus.tts.settings import read as tts_read

    characters = tts_read(Config.load(tmp_path / "argus.toml")).characters
    assert characters[first].voice_id == "voice-aaa1"
    assert characters[second].voice_id == "voice-bbb2"


async def test_save_character_reuses_the_same_slug_for_the_same_voice(tmp_path):
    """Not a namesake: the same voice under the same name a second time (`sync-voices` is
    restarted) must not breed `name-2`, `name-3` without end."""
    from argus.cli import _save_character

    (tmp_path / "argus.toml").write_text("", encoding="utf-8")
    cfg = Config.load(tmp_path / "argus.toml")

    first = _save_character(cfg, "voice-aaa1", "Dmitry")
    second = _save_character(cfg, "voice-aaa1", "Dmitry")
    assert first == second


# --------------------------------------------------------------------------
# Labels in the dropdown list
# --------------------------------------------------------------------------


def test_character_options_sort_favorites_first_then_by_label_with_language_tag():
    settings = TTSSettings(characters={
        "b": VoiceProfile(label="Dmitry", voice_id="1", language="ru"),
        "a": VoiceProfile(label="Alice", voice_id="2", language="en", favorite=True),
        # With no name the label must fall back to the key, not to an empty string: a real
        # regression, caught before it went into the code.
        "c": VoiceProfile(voice_id="3"),
    })
    # "c" < "dmitry" alphabetically; the order is not the point here, what matters is the fact
    # that "c" did not remain an empty string.
    options = _character_options(settings)
    assert options == [
        ("a", "★ Alice · en"),
        ("c", "c"),
        ("b", "Dmitry · ru"),
    ]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


async def test_schema_carries_tts_defaults(live):
    _, port, _ = live
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    paths = {f["path"] for s in schema["sections"] for f in s["settings"]}
    assert {"channel.tts.enabled", "channel.tts.active_character"} <= paths


async def test_schema_lists_seeded_characters_as_choice_options(live_with_voice):
    """A character is a drop-down list, not a field for typing a slug by hand."""
    _, port, _ = live_with_voice
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    field = next(
        f for s in schema["sections"] for f in s["settings"]
        if f["path"] == "channel.tts.active_character"
    )
    assert field["kind"] == "choice"
    assert {o["value"] for o in field["options"]} >= {"", "demo"}


async def test_active_character_setting_is_hidden(live_with_voice):
    """Regression from a review: a second `<select>` with the same characters under the widget's
    cards read as two different lists on one page. The choice is only by card (`api.set` from
    `voice-picker`); the row stays registered for the sake of `apply()`/`A.values`, it is just
    not drawn (`Setting.hidden`, the same trick as with `favorite`)."""
    _, port, _ = live_with_voice
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    field = next(
        f for s in schema["sections"] for f in s["settings"]
        if f["path"] == "channel.tts.active_character"
    )
    assert field["hidden"] is True


async def test_enabled_setting_is_hidden(live_with_voice):
    """The same review, the next round: the toggle should come first, then the list. But the
    scaffold (`section()` in admin.js) always puts the widget BEFORE `.fields`, regardless of
    the order of `settings`. Since the widget decides the order, not the scaffold, the toggle
    is hidden too and is drawn as the first row inside "voice-picker" (see `#vp-enabled` in
    `admin.html`)."""
    _, port, _ = live_with_voice
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    field = next(
        f for s in schema["sections"] for f in s["settings"]
        if f["path"] == "channel.tts.enabled"
    )
    assert field["hidden"] is True


async def test_schema_marks_character_favorite_setting_hidden(live_with_voice):
    """`favorite` exists and is accepted by `apply()` (see the test below), but gets no separate
    row in the form: the "voice-picker" widget draws it itself, as a star on the card
    (`Setting.hidden`, `admin_base.py`)."""
    _, port, _ = live_with_voice
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    field = next(
        f for s in schema["sections"] for f in s["settings"]
        if f["path"] == "channel.tts.characters.demo.favorite"
    )
    assert field["hidden"] is True
    assert field["kind"] == "bool"


async def test_favorite_toggle_persists_and_is_visible_on_reload(live_with_voice):
    """An edit that bypasses the visible form, through the same shared `/admin/apply` as an
    ordinary setting, must reach not only the file on disk but also the next `/admin/schema`:
    the panel has its own snapshot of values (`AdminPanel.store`), separate from what was read
    from disk."""
    _, port, _ = live_with_voice
    async with await client(port) as http:
        r = await http.post("/admin/apply", json={"values": {
            "channel.tts.characters.demo.favorite": True,
        }})
        assert r.json()["ok"] is True
        assert r.json()["values"]["channel.tts.characters.demo.favorite"] is True

        schema = (await http.get("/admin/schema")).json()
    assert schema["values"]["channel.tts.characters.demo.favorite"] is True


async def test_schema_values_expose_unregistered_character_fields(tmp_path, monkeypatch):
    """`language`/`preview_url` do not become a `Setting`: nobody edits them in the form, the
    widget only reads them from the values. This checks that they are there anyway:
    `/admin/schema` carries the whole config (`SettingsStore.values()`), not only the
    pre-registered paths."""
    import json

    from argus.core.settings_store import LOCAL_NAME

    # `preview_url` is the same case as `language`: it goes next to it, with the same check,
    # without creating a separate test for the second non-editable field.
    preview_url = "https://api.us.elevenlabs.io/v1/voices/v1/previews/audio?payload=x"
    (tmp_path / LOCAL_NAME).write_text(
        json.dumps({"channel": {"tts": {"characters": {
            "demo": {"voice_id": "v1", "language": "ru", "preview_url": preview_url}
        }}}}),
        encoding="utf-8",
    )
    app, port, server, task = await _start(tmp_path, monkeypatch, seed_character=False)
    try:
        async with await client(port) as http:
            schema = (await http.get("/admin/schema")).json()
        assert schema["values"]["channel.tts.characters.demo.language"] == "ru"
        assert schema["values"]["channel.tts.characters.demo.preview_url"] == preview_url
        paths = {f["path"] for s in schema["sections"] for f in s["settings"]}
        assert "channel.tts.characters.demo.language" not in paths
        assert "channel.tts.characters.demo.preview_url" not in paths
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


# --------------------------------------------------------------------------
# An edit reaches the pipeline
# --------------------------------------------------------------------------


async def test_enabling_tts_starts_the_watcher(live):
    app, port, _ = live
    assert getattr(app, "_tts_watcher", None) is None

    async with await client(port) as http:
        r = await http.post("/admin/apply", json={"values": {
            "channel.tts.enabled": True,
            "channel.tts.active_character": "demo",
        }})
    assert r.json()["ok"] is True
    assert getattr(app, "_tts_watcher", None) is not None


async def test_disabling_tts_tears_down_the_watcher(live):
    app, port, _ = live
    async with await client(port) as http:
        await http.post("/admin/apply", json={"values": {
            "channel.tts.enabled": True,
            "channel.tts.active_character": "demo",
        }})
        assert getattr(app, "_tts_watcher", None) is not None

        await http.post("/admin/apply", json={"values": {"channel.tts.enabled": False}})
    assert getattr(app, "_tts_watcher", None) is None


# --------------------------------------------------------------------------
# The clip route
# --------------------------------------------------------------------------


async def test_clip_route_serves_synthesized_audio(live_with_voice):
    app, port, _ = live_with_voice
    async with await client(port) as http:
        await http.post("/admin/apply", json={"values": {
            "channel.tts.enabled": True,
            "channel.tts.active_character": "demo",
        }})

        answer = Answer(text="hello", provider="stub")
        answer.meta["t"] = 777
        app.bus.publish(answer)
        await _wait_for(lambda: answer.meta.get("audio_ids"))

        r = await http.get(f"/speech/{answer.meta['audio_ids'][0]}.mp3")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"clip-for-v1"


async def test_clip_route_404s_on_unknown_id(live):
    _, port, _ = live
    async with await client(port) as http:
        r = await http.get("/speech/999999.mp3")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Listen before saving (`POST /tts/preview`)
# --------------------------------------------------------------------------


async def test_preview_route_serves_synthesized_audio(live_with_voice):
    """People listen to a voice before they turn speech on, so `enabled` is not touched."""
    _, port, _ = live_with_voice
    async with await client(port) as http:
        r = await http.post("/tts/preview", json={"character": "demo"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"clip-for-v1"


async def test_preview_route_404s_on_unknown_character(live):
    _, port, _ = live
    async with await client(port) as http:
        r = await http.post("/tts/preview", json={"character": "nope"})
    assert r.status_code == 404


async def test_preview_route_400s_without_a_character_chosen(live):
    """Neither in the request body nor as `active_character`: there is nothing to listen with."""
    _, port, _ = live
    async with await client(port) as http:
        r = await http.post("/tts/preview", json={})
    assert r.status_code == 400


async def test_preview_route_400s_without_api_key(live_with_voice, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    _, port, _ = live_with_voice
    async with await client(port) as http:
        r = await http.post("/tts/preview", json={"character": "demo"})
    assert r.status_code == 400
