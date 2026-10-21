"""The few git operations the guardian needs, as plain functions over `deps.run`.

Every call is an argument list, so a branch name or a path that came from a model can never become
shell syntax. Work always happens in a throwaway worktree next to the guardian's own folder; the
developer's checkout is only read (fetched, never modified).

Git is also a place where code can hide (hooks, a file-system monitor, an external diff driver), so
every call switches those off (`-c` options, `--no-ext-diff`) and points `core.hooksPath` at an empty folder.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from argus.guardian.deps import Deps, Result
from argus.guardian.policy import FileChange

BRANCH_OK = re.compile(r"^guardian/[A-Za-z0-9._-]{1,80}$")
AUTHOR = ("Argus Guardian", "guardian@users.noreply.github.com")


class GitError(Exception):
    pass


def _safe_options() -> list[str]:
    empty = Path(tempfile.gettempdir()) / "argus-guardian-no-hooks"
    empty.mkdir(exist_ok=True)
    return ["-c", f"core.hooksPath={empty}", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
            "-c", "protocol.ext.allow=never"]


def git(deps: Deps, cwd: Path, *args: str, timeout: float = 120) -> Result:
    return deps.run(["git", *_safe_options(), *args], cwd=cwd, timeout=timeout)


def must(result: Result, what: str) -> str:
    if not result.ok:
        raise GitError(f"{what} failed: {(result.err or result.out).strip()[:300]}")
    return result.out


def fetch(deps: Deps, repo: Path, remote: str) -> None:
    must(git(deps, repo, "fetch", "--quiet", remote), "git fetch")


def rev(deps: Deps, repo: Path, ref: str) -> str:
    return must(git(deps, repo, "rev-parse", "--verify", ref), f"git rev-parse {ref}").strip()


def add_worktree(deps: Deps, repo: Path, path: Path, ref: str, branch: str | None = None) -> None:
    if path.exists():
        remove_worktree(deps, repo, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch is None:
        must(git(deps, repo, "worktree", "add", "--detach", str(path), ref), "git worktree add")
    else:
        if not BRANCH_OK.match(branch):
            raise GitError(f"refusing odd branch name {branch!r}")
        must(git(deps, repo, "worktree", "add", "-b", branch, str(path), ref), "git worktree add")


def remove_worktree(deps: Deps, repo: Path, path: Path) -> None:
    git(deps, repo, "worktree", "remove", "--force", str(path))
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(deps, repo, "worktree", "prune")


def delete_branch(deps: Deps, repo: Path, branch: str) -> None:
    """Drop a local guardian branch that led nowhere (the remote copy, if any, is not touched)."""
    if BRANCH_OK.match(branch):
        git(deps, repo, "branch", "-D", branch)


def stage_everything(deps: Deps, worktree: Path) -> None:
    must(git(deps, worktree, "add", "-A"), "git add")


def _range(base_ref: str, target: str | None) -> list[str]:
    """Arguments that select what to compare: the staged index against `base_ref`, or `base_ref..target`."""
    return [base_ref] if target is None else [base_ref, target]


def changes(deps: Deps, worktree: Path, base_ref: str, target: str | None = None) -> list[FileChange]:
    """What changed against `base_ref`, as git itself reports it.

    With no `target` the worktree is staged first and the index is compared. With a `target` (a commit) the two
    commits are compared, which is how a finished commit is checked a second time.
    """
    flags = ["--cached"] if target is None else []
    if target is None:
        stage_everything(deps, worktree)
    numstat = must(git(deps, worktree, "diff", "--no-ext-diff", "--no-textconv", *flags, "--numstat", "-M", *_range(base_ref, target)), "git diff --numstat")
    status = must(git(deps, worktree, "diff", "--no-ext-diff", "--no-textconv", *flags, "--name-status", "-M", *_range(base_ref, target)), "git diff --name-status")
    stats: dict[str, tuple[int, int, bool]] = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            a, r, path = parts[0], parts[1], parts[-1]
            binary = a == "-" or r == "-"
            stats[path] = (0 if binary else int(a), 0 if binary else int(r), binary)
    out: list[FileChange] = []
    for line in status.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1]
        added, removed, binary = stats.get(path, (0, 0, False))
        out.append(FileChange(path=path.replace("\\", "/"), status=parts[0], added=added, removed=removed, binary=binary))
    return out


def added_lines(deps: Deps, worktree: Path, base_ref: str, target: str | None = None) -> dict[str, list[str]]:
    """The text each file gained, from a zero-context diff."""
    flags = ["--cached"] if target is None else []
    patch = must(git(deps, worktree, "diff", "--no-ext-diff", "--no-textconv", *flags, "-U0", "-M", *_range(base_ref, target)), "git diff")
    result: dict[str, list[str]] = {}
    current = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            current = name.removeprefix("b/")
            result.setdefault(current, [])
        elif line.startswith("+") and not line.startswith("+++") and current:
            result[current].append(line[1:])
    return result


def commit(deps: Deps, worktree: Path, message: str) -> str:
    name, email = AUTHOR
    must(git(deps, worktree, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "--no-verify",
             "-m", message, timeout=60), "git commit")
    return must(git(deps, worktree, "rev-parse", "HEAD"), "git rev-parse").strip()


def push(deps: Deps, worktree: Path, remote: str, branch: str) -> None:
    if not BRANCH_OK.match(branch):
        raise GitError(f"refusing odd branch name {branch!r}")
    # `--force` is never used: a guardian branch that already exists on the remote is a conflict to look at.
    must(git(deps, worktree, "push", "--no-verify", "--set-upstream", remote, f"{branch}:{branch}", timeout=180), "git push")
