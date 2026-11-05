"""Durable outbound-call WebSocket lifecycle; provider framing stays isolated."""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .audio import StreamingCodec
from .auth import bearer_value
from .call_service import CallService
from .diagnostics import stream_event_summary
from .media_diagnostics import MediaConnectionLoggingMiddleware
from .models import Brief, StartCall
from .observability import error_code, error_type, log_event
from .personaplex import PersonaPlexClient, prompt_for
from .repository import RepositoryError
from .vih_protocol import VihOutboundAudio, VihProtocolError, VihStream, mulaw_payload

MEDIA_CHECKPOINT_SECONDS = 2.0
MEDIA_LOG_SECONDS = 10.0


def model_connection_url(base: str, request: StartCall) -> str:
    sections = []
    if request.script is not None:
        sections.append("Approved call script:\n" + request.script)
    elif request.context is not None:
        brief = Brief(leadId=request.leadId, voiceName=request.voiceName,
                      **request.context.model_dump())
        sections.append(prompt_for(brief))
    sections.append(
        "Call closing: When the caller asks to end or the conversation is finished, "
        "briefly acknowledge and say: Goodbye. Please hang up now. "
        "Do not claim you disconnected the call."
    )
    split = urlsplit(base)
    prompt = "\n\n".join(sections)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query.update(voice_prompt=request.voiceName, text_prompt=prompt)
    return urlunsplit((split.scheme, split.netloc, split.path,
                      urlencode(query), ""))


class _Recordings:
    def __init__(self, directory: str, call_id: str, limit: int):
        self.streams = {}
        self.refs = {}
        self.sizes = {"caller": 0, "agent": 0}
        self.limit = limit
        if directory:
            if not re.fullmatch(r"call_[a-f0-9]{36}", call_id):
                raise ValueError("invalid recording call ID")
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                for side in ("caller", "agent"):
                    name = f"{call_id}-{side}.ulaw"
                    descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    self.streams[side] = os.fdopen(descriptor, "wb")
                    self.refs[side] = name
            except BaseException:
                self.close()
                raise

    def write(self, side: str, data: bytes) -> None:
        self.sizes[side] += len(data)
        if self.sizes[side] > self.limit:
            raise VihProtocolError("recording/audio duration limit exceeded")
        if side in self.streams:
            self.streams[side].write(data)

    def close(self) -> None:
        for stream in self.streams.values():
            with suppress(OSError):
                stream.close()
        self.streams.clear()

    def flush(self) -> None:
        for stream in self.streams.values():
            stream.flush()


