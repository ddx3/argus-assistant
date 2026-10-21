"""Publishing the Windows build (`deploy/win/publish_win.py`): the only gate between the
working folder and other people.

From the 2026-10-14 audit: no file in `tests/` ran the gate, and the check showed green
where the "trial run" was answered by the owner's live program, while `argus.legal.json`
and `claude.exe` slipped into the archive. Here there is the real `verify` function over
fake build folders, `smoke` with a fake process and a real http server on a port, and the
release number and the rollback with a fake `ssh`.
"""

from __future__ import annotations

import http.server
import json
import re
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY / "win"))
sys.path.insert(0, str(DEPLOY))

import publish_win  # noqa: E402

REAL = b"the-real-openai-value-0123456789abcdef"
TOML = '[ocr]\nenabled = false\n\n[update]\nserver = "https://arguspilot.com"\n'


def make_app(tmp_path: Path, **override) -> Path:
    app = tmp_path / "dist" / "Argus"
    (app / "_internal" / "argus" / "ui" / "static").mkdir(parents=True)
    (app / "models").mkdir()
    (app / "Argus.exe").write_bytes(b"MZ" + b"\0" * 200)
    (app / "VERSION").write_text("1.0+abc", encoding="utf-8")
    (app / "argus.toml").write_text(TOML, encoding="utf-8")
    (app / "argus.local.json").write_text('{"channel": {"tts": {"characters": {}}}}', encoding="utf-8")
    (app / "models" / "silero_vad.onnx").write_bytes(b"onnx")
    (app / "_internal" / "argus" / "ui" / "static" / "legal-terms.html").write_text("<p>ok</p>", encoding="utf-8")
    for relative, data in override.items():
        path = app / relative.replace("\\", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return app


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    """Values from the project's `.env` are not read in tests: a deliberately foreign value is
    substituted.
    """
    monkeypatch.setattr(publish_win, "secrets_from_env", lambda: [REAL])


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def test_a_clean_build_passes(tmp_path):
    assert publish_win.verify(make_app(tmp_path)) == "1.0+abc"


def test_the_health_capability_file_is_allowed_and_its_absence_is_warned_about(tmp_path, capsys):
    """The update script waits for the launch mark and rolls back the swap only for builds with
    `update-alive.v1` (`stage.py`).
    """
    publish_win.verify(make_app(tmp_path / "with", **{"update-alive.v1": "1"}))
    assert "has no update-alive.v1" not in capsys.readouterr().out
    publish_win.verify(make_app(tmp_path / "without"))
    assert "has no update-alive.v1" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["update-journal.json", "update-result.json", "update-alive", "update.lock"])
def test_the_update_working_files_never_ship(tmp_path, name):
    with pytest.raises(SystemExit, match="personal data"):
        publish_win.verify(make_app(tmp_path, **{name: "x"}))


@pytest.mark.parametrize("name", ["argus.account.json", "argus.log", "update.log", "update.json", "debug_prompt.txt"])
def test_personal_files_are_refused(tmp_path, name):
    with pytest.raises(SystemExit, match="personal data"):
        publish_win.verify(make_app(tmp_path, **{name: "x"}))


@pytest.mark.parametrize("name", ["sessions/s1/rec.wav", "chats/c1/chat.json", "materials/lecture.pdf"])
def test_personal_folders_are_refused(tmp_path, name):
    with pytest.raises(SystemExit, match="personal data"):
        publish_win.verify(make_app(tmp_path, **{name: "x"}))


def test_the_acknowledged_recording_notice_cannot_ship_pre_accepted(tmp_path):
    """`argus.legal.json` could ship in the archive: for every other person the first notice would
    already be "accepted".
    """
    with pytest.raises(SystemExit, match="argus.legal.json"):
        publish_win.verify(make_app(tmp_path, **{"argus.legal.json": '{"acknowledged": true}'}))


def test_any_stray_top_level_file_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="notes.txt"):
        publish_win.verify(make_app(tmp_path, **{"notes.txt": "x"}))


