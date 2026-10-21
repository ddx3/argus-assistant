"""Publish the Mac build to the download server (nginx, 203.0.113.10) with one command.

    python deploy/mac/publish_mac.py               # the latest successful macos-port build
    python deploy/mac/publish_mac.py --run 123     # the build of a specific run
    python deploy/mac/publish_mac.py --check       # only download and verify, do not upload
    python deploy/mac/publish_mac.py --dir dist-mac   # no CI: use the build made on this Mac
                                                      # (`packaging/build_macos.sh`)
    python deploy/mac/publish_mac.py --yes         # no confirmation prompt (terminal without input)
    python deploy/mac/publish_mac.py --allow-legal-placeholders   # deliberate: placeholder terms

What happens. The `Argus-macOS` artifact is taken from GitHub Actions (the login is the one
used by `git push`: Git Credential Manager; the token is never printed and goes only to
api.github.com, not to the download address, which is a signed storage link). Only a run
triggered by a push to the `macos-port` branch that finished successfully is taken, and only
if its commit is exactly the one currently in `origin/macos-port`; the commit and the author
are printed, and signing with the release key (which all Macs trust) is impossible without
confirmation. The archive is checked: no `.env`, no key-like strings (including inside binary
files and compressed code), no placeholders in the terms and the privacy policy, and
`VERSION` inside the package matches the version in `latest-mac.json`. The files go to the
server under temporary names, the size and sha256 are compared, and only then are they
renamed.

There is no download page any more (2026-10-13): the landing buttons lead to `/download`, and
nginx itself serves `Argus-mac.dmg` to a visitor on a Mac (`deploy/landing/`), so after the
upload the link works on its own and nothing else needs to be published. The image is not
signed by Apple; what to do on first launch is written on the landing's "Help" page.

Speed: from the server to this computer the file goes at about 90 KB/s per connection (on all
ports; BBR did not help: the limit is per stream, not packet loss), so a 100 MB image
downloads in about 19 minutes. This is a property of the path to the server, not of the
build; if it gets in the way, use a mirror on a hosting with a CDN.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

LOCAL_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL_REPO / "src"))
sys.path.insert(0, str(LOCAL_REPO / "deploy"))
from argus import update_trust  # noqa: E402 — signature of the release description: apps trust only it
import release_guard  # noqa: E402 — shared release checks (keys, placeholders in the terms, release number)

REPO = "OWNER/argus"
BRANCH = "macos-port"
ARTIFACT = "Argus-macOS"
VERIFIED = "Argus-macOS-verified"      # marker: the trial launch of the package in CI passed (`.github/workflows/macos.yml`)
SERVER = "root@203.0.113.10"
KEY = Path.home() / ".ssh" / "id_ed25519"
REMOTE_DIR = "/opt/argus-server/deploy/server/downloads"
PUBLIC = "http://203.0.113.10"
FILES = ("Argus-mac.dmg", "Argus-mac.zip", "latest-mac.json")

# Version inside the package: the seeds (`packaging/build_mac_seed.py`) put it in
# `Resources/seed/VERSION`, and the program uses it to decide that it already has the latest
# release. If it differs from the version in the release description, the app "updates" on
# every launch.
SEED_VERSION = "Argus.app/Contents/Resources/seed/VERSION"
PERSONAL_NAMES = (".env", "argus.account.json", "argus.legal.json")


# -- GitHub ----------------------------------------------------------------------------------------


def token() -> str:
    env = dict(os.environ, GCM_INTERACTIVE="never", GIT_TERMINAL_PROMPT="0")
    out = subprocess.run(
        ["git", "credential", "fill"], input=f"protocol=https\nhost=github.com\npath={REPO}.git\n\n",
        capture_output=True, text=True, env=env, timeout=30,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no saved GitHub login")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def api(path: str):
    request = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "argus-publish",
    })
    try:
        with _OPENER.open(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location")
        raise SystemExit(f"GitHub responded {e.code} for {path}: {e.read().decode('utf-8', 'replace')[:300]}")


def run_problem(run: dict) -> str:
    """Whether this run's artifact can be used. Empty means yes. What is signed with the release key
    is trusted by every Mac.
    """
    if run.get("head_branch") != BRANCH:
        return f"run is not from branch {BRANCH} (but from {run.get('head_branch')!r})"
    if run.get("event") != "push":
        return f"run was not triggered by a push (event {run.get('event')!r})"
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return "run did not finish successfully"
    return ""


def has_artifacts(run: dict) -> tuple[bool, bool]:
    """(build exists, "verified" mark exists). The CI step publishes the mark only after a successful
    trial run of the package.
    """
    arts = api(f"/repos/{REPO}/actions/runs/{run['id']}/artifacts")["artifacts"]
    live = {a["name"] for a in arts if not a["expired"]}
    return ARTIFACT in live, VERIFIED in live


def pick_run(run_id: str | None) -> dict:
    if run_id:
        run = api(f"/repos/{REPO}/actions/runs/{run_id}")
        problem = run_problem(run)
        if problem:
            raise SystemExit(f"run {run_id} cannot be used: {problem}")
        built, verified = has_artifacts(run)
        if not built or not verified:
            raise SystemExit(f"run {run_id} has no {'build' if not built else 'verified marker (the trial run of the package in CI failed)'}")
        return run
    runs = api(f"/repos/{REPO}/actions/runs?branch={BRANCH}&event=push&status=success&per_page=10")["workflow_runs"]
    for run in runs:
        if run_problem(run):
            continue
        built, verified = has_artifacts(run)
        if built and verified:
            return run
    raise SystemExit("no successful verified build (retention period expired, the trial run of the package in CI failed, or there have been no builds yet)")


def local_head() -> str:
    """Which commit is currently in `origin/macos-port` according to local data (run `git fetch`
    first).
    """
    done = subprocess.run(["git", "-C", str(LOCAL_REPO), "rev-parse", f"origin/{BRANCH}"],
                          capture_output=True, text=True, timeout=30)
    sha = done.stdout.strip()
    if done.returncode or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"could not determine which commit is currently on origin/{BRANCH} (was git fetch not run?)")
    return sha


def confirm_run(run: dict, expected_sha: str, assume_yes: bool, interactive: bool | None = None, ask=input) -> None:
    """Show exactly what will be signed and make sure it is a build of our commit, not of someone
    else's push.

    The artifact that goes under the release signature was built on GitHub from what is pushed: a
    push by anyone with write access to the repository, or a stolen token, would produce a build
    with foreign code that all Macs would trust.
    """
    sha = str(run.get("head_sha", ""))
    actor = (run.get("actor") or {}).get("login", "?")
    lines = str((run.get("head_commit") or {}).get("message") or "").splitlines()
    print(f"build from run {run.get('id')}: commit {sha[:12]} '{lines[0] if lines else ''}', started by {actor}, "
          f"event {run.get('event')}")
    if sha != expected_sha:
        raise SystemExit(f"build commit {sha[:12]} is not the one currently on origin/{BRANCH} ({expected_sha[:12]}): "
                         "signing a foreign or outdated build is not allowed (if the push is fresh, run `git fetch` first)")
    if assume_yes:
        return
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit("the release can be signed only after confirmation: run in a terminal or with --yes")
    if ask("Sign this release with the release key and publish it? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        raise SystemExit("cancelled")


def download_artifact(run: dict, into: Path) -> None:
    arts = api(f"/repos/{REPO}/actions/runs/{run['id']}/artifacts")["artifacts"]
    art = next((a for a in arts if a["name"] == ARTIFACT and not a["expired"]), None)
    if art is None:
        raise SystemExit("the run has no build artifact")
    location = api(f"/repos/{REPO}/actions/artifacts/{art['id']}/zip")
    if not isinstance(location, str):
        raise SystemExit("GitHub did not provide a download URL")
    print(f"downloading {art['name']} ({art['size_in_bytes'] / 1e6:.0f} MB) from run {run['id']} ({run['head_sha'][:7]})")
    archive = into / "artifact.zip"
    with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": "argus-publish"}), timeout=600) as r:
        archive.write_bytes(r.read())
    with zipfile.ZipFile(archive) as z:
        z.extractall(into)


# -- check -----------------------------------------------------------------------------


def verify(folder: Path, secrets: list[bytes] | None = None, allow_legal_placeholders: bool = False) -> None:
    """Check the build before signing and uploading. `secrets` are the real values from the
    project's `.env` (read by default).
    """
    for name in FILES:
        if not (folder / name).is_file() or (folder / name).stat().st_size == 0:
            raise SystemExit(f"the build has no {name}")
    dmg = folder / "Argus-mac.dmg"
    with dmg.open("rb") as f:
        f.seek(-512, os.SEEK_END)
        if f.read(4) != b"koly":
            raise SystemExit("Argus-mac.dmg does not look like a disk image (no marker at the end of the file)")
    if secrets is None:
        secrets = release_guard.secrets_from_env(LOCAL_REPO)
    try:
        built = json.loads((folder / "latest-mac.json").read_text(encoding="utf-8"))
    except ValueError:
        raise SystemExit("latest-mac.json cannot be parsed") from None
    with zipfile.ZipFile(folder / "Argus-mac.zip") as z:
        names = z.namelist()
        if "Argus.app/Contents/MacOS/Argus" not in names:
            raise SystemExit("no application in Argus-mac.zip")
        for n in names:
            last = n.rsplit("/", 1)[-1]
            if last in PERSONAL_NAMES or last.endswith(".env"):
                raise SystemExit(f"the build contains {n} - it must not be distributed")
        flavor = release_guard.flavor_problem(names)
        if flavor:
            raise SystemExit(flavor)
        # The version in the description and in the package must be the same. The build computed it
        # twice (in the build script and in the seeds), and when the working folder was edited the
        # values drifted apart: the app could not find its version in the description and kept updating
        # forever.
        if SEED_VERSION not in names:
            raise SystemExit(f"the package has no {SEED_VERSION}: the seed files were built without VERSION")
        inside = z.read(SEED_VERSION).decode("utf-8", errors="replace").strip()
        if inside != str(built.get("version", "")).strip():
            raise SystemExit(f"VERSION in the package is '{inside}', but latest-mac.json says '{built.get('version')}': "
                             "the app would keep 'updating' on every launch")
        pages = [(n, z.read(n)) for n in names if n.rsplit("/", 1)[-1].startswith("legal-") and n.endswith(".html")]
        legal = release_guard.legal_problems(pages) if pages else ["the package has no terms and privacy pages (legal-*.html)"]
        if legal:
            if not allow_legal_placeholders:
                raise SystemExit(release_guard.legal_message(legal))
            print("WARNING: publishing with placeholders in the terms (--allow-legal-placeholders):\n  " + "\n  ".join(legal))
        for info in z.infolist():
            if info.is_dir():
                continue
            with z.open(info) as reader:
                problem = release_guard.scan_reader(info.filename, reader, info.file_size, secrets)
            if problem:
                raise SystemExit(problem)
    print("build check: image is intact, package version matches the description, no .env or keys")


# -- server -----------------------------------------------------------------------------


def sign_release(folder: Path, live: dict | None = None, server_now: float | None = None) -> dict:
    """Sign the release description: `latest-mac.json` from the build carries only the version, and
    the app takes an update only when the description is signed with the release key
    (`~/.argus/update-signing.key`) and carries the hash, size and build number of the archive.
    The result is checked the same way the app will check it
    (`update_trust.problem_with_manifest`).

    The build number is at least the published one plus one, and the local clock does not
    disagree with the server's (`release_guard.next_build`)."""
    built = json.loads((folder / "latest-mac.json").read_text(encoding="utf-8"))
    archive = folder / "Argus-mac.zip"
    info = {
        "version": str(built["version"]), "zip": archive.name, "sha256": sha256(archive),
        "size": archive.stat().st_size, "build": release_guard.next_build(live, server_now=server_now),
    }
    signed = update_trust.sign_manifest(info, "mac", update_trust.load_signing_seed())
    problem = update_trust.problem_with_manifest(signed, "mac")
    if problem:
        raise SystemExit(f"release is signed, but the app will not accept it: {problem} (do the release key and the embedded public key not match?)")
    (folder / "latest-mac.json").write_text(json.dumps(signed), encoding="utf-8")
    print(f"release description signed: version {signed['version']}, release {signed['build']}")
    return signed


