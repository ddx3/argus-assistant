"""The settings a person edits in the admin panel, as a separate layer over `argus.toml`.

`argus.toml` lives in the repository and is half explanations: why 1920 and not 1280;
why the recording ceiling is 8 GB; what happens if you raise the VAD threshold. This is
text written by hand, and it is worth more than the numbers themselves.

A machine must not rewrite such a file. The first write through a TOML library would lay
it out as bare "key = value" pairs and wipe out everything that makes it so long. So the
admin panel writes **elsewhere**: `argus.local.json` goes next to it, holding only what
the person actually changed and nothing more. The config is assembled in three layers:
the defaults from the models, `argus.toml` on top, and this file on top of that.

Why JSON and not TOML: the file is machine-made, it will never have comments, and TOML
has no way to express `None`, so `stt.language = null` ("detect from the audio") would
simply be impossible to write.

The file is also **its own for every working copy**: three branches live in three
folders, and each has its own channel, its own port and its own setting. One file for
all would mean a conflict at every merge.

For the **developer's** built app this has a flip side: `root` is `dist/Argus`, and
`pyinstaller --noconfirm` wipes it entirely on every `.exe` rebuild, together with the
person's edits. So wherever the file is written, a backup copy is left next to it
(outside `dist/Argus`), see `_backup_path` and `keeps_backup`, and the next launch of the
built app picks it up by itself if the main file is not found. An installed program has
no such copy: it would sit in someone else's folder and outlive the uninstall.

The file is personal, and there is nothing in it to lose "by accident": an unreadable
file is not overwritten by the next write but set aside alongside as
`argus.local.json.bad-*` (see `_set_aside`), and the write goes through a temporary file
with `fsync` and a replace.
"""

from __future__ import annotations

import codecs
import copy
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argus.core.config import Config

log = logging.getLogger(__name__)

LOCAL_NAME = "argus.local.json"

# The "read, change, write" lock (see `SettingsStore.__init__`).
_LOCK = threading.RLock()

# What the admin panel never touches. `root` and `config_path` are computed paths to the
# project, not settings: writing them would nail a working copy to one folder on disk.
FROZEN = frozenset({"root", "config_path"})

# "No path" is a separate value, not `None`: `None` is a legitimate setting value
# (`stt.language = null`), while a vanished key of the free-form `channel` section is
# something else, and the live config must be able to be told "remove it".
MISSING = object()


