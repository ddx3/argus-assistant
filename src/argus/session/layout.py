"""Format of a recorded session: a shared vocabulary for the writer and the reader.

Layout on disk:

    sessions/2026-08-28_1433-07/
        meta.json       what the session is: start time, devices, settings
        me.wav          microphone,     16 kHz, mono, int16
        them.wav        system audio, the same
        frames.jsonl    one line per frame: time and file name
        frames/000001.jpg

The format is chosen so that a recording can be poked at by hand: WAV opens in any
player, JPEG in a viewer, JSONL is readable by eye. A debugging tool that cannot be looked
into is useless.

**The key property: position in the WAV equals time.** Gaps in the stream are filled with
silence while recording, so an offset in the file corresponds unambiguously to a
timestamp. Without this we would have to keep a separate index of chunks and stitch it
together on every read.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from argus.core.events import SAMPLE_RATE, Source

log = logging.getLogger(__name__)

META_NAME = "meta.json"
FRAMES_INDEX = "frames.jsonl"
FRAMES_DIR = "frames"
WAV_NAMES = {Source.ME: "me.wav", Source.THEM: "them.wav"}


class DeviceMeta(BaseModel):
    """What was used for recording, so that when analyzing a recording it is visible what it was
    taken from.
    """

    name: str
    rate: int
    channels: int


class SessionMeta(BaseModel):
    """`meta.json` is the recording's passport."""

    version: int = 1
    started_at: datetime
    duration_ns: int = 0
    sample_rate: int = SAMPLE_RATE

    mic: DeviceMeta | None = None
    loopback: DeviceMeta | None = None
    screen_size: tuple[int, int] | None = None

    chunks: dict[str, int] = Field(default_factory=dict)
    frames: int = 0

    # How much silence had to be appended into gaps in the stream. A non-zero value is direct
    # proof that the device stopped delivering data.
    silence_filled_ms: dict[str, float] = Field(default_factory=dict)

    # How much idle time of the whole recording (computer sleep, a device that dropped off) was
    # cut out of the file: the time after it is shifted by the same amount relative to the clock
    # (`recorder._rebase`).
    idle_skipped_ms: float = 0.0

    # The metadata of a live recording (updated every half a minute) differs from a closed one.
    # Old files were written only on close, so without this field the recording is closed.
    finished: bool = True

    # What went wrong during recording (a track stopped, did not reach the end), in words.
    problems: list[str] = Field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.duration_ns / 1_000_000_000


class FrameRecord(BaseModel):
    """A row in `frames.jsonl`."""

    t: int
    seq: int
    file: str


def session_id(started_at: datetime) -> str:
    """Folder name: sorts alphabetically and is readable by a person."""
    return started_at.strftime("%Y-%m-%d_%H%M-%S")


def find_session(sessions_dir: Path, ident: str) -> Path:
    """Find a recording by name or by the tail of the name.

    The full name is long, and in practice it is convenient to type `argus replay 1433`.
    """
    exact = sessions_dir / ident
    if (exact / META_NAME).exists():
        return exact
    matches = sorted(
        d for d in sessions_dir.glob("*") if (d / META_NAME).exists() and ident in d.name
    )
    if not matches:
        raise FileNotFoundError(f"recording '{ident}' not found in {sessions_dir}")
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        raise ValueError(f"several recordings match '{ident}': {names}")
    return matches[0]


def list_sessions(sessions_dir: Path) -> list[tuple[Path, SessionMeta]]:
    """All recordings, newest on top."""
    out = []
    for d in sessions_dir.glob("*"):
        meta_file = d / META_NAME
        if not meta_file.exists():
            continue
        try:
            out.append((d, SessionMeta.model_validate_json(meta_file.read_text("utf-8"))))
        except (ValueError, OSError):
            continue  # a broken recording is simply not shown, instead of breaking the list
    return sorted(out, key=lambda p: p[1].started_at, reverse=True)


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# Folders in `sessions/` that are not recordings: a sound sample for checking the
# microphone. The `/sample` route that wrote it was removed (2026-10-14), but the folder
# may have stayed on disk, and it does not count as an abandoned recording.
NOT_SESSIONS = frozenset({"sample"})

# Anything fresher than this is not considered abandoned: the process may have just crashed,
# but it may also be recording, as another instance (`argus record`), and a folder must not
# be erased under a live writer.
ORPHAN_GRACE_S = 600


def _orphans(sessions_dir: Path, known: set[Path]) -> list[tuple[Path, float, bool]]:
    """Folders with a recording inside (audio, frames) but without a readable `meta.json`: the
    process was cut off (crash, power loss) before the metadata started being written as it
    went, or the metadata file is corrupted. They are not included in the lists — there is
    nothing to read — but they take up space and must count toward `keep_gb`.

    Returns (folder, when it started, whether it can be deleted)."""
    wanted = (*WAV_NAMES.values(), FRAMES_INDEX, FRAMES_DIR)
    found = []
    now = time.time()
    for d in sessions_dir.iterdir():
        if d in known or d.name in NOT_SESSIONS or not d.is_dir():
            continue
        touched = [(d / name) for name in wanted if (d / name).exists()]
        if not touched:
            continue
        try:
            started = datetime.strptime(d.name[:18], "%Y-%m-%d_%H%M-%S").replace(tzinfo=UTC)
            began = started.timestamp()
        except ValueError:
            began = min(p.stat().st_mtime for p in touched)
        newest = max(p.stat().st_mtime for p in touched)
        found.append((d, began, now - newest > ORPHAN_GRACE_S))
    return found


def prune_sessions(sessions_dir: Path, keep_gb: float) -> list[tuple[Path, int]]:
    """Delete the oldest recordings until the total fits under the ceiling.

    This is needed because audio weighs much more than it seems: 16 kHz int16 is 31 KB/s per
    channel, that is 220 MB per hour for both. With constant recording this eats the disk
    within a week of use, and you learn about it at the worst moment, when space runs out in
    the middle of a session.

    We delete from the oldest: a fresh recording is more needed, since that is the one replayed
    when the engine made a mistake half an hour ago.

    It returns what was deleted: other people's recordings must not be erased silently, this
    has to be said out loud.
    """
    if keep_gb <= 0 or not sessions_dir.exists():
        return []

    budget = int(keep_gb * 2**30)
    known = list_sessions(sessions_dir)          # newest first
    # (when it started, folder, size, whether it may be deleted). Abandoned recordings without
    # metadata are on par with the others: their space is not counted anywhere else and would
    # never free itself.
    entries = [(meta.started_at.timestamp(), d, dir_size(d), True) for d, meta in known]
    entries += [
        (began, d, dir_size(d), removable)
        for d, began, removable in _orphans(sessions_dir, {d for d, _ in known})
    ]
    entries.sort(key=lambda e: e[0])             # from oldest to newest

    total = sum(size for _, _, size, _ in entries)
    removed: list[tuple[Path, int]] = []

    # We go from oldest to newest until it fits.
    for _, path, size, removable in entries:
        if total <= budget:
            break
        if not removable:
            continue
        try:
            shutil.rmtree(path)
        except OSError as e:
            log.warning("could not delete the old recording %s: %s", path.name, e)
            continue
        total -= size
        removed.append((path, size))

    return removed
