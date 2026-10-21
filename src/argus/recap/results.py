"""Results of requests to the recording: what was asked and what the model answered.

They lie in `results.json` in the session folder, next to the log: when the session is
deleted, the results go too. This is a list separate from the live assistant's answers:
the answers (`history.jsonl`, `k: "a"`) are about the conversation while it was going on,
and here it is about the recording after it ended, and the "Work with the recording" page
must not mix one with the other.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from argus.core import atomic

if TYPE_CHECKING:
    from argus.chats.store import ChatStore

FILE = "results.json"

# This many results per session; old ones are evicted. Each is a text of several
# kilobytes, and the file must not grow without limit.
MAX_RESULTS = 200

ID = re.compile(r"[0-9a-f]{8}")


def valid_id(value: object) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


class Results:
    def __init__(self, store: ChatStore, chat_id: str) -> None:
        self.path: Path = store.folder(chat_id) / FILE
        # What the last `add` evicted from the list because of `MAX_RESULTS`: each result could
        # have a PDF in the session folder, and without the result nobody needs it (the caller
        # cleans up, `records`).
        self.evicted: list[dict[str, Any]] = []

    def all(self) -> list[dict[str, Any]]:
        """From old to new, in the order they were added."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [
            item for item in items
            if isinstance(item, dict) and valid_id(item.get("id")) and isinstance(item.get("text"), str)
        ]

    def get(self, result_id: str) -> dict[str, Any] | None:
        return next((item for item in self.all() if item["id"] == result_id), None)

    def add(self, request: str, text: str, **meta: Any) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex[:8], "ts": time.time(),
            "request": request, "text": text, **meta,
        }
        everyone = [*self.all(), item]
        self.evicted = everyone[:-MAX_RESULTS]
        self._save(everyone[-MAX_RESULTS:])
        return item

    def update(self, result_id: str, **fields: Any) -> dict[str, Any] | None:
        """Change the fields of a result in place: `id` and the place in the timeline stay. A `None`
        field is removed.
        """
        items = self.all()
        for item in items:
            if item["id"] == result_id:
                for key, value in fields.items():
                    if value is None:
                        item.pop(key, None)
                    else:
                        item[key] = value
                self._save(items)
                return item
        return None

    def delete(self, result_id: str) -> bool:
        items = self.all()
        kept = [item for item in items if item["id"] != result_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def _save(self, items: list[dict[str, Any]]) -> None:
        if not self.path.parent.is_dir():
            return      # the session was deleted mid-run: we do not resurrect the folder for a result
        atomic.write_text(
            self.path, json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
        )


def count(store: ChatStore, chat_id: str) -> int:
    return len(Results(store, chat_id).all())
