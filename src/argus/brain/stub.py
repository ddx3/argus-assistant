"""A stub instead of the model, for checking the pipeline without a token bill.

`claude-code` and `anthropic-api` both cost money on every question, and often something
else needs checking: whether the answer card is drawn, whether the "thinking..."
badge goes out on time, whether "again" and "edit" work, whether
the layout breaks on long text. For that you need no subscription, no key, and no network,
just a predictable answer that arrives without questions about how much it cost.

The text and the delay come from the settings (`brain.stub_text`, `brain.stub_delay_ms`),
not hardwired here: to change them means to edit one line in `argus.toml`, not to read the
code.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from argus.brain.base import Answer

if TYPE_CHECKING:
    from argus.core.config import BrainConfig


class StubProvider:
    """Answers with a preset text, calling nothing."""

    name = "stub"

    def __init__(self, cfg: BrainConfig) -> None:
        self.cfg = cfg
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0

    async def start(self, system: str | None = None) -> None:
        """Nothing to bring up: the stub holds nothing open."""
        return

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
    ) -> Answer:
        t0 = time.perf_counter()
        if self.cfg.stub_delay_ms > 0:
            await asyncio.sleep(self.cfg.stub_delay_ms / 1000)

        elapsed = (time.perf_counter() - t0) * 1000
        self.calls += 1
        self.total_ms += elapsed
        return Answer(
            text=self._next_text(),
            latency_ms=elapsed,
            provider=self.name,
            model=self.name,
            # The stub decides nothing itself, but we put `deep` into the answer honestly: that way a
            # test of the flag wiring through `Assistant` does not need a real provider with two
            # levels.
            meta={"deep": deep},
        )

    def _next_text(self) -> str:
        """Round-robin through `stub_answers` if it is set, otherwise always `stub_text`.

        Round-robin, not in sequence without repeating: a scenario (`stub_stt.dialogue`) may call
        the model more times than there are answers in the list, and that is no reason to stop.
        """
        answers = self.cfg.stub_answers
        if not answers:
            return self.cfg.stub_text
        return answers[(self.calls - 1) % len(answers)]

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        """There is no cache, so there is nothing to warm."""
        return

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    async def close(self) -> None:
        return
