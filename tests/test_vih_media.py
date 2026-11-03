"""Exercise real start/service/media/repository paths with model/codec doubles."""

import asyncio
import base64
import json
import struct
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_vih_poc import body, settings

from voice_harness import vih_media
from voice_harness.call_service import CallService
from voice_harness.controller_app import create_controller_app
from voice_harness.models import StartCall
from voice_harness.repository import RepositoryError
from voice_harness.session_store import SessionStore
from voice_harness.vih_client import DialResult
from voice_harness.vih_protocol import (
    VihOutboundAudio,
    VihProtocolError,
    VihStream,
    decode_mulaw,
    encode_mulaw,
)


def start_event():
    return {"event": "start", "sequenceNumber": "1", "streamSid": "MZ-test",
            "start": {"streamSid": "MZ-test", "callSid": "CA-test", "accountSid": "AC-test",
                      "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000,
                                      "bitDepth": 8, "bitRate": 64}}}


class Socket:
    def __init__(self, token):
        self.query_params = {"token": token}
        self.headers = {"authorization": "Bearer " + token}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = False
        self.close_code = None

    async def accept(self):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, message):
        await self.outgoing.put(json.loads(message))

    async def close(self, code=1000):
        self.closed = True
        self.close_code = code

    async def event(self, event):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(event)})


