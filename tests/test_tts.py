"""Voicing answers: text cleanup, on/off switching, synthesis in the background.

Without a network: `tts.client.synthesize` is replaced by a fake. Publishing an answer on
the bus is the entry point: `Watcher` listens for `Answer` the way a second subscriber of
`app._wire()` would, and cannot break anything in the rest of the pipeline simply because
it does not know about it.
"""

from __future__ import annotations

import asyncio

from argus.brain.base import Answer
from argus.core.bus import Bus
from argus.tts import reply
from argus.tts.settings import TTSSettings, VoiceProfile, read
from argus.tts.text import speakable, split_sentences, take_ready_sentences


class FakeApp:
    def __init__(self) -> None:
        self.bus = Bus()


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


def test_speakable_strips_markup_and_math():
    text = "# Heading\n**Important**: complexity is $$O(n \\log n)$$, see `code()`"
    out = speakable(text, max_chars=1000)
    assert "**" not in out
    assert "$$" not in out
    assert "`" not in out
    assert not out.startswith("#")
    assert "O(n" in out  # the content of the formula remained, not only the wrapper


def test_speakable_cuts_on_word_boundary():
    text = "one two three four five"
    out = speakable(text, max_chars=10)
    assert out == "one two"
    assert len(out) <= 10


def test_speakable_keeps_short_text_untouched():
    assert speakable("Hello", max_chars=500) == "Hello"


def test_split_sentences_splits_on_sentence_ends():
    # Every sentence is longer than the merge threshold, otherwise short neighbors would merge
    # into one piece (see the test below), and that would be a different check.
    text = (
        "The first sentence is definitely longer than forty characters. "
        "The second sentence also came out longer than the threshold! "
        "The third sentence is again longer than forty characters?"
    )
    out = split_sentences(text)
    assert len(out) == 3
    assert out[0].endswith(".")
    assert out[1].endswith("!")
    assert out[2].endswith("?")


def test_split_sentences_merges_short_neighbours():
    # "Yes." is shorter than the merge threshold: it must not go out as a separate piece
    # for the sake of one word, since that is a separate network request for two seconds of
    # audio.
    out = split_sentences("Yes. But the question here is harder than it seems at first glance.")
    assert len(out) == 1
    assert out[0].startswith("Yes.")


def test_split_sentences_empty_text():
    assert split_sentences("") == []
    assert split_sentences("   ") == []


def test_take_ready_sentences_holds_back_the_growing_tail():
    """Until `final`, the last, still unfinished piece is not handed out: it may grow with the
    next portion of text from the model."""
    pieces, upto = take_ready_sentences("The first sentence. Not fini", final=False)
    assert pieces == ["The first sentence."]
    assert "The first sentence. Not fini"[upto:] == "Not fini"


def test_take_ready_sentences_final_flushes_the_tail_without_punctuation():
    pieces, upto = take_ready_sentences("First. A tail without a period", final=True)
    assert pieces == ["First.", "A tail without a period"]
    assert upto == len("First. A tail without a period")


def test_take_ready_sentences_does_not_merge_short_neighbours():
    """Unlike `split_sentences`: on a stream there is no neighbour yet, and waiting for one to
    merge would slow down exactly what we cut into pieces for."""
    pieces, _ = take_ready_sentences("Yes. Well, it is harder.", final=True)
    assert pieces == ["Yes.", "Well, it is harder."]


def test_take_ready_sentences_empty_text():
    assert take_ready_sentences("", final=False) == ([], 0)
    assert take_ready_sentences("", final=True) == ([], 0)


def test_take_ready_sentences_nothing_ready_yet():
    pieces, upto = take_ready_sentences("Still typing without a period", final=False)
    assert pieces == []
    assert upto == 0


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_read_falls_back_to_defaults_on_garbage():
    cfg = type("Cfg", (), {"channel": {"tts": {"enabled": "not a boolean at all"}}})()
    assert read(cfg) == TTSSettings()


def test_characters_is_a_dict_not_a_list():
    settings = TTSSettings(characters={"demo": VoiceProfile(voice_id="abc")})
    assert settings.characters["demo"].voice_id == "abc"


def test_voice_profile_language_and_favorite_default_to_unset():
    p = VoiceProfile(voice_id="abc")
    assert p.language == ""
    assert p.favorite is False


def test_voice_profile_carries_language_and_favorite():
    p = VoiceProfile(voice_id="abc", language="ru", favorite=True)
    assert p.language == "ru"
    assert p.favorite is True


def test_voice_profile_preview_url_defaults_empty():
    """Empty means the character was created before this field existed (by hand, via
    `clone-voice`/`add-voice`): the widget falls back to paid synthesis when there is no
    sample."""
    assert VoiceProfile(voice_id="abc").preview_url == ""


