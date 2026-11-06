"""Independent offline regressions: real media lifecycle, no providers/inference."""

import asyncio
import base64
import json
import logging
import struct
from contextlib import asynccontextmanager

import pytest
from test_vih_media import Socket, start_event
from test_vih_poc import body, settings

from voice_harness import vih_media
from voice_harness.call_service import CallService
from voice_harness.vih_protocol import encode_mulaw

STOP = {"event": "stop", "sequenceNumber": "100", "streamSid": "MZ-test",
        "stop": {"callSid": "CA-test", "accountSid": "AC-test"}}
ZERO = b"\0\0" * 160
LOUD = struct.pack("<hh", -3000, 3000) * 80


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


@asynccontextmanager
async def bridge(tmp_path, monkeypatch, *, socket_type=Socket, **overrides):
    config = settings(tmp_path, media_input_startup_ms=0, **overrides)
    calls = CallService(config)
    record, _ = calls.repository.create_or_replay("independent-qa-call", body(), False)
    call_id = record["id"]
    calls.repository.update(call_id, state="dial_submitted", gpu_lease=1)
    calls.repository.set_media_token(call_id, "one-use-token")
    instances = []

    class Codec:
        def __init__(self):
            self.inputs, self.decoded, self.closed = [], [], False
        def phone_to_opus(self, pcm):
            self.inputs.append(pcm)
            return [b"opus"]
        def opus_to_phone(self, packet):
            self.decoded.append(packet)
            return packet
        def close(self):
            self.closed = True

    codec = Codec()

    class Model:
        def __init__(self, *args):
            self.frames, self.closed = asyncio.Queue(), False
            instances.append(self)
        async def connect(self):
            pass
        async def send_opus(self, packet):
            pass
        async def recv(self):
            return await self.frames.get()
        async def close(self):
            self.closed = True

    monkeypatch.setattr(vih_media, "StreamingCodec", lambda: codec)
    monkeypatch.setattr(vih_media, "PersonaPlexClient", Model)
    socket = socket_type("one-use-token")
    await socket.event({"event": "connected"})
    await socket.event(start_event())
    task = asyncio.create_task(vih_media.create_vih_media_app(calls).routes[0].endpoint(socket, call_id))
    try:
        await until(lambda: instances and codec.inputs)
        yield calls, call_id, socket, instances[0], codec, task
    finally:
        if not task.done():
            await socket.event(STOP)
        await asyncio.wait_for(task, 3)
        assert socket.closed and codec.closed and instances[0].closed
        calls.repository.connection.close()


@pytest.mark.asyncio
async def test_initial_silence_variable_packets_and_nonmedia_sequence_gaps(tmp_path, monkeypatch):
    async with bridge(tmp_path, monkeypatch) as (calls, call_id, socket, _model, codec, task):
        assert codec.inputs[0] == ZERO
        # Unequal packet boundaries cross a tick boundary; marks consume sequence numbers too.
        raw = bytes(range(256)) + bytes(range(64))
        await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(raw[:93]).decode()}})
        await socket.event({"event": "mark", "sequenceNumber": "4", "streamSid": "MZ-test",
                            "mark": {"name": "unknown-safe-mark"}})
        await socket.event({"event": "media", "sequenceNumber": "5", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(raw[93:]).decode()}})
        await until(lambda: len(codec.inputs) >= 5)
        await socket.event(STOP)
        await task
        from voice_harness.vih_protocol import decode_mulaw
        nonzero = b"".join(frame for frame in codec.inputs if frame != ZERO)
        assert nonzero == decode_mulaw(raw)
        stats = calls.result(call_id)["streamStats"]
        assert stats["inputRealSamplesReceived"] == stats["inputRealSamplesSent"] == 320
        assert stats["sequenceGaps"] == 95  # Includes the deliberately distant stop sequence.
        assert stats["inputSyntheticSilenceSamples"] == (len(codec.inputs) - 2) * 160


@pytest.mark.asyncio
async def test_pending_slow_clear_suppresses_tail_even_after_energy_release(tmp_path, monkeypatch):
    clear_started, release_clear = asyncio.Event(), asyncio.Event()

    class SlowClear(Socket):
        async def send_text(self, message):
            if json.loads(message)["event"] == "clear":
                clear_started.set()
                await release_clear.wait()
            await super().send_text(message)

    async with bridge(tmp_path, monkeypatch, socket_type=SlowClear,
                      media_interruption_mode="sustained_energy", media_interruption_release_ms=100) as rig:
        calls, call_id, socket, model, codec, task = rig
        await model.frames.put(b"\x01" + LOUD)
        await until(lambda: not socket.outgoing.empty())
        await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(encode_mulaw(LOUD * 20)).decode()}})
        # Keep local playback recent throughout the 400 ms candidate.
        for _ in range(22):
            await model.frames.put(b"\x01" + LOUD)
            await asyncio.sleep(.02)
        await asyncio.wait_for(clear_started.wait(), 2)
        inputs_at_clear = len(codec.inputs)
        # Six quiet ticks release the energy latch while the clear remains blocked.
        await until(lambda: len(codec.inputs) >= inputs_at_clear + 6)
        tail = struct.pack("<h", 999) * 160
        await model.frames.put(b"\x01" + tail)
        await until(lambda: tail in codec.decoded)
        release_clear.set()
        await until(lambda: any(frame["event"] == "clear" for frame in list(socket.outgoing._queue)))
        await asyncio.sleep(.05)
        sent = list(socket.outgoing._queue)
        clear_index = next(index for index, frame in enumerate(sent) if frame["event"] == "clear")
        assert not any(frame["event"] == "media" for frame in sent[clear_index + 1:])
        assert len(codec.inputs) >= inputs_at_clear + 6
        # New post-clear output may resume, without resetting either codec or model.
        await model.frames.put(b"\x01" + LOUD)
        await until(lambda: len(socket.outgoing._queue) > len(sent))
        await socket.event(STOP)
        await task
        stats = calls.result(call_id)["streamStats"]
        assert stats["interruptionClears"] == 1 and stats["outputSuppressedMs"] >= 20