def diff_paths(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """Paths whose value became different, **including those that disappeared and those that
    appeared**.

    A disappeared path matters where settings are assembled in layers: a chat's key
    (`channel.assist.materials.dir`) exists while the chat is open and goes away when it is
    closed. A comparison only by what remains (`after`) did not see this, and a closed chat
    would go on reading its own materials folder.
    """
    return {
        p for p in before.keys() | after.keys()
        if before.get(p, MISSING) != after.get(p, MISSING)
    }


# --------------------------------------------------------------------------
# The settings tree as a dict
# --------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Lay `patch` over `base` without spoiling either.

    Only dicts are merged. A list is a single value as a whole: "remove an item from
    `skip_windows`" must remove the item, not append on top of the old one.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Tree -> flat paths: `{"gate_brain.cooldown_s": 8.0}`.

    An empty dict stays a leaf: otherwise a section from which everything was deleted would
    vanish without a trace, and "empty" would become indistinguishable from "not set".
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def get_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    cur = data
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def drop_path(data: dict[str, Any], path: str) -> None:
    """Remove a key and the sections that became empty above it.

    Without the second half the file overgrows with empty tables: a value is returned to
    what it was, but the section stays and looks like "something is configured here".
    """
    parts = path.split(".")
    stack: list[tuple[dict[str, Any], str]] = []
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        stack.append((cur, part))
        cur = nxt
    cur.pop(parts[-1], None)
    for parent, key in reversed(stack):
        if parent.get(key) == {}:
            parent.pop(key)


def _backup_path(root: Path) -> Path:
    """A place next to `root` but outside it, where a rebuild cannot reach.

    Only for the built app (`sys.frozen`): there `root` is `dist/Argus`, and
    `pyinstaller --noconfirm` wipes exactly this folder on every rebuild, taking along
    `argus.local.json`, the only place where the person's edits live (see the module
    docstring). The parent of this folder (`dist/`) survives a rebuild, so that is where we
    put the copy.

    For `argus run` this backup is not needed and is harmful: there `root` is the
    repository root, and `root.parent` is not a repository at all.
    """
    return root.parent / f".{root.name}.{LOCAL_NAME}.backup"


def keeps_backup(root: Path) -> bool:
    """Whether a backup of the edits is needed; only in the developer's layout.

    The copy lives **outside** the program's folder. For the developer's built app this is
    `dist/`, which survives a rebuild, and that is where it is needed. For an installed
    program "outside" means the Desktop, Downloads or the disk root: a copy of personal
    settings (device names, windows, hotkeys) would settle there forever, outlive the
    uninstall, and quietly bring the settings back after the person deleted
    `argus.local.json` to reset them.

    The layout is the same one `live.source_root` checks: `<copy>/dist/Argus`, with a
    `pyproject.toml` in the root of the copy. An installed program never has that.
    """
    if not getattr(sys, "frozen", False):
        return False
    return root.parent.name == "dist" and (root.parent.parent / "pyproject.toml").exists()


def decode_text(raw: bytes, errors: str = "strict") -> str:
    """The text of a file a person edited: UTF-8 with or without BOM, UTF-16 with BOM.

    Notepad and PowerShell 5.1 (`Out-File -Encoding utf8`, `>`) add a BOM or write UTF-16,
    and a plain read as "utf-8" cannot parse such a file: everything in it would vanish
    without a word. For a file without a BOM the result is the same.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors)
    return raw.decode("utf-8-sig", errors)


def _read_json(path: Path) -> dict[str, Any]:
    """An object from the file. `OSError` means the file could not be read, `ValueError` means it is
    not our JSON.
    """
    data = json.loads(decode_text(path.read_bytes()))
    if not isinstance(data, dict):
        # ValueError, not TypeError: the same as for JSON that failed to parse, one branch.
        raise ValueError(f"the root must be an object, not {type(data).__name__}")  # noqa: TRY004
    return data


def _set_aside(path: Path) -> None:
    """A corrupted file is set aside, not left under the next write.

    The very first write from the settings page would create a new file over this one, and
    everything the person configured in it (and it may have been one stray comma) would be
    gone forever. `argus.local.json.bad-<time>` is placed alongside, so it can be opened and
    fixed by hand. If moving fails, at least a copy; if that fails too, we say so in the log.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bad-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.bad-{stamp}-{n}")
    try:
        os.replace(path, target)
    except OSError:
        try:
            shutil.copy2(path, target)
        except OSError as e:
            log.warning("could not save the corrupted %s (%s)", path.name, e)
            return
    log.warning("corrupted %s: saved as %s", path.name, target.name)


def _replace(src: Path, dst: Path) -> None:
    """`os.replace` with short retries.

    On Windows an antivirus or an indexer holds the file for a moment, and the replace fails
    with `PermissionError`, although a couple of tenths of a second later it would pass. In
    total we wait less than half a second, because this is called from a page handler.
    """
    for pause in (0.02, 0.05, 0.1, 0.2):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(pause)
    os.replace(src, dst)


def read_overrides(root: Path) -> dict[str, Any]:
    """Read the admin panel's edits. A broken file must not crash the launch.

    It is machine-made, but it sits on disk next to a person: it may be opened and spoiled.
    Dying because of that is not acceptable: the pipeline comes up on what is written in
    `argus.toml`, and we report the loss in the log. But the file must not be wiped either:
    an unreadable one (not JSON, cut off, not an object) is set aside alongside as
    `argus.local.json.bad-*`, and from there everything goes as if there were no file.

    If the file itself is missing (for the developer's built app that first of all means
    "a `.exe` rebuild has just wiped it", see `_backup_path`), we try the backup and, if it
    is found, immediately put it back in the usual place: from then on the file lives as
    usual, not as a one-off restore. The same applies when the main file is corrupted: the
    copy is the last healthy state, and the corrupted original stays alongside.
    """
    path = root / LOCAL_NAME
    damaged = False
    if path.exists():
        try:
            return _read_json(path)
        except OSError as e:
            # Busy with another program or no permission: the file may be intact, so we set nothing
            # aside.
            log.warning("cannot read %s (%s) — taking only argus.toml", path.name, e)
            return {}
        except ValueError as e:         # both JSONDecodeError and UnicodeDecodeError
            log.warning("cannot parse %s (%s) — setting it aside", path.name, e)
            _set_aside(path)
            damaged = True
    if keeps_backup(root):
        backup = _backup_path(root)
        if backup.exists():
            try:
                data = _read_json(backup)
            except (OSError, ValueError) as e:
                log.warning(
                    "backup copy of %s: damaged (%s) — taking only argus.toml",
                    backup.name, e,
                )
                if isinstance(e, ValueError):
                    # The next write will update the copy; do not wipe the corrupted one.
                    _set_aside(backup)
                return {}
            if path.exists() and not damaged:
                # Someone managed to create the file while the backup was being read: we take it and do
                # not overwrite the fresh edits with old ones.
                log.info(
                    "%s: appeared while the backup copy was being read — taking it",
                    path.name,
                )
                return read_overrides(root)
            log.info(
                "%s %s - restored from the backup copy",
                path.name, "is damaged" if damaged else "is missing (lost in a rebuild)",
            )
            try:
                write_overrides(root, data)
            except OSError as e:
                log.warning("could not put back in place: %s (%s)", path.name, e)
            return data
    return {}


def write_overrides(root: Path, data: dict[str, Any]) -> Path:
    """Write the edits whole and atomically.

    Through a temporary file and `os.replace`: the settings are written from a running
    pipeline, and half a file on disk after a crash would mean it does not come up at all
    next time. The temporary file is flushed to disk (`fsync`) before the replace: without
    that, a power cut between the write and the replace left the new name with empty
    content.
    """
    path = root / LOCAL_NAME
    # The temporary file name is unique per process and thread: the file is written both by
    # the app and by `argus sync-voices` from another terminal window, and a shared `.tmp`
    # would get mixed up between the two.
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:         # a file system without fsync: the write is whole anyway
                pass
        _replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)     # we do not leave a half-written tail
        except OSError:
            pass
        raise
    if keeps_backup(root):
        # We keep the copy outside `dist/Argus` so that after the next `.exe` rebuild there is
        # someone to return the edits: see `_backup_path`. It goes through a temporary file and
        # `os.replace`, like the main file above: `shutil.copy2` straight onto the final path is
        # not atomic, and a process killed halfway would leave a broken backup, the one thing the
        # restore in `read_overrides` relies on.
        try:
            backup = _backup_path(root)
            tag = f"{os.getpid()}-{threading.get_ident()}"
            backup_tmp = backup.with_name(f"{backup.name}.{tag}.tmp")
            shutil.copy2(path, backup_tmp)
            _replace(backup_tmp, backup)
        except OSError as e:
            log.warning("could not save the backup copy of the edits (%s)", e)
    return path


