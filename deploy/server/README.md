# Accounts server: what lives here and what not to do

This folder mirrors `/opt/argus-server/deploy/server` on the server (203.0.113.10), but **not completely**.
Do not upload it to the server by copying (`rsync`, `scp -r`): some of the files in the repository are retired,
while the server holds the live ones.

| What | Where the truth is | Note |
|---|---|---|
| `Dockerfile`, `requirements.txt`, `.dockerignore` (in the root) | here | dependencies are pinned from `uv.lock`, and `tests/test_server_deploy.py` checks them |
| `docker-compose.yml`: the `argus-server` service | here | `mem_limit`; the liveness check is in the Dockerfile |
| `docker-compose.yml`: the `downloads` service | here = `COMPOSE_BLOCK` in `deploy/landing/publish_landing.py` | ports 80 and 8080, folders `sites/` and `nginx/`; a test makes sure the copies do not diverge |
| `downloads/latest*.json`, `Argus-*.zip`, `Argus-mac.dmg` | **the server only** | published by `deploy/win/publish_win.py` and `deploy/mac/publish_mac.py`; `latest.json` is signed with the release key |
| `downloads/terms.html`, `downloads/privacy.html` | built by `legal/build.py` | live, do not touch by hand |
| `downloads/index.html`, `downloads/latest.json` in the repository | retired | the old download page and an unsigned `latest.json`; if you overwrite the server ones with them, you lose the release signature |
| `.env` | **the server only** | secrets; they never get into the repository or the image (`.gitignore`, `.dockerignore`) |

## Service commands on the server

They run inside the container, and the person's password is not needed:

    docker exec argus-server python3 -m argus.server.grant email@example.com 25       # add money
    docker exec argus-server python3 -m argus.server.block email@example.com          # close the account (--undo reopens it)
    docker exec argus-server python3 -m argus.server.export email@example.com         # a copy of the person's data (JSON)
    docker exec argus-server python3 -m argus.server.erase email@example.com          # show what will be erased
    docker exec argus-server python3 -m argus.server.erase email@example.com --yes    # erase

An email asking for a copy of the data or for deletion (policy, section 6) is handled by the last
two commands; the promised deadline is 30 days. A payment reversed by the payment provider shows up with the query
`SELECT * FROM orders WHERE state = 'reversed'` in the database (`/app/data/argus-accounts.sqlite`).

## Environment variables (`.env` on the server)

