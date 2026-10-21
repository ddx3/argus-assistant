"""Design system: one stylesheet and one runtime for all surfaces.

It lives **outside** `ui/static/`, and this is not about merging: a merge quarrels over
files, not over folders. The point is different: a file inside `ui/static/` looks like a
branch's file and sooner or later a branch will fix it "for five minutes". A separate
package makes ownership visible to the eye: everything here is edited only by master,
and a branch reads and links.

We serve the files by our own route, not `StaticFiles`, for the sake of one header:
`Cache-Control: no-store`. The reason is already described in `ui/server.py`: the
browser kept an old version, and the person saw buttons that are not in the code. On a
**shared** stylesheet a stuck cache costs three times as much: it lies in four places
at once.

The path is computed from the module, not from the current folder: there are four
working copies, their roots differ, and a relative path would break in three of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from argus.live import live

if TYPE_CHECKING:
    from fastapi import FastAPI

# As with the dashboard pages: if the sources lie next to the build, we read from them.
# Otherwise the shared stylesheet would freeze in the build while the pages around it live
# on, and the divergence would show up not as an "old file" but as a broken layout.
# Details in `argus/live.py`.
DIR = live(Path(__file__).parent)

# The list is closed: the route serves files from disk by the name in the address, and
# without a list "argus.css" easily turns into "../../argus.toml".
FILES = {
    "argus.css": "text/css",
    "panel.js": "application/javascript",
    "specimen.html": "text/html; charset=utf-8",
}

NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def mount(web: FastAPI) -> None:
    """Mount /design/{name} on the dashboard app."""
    from fastapi import Response

    @web.get("/design/{name}")
    async def design_file(name: str) -> Response:
        media = FILES.get(name)
        if media is None:
            return Response(status_code=404)
        path = DIR / name
        if not path.exists():
            return Response(status_code=404)
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type=media,
            headers=NO_CACHE,
        )
