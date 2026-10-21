"""The bridge to sessions (`argus/chats/`): which one is open, where its folder is, what is
in it.

The only place where the recap and the recording know about chats. The import is lazy,
and "no chat" is `None`, not an exception: chats are a separate and fast-changing part of
the app, and the recap must survive its rework and, with no chats at all, keep working
from the live timeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argus.app import Argus
    from argus.chats.store import Chat, ChatStore


def active(app: Argus) -> tuple[str, ChatStore] | None:
    """(id of the open chat, its store), or `None` if no chat is open."""
    try:
        from argus.chats import chats_of
    except ImportError:
        return None
    manager = chats_of(app)
    chat_id = manager.active_id if manager is not None else None
    return (chat_id, manager.store) if chat_id else None


def store_of(app: Argus) -> ChatStore | None:
    """The store of all sessions: for working with any of them, not only the open one."""
    try:
        from argus.chats.store import ChatStore
    except ImportError:
        return None
    return ChatStore(app.cfg.chats_dir)


def find(app: Argus, chat_id: str) -> Chat | None:
    """A session by id or `None`: there is no such session, the id is invalid, or there are no chats
    at all.
    """
    store = store_of(app)
    return store.get(chat_id) if store is not None else None


def recording(app: Argus, chat_id: str) -> bool:
    """Whether a recording is going on in this session right now. There is one recognition per app:
    it runs in the open session and only in it."""
    found = active(app)
    return bool(found and found[0] == chat_id and getattr(app, "running", False))
