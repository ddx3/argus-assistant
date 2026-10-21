"""Startup timestamps on macOS: a `ps` failure is remembered (audit of 2026-10-14,
core-session-15).
"""

from __future__ import annotations

import subprocess

from argus import startup


def test_a_failed_ps_is_remembered_and_not_started_again(monkeypatch):
    """Every `mark` ran `ps` again, and a hung `ps` held the thread that is trying to show the
    window for the whole wait, every time."""
    calls: list[float] = []

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired("ps", kwargs["timeout"])

    monkeypatch.setattr(startup.subprocess, "run", run)
    monkeypatch.setattr(startup, "_mac_started", None)

    assert startup._mac_process_start() is None
    assert startup._mac_process_start() is None
    assert startup._mac_process_start() is None

    assert calls == [1], "one launch and a short lifetime"


def test_a_good_answer_is_remembered_too(monkeypatch):
    calls: list[int] = []

    def run(*args, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(args, 0, stdout="Tue Oct 13 12:00:00 2026\n", stderr="")

    monkeypatch.setattr(startup.subprocess, "run", run)
    monkeypatch.setattr(startup, "_mac_started", None)

    first = startup._mac_process_start()
    second = startup._mac_process_start()

    assert first is not None and first == second and calls == [1]


def test_an_unparsable_answer_counts_as_a_failure(monkeypatch):
    monkeypatch.setattr(
        startup.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not a date", stderr=""),
    )
    monkeypatch.setattr(startup, "_mac_started", None)

    assert startup._mac_process_start() is None
    assert startup._mac_started == -1.0
