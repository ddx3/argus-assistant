"""The guardian's eyes on the server itself: containers, disk, backup, certificate, the money tables, the access log.

The server is replaced by a fake `run`. The database script is the exception: it runs for real, against a real
accounts database made by the server's own code, because a query that is never run proves nothing.
"""

from __future__ import annotations

import base64
import re
import sqlite3
import subprocess
import sys
import time

import pytest
from guardian_helpers import Clock, FakeNotifier, make_deps

from argus.guardian.checks import data, host, logs
from argus.guardian.config import Config, Mode, Probe
from argus.guardian.deps import Deps, Result
from argus.guardian.runner import run_cycle
from argus.guardian.state import State

SSH = ["ssh", "-i", "key", "-o", "BatchMode=yes", "root@server.example"]
NOW = 1_800_000_000.0     # 2027-01-15


def cfg_for(tmp_path, **kw) -> Config:
    base = {"home": tmp_path, "ssh_command": SSH, "host_containers": ["argus-server", "argus-discord"],
            "host_backup_dir": "/opt/backups", "host_cert": "/opt/certs/fullchain.pem", "data_container": "argus-server"}
    base.update(kw)
    cfg = Config(**base)
    cfg.validate()
    return cfg


def good_output(**over) -> str:
    lines = {
        "c1": "C argus-server true healthy", "c2": "C argus-discord true healthy", "d": "D 12%",
        "b": f"B {int(NOW)} {int(NOW) - 4 * 3600}", "t": "T notAfter=Apr 10 08:59:52 2027 GMT",
    }
    lines.update(over)
    return "\n".join(v for v in lines.values() if v) + "\n"


# --- the host --------------------------------------------------------------------------------


def test_a_healthy_server_has_nothing_to_report(tmp_path):
    assert host.parse(good_output(), cfg_for(tmp_path), NOW) == []


@pytest.mark.parametrize("override, key, severity", [
    ({"c1": "C argus-server false none"}, "host:container:argus-server", "critical"),
    ({"c2": "C argus-discord true unhealthy"}, "host:container:argus-discord", "high"),
    ({"c2": ""}, "host:container:argus-discord", "high"),
    ({"c1": "C argus-server Error: No such object: argus-server"}, "host:container:argus-server", "critical"),
    ({"d": "D 93%"}, "host:disk", "high"),
    ({"b": f"B {int(NOW)} {int(NOW) - 40 * 3600}"}, "host:backup", "high"),
    ({"b": f"B {int(NOW)} "}, "host:backup", "high"),
    ({"t": "T notAfter=Jan 20 08:59:52 2027 GMT"}, "host:cert", "high"),
    ({"t": "T Could not open file"}, "host:cert", "medium"),
])
def test_each_server_problem_is_named_and_graded(tmp_path, override, key, severity):
    found = host.parse(good_output(**override), cfg_for(tmp_path), NOW)
    assert [(o.key, o.severity) for o in found] == [(key, severity)]


def test_the_command_is_built_only_from_validated_names(tmp_path):
    command = host.build_command(cfg_for(tmp_path))
    assert "docker inspect" in command and "argus-server" in command and "/opt/backups" in command
    for forbidden in ("rm ", "rm\t", ">", "kill", "restart", "stop", "exec", "mv ", "chmod", "write"):
        assert forbidden not in command.replace("2>&1", "").replace("2>/dev/null", "")


@pytest.mark.parametrize("field, value", [
    ("host_containers", ["argus; reboot"]), ("host_backup_dir", "/opt/b $(id)"), ("host_cert", "/etc/x`id`"),
    ("data_container", "a b"), ("data_db", "/app/db'; drop table x; --"),
])
def test_a_setting_that_could_change_the_command_is_refused(tmp_path, field, value):
    with pytest.raises(ValueError, match="may contain only"):
        cfg_for(tmp_path, **{field: value})


def test_the_ssh_prefix_must_be_a_list_of_strings(tmp_path):
    with pytest.raises(ValueError, match="list of strings"):
        cfg_for(tmp_path, ssh_command=["ssh", 5])