def test_an_env_file_anywhere_is_refused(tmp_path):
    with pytest.raises(SystemExit, match=r"\.env"):
        publish_win.verify(make_app(tmp_path, **{"_internal/x/.env": "OPENAI_API_KEY=whatever"}))
    with pytest.raises(SystemExit, match="env"):
        publish_win.verify(make_app(tmp_path / "b", **{"prod.env": "A=b"}))


@pytest.mark.parametrize("path", ["_internal/claude_agent_sdk/__init__.py", "_internal/claude_agent_sdk/_bundled/claude.exe",
                                  "_internal/ctranslate2/x.dll"])
def test_a_build_of_the_wrong_flavor_is_refused(tmp_path, path):
    """A forgotten ARGUS_BUILD_NO_SUBSCRIPTION put an extra executable (≈217 MiB) into the archive,
    and nobody noticed.
    """
    with pytest.raises(SystemExit, match="wrong build flavor"):
        publish_win.verify(make_app(tmp_path, **{path: "x"}))


def test_the_screen_reader_must_be_absent_when_it_is_switched_off(tmp_path):
    with pytest.raises(SystemExit, match="rapidocr"):
        publish_win.verify(make_app(tmp_path, **{"_internal/rapidocr/__init__.py": "x"}))
    app = make_app(tmp_path / "second", **{"_internal/rapidocr/__init__.py": "x"})
    (app / "argus.toml").write_text(TOML.replace("enabled = false", "enabled = true"), encoding="utf-8")
    assert publish_win.verify(app)


def test_a_planted_key_like_string_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="looks like a key"):
        publish_win.verify(make_app(tmp_path, **{"_internal/cfg.txt": "k = sk-proj-" + "A1b2C3d4" * 6}))


def test_formats_the_old_scan_missed_are_refused(tmp_path):
    for i, planted in enumerate(["sk_" + "0123456789abcdef" * 3, "MTA0NzM5NTQzMjE5ODc2NTQzMg.GaBcDe." + "x" * 27,
                                 "-----BEGIN PRIVATE KEY-----\nabc", "LIQPAY_PRIVATE_KEY=abcdefghijklmnopqrstuvwxyz123456"]):
        with pytest.raises(SystemExit):
            publish_win.verify(make_app(tmp_path / f"case{i}", **{"_internal/cfg.txt": planted}))


def test_the_real_env_value_inside_a_binary_or_compressed_code_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="real value"):
        publish_win.verify(make_app(tmp_path / "a", **{"_internal/x.dll": b"MZ\0\0" + b"\0" * 64 + REAL}))
    module = b"import os\nKEY = '" + REAL + b"'\n"
    with pytest.raises(SystemExit, match="real value"):
        publish_win.verify(make_app(tmp_path / "b", **{"Argus.exe": b"MZ\0\0" + b"\0" * 64 + zlib.compress(module)}))


def test_a_file_over_five_megabytes_is_scanned_too(tmp_path):
    with pytest.raises(SystemExit, match="real value"):
        publish_win.verify(make_app(tmp_path, **{"_internal/big.bin": b"\0" * 6_000_000 + REAL}))


def test_only_voices_may_stay_in_the_local_settings(tmp_path):
    with pytest.raises(SystemExit, match="more than voices"):
        publish_win.verify(make_app(tmp_path, **{"argus.local.json": '{"hotkeys": "foreign"}'}))


def test_a_personal_update_server_override_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="personal override"):
        publish_win.verify(make_app(tmp_path, **{"argus.toml": TOML + "# LOCAL DEV OVERRIDE\n"}))
    with pytest.raises(SystemExit, match="personal override"):
        publish_win.verify(make_app(tmp_path / "b", **{"argus.toml": '[ocr]\nenabled = false\n[update]\nserver = "http://127.0.0.1:1"\n'}))


