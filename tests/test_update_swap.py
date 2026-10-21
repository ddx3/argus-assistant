"""Swap on Windows and attempt accounting: what the PowerShell script really does and what
the program remembers.

The script is run by a real Windows PowerShell on a fake program folder (`tmp_path`, no
Argus.exe, no windows, no network): a successful swap, a busy file (rollback), a foreign
archive, a living old program, brackets in the folder name. The rest is pure logic
without the system: memory of failed swaps, the "swap in progress" lock, the release
number in the build, leftovers in folders.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus import update, update_trust

POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
real_script = pytest.mark.skipif(not POWERSHELL, reason="needs Windows PowerShell")


@pytest.fixture(autouse=True)
def private_temp(monkeypatch, tmp_path):
    own = tmp_path / "_tmp"
    own.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(own))


# --------------------------------------------------------------------------
# A fake program and an archive
# --------------------------------------------------------------------------

OLD = {
    "Argus.exe": "old-exe",
    "_internal/a.dll": "old-a",
    "_internal/gone.dll": "old-gone",
    "VERSION": "1.0+old",
    "argus.toml": "old-toml",
    "argus.local.json": "mine",
    "argus.account.json": "token",
    "models/silero_vad.onnx": "old-vad",
    "models/extra.bin": "mine-weights",
    "sessions/s1/rec.wav": "audio",
}
NEW = {
    "Argus.exe": "new-exe",
    "_internal/a.dll": "new-a",
    "_internal/b.dll": "new-b",
    "VERSION": "1.0+new",
    "argus.toml": "new-toml",
    "argus.local.json": "voices-only",
    "models/silero_vad.onnx": "new-vad",
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(f"Argus/{name}", text)
    return path


def snapshot(root: Path) -> dict[str, str]:
    """Everything in the program folder except what the script itself writes (log, result, lock)."""
    skip = {"update.log", update.RESULT_NAME, update.LOCK_NAME, update.JOURNAL_NAME, update.ALIVE_NAME}
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*")) if p.is_file() and p.name not in skip
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def swap(tmp_path):
    """A program folder with the old build, an archive with the new one, and a run of the real
    script over them.
    """
    app = write_tree(tmp_path / "Argus", OLD)
    work = tmp_path / "_tmp" / "argus-update-test"
    work.mkdir()
    zip_path = make_zip(work / "update.zip", NEW)
    (app / update.LOCK_NAME).write_text(json.dumps({"pid": 1, "time": time.time()}), encoding="utf-8")

    def build(launch, override):
        script = work / "update.ps1"
        script.write_text(update._UPDATE_SCRIPT, encoding="utf-8-sig")
        args = {
            "OldPid": dead_pid(), "ZipPath": zip_path, "AppDir": app, "WorkDir": work, "LogPath": app / "update.log",
            "Sha256": sha256(zip_path), "Keep": ",".join(update.PRESERVE), "LockPath": app / update.LOCK_NAME,
            "ResultPath": app / update.RESULT_NAME, "Build": 2000, "Retries": 1, "RetryDelay": 0, "WaitSeconds": 2,
            "HealthCap": update.HEALTH_CAP_NAME, "AliveSeconds": 5,
        }
        args.update(override)
        # The launch mark and the log lie in the same program folder, whatever the test
        # substitutes.
        args.setdefault("AlivePath", Path(args["AppDir"]) / update.ALIVE_NAME)
        args.setdefault("JournalPath", Path(args["AppDir"]) / update.JOURNAL_NAME)
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        for key, value in args.items():
            command += [f"-{key}", str(value)]
        if not launch:
            command.append("-NoLaunch")                   # checking the script does not launch the program
        return command, args

    def finish(args, done):
        app_dir = Path(args["AppDir"])
        result_path = app_dir / update.RESULT_NAME
        result = json.loads(result_path.read_text(encoding="utf-8-sig")) if result_path.exists() else None
        log = (app_dir / "update.log").read_text(encoding="utf-8-sig") if (app_dir / "update.log").exists() else ""
        return SimpleNamespace(done=done, result=result, log=log)

    def run(launch=False, **override):
        command, args = build(launch, override)
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=240, creationflags=0x08000000, cwd=tempfile.gettempdir(),
        )
        return finish(args, done)

    def start(launch=False, **override):
        """The script in the background: the test can look at it in the middle of its work or kill
        it.
        """
        command, args = build(launch, override)
        proc = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000, cwd=tempfile.gettempdir(),
        )
        return proc

    return SimpleNamespace(app=app, work=work, zip=zip_path, run=run, start=start)


class Locked:
    """A file open in such a way that it can be neither replaced nor renamed (as an antivirus or
    another copy holds it).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Locked:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
        ]
        self.kernel32 = kernel32
        self.handle = kernel32.CreateFileW(str(self.path), 0x80000000, 1, None, 3, 0x80, None)   # read, share read-only
        assert self.handle not in (None, ctypes.c_void_p(-1).value), "could not lock the file"
        return self

    def __exit__(self, *exc) -> None:
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle(self.handle)


# --------------------------------------------------------------------------
# The script for real
# --------------------------------------------------------------------------


