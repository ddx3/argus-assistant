"""Auto-update of the built app.

Only for an `.exe` built by PyInstaller (`sys.frozen`) and for an `.app` on macOS; a
working copy from the sources is updated through `git pull` and does not need this. For an
`.app` the replacement procedure is its own, `mac/update.py`; everything else (what the
release is, whether it can be trusted, how to download and check it) is shared and lives
here.

**Trust.** The release description (`latest.json`, `latest-mac.json`) is signed with the
release key (`update_trust.py`), and the app accepts only what is signed: the version, the
archive name, sha256, size and release number. The downloaded archive must match the
signed hash and size, otherwise it is not unpacked. The release number only grows: a
signed but older release does not count as an update (a rollback cannot be slipped in).
There are two entry points: at launch, before the window (`maybe_self_update`,
automatic), and from the running app by a button (`install_release`, called by
`update_service.py`).

How the replacement works on Windows: while the process is alive, its files (including
`Argus.exe`) are in use and cannot be overwritten. So this module does not overwrite
itself; it downloads the archive, checks it and hands the rest to a separate PowerShell
script, started as a detached process. The script waits for exactly this PID to exit,
checks the archive hash against the signed one, unpacks the archive next to the program
(in its own folder, so on the same disk), swaps the top-level items of the build by
renaming (the old ones go to a spare folder), on any error puts everything back as it was,
and in any case restarts `Argus.exe`. The caller must exit immediately afterwards (at
launch, without bringing up the pipeline; from the running app, by closing the windows).

The spare folder lives until the new program confirms that it started: it writes an
`update-alive` mark (`_mark_alive`, at the beginning of `maybe_self_update`), and after
the restart the script waits for it for up to `ALIVE_WAIT_S` seconds. No mark means the
new build crashes at startup or hung: the script kills its processes, returns the
previous files, starts the previous program and writes a failure result into
`update-result.json`, and the program remembers the failure (`failed`). The script waits
only for a build that contains `HEALTH_CAP_NAME`. If the script died in the middle (power
loss, killed process), the next launch finishes the job from the journal
`update-journal.json` (`recover_interrupted_swap`).

Personal files (`argus.local.json` with the person's own settings and voices,
`argus.account.json` with the sign-in) are not touched by the replacement at all: what the
person already has is not overwritten from the archive (`PRESERVE`), otherwise every
update would reset the person and their choice of voice to the clean defaults from the
fresh archive.

Three files next to the data keep the records: `update.json` holds what was installed and
what is being installed, and how many times the replacement failed (`failed`: after a
failure the release is not tried for half an hour, after the second one for a day, instead
of on every launch); `update.lock` means "replacement in progress": while it is fresh and
the script's process is alive, a second launch quietly exits instead of bringing up the
old program over the files being rewritten; `update-result.json` holds what the script
wrote (whether it succeeded), and it is read at the next launch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from argus import update_trust

log = logging.getLogger(__name__)

VERSION_NAME = "VERSION"

# The release number that publishing puts into the build (`deploy/win/publish_win.py`): it
# is baked into the folder itself, so the rollback protection is not empty on a fresh
# install or after `update.json` is lost.
BUILD_NAME = "BUILD"

# What we remember about updates between launches: which release was already installed
# (against rollback), which one is being installed now, and how many times the replacement
# to it has failed.
STATE_NAME = "update.json"

# Files the archive must not overwrite: they are personal, not part of the build. The
# replacement script gets this list from here too; it has no second copy.
PRESERVE = ("argus.local.json", "argus.account.json", "argus.legal.json", ".env")

# "Replacement in progress" (created by `_apply`, removed by the script), the script's
# result, and the "new program started" mark (waited for by the replacement script on macOS,
# `mac/update.py`).
LOCK_NAME = "update.lock"
RESULT_NAME = "update-result.json"
ALIVE_NAME = "update-alive"

# Health check of the new program on Windows. `JOURNAL_NAME` is the replacement journal
# (what was renamed to where: an interrupted replacement is rolled back by it if the script
# died); `HEALTH_CAP_NAME` is a file that `stage.py` puts into the build: "this build
# reports its launch" (`ALIVE_NAME`). The script waits for the mark only for such a build: a
# release re-signed from a previous version (`publish_win.py --rollback`) cannot report, and
# waiting for it is pointless; it would roll back the rollback itself.
JOURNAL_NAME = "update-journal.json"
HEALTH_CAP_NAME = "update-alive.v1"

# How long the script waits for the new program's mark after the restart before it returns
# the previous build.
ALIVE_WAIT_S = 90

# A lock older than this was left by a dead script (power off in the middle of a
# replacement): we do not honor it.
LOCK_MAX_AGE_S = 20 * 60

# How long not to try a release after a failed replacement: half an hour after the first
# one (the script restarts the previous program, and it must not immediately download the
# same thing again), a day after the second and later ones.
RETRY_AFTER_S = (30 * 60, 24 * 3600)

# A release description is a few hundred bytes. More than that is not it (the response used
# to be read in full: nested `[` of 200 KB gave `RecursionError`, and a slow trickle held the
# splash screen).
MAX_MANIFEST_BYTES = 64 * 1024

# Leftover folders (an interrupted replacement) older than a day are removed at launch; if
# they hold no previous files, after a quarter of an hour.
LEFTOVER_AGE_S = 24 * 3600
EMPTY_STAGE_AGE_S = 15 * 60

# The check sits on the launch path: while it runs, the pipeline does not come up and the
# window shows the splash screen. A server that answers returns `latest.json` in fractions of
# a second, while an unreachable one used to eat six seconds here on every launch, for
# everyone for whom it was unreachable. If the check does not finish in time, we stay on the
# current version, and the next launch asks again. From the running app
# (`update_service.py`) we can wait longer.
TIMEOUT_S = 2.0

Progress = Callable[[int, int], None]


class UpdateError(Exception):
    """The update cannot be installed. The text is the reason in words: for the log and for the
    window.
    """


@dataclass(frozen=True)
class Release:
    """A signed description of a release that can be installed."""

    version: str
    zip: str
    sha256: str
    size: int
    build: int


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def read_version(root: Path) -> str:
    path = root / VERSION_NAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def latest_name() -> str:
    """Which file to ask for: each system has its own (`latest-mac.json` on macOS), so that serving
    one does not hand the other a foreign build."""
    return "latest-mac.json" if sys.platform == "darwin" else "latest.json"


def fetch_latest(server: str, name: str = "latest.json", timeout: float = TIMEOUT_S) -> dict | None:
    """What the server considers the latest version. `None` means the network failed or the
    answer is wrong; the caller should simply stay on the current version, not crash.

    `name` is which file to ask for (`latest_name()`).

    It catches everything, not only network errors: the answer comes over bare `http://`, and
    any form of it (nested brackets, non-JSON, too large, slow) must neither crash the launch
    nor hold the splash screen. No more than `MAX_MANIFEST_BYTES` is read, and no longer than
    the overall deadline (the network `timeout` applies to each wait, not to the whole read).
    """
    try:
        deadline = time.monotonic() + max(timeout * 3, 5.0)
        body = bytearray()
        with httpx.stream("GET", f"{server.rstrip('/')}/{name}", timeout=timeout) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(chunk_size=8192):
                body += chunk
                if len(body) > MAX_MANIFEST_BYTES:
                    raise ValueError("the release description is larger than allowed")
                if time.monotonic() > deadline:
                    raise TimeoutError("the release description is taking too long to read")
        return json.loads(bytes(body))
    except Exception as e:  # noqa: BLE001 — network boundary: anything means "staying on the current version"
        log.info("update check failed (%s) — staying on the current version", e)
        return None


# ---------- what is installed and what is being installed ----------


def _state_path(root: Path) -> Path:
    return root / STATE_NAME


def _read_state(root: Path) -> dict:
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(root: Path, state: dict) -> None:
    """Atomic: into a temporary file next to it, then a replace. A torn write must not wipe out the
    memory of which release was already installed, since without it the rollback protection is
    empty."""
    path = _state_path(root)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not write the update state: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):      # OverflowError — `Infinity` from JSON
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _bundled_build(root: Path) -> int:
    """The build number baked into the build folder itself (`BUILD`). 0 means there is no file (an
    old build, Mac).
    """
    try:
        return _as_int((root / BUILD_NAME).read_text(encoding="utf-8").strip())
    except OSError:
        return 0


def _take_result(root: Path) -> dict:
    """What the swap script wrote last time (`update-result.json`). Read once and erased."""
    path = root / RESULT_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else {}


def _note_failure(state: dict, pending: dict, error: str = "") -> None:
    """The swap to release `pending` failed: remember the attempt so the archive is not downloaded
    on every launch.
    """
    build = _as_int(pending.get("build"))
    failed = state.get("failed") if isinstance(state.get("failed"), dict) else {}
    attempts = _as_int(failed.get("attempts")) + 1 if _as_int(failed.get("build")) == build else 1
    state["failed"] = {"build": build, "attempts": attempts, "last_try": time.time()}
    log.warning("swap failed for release %s (attempt %s)%s", pending.get("version") or build, attempts,
                f": {error}" if error else "")


def installed_build(root: Path) -> int:
    """The release number that this app already had installed (0 means unknown: it was not
    installed by an update).

    Before the replacement, what is being installed is recorded in `pending`. If the
    replacement succeeded, the new program on its very first query sees its `VERSION` next to
    `pending` (or the "succeeded" mark from the script) and recognizes the release as
    installed; if it failed, the version is the old one, `pending` is reset, and the failure
    is recorded in `failed` (see `retry_wait`): the same replacement is not repeated on every
    launch.
    """
    state = _read_state(root)
    pending = state.get("pending")
    if isinstance(pending, dict):
        result = _take_result(root)
        installed = bool(pending.get("version")) and pending["version"] == read_version(root)
        if not installed and result.get("ok") is True and _as_int(result.get("build")) == _as_int(pending.get("build")):
            installed = True       # the script completed the replacement, although VERSION in the build is written differently
        if installed:
            state["build"] = max(_as_int(state.get("build")), _as_int(pending.get("build")))
            state.pop("failed", None)
        else:
            _note_failure(state, pending, str(result.get("error") or "").strip()[:300])
        state.pop("pending", None)
        _write_state(root, state)
    return max(_as_int(state.get("build")), _bundled_build(root))


def retry_wait(root: Path, release: Release) -> float:
    """How many more seconds not to try this release after a failed swap. 0 means it can be tried."""
    failed = _read_state(root).get("failed")
    if not isinstance(failed, dict) or _as_int(failed.get("build")) != release.build:
        return 0.0
    attempts = max(1, _as_int(failed.get("attempts")))
    wait = RETRY_AFTER_S[min(attempts, len(RETRY_AFTER_S)) - 1]
    left = _as_float(failed.get("last_try")) + wait - time.time()
    return min(max(0.0, left), float(wait))     # the clock went backward: still not longer than allowed


def note_pending(root: Path, release: Release) -> None:
    """Remember that this release is being installed right now (see `installed_build`)."""
    state = _read_state(root)
    state["pending"] = {"version": release.version, "build": release.build}
    _write_state(root, state)


def check_release(server: str, root: Path, timeout: float = TIMEOUT_S) -> Release | None:
    """Whether there is a newer release that can be trusted. `None` means no: the network failed or
    the signature did not match.
    """
    built = installed_build(root)       # first deal with the previous replacement: did it succeed (no network needed)
    info = fetch_latest(server, latest_name(), timeout=timeout)
    if not info:
        return None
    problem = update_trust.problem_with_manifest(info, update_trust.platform_name())
    if problem:
        log.warning("update not accepted: %s", problem)
        return None
    release = Release(
        version=str(info["version"]).strip(), zip=info["zip"], sha256=info["sha256"],
        size=int(info["size"]), build=int(info["build"]),
    )
    if release.version == read_version(root):
        return None
    if release.build <= built:
        log.info("release %s (%s): not newer than the one already installed — this is a rollback, skipping", release.version, release.build)
        return None
    return release


def _remove(path: Path) -> None:
    """Remove a file without failing: a busy or foreign file must not stand in for the real cause of
    the failure.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete %s: %s", path, e)


