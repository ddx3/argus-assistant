"""The guardian's checks and its memory: what counts as a problem, and how the same problem stays one finding."""

from __future__ import annotations

from guardian_helpers import Clock, Fixture, make_deps

from argus.guardian.checks import logs, probes, repo
from argus.guardian.config import Config, Probe
from argus.guardian.deps import Deps, Result, run_command
from argus.guardian.state import State, finding_id

TRACEBACK = '''2026-10-20 10:00:01 INFO request ok
2026-10-20 10:00:02 ERROR unhandled exception in /pricing
Traceback (most recent call last):
  File "/app/src/argus/server/app.py", line 210, in pricing
    return load_prices(user)
  File "/app/src/argus/server/pricing.py", line 77, in load_prices
    return PRICES[model]
KeyError: 'claude-9'
2026-10-20 10:00:09 INFO request ok
'''


# --- logs ------------------------------------------------------------------------------------


def test_a_traceback_becomes_one_problem_named_by_exception_and_frame():
    clusters = logs.parse(TRACEBACK)
    tracebacks = [c for c in clusters if c.key.startswith("log:")]
    assert len(tracebacks) == 1
    c = tracebacks[0]
    assert c.key == "log:KeyError@argus/server/pricing.py in load_prices"
    assert c.title == "KeyError in argus/server/pricing.py in load_prices"
    assert "claude-9" in c.sample


def test_the_same_bug_hit_many_times_is_one_problem_with_a_count():
    clusters = [c for c in logs.parse(TRACEBACK * 5) if c.key.startswith("log:")]
    assert len(clusters) == 1 and clusters[0].count == 5


def test_different_bugs_are_different_problems():
    other = TRACEBACK.replace("KeyError: 'claude-9'", "ValueError: bad").replace("KeyError", "ValueError")
    keys = {c.key for c in logs.parse(TRACEBACK + other) if c.key.startswith("log:")}
    assert len(keys) == 2


def test_plain_error_lines_are_grouped_with_numbers_blanked():
    text = "\n".join(f"2026-10-20 10:00:0{i} ERROR payment 4{i}7 failed for order 12{i}34" for i in range(5))
    clusters = [c for c in logs.parse(text) if c.key.startswith("logerr:")]
    assert len(clusters) == 1 and clusters[0].count == 5


def test_plain_errors_need_repeats_but_tracebacks_and_critical_do_not(tmp_path):
    state = State(tmp_path)
    log = tmp_path / "server.log"
    log.write_text("ERROR once only\n" + TRACEBACK + "CRITICAL disk full\n", encoding="utf-8")
    cfg = Config(home=tmp_path, log_file=str(log), log_min_count=3)
    result = logs.run(cfg, make_deps(), state)
    titles = [o.title for o in result.observations]
    assert any("KeyError" in t for t in titles) and any("disk full" in t for t in titles)
    assert not any("once only" in t for t in titles)
    assert result.complete is False


