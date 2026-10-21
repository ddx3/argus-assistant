"""One bad key in the helper's settings does not wipe the others.

Before, any bad value (a `mic_mode` that no longer exists after an update, or `years = 61`
typed by hand) returned the defaults for the WHOLE section, and the prompt was built without
the profile, tone, language and the "What not to do" block; the only sign
of it was one line in the log.
"""

from __future__ import annotations

import logging
import re

from argus.assist import settings as assist_settings
from argus.assist.prompt import compose
from argus.assist.settings import Answering, Profile, problems, read
from argus.core.config import Config


def cfg_with(section) -> Config:
    cfg = Config()
    cfg.channel["assist"] = section
    return cfg


def test_a_bad_field_drops_only_itself():
    cfg = cfg_with({
        "profile": {"name": "Anton", "years": 61, "stop_topics": "do not invent projects"},
        "answering": {"min_words": 7},
    })
    got = read(cfg)
    assert got.profile.name == "Anton"                               # it stayed
    assert got.profile.stop_topics == "do not invent projects"       # it stayed
    assert got.profile.years == Profile().years                      # the bad one gets the default
    assert got.answering.min_words == 7                              # the other section is intact


def test_a_removed_enum_value_drops_only_that_key():
    cfg = cfg_with({
        "answering": {"mic_mode": "used-to-exist", "min_words": 9, "format": "code"},
        "prompting": {"tone": "strict"},
    })
    got = read(cfg)
    assert got.answering.mic_mode == Answering().mic_mode
    assert got.answering.min_words == 9 and got.answering.format == "code"
    assert got.prompting.tone == "strict"


def test_the_prompt_keeps_the_profile_and_the_safety_block_after_one_bad_key():
    cfg = cfg_with({
        "profile": {"name": "Anton", "stop_topics": "do not invent projects"},
        "answering": {"mic_mode": "used-to-exist"},
    })
    system = compose(read(cfg))
    assert "Name: Anton" in system
    assert "# What not to do" in system and "do not invent projects" in system


def test_a_whole_section_of_the_wrong_type_falls_back_alone():
    cfg = cfg_with({"profile": "not a dict", "answering": {"min_words": 8}})
    got = read(cfg)
    assert got.profile == Profile()
    assert got.answering.min_words == 8


def test_a_section_that_is_not_a_dict_at_all_gives_the_defaults():
    assert read(cfg_with("garbage")).profile == Profile()


def test_a_good_config_is_read_as_before():
    cfg = cfg_with({"profile": {"name": "Anton"}})
    assert read(cfg).profile.name == "Anton"


def test_the_list_of_problems_says_so_when_something_was_replaced():
    assert problems(cfg_with({"profile": {"years": 61}}))
    assert problems(cfg_with({"profile": {"name": "Anton"}})) == []
    assert problems(Config()) == []


def test_the_words_of_the_problem_name_no_internals():
    line = problems(cfg_with({"profile": {"years": 61}}))[0]
    for bad in ("years", "pydantic", "validation", "json"):
        assert bad not in line.lower()
    # "assistant" is an ordinary word of the message; the internal name is the bare "assist".
    assert not re.search(r"\bassist\b", line.lower())


def test_the_same_trouble_is_logged_once_not_on_every_read(caplog):
    assist_settings._warned.clear()
    cfg = cfg_with({"profile": {"years": 61}})
    with caplog.at_level(logging.WARNING, logger="argus.assist.settings"):
        for _ in range(5):
            read(cfg)
    assert len([r for r in caplog.records if "are invalid" in r.getMessage()]) == 1
