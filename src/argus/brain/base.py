"""Interface for calling the model.

There are three implementations, and that is not a reserve for the future but a
necessity.

`claude_code` goes through the subscription. The SDK documentation says plainly that,
unless previously approved, third-party developers may not offer subscription login or
subscription rate limits in their products. That is, the subscription covers development
on your own machine, but not handing the product out to other people.

`anthropic_api` is direct access by key. It will be needed when there is something to
hand out. Switching must be a line in the config, not a rewrite; that is why the
interface was introduced from the very beginning.

`local` is a model on your own machine. Not a way to save money (after recognition only
text goes up, and there is little of it), but a future product feature: "nothing leaves
for the network". For scenarios where that matters, a strong argument.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class Answer:
    """The model's answer together with what it cost."""

    text: str
    latency_ms: float = 0.0
    provider: str = ""
    model: str = ""
    error: str | None = None
    meta: dict[str, object] = field(default_factory=dict)

    # `False` only for a streaming answer, and only until it is read to the end:
    # `Assistant._ask` publishes such an `Answer` to the bus at once, empty, and then grows
    # `text` in place (`answer.text += delta`) without publishing a second time. A subscriber
    # that needs the pieces as they become ready (voice-over: start synthesizing the first
    # phrase without waiting for the whole answer) has a way to tell that it may still grow,
    # and not to treat the empty text as final. Without streaming (the other providers) the
    # answer arrives whole anyway: `done=True` from birth, nothing changes.
    done: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


@runtime_checkable
class LLMProvider(Protocol):
    """Sends a request to the model and returns text.

    The error is returned in `Answer.error`, not raised: calling the model is the least
    reliable part of the pipeline (network, limits, overload), and the whole thing must not
    fall over because of it. Capture and recognition must keep working even when the model
    is unavailable.
    """

    name: str

    async def start(self) -> None:
        """Bring the connection up in advance so that the first request does not wait."""
        ...

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
    ) -> Answer:
        """`image` is a screen frame in JPEG, if the model must see it.

        `pinned` are images pinned to the system part: pages of a lecture shot on a phone, or a
        scan with no text layer. They do not change from question to question, so they go
        **before** the screen frame and before the cache point: the frame is rewritten every
        time, and if it stood earlier, it would break the cache of the whole batch.

        Why images and not recognized text: a lecture page is not only letters. A diagram, a
        formula, a drawing, a handwritten derivation in the margin: the recognizer silently
        loses all of it, and the model answers from half a page without knowing it saw only
        half.

        `deep` is "think longer", a request the person makes by hand (a button on the panel),
        not a filter decision. A provider without a second level ignores it: there is nothing to
        answer with that is stronger than usual.
        """
        ...

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        """Write the cache of the system part ahead of time, before the first live question.

        It is needed because of how the prompt cache works: the first request with a new prefix
        *writes* it, which is a full prefill, a second or more on a large prompt, while all the
        following ones *read* the ready one in tens of milliseconds. Without warming, the "first
        request" would be the first question in the exam, and the write would be paid for at
        exactly the moment when a second is visible. Warming moves that payment to the loading of
        the materials, where nobody is in a hurry.

        A provider without a cache has nothing to do here; for it this is a no-op.
        """
        ...

    async def close(self) -> None: ...