def test_a_problem_is_reported_after_two_checks_in_a_row_and_clears_on_recovery(tmp_path):
    state = State(tmp_path)
    outputs = iter([good_output(d="D 95%"), good_output(d="D 95%"), good_output()])
    deps = Deps(run=lambda argv, **kw: Result(0, next(outputs)), now=Clock(NOW))
    cfg = cfg_for(tmp_path)
    first = host.run(cfg, deps, state)
    assert first.observations == [] and first.still_open == {"host:disk"}
    second = host.run(cfg, deps, state)
    assert [o.key for o in second.observations] == ["host:disk"]
    third = host.run(cfg, deps, state)
    assert third.observations == [] and third.still_open == set() and third.complete


def test_the_command_goes_to_the_server_as_the_one_last_argument(tmp_path):
    seen = []
    deps = Deps(run=lambda argv, **kw: seen.append((argv, kw)) or Result(0, good_output()), now=Clock(NOW))
    host.run(cfg_for(tmp_path), deps, State(tmp_path))
    argv, kw = seen[0]
    assert argv[:-1] == SSH and argv[-1].startswith('echo "C argus-server') and kw["timeout"] <= 90


def test_an_unreachable_server_is_one_problem_after_two_tries_and_hides_nothing_else(tmp_path):
    state = State(tmp_path)
    deps = Deps(run=lambda argv, **kw: Result(255, "", "ssh: connect to host: Connection timed out"), now=Clock(NOW))
    cfg = cfg_for(tmp_path)
    first = host.run(cfg, deps, state)
    assert first.observations == [] and first.still_open == {"host:ssh"} and not first.complete
    second = host.run(cfg, deps, state)
    assert [o.key for o in second.observations] == ["host:ssh"] and "Connection timed out" in second.observations[0].evidence
    assert not second.complete          # an unreachable server proves no other problem is gone
    deps.run = lambda argv, **kw: Result(0, good_output())
    third = host.run(cfg, deps, state)
    assert third.complete and third.observations == []


def test_without_ssh_there_is_no_opinion(tmp_path):
    cfg = Config(home=tmp_path)
    assert host.run(cfg, make_deps(), State(tmp_path)) is None
    assert data.run(cfg, make_deps(), State(tmp_path)) is None


# --- the money tables, with the real query ----------------------------------------------------


def make_database(path):
    from argus.server.db import connect

    db = connect(path)
    db.close()
    return sqlite3.connect(path, isolation_level=None)


