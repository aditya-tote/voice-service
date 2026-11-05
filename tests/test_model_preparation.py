"""A slow model must be ready before any telephone dial side effect."""

import asyncio

import pytest
from test_vih_failures import live_settings
from test_vih_poc import body

from voice_harness import call_service
from voice_harness.call_service import CallService
from voice_harness.models import StartCall
from voice_harness.personaplex import PersonaPlexError
from voice_harness.repository import CallRepository
from voice_harness.vih_client import DialResult


@pytest.fixture
def prepared_service(tmp_path, monkeypatch):
    calls = CallService(live_settings(tmp_path))
    clients = []
    entering, ready = asyncio.Event(), asyncio.Event()

    class Model:
        def __init__(self, url, timeout):
            self.url, self.timeout = url, timeout
            self.connected = self.closed = False
            clients.append(self)

        async def connect(self):
            entering.set()
            await ready.wait()
            self.connected = True

        async def close(self):
            self.closed = True

    monkeypatch.setattr(call_service, "find_spec", lambda _: object())
    monkeypatch.setattr(call_service, "PersonaPlexClient", Model)
    return calls, clients, entering, ready


@pytest.mark.asyncio
async def test_waits_for_actual_ready_before_dial_and_reuses_the_same_session(prepared_service):
    calls, clients, entering, ready = prepared_service
    dialed = []

    async def dial(*args):
        assert clients[0].connected
        assert "Approved+call+script" in clients[0].url
        dialed.append(args)
        return DialResult("submitted", {})

    calls.vih.dial = dial
    request = StartCall.model_validate(body())
    task = asyncio.create_task(calls.start(request, "prepare-before-dial"))
    await entering.wait()
    assert not dialed
    second = await calls.start(request, "reject-during-prepare")
    assert calls.result(second.callId)["dial"]["status"] == "gpu_unavailable"
    ready.set()
    first = await task
    assert first.state == "dial_submitted" and len(dialed) == 1
    replay = await calls.start(request, "prepare-before-dial")
    assert replay.callId == first.callId and len(clients) == 1
    client = calls.take_prepared_model(first.callId)
    assert client is clients[0] and not client.closed
    await asyncio.sleep(0)
    assert not client.closed  # cancelling expiry must not close a transferred session
    with pytest.raises(PersonaPlexError):
        calls.take_prepared_model(first.callId)
    await client.close()
    await calls.close()
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_prepare_timeout_closes_model_releases_lease_and_never_dials(prepared_service):
    calls, clients, _, _ = prepared_service
    object.__setattr__(calls.settings, "personaplex_prepare_timeout_seconds", 0.01)

    async def forbidden(*_):
        raise AssertionError("unready model must never dial")

    calls.vih.dial = forbidden
    result = await calls.start(StartCall.model_validate(body()), "model-ready-timeout")
    assert result.state == "failed"
    assert calls.result(result.callId)["dial"]["status"] == "runtime_not_ready"
    assert calls.repository.get(result.callId)["gpu_lease"] == 0
    assert clients[0].closed
    assert not calls._prepared_models
    await calls.close()
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_cancel_during_preparation_never_dials_and_releases_lease(prepared_service):
    calls, clients, entering, _ = prepared_service

    async def forbidden(*_):
        raise AssertionError("cancelled preparation must never dial")

    calls.vih.dial = forbidden
    task = asyncio.create_task(calls.start(StartCall.model_validate(body()), "cancel-model-prepare"))
    await entering.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].closed
    assert calls.repository.connection.execute("SELECT COUNT(*) FROM calls WHERE gpu_lease=1").fetchone()[0] == 0
    await calls.close()
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_unknown_submission_keeps_session_but_expiry_retains_provider_reservation(prepared_service):
    calls, clients, _, ready = prepared_service
    object.__setattr__(calls.settings, "session_ttl_seconds", 0.03)
    ready.set()

    async def dial(*_):
        return DialResult("unknown", {"error": "timeout"})

    calls.vih.dial = dial
    result = await calls.start(StartCall.model_validate(body()), "unknown-provider-outcome")
    assert result.state == "dial_unknown" and not clients[0].closed
    expiry = calls._model_expiries[result.callId]
    await asyncio.wait_for(expiry, 1)
    record = calls.repository.get(result.callId)
    assert record["state"] == "recovery_required" and record["gpu_lease"] == 1
    assert record["media_token_digest"] is None and clients[0].closed
    await calls.close()
    calls.repository.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["cdr", "shutdown", "failed_dial"])
