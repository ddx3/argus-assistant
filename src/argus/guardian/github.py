"""Opening a draft pull request. That is all the guardian does on GitHub.

The token comes from the environment (`GUARDIAN_GITHUB_TOKEN`) and is used only by this function,
never given to the model. There is no merge call here and there never should be: a person merges.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable


def _post(url: str, token: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "argus-guardian/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class GitHub:
    def __init__(self, repo: str, token: str = "", post: Callable[[str, str, dict], tuple[int, dict]] = _post) -> None:
        self.repo = repo
        self.token = token or os.environ.get("GUARDIAN_GITHUB_TOKEN", "")
        self._post = post

    @property
    def can_open_pull_requests(self) -> bool:
        return bool(self.repo and self.token)

    def compare_url(self, base: str, head: str) -> str:
        return f"https://github.com/{self.repo}/compare/{base}...{head}?expand=1"

    def open_draft_pull_request(self, *, base: str, head: str, title: str, body: str) -> str:
        """Returns the pull request's URL, or raises RuntimeError with a short reason."""
        if not self.can_open_pull_requests:
            raise RuntimeError("no GitHub token configured")
        status, data = self._post(f"https://api.github.com/repos/{self.repo}/pulls", self.token,
                                  {"title": title, "head": head, "base": base, "body": body, "draft": True})
        if status not in (200, 201) or "html_url" not in data:
            raise RuntimeError(f"GitHub answered {status}: {str(data.get('message', ''))[:200]}")
        return str(data["html_url"])
