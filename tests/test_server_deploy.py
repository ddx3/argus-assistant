"""The server layout in the repository: what gets installed, what it is started with, and
what does not get into the image.

The tests build and run nothing (Docker is not needed here): they check files. They catch
what would otherwise come out in production at deployment: dependencies that drifted from
the ones the tests run on; recognition sockets without the needed library; an image that
got `.env` in; a copy of compose that wipes out the landing.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "deploy" / "server"

# The packages the image exists for; the rest are their dependencies.
ROOTS = ("fastapi", "uvicorn", "pydantic", "httpx", "anthropic", "openai", "websockets")


def _name(raw: str) -> str:
    return re.sub(r"[-_.]+", "-", raw).lower()


def pins() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (SERVER / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+!-]*)", line)
        assert match, f"not pinned to an exact version: {line!r}"
        out[_name(match.group(1))] = match.group(2)
    return out


def lock() -> dict[str, dict]:
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {_name(p["name"]): p for p in data["package"]}


def closure(packages: dict[str, dict]) -> dict[str, str]:
    """The roots and everything they pull in on Linux with Python 3.12, at the versions from
    `uv.lock`.
    """
    env = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
           "python_version": "3.12", "python_full_version": "3.12.13",
           "platform_machine": "x86_64", "implementation_name": "cpython",
           "platform_python_implementation": "CPython", "extra": ""}
    seen: dict[str, str] = {}
    stack = list(ROOTS)
    while stack:
        name = _name(stack.pop())
        if name in seen:
            continue
        seen[name] = packages[name]["version"]
        for dep in packages[name].get("dependencies", []):
            marker = dep.get("marker")
            if not marker or Marker(marker).evaluate(env):
                stack.append(dep["name"])
    return seen


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def test_every_dependency_is_pinned_to_the_version_the_tests_run_on():
    packages = lock()
    for name, version in pins().items():
        assert name in packages, f"{name} is not in uv.lock"
        assert packages[name]["version"] == version, (
            f"{name}: the image has {version}, but the tests run on {packages[name]['version']}")


def test_nothing_the_server_needs_is_left_to_float():
    """Every dependency of the roots is pinned, and nothing extra is pinned."""
    wanted = closure(lock())
    have = pins()
    assert set(wanted) == set(have), (
        f"missing: {sorted(set(wanted) - set(have))}; extra: {sorted(set(have) - set(wanted))}")


def test_the_speech_relay_gets_its_websocket_library_directly():
    """`relay_stt.py` imports `websockets.asyncio.client` (version 13+ is needed): it is named
    directly in requirements instead of arriving by chance through the uvicorn extra."""
    version = pins()["websockets"]
    assert int(version.split(".")[0]) >= 13
    text = (SERVER / "requirements.txt").read_text(encoding="utf-8")
    assert "uvicorn[standard]" not in text


# --------------------------------------------------------------------------
# Image
# --------------------------------------------------------------------------


def test_the_image_copies_only_the_server_and_does_not_run_as_root():
    text = (SERVER / "Dockerfile").read_bytes().decode("utf-8")
    assert "\r" not in text, "Windows line endings in the Dockerfile"
    assert not re.search(r"^\s*(COPY|ADD)\s+\.\s", text, re.MULTILINE), "the whole tree goes into the image"
    copied = re.findall(r"^COPY\s+(\S+)\s", text, re.MULTILINE)
    assert sorted(copied) == ["deploy/server/requirements.txt", "src/argus/__init__.py",
                              "src/argus/server"]
    assert "setpriv --reuid=10001" in text and "--clear-groups" in text
    assert "ENTRYPOINT" in text and "chown" in text, "the volume with the database belongs to root"
    assert not re.search(r"^USER\s+root", text, re.MULTILINE)


def test_the_image_checks_that_the_server_is_alive():
    text = (SERVER / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text and "/pricing" in text
    assert "--timeout" in text and "--retries" in text


def test_the_build_context_lets_through_only_what_the_dockerfile_copies():
    lines = [ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines[0] == "*", "everything is closed first"
    allowed = [ln[1:] for ln in lines if ln.startswith("!")]
    assert sorted(allowed) == ["deploy/server/requirements.txt", "src/argus/__init__.py",
                               "src/argus/server"]
    for secret in (".env", "chats", "sessions", "models", "dist", "_offline"):
        assert not any(secret in item for item in allowed), secret


# --------------------------------------------------------------------------
# compose
# --------------------------------------------------------------------------


def _landing_script():
    spec = importlib.util.spec_from_file_location(
        "publish_landing_for_test", ROOT / "deploy" / "landing" / "publish_landing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repo_compose_has_the_service_that_runs_in_production():
    """This used to hold a retired layout: uploading it to the server wiped out the landing page
    and port 80. The `downloads` service in the file must match what `publish_landing --setup`
    installs."""
    text = (SERVER / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    patched = _landing_script().patch_compose(text)
    assert patched.rstrip("\n") == text.rstrip("\n"), "the downloads service diverged from COMPOSE_BLOCK"
    assert '"80:80"' in text and '"8080:80"' in text
    assert "./sites:/usr/share/nginx/sites:ro" in text


def test_the_server_service_has_a_memory_ceiling_and_keeps_secrets_out_of_the_file():
    text = (SERVER / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+mem_limit:\s*\S+", text, re.MULTILINE)
    assert "env_file" in text and ".env" in text
    for secret in ("sk-", "OPENAI_API_KEY=", "ARGUS_SERVER_SECRET="):
        assert secret not in text


def test_the_retired_download_page_says_it_is_retired():
    page = (SERVER / "downloads" / "index.html").read_text(encoding="utf-8")
    assert "REMOVED" in page.split("<html", 1)[0]
    assert (SERVER / "README.md").exists()


def test_every_operator_command_the_readme_names_exists():
    text = (SERVER / "README.md").read_text(encoding="utf-8")
    for name in re.findall(r"python3 -m argus\.server\.(\w+)", text):
        assert importlib.util.find_spec(f"argus.server.{name}"), name
