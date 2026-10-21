"""Write a file whole and atomically, for the person's data (sessions, results, edits).

A temporary file and `os.replace` protect against a break in the middle of a write,
but not against a power loss: without `fsync` NTFS may keep the rename itself while
losing the data of the new file, and a zero-length file ends up in the place of
`chat.json`. So the data is flushed to disk first, and only then is the file put in
place.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".bak"


def _stage(path: Path, data: bytes) -> Path:
    """The data goes into a temporary file next to the target and onto disk; the caller puts it in
    place.
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def _commit(tmp: Path, path: Path) -> None:
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)     # do not leave a half-written temporary file behind
        raise


def write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path`: a temporary file next to it, flush to disk, replace."""
    _commit(_stage(path, data), path)


def write_text(path: Path, text: str, *, backup: bool = False) -> None:
    """The same for text (UTF-8). `backup` keeps the previous version alongside, as
    `<name>.bak`: a corrupted new file must not cost the person the old one too.

    The copy is made only from readable JSON: a half-written file over a good copy would
    wipe out the last thing left of the data.
    """
    tmp = _stage(path, text.encode("utf-8"))
    if backup:
        _keep_previous(path)
    _commit(tmp, path)


def _keep_previous(path: Path) -> None:
    try:
        previous = path.read_bytes()
        json.loads(previous)
    except (OSError, ValueError):
        return          # there is no previous file or it is itself corrupted, so there is nothing to copy
    try:
        write_bytes(path.with_name(path.name + BACKUP_SUFFIX), previous)
    except OSError:
        log.warning("could not save the previous copy of %s", path.name, exc_info=True)
