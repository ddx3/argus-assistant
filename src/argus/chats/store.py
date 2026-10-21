"""Chats on disk: settings, answer history, materials, frames.

A chat is one conversation together with everything that belongs to it. It gets a
folder per chat, not one shared file for all of them: a chat is deleted in one move
(`rmtree`), moved by copying the folder, and damage to one file does not take the
other chats with it.

    chats/<id>/chat.json      name, time, number of answers and the **chat settings**
    chats/<id>/history.jsonl  what happened: answers and lines, one line per event
    chats/<id>/materials/     files the chat answers from (`assist/materials.py`)
    chats/<id>/shots/<t>.jpg  frames and photos sent to the model along with answers

The settings are stored **sparsely**: only what is specific to the chat. The rest
comes from the shared ones (`argus.toml` and `argus.local.json`), as for any other
chat. So a change to a shared default reaches old chats too, instead of staying in a
copy taken on the day they were created.

`history.jsonl` is an append-only log, not a JSON array: recording an answer must not
reread and rewrite the whole history, and a process that dies mid-write corrupts
exactly one last line, which the reader skips.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argus.core import atomic

log = logging.getLogger(__name__)

META = "chat.json"
HISTORY = "history.jsonl"
MATERIALS = "materials"
SHOTS = "shots"

# How many frames we keep on disk. The page shows ten (`manager.SHOTS_KEPT`), the rest
# are unreachable, and each one is a screenshot of the person's screen of about a
# hundred kilobytes: over a long conversation that adds up to gigabytes, which also
# stay until the whole chat is deleted. We prune once `SHOTS_PRUNE_SLACK` extra frames
# have piled up: reading the history for this costs more than it is worth on every
# answer.
SHOTS_ON_DISK = 30
SHOTS_PRUNE_SLACK = 10

# Which chats we have already reported as having an unreadable file: `get` is called on
# every display of the list.
_UNREADABLE_SAID: set[str] = set()

# A name in the page address and in the path on disk: only what we generated ourselves.
# A string from the browser that passes this check cannot escape the chats folder.
ID = re.compile(r"[0-9a-f]{10}")

NAME_MAX = 80


def valid_id(chat_id: object) -> bool:
    return isinstance(chat_id, str) and ID.fullmatch(chat_id) is not None


def clean_name(value: object) -> str:
    """Chat name: no line breaks or extra spaces, no longer than `NAME_MAX`."""
    return " ".join(str(value or "").split())[:NAME_MAX]


# Where a session's name came from. "manual": the person wrote it (or renamed the
# session). "default": they skipped the name, and a placeholder stands until the app
# comes up with its own from the description of the conversation. "auto": the app came
# up with it. Only "default" is named automatically: a name written by a person, or
# one that was already generated once, is not touched again.
NAME_STATES = ("manual", "default", "auto")


@dataclass
class Chat:
    id: str
    name: str
    created: float
    updated: float
    answers: int = 0
    settings: dict[str, Any] = field(default_factory=dict)
    name_state: str = "manual"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "name_state": self.name_state,
            "created": self.created, "updated": self.updated, "answers": self.answers,
            "settings": self.settings,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], chat_id: str) -> Chat:
        """`chat_id` is the folder name, and it is the only source of the identifier.

        We do not read the `id` field inside the file: a chat folder can be brought from
        another computer, while the identifier goes to the page, into the address and into
        a path on disk. A file with a planted `id` must not control any of that.
        """
        settings = data.get("settings")
        return cls(
            id=chat_id,
            name=clean_name(data.get("name")) or "Untitled",
            created=float(data.get("created") or 0),
            updated=float(data.get("updated") or data.get("created") or 0),
            answers=int(data.get("answers") or 0),
            settings=dict(settings) if isinstance(settings, dict) else {},
            # Files without the field are sessions created before automatic naming: the person
            # wrote the name in them.
            name_state=state if (state := data.get("name_state")) in NAME_STATES else "manual",
        )


class ChatStore:
    """The chats folder. Stateless: each call goes straight to disk."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- paths -------------------------------------------------------------

    def folder(self, chat_id: str) -> Path:
        if not valid_id(chat_id):
            raise KeyError(chat_id)
        return self.root / chat_id

    # -- chats -------------------------------------------------------------

    def get(self, chat_id: str) -> Chat | None:
        if not valid_id(chat_id):
            return None
        path = self.root / chat_id / META
        # First the file itself, then its previous copy (`save` leaves it alongside): a file
        # zeroed by a power loss must not cost the person a session with its settings and
        # history.
        for source in (path, path.with_name(path.name + atomic.BACKUP_SUFFIX)):
            try:
                found = Chat.from_json(json.loads(source.read_text(encoding="utf-8")), chat_id)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                if chat_id not in _UNREADABLE_SAID:
                    _UNREADABLE_SAID.add(chat_id)
                    log.warning("session file %s: cannot be read (%s): %s", chat_id, source.name, e)
                continue
            if source is not path:
                log.warning("session opened from the previous copy of the file: %s", chat_id)
            return found
        return None

    def list(self) -> list[Chat]:
        """All chats, recent first. A broken folder is skipped (the log says so): the list must not
        disappear entirely because of one damaged file."""
        if not self.root.is_dir():
            return []
        found = [c for p in self.root.iterdir() if p.is_dir() and (c := self.get(p.name))]
        return sorted(found, key=lambda c: (c.updated, c.created), reverse=True)

    def create(
        self,
        name: str,
        make_settings: Callable[[str], dict[str, Any]],
        name_state: str = "manual",
    ) -> Chat:
        """Create a session. Settings are built from the ready folder name: they hold the path to its
        own materials."""
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            chat_id = uuid.uuid4().hex[:10]
            folder = self.root / chat_id
            try:
                folder.mkdir()
            except FileExistsError:
                continue
            now = time.time()
            chat = Chat(chat_id, clean_name(name) or "New session", now, now,
                        settings=make_settings(chat_id), name_state=name_state)
            self.save(chat)
            return chat
        raise OSError("could not find a folder name for the session")

    def save(self, chat: Chat) -> None:
        """Write the whole file atomically: the chat settings are written from the running app,
        and half a file after a crash would mean a lost chat.

        It does not create the folder; `create` does that. Otherwise a write that arrives
        after the chat was deleted (an answer finished reading after it) would resurrect a
        chat the person has just removed.
        """
        path = self.folder(chat.id) / META
        # The data is flushed to disk before the swap (`fsync`), and the previous version stays
        # alongside as a copy: after a power loss NTFS may keep the rename without the content.
        atomic.write_text(
            path, json.dumps(chat.to_json(), ensure_ascii=False, indent=2) + "\n", backup=True,
        )

    def delete(self, chat_id: str) -> None:
        shutil.rmtree(self.folder(chat_id))

    # -- history ----------------------------------------------------------

    def append(self, chat_id: str, record: dict[str, Any]) -> None:
        folder = self.folder(chat_id)
        if not folder.is_dir():
            return          # the chat was deleted while the answer was still coming in, so there is nowhere to write
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        path = folder / HISTORY
        # The previous write may have broken off in the middle of a line, with no newline at
        # the end. A line appended after it would stick to the fragment, and the reader would
        # lose both instead of one.
        torn = False
        try:
            if path.stat().st_size:
                with path.open("rb") as f:
                    f.seek(-1, os.SEEK_END)
                    torn = f.read(1) != b"\n"
        except OSError:
            pass
        with path.open("ab") as f:
            # `replace`: a lone surrogate in the text (a recognition glitch) cannot be encoded as
            # UTF-8, and the exception, which is not an `OSError`, would kill the history writer
            # until a restart.
            f.write((("\n" if torn else "") + line + "\n").encode("utf-8", errors="replace"))

    def history(self, chat_id: str) -> list[dict[str, Any]]:
        path = self.folder(chat_id) / HISTORY
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue            # a broken line is no reason to lose the rest
                if isinstance(item, dict):
                    out.append(item)
        return out

    # -- frames ------------------------------------------------------------

    def save_shot(self, chat_id: str, shot_id: int, jpeg: bytes) -> bool:
        """Save a frame. `False` means the chat no longer exists: we do not resurrect the folder (see
        `save`).
        """
        chat_dir = self.folder(chat_id)
        if not chat_dir.is_dir():
            return False
        folder = chat_dir / SHOTS
        folder.mkdir(exist_ok=True)
        (folder / f"{int(shot_id)}.jpg").write_bytes(jpeg)
        return True

    def load_shot(self, chat_id: str, shot_id: int) -> bytes | None:
        try:
            return (self.folder(chat_id) / SHOTS / f"{int(shot_id)}.jpg").read_bytes()
        except (OSError, ValueError, TypeError):    # no file, or the entry holds no number
            return None

    def prune_shots(self, chat_id: str, keep: int = SHOTS_ON_DISK) -> int:
        """Erase frames that none of the last `keep` answers with a frame refers to any longer (the page
        picks only the last ten anyway). Returns how many were erased.

        We read the history only when noticeably more files have accumulated than needed, otherwise
        cleanup would run on every answer."""
        try:
            files = [p for p in (self.folder(chat_id) / SHOTS).iterdir() if p.suffix == ".jpg"]
        except OSError:
            return 0
        if len(files) <= keep + SHOTS_PRUNE_SLACK:
            return 0
        wanted: set[str] = set()
        for record in reversed(self.history(chat_id)):
            shot = record.get("shot") if record.get("k") == "a" else None
            if isinstance(shot, int) and not isinstance(shot, bool):
                wanted.add(f"{shot}.jpg")
                if len(wanted) >= keep:
                    break
        removed = 0
        for p in files:
            if p.name in wanted:
                continue
            try:
                p.unlink()
                removed += 1
            except OSError:
                log.debug("frame was not deleted: %s", p.name, exc_info=True)
        return removed