def create_vih_media_app(calls: CallService) -> FastAPI:
    app = FastAPI(title="ViH Voice Harness Media", docs_url=None, redoc_url=None, openapi_url=None)
    settings = calls.settings
    app.add_middleware(MediaConnectionLoggingMiddleware, auth_mode=settings.media_auth_mode)

    @app.websocket("/calls/{call_id}")
    async def call(socket: WebSocket, call_id: str) -> None:
        token = (bearer_value(socket.headers.get("authorization"))
                 if settings.media_auth_mode == "header" else socket.query_params.get("token"))
        try:
            request = await calls.claim_media(call_id, token or "")
        except RepositoryError as error:
            log_event("media_rejected", logging.WARNING, call_id=call_id, stage="media_auth",
                      status_code=error.status, error_type="RepositoryError", error_code=error_code(error))
            await socket.close(4401 if error.status == 401 else 4409)
            return
        except Exception as error:
            log_event("media_failed", logging.ERROR, call_id=call_id, stage="media_auth",
                      error_type=error_type(error))
            raise
        log_event("media_authorized", call_id=call_id, stage="media_auth")

        protocol = VihStream(settings.max_frame_bytes)
        text: list[str] = []
        text_chars = 0
        reason = "provider_disconnect"
        client = codec = recorder = outbound = None
        tasks: list[asyncio.Task] = []
        started = time.monotonic()
        sent_frames = sent_bytes = 0
        send_lock = asyncio.Lock()
        # Each concurrent direction retains its own failure stage.
        failed_stage = "provider_handshake"

        def progress(event: str, level: int = logging.INFO) -> None:
            log_event(event, level, call_id=call_id,
                      inbound_media_frames=protocol.media_frames, inbound_audio_bytes=protocol.audio_bytes,
                      outbound_media_frames=sent_frames, outbound_audio_bytes=sent_bytes,
                      sequence_gaps=protocol.sequence_gaps, agent_text_chars=text_chars,
                      duration_ms=round((time.monotonic() - started) * 1000, 2))

        async def receive() -> str:
            message = await asyncio.wait_for(socket.receive(), settings.idle_timeout_seconds)
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("text") is None:
                raise VihProtocolError("ViH requires text JSON events")
            return message["text"]

        async def send(message: str) -> None:
            async with send_lock:
                if protocol.stopped:
                    return
                await asyncio.wait_for(socket.send_text(message), settings.idle_timeout_seconds)

        def recording_refs() -> dict:
            return ({"encoding": "audio/x-mulaw", "sampleRate": 8000,
                     "channels": 1, **recorder.refs} if recorder and recorder.refs else {})

        def stream_stats() -> dict:
            return {**protocol.stats(), "outboundMediaFrames": sent_frames,
                    "outboundAudioBytes": sent_bytes,
                    "bridgeDurationSeconds": round(time.monotonic() - started, 3)}

        async def cleanup() -> None:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if outbound and not protocol.stopped:
                # A bridge failure must not leave old generated audio queued at the provider.
                with suppress(Exception):
                    await asyncio.wait_for(send(outbound.clear()), settings.close_timeout_seconds)
            if client:
                with suppress(Exception):
                    await asyncio.wait_for(client.close(), settings.close_timeout_seconds)
            with suppress(Exception):
                await asyncio.wait_for(socket.close(), settings.close_timeout_seconds)
            if codec is not None:
                with suppress(Exception):
                    codec.close()
            if recorder:
                recorder.close()
            # The provider's validated terminal event remains authoritative even
            # if a model task fails or shutdown races with the same stop.
            final_reason = "provider_stop" if protocol.stopped else reason
            try:
                await calls.finish_media(call_id, final_reason, "".join(text), stream_stats(), recording_refs())
            except Exception as error:
                log_event("media_failed", logging.ERROR, call_id=call_id, stage="database",
                          error_type=error_type(error))
                raise
            final = calls.repository.get(call_id)
            log_event("media_finished", logging.INFO if protocol.stopped else logging.WARNING,
                      call_id=call_id, reason=final_reason,
                      state=final["state"], recovery_required=bool(final["recovery_required"]))
            progress("media_progress")
            text.clear()

        try:
            await socket.accept()
            async with asyncio.timeout(settings.max_call_seconds):
                # Handshake deadline is independent of repeated connected/idle messages.
                async with asyncio.timeout(settings.ready_timeout_seconds):
                    while not protocol.started:
                        name, _ = protocol.ingest(await receive())
                        log_event("provider_event_received", call_id=call_id, stage="provider_handshake",
                                  provider_event=stream_event_summary(protocol.last_event))
                        if name == "start":
                            await calls.bind_stream(call_id, protocol.last_event)
                log_event("stream_started", call_id=call_id, stage="provider_handshake")
                failed_stage = "codec"
                codec = StreamingCodec()
                failed_stage = "recording"
                recorder = _Recordings(settings.recording_dir, call_id, settings.max_call_seconds * 8000)
                failed_stage = "checkpoint"
                await calls.checkpoint_media(call_id, "", protocol.stats(), recording_refs())
                outbound = VihOutboundAudio(protocol.stream_sid)
                client = PersonaPlexClient(model_connection_url(settings.personaplex_url, request),
                                           settings.ready_timeout_seconds)
                failed_stage = "model_connect"
                log_event("model_connecting", call_id=call_id, stage="model_connect")
                await client.connect()
                log_event("model_ready", call_id=call_id, stage="model_connect")
                failed_stage = "media_session"

                async def track(awaitable, stage: str):
                    nonlocal failed_stage
                    try:
                        return await awaitable
                    except Exception:
                        failed_stage = stage
                        raise

                async def upstream() -> None:
                    nonlocal reason, failed_stage
                    while True:
                        message = await track(receive(), "provider_receive")
                        try:
                            name, pcm = protocol.ingest(message)
                        except Exception:
                            failed_stage = "provider_receive"
                            raise
                        if name in {"dtmf", "mark"}:
                            log_event("provider_event_received", logging.DEBUG, call_id=call_id,
                                      stage="provider_receive", provider_event=stream_event_summary(protocol.last_event))
                        if name == "stop":
                            log_event("provider_event_received", call_id=call_id, stage="provider_receive",
                                      provider_event=stream_event_summary(protocol.last_event))
                            reason = "provider_stop"
                            return
                        if pcm is not None:
                            if protocol.media_frames == 1:
                                log_event("media_input_started", call_id=call_id, stage="provider_receive")
                            try:
                                recorder.write("caller", mulaw_payload(protocol.last_event["media"]["payload"]))
                            except Exception:
                                failed_stage = "recording"
                                raise
                            try:
                                packets = codec.phone_to_opus(pcm)
                            except Exception:
                                failed_stage = "audio_decode"
                                raise
                            for packet in packets:
                                await track(asyncio.wait_for(client.send_opus(packet), settings.idle_timeout_seconds), "model_send")

                async def downstream() -> None:
                    nonlocal text_chars, sent_frames, sent_bytes, failed_stage
                    while True:
                        frame = await track(asyncio.wait_for(client.recv(), settings.idle_timeout_seconds), "model_receive")
                        if not isinstance(frame, bytes) or len(frame) < 2:
                            failed_stage = "model_receive"
                            raise VihProtocolError("malformed model frame")
                        if frame[0] == 2:
                            try:
                                piece = frame[1:].decode("utf-8")
                                if text_chars + len(piece) > settings.max_transcript_chars:
                                    raise VihProtocolError("agent transcript limit exceeded")
                            except Exception:
                                failed_stage = "model_receive"
                                raise
                            text.append(piece)
                            if text_chars == 0 and piece:
                                log_event("model_text_started", call_id=call_id, stage="model_receive")
                            text_chars += len(piece)
                        elif frame[0] == 1:
                            try:
                                pcm = codec.opus_to_phone(frame[1:])
                                if len(pcm) > 80000:
                                    raise VihProtocolError("model audio frame too large")
                                messages = outbound.encode(pcm)
                            except Exception:
                                failed_stage = "audio_decode"
                                raise
                            for message in messages:
                                if protocol.stopped:
                                    return
                                await track(send(message), "provider_send")
                                # Retain the audio sent toward telephony, not an unplayed model buffer.
                                data = mulaw_payload(json.loads(message)["media"]["payload"])
                                try:
                                    recorder.write("agent", data)
                                except Exception:
                                    failed_stage = "recording"
                                    raise
                                sent_frames += 1
                                sent_bytes += len(data)
                                if sent_frames == 1:
                                    log_event("media_output_started", call_id=call_id, stage="provider_send")
                            if messages and not protocol.stopped:
                                await track(send(outbound.mark(f"audio-{outbound.chunk}")), "provider_send")
                        else:
                            failed_stage = "model_receive"
                            raise VihProtocolError("unexpected model frame")

                async def checkpoint() -> None:
                    nonlocal failed_stage
                    last_log = time.monotonic()
                    while True:
                        await asyncio.sleep(MEDIA_CHECKPOINT_SECONDS)
                        try:
                            recorder.flush()
                            await calls.checkpoint_media(call_id, "".join(text), stream_stats(), recording_refs())
                        except Exception:
                            failed_stage = "checkpoint"
                            raise
                        progress("media_checkpoint", logging.DEBUG)
                        if time.monotonic() - last_log >= MEDIA_LOG_SECONDS:
                            progress("media_progress")
                            last_log = time.monotonic()

                tasks.extend((asyncio.create_task(upstream()), asyncio.create_task(downstream()),
                              asyncio.create_task(checkpoint())))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            log_event("media_failed", logging.WARNING, call_id=call_id, stage="provider_receive",
                      reason="provider_disconnect", error_type="WebSocketDisconnect")
        except TimeoutError:
            reason = "call_timeout"
            log_event("media_failed", logging.ERROR, call_id=call_id, stage=failed_stage,
                      reason="timeout", error_type="TimeoutError")
        except asyncio.CancelledError:
            reason = "service_shutdown"
            log_event("media_failed", logging.WARNING, call_id=call_id, stage="shutdown", reason=reason)
            raise
        except Exception as error:  # noqa: BLE001 - upstream errors can include prompt/token URLs
            reason = "bridge_error"
            log_event("media_failed", logging.ERROR, call_id=call_id, stage=failed_stage,
                      reason=reason, error_type=error_type(error), error_code=error_code(error))
        finally:
            finishing = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(finishing)
            except asyncio.CancelledError:
                await finishing
                raise

    @app.websocket("/stream")
    async def static_stream(socket: WebSocket) -> None:
        # The PDF doesn't define a trusted static-stream->our-call correlation contract.
        log_event("media_rejected", logging.WARNING, stage="media_auth", status_code=403,
                  error_code="static_endpoint_disabled")
        await socket.close(4403)

    return app
