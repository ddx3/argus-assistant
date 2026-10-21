"""A stand-in for a real sandbox (a container, a throwaway VM): runs the command after `--`, and leaves a mark.

Usage: python -I guardian_sandbox.py <worktree> -- <command...>

Like a real sandbox it sets up the inside itself (here: the import path of the project under test). The guardian passes
the launcher nothing that points into the untrusted worktree.
"""

import os
import pathlib
import subprocess
import sys

worktree = pathlib.Path(sys.argv[1])
command = sys.argv[sys.argv.index("--") + 1:]
assert worktree.is_dir(), "the sandbox was given a folder that does not exist"
(worktree.parent / "sandbox-was-used.txt").write_text(str(worktree), encoding="utf-8")
inside = dict(os.environ, PYTHONPATH=str(worktree / "src"))
sys.exit(subprocess.run(command, cwd=worktree, env=inside, check=False).returncode)
