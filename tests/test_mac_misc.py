"""macOS: PDF, system language, `argus doctor` and `argus devices`.

Small places where the Windows code gives way to the macOS code. System calls are
faked; the tests check what does not depend on the system.
"""

from __future__ import annotations

import subprocess
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from argus.mac import coreaudio, doctor, permissions, syscap
from argus.recap import pdf
from argus.ui import i18n

# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


@pytest.fixture
def on_mac(monkeypatch, tmp_path):
    # We request `tmp_path` here, before the patch: once pytest learns that the platform is
    # "not Windows", it would reach for `os.getuid`, which Windows does not have.
    monkeypatch.setattr(sys, "platform", "darwin")


def test_pdf_finds_a_chromium_browser_in_applications(on_mac, monkeypatch, tmp_path):
    chrome = tmp_path / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    monkeypatch.setattr(pdf, "_mac_app_folders", lambda: (tmp_path / "missing", tmp_path))
    assert pdf.find_edge() == chrome


def test_pdf_prefers_edge_when_both_are_installed(on_mac, monkeypatch, tmp_path):
    for rel in ("Google Chrome.app/Contents/MacOS/Google Chrome",
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True)
        path.write_text("")
    monkeypatch.setattr(pdf, "_mac_app_folders", lambda: (tmp_path,))
    assert pdf.find_edge().name == "Microsoft Edge"


def test_without_a_browser_pdf_says_what_to_install(on_mac, monkeypatch, tmp_path):
    monkeypatch.setattr(pdf, "_mac_app_folders", lambda: (tmp_path,))
    assert pdf.find_edge() is None
    with pytest.raises(pdf.PdfError, match="Chrome or Edge"):
        pdf._render("<p>x</p>", tmp_path / "out.pdf")


def test_the_no_browser_messages_are_plain_text():
    from argus.core import friendly

    for text in pdf.NO_BROWSER:
        assert friendly.plain(text), text


# --------------------------------------------------------------------------
# System language
# --------------------------------------------------------------------------


def fake_foundation(monkeypatch, languages):
    module = ModuleType("Foundation")
    module.NSLocale = SimpleNamespace(preferredLanguages=lambda: languages)
    monkeypatch.setitem(sys.modules, "Foundation", module)


def test_the_mac_language_is_the_first_in_the_system_list(monkeypatch):
    fake_foundation(monkeypatch, ["ru-RU", "en-US"])
    assert i18n._mac_ui_language() == "ru"
    fake_foundation(monkeypatch, ["pt_BR"])
    assert i18n._mac_ui_language() == "pt"
    fake_foundation(monkeypatch, ["zh-Hans-CN"])
    assert i18n._mac_ui_language() == "zh"


@pytest.mark.parametrize("output, expected", [
    ('(\n    "ru-RU",\n    "en-US"\n)\n', "ru"),
    ('(\n    "uk"\n)\n', "uk"),
    ('(\n    "pl-PL"\n)', "pl"),
    ("", ""),
    ("garbage", ""),
])
def test_without_pyobjc_the_language_comes_from_defaults(monkeypatch, output, expected):
    monkeypatch.setitem(sys.modules, "Foundation", None)           # import Foundation -> ImportError
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    assert i18n._mac_ui_language() == expected


def test_a_failing_defaults_is_an_empty_language_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "Foundation", None)

    def broken(*a, **k):
        raise OSError("no defaults")

    monkeypatch.setattr(subprocess, "run", broken)
    assert i18n._mac_ui_language() == ""


def test_the_system_language_on_macos_uses_the_mac_answer(on_mac, monkeypatch):
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: "")
    monkeypatch.setattr(i18n, "_mac_ui_language", lambda: "uk")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    assert i18n.system_language() == "uk"
    assert i18n.resolve("auto") == "uk"


def test_a_finder_launched_app_without_lang_falls_back_to_english(on_mac, monkeypatch):
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: "")
    monkeypatch.setattr(i18n, "_mac_ui_language", lambda: "")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    assert i18n.resolve("auto") == "en"


# --------------------------------------------------------------------------
# argus doctor / argus devices
# --------------------------------------------------------------------------


def test_doctor_audio_fails_without_a_microphone(monkeypatch):
    monkeypatch.setattr(coreaudio, "default_names", lambda: {"out": "Speakers"})
    status, text = doctor.audio()
    assert status == doctor.FAIL and "microphone" in text


def test_doctor_audio_names_a_denied_microphone(monkeypatch):
    monkeypatch.setattr(coreaudio, "default_names", lambda: {"in": "MacBook Pro Microphone"})
    monkeypatch.setattr(permissions, "microphone", lambda: permissions.DENIED)
    status, text = doctor.audio()
    assert status == doctor.FAIL and permissions.MIC_ADVICE in text


def test_doctor_audio_is_a_warning_when_only_the_system_sound_is_missing(monkeypatch):
    monkeypatch.setattr(coreaudio, "default_names", lambda: {"in": "Mic"})
    monkeypatch.setattr(permissions, "microphone", lambda: permissions.GRANTED)
    monkeypatch.setattr(syscap, "probe", lambda: (False, "no permission"))
    status, text = doctor.audio()
    assert status == doctor.WARN and "no permission" in text and "Mic" in text


