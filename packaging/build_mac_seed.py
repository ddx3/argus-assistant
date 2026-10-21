"""Seed data for `Argus.app`: what the data folder is filled from at first launch.

    uv run python packaging/build_mac_seed.py [folder]

On Windows `stage.py` lays everything out next to `Argus.exe`. On macOS this is impossible:
writing inside a signed `.app` is not allowed, so the same things go into the package's
`seed/`, and the program moves them to `~/Library/Application Support/Argus` at launch
(`argus/host.py: seed_user_data`). What exactly is laid out is decided by `stage.stage`, the
same function as for Windows: one set of files for both systems.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage  # noqa: E402 — the path is added by the line above


def build(project: Path, seed: Path) -> list[str]:
    """Rebuild the seed templates in `seed`. Returns a report: what was put there and what that
    risks.
    """
    shutil.rmtree(seed, ignore_errors=True)
    seed.mkdir(parents=True)
    # Always "for distribution": the Mac app is distributed (DMG, zip), and it needs no keys —
    # answers, voice-over and recognition go through the account server. Before, an ordinary build
    # "for oneself" stood here, and a local run on the Mac with a `.env` in the project (for
    # example, after a move) put a working key into the DMG. In CI there is no `.env`, so this did
    # not happen there, but the protection must be by construction, not by luck.
    report = stage.stage(project, seed, public=True)

    warning = stage.legal_warning(project)
    if warning:
        report.append(f"WARNING: {warning}")

    # A safeguard in case `stage` ever changes: a `.env` in the distributed seed data is a leaked
    # key, so we fail instead of warning.
    if (seed / ".env").exists():
        raise SystemExit("a .env ended up in the seed files - such a build must not be distributed")
    return report


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    project = Path(__file__).resolve().parents[1]
    seed = Path(sys.argv[1]) if len(sys.argv) > 1 else project / "build-mac" / "seed"
    for line in build(project, seed):
        print(" ", line)
    print(f"  done: {seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