def download_release(server: str, release: Release, dest: Path, progress: Progress | None = None) -> None:
    """Download the release archive and check it against the signed hash and size. If they do not
    match: `UpdateError`, and no file remains.
    """
    url = f"{server.rstrip('/')}/{release.zip}"
    digest = hashlib.sha256()
    size = 0
    try:
        with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    size += len(chunk)
                    if size > release.size:
                        raise UpdateError("the archive is larger than the signed description says")
                    digest.update(chunk)
                    f.write(chunk)
                    if progress is not None:
                        progress(size, release.size)
    except UpdateError:
        _remove(dest)
        raise
    # `InvalidURL` is not an `HTTPError`: an address with an extra character (such a name used
    # to pass the check) crashed the launch.
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as e:
        _remove(dest)
        raise UpdateError(f"couldn't download the update ({e})") from e
    if size != release.size or digest.hexdigest() != release.sha256:
        _remove(dest)
        raise UpdateError("the downloaded archive doesn't match the signed description — update cancelled")


def _can_write_here(folder: Path) -> bool:
    """Whether a file can really be created in the folder.

    `os.access(folder, W_OK)` on Windows is always "yes": it does not look at the folder's
    permissions (ACL). A program in `C:\\Program Files` would download the archive, close and
    not come back up, because the script had nowhere to write.
    """
    probe = folder / f".write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def _powershell() -> str:
    """Path to the PowerShell that will run the replacement. Empty means there is none (then
    there is no point downloading).

    The full path, not just "powershell": the built `.exe` is a windowed app with no console of
    its own, and what is really in its PATH is not known in advance; with a trimmed PATH the
    fixed path works anyway.
    """
    # System path first: on Windows `shutil.which` also searches the current folder, and a
    # `powershell.exe` planted there (say, in "Downloads", where the program was launched from)
    # would be executed during the update.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if system.is_file():
        return str(system)
    return shutil.which("powershell") or ""