@real_script
def test_a_good_swap_replaces_the_build_keeps_personal_files_and_cleans_up(swap):
    out = swap.run()
    assert out.result and out.result["ok"] is True, (out.log, out.done.stderr)
    assert out.result["build"] == 2000 and out.result["version"] == "1.0+new"
    now = snapshot(swap.app)
    assert now["Argus.exe"] == "new-exe" and now["VERSION"] == "1.0+new" and now["argus.toml"] == "new-toml"
    assert now["_internal/a.dll"] == "new-a" and now["_internal/b.dll"] == "new-b"
    assert "_internal/gone.dll" not in now, "the folder is replaced as a whole, not laid over the old one"
    assert now["argus.local.json"] == "mine", "personal settings are not overwritten from the archive"
    assert now["argus.account.json"] == "token"
    assert now["models/silero_vad.onnx"] == "new-vad" and now["models/extra.bin"] == "mine-weights"
    assert now["sessions/s1/rec.wav"] == "audio"
    assert not list(swap.app.glob(".update-*")), "the spare and the unpacked folders are removed"
    assert not (swap.app / update.LOCK_NAME).exists(), "the lock is released"
    assert not swap.work.exists(), "the temporary folder with the archive and the script is removed"
    assert "archive hash matches" in out.log


@real_script
def test_a_locked_file_rolls_everything_back_and_says_so(swap):
    """Previously a busy file stayed old while Argus.exe and VERSION became new, and that counted
    as "updated".
    """
    before = snapshot(swap.app)
    with Locked(swap.app / "models" / "silero_vad.onnx"):
        out = swap.run()
    assert snapshot(swap.app) == before, "the previous program is restored as a whole"
    assert out.result and out.result["ok"] is False and out.result["error"], out.done.stderr
    assert "the previous program was restored" in out.log
    assert not list(swap.app.glob(".update-*"))
    assert not (swap.app / update.LOCK_NAME).exists() and not swap.work.exists()


@real_script
def test_a_locked_program_folder_rolls_back_too(swap):
    """A busy file inside the folder being replaced (`_internal`) also means a rollback, not a
    mixture.
    """
    before = snapshot(swap.app)
    with Locked(swap.app / "_internal" / "a.dll"):
        out = swap.run()
    assert snapshot(swap.app) == before
    assert out.result and out.result["ok"] is False


@real_script
def test_a_locked_file_late_in_the_folder_does_not_split_it(swap):
    """Previously `Move-Item` on a folder with a busy file fell back to moving one file at a time
    and broke off in the middle: `a.dll` and `gone.dll` ended up in the spare folder, `zz.dll`
    stayed, the rollback could not reassemble the folder, and the cleanup erased the spare,
    leaving a program without half its libraries. The busy file is the last one in order, so
    the earlier ones had already moved before the break."""
    (swap.app / "_internal" / "zz.dll").write_text("old-zz", encoding="utf-8")
    bundle = make_zip(swap.work / "with-zz.zip", {**NEW, "_internal/zz.dll": "new-zz"})
    before = snapshot(swap.app)
    with Locked(swap.app / "_internal" / "zz.dll"):
        out = swap.run(ZipPath=bundle, Sha256=sha256(bundle))
    assert out.result and out.result["ok"] is False, (out.log, out.done.stderr)
    assert snapshot(swap.app) == before, "the folder is intact, no spare or unpacked leftovers"
    assert "the previous program was restored" in out.log


@real_script
def test_a_tampered_archive_changes_nothing(swap):
    before = snapshot(swap.app)
    out = swap.run(Sha256="0" * 64)
    assert snapshot(swap.app) == before
    assert out.result and out.result["ok"] is False and "hash" in out.result["error"]
    assert not list(swap.app.glob(".update-*"))


@real_script
def test_without_a_signed_hash_nothing_is_swapped(swap):
    before = snapshot(swap.app)
    out = swap.run(Sha256="")
    assert snapshot(swap.app) == before and out.result["ok"] is False


@real_script
def test_a_still_running_old_program_stops_the_swap(swap):
    """The old program did not close: its files are busy and must not be touched."""
    before = snapshot(swap.app)
    out = swap.run(OldPid=os.getpid(), WaitSeconds=2)
    assert snapshot(swap.app) == before
    assert out.result and out.result["ok"] is False and "did not close" in out.result["error"]


@real_script
def test_brackets_in_the_folder_name_are_not_a_wildcard(tmp_path, swap):
    """`Argus [portable]`: with `-Path`, PowerShell read the brackets as a wildcard, and the
    script copied nothing.
    """
    portable = tmp_path / "Argus [portable]"
    shutil.move(str(swap.app), str(portable))
    swap.run(AppDir=portable, LogPath=portable / "update.log", LockPath=portable / update.LOCK_NAME,
             ResultPath=portable / update.RESULT_NAME)
    result = json.loads((portable / update.RESULT_NAME).read_text(encoding="utf-8-sig"))
    assert result["ok"] is True, (portable / "update.log").read_text(encoding="utf-8-sig")
    assert (portable / "Argus.exe").read_text(encoding="utf-8") == "new-exe"
    assert (portable / "argus.local.json").read_text(encoding="utf-8") == "mine"