def test_voice_profile_carries_preview_url():
    url = "https://api.us.elevenlabs.io/v1/voices/abc/previews/audio?payload=x"
    assert VoiceProfile(voice_id="abc", preview_url=url).preview_url == url


# --------------------------------------------------------------------------
# install(): idempotence and on/off switching
# --------------------------------------------------------------------------


async def test_install_disabled_does_not_subscribe():
    app = FakeApp()
    watcher = reply.install(app, TTSSettings(enabled=False))
    assert watcher is None
    assert getattr(app, "_tts_watcher", None) is None
    assert not app.bus._subs.get(Answer)


async def test_install_enabled_subscribes_once():
    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    w1 = reply.install(app, settings)
    assert w1 is not None
    assert len(app.bus._subs[Answer]) == 1

    # A second call does not spawn a second subscription; it updates the settings of the same
    # `Watcher`.
    w2 = reply.install(app, settings)
    assert w2 is w1
    assert len(app.bus._subs[Answer]) == 1

    w1.stop()
    await asyncio.sleep(0)


async def test_install_disabling_tears_down_existing_watcher():
    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    reply.install(app, settings)
    assert app.bus._subs.get(Answer)

    reply.install(app, TTSSettings(enabled=False))
    assert getattr(app, "_tts_watcher", None) is None
    assert not app.bus._subs.get(Answer)


# --------------------------------------------------------------------------
# Synthesis: the end-to-end path through the bus, without a network
# --------------------------------------------------------------------------


async def _wait_for(predicate, tries: int = 50) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)


