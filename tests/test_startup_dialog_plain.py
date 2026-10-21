"""The "Failed to start" window speaks like a human.

Before, the raw exception text went into it: the settings check would show the name of a
library, the path to the error and a link to its site. The full text stays in the log.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGING_DIR = Path(__file__).parent.parent / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import argus_app  # the path was just added, above

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="MessageBoxW exists only on Windows")


@pytest.fixture
def box(monkeypatch):
    import ctypes

    shown: list[str] = []
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *a: shown.append(a[1]) or 1)
    return shown


def test_a_settings_error_is_not_shown_raw(box):
    from argus.core.config import Config

    try:
        Config.model_validate({"ui": {"port": 80}})
    except Exception as e:      # noqa: BLE001 — this exact text is needed
        error = e
    argus_app.report_startup_failure(error)

    text = box[0]
    assert "pydantic" not in text and "ui.port" not in text and "greater_than" not in text
    assert "something went wrong" in text


def test_our_own_sentence_survives_without_its_path(box):
    argus_app.report_startup_failure(FileNotFoundError(
        "Settings file argus.toml not found next to the program (C:\\Users\\x\\Argus). "
        "Unpack the archive again."
    ))
    text = box[0]
    assert "Unpack the archive again" in text
    assert "C:\\Users" not in text


def test_a_plain_reason_is_shown_as_is(box):
    argus_app.report_startup_failure(RuntimeError("the port is busy"))
    assert "the port is busy" in box[0]
