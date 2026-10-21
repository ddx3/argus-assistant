"""Checks for `argus doctor` and the device list for `argus devices` on macOS.

The same questions as on Windows (`cli._check_audio`, `_check_screen`), but answered
on macOS: whether the microphone hears, whether there is permission to record the screen
and system audio, and whether the assistant responds. They return
`("ok" | "warn" | "fail", words)`, and `cli.py` colors them, so the checks do not depend
on the output library.
"""

from __future__ import annotations

from typing import Any

from argus.mac import coreaudio, permissions, syscap

OK, WARN, FAIL = "ok", "warn", "fail"


def audio(cfg: Any = None) -> tuple[str, str]:
    """Microphone, permission for it, and system audio."""
    defaults = coreaudio.default_names()
    mic = defaults.get("in")
    if not mic:
        return FAIL, "no microphone found"

    parts = [f"microphone '{mic}'"]
    state = permissions.microphone()
    if state == permissions.DENIED:
        return FAIL, f"{parts[0]} — {permissions.MIC_ADVICE}"
    if state == permissions.RESTRICTED:
        return FAIL, f"{parts[0]} — {permissions.MIC_RESTRICTED}"
    if state == permissions.NOT_DETERMINED:
        parts.append("microphone permission will be requested on first launch")

    ok, why = syscap.probe()
    if ok:
        parts.append("system audio works")
        return OK, " | ".join(parts)
    parts.append(f"system audio: {why}")
    return WARN, " | ".join(parts)


def screen(cfg: Any) -> tuple[str, str]:
    """Whether the screen can be captured and whether there is permission for it."""
    state = permissions.screen_recording()
    if state == permissions.DENIED:
        return FAIL, permissions.SCREEN_ADVICE
    try:
        from argus.mac.camera import MacCamera

        camera = MacCamera(output_idx=cfg.screen.output_idx)
        frame = camera.grab()
    except Exception as e:  # noqa: BLE001 — the doctor must not crash itself
        return FAIL, f"screen capture is not working: {e}"
    if frame is None:
        return WARN, "capture created, but no frame arrived"
    h, w = frame.shape[:2]
    note = "" if state == permissions.GRANTED else " (permission could not be checked)"
    return OK, f"screen {cfg.screen.output_idx}: {w}x{h}, {frame.nbytes / 2**20:.1f} MB{note}"


def device_rows() -> list[tuple[str, str, str, str, str, str]]:
    """Rows of the device table: number, name, inputs, outputs, frequency, notes."""
    rows = []
    for index, d in enumerate(coreaudio.devices()):
        marks = []
        if d.virtual:
            marks.append("virtual")
        if d.default_input:
            marks.append("default input")
        if d.default_output:
            marks.append("default output")
        rows.append((str(index), d.name, str(d.inputs), str(d.outputs), str(d.rate), " ".join(marks)))
    return rows
