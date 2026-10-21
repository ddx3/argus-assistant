"""Conversation recap and watch topics: what the app does when the user asks.

A request arrives by two paths: with words in the question line ("recap",
"watch for the price", `commands.py`) and with a button on the page
(`routes.py`). After that there is one path: collect the utterances, ask the model
(`engine.py`), and put the result into the same timeline as a card, like ordinary
answers. The page does not change at all for this: the card arrives in the same state
snapshot, and goes to the chat log by the same path as the other answers, and comes back
together with the chat when it is opened again.

The model is called directly through the assistant's provider, bypassing its filter and
counters: a recap is a user's request, not an occasion, and the "no more often than once
every N seconds" limit does not apply to it. The provider is the same as for answers, so
the billing (for the relay, token usage) goes the same way too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from argus.brain.base import Answer
from argus.core import friendly
from argus.recap import chat, commands, engine
from argus.recap import lines as lines_mod
from argus.recap.commands import Command
from argus.recap.topics import MAX_TOPICS, Topics

if TYPE_CHECKING:
    from argus.app import Argus

log = logging.getLogger(__name__)

# Where the utterances came from: the chat log (the whole conversation) or the live
# timeline (the last minutes).
CHAT = "chat"
LIVE = "live"

LIVE_NOTE = (
    "*The session is not open, so the summary covers only the last few minutes: the live "
    "feed does not keep more.*"
)
DROPPED_NOTE = (
    "*The conversation is longer than fits at once: only its last part was summarized, "
    "without the first {n} lines.*"
)

# Stopping recognition is the end of the conversation (or a break in it). If the user asked
# to watch topics, a recap on them is prepared by itself. Before reading the log we wait:
# the last utterance is still being recognized and reaches it in a second or two. And after
# a break in which almost nothing was said, there is no point in repeating the recap: at
# least this many new utterances since the last recap are needed.
AUTO_DELAY_S = 2.0
AUTO_MIN_LINES = 5


def recap_of(app: Argus) -> Recap:
    """This app's recap. Created on first use, like the chat manager."""
    found = getattr(app, "_recap", None)
    if not isinstance(found, Recap):
        found = Recap(app)
        app._recap = found
    return found


async def intercept(app: Argus, question: str | None) -> Answer | None:
    """If the question line is a recap command, execute it and return a card; otherwise `None`, and
    the question goes to the model as usual."""
    command = commands.parse(question)
    if command is None:
        return None
    return await recap_of(app).handle(command, question or "")


