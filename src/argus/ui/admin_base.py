"""The base of the admin page, shared by all three channels.

The dashboard (`ui/server.py`) is a gauge: it shows what is happening inside the
pipeline right now. The admin page answers a different question: "how is this
configured, and why". They are separated on purpose. You cannot turn settings during a
live session, and configuring blind, without the numbers at hand, is even worse. So the
admin page pulls the same counters but lives on its own page.

Only the frame is here. It does three things and knows nothing about channels:

* **describe a setting**: path, label, limits. Limits are not rewritten by hand but
  taken from the models in `core/config.py`. A range written twice that has drifted
  apart is a bug that will surface in production, not in a test;
* **save an edit** through `SettingsStore`, with validation before the write;
* **apply on the live pipeline** whatever can be applied on the fly, and honestly say
  "restart needed" about the rest. Lying here is the most costly mistake: a person
  turns a threshold, nothing changes, and they keep turning.

The sections are declared by the branch, in its own `ui/admin.py`. Channels differ not
in styling but in what makes sense to configure at all: in an interview it is the resume
and stealth, on a stream it is the character and the limit of messages to the chat, in
voice it is the latency budget in milliseconds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

# At the top level, not next to the other FastAPI imports inside `attach()`. Everywhere
# below, this file imports FastAPI lazily, but with `Request` as the type of a route
# PARAMETER, a lazy import does not work. `from __future__ import annotations` turns the
# annotation into a string, and FastAPI resolves it through `typing.get_type_hints`,
# looking at the module's `__globals__`, not at the local names of the method where it
# would have been declared. A local import does not resolve that string. FastAPI quietly
# decides that the parameter is not "give me the request" but a required string in the
# address, and `/admin` answers 422 on every visit. Only `Request` needed a separate
# import: `Response` and `HTMLResponse` below stand as VALUES (used in the body, in
# instantiation), and the same trap does not reach them.
from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError

from argus.core.config import Config
from argus.live import header as build_header
from argus.live import live
from argus.core.settings_store import (
    drop_path,
    flatten,
    get_path,
    set_path,
    store_of,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from argus.app import Argus

log = logging.getLogger(__name__)

# The name of the open chat on the settings page (`argus/chats/manager.py`).
_CHAT_NAME = "chat.name"

# The kinds of fields that `admin.js` knows how to draw.
KINDS = ("bool", "int", "float", "line", "text", "choice", "chips", "secret", "note",
         "hotkey")


@dataclass(slots=True)
class Setting:
    """One setting on the page.

    `hot` is the main field. It says whether an edit reaches the pipeline right now or lands
    in the file and waits for a restart. Model weights take a minute to load, so the
    recognition model cannot be changed on the fly; the filter threshold can.

    `todo` is the second honesty. Some settings describe what the channel does not have yet:
    TTS in `voice`, the chat connection in `twitch`. The value is saved and waits for the
    code, but the user must see that for now it affects nothing. Silently accepting such a
    setting would be deception.
    """

    path: str
    label: str
    kind: str = "line"
    hint: str = ""
    unit: str = ""
    hot: bool = True
    todo: str = ""
    options: Sequence[tuple[Any, str]] = ()
    rows: int = 4
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None

    # Show the row only while at least one of the listed fields is set to the value it needs:
    # ((path, value), ...), the conditions are joined by "or". For example, "bot name" is
    # meaningless while neither `mic_mode` nor `system_mode` is set to `wake_word`: it would
    # be a field about something that cannot exist now for either speech source. The value is
    # still accepted and stored in any mode: switching off both modes must not erase a name
    # that was once entered.
    depends_on: tuple[tuple[str, Any], ...] | None = None

    # The setting exists (`apply()` accepts it, it is in `A.values`) but gets no row of its
    # own in `.fields`: the channel block (`widget`) draws it itself, in its own markup. It
    # is needed where there are many values and they are not known in advance (a voice per
    # character). Creating a standard row for each would make a section of fifty identical
    # switches.
    hidden: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown field kind: {self.kind}")


@dataclass(slots=True)
class Section:
    """An admin section.

    `lead` is not decoration. Almost every setting in this project has a history: 1920
    instead of 1280 is worth a measurement, and so is the 0.35 threshold. A person who
    opens the admin page a month later does not remember that history, and without an
    explanation they will turn the knob back. That is why every section has a line about
    why it exists.
    """

    id: str
    title: str
    lead: str = ""
    settings: list[Setting] = field(default_factory=list)

    # The name of a block that the branch draws itself: measuring the leak into headphones, a
    # voice trial, a video-memory calculator. Such things cannot be expressed as a form of
    # fields.
    widget: str = ""
    badge: str = ""

    # Whose it is: "chat" is a setting of one conversation, and each chat has its own
    # (`argus/chats/`); "app" is common to all. The home page shows only the common ones:
    # while you are not inside any conversation, there is nothing to configure for "this
    # conversation".
    scope: str = "app"


# What to do with the live pipeline after an edit. The branch adds its own.
Applier = Callable[["Argus", Config, set[str]], None]

# A channel's own routes: measuring the leak into headphones, a voice trial, a check of
# the chat connection. A form of fields cannot express such things, and they cannot be
# dragged into the common frame, because they are different for all three channels.
Routes = Callable[["FastAPI"], None]


class AdminPanel:
    """The admin panel of one channel."""

    def __init__(
        self,
        app: Argus,
        *,
        title: str,
        subtitle: str,
        channel: str,
        accent: str,
        sections: list[Section],
        page: Path,
        channel_model: type[BaseModel] | None = None,
        extra_channel_models: dict[str, type[BaseModel]] | None = None,
        appliers: Sequence[Applier] = (),
        routes: Sequence[Routes] = (),
    ) -> None:
        self.app = app
        self.title = title
        self.subtitle = subtitle
        self.channel = channel
        self.accent = accent
        self.sections = sections
        self.page = page
        self.channel_model = channel_model
        # A second (third, ...) free-form key of the `channel` section that lives in the same
        # panel, for example the test voice-over next to the conversation settings. Its own model
        # must also supply the defaults in `values()`, not only the single `channel` that the
        # panel calls its own.
        self.extra_channel_models: dict[str, type[BaseModel]] = extra_channel_models or {}
        self.appliers: list[Applier] = [apply_core, *appliers]
        self.extra_routes: list[Routes] = list(routes)
        # One per app, not its own: the layer of the open chat lives in the store, and a second
        # copy would build a config without it (see `store_of`).
        self.store = store_of(app)

        # Settings that are already written but will reach the pipeline only on the next start.
        # The list accumulates until the restart. Otherwise the "restart needed"
        # badge would disappear when the page reloads, and the person would go off to work
        # on the old settings, thinking they were new.
        self.restart_pending: set[str] = set()

        self._index: dict[str, Setting] = {
            s.path: s for section in sections for s in section.settings
        }

    # -- description for the page ---------------------------------------------

    def values(self) -> dict[str, Any]:
        """Current values by flat paths.

        The channel defaults are added to the core settings. The `channel` section is free-form
        and may be absent from the config entirely, and without this step the first visit to the
        admin page would show empty fields instead of default values. The same goes for every
        `extra_channel_models`: its own free-form key of the `channel` section, whose defaults
        it is responsible for itself.
        """
        values = self.store.values()
        models = dict(self.extra_channel_models)
        if self.channel_model is not None:
            models[self.channel] = self.channel_model
        for key, model in models.items():
            defaults = flatten({"channel": {key: model().model_dump(mode="json")}})
            for path, value in defaults.items():
                values.setdefault(path, value)
        # The name of the open chat is not a config field but a property of the chat itself. The
        # page gets it together with the other values and edits it by the same path.
        chat = self._chat()
        if chat is not None:
            # A name that the person did not write (skipped) does not fill the field: it stays empty,
            # with the hint "It will be made up for you — or write your own". The "New session"
            # placeholder would read in it as a real name that has to be erased.
            values[_CHAT_NAME] = "" if chat.name_state == "default" else chat.name
        return values

    def _chat(self):
        """The open chat — if the app is currently in a chat."""
        from argus.chats import chats_of

        manager = chats_of(self.app)
        return manager.active if manager is not None else None

    def schema(self, scope: str = "") -> dict[str, Any]:
        """Sections and values for the page.

        `scope="app"` gives only the common sections (the home page: it is not inside any chat).
        Without it, all sections. Then the chat name is in the field list only if a chat is
        open, since an edit of the name would otherwise have nowhere to go.
        """
        values = self.values()
        in_chat = self._chat() is not None
        sections = [s for s in self.sections if scope != "app" or s.scope == "app"]
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "channel": self.channel,
            "accent": self.accent,
            "file": str(self.store.path),
            "version": self._version(),
            "sections": [
                {
                    "id": s.id,
                    "title": s.title,
                    "lead": s.lead,
                    "widget": s.widget,
                    "badge": s.badge,
                    "scope": s.scope,
                    "settings": [
                        self._setting_json(x) for x in s.settings
                        if in_chat or x.path != _CHAT_NAME
                    ],
                }
                for s in sections
            ],
            "values": values,
            "overridden": sorted(self.store.changed()),
            "restart_pending": sorted(self.restart_pending),
        }

    def _version(self) -> dict[str, str]:
        """What to show in the header and at the bottom of the settings page. This string is what
        people ask for when they request the log. Without it, it is unclear whether an update
        has reached the other person or they are still looking at the build from the day before
        yesterday.

        `VERSION` is placed by `packaging/stage.py` next to the built `.exe` as
        `"1.0+7cd1c96"`. The number is for the person; the commit hash after `+` is what
        `update.py` uses to compare versions, and it is also what people are asked to send to
        support when the number alone is not enough (a deploy fix without a number bump is also
        a new build). A working copy from source has no such file and never will: there the
        version is asked of `git log`, not of the settings page.
        """
        from argus.update import read_version

        raw = read_version(self.store.path.parent)
        if not raw:
            return {"number": "source", "build": ""}
        number, _, build = raw.partition("+")
        return {"number": number, "build": build}

    def _setting_json(self, s: Setting) -> dict[str, Any]:
        low, high, step = self._limits(s)
        return {
            "path": s.path,
            "label": s.label,
            "kind": s.kind,
            "hint": s.hint,
            "unit": s.unit,
            "hot": s.hot,
            "todo": s.todo,
            "hidden": s.hidden,
            "rows": s.rows,
            "placeholder": s.placeholder,
            "options": [{"value": v, "label": t} for v, t in s.options],
            "min": low,
            "max": high,
            "step": step,
            "depends_on": (
                [{"path": p, "value": v} for p, v in s.depends_on]
                if s.depends_on else None
            ),
        }

    def _limits(self, s: Setting) -> tuple[float | None, float | None, float | None]:
        """Field limits: those set by hand win, the rest come from the model.

        Duplicating `ge=0.1, le=0.95` in the field description means creating a second truth.
        It will diverge from the first on the first config edit, and the admin page will start
        accepting values that the pipeline later rejects.
        """
        low, high = s.minimum, s.maximum
        info = _field_info(self._model_for(s.path), _tail(s.path))
        if info is not None:
            for mark in info.metadata:
                if low is None and hasattr(mark, "ge"):
                    low = mark.ge
                if low is None and hasattr(mark, "gt"):
                    low = mark.gt
                if high is None and hasattr(mark, "le"):
                    high = mark.le
                if high is None and hasattr(mark, "lt"):
                    high = mark.lt
        step = s.step
        if step is None:
            step = 1 if s.kind == "int" else 0.1 if s.kind == "float" else None
        return low, high, step

    def _model_for(self, path: str) -> type[BaseModel] | None:
        if path.startswith("channel."):
            key = path.split(".", 2)[1]
            if key == self.channel:
                return self.channel_model
            return self.extra_channel_models.get(key)
        return Config

    # -- edit -------------------------------------------------------------

    def apply(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate, save, apply. Return errors by field.

        Errors are collected by path, not in a single line: a form of twenty fields with the
        message "validation error" at the top of the page is useless.
        """
        patch = {k: v for k, v in patch.items() if k in self._index}
        if not patch:
            return {"ok": True, "changed": [], "restart": sorted(self.restart_pending)}

        # A chat is open: its settings (who you are, tone, materials, whom to answer) go into the
        # chat itself, not into `argus.local.json`. Otherwise an edit in one conversation would
        # change all the others. No chat: everything as before.
        mine: dict[str, Any] = {}
        if self._chat() is not None:
            from argus.chats.manager import is_chat_path

            mine = {k: v for k, v in patch.items() if k == _CHAT_NAME or is_chat_path(k)}
        rest = {k: v for k, v in patch.items() if k not in mine}
        manager = None
        overlay: dict[str, Any] | None = None
        name: str | None = None

        try:
            if mine:
                from argus.chats import chats_of
                from argus.chats.manager import ChatEditError

                manager = chats_of(self.app)
                try:
                    overlay, name = manager.plan_edit(mine)
                except ChatEditError as e:
                    return {"ok": False, "errors": {e.path: e.message}}
            cfg = self.store.build(rest, overlay=overlay)
            self._validate_channel(cfg)
        except ValidationError as e:
            return {"ok": False, "errors": _errors(e, patch)}

        changed: set[str] = set()
        if manager is not None and overlay is not None:
            try:
                cfg, delta = manager.commit_edit(overlay, name)
            except OSError:
                log.exception("chat settings were not saved")
                return {"ok": False, "errors": {
                    next(iter(mine)): "couldn't save the session settings to disk"}}
            changed |= delta
        failure: dict[str, Any] | None = None
        if rest:
            try:
                cfg, delta = self.store.apply(rest)
                changed |= delta
            except ValidationError as e:
                # The file managed to change outside, and the edit does not match it.
                failure = {"ok": False, "errors": _errors(e, rest)}
            except OSError:
                # The disk is full or another program holds the file: the value is written neither to the
                # file nor to the store's memory (`SettingsStore.apply` works on a copy). We say exactly
                # that, rather than answering with a server error page.
                log.exception("settings were not saved")
                failure = {"ok": False, "errors": {
                    next(iter(rest)): "couldn't save the settings to disk"}}
        # The session settings are already written (above), and they reach the pipeline whatever
        # the outcome. Otherwise what was saved would take effect only after a restart.
        if failure is None or changed:
            self._push(cfg, changed)

        self.restart_pending |= {
            p for p in changed if p in self._index and not self._index[p].hot
        }
        if failure is not None:
            return failure
        return {
            "ok": True,
            "changed": sorted(changed),
            "restart": sorted(self.restart_pending),
            "overridden": sorted(self.store.changed()),
            "values": self.values(),
        }

    def reset(self, paths: list[str] | None) -> dict[str, Any]:
        cfg, changed = self.store.reset(paths)
        self._push(cfg, changed)
        self.restart_pending |= {
            p for p in changed if p in self._index and not self._index[p].hot
        }
        return self._result(changed)

    def _result(self, changed: set[str]) -> dict[str, Any]:
        return {
            "ok": True,
            "changed": sorted(changed),
            "restart": sorted(self.restart_pending),
            "overridden": sorted(self.store.changed()),
            "values": self.values(),
        }

    def _push(self, cfg: Config, changed: set[str]) -> None:
        """Pour the new values into the live pipeline and call the appliers.

        The order matters more than it seems. First the values are poured into **that very**
        config object that the pipeline nodes hold, and only then are the appliers called, on
        it. If they were handed a fresh object, everything they fix in it (the assembled
        prompt, the rebuilt filter) would stay in an object that nobody reads anymore. The edit
        would be saved to the file and never reach the model, without a single error in the log.
        """
        adopt_changed(self.app.cfg, cfg, changed)
        for apply_live in self.appliers:
            try:
                apply_live(self.app, self.app.cfg, changed)
            except Exception:
                # One failure must not eat the whole edit: the remaining appliers must still run, and the
                # values are already in the file.
                log.exception("could not apply settings on the fly")

    def _validate_channel(self, cfg: Config) -> None:
        """Validate the channel section(s) with their own models.

        The core stores `channel` as a free-form dict and does not look inside it; otherwise
        fields needed by exactly one branch would have to be dragged into master. So only the
        branch can validate them, and it does so here, before the write. The same goes for
        `extra_channel_models`: the second free-form key of the same panel is validated by its
        own model, not silently skipped.
        """
        if self.channel_model is not None:
            self.channel_model.model_validate(cfg.channel.get(self.channel, {}))
        for key, model in self.extra_channel_models.items():
            model.model_validate(cfg.channel.get(key, {}))

    # -- routes -----------------------------------------------------------

    def attach(self, web: FastAPI) -> None:
        from fastapi import Response
        from fastapi.responses import HTMLResponse, RedirectResponse

        no_cache = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
        # The live folder, if the sources lie next to the build: the settings page must not lag
        # behind the dashboard. See `argus/live.py`.
        shared = live(Path(__file__).parent / "static")

        @web.get("/admin", response_class=HTMLResponse)
        async def admin_page(request: Request) -> Response:
            # Settings already open as a panel over the dashboard, through the same menu as
            # "account". A direct visit to `/admin` (a hand-typed address, an old bookmark, a page
            # refresh) would draw a second, dissimilar version of the same form: its own separate
            # heading, its own "save" button, its own menu behind three bars, with the same two items
            # as the first one. We send it back to the dashboard.
            #
            # Except in two cases where there is nowhere to send it:
            #
            # `?embedded=1` is the dashboard panel itself: it is the very request that would
            # otherwise fly into an endless redirect to itself.
            #
            # `not self.app.started` is the `argus settings` mode. `start()` is deliberately not
            # called there: three working copies share one sound card, and bringing up the full
            # pipeline is unnecessary and risky, because a dashboard with a working button would open
            # devices that another copy occupies. There is simply no dashboard there, and nowhere to
            # send anyone.
            if "embedded" not in request.query_params and self.app.started:
                return RedirectResponse(url="/#admin")
            # With a version marker, like the dashboard: `argus settings` opens this page as a
            # separate tab, and the question "did the edit arrive" is asked here no less often. The
            # answer must come from a single `curl -I` on any page, not only the main one.
            return HTMLResponse(
                self.page.read_text(encoding="utf-8"),
                headers={**no_cache, "X-Argus-Build": build_header()},
            )

        @web.get("/admin/admin.css")
        async def admin_css() -> Response:
            return Response(
                (shared / "admin.css").read_text(encoding="utf-8"),
                media_type="text/css",
                headers=no_cache,
            )

        @web.get("/admin/admin.js")
        async def admin_js() -> Response:
            return Response(
                (shared / "admin.js").read_text(encoding="utf-8"),
                media_type="application/javascript",
                headers=no_cache,
            )

        @web.get("/admin/schema")
        async def admin_schema(scope: str = "") -> dict:
            return self.schema(scope)

        @web.post("/admin/apply")
        async def admin_apply(payload: dict) -> dict:
            values = payload.get("values") or {}
            if not isinstance(values, dict):
                # The page sends a set of "setting: value" pairs. With a string or a number, `dict()`
                # would fail with a 500 error, and hotkeys and scripts call here not only from the page.
                raise HTTPException(status_code=400, detail="values must be a set of settings")
            return self.apply(dict(values))

        # There is no reset from the panel anymore: the "restore
        # argus.toml" button wiped everything at once, and it was pressed by accident, twice in
        # one evening. The route is kept for a targeted reset of a single field, which the panel
        # does not have yet. Nobody needs to do a general reset through it, and there is no reason
        # to.
        @web.post("/admin/reset")
        async def admin_reset(payload: dict | None = None) -> dict:
            payload = payload or {}
            paths = payload.get("paths")
            if paths is not None and not isinstance(paths, list | tuple):
                raise HTTPException(status_code=400, detail="paths must be a list")
            return self.reset(None if payload.get("all") else list(paths or []))

        for add in self.extra_routes:
            add(web)


