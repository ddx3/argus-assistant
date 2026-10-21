"""Self-update of the `.app` on macOS.

The idea is the same as on Windows (`argus/update.py`): a process cannot replace itself,
so it downloads and verifies the new build and hands the swap to a separate script that
waits for it to exit. There are three differences, and all of them favor macOS.

* **Data is not inside the program.** Settings, recordings, chats and the login live in
  `~/Library/Application Support/Argus` (`host.py`), so the swap is simply replacing the
  whole bundle, without "save personal files, unpack, restore personal files".
* **Trust is the release signature, not Apple's signature.** The authenticity of an
  update rests on the signature of the release description made with the release key
  (`update_trust.py`) and on the archive hash. This works even for a build made without
  an Apple Developer account. After unpacking, the bundle is still verified
  (`codesign --verify`, the same bundle identifier). If the running program has a
  permanent signature (its own certificate or a Developer ID), the **same signing key**
  is required too: the system binds the microphone and screen-recording permissions to
  it, and a new build with a different key would make the user grant them again. An
  ad-hoc signature has no key. The permissions are bound to the build hash and are asked
  again after every update, so a distributed build must be signed with a permanent
  certificate.
* **It is not possible everywhere.** A program launched straight from "Downloads" or from
  a disk image is mounted read-only by the system (App Translocation), and writing into
  another folder is not allowed. In that case the update does not start, and the reason
  goes to the log.

It has its own "latest version" file, `latest-mac.json`, next to `latest.json`: the
Windows distribution must not hand Mac a foreign version and vice versa.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

LATEST_NAME = "latest-mac.json"
DEFAULT_ZIP = "Argus-mac.zip"
APP_NAME = "Argus.app"
EXECUTABLE = f"{APP_NAME}/Contents/MacOS/Argus"

# The swap: wait for the old process to exit, swap the bundle with a spare copy and open
# the new one. Any failure puts the old bundle back in place, because the user must never
# be left without the program.
#
# The spare copy lives until the new program confirms that it started: it writes a marker
# itself ($ALIVE, `update.ALIVE_NAME`) once it reaches the update check. The build check in
# CI runs the executable directly, while people open it through LaunchServices (`open`), so
# a crash with that kind of launch shows up only here. If there is no marker after
# ARGUS_ALIVE_TRIES half-seconds (180 by default, i.e. a minute and a half, because the
# first launch under Gatekeeper is slow), the previous bundle is put back in place, and the
# failed swap is recorded in `update.json` (`failed`) by the previous program itself. A retry
# happens no sooner than half an hour later.
SWAP_SCRIPT = r"""#!/bin/bash
OLD_PID="$1"; NEW_APP="$2"; APP="$3"; WORK="$4"; LOG="$5"; ALIVE="${6:-}"
exec >>"$LOG" 2>&1
echo "$(date '+%F %T') update: waiting for process $OLD_PID to exit"
for _ in $(seq 1 240); do
    kill -0 "$OLD_PID" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "the process did not exit within two minutes — update cancelled"
    rm -rf "$WORK"
    exit 1
fi
BACKUP="$APP.old.$$"
if /bin/mv "$APP" "$BACKUP" && /bin/mv "$NEW_APP" "$APP"; then
    echo "package replaced"
    if [ -n "$ALIVE" ]; then
        /bin/rm -f "$ALIVE"
    fi
    /usr/bin/open "$APP"
    if [ -n "$ALIVE" ]; then
        for _ in $(seq 1 "${ARGUS_ALIVE_TRIES:-180}"); do
            [ -f "$ALIVE" ] && break
            sleep 0.5
        done
        if [ ! -f "$ALIVE" ] && [ -d "$BACKUP" ]; then
            echo "the new program did not report that it started — restoring the previous package"
            /usr/bin/pkill -f "$APP/Contents/MacOS/Argus" 2>/dev/null
            sleep 1
            /bin/rm -rf "$APP"
            /bin/mv "$BACKUP" "$APP"
            /bin/rm -rf "$WORK"
            /usr/bin/open "$APP"
            exit 4
        fi
        echo "the new program started"
    fi
    /bin/rm -rf "$BACKUP" "$WORK"
else
    echo "replacement failed — restoring the previous package"
    [ -d "$BACKUP" ] && [ ! -d "$APP" ] && /bin/mv "$BACKUP" "$APP"
    /bin/rm -rf "$WORK"
    /usr/bin/open "$APP"
    exit 3
