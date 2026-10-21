"""Lay out next to the built program what it reads from disk.

This is not "copying files just in case". Each of the three items is a breakage that would
otherwise happen for a stranger on first launch, and would look like "the app does not open".

* `argus.toml` — without it the program takes the core defaults, that is, local Whisper, and
  goes off to download one and a half gigabytes of weights.
* `.env` — only for your own build. Model answers, voice-over and recognition go through
  `server/relay.py` and `server/relay_stt.py` (see `[brain] provider`, `[channel.tts] relay`,
  `[realtime_stt] relay` in `argus.toml`) — they need no key on this computer, and
  distributing it with the build means distributing access to your own account. Your own
  build (without `--public`) gets `OPENAI_API_KEY`: it will be needed if the developer turns
  off the recognition relay or removes `[account] server`. The other keys (Anthropic,
  ElevenLabs, Discord) do not go into the build at all.
* `models/silero_vad.onnx` — two megabytes that the program would otherwise pull from the
  network at startup. A trifle until the moment there is no network.

Separately — what is not simply copied from here but built anew. `argus.local.json` and its
backup (`core/settings_store.py`) exist for the convenience of development: rebuilding the
`.exe` cleans `dist`, and without the backup every rebuild run would erase the personal
settings of whoever builds — microphone, hotkeys, profile. The same mechanism turns into a
leak as soon as the built folder is distributed: the person it is given to gets somebody
else's hotkeys and profile instead of clean defaults, and the developer's very first test
run of the `.exe` before archiving `dist/Argus` puts them there through the backup
automatically.

Throwing the file out entirely was the first attempt to fix this — and it took the voice-over
voices along with it (`channel.tts.characters`): the characters deliberately live not in
`argus.toml` (they are created by the command `argus sync-voices`, not by hand), but without
them a person who gets the voice plan turned on has nothing to turn on — there is nothing to
choose a character from. `default_voices.json` next to it is the list of ElevenLabs voices
taken by `argus sync-voices` (without a key, without personal data: a voice is not the same
as a microphone or a hotkey); update it with the same `sync-voices` and copy it again when the
list changes. `stage()` writes `argus.local.json` anew, with exactly these voices and nothing
else.

Run after the build:

    uv run python packaging/stage.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import sys
import tomllib
from pathlib import Path

VAD_NAME = "silero_vad.onnx"
LOCAL_SETTINGS_NAME = "argus.local.json"
DEFAULT_VOICES_NAME = "default_voices.json"
VERSION_NAME = "VERSION"

# The version can be set from outside: the Mac build script computes it once and hands it to
# everyone who asks (see `build_version`).
FULL_VERSION_ENV = "ARGUS_FULL_VERSION"

# What cannot be in the folder of a build for distribution (`public=True`): personal files are
# left there from a test run of the program before archiving. Files are removed (they are
# copies, not working data), but folders with recordings are not: if there is anything in them,
# we refuse.
PUBLIC_FORBIDDEN_FILES = (
    ".env", "argus.account.json", "argus.legal.json", "argus.log", "update.log", "update.json", "update.lock",
    "update-result.json", "update-alive", "update-journal.json", "debug_prompt.txt",
)

# The same, but by name pattern, in the root of the build folder: debris from settings writes
# of a test run. A damaged `argus.local.json` is set aside next to it as
# `argus.local.json.bad-<time>` (it holds somebody else's hotkeys and profile), and an
# interrupted write leaves `argus.local.json.<process>-<thread>.tmp` (`core/settings_store.py`);
# `stage()` rebuilds `argus.local.json` itself, but did not know these neighbors — and they
# went into the archive.
PUBLIC_FORBIDDEN_PATTERNS = (f"{LOCAL_SETTINGS_NAME}.bad-*", "*.tmp")

# "This build reports its launch" (`argus.update.HEALTH_CAP_NAME` — the same name, a test
# checks it): the Windows update script waits for the mark and rolls back the swap if there is
# none, only for a build with this file. It is put there by `main()` (the Windows build), not
# by `stage()`: the Mac seed-data builder uses `stage()` too, and its swap script is its own.
HEALTH_CAP_NAME = "update-alive.v1"

PUBLIC_FORBIDDEN_DIRS = ("sessions", "chats", "materials")


def _git_commit(project: Path) -> str:
    """Short hash of the current commit (HEAD). Empty string if this is not a git repository. Whether
    there are uncommitted changes is not checked here; see `_has_uncommitted_changes`."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _has_uncommitted_changes(project: Path) -> bool:
    """There are uncommitted changes in tracked files. Not a git repository or git unavailable: no."""
    try:
        done = subprocess.run(["git", "-C", str(project), "diff", "--quiet", "HEAD"], capture_output=True)
        return done.returncode == 1
    except OSError:
        return False


