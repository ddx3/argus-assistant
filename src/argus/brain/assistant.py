"""Assembling the brain: timeline -> filter -> model -> answer.

The prompt is deliberately neutral: "answer the interlocutor's last question, relying on
what is visible on the screen". It fits all four scenarios from the plan and prejudges
nothing. Specialization ("prompt in an interview", "comment on a stream", "be a
companion") is already a choice of direction, and making it before the core works as a
whole means choosing blind.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from contextlib import aclosing
from typing import TYPE_CHECKING

from argus.brain.base import Answer, LLMProvider
from argus.brain.gate import Decision, Gate, GateSettings, ReplyMode, Trigger
from argus.context.render import render_context
from argus.context.timeline import Kind, Timeline
from argus.core import friendly
from argus.core.events import ScreenEvent, Source, SpeechEvent

if TYPE_CHECKING:
    from argus.account.client import Account
    from argus.core.bus import Bus
    from argus.core.clock import Clock
    from argus.core.config import Config

log = logging.getLogger(__name__)

# A ceiling on one whole request to the provider. Each provider has its own timeouts (the
# relay waits up to 160 s: `RELAY_READ_TIMEOUT_S`), but "the assistant is still answering"
# must not depend on whether one of them failed to set a timeout: a hung request would
# hold the flag until the chat changes.
ASK_GUARD_S = 180.0

# An empty answer with no reason is not silence but something the person must see in the
# card.
EMPTY_ANSWER = "the answer came back empty"

# Echo of our own voice-over (`Assistant._is_echo`): the window after an answer, the
# shortest utterance that is taken for an echo at all, and what share of its words must run
# consecutively, as in a recent answer.
ECHO_WINDOW_S = 180.0
ECHO_MIN_WORDS = 6
ECHO_SHARE = 0.8


def _longest_run(a: list[str], b: list[str]) -> int:
    """The longest chain of words that go consecutively in both `a` and `b`."""
    best = 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def default_system(has_image: bool) -> str:
    """The core prompt, with a screen image in this request or without one.

    Before, there was one prompt for all cases and it always talked about the screen, even
    when not a single frame was attached to the question: "the screenshot is probably the
    main source" would read as a direct lie in a conversation where there is physically no
    picture. The crutch for that was the caveat "a rare addition": it held two mutually
    exclusive facts in one text, "look at the picture" and "most often there is no picture".
    `image` is known in advance every time (`Assistant._ask`), so there is nothing left to
    wait for: if a picture came, the prompt says directly "here it is"; if not, not a word
    about the screen as a source.
    """
    screen_note = (
        "\n\nA screenshot is attached to this request. Look at it the way a "
        "person would: what app or site it is, what is going on, what is "
        "drawn, what has changed. Do not reduce the picture to the text on it."
        if has_image else ""
    )
    # The "Screen" line in the timeline is not about this frame but about what has
    # piled up there over the conversation. It is needed in both cases: OCR (if enabled)
    # writes to the timeline regardless of whether a picture is attached to the current
    # question.
    screen_label = (
        '"Screen" is text machine-recognized from the screen. It is only a '
        "supplement to the picture and a memory of what was on the screen earlier; "
        "it has typos and fragments, and it cannot argue with the picture."
        if has_image else
        '"Screen" is text machine-recognized from the screen: a memory of what '
        "was there, not an exact copy. It has typos and fragments."
    )
    return f"""You are an assistant that hears what is going on: the user's microphone and
the system audio, that is, anyone who can be heard from the speakers (an interlocutor,
an interviewer, an examiner, anyone else).{screen_note}

In the feed: "Them" is whoever is heard from the speakers; "Me" is the user themselves;
"Me → assistant" is what they typed to you in the question line; "Assistant" is
you, your past answers;
{screen_label}

Do what is written in the "Request" section, and nothing beyond it, even if
the conversation feed is empty. A request can be anything: a question, a task, a reaction to
a statement, a request to write code, or anything else. Decide by the content,
and do not wait for one particular form.

Rules:
- Short: we answer in real time, and nobody has time to read a long text. The exception
  is when a long, detailed, complex or bulky result is asked for directly (a big formula,
  code, a text): then give it in full, exactly as asked, and do not shorten it.