@pytest.mark.asyncio
async def test_stop_rechecked_inside_scheduled_send_task(tmp_path, monkeypatch):
    original = asyncio.wait_for
    original_ingest = vih_media.VihStream.ingest
    protocols = []
    def capture_protocol(self, message):
        if not protocols:
            protocols.append(self)
        return original_ingest(self, message)
    monkeypatch.setattr(vih_media.VihStream, "ingest", capture_protocol)
    injected = False
    async with bridge(tmp_path, monkeypatch) as rig:
        calls, call_id, socket, model, _codec, task = rig

        async def racing_wait_for(awaitable, timeout):
            nonlocal injected
            name = getattr(getattr(awaitable, "cr_code", None), "co_name", "")
            if not injected and name in {"provider_send", "send_text"}:
                injected = True
                # Force provider validation between send-lock admission and the child task.
                protocols[0].ingest(json.dumps(STOP))
            return await original(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", racing_wait_for)
        await model.frames.put(b"\x01" + ZERO)
        await original(task, 3)
        assert injected and socket.outgoing.empty()
        assert calls.result(call_id)["streamReason"] == "provider_stop"


@pytest.mark.asyncio
async def test_continuous_model_silence_and_marks_are_not_agent_activity(tmp_path, monkeypatch):
    async with bridge(tmp_path, monkeypatch, media_interruption_mode="sustained_energy") as rig:
        calls, call_id, socket, model, codec, task = rig
        await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(encode_mulaw(LOUD * 25)).decode()}})
        # The listening model continuously generates encoded silence, including mark cadence.
        for _ in range(28):
            await model.frames.put(b"\x01" + ZERO)
            await asyncio.sleep(.02)
        await until(lambda: sum(frame != ZERO for frame in codec.inputs) >= 25)
        assert not any(frame["event"] == "clear" for frame in socket.outgoing._queue)
        assert any(frame["event"] == "mark" for frame in socket.outgoing._queue)
        # A real subsequent response is not muted just because the caller spoke while listening.
        await model.frames.put(b"\x01" + LOUD)
        expected = encode_mulaw(LOUD)
        await until(lambda: any(frame["event"] == "media" and
                                base64.b64decode(frame["media"]["payload"]) == expected
                                for frame in socket.outgoing._queue))
        await socket.event(STOP)
        await task
        stats = calls.result(call_id)["streamStats"]
        assert stats["interruptionCandidates"] == stats["interruptionConfirmed"] == 0
        assert stats["interruptionClears"] == stats["outputSuppressedMs"] == 0


@pytest.mark.asyncio
async def test_epoch_rechecked_inside_scheduled_send_task(tmp_path, monkeypatch):
    queues = []
    original_fifo, original_wait = vih_media.PlaybackFIFO, asyncio.wait_for
    def capture_fifo(maximum_ms):
        fifo = original_fifo(maximum_ms)
        queues.append(fifo)
        return fifo
    monkeypatch.setattr(vih_media, "PlaybackFIFO", capture_fifo)
    injected = False
    async with bridge(tmp_path, monkeypatch) as rig:
        _calls, _call_id, socket, model, _codec, _task = rig
        async def racing_wait_for(awaitable, timeout):
            nonlocal injected
            name = getattr(getattr(awaitable, "cr_code", None), "co_name", "")
            if not injected and name in {"provider_send", "send_text"}:
                injected = True
                queues[0].clear()
                await socket.send_text('{"event":"clear"}')
            return await original_wait(awaitable, timeout)
        monkeypatch.setattr(asyncio, "wait_for", racing_wait_for)
        await model.frames.put(b"\x01" + LOUD)
        await until(lambda: injected and queues[0].held is None)
        assert list(socket.outgoing._queue) == [{"event": "clear"}]
        assert queues[0].samples == 0


