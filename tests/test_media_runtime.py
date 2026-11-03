"""Supervise the real runtime with deterministic model/codec boundary doubles."""

import asyncio

import pytest
from test_security_regressions import brief, settings

from voice_harness import media_app
from voice_harness.models import SessionState
from voice_harness.session_store import SessionStore


class Socket:
    def __init__(self, token):
        self.headers = {"authorization": token}
        self.query_params = {}
        self.incoming = asyncio.Queue()
        self.output = asyncio.Queue()
        self.closed = False

    async def accept(self):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        await self.output.put(data)

    async def close(self, code=1000):
        self.closed = True


class Codec:
    def phone_to_opus(self, pcm):
        return [b"encoded"]

    def opus_to_phone(self, packet):
        assert packet == b"model-opus"
        return b"\x01\x02" * 160


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "connect", "malformed", "close", "callback"])
async def test_runtime_routes_media_and_releases_resources(monkeypatch, failure):
    callbacks = []
    clients = []

    class Model:
        def __init__(self, url, timeout):
            self.queue = asyncio.Queue()
            self.closed = False
            self.sent = []
            clients.append(self)

        async def connect(self):
            if failure == "connect":
                raise RuntimeError("upstream unavailable")
            await self.queue.put(b"\x09bad" if failure == "malformed" else b"\x02Hello")

        async def send_opus(self, data):
            self.sent.append(data)
            await self.queue.put(b"\x01model-opus")

        async def recv(self):
            return await self.queue.get()

        async def close(self):
            self.closed = True
            if failure == "close":
                raise RuntimeError("broken upstream close")

    async def callback(base, key, identifier, payload):
        callbacks.append(payload)
        if failure == "callback":
            raise RuntimeError("callback unavailable")
        return True

    monkeypatch.setattr(media_app, "PersonaPlexClient", Model)
    monkeypatch.setattr(media_app, "StreamingCodec", Codec)
    monkeypatch.setattr(media_app, "send_callback", callback)
    store = SessionStore(settings())
    created = await store.create(brief())
    socket = Socket(created.streamHeaders["Authorization"])
    endpoint = media_app.create_media_app(store).routes[0].endpoint
    call = asyncio.create_task(endpoint(socket, created.sessionId))
    if failure not in {"connect", "malformed"}:
        await socket.incoming.put({"type": "websocket.receive", "bytes": b"\0" * 320})
        audio = await asyncio.wait_for(socket.output.get(), 2)
        assert len(audio) == 320
        await socket.incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(call, 2)
    assert socket.closed and clients[0].closed
    assert store.live_id is None
    assert (await store.status(created.sessionId)).state == SessionState.ENDED
    assert len(callbacks) == 1
    assert callbacks[0]["customerTranscript"] is None
    if failure not in {"connect", "malformed"}:
        assert callbacks[0]["agentText"] == "Hello"
        assert clients[0].sent == [b"encoded"]
    else:
        assert callbacks[0]["hangupReason"] == "bridge_error"


@pytest.mark.asyncio
async def test_server_cancellation_cleans_up_sibling_tasks(monkeypatch):
    connected = asyncio.Event()
    closed = asyncio.Event()
    callback_done = asyncio.Event()

    class Model:
        def __init__(self, *args):
            pass

        async def connect(self):
            connected.set()

        async def recv(self):
            await asyncio.Future()

        async def close(self):
            closed.set()

    async def callback(*args):
        assert args[-1]["hangupReason"] == "service_shutdown"
        callback_done.set()
        return True

    monkeypatch.setattr(media_app, "PersonaPlexClient", Model)
    monkeypatch.setattr(media_app, "StreamingCodec", Codec)
    monkeypatch.setattr(media_app, "send_callback", callback)
    store = SessionStore(settings())
    created = await store.create(brief())
    socket = Socket(created.streamHeaders["Authorization"])
    endpoint = media_app.create_media_app(store).routes[0].endpoint
    call = asyncio.create_task(endpoint(socket, created.sessionId))
    await asyncio.wait_for(connected.wait(), 2)
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 2)
    assert closed.is_set() and callback_done.is_set() and socket.closed
    assert store.live_id is None
