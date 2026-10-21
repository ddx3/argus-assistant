"""The "Work with the recording" screen: requests to a session's recording, their results, text
edits, export.

It works with any session, not only the open one: opening it is not needed for this
(opening turns recognition off and resets the timeline), its folder on disk is enough.
The only thing that is not allowed is touching a session in which recording is going on
right now: the log is growing, and "the whole recording" is not whole yet.

The model is called directly through the assistant's provider, like the recap: a request
is the user's decision, and the "no more often than once every N seconds" limit does not
apply to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from argus.core import friendly
from argus.recap import chat, engine, markdown, material, pdf, service
from argus.recap.results import Results, valid_id

if TYPE_CHECKING:
    from argus.app import Argus
    from argus.chats.store import Chat, ChatStore

log = logging.getLogger(__name__)

# What belongs to the session itself (`chats/manager.py: CHAT_PREFIXES`): the conversation
# context and the language.
STAGE = "channel.assist.vacancy.stage"
DESCRIPTION = "channel.assist.vacancy.description"
LANGUAGE = "channel.assist.profile.language"


@dataclass
class RecordError(Exception):
    """A refusal that must be shown to the person as is."""

    message: str
    status: int = 400

    def __str__(self) -> str:
        return self.message


def records_of(app: Argus) -> Records:
    found = getattr(app, "_records", None)
    if not isinstance(found, Records):
        found = Records(app)
        app._records = found
    return found


def public(item: dict[str, Any]) -> dict[str, Any]:
    """What the page sees of the result."""
    keep = ("id", "ts", "request", "text", "lines", "parts", "dropped")
    return {k: item[k] for k in keep if k in item}


class Records:
    def __init__(self, app: Argus) -> None:
        self.app = app
        self._busy: set[str] = set()

    # -- reading --------------------------------------------------------------

    async def snapshot(self, chat_id: str) -> dict[str, Any]:
        found, store = self._open(chat_id)
        entries = await asyncio.to_thread(material.load, store, chat_id)
        results = await asyncio.to_thread(Results(store, chat_id).all)
        stats = material.stats(e.line for e in entries)
        return {
            "chat": {"id": chat_id, "name": found.name},
            "recording": chat.recording(self.app, chat_id),
            "busy": chat_id in self._busy,
            "lines": [
                {
                    "key": e.key, "t": material.clock(e.line.ts), "who": e.line.source.value,
                    "text": e.line.text, "edited": e.edited,
                }
                for e in entries
            ],
            "results": [{**public(r), "pdf": self.pdf_info(store, chat_id, r)} for r in reversed(results)],
            "stats": {
                "lines": stats.lines, "chars": stats.chars, "tokens": stats.tokens,
                "minutes": stats.minutes,
            },
        }

    # -- request -------------------------------------------------------------

    async def request(
        self, chat_id: str, instruction: str, title: str = "", replace: str = "",
    ) -> dict[str, Any]:
        """A request to the recording. `title` is a short name for the card ("Notes"); empty
        means the request as is.

        `replace` is the id of a result to be redone: the new answer takes its place (the same `id`,
        the same place in the timeline) instead of being added alongside. Requirement: new answers
        were always being created instead of replacing the ones where the button was clicked. With
        the earlier conversation the model sees only what came BEFORE the replaced answer: that answer
        is itself what is being redone, and must not become a hint."""
        instruction = " ".join(instruction.split())
        title = " ".join(title.split())[:80]
        if not instruction:
            raise RecordError("Write what to do with the recording.")
        found, store = self._open(chat_id)
        self._not_recording(chat_id)
        results = Results(store, chat_id)
        old: dict[str, Any] | None = None
        if replace:
            old = results.get(replace) if valid_id(replace) else None
            if old is None:
                raise RecordError("That answer no longer exists.", 404)
        assistant = self.app.assistant
        if assistant is None:
            raise RecordError("The assistant is turned off.", 503)
        if chat_id in self._busy:
            raise RecordError("This session is still working on the previous request.", 409)
        self._busy.add(chat_id)
        try:
            entries = await asyncio.to_thread(material.load, store, chat_id)
            # The timeline is a real conversation: any request, both a ready-made one ("Recap",
            # "Tasks") and one in the user's own words ("shorter", "in English"), goes to the
            # model together with the earlier requests and answers. The model decides by itself whether
            # it is a new one or a continuation.
            items = await asyncio.to_thread(results.all)
            if old is not None:
                position = next((i for i, item in enumerate(items) if item["id"] == old["id"]), len(items))
                items = items[:position]
            earlier = [(item["request"], item["text"]) for item in items]
            context, language = _context_for(self.app, found)
            outcome = await engine.perform(
                assistant.provider, [e.line for e in entries], instruction,
                context=context, language=language, history=earlier,
            )
        finally:
            self._busy.discard(chat_id)
        if outcome.tiny:
            raise RecordError(outcome.text, 422)
        if not outcome.ok:
            raise RecordError(f"Didn't work: {outcome.error}", 502)
        text = outcome.text
        if outcome.dropped:
            text = service.DROPPED_NOTE.format(n=outcome.dropped) + "\n\n" + text
        fields = {"lines": outcome.lines, "parts": outcome.parts, "dropped": outcome.dropped}
        item = None
        if old is not None:
            # The ready PDF was made from the previous text, so the file and the link to it go away
            # together with it.
            item = results.update(
                old["id"], request=title or instruction, text=text, ts=time.time(), pdf=None, **fields,
            )
            if item is not None:
                self._forget_pdf(store, chat_id, old)
        if item is None:     # nothing to replace (deleted while the model was thinking): just a new one
            item = results.add(title or instruction, text, **fields)
            for evicted in results.evicted:     # evicted by the limit: their PDFs are no longer needed
                self._forget_pdf(store, chat_id, evicted)
        return public(item)

    def delete_result(self, chat_id: str, result_id: str) -> None:
        _, store = self._open(chat_id)
        results = Results(store, chat_id)
        item = results.get(result_id) if valid_id(result_id) else None
        if item is None or not results.delete(result_id):
            raise RecordError("There is no such result.", 404)
        # The ready PDF existed for the sake of this result; without it the file is garbage in the
        # session folder.
        self._forget_pdf(store, chat_id, item)

    # -- text edit -------------------------------------------------------

    def fix_line(self, chat_id: str, key: str, text: str) -> None:
        _, store = self._open(chat_id)
        self._not_recording(chat_id)
        if not material.valid_key(key):
            raise RecordError("There is no such line.", 400)
        if not " ".join(text.split()):
            raise RecordError("A line can't be empty. To remove it, press “Remove”.")
        material.Edits(store, chat_id).fix(key, text)

    def drop_line(self, chat_id: str, key: str) -> None:
        _, store = self._open(chat_id)
        self._not_recording(chat_id)
        if not material.valid_key(key):
            raise RecordError("There is no such line.", 400)
        material.Edits(store, chat_id).drop(key)

    # -- export ----------------------------------------------------------------

    def markdown_of(self, chat_id: str, result_id: str) -> tuple[str, str]:
        """(file name, text) for downloading the .md."""
        found, store = self._open(chat_id)
        item = self._result(store, chat_id, result_id)
        body = f"# {found.name}\n\n_{item['request']}_\n\n{item['text']}\n"
        return _filename(found.name, item["request"], "md"), body

    async def pdf_of(self, chat_id: str, result_id: str) -> tuple[str, Path]:
        """(file name, path) of the ready PDF. It is rebuilt for every request: the session name could
        have changed, and the file is cheap. It lies in the session folder (`pdf/`) under a clear
        name, and the name is remembered on the result: the page shows the file under the answer and
        opens it from there."""
        found, store = self._open(chat_id)
        results = Results(store, chat_id)
        item = self._result(store, chat_id, result_id)
        # The report has the heading and the text of the answer: without time references (on paper
        # they lead nowhere), without the request, the date and added notes.
        # Parsing a long answer is done in a thread: on the event loop it would freeze the windows
        # for the duration of the work.
        body = await asyncio.to_thread(lambda: markdown.to_html(markdown.strip_timecodes(item["text"])))
        document = pdf.page(title=found.name, body=body)
        name = _pdf_name(found.name, item, results.all())
        out = _pdf_dir(store, chat_id) / name
        try:
            await pdf.render(document, out)
        except pdf.PdfError as e:
            raise RecordError(str(e), 501 if str(e) in pdf.NO_BROWSER else 502) from e
        previous = item.get("pdf")
        if isinstance(previous, str) and previous != name and Path(previous).name == previous:
            _unlink_quietly(_pdf_dir(store, chat_id) / previous)
        results.update(result_id, pdf=name)
        return name, out

    def pdf_info(self, store: ChatStore, chat_id: str, item: dict[str, Any]) -> dict[str, str] | None:
        """The ready PDF of a result: name and full path (the page shows them under the answer); `None`
        if there is no file.
        """
        name = item.get("pdf")
        if not isinstance(name, str) or not name or Path(name).name != name:
            return None
        path = _pdf_dir(store, chat_id) / name
        return {"name": name, "path": str(path)} if path.is_file() else None

    async def open_pdf(self, chat_id: str, result_id: str) -> dict[str, str]:
        """Open the PDF in the default program: "click on the file and it opens". If there is no file,
        build it first.
        """
        _, store = self._open(chat_id)
        item = self._result(store, chat_id, result_id)
        info = self.pdf_info(store, chat_id, item)
        if info is None:
            name, path = await self.pdf_of(chat_id, result_id)
            info = {"name": name, "path": str(path)}
        await asyncio.to_thread(_launch, Path(info["path"]))
        return info

    def _forget_pdf(self, store: ChatStore, chat_id: str, item: dict[str, Any]) -> None:
        """Remove the PDF file of a result (and the old one, by id in the name, from the times before
        clear names).

        A file open in a reader is not erased (Windows); that is no reason to drop what is already
        done: the text is saved, the result is deleted, and the extra file stays until the next
        cleanup."""
        folder = _pdf_dir(store, chat_id)
        name = item.get("pdf")
        if isinstance(name, str) and name and Path(name).name == name:
            _unlink_quietly(folder / name)
        if valid_id(item.get("id")):
            _unlink_quietly(folder / f"{item['id']}.pdf")

    # -- internals -------------------------------------------------------------

    def _open(self, chat_id: str) -> tuple[Chat, ChatStore]:
        store = chat.store_of(self.app)
        found = chat.find(self.app, chat_id) if store is not None else None
        if store is None or found is None:
            raise RecordError("There is no such session.", 404)
        return found, store

    def _not_recording(self, chat_id: str) -> None:
        if chat.recording(self.app, chat_id):
            raise RecordError("Recording is on. You can work with it after it stops.", 409)

    @staticmethod
    def _result(store: ChatStore, chat_id: str, result_id: str) -> dict[str, Any]:
        item = Results(store, chat_id).get(result_id) if valid_id(result_id) else None
        if item is None:
            raise RecordError("There is no such result.", 404)
        return item


def _context_for(app: Argus, found: Chat) -> tuple[str, str]:
    """(what the person wrote about the conversation, language rule): the session's own, and what it
    lacks comes from the shared settings."""
    context, language = service._context(app)
    own = found.settings or {}
    mine = " — ".join(part for part in (str(own.get(STAGE) or ""), str(own.get(DESCRIPTION) or "")) if part)
    if mine:
        context = mine
    if LANGUAGE in own:
        try:
            from argus.assist.prompt import LANGUAGE_RULES

            language = LANGUAGE_RULES.get(str(own[LANGUAGE] or ""), LANGUAGE_RULES.get("", ""))
        except ImportError:
            pass
    return context, language


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _filename(name: str, request: str, extension: str) -> str:
    """A file name from the session title and the request, the way a person would name them."""
    parts = [" ".join(_UNSAFE.sub(" ", part).split()) for part in (name, request)]
    base = " — ".join(part for part in parts if part)[:90].rstrip(" .")
    return f"{base or 'recording'}.{extension}"


def _pdf_dir(store: ChatStore, chat_id: str) -> Path:
    return store.folder(chat_id) / "pdf"


def _pdf_name(chat_name: str, item: dict[str, Any], everyone: list[dict[str, Any]]) -> str:
    """PDF file name: session title and request. If the PDF of another answer is already named that
    way (two identical requests), the id is appended to the name, otherwise the second would
    overwrite the first."""
    base = _filename(chat_name, item["request"], "pdf")
    taken = {other.get("pdf") for other in everyone if other["id"] != item["id"]}
    if base not in taken:
        return base
    return f"{base[: -len('.pdf')]} ({item['id']}).pdf"


def _unlink_quietly(path: Path) -> None:
    """Erase the file, and if it does not yield (busy with a reader), write to the log and move on."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("file was not deleted (%s): %s", path.name, e)


def _launch(path: Path) -> None:
    """Open a file with the default program, like a double click in Explorer."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606  (the path is ours, inside the session folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        # The system error text contains a code and the full path in the user's profile: it goes to
        # the log, not to the screen.
        log.warning("file did not open (%s): %s", path.name, e)
        raise RecordError(friendly.plain_or(e, "Couldn't open the file."), 500) from e
