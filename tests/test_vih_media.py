"""Exercise real start/service/media/repository paths with model/codec doubles."""

import asyncio
import base64
import json
import logging
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
async def test_start_to_vih_audio_recording_cdr_and_durable_result(tmp_path, monkeypatch, cdr_timing, caplog):
    caplog.set_level(logging.INFO, logger="voice_harness")
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

    async def ready(call_id, model_url):
        client = Model(model_url, 60)
        await client.connect()
        calls._prepared_models[call_id] = client
        return True

    async def dial(phone, wss=None):
        assert phone == body()["customerPhone"]
        dialed["wss"] = wss
        return DialResult("submitted", {"httpStatus": 200, "body": {"callSid": "CA-test"}})

    monkeypatch.setattr(calls, "_prepare_model", ready)
    monkeypatch.setattr(calls.vih, "dial", dial)
    clients, codecs = [], []

    class Codec:
        def __init__(self):
            self.closed = False
            codecs.append(self)

        def phone_to_opus(self, pcm):
            assert pcm == b"\0\0" * 160
            return [b"phone-opus"]

        def opus_to_phone(self, packet):
            return b"\0\0" * (200 if packet == b"first" else 120)

        def close(self):
            self.closed = True

    class Model:
        def __init__(self, url, timeout):
            prompt = parse_qs(urlsplit(url).query)["text_prompt"][0]
            assert body()["leadContext"]["customerName"] in prompt and "Persona:" not in prompt
            self.frames, self.sent, self.closed = asyncio.Queue(), [], False
            clients.append(self)

        async def connect(self):
            await self.frames.put(b"\x02Hello customer")

        async def send_opus(self, packet):
            self.sent.append(packet)
            if len(self.sent) == 1:
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
    assert clients[0].sent and all(packet == b"phone-opus" for packet in clients[0].sent)
    assert result["streamStats"]["inputRealSamplesSent"] >= 160
    assert result["streamStats"]["interruptionMode"] == "off"
    timeline = tmp_path / "audio" / result["recordings"]["timeline"]
    rows = [json.loads(line) for line in timeline.read_text().splitlines()]
    assert any(row["event"] == "caller_arrival" and row["samples"] == 800 for row in rows)
    assert sum(row["samples"] for row in rows if row["event"] == "provider_send") == 320
    assert calls.repository.get(call_id)["gpu_lease"] == 0
    assert token not in json.dumps(result)
    events = [record.safe_event for record in caplog.records if record.name == "voice_harness"]
    for name in ("call_created", "media_authorized", "stream_started", "model_session_reused",
                 "media_input_started", "media_output_started", "model_text_started", "media_finished"):
        assert any(event["event"] == name and event["call_id"] == call_id for event in events)
    assert any(event["event"] == "media_finished" and event["reason"] == "provider_stop"
               and not event["recovery_required"] for event in events)
    rendered_logs = json.dumps(events)
    for private in (token, body()["customerPhone"], body()["leadContext"]["customerEmail"], "Hello customer", "phone-opus"):
        assert private not in rendered_logs
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
async def test_vih_failure_cleanup_preserves_unknown_provider_lease(tmp_path, monkeypatch, failure, caplog):
    caplog.set_level(logging.INFO, logger="voice_harness")
    config = settings(tmp_path, idle_timeout_seconds=0.05 if failure == "timeout" else 60)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("cleanup-1234", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    connected = asyncio.Event()
    clients = []

    class Codec:
        def phone_to_opus(self, pcm):
            return []

        def close(self):
            pass

    class Model:
        def __init__(self, *args):
            self.closed = False
            clients.append(self)

        async def connect(self):
            connected.set()
            if failure == "connect":
                raise RuntimeError("wss://private.example/?token=do-not-log&text_prompt=secret-script")

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
    events = [record.safe_event for record in caplog.records if record.name == "voice_harness"]
    errors = [event for event in events if event["event"] == "media_failed"]
    assert errors and all(event["call_id"] == call_id for event in errors)
    if failure != "timeout":
        stages = {"connect": "model_connect", "malformed": "model_receive", "cancel": "shutdown"}
        assert errors[0]["stage"] == stages.get(failure, "provider_receive")
    assert any(event["event"] == "media_finished" and event["recovery_required"] for event in events)
    assert "do-not-log" not in json.dumps(events) and "secret-script" not in json.dumps(events)
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


def test_model_prompt_uses_service_policy_and_lead_facts():
    request = StartCall.model_validate(body())
    prompt = parse_qs(urlsplit(vih_media.model_connection_url(
        "ws://localhost/api/chat?extra=value", request.leadContext, "NATF2.pt")).query)
    assert request.leadContext.customerEmail in prompt["text_prompt"][0]
    assert "Persona:" not in prompt["text_prompt"][0]
    assert prompt["extra"] == ["value"]


@pytest.mark.asyncio
async def test_live_media_missing_prepared_session_fails_without_reconnecting(tmp_path, monkeypatch):
    from test_vih_failures import live_settings

    calls = CallService(live_settings(tmp_path))
    record, _ = calls.repository.create_or_replay("missing-prepared-model", body(), False)
    calls.repository.update(record["id"], state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(record["id"], "one-use-token")

    def forbidden(*_, **__):
        raise AssertionError("live media must not construct or connect another model")

    monkeypatch.setattr(vih_media, "PersonaPlexClient", forbidden)
    monkeypatch.setattr(vih_media, "model_connection_url", forbidden)
    socket = Socket("one-use-token")
    await vih_media.create_vih_media_app(calls).routes[0].endpoint(socket, record["id"])
    result = calls.result(record["id"])
    assert socket.closed and result["state"] == "recovery_required"
    assert calls.repository.get(record["id"])["gpu_lease"] == 1
    assert result["customerTranscript"] is None
    calls.repository.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,activity_frames,expected_clears", [
    ("off", 25, 0), ("sustained_energy", 8, 0), ("sustained_energy", 25, 1),
])
async def test_continuous_input_playback_energy_and_stop_authority(
    tmp_path, monkeypatch, mode, activity_frames, expected_clears,
):
    config = settings(tmp_path, media_interruption_mode=mode, media_input_startup_ms=0)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("continuous-media-test", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    clients, codecs = [], []
    class Codec:
        def __init__(self):
            self.inputs, self.decodes, self.closed = [], 0, False
            codecs.append(self)
        def phone_to_opus(self, pcm):
            self.inputs.append(pcm)
            return [b"opus"]
        def opus_to_phone(self, packet):
            self.decodes += 1
            return struct.pack("<hh", -2000, 2000) * 80
        def close(self):
            self.closed = True
    class Model:
        def __init__(self, *args):
            self.frames, self.sent, self.connections, self.closed = asyncio.Queue(), 0, 0, False
            clients.append(self)
        async def connect(self):
            self.connections += 1
        async def send_opus(self, packet):
            self.sent += 1
            await self.frames.put(b"\x01continuous-audio")
        async def recv(self):
            return await self.frames.get()
        async def close(self):
            self.closed = True
    monkeypatch.setattr(vih_media, "StreamingCodec", Codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    socket = Socket("one-use-token")
    task = asyncio.create_task(vih_media.create_vih_media_app(calls).routes[0].endpoint(socket, call_id))
    await socket.event({"event": "connected"})
    await socket.event(start_event())
    # All samples arrive in order, then a deliberate provider media gap. The clock keeps sending silence.
    loud = encode_mulaw(struct.pack("<hh", -3000, 3000) * 80 * activity_frames)
    await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                        "media": {"payload": base64.b64encode(loud).decode()}})
    await asyncio.sleep(.9)
    await socket.event({"event": "stop", "sequenceNumber": "3", "streamSid": "MZ-test",
                        "stop": {"callSid": "CA-test", "accountSid": "AC-test"}})
    await asyncio.wait_for(task, 2)
    output = []
    while not socket.outgoing.empty():
        output.append(socket.outgoing.get_nowait())
    result = calls.result(call_id)
    assert result["streamReason"] == "provider_stop"
    assert result["streamStats"]["interruptionClears"] == expected_clears
    assert sum(frame["event"] == "clear" for frame in output) == expected_clears
    assert result["streamStats"]["marksSent"] == sum(frame["event"] == "mark" for frame in output)
    assert clients[0].connections == 1 and len(clients) == len(codecs) == 1
    assert clients[0].sent > activity_frames
    # Stop may cancel the final just-enqueued model response; decoding continued through overlap.
    assert activity_frames <= codecs[0].decodes <= clients[0].sent
    assert result["streamStats"]["inputSyntheticSilenceSamples"] > 0
    if expected_clears:
        assert result["streamStats"]["outputSuppressedMs"] > 0
    assert clients[0].closed and codecs[0].closed
    assert socket.outgoing.empty()
    assert len(codecs[0].inputs) * 160 == (result["streamStats"]["inputRealSamplesSent"]
                                         + result["streamStats"]["inputSyntheticSilenceSamples"])
    # Recording off: no audio, metadata or output paths are created.
    assert result["recordings"] == {} and not (tmp_path / "audio").exists()
    calls.repository.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_before_clear", [False, True])
