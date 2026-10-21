"""A request to the recording: transcript → requests to the model → text.

One job for everything: take the utterances and tell the model what to do with them. A
"recap" is one of the requests (`summarize`), and any other request in the user's own
words is `perform`. The request builders differ between them (`prompts.py`), and
everything else is shared.

The main constraint is the request size. The relay (`server/relay.py`) accepts a `prompt`
no longer than 20,000 characters, while an hour of conversation is 60-70 thousand. So a
long transcript is cut into parts, the model writes notes from each, and the notes are
combined into an answer. A short conversation goes in a single request. The split is by
size, not by provider, on purpose: the same code works for a user with the relay and for
one who sits on their own key, and nobody has to remember whose ceiling is what.

There are no more than `MAX_PARTS` parts per conversation (about three hours of speech);
beyond that the oldest utterances are dropped, and the answer honestly says so.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from argus.core import friendly
from argus.recap import prompts
from argus.recap.lines import Line, chunks, render

log = logging.getLogger(__name__)

# The request ceiling in characters. The relay's is 20,000 for `prompt`; a thousand is kept
# in reserve.
PROMPT_CHARS = 19_000

# A reserve for whatever the transcript budget calculation did not take into account.
SLACK = 200

# How many requests to the model run at the same time. Some of the notes are independent,
# and there is no reason to wait for them one by one; but hitting the provider with a dozen
# at once means running into the rate limit exactly when the user is waiting for the recap.
PARALLEL = 3

# The maximum number of conversation parts (~3 hours of speech). Beyond that, we drop the
# old ones.
MAX_PARTS = 12

# Rounds of combining notes until they fit into the final request.
MAX_ROUNDS = 4

# For anything shorter there is nothing to sum up, and the model would only make things up.
MIN_CHARS = 120

TOO_LITTLE = "Too little has been said so far to sum up."
TOO_LITTLE_FOR_REQUEST = "There is too little text in the recording so far to do anything with it."

# A longer request is no longer a request but pasted text; and every extra thousand
# characters takes space from the transcript in every request.
INSTRUCTION_CHARS = 2000

# The ceiling of the final answer. A live answer is short, and its ceiling
# (`brain.max_tokens`, 2000) is too tight for a recap: a section for every watched topic or
# the summary of an hour-long conversation did not fit into it, and the answer broke off
# mid-word while still showing as "complete" on the card, in the recording and in the PDF.
# It is wider only at the last step (the notes for the parts are short) and only for
# providers whose ceiling is set by a setting; eight thousand is the relay's limit, and
# payment is for actual use, not for the ceiling.
FINAL_MAX_TOKENS = 8000
ROOMY_PROVIDERS = frozenset({"relay", "openai-api", "anthropic-api"})

# Providers with one live conversation for the whole run time: a recap cannot be written
# into it, because the following live answers would carry the transcripts and the recap
# role with them, and a live question would stand in line behind the recap requests. Each
# request goes in a separate one-time session.
SINGLE_SESSION_PROVIDERS = frozenset({"claude-code"})

# The note added when an answer hit the output-size ceiling. The words are the same as the
# relay's (`brain/relay_api.py`): a card must not say the same thing in two different
# phrases.
CUT_NOTE = "*The answer came out too long and was cut off.*"
CUT_EMPTY = "the answer did not fit in the allotted size and came back empty"


@dataclass(slots=True)
class Outcome:
    text: str = ""
    error: str | None = None
    lines: int = 0          # number of utterances there were
    dropped: int = 0        # utterances dropped: the oldest ones, they did not fit
    parts: int = 0          # parts the conversation was cut into
    calls: int = 0          # requests to the model
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    tiny: bool = False      # too little was said, so the model was not asked
    truncated: bool = False  # one of the model's answers was cut off by the output-size ceiling

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


class _Failed(Exception):
    """The request to the model failed; `str(e)` is the reason for a person."""


@dataclass(frozen=True, slots=True)
class Task:
    """What exactly is asked of the model: four request builders. The rest, the course of the job
    (slicing, notes, merging), is the same for all requests."""

    single: Callable[[str], str]                 # transcript → the whole prompt
    partial: Callable[[str, int, int], str]      # part, number, total → notes
    merge: Callable[[Sequence[str]], str]        # notes → combined notes
    final: Callable[[Sequence[str]], str]        # notes → result
    tiny_text: str = TOO_LITTLE                  # what to say when there is nothing to read
    label: str = "recap"                         # for the log


async def summarize(
    provider,
    lines: Sequence[Line],
    *,
    topics: Sequence[str] = (),
    focus: str = "",
    context: str = "",
    language: str = "",
) -> Outcome:
    """A summary of the utterances. `focus`: retell one topic instead of a general summary;
    `topics`: tracking topics, added to the general summary (with `focus` there are none)."""
    topics = () if focus else tuple(topics)
    task = Task(
        single=lambda text: prompts.single(
            text, topics=topics, focus=focus, context=context, language=language,
        ),
        partial=lambda text, i, n: prompts.partial(
            text, i, n, topics=topics, focus=focus, context=context,
        ),
        merge=lambda notes: prompts.merge_notes(notes, topics=topics, focus=focus),
        final=lambda notes: prompts.final(
            notes, topics=topics, focus=focus, context=context, language=language,
        ),
    )
    return await run(provider, lines, task)


async def perform(
    provider,
    lines: Sequence[Line],
    instruction: str,
    *,
    context: str = "",
    language: str = "",
    history: Sequence[tuple[str, str]] = (),
) -> Outcome:
    """Any request to the recording, said in the person's own words: "notes", "what did I promise",
    "a letter to the client". The model receives the raw text with timestamps and the request; a
    long one goes in parts, as with a summary.

    `history` is the earlier (request, answer) pairs for this recording, oldest first: so that
    "shorter" or "in English" refers to the previous answer and not to the recording from scratch.
    The size of the conversation is limited (`prompts.HISTORY_CHARS`) and is counted in the space
    reserved for the transcript, like the rest of the request header."""
    instruction = " ".join(instruction.split())[:INSTRUCTION_CHARS]
    earlier = prompts.history_block(history)
    hint = prompts.last_answer(history)
    task = Task(
        single=lambda text: prompts.request_single(
            text, instruction, context=context, language=language, history=earlier,
        ),
        partial=lambda text, i, n: prompts.request_partial(
            text, i, n, instruction, context=context, hint=hint,
        ),
        merge=lambda notes: prompts.request_merge(notes, instruction),
        final=lambda notes: prompts.request_final(
            notes, instruction, context=context, language=language, history=earlier,
        ),
        tiny_text=TOO_LITTLE_FOR_REQUEST,
        label="request",
    )
    return await run(provider, lines, task)


async def run(provider, lines: Sequence[Line], task: Task) -> Outcome:
    started = time.perf_counter()
    out = Outcome(lines=len(lines))

    if sum(len(line.text) for line in lines) < MIN_CHARS:
        out.tiny = True
        out.text = task.tiny_text
        return out

    # How many characters of transcript fit into a request next to the rules and the task. We
    # take the length of the "header" from the builders themselves, not from separate
    # constants, so that an edit to the rules text does not drift apart from the calculation.
    overhead = max(len(task.single("")), len(task.partial("", 99, 99)))
    budget = max(PROMPT_CHARS - overhead - SLACK, 1000)

    parts = chunks(lines, budget)
    if len(parts) > MAX_PARTS:
        out.dropped = sum(len(part) for part in parts[:-MAX_PARTS])
        parts = parts[-MAX_PARTS:]
    out.parts = len(parts)

    ask = _Asker(provider, out)
    try:
        if len(parts) == 1:
            out.text = await ask(task.single(render(parts[0])), final=True)
        else:
            total = len(parts)
            notes = await _all(
                ask(task.partial(render(part), i, total)) for i, part in enumerate(parts, 1)
            )
            notes = await _reduce(ask, notes, task)
            out.text = await ask(task.final(notes), final=True)
    except _Failed as e:
        out.error = str(e)
    if out.truncated and out.ok:
        # A cut-off answer must not look finished: it will be saved, shown and sent to the PDF as
        # it is. There is one note, at the end, even if a step in the middle was cut off.
        out.text = f"{out.text}\n\n{CUT_NOTE}"
    out.latency_ms = (time.perf_counter() - started) * 1000
    log.info(
        "%s: utterances: %d, parts: %d, requests: %d, %.1f s%s",
        task.label, out.lines, out.parts, out.calls, out.latency_ms / 1000,
        f", error: {out.error}" if out.error else "",
    )
    return out


class _Asker:
    """Requests to the model: a shared counter and a shared limit on concurrent ones."""

    def __init__(self, provider, out: Outcome) -> None:
        self.provider = provider
        self.out = out
        self.sem = asyncio.Semaphore(PARALLEL)
        # The reason for the first failure. Since the recap will not come out anyway, the requests
        # that were still waiting in line are not sent at all: they would have to be paid for in
        # vain.
        self.failed = ""
        self._wide = None       # the same provider with a wide answer ceiling (`_wide_provider`)

    async def __call__(self, prompt: str, *, final: bool = False) -> str:
        """`final` is the answer the person will see (not notes on the parts): it needs a wider cap."""
        if len(prompt) > PROMPT_CHARS + 500:
            # The budget calculation must not allow this; if it did, it is better to say so in the log
            # than to guess later why the relay answered 422.
            log.warning("request exceeds the limit (characters): %d", len(prompt))
        async with self.sem:
            if self.failed:
                raise _Failed(self.failed)
            try:
                answer, asked = await self._ask(prompt, final)
            except Exception as e:  # providers usually do not raise, but networks are what they are
                log.warning("recap request failed: %s: %s", type(e).__name__, e)
                self.failed = friendly.explain(e)
                raise _Failed(self.failed) from e
        self.out.calls += 1
        self.out.provider = answer.provider or getattr(self.provider, "name", "")
        self.out.model = answer.model or self.out.model
        if not answer.ok:
            if answer.error:
                self.failed = friendly.scrub(answer.error)
            elif _cut_off(answer, asked):
                # The ceiling was eaten up before the first word (reasoning in models with a "think" mode):
                # there is no text, and the reason is not "came back empty" but a lack of room for the
                # answer.
                self.failed = CUT_EMPTY
            else:
                self.failed = "the answer came back empty"
            raise _Failed(self.failed)
        text = answer.text.strip()
        if _cut_off(answer, asked):
            log.warning("the recap response hit the output size limit (%s)", answer.meta.get("output_tokens"))
            self.out.truncated = True
            # The relay appends the note itself; here it is needed once and at the very end (`run`),
            # and we do not litter with the note the notes that go into the next request.
            text = text.removesuffix(CUT_NOTE).rstrip()
        return text

    async def _ask(self, prompt: str, final: bool):
        """(answer, the provider that was asked). A summary has its own provider in two cases: the final
        step runs with a wide answer cap, and for a provider with a single conversation each request
        goes in a throwaway session (`SINGLE_SESSION_PROVIDERS`)."""
        provider = self.provider
        if getattr(provider, "name", "") in SINGLE_SESSION_PROVIDERS:
            from argus.brain import claude_code  # lazy import: only it needs the SDK

            own = claude_code.ClaudeCodeProvider(provider.cfg)
            try:
                return await own.ask(prompt, system=prompts.ROLE), own
            finally:
                try:
                    await own.close()
                except Exception:       # closing must not take a ready answer away from the recap
                    log.debug("one-off session closed with an error", exc_info=True)
        if final:
            provider = self._wide_provider()
        return await provider.ask(prompt, system=prompts.ROLE), provider

    def _wide_provider(self):
        """A provider with the answer cap `FINAL_MAX_TOKENS`: a copy of the same one, with a copy of the
        settings. We do not touch the shared config of live answers: it changes under the feet of a
        question in progress."""
        if self._wide is None:
            self._wide = _with_room(self.provider, FINAL_MAX_TOKENS)
        return self._wide


def _with_room(provider, tokens: int):
    """`provider` with an answer cap of at least `tokens`; an unknown one, or one that is already
    sufficient, is returned as is.
    """
    cfg = getattr(provider, "cfg", None)
    if getattr(provider, "name", "") not in ROOMY_PROVIDERS or cfg is None:
        return provider
    if (getattr(cfg, "max_tokens", tokens) or 0) >= tokens:
        return provider
    try:
        wide = copy.copy(provider)
        wide.cfg = cfg.model_copy(update={"max_tokens": tokens})
    except Exception:       # the copy failed: we ask as before, a short answer is better than none
        log.debug("cannot set the wide response limit", exc_info=True)
        return provider
    return wide


def _cut_off(answer, provider) -> bool:
    """The answer was cut off by the length cap, not finished."""
    flag = answer.meta.get("truncated")
    if flag is not None:        # the provider knows by itself (the relay passes the server's flag)
        return bool(flag)
    used = answer.meta.get("output_tokens")
    ceiling = getattr(getattr(provider, "cfg", None), "max_tokens", None)
    # Other providers give no flag, but they report usage: if it hit the ceiling, the answer
    # was cut off.
    return (
        isinstance(used, int) and not isinstance(used, bool)
        and isinstance(ceiling, int) and 0 < ceiling <= used
    )


async def _all(coros: Iterable[Awaitable[str]]) -> list[str]:
    """All at once; the first error cancels the rest, rather than leaving them to keep paying for
    requests whose result nobody needs any more."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _reduce(ask: _Asker, notes: list[str], task: Task) -> list[str]:
    """Merge the notes so that the answer based on them fits into one request."""

    def fits(items: Sequence[str]) -> bool:
        return len(task.final(items)) <= PROMPT_CHARS

    for _ in range(MAX_ROUNDS):
        if fits(notes):
            return notes
        groups = _pack(notes, task)
        if len(groups) >= len(notes):
            break   # nothing to combine: each note is large by itself
        notes = await _all(ask(task.merge(group)) for group in groups)
    if fits(notes):
        return notes
    # The last resort: the notes themselves should have been short, but if the model did not
    # obey, each gets an equal share and the excess is cut off.
    room = PROMPT_CHARS - len(task.final([]))
    share = max(room // max(len(notes), 1) - 20, 200)
    return [note[:share] for note in notes]


def _pack(notes: list[str], task: Task) -> list[list[str]]:
    """Lay the notes out in consecutive groups, each of which fits into a merge request."""
    groups: list[list[str]] = []
    current: list[str] = []
    for note in notes:
        note = note[: PROMPT_CHARS // 2]
        trial = [*current, note]
        if current and len(task.merge(trial)) > PROMPT_CHARS:
            groups.append(current)
            current = [note]
        else:
            current = trial
    if current:
        groups.append(current)
    return groups
