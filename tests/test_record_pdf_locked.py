"""A PDF under an open reader, and failures around the record files (chats-recap-3,
fe-pages-5, ui-server-16, -9).

Windows does not let you delete a file held by a reader (Acrobat and similar open files
without sharing). Before, such a PDF turned "try again", "make a PDF" and "delete" into a
bare server error after the paid request to the model had already been made: the text is
saved, but the page says "something went wrong".
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest
from test_record import _client, make_app, session

from argus.chats.store import ChatStore
from argus.recap import pdf
from argus.recap import records as records_module
from argus.recap.results import Results


def _lock_pdfs(monkeypatch) -> list[str]:
    """Any `.pdf` cannot be erased, as if a reader were holding it. Everything else is erased as
    usual.
    """
    real = pathlib.Path.unlink
    refused: list[str] = []

    def unlink(self, missing_ok=False):
        if self.suffix == ".pdf" and self.exists():
            refused.append(self.name)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(self))
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    return refused


async def _fake_render(document, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"%PDF-1.4 fake")


async def test_redo_survives_a_pdf_held_open_by_a_viewer(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "render", _fake_render)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")
        refused = _lock_pdfs(monkeypatch)

        redo = await client.post(
            f"/record/{chat_id}/request", json={"instruction": "Summary", "replace": made["id"]},
        )
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert refused, "the test did not reproduce a busy file"
    assert redo.status_code == 200 and redo.json()["ok"]
    assert [r["id"] for r in shown["results"]] == [made["id"]]      # the result is in place, not lost
    assert shown["results"][0]["pdf"] is None                       # the link to the old PDF is removed


async def test_delete_survives_a_pdf_held_open_by_a_viewer(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "render", _fake_render)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")
        _lock_pdfs(monkeypatch)

        gone = await client.delete(f"/record/{chat_id}/results/{made['id']}")
        again = await client.delete(f"/record/{chat_id}/results/{made['id']}")
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert gone.status_code == 200 and gone.json() == {"ok": True}
    assert again.status_code == 404         # deleted once: a repeat honestly says "no such thing"
    assert shown["results"] == []


async def test_a_new_pdf_next_to_a_locked_old_name_does_not_fail(tmp_path, monkeypatch):
    """The PDF name changed (the session was renamed) and the old file is held by a reader: the new
    one will lie beside it and the old one will stay, since it cannot be erased right now, but
    it must do no harm."""
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "render", _fake_render)
    store = ChatStore(app.cfg.chats_dir)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")
        found = store.get(chat_id)
        store.save(replace(found, name="Another name"))
        _lock_pdfs(monkeypatch)

        r = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")

    assert r.status_code == 200 and r.json()["pdf"]["name"] == "Another name — Summary.pdf"


async def test_the_pdf_that_cannot_be_replaced_says_to_close_it(tmp_path, monkeypatch):
    """The same file is busy and its place cannot be freed for the new one: the person is told what
    to do, not given a server error.
    """
    monkeypatch.setattr(pdf, "find_edge", lambda: pathlib.Path("edge.exe"))
    out = tmp_path / "answer.pdf"
    out.write_bytes(b"%PDF-1.4 old")
    _lock_pdfs(monkeypatch)

    with pytest.raises(pdf.PdfError) as refusal:
        await pdf.render("<p>x</p>", out)

    assert str(refusal.value) == pdf.LOCKED and "Close it" in pdf.LOCKED
    assert out.read_bytes() == b"%PDF-1.4 old"      # the old file is intact, nothing is lost


async def test_a_locked_pdf_is_a_json_answer_with_the_reason_not_a_bare_500(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "find_edge", lambda: pathlib.Path("edge.exe"))

    def printed(command, **kwargs):
        target = next(a for a in command if a.startswith("--print-to-pdf="))
        pathlib.Path(target.split("=", 1)[1]).write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf.subprocess, "run", printed)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        first = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")
        _lock_pdfs(monkeypatch)
        second = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")

    assert first.status_code == 200
    assert second.status_code == 502 and second.json() == {"ok": False, "error": pdf.LOCKED}


async def test_a_temp_profile_that_will_not_go_away_does_not_lose_the_pdf(tmp_path, monkeypatch):
    kwargs_seen: dict = {}
    real = pdf.tempfile.TemporaryDirectory

    def recording(*args, **kwargs):
        kwargs_seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pdf, "find_edge", lambda: pathlib.Path("edge.exe"))
    monkeypatch.setattr(pdf.tempfile, "TemporaryDirectory", recording)

    def printed(command, **kw):
        target = next(a for a in command if a.startswith("--print-to-pdf="))
        pathlib.Path(target.split("=", 1)[1]).write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf.subprocess, "run", printed)

    await pdf.render("<p>x</p>", tmp_path / "x.pdf")

    assert kwargs_seen.get("ignore_cleanup_errors") is True


async def test_a_viewer_that_will_not_start_gives_a_plain_phrase_not_the_system_text(
    tmp_path, monkeypatch, caplog,
):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "render", _fake_render)

    def refuse(*args, **kwargs):
        raise OSError(1155, "No application is associated with the specified file for this operation")

    # `_launch` chooses the method by the system; we break all of them so the test does not
    # depend on it.
    monkeypatch.setattr(records_module.os, "startfile", refuse, raising=False)
    monkeypatch.setattr(records_module.subprocess, "Popen", refuse)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        with caplog.at_level("WARNING"):
            r = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf/open")

    assert r.status_code == 500
    error = r.json()["error"]
    assert error == "Couldn't open the file."
    assert "WinError" not in error and "1155" not in error and str(tmp_path) not in error
    assert any("1155" in rec.getMessage() for rec in caplog.records), "the reason must be in the log"


def test_results_pushed_out_by_the_limit_are_reported_to_the_caller(tmp_path, monkeypatch):
    from argus.recap import results as results_module

    monkeypatch.setattr(results_module, "MAX_RESULTS", 2)
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    results = Results(store, chat.id)

    first = results.add("first", "text")
    assert results.evicted == []
    results.add("second", "text")
    results.add("third", "text")

    assert [item["id"] for item in results.evicted] == [first["id"]]
    assert [item["request"] for item in Results(store, chat.id).all()] == ["second", "third"]


async def test_the_pdf_of_an_evicted_result_is_removed_with_it(tmp_path, monkeypatch):
    from argus.recap import results as results_module

    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "render", _fake_render)
    monkeypatch.setattr(results_module, "MAX_RESULTS", 2)
    folder = ChatStore(app.cfg.chats_dir).folder(chat_id)
    async with await _client(app) as client:
        first = (await client.post(f"/record/{chat_id}/request", json={"instruction": "One"})).json()["result"]
        await client.post(f"/record/{chat_id}/results/{first['id']}/pdf")
        assert len(list((folder / "pdf").iterdir())) == 1

        await client.post(f"/record/{chat_id}/request", json={"instruction": "Two"})
        await client.post(f"/record/{chat_id}/request", json={"instruction": "Three"})
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert [r["request"] for r in shown["results"]] == ["Three", "Two"]
    assert list((folder / "pdf").iterdir()) == [], "the PDF of the evicted result stayed in the folder"
