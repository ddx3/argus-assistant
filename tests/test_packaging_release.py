"""Build and seeds: the version is computed once, a "for distribution" build inherits
nothing foreign, seeds do not overwrite the person's files.

From the 2026-10-14 audit: `build_macos.sh` computed the version twice (a minute-level
stamp diverged when the working folder had edits), so the release description and the
package did not match and the app "updated" on every launch; `stage --public` merely did
not copy `.env`, while what was in the folder (sign-in, log, `argus.legal.json`) it left
in place; the Mac seeds could overwrite the person's `.env`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(PACKAGING))

import stage  # noqa: E402

from argus import host  # noqa: E402


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "models").mkdir(parents=True)
    (project / "argus.toml").write_text("[brain]\n", encoding="utf-8")
    (project / "models" / "silero_vad.onnx").write_bytes(b"onnx")
    (project / ".env").write_text("OPENAI_API_KEY=sk-real-openai-key-0123456789\n", encoding="utf-8")
    return project


# --------------------------------------------------------------------------
# version: once
# --------------------------------------------------------------------------


def git_repo_with_a_dirty_tree(path: Path) -> None:
    def git(*args):
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)

    git("init", "-q")
    (path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0"\n', encoding="utf-8")
    (path / "a.txt").write_text("one", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "first")
    (path / "a.txt").write_text("two", encoding="utf-8")


def test_two_calls_in_different_minutes_disagree_unless_the_version_is_pinned(tmp_path, monkeypatch):
    """That was the bug: the Mac build computed the version for the release description and then
    again for `VERSION` inside the package.
    """
    git_repo_with_a_dirty_tree(tmp_path)
    monkeypatch.delenv(stage.FULL_VERSION_ENV, raising=False)
    stamps = iter([".2610030700", ".2610030702"])
    monkeypatch.setattr(stage.time, "strftime", lambda fmt: next(stamps))
    assert stage.build_version(tmp_path) != stage.build_version(tmp_path)


def test_a_pinned_version_is_returned_as_is_by_every_call(tmp_path, monkeypatch):
    git_repo_with_a_dirty_tree(tmp_path)
    monkeypatch.setenv(stage.FULL_VERSION_ENV, "1.0+abc1234.2610030700")
    stamps = iter([".2610030700", ".2610030702", ".2610030703"])
    monkeypatch.setattr(stage.time, "strftime", lambda fmt: next(stamps))
    assert stage.build_version(tmp_path) == stage.build_version(tmp_path) == "1.0+abc1234.2610030700"


def test_the_version_written_into_the_seed_is_the_pinned_one(tmp_path, monkeypatch):
    import build_mac_seed

    monkeypatch.setenv(stage.FULL_VERSION_ENV, "1.0+pinned.2610030700")
    seed = tmp_path / "seed"
    build_mac_seed.build(make_project(tmp_path), seed)
    assert (seed / "VERSION").read_text(encoding="utf-8") == "1.0+pinned.2610030700"


def test_the_mac_build_script_computes_the_version_once_and_hands_it_on():
    text = (PACKAGING / "build_macos.sh").read_text(encoding="utf-8")
    first = text.index("stage.build_version")
    assert text.count("stage.build_version") == 1, "the script itself must not compute the version a second time"
    assert text.index("unset ARGUS_FULL_VERSION") < first < text.index('export ARGUS_FULL_VERSION="$FULL_VERSION"')
    assert text.index('export ARGUS_FULL_VERSION="$FULL_VERSION"') < text.index("build_mac_seed.py") < text.index("latest-mac.json")
    assert 'printf \'{"version": "%s", "zip": "Argus-mac.zip"}' in text and '"$FULL_VERSION"' in text


# --------------------------------------------------------------------------
# build "for distribution"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", [".env", "argus.account.json", "argus.legal.json", "argus.log", "update.log", "update.json", "update.lock",
             "update-result.json", "update-alive", "update-journal.json", "debug_prompt.txt"],
)
def test_a_public_stage_removes_what_a_test_run_left_in_the_folder(tmp_path, name):
    project = make_project(tmp_path)
    app = tmp_path / "dist" / "Argus"
    app.mkdir(parents=True)
    (app / name).write_text("personal", encoding="utf-8")
    done = stage.stage(project, app, public=True)
    assert not (app / name).exists()
    assert any(line.startswith(f"{name} — removed") for line in done)


def test_a_non_public_stage_leaves_the_developers_own_files_alone(tmp_path):
    project = make_project(tmp_path)
    app = tmp_path / "dist" / "Argus"
    app.mkdir(parents=True)
    (app / "argus.account.json").write_text("sign-in", encoding="utf-8")
    (app / "argus.legal.json").write_text("{}", encoding="utf-8")
    stage.stage(project, app)
    assert (app / "argus.account.json").exists() and (app / "argus.legal.json").exists()


@pytest.mark.parametrize("folder", ["sessions", "chats", "materials"])
def test_a_public_stage_refuses_to_delete_somebodys_recordings_and_stages_nothing(tmp_path, folder):
    project = make_project(tmp_path)
    app = tmp_path / "dist" / "Argus"
    (app / folder).mkdir(parents=True)
    (app / folder / "x.wav").write_bytes(b"audio")
    with pytest.raises(SystemExit, match=folder):
        stage.stage(project, app, public=True)
    assert (app / folder / "x.wav").exists(), "somebody else's recordings are untouched"
    assert not (app / "argus.toml").exists() and not (app / "VERSION").exists(), "nothing was staged before the refusal"


def test_a_public_stage_needs_an_explicit_folder_because_the_default_one_is_the_owners_own_app(monkeypatch, capsys):
    """Without `--app` the folder is `dist/Argus`, the owner's working app: a build for
    distribution would erase the sign-in and `.env` there.
    """
    monkeypatch.setattr(sys, "argv", ["stage.py", "--public"])
    assert stage.main() == 2
    assert "--app" in capsys.readouterr().err


def test_every_action_in_the_mac_workflow_is_pinned_to_a_full_commit_with_the_tag_in_a_comment():
    """A floating tag (`@v4`) moves to somebody else's commit without our knowledge, and this build
    handles the signature and the release keys.
    """
    text = (ROOT / ".github" / "workflows" / "macos.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", text, flags=re.M)
    assert uses, "the Mac build must use third-party actions"
    for ref, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.match(r"\s+#\s*v\d+(\.\d+)*\s*$", rest), f"the tag goes in a comment after the commit: {ref}{rest}"


def test_the_windows_build_carries_the_health_capability_file_the_update_script_looks_for(tmp_path):
    """The Windows update script waits for the launch mark and rolls back the swap only for builds
    with this file: its name is the same as in `argus.update`, and it is put there by `main()`
    (the Windows build), not by `stage()` (which the Mac seed-data builder also uses)."""
    from argus import update

    assert stage.HEALTH_CAP_NAME == update.HEALTH_CAP_NAME
    project = make_project(tmp_path)
    app = tmp_path / "dist" / "Argus"
    app.mkdir(parents=True)
    stage.stage(project, app, public=True)
    assert not (app / stage.HEALTH_CAP_NAME).exists(), "`stage()` is shared with the Mac build — the file must not be there"
    assert "update-alive" in stage.mark_update_health(app)
    assert (app / stage.HEALTH_CAP_NAME).is_file()
    import inspect

    assert "mark_update_health(app)" in inspect.getsource(stage.main), "`main()` (the Windows build) is what puts it there"


def test_the_git_commit_docstring_no_longer_promises_what_it_does_not_do():
    doc = stage._git_commit.__doc__ or ""
    assert "or there are uncommitted changes" not in doc, "the hash is returned with edits too — build_version adds the mark"
    assert "_has_uncommitted_changes" in doc


# --------------------------------------------------------------------------
# Mac seeds do not overwrite the person's files
# --------------------------------------------------------------------------


def test_a_seed_never_replaces_the_users_own_env(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "VERSION").write_text("2.0", encoding="utf-8")
    (seed / "argus.toml").write_text("[brain]\n", encoding="utf-8")
    (seed / ".env").write_text("OPENAI_API_KEY=from-the-seed\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text("OPENAI_API_KEY=my-key\n", encoding="utf-8")
    (data / ".seed-version").write_text("1.0", encoding="utf-8")        # the version changed: the seed files are updated
    host.seed_user_data(seed, data)
    assert (data / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=my-key\n"
    assert ".env" in host.SEED_ONCE


# --------------------------------------------------------------------------
# CI workflow
# --------------------------------------------------------------------------


def workflow() -> str:
    return (ROOT / ".github" / "workflows" / "macos.yml").read_text(encoding="utf-8")


def test_the_workflow_does_not_resolve_dependencies_where_the_apple_secrets_live():
    """Dependencies are installed in a step without secrets, while the Apple and signing keys go
    only to the build step itself.
    """
    text = workflow()
    build = text[text.index("      - name: Build\n"):text.index("      - name: Permanent signature")]
    assert "uv sync" not in build and "APPLE_APP_PASSWORD" in build
    assert "uv sync --locked" in text
    assert "uv sync --group dev" not in text, "without --locked uv silently recalculates a stale uv.lock"


def test_the_workflow_cleans_up_the_signing_keychain_and_checks_the_weights_when_it_can():
    text = workflow()
    assert "security delete-keychain" in text and "if: always() && env.HAS_CERT == 'true'" in text
    assert "shasum -a 256 -c packaging/silero_vad.sha256" in text


def test_the_windows_build_script_always_sets_both_flavor_variables_and_stages_for_distribution():
    """A forgotten ARGUS_BUILD_NO_SUBSCRIPTION put an extra executable (≈217 MiB) into the
    distributed archive: now the script sets the variables.
    """
    raw = (PACKAGING / "build_win.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "without a byte-order mark PowerShell 5.1 reads non-ASCII text in the system encoding"
    text = raw.decode("utf-8-sig")
    assert "$env:ARGUS_BUILD_API_ONLY = '1'" in text and "$env:ARGUS_BUILD_NO_SUBSCRIPTION = '1'" in text
    assert "stage.py --public" in text and "$ErrorActionPreference = 'Stop'" in text
    assert text.index("ARGUS_BUILD_NO_SUBSCRIPTION") < text.index("-m PyInstaller") < text.index("stage.py --public")
    assert "distpath (Join-Path $Out 'dist')" in text, "not in the project's dist: the developer's app may be running there"


def test_the_dependencies_the_code_imports_directly_are_declared():
    """`cv2` and `comtypes` are imported directly, but in the dependencies they were present only as
    other packages' leftovers (rapidocr, bettercam).
    """
    import tomllib

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    names = {re.split(r"[<>=;\s]", d, maxsplit=1)[0] for d in deps}
    assert {"opencv-python", "comtypes"} <= names
    assert any(d.startswith("comtypes") and "win32" in d for d in deps), "COM exists only on Windows"


def test_fastapi_is_not_older_than_the_release_that_checks_the_content_type():
    """A foreign page must not be able to send a JSON request with a simple form: the strict
    Content-Type check is in FastAPI itself, and separate tests (`test_update_service.py`) hold
    its behavior; the lower bound is the release on which this was verified."""
    import tomllib

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    floor = next(d for d in deps if d.startswith("fastapi"))
    version = tuple(int(x) for x in re.search(r">=([\d.]+)", floor).group(1).split("."))
    assert version >= (0, 141, 1), floor


# --------------------------------------------------------------------------
# Language of the system microphone prompt
# --------------------------------------------------------------------------


def ui_languages() -> list[str]:
    folder = ROOT / "src" / "argus" / "ui" / "static" / "i18n"
    return sorted(["en"] + [p.stem for p in folder.glob("*.json") if p.stem != "keys"])


def test_the_microphone_prompt_is_translated_into_every_ui_language():
    """The microphone prompt showed one Russian phrase to everyone, including people with the other
    eight UI languages.
    """
    spec = (PACKAGING / "argus_mac.spec").read_text(encoding="utf-8")
    for lang in ui_languages():
        strings = PACKAGING / "macos" / "lproj" / f"{lang}.lproj" / "InfoPlist.strings"
        text = strings.read_text(encoding="utf-8")
        match = re.fullmatch(r'"NSMicrophoneUsageDescription" = "([^"]{20,200})";\n', text)
        assert match, (lang, text)
        # Language data: only the Russian and Ukrainian texts are written in Cyrillic.
        has_cyrillic = bool(re.search("[а-яА-ЯіїєґІЇЄҐ]", match.group(1)))
        assert has_cyrillic == (lang in ("ru", "uk")), f"{lang}: the language of the text is wrong"
        assert f'"{lang}"' in spec.split("LANGUAGES = (")[1].split(")")[0], f"{lang} is not in the build list"
    assert '"NSMicrophoneUsageDescription":\n            "Argus listens to the microphone' in spec, "the main text is English"
    assert '"CFBundleLocalizations": list(LANGUAGES)' in spec and '"CFBundleDevelopmentRegion": "en"' in spec
