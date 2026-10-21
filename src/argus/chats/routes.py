"""Session routes (called `chats` in the code): list, create, rename, copy, delete.

The pages (`/` is the home page, `/chat/<id>` is the work screen) are served by
`ui/server.py`: it needs its headers and the path to the files. Only data lives here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from argus.chats.manager import ChatEditError, ChatManager
from argus.chats.store import valid_id

if TYPE_CHECKING:
    from argus.app import Argus

log = logging.getLogger(__name__)


def attach(web: FastAPI, app: Argus, manager: ChatManager) -> None:
    def known(chat_id: str) -> None:
        if not valid_id(chat_id):
            raise HTTPException(404, "there is no such session")

    @web.get("/chats")
    async def chats_list() -> dict:
        return manager.overview()

    @web.post("/chats")
    async def chats_create(payload: dict | None = None) -> dict:
        try:
            chat = manager.create(str((payload or {}).get("name") or ""))
        except OSError as e:
            # The chats folder cannot be created: the disk is full or the location is read-only.
            # We show the reason in the window where the person has just pressed "Create".
            log.warning("chat was not created: %s", e)
            return {"ok": False, "error": "couldn't create the session folder — check the disk space"}
        return {"ok": True, "chat": manager.card(chat)}

    @web.patch("/chats/{chat_id}")
    async def chats_rename(chat_id: str, payload: dict) -> dict:
        known(chat_id)
        try:
            chat = manager.rename(chat_id, str(payload.get("name") or ""))
        except ChatEditError as e:
            return {"ok": False, "error": e.message}
        except KeyError:
            raise HTTPException(404, "there is no such session") from None
        except OSError as e:
            log.warning("chat was not renamed: %s", e)
            return {"ok": False, "error": "couldn't save the name to disk"}
        return {"ok": True, "chat": manager.card(chat)}

    @web.post("/chats/{chat_id}/duplicate")
    async def chats_duplicate(chat_id: str) -> dict:
        known(chat_id)
        try:
            # In a thread: copying a session copies the materials folder (lectures, scans, hundreds
            # of megabytes), and the windows, audio and answers would otherwise stall on the event
            # loop.
            chat = await asyncio.to_thread(manager.duplicate, chat_id)
        except KeyError:
            raise HTTPException(404, "there is no such session") from None
        except OSError as e:
            log.warning("chat copy was not made: %s", e)
            return {"ok": False, "error": "couldn't make a copy — check the disk space"}
        return {"ok": True, "chat": manager.card(chat)}

    @web.delete("/chats/{chat_id}")
    async def chats_delete(chat_id: str) -> dict:
        known(chat_id)
        try:
            await manager.delete(chat_id)
        except KeyError:
            raise HTTPException(404, "there is no such session") from None
        except OSError as e:
            # The file is busy, for example open in Explorer. The folder may have been only
            # partly deleted, so we do not promise that the chat is intact: we ask the person to
            # close whatever is in the way and try again.
            log.warning("chat was not deleted: %s", e)
            return {"ok": False,
                    "error": "the session folder wasn't fully deleted — close whatever is open "
                             "from it and try again"}
        return {"ok": True}
