"""The `.env` that goes into the build must carry only the key the client still really needs
(Realtime STT). The others (the language-model provider, ElevenLabs, Discord) stay on the
relay server and do not travel with the build to everyone who downloads it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
sys.path.insert(0, str(PACKAGING))

from stage import _filtered_env, _git_commit, build_version, stage  # noqa: E402

PROJECT_ROOT = PACKAGING.parent


def test_only_openai_key_survives(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "OPENAI_API_KEY=sk-real-openai\n"
        "ANTHROPIC_API_KEY=sk-ant-real\n"
        "ELEVENLABS_API_KEY=sk-eleven-real\n"
        "DISCORD_BOT_TOKEN=real-token\n",
        encoding="utf-8",
    )

    out = _filtered_env(env)

    assert "OPENAI_API_KEY=sk-real-openai" in out
    assert "ANTHROPIC_API_KEY" not in out
    assert "ELEVENLABS_API_KEY" not in out
    assert "DISCORD_BOT_TOKEN" not in out


def test_empty_without_the_kept_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-real\n", encoding="utf-8")

    assert _filtered_env(env) == ""


def test_stage_replaces_developer_local_settings_with_just_the_voices(tmp_path):
    """A distributed build must not carry someone else's hotkeys, microphone and profile.
    Only the backup copy kept for developer convenience (core/settings_store.py) leaves them
    behind, and it also quietly puts them back into `dist/Argus` on every test run of the
    `.exe`, before the folder is archived.

    The voices, however, must stay: without them there is nothing to turn the voice plan on
    with. The characters are created by `argus sync-voices`, not by `argus.toml`.
    """
    project = tmp_path / "project"
    app = project / "dist" / "Argus"
    app.mkdir(parents=True)
    (app / "argus.local.json").write_text(
        '{"hotkeys": "foreign", "audio": {"mic_device": "foreign microphone"}}',
        encoding="utf-8",
    )
    backup = app.parent / ".Argus.argus.local.json.backup"
    backup.write_text('{"hotkeys": "foreign"}', encoding="utf-8")

    stage(project, app)

    assert not backup.exists()
    result = json.loads((app / "argus.local.json").read_text(encoding="utf-8"))
    assert result == {
        "channel": {
            "tts": {
                "characters": json.loads(
                    (PACKAGING / "default_voices.json").read_text(encoding="utf-8")
                )
            }
        }
    }
    assert "hotkeys" not in json.dumps(result)
    assert "foreign microphone" not in json.dumps(result)


def test_stage_writes_the_version_update_checks_against(tmp_path):
    """`VERSION` is "1.0+hash": a person needs the number, while `update.py` needs a string that
    changes with every build, even if the number was not touched.
    """
    app = tmp_path / "Argus"
    app.mkdir()

    stage(PROJECT_ROOT, app)

    commit = _git_commit(PROJECT_ROOT)
    assert commit, "the test expects to run inside a git repository"
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == build_version(PROJECT_ROOT)
    assert commit in (app / "VERSION").read_text(encoding="utf-8")


def test_version_still_written_without_a_git_repo(tmp_path):
    """Not being a git repository is no reason to leave the program without a version at all:
    the number from pyproject.toml, if it is nearby, is read independently of git, and
    without it `_project_version` returns "0" and does not crash.
    """
    project = tmp_path / "not-a-repo"
    app = project / "dist" / "Argus"
    app.mkdir(parents=True)

    stage(project, app)

    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0"


def test_public_stage_ships_no_env_at_all(tmp_path):
    """A build for distribution carries no `.env` at all: neither `OPENAI_API_KEY`, nor an empty
    file with foreign names.
    """
    project = tmp_path / "project"
    app = project / "dist" / "Argus"
    app.mkdir(parents=True)
    (project / ".env").write_text("OPENAI_API_KEY=sk-real-openai\n", encoding="utf-8")

    done = stage(project, app, public=True)

    assert not (app / ".env").exists()
    assert any(line.startswith(".env — not included") for line in done)
    stage(project, app)   # an ordinary build for yourself still puts the key in
    assert "sk-real-openai" in (app / ".env").read_text(encoding="utf-8")


def test_public_stage_removes_leftovers_of_settings_writes(tmp_path):
    """A test run of the program before archiving leaves debris next to the settings: a deferred
    corrupted `argus.local.json.bad-<time>` (someone else's hotkeys and profile) and an
    interrupted write `*.tmp`. They do not go into the archive for distribution; in a build for
    oneself we leave them alone."""
    project = tmp_path / "project"
    app = project / "dist" / "Argus"
    app.mkdir(parents=True)
    leftovers = [
        app / "argus.local.json.bad-20261014-101500",
        app / "argus.local.json.4242-7.tmp",
        app / "other.tmp",
    ]
    for f in leftovers:
        f.write_text('{"channel": {"assist": {"profile": {"summary": "personal"}}}}', encoding="utf-8")
    keeper = app / "notes.txt"
    keeper.write_text("not a leftover", encoding="utf-8")

    stage(project, app)                       # for yourself: nothing is erased
    assert all(f.exists() for f in leftovers)

    done = stage(project, app, public=True)

    assert not any(f.exists() for f in leftovers)
    assert keeper.exists()
    assert sum("leftover of a settings write" in line for line in done) == 3


def test_a_build_from_a_tree_with_uncommitted_changes_gets_its_own_version(tmp_path):
    """The commit hash does not change with edits in the working folder, and a published update is
    distinguished only by its version: without a timestamp the new build would look the same to
    the apps and would not be installed."""
    import re
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0"\n', encoding="utf-8")
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "first")
    clean = build_version(tmp_path)
    assert re.fullmatch(r"1\.0\+[0-9a-f]{7,}", clean), clean

    (tmp_path / "a.txt").write_text("two", encoding="utf-8")
    dirty = build_version(tmp_path)
    assert re.fullmatch(r"1\.0\+[0-9a-f]{7,}\.\d{10}", dirty), dirty
    assert dirty.startswith(clean)