def test_placeholders_in_the_legal_pages_stop_the_release_unless_allowed(tmp_path, capsys):
    page = {"_internal/argus/ui/static/legal-privacy.html": '<p>Write to <a href="mailto:TODO-email">us</a></p>'}
    with pytest.raises(SystemExit, match="with placeholders instead of"):
        publish_win.verify(make_app(tmp_path, **page))
    assert publish_win.verify(make_app(tmp_path / "b", **page), allow_legal_placeholders=True) == "1.0+abc"
    assert "WARNING" in capsys.readouterr().out


def test_a_build_without_any_legal_pages_is_refused(tmp_path):
    app = make_app(tmp_path)
    (app / "_internal" / "argus" / "ui" / "static" / "legal-terms.html").unlink()
    with pytest.raises(SystemExit, match="no terms and privacy pages"):
        publish_win.verify(app)


# --------------------------------------------------------------------------
# smoke
# --------------------------------------------------------------------------


class FakeProc:
    pid = 4321

    def __init__(self, polls):
        self.polls = list(polls)

    def poll(self):
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]


class Backend:
    """A real http server on a free port: it answers `/build` with whatever it is given."""

    def __init__(self):
        self.root = ""
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"root": outer.root}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def smoke_env(monkeypatch, tmp_path):
    backend = Backend()
    seen = SimpleNamespace(popen=None, local=None, env=None)
    monkeypatch.setattr(publish_win, "free_port", lambda: backend.port)
    monkeypatch.setattr(publish_win, "time", SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(publish_win.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))

    def launch(polls, answer_from_copy=True):
        def fake_popen(command, cwd=None, env=None, **kwargs):
            seen.popen = (command, cwd)
            seen.env = env
            seen.local = json.loads((Path(cwd) / "argus.local.json").read_text(encoding="utf-8"))
            backend.root = str(Path(cwd) / "_internal" / "argus" / "ui" / "static") if answer_from_copy else str(tmp_path / "dev" / "static")
            return FakeProc(polls)

        monkeypatch.setattr(publish_win.subprocess, "Popen", fake_popen)

    yield SimpleNamespace(launch=launch, seen=seen, backend=backend, app=make_app(tmp_path))
    backend.stop()


def test_the_copy_answers_on_its_own_port(smoke_env, capsys):
    smoke_env.launch([None])
    publish_win.smoke(smoke_env.app, timeout=5)
    assert smoke_env.seen.local["ui"]["port"] == smoke_env.backend.port and smoke_env.seen.local["update"]["server"] == ""
    assert "window" not in smoke_env.seen.local.get("ui", {}), "the built program does not read this key"
    assert smoke_env.seen.env["ARGUS_NO_LIVE"] == "1", "the pages are its own, not from the sources next to it"
    assert "this exact copy responds" in capsys.readouterr().out
    assert smoke_env.app.parent.exists() and not list(smoke_env.app.parent.glob("argus-smoke-*")), "the copy is removed"


def test_another_program_on_the_port_does_not_pass_for_the_build(smoke_env):
    """Before, the 200 answer came from the owner's running program on port 8420, while the copy
    exited at once.
    """
    smoke_env.launch([None], answer_from_copy=False)
    with pytest.raises(SystemExit, match="something other than a copy"):
        publish_win.smoke(smoke_env.app, timeout=5)


def test_a_build_that_dies_at_once_fails_the_smoke(smoke_env):
    smoke_env.launch([3])
    smoke_env.backend.stop()
    with pytest.raises(SystemExit, match="did not respond"):
        publish_win.smoke(smoke_env.app, timeout=5)


def test_a_build_that_answers_and_then_exits_fails_the_smoke(smoke_env):
    smoke_env.launch([None, 1])
    with pytest.raises(SystemExit, match="immediately exited"):
        publish_win.smoke(smoke_env.app, timeout=5)