@pytest.mark.asyncio
@pytest.mark.parametrize("cdr_timing", ["disabled", "before_stop", "after_stop"])
async def test_start_to_vih_audio_recording_cdr_and_durable_result(tmp_path, monkeypatch, cdr_timing):
    config = settings(tmp_path, media_auth_mode="query", vih_dial_mode="live",
                      enable_live_dials=True, vih_obd_url="https://vih.example/dial",
                      vih_ukey="not-a-real-key", vih_service_no="service", vih_template_id="wss-template",
                      vih_campaign_type="3",
                      vih_response_call_id_path="" if cdr_timing == "disabled" else "/callSid",
                      vih_wss_value_mode="confirmed_dynamic",
                      vih_cdr_auth_mode="disabled" if cdr_timing == "disabled" else "bearer",
                      vih_cdr_secret="" if cdr_timing == "disabled" else "c" * 32,
                      recording_dir=str(tmp_path / "audio"),
                      vih_wss_value="wss://bridge.example.com/calls/{callId}?token={token}")
    calls = CallService(config)
    dialed = {}

    async def ready():
        return True

    async def dial(phone, wss=None):
        assert phone == body()["customerPhone"]
        dialed["wss"] = wss
        return DialResult("submitted", {"httpStatus": 200, "body": {"callSid": "CA-test"}})

    monkeypatch.setattr(calls, "_runtime_ready", ready)
    monkeypatch.setattr(calls.vih, "dial", dial)
    clients, codecs = [], []

    class Codec:
        def __init__(self):
            self.closed = False
            codecs.append(self)

        def phone_to_opus(self, pcm):
            assert pcm == b"\0\0" * 800
            return [b"phone-opus"]

        def opus_to_phone(self, packet):
            return b"\0\0" * (200 if packet == b"first" else 120)

        def close(self):
            self.closed = True

    class Model:
        def __init__(self, url, timeout):
            prompt = parse_qs(urlsplit(url).query)["text_prompt"][0]
            assert body()["script"] in prompt and body()["context"]["goal"] in prompt
            self.frames, self.sent, self.closed = asyncio.Queue(), [], False
            clients.append(self)

        async def connect(self):
            await self.frames.put(b"\x02Hello customer")

        async def send_opus(self, packet):
            self.sent.append(packet)
            await self.frames.put(b"\x01first")
            await self.frames.put(b"\x01second")

        async def recv(self):
            return await self.frames.get()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(vih_media, "StreamingCodec", Codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    monkeypatch.setattr(vih_media, "MEDIA_CHECKPOINT_SECONDS", 0.01)
    app = create_controller_app(SessionStore(config), calls)
    headers = {"Authorization": "Bearer " + config.bridge_api_key, "Idempotency-Key": "media-poc-1234"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as http:
        response = await http.post("/v1/calls", headers=headers, json=body())
        assert response.status_code == 202
        call_id = response.json()["callId"]
        token = parse_qs(urlsplit(dialed["wss"]).query)["token"][0]
        socket = Socket(token)
        endpoint = next(route.endpoint for route in vih_media.create_vih_media_app(calls).routes
                        if route.path == "/calls/{call_id}")
        task = asyncio.create_task(endpoint(socket, call_id))
        await socket.event({"event": "connected"})
        await socket.event(start_event())
        await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(b"\xff" * 800).decode()}})
        media = []
        while len(media) < 2:
            frame = await asyncio.wait_for(socket.outgoing.get(), 2)
            if frame["event"] == "media":
                media.append(frame)
        assert [frame["media"]["chunk"] for frame in media] == [1, 2]
        assert all(len(base64.b64decode(frame["media"]["payload"])) == 160 for frame in media)
        async with asyncio.timeout(2):
            while calls.result(call_id)["agentText"] != "Hello customer":
                await asyncio.sleep(0.01)
        assert calls.result(call_id)["state"] == "live"
        assert calls.result(call_id)["recordings"]["caller"].endswith(".ulaw")
        cdr = {"event": "cdr", "callSid": "CA-test", "streamSid": "MZ-test", "disposition": "ANSWERED"}
        if cdr_timing == "before_stop":
            calls.cdr(cdr)
            assert calls.repository.get(call_id)["gpu_lease"] == 1
            assert calls.result(call_id)["complete"] is False
        await socket.event({"event": "stop", "sequenceNumber": "3", "streamSid": "MZ-test",
                            "stop": {"callSid": "CA-test", "accountSid": "AC-test", "reason": "done"}})
        await asyncio.wait_for(task, 2)
        assert calls.result(call_id)["state"] == "completed"
        assert calls.repository.get(call_id)["gpu_lease"] == 0
        if cdr_timing == "after_stop":
            calls.cdr(cdr)
        result = (await http.get(response.json()["resultUrl"], headers=headers)).json()
        if cdr_timing == "disabled":
            assert (await http.post("/webhooks/vih/cdr", json=cdr)).status_code == 401
    assert result["complete"] and not result["recoveryRequired"]
    assert result["streamReason"] == "provider_stop"
    if cdr_timing == "disabled":
        assert result["disposition"] is None and result["cdr"] is None
        assert result["correlationStatus"] == "not_received"
    else:
        assert result["disposition"] == "ANSWERED" and result["correlationStatus"] == "matched"
    assert result["agentText"] == "Hello customer" and result["customerTranscript"] is None
    assert result["streamStats"]["inboundAudioBytes"] == 800
    assert result["streamStats"]["outboundAudioBytes"] == 320
    assert (tmp_path / "audio" / result["recordings"]["caller"]).stat().st_size == 800
    assert (tmp_path / "audio" / result["recordings"]["agent"]).stat().st_size == 320
    assert socket.closed and clients[0].closed and codecs[0].closed
    assert clients[0].sent == [b"phone-opus"]
    assert calls.repository.get(call_id)["gpu_lease"] == 0
    assert token not in json.dumps(result)
    with pytest.raises(RepositoryError):
        await calls.claim_media(call_id, token)
    calls.repository.connection.close()
    reopened = CallService(config)
    assert reopened.result(call_id)["agentText"] == "Hello customer"
    assert reopened.result(call_id)["complete"] and not reopened.result(call_id)["recoveryRequired"]
    next_call, _ = reopened.repository.create_or_replay("next-call-after-stop", body(), False)
    assert reopened.repository.acquire_gpu(next_call["id"])
    reopened.repository.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "malformed", "cancel", "binary", "disconnect", "timeout", "invalid_stop"])