def ssh(command: str) -> str:
    done = subprocess.run(
        ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SERVER, command],
        capture_output=True, text=True, timeout=600,
    )
    if done.returncode:
        raise SystemExit(f"failed on the server: {command}\n{done.stderr.strip()}")
    return done.stdout.strip()


def scp(source: Path, target: str) -> None:
    done = subprocess.run(
        ["scp", "-q", "-i", str(KEY), "-o", "BatchMode=yes", str(source), f"{SERVER}:{target}"],
        capture_output=True, text=True, timeout=1800,
    )
    if done.returncode:
        raise SystemExit(f"upload failed for {source.name}: {done.stderr.strip()}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def deploy(folder: Path) -> None:
    for name in FILES:
        local = folder / name
        scp(local, f"{REMOTE_DIR}/{name}.part")
        remote_size = int(ssh(f"stat -c %s {REMOTE_DIR}/{name}.part"))
        if remote_size != local.stat().st_size:
            raise SystemExit(f"size of {name} on the server is {remote_size}, but should be {local.stat().st_size}")
        if name.endswith(".dmg"):
            remote_hash = ssh(f"sha256sum {REMOTE_DIR}/{name}.part | cut -d' ' -f1")
            if remote_hash != sha256(local):
                raise SystemExit(f"sha256 of {name} on the server does not match")
        print(f"uploaded: {name} ({remote_size} bytes)")
    ssh(f"cd {REMOTE_DIR} && " + " && ".join(f"mv -f {n}.part {n}" for n in FILES)
        + " && chmod 644 " + " ".join(FILES))


def check_public() -> None:
    """The files are served, and the "Download" button on Mac leads precisely to the disk
    image.
    """
    for path in ("Argus-mac.dmg", "latest-mac.json"):
        request = urllib.request.Request(f"{PUBLIC}/{path}", method="HEAD")
        with urllib.request.urlopen(request, timeout=30) as r:
            print(f"  /{path}: {r.status} {r.headers.get('Content-Type')} {r.headers.get('Content-Length')} bytes")
    with urllib.request.urlopen(f"{PUBLIC}/latest-mac.json", timeout=30) as r:
        published = json.loads(r.read())
    problem = update_trust.problem_with_manifest(published, "mac")
    if problem:
        raise SystemExit(f"the published release description will not be accepted by the app: {problem}")
    print("  latest-mac.json: release signature verifies")
    mac_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    for path in ("download", "download/mac"):
        request = urllib.request.Request(f"{PUBLIC}/{path}", headers={"User-Agent": mac_ua})
        try:
            with _OPENER.open(request, timeout=30) as r:
                raise SystemExit(f"/{path} does not redirect: {r.status}")
        except urllib.error.HTTPError as e:
            print(f"  /{path} (Mac): {e.code} → {e.headers.get('Location')}")


def main() -> int:
    args = sys.argv[1:]
    run_id = args[args.index("--run") + 1] if "--run" in args else None
    local = Path(args[args.index("--dir") + 1]) if "--dir" in args else None
    allow_legal = "--allow-legal-placeholders" in args
    with tempfile.TemporaryDirectory(prefix="argus-mac-") as tmp:
        folder = Path(tmp)
        if local is not None:
            # A build from this same computer, without GitHub Actions: a copy of the needed files (the
            # release description is signed in the copy; the source folder is not changed).
            for name in FILES:
                source = local / name
                if not source.is_file():
                    raise SystemExit(f"in {local} there is no {name}: run `bash packaging/build_macos.sh` first")
                shutil.copy2(source, folder / name)
        else:
            run = pick_run(run_id)
            expected = local_head()
            if "--check" in args:
                confirm_run(run, expected, assume_yes=True)      # no signature goes to the server: it is enough that the commit is the right one
            else:
                confirm_run(run, expected, assume_yes="--yes" in args)
            download_artifact(run, folder)
        verify(folder, allow_legal_placeholders=allow_legal)
        live, server_now = (None, None) if "--check" in args else release_guard.fetch_live(f"{PUBLIC}/latest-mac.json")
        sign_release(folder, live, server_now)
        if "--check" in args:
            print("--check: not uploading to the server")
            return 0
        deploy(folder)
    check_public()
    return 0


if __name__ == "__main__":
    sys.exit(main())
