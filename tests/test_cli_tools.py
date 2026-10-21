"""Terminal commands: doctor, voices, development server.

The doctor is needed where something is broken, and before, the first failed check took
down the whole table.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from argus import cli

runner = CliRunner()


@pytest.fixture
def quiet_machine(tmp_path, monkeypatch):
    """The doctor without hardware and without personal files: settings come from a temporary
    folder.
    """
    (tmp_path / "argus.toml").write_text('[brain]\nprovider = "stub"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "find_config", lambda *a, **k: tmp_path / "argus.toml")
    for name in ("_check_audio", "_check_screen", "_check_window", "_check_compare"):
        monkeypatch.setattr(cli, name, lambda cfg: (cli.OK, "stub"))
    monkeypatch.setattr(cli, "_check_models", lambda cfg: (cli.OK, "stub"))
    return tmp_path


def test_a_check_that_raises_is_reported_not_fatal():
    def boom(cfg):
        raise ImportError("DLL load failed while importing onnxruntime")

    status, text = cli._run_check(boom, None)
    assert status == cli.FAIL
    assert "ImportError" in text and "onnxruntime" in text


def test_the_doctor_prints_every_row_even_when_one_check_crashes(quiet_machine, monkeypatch):
    def boom(cfg):
        raise ImportError("rapidocr failed to load")

    monkeypatch.setattr(cli, "_check_models", boom)
    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1                         # a failure is not "all good"
    assert "the check failed to run" in result.output
    for row in ("Python", "Disk", "Audio", "Screen", "Window"):
        assert row in result.output                       # the table reached the end


def test_the_doctor_says_so_when_the_settings_cannot_be_read(tmp_path, monkeypatch):
    (tmp_path / "argus.toml").write_text("[ui]\nport = 80\n", encoding="utf-8")
    monkeypatch.setattr(cli, "find_config", lambda *a, **k: tmp_path / "argus.toml")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "settings cannot be read" in result.output


def test_the_doctor_has_no_gpu_or_milestone_hints(quiet_machine, monkeypatch):
    """There is no local engine in the product: Macs and machines without a graphics card are
    not "slow".
    """
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    result = runner.invoke(cli.app, ["doctor"])
    assert "nvidia" not in result.output.lower() and "GPU" not in result.output
    assert "M6" not in result.output


def test_a_missing_claude_cli_is_fine_for_other_providers(quiet_machine, monkeypatch):
    from argus.core.config import Config

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cfg = Config()
    cfg.brain.provider = "relay"
    assert cli._check_claude(cfg)[0] == cli.OK
    cfg.brain.provider = "claude-code"
    assert cli._check_claude(cfg)[0] == cli.WARN


def test_the_doctor_lists_misspelled_keys(tmp_path):
    (tmp_path / "argus.toml").write_text("[gate_brain]\ncooldown = 3\n", encoding="utf-8")
    status, text = cli._check_settings(tmp_path / "argus.toml")
    assert status == cli.WARN and "gate_brain.cooldown" in text
    assert cli._check_settings(None)[0] == cli.OK


# --------------------------------------------------------------------------
# Voices
# --------------------------------------------------------------------------


@pytest.mark.parametrize("voice, expected", [
    ({"fine_tuning": {"language": "uk"}}, "uk"),
    ({"fine_tuning": None, "language": "ru"}, "ru"),            # `fine_tuning: null` from the response
    ({"fine_tuning": None}, ""),
    ({"language": "en"}, "en"),
    ({"fine_tuning": {"language": None}, "language": "pl"}, "pl"),
    ({}, ""),
])
def test_voice_language_survives_nulls_in_the_answer(voice, expected):
    assert cli._voice_language(voice) == expected


# --------------------------------------------------------------------------
# Development server
# --------------------------------------------------------------------------


def _dev_server(monkeypatch, tmp_path, *args: str, allow: str | None = None):
    """Start `argus account-server --dev ...` with a fake uvicorn. Returns the result and the
    `run` calls.
    """
    import uvicorn

    monkeypatch.chdir(tmp_path)               # --dev puts the database and the secret next to itself, not into the repository
    if allow is None:
        monkeypatch.delenv("ARGUS_ALLOW_STUB", raising=False)
    else:
        monkeypatch.setenv("ARGUS_ALLOW_STUB", allow)
    started: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(k))
    result = runner.invoke(cli.app, ["account-server", "--dev", *args])
    return result, started


def test_the_dev_account_server_refuses_to_listen_on_the_network(monkeypatch, tmp_path):
    """A stub till tops up the balance without payment."""
    result, started = _dev_server(monkeypatch, tmp_path, "--host", "0.0.0.0")
    assert result.exit_code == 1 and not started
    assert "for this computer only" in result.output
    assert not (tmp_path / "argus-accounts.sqlite").exists()       # it did not get as far as the database


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "example.com"])
@pytest.mark.parametrize("allow", ["", "0", "true", "yes", "2"])
def test_the_stub_till_stays_off_the_network_unless_the_interlock_says_exactly_one(
        monkeypatch, tmp_path, host, allow):
    """The same interlock as the production server (`billing.from_env`): only
    `ARGUS_ALLOW_STUB=1`.
    """
    result, started = _dev_server(monkeypatch, tmp_path, "--host", host, allow=allow)
    assert result.exit_code == 1 and not started
    assert "ARGUS_ALLOW_STUB=1" in result.output


def test_the_interlock_lets_a_developer_listen_on_a_network_but_loudly(monkeypatch, tmp_path):
    """A virtual machine or a container: it has to listen on 0.0.0.0, and the connections come
    from the host machine.
    """
    result, started = _dev_server(monkeypatch, tmp_path, "--host", "0.0.0.0", allow="1")
    assert result.exit_code == 0
    assert [k["host"] for k in started] == ["0.0.0.0"]
    assert "OPEN TO THE NETWORK" in result.output
    assert (tmp_path / "argus-accounts.sqlite").exists()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "::1"])
def test_a_local_dev_server_needs_no_interlock(monkeypatch, tmp_path, host):
    result, started = _dev_server(monkeypatch, tmp_path, "--host", host)
    assert result.exit_code == 0 and [k["host"] for k in started] == [host]
    assert "OPEN TO THE NETWORK" not in result.output


def test_the_default_host_of_the_dev_server_is_this_computer_only(monkeypatch, tmp_path):
    result, started = _dev_server(monkeypatch, tmp_path)
    assert result.exit_code == 0 and [k["host"] for k in started] == ["127.0.0.1"]
