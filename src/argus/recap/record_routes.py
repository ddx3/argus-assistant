"""Routes of the "Work with the recording" screen: the page and the data for it.

The page `/chat/<id>/record` is a separate screen for working with a session's recording;
the work screen (`/chat/<id>`) remains the place where recording happens and where a
click on the session row leads. A dedicated button in the row leads here
(`ui/static/home.html`), and from here back to the work screen goes "Continue recording".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from argus.chats.store import valid_id
from argus.recap import chat
from argus.recap.records import RecordError, records_of
from argus.recap.results import valid_id as valid_result_id

if TYPE_CHECKING:
    from argus.app import Argus

log = logging.getLogger(__name__)


def _failure(error: RecordError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error.message}, status_code=error.status)


def attach(web: FastAPI, app: Argus) -> None:
    records = records_of(app)

    def known(chat_id: str) -> None:
        if not valid_id(chat_id):
            raise RecordError("There is no such session.", 404)

    @web.get("/chat/{chat_id}/record", response_class=HTMLResponse)
    async def record_page(chat_id: str) -> Response:
        """The page itself. If there is no such session, redirect to the main page, as with the working
        screen.
        """
        if not valid_id(chat_id) or chat.find(app, chat_id) is None:
            return RedirectResponse("/", status_code=302)
        # The import is here, not at the top: `ui.server` hooks these routes up by itself, and at
        # the top of the module they would see it half-assembled.
        from argus.ui import server as ui

        return HTMLResponse(
            (ui.STATIC / "record.html").read_text(encoding="utf-8"), headers=ui.page_headers(),
        )

    @web.get("/record/{chat_id}")
    async def record_get(chat_id: str) -> Response:
        try:
            known(chat_id)
            return JSONResponse({"ok": True, **await records.snapshot(chat_id)})
        except RecordError as e:
            return _failure(e)

    @web.post("/record/{chat_id}/request")
    async def record_request(chat_id: str, payload: dict) -> Response:
        try:
            known(chat_id)
            instruction = payload.get("instruction")
            if not isinstance(instruction, str):
                raise RecordError("The request must be text.")
            title = payload.get("title") or ""
            if not isinstance(title, str):
                raise RecordError("The request title must be text.")
            replace = payload.get("replace") or ""
            if not isinstance(replace, str):
                raise RecordError("Which answer to replace must be given as an ID.")
            result = await records.request(chat_id, instruction, title, replace)
            return JSONResponse({"ok": True, "result": result})
        except RecordError as e:
            return _failure(e)

    @web.delete("/record/{chat_id}/results/{result_id}")
    async def record_delete_result(chat_id: str, result_id: str) -> Response:
        try:
            known(chat_id)
            records.delete_result(chat_id, result_id)
            return JSONResponse({"ok": True})
        except RecordError as e:
            return _failure(e)

    @web.put("/record/{chat_id}/lines/{key}")
    async def record_fix_line(chat_id: str, key: str, payload: dict) -> Response:
        try:
            known(chat_id)
            text = payload.get("text")
            if not isinstance(text, str):
                raise RecordError("A line must be text.")
            records.fix_line(chat_id, key, text)
            return JSONResponse({"ok": True})
        except RecordError as e:
            return _failure(e)

    @web.delete("/record/{chat_id}/lines/{key}")
    async def record_drop_line(chat_id: str, key: str) -> Response:
        try:
            known(chat_id)
            records.drop_line(chat_id, key)
            return JSONResponse({"ok": True})
        except RecordError as e:
            return _failure(e)

    @web.get("/record/{chat_id}/results/{result_id}.md")
    async def record_markdown(chat_id: str, result_id: str) -> Response:
        try:
            known(chat_id)
            name, body = records.markdown_of(chat_id, result_id)
        except RecordError as e:
            return _failure(e)
        return _download(body.encode("utf-8"), name, "text/markdown; charset=utf-8")

    @web.post("/record/{chat_id}/results/{result_id}/pdf")
    async def record_make_pdf(chat_id: str, result_id: str) -> Response:
        """Build the PDF and leave it in the session folder (without a download): the page shows the
        file under the answer.
        """
        try:
            known(chat_id)
            if not valid_result_id(result_id):
                raise RecordError("There is no such result.", 404)
            name, path = await records.pdf_of(chat_id, result_id)
        except RecordError as e:
            return _failure(e)
        return JSONResponse({"ok": True, "pdf": {"name": name, "path": str(path)}})

    @web.post("/record/{chat_id}/results/{result_id}/pdf/open")
    async def record_open_pdf(chat_id: str, result_id: str) -> Response:
        """Open the PDF with the default program (building it first if it does not exist yet)."""
        try:
            known(chat_id)
            if not valid_result_id(result_id):
                raise RecordError("There is no such result.", 404)
            info = await records.open_pdf(chat_id, result_id)
        except RecordError as e:
            return _failure(e)
        return JSONResponse({"ok": True, "pdf": info})

    @web.get("/record/{chat_id}/results/{result_id}.pdf")
    async def record_pdf(chat_id: str, result_id: str) -> Response:
        try:
            known(chat_id)
            if not valid_result_id(result_id):
                raise RecordError("There is no such result.", 404)
            name, path = await records.pdf_of(chat_id, result_id)
        except RecordError as e:
            return _failure(e)
        return FileResponse(path, media_type="application/pdf", filename=name)


def _download(body: bytes, filename: str, media_type: str) -> Response:
    """A file with a download. A name with Russian letters goes by RFC 5987 (`filename*`)."""
    from urllib.parse import quote

    return Response(
        body, media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )

