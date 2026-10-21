"""Design-system watchdog: four greps that survive good intentions.

Three panels are made by three implementers who do not see each other. Such copies
diverge not through "let's rewrite the system" but through "I'll fix one color right in
the page, and extract it later". Nobody comes back to extract it, and a month later the
three panels look like three products.

So the boundary here is not a convention but a failing test. It lives in master and
arrives in branches with the same merge as the system itself; a branch cannot opt out
of it.

Exactly what can be checked with grep is checked, and nothing beyond that:

1. a channel page does not declare its own `:root` and contains no `<style>`,
   but links `/design/argus.css`;
2. `<channel>.css` contains only variables, not a single class selector;
3. the values of `data-state`, `data-density`, `data-phase-only` belong to the declared
   sets, and there are no literal colors in the page;
4. `.verdict` appears on the page exactly once.

What the test does not catch and cannot catch: the length of a phrase. "SILENT: the cap of
200 requests is used up" does not fit at 64 px and must become "RESERVE USED UP". This is
discipline by hand, and it is spelled out in layout_rules section 12.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static"
DESIGN = Path(__file__).resolve().parents[1] / "src" / "argus" / "design"

# The instrument (the chat's working screen), the home page with the chat list, the
# "Recording" screen (work with the session text) and the settings form are not
# design-system pages. The instrument moves to the shared stylesheet as a separate step in
# master, the form lives on its own admin.css, and the home page and "Recording" live on the
# shared chrome.css, like the instrument. The exception lifts by itself: as soon as a page
# links /design/argus.css, it falls under all the rules below (see `pages()`).
LEGACY = {"index.html", "admin.html", "home.html", "record.html"}

STATES = {"live", "work", "hold", "dead", "idle"}

# `.brief` has its own short vocabulary: the text is either not loaded, or saved, or being
# edited right now. These are not pipeline states, and there is nothing to color it with a
# state color, so the list is separate, not a sixth value added to five.
BRIEF_STATES = {"empty", "saved", "dirty"}

DENSITIES = {"instrument", "glance"}
PHASES = {"before", "live", "after"}

COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")


def pages() -> list[Path]:
    """Pages the system applies to."""
    out = []
    for path in sorted(STATIC.glob("*.html")):
        # Terms and policy are not application pages but documents that `legal/build.py` builds
        # and that also lie on the website (where there is no application system). They have their
        # own typography; in the application they open as a window over the page.
        if path.name.startswith("legal-"):
            continue
        text = path.read_text(encoding="utf-8")
        if path.name in LEGACY and "/design/argus.css" not in text:
            continue
        out.append(path)
    return out


def values(text: str, attr: str, skip: str = "") -> list[str]:
    """Attribute values are literal ones only.

    Template substitutions (`data-state="${...}"`) have nothing to check them with: their
    value comes from the snapshot. The server is responsible for them, and it has its own
    tests.
    """
    out = []
    for tag in re.findall(r"<[^<>]+>", text):
        if skip and skip in tag:
            continue
        for value in re.findall(rf'{attr}="([^"]*)"', tag):
            if "${" not in value and "{{" not in value:
                out.append(value)
    return out


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_page_uses_the_shared_stylesheet(page: Path):
    """A branch page does not create its own system — it links the shared one."""
    text = page.read_text(encoding="utf-8")
    assert "/design/argus.css" in text, f"{page.name} does not link the shared stylesheet"
    assert "<style" not in text, f"{page.name} declares its own <style>"
    assert ":root{" not in text.replace(" ", ""), f"{page.name} declares its own :root"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_page_states_are_declared(page: Path):
    """There is no sixth state, and it cannot be created even by a typo."""
    text = page.read_text(encoding="utf-8")
    assert set(values(text, "data-state", skip="brief")) <= STATES | {""}
    assert set(values(text, "data-state")) <= STATES | BRIEF_STATES | {""}
    assert set(values(text, "data-density")) <= DENSITIES
    for value in values(text, "data-phase-only"):
        assert set(value.split()) <= PHASES, f"{page.name}: phase '{value}'"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_page_has_no_literal_colors(page: Path):
    """Color lives in one file. A literal in a page is the start of divergence."""
    text = re.sub(r"<!--.*?-->", "", page.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert not COLOR.search(text), f"{page.name}: literal color"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_one_verdict_per_page(page: Path):
    """A second verdict on a page is two different answers to one question."""
    text = page.read_text(encoding="utf-8")
    assert text.count('class="verdict"') == 1, f"{page.name}: there is not exactly one verdict"


def test_channel_css_declares_only_variables():
    """A branch owns variables, not classes.

    As soon as a branch declares `.verdict--big`, the system is over: the same component
    starts to look different on three surfaces, and it can no longer be fixed with one file.
    """
    # The shared stylesheets of master do not belong here: the rule is about a branch, and
    # `admin.css` and `chrome.css` belong to the core and live by their own laws.
    shared = {"admin.css", "chrome.css"}
    for path in sorted(STATIC.glob("*.css")):
        if path.name in shared:
            continue
        body = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        selectors = re.findall(r"([^{}]*)\{", body)
        for selector in selectors:
            name = selector.strip()
            assert name.startswith(":root"), f"{path.name}: selector '{name}'"
        for line in re.findall(r"\{([^{}]*)\}", body):
            for decl in line.split(";"):
                decl = decl.strip()
                assert not decl or decl.startswith("--"), f"{path.name}: '{decl}'"


# --- what the guard checks today -------------------------------------------------------------------------------
#
# All four application pages are in LEGACY and do not link `/design/argus.css`, so the
# parametrized tests above report "empty parameter set" and are skipped (audit of
# 2026-10-14, css-design-13): they will switch on by themselves as soon as a page moves
# from the shared `chrome.css` stylesheet to the system. Until then the boundary is held
# by what is already shared: the tokens of `chrome.css` and of the system do not diverge,
# pages do not declare their own copies of the `chrome.css` tokens, and the highlight along
# the top edge is drawn nowhere.

SHIPPED = ("index.html", "home.html", "record.html", "admin.html")
# Layout tokens differ between the system and `chrome.css` on purpose (each has its own
# grid); we compare only the look.
LAYOUT_TOKENS = {"--pad"}


def root_tokens(css: str) -> dict[str, str]:
    """Declarations `--name: value` from ordinary `:root` blocks (the light theme and rules
    inside selectors do not count).
    """
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out: dict[str, str] = {}
    for block in re.finditer(r"(?:^|\n)\s*:root\s*\{([^{}]*)\}", body):
        for decl in block.group(1).split(";"):
            name, _, value = decl.partition(":")
            if name.strip().startswith("--"):
                out[name.strip()] = " ".join(value.split())
    return out


def test_the_shared_visual_tokens_do_not_drift_between_chrome_and_the_design_system():
    """`chrome.css` (what the app lives by) and `design/argus.css` (the system, the showcase)
    declare the same buttons, glass and edges. A divergence of values is exactly the "I'll
    fix one color right here, extract it later" that the watchdog exists to stop."""
    chrome = root_tokens((STATIC / "chrome.css").read_text(encoding="utf-8"))
    system = root_tokens((DESIGN / "argus.css").read_text(encoding="utf-8"))
    common = (set(chrome) & set(system)) - LAYOUT_TOKENS
    assert len(common) >= 15, f"{len(common)} shared tokens left: the test no longer compares anything"
    drift = {name: (chrome[name], system[name]) for name in sorted(common) if chrome[name] != system[name]}
    assert not drift, f"tokens drifted apart (chrome.css, argus.css): {drift}"


