"""The Mac app's seeds carry no keys, even if a `.env` sits in the project.

The Mac version is built by CI, where there is no `.env`. But a local
`bash packaging/build_macos.sh` on a machine with a `.env` used to put a key into the DMG
and the zip: the seed builder called `stage.stage` in the "for myself" mode. Now the
seeds are always built "for distribution", and this file prevents going back to the old
behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
sys.path.insert(0, str(PACKAGING))

import build_mac_seed  # noqa: E402
import stage  # noqa: E402

KEY = "sk-real-openai-key-0123456789"


def project_with_env(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "models").mkdir(parents=True)
    (project / "argus.toml").write_text("[brain]\n", encoding="utf-8")
    (project / "models" / "silero_vad.onnx").write_bytes(b"onnx")
    (project / ".env").write_text(
        f"OPENAI_API_KEY={KEY}\nANTHROPIC_API_KEY=sk-ant-real-key-0123456789\n", encoding="utf-8")
    return project


def test_a_developers_env_never_reaches_the_seed(tmp_path):
    project = project_with_env(tmp_path)
    seed = tmp_path / "seed"

    report = build_mac_seed.build(project, seed)

    assert not (seed / ".env").exists()
    for path in seed.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
    assert any(".env — not included" in line for line in report)


def test_an_old_env_left_in_the_seed_folder_is_wiped(tmp_path):
    """The seed folder is rebuilt from scratch: whatever lay there from the last time does not make
    it into the build.
    """
    project = project_with_env(tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / ".env").write_text(f"OPENAI_API_KEY={KEY}\n", encoding="utf-8")
    (seed / "argus.account.json").write_text('{"refresh": "foreign"}', encoding="utf-8")

    build_mac_seed.build(project, seed)

    assert not (seed / ".env").exists()
    assert not (seed / "argus.account.json").exists()


def test_the_seed_is_built_as_public(tmp_path, monkeypatch):
    """The call itself matters: `public=True` is the only thing that distinguishes the distributed
    build from a build for oneself.
    """
    seen: dict = {}
    real = stage.stage

    def spy(project, app, weights=None, public=False):
        seen["public"] = public
        return real(project, app, weights, public)

    monkeypatch.setattr(stage, "stage", spy)
    build_mac_seed.build(project_with_env(tmp_path), tmp_path / "seed")
    assert seen["public"] is True


def test_the_builder_stops_if_an_env_ever_appears_in_the_seed_anyway(tmp_path, monkeypatch):
    """A safeguard in case `stage` ever changes: we fail instead of just warning."""
    def leaky(project, app, weights=None, public=False):
        (app / ".env").write_text(f"OPENAI_API_KEY={KEY}\n", encoding="utf-8")
        return []

    monkeypatch.setattr(stage, "stage", leaky)
    with pytest.raises(SystemExit, match=r"\.env"):
        build_mac_seed.build(project_with_env(tmp_path), tmp_path / "seed")