def cannot_install_reason(root: Path) -> str:
    """Why this instance cannot update. Empty means it can.

    The checks are cheap and come before the download: after it the program closes, and if the
    script then has nowhere to write or nothing to run with, the person is left without a
    program.
    """
    if not is_frozen():
        return "this is a run from source — it updates through git"
    if sys.platform == "darwin":
        from argus import host
        from argus.mac.update import cannot_update_reason

        return cannot_update_reason(host.app_bundle())
    if not _can_write_here(root):
        return f"no permission for the folder {root}"
    if not _can_write_here(Path(tempfile.gettempdir())):
        return "no permission for the temp folder"
    if not _powershell():
        return "PowerShell isn't available on this computer"
    return ""


def _looks_like_a_build(zip_path: Path) -> bool:
    """A check not for a virus but for "not half a file from a broken network": the archive must
    open and carry an executable, rather than being garbage that would damage a working
    installation when unpacked over it.
    """
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            if bad is not None:
                return False
            return any(name.endswith("Argus.exe") for name in z.namelist())
    except zipfile.BadZipFile:
        return False


_UPDATE_SCRIPT = r"""
param(
    [int]$OldPid,
    [string]$ZipPath,
    [string]$AppDir,
    [string]$WorkDir,
    [string]$LogPath,
    [string]$Sha256,
    [string]$Keep,
    [string]$LockPath,
    [string]$ResultPath,
    [long]$Build,
    [int]$WaitSeconds = 60,
    # The folder is renamed as a whole, and while even one file in it is open (the antivirus is still reading what was just unpacked,
    # the indexer), Windows refuses: we wait longer than for a single file (about 20 s per step in total).
    [int]$Retries = 8,
    [int]$RetryDelay = 3,
    # The new program must check in about its launch (`AlivePath`) within this many seconds, otherwise the swap is rolled back and the previous
    # build starts again. We wait only for a build that can check in (it contains `HealthCap`): earlier releases
    # cannot. The journal (`JournalPath`) records what was renamed: it is used to undo a swap that was cut off midway.
    [string]$AlivePath,
    [string]$HealthCap,
    [string]$JournalPath,
    [int]$AliveSeconds = 90,
    [switch]$NoLaunch
)

# `$Pid` is a reserved PowerShell variable (the id of this very process),
# a parameter with that name cannot be declared at all: the script fails while parsing the
# parameters, before the first line of the body, and all that comes out is exit code 1,
# without a word about the reason. Any other failure here would be just as silent:
# nobody has a window on this process, so the whole body is wrapped
# in try/catch with its own log: without it the only trace of a failure is the old
# version that keeps working quietly, with no hint why.
#
# Stop: by default, copying or moving a file onto a busy place only writes an error and carries on, which produced a mixed
# install (some files new, some old) marked as updated. Now any error goes to catch and a rollback.
# All paths go through -LiteralPath: square brackets in the folder name (`Argus [portable]`) would otherwise be read as a pattern.
$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$Text) {
    # One encoding for the whole log (Add-Content and Out-File in PowerShell 5.1 write differently).
    try {
        [System.IO.File]::AppendAllText($LogPath, ("{0} {1}`r`n" -f (Get-Date -Format o), $Text), $Utf8)
    } catch {}
}

function Touch-Lock {
    try { if (Test-Path -LiteralPath $LockPath) { (Get-Item -LiteralPath $LockPath).LastWriteTime = Get-Date } } catch {}
}

function Move-WithRetry([string]$From, [string]$To) {
    # A file busy for a second with the antivirus or the indexer is no reason to abandon the swap.
    # Renaming by .NET, not `Move-Item`: for a folder with a busy file inside, `Move-Item` in PowerShell 5.1 falls back to
    # moving file by file and breaks off midway: the folder ends up torn in two, the rollback does not put it together, and the cleanup
    # wipes the spare files (verified on a busy `zz.dll`). `Directory.Move` renames the folder as a whole or does nothing.
    for ($i = 1; $true; $i++) {
        try {
            if ([System.IO.Directory]::Exists($From)) { [System.IO.Directory]::Move($From, $To) } else { [System.IO.File]::Move($From, $To) }
            return
        }
        catch {
            if ($i -ge $Retries) { throw }
            Write-Log "busy, retry ($i): $From"
            Start-Sleep -Seconds $RetryDelay
        }
    }
}

$done = New-Object System.Collections.ArrayList
$rollbackFailed = $false

function Save-Journal([string]$Phase) {
    # Swap journal: while the script is alive it is not needed, but if the script died midway (power off, process killed), the
    # next launch of the program uses it to restore the previous build (`update.recover_interrupted_swap`). It is written before each rename
    # and atomically (a temporary file and a replace): a broken write must not leave half a journal. If the write fails, the swap
    # goes on, just without the safety net.
    if (-not $JournalPath) { return }
    try {
        $steps = @(foreach ($s in $done) { @{ target = $s.Target; backup = $s.Backup; hadOld = [bool]$s.HadOld } })
        $doc = @{ pid = $PID; stage = $stage; work = $WorkDir; build = $Build; phase = $Phase; time = (Get-Date -Format o); steps = $steps }
        $tmp = $JournalPath + '.tmp'
        [System.IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Depth 5 -Compress), $Utf8)
        Move-Item -LiteralPath $tmp -Destination $JournalPath -Force
    } catch {
        Write-Log "journal not written: $_"
    }
}

function Remove-Journal {
    # We remove our own journal and leave a foreign one alone (another script managed to start a new swap).
    if (-not $JournalPath -or -not (Test-Path -LiteralPath $JournalPath)) { return }
    try {
        $mine = (Get-Content -LiteralPath $JournalPath -Raw | ConvertFrom-Json).pid -eq $PID
        if ($mine) { Remove-Item -LiteralPath $JournalPath -Force }
    } catch {
        Write-Log "journal not removed: $_"
    }
}

function Write-Result([bool]$Ok, [string]$Err, [string]$Ver) {
    try {
        $result = @{ ok = $Ok; error = $Err; build = $Build; version = $Ver; time = (Get-Date -Format o) }
        [System.IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json -Compress), $Utf8)
    } catch {
        Write-Log "result not written: $_"
    }
}

function Place-Item([string]$Source, [string]$Target, [string]$Backup) {
    # New in place of old: the previous one (if there was one) goes to the spare folder, and every step is recorded for the rollback.
    $had = Test-Path -LiteralPath $Target
    $step = [pscustomobject]@{ Target = $Target; Backup = $Backup; HadOld = $had; Placed = $false }
    [void]$done.Add($step)
    Save-Journal 'swapping'
    if ($had) {
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Backup))
        Move-WithRetry $Target $Backup
    }
    Move-WithRetry $Source $Target
    $step.Placed = $true
}

function Merge-Dir([string]$From, [string]$To, [string]$Backup) {
    # The weights folder: new on top, while foreign weights that the person downloaded themselves stay.
    if (-not (Test-Path -LiteralPath $To)) { Place-Item $From $To $Backup; return }
    foreach ($child in (Get-ChildItem -LiteralPath $From -Force)) {
        $dest = Join-Path $To $child.Name
        $save = Join-Path $Backup $child.Name
        if ($child.PSIsContainer) { Merge-Dir $child.FullName $dest $save } else { Place-Item $child.FullName $dest $save }
    }
}

function Invoke-Rollback {
    # Rollback: what we managed to replace we put back, what we managed to place with no previous one we remove. All by renaming: the new
    # goes aside (it can be wiped later), and the previous one must come back whole and at once, not after a recursive delete.
    $trash = Join-Path $stage 'trash'
    try { [void][System.IO.Directory]::CreateDirectory($trash) } catch {}
    for ($i = $done.Count - 1; $i -ge 0; $i--) {
        $step = $done[$i]
        try {
            $aside = Join-Path $trash ([guid]::NewGuid().ToString('N'))
            if (Test-Path -LiteralPath $step.Backup) {
                if (Test-Path -LiteralPath $step.Target) { Move-WithRetry $step.Target $aside }
                Move-WithRetry $step.Backup $step.Target
            } elseif ($step.Placed -and -not $step.HadOld) {
                Move-WithRetry $step.Target $aside
            }
        } catch {
            $script:rollbackFailed = $true
            Write-Log "rollback failed ($($step.Target)): $_"
        }
    }
    if ($script:rollbackFailed) { Write-Log "the previous files stayed in ${backups} — restore them by hand" } else { Write-Log 'the previous program was restored' }
}

function Start-App {
    if ($NoLaunch) {
        Write-Log 'launch skipped (NoLaunch)'
        return
    }
    $exe = Join-Path $AppDir 'Argus.exe'
    try {
        # Not `Start-Process -WorkingDirectory`: square brackets in the path (`Argus [portable]`) are read by it as a pattern and
        # it refuses to start: the swap went through but the program did not come up. Launching through .NET takes the path as is.
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $exe
        $start.WorkingDirectory = $AppDir
        $start.UseShellExecute = $true
        [void][System.Diagnostics.Process]::Start($start)
        Write-Log 'program started'
    } catch {
        Write-Log "direct launch failed ($_), trying another way"
        try {
            Start-Process -FilePath $exe
            Write-Log 'program started'
        } catch {
            Write-Log "the program could not be started: $_"
        }
    }
}

function Wait-Alive {
    # The new program writes a mark when it reaches the update check (`update._mark_alive`). If it did not, it crashed at launch
    # or hung, and it cannot be left as the only one.
    $until = (Get-Date).AddSeconds($AliveSeconds)
    while ((Get-Date) -lt $until) {
        if (Test-Path -LiteralPath $AlivePath) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return [bool](Test-Path -LiteralPath $AlivePath)
}

function Stop-AppProcesses {
    # We kill a hung new program before the rollback: while it holds the files, the previous ones cannot be put back.
    $prefix = $AppDir.TrimEnd('\') + '\'
    foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) {
        try {
            if ($p.Id -ne $PID -and $p.Path -and $p.Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                Write-Log "stopping process $($p.Id): $($p.Path)"
                Stop-Process -Id $p.Id -Force
            }
        } catch {}
    }
    Start-Sleep -Seconds 2
}

$ok = $false
$needAlive = $false
$errorText = ''
$version = ''
$stage = Join-Path $AppDir ('.update-' + [guid]::NewGuid().ToString('N').Substring(0, 8))   # in the program folder: the same disk
$backups = Join-Path $stage 'old'

try {
    Write-Log "start: OldPid=$OldPid ZipPath=$ZipPath AppDir=$AppDir"

    # We wait for the old program to exit. If it did not, its files are busy and must not be touched: better to stay on the old version.
    try { Wait-Process -Id $OldPid -Timeout $WaitSeconds } catch {}
    $alive = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
    if ($alive -and -not $alive.HasExited) { throw "the old program did not close within $WaitSeconds s" }
    Start-Sleep -Seconds 1

    # The archive sat in a temporary folder while the program was closing: we check it against the signed hash once more.
    if (-not $Sha256) { throw 'the signed hash of the archive was not passed' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash -ne $Sha256) { throw 'the archive does not match the signed hash' }
    Write-Log 'archive hash matches'

    [void][System.IO.Directory]::CreateDirectory($stage)
    $new = Join-Path $stage 'new'
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $new -Force
    Touch-Lock

    # The archive carries one top-level directory (`Argus/...`), and its contents are what we swap with the old ones.
    $inner = Get-ChildItem -LiteralPath $new -Directory | Select-Object -First 1
    if (-not $inner -or -not (Test-Path -LiteralPath (Join-Path $inner.FullName 'Argus.exe'))) { throw 'there is no Argus.exe in the archive' }
    Write-Log 'archive unpacked'

    # We look at the NEW build, not at the program folder: the old one may have left a file there that the current one does not have.
    $needAlive = [bool]($AlivePath -and $HealthCap -and (Test-Path -LiteralPath (Join-Path $inner.FullName $HealthCap)))

    [void][System.IO.Directory]::CreateDirectory($backups)
    $keepNames = @($Keep -split ',' | Where-Object { $_ })
    # VERSION goes last: while it is old, the swap counts as unfinished.
    $items = @(Get-ChildItem -LiteralPath $inner.FullName -Force)
    $items = @($items | Where-Object { $_.Name -ne 'VERSION' }) + @($items | Where-Object { $_.Name -eq 'VERSION' })
    foreach ($item in $items) {
        $target = Join-Path $AppDir $item.Name
        $had = Test-Path -LiteralPath $target
        if (($keepNames -contains $item.Name) -and $had) { continue }      # personal: what already exists is not overwritten
        $save = Join-Path $backups $item.Name
        if ($item.PSIsContainer -and $item.Name -eq 'models' -and $had) {
            Merge-Dir $item.FullName $target $save
        } else {
            Place-Item $item.FullName $target $save
        }
        Write-Log "replaced: $($item.Name)"
        Touch-Lock
    }

    try { $version = (Get-Content -LiteralPath (Join-Path $AppDir 'VERSION') -Raw).Trim() } catch {}
    $ok = $true
    Save-Journal 'swapped'
    Write-Log "swap finished, version: $version"
} catch {
    $errorText = ($_ | Out-String).Trim()
    Write-Log "ERROR: $errorText"
    Invoke-Rollback
} finally {
    # First the result and releasing the lock: the new (or the previous) program reads the result at launch and looks at the lock. The lock is released
    # BEFORE launch: otherwise the new program would see it, decide "swap in progress" and quietly exit without checking in.
    Write-Result $ok $errorText $version
    try { Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue } catch {}
    # The old mark does not count: we wait for the one written by this very program that was just launched.
    if ($ok -and $needAlive) { try { Remove-Item -LiteralPath $AlivePath -Force -ErrorAction SilentlyContinue } catch {} }
    # We launch the program in any case: after a failure it is the previous version, and the person is not left without it.
    Start-App
    if ($ok -and $needAlive) {
        if (Wait-Alive) {
            Write-Log 'the new program checked in about its launch'
        } else {
            # The new build did not reach the update check (it crashes at launch or hangs): the previous one comes back, and the result
            # "failed" is read by the previous program, which remembers the failure, so the same release is not downloaded again at every launch.
            $errorText = "the new program did not check in about its launch within $AliveSeconds s — the previous one was restored"
            Write-Log "ERROR: $errorText"
            Save-Journal 'rolling-back'
            Stop-AppProcesses
            Invoke-Rollback
            $ok = $false
            $version = ''
            Write-Result $false $errorText ''
            if (-not $rollbackFailed) { Start-App }
        }
    }
    # The journal is needed while the previous files may be needed; if the rollback failed, it stays for the next launch of the program.
    if ($rollbackFailed) { Save-Journal 'rollback-failed' } else { Remove-Journal }
    # Cleanup goes last: on deleting thousands of old files the person is no longer waiting.
    try { Set-Location -LiteralPath $env:TEMP } catch {}
    foreach ($leftover in @($stage, $WorkDir)) {
        # The rollback failed: the spare folder holds the only copy of the previous files, so we do not wipe it (the launch cleans it up after a day).
        if ($rollbackFailed -and $leftover -eq $stage) { continue }
        try { if (Test-Path -LiteralPath $leftover) { Remove-Item -LiteralPath $leftover -Recurse -Force } }
        catch { Write-Log "did not remove ${leftover}: $_" }
    }
}
"""