def _drop_bad(overrides: dict[str, Any], error: Any) -> tuple[dict[str, Any], list[str]]:
    """A copy of the edits without the values the model validation complains about.

    The error's `loc` is the path to the value. We walk down it into the edits while the keys
    are found, and remove the deepest one found: for an error inside a list or a union
    branch (`audio.mic_device.int`) that is the key itself, whose content is bad. An error
    whose key was already removed (or is not in the edits at all, which means the complaint
    is about `argus.toml`) is skipped, otherwise it would take down the whole section.
    """
    out = copy.deepcopy(overrides)
    dropped: list[str] = []
    for item in error.errors():
        cur: Any = out
        chain: list[tuple[dict[str, Any], str]] = []
        alien = False
        for part in item["loc"]:
            if not isinstance(cur, dict):
                break
            if part not in cur:
                # Either a complaint about `argus.toml`, or `part` is the name of a union branch (`int`,
                # `str`) while `cur` is the bad value itself (a dict instead of a device number).
                alien = not (chain and cur == item.get("input"))
                break
            chain.append((cur, part))
            cur = cur[part]
        if alien or not chain:
            continue
        parent, key = chain[-1]
        shown = repr(parent[key])
        dropped.append(f"{'.'.join(k for _, k in chain)} = {shown[:80]}")
        del parent[key]
        for up, name in reversed(chain[:-1]):       # a section from which everything has gone, too
            if up.get(name) == {}:
                del up[name]
    return out, dropped


