"""PDF from the result: HTML -> Microsoft Edge without a window -> file.

Edge is installed on almost every Windows 10/11, and any instance can print from the
command line (`--headless --print-to-pdf`): layout from ordinary HTML and styles, system
fonts, Cyrillic out of the box, a second per page. We do not put our own PDF libraries
into the build: laying out tables and fonts by hand would turn out worse. If there is no
Edge, we say so honestly instead of staying silent.

The template page (`page`) deliberately has no letter spacing in headings: people copy
text from the PDF and search in it, and Edge puts spaced-out capitals into the file one
letter at a time, so a search for them does not work.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# How long to wait for Edge. A second per page; a minute is a reserve for a slow machine
# and for the fact that the first launch after a reboot takes a long time to bring up the
# profile.
TIMEOUT_S = 60

# For the user, no name of what we print with: what matters to them is the outcome, not
# how it was done.
NO_EDGE = "A PDF can't be made on this computer."
# On macOS there may be no browser at all (Safari cannot print from the command line), and
# the honest answer there is that one needs to be installed.
NO_BROWSER_MAC = "To make a PDF, you need the Chrome or Edge browser."
NO_BROWSER = (NO_EDGE, NO_BROWSER_MAC)
FAILED = "The PDF didn't work out — try again."
# The previous file is open in a program that does not let it be erased (readers like
# Acrobat hold the file without shared access): the new PDF would land on top of a busy
# one.
LOCKED = "This PDF is open in another program right now. Close it and try again."


def no_browser_text() -> str:
    """What to tell the person when there is nothing to print the PDF with: different for each
    system.
    """
    return NO_BROWSER_MAC if sys.platform == "darwin" else NO_EDGE

# Chromium-based browsers that can print on macOS, in order of preference:
# `--headless --print-to-pdf` is the same in all of them.
MAC_BROWSERS = (
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
)


class PdfError(Exception):
    """The PDF failed; `str(e)` is the reason for a person."""


def _mac_app_folders() -> tuple[Path, ...]:
    return Path("/Applications"), Path.home() / "Applications"


def find_edge() -> Path | None:
    """The browser we print with: Edge on Windows, any Chromium browser on macOS."""
    if sys.platform == "darwin":
        for folder in _mac_app_folders():
            for relative in MAC_BROWSERS:
                candidate = folder / relative
                if candidate.is_file():
                    return candidate
        return None
    found = shutil.which("msedge")
    if found:
        return Path(found)
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.is_file():
                return candidate
    return None


CSS = """
@page { size: A4; margin: 16mm 17mm 16mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; color: #1b1f26; font: 10.5pt/1.5 "Segoe UI", system-ui, sans-serif; }
h1 { margin: 0 0 14pt; padding-bottom: 8pt; border-bottom: 1px solid #d6dae1; font-size: 20pt; line-height: 1.15; font-weight: 600; }
h2, h3, h4, h5 { margin: 14pt 0 5pt; font-weight: 600; break-after: avoid; }
h2 { font-size: 13pt; color: #1c6f8c; }
h3 { font-size: 11.5pt; }
h4, h5 { font-size: 10.5pt; }
p { margin: 0 0 7pt; }
ul, ol { margin: 0 0 7pt; padding-left: 16pt; }
li { margin: 0 0 3pt; }
li::marker { color: #1c6f8c; }
table { width: 100%; border-collapse: collapse; margin: 0 0 8pt; font-size: 10pt; }
th { text-align: left; font-size: 9pt; color: #5c6673; font-weight: 600;
     padding: 0 8pt 4pt 0; border-bottom: 1px solid #1b1f26; }
td { padding: 5pt 8pt 5pt 0; border-bottom: 1px solid #d6dae1; vertical-align: top; }
tr { break-inside: avoid; }
blockquote { margin: 0 0 7pt; padding: 6pt 10pt; border-left: 3pt solid #1c6f8c; background: #f3f5f8; }
pre { margin: 0 0 7pt; padding: 7pt 10pt; background: #f3f5f8; font: 9pt/1.4 Consolas, ui-monospace,Menlo,monospace;
      white-space: pre-wrap; }
code { font: 9.5pt Consolas, ui-monospace,Menlo,monospace; background: #f3f5f8; padding: 0 2pt; }
hr { border: 0; border-top: 1px solid #d6dae1; margin: 10pt 0; }
"""


def page(*, title: str, body: str) -> str:
    """A ready page for printing: the title and the text of the report, nothing else. Requirement: no
    timecodes, no word "Argus", no prompt, postscript or date; only what matters, so that it is a
    real report. `body` is already safe HTML (`markdown.to_html`); the title is escaped here."""
    e = html.escape
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title><style>{CSS}</style></head>
<body>
<h1>{e(title)}</h1>
{body}
</body></html>
"""


def _render(document: str, out: Path) -> None:
    edge = find_edge()
    if edge is None:
        raise PdfError(no_browser_text())
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("cannot create the PDF folder: %s", e)
        raise PdfError(FAILED) from e
    try:
        out.unlink(missing_ok=True)
    except OSError as e:
        log.warning("cannot remove the previous PDF (%s): %s", out.name, e)
        raise PdfError(LOCKED) from e
    # `ignore_cleanup_errors`: the Edge profile in Temp is sometimes still held by a process
    # while the PDF is already ready; a cleanup failure must not take a ready file away from
    # the user.
    with tempfile.TemporaryDirectory(prefix="argus-pdf-", ignore_cleanup_errors=True) as tmp:
        source = Path(tmp) / "document.html"
        source.write_text(document, encoding="utf-8")
        command = [
            str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
            f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={out}",
            source.as_uri(),
        ]
        try:
            subprocess.run(
                command, capture_output=True, timeout=TIMEOUT_S, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as e:
            log.warning("Edge did not manage to print the PDF in time (timeout, seconds: %d)", TIMEOUT_S)
            raise PdfError(FAILED) from e
        except OSError as e:
            log.warning("Edge failed to start: %s", e)
            raise PdfError(FAILED) from e
    if not out.is_file() or out.stat().st_size == 0:
        log.warning("Edge finished, but there is no PDF file")
        raise PdfError(FAILED)


async def render(document: str, out: Path) -> None:
    """Print `document` into `out`. Edge is a separate process for a second or two, so call it from a
    thread: the event loop does not stall meanwhile."""
    await asyncio.to_thread(_render, document, out)
