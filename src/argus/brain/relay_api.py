"""Calling the model through the account server, not directly by key.

The same as `anthropic_api.py`/`openai_api.py` (request and response, images, "think
longer"), but the key (`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`) stays on the server
(`server/relay.py`): it does not get into the build that is handed out. Which upstream to
call on our behalf is set by `cfg.brain.relay_upstream`.

There is no prompt cache here: the account server does not keep an API client alive
between the requests of two different people in the same way, so there is nothing to
warm, and `warm()` is empty.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from argus.account.client import Account, json_of
from argus.brain.base import Answer
from argus.core import friendly

if TYPE_CHECKING:
    from argus.core.config import BrainConfig

log = logging.getLogger(__name__)

# Server limits (`server/relay.py`, `server/limits.py`). It takes nothing larger: it
# refuses with 422 or, if the body is longer than 24 MB, drops the connection, and the
# person would see "something went wrong" on an honest question. The
# excess is cut before sending; the reason is told to the person by whoever assembles the
# materials (`assist/materials.py` takes these numbers from here), and this is a backup
# safety net.
MAX_SYSTEM_CHARS = 200_000
MAX_PROMPT_CHARS = 60_000
MAX_IMAGES = 42                    # total: material pages and the screen frame or photo
MAX_IMAGE_B64 = 4_000_000          # characters of base64 in one image (about 3 MB of JPEG)
MAX_IMAGES_B64 = 20_000_000        # for all images together: the request body is up to 24 MB
MAX_ANSWER_TOKENS = 16_000

# The answer was cut off by the size ceiling: the text is incomplete but paid for in full
# (`truncated` in the server's response). The note goes into the text itself: the card,
# history, PDF and copying show it the same way, and the next question sees that the
# previous answer was not finished.
TRUNCATED_NOTE = "*The answer came out too long and was cut off.*"
TRUNCATED_EMPTY = "the answer did not fit in the allotted size and came back empty — ask again"
EMPTY = "the answer came back empty"


def b64_len(n: int) -> int:
    """How many base64 characters `n` bytes will produce."""
    return (n + 2) // 3 * 4


def fit_pinned(pinned: Sequence[bytes], image: bytes | None) -> list[bytes]:
    """How many material pages fit within the server's limits, from the start, in their order.

    A page that did not fit, and all those after it, are dropped: a gap in the middle would
    spoil the order of the lecture that the prompt is built for.
    """
    room = MAX_IMAGES - (1 if image is not None else 0)
    budget = MAX_IMAGES_B64 - (b64_len(len(image)) if image is not None else 0)
    kept: list[bytes] = []
    for page in pinned:
        size = b64_len(len(page))
        if len(kept) >= room or size > MAX_IMAGE_B64 or size > budget:
            break
        kept.append(page)
        budget -= size
    return kept


async def _in_daemon_thread(fn, *args, **kwargs):
    """`asyncio.to_thread`, but the thread is a daemon: it does not hold up program exit.

    On exit the process waits for `to_thread` threads, and a request to the model now waits
    for an answer for up to 160 seconds (`RELAY_READ_TIMEOUT_S`): a window closed in the
    middle of an answer would leave a live process holding the single-instance lock, and a
    new launch would run into it. The server finishes and pays for the call without us, so
    abandoning the thread on exit loses nothing.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(setter, value) -> None:
        if not future.done():           # the waiter has already left (cancelled, timed out): nobody to deliver to
            setter(value)

    def work() -> None:
        try:
            outcome = (future.set_result, fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 — as with `to_thread`, everything goes to whoever is waiting
            outcome = (future.set_exception, e)
        try:
            loop.call_soon_threadsafe(deliver, *outcome)
        except RuntimeError:
            pass                        # the loop is already closed: the program is exiting

    threading.Thread(target=work, name="relay-ask", daemon=True).start()
    return await future


class RelayProvider:
    """A model via the account server relay. Needs a sign-in (`Account.signed_in`)."""

    name = "relay"

    def __init__(self, cfg: BrainConfig, account: Account) -> None:
        self.cfg = cfg
        self.account = account
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0

    async def start(self) -> None:
        return None

    def _pick(self, deep: bool) -> tuple[str, str]:
        if deep:
            return self.cfg.deep_model or self.cfg.api_model, self.cfg.deep_effort
        return self.cfg.api_model, self.cfg.effort

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
    ) -> Answer:
        model, effort = self._pick(deep)
        t0 = time.perf_counter()
        if image is not None and b64_len(len(image)) > MAX_IMAGE_B64:
            self.failures += 1
            return Answer(text="", provider=self.name, model=model,
                          error="the picture is too large — use a smaller one")

        # No more than the server takes (see the limits above). We cut what is not needed anyway:
        # the most important part of a request is at the end, so for one that is too long the
        # beginning is dropped.
        system = system or self.cfg.system_prompt
        if len(system) > MAX_SYSTEM_CHARS:
            log.warning("system part of the request, characters: %d — cutting to %d",
                        len(system), MAX_SYSTEM_CHARS)
            system = system[:MAX_SYSTEM_CHARS]
        if len(prompt) > MAX_PROMPT_CHARS:
            log.warning("request, characters: %d — keeping the last %d", len(prompt), MAX_PROMPT_CHARS)
            prompt = prompt[-MAX_PROMPT_CHARS:]
        pages = fit_pinned(pinned, image)
        if len(pages) < len(pinned):
            log.warning("pages of materials: %d, the server takes %d — the rest were not sent",
                        len(pinned), len(pages))

        payload = {
            "provider": self.cfg.relay_upstream,
            "model": model,
            "system": system,
            "prompt": prompt,
            "image_b64": base64.b64encode(image).decode("ascii") if image else None,
            "pinned_b64": [base64.b64encode(p).decode("ascii") for p in pages],
            "max_tokens": min(self.cfg.max_tokens, MAX_ANSWER_TOKENS),
            "effort": effort,
        }

        try:
            # In a thread, not right here: `_call` is an ordinary synchronous request, and in an
            # `async` function it holds the whole event loop while the model thinks (seconds; tens of
            # seconds for "think longer"). All that time the dashboard server stood still: neither the
            # "thinking" badge, nor the frame, nor utterances went out to the windows, and
            # everything appeared at once when the answer had already come. `_call` waits for a long
            # time (`RELAY_READ_TIMEOUT_S`): the server finishes the paid call even if we have left.
            response = await _in_daemon_thread(
                self.account._call, "POST", "/relay/brain/ask", auth=True, json=payload,
            )
            body = json_of(response)
        except Exception as e:  # noqa: BLE001 — network, limits, someone else's page: no reason to crash
            # Provider contract (`brain/base.py`): the error arrives in `Answer.error`, not as an
            # exception, otherwise it would kill the loop that waits for voice questions.
            self.failures += 1
            log.warning("model relay failed: %s: %s", type(e).__name__, e)
            return Answer(
                text="", provider=self.name, model=model,
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=friendly.explain(e),
            )

        elapsed = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += elapsed
        text = str(body.get("text", "")).strip()
        meta: dict[str, object] = {
            "input_tokens": body.get("input_tokens", 0),
            "output_tokens": body.get("output_tokens", 0),
        }
        error = None
        if body.get("truncated"):
            meta["truncated"] = True
            if text:
                text += "\n\n" + TRUNCATED_NOTE
            else:
                error = TRUNCATED_EMPTY
        elif not text:
            error = EMPTY
        return Answer(
            text=text,
            latency_ms=elapsed,
            provider=self.name,
            model=model,
            error=error,
            meta=meta,
        )

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        return None

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    async def close(self) -> None:
        return None