async def test_pending_model_cleanup(prepared_service, cleanup):
    calls, clients, _, ready = prepared_service
    ready.set()

    async def dial(*_):
        return DialResult("failed" if cleanup == "failed_dial" else "submitted", {})

    calls.vih.dial = dial
    result = await calls.start(StartCall.model_validate(body()), "cleanup-model-session")
    if cleanup == "cdr":
        calls.repository.bind_provider_ids(result.callId, call_sid="CA-ended")
        call_id, _ = calls.cdr({"event": "cdr", "callSid": "CA-ended", "disposition": "NO_ANSWER"})
        await calls.release_model_if_terminal(call_id)
    elif cleanup == "shutdown":
        await calls.close()
    assert clients[0].closed and not calls._prepared_models and not calls._model_expiries
    await calls.close()
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_cancel_during_dial_retains_unknown_outcome_and_prepared_model(prepared_service):
    calls, clients, _, ready = prepared_service
    ready.set()
    dialing = asyncio.Event()

    async def dial(*_):
        dialing.set()
        await asyncio.Event().wait()

    calls.vih.dial = dial
    task = asyncio.create_task(calls.start(StartCall.model_validate(body()), "cancel-provider-post"))
    await dialing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = dict(calls.repository.connection.execute("SELECT * FROM calls").fetchone())
    assert record["state"] == "dial_unknown" and record["gpu_lease"] == 1
    assert not clients[0].closed and record["id"] in calls._prepared_models
    await calls.close()
    assert clients[0].closed
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_real_websocket_ready_precedes_dial_and_connection_is_reused(tmp_path, monkeypatch):
    pytest.importorskip("websockets.asyncio.server")
    from websockets.asyncio.server import serve

    connected, allow_ready = asyncio.Event(), asyncio.Event()
    connections = []
    dialed = []

    async def handler(socket):
        connections.append(socket)
        connected.set()
        await allow_ready.wait()
        await socket.send(b"\x00")
        assert await socket.recv() == b"\x01caller-opus"
        await socket.send(b"\x02Hello")
        await socket.wait_closed()

    monkeypatch.setattr(call_service, "find_spec", lambda _: object())
    async with serve(handler, "127.0.0.1", 0) as server:
        config = live_settings(tmp_path)
        port = server.sockets[0].getsockname()[1]
        object.__setattr__(config, "personaplex_url", f"ws://127.0.0.1:{port}/api/chat")
        calls = CallService(config)

        async def dial(*_):
            assert allow_ready.is_set() and len(calls._prepared_models) == 1
            dialed.append(True)
            return DialResult("submitted", {})

        calls.vih.dial = dial
        task = asyncio.create_task(calls.start(StartCall.model_validate(body()), "real-websocket-prepare"))
        client = None
        try:
            await asyncio.wait_for(connected.wait(), 2)
            assert not dialed
            allow_ready.set()
            result = await asyncio.wait_for(task, 2)
            client = calls.take_prepared_model(result.callId)
            await client.send_opus(b"caller-opus")
            assert await asyncio.wait_for(client.recv(), 2) == b"\x02Hello"
            assert len(connections) == len(dialed) == 1
        finally:
            allow_ready.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if client:
                await client.close()
            await calls.close()
            calls.repository.connection.close()


def test_restart_releases_only_pre_dial_preparation_not_an_uncertain_submission(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    repository = CallRepository(path)
    preparing, _ = repository.create_or_replay("crashed-preparation", body(), False)
    repository.update(preparing["id"], gpu_lease=1)
    uncertain, _ = repository.create_or_replay("crashed-provider-post", body(), False)
    repository.update(uncertain["id"], gpu_lease=1, state="dialing")
    repository.connection.close()
    restarted = CallRepository(path)
    try:
        before_dial = restarted.get(preparing["id"])
        assert before_dial["state"] == "failed" and before_dial["gpu_lease"] == 0
        after_dial = restarted.get(uncertain["id"])
        assert after_dial["state"] == "recovery_required" and after_dial["gpu_lease"] == 1
    finally:
        restarted.connection.close()


@pytest.mark.asyncio
async def test_local_failure_after_ready_closes_model_without_dialing(prepared_service, monkeypatch):
    calls, clients, _, ready = prepared_service
    ready.set()

    def fail_token(*_):
        raise RuntimeError("local token storage failed")

    async def forbidden(*_):
        raise AssertionError("local pre-dial failure must never dial")

    monkeypatch.setattr(calls.repository, "set_media_token", fail_token)
    calls.vih.dial = forbidden
    with pytest.raises(RuntimeError, match="local token storage failed"):
        await calls.start(StartCall.model_validate(body()), "local-failure-after-ready")
    assert clients[0].closed and not calls._prepared_models
    record = dict(calls.repository.connection.execute("SELECT * FROM calls").fetchone())
    assert record["state"] == "failed" and record["gpu_lease"] == 0
    await calls.close()
    calls.repository.connection.close()