def test_shipped_pages_use_chrome_css_and_do_not_redefine_its_tokens_differently():
    """An application page links `/chrome.css` and does not declare in its own `<style>` tokens
    that it already sets (`--btn`, `--win`, `--focus`…), otherwise a second copy with a
    different value would silently recolor the page. A repeat with the same value (`--meta`
    is declared both on the main page and on "Recording", and in `chrome.css`) is
    harmless and allowed."""
    owned = root_tokens((STATIC / "chrome.css").read_text(encoding="utf-8"))
    for name in SHIPPED:
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "/chrome.css" in text, f"{name} does not link chrome.css"
        style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.DOTALL))
        assert style or name == "admin.html", f"{name}: its own <style> is gone: the test no longer checks anything"
        again = {k: v for k, v in root_tokens(style).items() if k in owned and v != owned[k]}
        assert not again, f"{name} declares chrome.css tokens its own way: {again} (in chrome.css: { {k: owned[k] for k in again} })"


def test_no_top_edge_highlight_is_painted_anywhere():
    """`chrome.css` (the `:root` block) says that the highlight on the top edge of glass
    surfaces — an inset shadow on top of the ordinary border — has been removed across the
    whole project. In `design/argus.css` it remained (`.band`, `.sheet`, the selected segment
    `.dial__seg`): the showcase showed something the app does not have."""
    highlight = re.compile(r"inset\s+0\s+1px\s+0|0\s+1px\s+0\s+[^;,}]*\binset\b", re.IGNORECASE)
    files = [STATIC / "chrome.css", STATIC / "admin.css", DESIGN / "argus.css"] + [STATIC / name for name in SHIPPED]
    for path in files:
        body = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        assert not highlight.search(body), f"{path.name}: highlight along the top edge"


def test_the_system_itself_is_where_it_belongs():
    """The system lives in its own package, not in a branch's static files.

    A file inside `ui/static/` looks like a branch file and sooner or later will be touched
    by a branch "for five minutes". A separate package makes the ownership visible to the
    eye.
    """
    assert (DESIGN / "argus.css").exists()
    assert (DESIGN / "panel.js").exists()
    assert (DESIGN / "specimen.html").exists()
    assert not list(STATIC.glob("argus.css"))


def test_specimen_shows_every_component():
    """The showcase is not decoration but the place a component must land first.

    A convention without a showcase holds nothing: there is nowhere else to check the light
    theme and both densities, which means nobody checks them.
    """
    text = (DESIGN / "specimen.html").read_text(encoding="utf-8")
    for block in ("verdict", "utterance", "lamps", "checks", "budget", "keys",
                  "dial", "target", "log", "brief", "overlay"):
        assert f'class="{block}' in text or f' {block}"' in text, f"no {block}"
    for state in STATES:
        assert f'data-state="{state}"' in text, f"no state {state}"
    for density in DENSITIES:
        assert f'data-density="{density}"' in text


@pytest.mark.xfail(
    strict=True,
    reason="owner's decision: no page has moved to /design/argus.css (audit of 2026-10-14, fe-pages-17)",
)
def test_some_page_uses_the_design_system():
    """The design system is needed only while a page uses it. Right now every page is in
    `LEGACY` and none links `/design/argus.css`, so the four checks above get an empty list
    and are silently skipped, and nobody executes `panel.js` and `argus.css` (they are served
    only to the showcase).

    Hence an explicit "not yet" here: the test waits for a decision (move one real page to
    the system or delete the package `argus/design` together with these checks). As soon as a
    page links the stylesheet, the test will unexpectedly pass and will require removing the
    `xfail` mark — and the four checks above will start working on that page by themselves."""
    assert pages(), "no page uses the shared design system"