def _project_version(project: Path) -> str:
    """For a person — "1.0", not a hash. Edited by hand in `pyproject.toml` on every release that is
    worth showing as new: a small deploy fix may not change the number, but `VERSION` does not
    stop being newer than the previous one because of that — uniqueness is the job of the commit
    hash glued on below.
    """
    try:
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0"


def build_version(project: Path) -> str:
    """`"1.0+7cd1c96"` — what `update.py` compares as is and what the settings page splits into
    "1.0" for the person and the hash for support. Without the hash, two runs with the same
    version number (an edit not in pyproject.toml but anywhere else) would look identical to
    `update.py`, and auto-update would not notice them.

    `ARGUS_FULL_VERSION` in the environment is a ready string, and we return it as is. A
    timestamp precise to the minute changes between two calls when the working folder has edits:
    the Mac build computed the version twice (for the release description and for `VERSION` inside
    the package), the strings diverged, and the app "updated" on every launch. Now it is computed
    once.
    """
    fixed = os.environ.get(FULL_VERSION_ENV, "").strip()
    if fixed:
        return fixed
    number = _project_version(project)
    commit = _git_commit(project)
    if not commit:
        return number
    version = f"{number}+{commit}"
    if _has_uncommitted_changes(project):
        # A build from a working folder with edits: the commit hash is the same but the content is
        # different. Without a timestamp, such a build would look to `update.py` like the one already
        # published (discovered on 2026-10-13: the commit was the same, and version `1.0+ea40195` was
        # already on the server), and apps would not install it. A clean build (CI) gets no timestamp.
        version += time.strftime(".%y%m%d%H%M")
    return version

# The only key an own build may need on this computer: recognition without the relay
# (`realtime_stt.relay = false` or no `[account] server`) goes to OpenAI directly
# (`perception/stt/realtime_openai.py`). Model answers and voice-over go through
# `server/relay.py` — there is no reason to keep ANTHROPIC_API_KEY/ELEVENLABS_API_KEY here:
# it would be an account that belongs to someone else, open to everyone who gets the build.
KEPT_ENV_KEYS = ("OPENAI_API_KEY",)


def _filtered_env(src: Path) -> str:
    kept = []
    for line in src.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        name = stripped.split("=", 1)[0]
        if name in KEPT_ENV_KEYS:
            kept.append(line)
    return "\n".join(kept) + "\n" if kept else ""


