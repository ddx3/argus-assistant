"""Checks for the event feed.

The main point is that the screen enters the feed as a diff, not as a whole. Without that
the prompt gets clogged with repeats: menus and panels do not change for hours, but are
read again every couple of seconds.
"""

from __future__ import annotations

from argus.context.render import estimate_tokens, render, render_context, render_entry, timestamp
from argus.context.store import TimelineStore
from argus.context.timeline import (
    ASK_CHARS,
    CUT_MARK,
    REPLY_CHARS,
    Entry,
    Kind,
    ScreenDigest,
    Timeline,
)
from argus.core.events import ScreenBlock, ScreenEvent, Source, SpeechEvent

NS = 1_000_000_000


def speech(t_s: float, source: Source, text: str, conf: float = 0.9) -> SpeechEvent:
    return SpeechEvent(
        t=int(t_s * NS), t_end=int((t_s + 2) * NS), source=source, text=text, confidence=conf
    )


def screen(t_s: float, *lines: str) -> ScreenEvent:
    return ScreenEvent(
        t=int(t_s * NS),
        blocks=[ScreenBlock(text=line, box=(0, i * 30, 200, 25)) for i, line in enumerate(lines)],
    )


# --------------------------------------------------------------------------
# Screen diff
# --------------------------------------------------------------------------


def test_first_screen_is_all_new():
    d = ScreenDigest()
    new, wholesale = d.diff(screen(0, "File", "Edit", "Question 1"))
    assert new == ["File", "Edit", "Question 1"]
    assert not wholesale


def test_unchanged_screen_adds_nothing():
    """Panels and menus must not get into the prompt a second time."""
    d = ScreenDigest()
    d.diff(screen(0, "File", "Edit", "Question 1"))
    new, _ = d.diff(screen(2, "File", "Edit", "Question 1"))
    assert new == []


def test_only_the_new_line_gets_through():
    """That is what it was all for: the question that appeared goes into the timeline, not the
    whole screen.
    """
    d = ScreenDigest()
    d.diff(screen(0, "File", "Edit", "Question 1 of 10"))
    new, wholesale = d.diff(screen(2, "File", "Edit", "Question 2 of 10"))
    assert new == ["Question 2 of 10"]
    assert not wholesale


def test_window_switch_is_reported_separately():
    """When almost everything has changed, it is more honest to say "the screen changed".

    Otherwise a hundred lines of the new interface would spill into the feed, and the real
    content would drown in them.
    """
    d = ScreenDigest()
    d.diff(screen(0, "File", "Edit", "View", "Question 1"))
    _, wholesale = d.diff(screen(5, "Inbox", "Sent", "Drafts", "Spam"))
    assert wholesale


def test_new_lines_are_capped():
    """Even on a new screen, an unlimited number of lines must not get into the timeline."""
    d = ScreenDigest(max_new_lines=3)
    new, _ = d.diff(screen(0, *[f"line {i}" for i in range(20)]))
    assert len(new) == 3


def test_empty_screen_changes_nothing():
    d = ScreenDigest()
    d.diff(screen(0, "File"))
    new, wholesale = d.diff(ScreenEvent(t=NS, blocks=[]))
    assert new == []
    assert not wholesale
    assert d.current_text == "File"


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------


def test_speech_and_screen_share_one_order():
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "Tell me about databases"))
    tl.add_screen(screen(2, "Question 3 of 10"))
    tl.add_speech(speech(5, Source.ME, "I have five years of experience"))

    times = [e.t for e in tl]
    assert times == sorted(times)
    assert len(tl) == 3


def test_repeated_screen_does_not_grow_the_timeline():
    tl = Timeline()
    for i in range(10):
        tl.add_screen(screen(i, "File", "Edit", "Question 1"))
    assert len(tl) == 1


def test_entries_older_than_the_window_fall_out():
    tl = Timeline(window_s=60.0)
    tl.add_speech(speech(0, Source.THEM, "old"))
    tl.add_speech(speech(120, Source.THEM, "new"))
    assert [e.text for e in tl] == ["new"]
    assert tl.dropped == 1


def test_timeline_is_capped_by_count():
    tl = Timeline(max_entries=5, window_s=10_000)
    for i in range(20):
        tl.add_speech(speech(i, Source.ME, f"remark {i}"))
    assert len(tl) == 5
    assert [e.text for e in tl][-1] == "remark 19"


def test_empty_text_is_not_stored():
    tl = Timeline()
    tl.add_speech(speech(1, Source.ME, "   "))
    assert len(tl) == 0


def test_last_speech_can_be_filtered_by_speaker():
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "question"))
    tl.add_speech(speech(3, Source.ME, "answer"))
    assert tl.last_speech().text == "answer"
    assert tl.last_speech(Source.THEM).text == "question"
    assert tl.last_speech(Source.SCREEN) is None


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------