async def test_clear_control_does_not_block_input_and_stop_wins_send_lock_race(tmp_path, monkeypatch, stop_before_clear):
    config = settings(tmp_path, media_interruption_mode="sustained_energy", media_input_startup_ms=0,
                      media_output_max_buffer_ms=1000)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("locked-send-media-test", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    release, enough_input = asyncio.Event(), asyncio.Event()
    clients = []
    class Codec:
        def phone_to_opus(self, pcm):
            return [b"opus"]
        def opus_to_phone(self, packet):
            return struct.pack("<hh", -2000, 2000) * 80
        def close(self):
            pass
    class Model:
        def __init__(self, *args):
            self.frames, self.inputs = asyncio.Queue(), 0
            clients.append(self)
        async def connect(self):
            pass
        async def send_opus(self, packet):
            self.inputs += 1
            if self.inputs >= 24:
                enough_input.set()
            await self.frames.put(b"\x01audio")
        async def recv(self):
            return await self.frames.get()
        async def close(self):
            pass
    class LockedSocket(Socket):
        async def send_text(self, message):
            await super().send_text(message)
            if json.loads(message)["event"] == "media" and not release.is_set():
                await release.wait()
    monkeypatch.setattr(vih_media, "StreamingCodec", Codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    socket = LockedSocket("one-use-token")
    await socket.event({"event": "connected"})
    await socket.event(start_event())
    loud = encode_mulaw(struct.pack("<hh", -3000, 3000) * 80 * 30)
    await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                        "media": {"payload": base64.b64encode(loud).decode()}})
    task = asyncio.create_task(vih_media.create_vih_media_app(calls).routes[0].endpoint(socket, call_id))
    await asyncio.wait_for(enough_input.wait(), 2)
    # Clear has confirmed at tick 20. Input still advances while control waits for a slow media send.
    assert clients[0].inputs >= 24
    stop = {"event": "stop", "sequenceNumber": "3", "streamSid": "MZ-test",
            "stop": {"callSid": "CA-test", "accountSid": "AC-test"}}
    if stop_before_clear:
        await socket.event(stop)
        await asyncio.sleep(.02)
    release.set()
    if not stop_before_clear:
        async with asyncio.timeout(2):
            while socket.outgoing.qsize() < 2:
                await asyncio.sleep(.005)
        await socket.event(stop)
    await asyncio.wait_for(task, 2)
    frames = []
    while not socket.outgoing.empty():
        frames.append(socket.outgoing.get_nowait())
    assert [frame["event"] for frame in frames] == (["media"] if stop_before_clear else ["media", "clear"])
    stats = calls.result(call_id)["streamStats"]
    assert stats["interruptionConfirmed"] == 1
    assert stats["outputDiscardedClearMs"] > 0 and stats["outputSuppressedMs"] > 0
    assert calls.result(call_id)["streamReason"] == "provider_stop"
    calls.repository.connection.close()


