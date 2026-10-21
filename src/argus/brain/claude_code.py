"""Calling the model through the subscription: the development path.

Two decisions here are not obvious, and both matter.

**One long-lived session, not a process per request.** `claude -p` starts a process on
every call and pulls in the whole environment. With a latency budget of a couple of
seconds that is unacceptable, so `ClaudeSDKClient` is used, which keeps the connection
open: the startup cost is paid once.

**`setting_sources=[]`: the user's environment is not loaded.** Otherwise plugins, MCP
servers and, above all, hooks would be pulled into every request: in this setup a handler
is attached to every event, including `PreToolUse` and `PostToolUse`. A pipeline that
pokes the model would run them dozens of times a minute. The tools are disabled too: we
need text, not an agent that goes off to read files.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from argus.brain.base import Answer
from argus.core import friendly

if TYPE_CHECKING:
    from argus.core.config import BrainConfig

log = logging.getLogger(__name__)

# How long to wait for the whole reply of the session. A hung subscription process with no
# timeout would hold the lock and `Assistant._busy` until the chat changes; after the
# timeout the session is reset, and the next question brings up a new one.
CALL_TIMEOUT_S = 120.0

# What the subscription subprocess must not receive from the environment (see
# `ClaudeCodeProvider._options`).
SUBSCRIPTION_ENV = {"ANTHROPIC_API_KEY": ""}


class ClaudeCodeProvider:
    """Claude through a subscription, with a single live session."""

    name = "claude-code"

    def __init__(self, cfg: BrainConfig) -> None:
        self.cfg = cfg
        self._client = None
        # The system part the live session was started with: the session remembers it forever.
        self._system: str | None = None
        self._lock = asyncio.Lock()
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0

    def _options(self, system: str | None):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            model=self.cfg.model,
            system_prompt=system or self.cfg.system_prompt,
            # Both are empty: we need an interlocutor, not an agent. Tools would add both latency and
            # unpredictability.
            allowed_tools=[],
            tools=[],
            # Do not pull in the user's settings: hooks, plugins, MCP.
            setting_sources=[],
            permission_mode="dontAsk",
            max_turns=1,
            # The subscription stays a subscription. The subprocess inherits the environment, and with
            # `ANTHROPIC_API_KEY` in it the CLI silently switches to per-token billing; the key could
            # have come from `.env` (for the `anthropic-api` provider) or from the terminal. The SDK
            # overlays `env` on top of the inherited environment, so the variable cannot be deleted;
            # that is why it is set empty.
            env=dict(SUBSCRIPTION_ENV),
        )

    async def start(self, system: str | None = None) -> None:
        """Bring the session up in advance: otherwise the first request waits for the launch."""
        from claude_agent_sdk import ClaudeSDKClient

        async with self._lock:
            wanted = system or self.cfg.system_prompt
            if self._client is not None:
                if wanted == self._system:
                    return
                # An edit of the profile or materials (`assist/prompt.install`), or a different prompt
                # variant (with a screen frame or without): the session keeps the old text, and without a
                # new session it would not reach the model at all.
                await self._disconnect()
            client = ClaudeSDKClient(options=self._options(system))
            await client.connect()
            self._client = client
            self._system = wanted
            log.info("Claude session started (model %s)", self.cfg.model)

    async def _disconnect(self) -> None:
        client, self._client, self._system = self._client, None, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001 — closing must not crash the caller
                log.debug("Claude session closed with an error: %s", e)

    @staticmethod
    def _with_images(prompt: str, images: list[bytes]):
        """Assemble a message with images.

        A plain `query(str)` will not take an image: the streaming input form is needed, where
        the message content is a list of blocks. Verified: images do pass through the
        subscription, and the model really sees them.

        A cache point cannot be placed here, because the SDK builds the request. So the pinned
        pages go out in full on every request, and on the subscription that is a direct drain on
        the limit: fifty pages of a lecture is seventy thousand tokens per question. It works,
        but you pay for it; materials like these properly live on `anthropic-api`, where they
        are paid for once.
        """
        import base64

        def block(data: bytes) -> dict:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }

        async def stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [*(block(x) for x in images),
                                {"type": "text", "text": prompt}],
                },
            }

        return stream()

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,  # noqa: ARG002 — the subscription has no second level, nothing stronger to answer with
    ) -> Answer:
        from claude_agent_sdk import AssistantMessage, TextBlock

        if self._client is None or (system or self.cfg.system_prompt) != self._system:
            await self.start(system)

        # Pinned pages go ahead of the screen frame: the same order as in `anthropic-api`, so that
        # the model sees the same thing in both paths.
        images = [*pinned] + ([image] if image is not None else [])

        t0 = time.perf_counter()
        # Requests run strictly one at a time: there is one session, and parallel requests would
        # mix the answers up with each other.
        async with self._lock:
            try:
                client = self._client
                async with asyncio.timeout(CALL_TIMEOUT_S):
                    await client.query(
                        self._with_images(prompt, images) if images else prompt
                    )
                    parts: list[str] = []
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    parts.append(block.text)
            except Exception as e:  # noqa: BLE001 — a dropped connection must not crash the pipeline
                self.failures += 1
                log.warning("request to Claude failed: %s", e)
                if isinstance(e, TimeoutError):
                    # A hung session: an unread answer would get mixed up with the next question.
                    await self._disconnect()
                return Answer(
                    text="", provider=self.name, model=self.cfg.model,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    error=friendly.explain(e),
                )

        elapsed = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += elapsed
        return Answer(
            text="".join(parts).strip(),
            latency_ms=elapsed,
            provider=self.name,
            model=self.cfg.model,
        )

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        """Nothing to warm: the cache is managed by the subscription itself.

        A cache point cannot be placed here, because the SDK builds the request, not us. In
        exchange a live session works: it keeps the system part on its side and does not rebuild
        it for every question. The no-op is here exactly so that the caller does not have to ask
        which provider is on.
        """
        return

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    async def close(self) -> None:
        await self._disconnect()
