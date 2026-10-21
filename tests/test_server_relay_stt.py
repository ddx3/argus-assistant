"""Recognition relay: pass, a lock on what goes to the cloud, money, silence in the log.

We do not hit the real cloud; a fake one stands in for it and speaks the same protocol
(`FakeRealtime` from `test_realtime_stt.py`). But the account server here is real:
uvicorn on a free port, reached with a real WebSocket client. The tests check what
surrounds the cloud: who is let in, what is forwarded, how much is charged, and what is
left in the log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket

import pytest
import websockets

from argus.server import accounts, entitlements, pricing, relay_stt
from argus.server.app import build
from argus.server.db import connect
from test_realtime_stt import FakeRealtime

SECRET = "server-secret-of-sufficient-length-for-the-test"
PASSWORD = "a-fairly-long-password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pcm(seconds: float) -> str:
    """Silence of the needed length in the session format (24 kHz, 16-bit), as base64."""
    return base64.b64encode(bytes(int(relay_stt.SAMPLE_RATE * seconds) * 2)).decode("ascii")


class FakeCloud(FakeRealtime):
    """A cloud that also remembers what was sent to it."""

    def __init__(self) -> None:
        super().__init__()
        self.headers: list[str] = []
        self.paths: list[str] = []
        self.chatter = True            # send "extra" events (a session description) that the client should not see
        self.error_event: dict | None = None
        self.log: list[str] = []       # types of the messages received, in order
        self.connections: list = []    # open sockets, so that the cloud can be closed "from its side"
        self.delete_errors = False     # answer any deletion with a refusal, as for an item that is already deleted

    async def handler(self, ws) -> None:
        self.headers.append(ws.request.headers.get("Authorization", ""))
        self.paths.append(ws.request.path)
        self.connections.append(ws)
        if self.chatter:
            await ws.send(json.dumps({
                "type": "session.created",
                "session": {"model": "gpt-realtime-mini", "instructions": "internals"},
            }))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.log.append(msg.get("type", ""))
                if msg.get("type") == "input_audio_buffer.commit" and self.error_event:
                    await ws.send(json.dumps(self.error_event))
                    continue
                await self._one(ws, msg)
        finally:
            self.connections.remove(ws)

    async def _one(self, ws, msg: dict) -> None:
        """The same parsing as `FakeRealtime.handler`, but for a single message."""
        kind = msg.get("type")
        if kind == "session.update":
            self.session_config = msg["session"]
            await ws.send(json.dumps({"type": "session.updated", "session": msg["session"]}))
        elif kind == "input_audio_buffer.append":
            self.audio_chunks.append(msg["audio"])
        elif kind == "input_audio_buffer.commit":
            item_id = await self._add_item(ws, "user")
            await self._respond(ws, item_id)
        elif kind == "conversation.item.delete":
            self.deleted.append(msg["item_id"])
            if self.delete_errors:
                await ws.send(json.dumps({"type": "error", "error": {
                    "code": "item_not_found", "message": "Item not found",
                    "event_id": msg.get("event_id")}}))
        elif kind == "response.create":
            self.responses_created += 1


@pytest.fixture
async def cloud(monkeypatch):
    from websockets.asyncio.server import serve

    fake = FakeCloud()
    async with serve(fake.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(relay_stt, "UPSTREAM_URL", f"ws://127.0.0.1:{port}")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")
        yield fake


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def token(db) -> str:
    return accounts.register(db, SECRET, "user@example.com", PASSWORD).access


@pytest.fixture
async def relay(db, token, cloud):
    """A real account server on a free port. Returns its port."""
    import uvicorn

    relay_stt._open.clear()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        build(db, SECRET), host="127.0.0.1", port=port,
        log_level="critical", access_log=False, ws="auto",
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("the server did not come up")
    yield port
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


def url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/relay/stt"


async def open_relay(port: int, access: str):
    """Open a session and wait for "ready". Returns the socket and the first event."""
    ws = await websockets.connect(
        url(port), additional_headers={"Authorization": f"Bearer {access}"})
    first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    return ws, first


async def say(ws, **msg) -> None:
    await ws.send(json.dumps(msg))


async def events(ws, until: str, timeout: float = 5.0) -> list[dict]:
    """Read until an event of the needed type arrives (inclusive)."""
    got: list[dict] = []
    async with asyncio.timeout(timeout):
        while True:
            msg = json.loads(await ws.recv())
            got.append(msg)
            if msg["type"] == until:
                return got


async def hear(ws, seconds: float = 1.0) -> str:
    """Hand over an utterance and wait for the transcription."""
    await say(ws, type="input_audio_buffer.append", audio=pcm(seconds))
    await say(ws, type="input_audio_buffer.commit")
    got = await events(ws, "conversation.item.input_audio_transcription.completed")
    return got[-1]["transcript"]


async def settle(ws) -> None:
    """Wait until everything sent so far has reached the cloud and been processed by it.

    For "this did not reach the cloud" checks: first send something that is certain to be let
    through, get the reply to it, and only then look at the cloud.
    """
    await hear(ws, 0.1)


def spent(db, kind="stt_seconds") -> float:
    return entitlements.spent(db, 1, kind)


def held_rows(db) -> int:
    """How many holds are open right now (`entitlements.hold`): one while audio is flowing, none
    after the end.
    """
    return db.execute("SELECT COUNT(*) FROM holds").fetchone()[0]


# --------------------------------------------------------------------------
# Who is let in
# --------------------------------------------------------------------------


async def test_no_pass_no_session(relay, cloud):
    with pytest.raises(websockets.exceptions.InvalidStatus) as e:
        await websockets.connect(url(relay))
    assert e.value.response.status_code == 403
    assert cloud.paths == []                      # the request never reached the cloud


async def test_a_forged_pass_is_turned_away(relay, cloud):
    with pytest.raises(websockets.exceptions.InvalidStatus) as e:
        await websockets.connect(
            url(relay), additional_headers={"Authorization": "Bearer not.a-real-token"})
    assert e.value.response.status_code == 403
    assert cloud.paths == []


async def test_a_deleted_account_is_turned_away(relay, cloud, db, token):
    db.execute("DELETE FROM accounts")
    db.commit()
    with pytest.raises(websockets.exceptions.InvalidStatus):
        await websockets.connect(
            url(relay), additional_headers={"Authorization": f"Bearer {token}"})


# --------------------------------------------------------------------------
# The main path
# --------------------------------------------------------------------------


async def test_speech_goes_through_and_text_comes_back(relay, cloud, token):
    cloud.replies = ["they are pushing mid"]
    ws, first = await open_relay(relay, token)
    async with ws:
        assert first == {"type": "relay.ready"}
        assert await hear(ws) == "they are pushing mid"
    assert len(cloud.audio_chunks) == 1


async def test_the_cloud_gets_our_key_and_our_model_not_the_clients(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await settle(ws)
    assert cloud.headers == ["Bearer sk-test-0123456789"]
    assert cloud.paths == ["/?model=gpt-realtime-mini"]


async def test_the_key_never_reaches_the_client(relay, cloud, token):
    ws, first = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(0.2))
        await say(ws, type="input_audio_buffer.commit")
        got = await events(ws, "conversation.item.input_audio_transcription.completed")
    assert "sk-test" not in json.dumps([first, *got])


async def test_the_session_is_set_up_even_if_the_client_never_says_how(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await settle(ws)
    audio_in = cloud.session_config["audio"]["input"]
    assert audio_in["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert audio_in["turn_detection"] is None


async def test_item_ids_come_through_for_trimming_and_are_deletable(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(0.2))
        await say(ws, type="input_audio_buffer.commit")
        got = await events(ws, "conversation.item.input_audio_transcription.completed")
        added = [m for m in got if m["type"] == "conversation.item.added"]
        assert added and added[0]["item"] == {"id": "item_1"}
        await say(ws, type="conversation.item.delete", item_id="item_1")
        await settle(ws)
    assert cloud.deleted == ["item_1"]


async def test_a_strange_item_id_is_not_passed_on(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="conversation.item.delete", item_id="../../x y")
        await say(ws, type="conversation.item.delete", item_id=5)
        await settle(ws)
    assert cloud.deleted == []


async def test_history_is_capped_by_the_server_even_if_the_client_never_trims(
        relay, cloud, token, monkeypatch):
    """At the cloud, the history is re-billed on every turn: a client that does not trim it would
    drive up our spending.
    """
    monkeypatch.setattr(relay_stt, "MAX_ITEMS", 3)
    ws, _ = await open_relay(relay, token)
    async with ws:
        for _ in range(5):
            await hear(ws, 0.2)
        await asyncio.sleep(0.2)
    assert cloud.deleted == ["item_1", "item_2"]


async def test_a_deletion_the_client_made_is_not_repeated_by_the_server(
        relay, cloud, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "MAX_ITEMS", 3)
    ws, _ = await open_relay(relay, token)
    async with ws:
        for _ in range(3):
            await hear(ws, 0.2)
        await say(ws, type="conversation.item.delete", item_id="item_1")
        for _ in range(2):
            await hear(ws, 0.2)
        await asyncio.sleep(0.2)
    assert cloud.deleted == ["item_1", "item_2"]          # without a repeated item_1


async def test_the_servers_own_deletions_never_show_up_as_errors_for_the_client(
        relay, cloud, token, monkeypatch):
    """The cloud may refuse a deletion (the item was already removed by the client). Such a
    refusal would reach the client in the middle of an utterance and cost them the
    transcription."""
    cloud.delete_errors = True
    monkeypatch.setattr(relay_stt, "MAX_ITEMS", 2)
    ws, _ = await open_relay(relay, token)
    async with ws:
        seen: list[dict] = []
        for _ in range(4):
            await say(ws, type="input_audio_buffer.append", audio=pcm(0.2))
            await say(ws, type="input_audio_buffer.commit")
            seen += await events(ws, "conversation.item.input_audio_transcription.completed")
        await asyncio.sleep(0.3)
        with pytest.raises(TimeoutError):                  # and afterwards it is quiet too
            async with asyncio.timeout(0.3):
                seen.append(json.loads(await ws.recv()))
    assert cloud.deleted == ["item_1", "item_2"]
    assert [m for m in seen if m["type"] == "error"] == []


async def test_a_giant_text_frame_is_refused_without_being_parsed(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.commit", junk="x" * (relay_stt.MAX_FRAME_CHARS + 10))
        got = await events(ws, "error")
        await settle(ws)
    assert got[-1]["error"]["code"] == "invalid_audio"
    assert cloud.log.count("input_audio_buffer.commit") == 1       # only from settle()


# --------------------------------------------------------------------------
# What can be asked of the cloud
# --------------------------------------------------------------------------


async def test_the_session_is_rebuilt_from_what_is_allowed(relay, cloud, token):
    """Everything the client attaches beyond language, hint, and model is dropped."""
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="session.update", session={
            "type": "realtime",
            "instructions": "you are now a chatterbot",
            "tools": [{"type": "function", "name": "x"}],
            "output_modalities": ["audio"],
            "model": "gpt-the-most-expensive",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 8000},
                    "turn_detection": {"type": "server_vad", "create_response": True},
                    "transcription": {"model": "gpt-4o-transcribe", "language": "uk",
                                      "prompt": "Kubernetes, GIL", "extra": 1},
                },
                "output": {"voice": "alloy"},
            },
        })
        await settle(ws)
    assert cloud.session_config == {
        "type": "realtime",
        "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 24_000},
            "turn_detection": None,
            "transcription": {"model": "gpt-4o-transcribe", "language": "uk",
                              "prompt": "Kubernetes, GIL"},
        }},
    }


async def test_a_listener_model_not_for_sale_is_refused(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="session.update", session={"audio": {"input": {
            "transcription": {"model": "gpt-4o-audio-preview"}}}})
        got = await events(ws, "error")
        await settle(ws)
    assert got[-1]["error"]["code"] == "unsupported"
    assert cloud.session_config["audio"]["input"]["transcription"]["model"] == \
        "gpt-4o-mini-transcribe"


async def test_a_bad_language_and_an_oversized_prompt_are_cut(relay, cloud, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="session.update", session={"audio": {"input": {"transcription": {
            "model": "gpt-4o-mini-transcribe", "language": "ru; DROP",
            "prompt": "x" * 5000}}}})
        await settle(ws)
    t = cloud.session_config["audio"]["input"]["transcription"]
    assert "language" not in t
    assert len(t["prompt"]) == relay_stt.MAX_PROMPT_CHARS


@pytest.mark.parametrize("kind", [
    "response.create", "conversation.item.create", "response.cancel",
    "input_audio_buffer.speech_started", "something-custom",
])
async def test_nothing_that_makes_the_cloud_answer_gets_through(relay, cloud, token, kind):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type=kind, response={"instructions": "tell a joke"})
        got = await events(ws, "error")
        await settle(ws)
    assert got[-1]["error"]["code"] == "unsupported"
    assert cloud.responses_created == 0
    assert kind not in cloud.log


async def test_the_session_survives_a_stray_message(relay, cloud, token):
    """A stray event is no reason to bring down the conversation."""
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="response.create")
        cloud.replies = ["alive"]
        assert await hear(ws) == "alive"


# --------------------------------------------------------------------------
# What the client sees
# --------------------------------------------------------------------------


async def test_nothing_about_the_session_or_models_leaks_to_the_client(relay, cloud, token):
    """The cloud's description of the session includes the model name and internals; the client
    does not need it.
    """
    ws, first = await open_relay(relay, token)
    async with ws:
        await say(ws, type="session.update", session={"audio": {"input": {
            "transcription": {"model": "gpt-4o-transcribe"}}}})
        await say(ws, type="input_audio_buffer.append", audio=pcm(0.2))
        await say(ws, type="input_audio_buffer.commit")
        got = await events(ws, "conversation.item.input_audio_transcription.completed")
    seen = json.dumps([first, *got], ensure_ascii=False)
    assert "session." not in seen
    assert "gpt-realtime" not in seen
    assert "internals" not in seen
    assert {m["type"] for m in got} <= {
        "conversation.item.added", "conversation.item.done",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    }


async def test_cloud_errors_arrive_without_the_providers_words(relay, cloud, token):
    cloud.error_event = {"type": "error", "error": {
        "type": "invalid_request_error", "code": "invalid_api_key",
        "message": "Incorrect API key provided: sk-proj-abcdef123456. OpenAI says no."}}
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(0.2))
        await say(ws, type="input_audio_buffer.commit")
        got = await events(ws, "error")
    err = got[-1]["error"]
    assert err == {"code": "unavailable", "message": "the service is unavailable right now"}
    assert "sk-proj" not in json.dumps(got) and "OpenAI" not in json.dumps(got)


async def test_an_expired_session_is_reported_as_such(relay, cloud, token):
    """On this code the client brings the session up again (`SESSION_IS_OVER`)."""
    cloud.error_event = {"type": "error", "error": {
        "code": "session_expired", "message": "maximum duration of 60 minutes"}}
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.commit")
        got = await events(ws, "error")
    assert got[-1]["error"]["code"] == "session_expired"


async def test_a_failed_utterance_comes_without_the_providers_text(relay, cloud, token):
    cloud.item_fails = True
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(0.2))
        await say(ws, type="input_audio_buffer.commit")
        got = await events(ws, "conversation.item.input_audio_transcription.failed")
    assert got[-1]["error"] == {"code": "failed", "message": "the audio wasn't recognized"}


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


async def test_audio_is_charged_by_the_second(relay, cloud, db, token):
    before = entitlements.balance(db, 1)
    ws, _ = await open_relay(relay, token)
    async with ws:
        for _ in range(3):
            await hear(ws, 5.0)                  # 15 seconds: one charge per 10 s, the remainder at close
    await asyncio.sleep(0.3)                     # the server writes the usage after the close
    assert spent(db) == pytest.approx(15.0, abs=0.01)
    assert before - entitlements.balance(db, 1) == pytest.approx(
        pricing.charged(pricing.stt_cost(15.0)), abs=3)


async def test_what_is_not_yet_charged_is_charged_on_close(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 3.0)
        assert spent(db) == 0                    # under ten seconds, so nothing has been charged yet
    await asyncio.sleep(0.3)
    assert spent(db) == pytest.approx(3.0, abs=0.01)


async def test_the_charge_is_written_with_what_it_cost_us(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 4.0)
    await asyncio.sleep(0.3)
    row = db.execute("SELECT kind, model, cost_micro, charged_micro FROM usage").fetchone()
    assert row["kind"] == "stt_seconds"
    assert row["model"] == "gpt-4o-mini-transcribe"
    assert row["cost_micro"] == pricing.stt_cost(4.0)
    assert row["charged_micro"] == pricing.charged(pricing.stt_cost(4.0))


async def test_the_smart_listener_costs_more(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 6.0)
        await say(ws, type="session.update", session={"audio": {"input": {
            "transcription": {"model": "gpt-4o-transcribe"}}}})
        await hear(ws, 6.0)
    await asyncio.sleep(0.3)
    rows = db.execute(
        "SELECT model, amount, cost_micro FROM usage ORDER BY id").fetchall()
    by_model = {r["model"]: r for r in rows}
    assert by_model["gpt-4o-mini-transcribe"]["cost_micro"] == pricing.stt_cost(6.0, "normal")
    assert by_model["gpt-4o-transcribe"]["cost_micro"] == pricing.stt_cost(6.0, "high")
    assert by_model["gpt-4o-transcribe"]["cost_micro"] > by_model["gpt-4o-mini-transcribe"]["cost_micro"]


async def test_no_money_no_session(relay, cloud, db, token):
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()
    ws = await websockets.connect(
        url(relay), additional_headers={"Authorization": f"Bearer {token}"})
    async with ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=5)
    assert first["type"] == "error"
    assert first["error"]["code"] == "insufficient_quota"
    assert "top up" in first["error"]["message"]
    assert closed.value.rcvd.code == 4402
    assert cloud.paths == []                     # the cloud was not even opened


async def test_money_running_out_stops_the_audio(relay, cloud, db, token):
    """Money is enough for a couple of seconds, while the utterance lasts half a minute: it will
    not reach the cloud.
    """
    need_2s = pricing.charged(pricing.stt_cost(2.0))
    need_open = pricing.charged(pricing.stt_cost(relay_stt.MIN_RESERVE_S))
    db.execute("UPDATE accounts SET balance_micro = ?", (max(need_open, need_2s) + 10,))
    db.commit()
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(34.0))
        got = await events(ws, "error")
        with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), timeout=5)
    assert got[-1]["error"]["code"] == "insufficient_quota"
    assert closed.value.rcvd.code == 4402
    assert cloud.audio_chunks == []


async def test_unpaid_audio_counts_against_the_next_chunk(relay, cloud, db, token, monkeypatch):
    """Until audio is charged, it still ties up money: otherwise a series of small chunks would
    get around the check. There is enough for five seconds but not for ten, so the second five
    do not pass."""
    monkeypatch.setattr(relay_stt, "MIN_RESERVE_S", 1.0)
    db.execute("UPDATE accounts SET balance_micro = ?",
               (pricing.charged(pricing.stt_cost(7.0)),))
    db.commit()
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(5.0))
        await say(ws, type="input_audio_buffer.append", audio=pcm(5.0))
        got = await events(ws, "error")
    await asyncio.sleep(0.2)
    assert got[-1]["error"]["code"] == "insufficient_quota"
    assert len(cloud.audio_chunks) == 1
    # The first five seconds went to the cloud and were paid at close; nothing more was taken
    # from the person.
    assert spent(db) == pytest.approx(5.0, abs=0.01)


async def test_sessions_of_one_account_share_one_balance(relay, cloud, db, token, monkeypatch):
    """The app needs two sessions (one channel for each), and an attacker needs no more than that:
    if the balance were checked in each one separately, both would let through audio that the
    money covers only once, and the charge would come twice from one balance, at our expense.
    The money is shared, so the reserve is shared."""
    monkeypatch.setattr(relay_stt, "MIN_RESERVE_S", 1.0)
    db.execute("UPDATE accounts SET balance_micro = ?", (pricing.charged(pricing.stt_cost(7.0)),))
    db.commit()
    (a, _), (b, _) = await asyncio.gather(open_relay(relay, token), open_relay(relay, token))

    async def refused(ws) -> bool:
        try:
            async with asyncio.timeout(1.5):
                while True:
                    if json.loads(await ws.recv()).get("error", {}).get("code") == "insufficient_quota":
                        return True
        except (TimeoutError, websockets.exceptions.ConnectionClosed):
            return False

    async with a, b:
        await asyncio.gather(say(a, type="input_audio_buffer.append", audio=pcm(5.0)),
                             say(b, type="input_audio_buffer.append", audio=pcm(5.0)))
        results = await asyncio.gather(refused(a), refused(b))
    await asyncio.sleep(0.3)
    assert sorted(results) == [False, True], "a five-second piece went through twice with money for one"
    assert len(cloud.audio_chunks) == 1
    assert spent(db) == pytest.approx(5.0, abs=0.01)
    assert held_rows(db) == 0                       # and it removed its own reserve
    assert entitlements.balance(db, 1) == pricing.charged(pricing.stt_cost(7.0)) - pricing.charged(
        pricing.stt_cost(5.0))


async def test_the_shared_reserve_is_released_when_a_session_ends(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 2.0)
        assert held_rows(db) == 1                     # until it is charged, the money is held in the database
    await asyncio.sleep(0.3)
    assert held_rows(db) == 0
    assert spent(db) == pytest.approx(2.0, abs=0.01)


async def test_a_refused_chunk_is_not_charged(relay, cloud, db, token):
    db.execute("UPDATE accounts SET balance_micro = ?",
               (pricing.charged(pricing.stt_cost(relay_stt.MIN_RESERVE_S)) + 5,))
    db.commit()
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(100.0))
        await events(ws, "error")
    await asyncio.sleep(0.3)
    assert spent(db) == 0


async def test_the_balance_never_goes_below_zero(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        for _ in range(3):
            await hear(ws, 5.0)
    await asyncio.sleep(0.3)
    assert entitlements.balance(db, 1) >= 0


# --------------------------------------------------------------------------
# Sound that is not sound
# --------------------------------------------------------------------------


@pytest.mark.parametrize("what", ["empty", "null", "number", "too-long"])
async def test_not_audio_is_not_forwarded_or_charged(relay, cloud, db, token, what):
    audio = {"empty": "", "null": None, "number": 5,
             "too-long": "x" * (relay_stt.MAX_APPEND_B64 + 1)}[what]
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=audio)
        got = await events(ws, "error")
        await settle(ws)
    assert got[-1]["error"]["code"] == "invalid_audio"
    assert len(cloud.audio_chunks) == 1          # only what settle() brought


def test_audio_length_is_read_from_the_encoding():
    assert relay_stt.audio_seconds(pcm(1.0)) == pytest.approx(1.0)
    assert relay_stt.audio_seconds(pcm(2.5)) == pytest.approx(2.5, abs=0.001)
    assert relay_stt.audio_seconds("") is None
    assert relay_stt.audio_seconds(None) is None


# --------------------------------------------------------------------------
# Safety limits
# --------------------------------------------------------------------------


async def test_not_more_than_a_few_sessions_per_account(relay, cloud, token):
    opened = []
    try:
        for _ in range(relay_stt.MAX_SESSIONS):
            ws, first = await open_relay(relay, token)
            assert first == {"type": "relay.ready"}
            opened.append(ws)
        ws, first = await open_relay(relay, token)
        opened.append(ws)
        assert first["type"] == "error"
        assert first["error"]["code"] == "too_many_sessions"
    finally:
        for ws in opened:
            await ws.close()


async def test_a_closed_session_frees_its_place(relay, cloud, token):
    for _ in range(relay_stt.MAX_SESSIONS + 2):
        ws, first = await open_relay(relay, token)
        assert first == {"type": "relay.ready"}
        await ws.close()
        await asyncio.sleep(0.1)


async def test_a_silent_client_is_let_go(relay, cloud, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "IDLE_S", 0.3)
    ws, _ = await open_relay(relay, token)
    async with ws:
        got = await events(ws, "error", timeout=3)
    assert got[-1]["error"]["code"] == "idle"


async def test_when_the_cloud_is_down_the_person_is_told_and_not_charged(
        relay, db, token, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")
    monkeypatch.setattr(relay_stt, "UPSTREAM_URL", f"ws://127.0.0.1:{free_port()}")
    ws = await websockets.connect(
        url(relay), additional_headers={"Authorization": f"Bearer {token}"})
    async with ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    assert first["error"] == {"code": "unavailable", "message": "the service is unavailable right now"}
    assert spent(db) == 0
    assert relay_stt._open == {}


async def test_a_cloud_that_closes_right_after_opening_is_a_refusal_not_a_crash(
        relay, cloud, db, token, monkeypatch):
    def broken():
        raise OSError("closed")

    monkeypatch.setattr(relay_stt, "default_session", broken)
    ws = await websockets.connect(
        url(relay), additional_headers={"Authorization": f"Bearer {token}"})
    async with ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert first["error"]["code"] == "unavailable"
    await asyncio.sleep(0.2)
    assert relay_stt._open == {}
    assert spent(db) == 0


async def test_without_a_key_on_the_server_the_person_is_told_it_is_unavailable(
        relay, cloud, token, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ws = await websockets.connect(
        url(relay), additional_headers={"Authorization": f"Bearer {token}"})
    async with ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert first["error"]["code"] == "unavailable"
    assert cloud.paths == []


async def test_the_cloud_closing_ends_the_session_cleanly(relay, cloud, db, token):
    """The cloud closed (session ceiling): the client must see the close, not endless silence."""
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 1.0)
        for theirs in list(cloud.connections):
            await theirs.close()
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            async with asyncio.timeout(5):
                while True:
                    await ws.recv()
    await asyncio.sleep(0.2)
    assert relay_stt._open == {}
    assert spent(db) == pytest.approx(1.0, abs=0.01)


# --------------------------------------------------------------------------
# Silence in the log
# --------------------------------------------------------------------------


async def test_what_was_said_is_never_written_to_the_log(relay, cloud, token, caplog):
    """The policy promises that the server does not store the content of the conversation. A log
    is storage too.
    """
    import logging

    # We look at the log of our own package. The socket library prints frames itself at DEBUG
    # level (including in this same process, for the fake cloud and the test client), but on
    # the server it runs at WARNING level.
    caplog.set_level(logging.DEBUG, logger="argus")
    cloud.replies = ["a confidential phrase about payroll"]
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="session.update", session={"audio": {"input": {
            "transcription": {"model": "gpt-4o-mini-transcribe", "prompt": "hidden-word"}}}})
        await hear(ws, 1.0)
    await asyncio.sleep(0.2)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("argus"))
    assert "confidential" not in ours and "payroll" not in ours
    assert "hidden-word" not in ours
    assert pcm(1.0)[:40] not in ours
    assert "sk-test" not in ours
    assert "recognition relay" in ours         # but the session line itself is there


# --------------------------------------------------------------------------
# Hold: audio is paid for in advance, not afterwards
# --------------------------------------------------------------------------
#
# Before, the relay looked at the balance, let the audio through and charged later: an open
# session lived on the check made at connect time, and a long socket could take more than
# there was. Now, as with answers (`test_server_money_hold.py`), the money is held in the
# database before a chunk goes out.


def hold_micro(db) -> int:
    return db.execute("SELECT COALESCE(SUM(micro), 0) FROM holds").fetchone()[0]


def set_balance(db, micro: int) -> None:
    db.execute("UPDATE accounts SET balance_micro = ?", (micro,))
    db.commit()


async def closed_with(ws, timeout: float = 5.0) -> int:
    """The code with which the server closed the socket (after everything that was said has been
    read).
    """
    with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
        async with asyncio.timeout(timeout):
            while True:
                await ws.recv()
    return closed.value.rcvd.code


async def test_audio_is_held_before_it_goes_out_and_the_window_is_bounded(relay, cloud, db, token):
    before = entitlements.balance(db, 1)
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 2.0)
        window = pricing.charged(pricing.stt_cost(relay_stt.HOLD_S))
        assert hold_micro(db) == window                # a window of one minute of audio, not the whole balance
        assert entitlements.balance(db, 1) == before - window
    await asyncio.sleep(0.3)
    assert held_rows(db) == 0
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.stt_cost(2.0))


async def test_a_balance_smaller_than_the_window_holds_only_what_it_has(
        relay, cloud, db, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "MIN_RESERVE_S", 1.0)
    small = pricing.charged(pricing.stt_cost(7.0))
    set_balance(db, small)
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 5.0)
        assert hold_micro(db) == small and entitlements.balance(db, 1) == 0
    await asyncio.sleep(0.3)
    assert entitlements.balance(db, 1) == small - pricing.charged(pricing.stt_cost(5.0))
    assert held_rows(db) == 0


async def test_the_window_is_renewed_and_everything_is_settled_exactly(
        relay, cloud, db, token, monkeypatch):
    """A five-second window, chunks of three: every second chunk does not fit, so the window is
    closed with the real spend and a new one is opened. The total is exactly what went out."""
    monkeypatch.setattr(relay_stt, "HOLD_S", 5.0)
    monkeypatch.setattr(relay_stt, "FLUSH_S", 10_000.0)           # charge only when the window changes
    before = entitlements.balance(db, 1)
    ws, _ = await open_relay(relay, token)
    async with ws:
        for _ in range(3):
            await hear(ws, 3.0)
        assert held_rows(db) == 1
    await asyncio.sleep(0.3)
    assert held_rows(db) == 0
    assert spent(db) == pytest.approx(9.0, abs=0.01)
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 3          # each window has its own row
    charged = db.execute("SELECT SUM(charged_micro) FROM usage").fetchone()[0]
    assert entitlements.balance(db, 1) == before - charged


async def test_money_in_a_running_window_is_not_available_to_a_parallel_call(relay, cloud, db, token):
    """Money held for audio is not free balance: a parallel call of the same account will not get
    it, and it is honestly said why (the money has not run out, it is tied up)."""
    window = pricing.charged(pricing.stt_cost(relay_stt.HOLD_S))
    set_balance(db, window + 100)
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 1.0)
        assert entitlements.balance(db, 1) == 100
        denied = entitlements.hold(db, 1, 500)
        assert not denied.allowed
        assert "held" in denied.reason and "top up" not in denied.reason
    await asyncio.sleep(0.3)
    assert entitlements.hold(db, 1, 500).allowed          # the session is over, so the money is free again


async def test_a_dropped_connection_settles_what_went_out_and_gives_back_the_rest(
        relay, cloud, db, token):
    before = entitlements.balance(db, 1)
    ws, _ = await open_relay(relay, token)
    await say(ws, type="input_audio_buffer.append", audio=pcm(3.0))
    for _ in range(100):
        if cloud.audio_chunks:
            break
        await asyncio.sleep(0.05)
    ws.transport.abort()                                   # without a goodbye, the way a network drops
    for _ in range(100):
        if not relay_stt._open and not held_rows(db):
            break
        await asyncio.sleep(0.05)
    assert relay_stt._open == {} and held_rows(db) == 0
    assert spent(db) == pytest.approx(3.0, abs=0.01)
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.stt_cost(3.0))


class _Socket:
    """A minimal WebSocket for calling `serve` directly: what was said to the client is in
    `sent`.
    """

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed: int | None = None

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    def put(self, **msg) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(msg)})


async def test_a_cancelled_session_settles_what_went_out_and_gives_back_the_rest(cloud, db, token):
    """The server is stopped in the middle of a session (task cancellation): the held money must
    not be lost.
    """
    relay_stt._open.clear()
    before = entitlements.balance(db, 1)
    sock = _Socket()
    task = asyncio.create_task(relay_stt.serve(sock, db, 1))
    sock.put(type="input_audio_buffer.append", audio=pcm(2.0))
    for _ in range(100):
        if cloud.audio_chunks:
            break
        await asyncio.sleep(0.05)
    assert cloud.audio_chunks and held_rows(db) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert held_rows(db) == 0 and relay_stt._open == {}
    assert spent(db) == pytest.approx(2.0, abs=0.01)
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.stt_cost(2.0))


async def test_a_session_that_ends_with_an_error_gives_the_money_back(cloud, db, token, monkeypatch):
    """Whatever disaster interrupts the session, the held money is returned (and `recover_holds`
    is there for the last resort).
    """
    relay_stt._open.clear()
    before = entitlements.balance(db, 1)
    sock = _Socket()

    def broken(self, seconds):
        raise RuntimeError("failure while counting")

    monkeypatch.setattr(relay_stt._Bill, "confirm", broken)
    sock.put(type="input_audio_buffer.append", audio=pcm(2.0))
    await asyncio.wait_for(relay_stt.serve(sock, db, 1), timeout=10)
    assert held_rows(db) == 0
    assert entitlements.balance(db, 1) == before                  # nothing charged, everything came back


# --------------------------------------------------------------------------
# An open session does not live on the check made at connect time
# --------------------------------------------------------------------------


async def test_a_blocked_account_gets_no_more_audio_through(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 1.0)
        db.execute("UPDATE accounts SET blocked_at = 1")
        db.commit()
        await say(ws, type="input_audio_buffer.append", audio=pcm(1.0))
        got = await events(ws, "error")
        code = await closed_with(ws)
    assert got[-1]["error"] == {"code": "account_closed", "message": "account is closed"}
    assert code == 4403
    assert len(cloud.audio_chunks) == 1                           # the second chunk never reached the cloud
    await asyncio.sleep(0.3)
    assert held_rows(db) == 0 and spent(db) == pytest.approx(1.0, abs=0.01)


async def test_a_blocked_account_is_let_go_even_when_it_says_nothing(relay, cloud, db, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "CHECK_S", 0.15)
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 1.0)
        db.execute("UPDATE accounts SET blocked_at = 1")
        db.commit()
        got = await events(ws, "error", timeout=3)
        code = await closed_with(ws)
    assert got[-1]["error"]["code"] == "account_closed" and code == 4403
    await asyncio.sleep(0.3)
    assert relay_stt._open == {} and held_rows(db) == 0
    assert spent(db) == pytest.approx(1.0, abs=0.01)


async def test_a_deleted_account_is_let_go_too(relay, cloud, db, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "CHECK_S", 0.15)
    ws, _ = await open_relay(relay, token)
    async with ws:
        db.execute("DELETE FROM accounts")
        db.commit()
        got = await events(ws, "error", timeout=3)
    assert got[-1]["error"]["code"] == "account_closed"


async def test_an_account_that_ran_out_of_money_while_silent_is_let_go(
        relay, cloud, db, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "CHECK_S", 0.15)
    monkeypatch.setattr(relay_stt, "IDLE_FLUSH_S", 0.05)
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 1.0)
        await asyncio.sleep(0.5)                          # quiet: the usage is handed in, the window came back
        assert held_rows(db) == 0
        set_balance(db, 0)                                # the money went another way
        got = await events(ws, "error", timeout=3)
        code = await closed_with(ws)
    assert got[-1]["error"]["code"] == "insufficient_quota"
    assert "empty" in got[-1]["error"]["message"] or "top up" in got[-1]["error"]["message"]
    assert code == 4402


async def test_silence_gives_the_window_back_and_the_session_keeps_working(
        relay, cloud, db, token, monkeypatch):
    """A silent session must not hold money, but the conversation must not be interrupted because
    of that.
    """
    monkeypatch.setattr(relay_stt, "CHECK_S", 0.15)
    monkeypatch.setattr(relay_stt, "IDLE_FLUSH_S", 0.05)
    ws, _ = await open_relay(relay, token)
    async with ws:
        cloud.replies = ["first", "second"]
        assert await hear(ws, 1.0) == "first"
        await asyncio.sleep(0.6)
        assert held_rows(db) == 0
        assert spent(db) == pytest.approx(1.0, abs=0.01)  # the usage is already recorded, the socket is alive
        assert await hear(ws, 1.0) == "second"
    await asyncio.sleep(0.3)
    assert spent(db) == pytest.approx(2.0, abs=0.01)


async def test_money_tied_up_in_the_persons_own_calls_does_not_close_the_session(
        relay, cloud, db, token, monkeypatch):
    """Free balance is zero, but the money has not run out: it is in the same person's running
    answer.
    """
    monkeypatch.setattr(relay_stt, "CHECK_S", 0.1)
    ws, _ = await open_relay(relay, token)
    async with ws:
        mine = entitlements.hold(db, 1, entitlements.balance(db, 1))
        assert mine.allowed and entitlements.balance(db, 1) == 0
        await asyncio.sleep(0.5)                          # a few scheduled checks
        entitlements.release(db, mine)
        cloud.replies = ["I hear you"]
        assert await hear(ws, 1.0) == "I hear you"             # the session is alive


async def test_a_living_account_is_not_bothered_by_the_checks(relay, cloud, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "CHECK_S", 0.1)
    ws, _ = await open_relay(relay, token)
    async with ws:
        await asyncio.sleep(0.6)
        cloud.replies = ["still here"]
        assert await hear(ws, 0.5) == "still here"


async def test_the_whole_server_has_a_cap_on_sessions_too(relay, cloud, token, monkeypatch):
    monkeypatch.setattr(relay_stt, "MAX_SESSIONS_TOTAL", 2)
    opened = []
    try:
        for _ in range(2):
            ws, first = await open_relay(relay, token)
            assert first == {"type": "relay.ready"}
            opened.append(ws)
        ws, first = await open_relay(relay, token)
        opened.append(ws)
        assert first["type"] == "error" and first["error"]["code"] == "unavailable"
        assert "overloaded" in first["error"]["message"]
    finally:
        for ws in opened:
            await ws.close()


# --------------------------------------------------------------------------
# Audit 2026-10-16: the audio count cannot be understated, frames are limited, the cloud
# does not answer on its own
# --------------------------------------------------------------------------


@pytest.mark.parametrize("audio", [
    pcm(1.0) + "=====",                                # extra "=" signs at the tail would understate the count
    pcm(1.0)[:-4] + "!!!!",                            # not base64
    base64.b64encode(bytes(3)).decode(),         # odd number of bytes, so not 16-bit audio
    "",
], ids=["extra-padding", "not-base64", "odd-byte-count", "empty"])
async def test_a_chunk_that_is_not_clean_audio_is_refused_and_never_billed(relay, cloud, db, token, audio):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=audio)
        got = await events(ws, "error")
    assert got[-1]["error"]["code"] == "invalid_audio"
    assert cloud.audio_chunks == []
    assert spent(db) == 0


async def test_a_chunk_longer_than_any_real_utterance_is_refused(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(40.0))
        got = await events(ws, "error")
    assert got[-1]["error"]["code"] == "invalid_audio"
    assert cloud.audio_chunks == []


async def test_the_audio_length_is_counted_by_decoding_not_by_string_length():
    seconds = relay_stt.audio_seconds(pcm(3.0))
    assert seconds == pytest.approx(3.0)
    assert relay_stt.audio_seconds(pcm(3.0) + "=" * 40) is None


async def test_a_very_short_utterance_is_billed_at_the_floor(relay, cloud, db, token):
    """Every utterance is a separate call to the cloud; a stream of 0.1 s scraps must not cost us
    more than we charge for them.
    """
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 0.1)
    await asyncio.sleep(0.3)
    assert spent(db) == pytest.approx(relay_stt.MIN_COMMIT_S, abs=0.01)


async def test_a_normal_utterance_is_billed_for_its_audio_only(relay, cloud, db, token):
    ws, _ = await open_relay(relay, token)
    async with ws:
        await hear(ws, 3.0)
    await asyncio.sleep(0.3)
    assert spent(db) == pytest.approx(3.0, abs=0.01)


async def test_a_cloud_that_starts_answering_by_itself_ends_the_session(relay, cloud, db, token):
    """The session is built so that the cloud does not answer. If it does answer, the condition
    is violated and we are the ones paying: we cut the connection.
    """
    cloud.error_event = {"type": "response.created", "response": {"id": "resp_1"}}
    ws, _ = await open_relay(relay, token)
    async with ws:
        await say(ws, type="input_audio_buffer.append", audio=pcm(1.0))
        await say(ws, type="input_audio_buffer.commit")
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            async with asyncio.timeout(5):
                while True:
                    await ws.recv()
    await asyncio.sleep(0.2)
    assert "response.cancel" in cloud.log
    assert held_rows(db) == 0
