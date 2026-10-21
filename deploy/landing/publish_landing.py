"""Publish the landing page to our server (nginx, 203.0.113.10, ports 80 and 8080) with one
command.

    python deploy/landing/publish_landing.py              # build the landing page, publish, verify
    python deploy/landing/publish_landing.py --no-build   # use the ready landing/out, no rebuild
    python deploy/landing/publish_landing.py --check      # build and verify, do not upload
    python deploy/landing/publish_landing.py --rollback   # restore the previous release
    python deploy/landing/publish_landing.py --setup      # once, new server: nginx, compose, backup
    python deploy/landing/publish_landing.py --setup --dry-run   # same, but only show the changes
    ... --base http://127.0.0.1:18080                     # check another address (e.g. via ssh -L)

Since 2026-10-13 the landing page is served from here instead of being a hosted artifact,
and there is no separate download page. What happens:

1. `landing/build_landing.py --out <temp folder>` builds the pages and frames (it does not
   write to anyone else's `landing/out`).
2. The main page in the build is a piece of markup without `<html>`: the shell around it used
   to be added by the artifact host. Here the shell is the same (`WRAP_HEAD`), so the page
   looks the same as before; the other pages are already complete.
3. Release check: the required files and frames are present in all languages, there are no
   links to the old download page or to the artifact host, and the "Download"
   buttons lead to `/download…` and do not open a new tab.
4. The whole release goes to `sites/landing-<date-time>` (tar over ssh, sha256 comparison
   over the file list), and only then is the `sites/current` link swapped with a single
   rename: a visitor sees either the old release or the new one in full. Old releases (the
   last five) are kept: `--rollback` restores the previous one.
5. Check of the deployed site over HTTP: the pages match the local ones byte for byte,
   compression is on, `/download` picks the file by system, the build is served as an
   attachment, port 8080 answers the same.

The builds themselves (`Argus-mac.dmg`, `Argus-win.zip`, `latest*.json`) live separately in
`downloads/` and are not part of this: they are published by `deploy/mac/publish_mac.py` and
by hand. The "Download" link works as soon as the needed file appears there.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import gzip
import hashlib
import http.client
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LANDING = REPO / "landing"
ICON = REPO / "packaging" / "argus.ico"

sys.path.insert(0, str(REPO / "deploy"))
import release_guard  # noqa: E402 — shared release checks (placeholders in the terms and help)

# Pages that must not contain placeholders instead of the operator's name and mail address
# (`legal/build.py`): terms, privacy policy, help.
LEGAL_PAGES = ("terms.html", "privacy.html", "help.html")

HOST = "203.0.113.10"
SERVER = f"root@{HOST}"
KEY = Path.home() / ".ssh" / "id_ed25519"
REMOTE = "/opt/argus-server/deploy/server"
SITES = f"{REMOTE}/sites"
NGINX_DIR = f"{REMOTE}/nginx"
PUBLIC = f"http://{HOST}"
KEEP = 5

LANGS = ["ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl"]
PAGES = ("index.html", "help.html", "pricing.html", "terms.html", "privacy.html")
# The seller details page is built only while the personal file is on the build machine
# (`landing/subpages.py`, SELLER_FILE).
OPTIONAL_PAGES = ("seller.html",)


def pages(folder: Path) -> tuple[str, ...]:
    return PAGES + tuple(n for n in OPTIONAL_PAGES if (folder / n).is_file())
RELEASE_NAME = re.compile(r"^landing-\d{8}-\d{6}$")
ROOT_LINKS = {"/", "/download", "/download/windows", "/download/mac", "/favicon.ico"}   # the only root-relative links the pages are allowed to have
MANIFEST = ".manifest.sha256"   # leading dot: nginx does not serve such files

DESCRIPTION = "Hears the conversation, sees your screen, and suggests an answer right away."

# The shell of the main page: exactly what the artifact host added when it published the
# artifact (the `body`, `img` and `[hidden]` styles; the page depends on them), plus the
# document language and a description for link previews in messengers.
WRAP_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset=utf8>'
    '<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
    f'<meta name="description" content="{DESCRIPTION}">'
    '<meta name="theme-color" content="#0a0c10">'
    f'<meta property="og:type" content="website"><meta property="og:title" content="Argus"><meta property="og:description" content="{DESCRIPTION}">'
    '<style>:root{color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
    'html{scroll-padding-top:env(safe-area-inset-top,0px)}'
    'body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#000}'
    'img{max-width:100%}[hidden]:not([hidden=until-found i]){display:none!important}</style></head><body>'
)
WRAP_TAIL = "\n</body></html>"

MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1"

# The serving service in docker-compose on the server (we do not touch the rest of the file).
COMPOSE_BLOCK = '''\
  # The Argus site and the distribution of builds: the landing page (sites/current), the "Download" (/download) buttons, and the files themselves (downloads).
  # One nginx on ports 80 and 8080 (8080 is the former address, baked into the builds already issued for self-update).
  # Settings are in nginx/ (in the repository, deploy/landing/nginx); the pages are published by deploy/landing/publish_landing.py.
  downloads:
    image: nginx:alpine
    container_name: argus-downloads
    restart: unless-stopped
    ports:
      - "80:80"
      - "8080:80"
      - "443:443"
      - "8421:8421"        # the former API address for the builds already issued (nginx/default.conf, the last block)
    volumes:
      - ./sites:/usr/share/nginx/sites:ro
      - ./downloads:/usr/share/nginx/downloads:ro
      - ./nginx:/etc/nginx/conf.d:ro
      - ./acme:/usr/share/nginx/acme:ro
      - /opt/argus-server/certs:/etc/letsencrypt:ro
    # Request logs store IP addresses, and the privacy policy promises a limited
    # retention time (`legal/text/privacy.html`, section 5): without this, docker
    # keeps them without limit. Three files of 10 MB each; the old ones erase themselves.
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
'''


# -- ssh ----------------------------------------------------------------------------------------------------


def ssh(command: str, timeout: int = 600, check: bool = True) -> str:
    done = subprocess.run(
        ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", SERVER, command],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    if check and done.returncode:
        raise SystemExit(f"failed on the server: {command}\n{done.stderr.strip()}")
    return done.stdout.strip()


def scp(source: Path, target: str) -> None:
    done = subprocess.run(
        ["scp", "-q", "-i", str(KEY), "-o", "BatchMode=yes", str(source), f"{SERVER}:{target}"],
        capture_output=True, text=True, timeout=600,
    )
    if done.returncode:
        raise SystemExit(f"upload failed for {source.name}: {done.stderr.strip()}")


# -- build ------------------------------------------------------------------------------


def landing_python() -> str:
    """The build needs playwright (pulled in by `screens.py`), and the system Python does not have
    it: the environment lives outside the repository.
    """
    venv = REPO.parent / ".landing-venv" / "Scripts" / "python.exe"
    return os.environ.get("ARGUS_LANDING_PYTHON") or (str(venv) if venv.is_file() else sys.executable)


def build(into: Path) -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    done = subprocess.run([landing_python(), "build_landing.py", "--out", str(into)], cwd=LANDING, env=env,
                          capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise SystemExit(f"landing build failed:\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}")
    print(done.stdout.strip().splitlines()[-2] if done.stdout.strip() else "built")


def assemble(built: Path, release: Path) -> None:
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    fragment = (built / "index.html").read_text(encoding="utf-8")
    # In the main-page fragment the title and the tab icon come AFTER the shell's `</head>`, that
    # is, in the page body; the browser does not always take the icon from there (the tab had no
    # logo on 2026-10-16). We move them into `<head>` and also add a plain `favicon.ico` to the
    # shell.
    head_bits = ""
    for pattern in (r"<title>.*?</title>\s*", r'<link rel="icon"[^>]*>\s*'):
        found = re.search(pattern, fragment, re.S)
        if found:
            head_bits += found.group(0).strip()
            fragment = fragment[:found.start()] + fragment[found.end():]
    head_bits += '<link rel="shortcut icon" href="/favicon.ico">'
    wrap_head = WRAP_HEAD.replace("<style>", head_bits + "<style>", 1)
    (release / "index.html").write_bytes(wrap_head.encode("utf-8") + b"\n" + fragment.encode("utf-8") + WRAP_TAIL.encode("utf-8"))
    for name in pages(built)[1:]:
        shutil.copy2(built / name, release / name)
    shutil.copytree(built / "img", release / "img")
    for name in ("404.html", "robots.txt"):
        shutil.copy2(HERE / "site" / name, release / name)
    shutil.copy2(ICON, release / "favicon.ico")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(release: Path) -> int:
    lines = []
    for p in sorted(release.rglob("*")):
        if p.is_file() and p.name != MANIFEST:
            lines.append(f"{sha256_file(p)}  {p.relative_to(release).as_posix()}")
    (release / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return len(lines)


def check_release(release: Path, allow_legal_placeholders: bool = False) -> None:
    problems: list[str] = []
    # `<mark class="todo">` marks and `mailto:TODO-email` instead of the operator's name and mail
    # address: the public page would have no working address for refunds and data requests.
    legal = release_guard.legal_problems((n, (release / n).read_bytes()) for n in LEGAL_PAGES if (release / n).is_file())
    if legal:
        if allow_legal_placeholders:
            print("WARNING: publishing with placeholders (--allow-legal-placeholders):\n  " + "\n  ".join(legal))
        else:
            problems.append(release_guard.legal_message(legal))
    for name in (*pages(release), "404.html", "robots.txt", "favicon.ico"):
        if not (release / name).is_file():
            problems.append(f"missing file {name}")
    index_text = (release / "index.html").read_text(encoding="utf-8") if (release / "index.html").is_file() else ""
    frames = sorted(set(re.findall(r'data-img="([a-z0-9_]+)"', index_text)))
    if len(frames) < 10:
        problems.append(f"frames found on the main page: {len(frames)}")
    for lang in LANGS:
        for frame in frames:
            ext = "jpg" if frame == "poster_dash" else "png"   # same as EXT in page.js
            if not (release / "img" / lang / f"{frame}.{ext}").is_file():
                problems.append(f"missing frame img/{lang}/{frame}.{ext}")
    for name in pages(release):
        path = release / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for bad in ("203.0.113.10:8080", "claude.ai", "window.claude", "artifact"):
            if bad in text:
                problems.append(f"{name}: left over '{bad}'")
        for tag in re.findall(r'<a [^>]*href="/download[^"]*"[^>]*>', text):
            if "target=" in tag or "download=" in tag:
                problems.append(f"{name}: download button with target/download: {tag[:90]}")
        for href in set(re.findall(r'href="(/[^"#?]*)"', text)):
            if href not in ROOT_LINKS:
                problems.append(f"{name}: root-relative link that must not be there: {href}")
        for href in set(re.findall(r'href="([A-Za-z0-9_-]+\.html)(?:#[^"]*)?"', text)):
            if not (release / href).is_file():
                problems.append(f"{name}: link to a nonexistent {href}")
    index = (release / "index.html").read_text(encoding="utf-8") if (release / "index.html").is_file() else ""
    if not index.startswith("<!doctype html>") or not index.rstrip().endswith("</body></html>"):
        problems.append("index.html: the document shell is out of place")
    for needle in ('id="dl-note"', 'href="/download"', 'href="/download/windows"', 'href="/download/mac"', '"dl.soon"', '"dl.other"'):
        if needle not in index:
            problems.append(f"index.html: missing {needle}")
    helptext = (release / "help.html").read_text(encoding="utf-8") if (release / "help.html").is_file() else ""
    if 'data-i18n="faq.13.q"' not in helptext:
        problems.append("help.html: no question about Mac (faq.13)")
    if len(index) < 1_000_000:
        problems.append(f"index.html is suspiciously small: {len(index)} characters")
    if problems:
        raise SystemExit("release failed validation:\n  " + "\n  ".join(problems))
    files = [p for p in release.rglob("*") if p.is_file()]
    print(f"release validated: {len(files)} files, {sum(p.stat().st_size for p in files) / 1e6:.1f} MB")


# -- publishing --------------------------------------------------------------------------


def releases() -> list[str]:
    names = ssh(f"ls -1 {SITES}").split()
    return sorted(n for n in names if RELEASE_NAME.match(n))


def current_release() -> str:
    return ssh(f"readlink {SITES}/current || true").strip().rsplit("/", 1)[-1]


def switch_to(name: str) -> None:
    if not RELEASE_NAME.match(name) and name != "landing-test":
        raise SystemExit(f"unexpected release name: {name}")
    ssh(f"cd {SITES} && test -d {name} && ln -sfn {name} .current.new && mv -T .current.new current")


def upload(release: Path) -> str:
    name = "landing-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    count = write_manifest(release)
    incoming = f"{SITES}/.incoming-{name}"
    ssh(f"mkdir -p {SITES} && rm -rf {incoming} && mkdir -p {incoming}")
    tar = shutil.which("tar")
    if not tar:
        raise SystemExit("tar is not available on this computer")
    pack = subprocess.Popen([tar, "-cf", "-", "-C", str(release), "."], stdout=subprocess.PIPE)
    unpack = subprocess.Popen(
        ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", SERVER,
         f"tar -xf - -C {incoming} --no-same-owner"],
        stdin=pack.stdout, stderr=subprocess.PIPE,
    )
    pack.stdout.close()
    _, err = unpack.communicate(timeout=1800)
    if pack.wait() or unpack.returncode:
        raise SystemExit(f"release did not arrive: {err.decode('utf-8', 'replace')[:400]}")
    ssh(f"cd {incoming} && find . -type d -exec chmod 755 {{}} + && find . -type f -exec chmod 644 {{}} + "
        f"&& sha256sum -c --quiet {MANIFEST}", timeout=900)
    ssh(f"cd {SITES} && mv {incoming} {name}")
    print(f"uploaded: {name} ({count} files, sha256 check passed)")
    return name


def prune() -> None:
    # Half-uploaded releases from earlier runs (a dropped connection): by this point our own has
    # already been renamed, so everything named `.incoming-…` is garbage.
    for stale in ssh(f"ls -1A {SITES}").split():
        if re.match(r"^\.incoming-landing-\d{8}-\d{6}$", stale):
            ssh(f"rm -rf {SITES}/{stale}")
            print(f"removed partially uploaded {stale}")
    names = releases()
    keep = set(names[-KEEP:]) | {current_release()}
    for name in names:
        if name not in keep and RELEASE_NAME.match(name):
            ssh(f"rm -rf {SITES}/{name}")
            print(f"removed old release {name}")


def rollback() -> None:
    names = releases()
    now = current_release()
    if now not in names or names.index(now) == 0:
        raise SystemExit(f"nothing to roll back to: current is {now}, releases {names}")
    previous = names[names.index(now) - 1]
    switch_to(previous)
    print(f"restored release {previous} (was {now})")


# -- checking the deployed site ----------------------------------------------------------


class Probe:
    def __init__(self, base: str):
        parts = urlsplit(base)
        self.host, self.port = parts.hostname, parts.port or 80
        self.failed = 0

    def request(self, path: str, method: str = "GET", headers: dict | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        body = response.read() if method != "HEAD" else b""
        headers_out = {k.lower(): v for k, v in response.getheaders()}
        conn.close()
        return response.status, headers_out, body

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' — ' + detail) if detail and not ok else ''}")
        if not ok:
            self.failed += 1


def smoke(release: Path, base: str, windows_zip_present: bool | None, mac_files_present: bool | None = True) -> int:
    """Check of what was published. Returns the number of failed checks (0 means all is well).

    The checks of the Mac builds run only if the files are on the server (`mac_files_present`):
    publishing the landing page alone must not "fail" because of them, otherwise the owner gets
    used to not trusting the result."""
    probe = Probe(base)
    print(f"checking {base}:")
    for name in pages(release):
        path = "/" if name == "index.html" else f"/{name}"
        status, headers, body = probe.request(path, headers={"Accept-Encoding": "gzip"})
        gz = headers.get("content-encoding") == "gzip"
        data = gzip.decompress(body) if gz and body else body
        same = data == (release / name).read_bytes()
        probe.check(f"{path}: 200, compressed, matches the local one", status == 200 and gz and same,
                    f"status={status} gzip={gz} same={same} ({len(data)} vs {(release / name).stat().st_size})")
    for path, kind in (("/robots.txt", "text/plain"), ("/favicon.ico", "")):
        status, headers, _ = probe.request(path)
        probe.check(f"{path}: 200", status == 200 and kind in headers.get("content-type", ""), f"status={status}")
    sample = sorted((release / "img" / "en").glob("*.png"))[0]
    status, headers, body = probe.request(f"/img/en/{sample.name}")
    probe.check(f"/img/en/{sample.name}: same size as local", status == 200 and len(body) == sample.stat().st_size,
                f"status={status} len={len(body)}")
    status, headers, body = probe.request("/no-such-page")
    probe.check("/no-such-page: 404 and its own page", status == 404 and b"This page doesn" in body, f"status={status}")
    for hidden in ("/.manifest.sha256", "/.env", "/img/", "/sites/"):
        status, _, _ = probe.request(hidden)
        probe.check(f"{hidden}: not served", status == 404, f"status={status}")
    for label, ua, where in (("Windows", WIN_UA, "/download/windows"), ("Mac", MAC_UA, "/download/mac"),
                             ("iPhone", PHONE_UA, "/download/other"), ("curl", "curl/8.5.0", "/download/other")):
        status, headers, _ = probe.request("/download", headers={"User-Agent": ua})
        probe.check(f"/download ({label}) → {where}", status == 302 and headers.get("location") == where
                    and "user-agent" in headers.get("vary", "").lower(), f"status={status} location={headers.get('location')}")
    expect_mac = "/Argus-mac.dmg" if mac_files_present else "/?dl=soon"
    status, headers, _ = probe.request("/download/mac")
    probe.check(f"/download/mac → {expect_mac}", status == 302 and headers.get("location") == expect_mac,
                f"status={status} location={headers.get('location')}")
    expect = "/Argus-win.zip" if windows_zip_present else "/?dl=soon"
    status, headers, _ = probe.request("/download/windows")
    probe.check(f"/download/windows → {expect}", status == 302 and headers.get("location") == expect,
                f"status={status} location={headers.get('location')}")
    if mac_files_present:
        status, headers, _ = probe.request("/Argus-mac.dmg", method="HEAD")
        probe.check("/Argus-mac.dmg: attachment, resumable", status == 200 and "attachment" in headers.get("content-disposition", "")
                    and headers.get("accept-ranges") == "bytes" and int(headers.get("content-length", 0)) > 50_000_000,
                    f"status={status} {headers.get('content-disposition')} {headers.get('content-length')}")
        status, headers, body = probe.request("/Argus-mac.dmg", headers={"Range": "bytes=0-99"})
        probe.check("/Argus-mac.dmg: Range → 206", status == 206 and len(body) == 100, f"status={status} len={len(body)}")
        status, headers, body = probe.request("/latest-mac.json")
        probe.check("/latest-mac.json: json", status == 200 and b'"zip"' in body, f"status={status}")
    else:
        print("  (no Mac files on the server - their checks were skipped)")
    return probe.failed


# -- server setup ----------------------------------------------------------------------


def patch_compose(text: str) -> str:
    """Replace only the `downloads` service in the compose file (together with the comment above it)."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if l.rstrip() == "  downloads:"), None)
    if start is None:
        raise SystemExit("compose has no downloads service: a different file is expected, not touching it by hand")
    top = start
    while top > 0 and lines[top - 1].startswith("  #"):
        top -= 1
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or lines[end].strip() == "" and end + 1 < len(lines) and lines[end + 1].startswith("    ")):
        end += 1
    after = lines[end:]
    while after and after[0].strip() == "":
        after.pop(0)
    new = lines[:top] + COMPOSE_BLOCK.rstrip("\n").split("\n") + ([""] + after if after else [])
    return "\n".join(new)


