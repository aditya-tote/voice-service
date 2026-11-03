"""The model probe must drive streaming input while awaiting streaming output."""

import asyncio
from types import SimpleNamespace

import pytest

from voice_harness import probe


def config():
    return SimpleNamespace(
        allowed_voices={"NATF2.pt"}, personaplex_url="ws://127.0.0.1:8998/api/chat",
        ready_timeout_seconds=1, close_timeout_seconds=0.1,
    )


def boundaries(monkeypatch, mode="normal"):
    instances = SimpleNamespace(model=None, codec=None, sending=asyncio.Event())

    class Codec:
        def __init__(self):
            if mode == "codec_error":
                raise RuntimeError("private prompt URL")
            self.frames = 0
            self.closed = False
            instances.codec = self

        def phone_to_opus(self, pcm):
            assert pcm == b"\0\0" * 160
            self.frames += 1
            # Simulate resampler/Opus buffering; a 60 ms burst isn't sufficient.
            return [b"input-opus"] if self.frames % 4 == 0 or mode == "audio_only" else []

        def opus_to_phone(self, packet):
            assert packet == b"output-opus"
            return b"\0\0" * 160

        def close(self):
            self.closed = True

    class Client:
        def __init__(self, url, timeout):
            self.frames = asyncio.Queue()
            self.sent = 0
            self.closed = False
            instances.model = self

        async def connect(self):
            pass

        async def send_opus(self, packet):
            assert packet == b"input-opus"
            instances.sending.set()
            if mode == "send_error":
                raise RuntimeError("private prompt URL")
            self.sent += 1
            if self.sent >= 6 or mode == "audio_only":
                await self.frames.put(b"\x01output-opus")
                if mode != "audio_only":
                    await self.frames.put(b"\x02Hello")

        async def recv(self):
            return await self.frames.get()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(probe, "StreamingCodec", Codec)
    monkeypatch.setattr(probe, "PersonaPlexClient", Client)
    return instances


@pytest.mark.asyncio
async def test_probe_continues_sending_past_initial_burst_and_decodes_response(monkeypatch, capsys):
    instances = boundaries(monkeypatch)
    result = await probe.run_probe(config(), "NATF2.pt", timeout_seconds=1, frame_seconds=0.001)
    assert instances.codec.frames >= 24 and instances.model.sent >= 6
    assert result.decoded_pcm_bytes >= 320 and result.text_frames >= 1
    assert instances.model.closed and instances.codec.closed
    assert "PersonaPlex Opus and text frames received" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_audio_without_text_has_overall_deadline_and_reports_progress(monkeypatch):
    instances = boundaries(monkeypatch, "audio_only")
    with pytest.raises(probe.ProbeError, match=r"Timed out.*textFrames=0"):
        await probe.run_probe(config(), "NATF2.pt", timeout_seconds=0.3, frame_seconds=0.001)
    assert instances.model.sent >= 1 and instances.codec.closed and instances.model.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send_error", "codec_error"])
async def test_probe_failure_cleans_resources_without_leaking_prompt(monkeypatch, mode):
    instances = boundaries(monkeypatch, mode)
    with pytest.raises(probe.ProbeError) as failure:
        await probe.run_probe(config(), "NATF2.pt", timeout_seconds=1, frame_seconds=0.001)
    assert "private prompt URL" not in str(failure.value)
    assert instances.model.closed
    assert instances.codec is None or instances.codec.closed


@pytest.mark.asyncio
async def test_probe_cancellation_closes_stream_and_codec(monkeypatch):
    instances = boundaries(monkeypatch, "audio_only")
    task = asyncio.create_task(probe.run_probe(config(), "NATF2.pt", timeout_seconds=5,
                                               frame_seconds=0.001))
    await instances.sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instances.model.closed and instances.codec.closed


@pytest.mark.asyncio
async def test_probe_rejects_unapproved_voice_before_connecting(monkeypatch):
    instances = boundaries(monkeypatch)
    with pytest.raises(probe.ProbeError, match="ALLOWED_VOICES"):
        await probe.run_probe(config(), "missing.pt")
    assert instances.model is None
