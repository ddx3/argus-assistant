"""Test session "Test: answer formats", for the eyes: how the work
screen renders everything the model writes.

    python docs/make_format_test_chat.py              # into the build's chats (dist/Argus/chats)
    python docs/make_format_test_chat.py path/to/folder/chats   # or into any other folder

What is inside is `SCENARIOS`: each answer tests one thing (a table, nested lists, code,
formulas, service reasons, a long question…). The lines and answers are a ready-made session
history: open it on the home screen and scroll through the answers. Nobody queries the model,
so it costs nothing. A repeated run re-creates the session with the same title.

The same texts are run by `tests/test_answer_markdown.py` through a real page parse (headless
Edge): none of them must leave raw markup on the screen.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

NAME = "Test: answer formats"
FENCE = "`" * 3

# (what it checks, the question, the answer, extra fields of the record)
SCENARIOS: list[tuple[str, str, str, dict]] = [
    (
        "table: column alignment, code and bold in cells",
        "Compare threads and processes",
        "\n".join([
            "## Threads and processes",
            "",
            "| Approach | Memory | Downsides |",
            "|:--|:--:|--:|",
            "| Threads | shared | **races**, `deadlock` |",
            "| Processes | separate | expensive startup |",
            "| Coroutines | shared | *one thread* |",
            "",
            "In short: threads are for waiting on I/O, processes are for real parallelism.",
        ]),
        {},
    ),
    (
        "wide table: must scroll sideways inside the frame",
        "Give me a summary of the plans in a table",
        "\n".join([
            "| Plan | Price per month | Storage | Users | Support | API limit | SLA | Backups | Encryption |",
            "|---|---|---|---|---|---|---|---|---|",
            "| Starter | $5 | 10 GB | 1 | email | 1,000 requests | none | weekly | yes |",
            "| Team | $20 | 100 GB | 10 | chat | 50,000 requests | 99.5% | daily | yes |",
            "| Business | $90 | 1 TB | 100 | phone | unlimited | 99.95% | hourly | yes, own keys |",
        ]),
        {},
    ),
    (
        "table without outer vertical bars and with a short row",
        "What about the deadlines?",
        "\n".join([
            "Task | Due | Who",
            "--- | --- | ---",
            "Release 4.2 | October 14 | Sergey",
            "Payment tests | October 11 |",
        ]),
        {},
    ),
    (
        "nested and numbered lists, task list",
        "What needs to be done before the release?",
        "\n".join([
            "**Plan before the release:**",
            "1. Preparation",
            "   - close the critical bugs",
            "   - update the documentation",
            "     - README",
            "     - release notes",
            "2. Verification",
            "   - run the regression",
            "3. Deployment",
            "",
            "**Checklist:**",
            "- [x] branch frozen",
            "- [x] tests green",
            "- [ ] release notes",
            "- [ ] notify the customer",
        ]),
        {},
    ),
    (
        "numbering from the middle and switching the list kind",
        "Continue from the third item",
        "\n".join([
            "3. Third step — check access.",
            "4. Fourth step — agree on the deadlines.",
            "",
            "And separately, without numbers:",
            "- risk 1: not enough testers",
            "- risk 2: changing requirements",
        ]),
        {},
    ),
    (
        "quote, headings of different levels, horizontal rule",
        "Recap what was decided at the meeting",
        "\n".join([
            "# Meeting summary",
            "",
            "## Decisions",
            "> We move the release to **October 14**.",
            "> The testing budget stays the same.",
            "",
            "---",
            "",
            "### Open questions",
            "We need to clarify access to the test environment.",
        ]),
        {},
    ),
    (
        "code: block with a language, block without one, a long line and `code` in a line",
        "Show an example in Python and the command to run it",
        "\n".join([
            "Here is the function, `await` is not needed here:",
            "",
            FENCE + "python",
            "def has_duplicates(items):",
            "    seen = set()",
            "    for x in items:",
            "        if x in seen:",
            "            return True",
            "        seen.add(x)",
            "    return False",
            FENCE,
            "",
            "To run it:",
            "",
            FENCE,
            "python -m pytest tests/test_answer_markdown.py -q --maxfail=1 --disable-warnings --tb=short -p no:cacheprovider --durations=10",
            FENCE,
        ]),
        {},
    ),
    (
        "formulas: block, long, and inside a phrase; dollars are not a formula",
        "Derive the formula and compare it with the price",
        "\n".join([
            "Mass-energy equivalence:",
            "",
            "$$E = mc^2$$",
            "",
            "Inside a phrase: the area of a circle \\(S = \\pi r^2\\), and also \\(\\frac{a}{b}\\).",
            "",
            "A long one:",
            "",
            "$$\\sum_{i=1}^{n} a_i = a_1 + a_2 + a_3 + a_4 + a_5 + a_6 + a_7 + a_8 + a_9 + a_{10} + a_{11} + a_{12} + a_{13} + a_{14} + a_{15} + a_{16}$$",
            "",
            "Price: from $5 to $10 a month — this is not a formula.",
        ]),
        {},
    ),
    (
        "inline markup: bold, italic, strikethrough, link, symbols without markup",
        "Show all kinds of emphasis",
        "\n".join([
            "**bold**, *italic*, ***both at once***, ~~strikethrough~~, `code`, [link](https://example.com/docs).",
            "The word snake_case_name and the number 2*3*4 stay as they are; \\*asterisks in a frame\\* too.",
            "The second line of the same paragraph — with a line break.",
        ]),
        {},
    ),
    (
        "safety: foreign markup is shown as text",
        "What happens if the answer contains HTML?",
        "<script>alert(1)</script> <img src=x onerror=alert(1)> [click](javascript:alert(1))\n\n"
        "| <b>a</b> | <i>b</i> |\n|---|---|\n| <u>c</u> | d |",
        {},
    ),
    (
        "service reason \"asked manually\": there is no \"question\" block",
        "asked manually",
        "Not enough context: the other person's question was not heard.",
        {"trigger": "hotkey", "reason": "asked manually"},
    ),
    (
        "service reason about the screen and silence: there is no \"question\" block either",
        "12 new lines on screen",
        "A list of build errors appeared on the screen: three warnings and one error in `build.py`.",
        {"trigger": "screen", "reason": "12 new lines on screen"},
    ),
    (
        "a very long question: the arrow expands and collapses it",
        "Explain how a process differs from a thread and when to choose multithreading and when asynchrony, "
        "using the example of a server that takes thousands of requests per second and goes to a database and to external services",
        "For waiting on I/O, **asynchrony** is enough: one thread serves thousands of connections. "
        "Multithreading is needed when there are blocking calls that cannot be rewritten.",
        {},
    ),
    (
        "\"think longer\": the answer has no label, the size does not change",
        "Think longer: is it worth moving to microservices?",
        "**In short: not yet.**\n\n- the team is under ten people;\n- there are no boundaries between domains;\n"
        "- there is no mature deployment.\n\nFirst a modular monolith, then split out services as needed.",
        {"deep": True},
    ),
    (
        "a one-word answer",
        "Are you there?",
        "Yes.",
        {},
    ),
    (
        "a mix: heading, table, list and code in one answer",
        "Give a short testing report",
        "\n".join([
            "## Report",
            "",
            "We ran **three** suites:",
            "",
            "| Suite | Total | Failed |",
            "|---|--:|--:|",
            "| Unit | 412 | 0 |",
            "| Integration | 58 | 2 |",
            "| E2E | 14 | 1 |",
            "",
            "Failed:",
            "- `test_payment_refund` — timeout;",
            "- `test_login_flow` — button not found.",
            "",
            FENCE + "bash",
            "pytest tests/ -k 'refund or login' -x",
            FENCE,
        ]),
        {},
    ),
]

SPEECH = [
    ("them", "Good afternoon! Let's go through the release preparation."),
    ("me", "Yes, I hear you well. Let's start with testing."),
    ("them", "Please show the summary, and what about the deadlines?"),
    ("me", "I'll put it together now."),
]


def build(chats_dir: Path, now: float | None = None) -> Path:
    """Creates a session in `chats_dir`; removes an earlier one with the same name."""
    now = now or time.time()
    chats_dir.mkdir(parents=True, exist_ok=True)
    for old in chats_dir.glob("*/chat.json"):
        try:
            if json.loads(old.read_text(encoding="utf-8")).get("name") == NAME:
                shutil.rmtree(old.parent, ignore_errors=True)
        except (OSError, ValueError):
            continue

    chat_id = uuid.uuid4().hex[:10]
    folder = chats_dir / chat_id
    folder.mkdir()
    start = now - 60 * (len(SCENARIOS) + len(SPEECH)) - 30

    rows = []
    t = start
    for who, text in SPEECH:
        t += 6
        rows.append({"k": "s", "ts": t, "src": who, "text": text})
    for _title, cause, text, extra in SCENARIOS:
        t += 45
        rows.append({
            "k": "a", "ts": t, "text": text, "cause": cause, "trigger": "auto", "reason": "question",
            "provider": "assist", "model": "", "latency_ms": 1300, "deep": False, "image_kb": 0, "image_src": "screen",
            **extra,
        })
    (folder / "history.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    (folder / "chat.json").write_text(json.dumps({
        "id": chat_id, "name": NAME, "name_state": "manual", "created": now, "updated": now,
        "answers": len(SCENARIOS),
        "settings": {
            "channel.assist.profile.summary": "",
            "channel.assist.materials.dir": f"chats/{chat_id}/materials",
            "channel.assist.materials.enabled": True,
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "dist" / "Argus" / "chats"
    made = build(target)
    print(f"done: {made}  ({len(SCENARIOS)} answers)")
    for i, (title, *_rest) in enumerate(SCENARIOS, 1):
        print(f"  {i:2}. {title}")