class Recap:
    def __init__(self, app: Argus) -> None:
        self.app = app
        self.topics = Topics(app)
        self._running = False
        # The time of the last utterance that went into a recap, per chat, so that a stop after a
        # break with no new words does not ask for the same recap a second time. By time and not by
        # the number of utterances: the live timeline (when no chat is open) is trimmed by a
        # window, and the number in it does not grow.
        self._covered: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- what the page sees --------------------------------------------------

    def state(self) -> dict:
        found = chat.active(self.app)
        return {
            "topics": self.topics.get(),
            "chat": found[0] if found else None,
            "max": MAX_TOPICS,
            "busy": self._running,
        }

    def set_topics(self, raw: str | Iterable[object]) -> dict:
        return {"topics": self.topics.replace(raw)}

    # -- requests ------------------------------------------------------------

    async def handle(self, command: Command, cause: str) -> Answer:
        """A command from the question line."""
        assistant = self.app.assistant
        started, epoch = assistant.clock.now(), assistant.epoch
        if command.kind == commands.RECAP:
            # The card's label is the user's own words ("recap on deadlines"), not one assembled from the
            # topic put into a grammatical case that may come out wrong (Russian). It is
            # also re-parsed as a command when "once more" is pressed: if it parsed once, it will
            # parse the second time.
            label = _sentence(cause) if command.arg else ""
            return await self._recap(command.arg, started, epoch, label=label)
        try:
            text, error = self._topics_reply(command), None
        except OSError as e:
            text, error = "", f"Couldn't save the topics: {e}"
        return self._publish(Answer(
            text=text, error=error, meta=self._meta(cause, "topics", started, epoch),
        ))

    async def run(self, focus: str = "") -> Answer:
        """A recap by button: `focus` is one topic, empty is the whole conversation."""
        assistant = self.app.assistant
        if assistant is None:
            return Answer(text="", error="the assistant is turned off")
        return await self._recap(focus, assistant.clock.now(), assistant.epoch)

    def stopped(self) -> None:
        """The user turned off recognition themselves. If they asked to watch topics, a recap on
        them is prepared by itself, and if they did not ask, nothing happens: we do not spend
        tokens without a request.

        Only the `/control` route calls it, that is, the manual switch. A stop caused by a chat
        change or a sound failure does not get here: by that moment there is nowhere to put the
        recap of the conversation that is leaving anyway.
        """
        assistant = self.app.assistant
        if assistant is None or self._running or not self.topics.get():
            return
        # We remember whose conversation this is now, and not when the task starts working: another
        # chat can be opened between those two moments.
        task = asyncio.get_running_loop().create_task(self._auto(assistant.epoch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _auto(self, epoch: int) -> None:
        assistant = self.app.assistant
        try:
            await asyncio.sleep(AUTO_DELAY_S)
            if assistant.epoch != epoch:
                return      # another chat was opened in the meantime
            transcript, _ = await self._lines()
            seen = self._covered.get(self._key(), 0.0)
            if sum(1 for line in transcript if line.ts > seen) < AUTO_MIN_LINES:
                return
            await self._recap("", assistant.clock.now(), epoch, reason="recognition is off")
        except Exception:
            log.exception("recap on stop failed")

    # -- recap ----------------------------------------------------------------

    async def _recap(
        self, focus: str, started: int, epoch: int, reason: str = "recap", label: str = "",
    ) -> Answer:
        assistant = self.app.assistant
        if self._running:
            return Answer(text="", provider=assistant.provider.name, error="a summary is already being prepared")
        self._running = True
        # The card's label is what the user will read as the "question".
        label = label or (
            f"Summary of the conversation about {focus}" if focus else "Summarize the conversation"
        )
        # The request in progress, so that "thinking" is visible in all windows at once and so that
        # the hotkey does not butt in on top. Someone else's (a live answer is already going) we do
        # not touch.
        pending: dict[str, object] = {
            "t": started, "cause": label, "trigger": "hotkey", "reason": reason,
            "image_kb": 0, "image_src": "screen", "deep": False,
            # In such a card the page writes "preparing:", not "replying to:".
            "kind": "recap",
        }
        if assistant.pending is None:
            assistant.pending = pending
        scope = ""
        try:
            key = self._key()
            transcript, scope = await self._lines()
            context, language = _context(self.app)
            outcome = await engine.summarize(
                assistant.provider, transcript,
                topics=self.topics.get(), focus=focus, context=context, language=language,
            )
            if outcome.ok and transcript:
                self._covered[key] = transcript[-1].ts
        except Exception as e:  # a recap must not bring the app down
            log.exception("recap failed")
            outcome = engine.Outcome(error=friendly.explain(e))
        finally:
            self._running = False
            if assistant.pending is pending:
                assistant.pending = None
        return self._publish(self._answer(outcome, label, scope, started, epoch, reason))

    def _key(self) -> str:
        found = chat.active(self.app)
        return found[0] if found else ""

    async def _lines(self) -> tuple[list[lines_mod.Line], str]:
        found = chat.active(self.app)
        if found is not None:
            chat_id, store = found
            # The log is a file, and for a long conversation it is not small: we read it not in the
            # event loop, otherwise the whole page would freeze for that time.
            records = await asyncio.to_thread(store.history, chat_id)
            return lines_mod.from_history(records), CHAT
        assistant = self.app.assistant
        return lines_mod.from_timeline(list(assistant.timeline), assistant.clock), LIVE

    def _answer(
        self, outcome: engine.Outcome, label: str, scope: str, started: int, epoch: int,
        reason: str,
    ) -> Answer:
        meta = self._meta(label, reason, started, epoch)
        meta["prompt"] = (
            f"(summary: {outcome.lines} lines, {outcome.parts} parts, "
            f"{outcome.calls} requests to the model)"
        )
        meta["recap"] = {
            "lines": outcome.lines, "dropped": outcome.dropped, "parts": outcome.parts,
            "calls": outcome.calls, "scope": scope,
        }
        provider = outcome.provider or self.app.assistant.provider.name
        if outcome.error:
            return Answer(
                text="", error=f"The summary didn't work out: {outcome.error}", provider=provider,
                model=outcome.model, latency_ms=outcome.latency_ms, meta=meta,
            )
        notes = []
        if scope == LIVE and not outcome.tiny:
            notes.append(LIVE_NOTE)
        if outcome.dropped:
            notes.append(DROPPED_NOTE.format(n=outcome.dropped))
        return Answer(
            text="\n\n".join([*notes, outcome.text]), provider=provider, model=outcome.model,
            latency_ms=outcome.latency_ms, meta=meta,
        )

    # -- topics ---------------------------------------------------------------

    def _topics_reply(self, command: Command) -> str:
        topics = self.topics
        if command.kind == commands.WATCH:
            change = topics.add(command.arg)
            if change.skipped:
                return (
                    f"Already {MAX_TOPICS} topics, these did not fit: {_names(change.skipped)}. "
                    "Remove some first: \"stop watching …\"."
                )
            if not change.changed:
                return f"Already watching: {_names(change.topics)}."
            tail = "" if chat.active(self.app) else (
                " The session is not open, so I will remember the topics only until the program "
                "is closed."
            )
            return (
                f"Watching: {_names(change.topics)}. I will stay quiet for now, and when you turn "
                "off recognition I will send a summary of the conversation and a separate one on these "
                f"topics. For one earlier, type \"recap\".{tail}"
            )
        if command.kind == commands.UNWATCH:
            if not command.arg:
                change = topics.clear()
                return "Topics cleared." if change.changed else "I am not watching anything anyway."
            change = topics.remove(command.arg)
            if not change.changed:
                now = f"Currently watching: {_names(change.topics)}." if change.topics else (
                    "Currently not watching anything."
                )
                return f"There is no such topic. {now}"
            rest = f" Remaining: {_names(change.topics)}." if change.topics else ""
            return f"No longer watching: {_names(change.changed)}.{rest}"
        current = topics.get()
        if not current:
            return (
                "Not watching anything. Say \"watch for deadlines\" and the summary will have a "
                "separate section on that topic, and when you turn off recognition it will "
                "arrive on its own."
            )
        return (
            f"Watching: {_names(current)}. When you turn off recognition, I will send a summary on "
            "them too; for one earlier, type \"recap\"."
        )

    # -- card ------------------------------------------------------------------

    def _meta(self, cause: str, reason: str, started: int, epoch: int) -> dict[str, object]:
        """The same as what `Assistant._ask` puts there: the card and the chat log need these fields in
        the same way, wherever the answer came from."""
        return {
            "trigger": "hotkey", "reason": reason, "t": started, "cause": cause,
            "deep": False, "prompt": "", "image_kb": 0, "image_src": "screen",
            "pinned": 0, "epoch": epoch,
            # Voice output does not read such cards (`tts/reply.py`).
            "silent": True,
        }

    def _publish(self, answer: Answer) -> Answer:
        assistant = self.app.assistant
        if answer.meta.get("epoch") != assistant.epoch:
            # The conversation changed during the calculation (another chat was opened): we do not put
            # this into someone else's timeline. The user will come back and press again.
            return answer
        assistant.answers.append(answer)
        assistant.bus.publish(answer)
        # A flag for the button on the page: the card is in the timeline, and its error does not
        # need to be shown a second time (`routes.py`).
        answer.meta["published"] = True
        return answer


def _sentence(text: str) -> str:
    """A person's phrase as a caption: without the slash and trailing marks, capitalized."""
    text = " ".join(text.split()).removeprefix("/").rstrip(" .!?…")
    return text[:1].upper() + text[1:]


def _names(topics: Iterable[str]) -> str:
    return ", ".join(f"**{topic}**" for topic in topics)


def _context(app: Argus) -> tuple[str, str]:
    """(what the person wrote about the conversation, the answer language rule)."""
    try:
        from argus.assist.prompt import LANGUAGE_RULES
        from argus.assist.settings import read

        settings = read(app.cfg)
    except Exception:  # a recap must not depend on whether the settings can be read
        log.exception("channel settings not read — recap without context")
        return "", ""
    vacancy = settings.vacancy
    context = " — ".join(part for part in (vacancy.stage, vacancy.description) if part)
    language = LANGUAGE_RULES.get(settings.profile.language, LANGUAGE_RULES.get("", ""))
    return context, language
