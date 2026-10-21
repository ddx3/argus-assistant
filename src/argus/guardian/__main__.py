"""Command line: `python -m argus.guardian <command>`.

    status                   mode, today's usage, open problems
    run-once [--dry-run]     one cycle (what a scheduler should call)
    run [--every MINUTES]    cycles in a loop until interrupted
    findings                 all problems, newest first
    show ID                  everything known about one problem
    approve ID [--now]       allow the guardian to prepare a fix on a branch
    reject ID                dismiss a problem or a proposed fix
    off / on                 the kill switch: create or remove the OFF file
    audit [N]                the last N audit records
    test-notify              send a test message through every configured channel
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from argus.guardian import runner
from argus.guardian.audit import Audit
from argus.guardian.config import Config
from argus.guardian.state import State


def _show(cfg: Config, ident: str) -> int:
    state = State(cfg.home)
    try:
        f = state.get(ident)
        if f is None:
            print(f"no such problem: {ident}")
            return 1
        print(f"{f.id}  [{f.severity}]  {f.status}\n{f.title}\nkey: {f.key}\nseen {f.count} time(s)")
        if f.pr_url:
            print(f"pull request / branch: {f.pr_url}")
        if f.note:
            print(f"note: {f.note}")
        if f.diagnosis:
            print("\ndiagnosis:\n" + json.dumps(f.diagnosis, indent=2, ensure_ascii=False))
        print("\nevidence:\n" + f.evidence)
    finally:
        state.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argus.guardian", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to guardian.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p_once = sub.add_parser("run-once")
    p_once.add_argument("--dry-run", action="store_true", help="prepare fixes locally but push nothing")
    p_run = sub.add_parser("run")
    p_run.add_argument("--every", type=float, default=15.0, help="minutes between cycles")
    sub.add_parser("findings")
    p_show = sub.add_parser("show")
    p_show.add_argument("id")
    p_app = sub.add_parser("approve")
    p_app.add_argument("id")
    p_app.add_argument("--now", action="store_true", help="prepare the fix immediately instead of at the next cycle")
    p_rej = sub.add_parser("reject")
    p_rej.add_argument("id")
    sub.add_parser("off")
    sub.add_parser("on")
    sub.add_parser("test-notify")
    p_aud = sub.add_parser("audit")
    p_aud.add_argument("n", nargs="?", type=int, default=30)
    args = parser.parse_args(argv)

    from pathlib import Path

    cfg = Config.load(Path(args.config) if args.config else None)

    if args.command == "off":
        cfg.home.mkdir(parents=True, exist_ok=True)
        cfg.off_file.write_text("off\n", encoding="utf-8")
        print(f"switched off ({cfg.off_file})")
        return 0
    if args.command == "on":
        cfg.off_file.unlink(missing_ok=True)
        print("switched on")
        return 0
    if args.command == "test-notify":
        from argus.guardian.notify import build_notifier

        notifier = build_notifier(cfg)
        channels = getattr(notifier, "notifiers", [notifier])
        notifier.send("Argus guardian: test message",
                      "If you can read this, this channel works. Nothing is wrong; this was sent by `test-notify`.")
        failed = 0
        for n in channels:
            error = getattr(n, "last_error", "")
            failed += bool(error)
            detail = f"FAILED: {error}" if error else ("accepted, id " + n.last_id if getattr(n, "last_id", "") else "done")
            print(f"{type(n).__name__}: {detail}")
        return 1 if failed else 0
    if args.command == "audit":
        for rec in Audit(cfg.home).tail(args.n):
            print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.command == "show":
        return _show(cfg, args.id)
    if args.command in ("status", "findings"):
        state = State(cfg.home)
        try:
            print(runner.summary(cfg, state))
        finally:
            state.close()
        return 0
    if args.command in ("approve", "reject"):
        state = State(cfg.home)
        audit = Audit(cfg.home, run_id="cli")
        try:
            f = runner.approve(state, audit, args.id) if args.command == "approve" else runner.reject(state, audit, args.id)
            print(f"{f.id} is now {f.status}")
        except (KeyError, ValueError) as e:
            print(e)
            return 1
        finally:
            state.close()
        if args.command == "approve" and args.now:
            report = asyncio.run(runner.run_cycle(cfg))
            print(report)
        return 0
    if args.command == "run-once":
        report = asyncio.run(runner.run_cycle(cfg, dry_run=args.dry_run))
        print(report)
        return 0
    if args.command == "run":
        print(f"guardian running every {args.every:g} min; stop with Ctrl+C. Kill switch: {cfg.off_file}")
        try:
            while True:
                report = asyncio.run(runner.run_cycle(cfg))
                print(time.strftime("%H:%M:%S"), report)
                time.sleep(max(1.0, args.every * 60))
        except KeyboardInterrupt:
            return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