def add_account(db, ident, balance, *, credit=None, charged=0, held=0):
    db.execute("INSERT INTO accounts (id, email, email_key, password, created_at, balance_micro) VALUES (?,?,?,?,?,?)",
               (ident, f"p{ident}@example.com", f"p{ident}@example.com", "x", NOW, balance))
    if credit:
        db.execute("INSERT INTO credits (account_id, kind, micro, note, at) VALUES (?, 'gift', ?, '', ?)", (ident, credit, NOW))
    if charged:
        db.execute("INSERT INTO usage (account_id, kind, amount, at, cost_micro, charged_micro) VALUES (?, 'stt_seconds', 1, ?, ?, ?)",
                   (ident, NOW, charged // 2, charged))
    if held:
        db.execute("INSERT INTO holds (account_id, micro, at) VALUES (?, ?, ?)", (ident, held, time.time() - 3600))


def run_script_against(database_path) -> list:
    """Run the guardian's database script the way the server would: python reads it from stdin, path as argument."""
    cfg = cfg_for(database_path.parent)

    def fake_run(argv, **kw):
        command = argv[-1]
        payload = re.search(r"echo (\S+) \| base64 -d \| docker exec -i argus-server python3 - (\S+)$", command)
        assert payload.group(2) == cfg.data_db            # the path comes from the settings, not from anywhere else
        script = base64.b64decode(payload.group(1)).decode()
        done = subprocess.run([sys.executable, "-I", "-", str(database_path)], input=script.encode("utf-8"), capture_output=True, check=False)
        return Result(done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))

    state = State(database_path.parent)
    deps = Deps(run=fake_run, now=Clock(NOW))
    observations = []
    for _ in range(data.NEEDED_FAILS):           # a problem needs two sightings in a row
        observations = data.run(cfg, deps, state).observations
    return sorted(o.key for o in observations)


def test_a_consistent_database_is_quiet(tmp_path):
    db = make_database(tmp_path / "accounts.sqlite")
    add_account(db, 1, 300_000, credit=300_000)
    add_account(db, 2, 700_000, credit=1_000_000, charged=300_000)
    add_account(db, 3, 0, credit=100_000, charged=150_000)            # spent more than it had: the balance stops at zero
    add_account(db, 4, 400_000, credit=500_000, held=100_000)         # a call in progress holds 100k
    db.execute("UPDATE holds SET at = ?", (time.time(),))
    assert run_script_against(tmp_path / "accounts.sqlite") == []


def test_money_that_appeared_or_vanished_is_found(tmp_path):
    db = make_database(tmp_path / "accounts.sqlite")
    add_account(db, 1, 900_000, credit=300_000)         # more than was ever credited
    add_account(db, 2, 100_000, credit=300_000)         # less, with nothing charged
    add_account(db, 3, -5, credit=0)                    # negative
    assert run_script_against(tmp_path / "accounts.sqlite") == ["data:balance-drift", "data:negative-balance"]


def test_a_forgotten_hold_is_found(tmp_path):
    db = make_database(tmp_path / "accounts.sqlite")
    add_account(db, 1, 400_000, credit=500_000, held=100_000)     # the hold dates from NOW, long past
    assert run_script_against(tmp_path / "accounts.sqlite") == ["data:stale-holds"]


def test_a_paid_order_without_a_top_up_is_found_and_a_credited_one_is_not(tmp_path):
    db = make_database(tmp_path / "accounts.sqlite")
    add_account(db, 1, 5_000_000, credit=5_000_000)
    for ident, state in ((7, "paid"), (8, "paid"), (9, "new"), (10, "reversed")):
        db.execute("INSERT INTO orders (id, account_id, plan, amount, currency, provider, state, created_at, paid_at)"
                   " VALUES (?, 1, 'topup', 500, 'USD', 'x', ?, ?, ?)", (ident, state, time.time() - 7200, time.time() - 3600))
    db.execute("UPDATE credits SET kind='topup', note=?", (data.ORDER_NOTE + "7",))      # order 7 is credited, 8 is not
    found = run_script_against(tmp_path / "accounts.sqlite")
    assert found == ["data:uncredited-order"]


def test_a_recent_loss_making_charge_is_found(tmp_path):
    db = make_database(tmp_path / "accounts.sqlite")
    add_account(db, 1, 1_000_000, credit=1_000_000)
    db.execute("INSERT INTO usage (account_id, kind, amount, at, cost_micro, charged_micro) VALUES (1, 'brain_tokens', 5, ?, 500, 0)",
               (time.time(),))
    # the charged amount of 0 changes the expected balance, so the balance moves with it and only the price is wrong
    assert run_script_against(tmp_path / "accounts.sqlite") == ["data:loss-making"]


def test_the_script_only_reads_and_the_report_has_no_personal_data(tmp_path):
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "PRAGMA", "ATTACH", "VACUUM"):
        assert not re.search(rf"\b{word}\b", data.SCRIPT, re.IGNORECASE), word
    assert "mode=ro" in data.SCRIPT and "email" not in data.SCRIPT.lower()


def test_garbled_database_output_is_one_visible_problem(tmp_path):
    assert [o.key for o in data.parse("Traceback (most recent call last): ...")] == ["data:unreadable"]


def test_an_unreachable_server_gives_no_data_opinion(tmp_path):
    deps = Deps(run=lambda argv, **kw: Result(255, "", "timed out"), now=Clock(NOW))
    assert data.run(cfg_for(tmp_path), deps, State(tmp_path)) is None