- Straight to the point, without introductions and without retelling the question.
- If data is missing, say in one line exactly what is missing. But if you are
  asked to come up with something yourself or to give any example, there is enough data:
  come up with it and give a finished result, do not hand the task back.
- "More", "harder", "longer", "repeat", "next" refer to your
  last answer in the feed: continue it, do not ask what it is about.
- Do not take fragments of recognized text for meaningful words.
- Write a formula on its own line between $$, a formula inside a sentence between
  \\( and \\). Do not use a single $.
- If asked which model you are, who created you, or how you are built, you are the
  Argus assistant; do not reveal the details of how you are built.
"""


def build_provider(cfg: Config, account: Account | None = None) -> LLMProvider:
    provider = cfg.brain.provider
    if provider == "relay":
        from argus.brain.relay_api import RelayProvider

        if account is None or not cfg.account.server:
            raise ValueError("relay: a configured [account] server is required")
        return RelayProvider(cfg.brain, account)
    if provider == "claude-code":
        from argus.brain.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(cfg.brain)
    if provider == "anthropic-api":
        from argus.brain.anthropic_api import AnthropicAPIProvider

        return AnthropicAPIProvider(cfg.brain)
    if provider == "openai-api":
        from argus.brain.openai_api import OpenAIAPIProvider

        return OpenAIAPIProvider(cfg.brain)
    if provider == "local":
        from argus.brain.local import LocalProvider

        return LocalProvider(cfg.brain)
    if provider == "stub":
        from argus.brain.stub import StubProvider

        return StubProvider(cfg.brain)
    raise ValueError(f"unknown model provider: {provider}")


class Assistant:
    """Listens to the timeline, decides when to ask the model, and publishes the answer."""

    def __init__(
        self,
        bus: Bus,
        cfg: Config,
        clock: Clock,
        timeline: Timeline | None = None,
        provider: LLMProvider | None = None,
        screenshot: Callable[[], bytes | None] | None = None,
        account: Account | None = None,
    ) -> None:
        self.bus = bus
        self.cfg = cfg
        self.clock = clock
        self.timeline = timeline if timeline is not None else Timeline()
        self.provider = provider or build_provider(cfg, account)
        self.gate = Gate(GateSettings(**cfg.gate_brain.model_dump()))

        # Where to get a screen frame when the model must see it. The assistant knows nothing
        # about capture; it is given a ready function.
        self.screenshot = screenshot

        self.answers: list[Answer] = []
        self.last_decision: Decision | None = None
        self._busy = False

        # Number of the conversation "session". It grows when another conversation starts
        # (`reset`, another chat opened): a request that went to the model before that comes back
        # into someone else's timeline, and the number shows it. Without it, the answer to a
        # question from the previous chat would land as a card in the next one.
        self.epoch = 0
        # Utterances that ended before this moment (the assistant's clock, ns) belong to the past
        # conversation and give no reason to wake the model (see `reset`).
        self.since = 0

        # What the model is working on right now: when it was asked and because of what. It lives
        # only while the request is in progress; that is the whole point. The answer will land in
        # `answers` in two or three seconds, but we need to know at once that it is on its way and
        # which utterance it belongs to: otherwise waiting is indistinguishable from the filter's
        # silence, and the person presses the hotkey on top of a running request.
        self.pending: dict[str, object] | None = None

        # The system prompt whose cache has already been written. Not a "warmed" flag: the prompt
        # is rebuilt on every settings edit, and it must be warmed again exactly when the text
        # changed, not when someone pressed "save" once more. A write costs money, and a repeated
        # one for the same prefix gives nothing.
        self._warmed: tuple | None = None
        self._warming: set[asyncio.Task] = set()

        # Material pages as images: a lecture shot on a phone, or a scan without a text layer. The
        # channel holds them, not the config: they are bytes and do not belong in the settings
        # file, which is read on every start and goes whole to the browser every time the admin
        # page is opened.
        self.pinned: list[bytes] = []

    @property
    def busy(self) -> bool:
        """The model is thinking right now."""
        return self._busy

    async def start(self) -> None:
        await self.provider.start()

    async def warm(self) -> None:
        """Warm the cache of the unchanging part of the request, both variants if they changed.

        There are two variants: with a screen frame attached and without one (see
        `default_system` in this module). Which one goes out is decided by `_ask` by the fact of
        whether this request has a frame (screen switched off, no frame found, `ask` mode), so we
        warm both: the first question of a session must not become a second that is visible.
        """
        system = self.cfg.brain.system_prompt
        system_img = self.cfg.brain.system_prompt_with_image
        # Images go into the key by length, not by bytes: comparing a batch of seventy thousand
        # tokens whole would mean megabytes on every press of "save", and swapping a page for
        # another of exactly the same size can only be done on purpose.
        key = (system, system_img, tuple(len(x) for x in self.pinned))
        if not system or key == self._warmed:
            return
        self._warmed = key
        await self.provider.warm(system, self.pinned)
        if system_img and system_img != system:
            await self.provider.warm(system_img, self.pinned)

    def warm_soon(self) -> None:
        """The same, but from synchronous code, a settings applier for example.

        We keep the reference to the task ourselves: the garbage collector takes away a task
        nobody is looking at, and the warm-up then breaks off in the middle, silently, like
        everything in this part.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop means the pipeline is not up yet, and it is too early to warm.
            return
        task = loop.create_task(self.warm())
        self._warming.add(task)
        task.add_done_callback(self._warming.discard)

    async def run(self) -> None:
        """Watch the timeline and wake the model when appropriate."""
        speech = self.bus.subscribe(SpeechEvent, maxsize=64, name="brain-speech")
        screen = self.bus.subscribe(ScreenEvent, maxsize=16, name="brain-screen")
        try:
            await asyncio.gather(self._watch_speech(speech), self._watch_screen(screen))
        finally:
            speech.close()
            screen.close()

    async def _watch_speech(self, sub) -> None:
        async for event in sub:
            # A failure on one utterance must not end the loop: while the assistant exists, utterances
            # are put into the timeline only by it (`Argus._pump_speech`), and a dead `run()` would
            # leave the timeline frozen. Hotkey answers would come from the old context, and there
            # would not be a line about it in the log or on the page.
            try:
                await self._on_speech(event)
            except Exception:  # any failure of one utterance; the loop lives on
                log.exception("assistant could not parse the utterance - listening on")

    async def _on_speech(self, event: SpeechEvent) -> None:
        if event.error or event.t_end <= self.since:
            return
        if event.engine:
            # An utterance from the shadow engine: the same audio, recognized by a second recognizer
            # for comparison. Letting it through would double every phrase in the timeline and wake
            # the model twice per question.
            return
        if event.source is Source.THEM and self._is_echo(event):
            # This is the assistant itself, heard from the speakers: it goes neither into the timeline
            # as the "other party" nor in as a trigger.
            return
        entry = self.timeline.add_speech(event)
        decision = self.gate.on_speech(entry, self.clock.now())
        if decision.wake and self._is_semantic_candidate(entry):
            decision = await self._semantic_check(entry, decision)
        self.last_decision = decision
        if decision.wake:
            await self._ask(decision)

    def _is_echo(self, event: SpeechEvent) -> bool:
        """Is this our own voice-over coming back from the speakers?

        The voice-over plays the answer on the output device, and the system audio capture hears
        the whole device: an answer read aloud comes back as the other party's utterance, and an
        interrogative word or an order in it («реши» (solve), «найди» (find)) would wake the
        model again, on its own voice. We recognize it by the text: the utterance consists almost
        entirely of words that follow one another in a recent answer. Merely shared words do not
        count: a question on the same topic shares terms with the answer, but not a chain. Only
        when voice-over is on (`[channel.tts] enabled`); without it there is nothing to listen
        to.
        """
        if not (self.cfg.channel.get("tts") or {}).get("enabled"):
            return False
        words = re.findall(r"\w+", event.text.lower())
        if len(words) < ECHO_MIN_WORDS:
            return False
        since = event.t_end - int(ECHO_WINDOW_S * 1_000_000_000)
        for entry in self.timeline.since(since):
            if entry.kind is not Kind.REPLY or entry.t > event.t_end:
                continue
            said = re.findall(r"\w+", entry.text.lower())
            if _longest_run(words, said) >= ECHO_SHARE * len(words):
                return True
        return False

    def _is_semantic_candidate(self, entry) -> bool:
        """Whether one more, semantic check is needed on top of the gate's decision.

        Only for the step that is currently in `ReplyMode.SEMANTIC`, either your own
        (`mic_mode`) or the other party's (`system_mode`); the other steps the gate has already
        decided by itself, without the model.
        """
        mode = self.gate.settings.mic_mode if entry.source is Source.ME else self.gate.settings.system_mode
        return mode is ReplyMode.SEMANTIC

    async def _semantic_check(self, entry, decision: Decision) -> Decision:
        """A second check on top of the gate's decision, by meaning and not by form.

        The core (`Gate.on_speech`) has already run the utterance through the common free filter
        by form: question mark, length, not a repeat. Only the rare ones get this far, and on
        this remainder there is a real model call, the same provider and the same limit counter
        as for the answer: the check also costs money and seconds, it is not a free guess.

        It errs toward silence on purpose, on the same principle as the rules in `gate.py`: an
        unsure or cut-off answer is not consent.
        """
        if self._busy:
            # The previous request is still in progress: `_ask` would drop this one too, and the check
            # would cost money.
            return Decision(False, "the assistant is still answering")
        # The check is the same kind of paid call, but its "no" does not count as a request, and
        # without a count of its own every question-like utterance would cost a call without limit.
        refused = self.gate.check_budget(self.clock.now())
        if refused is not None:
            return refused
        verdict = await asyncio.wait_for(
            self.provider.ask(
                f'A line was said aloud in the conversation: "{entry.text.strip()}". '
                "Should the assistant step in and answer now? "
                "Answer with one word: yes or no.",
                system="You are a filter in front of a voice assistant. Answer strictly "
                       '"yes" or "no", with no explanations and no punctuation.',
            ),
            ASK_GUARD_S,
        )
        if verdict.ok and verdict.text.strip().lower().startswith(("yes", "да")):
            return decision
        self.gate.blocked_not_relevant += 1
        return Decision(False, "the line is irrelevant")

    async def _watch_screen(self, sub) -> None:
        async for event in sub:
            try:
                await self._on_screen(event)
            except Exception:  # see `_watch_speech`
                log.exception("assistant could not parse the screen frame - still watching")

    async def _on_screen(self, event: ScreenEvent) -> None:
        made = self.timeline.add_screen(event)
        new_lines = sum(e.text.count("\n") + 1 for e in made if e.kind is Kind.SCREEN)
        decision = self.gate.on_screen(new_lines, self.clock.now())
        if decision.wake:
            self.last_decision = decision
            await self._ask(decision)

    async def ask_now(
        self,
        question: str | None = None,
        deep: bool = False,
        *,
        force_image: bool = False,
        image: bytes | None = None,
    ) -> Answer:
        """Ask by hand: with a hotkey or from the dashboard.

        `deep` is a request to "think longer", not a filter decision: the gate does not see it at
        all, and it goes straight to the provider.

        `force_image` is only for "ask about the screen" (`app.ask_about_screen`): the ordinary
        "ask" and "ask about the screen" both arrive here with the same `Trigger.HOTKEY`, and
        previously the picture was attached to either of them by that one trigger: the person
        pressed "ask" about the other party's utterance, but got an answer analyzing what is on
        the screen, because the frame was sent silently. An explicit flag instead of the trigger:
        the ordinary hotkey does not pass it and never gets a picture.

        `image` is a photo the person pasted into the question line themselves (JPEG). It goes
        instead of the screenshot, not together with it: the person showed what to look at, and
        the screen is superfluous here; providers accept one picture per request, and an extra
        frame would cost tokens for nothing.
        """
        decision = self.gate.on_hotkey(self.clock.now())
        self.last_decision = decision
        if not decision.wake:
            return Answer(text="", provider=self.provider.name, error=decision.reason)
        return await self._ask(decision, question, deep, force_image=force_image, image=image)

    async def _ask(
        self, decision: Decision, question: str | None = None, deep: bool = False,
        *, force_image: bool = False, image: bytes | None = None,
    ) -> Answer:
        if self._busy:
            # The model is still thinking about the previous one. Queueing is out of the question: an
            # answer to the day-before-yesterday's question is useless to anyone, and it would spend
            # the limits.
            return Answer(text="", provider=self.provider.name, error="the assistant is still answering")

        epoch = self.epoch
        prompt = self._build_prompt(decision, question)
        # A photo pasted by the person is its own thing, not a screenshot: neither the prompt "a
        # screenshot is attached to this request" nor screen capture fits it.
        pasted = image is not None
        if pasted:
            prompt += "\n\n(An image is attached to the question, not a screenshot.)"
        elif (force_image or self.cfg.brain.send_screenshot) and self.screenshot is not None:
            # `force_image` is an explicit request to "ask about the screen", not a guess from the
            # trigger: the ordinary "ask" hotkey also arrives with `Trigger.HOTKEY` (see `ask_now`),
            # and before, the frame was attached to both at once. A spoken question to the other party
            # got an answer analyzing the screen even though nobody asked about the screen, and every
            # ordinary question paid for a frame. `screenshot_mode == "always"` is a separate knob for
            # "frame always", unrelated to how exactly the question was asked.
            #
            # An explicit request is stronger than the "Screen" switch: the switch turns off
            # constant sending to save limits, not the question "what is on the screen". The ban is
            # lifted by the flag itself, not by a temporary edit of the shared config
            # (`Argus.ask_about_screen`): that edit overwrote a "Screen off" pressed
            # while the question was in progress.
            if force_image or self.cfg.brain.screenshot_mode == "always":
                image = self.screenshot()

        # What we are asking because of. Your own request is the request itself; the other party's
        # question (or, if mic_mode lets your own speech through, that utterance) is the
        # utterance; everything else is described by the filter's decision. We take the source
        # from the gate's decision rather than assuming THEM is the only possibility:
        # `decision.source` is precisely the answer to "whose utterance to take".
        asked_at = self.clock.now()
        if question:
            cause = question
        elif decision.trigger is Trigger.SCREEN:
            cause = decision.reason
        else:
            # QUESTION (the filter caught an utterance) and HOTKEY (asked by hand, with no typed
            # text): in both cases "what we are answering" is decided by the last utterance in the
            # timeline, not by the service text of the gate's decision. No `or Source.THEM`: for
            # QUESTION `decision.source` is always set anyway (the filter knows whose utterance
            # triggered it), while for HOTKEY it is always `None`. With a fallback to THEM,
            # `last_speech` would look only for the other party's utterances and would never see your
            # own; `last_speech(None)` truly takes the last one, whoever said it.
            last = self.timeline.last_speech(decision.source)
            cause = last.text if last else decision.reason

        # Not the prompt: it carries the whole conversation timeline and will not match itself
        # even a minute later, since new utterances in the context change the string every time.
        # What must be compared for a repeat in `on_speech` is the bare question, exactly what
        # `cause` holds.
        self.gate.accept(asked_at, cause, manual=decision.trigger is Trigger.HOTKEY)
        self._busy = True
        self.pending = {
            "t": asked_at,
            "cause": cause,
            "trigger": decision.trigger.value if decision.trigger else "?",
            "reason": decision.reason,
            "image_kb": len(image) // 1024 if image else 0,
            "image_src": "paste" if pasted else "screen",
            "deep": deep,
        }
        # Which prompt variant, with a picture for this request or without, is decided by `image`,
        # computed just above, not by some separate knob. Both variants are built and sit ready in
        # the config (`install` in `assist/prompt.py`); here there is only a choice between two
        # strings. A pasted photo takes the variant without a picture: the one with it says "a
        # screenshot is attached" and tells the model to analyze an application or site, which is
        # untrue for a photo of a document or a person (the photo itself is covered at the end of
        # the prompt).
        system = (
            self.cfg.brain.system_prompt_with_image if image is not None and not pasted
            else self.cfg.brain.system_prompt
        )
        # All of this is known BEFORE the model is called; before, it was assembled at the end,
        # after the answer. For a streaming provider that would mean the card (question, tags)
        # cannot appear before all the text has arrived. Here all the meta is ready at once, before
        # the first token; `latency_ms`, `model` and the token counts are added later, as they
        # cannot be known before the request ends.
        meta = {
            "trigger": decision.trigger.value if decision.trigger else "?",
            "reason": decision.reason,
            "t": asked_at,
            "cause": cause,
            # Not from the provider's answer: the stub and `claude-code` know nothing about the level,
            # and the panel has to draw the tag the same way for all of them.
            "deep": deep,
            # We store the request itself: without it you cannot tell what the model was answering,
            # and "the model is talking nonsense" most often means "nonsense went into the prompt".
            "prompt": prompt,
            "image_kb": len(image) // 1024 if image else 0,
            # Where the picture came from: a screenshot, or a photo pasted by the person. The card
            # labels them differently.
            "image_src": "paste" if pasted else "screen",
            # How many material pages went out as images. Each is fifteen hundred tokens, and when the
            # bill is a surprise, the question "how many were there" has to be answered from the
            # record, not from memory.
            "pinned": len(self.pinned),
            # Whose conversation this is: see `epoch` in `__init__`.
            "epoch": epoch,
        }

        # `ask_stream` is a capability, not part of `LLMProvider`: only `openai-api` has it for now
        # (see its docstring); the other providers do not define it and simply take the ordinary
        # `ask` below, as before.
        stream = getattr(self.provider, "ask_stream", None)
        if stream is None:
            t0 = time.perf_counter()
            try:
                answer = await asyncio.wait_for(
                    self.provider.ask(
                        prompt,
                        system=system,
                        image=image,
                        pinned=self.pinned,
                        deep=deep,
                    ),
                    ASK_GUARD_S,
                )
            except Exception as e:  # noqa: BLE001 — the provider must return an error as an answer, but may fail to
                log.warning("model call was interrupted: %s: %s", type(e).__name__, e)
                answer = Answer(
                    text="", provider=self.provider.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    error=friendly.explain(e),
                )
            finally:
                # We do not touch the flag if the conversation changed in the meantime: it was already
                # reset (`reset`), and the current `_busy` belongs to a request of the new chat, so
                # someone else's ending must not clear it.
                if epoch == self.epoch:
                    self._busy = False
                    self.pending = None
            answer.meta.update(meta)
            answer.done = True
            if epoch != self.epoch:
                # The conversation changed in the meantime: we return the answer from the previous chat to
                # the caller, but do not put it into the new timeline.
                return answer
            self.answers.append(answer)
            self._remember(answer, question, pasted, epoch)
            if not answer.ok:
                log.warning("model did not respond: %s", answer.error)
                if not answer.error:
                    answer.error = EMPTY_ANSWER
            # We publish the failed answer too: the card can show `error` (the same as on the
            # streaming path), and without that the person pressed "Ask", the "thinking"
            # badge disappeared, and nothing said why. History and voice-over are not
            # affected by such an answer: both take the text, and a failed one has none.
            self.bus.publish(answer)
            return answer

        # We publish IMMEDIATELY, with an empty `Answer` (`done=False`), without waiting for even
        # one token, let alone the whole answer. The card with all the tags (`meta` is already
        # complete) appears in the dashboard at once (`self.answers` grows right here, not at the
        # end of the function). Voice-over (`tts/reply.py`) gets the same object the same way,
        # through the bus, and itself watches `answer.text` grow, synthesizing finished sentences
        # as they appear. Before, both the card and the first sound waited not for the first token
        # but for the whole answer, even though the synthesis pipeline was already in full swing
        # inside.
        answer = Answer(text="", provider=self.provider.name, meta=dict(meta), done=False)
        self.bus.publish(answer)
        self.answers.append(answer)
        # "thinking" is no longer the right word: a real (if still empty) card is
        # already in the timeline, and the badge over it would show the same thing twice. `_busy`
        # stays: a second question right on its heels is still ill-timed until this one is read to
        # the end.
        self.pending = None
        info: dict = {}
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(ASK_GUARD_S), aclosing(stream(
                prompt,
                system=system,
                image=image,
                pinned=self.pinned,
                deep=deep,
                info=info,
            )) as chunks:
                async for delta in chunks:
                    if epoch != self.epoch:
                        # Another chat was opened: this answer has already been thrown away (`_pump_answers`,
                        # `_remember`), and writing it further would pay for text nobody will read. Leaving the
                        # loop closes the stream at the provider.
                        break
                    answer.text += delta
        except Exception as e:  # noqa: BLE001 — network and limits must not crash the pipeline
            # Details go to the log; the person gets one clear phrase.
            log.warning("response stream was interrupted: %s: %s", type(e).__name__, e)
            answer.error = friendly.explain(e)
        finally:
            answer.latency_ms = (time.perf_counter() - t0) * 1000
            answer.model = str(info.pop("model", ""))
            answer.meta.update(info)
            # `done` means finished for good. There is no `await` between this and the write above, so
            # from outside (the event loop is single-threaded) the state between "meta finished" and
            # "marked done" cannot be seen.
            answer.done = True
            if epoch == self.epoch:
                self._busy = False
                self.pending = None
        self._remember(answer, question, pasted, epoch)
        if not answer.ok:
            log.warning("model did not respond: %s", answer.error)
        return answer

    def _remember(self, answer: Answer, question: str | None, pasted: bool, epoch: int) -> None:
        """Put a finished answer into the timeline: the next request must see it.

        Before, our own answers did not get into the timeline at all, and every question was the
        first one for the model: to "even harder" it answered "I need the previous text". An
        empty or failed answer in the timeline is just garbage; an answer from a past
        conversation (`epoch`) does not go into the timeline of a new one.
        """
        if epoch != self.epoch or not answer.ok:
            return
        ask = (question or "").strip()
        if ask and pasted:
            # The photo itself is not in the timeline, so let us at least say that it was there.
            ask += " (a photo was attached to the question)"
        self.timeline.add_reply(self.clock.now(), answer.text, ask=ask)

    def _build_prompt(self, decision: Decision, question: str | None) -> str:
        context = render_context(
            self.timeline,
            screen_now=self.timeline.digest.current_text,
            max_chars=self.cfg.brain.max_context_chars,
        )
        if question:
            task = question
        elif decision.trigger is Trigger.SCREEN:
            task = "Something new has appeared on the screen. Say briefly what it is and what to do."
        else:
            # QUESTION and HOTKEY are both branches without typed text, and both should answer the
            # last utterance if it is already in the timeline. Before, only QUESTION got that: the
            # "ask" hotkey always fell into the generic "say what is going on",
            # even when the other party's utterance was already in the timeline.
            last = self.timeline.last_speech(decision.source)
            if not last:
                task = "Say briefly what is going on right now and what is worth doing."
            elif last.source is Source.ME:
                # By the source of the FOUND utterance (`last.source`), not by `decision.source`: for the
                # hotkey it is always `None`, and wording based on it would silently decide that any found
                # utterance is from the other party, even when `last_speech(None)` returned your own.
                task = f'Answer my own question, asked aloud: "{last.text}"'
            else:
                task = f'Answer the last line from the other party: "{last.text}"'
        return f"{context}\n\n# Request\n{task}"

    async def reset(self) -> None:
        """Start the conversation over: forget everything that was before.

        The timeline (`timeline`) and the filter counters belong to the caller: the application
        holds the timeline, and the channel rebuilds the filter. What belongs to the assistant
        itself is here: its own answers, the request in progress, and the model session. The
        session because `claude-code` keeps one live conversation for the whole time it runs, and
        without resetting it the answers in a new chat would remember questions from the old one,
        and the system prompt would stay the one it started with.
        """
        self.epoch += 1
        self.since = self.clock.now()
        self.answers.clear()
        self.last_decision = None
        self.pending = None
        # A request from the previous chat, if it is still on its way, does not hold the new one:
        # its answer will be dropped by `epoch` anyway, and the first question here must not wait.
        self._busy = False
        if self.provider.name == "claude-code":
            # The next `ask` will bring the session up again: it already has `start` in it.
            await self.provider.close()

    async def close(self) -> None:
        await self.provider.close()
