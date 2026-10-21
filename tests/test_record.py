"""The "Recording" screen: requests to a session recording, text edits, results, PDF.

The errors here are silent: a corrected utterance that did not reach the model looks like
an honest answer about the wrong text; a PDF with broken Cyrillic opens without
complaints; a click on a session that leads the wrong way breaks the app's main path. So
what is checked is what exactly went to the model, what landed on disk and where the
address leads, not "the function returned a string".
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import pytest

from argus.app import Argus
from argus.brain.base import Answer
from argus.chats.store import ChatStore
from argus.core.config import Config
from argus.core.events import Source
from argus.recap import chat, engine, markdown, material, pdf, prompts
from argus.recap.lines import Line
from argus.recap.records import _filename
from argus.recap.results import Results
from argus.ui.server import STATIC, build

ME, THEM = Source.ME, Source.THEM
BASE = time.time() - 3600

DIALOGUE = [
    ("them", "Let's discuss the release: when can we realistically ship the new version?"),
    ("me", "If the tests pass this week, then on October twentieth."),
    ("them", "Good, the twentieth. The testing budget stays the same, a hundred thousand."),
    ("me", "Agreed. I will send the testing plan by Friday."),
]


class Model:
    """A model that records requests and answers with markup."""

    name = "model"

    def __init__(self, text: str = "**Summary**\n\n- item (14:01)", error: str | None = None) -> None:
        self.prompts: list[str] = []
        self.text = text
        self.error = error
        self.gate: asyncio.Event | None = None

    async def start(self, system=None) -> None: ...

    async def warm(self, system=None, pinned=()) -> None: ...

    async def close(self) -> None: ...

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False):
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            return Answer(text="", provider=self.name, error=self.error)
        return Answer(text=self.text, provider=self.name, model="m")


def make_app(root: Path) -> tuple[Argus, Model]:
    cfg = Config()
    cfg.root = root
    cfg.brain.provider = "stub"
    cfg.chats_dir.mkdir()
    app = Argus(cfg, with_screen=False, with_brain=True)
    model = Model()
    app.assistant.provider = model
    return app, model


def session(app: Argus, lines=DIALOGUE, name: str = "Release call", **settings) -> str:
    """Create a session with a recorded conversation; return its id."""
    store = ChatStore(app.cfg.chats_dir)
    created = store.create(name, lambda _id: dict(settings))
    speak(app, created.id, lines)
    return created.id


def speak(app: Argus, chat_id: str, lines=DIALOGUE) -> None:
    folder = ChatStore(app.cfg.chats_dir).folder(chat_id)
    journal = folder / "history.jsonl"
    last = max(
        (json.loads(row)["ts"] for row in journal.read_text(encoding="utf-8").splitlines()
         if row and json.loads(row).get("k") == "s") if journal.exists() else [BASE],
        default=BASE,
    )
    with journal.open("a", encoding="utf-8") as f:
        for i, (source, text) in enumerate(lines):
            record = {"k": "s", "ts": last + 5 + i * 5, "src": source, "text": text}
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def at(minutes: float, who: Source, text: str) -> Line:
    return Line(BASE + minutes * 60, who, text)


# --------------------------------------------------------------------------
# Markdown → HTML
# --------------------------------------------------------------------------


def test_markdown_renders_the_shapes_a_model_writes():
    html = markdown.to_html(
        "## Summary\n\nFirst line\nsecond line\n\n- one\n- two **bold**\n\n"
        "1. first\n2. second\n\n> a quote\n\n---\n\n`code` and *italic*"
    )

    assert "<h3>Summary</h3>" in html
    assert "<p>First line<br>second line</p>" in html
    assert "<ul><li>one</li><li>two <b>bold</b></li></ul>" in html
    assert "<ol><li>first</li><li>second</li></ol>" in html
    assert "<blockquote>a quote</blockquote>" in html
    assert "<hr>" in html
    assert "<code>code</code> and <i>italic</i>" in html


def test_markdown_nests_lists_by_indent():
    html = markdown.to_html("- a\n  - nested\n  - more\n- b")

    assert html == "<ul><li>a<ul><li>nested</li><li>more</li></ul></li><li>b</li></ul>"


def test_markdown_makes_tables():
    html = markdown.to_html("| Who | What |\n|---|---|\n| Me | plan |\n| Ann | docs |")

    assert "<th>Who</th><th>What</th>" in html
    assert "<tr><td>Me</td><td>plan</td></tr>" in html
    assert html.count("<tr>") == 3


def test_markdown_never_lets_the_models_text_become_page_markup():
    html = markdown.to_html('<script>alert(1)</script> and **<b onclick="x">**\n\n```\n<img src=x>\n```')

    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x&gt;" in html
    assert "<b onclick" not in html, "a foreign tag with an attribute must not become a real one"


# --------------------------------------------------------------------------
# Session record and edits
# --------------------------------------------------------------------------


def test_edits_change_what_everyone_reads_and_leave_the_journal_alone(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    store = ChatStore(app.cfg.chats_dir)
    journal = store.folder(chat_id) / "history.jsonl"
    before = journal.read_bytes()
    first, second, *_ = material.load(store, chat_id)

    edits = material.Edits(store, chat_id)
    edits.fix(first.key, "  Let's discuss   the RELEASE  ")
    edits.drop(second.key)

    texts = [e.line.text for e in material.load(store, chat_id)]
    assert texts[0] == "Let's discuss the RELEASE"
    assert len(texts) == len(DIALOGUE) - 1 and DIALOGUE[1][1] not in texts
    assert material.load(store, chat_id)[0].edited
    assert journal.read_bytes() == before, "edits must not touch someone else's journal"


def test_a_dropped_line_that_is_fixed_again_comes_back(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    store = ChatStore(app.cfg.chats_dir)
    key = material.load(store, chat_id)[0].key
    edits = material.Edits(store, chat_id)
    edits.drop(key)

    edits.fix(key, "brought back")

    assert material.load(store, chat_id)[0].line.text == "brought back"


def test_keys_from_the_browser_are_checked_strictly():
    assert material.valid_key("1760000000.123456") and material.valid_key("1760000000")
    for bad in ("", "abc", "../x", "1760000000.1.2", "17600", None, 5):
        assert not material.valid_key(bad)


def test_a_broken_edits_file_means_no_edits(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    store = ChatStore(app.cfg.chats_dir)
    (store.folder(chat_id) / "edits.json").write_text("{not json", encoding="utf-8")

    assert len(material.load(store, chat_id)) == len(DIALOGUE)


def test_edits_do_not_bring_back_a_deleted_session(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    store = ChatStore(app.cfg.chats_dir)
    key = material.load(store, chat_id)[0].key
    store.delete(chat_id)

    material.Edits(store, chat_id).drop(key)

    assert not store.folder(chat_id).exists()


def test_the_record_is_the_spoken_lines_only_not_the_answers(tmp_path):
    app, _ = make_app(tmp_path)
    store = ChatStore(app.cfg.chats_dir)
    chat_id = session(app)
    store.append(chat_id, {"k": "a", "ts": BASE + 100, "text": "the assistant's answer"})

    texts = [e.line.text for e in material.load(store, chat_id)]

    assert texts == [text for _, text in DIALOGUE]
    assert not material.load(store, store.create("empty", lambda _id: {}).id)


def test_stats_count_talk_time_without_the_pauses():
    lines = [at(0, ME, "one"), at(2, THEM, "two"), at(60, ME, "after the break"), at(61, THEM, "more")]

    stats = material.stats(lines)

    assert stats.lines == 4 and stats.minutes == 3         # 2 + 1, a break of an hour does not count
    assert stats.tokens == int(stats.chars / 3)
    assert material.stats([]).minutes == 0


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


def test_results_are_kept_in_the_session_folder_and_survive_a_restart(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    store = ChatStore(app.cfg.chats_dir)

    first = Results(store, chat_id).add("Notes", "text one")
    Results(store, chat_id).add("Tasks", "text two")

    again = Results(store, chat_id).all()                   # a new object, as after a restart
    assert [r["request"] for r in again] == ["Notes", "Tasks"]
    assert Results(store, chat_id).get(first["id"])["text"] == "text one"
    assert Results(store, chat_id).delete(first["id"]) and not Results(store, chat_id).delete(first["id"])


def test_results_are_capped_and_a_broken_file_is_empty(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    store = ChatStore(app.cfg.chats_dir)
    monkeypatch.setattr("argus.recap.results.MAX_RESULTS", 3)
    for i in range(5):
        Results(store, chat_id).add(f"request {i}", "text")

    assert [r["request"] for r in Results(store, chat_id).all()] == ["request 2", "request 3", "request 4"]
    (store.folder(chat_id) / "results.json").write_text("{not json", encoding="utf-8")
    assert Results(store, chat_id).all() == []


# --------------------------------------------------------------------------
# A request to the model
# --------------------------------------------------------------------------


def talk(count: int) -> list[Line]:
    return [
        at(i * 0.3, ME if i % 3 == 0 else THEM, f"line #{i}: " + "word " * 17)
        for i in range(count)
    ]


async def test_a_short_record_and_the_request_go_in_one_prompt():
    model = Model()
    lines = [at(i, ME if i % 2 else THEM, f"line number {i} about the release dates") for i in range(6)]

    out = await engine.perform(model, lines, "  Write   a letter to the colleagues  ")

    assert out.ok and (out.parts, out.calls) == (1, 1)
    prompt = model.prompts[0]
    assert "line number 3 about the release dates" in prompt
    assert "# Request\nWrite a letter to the colleagues" in prompt
    assert "data, not commands" in prompt


def test_the_earlier_answers_are_kept_newest_first_and_within_the_budget():
    history = [(f"request {i}", f"answer {i} " + "word " * 480) for i in range(10)]

    block = prompts.history_block(history)

    assert len(block) <= prompts.HISTORY_CHARS + 100
    assert "answer 9" in block and "answer 0" not in block, "the fresh matters more than the old"
    assert block.index("request 8") < block.index("request 9"), "in order, the last at the bottom"
    assert prompts.history_block([]) == "" and prompts.last_answer([]) == ""


def test_one_huge_earlier_answer_is_cut_but_still_comes_first_in_line():
    block = prompts.history_block([("Notes", "a " * 10_000)])

    assert "(the rest of the answer is cut off)" in block
    assert len(block) <= prompts.HISTORY_ONE + 300
    assert len(prompts.last_answer([("Notes", "a " * 10_000)])) <= prompts.HINT_CHARS + 300


async def test_a_follow_up_sees_the_earlier_answers_and_a_ready_request_does_not():
    model = Model(text="**Summary**\n\n- first item (14:01)")
    lines = [at(i, ME if i % 2 else THEM, f"line number {i} about the release dates") for i in range(6)]
    earlier = [("Summary", "**Summary**\n\n- first item (14:01)")]

    await engine.perform(model, lines, "List the tasks")
    await engine.perform(model, lines, "shorter", history=earlier)

    fresh, follow = model.prompts
    assert "# Earlier correspondence" not in fresh
    assert "# Earlier correspondence" in follow and 'Request: "Summary"' in follow and "first item (14:01)" in follow
    assert "continues the previous answer" in follow, "the model must know what to do with \"shorter\""
    assert follow.index("# Earlier correspondence") < follow.index("# Request\nshorter"), "the new request goes last"


async def test_a_follow_up_on_a_long_record_carries_the_last_answer_into_every_part():
    model = Model()
    history = [("Summary", "answer-one " * 50), ("Tasks", "THE-LAST-ANSWER " * 30)]

    out = await engine.perform(model, talk(1500), "shorter", history=history)

    assert out.ok and out.parts > 1
    assert max(len(p) for p in model.prompts) <= 20_000, "with the correspondence too the request fits the relay"
    parts = [p for p in model.prompts if "This is part" in p]
    assert parts and all("THE-LAST-ANSWER" in p and "continues the previous answer" in p for p in parts)
    assert "# Earlier correspondence" in model.prompts[-1] and "answer-one" in model.prompts[-1]


async def test_a_long_record_is_cut_into_parts_and_every_request_fits_the_relay():
    model = Model()

    out = await engine.perform(model, talk(1500), "What did the other person promise?", context="call " * 300)

    assert out.ok and out.parts > 1
    assert max(len(p) for p in model.prompts) <= 20_000
    parts = [p for p in model.prompts if "This is part" in p]
    assert parts and all('Request: "What did the other person promise?"' in p for p in parts)
    assert "# Request\nWhat did the other person promise?" in model.prompts[-1]


async def test_a_tiny_record_is_not_sent_to_the_model_and_says_so():
    model = Model()

    out = await engine.perform(model, [at(0, ME, "hello")], "Notes")

    assert out.tiny and out.text == engine.TOO_LITTLE_FOR_REQUEST and model.prompts == []


async def test_an_absurdly_long_request_is_cut():
    model = Model()

    await engine.perform(model, talk(10), "very " * 1200)

    assert len(model.prompts[0].split("# Request\n")[1]) <= engine.INSTRUCTION_CHARS + 50


async def test_a_model_failure_is_an_error_not_a_crash():
    out = await engine.perform(Model(error="request limit reached"), talk(10), "Notes")

    assert not out.ok and "request limit reached" in out.error


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def test_the_page_for_print_escapes_the_title_and_keeps_the_body():
    document = pdf.page(title="<b>Title</b>", body="<p>body</p>")

    assert "&lt;b&gt;Title&lt;/b&gt;" in document
    assert "<p>body</p>" in document
    assert "letter-spacing" not in document, "spaced-out capitals break searching in the PDF"


def test_the_pdf_is_only_the_report_without_the_service_stuff():
    """Requirement (2026-10-07): no timecodes, no brand word, no prompt, no postscript and no date,
    only what matters, so that it is a real report. The document holds a heading and text,
    nothing else: no brand, no request, no date, no "compiled from the recording"
    line."""
    document = pdf.page(title="Release call", body="<p>body</p>")
    for stuff in ("Argus", "Compiled from the recording", "Key figures", "class=\"top\"", "class=\"foot\"", "class=\"meta\"", "class=\"kind\""):
        assert stuff not in document, stuff
    import re

    assert not re.search(r"\d\d\.\d\d\.\d{4}", document), "there is no date in the report"
    assert document.count("<h1>") == 1 and "<p>body</p>" in document


def test_time_links_are_cut_out_of_the_text_for_print_but_meaningful_times_stay():
    cut = markdown.strip_timecodes
    assert cut("The release is on the twentieth (14:32).") == "The release is on the twentieth."
    assert cut("Decided (14:01–14:03), the budget (14:05, 14:06; 15:00) is the same") \
        == "Decided, the budget is the same"
    assert cut("- item (14:32:10)\n- another (9:05)") == "- item\n- another"
    assert cut("| Me | plan (14:05) |") == "| Me | plan |"
    # a time that is itself part of the meaning, not in a link-bracket, stays; brackets with
    # other content stay too
    assert cut("We meet on Tuesday at 11:00.") == "We meet on Tuesday at 11:00."
    assert cut("Pentest (separate budget) and (14 days)") == "Pentest (separate budget) and (14 days)"


async def test_no_edge_is_a_clear_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "find_edge", lambda: None)

    with pytest.raises(pdf.PdfError) as refusal:
        await pdf.render("<p>x</p>", tmp_path / "x.pdf")
    # For the person: without the name of what prints the PDF.
    assert str(refusal.value) == pdf.no_browser_text() and "Edge" not in pdf.NO_EDGE


async def test_edge_is_called_headless_with_its_own_profile(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        target = next(a for a in command if a.startswith("--print-to-pdf="))
        Path(target.split("=", 1)[1]).write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf, "find_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(pdf.subprocess, "run", fake_run)
    out = tmp_path / "folder" / "x.pdf"

    await pdf.render("<p>x</p>", out)

    command = seen["command"]
    assert command[0] == "edge.exe" and "--headless=new" in command and "--no-pdf-header-footer" in command
    assert any(a.startswith("--user-data-dir=") for a in command), "someone else's Edge profile is held by an open window"
    assert command[-1].startswith("file:///") and out.read_bytes().startswith(b"%PDF")


async def test_edge_that_makes_no_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "find_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(pdf.subprocess, "run", lambda *a, **k: None)

    with pytest.raises(pdf.PdfError, match="The PDF didn't work out"):
        await pdf.render("<p>x</p>", tmp_path / "x.pdf")


@pytest.mark.skipif(pdf.find_edge() is None, reason="Microsoft Edge not found")
async def test_a_real_pdf_with_russian_text_and_a_table(tmp_path):
    # The text stays Russian on purpose: the test checks that Cyrillic survives printing to PDF.
    from pypdf import PdfReader

    body = markdown.to_html("## Решили\n\n- релиз **двадцатого** октября\n\n| Кто | Что |\n|---|---|\n| Я | план |")
    out = tmp_path / "конспект.pdf"

    await pdf.render(pdf.page(title="Созвон по релизу", body=body), out)

    reader = PdfReader(str(out))
    text = " ".join((reader.pages[0].extract_text() or "").split())
    assert len(reader.pages) == 1
    assert "Созвон по релизу" in text and "двадцатого" in text and "план" in text


def test_file_names_are_made_from_the_session_and_the_request():
    assert _filename("Release call", "Notes", "pdf") == "Release call — Notes.pdf"
    assert _filename('a/b:c*', 'x?"<>|', "md") == "a b c — x.md", "characters forbidden in file names are dropped"
    assert _filename("Call", "", "pdf") == "Call.pdf"
    assert _filename("", "", "pdf") == "recording.pdf"
    assert len(_filename("x" * 300, "x" * 300, "pdf")) <= 100


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


async def _client(app: Argus) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(app)), base_url="http://test", follow_redirects=False,
    )


async def test_the_record_endpoint_gives_the_text_with_times_and_stats(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        data = (await client.get(f"/record/{chat_id}")).json()

    assert data["ok"] and data["chat"] == {"id": chat_id, "name": "Release call"}
    assert data["recording"] is False and data["results"] == []
    assert [(l["who"], l["text"]) for l in data["lines"]] == [(s, t) for s, t in DIALOGUE]
    assert len(data["lines"][0]["t"]) == 8 and data["lines"][0]["t"].count(":") == 2
    assert data["stats"]["lines"] == 4 and data["stats"]["chars"] == sum(len(t) for _, t in DIALOGUE)


async def test_a_request_goes_to_the_model_and_lands_in_the_session(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        r = await client.post(
            f"/record/{chat_id}/request", json={"instruction": "Make notes", "title": "Notes"},
        )
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert r.status_code == 200 and r.json()["ok"]
    result = r.json()["result"]
    assert result["request"] == "Notes" and result["text"].startswith("**Summary**")
    assert "# Request\nMake notes" in model.prompts[0]
    assert "Agreed. I will send the testing plan" in model.prompts[0]
    assert [x["id"] for x in shown["results"]] == [result["id"]]
    assert (ChatStore(app.cfg.chats_dir).folder(chat_id) / "results.json").is_file()


async def test_the_times_the_model_cites_are_the_times_the_page_shows(tmp_path):
    """A time in the answer ("14:01") is a reference to an utterance; the page looks it up by the
    clock it shows itself. The two notations of time must match."""
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        shown = (await client.get(f"/record/{chat_id}")).json()["lines"]
        await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})

    for line in shown:
        who = "Me" if line["who"] == "me" else "Them"
        assert f"[{line['t'][:5]}] {who}: {line['text']}" in model.prompts[0]


async def test_what_the_person_wrote_about_the_session_reaches_the_model(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app, **{"channel.assist.vacancy.description": "Call with the customer about release 4.2"})
    async with await _client(app) as client:
        await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})

    assert "Call with the customer about release 4.2" in model.prompts[0]


async def test_every_request_sees_the_whole_conversation_before_it(tmp_path):
    """The feed is a real conversation: "shorter" after "Summary" refers to the summary,
    and a ready-made button also knows what was said earlier."""
    app, model = make_app(tmp_path)
    chat_id = session(app)
    url = f"/record/{chat_id}/request"
    async with await _client(app) as client:
        await client.post(url, json={"instruction": "Sum it up", "title": "Summary"})
        model.text = "Briefly: the release is on the twentieth."
        await client.post(url, json={"instruction": "shorter"})
        await client.post(url, json={"instruction": "List the tasks", "title": "Tasks"})
        await client.post(url, json={"instruction": "and in French?"})

    first, short, tasks, french = model.prompts
    assert "# Earlier correspondence" not in first, "the first request has nothing to recall"
    assert 'Request: "Summary"' in short and "**Summary**" in short
    assert 'Request: "Summary"' in tasks and 'Request: "shorter"' in tasks, "a ready-made button sees the conversation too"
    # The last request sees all the earlier answers in order: summary, short, tasks.
    assert french.index('Request: "Summary"') < french.index('Request: "shorter"') < french.index('Request: "Tasks"')


async def test_a_fixed_line_reaches_the_model_and_a_dropped_one_does_not(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        lines = (await client.get(f"/record/{chat_id}")).json()["lines"]
        fixed = await client.put(f"/record/{chat_id}/lines/{lines[1]['key']}", json={"text": "Deadline: the THIRTIETH of October"})
        dropped = await client.delete(f"/record/{chat_id}/lines/{lines[2]['key']}")
        await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})
        after = (await client.get(f"/record/{chat_id}")).json()["lines"]

    assert fixed.json()["ok"] and dropped.json()["ok"]
    prompt = model.prompts[0]
    assert "THIRTIETH of October" in prompt and "the tests pass this week" not in prompt
    assert DIALOGUE[2][1] not in prompt
    assert len(after) == 3 and after[1]["edited"] is True


async def test_bad_input_is_refused_with_a_reason(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        empty = await client.post(f"/record/{chat_id}/request", json={"instruction": "   "})
        not_text = await client.post(f"/record/{chat_id}/request", json={"instruction": 5})
        unknown = await client.get("/record/aaaaaaaaaa")
        bad_id = await client.get("/record/not-an-id")
        bad_key = await client.put(f"/record/{chat_id}/lines/..%2Fx", json={"text": "x"})
        blank = await client.put(f"/record/{chat_id}/lines/1760000000.5", json={"text": "  "})
        no_result = await client.delete(f"/record/{chat_id}/results/deadbeef")

    assert (empty.status_code, empty.json()["ok"]) == (400, False) and "Write what to do" in empty.json()["error"]
    assert not_text.status_code == 400
    assert unknown.status_code == 404 and bad_id.status_code == 404
    assert bad_key.status_code in (400, 404)
    assert blank.status_code == 400 and "can't be empty" in blank.json()["error"]
    assert no_result.status_code == 404
    assert model.prompts == [], "a bad request must not reach the model"


async def test_a_tiny_record_is_refused_without_paying_for_the_model(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app, lines=[("me", "hello")])
    async with await _client(app) as client:
        r = await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})

    assert r.status_code == 422 and "too little text" in r.json()["error"] and model.prompts == []


async def test_a_model_failure_comes_back_as_an_error_and_stores_nothing(tmp_path):
    app, model = make_app(tmp_path)
    model.error = "request limit reached"
    chat_id = session(app)
    async with await _client(app) as client:
        r = await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})

    assert r.status_code == 502 and "request limit reached" in r.json()["error"]
    assert Results(ChatStore(app.cfg.chats_dir), chat_id).all() == []


async def test_a_session_that_is_being_recorded_cannot_be_worked_on(tmp_path, monkeypatch):
    app, model = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(chat, "recording", lambda a, cid: cid == chat_id)
    async with await _client(app) as client:
        shown = (await client.get(f"/record/{chat_id}")).json()
        ask = await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})
        fix = await client.put(f"/record/{chat_id}/lines/1760000000.5", json={"text": "x"})
        drop = await client.delete(f"/record/{chat_id}/lines/1760000000.5")

    assert shown["recording"] is True
    assert ask.status_code == fix.status_code == drop.status_code == 409
    assert "Recording is on" in ask.json()["error"] and model.prompts == []


async def test_two_requests_at_once_are_not_both_paid_for(tmp_path):
    app, model = make_app(tmp_path)
    model.gate = asyncio.Event()
    chat_id = session(app)
    async with await _client(app) as client:
        first = asyncio.create_task(client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"}))
        await asyncio.sleep(0.05)
        second = await client.post(f"/record/{chat_id}/request", json={"instruction": "Notes"})
        model.gate.set()
        done = await first

    assert second.status_code == 409 and done.status_code == 200 and len(model.prompts) == 1


async def test_a_result_can_be_deleted(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        gone = await client.delete(f"/record/{chat_id}/results/{made['id']}")
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert gone.json()["ok"] and shown["results"] == []


async def test_deleting_a_result_takes_its_pdf_with_it(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)

    async def fake_render(document, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf, "render", fake_render)
    folder = ChatStore(app.cfg.chats_dir).folder(chat_id)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        await client.get(f"/record/{chat_id}/results/{made['id']}.pdf")
        name = _filename("Release call", "Summary", "pdf")
        assert (folder / "pdf" / name).is_file()

        await client.delete(f"/record/{chat_id}/results/{made['id']}")

    assert not (folder / "pdf" / name).exists() and list((folder / "pdf").iterdir()) == []


async def test_markdown_download_is_a_file_with_the_result(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        made = (await client.post(
            f"/record/{chat_id}/request", json={"instruction": "Summary", "title": "Notes"},
        )).json()["result"]
        r = await client.get(f"/record/{chat_id}/results/{made['id']}.md")

    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"] and "filename*=UTF-8''" in r.headers["content-disposition"]
    assert r.text.startswith("# Release call") and "**Summary**" in r.text and "Notes" in r.text


async def test_pdf_download_builds_the_document_from_the_result(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    printed = {}

    async def fake_render(document, out):
        printed["document"] = document
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf, "render", fake_render)
    async with await _client(app) as client:
        made = (await client.post(
            f"/record/{chat_id}/request", json={"instruction": "Summary", "title": "Notes"},
        )).json()["result"]
        r = await client.get(f"/record/{chat_id}/results/{made['id']}.pdf")

    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    document = printed["document"]
    assert "Release call" in document and "<li>item" in document
    # The request ("Notes"), "Me and Them" and the brand are service text and do not go into
    # the report.
    assert "Notes" not in document and "Me and Them" not in document and "Argus" not in document


async def test_pdf_without_edge_says_so(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    monkeypatch.setattr(pdf, "find_edge", lambda: None)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        r = await client.get(f"/record/{chat_id}/results/{made['id']}.pdf")

    assert r.status_code == 501 and r.json()["error"] == pdf.no_browser_text()


# --------------------------------------------------------------------------
# The page and transitions
# --------------------------------------------------------------------------


async def test_the_record_page_is_served_for_a_real_session_only(tmp_path):
    app, _ = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        page = await client.get(f"/chat/{chat_id}/record")
        missing = await client.get("/chat/aaaaaaaaaa/record")
        bad = await client.get("/chat/not-an-id/record")

    assert page.status_code == 200 and "<title>Recording · Argus</title>" in page.text
    assert "x-argus-build" in page.headers
    assert missing.status_code == 302 and missing.headers["location"] == "/"
    assert bad.status_code in (302, 404)


async def test_a_click_on_a_session_opens_the_work_screen_even_when_it_has_a_record(tmp_path, monkeypatch):
    """A click on a row opens the work screen, as before; "Recording" is its own button
    next to it.
    """
    app, _ = make_app(tmp_path)
    ready = session(app)
    fresh = ChatStore(app.cfg.chats_dir).create("new", lambda _id: {}).id
    async with await _client(app) as client:
        with_text = await client.get(f"/chat/{ready}")
        empty = await client.get(f"/chat/{fresh}")
        monkeypatch.setattr(chat, "recording", lambda a, cid: True)
        recording = await client.get(f"/chat/{ready}")

    for r in (with_text, empty, recording):
        assert r.status_code == 200, "there must be no redirect"
        assert "<title>Recording · Argus</title>" not in r.text, "this is the work screen, not \"Recording\""


def test_the_home_row_has_a_separate_button_for_the_record():
    home = (STATIC / "home.html").read_text(encoding="utf-8")

    assert 'tool("record", "Work with the recording")' in home
    assert 'act === "record"' in home
    function = re.search(r"function openRecord\(id\) \{.*?\n\}", home, flags=re.DOTALL)
    assert function, "no openRecord"
    body = function.group(0)
    assert "/chat/${encodeURIComponent(id)}/record" in body
    # "Recording" does not open a session and does not stop recognition: there is nothing
    # to ask.
    assert "ask(" not in body
    # A click on the row opens the work screen and, as before, asks for confirmation.
    assert "Stop recognition?" in home and "opensAsRecord" not in home


def test_the_page_talks_to_the_endpoints_the_server_has(tmp_path):
    page = (STATIC / "record.html").read_text(encoding="utf-8")

    # Every `api("METHOD", `address`)` call of the page is a real server route with the same
    # method. The previous check looked for the substring ".pdf" in the page, which both
    # `r.pdf` and `data.pdf` supplied: a typo in the PDF address or in `/pdf/open` would have
    # slipped past and surfaced for the person on a click (audit 2026-10-14, fe-pages-18).
    calls = re.findall(r'api\("([A-Z]+)", `([^`]+)`', page)
    assert {url for _, url in calls} >= {
        "/record/${id}", "/record/${id}/request",
        "/record/${id}/results/${result.id}/pdf", "/record/${id}/results/${result.id}/pdf/open",
    }
    app, _ = make_app(tmp_path)
    routes = [(r.path_regex, r.methods) for r in build(app).routes if getattr(r, "methods", None)]
    for method, url in calls:
        concrete = url.replace("${id}", "c1").replace("${result.id}", "r1")
        assert any(rx.match(concrete) and method in methods for rx, methods in routes), (method, url)
    # Redoing an answer in place is the `replace` field of the request body, not a separate
    # address.
    assert "replace: result.id" in page
    # The work screen opens the session and stops recognition in the previous one: before the
    # switch, the page looks at the session list and warns.
    assert 'fetch("/chats")' in page and 'id="switch-go"' in page
    # The work screen is an ordinary session address (the settings on it are `#admin`), with no
    # special flags.
    assert "`/chat/${id}${hash}`" in page and "?live=1" not in page


def test_the_answer_style_lets_the_content_choose_the_form_but_the_notes_stay_dense():
    """The answers came out alike, with a bulleted list under every heading (2026-10-07: the
    answers look the same, just bullet points everywhere): the rules said "lists with
    dashes". Now a ready answer gets a formatting rule chosen by its content: paragraphs,
    sections with headings, a table for tasks, a numbered list for steps, a quote, a letter
    without bullets. The notes on the parts of a long recording are internal, they have their
    own dense form, and they do not get this rule."""
    from argus.recap import prompts

    assert "lists with dashes" not in prompts.REQUEST_RULES
    style = prompts.REQUEST_STYLE
    for hint in ("paragraphs", "table", "numbered list", "quote", "## Title", "**bold**", "no bulleted or numbered lists"):
        assert hint in style, hint
    assert "do not start every line with a dash" in style

    one = prompts.request_single("[14:00] Me: hello", "Write a letter")
    final = prompts.request_final(["a note"], "Write a letter")
    assert style in one and style in final
    assert style not in prompts.request_partial("[14:00] Me: hello", 1, 2, "Write a letter")
    assert style not in prompts.request_merge(["a note"], "Write a letter")


# --------------------------------------------------------------------------
# "Try again" and "edit" replace the answer in place; the PDF lies under a clear
# name and opens from the app
# --------------------------------------------------------------------------


async def _two_results(client, chat_id):
    first = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
    second = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Tasks"})).json()["result"]
    return first, second


async def test_a_redo_replaces_the_answer_in_place_and_does_not_add_a_new_one(tmp_path):
    """Requirement (2026-10-07): a redone answer replaces the one that was clicked, instead of a
    new answer always being created.
    """
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        first, second = await _two_results(client, chat_id)
        again = (await client.post(
            f"/record/{chat_id}/request", json={"instruction": "A shorter summary", "replace": first["id"]},
        )).json()
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert again["ok"] and again["result"]["id"] == first["id"], "the same id: it is the same answer"
    assert again["result"]["request"] == "A shorter summary"
    results = shown["results"]                       # new ones first
    assert [r["id"] for r in results] == [second["id"], first["id"]], "the same count and order"
    assert [r["request"] for r in results] == ["Tasks", "A shorter summary"]


async def test_a_redo_sees_only_what_came_before_the_answer_it_replaces(tmp_path):
    """The answer being replaced is the thing being redone: it must not become a "continue this"
    hint, and whatever came after it is later in time and does not relate to it."""
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        await client.post(f"/record/{chat_id}/request", json={"instruction": "the very first question"})
        mid = (await client.post(f"/record/{chat_id}/request", json={"instruction": "the middle question"})).json()["result"]
        await client.post(f"/record/{chat_id}/request", json={"instruction": "the last question"})
        before = len(model.prompts)
        await client.post(
            f"/record/{chat_id}/request", json={"instruction": "the middle question once more", "replace": mid["id"]},
        )

    prompt = model.prompts[before]
    assert "the very first question" in prompt, "the model remembers what came earlier"
    assert "the last question" not in prompt, "what came later, it does not"
    assert prompt.count("the middle question") == 1, "and the answer being replaced is no hint (only the new request)"


async def test_a_failed_redo_keeps_the_old_answer(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        first = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        model.error = "request limit reached"
        r = await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary", "replace": first["id"]})
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert r.status_code == 502
    assert [x["text"] for x in shown["results"]] == [first["text"]], "the old answer is in place"


async def test_redoing_an_answer_that_is_gone_is_refused_before_the_model(tmp_path):
    app, model = make_app(tmp_path)
    chat_id = session(app)
    async with await _client(app) as client:
        missing = await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary", "replace": "deadbeef"})
        bad = await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary", "replace": "../x"})

    assert missing.status_code == 404 and bad.status_code == 404 and model.prompts == []


async def test_a_redo_drops_the_pdf_of_the_old_text(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)

    async def fake_render(document, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf, "render", fake_render)
    folder = ChatStore(app.cfg.chats_dir).folder(chat_id)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")
        assert list((folder / "pdf").iterdir())
        await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary", "replace": made["id"]})
        shown = (await client.get(f"/record/{chat_id}")).json()

    assert shown["results"][0]["pdf"] is None, "the PDF was made from the old text"
    assert list((folder / "pdf").iterdir()) == []


async def test_a_pdf_stays_in_the_session_folder_under_a_readable_name_and_the_page_gets_it(tmp_path, monkeypatch):
    """Requirement: it must be clear where the file is saved. The file goes into the session
    folder, the name is readable, and the path is visible to the page.
    """
    app, _ = make_app(tmp_path)
    chat_id = session(app)

    async def fake_render(document, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf, "render", fake_render)
    folder = ChatStore(app.cfg.chats_dir).folder(chat_id)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        before = (await client.get(f"/record/{chat_id}")).json()["results"][0]["pdf"]
        r = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf")
        after = (await client.get(f"/record/{chat_id}")).json()["results"][0]["pdf"]

    assert before is None
    name = "Release call — Summary.pdf"
    assert r.json() == {"ok": True, "pdf": {"name": name, "path": str(folder / "pdf" / name)}}
    assert after == {"name": name, "path": str(folder / "pdf" / name)}
    assert (folder / "pdf" / name).read_bytes().startswith(b"%PDF")


async def test_two_answers_with_the_same_request_do_not_overwrite_each_others_pdf(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    chat_id = session(app)

    async def fake_render(document, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(document.encode("utf-8"))

    monkeypatch.setattr(pdf, "render", fake_render)
    async with await _client(app) as client:
        one = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        two = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        a = (await client.post(f"/record/{chat_id}/results/{one['id']}/pdf")).json()["pdf"]
        b = (await client.post(f"/record/{chat_id}/results/{two['id']}/pdf")).json()["pdf"]
        again = (await client.post(f"/record/{chat_id}/results/{one['id']}/pdf")).json()["pdf"]

    assert a["name"] == "Release call — Summary.pdf"
    assert b["name"] == f"Release call — Summary ({two['id']}).pdf" and a["path"] != b["path"]
    assert again == a, "building the same answer again gives the same file"


async def test_clicking_the_file_opens_it_with_the_default_program(tmp_path, monkeypatch):
    from argus.recap import records as records_module

    app, _ = make_app(tmp_path)
    chat_id = session(app)
    opened = []

    async def fake_render(document, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf, "render", fake_render)
    monkeypatch.setattr(records_module, "_launch", lambda path: opened.append(path))
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        first = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf/open")   # no file: it will build one
        second = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf/open")  # the file exists: it will just open it
        gone = await client.post(f"/record/{chat_id}/results/deadbeef/pdf/open")
        bad = await client.post(f"/record/{chat_id}/results/..%2Fx/pdf/open")

    assert first.json()["ok"] and second.json()["ok"]
    folder = ChatStore(app.cfg.chats_dir).folder(chat_id)
    assert opened == [folder / "pdf" / "Release call — Summary.pdf"] * 2
    assert gone.status_code == 404 and bad.status_code in (404, 422)


async def test_a_program_that_will_not_open_the_file_is_reported(tmp_path, monkeypatch):
    from argus.recap import records as records_module

    app, _ = make_app(tmp_path)
    chat_id = session(app)

    async def fake_render(document, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake")

    def refuse(path):
        raise records_module.RecordError("Couldn't open the file: no program", 500)

    monkeypatch.setattr(pdf, "render", fake_render)
    monkeypatch.setattr(records_module, "_launch", refuse)
    async with await _client(app) as client:
        made = (await client.post(f"/record/{chat_id}/request", json={"instruction": "Summary"})).json()["result"]
        r = await client.post(f"/record/{chat_id}/results/{made['id']}/pdf/open")

    assert r.status_code == 500 and "Couldn't open" in r.json()["error"]
