"""Calling the OpenAI API directly by key: the second provider, for comparison with
Anthropic.

The prompt cache is simpler here than at Anthropic: starting with GPT-5.6, OpenAI itself
picks the cache point from the unchanging prefix of the request, so there is no need to
set it explicitly. Our job is the same as at Anthropic: keep the unchanging part (the
system prompt, the pinned pages) first and byte-for-byte identical between requests, and
the changeable part (the screen frame, the question itself) last. Warming (`warm`) exists
for the same reason: the first request with a new prefix pays for its full processing,
and that must not happen on the very first question of a conversation.

Two answer levels. An ordinary question goes to `api_model` with the `effort` effort:
fast and cheap, for the tempo of a live conversation. "Think longer" (`deep=True`,
triggered by hand from the panel) goes to `deep_model` (empty means the same `api_model`,
only with a stronger effort) with `deep_effort`: for a question where a second or two of
waiting matters less than a stronger answer.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING

from argus.brain.base import Answer
from argus.core import friendly

if TYPE_CHECKING:
    from argus.core.config import BrainConfig

log = logging.getLogger(__name__)


def _image(jpeg: bytes) -> dict:
    return {
        "type": "input_image",
        "image_url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}",
        "detail": "auto",
    }


def _text(s: str) -> dict:
    return {"type": "input_text", "text": s}


class OpenAIAPIProvider:
    """GPT via the OpenAI API. Needs OPENAI_API_KEY."""

    name = "openai-api"

    def __init__(self, cfg: BrainConfig) -> None:
        self.cfg = cfg
        self._client = None
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0
        self.cache_reads = 0
        self.warms = 0
        self.warmed_tokens = 0

    async def start(self, system: str | None = None) -> None:
        from openai import AsyncOpenAI

        if self._client is None:
            # Our own timeout and retry count, not the library's (ten minutes and two retries): a
            # half-open connection would hold `Assistant._busy` for half an hour. For "think longer"
            # the default timeout (`BrainConfig.timeout_s`) may not be enough, so raise it.
            self._client = AsyncOpenAI(timeout=self.cfg.timeout_s, max_retries=1)

    def _pick(self, deep: bool) -> tuple[str, str]:
        """Model and effort for this question: the normal level or "think"."""
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
        if self._client is None:
            await self.start()

        model, effort = self._pick(deep)

        # Pinned pages do not change from question to question, so they go first, to match the
        # previous request byte-for-byte. The screen frame is rewritten every time and stands after
        # them, but before the text of the question, since the model parses the question better
        # that way.
        content = [_image(p) for p in pinned]
        if image is not None:
            content.append(_image(image))
        content.append(_text(prompt))

        t0 = time.perf_counter()
        try:
            response = await self._client.responses.create(
                model=model,
                instructions=system or self.cfg.system_prompt,
                input=[{"role": "user", "content": content}],
                reasoning={"effort": effort},
                max_output_tokens=self.cfg.max_tokens,
                prompt_cache_key="argus-brain",
            )
        except Exception as e:  # noqa: BLE001 — network and limits must not crash the pipeline
            self.failures += 1
            log.warning("request to OpenAI failed: %s", e)
            return Answer(
                text="", provider=self.name, model=model,
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=friendly.explain(e),
            )

        elapsed = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += elapsed
        usage = response.usage
        cached = usage.input_tokens_details.cached_tokens if usage else 0
        self.cache_reads += cached

        return Answer(
            text=response.output_text.strip(),
            latency_ms=elapsed,
            provider=self.name,
            model=model,
            meta={
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "cached_tokens": cached,
            },
        )

    async def ask_stream(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
        *,
        info: dict | None = None,
    ) -> AsyncIterator[str]:
        """The same as `ask`, but in pieces as they are generated, not as one answer at the end.
        For voice-over (`tts/reply.py`): the first phrase can start being synthesized while the
        model is still finishing the rest, and not only after the whole answer is ready.

        Not part of `LLMProvider` but an optional capability: `Assistant._ask` itself checks
        whether the provider has this method (`getattr`, not a `Protocol`), and silently falls
        back to the ordinary `ask` where it does not. The other providers (`anthropic-api`,
        `claude-code`, `local`) do not define it: nothing needs to be added to them, and nothing
        breaks.

        `info` is not text but everything else that an `Answer` usually carries (`model`, token
        counts): the generator yields only pieces of a string, not an `Answer` object, and there
        is nowhere to put them except a separate output dictionary that the caller fills in.
        """
        if self._client is None:
            await self.start()

        model, effort = self._pick(deep)
        if info is not None:
            info["model"] = model

        content = [_image(p) for p in pinned]
        if image is not None:
            content.append(_image(image))
        content.append(_text(prompt))

        t0 = time.perf_counter()
        try:
            stream = await self._client.responses.create(
                model=model,
                instructions=system or self.cfg.system_prompt,
                input=[{"role": "user", "content": content}],
                reasoning={"effort": effort},
                max_output_tokens=self.cfg.max_tokens,
                prompt_cache_key="argus-brain",
                stream=True,
            )
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield event.delta
                elif event.type == "response.completed":
                    self.calls += 1
                    self.total_ms += (time.perf_counter() - t0) * 1000
                    usage = event.response.usage
                    cached = usage.input_tokens_details.cached_tokens if usage else 0
                    self.cache_reads += cached
                    if info is not None:
                        info["input_tokens"] = usage.input_tokens if usage else 0
                        info["output_tokens"] = usage.output_tokens if usage else 0
                        info["cached_tokens"] = cached
                elif event.type in ("response.failed", "response.incomplete"):
                    err = getattr(event.response, "error", None)
                    details = getattr(event.response, "incomplete_details", None)
                    reason = err.message if err else (details.reason if details else event.type)
                    raise RuntimeError(f"{event.type}: {reason}")
                elif event.type == "error":
                    raise RuntimeError(event.message)
        except Exception:  # noqa: BLE001 — the same bookkeeping as in `ask`
            self.failures += 1
            raise

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        """Write the cache ahead of time, at the ordinary level (`api_model`).

        At OpenAI the cache point is one per unchanging prefix of a specific model, not one per
        application: if `deep_model` differs from `api_model`, it has its own cache, separate
        from this warm-up. This is a rare path ("think" is not asked for on every question), and
        a second warm-up for it has not been added yet: the first question on `deep_model` simply
        pays the ordinary price of an unwritten cache, as it would without any warm-up at all.
        """
        if self._client is None:
            await self.start()

        t0 = time.perf_counter()
        try:
            response = await self._client.responses.create(
                model=self.cfg.api_model,
                instructions=system or self.cfg.system_prompt,
                input=[{
                    "role": "user",
                    "content": [*(_image(p) for p in pinned), _text("warm-up")],
                }],
                reasoning={"effort": self.cfg.effort},
                max_output_tokens=16,
                prompt_cache_key="argus-brain",
            )
        except Exception as e:  # noqa: BLE001 — see "only a line in the log" in anthropic_api
            log.warning("cache not warmed: %s", e)
            return

        self.warms += 1
        usage = response.usage
        self.warmed_tokens = usage.input_tokens_details.cache_write_tokens if usage else 0
        log.info(
            "cache warmed: tokens %d, %.0f ms",
            self.warmed_tokens, (time.perf_counter() - t0) * 1000,
        )

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
