"""Publish the Windows build to the download server (nginx, 203.0.113.10) with one command.

    python deploy/win/publish_win.py --app dist/Argus # check, pack, upload
    python deploy/win/publish_win.py --app dist/Argus --check # no upload
    python deploy/win/publish_win.py --app ... --no-smoke   # skip the trial launch of the program
    python deploy/win/publish_win.py --rollback             # restore the previous release
                                                            # (signed again)
    python deploy/win/publish_win.py --app ... --allow-legal-placeholders   # deliberate

It is built separately, without touching the running app in `dist/` (README in `packaging/`).
Easiest with a script that always sets both build "flavor" variables and lays out the files
for distribution:

    powershell -ExecutionPolicy Bypass -File packaging\\build_win.ps1

The same by hand:

    $env:ARGUS_BUILD_API_ONLY="1"; $env:ARGUS_BUILD_NO_SUBSCRIPTION="1"
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --distpath dist --workpath build/work packaging/argus.spec
    .venv\\Scripts\\python.exe packaging/stage.py --public --app dist/Argus

What is checked before anything goes to the server: the folder contains only what it should
(`ALLOWED_TOP`: no `.env`, no login, no sessions, no log, no `argus.legal.json`), there is no
subscription path to the model (`claude.exe`, `claude_agent_sdk`) and no local engines, not a
single real value from the project's `.env` (searched byte by byte in all files, including
inside compressed code; the values are not printed), no key-like strings
(`deploy/release_guard.py`), `argus.local.json` holds only voices; `argus.toml` has no
personal override of `[update] server`; the terms and the privacy policy have no
placeholders instead of the operator's name. Then comes a trial launch of the copy on its own
free port: it is this copy that answers, not the program the owner already has running.
The archive `Argus-win.zip` (one `Argus/` folder inside, which is how self-update unpacks it)
is uploaded as `.part`, the size and sha256 are compared, it is renamed, and only after that
`latest.json` is updated: the version in it must match the build's `VERSION`, otherwise
self-update would "update" to the same build. The previous `Argus-win.zip` and `latest.json`
stay as `Argus-win.prev.zip` and `latest.prev.json`; `--rollback` restores them under a new
release number (apps that have already installed the broken one would otherwise treat the
return as a rollback and ignore it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "deploy"))
from argus import update_trust  # noqa: E402 — signature of the release description: apps trust only it
import release_guard  # noqa: E402 — shared release checks (keys, placeholders in the terms)

SERVER = "root@203.0.113.10"
KEY = Path.home() / ".ssh" / "id_ed25519"
REMOTE_DIR = "/opt/argus-server/deploy/server/downloads"
PUBLIC = "http://203.0.113.10"
ZIP_NAME = "Argus-win.zip"
PREV_ZIP_NAME = "Argus-win.prev.zip"
PREV_MANIFEST_NAME = "latest.prev.json"

# What may sit at the top of the distributed folder. A whitelist, not a list of what is
# forbidden: any other file is either someone's personal one (for example, `argus.legal.json`
# could have shipped to everyone with "notice already accepted") or forgotten.
ALLOWED_TOP = ("Argus.exe", "_internal", "VERSION", "BUILD", "argus.toml", "argus.local.json", "models", "update-alive.v1")
PERSONAL = ("argus.account.json", "argus.log", "update.log", "update.json", "update-journal.json", "update-result.json",
            "update-alive", "update.lock", "sessions", "chats", "materials", "debug_prompt.txt")

# Text recognition from the screen pulls in rapidocr: if it is turned off in `argus.toml`, the
# build must not contain it.
OCR_DIRS = ("rapidocr",)


def secrets_from_env() -> list[bytes]:
    """The real values from the project's `.env` (what must not be in the distribution). They must
    never be printed.
    """
    return release_guard.secrets_from_env(REPO)


def _flavor_problems(app: Path, toml_text: str) -> str:
    """A build made without the two environment variables (`argus.spec`) carries the subscription
    path to the model and the local engines.
    """
    ocr_on = True
    try:
        ocr_on = bool(tomllib.loads(toml_text).get("ocr", {}).get("enabled", True))
    except tomllib.TOMLDecodeError:
        pass
    paths = [p.relative_to(app).as_posix() for p in app.rglob("*")]
    problem = release_guard.flavor_problem(paths)
    if problem:
        return problem
    if not ocr_on:
        for path in paths:
            if any(part.lower() in OCR_DIRS for part in path.split("/")):
                return f"in build {path}: screen reading is turned off in argus.toml (ocr.enabled), but the engine is in the build (no ARGUS_BUILD_API_ONLY=1)"
    return ""


def _legal_pages(app: Path) -> list[tuple[str, bytes]]:
    return [(p.relative_to(app).as_posix(), p.read_bytes()) for p in sorted(app.rglob("legal-*.html"))]


def verify(app: Path, allow_legal_placeholders: bool = False) -> str:
    if not (app / "Argus.exe").is_file():
        raise SystemExit(f"in {app} there is no Argus.exe")
    version = (app / "VERSION").read_text(encoding="utf-8").strip() if (app / "VERSION").is_file() else ""
    if not version:
        raise SystemExit("no VERSION: stage.py --public was not run")
    for p in app.rglob("*"):
        if p.is_file() and (p.name.lower() == ".env" or p.name.lower().endswith(".env")):
            raise SystemExit(f"the build contains {p.relative_to(app)} - it must not be distributed")
    for name in PERSONAL:
        if (app / name).exists():
            raise SystemExit(f"the build contains personal data: {name}")
    stray = sorted(p.name for p in app.iterdir() if p.name not in ALLOWED_TOP)
    if stray:
        raise SystemExit(f"the build has extra items at the top level (someone's personal files or forgotten leftovers): {', '.join(stray)}")
    local = json.loads((app / "argus.local.json").read_text(encoding="utf-8")) if (app / "argus.local.json").is_file() else {}
    if set(local) - {"channel"} or set(local.get("channel", {})) - {"tts"} or set(local.get("channel", {}).get("tts", {})) - {"characters"}:
        raise SystemExit(f"argus.local.json contains more than voices: {sorted(local)}")
    toml = (app / "argus.toml").read_text(encoding="utf-8")
    # Since 2026-10-15 the build updates over https from our domain (it serves `latest.json` and
    # the archives); the former `http://203.0.113.10:8080` stays alive only for builds that have
    # already been issued and is not baked into new ones.
    if "LOCAL DEV OVERRIDE" in toml or not re.search(r'(?m)^server = "https://arguspilot\.com"', toml):
        raise SystemExit("argus.toml has a personal override or no update address (expected https://arguspilot.com)")
    flavor = _flavor_problems(app, toml)
    if flavor:
        raise SystemExit(flavor)
    pages = _legal_pages(app)
    legal = release_guard.legal_problems(pages) if pages else ["the build has no terms and privacy pages (legal-*.html): run python legal/build.py"]
    if legal:
        if not allow_legal_placeholders:
            raise SystemExit(release_guard.legal_message(legal))
        print("WARNING: publishing with placeholders in the terms (--allow-legal-placeholders):\n  " + "\n  ".join(legal))
    scanned, problem = release_guard.scan_tree(app, secrets_from_env())
    if problem:
        raise SystemExit(problem)
    print(f"build check: {scanned} files, version {version}; no .env, personal data or values from .env")
    if not (app / "update-alive.v1").is_file():
        print("WARNING: the build has no update-alive.v1 (stage.py puts it there) - the update will not wait for it to start, "
              "and will not roll back a build that crashes on launch")
    return version


def free_port() -> int:
    """A free port for the trial run: the owner's app is usually already running on the port from
    `argus.toml` (8420).
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _inside(path: str, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def smoke(app: Path, timeout: float = 90.0) -> None:
    """Trial launch of the copy: the program starts its own server on its own port, and it is that
    program that answers.

    Previously the copy took the port from `argus.toml`, the same 8420 as the owner's running
    program. The single-instance lock is named after the port, so the copy exited at once, and
    the 200 answer came from someone else's program: a broken build passed the only gate and went
    out to everyone. Now there is a free port (a separate lock), the `/build` answer must name a
    folder inside the copy, and the process must still be alive after the answer.
    """
    with tempfile.TemporaryDirectory(prefix="argus-smoke-", dir=app.parent, ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "Argus"
        shutil.copytree(app, copy)
        port = free_port()
        # The built program does not read `ui.window` from this file (only `argus run` reads it); the
        # window will appear, as it does for regular users.
        (copy / "argus.local.json").write_text(json.dumps({"ui": {"port": port}, "update": {"server": ""}}), encoding="utf-8")
        env = dict(os.environ, ARGUS_NO_LIVE="1")          # the pages are its own, not from the sources next to it
        proc = subprocess.Popen([str(copy / "Argus.exe")], cwd=copy, env=env, creationflags=0x08000000)   # CREATE_NO_WINDOW
        try:
            deadline = time.time() + timeout
            info = None
            while time.time() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/build", timeout=3) as r:
                        info = json.loads(r.read())
                        break
                except Exception:
                    time.sleep(1.5)
            if info is None:
                log = (copy / "argus.log").read_text(encoding="utf-8", errors="replace")[-1500:] if (copy / "argus.log").is_file() else ""
                raise SystemExit(f"trial launch: the program server did not respond (code {proc.poll()}).\n{log}")
            if not isinstance(info, dict) or not _inside(str(info.get("root", "")), copy):
                raise SystemExit(f"trial launch: port {port} is answered by something other than a copy of the program ({info.get('root') if isinstance(info, dict) else info!r})")
            time.sleep(2)
            if proc.poll() is not None:
                raise SystemExit(f"trial launch: the program responded and immediately exited (code {proc.poll()})")
            print(f"trial launch: this exact copy responds (port {port})")
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            time.sleep(1)


def make_zip(app: Path, out: Path) -> None:
    started = time.time()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, f"Argus/{p.relative_to(app).as_posix()}")
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None or "Argus/Argus.exe" not in z.namelist():
            raise SystemExit("archive failed validation")
    print(f"archive: {out.stat().st_size / 1e6:.0f} MB in {time.time() - started:.0f} s")


def ssh(command: str, timeout: int = 600) -> str:
    done = subprocess.run(["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", SERVER, command],
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise SystemExit(f"failed on the server: {command}\n{done.stderr.strip()}")
    return done.stdout.strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_live() -> tuple[dict | None, float | None]:
    """The release description published right now (None if there is no release yet) and the server
    time from the `Date` header.
    """
    return release_guard.fetch_live(f"{PUBLIC}/latest.json")


def next_build(live: dict | None, now: float | None = None, server_now: float | None = None) -> int:
    """Build number: at least the published one plus one, and the local clock does not disagree with
    the server's (`release_guard.next_build`).
    """
    return release_guard.next_build(live, now, server_now)


def sign_release(info: dict) -> str:
    """Sign the description with the release key and verify it the way the app will. Returns the
    finished JSON.
    """
    signed = update_trust.sign_manifest(info, "win", update_trust.load_signing_seed())
    problem = update_trust.problem_with_manifest(signed, "win")
    if problem:
        raise SystemExit(f"release is signed, but the app will not accept it: {problem} (do the release key and the embedded public key not match?)")
    return json.dumps(signed)


def publish_commands(manifest: str, keep_prev: bool, from_prev: bool = False) -> str:
    """Command for the server: replace the archive and the description, keeping the previous ones as
    `.prev` (if `keep_prev`).
    """
    keep = (f"(test ! -f {ZIP_NAME} || cp -p {ZIP_NAME} {PREV_ZIP_NAME}) && "
            f"(test ! -f latest.json || cp -p latest.json {PREV_MANIFEST_NAME}) && ") if keep_prev else ""
    source = f"cp -p {PREV_ZIP_NAME} {ZIP_NAME}.part && " if from_prev else ""
    return (f"cd {REMOTE_DIR} && {source}chmod 644 {ZIP_NAME}.part && {keep}mv -f {ZIP_NAME}.part {ZIP_NAME} && "
            f"printf '%s' '{manifest}' > latest.json.part && chmod 644 latest.json.part && mv -f latest.json.part latest.json")


def upload(zip_path: Path, version: str, build: int, live: dict | None = None) -> None:
    part = f"{REMOTE_DIR}/{ZIP_NAME}.part"
    done = subprocess.run(["scp", "-q", "-i", str(KEY), "-o", "BatchMode=yes", str(zip_path), f"{SERVER}:{part}"],
                          capture_output=True, text=True, timeout=3600)
    if done.returncode:
        raise SystemExit(f"upload failed: {done.stderr.strip()}")
    if int(ssh(f"stat -c %s {part}")) != zip_path.stat().st_size:
        raise SystemExit("size on the server does not match")
    if ssh(f"sha256sum {part} | cut -d' ' -f1") != sha256(zip_path):
        raise SystemExit("sha256 on the server does not match")
    # The description is signed with the release key and carries the hash, size and release number
    # of the archive: the app takes an update only if the signature checks out and the downloaded
    # file matches the hash (`update.py`, `update_trust.py`).
    info = {"version": version, "zip": ZIP_NAME, "sha256": sha256(zip_path), "size": zip_path.stat().st_size, "build": build}
    manifest = sign_release(info)
    # The previous release is not erased: a broken one that passed the checks would otherwise be
    # the only one left (`--rollback`). Publishing the same version again does not make it the
    # previous one; otherwise the real previous one would be overwritten.
    keep_prev = bool(live) and live.get("version") != version
    ssh(publish_commands(manifest, keep_prev))
    print(f"uploaded: {ZIP_NAME} and latest.json (version {version}, release {build})")


def rollback() -> None:
    """Restore the previous release: its archive and description are saved as `.prev`, and the
    description is signed again.

    The release number is new (greater than the published one): apps that have already installed
    the broken one would take a return under the old number for a rollback and ignore it. The
    rollback protection works against ourselves too.
    """
    live, server_now = fetch_live()
    try:
        prev = json.loads(ssh(f"cat {REMOTE_DIR}/{PREV_MANIFEST_NAME}"))
    except ValueError:
        raise SystemExit("the previous release description on the server is corrupted") from None
    digest = ssh(f"sha256sum {REMOTE_DIR}/{PREV_ZIP_NAME} | cut -d' ' -f1")
    size = int(ssh(f"stat -c %s {REMOTE_DIR}/{PREV_ZIP_NAME}"))
    if not isinstance(prev, dict) or digest != prev.get("sha256") or size != prev.get("size"):
        raise SystemExit("the previous archive on the server does not match its description - nothing to roll back with")
    build = next_build(live, server_now=server_now)
    manifest = sign_release({"version": prev["version"], "zip": ZIP_NAME, "sha256": digest, "size": size, "build": build})
    ssh(publish_commands(manifest, keep_prev=False, from_prev=True))
    print(f"restored release {prev['version']} under number {build}")
    check_public(prev["version"], build)


def check_public(version: str, build: int | None = None) -> None:
    win_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    for path in ("download", "download/windows"):
        try:
            opener.open(urllib.request.Request(f"{PUBLIC}/{path}", headers={"User-Agent": win_ua}), timeout=30)
        except urllib.error.HTTPError as e:
            print(f"  /{path} (Windows): {e.code} → {e.headers.get('Location')}")
    with urllib.request.urlopen(urllib.request.Request(f"{PUBLIC}/{ZIP_NAME}", method="HEAD"), timeout=30) as r:
        print(f"  /{ZIP_NAME}: {r.status} {r.headers.get('Content-Length')} bytes, {r.headers.get('Content-Disposition')}")
    with urllib.request.urlopen(f"{PUBLIC}/latest.json", timeout=30) as r:
        info = json.loads(r.read())
    if info.get("version") != version:
        raise SystemExit(f"latest.json says {info}, but the build is {version}")
    if build is not None and info.get("build") != build:
        raise SystemExit(f"latest.json carries release {info.get('build')}, but the one published was {build}")
    problem = update_trust.problem_with_manifest(info, "win")
    if problem:
        raise SystemExit(f"the published release description will not be accepted by the app: {problem}")
    print("  latest.json: release signature verifies")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", type=Path, help="folder of the built program (with Argus.exe), after stage.py --public")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--rollback", action="store_true", help="roll back to the previous release (re-signing it)")
    ap.add_argument("--allow-legal-placeholders", action="store_true",
                    help="publish even though the terms and policy contain placeholders instead of operator data")
    args = ap.parse_args()
    if args.rollback:
        rollback()
        return 0
    if args.app is None:
        ap.error("--app (or --rollback) is required")
    app = args.app.resolve()
    version = verify(app, args.allow_legal_placeholders)
    if not args.no_smoke:
        smoke(app)
    live, server_now = (None, None) if args.check else fetch_live()
    build = next_build(live, server_now=server_now)
    # The release number goes both in the signed description and in the build itself (`BUILD`):
    # rollback protection is not empty on a fresh install.
    (app / "BUILD").write_text(str(build), encoding="utf-8")
    zip_path = app.parent / ZIP_NAME
    make_zip(app, zip_path)
    if args.check:
        print("--check: not uploading to the server")
        return 0
    upload(zip_path, version, build, live)
    check_public(version, build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
