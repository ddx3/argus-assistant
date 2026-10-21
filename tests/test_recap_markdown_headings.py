"""Headings in the model's Markdown (chats-recap-5): no hang and no trimming of "C#".

The heading regular expression tried every way to split the spaces and grew cubically: a
line with spaces in the middle of a heading (a degenerate model generation) froze the
server for minutes, and a saved answer would freeze the "Record" page on every
open.
"""

from __future__ import annotations

import time

import pytest

from argus.recap import markdown


def test_a_heading_with_a_long_run_of_spaces_is_parsed_at_once():
    line = "# a" + " " * 2000 + "b"             # the old expression: about 10 seconds, on 5000 it is minutes

    started = time.perf_counter()
    html = markdown.to_html(line)

    assert time.perf_counter() - started < 1.0
    assert html.startswith("<h2>a") and html.endswith("b</h2>")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("## The C# language", "<h3>The C# language</h3>"),
        ("### F#", "<h4>F#</h4>"),
        ("# Summary", "<h2>Summary</h2>"),
        ("## Summary ##", "<h3>Summary</h3>"),                  # closing hashes come after a space
        ("## Summary   ###   ", "<h3>Summary</h3>"),
        ("#   Many spaces  ", "<h2>Many spaces</h2>"),
        ("## C# and F# together", "<h3>C# and F# together</h3>"),
        ("## ##", "<h3></h3>"),
    ],
)
def test_headings_keep_the_hashes_that_belong_to_the_text(source, expected):
    assert markdown.to_html(source) == expected


@pytest.mark.parametrize("source", ["####### seven hashes", "#no space", "##"])
def test_what_is_not_a_heading_stays_a_paragraph(source):
    assert markdown.to_html(source).startswith("<p>")


def test_a_less_indented_item_does_not_swallow_the_others():
    """chats-recap-8: a list that started with an indent lost all the items with a smaller indent."""
    html = markdown.to_html("  - one\n- two\n- three")

    for word in ("one", "two", "three"):
        assert f"<li>{word}</li>" in html