def test_timestamps_are_readable():
    assert timestamp(0) == "00:00"
    assert timestamp(75) == "01:15"
    assert timestamp(3599) == "59:59"


def test_speaker_names_are_explicit():
    """The prompt must show who is speaking, with no guessing."""
    line = render_entry(Entry(t=5 * NS, kind=Kind.SPEECH, source=Source.THEM, text="hello"))
    assert line == "[00:05] Them: hello"
    line = render_entry(Entry(t=5 * NS, kind=Kind.SPEECH, source=Source.ME, text="hello"))
    assert line == "[00:05] Me: hello"


def test_multiline_screen_is_indented():
    line = render_entry(
        Entry(t=0, kind=Kind.SCREEN, source=Source.SCREEN, text="first\nsecond")
    )
    assert line == "[00:00] Screen:\n    first\n    second"


def test_render_keeps_chronological_order():
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "first"))
    tl.add_speech(speech(2, Source.ME, "second"))
    text = render(tl)
    assert text.index("first") < text.index("second")


def test_render_is_append_only():
    """A stable beginning is the condition for prompt caching.

    If assembly reordered or rewrote lines that were already finished, the cache would break
    on every request, and the whole context would have to be paid for again each time.
    """
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "first"))
    before = render(tl)
    tl.add_speech(speech(2, Source.ME, "second"))
    after = render(tl)
    assert after.startswith(before)


def test_reply_is_a_line_of_the_assistant_with_its_request():
    """Its own answer in the timeline is labeled, and the typed request stands right above it."""
    tl = Timeline()
    tl.add_reply(5 * NS, "$$E = mc^2$$", ask="give me the formula")
    assert render(tl) == (
        "[00:05] Me → assistant: give me the formula\n"
        "[00:05] Assistant: $$E = mc^2$$"
    )


def test_reply_to_a_spoken_question_has_no_request_line():
    """The other party's utterance is already in the timeline, so there is no need to repeat it
    above the answer.
    """
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "Tell me about the GIL"))
    tl.add_reply(4 * NS, "The global interpreter lock.")
    assert render(tl) == (
        "[00:01] Them: Tell me about the GIL\n"
        "[00:04] Assistant: The global interpreter lock."
    )


def test_multiline_reply_is_indented_like_the_screen():
    line = render_entry(
        Entry(t=0, kind=Kind.REPLY, source=Source.ASSISTANT, text="$$\nx^2\n$$", ask="a formula")
    )
    assert line == (
        "[00:00] Me → assistant: a formula\n"
        "[00:00] Assistant:\n    $$\n    x^2\n    $$"
    )


def test_reply_is_appended_at_the_end_and_keeps_the_cache():
    """The answer is appended like any entry, and the start of the prompt does not change."""
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "first"))
    before = render(tl)
    tl.add_reply(3 * NS, "answer", ask="question")
    assert render(tl).startswith(before)


def test_reply_is_not_a_spoken_line():
    """Its own answer is never the "last utterance": nobody replies to it."""
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "question"))
    tl.add_reply(3 * NS, "answer")
    assert tl.last_speech().text == "question"
    assert tl.last_speech(Source.ASSISTANT) is None


def test_empty_reply_is_not_stored():
    tl = Timeline()
    assert tl.add_reply(NS, "   ", ask="question") is None
    assert len(tl) == 0


def test_huge_reply_is_clipped_and_says_so():
    """An answer longer than the timeline budget would push everything else out of the prompt."""
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "question"))
    entry = tl.add_reply(3 * NS, "x" * 10_000, ask="y" * 5_000)
    assert len(entry.text) < REPLY_CHARS + 100
    assert len(entry.ask) < ASK_CHARS + 100
    assert entry.text.endswith(CUT_MARK)
    assert entry.ask.endswith(CUT_MARK)
    # A feed with such an answer still fits into the usual budget and carries the question.
    text = render(tl, max_chars=12_000)
    assert "Them: question" in text
    assert "they did not fit" not in text


def test_old_replies_fall_out_with_the_window():
    """Its own answers age just like utterances: through the timeline window."""
    tl = Timeline(window_s=60)
    tl.add_reply(NS, "ancient answer", ask="ancient question")
    tl.add_speech(speech(120, Source.THEM, "fresh question"))
    assert "ancient" not in render(tl)


def test_render_trims_from_the_start_and_says_so():
    """We trim the old, but we must not be silent about it.

    A model that sees a fragment starts to fill in what was not said, which is exactly what
    the assistant must not do.
    """
    tl = Timeline(max_entries=100)
    for i in range(100):
        tl.add_speech(speech(i, Source.ME, f"remark number {i} with rather long text"))
    text = render(tl, max_chars=500)
    assert len(text) <= 600
    assert "they did not fit" in text
    assert "remark number 99" in text


