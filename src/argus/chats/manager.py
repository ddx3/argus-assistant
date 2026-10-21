"""Chats in the running application: which is open, what is written into it, how it changes
the settings.

The application has one pipeline and many chats, and at any moment the application is "in"
one of them or in none (the home page). Opening a chat means doing three things, and the
order matters:

1. **Settings.** The chat's own values lay over the shared ones as a layer
   (`SettingsStore.overlay`), and the live config, prompt, filter, and materials are
   rebuilt for it. Nothing is written to `argus.local.json`: the chat keeps its settings to
   itself, and the shared ones stay as they were.
2. **Clean slate.** The timeline, answers, frames, and the model's memory of the previous
   conversation are reset (`Argus.reset_conversation`): otherwise in an exam chat the model
   would remember the interview, and its cards would hang in the timeline.
3. **History.** Answers recorded in this chat earlier are returned to the page by the same
   path it receives fresh ones, through the application state, so the card rendering does
   not know they are not new.

Recognition is switched off on a chat change: it is paid, it is switched on only by a
button, and it must not be dragged along with the person from one conversation to another.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from argus.brain.base import Answer
from argus.chats.naming import (
    NAME_MIN_CHARS,
    TITLE_SYSTEM,
    clean_title,
    derive_title,
    title_prompt,
)
from argus.chats.store import MATERIALS, Chat, ChatStore, clean_name, valid_id
from argus.core.events import SpeechEvent
from argus.core.settings_store import flatten, store_of

if TYPE_CHECKING:
    from argus.app import Argus
    from argus.core.config import Config

log = logging.getLogger(__name__)

# What belongs to the chat. By prefixes, not a list of fields: everything that configures
# the conversation itself (who you are, what tone, which materials, when to answer) is the
# chat's. Common to all: hardware, hotkeys, account, voice-over, overlay, the language of
# the application itself. Not in the list means common.
CHAT_PREFIXES = (
    "channel.assist.profile.",
    "channel.assist.vacancy.",
    "channel.assist.prompting.",
    "channel.assist.materials.",
    "channel.assist.answering.",
)
CHAT_EXACT = frozenset({
    # Recognition language is a core field, but by meaning it is about the conversation: one
    # chat in Russian, another in English. The same goes for smart recognition: one conversation
    # needs it for a strong accent, another has no difficulties.
    "realtime_stt.language",
    "realtime_stt.smart",
})

# The chat name on the settings page is not a config field but a property of the chat
# itself.
NAME_PATH = "chat.name"

SUMMARY_PATH = "channel.assist.profile.summary"
MATERIALS_DIR_PATH = "channel.assist.materials.dir"
MATERIALS_ENABLED_PATH = "channel.assist.materials.enabled"

# How many answers the page keeps for the open chat. More, and the feed, which is rebuilt
# twice a second, starts to visibly lag; fewer, and "seeing what was" will not work in a
# long conversation.
FEED = 100

# How many frames we return: as many as the application memory holds (`AppState.shots`).
# Older ones leave the card without the "show frame" button.
SHOTS_KEPT = 10

# Lower bound of the numbers of restored frames by absolute value: the window could have
# cached the earlier numbers (-1e9 ... -1e10, one second back from zero), and the new ones
# must not collide with them.
RESTORED_SHOT_BASE = 10**11


def restored_shot_id(chat_id: str, shot_id: object) -> int:
    """The number of a frame returned from a saved chat: its own for each frame of each chat, and
    the same on every visit.

    The number is also the picture address (`/shot/<number>.jpg`), and the window remembers
    pictures by address for a very long time. When the numbers ran consecutively (-1 s, -2 s ...)
    in every chat, after viewing a frame in one session a foreign screenshot was shown in a
    second one. A negative number is the mark of "from a previous visit" (the timeline does not
    draw it); live ones come from the clock and are positive. The number fits in 2^53, otherwise
    the page would read it incorrectly."""
    digest = hashlib.blake2b(f"{chat_id}/{int(shot_id)}".encode(), digest_size=6).digest()
    return -(RESTORED_SHOT_BASE + int.from_bytes(digest, "big"))


# How long to wait for the model to finish its answer before switching the conversation.
SETTLE_S = 8.0

# The name while the person has skipped it. It is invented later, from the description.
DEFAULT_NAME = "New session"

# How long after the last edit of the description to name the session: while the person is
# typing, there is nothing to name, and a name from half a phrase would come out random.
NAME_DELAY_S = 6.0

# How long after the first answer, if there is no description at all: then the name is
# taken from the question and answer.
NAME_AFTER_ANSWER_S = 1.0

# Whom we do not ask to name it. `claude-code` keeps one live conversation for the whole
# time it runs, and an extra question in it would stay in the model's memory and get in the
# way of the answers of the conversation; the stub answers with its canned text anyway. For
# both, the name is made from the start of the description (`derive_title`).
NO_TITLE_PROVIDERS = frozenset({"claude-code", "stub"})

# We do not wait longer than this for the model to name it: the name is optional, and there
# is a fallback.
TITLE_TIMEOUT_S = 20.0


def is_chat_path(path: str) -> bool:
    return path in CHAT_EXACT or path.startswith(CHAT_PREFIXES)


class ChatEditError(ValueError):
    """An edit of the chat's own setting that is not acceptable (empty name)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.message = message