def _pid_alive(pid: int) -> bool:
    """Whether the process is alive. Not `os.kill(pid, 0)`: on Windows that is not a check but a
    `TerminateProcess` (it would kill someone else's process).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259                           # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _write_lock(app_dir: Path, pid: int) -> None:
    try:
        (app_dir / LOCK_NAME).write_text(json.dumps({"pid": pid, "time": time.time()}), encoding="utf-8")
    except OSError as e:
        log.warning("could not write the swap lock: %s", e)


def _remove_lock(app_dir: Path) -> None:
    _remove(app_dir / LOCK_NAME)


def update_in_progress(root: Path) -> bool:
    """Whether a replacement is going on right now (the lock is fresh and the script's process is
    alive).

    While the script rewrites files, the old program can still be launched (the shortcut gets
    pressed again and again): it would take the free instance lock and start working on top of
    files the script is changing, which gives a mixed installation. Such a launch must exit
    quietly, and the script itself will bring the program up. The lock of a dead script (power
    off, killed process) is stale and gets removed: it cannot keep the program shut.
    """
    path = root / LOCK_NAME
    try:
        raw = path.read_text(encoding="utf-8-sig")
        started = path.stat().st_mtime
    except OSError:
        return False
    pid = 0
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            pid = _as_int(data.get("pid"))
            started = _as_float(data.get("time")) or started
    except ValueError:
        pass
    # The script touches the lock at every step (mtime), so a lock touched recently counts as
    # alive.
    try:
        started = max(started, path.stat().st_mtime)
    except OSError:
        return False
    if time.time() - started <= LOCK_MAX_AGE_S and (pid == 0 or _pid_alive(pid)):
        return True
    log.info("swap lock is stale (process %s) — removing it", pid)
    _remove(path)
    return False


def _has_backups(stage: Path) -> bool:
    """Whether the swap's backup folder (`old`) holds at least one file, that is, previous files that
    exist nowhere else.
    """
    try:
        return any(p.is_file() for p in (stage / "old").rglob("*"))
    except OSError:
        return True            # could not look: we assume it is there and do not erase extra


def _sweep_leftovers(root: Path) -> None:
    """Remove leftovers of interrupted replacements: `.update-*` in the program folder and
    `argus-update-*` in the temporary one.

    Fresh ones are not touched, since one may be a replacement in progress right now;
    everything older than a day goes. There are two exceptions. While the replacement journal
    (`JOURNAL_NAME`) is in the folder, the spare folders are not touched at all: they hold the
    previous files waiting for a rollback (`recover_interrupted_swap`). And a folder with no
    spare files in it (the script died while only unpacking the archive) goes after a quarter
    of an hour, not a day: it holds only an unpacked copy of the archive, hundreds of
    megabytes.
    """
    candidates: list[Path] = []
    try:
        candidates += [p for p in root.glob(".update-*") if p.is_dir()]
        candidates += list(Path(tempfile.gettempdir()).glob("argus-update-*"))
    except OSError:
        return
    held = (root / JOURNAL_NAME).exists()
    for path in candidates:
        try:
            age = time.time() - path.stat().st_mtime
            is_stage = path.parent == root
            if is_stage and held:
                continue
            if age < LEFTOVER_AGE_S and not (is_stage and age >= EMPTY_STAGE_AGE_S and not _has_backups(path)):
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def _roll_back_by_journal(steps: list, root: Path, stage: Path) -> bool:
    """Return the previous files according to the replacement journal. `True` means everything
    came back.

    The same rules as the rollback in the script, but by the state of the disk, not by the
    script's memory: the journal is written before the rename, and the script could have died
    between a rename and the next write. If there is a spare file, it is put back in place
    (whatever stood there is moved aside); if there is no spare and there was no previous file
    in that place, what stands there was put by the replacement, and it goes. All of it by
    renaming: erasing can be done later.
    """
    trash = stage / "trash"
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    complete = True
    for step in reversed(steps):
        try:
            target, backup = Path(str(step["target"])), Path(str(step["backup"]))
            had_old = bool(step.get("hadOld"))
        except (KeyError, TypeError, ValueError):
            complete = False
            continue
        if not _inside(target, root) or not _inside(backup, stage):
            log.warning("swap journal contains a foreign path (%s) — step skipped", target)
            complete = False
            continue
        try:
            aside = trash / uuid.uuid4().hex
            if backup.exists():
                if target.exists():
                    os.rename(target, aside)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(backup, target)
            elif not had_old and target.exists():
                os.rename(target, aside)
        except OSError as e:
            log.warning("could not restore from the backup folder: %s: %s", target, e)
            complete = False
    return complete


def recover_interrupted_swap(root: Path) -> None:
    """The replacement was interrupted: the script died (power off, killed process); bring things
    to order at launch.

    By the journal (`update-journal.json`, which the script writes before each rename):

    * the script is still alive: it is waiting for the new program's mark or rolling back
      itself, and we do not interfere;
    * the replacement was completed (`swapped`): this program is the new build, and it has
      already reached the update check, so the spare files are not needed and the folder is
      removed;
    * the replacement was interrupted midway (`swapping`) or the rollback failed
      (`rollback-failed`): the previous files are returned from the spare folder by the
      journal. The release is not recorded as installed: `VERSION` is the old one, `pending` in
      `update.json` turns into a remembered failure (`installed_build`), and the same release
      is not downloaded again right away.

    If the program itself does not start (files are mixed in the folder), this is never
    reached: then the previous files are in `.update-*\\old`. A journal that could not be read,
    and foreign paths in it, we neither touch nor erase.
    """
    path = root / JOURNAL_NAME
    try:
        raw = path.read_text(encoding="utf-8-sig")
        written = path.stat().st_mtime
    except OSError:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        log.warning("swap journal cannot be read - leaving the backup folders as they are")
        return
    pid = _as_int(data.get("pid"))
    if pid and _pid_alive(pid) and time.time() - written <= LOCK_MAX_AGE_S:
        return
    stage = Path(str(data.get("stage") or ""))
    if not (stage.name.startswith(".update-") and _inside(stage, root) and stage.resolve() != root.resolve()):
        log.warning("swap journal contains a foreign folder (%s) — not touching it", stage)
        return
    work = Path(str(data.get("work") or ""))
    phase = data.get("phase")
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    if phase == "swapped":
        log.info("previous swap completed, but the script did not clean up after itself - removing the backup files")
    elif _roll_back_by_journal(steps, root, stage):
        log.warning("previous swap interrupted for release %s — previous files restored", data.get("build"))
    else:
        log.error("previous swap was interrupted, previous files could not be restored - they remain in %s", stage)
        return
    shutil.rmtree(stage, ignore_errors=True)
    if work.name.startswith("argus-update-") and work.parent == Path(tempfile.gettempdir()):
        shutil.rmtree(work, ignore_errors=True)
    _remove(path)


def _apply(zip_path: Path, app_dir: Path, release: Release) -> bool:
    """Start the replacement as a separate process. `True` means the process is alive and is
    waiting for our exit.

    `zip_path` is in its own random temporary folder (`install_release`): the name cannot be
    guessed in advance, and the script itself checks the archive against the signed hash
    `release.sha256` before unpacking.
    """
    work = zip_path.parent
    script_path = work / "update.ps1"
    # With a byte order mark: PowerShell 5.1 reads a file without one in the system encoding,
    # and capital Cyrillic letters in strings turn into "smart quotes", so the parsing of the
    # script fails after the program has already exited.
    script_path.write_text(_UPDATE_SCRIPT, encoding="utf-8-sig")

    # Next to the program, not in the temporary folder: that temporary one disappears together
    # with the script in its very last lines, while this log must outlive both the script and
    # the process; it will be read after both are gone, if something went wrong.
    log_path = app_dir / "update.log"

    powershell = _powershell()
    if not powershell:
        raise UpdateError("PowerShell isn't available on this computer")

    # Explicit DEVNULL, not inherited handles: in a built app `sys.stdout`/`sys.stderr` are a
    # file open for writing (`argus.log`, see `packaging/argus_app.py: prepare_output`), not a
    # console. What happens when `CreateProcess` tries to pass such a handle to a detached
    # process with no window and no parent is not documented anywhere, and a version that
    # quietly stayed the old one with a live Popen and no exception was exactly such a finding.
    # Explicit `DEVNULL` removes this uncertainty entirely: the script does not need them, and
    # all its output goes to `-LogPath` anyway.
    proc = subprocess.Popen(
        [
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script_path),
            "-OldPid", str(os.getpid()),
            "-ZipPath", str(zip_path),
            "-AppDir", str(app_dir),
            "-WorkDir", str(work),
            "-LogPath", str(log_path),
            "-Sha256", release.sha256,
            "-Keep", ",".join(PRESERVE),
            "-LockPath", str(app_dir / LOCK_NAME),
            "-ResultPath", str(app_dir / RESULT_NAME),
            "-Build", str(release.build),
            "-AlivePath", str(app_dir / ALIVE_NAME),
            "-HealthCap", HEALTH_CAP_NAME,
            "-JournalPath", str(app_dir / JOURNAL_NAME),
            "-AliveSeconds", str(ALIVE_WAIT_S),
        ],
        # Not the program folder: the script renames its contents, and the process's working
        # directory would hold it busy.
        cwd=tempfile.gettempdir(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # `DETACHED_PROCESS` is deliberately not used here: under it the process has no console at
        # all, and the classic `powershell.exe` (not `pwsh.exe`) without one does not bring up its
        # environment and quietly exits with code 0 without running a single line of the script.
        # This is exactly how this version failed to work on this machine, without a word in the
        # log. `CREATE_NO_WINDOW` gives a console, just does not show it, and that is enough.
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    # `Popen()` does not raise an exception even when Windows created the process and it
    # immediately exited by itself (an incompatible flag, blocked by the antivirus): the only way
    # to find out is to poll it. A second and a bit is longer than PowerShell's own startup but
    # shorter than what could noticeably delay the exit from `maybe_self_update`.
    # The lock goes right after the launch, while this process still holds the instance lock: a
    # second launch will not have time to slip through.
    _write_lock(app_dir, proc.pid)
    time.sleep(1.5)
    code = proc.poll()
    if code is not None:
        log.warning("update process died immediately, code %s — version will stay the same", code)
        _remove_lock(app_dir)
    return code is None


def install_release(server: str, release: Release, root: Path, progress: Progress | None = None) -> bool:
    """Download the release, check it, and hand the replacement to a separate process.

    `True` means the replacement is started and is waiting for this process to exit: the
    caller must exit at once. If it fails, `UpdateError` is raised with the reason in words,
    and nothing on disk is touched (except temporary files, which are cleaned up).
    """
    if sys.platform == "darwin":
        from argus.mac.update import install_release as mac_install

        return mac_install(server, release, root, progress)

    # Its own random folder for each launch: a predictable name in the shared temporary folder
    # allowed planting an archive between the hash check and the replacement. If the
    # replacement started, the script removes the folder.
    try:
        work = Path(tempfile.mkdtemp(prefix="argus-update-"))
    except OSError as e:
        raise UpdateError(f"no temp folder ({e})") from e
    started = False
    try:
        zip_path = work / "update.zip"
        download_release(server, release, zip_path, progress)
        if not _looks_like_a_build(zip_path):
            raise UpdateError("the downloaded archive doesn't look like an Argus build — update cancelled")
        note_pending(root, release)
        try:
            started = _apply(zip_path, root, release)
        except OSError as e:
            raise UpdateError(f"couldn't start the update ({e})") from e
        if not started:
            raise UpdateError("the update process didn't start")
        return True
    finally:
        if not started:
            shutil.rmtree(work, ignore_errors=True)


def _mark_alive(root: Path) -> None:
    """Mark that the program has started: the replacement script waits for it (macOS:
    `mac/update.py`, Windows: `_UPDATE_SCRIPT`).

    If a new build crashes at startup, only the one watching over it can roll it back: the
    replacement script waits for this mark after the launch and, without it, returns the
    previous build. Called from `maybe_self_update`, that is, only in a built program and
    after taking the instance lock, but before the network, the window and the pipeline.
    """
    try:
        (root / ALIVE_NAME).write_text(str(int(time.time())), encoding="utf-8")
    except OSError as e:
        log.warning("could not write the launch marker: %s", e)


def maybe_self_update(server: str, root: Path) -> bool:
    """Check at launch and, if there is something newer, install it without asking.

    `True` means the update has been started by a separate process: the caller must exit at
    once without bringing up the pipeline, since files that it would hold open (models, log)
    would prevent the script from rewriting them.

    Never raises: it is called on the launch path, and any error here (a "Failed to start"
    message at every start) would be worse than staying on
    the current version, and it cannot be fixed by an update, which is the very thing that is
    failing.
    """
    if not is_frozen():
        return False
    try:
        if update_in_progress(root):
            log.info("swap already in progress - this launch exits, the swap script will start the program")
            return True
        _mark_alive(root)
        recover_interrupted_swap(root)       # before `check_release`: an interrupted replacement must become a remembered failure
        _sweep_leftovers(root)
        if not server:
            return False

        release = check_release(server, root)
        if release is None:
            return False
        reason = cannot_install_reason(root)
        if reason:
            log.info("new version available: %s, but cannot update: %s", release.version, reason)
            return False
        wait = retry_wait(root, release)
        if wait > 0:
            log.info("not trying release yet (%s): the previous swap to it failed, minutes until retry: %d",
                     release.version, int(wait // 60) + 1)
            return False

        log.info("new version: %s -> %s, downloading", read_version(root) or "?", release.version)
        started = install_release(server, release, root)
    except UpdateError as e:
        log.warning("%s — staying on the current version", e)
        return False
    except Exception:  # noqa: BLE001 — launch matters more than the update
        log.exception("update check crashed - staying on the current version")
        return False
    if started:
        log.info("update started - the program will restart now")
    return started