def test_recording_partial_creation_never_overwrites_existing_files(tmp_path):
    call_id = "call_" + "a" * 36
    existing = tmp_path / f"{call_id}-agent.ulaw"
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        vih_media._Recordings(str(tmp_path), call_id, 8000)
    assert existing.read_bytes() == b"preserve"
    # The partially created caller file is closed, private, and can be reopened by its owner.
    with (tmp_path / f"{call_id}-caller.ulaw").open("ab") as stream:
        stream.write(b"closed cleanly")


@pytest.mark.asyncio
async def test_synthetic_silence_does_not_hide_dead_provider(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="voice_harness")
    config = settings(tmp_path, idle_timeout_seconds=.2, media_input_startup_ms=0)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("dead-provider-media-test", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    class Codec:
        def phone_to_opus(self, pcm):
            assert pcm == b"\0\0" * 160
            return [b"silence-opus"]
        def opus_to_phone(self, packet):
            return b"\0\0" * 160
        def close(self):
            pass
    class Model:
        def __init__(self, *args):
            self.frames = asyncio.Queue()
        async def connect(self):
            pass
        async def send_opus(self, packet):
            # Keep the model receive direction active; provider timeout must be independent.
            await self.frames.put(b"\x01audio")
        async def recv(self):
            return await self.frames.get()
        async def close(self):
            pass
    monkeypatch.setattr(vih_media, "StreamingCodec", Codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    socket = Socket("one-use-token")
    await socket.event({"event": "connected"})
    await socket.event(start_event())
    await asyncio.wait_for(vih_media.create_vih_media_app(calls).routes[0].endpoint(socket, call_id), 2)
    result = calls.result(call_id)
    assert result["streamReason"] == "call_timeout" and result["recoveryRequired"]
    assert result["streamStats"]["inputSyntheticSilenceSamples"] >= 160
    assert result["streamStats"]["inboundMediaFrames"] == 0
    errors = [record.safe_event for record in caplog.records if getattr(record, "safe_event", {}).get("event") == "media_failed"]
    assert errors[0]["stage"] == "provider_receive"
    calls.repository.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["input", "output"])
async def test_media_overflow_fails_bounded_without_releasing_uncertain_lease(tmp_path, monkeypatch, direction):
    config = settings(tmp_path, media_input_max_buffer_ms=100, media_output_max_buffer_ms=100)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("overflow-media-test", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    class Codec:
        def phone_to_opus(self, pcm):
            return []
        def opus_to_phone(self, packet):
            return b"\0\0" * 801
        def close(self):
            pass
    class Model:
        def __init__(self, *args):
            pass
        async def connect(self):
            pass
        async def recv(self):
            if direction == "output":
                return b"\x01burst"
            await asyncio.Future()
        async def close(self):
            pass
    monkeypatch.setattr(vih_media, "StreamingCodec", Codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    socket = Socket("one-use-token")
    await socket.event({"event": "connected"})
    await socket.event(start_event())
    if direction == "input":
        await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(b"\xff" * 801).decode()}})
    await asyncio.wait_for(vih_media.create_vih_media_app(calls).routes[0].endpoint(socket, call_id), 2)
    result = calls.result(call_id)
    assert result["streamStats"][f"{direction}Overflows"] == 1
    assert result["recoveryRequired"] and calls.repository.get(call_id)["gpu_lease"] == 1
    assert socket.outgoing.get_nowait()["event"] == "clear"
    calls.repository.connection.close()
