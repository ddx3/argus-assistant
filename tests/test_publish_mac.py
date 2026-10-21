"""Publishing the Mac build (`deploy/mac/publish_mac.py`): the gates before signing with the
release key.

Every Mac trusts what is signed with the release key, so: from CI only a run triggered by
a push to the right branch is taken, and only with the commit that is currently in
`origin/macos-port`; signing must happen after confirmation; and the archive check looks
not only for names but also for the real values from `.env`, foreign key formats, terms
placeholders, and a version mismatch between the package and the description (because of
which the app would "update" on every launch).
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY / "mac"))
sys.path.insert(0, str(DEPLOY))

import publish_mac  # noqa: E402

REAL = b"the-real-openai-value-0123456789abcdef"
APP = "Argus.app/Contents"
SEED = f"{APP}/Resources/seed"


def make_artifact(tmp_path: Path, version="1.0+abc", seed_version="1.0+abc", extra: dict | None = None) -> Path:
    folder = tmp_path / "art"
    folder.mkdir(parents=True)
    (folder / "Argus-mac.dmg").write_bytes(b"x" * 100 + b"koly" + b"\0" * 508)
    (folder / "latest-mac.json").write_text(json.dumps({"version": version, "zip": "Argus-mac.zip"}), encoding="utf-8")
    entries = {
        f"{APP}/MacOS/Argus": b"\xcf\xfa\xed\xfe" + b"\0" * 64,
        f"{SEED}/VERSION": seed_version.encode(),
        f"{SEED}/argus.toml": b"[update]\n",
        f"{APP}/Resources/argus/ui/static/legal-terms.html": b"<p>ok</p>",
    }
    entries.update({k: (v if isinstance(v, bytes) else v.encode()) for k, v in (extra or {}).items()})
    with zipfile.ZipFile(folder / "Argus-mac.zip", "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return folder


def test_a_clean_artifact_passes(tmp_path):
    publish_mac.verify(make_artifact(tmp_path), secrets=[REAL])


def test_the_version_inside_the_package_must_equal_the_one_in_the_manifest(tmp_path):
    """The build computed the version twice, with edits in the working folder the two strings
    diverged, and the app kept updating forever.
    """
    with pytest.raises(SystemExit, match="would keep 'updating' on every launch"):
        publish_mac.verify(make_artifact(tmp_path, version="1.0+abc.2610030700", seed_version="1.0+abc.2610030702"), secrets=[])


def test_a_package_without_the_seed_version_is_refused(tmp_path):
    folder = make_artifact(tmp_path)
    with zipfile.ZipFile(folder / "Argus-mac.zip", "w") as z:
        z.writestr(f"{APP}/MacOS/Argus", b"x")
    with pytest.raises(SystemExit, match="seed/VERSION"):
        publish_mac.verify(folder, secrets=[])


@pytest.mark.parametrize("name", [f"{SEED}/.env", f"{SEED}/argus.account.json", f"{SEED}/argus.legal.json", "x/prod.env"])
def test_personal_files_are_refused(tmp_path, name):
    with pytest.raises(SystemExit, match="must not be distributed"):
        publish_mac.verify(make_artifact(tmp_path, extra={name: "x"}), secrets=[])


def test_the_real_env_value_in_a_binary_is_found(tmp_path):
    """The Mac check had no search for real values, and it skipped everything binary (that is,
    almost everything).
    """
    with pytest.raises(SystemExit, match="real value"):
        publish_mac.verify(make_artifact(tmp_path, extra={f"{APP}/Frameworks/libx.dylib": b"\xcf\xfa\0\0" + b"\0" * 40 + REAL}),
                           secrets=[REAL])


@pytest.mark.parametrize("planted", ["sk_" + "0123456789abcdef" * 3, "MTA0NzM5NTQzMjE5ODc2NTQzMg.GaBcDe." + "x" * 27,
                                     "-----BEGIN PRIVATE KEY-----\nabc"])
def test_key_formats_of_other_services_are_found(tmp_path, planted):
    with pytest.raises(SystemExit):
        publish_mac.verify(make_artifact(tmp_path, extra={f"{SEED}/notes.txt": planted}), secrets=[])


def test_a_build_of_the_wrong_flavor_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="wrong build flavor"):
        publish_mac.verify(make_artifact(tmp_path, extra={f"{APP}/Frameworks/claude_agent_sdk/x.py": "x"}), secrets=[])


def test_placeholders_in_the_legal_pages_stop_the_release_unless_allowed(tmp_path, capsys):
    page = {f"{APP}/Resources/argus/ui/static/legal-privacy.html": '<mark class="todo">[email]</mark>'}
    with pytest.raises(SystemExit, match="with placeholders instead of"):
        publish_mac.verify(make_artifact(tmp_path, extra=page), secrets=[])
    publish_mac.verify(make_artifact(tmp_path / "b", extra=page), secrets=[], allow_legal_placeholders=True)
    assert "WARNING: publishing with placeholders" in capsys.readouterr().out


def test_a_package_without_any_legal_pages_is_refused(tmp_path):
    folder = make_artifact(tmp_path)
    with zipfile.ZipFile(folder / "Argus-mac.zip", "w") as z:
        z.writestr(f"{APP}/MacOS/Argus", b"x")
        z.writestr(f"{SEED}/VERSION", b"1.0+abc")
    with pytest.raises(SystemExit, match="no terms and privacy pages"):
        publish_mac.verify(folder, secrets=[])


def test_a_file_that_is_not_a_disk_image_is_refused(tmp_path):
    folder = make_artifact(tmp_path)
    (folder / "Argus-mac.dmg").write_bytes(b"x" * 1000)
    with pytest.raises(SystemExit, match="does not look like a disk image"):
        publish_mac.verify(folder, secrets=[])


# --------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------


@pytest.fixture
def signing(monkeypatch, release_signer):
    monkeypatch.setattr(publish_mac.update_trust, "load_signing_seed", lambda: release_signer.seed)
    return release_signer


def test_the_signed_manifest_is_accepted_by_the_app_and_numbered_above_the_published_one(tmp_path, signing):
    folder = make_artifact(tmp_path)
    signed = publish_mac.sign_release(folder, live={"build": 99_999_999_999}, server_now=None)
    assert signed["build"] == 100_000_000_000, "newer than the published one even with a lagging clock"
    assert publish_mac.update_trust.problem_with_manifest(signed, "mac") == ""
    assert json.loads((folder / "latest-mac.json").read_text(encoding="utf-8")) == signed


def test_a_clock_that_disagrees_with_the_servers_is_refused_before_signing(tmp_path, signing):
    import time

    with pytest.raises(SystemExit, match="clock on this computer differs"):
        publish_mac.sign_release(make_artifact(tmp_path), live=None, server_now=time.time() - 7200)


# --------------------------------------------------------------------------
# which CI build to sign
# --------------------------------------------------------------------------

SHA = "a" * 40


def good_run(**over) -> dict:
    run = {"id": 7, "head_branch": "macos-port", "event": "push", "status": "completed", "conclusion": "success",
           "head_sha": SHA, "actor": {"login": "OWNER"}, "head_commit": {"message": "Mac: a fix\ndetails"}}
    run.update(over)
    return run


@pytest.mark.parametrize(
    ("over", "word"),
    [({"head_branch": "evil"}, "not from branch"), ({"event": "workflow_dispatch"}, "triggered by a push"),
     ({"conclusion": "failure"}, "finish successfully"), ({"status": "in_progress"}, "finish successfully")],
)
def test_only_a_successful_push_run_of_the_right_branch_is_taken(over, word):
    assert word in publish_mac.run_problem(good_run(**over))
    assert publish_mac.run_problem(good_run()) == ""


def test_an_explicit_run_id_is_checked_too(monkeypatch):
    monkeypatch.setattr(publish_mac, "api", lambda path: good_run(head_branch="someone-elses"))
    with pytest.raises(SystemExit, match="cannot be used"):
        publish_mac.pick_run("123")


def artifacts_api(runs, built=True, verified=True, expired=False):
    asked = []

    def api(path):
        asked.append(path)
        if "/artifacts" in path:
            names = (["Argus-macOS"] if built else []) + (["Argus-macOS-verified"] if verified else [])
            return {"artifacts": [{"name": n, "expired": expired} for n in names]}
        if "/actions/runs/" in path:
            return runs[0]
        return {"workflow_runs": runs}

    return api, asked


def test_the_newest_good_verified_run_with_an_artifact_is_picked(monkeypatch):
    api, asked = artifacts_api([good_run(id=1, event="workflow_dispatch"), good_run(id=2, head_branch="x"), good_run(id=3)])
    monkeypatch.setattr(publish_mac, "api", api)
    assert publish_mac.pick_run(None)["id"] == 3
    assert "event=push" in asked[0], "the query itself selects runs by push"


def test_a_build_whose_smoke_test_failed_is_never_published(monkeypatch):
    """The trial launch of the package in CI is `continue-on-error`: a failed step did not stop the
    publishing. Now a "verified" mark is required.
    """
    api, _ = artifacts_api([good_run(id=3)], verified=False)
    monkeypatch.setattr(publish_mac, "api", api)
    with pytest.raises(SystemExit, match="no successful verified build"):
        publish_mac.pick_run(None)
    with pytest.raises(SystemExit, match="no verified marker"):
        publish_mac.pick_run("3")


def test_an_expired_or_missing_build_is_not_picked_either(monkeypatch):
    for kwargs in ({"expired": True}, {"built": False}):
        api, _ = artifacts_api([good_run(id=3)], **kwargs)
        monkeypatch.setattr(publish_mac, "api", api)
        with pytest.raises(SystemExit):
            publish_mac.pick_run(None)


def test_the_workflow_uploads_the_verified_mark_only_after_a_passed_smoke_test():
    text = (DEPLOY.parent / ".github" / "workflows" / "macos.yml").read_text(encoding="utf-8")
    assert "id: smoke" in text and "if: steps.smoke.outcome == 'success'" in text and "Argus-macOS-verified" in text
    assert text.index("id: smoke") < text.index("Argus-macOS-verified")


def test_a_build_of_another_commit_is_never_signed(capsys):
    with pytest.raises(SystemExit, match="is not the one currently on"):
        publish_mac.confirm_run(good_run(head_sha="b" * 40), SHA, assume_yes=True)
    publish_mac.confirm_run(good_run(), SHA, assume_yes=True)
    out = capsys.readouterr().out
    assert "OWNER" in out and SHA[:12] in out and "Mac: a fix" in out, "who and what is on the screen before signing"


def test_signing_needs_a_confirmation(capsys):
    with pytest.raises(SystemExit, match="only after confirmation"):
        publish_mac.confirm_run(good_run(), SHA, assume_yes=False, interactive=False)
    with pytest.raises(SystemExit, match="cancelled"):
        publish_mac.confirm_run(good_run(), SHA, assume_yes=False, interactive=True, ask=lambda prompt: "n")
    publish_mac.confirm_run(good_run(), SHA, assume_yes=False, interactive=True, ask=lambda prompt: "y")