# --------------------------------------------------------------------------
# Applying on the live pipeline
# --------------------------------------------------------------------------


def adopt(cfg: Config, fresh: Config) -> None:
    """Pour the new values into **the same** config object.

    Not replace the reference but pour in the fields. At assembly the pipeline handed out
    references to `cfg.brain`, `cfg.ocr`, and the rest to all its nodes. Swapping `app.cfg`
    would leave them all on the old object, and the edit would silently reach nowhere. This
    is the nastiest kind of breakage, with not a single error.
    """
    for name in Config.model_fields:
        if name in ("root", "config_path"):
            continue
        current = getattr(cfg, name)
        incoming = getattr(fresh, name)
        if isinstance(current, BaseModel) and isinstance(incoming, BaseModel):
            for inner in type(current).model_fields:
                setattr(current, inner, getattr(incoming, inner))
        else:
            setattr(cfg, name, incoming)


def adopt_changed(cfg: Config, fresh: Config, changed: set[str]) -> None:
    """Pour into the live config **only what the edit really changed**.

    `adopt` above poured in the whole config, and together with the edit it overwrote
    everything that the app itself had set in the live object and recorded nowhere: the
    window that the person pinned for screen reading, the windows for "system sound", the
    "Screen" checkbox turned off, the system prompt assembled from the
    self-description. An edit of a neighboring field returned them to the defaults: the
    "Screen" checkbox stayed off while frames went to the model, and the prompt lost the
    conversation context. While only the settings page was edited, this happened rarely.
    Opening a chat does it on every switch, so only `changed` is poured in: the paths whose
    value really became different (including the vanished ones: a chat's key disappears
    together with the chat).
    """
    for path in sorted(changed):
        head, _, rest = path.partition(".")
        if head in ("root", "config_path"):
            continue
        if head == "channel":
            # A free-form section is an ordinary dict: the path either exists in the new config (we
            # put it in) or has vanished (we remove it together with the parents that became empty).
            if rest and _has_path(fresh.channel, rest):
                set_path(cfg.channel, rest, get_path(fresh.channel, rest))
            elif rest:
                drop_path(cfg.channel, rest)
            continue

        live: Any = cfg
        source: Any = fresh
        parts = path.split(".")
        for part in parts[:-1]:
            live, source = getattr(live, part, None), getattr(source, part, None)
        if (
            isinstance(live, BaseModel)
            and isinstance(source, BaseModel)
            and parts[-1] in type(live).model_fields
        ):
            setattr(live, parts[-1], getattr(source, parts[-1]))
        else:
            # The path does not go through models (a dict field inside a model): we replace the top
            # field as a whole, as `adopt` did. Better too much than an edit that silently did not
            # arrive.
            setattr(cfg, head, getattr(fresh, head))


