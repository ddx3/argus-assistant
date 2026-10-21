# Terms and privacy policy

The original is in **English**; the translations into the other eight app languages (ru uk es pt de fr tr pl) lie next to it, in folders.
Decision of 2026-10-15: localize the terms and the policy into all languages (until then they existed only in English: a Russian legal text
without a lawyer is worse than one honest language). **The project has no lawyer, and the translations have not been checked by one**: they were made for
ease of reading, and each of them says that the English version prevails if there is a discrepancy. People from different countries read the translations, but there is nobody to check the meaning
against the law of their country; when a real operator appears, show the documents to a lawyer.

    legal/text/terms.html       Terms of Use (the original, English)
    legal/text/privacy.html     Privacy Policy (the original)
    legal/text/<lang>/*.html    translations: ru uk es pt de fr tr pl (the same names, the same markup)
    legal/legal_render.py       languages, dates, page labels, assembling the articles (shared by build.py and landing/subpages.py)
    legal/validate_translation.py  a sieve for translations: markup, numbers, substitutions, leftover English, script
    legal/template.html         the page shell
    legal/operator.json         who runs the service: name, email, law (a personal name and a country are NOT written — an owner's decision)
    legal/build.py              the build

## Build

    python legal/build.py              # build, show what is missing
    python legal/build.py --release    # the same, but fail on gaps

The same page is put in two places:

- `deploy/server/downloads/{terms,privacy}.html` — next to the download page. The landing page and the download page lead here:
  the person reads the document in a browser.
- `src/argus/ui/static/legal-{terms,privacy}.html` — inside the app. The app shows
  its own copy in a window over the page (`argusAccount.showDoc` in `account.js`, the address
  `/p/legal-<name>`) and does not send the person to a browser. It works without a network and shows exactly the text
  under which the person pressed "Create account". PyInstaller takes it together with the whole
  `ui/static`. The documents deliberately have no Python route of their own: the general `/p/<name>` is already in
  every build, so a document works as soon as the file is there, with no `.exe` rebuild.

`tests/test_legal_docs.py` makes sure that both copies are exactly what the build gives now (from `legal/text/*`,
`operator.json`, and `TERMS_VERSION`): editing a text without `python legal/build.py` fails the test. The links between documents
(`privacy.html` ↔ `terms.html`, "Download Argus") are rewritten by the script in `template.html` to the `/p/…` address,
both in the app window (`?embed=1`) and in a regular tab; on the website they stay as they are.

## What to fill in before distribution

`operator.json` — `name`, `email`, `country`, `law` (in English). While a field is empty, the
document shows a yellow note. `--release` fails on an empty field, and `packaging/stage.py`
prints a warning if the build was made with gaps.

## When to change the version

`TERMS_VERSION` in `src/argus/account/legal.py` is the date of the edit. Raise it when the **meaning** changes
(new data, a new recipient, new payment rules), not for a comma. A new version:

- stands in the header of the documents themselves ("Version of …") and is recorded for new registrations (the server stores
  it in `accounts.terms_version`);
- **notifies nobody**: the banner "The terms apply… I accept" was removed (2026-10-08), the route
  `/admin/account/terms` is called by no page, and the server compares the version nowhere. So the documents
  (terms, section 13; policy, section 10) say what can actually be done: the new text lies at the same
  address on the site, and for material changes the operator writes to people by email. There is no mailing in the code (in `server/`
  there are only password reset emails, `server/mailer.py`); if you raise the version because of a change of meaning, write to the owners of the
  accounts yourself and rebuild the pages before publishing;
- is pinned in `tests/test_legal_docs.py` (`PINNED_VERSION`, `PINNED_SHA256`) together with a hash of the text: editing
  `legal/text/*` without a decision on the version fails the test, and the same test says what to write down.

The notice before the first time recording is turned on (`NOTICE_VERSION`) is a separate version: raising it
means asking everyone again.

## What in the texts depends on the code

If the behavior changes, the text is edited too:

- which data the server stores (`server/db.py`: `accounts`, `credits`, `orders`, `usage`);
- the program on two systems: on Windows the data lies next to `Argus.exe`, on macOS in
  `~/Library/Application Support/Argus` (the log in `~/Library/Logs/Argus`, `host.py`); uninstalling the app does not
  erase the data on a Mac, and the policy (section 2.3) speaks of both; the terms (sections 1, 8) name both systems
  and the caveat about macOS 15 and newer (`docs/macos.md`);
- the update check: at launch (`update.maybe_self_update`) and then every 30 minutes (`update_service.py`; the first
  request from a running app comes after 2 minutes), a request without a version and without account data (`update.fetch_latest`) —
  policy, section 2.5; a test checks the period against the code;
- deleting an account by email, not by a button: the terms (sections 2, 12, 13) point to the same address as the policy;
- that the relay does **not** keep the content (`server/relay.py`);
- the buttons in the app: "Download my data" / "Delete account" were removed (2026-10-08), the policy points to an email to the operator; a test checks both directions;
- "no subscription, top-up from $5" (`server/pricing.py`, a test checks this);
- log retention is limited (`deploy/server/docker-compose.yml`, `logging`);
- the audio goes to the speech recognition service through our server (the relay `server/relay_stt.py`, since 2026-10-14) and is
  stored nowhere, neither on disk nor in the log (guarded by `test_what_was_said_is_never_written_to_the_log`);
  section 2.2 of the policy says exactly this. The terms version was not raised for this: the decision is the owner's.

## Requests for a copy of data and for deletion (policy, section 6)

There are no buttons in the app; the policy points to an email to the operator and promises an answer within 30 days. The code for
export and deletion (`accounts.export_data`, `accounts.delete_account`) requires the token and password of
the person, which the operator does not have, so there are two commands on the server (`docker exec`, no password
needed; the confirmation is an email from the account's address):

    docker exec argus-server python3 -m argus.server.export email@example.com > copy.json
    docker exec argus-server python3 -m argus.server.erase email@example.com          # shows what will be erased
    docker exec argus-server python3 -m argus.server.erase email@example.com --yes    # erases

`erase` does the same as the "Delete account" button: it erases the email, the password hash, the logins, and the recovery requests, and it re-binds
the money and the usage to `-id` (the money report must still add up, and by law orders are kept longer than the account). The number of the account itself is never given to anyone again
(`AUTOINCREMENT` in `server/db.py`), so a new person will not see someone else's history. The remaining balance
disappears together with the account. Database backups, if they are made, are erased within the period from the policy
(30 days). Do not write `DELETE FROM accounts` by hand.

## What was decided here on the owner's behalf and needs confirming

This is a draft, not legal advice. The defaults chosen; check them by eye:

- age 18+;
- a refund of the unspent part of a top-up within 14 days, the gift is not refunded;
- liability is limited to the amount of payments over 12 months;
- a ban on using it where help is forbidden (exams, interviews, the rules of an
  organization), and shifting responsibility for the interlocutors' consent to being recorded onto the user;
- database backups are erased no later than 30 days later (if they are made at all).
