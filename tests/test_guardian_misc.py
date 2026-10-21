"""The guardian's edges: the model wrapper's permissions, GitHub, notifications, settings, and the command line."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from argus.guardian import __main__ as cli
from argus.guardian.brain import BUILTIN_TOOLS, ClaudeBrain
from argus.guardian.config import Config, Mode
from argus.guardian.github import GitHub
from argus.guardian.notify import (
    EmailNotifier,
    FileNotifier,
    TelegramNotifier,
    build_notifier,
    resend_key,
)
from argus.guardian.state import State

# --- the model wrapper -------------------------------------------------------------------


def _options(cfg: Config):
    from claude_agent_sdk import create_sdk_mcp_server

    server = create_sdk_mcp_server(name="guardian", tools=[])
    return ClaudeBrain(cfg)._options("system text", server, ["mcp__guardian__read_file"], 7, 0.5)


def test_the_sdk_is_configured_with_every_layer_of_restriction():
    o = _options(Config())
    assert o.tools == []                                  # no built-in tools exist at all
    assert o.allowed_tools == ["mcp__guardian__read_file"]
    for name in ("Bash", "Write", "Edit", "WebFetch", "Task"):
        assert name in o.disallowed_tools and name in BUILTIN_TOOLS
    assert o.setting_sources == [] and o.strict_mcp_config is True
    assert o.permission_mode == "dontAsk" and o.max_turns == 7 and o.max_budget_usd == 0.5


def test_subscription_mode_blanks_the_api_key_and_api_mode_leaves_it():
    assert _options(Config(auth="subscription")).env == {"ANTHROPIC_API_KEY": ""}
    assert _options(Config(auth="api")).env == {}


def test_the_permission_callback_denies_everything_but_our_tools():
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    can_use = _options(Config()).can_use_tool
    assert isinstance(asyncio.run(can_use("mcp__guardian__read_file", {}, None)), PermissionResultAllow)
    for tool in ("Bash", "Read", "mcp__other__thing", "WebFetch"):
        assert isinstance(asyncio.run(can_use(tool, {}, None)), PermissionResultDeny)


def test_the_desktop_app_cannot_reach_the_guardian():
    code = "import argus.cli, sys; print('argus.guardian' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"


# --- GitHub --------------------------------------------------------------------------------


def test_a_draft_pull_request_is_opened_with_the_token_and_nothing_else():
    sent = []

    def post(url, token, payload):
        sent.append((url, token, payload))
        return 201, {"html_url": "https://github.com/o/n/pull/7"}
    gh = GitHub("o/n", token="t0ken", post=post)
    assert gh.open_draft_pull_request(base="main", head="guardian/x", title="t", body="b") == "https://github.com/o/n/pull/7"
    url, token, payload = sent[0]
    assert url == "https://api.github.com/repos/o/n/pulls" and token == "t0ken" and payload["draft"] is True


def test_github_failures_are_explained_and_a_missing_token_is_not_a_crash():
    gh = GitHub("o/n", token="t", post=lambda *a: (422, {"message": "branch not found"}))
    with pytest.raises(RuntimeError, match="422"):
        gh.open_draft_pull_request(base="main", head="x", title="t", body="b")
    none = GitHub("o/n", token="")
    assert not none.can_open_pull_requests
    with pytest.raises(RuntimeError):
        none.open_draft_pull_request(base="main", head="x", title="t", body="b")
    assert none.compare_url("main", "guardian/x") == "https://github.com/o/n/compare/main...guardian/x?expand=1"


def test_there_is_no_way_to_merge():
    assert not any("merge" in name for name in dir(GitHub))


# --- notifications -------------------------------------------------------------------------


def test_reports_are_written_redacted(tmp_path):
    FileNotifier(tmp_path).send("Problem", "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 and jane@example.com")
    latest = (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "sk-ant" not in latest and "jane@" not in latest and "# Problem" in latest


def test_telegram_messages_are_redacted_capped_and_failures_swallowed():
    sent = []
    TelegramNotifier("123:abc", "42", post=lambda url, data: sent.append((url, data))).send("T", "x" * 9000 + " ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "ghp_" not in sent[0][1].decode() and len(sent[0][1]) < 5000

    def boom(url, data):
        raise OSError("offline")
    TelegramNotifier("t", "c", post=boom).send("T", "b")      # must not raise


def test_telegram_is_added_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDIAN_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("GUARDIAN_TELEGRAM_CHAT", raising=False)
    cfg = Config(home=tmp_path, resend_key_file="")
    assert len(build_notifier(cfg).notifiers) == 1
    monkeypatch.setenv("GUARDIAN_TELEGRAM_TOKEN", "1:a")
    monkeypatch.setenv("GUARDIAN_TELEGRAM_CHAT", "9")
    assert len(build_notifier(cfg).notifiers) == 2


def test_an_email_is_sent_redacted_with_the_key_the_sender_and_the_recipient():
    sent = []
    mail = EmailNotifier("re_key", "Guardian <noreply@example.com>", "ops@example.com", post=lambda key, payload: sent.append((key, payload)))
    mail.send("Problem sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "from jane@example.com " + "x" * 30000)
    key, payload = sent[0]
    assert key == "re_key" and payload["from"] == "Guardian <noreply@example.com>" and payload["to"] == ["ops@example.com"]
    assert "sk-ant" not in payload["subject"] and "jane@" not in payload["text"] and len(payload["text"]) <= 20000


def test_a_failed_email_does_not_stop_the_cycle():
    def boom(key, payload):
        raise OSError("offline")
    mail = EmailNotifier("k", "a@example.com", "b@example.com", post=boom)
    mail.send("T", "b")      # must not raise
    assert "OSError" in mail.last_error


def test_a_sent_email_remembers_the_providers_id_for_test_notify():
    mail = EmailNotifier("k", "a@example.com", "b@example.com", post=lambda key, payload: {"id": "abc-123"})
    mail.send("T", "b")
    assert mail.last_id == "abc-123" and mail.last_error == ""


def test_email_needs_all_three_of_sender_recipient_and_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDIAN_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("GUARDIAN_RESEND_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text("re_from_file" + chr(10), encoding="utf-8")
    base = {"home": tmp_path, "resend_key_file": str(key_file)}
    assert len(build_notifier(Config(**base)).notifiers) == 1                                  # no sender or recipient
    assert len(build_notifier(Config(**base, email_to="b@example.com")).notifiers) == 1
    full = Config(**base, email_from="a@example.com", email_to="b@example.com")
    assert [type(n).__name__ for n in build_notifier(full).notifiers] == ["FileNotifier", "EmailNotifier"]
    assert resend_key(full) == "re_from_file"
    monkeypatch.setenv("GUARDIAN_RESEND_KEY", "re_from_env")
    assert resend_key(full) == "re_from_env"                                                    # the environment wins
    no_file = Config(home=tmp_path, resend_key_file=str(tmp_path / "missing"), email_from="a@x.com", email_to="b@x.com")
    assert len(build_notifier(no_file).notifiers) == 2                                          # an env key alone is enough


# --- settings ------------------------------------------------------------------------------


def test_settings_load_from_toml_and_the_environment(tmp_path, monkeypatch):
    (tmp_path / "guardian.toml").write_text(textwrap.dedent('''
        mode = "watch"
        repo = "C:/work/argus"
        max_fixes_per_day = 1
        log_command = ["ssh", "host", "docker", "logs", "--since", "{since}", "argus-server"]
        [[probes]]
        name = "api"
        url = "https://api.example/pricing"
        expect_text = "plans"
    '''), encoding="utf-8")
    monkeypatch.setenv("GUARDIAN_HOME", str(tmp_path))
    monkeypatch.delenv("GUARDIAN_MODE", raising=False)
    cfg = Config.load()
    assert cfg.mode is Mode.WATCH and cfg.max_fixes_per_day == 1 and cfg.probes[0].expect_text == "plans"
    monkeypatch.setenv("GUARDIAN_MODE", "fix")
    assert Config.load().mode is Mode.FIX


def test_an_unknown_setting_is_an_error_not_a_silent_typo(tmp_path, monkeypatch):
    (tmp_path / "guardian.toml").write_text("max_fixes_per_dy = 9\n", encoding="utf-8")
    monkeypatch.setenv("GUARDIAN_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="unknown setting"):
        Config.load()


def test_fix_mode_is_refused_without_a_sandbox(tmp_path):
    assert Config(mode=Mode.FIX, home=tmp_path).effective_mode is Mode.ASK
    assert Config(mode=Mode.FIX, home=tmp_path, sandbox=["docker", "run"]).effective_mode is Mode.FIX
    assert Config(mode=Mode.WATCH, home=tmp_path).effective_mode is Mode.WATCH


def test_the_sandbox_wraps_the_test_command_and_can_have_its_own(tmp_path):
    plain = Config(test_command=["python", "-m", "pytest"])
    assert plain.test_argv(tmp_path, ["tests/a.py"]) == ["python", "-m", "pytest", "tests/a.py"]
    boxed = Config(test_command=["python", "-m", "pytest"], sandbox=["docker", "run", "-v", "{worktree}:/w"],
                   sandbox_test_command=["pytest", "-q"])
    assert boxed.test_argv(tmp_path, ["x"]) == ["docker", "run", "-v", f"{tmp_path}:/w", "pytest", "-q", "x"]


def test_the_environment_of_a_sandboxed_run_points_nowhere_into_the_worktree(tmp_path):
    plain = Config(test_env={"PYTHONPATH": "src", "X": "1"})
    assert plain.test_env_for(tmp_path) == {"PYTHONPATH": str(tmp_path / "src"), "X": "1"}
    assert Config(test_env={"PYTHONPATH": "src"}, sandbox=["box"]).test_env_for(tmp_path) == {}


def test_model_written_code_runs_only_with_a_sandbox_or_a_written_opt_in():
    assert not Config().can_run_model_code
    assert Config(sandbox=["box"]).can_run_model_code
    assert Config(allow_unsandboxed=True).can_run_model_code


def test_the_default_mode_asks_before_fixing():
    assert Config().mode is Mode.ASK


def test_the_kill_switch_overrides_every_mode(tmp_path, monkeypatch):
    cfg = Config(mode=Mode.FIX, home=tmp_path, sandbox=['box'])
    assert cfg.effective_mode is Mode.FIX
    (tmp_path / "OFF").write_text("x", encoding="utf-8")
    assert cfg.effective_mode is Mode.OFF
    (tmp_path / "OFF").unlink()
    monkeypatch.setenv("GUARDIAN_OFF", "1")
    assert cfg.effective_mode is Mode.OFF


# --- the command line ----------------------------------------------------------------------


def test_the_command_line_status_off_on_and_unknown_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_HOME", str(tmp_path))
    monkeypatch.delenv("GUARDIAN_MODE", raising=False)
    monkeypatch.delenv("GUARDIAN_OFF", raising=False)
    assert cli.main(["status"]) == 0
    assert "mode: ask" in capsys.readouterr().out
    assert cli.main(["off"]) == 0 and (tmp_path / "OFF").exists()
    assert cli.main(["status"]) == 0 and "kill switch is on" in capsys.readouterr().out
    assert cli.main(["on"]) == 0 and not (tmp_path / "OFF").exists()
    assert cli.main(["approve", "F-nothere"]) == 1
    assert cli.main(["show", "F-nothere"]) == 1


def test_show_and_audit_work_on_a_real_problem(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GUARDIAN_HOME", str(tmp_path))
    state = State(tmp_path)
    f, _ = state.upsert(key="probe:api", check_name="probes", title="api down", severity="high", evidence="GET x: no answer", now=1.0)
    state.close()
    assert cli.main(["show", f.id]) == 0
    out = capsys.readouterr().out
    assert "api down" in out and "GET x: no answer" in out
    from argus.guardian.audit import Audit
    Audit(tmp_path).log("test", note="hello")
    assert cli.main(["audit", "5"]) == 0 and "hello" in capsys.readouterr().out
