"""The whole admin: a real server, real requests.

The same lesson as with the dashboard: what must be checked is not that a route exists but
that data actually comes through it. The admin also writes to disk and edits the running
pipeline; both halves break silently, so they are tested together.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from argus.app import Argus
from argus.core.config import Config
from argus.core.settings_store import LOCAL_NAME
from argus.ui.server import build


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live(tmp_path):
    """A server on a free port. Edits go to a temporary folder.

    `root` is replaced deliberately: otherwise the test would write `argus.local.json` into the
    working copy and change the project settings of whoever ran the tests.
    """
    import uvicorn

    cfg = Config()
    cfg.root = tmp_path
    # The settings page is hidden from the user and is enabled only for development. The tests
    # are development: without the flag the panel does not come up at all, and there would be
    # nothing to check.
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

    yield app, port, tmp_path

    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


async def client(port):
    import httpx

    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20)


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------


async def test_page_and_assets_are_served(live):
    _, port, _ = live
    async with await client(port) as http:
        for path, mark in (
            # The markup and the channel blocks moved to `admin.js` (see `mount()` there); the page
            # itself now only calls it.
            ("/admin", "argusAdmin.mount"),
            ("/admin/admin.css", "--accent"),
            ("/admin/admin.js", "adminWidgets"),
        ):
            r = await http.get(path)
            assert r.status_code == 200, path
            assert mark in r.text, path


async def test_admin_page_serves_bare_when_dashboard_is_not_running(live):
    """The `argus settings` mode: `start()` was not called, there is no dashboard at all.

    There is nowhere to send anything there: the page stays itself, with its own title. The
    fixture deliberately does not call `start()` (see its docstring); this is exactly that case.
    """
    app, port, _ = live
    assert app.started is False
    async with await client(port) as http:
        r = await http.get("/admin")
        assert r.status_code == 200
        assert "argusAdmin.mount" in r.text


async def test_admin_page_redirects_into_the_dashboard_sheet_once_running(live):
    """The dashboard is up: a direct visit to `/admin` is redirected into it.

    Settings already open as a panel over the dashboard, through the same menu as "account". A
    bare page with its own title and a "save" button beside it would duplicate the same form,
    rather than being a second independent screen.
    """
    app, port, _ = live
    app.started = True  # the part of `start()` that is tested here
    async with await client(port) as http:
        r = await http.get("/admin")
        assert r.status_code in (302, 303, 307)
        assert r.headers["location"] == "/#admin"


async def test_the_sheet_itself_is_not_redirected(live):
    """The dashboard's panel loads `/admin?embedded=1`, otherwise the redirect would loop onto
    itself: the page would lead to the dashboard, the dashboard would open this same panel, and
    the panel would lead to the dashboard again.
    """
    app, port, _ = live
    app.started = True
    async with await client(port) as http:
        r = await http.get("/admin?embedded=1")
        assert r.status_code == 200
        assert "argusAdmin.mount" in r.text


async def test_dashboard_shows_the_link(live):
    """The branch has an admin panel — so the link to it must appear."""
    _, port, _ = live
    async with await client(port) as http:
        assert (await http.get("/state")).json()["admin"] is True


async def test_schema_carries_values_and_limits(live):
    _, port, _ = live
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    assert schema["channel"] == "assist"
    # The story about oneself comes first: it is the only thing the helper cannot find out by
    # itself.
    assert schema["sections"][0]["id"] == "about"

    fields = {f["path"]: f for s in schema["sections"] for f in s["settings"]}
    assert set(fields) == {
        # The main window's taskbar button: it exists on macOS but has no row (`hidden`).
        "ui.main_taskbar_button",
        "channel.assist.profile.summary",
        # Tone has its own row in the same section: it is tuned per conversation, not written once
        # like the story about oneself.
        "channel.assist.prompting.tone",
        # Materials: the files are put in the block above, and the checkbox turns them on.
        "channel.assist.materials.enabled",
        # The answer language works; the app language is waiting for translation. The recognition
        # language is a core field (`realtime_stt.language`), not a channel one: STT is shared by all
        # channels and is not the property of "assist".
        "channel.assist.profile.language",
        "channel.assist.interface.language",
        "realtime_stt.language",
        # Smart recognition is next to the language: strong accents or unclear speech call for the
        # full model instead of the lightweight one.
        "realtime_stt.smart",
        # Hotkeys: the fields exist but there is no code behind them yet; the section is marked.
        "channel.assist.hotkeys.ask",
        "channel.assist.hotkeys.hide_main",
        "channel.assist.hotkeys.hide_panel",
        "channel.assist.hotkeys.ask_screen",
        "channel.assist.hotkeys.recognition",
        # Audio is mostly not expressed by fields: it has its own block ("audio-check"): the
        # microphone is chosen from a list, and the block itself shows the levels and the check. The
        # exception is the same category of "once, per conversation" decisions: the reaction to the
        # other person and to the microphone. They cannot go into "Hotkeys": a separate check
        # there runs every value of the section through the key-combination parser.
        "channel.assist.answering.mic_mode",
        "channel.assist.answering.system_mode",
        "channel.assist.answering.wake_word",
        # "Think longer" is the same knob as on the panel by the input field: without it the "ask"
        # hotkey did not know about this setting at all.
        "channel.assist.answering.think_longer",
        # Speech playback is an experimental feature, and its section is small: switch it on and
        # choose a character created by `argus clone-voice`.
        "channel.tts.enabled",
        "channel.tts.active_character",
    }
    # The channel settings show the defaults even when the section is absent from the config. The
    # answer format is not shown as a field but has a value of its own: the `channel` section is
    # free-form, and without the channel defaults there would be a hole here.
    assert schema["values"]["channel.assist.answering.format"] == "line"


async def test_wake_word_field_depends_on_either_reply_mode(live):
    """"Bot name" is not a separate setting but the tail of the "by name" mode.

    The condition is a list, not a single pair: the field must appear if "by name" is set on at
    least one of the two speech sources (the microphone or the other person), not only on a
    strictly specified one. Without `depends_on` in the schema the page does not know when to
    hide the field; it would hang there always, even when both modes are set to "do not answer".
    """
    _, port, _ = live
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    fields = {f["path"]: f for s in schema["sections"] for f in s["settings"]}
    dep = fields["channel.assist.answering.wake_word"]["depends_on"]
    assert dep == [
        {"path": "channel.assist.answering.mic_mode", "value": "wake_word"},
        {"path": "channel.assist.answering.system_mode", "value": "wake_word"},
    ]
    assert fields["channel.assist.answering.mic_mode"]["depends_on"] is None
    assert fields["channel.assist.answering.system_mode"]["depends_on"] is None


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------


async def test_profile_edit_reaches_the_model(live):
    """The most important: what a person says about themselves reaches the prompt, not only the
    file.
    """
    app, port, root = live
    async with await client(port) as http:
        r = await http.post("/admin/apply", json={"values": {
            "channel.assist.profile.summary": "Anton, seven years of Python and asyncio.",
        }})
    assert r.json()["ok"] is True
    assert "Anton" in app.cfg.brain.system_prompt
    assert "asyncio" in app.cfg.brain.system_prompt

    saved = json.loads((root / LOCAL_NAME).read_text(encoding="utf-8"))
    assert "Anton" in saved["channel"]["assist"]["profile"]["summary"]


async def test_recognition_language_reaches_both_stt_engines(live):
    """One choice in the admin covers both recognition engines, not only the main one.

    The shadow one (`gpt_live_transcribe`) is currently off (`stt.compare_with` is empty), but
    diverging them by language silently would mean planting a question without an answer for the
    day when the comparison is needed again.
    """
    app, port, root = live
    async with await client(port) as http:
        r = await http.post("/admin/apply", json={"values": {
            "realtime_stt.language": "en",
        }})
    body = r.json()
    assert body["ok"] is True
    # The field is live: an open recognition session is reconnected by the edit itself (see
    # `SpeechPipeline.reopen_realtime`), so "restart needed" is superfluous here.
    assert "realtime_stt.language" not in body["restart"]

    assert app.cfg.realtime_stt.language == "en"
    assert app.cfg.gpt_live_transcribe.language == "en"

    saved = json.loads((root / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["realtime_stt"]["language"] == "en"


async def test_smart_recognition_is_live_and_reopens_the_session_once(live, monkeypatch):
    """The switch reaches a running conversation without a restart: the recognition session is
    reopened, exactly once, even if the language changed along with it."""
    app, port, root = live
    reopened: list[str] = []
    monkeypatch.setattr(app.speech, "reopen_realtime", reopened.append)
    assert app.cfg.realtime_stt.smart is False

    async with await client(port) as http:
        r = await http.post("/admin/apply", json={"values": {
            "realtime_stt.smart": True,
            "realtime_stt.language": "en",
        }})
    body = r.json()
    assert body["ok"] is True
    assert "realtime_stt.smart" not in body["restart"]
    assert app.cfg.realtime_stt.smart is True
    assert len(reopened) == 1

    saved = json.loads((root / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["realtime_stt"]["smart"] is True


async def test_smart_recognition_is_a_switch_in_the_schema(live):
    """Requirement: exactly a slider "smart recognition on", not a
    list.
    """
    _, port, _ = live
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()
    fields = {f["path"]: f for s in schema["sections"] for f in s["settings"]}
    assert fields["realtime_stt.smart"]["kind"] == "bool"
    assert fields["realtime_stt.smart"]["label"] == "Smart recognition"
    assert schema["values"]["realtime_stt.smart"] is False


async def test_capture_says_when_there_is_nothing_to_release(live):
    """In a browser tab there are no hotkeys, and key presses can be captured right away.

    The page waits for an answer before listening to the keyboard. Staying silent here would
    make it wait for nothing, or, worse, lie that the combinations are released when there was
    nothing to release.
    """
    _, port, _ = live
    async with await client(port) as http:
        out = (await http.post("/admin/hotkeys/capture", json={"on": True})).json()
    assert out == {"held": False}


async def test_reset_returns_the_project_settings(live):
    app, port, _ = live
    async with await client(port) as http:
        await http.post("/admin/apply", json={"values": {"gate_brain.cooldown_s": 21.0}})
        out = (await http.post("/admin/reset", json={"all": True})).json()
    assert out["ok"] is True
    assert app.cfg.gate_brain.cooldown_s == 8.0
    assert app.assistant.gate.settings.cooldown_s == 8.0


# --------------------------------------------------------------------------
# The channel's own checks
# --------------------------------------------------------------------------


async def test_prompt_preview_counts_the_price(live):
    """The profile goes into every request — the price must be visible next to the text."""
    _, port, _ = live
    async with await client(port) as http:
        await http.post("/admin/apply", json={"values": {
            "channel.assist.profile.summary": "Seven years of Python, ran a payment gateway.",
        }})
        out = (await http.get("/admin/assist/prompt")).json()
    assert "payment gateway" in out["text"]
    assert out["tokens"] > 0


async def test_page_shows_only_general_settings(live):
    """The panel must stay small.

    It had grown to eight sections and fifty-odd fields, half of which described what the
    channel does not have, and the other half is operated from the control panel. The threshold
    here is not pedantry: a setting you cannot scroll to is a setting nobody uses.
    """
    _, port, _ = live
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    fields = [f for s in schema["sections"] for f in s["settings"]]
    # It was 5, plus "Voice replies (test)": a small section of two fields
    # (enable, choose a character), not a form of synthesis settings. Two more came with chats:
    # "When to answer", split out of "Audio" (that is a conversation
    # setting, not hardware), and "App" (the app language is common, not separate for
    # each chat).
    assert len(schema["sections"]) <= 8
    assert len(fields) <= 20

    # Nothing about the overlay or the recognition thresholds: the first does not exist yet, the
    # second were measured and are set.
    paths = {f["path"] for f in fields}
    assert not [p for p in paths if p.startswith(("channel.assist.overlay.",
                                                  "vad.", "ocr.", "stt."))]

    # Hotkeys have left this shelf: there is code behind them. The "not done" badge was removed
    # along with it, and that is what is tested here, because forgetting to remove it is the
    # cheapest mistake, and it lies in exactly the opposite direction: a person does not use what
    # works.
    from argus.hotkeys import parse

    hotkeys = next(s for s in schema["sections"] if s["id"] == "hotkeys")
    assert not hotkeys["badge"]
    for setting in hotkeys["settings"]:
        parse(schema["values"][setting["path"]])


async def test_nothing_on_the_page_says_interview(live):
    """The channel is the same at an interview, an exam and a call.

    The word "interview" in a field caption means that at an exam the person would decide they
    had opened the wrong panel.
    """
    _, port, _ = live
    async with await client(port) as http:
        schema = (await http.get("/admin/schema")).json()

    for section in schema["sections"]:
        for field in section["settings"]:
            assert "interview" not in (field["label"] + field["hint"]).lower(), field["path"]


# --------------------------------------------------------------------------
# Materials
# --------------------------------------------------------------------------


async def upload(http, name: str, data: bytes) -> dict:
    r = await http.post("/admin/assist/materials", params={"name": name}, content=data)
    return r.json()


async def test_uploaded_lecture_reaches_the_prompt(live):
    """This is why the block exists: a file is put in, and the model answers from it."""
    _, port, tmp_path = live
    async with await client(port) as http:
        r = await upload(http, "01-indexes.md", "A B-tree keeps the order".encode())
        assert r["ok"] and r["items"][0]["name"] == "01-indexes.md"
        assert r["tokens"] > 0

        prompt = (await http.get("/admin/assist/prompt")).json()["text"]
    assert "A B-tree keeps the order" in prompt
    assert (tmp_path / "materials" / "01-indexes.md").is_file()


async def test_unreadable_format_is_refused_with_a_reason(live):
    """A person reads "did not appear in the list" as a broken page."""
    _, port, _ = live
    async with await client(port) as http:
        r = await upload(http, "lecture.rar", b"...")
    assert not r["ok"]
    assert ".pdf" in r["error"]


async def test_the_name_cannot_climb_out_of_the_folder(live):
    """Otherwise "../../argus.toml" would be written exactly where it was asked."""
    _, port, tmp_path = live
    async with await client(port) as http:
        await upload(http, "../../hijacked.md", "text".encode())
    assert (tmp_path / "materials" / "hijacked.md").is_file()
    assert not (tmp_path.parent / "hijacked.md").exists()


async def test_deleting_takes_it_out_of_the_prompt_too(live):
    """The file is gone, but the model still answers from it — the worst of the silent outcomes."""
    _, port, tmp_path = live
    async with await client(port) as http:
        await upload(http, "lecture.md", "a secret topic".encode())
        r = (await http.post("/admin/assist/materials/delete",
                             json={"name": "lecture.md"})).json()
        assert r["items"] == []
        prompt = (await http.get("/admin/assist/prompt")).json()["text"]
    assert "a secret topic" not in prompt
    assert not (tmp_path / "materials" / "lecture.md").exists()