async def test_vih_failure_cleanup_preserves_unknown_provider_lease(tmp_path, monkeypatch, failure):
    config = settings(tmp_path, idle_timeout_seconds=0.05 if failure == "timeout" else 60)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("cleanup-1234", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    connected = asyncio.Event()
    clients = []

    class Codec:
        def close(self):
            pass

    class Model:
        def __init__(self, *args):
            self.closed = False
            clients.append(self)

        async def connect(self):
            connected.set()
            if failure == "connect":
                raise RuntimeError("model unavailable")

        async def recv(self):
            if failure == "malformed":
                return b"\x09unexpected"
            await asyncio.Future()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(vih_media, "StreamingCodec", Codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    endpoint = vih_media.create_vih_media_app(calls).routes[0].endpoint
    socket = Socket("one-use-token")
    task = asyncio.create_task(endpoint(socket, call_id))
    await socket.event({"event": "connected"})
    await socket.event(start_event())
    await asyncio.wait_for(connected.wait(), 2)
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif failure == "binary":
        await socket.incoming.put({"type": "websocket.receive", "bytes": b"\0\0"})
        await asyncio.wait_for(task, 2)
    elif failure == "disconnect":
        await socket.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 2)
    elif failure == "invalid_stop":
        await socket.event({"event": "stop", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "stop": {"callSid": "CA-other", "accountSid": "AC-test"}})
        await asyncio.wait_for(task, 2)
    else:
        await asyncio.wait_for(task, 2)
    assert socket.closed and clients[0].closed
    result = calls.result(call_id)
    assert result["state"] == "recovery_required" and not result["complete"]
    assert result["recoveryRequired"]
    assert calls.repository.get(call_id)["gpu_lease"] == 1
    expected = {"cancel": "service_shutdown", "disconnect": "provider_disconnect", "timeout": "call_timeout"}
    assert result["streamReason"] == expected.get(failure, "bridge_error")
    calls.repository.connection.close()


def test_g711_vectors_and_residual_audio():
    assert decode_mulaw(bytes([0, 128, 255, 127])) == struct.pack("<hhhh", -32124, 32124, 0, 0)
    assert encode_mulaw(struct.pack("<hhhh", -32768, 32767, 0, 1000)) == bytes([0, 128, 255, 206])
    outbound = VihOutboundAudio("MZ1")
    assert outbound.encode(b"\0\0" * 100) == []
    assert len(outbound.encode(b"\0\0" * 60)) == 1
    assert outbound.pending == b""
    outbound.encode(b"\0\0" * 20)
    assert len(outbound.flush()) == 1 and not outbound.pending
    assert json.loads(outbound.mark("done"))["mark"]["name"] == "done"
    outbound.encode(b"\0\0" * 20)
    assert json.loads(outbound.clear())["event"] == "clear" and not outbound.pending


@pytest.mark.parametrize("bad", ["binary", "base64", "sid", "duplicate", "format", "stop"])
def test_protocol_rejects_malformed_or_mismatched_events(bad):
    stream = VihStream()
    stream.ingest('{"event":"connected"}')
    event = start_event()
    if bad == "format":
        event["start"]["mediaFormat"]["sampleRate"] = 24000
        with pytest.raises(VihProtocolError):
            stream.ingest(json.dumps(event))
        return
    stream.ingest(json.dumps(event))
    event = {"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
             "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}}
    if bad == "binary":
        event = b"\0\0"
    elif bad == "base64":
        event["media"]["payload"] = "bad!"
    elif bad == "sid":
        event["streamSid"] = "MZ-other"
    elif bad == "duplicate":
        stream.ingest(json.dumps(event))
    elif bad == "stop":
        event = {"event": "stop", "sequenceNumber": "2", "streamSid": "MZ-test",
                 "stop": {"callSid": "CA-other", "accountSid": "AC-test"}}
    with pytest.raises(VihProtocolError):
        stream.ingest(event if isinstance(event, bytes) else json.dumps(event))


def test_model_prompt_contains_script_and_context():
    request = StartCall.model_validate(body())
    prompt = parse_qs(urlsplit(vih_media.model_connection_url("ws://localhost/api/chat?extra=value", request)).query)
    assert request.script in prompt["text_prompt"][0]
    assert request.context.goal in prompt["text_prompt"][0]
    assert prompt["extra"] == ["value"]