@pytest.mark.asyncio
async def test_synthetic_model_input_cannot_start_after_validated_stop(tmp_path, monkeypatch):
    protocols = []
    original_ingest, original_wait = vih_media.VihStream.ingest, asyncio.wait_for
    def capture_protocol(self, message):
        if not protocols:
            protocols.append(self)
        return original_ingest(self, message)
    monkeypatch.setattr(vih_media.VihStream, "ingest", capture_protocol)
    async with bridge(tmp_path, monkeypatch) as rig:
        calls, call_id, _socket, model, _codec, task = rig
        sent, injected = [], False
        async def observe_send(packet):
            sent.append(packet)
        monkeypatch.setattr(model, "send_opus", observe_send)
        async def racing_wait_for(awaitable, timeout):
            nonlocal injected
            name = getattr(getattr(awaitable, "cr_code", None), "co_name", "")
            if not injected and name in {"observe_send", "model_send"}:
                injected = True
                protocols[0].ingest(json.dumps(STOP))
            return await original_wait(awaitable, timeout)
        monkeypatch.setattr(asyncio, "wait_for", racing_wait_for)
        await original_wait(task, 3)
        assert injected and not sent
        assert calls.result(call_id)["streamReason"] == "provider_stop"


@pytest.mark.asyncio
async def test_clear_send_timeout_is_bounded_and_does_not_release_lease(tmp_path, monkeypatch):
    original_wait = asyncio.wait_for
    attempted = []
    class StuckClear(Socket):
        async def send_text(self, message):
            if json.loads(message)["event"] == "clear":
                attempted.append(True)
                await asyncio.Future()
            await super().send_text(message)
    async def short_clear_deadline(awaitable, timeout):
        frame = getattr(awaitable, "cr_frame", None)
        message = frame.f_locals.get("message", "") if frame else ""
        if isinstance(message, str) and message.startswith('{"event": "clear"'):
            timeout = .03
        return await original_wait(awaitable, timeout)
    monkeypatch.setattr(asyncio, "wait_for", short_clear_deadline)
    async with bridge(tmp_path, monkeypatch, socket_type=StuckClear,
                      media_interruption_mode="sustained_energy") as rig:
        calls, call_id, socket, model, codec, task = rig
        await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                            "media": {"payload": base64.b64encode(encode_mulaw(LOUD * 25)).decode()}})
        for _ in range(26):
            await model.frames.put(b"\x01" + LOUD)
            await asyncio.sleep(.02)
        await original_wait(task, 3)
        result = calls.result(call_id)
        assert attempted and len(codec.inputs) >= 20
        assert result["streamReason"] == "call_timeout" and result["recoveryRequired"]
        assert calls.repository.get(call_id)["gpu_lease"] == 1
        assert result["streamStats"]["interruptionConfirmed"] == 1
        assert result["streamStats"]["interruptionClears"] == 0


@pytest.mark.asyncio
async def test_final_recording_close_failure_visible_but_stop_authoritative(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="voice_harness")
    original = vih_media._Recordings.close
    def failed_close(self):
        original(self)
        raise OSError("private recording path and secret must not be logged")
    monkeypatch.setattr(vih_media._Recordings, "close", failed_close)
    async with bridge(tmp_path, monkeypatch, recording_dir=str(tmp_path / "audio")) as rig:
        calls, call_id, socket, _model, _codec, task = rig
        await socket.event(STOP)
        await task
        result = calls.result(call_id)
        assert result["streamReason"] == "provider_stop" and result["complete"]
        assert result["streamStats"]["recordingFailed"] is True
        assert "secret" not in caplog.text


def test_recording_close_attempts_every_resource_after_failure(tmp_path):
    recorder = vih_media._Recordings("", "unused-disabled-id", 100)
    closed = []
    class Resource:
        def __init__(self, name):
            self.name = name
        def close(self):
            closed.append(self.name)
            raise OSError("private error")
    recorder.timeline = Resource("timeline")
    recorder.streams = {"caller": Resource("caller"), "agent": Resource("agent")}
    with pytest.raises(OSError):
        recorder.close()
    assert closed == ["timeline", "caller", "agent"] and recorder.streams == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "flush", "timeline_write", "timeline_flush"])
async def test_recording_io_failure_is_safe_visible_and_preserves_lease(tmp_path, monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO, logger="voice_harness")
    monkeypatch.setattr(vih_media, "MEDIA_CHECKPOINT_SECONDS", .01)
    async with bridge(tmp_path, monkeypatch, recording_dir=str(tmp_path / "audio")) as rig:
        calls, call_id, socket, model, _codec, task = rig
        def broken(*args, **kwargs):
            raise OSError("private path with secret must not escape")
        if failure.startswith("timeline_"):
            monkeypatch.setattr(vih_media.Timeline, failure.removeprefix("timeline_"), broken)
        else:
            monkeypatch.setattr(vih_media._Recordings, failure, broken)
        if failure == "write":
            await socket.event({"event": "media", "sequenceNumber": "2", "streamSid": "MZ-test",
                                "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}})
        elif failure == "timeline_write":
            await model.frames.put(b"\x01" + ZERO)
        await asyncio.wait_for(task, 3)
        result = calls.result(call_id)
        assert result["recoveryRequired"] and result["streamReason"] == "bridge_error"
        assert result["streamStats"]["recordingFailed"]
        assert calls.repository.get(call_id)["gpu_lease"] == 1
        assert "secret" not in caplog.text