@real_script
@pytest.mark.parametrize("folder", ["Argus", "Argus [portable]"])
def test_the_program_is_started_again_whatever_the_folder_is_called(tmp_path, swap, folder):
    """`Start-Process -WorkingDirectory` read the brackets in the folder name as a wildcard and
    did not start the program: the swap went through, but the person was left without a
    window. Instead of `Argus.exe` there is a real tiny program (`whoami`): its launch is the
    check."""
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    if not exe.is_file():
        pytest.skip("no whoami.exe")
    target = tmp_path / folder
    if target != swap.app:
        shutil.move(str(swap.app), str(target))
    bundle = swap.work / "with-exe.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("Argus/Argus.exe", exe.read_bytes())
        z.writestr("Argus/VERSION", "1.0+new")
    out = swap.run(
        launch=True, ZipPath=bundle, Sha256=sha256(bundle), AppDir=target, LogPath=target / "update.log",
        LockPath=target / update.LOCK_NAME, ResultPath=target / update.RESULT_NAME,
    )
    assert out.result and out.result["ok"] is True, (out.log, out.done.stderr)
    assert "program started" in out.log and "could not be started" not in out.log, out.log


@real_script
def test_an_archive_without_the_program_is_refused(swap):
    bad = make_zip(swap.work / "bad.zip", {"readme.txt": "x"})
    before = snapshot(swap.app)
    out = swap.run(ZipPath=bad, Sha256=sha256(bad))
    assert snapshot(swap.app) == before
    assert out.result["ok"] is False and "Argus.exe" in out.result["error"]


# --------------------------------------------------------------------------
# Checking the new program after restart and rollback
# --------------------------------------------------------------------------


def healthy_zip(swap, name="healthy.zip", exe: bytes | None = None) -> Path:
    """An archive of a new build that can report in on launch (it contains `HEALTH_CAP_NAME`)."""
    path = swap.work / name
    with zipfile.ZipFile(path, "w") as z:
        for relative, text in {**NEW, update.HEALTH_CAP_NAME: "1"}.items():
            z.writestr(f"Argus/{relative}", exe if (exe is not None and relative == "Argus.exe") else text)
    return path


def report_in_when_started(app: Path, marker: str = "launch skipped") -> threading.Thread:
    """Like the new program: after launch (a line in the script's log) it writes an `update-alive`
    mark.
    """
    def work():
        end = time.monotonic() + 90
        while time.monotonic() < end:
            try:
                if marker in (app / "update.log").read_text(encoding="utf-8-sig"):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        (app / update.ALIVE_NAME).write_text("1", encoding="utf-8")

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


@real_script
def test_a_new_build_that_reports_in_stays_and_the_backups_go(swap):
    bundle = healthy_zip(swap)
    report_in_when_started(swap.app)
    out = swap.run(ZipPath=bundle, Sha256=sha256(bundle), AliveSeconds=60)
    assert out.result and out.result["ok"] is True, (out.log, out.done.stderr)
    now = snapshot(swap.app)
    assert now["Argus.exe"] == "new-exe" and now["VERSION"] == "1.0+new" and now[update.HEALTH_CAP_NAME] == "1"
    assert "the new program checked in about its launch" in out.log
    assert not list(swap.app.glob(".update-*")), "the spare folder is removed only after the check-in"
    assert not (swap.app / update.JOURNAL_NAME).exists() and not (swap.app / update.LOCK_NAME).exists()


@real_script
def test_a_new_build_that_never_reports_in_is_rolled_back_and_the_failure_is_remembered(swap):
    """Previously a new build that crashed on startup stayed forever, because the script had
    already erased the old files.
    """
    update.note_pending(swap.app, update.Release(version="1.0+new", zip="Argus-win.zip", sha256="a" * 64, size=1, build=2000))
    before = snapshot(swap.app)
    bundle = healthy_zip(swap)
    out = swap.run(ZipPath=bundle, Sha256=sha256(bundle), AliveSeconds=2)
    assert snapshot(swap.app) == before, "the previous program is restored as a whole, nothing of the new one is left"
    assert out.result and out.result["ok"] is False and "did not check in" in out.result["error"], out.log
    assert "the new program checked in" not in out.log and "the previous program was restored" in out.log
    assert not list(swap.app.glob(".update-*")) and not (swap.app / update.JOURNAL_NAME).exists()
    assert not (swap.app / update.LOCK_NAME).exists() and not swap.work.exists()
    # On its next launch the old program reads the result and remembers the failure: the same
    # release is not downloaded at every launch.
    update.installed_build(swap.app)
    failed = update._read_state(swap.app)["failed"]
    assert failed["build"] == 2000 and failed["attempts"] == 1


@real_script
def test_the_old_program_is_started_again_after_the_rollback(tmp_path, swap):
    """A real executable instead of `Argus.exe` (`whoami`): after the rollback it is launched a
    second time, now the old one.
    """
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    if not exe.is_file():
        pytest.skip("no whoami.exe")
    (swap.app / "Argus.exe").write_bytes(exe.read_bytes())
    old_hash = hashlib.sha256((swap.app / "Argus.exe").read_bytes()).hexdigest()
    bundle = healthy_zip(swap, exe=exe.read_bytes() + b"\0new")
    out = swap.run(launch=True, ZipPath=bundle, Sha256=sha256(bundle), AliveSeconds=2)
    assert out.result and out.result["ok"] is False, (out.log, out.done.stderr)
    assert out.log.count("program started") == 2, out.log
    assert hashlib.sha256((swap.app / "Argus.exe").read_bytes()).hexdigest() == old_hash, "the previous build is in place"


