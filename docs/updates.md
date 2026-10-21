# Updating Argus (Windows and macOS)

State as of 2026-10-14 (release signing since 2026-10-13, tighter swap and publishing gates after the 2026-10-14 audit). The app updates in two ways: **by itself at startup** (before the window opens) and **with the "Update" button**
in the top bar while it is running. Both go through the same code (`src/argus/update.py`); the swap itself is different on each system.

## Whom we trust: the release signature

The update arrives over plain `http://`, and a swap on the network path cannot be told from the real file. So the release
manifest (`latest.json` on Windows, `latest-mac.json` on macOS) is **signed with the release key** (Ed25519), and the public key is built into the
app (`src/argus/update_trust.py: TRUSTED_KEYS`).

```json
{"version": "1.0+6993e55", "zip": "Argus-mac.zip", "sha256": "…", "size": 120000000, "build": 1791895589, "sig": "…"}
```

The signed string is built from these fields (`manifest_message`): platform, version, archive name, sha256, size, build number. The app
takes an update only if:

1. the signature matches one of the built-in keys (no signature, or a foreign one, means refusal and nothing is downloaded);
2. the downloaded archive **matches the signed hash and size** (otherwise it is not unpacked);
3. the release is **newer** than the installed one: `build` only grows, so a signed but old release cannot be pushed as a rollback
   (`update.json` next to the data remembers the installed release; the "installing" record is confirmed only if `VERSION` matches
   after the swap);
4. the archive name is a plain file name (`ZIP_NAME_RE`), not a path and not a foreign address;
5. on macOS also: the bundle is intact (`codesign --verify`), has the same bundle identifier, and **the same signing key**, if the running
   program has a permanent one (see below).

The Ed25519 signature is verified in pure Python (`src/argus/ed25519.py`, checked against the RFC 8032 vectors and against `cryptography`):
the build gets no new dependency.

Earlier builds (before 2026-10-13) do not check the signature and ignore extra manifest fields: they install the first update to a build with
the check as before.

## How the swap survives failures

**Windows.** A running `Argus.exe` cannot be overwritten, so `update.py` downloads and verifies the archive and hands the swap to
a separate PowerShell script (`$ErrorActionPreference = 'Stop'`; the file has a BOM, since PowerShell 5.1 otherwise reads Cyrillic as ANSI;
paths go through `-LiteralPath`: `[` and `]` in the install folder are not a pattern). The script waits for exactly this process to exit, **checks the hash of the
archive against the signed one**, unpacks it next to the program (same drive), swaps the top-level items of the build by renaming
(the old ones go to a spare folder), on any error puts everything back as it was, and in every case restarts `Argus.exe`.
The swap does not touch personal files (`argus.local.json`, `argus.account.json`) (`PRESERVE`).

**Checking the new program (Windows).** The spare folder lives until the new program confirms that it started: its startup
writes an `update-alive` mark (`_mark_alive`, at the beginning of `maybe_self_update`, before the network and the window), and after the restart the script waits for it for up to
90 seconds (`ALIVE_WAIT_S`). No mark means the new build crashes at startup or hangs: the script kills its processes, restores the previous
files, starts the previous program again and records a failure in `update-result.json`; the previous program reads the result and remembers the failure
(`failed`, no retry for at least half an hour). The `update.lock` lock is removed before the new program starts, otherwise it would decide that "a swap
is in progress" and exit without marking itself. The script waits only for a build that contains the file **`update-alive.v1`** (`packaging/stage.py` puts it there
when building for Windows): a release re-signed from an earlier version (`publish_win.py --rollback`) cannot mark itself, so it is neither waited for nor
rolled back. The mark is written early in startup: a build that crashes later (window, recognition) is not caught by this check.

**Interrupted swap (Windows).** The script writes a journal, `update-journal.json` (what was renamed and where; before each rename,
atomically). If the script dies midway (power loss, killed process), the next program start (`recover_interrupted_swap`) uses the journal to
restore the previous files from `.update-*\old`, removes the spare folder and the journal, and the release is remembered as failed. A journal saying "swap
finished" (the script did not get to clean up after itself) simply deletes the spare files. While a journal exists, the spare folders are not removed by
the leftover cleanup: they hold the only previous files. A folder without previous files (the script died while unpacking) goes away after
15 minutes, other leftovers after a day. **Not covered:** if a mixed installation does not start at all, this code is never reached. The previous
files are in `.update-*\old`, and the swap did not touch personal data (`PRESERVE`, `sessions`, `chats`), so a new distribution will not overwrite it.

Three files next to the data keep the books: `update.json` (what is installed and what is being installed; `failed` holds failed swaps: after the first one the release is
not tried for half an hour, after the second for a day, instead of downloading 126 MB again at every start), `update.lock` ("a swap is in progress": while it is
fresh and the script process is alive, a second start quietly exits instead of bringing up the old program on top of files being overwritten), and
`update-result.json` (what the script wrote; read at the next start). The number of the installed release is the file **`BUILD`** next to
`VERSION` (the publishing step puts it there), so the "already installed" comparison does not depend on `update.json`. Whether the folder is writable is checked by
actually writing a test file (`os.access` always says "yes" on Windows): a program in `Program Files` immediately gets "Download" instead of
downloading the archive, closing, and never coming back up.

The release manifest is read defensively: an unreadable one (not JSON, nested deep enough to raise `RecursionError`, with a lone surrogate, larger than 64 KB,
served too slowly) means "stay on the current version", not "Failed to start" at every start
(`fetch_latest` and `maybe_self_update` do not raise).

**macOS.** After the swap, `swap.sh` opens the new app and **waits for the "alive" mark** that each of its starts writes
(`_mark_alive`); if it does not arrive, the script restores the previous `.app` and exits with code 4, so a bad build is never left as the only one.