def _has_path(data: dict[str, Any], path: str) -> bool:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def apply_core(app: Argus, cfg: Config, changed: set[str]) -> None:
    """Apply the part of the core that applies without a restart.

    By this point the values are already in the live config: `_push` poured them in. What
    remains is what a single assignment does not solve: the nodes that took a copy of the
    settings at assembly and will not learn about the edit by themselves.
    """
    if app.assistant is not None and _touched(changed, "gate_brain."):
        from argus.brain.gate import GateSettings

        # The refusal counters survive the edit: they belong to the session, not to the setting.
        # Zeroing them here would erase exactly the statistics that made the person start turning
        # the filter.
        app.assistant.gate.settings = GateSettings(**app.cfg.gate_brain.model_dump())

    if app.vision is not None and _touched(changed, "gate."):
        from argus.perception.framegate import FrameGate
        from argus.perception.framegate import GateSettings as FrameSettings

        app.vision.gate = FrameGate(FrameSettings(**app.cfg.gate.model_dump()))

    if "screen.target_window" in changed:
        # Through the control panel, not by assignment: changing the target resets the frame
        # dropout and the accumulated screen fingerprint. Otherwise the first frame of the new
        # window would be compared with the previous one, and the difference between two
        # different windows would get into the timeline.
        app.set_target_window(app.cfg.screen.target_window)

    if "brain.send_screenshot" in changed:
        app.sources["screen"] = app.cfg.brain.send_screenshot and app.sources.get(
            "screen", True
        )

    if "realtime_stt.language" in changed:
        # The same language goes to the shadow comparison engine, `stt.compare_with`. It is
        # switched off now (there is nothing to compare with), but having two different
        # recognition languages for one conversation would be an extra degree of freedom that
        # nobody uses and that can cause confusion.
        app.cfg.gpt_live_transcribe.language = app.cfg.realtime_stt.language

    if changed & {"realtime_stt.language", "realtime_stt.smart"}:
        # A session may already be running, so we reconnect it, so that the new language or smart
        # recognition reaches the conversation at once and not after a restart (see `reconnect`
        # of the realtime engines). One reconnect serves both edits: they arrive together when
        # another session is opened.
        app.speech.reopen_realtime("language or smart recognition changed")


