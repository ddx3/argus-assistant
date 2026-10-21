"""The "Payment" tab of the settings window (2026-10-08). Requirement: a separate
billing tab instead of the account one.

Before, the balance, top-up, prices and history lived in the "Account" tab.
Now "Account" is about the person (who they are, terms, usage, data) and "Payment" is
about money. It is drawn by `account.js` (`mountBilling`), and the window attaches it
through `chrome.js` (`addBillingTab`).
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static"


def text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def account_tab_code() -> str:
    """The body of `mount` — the "Account" tab: from its declaration to the end of the
    file.
    """
    js = text("account.js")
    return js[js.index("  function mount(el, api) {"):]


def test_the_window_has_a_billing_tab_between_settings_and_account():
    chrome = text("chrome.js")
    tab = re.search(r'w\.add\(\{\s*id: "billing", title: "Payment", icon: "card", order: (\d+)', chrome)
    assert tab, "chrome.js has no \"Payment\" tab"
    account = re.search(r'id: "account", title: "Account", icon: "user", order: (\d+)', chrome)
    assert account and int(tab.group(1)) < int(account.group(1)), "\"Payment\" must come before \"Account\""
    assert int(tab.group(1)) > 300, "\"Payment\" must not wedge itself between the settings tabs"
    assert re.search(r"^\s+card: '", chrome, re.MULTILINE), "no card icon in ICONS"
    assert "addBillingTab(win)" in chrome
    # the ⚙ button opens the window on the settings, not on billing or the account
    assert re.search(r'NOT_SETTINGS = new Set\(\[[^\]]*"billing"[^\]]*\]\)', chrome)
    assert "NOT_SETTINGS.has(t.id)" in chrome
    # an address with the hash opens the window on billing
    assert 'location.hash === "#billing"' in chrome


def test_the_billing_tab_refreshes_when_it_is_shown_again():
    """Signing in on "Account" and paying in the browser change the balance while the
    tab is not visible.
    """
    chrome = text("chrome.js")
    assert "onEnter" in chrome and "w.entered" in chrome
    assert "win.entered = null" in chrome, "reopening the window does not refresh the tab"


def test_money_and_usage_moved_out_of_the_account_tab():
    mount = account_tab_code()
    for gone in ("/admin/account/checkout", "pay-tile", "balanceBlock", "ratesBlock", "historyBlock",
                 "/admin/account/history", "usageBlock", "wireChart", "/admin/account/usage"):
        assert gone not in mount, f"something foreign is left in the \"Account\" tab: {gone}"
    # what remains is about the person: the password; the "Data" block (requirement:
    # remove it) and the terms banner ("I accept") are gone
    assert "passwordBlock" in mount and "dataBlock" not in mount and "termsBanner" not in mount


def test_the_window_has_a_usage_tab_next_to_billing():
    chrome = text("chrome.js")
    usage = re.search(r'w\.add\(\{\s*id: "usage", title: "Usage", icon: "chart", order: (\d+)', chrome)
    assert usage, "chrome.js has no \"Usage\" tab"
    billing = re.search(r'id: "billing", title: "Payment", icon: "card", order: (\d+)', chrome)
    account = re.search(r'id: "account", title: "Account", icon: "user", order: (\d+)', chrome)
    assert int(billing.group(1)) < int(usage.group(1)) < int(account.group(1))
    assert "addUsageTab(win)" in chrome and 'location.hash === "#usage"' in chrome
    assert re.search(r'NOT_SETTINGS = new Set\(\[[^\]]*"usage"[^\]]*\]\)', chrome)
    assert re.search(r"^\s+chart: '", chrome, re.MULTILINE), "no chart icon in ICONS"
    js = text("account.js")
    usage_code = js[js.index("function mountUsage"):js.index("  function mount(el, api) {")]
    assert "/admin/account/usage" in usage_code and "usageBlock(state.spent, daily, el.clientWidth)" in usage_code
    assert "mountUsage" in js.split("window.argusAccount = ")[1]


def test_the_billing_component_pays_through_the_browser_and_waits_for_the_money():
    js = text("account.js")
    assert "window.argusAccount = { mount, mountBilling, mountUsage, showDoc }" in js
    billing = js[js.index("function mountBilling"):js.index("  function mount(el, api) {")]
    # payment goes through the application, in the browser, not inside the frameless window
    assert "/admin/account/checkout" in billing
    # the tab learns about the credit by itself: the balance grew
    assert "async function watch(" in billing and "now > before" in billing
    # the main button is the shared .cta, with no accent fill
    assert re.search(r'class="cta pay-go"', billing)
    # choosing an amount does not redraw the tab: otherwise the "Other amount"
    # field loses focus on every digit
    assert "function refreshChoice()" in billing
    # without sign-in or without an account server there is a clear message ("Sign-in
    # required" and "There are no accounts here"), not an empty space
    assert "Sign-in required" in billing and "There are no accounts here" in billing


def test_the_amounts_fall_back_to_the_server_limits_when_pricing_is_missing():
    js = text("account.js")
    assert "const FALLBACK = { topups: [5, 10, 20, 50], min_topup: 5, max_topup: 500 };" in js


def test_the_pay_controls_are_styled_in_chrome_css():
    css = text("chrome.css")
    for selector in (".pay-tiles", ".pay-tile", ".pay-go", ".pay-status", ".pay-sum"):
        assert selector in css, f"chrome.css has no {selector}"


def test_module_level_helpers_do_not_reach_for_a_tab_container():
    """The helpers were moved out of `mount` to module level and do not see its `el`: the chart
    width is passed to them (that is what broke during the move: `el` was not defined, and
    the chart silently was not drawn)."""
    js = text("account.js")
    shared = js[js.index("  const ICON = {"):js.index("  function mountBilling")]
    # `wireChart(el, daily)` receives the container as a parameter, so it may use `el`
    a, b = shared.index("  function wireChart(el, daily)"), shared.index("  function usageBlock(")
    shared = shared[:a] + shared[b:]
    for line in shared.splitlines():
        code = line.split("//")[0]
        assert not re.search(r"(?<![\w.])el\.", code), f"a helper touches someone else's `el`: {line.strip()}"


def test_account_and_billing_tabs_do_not_push_the_legal_links():
    """Requirement (2026-10-08): do not push the Terms of Use and Privacy Policy links, here
    they appear twice anyway. The links stayed where they cannot be omitted: at the
    registration form and in the "I accept" banner, until consent is given. In the
    data block and on the "Payment" tab they are absent."""
    js = text("account.js")
    mount = account_tab_code()
    signed_in = mount[mount.index("    function draw(state) {"):mount.index("    function drawSignIn() {")]
    assert "docLink(" not in signed_in and "withDocs(" not in signed_in and "ac-docs" not in signed_in
    billing = js[js.index("function mountBilling"):js.index("function mountUsage")]
    assert "data-doc" not in billing and "docLinkTo" not in billing and "Terms of Use" not in billing
    assert "Data and documents" not in js
    assert "ac-docs" not in text("chrome.css")