@real_script
def test_a_build_without_the_capability_file_is_not_waited_for(swap):
    """A release re-signed from an earlier version (`publish_win.py --rollback`) cannot report in:
    waiting for it would roll back the rollback itself.
    """
    started = time.monotonic()
    out = swap.run(AliveSeconds=300)                 # NEW without `update-alive.v1`
    assert out.result and out.result["ok"] is True, out.log
    assert time.monotonic() - started < 120
    assert "checked in" not in out.log and "did not check in" not in out.log and snapshot(swap.app)["Argus.exe"] == "new-exe"


@real_script
def test_the_capability_is_read_from_the_new_build_not_from_leftovers_of_the_old_one(swap):
    """`update-alive.v1` is left in the folder from the old build, but the new one does not carry
    it: it cannot report in, so there is nothing to wait for.
    """
    (swap.app / update.HEALTH_CAP_NAME).write_text("1", encoding="utf-8")
    out = swap.run(AliveSeconds=300)
    assert out.result and out.result["ok"] is True and "checked in" not in out.log and "did not check in" not in out.log, out.log


@real_script
def test_a_script_killed_in_the_middle_of_the_swap_is_rolled_back_at_the_next_start(swap):
    """The power was cut (the script was killed) after `_internal`, `Argus.exe`, and `argus.toml`
    were already new but before `VERSION` was reached: a mixed installation. The next launch
    of the program restores the old files from the log."""
    before = snapshot(swap.app)
    with Locked(swap.app / "VERSION"):           # the script changes `VERSION` last: until then everything else is already new
        proc = swap.start(Retries=120, RetryDelay=1)
        end = time.monotonic() + 90
        while time.monotonic() < end:
            log = swap.app / "update.log"
            if log.exists() and "busy, retry" in log.read_text(encoding="utf-8-sig"):
                break
            time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail("the script did not reach the busy file")
        (swap.app / update.LOCK_NAME).write_text(json.dumps({"pid": proc.pid, "time": time.time()}), encoding="utf-8")
        proc.kill()
        proc.wait()
    mixed = snapshot(swap.app)
    log_text = (swap.app / "update.log").read_text(encoding="utf-8-sig")
    assert mixed["Argus.exe"] == "new-exe" and mixed["_internal/a.dll"] == "new-a" and mixed["VERSION"] == "1.0+old", log_text
    journal = json.loads((swap.app / update.JOURNAL_NAME).read_text(encoding="utf-8-sig"))
    assert journal["phase"] == "swapping" and journal["pid"] == proc.pid and len(journal["steps"]) >= 4
    assert list(swap.app.glob(".update-*"))

    assert update.update_in_progress(swap.app) is False, "the lock of a dead script does not keep the program closed"
    update.recover_interrupted_swap(swap.app)
    assert snapshot(swap.app) == before, "the previous build is restored as a whole"
    assert not list(swap.app.glob(".update-*")) and not (swap.app / update.JOURNAL_NAME).exists()


@real_script
def test_a_rollback_that_fails_keeps_the_old_files_and_the_next_start_finishes_it(swap):
    """The new build did not report in, and the old `_internal` folder in the spare place is busy
    (an antivirus is still reading it): the script cannot put it back. The spare files stay,
    the log is marked "rollback failed", and the next launch of the program restores
    everything from the log."""
    bundle = healthy_zip(swap)
    before = snapshot(swap.app)
    release = threading.Event()

    def hold_a_file_in_the_backup():
        end = time.monotonic() + 90
        while time.monotonic() < end:
            found = list(swap.app.glob(".update-*/old/_internal/a.dll"))
            log = swap.app / "update.log"
            if found and log.exists() and "launch skipped" in log.read_text(encoding="utf-8-sig"):
                with Locked(found[0]):
                    release.wait(60)
                return
            time.sleep(0.05)

    holder = threading.Thread(target=hold_a_file_in_the_backup, daemon=True)
    holder.start()
    out = swap.run(ZipPath=bundle, Sha256=sha256(bundle), AliveSeconds=4)
    release.set()
    holder.join(30)
    assert out.result and out.result["ok"] is False, out.log
    assert "rollback failed" in out.log
    journal = json.loads((swap.app / update.JOURNAL_NAME).read_text(encoding="utf-8-sig"))
    assert journal["phase"] == "rollback-failed"
    assert list(swap.app.glob(".update-*/old/_internal/a.dll")), "the only copies of the previous files are not erased"
    assert not (swap.app / "_internal").exists(), "the folder is missing: the program will not start without it"

    update.recover_interrupted_swap(swap.app)
    assert snapshot(swap.app) == before, "the next launch restored the previous build as a whole"
    assert not list(swap.app.glob(".update-*")) and not (swap.app / update.JOURNAL_NAME).exists()


def test_the_script_drops_the_lock_before_the_launch_and_waits_after_it():
    """The lock is released before the launch: otherwise the new program would see "a swap is in
    progress", quietly exit, and not report in.
    """
    text = update._UPDATE_SCRIPT
    tail = text[text.index("} finally {"):]
    assert tail.index("Remove-Item -LiteralPath $LockPath") < tail.index("Start-App") < tail.index("Wait-Alive")
    assert "Join-Path $inner.FullName $HealthCap" in text, "the capability is read from the NEW build"
    assert tail.index("Save-Journal 'rolling-back'") < tail.index("Invoke-Rollback")