def setup(dry_run: bool) -> None:
    conf = (HERE / "nginx" / "default.conf").read_text(encoding="utf-8").replace("\r\n", "\n")
    inc = (HERE / "nginx" / "security.inc").read_text(encoding="utf-8").replace("\r\n", "\n")
    compose_now = ssh(f"cat {REMOTE}/docker-compose.yml")
    compose_new = patch_compose(compose_now.replace("\r\n", "\n"))
    diff = "".join(difflib.unified_diff((compose_now + "\n").splitlines(True), (compose_new.rstrip("\n") + "\n").splitlines(True),
                                        "docker-compose.yml (server)", "docker-compose.yml (will be)"))
    print(diff or "compose is already in the desired state")
    if dry_run:
        return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="argus-nginx-") as tmp:
        tmp = Path(tmp)
        (tmp / "default.conf").write_text(conf, encoding="utf-8", newline="\n")
        (tmp / "security.inc").write_text(inc, encoding="utf-8", newline="\n")
        (tmp / "docker-compose.yml").write_text(compose_new.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        ssh(f"mkdir -p {SITES} {NGINX_DIR}")
        scp(tmp / "default.conf", f"{NGINX_DIR}/default.conf")
        scp(tmp / "security.inc", f"{NGINX_DIR}/security.inc")
        ssh(f"docker run --rm -v {NGINX_DIR}:/etc/nginx/conf.d:ro -v {SITES}:/usr/share/nginx/sites:ro "
            f"-v {REMOTE}/downloads:/usr/share/nginx/downloads:ro nginx:alpine nginx -t")
        print("nginx config passed nginx -t")
        if diff:
            ssh(f"cp -p {REMOTE}/docker-compose.yml {REMOTE}/docker-compose.yml.before-landing-{stamp}")
            scp(tmp / "docker-compose.yml", f"{REMOTE}/docker-compose.yml.new")
            ssh(f"cd {REMOTE} && docker compose -f docker-compose.yml.new config -q && mv docker-compose.yml.new docker-compose.yml")
    ssh(f"cd {REMOTE} && docker compose up -d downloads", timeout=300)
    print("downloads service restarted (80 and 8080)")


# -- main ------------------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Publish the landing page to the server")
    ap.add_argument("--no-build", action="store_true", help="use the prebuilt landing/out")
    ap.add_argument("--check", action="store_true", help="only build and check")
    ap.add_argument("--rollback", action="store_true", help="roll back to the previous release")
    ap.add_argument("--setup", action="store_true", help="set up nginx and compose on the server")
    ap.add_argument("--dry-run", action="store_true", help="with --setup: only show the changes")
    ap.add_argument("--base", default=PUBLIC, help="which address to use for checking the published site")
    ap.add_argument("--allow-legal-placeholders", action="store_true",
                    help="publish even though the terms, policy or help contain placeholders instead of operator data")
    args = ap.parse_args()

    if args.setup:
        setup(args.dry_run)
        return 0
    if args.rollback:
        rollback()
        return 0

    with tempfile.TemporaryDirectory(prefix="argus-landing-") as tmp:
        tmp = Path(tmp)
        built = LANDING / "out" if args.no_build else tmp / "built"
        if not args.no_build:
            build(built)
        release = tmp / "release"
        assemble(built, release)
        check_release(release, args.allow_legal_placeholders)
        if args.check:
            print("--check: not uploading to the server")
            return 0
        previous = current_release()
        name = upload(release)
        switch_to(name)
        print(f"release activated: {name}")
        zip_present = ssh(f"test -f {REMOTE}/downloads/Argus-win.zip && echo yes || echo no") == "yes"
        mac_present = ssh(f"test -f {REMOTE}/downloads/Argus-mac.dmg && echo yes || echo no") == "yes"
        failed = smoke(release, args.base, zip_present, mac_present)
        if failed:
            # A broken page does not stay on the site until someone reads a hint: the previous release is
            # restored automatically, and old releases (prune) are not touched on failure, so there is
            # something to go back to.
            if previous and previous != name:
                switch_to(previous)
                print(f"post-publish check: {failed} failed - automatically restored the previous release {previous}")
            else:
                print(f"post-publish check: {failed} failed, no previous release - kept {name}")
            return 1
        prune()
    print(f"done: {args.base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