async def test_answer_gets_one_clip_for_a_single_sentence(monkeypatch):
    calls = []

    async def fake_synthesize(text, profile, settings, api_key):
        calls.append((text, profile.voice_id, api_key))
        return b"fake-mp3-bytes"

    monkeypatch.setattr(reply.client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    watcher = reply.install(app, settings)

    answer = Answer(text="Hello, world", provider="stub")
    answer.meta["t"] = 12345
    app.bus.publish(answer)

    await _wait_for(lambda: answer.meta.get("audio_ids"))
    assert answer.meta["audio_ids"] == ["12345-0"]
    assert watcher.clips["12345-0"] == b"fake-mp3-bytes"
    assert calls == [("Hello, world", "v1", "test-key")]

    watcher.stop()
    await asyncio.sleep(0)


async def test_multi_sentence_answer_streams_clips_in_order(monkeypatch):
    """The pieces must appear one at a time, in sentence order: a pipeline, not "synthesize the
    whole answer first, then hand it over as one list"."""
    seen_order = []

    async def fake_synthesize(text, profile, settings, api_key):
        seen_order.append(text)
        return f"audio-for:{text}".encode()

    monkeypatch.setattr(reply.client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    watcher = reply.install(app, settings)

    text = (
        "This is the first sentence, made long on purpose. "
        "This is the second sentence, also made long on purpose. "
        "This is the third sentence, also made long on purpose."
    )
    expected = split_sentences(text)
    assert len(expected) == 3  # the check is meaningless if there are not three pieces here

    answer = Answer(text=text, provider="stub")
    answer.meta["t"] = 999
    app.bus.publish(answer)

    await _wait_for(lambda: len(answer.meta.get("audio_ids", [])) >= 3)
    assert answer.meta["audio_ids"] == [f"999-{i}" for i in range(3)]
    assert watcher.clips["999-1"] == f"audio-for:{expected[1]}".encode()
    assert seen_order == expected

    watcher.stop()
    await asyncio.sleep(0)


async def test_streaming_speech_stops_once_the_character_budget_is_spent(monkeypatch):
    """`settings.max_chars` is a ceiling for the whole answer (how much is paid to ElevenLabs per
    character), not for each sentence separately. Previously, in the streaming path it was
    applied to each piece anew, and a long answer of short sentences got around the limit
    several times over."""
    seen_order = []

    async def fake_synthesize(text, profile, settings, api_key):
        seen_order.append(text)
        return f"audio-for:{text}".encode()

    monkeypatch.setattr(reply.client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(reply, "POLL_S", 0)

    first = "The first sentence is made long enough for the test on purpose."
    rest = "The second sentence is already beyond the budget. The third is just as long."

    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
        max_chars=len(first),  # the budget is enough for exactly the first sentence
    )
    watcher = reply.install(app, settings)

    answer = Answer(text="", provider="stub", done=False)
    answer.meta["t"] = 7
    app.bus.publish(answer)

    answer.text = first + " "
    await _wait_for(lambda: answer.meta.get("audio_ids"))
    assert answer.meta["audio_ids"] == ["7-0"]

    # The stream finishes the answer to the end, but the budget is already exhausted by the
    # first sentence: the second and third must not be voiced at all.
    answer.text += rest
    answer.done = True
    await _wait_for(lambda: False, tries=10)  # give it a chance to continue if the budget does not stop it

    assert seen_order == [first]
    assert answer.meta["audio_ids"] == ["7-0"]
    assert sum(len(t) for t in seen_order) <= settings.max_chars

    watcher.stop()
    await asyncio.sleep(0)


async def test_missing_api_key_skips_synthesis_without_raising(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    async def boom(*_a, **_kw):
        raise AssertionError("must not be called without a key")

    monkeypatch.setattr(reply.client, "synthesize", boom)

    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    watcher = reply.install(app, settings)

    answer = Answer(text="hello", provider="stub")
    answer.meta["t"] = 1
    app.bus.publish(answer)
    await _wait_for(lambda: False, tries=10)  # give the handler a chance to fail if it is going to

    assert not answer.meta.get("audio_ids")
    watcher.stop()
    await asyncio.sleep(0)


async def test_streaming_answer_speaks_sentences_as_the_text_grows(monkeypatch):
    """A streaming answer (`done=False` at publish): speech does not wait for the whole text, it
    synthesizes finished sentences as they appear."""
    seen_order = []

    async def fake_synthesize(text, profile, settings, api_key):
        seen_order.append(text)
        return f"audio-for:{text}".encode()

    monkeypatch.setattr(reply.client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(reply, "POLL_S", 0)  # the test must not wait a real 100 ms

    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    watcher = reply.install(app, settings)

    answer = Answer(text="", provider="stub", done=False)
    answer.meta["t"] = 42
    app.bus.publish(answer)

    # The first sentence appears before the second, and the stream has not finished yet.
    answer.text = "The first sentence is ready. "
    await _wait_for(lambda: answer.meta.get("audio_ids"))
    assert answer.meta["audio_ids"] == ["42-0"]
    assert not answer.done  # the voiceover did not decide by itself that the answer was finished

    answer.text += "The second one has arrived too."
    answer.done = True
    await _wait_for(lambda: len(answer.meta.get("audio_ids", [])) >= 2)

    assert answer.meta["audio_ids"] == ["42-0", "42-1"]
    assert seen_order == ["The first sentence is ready.", "The second one has arrived too."]

    watcher.stop()
    await asyncio.sleep(0)


async def test_streaming_survives_bold_markup_closing_after_a_consumed_boundary(monkeypatch):
    """Regression: `**` opens before a finished sentence and closes after it, and before this
    fix that cut the voiceover at wrong positions.

    `speakable` is not idempotent on a growing prefix: with `**important. Really` and no closing
    pair the cleanup leaves it alone entirely, but once the `**` pair arrives in the next
    chunk, all the text it encloses collapses, and the old offset (computed from the earlier,
    longer layout) starts cutting at positions that no longer exist in the new text. Before,
    this literally lost letters: "Really" reached synthesis chopped down to "ally". Also
    verified by hand; here it is checked by the same path as the real `_speak`: through the
    bus, with a growing `answer.text`.
    """
    seen_order = []

    async def fake_synthesize(text, profile, settings, api_key):
        seen_order.append(text)
        return f"audio-for:{text}".encode()

    monkeypatch.setattr(reply.client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(reply, "POLL_S", 0)

    app = FakeApp()
    settings = TTSSettings(
        enabled=True, active_character="demo",
        characters={"demo": VoiceProfile(voice_id="v1")},
    )
    watcher = reply.install(app, settings)

    answer = Answer(text="", provider="stub", done=False)
    answer.meta["t"] = 7
    app.bus.publish(answer)

    # First chunk: a finished sentence with an unclosed `**` inside.
    answer.text = "This is **important. Really"
    await _wait_for(lambda: answer.meta.get("audio_ids"))
    assert answer.meta["audio_ids"] == ["7-0"]

    # The second chunk closes the `**` pair opened in the first, and appends the tail.
    answer.text += "! End**, then it is done. "
    answer.done = True
    await _wait_for(lambda: len(answer.meta.get("audio_ids", [])) >= 2)

    # Main point: "Really" is not chopped down to "ally": no words are lost, even if the markup
    # itself (a single `**`, without a pair inside its own chunk) stays uncleaned; that is a
    # much smaller price than losing text.
    spoken = " ".join(seen_order)
    assert "Really" in spoken
    assert "ally" not in spoken.replace("Really", "")

    watcher.stop()
    await asyncio.sleep(0)


async def test_unknown_character_skips_synthesis_without_raising():
    app = FakeApp()
    settings = TTSSettings(enabled=True, active_character="nobody-here")
    watcher = reply.install(app, settings)

    answer = Answer(text="hello", provider="stub")
    answer.meta["t"] = 1
    app.bus.publish(answer)
    await _wait_for(lambda: False, tries=10)

    assert not answer.meta.get("audio_ids")
    watcher.stop()
    await asyncio.sleep(0)