# --------------------------------------------------------------------------
# release number, previous release and rollback
# --------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path, release_signer):
    monkeypatch.setattr(publish_win.update_trust, "load_signing_seed", lambda: release_signer.seed)
    commands: list[str] = []
    answers: dict[str, str] = {}

    def fake_ssh(command, timeout=600):
        commands.append(command)
        for needle, answer in answers.items():
            if needle in command:
                return answer
        return ""

    monkeypatch.setattr(publish_win, "ssh", fake_ssh)
    monkeypatch.setattr(publish_win.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(publish_win, "check_public", lambda *a, **k: None)
    return SimpleNamespace(commands=commands, answers=answers, signer=release_signer, tmp=tmp_path)


def manifest_of(command: str) -> dict:
    return json.loads(re.search(r"printf '%s' '(\{.*?\})'", command).group(1))


def test_the_upload_keeps_the_previous_release_and_signs_the_new_number(server):
    zip_path = server.tmp / "Argus-win.zip"
    zip_path.write_bytes(b"zip-bytes" * 10)
    server.answers["stat -c"] = str(zip_path.stat().st_size)
    server.answers["sha256sum"] = publish_win.sha256(zip_path)
    publish_win.upload(zip_path, "1.0+new", 5000, live={"version": "1.0+old", "build": 4000})
    final = server.commands[-1]
    assert "cp -p Argus-win.zip Argus-win.prev.zip" in final and "cp -p latest.json latest.prev.json" in final
    assert final.index("cp -p Argus-win.zip") < final.index("mv -f Argus-win.zip.part Argus-win.zip")
    manifest = manifest_of(final)
    assert manifest["build"] == 5000 and manifest["version"] == "1.0+new"
    assert publish_win.update_trust.problem_with_manifest(manifest, "win") == ""


def test_republishing_the_same_version_does_not_overwrite_the_real_previous_release(server):
    zip_path = server.tmp / "Argus-win.zip"
    zip_path.write_bytes(b"zip-bytes" * 10)
    server.answers["stat -c"] = str(zip_path.stat().st_size)
    server.answers["sha256sum"] = publish_win.sha256(zip_path)
    publish_win.upload(zip_path, "1.0+same", 5001, live={"version": "1.0+same", "build": 5000})
    assert ".prev" not in server.commands[-1]


def test_a_rollback_republishes_the_previous_build_under_a_new_number(server, monkeypatch):
    """Apps that had already installed the broken release would treat a return under the old number
    as a rollback and ignore it.
    """
    prev_hash = "ab" * 32
    prev = {"version": "1.0+good", "zip": "Argus-win.zip", "sha256": prev_hash, "size": 123, "build": 3000, "sig": "x"}
    monkeypatch.setattr(publish_win, "fetch_live", lambda: ({"version": "1.0+bad", "build": 4000}, None))
    server.answers["cat "] = json.dumps(prev)
    server.answers["sha256sum"] = prev_hash
    server.answers["stat -c"] = "123"
    publish_win.rollback()
    final = server.commands[-1]
    assert "cp -p Argus-win.prev.zip Argus-win.zip.part" in final
    manifest = manifest_of(final)
    assert manifest["version"] == "1.0+good" and manifest["sha256"] == prev_hash and manifest["size"] == 123
    assert manifest["build"] > 4000, "newer than the spoiled one that was published"
    assert publish_win.update_trust.problem_with_manifest(manifest, "win") == ""


def test_a_rollback_with_a_damaged_previous_archive_is_refused(server, monkeypatch):
    monkeypatch.setattr(publish_win, "fetch_live", lambda: (None, None))
    server.answers["cat "] = json.dumps({"version": "1.0+good", "sha256": "ab" * 32, "size": 123})
    server.answers["sha256sum"] = "cd" * 32
    server.answers["stat -c"] = "123"
    with pytest.raises(SystemExit, match="does not match"):
        publish_win.rollback()
    assert not any("mv -f" in c for c in server.commands)


def test_the_build_number_follows_the_live_release(monkeypatch):
    assert publish_win.next_build({"build": 99_999_999_999}, now=1000.0) == 100_000_000_000
    with pytest.raises(SystemExit, match="clock"):
        publish_win.next_build(None, now=time.time() + 7200, server_now=time.time())
