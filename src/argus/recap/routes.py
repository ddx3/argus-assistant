"""Recap routes for the page: topics and the "Summarize" button.

The recap itself is not returned to the page: it arrives as a card in the timeline, in
the same state snapshot as all the answers (`service.py`). Here is only what cannot be
done through the question line: learning and changing the topics without words, and
pressing the button.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from argus.recap.service import recap_of

if TYPE_CHECKING:
    from argus.app import Argus

log = logging.getLogger(__name__)


def attach(web: FastAPI, app: Argus) -> None:
    # The "Work with the recording" screen (`record_routes.py`) is hooked up with the same call:
    # `ui/server.py` has one line about the recap, and it does not need a second one.
    from argus.recap import record_routes

    record_routes.attach(web, app)

    @web.get("/recap/topics")
    async def recap_topics() -> dict:
        return recap_of(app).state()

    @web.put("/recap/topics")
    async def recap_set_topics(payload: dict) -> dict:
        raw = payload.get("topics", "")
        if not isinstance(raw, str | list):
            raise HTTPException(400, "topics must be a comma-separated line or a list")
        try:
            return {"ok": True, **recap_of(app).set_topics(raw)}
        except OSError as e:
            log.warning("topics were not saved: %s", e)
            return {"ok": False, "error": "the topics weren't saved — try again"}

    @web.post("/recap")
    async def recap_run(payload: dict | None = None) -> dict:
        focus = str((payload or {}).get("topic") or "").strip()
        answer = await recap_of(app).run(focus)
        # `shown` means the card (including one with an error) has already landed in the timeline.
        # If not, the refusal happened earlier (the recap is already being prepared, the brain is
        # turned off), and it has to be reported to the button.
        return {
            "ok": answer.ok, "error": answer.error or "",
            "shown": bool(answer.meta.get("published")),
        }