fi
"""

Runner = Callable[..., subprocess.CompletedProcess]


def cannot_update_reason(bundle: Path | None) -> str:
    """Why an update is impossible. Empty means it is possible."""
    if bundle is None:
        return "the app is not running from an .app bundle"
    if "/AppTranslocation/" in bundle.as_posix():
        return (
            "the app is running from Downloads or from a disk image — "
            "move it to Applications and open it from there"
        )
    if not os.access(bundle.parent, os.W_OK):
        return f"no permission for the folder {bundle.parent}"
    return ""


def looks_like_a_build(zip_path: Path) -> bool:
    """The archive opens and carries a package with an executable: not half a file because of a
    network break.
    """
    try:
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                return False
            return any(name == EXECUTABLE for name in z.namelist())
    except zipfile.BadZipFile:
        return False


def _signature_field(app: Path, key: str, run: Runner = subprocess.run) -> str:
    """A field value from `codesign -dv` (it prints to stderr). Empty means there is no such field or
    `codesign` did not answer.
    """
    try:
        result = run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (result.stderr or "").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def bundle_identifier(app: Path, run: Runner = subprocess.run) -> str:
    """The package identifier from the signature (`com.argus.assistant`). Empty means it cannot be
    read.
    """
    return _signature_field(app, "Identifier", run)


def designated_requirement(app: Path, run: Runner = subprocess.run) -> str:
    """The requirement on the code by which the system recognizes the program
    (`codesign -d -r-`). Empty means it could not be read.

    For a permanent-key signature this is "identifier and certificate", which does not change
    from build to build. For an ad-hoc signature it is the hash of the build itself
    (`cdhash`), and every build is a different program to the system.
    """
    try:
        result = run(
            ["/usr/bin/codesign", "-d", "-r-", str(app)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in ((result.stdout or "") + "\n" + (result.stderr or "")).splitlines():
        line = line.strip().lstrip("# ")
        if line.startswith("designated =>"):
            return line.split("=>", 1)[1].strip()
    return ""


def has_stable_identity(requirement: str) -> bool:
    """The signature is not a trial one: the system can "recognize" the app by it even after an
    update.
    """
    return bool(requirement) and "cdhash" not in requirement


def verify_bundle(new_app: Path, current_app: Path, run: Runner = subprocess.run) -> str:
    """Whether the downloaded bundle may be installed. Empty means yes; otherwise the reason.

    Authenticity is already confirmed by the release signature and the archive hash
    (`update.check_release`, `update.download_release`). Here we check that the bundle is
    intact and that it is the same Argus with the same signing key.
    """
    try:
        check = run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(new_app)],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"the signature cannot be checked: {e}"
    if check.returncode != 0:
        return "the signature of the downloaded bundle does not match"
    # If we could not read the identifier, we do not trust it: it could be a foreign bundle
    # posing as an Argus update.
    wanted = bundle_identifier(current_app, run)
    if not wanted or bundle_identifier(new_app, run) != wanted:
        return "the downloaded bundle is a different app"
    mine = designated_requirement(current_app, run)
    if has_stable_identity(mine) and designated_requirement(new_app, run) != mine:
        return "the downloaded bundle is signed with a different key — microphone and screen permissions would have to be granted again"
    return ""


def extract(zip_path: Path, into: Path, run: Runner = subprocess.run) -> Path | None:
    """Unpack with `ditto`: it keeps links, permissions and the signature, and `zipfile` does not."""
    try:
        done = run(
            ["/usr/bin/ditto", "-x", "-k", str(zip_path), str(into)],
            capture_output=True, timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    app = into / APP_NAME
    return app if done.returncode == 0 and (app / "Contents" / "MacOS").is_dir() else None


def spawn_swap(
    new_app: Path, bundle: Path, work: Path, log_path: Path, alive: Path | None = None,
) -> subprocess.Popen:
    """Start the swap script as a separate process that outlives us.

    `alive` is a file that the new program writes on startup; without it the script does not
    wait for the program and does not roll back.
    """
    script = work / "swap.sh"
    script.write_text(SWAP_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["/bin/bash", str(script), str(os.getpid()), str(new_app), str(bundle), str(work), str(log_path)]
    if alive is not None:
        args.append(str(alive))
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def install_release(server: str, release, root: Path, progress=None) -> bool:
    """Download the release, verify the bundle and hand the swap to the script. `True` means the
    swap is waiting for us to exit.

    On failure, `UpdateError` with the reason in words; the temporary folder is removed and
    the program stays as it was.
    """
    from argus import host
    from argus import update as shared

    bundle = host.app_bundle()
    reason = cannot_update_reason(bundle)
    if reason:
        raise shared.UpdateError(reason)
    assert bundle is not None

    work = Path(tempfile.mkdtemp(prefix="argus-update-"))
    try:
        archive = work / "update.zip"
        shared.download_release(server, release, archive, progress)
        if not looks_like_a_build(archive):
            raise shared.UpdateError("the downloaded archive does not look like an Argus build — update cancelled")
        unpacked = work / "new"
        unpacked.mkdir()
        new_app = extract(archive, unpacked)
        if new_app is None:
            raise shared.UpdateError("the archive could not be unpacked — update cancelled")
        archive.unlink(missing_ok=True)
        problem = verify_bundle(new_app, bundle)
        if problem:
            raise shared.UpdateError(f"update cancelled: {problem}")
        shared.note_pending(root, release)
        try:
            proc = spawn_swap(new_app, bundle, work, host.logs_dir() / "update.log", root / shared.ALIVE_NAME)
        except OSError as e:
            raise shared.UpdateError(f"could not start the swap ({e})") from e
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise

    # Popen does not raise even if the process dies at once; a second is longer than bash
    # needs to start.
    time.sleep(0.5)
    code = proc.poll()
    if code is not None:
        shutil.rmtree(work, ignore_errors=True)
        raise shared.UpdateError(f"the update process died immediately, code {code} — the version will stay the same")
    return True


def maybe_self_update(server: str, root: Path) -> bool:
    """Check at startup and, if there is a newer version, prepare the swap. `True` means the
    program is about to restart.

    The caller must exit right away, without bringing up the pipeline: open files and audio
    would get in the way of the swap.
    """
    from argus import update as shared

    return shared.maybe_self_update(server, root)