# --------------------------------------------------------------------------
# The next launch after an interrupted swap (without PowerShell: the log is assembled by
# hand)
# --------------------------------------------------------------------------


def half_swapped(root: Path, phase: str, pid: int, steps_extra: list | None = None) -> Path:
    """The program folder in the middle of a swap: `Argus.exe` is already new, the old one is in
    the spare folder, and the new file is in its place.
    """
    write_tree(root, {"Argus.exe": "new-exe", "VERSION": "1.0+old", "extra.txt": "new-extra"})
    stage = root / ".update-deadbeef"
    write_tree(stage / "old", {"Argus.exe": "old-exe"})
    steps = [
        {"target": str(root / "Argus.exe"), "backup": str(stage / "old" / "Argus.exe"), "hadOld": True},
        {"target": str(root / "extra.txt"), "backup": str(stage / "old" / "extra.txt"), "hadOld": False},
        {"target": str(root / "VERSION"), "backup": str(stage / "old" / "VERSION"), "hadOld": True},
    ] + (steps_extra or [])
    (root / update.JOURNAL_NAME).write_text(json.dumps({
        "pid": pid, "stage": str(stage), "work": str(root / "nowhere"), "build": 2000, "phase": phase, "steps": steps,
    }), encoding="utf-8")
    return stage


@pytest.mark.parametrize("phase", ["swapping", "rollback-failed", "rolling-back"])
def test_an_interrupted_swap_is_rolled_back_from_the_journal(tmp_path, phase):
    root = tmp_path / "app"
    stage = half_swapped(root, phase, dead_pid())
    update.recover_interrupted_swap(root)
    assert (root / "Argus.exe").read_text(encoding="utf-8") == "old-exe", "the spare file came back"
    assert not (root / "extra.txt").exists(), "a new file that did not exist before is removed"
    assert (root / "VERSION").read_text(encoding="utf-8") == "1.0+old"
    assert not stage.exists() and not (root / update.JOURNAL_NAME).exists()


def test_a_finished_swap_whose_script_died_before_cleaning_up_just_loses_the_backups(tmp_path):
    root = tmp_path / "app"
    stage = half_swapped(root, "swapped", dead_pid())
    update.recover_interrupted_swap(root)
    assert (root / "Argus.exe").read_text(encoding="utf-8") == "new-exe", "the swap is finished: the new one stays"
    assert not stage.exists() and not (root / update.JOURNAL_NAME).exists()


def test_a_live_script_is_left_alone(tmp_path):
    """The script waits for the new program's mark (or rolls back by itself): the launch we are
    running in does not get in its way.
    """
    root = tmp_path / "app"
    stage = half_swapped(root, "swapped", os.getpid())
    update.recover_interrupted_swap(root)
    assert stage.exists() and (root / update.JOURNAL_NAME).exists()


def test_a_journal_with_foreign_paths_is_not_followed(tmp_path):
    root = tmp_path / "app"
    outside = tmp_path / "outside"
    write_tree(outside, {"keep.txt": "x"})
    stage = half_swapped(root, "swapping", dead_pid(), steps_extra=[
        {"target": str(outside / "keep.txt"), "backup": str(stage_path(root) / "old" / "nothing"), "hadOld": False},
    ])
    update.recover_interrupted_swap(root)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "x", "the journal does not let anything outside the program folder"
    assert stage.exists() and (root / update.JOURNAL_NAME).exists(), "incomplete rollback: the spare and the journal stay"
    (root / update.JOURNAL_NAME).write_text(json.dumps({"pid": 0, "stage": str(tmp_path), "phase": "swapped"}), encoding="utf-8")
    update.recover_interrupted_swap(root)
    assert tmp_path.exists() and outside.exists(), "a foreign folder in place of the spare one is not erased"


def stage_path(root: Path) -> Path:
    return root / ".update-deadbeef"


def test_an_unreadable_journal_is_kept_and_keeps_the_backups_from_the_sweep(tmp_path):
    root = tmp_path / "app"
    stage = half_swapped(root, "swapping", dead_pid())
    (root / update.JOURNAL_NAME).write_text("{truncated", encoding="utf-8")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(stage, (long_ago, long_ago))
    update.recover_interrupted_swap(root)
    update._sweep_leftovers(root)
    assert stage.exists() and (root / update.JOURNAL_NAME).exists(), "we do not erase the only copies of the previous files blindly"