def stage(project: Path, app: Path, weights: Path | None = None, public: bool = False) -> list[str]:
    """Lay out the files; return what was done — for the report.

    `public=True` — a build for distribution: `.env` is not copied at all (not even
    `OPENAI_API_KEY`), as in the Mac build, where there is none in the project. Speech
    recognition in such a build goes through the account server relay (`realtime_stt.relay` in
    `argus.toml`): the person needs a login to the account and money on the balance, but no key.
    """
    done: list[str] = []
    if not app.is_dir():
        raise SystemExit(f"built program not found: {app}")

    if public:
        # `public` used to only "not copy" `.env`: but whatever lay in the folder from a test run
        # (`.env`, the login, the log, `argus.legal.json` with the accepted notice) went into the
        # archive — only the checks at publish time stopped it.
        kept = [name for name in PUBLIC_FORBIDDEN_DIRS if (app / name).exists()]
        if kept:
            raise SystemExit(
                f"the distribution build folder contains personal files: {', '.join(kept)} - these are someone's records and data, not deleting them myself; "
                f"rebuild the program in a clean folder ({app})")
        for name in PUBLIC_FORBIDDEN_FILES:
            if (app / name).is_file():
                (app / name).unlink()
                done.append(f"{name} — removed (it must not be in a distribution build)")
        for pattern in PUBLIC_FORBIDDEN_PATTERNS:
            for leftover in sorted(app.glob(pattern)):
                if leftover.is_file():
                    leftover.unlink()
                    done.append(f"{leftover.name} — removed (leftover of a settings write; it must not be in a distribution build)")

    src = project / "argus.toml"
    if src.exists():
        shutil.copy2(src, app / "argus.toml")
        done.append("argus.toml")
    else:
        done.append("argus.toml — not in the project, skipped")

    env_src = project / ".env"
    if public:
        done.append(".env — not included (distribution build: no keys inside)")
    elif env_src.exists():
        (app / ".env").write_text(_filtered_env(env_src), encoding="utf-8")
        done.append(f".env — kept only {', '.join(KEPT_ENV_KEYS)}")
    else:
        done.append(".env — not in the project, skipped")

    models = app / "models"
    models.mkdir(exist_ok=True)
    source = (weights or project / "models") / VAD_NAME
    if source.exists():
        shutil.copy2(source, models / VAD_NAME)
        done.append(f"models/{VAD_NAME}")
    else:
        done.append(f"{VAD_NAME} not found in {source.parent} — the program will download it itself")

    local_settings = app / LOCAL_SETTINGS_NAME
    voices_src = Path(__file__).resolve().parent / DEFAULT_VOICES_NAME
    if voices_src.exists():
        voices = json.loads(voices_src.read_text(encoding="utf-8"))
        clean = {"channel": {"tts": {"characters": voices}}}
        local_settings.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        done.append(f"{LOCAL_SETTINGS_NAME} — rebuilt from scratch: only {len(voices)} voices, nothing personal")
    elif local_settings.exists():
        local_settings.unlink()
        done.append(f"{LOCAL_SETTINGS_NAME} — removed ({DEFAULT_VOICES_NAME} not found, nothing to give out)")

    backup = app.parent / f".{app.name}.{LOCAL_SETTINGS_NAME}.backup"
    if backup.exists():
        backup.unlink()
        done.append(f"{backup.name} — removed (otherwise the next test run would put it back into the build)")

    version = build_version(project)
    (app / VERSION_NAME).write_text(version, encoding="utf-8")
    done.append(f"{VERSION_NAME} — {version}")

    return done


def mark_update_health(app: Path) -> str:
    """Put the "I report at launch" file (`HEALTH_CAP_NAME`) into the Windows build. Returns a line
    for the report.
    """
    (app / HEALTH_CAP_NAME).write_text("1", encoding="utf-8")
    return f"{HEALTH_CAP_NAME} — the build reports that it started, the update script waits for that mark and rolls back the swap without it"


def legal_warning(project: Path) -> str:
    """What is wrong with the terms and the policy in this build. Empty — all is well.

    The document pages are baked into the app (`legal/build.py` puts them into
    `ui/static/legal-*.html`). While `legal/operator.json` is not filled in, they carry yellow
    markers instead of the operator's name and email: for yourself this does not matter, but to
    distribute such a build is to show a person terms without an address to write to. Not a
    refusal but a loud warning: you build for yourself ten times a day, and failing on this
    would do more harm than good.
    """
    folder = project / "src" / "argus" / "ui" / "static"
    pages = [folder / "legal-terms.html", folder / "legal-privacy.html"]
    if not all(page.is_file() for page in pages):
        return "no terms and privacy policy in the build — run python legal/build.py"
    if any('class="todo"' in page.read_text(encoding="utf-8") for page in pages):
        return ("legal/operator.json is not filled in for the terms and policy — "
                "such a build must not be distributed (python legal/build.py --release)")
    return ""


def main() -> int:
    # The output here is not plain ASCII (dashes, ellipses, folder names), and the Windows console
    # speaks the system encoding by default: without this the script does its job and dies on the
    # last line of the report, looking broken.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    # --public — a build for distribution (without `.env`); --app FOLDER — lay out not into
    # dist/Argus but where it was built (this is how you build without touching the running app
    # from dist).
    args = sys.argv[1:]
    if "--public" in args and "--app" not in args:
        # A build for distribution erases the login, `.env` and the log in the folder, and without
        # `--app` the folder is `dist/Argus`, that is, the developer's own working app (with their
        # login and keys).
        print("--public without --app: by default this is dist/Argus, the working app of the owner, and the distribution build erases the "
              "login, .env and log in that folder. Specify the folder: --app dist/Argus", file=sys.stderr)
        return 2
    project = Path(__file__).resolve().parents[1]
    app = Path(args[args.index("--app") + 1]) if "--app" in args else project / "dist" / "Argus"
    for line in stage(project, app, public="--public" in args):
        print(" ", line)
    print(" ", mark_update_health(app))
    print(f"  done: {app}")
    warning = legal_warning(project)
    if warning:
        print(f"  WARNING: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