def _touched(changed: set[str], prefix: str) -> bool:
    return any(p.startswith(prefix) for p in changed)


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def _tail(path: str) -> str:
    """`channel.assist.profile.stack` -> `profile.stack`; a core path is left as is."""
    return path.split(".", 2)[2] if path.startswith("channel.") else path


def _field_info(model: type[BaseModel] | None, path: str) -> Any:
    if model is None or not path:
        return None
    cur: Any = model
    parts = path.split(".")
    for i, part in enumerate(parts):
        if not (isinstance(cur, type) and issubclass(cur, BaseModel)):
            return None
        info = cur.model_fields.get(part)
        if info is None:
            return None
        if i == len(parts) - 1:
            return info
        cur = _inner_model(info.annotation)
    return None


def _inner_model(annotation: Any) -> Any:
    """Pull the model out of `Model | None` and the like."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in getattr(annotation, "__args__", ()):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _errors(e: ValidationError, patch: dict[str, Any]) -> dict[str, str]:
    """pydantic errors -> "this value does not fit this field"."""
    out: dict[str, str] = {}
    for item in e.errors():
        path = ".".join(str(x) for x in item["loc"])
        if path not in patch:
            # The check runs on the whole config, and the error could have come from a neighboring
            # field. Pinning it on the field the person did not touch would show the error where it
            # is not.
            path = next(iter(patch))
        out[path] = item["msg"]
    return out


def values_of(cfg: Config, *paths: str) -> dict[str, Any]:
    """Pull out several values by paths — for the branch's widgets."""
    data = cfg.model_dump(mode="json")
    return {p: get_path(data, p) for p in paths}
