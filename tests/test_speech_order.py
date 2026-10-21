"""Order of utterances in the snapshot: by speech time, not by arrival time.

These are different orders, and mixing them up shows on screen as an answer standing
before its question. Recognition spends different time on different pieces: a short
phrase from the interlocutor overtakes a long one of your own that was said earlier, and
they line up in the queue in a different order from the one in which they were spoken.

It is checked here, not on the page: the order is the same for everyone who reads the
snapshot, and on the web it would have to be caught with a browser.
"""

from __future__ import annotations

from argus.app import Argus
from argus.core.config import Config
from argus.core.events import Source, SpeechEvent
from argus.ui.server import snapshot

SECOND = 1_000_000_000


def app() -> Argus:
    return Argus(Config(), with_screen=False, with_brain=False)


def said(t_s: float, source: Source, text: str) -> SpeechEvent:
    start = int(t_s * SECOND)
    return SpeechEvent(
        t=start, t_end=start + SECOND, source=source, text=text, confidence=0.9
    )


def texts(state: dict, key: str = "speech") -> list[str]:
    return [e["text"] for e in state[key]]


def test_late_transcript_takes_its_place_in_time():
    """An utterance recognized later takes its place where it was said.

    Exactly the case for which all this exists: your own long phrase takes longer to
    recognize than the interlocutor's short question, arrives second, and without sorting
    would look as if it had been said after it.
    """
    a = app()
    a.state.speech.append(said(10.0, Source.THEM, "short question"))
    a.state.speech.append(said(2.0, Source.ME, "a long phrase said earlier"))

    assert texts(snapshot(a)) == [
        "a long phrase said earlier",
        "short question",
    ]


def test_channels_interleave_instead_of_grouping():
    """A conversation, not two columns by speaker.

    The channels are independent, and utterances can arrive in batches: first three of your
    own, then three of the other side's. That cannot be read as a conversation: the
    utterances must stand interleaved, by time.
    """
    a = app()
    for e in (
        said(1.0, Source.ME, "my first"),
        said(3.0, Source.ME, "my second"),
        said(2.0, Source.THEM, "a reply between them"),
    ):
        a.state.speech.append(e)

    assert texts(snapshot(a)) == ["my first", "a reply between them", "my second"]


def test_newest_goes_last():
    """The last thing said goes last in the list.

    The page is read from top to bottom, like a correspondence: an utterance said later must
    stand below the one it replies to.
    """
    a = app()
    a.state.speech.append(said(1.0, Source.ME, "the first"))
    a.state.speech.append(said(9.0, Source.THEM, "the last"))
    assert texts(snapshot(a))[-1] == "the last"


def test_second_engine_is_ordered_too():
    """Otherwise the comparison columns would drift apart from each other."""
    a = app()
    a.state.speech_b.append(said(5.0, Source.THEM, "later"))
    a.state.speech_b.append(said(1.0, Source.THEM, "earlier"))
    assert texts(snapshot(a), "speech_b") == ["earlier", "later"]


def test_equal_times_keep_their_order():
    """Equal time means arrival order, not a random permutation.

    Both engines receive the same cut from the VAD, so coinciding times are normal here. Sort
    stability is what the merging of the columns rests on.
    """
    a = app()
    a.state.speech.append(said(1.0, Source.ME, "first"))
    a.state.speech.append(said(1.0, Source.THEM, "second"))
    assert texts(snapshot(a)) == ["first", "second"]
