"""The audio helper: Swift and Python must say the same thing.

`macos/helper/Sources/ArgusSyscap/main.swift` (it cannot be built or run on Windows) and
`src/argus/mac/syscap.py` are written separately and agree only on a contract: which
arguments the program accepts, which codes it exits with, which events it writes, and in
what format it delivers sound. A mismatch does not fail a test here; it breaks system
sound for the user. That is why the contract is checked against the text of both files.
"""

from __future__ import annotations

import re
from pathlib import Path

from argus.mac import syscap

SWIFT = (Path(__file__).resolve().parents[1] / "macos" / "helper" / "Sources" / "ArgusSyscap" / "main.swift")


def swift() -> str:
    return SWIFT.read_text(encoding="utf-8")


def test_every_argument_python_passes_is_known_to_swift(monkeypatch):
    source = swift()
    monkeypatch.setattr(syscap, "find_helper", lambda: Path("/x/argus-syscap"))
    command = syscap.HelperProcess(pids=[1], exclude_pids=[2]).command()
    flags = {part for part in command if part.startswith("--")}
    assert flags == {"--rate", "--channels", "--pid", "--exclude-pid"}
    for flag in flags | {"--probe"}:
        assert f'"{flag}"' in source, f"Swift does not know {flag}"


def test_the_exit_codes_are_the_same_on_both_sides():
    source = swift()
    swift_codes = dict(re.findall(r"case (\w+) = (\d)", source[source.index("enum ExitCode"):]))
    assert int(swift_codes["permission"]) == syscap.EXIT_PERMISSION
    assert int(swift_codes["unsupported"]) == syscap.EXIT_UNSUPPORTED
    assert int(swift_codes["noDisplay"]) == syscap.EXIT_NO_DISPLAY
    assert int(swift_codes["ok"]) == syscap.EXIT_OK


def test_the_events_python_understands_are_the_ones_swift_writes():
    source = swift()
    for event in ("ready", "error", "stopped", "warning"):
        assert f'"event": "{event}"' in source, f"Swift does not write the event {event}"
    # The reason codes that Python answers with words must be named with the same words in
    # Swift.
    for code in ("permission", "no_display"):
        assert f'"{code}"' in source


def test_the_stream_format_is_the_one_python_resamples():
    source = swift()
    assert f"var rate = {syscap.RATE}" in source
    assert f"var channels = {syscap.CHANNELS}" in source
    assert syscap.BYTES_PER_FRAME == syscap.CHANNELS * 2, "int16 is two bytes per sample"
    assert "Int16(" in source and "32767" in source


def test_the_helper_exits_when_its_owner_closes_stdin():
    """Without this the helper would outlive a crashed app and keep holding the screen recording."""
    source = swift()
    assert "FileHandle.standardInput.read(upToCount:" in source and "isEmpty" in source
    # On a read failure `availableData` throws an Objective-C exception, which cannot be
    # caught from Swift.
    code_only = "\n".join(line.split("//")[0] for line in source.splitlines())
    assert "availableData" not in code_only


def test_the_helper_never_captures_its_owners_own_voice_by_accident():
    """`--exclude-pid` must reach the filter, otherwise the app's own voice output comes back into
    recognition.
    """
    source = swift()
    assert "excludePids.contains" in source and "excludingApplications: excluded" in source