def test_a_stage_without_backups_is_swept_after_a_quarter_of_an_hour_and_one_with_backups_is_not(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    empty = root / ".update-00000001"
    (empty / "new" / "Argus").mkdir(parents=True)
    (empty / "old").mkdir()
    precious = root / ".update-00000002"
    write_tree(precious / "old", {"Argus.exe": "old-exe"})
    for path in (empty, precious):
        past = time.time() - 20 * 60
        os.utime(path, (past, past))
    update._sweep_leftovers(root)
    assert not empty.exists(), "it holds only the unpacked copy of the archive"
    assert precious.exists(), "it holds the previous files: not touched for up to a day"


def test_starting_the_program_marks_it_alive_and_finishes_an_interrupted_swap(win):
    stage = half_swapped(win.root, "swapping", dead_pid())
    assert update.maybe_self_update("", win.root) is False
    assert (win.root / update.ALIVE_NAME).is_file(), "the swap script waits for this mark"
    assert (win.root / "Argus.exe").read_text(encoding="utf-8") == "old-exe" and not stage.exists()


# --------------------------------------------------------------------------
# The script text and how it is launched
# --------------------------------------------------------------------------


def test_the_script_stops_on_errors_and_never_treats_a_folder_name_as_a_pattern():
    text = update._UPDATE_SCRIPT
    assert "$ErrorActionPreference = 'Stop'" in text
    assert not re.search(r"(?<![\w-])-Path\b", text), "-Path reads [ ] as a pattern; -LiteralPath is needed"
    assert "Copy-Item" not in text, "copying over silently left a mixture; now it is a rename with a rollback"
    assert "-Sha256" not in text and "$Sha256" in text and "Get-FileHash" in text, "the archive is checked against the signed hash"


def test_a_failed_rollback_keeps_the_only_copy_of_the_old_files():
    """The rollback failed: the spare folder keeps the only copy of the old files, and the cleanup
    in `finally` does not touch it.
    """
    text = update._UPDATE_SCRIPT
    assert "$script:rollbackFailed = $true" in text
    assert re.search(r"if \(\$rollbackFailed -and \$leftover -eq \$stage\) \{ continue \}", text)
    assert "Save-Journal 'rollback-failed'" in text, "the spare folder and the journal stay for the rollback at the next launch"


@pytest.mark.windows_only          # `subprocess.CREATE_NO_WINDOW` exists only on Windows
def test_the_script_is_written_with_a_byte_order_mark(monkeypatch, tmp_path):
    """Without it, PowerShell 5.1 reads the file in the system encoding, and a non-ASCII
    character (a capital Cyrillic letter, say) in a string breaks parsing.
    """
    work = tmp_path / "w"
    work.mkdir()
    zip_path = work / "update.zip"
    zip_path.write_bytes(b"zip")
    started = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        started.append((command, kwargs))
        return FakeProc()

    monkeypatch.setattr(update.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    monkeypatch.setattr(update, "_powershell", lambda: "powershell.exe")
    app = tmp_path / "app"
    app.mkdir()
    release = update.Release(version="1.0+new", zip="Argus-win.zip", sha256="ab" * 32, size=3, build=777)

    assert update._apply(zip_path, app, release) is True

    assert (work / "update.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
    command, kwargs = started[0]
    flat = " ".join(command)
    assert "-Sha256 " + "ab" * 32 in flat and "-Build 777" in flat
    assert command[command.index("-Keep") + 1] == ",".join(update.PRESERVE), "the list of personal files comes from here, not from a copy in the script"
    for flag, value in (
        ("-AlivePath", str(app / update.ALIVE_NAME)), ("-HealthCap", update.HEALTH_CAP_NAME),
        ("-JournalPath", str(app / update.JOURNAL_NAME)), ("-AliveSeconds", str(update.ALIVE_WAIT_S)),
    ):
        assert command[command.index(flag) + 1] == value, "the script gets the file names from here, not from its own copy"
    assert kwargs["cwd"] == tempfile.gettempdir(), "the working folder of the script is not the program folder"
    lock = json.loads((app / update.LOCK_NAME).read_text(encoding="utf-8"))
    assert lock["pid"] == 4242, "the lock is written right after the script starts"


@pytest.mark.windows_only
def test_a_swap_that_dies_at_once_leaves_no_lock(monkeypatch, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "update.zip").write_bytes(b"zip")
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=7, poll=lambda: 1))
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    monkeypatch.setattr(update, "_powershell", lambda: "powershell.exe")
    release = update.Release(version="1.0+new", zip="Argus-win.zip", sha256="ab" * 32, size=3, build=777)
    assert update._apply(work / "update.zip", app, release) is False
    assert not (app / update.LOCK_NAME).exists(), "the lock of a dead script must not keep the program closed"


# --------------------------------------------------------------------------
# Memory of failed swaps
# --------------------------------------------------------------------------


@pytest.fixture
def win(monkeypatch, tmp_path, release_signer):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "_powershell", lambda: "powershell.exe")
    root = tmp_path / "app"
    root.mkdir()
    (root / update.VERSION_NAME).write_text("1.0+old", encoding="utf-8")
    manifest = release_signer.manifest("win")
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: manifest.info)
    return SimpleNamespace(root=root, manifest=manifest, signer=release_signer)


def fail_a_swap(win, release):
    """The script was run, but `VERSION` is still the old one: the swap failed."""
    update.note_pending(win.root, release)
    update.installed_build(win.root)


def release_of(win) -> update.Release:
    return update.check_release("http://example.invalid", win.root)


def test_a_failed_swap_is_remembered_and_the_same_release_is_not_retried_at_once(win):
    release = release_of(win)
    fail_a_swap(win, release)
    failed = update._read_state(win.root)["failed"]
    assert failed["build"] == 2000 and failed["attempts"] == 1
    assert 0 < update.retry_wait(win.root, release) <= 30 * 60
    other = update.Release(version="1.0+newer", zip="Argus-win.zip", sha256="a" * 64, size=1, build=2001)
    assert update.retry_wait(win.root, other) == 0, "another release is tried at once"