| Variable | What it does |
|---|---|
| `ARGUS_SERVER_SECRET` | signing of the passes, at least 32 characters; required |
| `ARGUS_SERVER_URL` | the public address of the server: links in emails and addresses for the payment provider |
| `ARGUS_BILLING` | `liqpay` — live payments; `stub` — a stub, **only** together with `ARGUS_ALLOW_STUB=1` and a local `ARGUS_SERVER_URL` (otherwise payments are disabled) |
| `ARGUS_MAIL`, `ARGUS_SMTP_*` | mail: `console` — emails go to the log; `ARGUS_SMTP_HOST/PORT/USER/PASSWORD/FROM/TLS` — real mail (port 465 is implicit TLS) |
| `ARGUS_TRUST_PROXY` | `1` — if the server sits behind its own proxy: the client address is taken from the last `X-Forwarded-For` (for the attempt counters); do not enable it without a proxy |
| `ARGUS_SERVER_DOCS` | `1` — open `/docs` and `/openapi.json` (for development only) |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY` | relay keys |

## Memory

The container is limited by `mem_limit: 1g` (docker-compose.yml). Large questions (materials, page images)
take more than five times their size in memory while they are being processed, so `server/limits.py`
lets no more than `BIG_BUDGET` bytes of such bodies into parsing at the same time (120 MB ≈ 600 MB of memory); to the extra ones
the server answers "busy" (503) immediately. When you raise one of the two numbers, raise the other too.

## Open items

- Pin the base image by `@sha256:…` (registry access is needed); it is marked in the `Dockerfile`.
- The first build after the switch to running not as root: check on a copy that the container comes up
  and writes to the volume with the database (`docker compose up -d --build argus-server`, then `docker logs argus-server`).
- Rate limiting of requests to `/auth/*` at the nginx level (`limit_req`): the accounts server has
  no nginx of its own, the server counts attempts itself (`server/throttle.py`), but a flood of requests still
  reaches it.

## Database backup and the proxy secret (2026-10-15)

- **Backup:** `backup-db.sh` (in the repository and on the server at `/opt/argus-server/backup-db.sh`) puts a consistent copy of the database every day at 03:30 via cron
  into `/opt/argus-server/backups/argus-accounts-YYYYMMDD.sqlite.gz` (mode 600, kept for 30 days);
  how to restore is in `restore-db.md`. The copies lie on the same server: once a week it is worth pulling a fresh one to your side.
- **Proxy secret:** nginx signs requests to the API with the `X-Argus-Proxy` header, and the server trusts `X-Forwarded-For` only on
  signed requests (the direct port `:8421` remains for issued builds, and without the signature the address cannot be forged —
  `server/limits.py`, `tests/test_server_proxy_ip.py`). The secret is `ARGUS_PROXY_SECRET` in the server's `.env` and the same one in the file
  `nginx/proxy-secret.inc` (`proxy_set_header X-Argus-Proxy <secret>;`, mode 600). The file is not in the repository, and on a new
  server it is created together with `.env`: `openssl rand -hex 24`.
- **Language of the emails.** The emails and the "new password" page come in the nine languages of the app: the words are in `src/argus/server/mail_texts/<lang>.json`,
  the language is reported by the app (`lang` at registration, login, and the recovery request), and the server stores it in `accounts.lang`.
  To change a text, edit the JSON, then run `python tools/validate_mail_texts.py` and the tests. A new column in the database means raising `SCHEMA_VERSION` in
  `db.py` (a database that already stands at the current version will not get the column by itself).
- **Deploying the full server code** (as it was done on 2026-10-15): build the image from a copy of the tree on the server, run it
  as a separate container on a **copy** of the live database (port 127.0.0.1:8422, `ARGUS_MAIL=console`, `ARGUS_BALANCE_SWEEP_S=0`),
  run the checks (registration with the terms, limits, password change, export, deletion), and only then
  `docker compose up -d --build argus-server`. Rollback: the copies in `/opt/argus-server/server.before-*`.

## The "server is alive" check and database copies

- **The "server is alive" check** — `healthcheck.py` via cron every 5 minutes (`/opt/argus-server/healthcheck.py`, log
  `healthcheck.log`): the container, the API from inside and outside, the distribution (including the old address `:8080`), the certificate expiry, the disk,
  the freshness of the database copy. An email to `support@arguspilot.com` after two checks in a row, a reminder
  every 6 hours, and one "recovered". `--test` sends a test email.
- **A database copy to the operator's computer** — `pull_backup.ps1` (a Task Scheduler task `ArgusServerBackupPull`, on Sundays at
  12:00, and if it is missed, at the next power-on): it pulls a fresh copy from the server into the local folder `_backups/argus-server/`,
  checks the integrity, and keeps the 12 latest. Inside are people's emails and password hashes, so it stays outside the project and outside git.

## Payments: monobank acquiring

The code is `billing.Mono` (`src/argus/server/billing.py`); the pages are `/billing/mono/<order>` (creates an invoice and sends the person to the mono page),
`/billing/mono/callback` (the notification), and `/billing/mono/back/<order>` (the person's return). The tests are `tests/test_server_mono.py`.
The main rule: **money is credited only on mono's own answer to our question about the invoice** (`GET /api/merchant/invoice/status` with our
token), and not on what is written in the notification; the ECDSA signature is not parsed, so a forged notification gives nothing.

Enabling (the live `.env` on the server, then recreate the container following the procedure above):

    ARGUS_BILLING=mono
    ARGUS_MONO_TOKEN=<the live token from web.monobank.ua, the "Merchant" section>
    ARGUS_SERVER_URL=https://api.arguspilot.com
    # optional: if dollars are not enabled for our merchant, the invoice is in UAH at an exchange rate, and the balance stays in dollars
    # ARGUS_MONO_CCY=980
    # ARGUS_MONO_UAH_PER_USD=44.8

**A test token** (`api.monobank.ua`: any card passes, no money moves) is recognized by the server by the merchant (the company registry code from the mono documentation) or by
`ARGUS_MONO_SANDBOX=1`; on a public server such payments are credited only to the accounts from `ARGUS_SANDBOX_ACCOUNTS`.
mono chooses the language of the payment page itself, from the buyer's browser (for uk/ru it shows the `uk` language, otherwise English).
Not verified on the live provider: whether dollars (`ccy` 840) are enabled for our merchant and what the buyer sees; fiscal receipts (PRRO): mono can do them itself.
