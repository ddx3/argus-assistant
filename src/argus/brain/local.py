"""Calling a local model over the OpenAI-compatible protocol.

Works with anything that brings up such an endpoint: LM Studio, llama.cpp server, Ollama,
vLLM. No separate client is needed; it is plain HTTP.

The point is not saving money. After recognition, text goes up, and there is little text:
an hour of conversation is on the order of ten thousand tokens. The expensive part,
recognition, is already done locally. The local model is here for something else: a fully
offline mode where nothing leaves for the network. For conversations you do not want to
send anywhere, that is the deciding argument.

A caveat worth remembering: video memory is 8 GB, and Whisper already takes about 1.3 GB.
A model of 7-8 billion parameters in 4-bit form will fit just barely, but a vision model
will no longer fit alongside it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from argus.brain.base import Answer
from argus.core import friendly

if TYPE_CHECKING:
    from argus.core.config import BrainConfig

log = logging.getLogger(__name__)


class LocalProvider:
    """A local model through an OpenAI-compatible endpoint."""

    name = "local"

    def __init__(self, cfg: BrainConfig) -> None:
        self.cfg = cfg
        self._client = None
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0

    async def start(self, system: str | None = None) -> None:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.local_url.rstrip("/"),
                timeout=self.cfg.timeout_s,
            )

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,  # noqa: ARG002 — there is one local model, no second level
    ) -> Answer:
        if self._client is None:
            await self.start()

        # Pinned pages do not go here: a local model on eight gigabytes of video memory cannot
        # handle the vision part at all, and it would not make sense of fifty pages of a lecture
        # even with it. Materials in offline mode are text only, whatever is already in the system
        # part.
        #
        # The image format is like OpenAI's: local engines copy it. Whether the model understands
        # it depends on the model itself: a text-only one will simply ignore the block.
        if image is not None:
            import base64

            data = base64.b64encode(image).decode("ascii")
            user_content: object = [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
                {"type": "text", "text": prompt},
            ]
        else:
            user_content = prompt

        t0 = time.perf_counter()
        try:
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.cfg.local_model,
                    "messages": [
                        {"role": "system", "content": system or self.cfg.system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": self.cfg.max_tokens,
                    "temperature": 0.3,
                },
            )
            response.raise_for_status()
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001 — the endpoint may simply not be running
            self.failures += 1
            log.warning("local model did not respond: %s", e)
            return Answer(
                text="", provider=self.name, model=self.cfg.local_model,
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=friendly.explain(e),
            )

        elapsed = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += elapsed
        return Answer(
            text=text.strip(),
            latency_ms=elapsed,
            provider=self.name,
            model=self.cfg.local_model,
        )

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        """The local endpoint has no prompt cache, so there is nothing to warm."""
        return

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
