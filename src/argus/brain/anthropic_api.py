"""Calling the Anthropic API directly by key: the production path.

It will be needed when there is something to hand out to people: the subscription does
not cover that. For now it sits ready and is switched on by a line in the config.

Prompt caching lives here too. The event timeline is built so that it is only appended to
at the end, which is exactly the shape in which a cache works: the model does not
recompute a stable beginning. The cache point is placed on the system part and on the
timeline, and the fresh question goes after it.

The cache is needed not only for the money. The system part has grown: it holds the story
about the person, and in the `assist` channel also a whole lecture. Thirty thousand tokens
paid for with a full prefill on every question are an extra second on every question, and
that second is noticeable in a live conversation. So there are two tricks here: the entry
lives an hour (`cache_ttl`), not five minutes, and it is written ahead of time (`warm`),
before the first question asked pays for it.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from argus.brain.base import Answer
from argus.core import friendly

if TYPE_CHECKING:
    from argus.core.config import BrainConfig

log = logging.getLogger(__name__)


def _image(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(jpeg).decode("ascii"),
        },
    }


class AnthropicAPIProvider:
    """Claude via the Anthropic API. Needs ANTHROPIC_API_KEY."""

    name = "anthropic-api"

    def __init__(self, cfg: BrainConfig) -> None:
        self.cfg = cfg
        self._client = None
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0
        self.cache_reads = 0

        # Warm-ups: how many times the cache was written ahead of time and how many tokens went in
        # the last time. It needs to be visible for the same reason as `cache_reads`: a warm-up
        # that silently did not work looks exactly like a warm-up that was never done, with a
        # second of delay on the first question.
        self.warms = 0
        self.warmed_tokens = 0

    async def start(self, system: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        if self._client is None:
            # Our own timeout and retry count instead of the library's (ten minutes and two retries):
            # see the same in `openai_api.start`.
            self._client = AsyncAnthropic(timeout=self.cfg.timeout_s, max_retries=1)

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
    ) -> Answer:
        if self._client is None:
            await self.start()

        # `deep_model` is empty by default, so "think" then just means a stronger effort on the
        # same model. If it is set, the request goes to a different model, and that one has its own
        # cache: the first such question in a conversation pays the price of an unwritten prefix,
        # as it would without a warm-up.
        model = (self.cfg.deep_model or self.cfg.api_model) if deep else self.cfg.api_model
        effort = self.cfg.deep_effort if deep else self.cfg.effort

        content = self._pinned(pinned)

        # The screen frame goes after the pinned pages and after the cache point: it is rewritten
        # on every request, and if it stood earlier, it would invalidate the cache of the whole
        # batch. Before the text of the question, since the model parses it better that way.
        if image is not None:
            content.append(_image(image))
        content.append({"type": "text", "text": prompt})

        t0 = time.perf_counter()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=self.cfg.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                system=self._system(system),
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:  # noqa: BLE001 — network and limit errors must not crash the pipeline
            self.failures += 1
            log.warning("API request failed: %s", e)
            return Answer(
                text="", provider=self.name, model=model,
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=friendly.explain(e),
            )

        elapsed = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += elapsed
        cached = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        self.cache_reads += cached

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return Answer(
            text=text.strip(),
            latency_ms=elapsed,
            provider=self.name,
            model=model,
            meta={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cached_tokens": cached,
            },
        )

    def _pinned(self, pinned: Sequence[bytes]) -> list[dict]:
        """Pinned pages with a cache point on the last one.

        The point goes on the last one, not on every one: there are only four per request, one is
        already taken by the system part, and the cache is read as a whole prefix anyway; a mark
        in the middle of the batch gives nothing and eats up a slot.
        """
        blocks = [_image(page) for page in pinned]
        if blocks:
            blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": self.cfg.cache_ttl}
        return blocks

    def _system(self, system: str | None) -> list[dict]:
        """The system part with a cache point: one per request and per warm-up.

        It is built in one place not for brevity. A cache is a match of prefix bytes: a warm-up
        that differs from the live request by even a space in the system text, or by the
        retention period, writes a **different** entry, and the live question still pays for a
        full prefill. The error stays silent: both sides worked without a single complaint, there
        is just twice the money and the seconds have not gone anywhere.
        """
        return [
            {
                "type": "text",
                "text": system or self.cfg.system_prompt,
                # The system part does not change for the whole session, so we cache it.
                "cache_control": {"type": "ephemeral", "ttl": self.cfg.cache_ttl},
            }
        ]

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        """Write the cache ahead of time with a request that answers nothing.

        `max_tokens=0` is the supported way: the model goes through the prefill, writes the
        cache, and returns an empty answer without spending a single output token.

        The same thinking and effort are passed as in the live request, and this is not
        over-caution: changing `thinking` or `effort` invalidates the cache just like changing
        the text itself. A "lighter" warm-up would warm the wrong entry.

        If it fails, there is only a line in the log. The warm-up speeds up the first question but
        decides nothing: everything works without it, the first answer just comes a second later.
        Crashing startup or a settings edit because of it is out of the question.
        """
        if self._client is None:
            await self.start()

        t0 = time.perf_counter()
        try:
            response = await self._client.messages.create(
                model=self.cfg.api_model,
                max_tokens=0,
                thinking={"type": "adaptive"},
                output_config={"effort": self.cfg.effort},
                system=self._system(system),
                # Pinned pages are warmed together with the system part: they are the same unchanging
                # prefix, and writing them is what costs the most, since a page as an image is fifteen
                # hundred tokens.
                messages=[{
                    "role": "user",
                    "content": [*self._pinned(pinned), {"type": "text", "text": "warm-up"}],
                }],
            )
        except Exception as e:  # noqa: BLE001 — see "only a line in the log"
            log.warning("cache not warmed: %s", e)
            return

        self.warms += 1
        self.warmed_tokens = getattr(
            response.usage, "cache_creation_input_tokens", 0
        ) or 0
        log.info(
            "cache warmed: tokens %d, %s, %.0f ms",
            self.warmed_tokens,
            self.cfg.cache_ttl,
            (time.perf_counter() - t0) * 1000,
        )

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
