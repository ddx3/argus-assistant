"""A photo pasted from the clipboard into the ask line.

It goes to the model instead of a screenshot, lands in the frame memory with the mark
`paste` (so that it is visible in the timeline and can be opened from the card), and is
accepted by the server only as JPEG.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from argus.app import Argus
from argus.brain.assistant import Assistant
from argus.brain.base import Answer
from argus.context.timeline import Timeline
from argus.core.bus import Bus
from argus.core.clock import ReplayClock
from argus.core.config import Config
from argus.ui.server import MAX_PASTED_IMAGE_BYTES, build, snapshot

# The header of a real JPEG: the server checks it, not the extension or the type.
JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 400


class Recorder:
    """A stub model: it remembers what it was asked."""

    name = "recorder"

    def __init__(self) -> None:
        self.images: list[bytes | None] = []
        self.systems: list[str | None] = []
        self.prompts: list[str] = []

    async def start(self, system=None) -> None:
        return None

    async def warm(self, system=None, pinned=()) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False) -> Answer:
        self.prompts.append(prompt)
        self.systems.append(system)
        self.images.append(image)
        return Answer(text="answer", provider=self.name, meta={})


def make_assistant(provider: Recorder, screenshot=None) -> Assistant:
    cfg = Config()
    clock = ReplayClock.__new__(ReplayClock)
    clock._t = 0
    clock._t0_wall = 0
    return Assistant(
        Bus(asyncio.get_event_loop()), cfg, clock, Timeline(), provider, screenshot=screenshot
    )


async def test_a_pasted_photo_goes_to_the_model_instead_of_the_screen():
    """The person showed what to look at: the screen is not added to the photo. The request has
    a single picture, and an extra frame would cost tokens for nothing.
    """
    provider = Recorder()

    def screen():
        raise AssertionError("a screenshot must not be added to a pasted photo")

    a = make_assistant(provider, screenshot=screen)
    a.cfg.brain.send_screenshot = True
    a.cfg.brain.screenshot_mode = "always"

    answer = await a.ask_now("what kind of document is this?", image=JPEG)

    assert provider.images == [JPEG]
    assert answer.meta["image_src"] == "paste"
    assert answer.meta["image_kb"] == len(JPEG) // 1024


async def test_a_pasted_photo_is_not_called_a_screenshot_in_the_prompt():
    """The prompt "a screenshot is attached to the request, analyze the app or the site" is
    untrue for a photo of a document or a person; the model is told that it is an image, not
    a screen.
    """
    provider = Recorder()
    a = make_assistant(provider)

    await a.ask_now("what is this?", image=JPEG)

    assert provider.systems[0] == a.cfg.brain.system_prompt          # the variant without "a screenshot"
    assert provider.systems[0] != a.cfg.brain.system_prompt_with_image
    assert "An image is attached to the question, not a screenshot" in provider.prompts[0]


async def test_an_ordinary_question_is_still_marked_as_a_screen_frame():
    provider = Recorder()
    a = make_assistant(provider, screenshot=lambda: b"frame")
    a.cfg.brain.send_screenshot = True
    a.cfg.brain.screenshot_mode = "always"

    answer = await a.ask_now("what is on the screen")

    assert provider.images == [b"frame"]
    assert answer.meta["image_src"] == "screen"
    assert "An image is attached" not in provider.prompts[0]


async def test_the_app_remembers_the_pasted_photo_as_a_shot_of_its_own_kind():
    cfg = Config()
    cfg.brain.provider = "stub"
    app = Argus(cfg, with_screen=False, with_brain=True)

    answer = await app.ask("what is this?", image=JPEG)

    assert answer.meta["image_src"] == "paste"
    assert [(s.kind, s.jpeg) for s in app.state.shots] == [("paste", JPEG)]
    # In the snapshot for the page, with the mark by which the timeline writes "Photo",
    # not "Screen".
    assert snapshot(app)["shots"][-1]["kind"] == "paste"


def test_the_snapshot_says_the_server_can_take_a_pasted_photo(stub_app):
    """The page is read live from `src`, while the server in the built program is a built one:
    without a flag, the old server would silently ignore the photo.
    """
    assert snapshot(stub_app)["can_paste_image"] is True


async def _post(app: Argus, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=build(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/ask", json=body)


@pytest.fixture
def stub_app() -> Argus:
    cfg = Config()
    cfg.brain.provider = "stub"
    return Argus(cfg, with_screen=False, with_brain=True)


async def test_the_ask_endpoint_takes_a_jpeg_and_reports_where_it_came_from(stub_app):
    body = {"question": "what is this?", "image_b64": base64.b64encode(JPEG).decode()}

    r = await _post(stub_app, body)

    assert r.status_code == 200
    assert r.json()["image_src"] == "paste"
    assert stub_app.state.shots[-1].jpeg == JPEG


async def test_the_ask_endpoint_still_works_without_a_photo(stub_app):
    r = await _post(stub_app, {"question": "an ordinary question"})

    assert r.status_code == 200
    assert r.json()["image_src"] == "screen"
    assert not stub_app.state.shots


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("this is not base64!!", 400),
        (base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 50).decode(), 400),   # PNG, not JPEG
        (base64.b64encode(b"\xff\xd8\xff" + b"x" * (MAX_PASTED_IMAGE_BYTES + 1)).decode(), 413),
        (12345, 400),
    ],
    ids=["not base64", "PNG instead of JPEG", "too large", "not a string"],
)
async def test_the_ask_endpoint_refuses_a_bad_photo_without_asking_the_model(stub_app, value, status):
    r = await _post(stub_app, {"question": "what is this?", "image_b64": value})

    assert r.status_code == status
    assert not stub_app.state.shots, "a bad photo must not get into the frame memory"