def drop_invalid(base_raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any] | None:
    """The edits without the values the models do not accept, or `None` if that cannot cure it.

    The edits file lives longer than the version that wrote it. A release narrowed the
    allowed values, a person set the port to 80 by hand, and without this the whole launch
    would fail on validation, and fixing it would mean working blindly in the file. A single
    bad value is dropped (with a log entry saying exactly which), and the other edits stay.
    We do not touch the file itself here: it belongs to the person.
    """
    from pydantic import ValidationError

    from argus.core.config import Config as ConfigModel

    cleaned = overrides
    for _ in range(4):
        try:
            ConfigModel.model_validate(deep_merge(base_raw, cleaned))
        except ValidationError as e:
            cleaned, dropped = _drop_bad(cleaned, e)
            if not dropped:
                return None             # the complaint is not about the edits (or has no address), so they cannot be helped
            for line in dropped:
                log.warning("in %s: the value is invalid and is not applied: %s", LOCAL_NAME, line)
        else:
            return cleaned
    return None


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class SettingsStore:
    """Three layers of settings and an edit on top of them.

    It holds two snapshots: `base`, which is what the config would be **without** the admin
    panel's edits (defaults plus `argus.toml`), and `overrides`, the edits themselves. The
    first is needed for exactly one thing: to tell that a value was returned to its original
    and then remove it from the file, rather than record "as it was". Otherwise
    `argus.local.json` overgrows in a month with a copy of the whole config, and it is no
    longer visible what the person really tweaked.
    """

    def __init__(self, cfg: Config) -> None:
        from argus.core.config import Config as ConfigModel

        self.cfg = cfg
        self.root = cfg.root
        self.path = self.root / LOCAL_NAME

        # One edit at a time: "read the file, change, write". The settings page and the
        # microphone choice (`Argus.set_mic_device` comes from a worker thread) may arrive at the
        # same time, and the second write would wipe the first. The lock is shared by the
        # process, not one per store: there can be two stores on one file.
        self._lock = _LOCK

        self.base_raw: dict[str, Any] = _read_toml(cfg.config_path)
        self.base = ConfigModel.model_validate(self.base_raw)
        self.overrides = read_overrides(self.root)
        # A bad value in the file (a hand edit, an old version) must not lock up the page: while
        # it sits in the edits, every "edit + edits" check would fail on it.
        cleaned = drop_invalid(self.base_raw, self.overrides)
        if cleaned is not None:
            self.overrides = cleaned

        # The fourth layer is the open chat's settings (`argus/chats/`): flat paths, as in
        # `patch`. It lies on top of `argus.local.json` and is **not written** into it: the chat
        # keeps its values itself, and here they only take effect while the chat is open. Switch
        # the chat and the layer is replaced, and the shared edits stay exactly as they were.
        self.overlay: dict[str, Any] = {}

        # What is in effect now, kept as its own snapshot and not read from the live config. The
        # live object is edited by whoever applies the settings on the pipeline, and relying on
        # it would mean counting "what changed" from the result of someone else's work: if they
        # did not apply it, the store would decide that nothing had changed.
        self._current = flatten(cfg.model_dump(mode="json"))

    # -- reading -------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        """What is in effect right now: as flat paths, ready for JSON."""
        return dict(self._current)

    def defaults(self) -> dict[str, Any]:
        """What there would be without the admin edits."""
        return flatten(self.base.model_dump(mode="json"))

    def changed(self) -> set[str]:
        """Paths that the admin overrode, both by shared edits and by a chat."""
        return set(flatten(self.overrides)) | set(self.overlay)

    # -- writing -------------------------------------------------------------

    def build(
        self,
        patch: dict[str, Any],
        overlay: dict[str, Any] | None = None,
        *,
        overrides: dict[str, Any] | None = None,
    ) -> Config:
        """Build the config with an edit, writing nothing.

        A separate step, because validation must happen **before** the write: saving a bad value
        and crashing on the next launch is the worst outcome, since it has to be fixed by hand in
        the file, blindly.

        The layers, bottom to top: defaults and `argus.toml`, the admin panel's edits, the open
        chat's settings, the edit itself. `overlay` is a different set of chat settings in place
        of the current one: this is how a chat edit is validated before the chat accepts it.
        `overrides` is a different set of admin panel edits in place of the ones in memory
        (`apply` builds from the fresh contents of the file).
        """
        from argus.core.config import Config as ConfigModel

        layer = self.overlay if overlay is None else overlay
        mine = self.overrides if overrides is None else overrides
        merged = deep_merge(deep_merge(mine, _tree(layer)), _tree(patch))
        cfg = ConfigModel.model_validate(deep_merge(self.base_raw, merged))
        cfg.root = self.root
        cfg.config_path = self.cfg.config_path
        return cfg

    def _fresh(self) -> dict[str, Any]:
        """The edits as they lie in the file **now**, not as they were at launch.

        The file is written not only by this app: `argus sync-voices`, `favorite` and `add-voice`
        append to it from another process while the app is running. A write "whole from memory"
        would wipe their work, and a page that cannot see those voices would wipe them at the
        first edit of any field. So before every write we read the file again and lay the edit
        over it.

        If there is no file (a rebuild, an antivirus or a person took it away), the truth is in
        memory: the app goes on living with these values, and one edit must not reset everything
        else on the fly and also wipe the backup copy (`write_overrides`). Settings are reset by
        deleting the file while the app is closed. If it cannot be read right now (it is busy
        with another program), the error goes up: writing over an unread file would mean losing
        everything in it. If it is corrupted under a running app, we write what the app
        remembers from launch, and set the corrupted one aside alongside (`_set_aside`).
        """
        path = self.root / LOCAL_NAME
        if not path.exists():
            return copy.deepcopy(self.overrides)
        try:
            raw = _read_json(path)
        except ValueError as e:
            log.warning("cannot parse %s (%s) — writing what I remember, and setting it aside", path.name, e)
            _set_aside(path)
            return copy.deepcopy(self.overrides)
        # A bad value appended to the file by hand must not lock up the page, just as at launch
        # (`__init__`): it is dropped, and the log shows which one.
        cleaned = drop_invalid(self.base_raw, raw)
        return raw if cleaned is None else cleaned

    def apply(self, patch: dict[str, Any]) -> tuple[Config, set[str]]:
        """Validate the edit, save it and return the new config.

        It also returns the list of paths whose value **really** changed. It is needed by whoever
        applies the settings to the live pipeline: rebuilding recognition because a person opened
        the page and pressed "save" without touching anything is a noticeable pause for nothing.

        Everything is done on a copy, and memory changes only after the file is written: a failed
        write (disk full, the file held by an antivirus) leaves no "ghost", a value that is
        neither in the file nor on the pipeline but that the next successful write would save
        together with its own.
        """
        patch = {k: v for k, v in patch.items() if k.split(".")[0] not in FROZEN}
        with self._lock:
            fresh = self._fresh()
            cfg = self.build(patch, overrides=fresh)   # will fail here if the value is bad

            before = self._current
            after = flatten(cfg.model_dump(mode="json"))
            base = self.defaults()
            new = copy.deepcopy(fresh)
            changed: set[str] = set()
            for path in patch:
                if path not in after:
                    continue
                if after[path] == base.get(path):
                    drop_path(new, path)
                else:
                    set_path(new, path, after[path])
                if after[path] != before.get(path):
                    changed.add(path)
            if fresh != self.overrides:
                # The file turned out to hold something different from memory (another program added to
                # it): the live pipeline must be told about it too, otherwise the page would show values
                # that it does not have.
                changed |= diff_paths(before, after)

            write_overrides(self.root, new)
            self.overrides = new
            self._current = after
            return cfg, changed

    def set_overlay(self, overlay: dict[str, Any]) -> tuple[Config, set[str]]:
        """Replace the open chat's settings and return what changed as a result.

        It writes nothing to disk: this is a layer over the file, not an edit of the file (see
        `overlay` in `__init__`). The values are validated before the replace: a bad layer
        raises the exception right here, having changed nothing.
        """
        with self._lock:
            cfg = self.build({}, overlay=overlay)
            before = self._current
            after = flatten(cfg.model_dump(mode="json"))
            self.overlay = dict(overlay)
            self._current = after
            return cfg, diff_paths(before, after)

    def reset(self, paths: list[str] | None = None) -> tuple[Config, set[str]]:
        """Remove the edits: all or the listed ones."""
        with self._lock:
            before = self._current
            if paths is None:
                new: dict[str, Any] = {}
            else:
                new = copy.deepcopy(self._fresh())
                for path in paths:
                    drop_path(new, path)
            cfg = self.build({}, overrides=new)         # before the write: we do not save a bad value
            write_overrides(self.root, new)
            self.overrides = new
            after = flatten(cfg.model_dump(mode="json"))
            self._current = after
            return cfg, diff_paths(before, after)


def _tree(patch: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, value in patch.items():
        set_path(out, path, value)
    return out


def _read_toml(path: Path | None) -> dict[str, Any]:
    import tomllib

    if path is None or not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def store_of(app) -> SettingsStore:
    """The settings store: one per application.

    Earlier the settings page created its own instance, and that was enough. Now two
    parties work with it, the page and the chats (`argus/chats/`), and the open chat's layer
    lives **in** the store. Two instances would mean that an edit on the page builds the
    config without the chat and puts someone else's values back in place, and a chat switched
    in one is not visible to the other.
    """
    # The microphone choice comes from a worker thread, the page from the loop: without a
    # lock both could create a store each, and the second write would not know about the
    # first.
    with _LOCK:
        store = getattr(app, "_settings_store", None)
        if store is None:
            store = SettingsStore(app.cfg)
            app._settings_store = store
        return store