# --- the access log --------------------------------------------------------------------------

ACCESS = ('INFO:     203.0.113.7:51234 - "POST /meter/stt?token=abc HTTP/1.1" 500 Internal Server Error\n'
          'INFO:     203.0.113.8:51235 - "POST /meter/stt?token=def HTTP/1.1" 500 Internal Server Error\n'
          'INFO:     203.0.113.9:51236 - "GET /pricing HTTP/1.1" 200 OK\n'
          'INFO:     203.0.113.9:51237 - "GET /nothing HTTP/1.1" 404 Not Found\n')


def test_server_errors_in_the_access_log_are_grouped_and_hold_no_addresses(tmp_path):
    found = logs.parse(ACCESS)
    assert [(c.key, c.count) for c in found] == [("log5xx:POST /meter/stt 500", 2)]
    assert "203.0.113" not in found[0].sample and "token" not in found[0].sample


def test_one_stray_server_error_is_not_a_problem_but_a_repeat_is(tmp_path):
    cfg = Config(home=tmp_path, log_command=["ssh", "host", "docker logs --since {since} argus-server"], log_min_count=2)
    one = Deps(run=lambda argv, **kw: Result(0, ACCESS.splitlines()[0], ""), now=Clock(NOW))
    assert logs.run(cfg, one, State(tmp_path)).observations == []
    two = Deps(run=lambda argv, **kw: Result(0, ACCESS, ""), now=Clock(NOW))
    assert [o.key for o in logs.run(cfg, two, State(tmp_path / "other")).observations] == ["log5xx:POST /meter/stt 500"]


# --- the daily "all is well" mail ------------------------------------------------------------


def quiet_cfg(tmp_path, **kw) -> Config:
    return Config(mode=Mode.WATCH, home=tmp_path, probes=[Probe("api", "https://api.example/pricing")], heartbeat_hour=0, **kw)


def quiet_deps(clock, notifier, status=200) -> Deps:
    deps = make_deps(clock, notifier=notifier)
    deps.fetch = lambda url: (status, "ok")
    return deps


async def test_a_quiet_day_still_gets_one_short_mail(tmp_path):
    clock, notifier = Clock(NOW), FakeNotifier()
    cfg = quiet_cfg(tmp_path)
    report = await run_cycle(cfg, quiet_deps(clock, notifier))
    assert report.checked == ["probes"]
    assert len(notifier.sent) == 1 and notifier.sent[0][0] == "Argus guardian: all quiet"
    assert "Looked at: probes" in notifier.sent[0][1]
    clock.advance(1800)
    await run_cycle(cfg, quiet_deps(clock, notifier))
    assert len(notifier.sent) == 1                          # once a day, not once a cycle
    clock.advance(36 * 3600)
    await run_cycle(cfg, quiet_deps(clock, notifier))
    assert len(notifier.sent) == 2


async def test_the_mail_waits_for_its_hour_and_can_be_switched_off(tmp_path):
    notifier = FakeNotifier()
    late = quiet_cfg(tmp_path)
    late.heartbeat_hour = 24                                # no hour of the day is that late
    await run_cycle(late, quiet_deps(Clock(NOW), notifier))
    off = quiet_cfg(tmp_path / "off")
    off.heartbeat_hour = -1
    await run_cycle(off, quiet_deps(Clock(NOW), notifier))
    assert notifier.sent == []


async def test_a_problem_report_counts_as_the_days_mail(tmp_path):
    state = State(tmp_path)
    state.kv_set("probe_fails:api", "1")                    # one failure already seen: the next one makes it a problem
    state.close()
    notifier = FakeNotifier()
    await run_cycle(quiet_cfg(tmp_path), quiet_deps(Clock(NOW), notifier, status=500))
    assert [t for t, _ in notifier.sent] == ["Argus guardian: 1 problem(s)"]      # no extra "all quiet" mail on top of it