def test_the_wait_grows_to_a_day_after_the_second_failure_and_ends(win, monkeypatch):
    release = release_of(win)
    fail_a_swap(win, release)
    fail_a_swap(win, release)
    assert update._read_state(win.root)["failed"]["attempts"] == 2
    assert 3600 < update.retry_wait(win.root, release) <= 24 * 3600
    later = time.time() + 25 * 3600
    monkeypatch.setattr(update, "time", SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep))
    assert update.retry_wait(win.root, release) == 0, "after a day we try again"


def test_a_successful_swap_clears_the_failures(win):
    release = release_of(win)
    fail_a_swap(win, release)
    update.note_pending(win.root, release)
    (win.root / update.VERSION_NAME).write_text("1.0+new", encoding="utf-8")
    assert update.installed_build(win.root) == 2000
    assert "failed" not in update._read_state(win.root)


def test_five_launches_with_a_swap_that_never_takes_effect_download_twice_not_five_times(win, monkeypatch):
    """Reproduction from the audit: the swap does not change VERSION, and previously every launch
    downloaded the archive again.
    """
    downloads = []
    monkeypatch.setattr(update, "download_release", lambda server, release, dest, progress=None: (
        downloads.append(1), zipfile.ZipFile(dest, "w").writestr("Argus/Argus.exe", b"x"))[0])
    monkeypatch.setattr(update, "_apply", lambda zip_path, root, release: True)
    now = [time.time()]
    monkeypatch.setattr(update, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic, sleep=time.sleep))
    for _ in range(3):                                   # three launches in a row, one right after another
        assert update.maybe_self_update("http://example.invalid", win.root) in (True, False)
    assert len(downloads) == 1, "the second and third launch do not download what has just failed"
    now[0] += 31 * 60                                    # a second attempt after half an hour
    update.maybe_self_update("http://example.invalid", win.root)
    update.maybe_self_update("http://example.invalid", win.root)
    assert len(downloads) == 2
    now[0] += 31 * 60                                    # after the second failure, a day
    update.maybe_self_update("http://example.invalid", win.root)
    assert len(downloads) == 2


def test_the_result_of_the_script_is_read_once_and_trusted_when_it_says_done(win):
    """The script took the swap to the end, although `VERSION` in the build is written
    differently: the release counts as installed, and there is no loop.
    """
    release = release_of(win)
    update.note_pending(win.root, release)
    (win.root / update.RESULT_NAME).write_text(json.dumps({"ok": True, "build": 2000, "version": "other"}), encoding="utf-8")
    assert update.installed_build(win.root) == 2000
    assert not (win.root / update.RESULT_NAME).exists()


def test_the_error_of_the_script_goes_to_the_log(win, caplog):
    release = release_of(win)
    update.note_pending(win.root, release)
    (win.root / update.RESULT_NAME).write_text(json.dumps({"ok": False, "error": "the file is busy"}), encoding="utf-8-sig")
    with caplog.at_level("WARNING"):
        update.installed_build(win.root)
    assert "the file is busy" in caplog.text


def test_a_garbage_number_in_the_state_is_zero_not_a_crash(win):
    (win.root / update.STATE_NAME).write_text('{"build": "x", "pending": {"version": "v", "build": []}}', encoding="utf-8")
    assert update.installed_build(win.root) == 0


def test_the_state_is_written_atomically(win, monkeypatch):
    update._write_state(win.root, {"build": 5})
    real_replace = os.replace

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(update.os, "replace", broken)
    update._write_state(win.root, {"build": 9})
    monkeypatch.setattr(update.os, "replace", real_replace)
    assert update._read_state(win.root) == {"build": 5}, "a broken write does not erase the previous state"
    assert not list(win.root.glob("update.json.tmp"))


# --------------------------------------------------------------------------
# Release number inside the build itself: protection from a rollback without update.json
# --------------------------------------------------------------------------


def test_the_build_number_inside_the_folder_blocks_a_rollback_on_a_fresh_install(win):
    (win.root / update.BUILD_NAME).write_text("3000\n", encoding="utf-8")
    assert not (win.root / update.STATE_NAME).exists()
    assert update.installed_build(win.root) == 3000
    assert update.check_release("http://example.invalid", win.root) is None, "signed but old release: a rollback"


def test_a_lost_state_file_does_not_empty_the_rollback_guard(win):
    (win.root / update.BUILD_NAME).write_text("1999", encoding="utf-8")
    (win.root / update.STATE_NAME).write_text("{", encoding="utf-8")          # a truncated record
    assert update.check_release("http://example.invalid", win.root).build == 2000
    (win.root / update.BUILD_NAME).write_text("2000", encoding="utf-8")
    assert update.check_release("http://example.invalid", win.root) is None


# --------------------------------------------------------------------------
# The "swap in progress" lock
# --------------------------------------------------------------------------


def put_lock(root: Path, pid: int, age_s: float = 0.0) -> Path:
    path = root / update.LOCK_NAME
    path.write_text(json.dumps({"pid": pid, "time": time.time() - age_s}), encoding="utf-8")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_a_second_launch_during_the_swap_exits_quietly_without_touching_anything(win, monkeypatch):
    put_lock(win.root, os.getpid())                      # the script is alive (for the check, this very process)
    monkeypatch.setattr(update, "check_release", lambda *a, **k: (_ for _ in ()).throw(AssertionError("do not ask")))
    assert update.maybe_self_update("http://example.invalid", win.root) is True