def chats_of(app: Argus) -> ChatManager | None:
    """The chat manager, if one has already been created. It does not create one itself."""
    found = getattr(app, "_chats", None)
    return found if isinstance(found, ChatManager) else None


def manager_of(app: Argus) -> ChatManager:
    found = chats_of(app)
    if found is None:
        found = ChatManager(app)
        app._chats = found
    return found


class ChatManager:
    def __init__(self, app: Argus) -> None:
        self.app = app
        self._active: Chat | None = None
        self._lock = asyncio.Lock()
        self._tracking: set[asyncio.Task] = set()
        # Deferred names by session: while the person is writing the description, the deadline is
        # pushed back (`_schedule_naming`).
        self._naming: dict[str, asyncio.Task] = {}
        self._last_speech_ts = 0.0

    # -- state ----------------------------------------------------------

    @property
    def store(self) -> ChatStore:
        # Anew each time: by itself it remembers nothing, and tests move `cfg.root` after the
        # application is built.
        return ChatStore(self.app.cfg.chats_dir)

    @property
    def active(self) -> Chat | None:
        return self._active

    @property
    def active_id(self) -> str | None:
        return self._active.id if self._active else None

    def brief(self) -> dict[str, Any] | None:
        """For the state snapshot: which chat is open. Does not touch the disk: the snapshot is assembled
        twice a second."""
        chat = self._active
        if chat is None:
            return None
        # `unnamed` means the name has not been invented yet: the settings page keeps the name field
        # empty while it is a placeholder.
        return {"id": chat.id, "name": chat.name, "unnamed": chat.name_state == "default"}

    def card(self, chat: Chat) -> dict[str, Any]:
        about = " ".join(str(chat.settings.get(SUMMARY_PATH) or "").split())
        return {
            "id": chat.id,
            "name": chat.name,
            "created": chat.created,
            "updated": chat.updated,
            "answers": chat.answers,
            "about": about[:200],
            "active": chat.id == self.active_id,
            # The name has not been invented yet (the person skipped it).
            "unnamed": chat.name_state == "default",
        }

    def overview(self) -> dict[str, Any]:
        try:
            self._migrate_legacy()
        except OSError:
            # Moving the earlier settings failed (the folder cannot be created); the list must not
            # disappear because of that: it will show what already exists anyway.
            log.exception("could not migrate the previous settings into the chat")
        chats = self.store.list()
        if self._active is not None:
            # The open chat is fresher in memory than on disk: the name may have just been changed, and
            # the answer counter grows during the conversation.
            chats = [self._active if c.id == self._active.id else c for c in chats]
        return {
            "chats": [self.card(c) for c in chats],
            "active": self.active_id,
            "running": bool(self.app.running),
        }

    # -- creating, name, copy, deleting ------------------------------------

    def create(self, name: str = "") -> Chat:
        """Create a session. Without a title (it was skipped) a placeholder is used, and the title is
        invented on its own once there is something to base it on (`_auto_name`)."""
        self._migrate_legacy()
        given = clean_name(name)
        return self.store.create(
            given or self._default_name(), self._fresh_settings,
            name_state="manual" if given else "default",
        )

    def _fresh_settings(self, chat_id: str) -> dict[str, Any]:
        """What a new chat has of its own from the first minute: an empty description, its own materials
        folder and the "take materials into account" checkbox turned on. It inherits the rest. All of
        this is chat content, not a habit of the person, and so it is written explicitly: otherwise
        the chat would pick up the description from the shared settings from the times when there
        were no chats, and the checkbox that was off then: files uploaded to a new chat would silently
        go unread."""
        return {
            SUMMARY_PATH: "",
            MATERIALS_DIR_PATH: self._materials_dir(chat_id),
            MATERIALS_ENABLED_PATH: True,
        }

    def _materials_dir(self, chat_id: str) -> str:
        return (Path(self.app.cfg.paths.chats) / chat_id / MATERIALS).as_posix()

    def _trusted(self, chat: Chat) -> dict[str, Any]:
        """Chat settings that can be trusted: only its own, and with the materials folder inside the
        chat itself.

        A chat folder can be brought from another computer or from sync, and the `chat.json` in it
        is someone else's file: while the layer was applied as is, it could set any application
        setting (the account server address, the materials folder path), and a materials folder
        outside the chat opened writing and deleting files anywhere (security audit, 2026-10-16).
        Foreign keys are dropped, and the materials folder is always `<chats>/<number>/materials`.
        """
        kept = {key: value for key, value in chat.settings.items() if is_chat_path(key)}
        if MATERIALS_DIR_PATH in kept:
            kept[MATERIALS_DIR_PATH] = self._materials_dir(chat.id)
        return kept

    def _default_name(self) -> str:
        taken = {c.name for c in self.store.list()}
        if DEFAULT_NAME not in taken:
            return DEFAULT_NAME
        n = 2
        while f"{DEFAULT_NAME} {n}" in taken:
            n += 1
        return f"{DEFAULT_NAME} {n}"

    def _load(self, chat_id: str) -> Chat:
        if self._active is not None and self._active.id == chat_id:
            return self._active
        chat = self.store.get(chat_id)
        if chat is None:
            raise KeyError(chat_id)
        return chat

    def rename(self, chat_id: str, name: str) -> Chat:
        name = clean_name(name)
        if not name:
            raise ChatEditError(NAME_PATH, "the name can't be empty")
        chat = self._load(chat_id)
        self.store.save(replace(chat, name=name, name_state="manual"))
        chat.name, chat.name_state = name, "manual"
        self._cancel_naming(chat_id)
        return chat

    def duplicate(self, chat_id: str) -> Chat:
        """A copy of the settings and materials without the history: a new conversation based on the old
        one, not its continuation."""
        source = self._load(chat_id)
        copy = self.store.create(
            clean_name(f"{source.name} (copy)"),
            lambda new_id: {**source.settings, MATERIALS_DIR_PATH: self._materials_dir(new_id)},
        )
        folder = self.app.cfg.resolve(Path(
            str(source.settings.get(MATERIALS_DIR_PATH) or "materials")
        ))
        if folder.is_dir():
            try:
                shutil.copytree(folder, self.store.folder(copy.id) / MATERIALS,
                                dirs_exist_ok=True)
            except OSError:
                log.exception("materials were not copied into the chat copy")
        return copy

    async def delete(self, chat_id: str) -> None:
        # Under the same lock as opening: a chat opens for a long time (we wait up to `SETTLE_S` for
        # an answer to finish), and a deletion from a second tab in those seconds would see "not
        # open", erase the folder, and the opening would then make open a chat that no longer
        # exists. The window would show a ghost session and silently drop every answer.
        async with self._lock:
            # If the folder exists, it can be deleted even when the `chat.json` in it is unreadable:
            # otherwise there would be no way to remove a corrupted session.
            if not valid_id(chat_id) or not self.store.folder(chat_id).is_dir():
                raise KeyError(chat_id)
            self._cancel_naming(chat_id)
            if self.active_id == chat_id:
                await self._swap(None)      # not `deactivate`: the lock is not reentrant
            self.store.delete(chat_id)

    # -- open and close ---------------------------------------------------

    async def activate(self, chat_id: str) -> Chat:
        """Open a chat. An already open one is left alone: the page was reloaded while the conversation
        goes on, and it must not be reset because of F5."""
        async with self._lock:
            if self._active is not None and self._active.id == chat_id:
                return self._active
            chat = self.store.get(chat_id)
            if chat is None:
                raise KeyError(chat_id)
            await self._swap(chat)
            if self.store.get(chat_id) is None:
                # While it was opening, the folder was removed (by Explorer, by another instance): the
                # window does not need a ghost, so we return to the shared settings.
                await self._swap(None)
                raise KeyError(chat_id)
            chat.updated = time.time()
            self.store.save(chat)
            return chat

    async def deactivate(self) -> None:
        """Return to the shared settings and a clean timeline, as at startup."""
        async with self._lock:
            if self._active is not None:
                await self._swap(None)

    async def _swap(self, chat: Chat | None) -> None:
        app = self.app
        await self._settle()
        if app.running:
            # We switch off before the change, not after: while recognition is running, utterances of
            # the old conversation still fly into the timeline of the new one.
            app.set_running(False)

        overlay = self._trusted(chat) if chat else {}
        store = store_of(app)
        try:
            cfg, changed = store.set_overlay(overlay)
        except ValidationError:
            # The chat settings fail validation: someone edited the file by hand, or the model
            # changed. Opening the chat with the shared settings beats not opening it at all:
            # the person will see the history and can fix the field.
            log.exception("chat settings %s: invalid — opening with the shared ones", chat.id if chat else "")
            cfg, changed = store.set_overlay({})
        self._push(cfg, changed)

        await app.reset_conversation(feed=FEED if chat else 20)
        self._fresh_gate()
        self._active = chat
        if chat is not None:
            self._restore(chat)

    async def _settle(self) -> None:
        """Wait for the model to finish its answer, so that it does not end up in another chat. We do
        not wait longer than `SETTLE_S`: the previous conversation is closed anyway, and whatever
        arrives later is cut off by the assistant's `epoch`."""
        assistant = self.app.assistant
        deadline = time.monotonic() + SETTLE_S
        while assistant is not None and assistant.busy and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

    def _fresh_gate(self) -> None:
        from argus.assist import gate as assist_gate
        from argus.assist.settings import read

        if self.app.assistant is not None:
            assist_gate.install(self.app, read(self.app.cfg), keep_counters=False)

    def _push(self, cfg: Config, changed: set[str]) -> None:
        """Carry the chat layer to the live pipeline the same way as an edit on the settings page
        (`AdminPanel._push`): first into the same config object that the nodes hold, then the
        appliers. Only what changed is poured over (`adopt_changed`): the window pinned for screen
        reading and the choice of sources are machine state, not chat state."""
        from argus.ui.admin import apply_assist
        from argus.ui.admin_base import adopt_changed, apply_core

        adopt_changed(self.app.cfg, cfg, changed)
        for apply_live in (apply_core, apply_assist):
            try:
                apply_live(self.app, self.app.cfg, changed)
            except Exception:
                log.exception("could not apply the chat settings on the fly")

    # -- editing the settings of the open chat --------------------------------------

    def plan_edit(self, patch: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """What the chat settings will become after an edit, without changing anything.

        Returns the whole layer and the new name (if it was edited). An invalid name raises
        `ChatEditError`; invalid field values are checked by the caller the same way as for the
        shared ones, by building a config with this layer.
        """
        chat = self._active
        if chat is None:
            raise KeyError("no chat is open")
        patch = dict(patch)
        name = None
        if NAME_PATH in patch:
            name = clean_name(patch.pop(NAME_PATH))
            if not name:
                raise ChatEditError(NAME_PATH, "the name can't be empty")
        return {**self._trusted(chat), **patch}, name

    def commit_edit(
        self, overlay: dict[str, Any], name: str | None
    ) -> tuple[Config, set[str]]:
        """Accept an edit verified by `plan_edit`: into the chat file and into the layer.

        Disk first, then memory: if the write fails (the folder was removed, the disk is full) we
        raise `OSError`, and nothing has changed in either place. The other way around, the page
        would show an accepted edit that is gone after a restart.
        """
        chat = self._active
        assert chat is not None
        state = "manual" if name is not None else chat.name_state
        self.store.save(replace(
            chat, settings=dict(overlay), name=name or chat.name, name_state=state,
        ))
        cfg, changed = store_of(self.app).set_overlay(overlay)
        chat.settings = dict(overlay)
        if name is not None:
            chat.name, chat.name_state = name, "manual"
            self._cancel_naming(chat.id)
        elif chat.name_state == "default" and (
            len(str(overlay.get(SUMMARY_PATH) or "").strip()) >= NAME_MIN_CHARS
        ):
            # A description has appeared, so we name the session once the person stops typing.
            self._schedule_naming(chat.id, NAME_DELAY_S)
        return cfg, changed

    # -- history ------------------------------------------------------------

    def _restore(self, chat: Chat) -> None:
        """Put back on the page the answers recorded earlier in this chat."""
        from argus.app import Shot

        state = self.app.state
        history = self.store.history(chat.id)
        records = [r for r in history if r.get("k") == "a"][-FEED:]
        # Conversation lines go to the work screen too: someone opened a session that had
        # history and saw nothing there. They are for display only and do not go into the
        # model's timeline.
        state.past_speech = [
            {"wall": float(r.get("ts") or 0), "source": str(r.get("src") or "them"),
             "text": str(r.get("text") or "")}
            for r in history if r.get("k") == "s" and str(r.get("text") or "").strip()
        ][-FEED:]
        for r in records:
            state.answers.append(Answer(
                text=str(r.get("text") or ""),
                latency_ms=float(r.get("latency_ms") or 0),
                provider=str(r.get("provider") or ""),
                model=str(r.get("model") or ""),
                meta={
                    "trigger": r.get("trigger") or "",
                    "reason": r.get("reason") or "",
                    "cause": r.get("cause") or "",
                    "deep": bool(r.get("deep")),
                    "image_kb": int(r.get("image_kb") or 0),
                    "image_src": r.get("image_src") or "screen",
                },
            ))
        # Frames are restored with a negative time. Their real time comes from a previous run
        # and has nothing in common with the current one: it would land among the fresh lines
        # in the wrong place. The minus sign tells the page that the frame is "from before this
        # visit": answer cards need the frame (`answerShots`), but it does not belong in the
        # "what is happening now" feed (`merge` in index.html). The number is also the address
        # of the image, so every frame of every chat gets its own (`restored_shot_id`).
        for r in records[-SHOTS_KEPT:]:
            shot_id = r.get("shot")
            jpeg = self.store.load_shot(chat.id, shot_id) if shot_id is not None else None
            if jpeg is not None:
                state.shots.append(Shot(
                    t=restored_shot_id(chat.id, shot_id), jpeg=jpeg,
                    kind=str(r.get("image_src") or "screen"),
                ))

    async def run(self) -> None:
        """Write the history of the open chat. Lives as long as the server lives."""
        bus = self.app.bus
        answers = bus.subscribe(Answer, maxsize=64, name="chats-answers")
        speech = bus.subscribe(SpeechEvent, maxsize=256, name="chats-speech")
        try:
            await asyncio.gather(self._watch_answers(answers), self._watch_speech(speech))
        finally:
            answers.close()
            speech.close()
            for task in [*self._tracking, *self._naming.values()]:
                task.cancel()

    async def _watch_answers(self, sub) -> None:
        async for answer in sub:
            try:
                chat = self._active
                assistant = self.app.assistant
                if chat is None or (
                    assistant is not None
                    and answer.meta.get("epoch", assistant.epoch) != assistant.epoch
                ):
                    # No chat is open, so there is nowhere to write. An answer from an already closed
                    # conversation that reaches us over the bus after the switch is also skipped: it
                    # would land in the history of the chat that is open now.
                    continue
                # We take the frame and the chat now, not when the answer finishes streaming: by then
                # another chat may be open or another screenshot taken.
                shot = self._shot_for(answer)
                task = asyncio.create_task(self._record_answer(chat.id, answer, shot))
                self._tracking.add(task)
                task.add_done_callback(self._tracking.discard)
            except Exception:
                # One bad answer must not stop the recording of all the ones after it: without this
                # the loop died silently and the history stopped growing until a restart.
                log.exception("reply was not accepted into the chat history")

    def _shot_for(self, answer: Answer):
        kb = int(answer.meta.get("image_kb") or 0)
        shots = self.app.state.shots
        if kb and shots and shots[-1].kb == kb:
            return shots[-1]
        return None

    async def _record_answer(self, chat_id: str, answer: Answer, shot) -> None:
        # A streamed answer is published empty and grows in place, so we write it when it is
        # fully read (`Answer.done`). The others arrive already complete.
        deadline = time.monotonic() + 600
        while not answer.done and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        if not answer.ok:
            return          # an error or an empty answer does not belong in the history

        record: dict[str, Any] = {
            "k": "a",
            "ts": time.time(),
            "text": answer.text,
            "cause": answer.meta.get("cause") or "",
            "trigger": answer.meta.get("trigger") or "",
            "reason": answer.meta.get("reason") or "",
            "provider": answer.provider,
            "model": answer.model,
            "latency_ms": round(answer.latency_ms),
            "deep": bool(answer.meta.get("deep")),
            "image_kb": int(answer.meta.get("image_kb") or 0),
            "image_src": answer.meta.get("image_src") or "screen",
        }
        store = self.store
        try:
            if shot is not None and store.save_shot(chat_id, shot.t, shot.jpeg):
                record["shot"] = shot.t
            store.append(chat_id, record)
            store.prune_shots(chat_id)      # no more frames on disk than the page can fetch
            chat = self._load_quiet(chat_id)
            if chat is not None:
                chat.answers += 1
                chat.updated = record["ts"]
                store.save(chat)
                if chat.name_state == "default":
                    # There was no description but the conversation has started, so we name the session
                    # from it.
                    self._schedule_naming(chat_id, NAME_AFTER_ANSWER_S)
        except Exception:
            # Not only `OSError`: the task runs without an owner, and an unhandled exception in it
            # is visible nowhere except as "Task exception was never retrieved" at exit.
            log.exception("could not write the reply to the chat history")

    def _load_quiet(self, chat_id: str) -> Chat | None:
        if self._active is not None and self._active.id == chat_id:
            return self._active
        return self.store.get(chat_id)

    def _next_speech_ts(self) -> float:
        """The timestamp of a new utterance in the log: strictly greater than the previous one. The
        timestamp is also the utterance key for record edits (`recap/material.py`), and the Windows
        clock has a step of about 16 ms: two channels that finish speaking in the same tick would get
        the same key, and an edit to one would hit the other."""
        self._last_speech_ts = ts = max(time.time(), self._last_speech_ts + 1e-6)
        return ts

    async def _watch_speech(self, sub) -> None:
        async for event in sub:
            try:
                chat = self._active
                if chat is None or event.error or event.engine or not event.text.strip():
                    continue        # errors and the shadow engine are not part of the conversation
                if event.t_end <= self.app.conversation_since:
                    continue        # ended before the chat switch, so it belongs to the previous conversation
                self.store.append(chat.id, {
                    "k": "s", "ts": self._next_speech_ts(), "src": event.source.value,
                    "text": event.text.strip(),
                })
            except Exception:
                # Same as for answers: one line must not stop the recording of the rest.
                log.exception("could not write the utterance to the chat history")

    # -- naming from the description ------------------------------------------------

    def _cancel_naming(self, chat_id: str) -> None:
        task = self._naming.pop(chat_id, None)
        if task is not None:
            task.cancel()

    def _schedule_naming(self, chat_id: str, delay: float) -> None:
        """Name the session after `delay` seconds. A new reason (another edit of the description) pushes
        the deadline back: while the person is writing, there is nothing to name."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return          # there is no event loop (synchronous call), so nobody can do the naming
        self._cancel_naming(chat_id)
        task = loop.create_task(self._name_later(chat_id, delay))
        self._naming[chat_id] = task

        def forget(done: asyncio.Task, chat_id: str = chat_id) -> None:
            if self._naming.get(chat_id) is done:
                del self._naming[chat_id]

        task.add_done_callback(forget)

    async def _name_later(self, chat_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._auto_name(chat_id)

    async def _auto_name(self, chat_id: str) -> None:
        """Come up with a name for a session the person left unnamed.

        From the description of the conversation, or, if there is none, from the first question
        and answer. A name given by the person is not touched, and one that was invented is not
        changed again.
        """
        chat = self._load_quiet(chat_id)
        if chat is None or chat.name_state != "default":
            return
        source = self._naming_source(chat)
        if source is None:
            return
        shown, fallback = source
        title = await self._model_title(shown) or derive_title(fallback)
        if not title:
            return
        # While we waited for the model, the person could have named the session themselves
        # or deleted it.
        chat = self._load_quiet(chat_id)
        if chat is None or chat.name_state != "default":
            return
        try:
            self.store.save(replace(chat, name=title, name_state="auto"))
        except OSError:
            log.exception("could not write the generated title")
            return
        chat.name, chat.name_state = title, "auto"
        log.info("session titled from the description: %s", title)

    def _naming_source(self, chat: Chat) -> tuple[str, str] | None:
        """What to show the model and what to take the fallback title from. `None` means there is
        nothing to go on yet."""
        summary = str(chat.settings.get(SUMMARY_PATH) or "").strip()
        if len(summary) >= NAME_MIN_CHARS:
            return summary, summary
        for record in self.store.history(chat.id):
            if record.get("k") != "a":
                continue
            cause = str(record.get("cause") or "").strip()
            answer = str(record.get("text") or "").strip()
            if cause or answer:
                shown = f"Question: {cause}\nAnswer: {answer[:600]}" if cause else answer[:600]
                return shown, cause or answer
        return None

    async def _model_title(self, text: str) -> str:
        """The title from the model; empty means it is absent, did not answer, or answered with something
        that is not a title (then `derive_title`)."""
        assistant = self.app.assistant
        provider = assistant.provider if assistant is not None else None
        if provider is None or provider.name in NO_TITLE_PROVIDERS:
            return ""
        try:
            answer = await asyncio.wait_for(
                provider.ask(title_prompt(text), system=TITLE_SYSTEM), TITLE_TIMEOUT_S
            )
        except Exception:  # no network, a quota limit, a server refusal: we will name it ourselves
            log.info("model did not come up with a title - using the beginning of the description", exc_info=True)
            return ""
        return clean_title(answer.text) if answer.ok else ""

    # -- sessions that existed before sessions ---------------------------------------

    def _migrate_legacy(self) -> None:
        """First launch with chats: what the person had already configured is not lost.

        Before chats, the conversation context and the materials folder were one for the whole
        application. If there is something there, it becomes the first chat, with its own story
        and the old `materials/` folder, from which nothing is moved. Otherwise the person would
        open the updated application and not find the lectures they had been putting in for a
        week.

        It fires while there is no chats folder at all: after the first chat (even one deleted
        later) the folder exists, and nothing is created again.
        """
        if self.app.cfg.chats_dir.exists():
            return
        from argus.assist import materials as materials_mod
        from argus.assist.settings import read

        cfg = self.app.cfg
        settings = read(cfg)
        folder = cfg.resolve(Path(materials_mod.DIRNAME))
        has_files = folder.is_dir() and any(
            p.is_file() and p.suffix.lower() in materials_mod.SUFFIXES
            for p in folder.iterdir()
        )
        if not (settings.profile.summary.strip() or has_files):
            return

        mine: dict[str, Any] = {
            path: value
            for path, value in flatten(store_of(self.app).overrides).items()
            if is_chat_path(path)
        }
        mine[SUMMARY_PATH] = settings.profile.summary
        mine[MATERIALS_DIR_PATH] = materials_mod.DIRNAME
        self.store.create("Main session", lambda _id: mine)
        log.info("settings that existed before sessions became the session 'Main session'")