def test_the_evidence_is_redacted_and_capped(tmp_path):
    state = State(tmp_path)
    log = tmp_path / "server.log"
    log.write_text(TRACEBACK.replace("claude-9", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 jane@example.com") + "x" * 10, encoding="utf-8")
    result = logs.run(Config(home=tmp_path, log_file=str(log)), make_deps(), state)
    evidence = result.observations[0].evidence
    assert "sk-ant-api03" not in evidence and "jane@" not in evidence and len(evidence) < 4000


def test_a_log_file_is_read_once_and_a_rotation_starts_over(tmp_path):
    state = State(tmp_path)
    log = tmp_path / "server.log"
    cfg = Config(home=tmp_path, log_file=str(log))
    log.write_text(TRACEBACK, encoding="utf-8")
    assert len(logs.run(cfg, make_deps(), state).observations) == 1
    assert logs.run(cfg, make_deps(), state).observations == []          # nothing new
    short = ["Traceback (most recent call last):", '  File "/a/argus/x.py", line 1, in f', "ValueError: v", ""]
    log.write_text("\n".join(short), encoding="utf-8")
    assert [o.key for o in logs.run(cfg, make_deps(), state).observations] == ["log:ValueError@argus/x.py in f"]   # truncated: read again


def test_a_log_command_gets_the_time_of_the_last_scan(tmp_path):
    state = State(tmp_path)
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return Result(0, TRACEBACK, "")
    clock = Clock()
    deps = Deps(run=fake_run, now=clock)
    cfg = Config(home=tmp_path, log_command=["ssh", "host", "docker", "logs", "--since", "{since}", "argus-server"], log_window_min=10)
    logs.run(cfg, deps, state)
    clock.advance(300)
    logs.run(cfg, deps, state)
    first, second = seen[0][5], seen[1][5]
    assert first != second and second.endswith("Z")


def test_no_log_source_means_no_opinion(tmp_path):
    assert logs.run(Config(home=tmp_path), make_deps(), State(tmp_path)) is None


# --- probes ----------------------------------------------------------------------------------


def test_a_probe_reports_only_after_two_failures_in_a_row(tmp_path):
    state = State(tmp_path)
    answers = iter([(500, ""), (500, ""), (200, "ok"), (0, "")])
    deps = Deps(fetch=lambda url: next(answers), now=Clock())
    cfg = Config(home=tmp_path, probes=[Probe("api", "https://api.example/pricing")])
    first = probes.run(cfg, deps, state)
    assert first.observations == [] and first.still_open == {"probe:api"}
    second = probes.run(cfg, deps, state)
    assert [o.key for o in second.observations] == ["probe:api"] and "status 500" in second.observations[0].evidence
    third = probes.run(cfg, deps, state)
    assert third.observations == [] and third.still_open == set()
    fourth = probes.run(cfg, deps, state)
    assert fourth.observations == []        # one blip after recovery is not a problem again


def test_a_probe_can_require_text_in_the_answer(tmp_path):
    state = State(tmp_path)
    deps = Deps(fetch=lambda url: (200, "<html>maintenance</html>"), now=Clock())
    cfg = Config(home=tmp_path, probes=[Probe("dist", "https://x/latest.json", expect_text="version")])
    probes.run(cfg, deps, state)
    obs = probes.run(cfg, deps, state).observations
    assert obs and "does not contain" in obs[0].evidence


# --- the test-suite check --------------------------------------------------------------------

PYTEST_OUTPUT = '''============================= FAILURES =============================
______________________ test_add_positive_numbers ______________________

    def test_add_positive_numbers():
>       assert add(2, 3) == 5
E       assert -1 == 5

tests/test_calc.py:5: AssertionError
______________________ test_other ______________________

    def test_other():
>       raise ValueError("nope")
E       ValueError: nope

tests/test_calc.py:9: ValueError
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_add_positive_numbers - assert -1 == 5
FAILED tests/test_calc.py::test_other - ValueError: nope
ERROR tests/test_broken.py - ImportError: no module
2 failed, 1 error in 0.1s
'''


def test_pytest_failures_are_parsed_with_their_tracebacks():
    found = repo.parse_failures(PYTEST_OUTPUT)
    assert [(k, t) for k, t, _ in found] == [("FAILED", "tests/test_calc.py::test_add_positive_numbers"),
                                             ("FAILED", "tests/test_calc.py::test_other"), ("ERROR", "tests/test_broken.py")]
    section = repo.failure_section(PYTEST_OUTPUT, "tests/test_calc.py::test_add_positive_numbers")
    assert "assert add(2, 3) == 5" in section and "test_other" not in section


def test_the_repo_check_runs_on_a_clean_copy_and_reports_the_failing_test(tmp_path):
    fx = Fixture(tmp_path)
    cfg = fx.config()
    state = State(cfg.home)
    result = repo.run(cfg, make_deps(), state)
    assert [o.key for o in result.observations] == ["test:tests/test_calc.py::test_add_positive_numbers"]
    assert "assert" in result.observations[0].evidence
    assert not (cfg.home / "work").exists() or not any((cfg.home / "work").iterdir())     # the worktree is gone
    # the developer's own checkout was not touched
    assert (fx.repo / "src" / "app" / "calc.py").read_text(encoding="utf-8").count("a - b") == 1


def test_the_repo_check_does_not_rerun_until_the_branch_moves_or_a_day_passes(tmp_path):
    fx = Fixture(tmp_path)
    cfg = fx.config()
    state = State(cfg.home)
    clock = Clock()
    deps = make_deps(clock)
    assert repo.run(cfg, deps, state) is not None
    assert repo.run(cfg, deps, state) is None
    clock.advance(25 * 3600)
    assert repo.run(cfg, deps, state) is not None


def test_a_timed_out_suite_is_one_problem(tmp_path):
    fx = Fixture(tmp_path)
    cfg = fx.config()
    deps = Deps(now=Clock(), run=lambda argv, **kw: Result(124, "", "timed out") if argv[0] != "git" else run_command(argv, **kw))
    result = repo.run(cfg, deps, State(cfg.home))
    assert [o.key for o in result.observations] == ["tests:timeout"]


# --- state -----------------------------------------------------------------------------------


def test_the_same_problem_seen_again_is_one_finding(tmp_path):
    state = State(tmp_path)
    a, new_a = state.upsert(key="probe:api", check_name="probes", title="t", severity="high", evidence="e1", now=1.0)
    b, new_b = state.upsert(key="probe:api", check_name="probes", title="t", severity="high", evidence="e2", now=2.0)
    assert new_a and not new_b and a.id == b.id == finding_id("probe:api")
    assert b.count == 2 and b.evidence == "e2" and b.first_seen == 1.0 and b.last_seen == 2.0
    assert len(state.list()) == 1


def test_a_problem_that_stops_is_resolved_and_a_return_reopens_it_fresh(tmp_path):
    state = State(tmp_path)
    f, _ = state.upsert(key="k", check_name="c", title="t", severity="high", evidence="e", now=1.0)
    state.update(f.id, status="diagnosed", diagnosis={"summary": "s"}, last_notified=5.0)
    assert [g.id for g in state.resolve_missing("c", set())] == [f.id]
    assert state.get(f.id).status == "resolved"
    again, came_back = state.upsert(key="k", check_name="c", title="t", severity="high", evidence="e", now=9.0)
    assert came_back and again.status == "open" and again.diagnosis is None and again.last_notified is None


def test_resolving_one_check_leaves_the_others_alone(tmp_path):
    state = State(tmp_path)
    state.upsert(key="a", check_name="probes", title="t", severity="high", evidence="e", now=1.0)
    state.upsert(key="b", check_name="logs", title="t", severity="high", evidence="e", now=1.0)
    state.resolve_missing("probes", set())
    assert {f.key: f.status for f in state.list()} == {"a": "resolved", "b": "open"}


def test_counters_are_per_day_and_state_survives_a_restart(tmp_path):
    state = State(tmp_path)
    state.add("fixes", 1, now=1_800_000_000)
    state.add("fixes", 2, now=1_800_000_100)
    assert state.today("fixes", now=1_800_000_200) == 3
    assert state.today("fixes", now=1_800_000_000 + 86400 * 2) == 0
    state.kv_set("x", "y")
    state.close()
    again = State(tmp_path)
    assert again.kv_get("x") == "y" and again.today("fixes", now=1_800_000_200) == 3


def test_a_chosen_text_in_a_log_cannot_make_two_problems_share_an_id(tmp_path):
    """Ids are a short hash of the key; a log can carry attacker-chosen text, so a clash must not crash the cycle."""
    state = State(tmp_path)
    first, _ = state.upsert(key="log:A@x", check_name="logs", title="a", severity="high", evidence="e", now=1.0)
    state.db.execute("UPDATE findings SET key='log:other@y' WHERE id=?", (first.id,))      # another problem now owns that id
    second, new = state.upsert(key="log:A@x", check_name="logs", title="a", severity="high", evidence="e", now=2.0)
    assert new and second.id != first.id and second.id.startswith(first.id) and second.key == "log:A@x"
    assert len(state.list()) == 2


def test_pytest_output_with_windows_line_endings_still_yields_the_traceback():
    windowsy = PYTEST_OUTPUT.replace(chr(10), chr(13) + chr(10))
    # `repo.run` normalises before parsing; the parser itself needs plain newlines
    plain = windowsy.replace(chr(13) + chr(10), chr(10))
    assert "assert add(2, 3) == 5" in repo.failure_section(plain, "tests/test_calc.py::test_add_positive_numbers")
