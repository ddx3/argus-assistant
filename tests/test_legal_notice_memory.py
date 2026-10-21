"""A notice mark that could not be written to disk lives in memory.

A full disk (C: has already reached 100%), a read-only folder or an antivirus: the file
`argus.legal.json` is not written. Before, `confirm_notice()` put the mark in memory,
while `notice_done()` read only the file, and "Got it, turn on" went
into a loop: the notice window opened again after every press, and recognition did not
switch on.
"""

from __future__ import annotations

from pathlib import Path

from argus.account.legal import NOTICE_VERSION, STORE_NAME, Acks


def test_a_failed_write_is_remembered_for_this_run(tmp_path):
    acks = Acks(tmp_path / "no" / "such" / "folder")
    assert acks.notice_done() is False

    acks.confirm_notice()

    assert acks.notice_done() is True


def test_a_failed_write_stops_asking_for_the_notice_again(tmp_path):
    acks = Acks(tmp_path / "no" / "such" / "folder")
    acks.ask()
    assert acks.asking() is True

    acks.confirm_notice()

    assert acks.asking() is False


def test_a_written_mark_is_still_read_from_the_file(tmp_path):
    first = Acks(tmp_path)
    first.confirm_notice()
    assert (tmp_path / STORE_NAME).exists()

    again = Acks(tmp_path)                       # a new launch: no memory, the file is there
    assert again.notice_done() is True


def test_the_memory_does_not_hide_an_unconfirmed_notice(tmp_path):
    assert Acks(tmp_path).notice_done() is False
    assert Acks(None).notice_done() is False


def test_the_memory_copy_keeps_the_other_marks_of_the_file(tmp_path, monkeypatch):
    (tmp_path / STORE_NAME).write_text('{"other": 1}', encoding="utf-8")
    acks = Acks(tmp_path)

    def read_only(self, *args, **kwargs):
        raise PermissionError("the folder is read-only")

    monkeypatch.setattr(Path, "write_text", read_only)
    acks.confirm_notice()

    assert acks._read() == {"other": 1, "notice": NOTICE_VERSION}