## Keys (kept by whoever publishes; they are not in the repository)

| File (`~/.argus/`) | Purpose | If lost |
|---|---|---|
| `update-signing.key` | the release private key (Ed25519, base64, 32 bytes) | there is nothing to update the installed apps with: a new build would have to be distributed by hand; that is why `TRUSTED_KEYS` also holds a spare public key |
| `update-signing-spare.key` | the private half of the spare key (the second `TRUSTED_KEYS` entry, created 2026-10-14), same format | the spare key can be used only if a build with its public half is already installed on people's machines; keep it **separate from the main one, preferably on a medium outside the computer**; to sign a release with it, use `update_trust.load_signing_seed(update_trust.SPARE_SIGNING_KEY_PATH)` and `sign_manifest` |
| `macos-signing.p12` + `.password` | the permanent signing certificate of the Mac build (self-signed, 20 years) | a new build would be signed with a different certificate: the microphone and screen recording permissions would have to be granted again, and the updater **will refuse to install** such a build (different signing key), so it has to be distributed by hand |
| `macos-signing.cer` | the public part of the certificate, for reference | n/a |

**Make a copy of both on a reliable medium.** The CI secrets (`MACOS_CERTIFICATE`, `MACOS_CERTIFICATE_PASSWORD`,
`MACOS_IDENTITY`) are stored in the repository settings; they are set with
`uv run --no-project --with pynacl python deploy/mac/signing_setup.py secrets`; a new certificate is created with
`python deploy/mac/signing_setup.py cert` (it does not overwrite an existing one).

## How to publish a release

- **macOS:** push to the `macos-port` branch (CI builds, signs with the permanent certificate and does a trial launch of the bundle; the
  **`Argus-macOS-verified`** artifact is published only after a successful launch), then `python deploy/mac/publish_mac.py` takes the
  latest successful run with that mark, checks the branch, the event and the commit, and asks for confirmation (in a terminal without input,
  `--yes`), downloads the artifact, verifies it, **signs the release manifest**, uploads it and verifies what was uploaded. `--check` does everything except
  the upload.
- **Windows:** build in a separate folder (see `packaging/README.md`, `packaging/build_win.ps1`, `deploy/win/publish_win.py`), then
  `python deploy/win/publish_win.py --app …\dist\Argus`. It works the same way: the manifest is signed, checked before upload with the same code that
  the app uses, and the public file is checked after upload. `--check` does everything except the upload, `--no-smoke` skips the trial launch,
  `--rollback` brings back the previous release (signed again and with a new build number; the previous one is kept as `Argus-win.prev.zip` and
  `latest.prev.json`).
- **Landing page:** `python deploy/landing/publish_landing.py` (`--check`; `--rollback` — the five latest releases are kept; it rolls back by itself
  if the check after upload fails; `deploy/landing/README.md`).

**The gates before distribution are shared** (`deploy/release_guard.py`, called by all three publishing scripts):

1. secrets: the real values from `.env` are searched byte by byte everywhere, including inside the PyInstaller PYZ archive; key patterns are searched in
   text files and (with a narrow set) in binary files;
2. legal placeholders: `<mark class="todo">` and `TODO-…` in the terms, the policy and the help text make publishing refuse until
   `legal/operator.json` is filled in; `--allow-legal-placeholders` is a deliberate bypass and prints a warning;
3. a build of the "wrong flavor" (Windows): `claude.exe` and the subscription path to the model must not be distributed;
4. the `build` number must be strictly greater than the one already published and not from the future (the server clock is compared), so a bad
   release rolled back with `--rollback` gets a new number, not the old one;
5. the trial launch of the Windows build runs on a **free port**, not on the live 8420, and checks that this very build answers
   (`/build`): before, the check passed on an already running app, and a broken build went out to people.

## Why a permanent certificate on Mac

macOS permissions (microphone, screen recording) are tied to the program's "code requirement". With an ad-hoc signature this is the hash of the
build itself: every new build is a different program for the system, and after every update the permissions would be requested again (screen
recording, by hand in System Settings). Signing with a permanent certificate gives the requirement "bundle identifier and this
certificate", the same for all builds. The certificate is self-signed: the first install from a browser still asks to "Open Anyway"
(only Apple Developer, $99/year, solves that), while updates the app installs itself get no quarantine flag and raise no questions.

If you move to Developer ID, the certificate will change, and that will take one manual install (the "same signing key" check will not let
a build with a different key install automatically; this is by design).

## The "Update" button

`update-button.js` is on all pages (`index`, `home`, `record`, `admin`): it asks `GET /update/state`
(`src/argus/update_service.py`) and draws a bright button in the window bar when a release is available. States: "Update", then "Downloading…"
(progress bar), then "Restarting…"; "Download" where installing is impossible (launched from Downloads, no write access to the folder) opens
the download page in the browser; "Failed — download" after a failure (the first press tries to install again, since the server
resets the failure itself, and only a second failure opens the download page; a failure is forgotten when a newer release appears). If recognition is running, the first press asks "Recording in progress. Update?", and the service
installs nothing during recognition without confirmation (`confirm`): the check is on the route, not only in the button.
POST routes require a JSON body, so a foreign page in the browser cannot press them. The service asks the server two
minutes after startup and every half hour after that; at startup the app installs a release by itself.

## What to check by hand

- on Mac: after an update from a signed build to a signed one, the microphone and screen recording permissions are **not** requested again;
- that a Windows app of an earlier build (without signature checking) installs the first build with the check;
- the button, by hand: put a newer release manifest on the server and press "Update" in the built app.
