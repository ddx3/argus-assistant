# Landing page and downloads on our server

Since 2026-10-13 the Argus site has lived on our own server (203.0.113.10) instead of a hosted artifact (requirement: move the landing page to the server and have no
separate download page). The "Download" buttons lead to `/download`, and nginx itself picks the file for the visitor's
system: the person clicks, the file starts downloading, and the page stays where it is.

## Addresses

| address | what |
|---|---|
| `http://203.0.113.10/` (and `:8080`) | the landing page: `index.html`, `pricing.html`, `help.html`, `terms.html`, `privacy.html` |
| `/download` | the "Download" button: by the browser string to `/download/windows`, `/download/mac`, or `/download/other` |
| `/download/mac` | `302 → /Argus-mac.dmg` (an attachment, resuming works) |
| `/download/windows` | `302 → /Argus-win.zip`, and while there is no file, `302 → /?dl=soon` (the landing page shows an explanation and cleans up the address) |
| `/download/other` | phone, Linux, everything else: `302 → /?dl=other` ("the app is for computers") |
| `/Argus-*.dmg\|zip\|exe`, `/latest.json`, `/latest-mac.json` | the builds themselves and the version files (issued builds update themselves from them) |

Ports 80 and 8080 serve the same content. **Port 8080 must not be turned off**: the address `http://203.0.113.10:8080` is baked into `argus.toml`
(`[update] server`) of the builds that have already been issued.

## What is where on the server (`/opt/argus-server/deploy/server/`)

- `docker-compose.yml` — the `downloads` service (nginx:alpine; ports 80 and 8080 → 80). A backup of the previous file is
  `docker-compose.yml.before-landing-<time>`.
- `nginx/default.conf`, `nginx/security.inc` — settings; the source of truth is `deploy/landing/nginx/` in the repository.
- `sites/landing-<date-time>/` — landing page releases (the last five), `sites/current` — a link to the active one.
- `downloads/` — the builds themselves and `latest*.json` (put there by `deploy/mac/publish_mac.py` and `deploy/win/publish_win.py`). Only the names
  `Argus-*.dmg|zip|exe|msi` and `latest*.json` are served, nothing else from this folder.
- `_offline/` — what was taken off distribution (it is not part of it): the previous download page and an old `Argus-win.zip` archive that must never be served again.

## Publishing the landing page

```
python deploy/landing/publish_landing.py              # build the landing page, publish it, check it over HTTP (~10 s)
python deploy/landing/publish_landing.py --check      # build and check, do not upload to the server
python deploy/landing/publish_landing.py --rollback   # restore the previous release
```

The build goes into a temporary folder (`build_landing.py --out`); the script does not write to `landing/out`, so an existing build is not affected.
In the build the home page is a piece of markup without `<html>`: the wrapper around it used to be added by the artifact host, now the publishing step adds it
(`WRAP_HEAD` in the script; it is the same, so the page looks the same). The whole release goes to a new folder, is checked against
sha256 over the list of files, and only then `sites/current` is switched with a single rename: a visitor sees either the old release
or the new one, whole. At the end there is an HTTP check: the pages match the local ones byte for byte, compression is on, `/download` picks the
file by system, the file is served as an attachment, and `Range` answers 206.

**Browser check** (nine languages, PC and phone, the buttons, the question about Mac): `python deploy/landing/check_live.py http://203.0.113.10 <screenshots folder>`, run with the Python of the `.landing-venv` environment (~2.5 min, opens no windows).

**Once on a new server:** `publish_landing.py --setup` (`--dry-run` shows what would change in compose): it installs the nginx config,
replaces only the `downloads` service in compose (with a backup), checks `nginx -t`, and restarts the service.

## Windows

Since 2026-10-13 the distribution has `Argus-win.zip` again, a **clean** build (reason: downloading did not work, and without the file the Windows button showed the note "This download is being updated").
It is built separately, without touching the running app in `dist/`, and published with one command, `python deploy/win/publish_win.py --app <folder>`
(the build is `packaging/build_win.ps1`: it sets both "flavor" variables (`ARGUS_BUILD_API_ONLY=1`, `ARGUS_BUILD_NO_SUBSCRIPTION=1`) itself, computes the version once
(`ARGUS_FULL_VERSION`), builds in the `_winbuild` folder, and calls `packaging/stage.py --public --app …`; details are in the docstring of the script). Before
publishing, the script compares all files with the real values from the project's `.env` (including inside the PyInstaller archive), looks for strings that look like keys and for personal
files, refuses a build of the wrong flavor (`claude.exe`), starts a copy without a window **on a free port** (it must be that copy that answers, `/build`), and only
then uploads the archive and updates `latest.json` (its version must match the build's `VERSION`, otherwise the self-update "updates" to the same thing; the release
number strictly increases). The gate is shared by Windows, Mac, and the landing page: `deploy/release_guard.py`, details are in `docs/updates.md`.
**Placeholders in the terms and the policy now block publishing** (`<mark class="todo">`, `TODO-…`: while `legal/operator.json` is empty, `publish_win.py`,
`publish_mac.py`, and `publish_landing.py` refuse; `--allow-legal-placeholders` is a deliberate bypass with a warning), so without a filled-in
`operator.json` the distribution cannot be updated at all. Speech recognition goes through the server relay (since 2026-10-14), so the person needs no key. The build is not signed: SmartScreen warns, and what to do is the question `faq.14`
on the help page. If there is no `Argus-win.zip` file in `downloads/`, `/download/windows` redirects to the landing page with an explanation (`/?dl=soon`).

## What is wrong, and what to know

- **HTTP only, no domain and no certificate.** The browser says "not secure", and Chrome/Edge may warn about downloading `.dmg`/`.zip`
  over HTTP. When there is a domain: port 443 and a certificate in this same nginx (in the config `.well-known` is no longer closed), the links on the pages
  are relative, so they will not need reconfiguring.
- **Speed.** From the server to the development computer a file goes at ~90 KB/s per connection (an image takes ~19 minutes); the landing pages are compressed with gzip
  (1.4 MB → ~0.3 MB). The "Download in several streams" link left together with the download page (the code is in `landing/archive/` and
  `deploy/mac/index.src.html`). The real solution is a mirror behind a CDN.
- **iPad and Intel Mac.** By the browser string nginx cannot tell them from a Mac with Apple Silicon: every Mac in Safari calls itself "Intel Mac OS X", and
  iPadOS 13+ sends the desktop Safari string. The landing page tells an iPad apart (`navigator.maxTouchPoints > 1`: the download buttons and the link to
  Mac lead to `/download/other`, that is, to the explanation "open this page on your computer", `landing/page.js`); an Intel Mac is told apart only by the caption under the
  footer link ("Apple Silicon, macOS 14 or newer", next to it "Windows 10 2004 or newer"; keys `foot.reqmac`, `foot.reqwin` in `landing/copy/*.json`,
  nine languages in all). The test is `tests/test_landing_downloads.py`.
- **Mac without an Apple signature.** What to do on the first launch is written on the help page (the question about Mac, `faq.13`).
- **The old hosted artifact page** is no longer updated; it was left as it was, and its buttons lead to `:8080/`, which now
  serves the landing page itself.
- The pages `deploy/mac/index.src.html` and `deploy/server/downloads/index.html` are the retired download page; they remain in the repository
  because tests look at them (`test_mac_packaging`, `test_user_text_is_plain`).
- The nginx request log stores visitors' IP addresses: rotation keeps three files of 10 MB each (privacy policy, section 5).
