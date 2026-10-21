#!/usr/bin/env python3
"""A "server is alive" check that sends mail to `support@arguspilot.com`. Runs ON the server
from cron every 5 minutes.

    */5 * * * * /usr/bin/python3 /opt/argus-server/healthcheck.py >> /opt/argus-server/healthcheck.log 2>&1

What is checked (each problem is a short stable string):
- the `argus-server` container is running and healthy (`docker inspect`);
- the API answers both from inside (`127.0.0.1:8421/pricing`) and from outside through nginx
  and the certificate (`https://api.arguspilot.com/pricing`);
- the download host answers: `https://arguspilot.com/latest.json` and the old address of
  already issued builds `http://203.0.113.10:8080/latest.json` (it must not be turned off:
  it is baked into builds that have already been issued);
- certificate: more than 14 days left (otherwise renewal is not working);
- disk: less than 90% used;
- a fresh database copy: not older than 36 hours (`backup-db.sh`).

Mail goes through Resend (the send-only key from `.env`): `Argus <noreply@arguspilot.com>` ->
`support@arguspilot.com`, and from there to the owner. A single failure sends no mail: two
checks in a row are needed (the network and docker sometimes blink). Each problem is
reported once, then reminded about every 6 hours, and when everything is fixed there is one
"recovered" mail. The state is kept in `healthcheck.state.json` next to the
script.

    python3 healthcheck.py --test     # send a test mail and exit
    HC_FAKE_PROBLEM="test" python3 healthcheck.py   # fake problem (twice in a row: mail)
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path("/opt/argus-server")
ENV = HOME / "deploy" / "server" / ".env"
STATE = Path(os.environ.get("HC_STATE", HOME / "healthcheck.state.json"))
BACKUPS = HOME / "backups"
CERT = HOME / "certs" / "live" / "arguspilot.com" / "fullchain.pem"
TO = "support@arguspilot.com"
FROM = "Argus <noreply@arguspilot.com>"

NEEDED_FAILS = 2                 # number of checks in a row before writing
REMIND_S = 6 * 3600              # how often to remind about a live problem
CERT_MIN_DAYS = 14
DISK_MAX_PCT = 90
BACKUP_MAX_AGE_S = 36 * 3600


# -------------------------------------- decision: whether to write (a pure function, tested)


def decide(state: dict, problems: set[str], now: float) -> tuple[list[str], bool, dict]:
    """From the previous state and the current problems: what to write about, whether a
    "recovered" mail is needed, and the new state.

    `state` = {"fails": {problem: how many checks in a row}, "alerted": {problem: when it was
    reported}}.
    """
    fails = {p: state.get("fails", {}).get(p, 0) + 1 for p in problems}
    alerted = dict(state.get("alerted", {}))
    to_alert = []
    for problem in sorted(problems):
        if fails[problem] < NEEDED_FAILS:
            continue
        last = alerted.get(problem)
        if last is None or now - last >= REMIND_S:
            to_alert.append(problem)
            alerted[problem] = now
    # the problem is gone, so we forget it; "recovered" is written only if the
    # problem was reported before
    recovered = any(p not in problems for p in alerted)
    alerted = {p: t for p, t in alerted.items() if p in problems}
    return to_alert, recovered, {"fails": fails, "alerted": alerted}


# --------------------------------------------------- checks


def fetch(url: str, timeout: int = 15) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, r.read(200_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:                                      # noqa: BLE001 — no connection, certificate error, timeout: all of it means "not answering"
        return 0, b""


def check_all() -> set[str]:
    problems: set[str] = set()
    health = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}} {{.State.Health.Status}}", "argus-server"],
                            capture_output=True, text=True).stdout.strip()
    if health != "true healthy":
        problems.add(f"container argus-server is not healthy ({health or 'not found'})")
    if fetch("http://127.0.0.1:8421/pricing")[0] != 200:
        problems.add("API does not respond from inside (127.0.0.1:8421)")
    if fetch("https://api.arguspilot.com/pricing")[0] != 200:
        problems.add("API does not respond from outside (https://api.arguspilot.com): nginx, certificate or DNS")
    status, body = fetch("https://arguspilot.com/latest.json")
    if status != 200 or b"version" not in body:
        problems.add("the download endpoint does not respond (https://arguspilot.com/latest.json)")
    if fetch("http://203.0.113.10:8080/latest.json")[0] != 200:
        problems.add("the old download address does not respond (http://203.0.113.10:8080) — already issued builds expect it")
    try:
        end = subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", str(CERT)], capture_output=True, text=True).stdout
        left = (time.mktime(time.strptime(end.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")) - time.time()) / 86400
        if left < CERT_MIN_DAYS:
            problems.add(f"certificate expires in {int(left)} days — renewal (renew-certs.sh) did not work")
    except Exception:                                      # noqa: BLE001
        problems.add("could not read the certificate")
    usage = shutil.disk_usage("/")
    if usage.used / usage.total * 100 > DISK_MAX_PCT:
        problems.add(f"disk usage is at {usage.used / usage.total * 100:.0f}%")
    files = sorted(BACKUPS.glob("argus-accounts-*.sqlite.gz"), key=lambda p: p.stat().st_mtime) if BACKUPS.is_dir() else []
    if not files or time.time() - files[-1].stat().st_mtime > BACKUP_MAX_AGE_S:
        problems.add("no fresh database copy for more than 36 hours (backup-db.sh)")
    fake = os.environ.get("HC_FAKE_PROBLEM")
    if fake:
        problems.add(fake)
    return problems


# --------------------------------------------------- mail


def resend_key() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("ARGUS_SMTP_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return ""


def send(subject: str, text: str) -> bool:
    key = resend_key()
    if not key:
        print("no mail key - email not sent")
        return False
    body = json.dumps({"from": FROM, "to": [TO], "subject": subject, "text": text}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                          "User-Agent": "argus-healthcheck/1"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:                                 # noqa: BLE001
        print("email was not sent:", e)
        return False


def main(argv: list[str]) -> int:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if "--test" in argv:
        ok = send("Argus: server check — test email", "If you are reading this, the server status alerts reach you.")
        print(stamp, "test email:", "sent" if ok else "NOT sent")
        return 0 if ok else 1
    problems = check_all()
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    to_alert, recovered, new = decide(state, problems, time.time())
    if to_alert:
        send("Argus: server problem", "What is wrong:\n- " + "\n- ".join(to_alert)
             + "\n\nThe check runs every 5 minutes; a reminder comes every 6 hours until it is fixed.")
    if recovered and not problems:
        send("Argus: server is fine again", "All checks pass.")
    elif recovered:
        send("Argus: some problems are gone", "Still open:\n- " + "\n- ".join(sorted(problems)))
    STATE.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    # To the log only when there is something to say, plus an "alive" line once an hour (otherwise
    # it grows by 288 lines a day for nothing).
    if problems or recovered or time.localtime().tm_min < 5:
        print(stamp, "problems:", len(problems), "| email about the problem:", bool(to_alert), "| recovered:", recovered)
        for p in sorted(problems):
            print("  -", p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