def test_doctor_audio_is_ok_when_everything_answers(monkeypatch):
    monkeypatch.setattr(coreaudio, "default_names", lambda: {"in": "Mic"})
    monkeypatch.setattr(permissions, "microphone", lambda: permissions.GRANTED)
    monkeypatch.setattr(syscap, "probe", lambda: (True, "ok"))
    assert doctor.audio()[0] == doctor.OK


def test_doctor_screen_reports_a_missing_right_first(monkeypatch):
    monkeypatch.setattr(permissions, "screen_recording", lambda: permissions.DENIED)
    status, text = doctor.screen(SimpleNamespace(screen=SimpleNamespace(output_idx=0)))
    assert status == doctor.FAIL and text == permissions.SCREEN_ADVICE


def test_doctor_screen_describes_the_frame(monkeypatch):
    monkeypatch.setattr(permissions, "screen_recording", lambda: permissions.GRANTED)
    from argus.mac import camera

    class Cam:
        def __init__(self, output_idx=0, **kw):
            pass

        def grab(self):
            return np.zeros((982, 1512, 3), dtype=np.uint8)

    monkeypatch.setattr(camera, "MacCamera", Cam)
    status, text = doctor.screen(SimpleNamespace(screen=SimpleNamespace(output_idx=0)))
    assert status == doctor.OK and "1512x982" in text


def test_doctor_screen_does_not_crash_the_doctor(monkeypatch):
    monkeypatch.setattr(permissions, "screen_recording", lambda: permissions.GRANTED)
    from argus.mac import camera

    class Broken:
        def __init__(self, **kw):
            raise RuntimeError("no screen")

    monkeypatch.setattr(camera, "MacCamera", Broken)
    status, text = doctor.screen(SimpleNamespace(screen=SimpleNamespace(output_idx=0)))
    assert status == doctor.FAIL and "no screen" in text


def test_device_rows_mark_the_defaults_and_the_virtual_ones(monkeypatch):
    monkeypatch.setattr(coreaudio, "devices", lambda: [
        coreaudio.AudioDevice(1, "MacBook Pro Microphone", "u1", 1, 0, 48000, default_input=True),
        coreaudio.AudioDevice(2, "BlackHole 2ch", "u2", 2, 2, 48000, virtual=True),
    ])
    rows = doctor.device_rows()
    assert rows[0][1] == "MacBook Pro Microphone" and "default input" in rows[0][5]
    assert "virtual" in rows[1][5]


def test_the_cli_uses_the_mac_checks_on_macos(on_mac, monkeypatch):
    from argus import cli

    monkeypatch.setattr(doctor, "audio", lambda cfg=None: ("warn", "test"))
    monkeypatch.setattr(doctor, "screen", lambda cfg: ("fail", "test2"))
    assert cli._check_audio(None) == (cli.WARN, "test")
    assert cli._check_screen(None) == (cli.FAIL, "test2")


def test_the_devices_command_prints_the_mac_table(on_mac, monkeypatch):
    from typer.testing import CliRunner

    from argus import cli

    monkeypatch.setattr(doctor, "device_rows", lambda: [("0", "Test microphone", "1", "0", "48000", "")])
    result = CliRunner().invoke(cli.app, ["devices"])
    assert result.exit_code == 0 and "Test microphone" in result.output


# --------------------------------------------------------------------------
# The main thread
# --------------------------------------------------------------------------


def test_a_call_from_the_main_thread_runs_at_once_without_the_event_loop(monkeypatch):
    import sys

    from argus.mac import mainthread

    monkeypatch.setitem(sys.modules, "PyObjCTools", None)        # the import would have failed: the loop's queue is not needed
    assert mainthread.run_in_main_thread(lambda: 42) == 42


def test_a_call_from_another_thread_waits_for_the_event_loop_and_gets_the_answer(monkeypatch):
    import sys
    import threading
    import time
    from types import SimpleNamespace

    from argus.mac import mainthread

    queue: list = []
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=queue.append)))
    got: dict = {}

    def caller():
        try:
            got["value"] = mainthread.run_in_main_thread(lambda: "answer", timeout=3.0)
        except BaseException as e:  # noqa: BLE001
            got["error"] = e

    worker = threading.Thread(target=caller)
    worker.start()
    deadline = time.monotonic() + 3.0
    while not queue and time.monotonic() < deadline:
        time.sleep(0.01)
    assert queue, "the work is queued on the loop"
    queue[0]()                                    # the "main thread" reached it
    worker.join(3.0)
    assert got == {"value": "answer"}


def test_a_loop_that_never_comes_is_a_timeout_and_an_error_inside_comes_back(monkeypatch):
    import sys
    import threading
    import time
    from types import SimpleNamespace

    from argus.mac import mainthread

    queue: list = []
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=queue.append)))
    errors: list = []

    def stuck():
        try:
            mainthread.run_in_main_thread(lambda: 1, timeout=0.05)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=stuck)
    t.start()
    t.join(3.0)
    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)

    def failing():
        raise ValueError("failed inside")

    queue.clear()
    box: list = []

    def caller():
        try:
            mainthread.run_in_main_thread(failing, timeout=3.0)
        except BaseException as e:  # noqa: BLE001
            box.append(e)

    t = threading.Thread(target=caller)
    t.start()
    deadline = time.monotonic() + 3.0
    while not queue and time.monotonic() < deadline:
        time.sleep(0.01)
    assert queue
    queue[0]()
    t.join(3.0)
    assert len(box) == 1 and isinstance(box[0], ValueError)