def test_render_context_puts_screen_last():
    """The current screen is rewritten every time and therefore goes at the end.

    If it stood at the start, it would break the cache of the whole feed on every request.
    """
    tl = Timeline()
    tl.add_speech(speech(1, Source.THEM, "question"))
    text = render_context(tl, screen_now="Question 3 of 10")
    assert text.index("What happened") < text.index("What is on the screen now")
    assert text.rstrip().endswith("Question 3 of 10")


def test_empty_timeline_renders_empty():
    assert render(Timeline()) == ""


def test_token_estimate_is_sane():
    assert 5 < estimate_tokens("Tell me about your experience working with databases") < 30


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_entries_survive_on_disk(tmp_path):
    with TimelineStore(tmp_path / "t.db") as store:
        store.add(Entry(t=NS, kind=Kind.SPEECH, source=Source.THEM, text="hello", confidence=0.9))
        store.commit()

    with TimelineStore(tmp_path / "t.db") as store:
        rows = list(store.all())
    assert len(rows) == 1
    assert rows[0].text == "hello"
    assert rows[0].source is Source.THEM


def test_full_text_search_finds_russian(tmp_path):
    """Search in Russian is why SQLite was chosen rather than a file of lines."""
    # The subject of this test is searching Russian text (Cyrillic word forms), so the data stays Russian.
    with TimelineStore(tmp_path / "t.db") as store:
        store.add(Entry(t=NS, kind=Kind.SPEECH, source=Source.THEM,
                        text="Расскажите про ваш опыт работы с базами данных"))
        store.add(Entry(t=2 * NS, kind=Kind.SPEECH, source=Source.ME,
                        text="Работал с PostgreSQL пять лет"))
        store.commit()

        found = store.search("базами")
        assert len(found) == 1
        assert "базами данных" in found[0].text

        assert len(store.search("PostgreSQL")) == 1
        assert store.search("совершенно другое") == []


def test_search_results_are_in_time_order(tmp_path):
    with TimelineStore(tmp_path / "t.db") as store:
        for i in (5, 1, 3):
            store.add(Entry(t=i * NS, kind=Kind.SPEECH, source=Source.ME, text=f"test {i}"))
        store.commit()
        assert [e.t for e in store.search("test")] == [NS, 3 * NS, 5 * NS]


def test_store_counts_entries(tmp_path):
    with TimelineStore(tmp_path / "t.db") as store:
        assert store.count() == 0
        store.add_many([
            Entry(t=i * NS, kind=Kind.SPEECH, source=Source.ME, text=f"line {i}")
            for i in range(4)
        ])
        store.commit()
        assert store.count() == 4


def test_digest_remembers_what_went_to_the_model():
    """The interface needs to show which of what was read the model actually saw.

    Without that, the "Screen" panel looks like a dump: it is unclear what goes into
    the prompt and what was simply on the screen earlier.
    """
    d = ScreenDigest()
    d.diff(screen(0, "File", "Edit", "Question 1"))
    assert set(d.last_new) == {"File", "Edit", "Question 1"}

    d.diff(screen(2, "File", "Edit", "Question 2"))
    assert d.last_new == ["Question 2"]

    d.diff(screen(4, "File", "Edit", "Question 2"))
    assert d.last_new == []


def test_reset_forgets_what_went_to_the_model():
    d = ScreenDigest()
    d.diff(screen(0, "File"))
    d.reset()
    assert d.last_new == []


def test_timeline_takes_the_substantial_lines_not_the_topmost():
    """A browser tab row ate all the places in the feed.

    Before, the first N lines from the top down were taken, and at the top of a browser are
    the tabs. On a live screen that gave "HD Smc X", "UDSm X", "XEC X": fragments of titles
    three letters long. The real content lay lower and did not get into the feed at all, and
    the model answered such a prompt with "the text is unreadable".
    """
    tabs = ["NIX X", "© HoE X", "HD Smc X", "D Smc X", "UDSm X", "XEC X", "DRL X"]
    content = [
        "Stream chat",
        "Steam game library",
        "Overwatch — competitive game",
        "twitch.tv/trrustnobody",
    ]
    d = ScreenDigest(max_new_lines=5)
    new, _ = d.diff(screen(0, *(tabs + content)))

    assert len(new) == 5
    for line in content:
        assert line in new, f'the substantial line "{line}" did not reach the feed'


def test_short_list_is_kept_whole():
    """When there are few lines, there is nothing to select: take them all."""
    d = ScreenDigest(max_new_lines=10)
    new, _ = d.diff(screen(0, "File", "Edit", "Question 1"))
    assert new == ["File", "Edit", "Question 1"]


def test_reading_order_survives_the_selection():
    """Selection by substance must not shuffle the lines.

    The feed is read by a person and goes into the prompt: the order in it must stay the same
    as on the screen.
    """
    d = ScreenDigest(max_new_lines=3)
    new, _ = d.diff(screen(0, "the first longer line", "X", "the second longer line",
                           "Y", "the third longer line"))
    assert new == ["the first longer line", "the second longer line",
                   "the third longer line"]