def test_the_lock_of_a_dead_script_does_not_keep_the_app_closed(win):
    lock = put_lock(win.root, dead_pid())
    assert update.update_in_progress(win.root) is False
    assert not lock.exists()


def test_an_old_lock_is_ignored_even_if_the_pid_was_reused(win):
    lock = put_lock(win.root, os.getpid(), age_s=update.LOCK_MAX_AGE_S + 60)
    assert update.update_in_progress(win.root) is False
    assert not lock.exists()


def test_a_lock_the_script_keeps_touching_stays_valid(win):
    lock = put_lock(win.root, os.getpid(), age_s=update.LOCK_MAX_AGE_S + 60)
    now = time.time()
    os.utime(lock, (now, now))                           # the script marked the lock at its next step
    assert update.update_in_progress(win.root) is True


def test_no_lock_and_a_garbage_lock(win):
    assert update.update_in_progress(win.root) is False
    (win.root / update.LOCK_NAME).write_text("not json", encoding="utf-8")
    assert update.update_in_progress(win.root) is True, "a fresh lock without a pid is respected by its age"
    old = time.time() - update.LOCK_MAX_AGE_S - 60
    os.utime(win.root / update.LOCK_NAME, (old, old))
    assert update.update_in_progress(win.root) is False


def test_the_liveness_check_never_kills_anything():
    """`os.kill(pid, 0)` on Windows is not a check but `TerminateProcess`: we check in a way that
    leaves our own process alive.
    """
    assert update._pid_alive(os.getpid()) is True
    assert update._pid_alive(dead_pid()) is False
    assert update._pid_alive(0) is False


# --------------------------------------------------------------------------
# Leftovers of interrupted swaps
# --------------------------------------------------------------------------


def test_leftovers_older_than_a_day_are_swept_and_fresh_ones_are_kept(win):
    stale_stage = win.root / ".update-deadbeef"
    stale_stage.mkdir()
    fresh_stage = win.root / ".update-cafe0000"
    fresh_stage.mkdir()
    temp = Path(tempfile.gettempdir())
    stale_zip = temp / "argus-update-1.zip"
    stale_zip.write_bytes(b"x")
    stale_dir = temp / "argus-update-abc123"
    stale_dir.mkdir()
    (stale_dir / "update.ps1").write_text("x", encoding="utf-8")
    unrelated = temp / "something-else"
    unrelated.mkdir()
    long_ago = time.time() - 3 * 24 * 3600
    for path in (stale_stage, stale_zip, stale_dir, unrelated):
        os.utime(path, (long_ago, long_ago))
    update._sweep_leftovers(win.root)
    assert not stale_stage.exists() and not stale_zip.exists() and not stale_dir.exists()
    assert fresh_stage.exists(), "a fresh one may be a swap running right now"
    assert unrelated.exists(), "we do not touch what is not ours"


# --------------------------------------------------------------------------
# Download and archive name
# --------------------------------------------------------------------------


@pytest.mark.parametrize("zip_name", ["Argus-win.zip\n", "Argus-win.zip\r\n"])
def test_a_trailing_newline_in_the_archive_name_is_refused(release_signer, zip_name):
    signed = release_signer.manifest("win", zip_name=zip_name).info
    assert "archive name" in update_trust.problem_with_manifest(signed, "win")


def test_a_trailing_newline_in_the_hash_is_refused(release_signer):
    info = release_signer.manifest("win").info
    info = update_trust.sign_manifest(dict(info, sha256=info["sha256"] + "\n"), "win", release_signer.seed)
    assert "hash" in update_trust.problem_with_manifest(info, "win")


def test_an_invalid_url_is_an_update_error_not_a_crash(monkeypatch, tmp_path):
    import httpx

    def broken(*a, **k):
        raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")

    monkeypatch.setattr(update.httpx, "stream", broken)
    release = update.Release(version="v", zip="Argus-win.zip", sha256="a" * 64, size=1, build=1)
    with pytest.raises(update.UpdateError):
        update.download_release("http://example.invalid", release, tmp_path / "u.zip")


def test_a_file_that_cannot_be_deleted_does_not_hide_the_real_error(monkeypatch, tmp_path):
    import httpx

    def refuse(self, missing_ok=False):
        raise PermissionError("busy")

    monkeypatch.setattr(update.httpx, "stream", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no connection")))
    monkeypatch.setattr(Path, "unlink", refuse)
    release = update.Release(version="v", zip="Argus-win.zip", sha256="a" * 64, size=1, build=1)
    with pytest.raises(update.UpdateError, match="couldn't download the update"):
        update.download_release("http://example.invalid", release, tmp_path / "u.zip")


def test_install_release_leaves_no_temporary_folder_when_it_fails(win, monkeypatch):
    def broken(server, release, dest, progress=None):
        raise update.UpdateError("no connection")

    monkeypatch.setattr(update, "download_release", broken)
    with pytest.raises(update.UpdateError):
        update.install_release("http://example.invalid", release_of(win), win.root)
    assert not list(Path(tempfile.gettempdir()).glob("argus-update-*"))
