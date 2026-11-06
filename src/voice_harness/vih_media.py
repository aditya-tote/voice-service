"""Durable outbound-call WebSocket lifecycle; provider framing stays isolated."""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .audio import StreamingCodec
from .auth import bearer_value
from .call_service import CallService
from .diagnostics import stream_event_summary
from .media_diagnostics import MediaConnectionLoggingMiddleware
from .media_timeline import Timeline
from .media_timing import (
    MARK_FRAMES,
    OUTPUT_ACTIVITY_RMS,
    Deadline,
    EnergyInterruption,
    MarkLedger,
    PlaybackFIFO,
    SampleFIFO,
    pcm_rms,
)
from .observability import error_code, error_type, log_event
from .personaplex import PersonaPlexClient, model_connection_url
from .repository import RepositoryError
from .vih_protocol import VihOutboundAudio, VihProtocolError, VihStream, decode_mulaw, mulaw_payload

MEDIA_CHECKPOINT_SECONDS = 2.0
MEDIA_LOG_SECONDS = 10.0


class _Recordings:
    def __init__(self, directory: str, call_id: str, limit: int):
        self.streams = {}
        self.refs = {}
        self.sizes = {"caller": 0, "agent": 0}
        self.limit = limit
        self.timeline = None
        if directory:
            if not re.fullmatch(r"call_[a-f0-9]{36}", call_id):
                raise ValueError("invalid recording call ID")
            folder = Path(directory)
            if any(part.is_symlink() for part in (folder, *folder.parents)):
                raise ValueError("unsafe recording directory")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix" and folder.stat().st_mode & 0o077:
                raise ValueError("recording directory must be private")
            try:
                for side in ("caller", "agent"):
                    name = f"{call_id}-{side}.ulaw"
                    descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    self.streams[side] = os.fdopen(descriptor, "wb")
                    self.refs[side] = name
                name = f"{call_id}-timeline.jsonl"
                self.timeline = Timeline(folder / name)
                self.refs["timeline"] = name
            except BaseException:
                with suppress(Exception):
                    self.close()
                raise

    def write(self, side: str, data: bytes) -> None:
        self.sizes[side] += len(data)
        if self.sizes[side] > self.limit:
            raise VihProtocolError("recording/audio duration limit exceeded")
        if side in self.streams:
            self.streams[side].write(data)

    def close(self) -> None:
        failure = None
        for stream in ([self.timeline] if self.timeline else []) + list(self.streams.values()):
            try:
                stream.close()
            except OSError as error:
                failure = failure or error
        self.streams.clear()
        if failure:
            raise failure

    def flush(self) -> None:
        if self.timeline:
            self.timeline.flush()
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
        input_fifo = SampleFIFO(settings.media_input_max_buffer_ms)
        playback = PlaybackFIFO(settings.media_output_max_buffer_ms)
        marks = MarkLedger()
        energy = EnergyInterruption(settings.media_interruption_mode, settings.media_interruption_rms,
                                    settings.media_interruption_sustain_ms, settings.media_interruption_release_ms)
        input_clock = output_clock = None
        stream_start = started
        first_input_ms = first_output_ms = None
        last_non_silent_sent = -float("inf")
        clear_count = 0
        clear_latency_ms = 0.0
        output_ready = asyncio.Event()
        clear_requested = asyncio.Event()
        clear_confirmed_at = 0.0
        clear_pending = False
        recording_failed = False
        send_lock = asyncio.Lock()
        # Each concurrent direction retains its own failure stage.
        failed_stage = "provider_handshake"

        def progress(event: str, level: int = logging.INFO) -> None:
            log_event(event, level, call_id=call_id,
                      inbound_media_frames=protocol.media_frames, inbound_audio_bytes=protocol.audio_bytes,
                      outbound_media_frames=sent_frames, outbound_audio_bytes=sent_bytes,
                       sequence_gaps=protocol.sequence_gaps, agent_text_chars=text_chars,
                       input_queue_high_water_ms=input_fifo.high_water / 8,
                       input_queue_ms=input_fifo.samples / 8,
                       output_queue_high_water_ms=playback.high_water / 8,
                       input_late_ticks=input_clock.late if input_clock else 0,
                       input_max_lateness_ms=input_clock.max_lateness_ms if input_clock else 0,
                       input_missed_ticks=input_clock.missed if input_clock else 0,
                      output_missed_ticks=output_clock.missed if output_clock else 0,
                      media_buffer_overflows=input_fifo.overflows + playback.overflows,
                      interruption_clears=clear_count,
                      duration_ms=round((time.monotonic() - started) * 1000, 2))

        async def receive() -> str:
            message = await asyncio.wait_for(socket.receive(), settings.idle_timeout_seconds)
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("text") is None:
                raise VihProtocolError("ViH requires text JSON events")
            return message["text"]

        async def send(message: str, epoch: int | None = None, frame=None) -> bool:
            async with send_lock:
                async def provider_send():
                    # wait_for schedules a task: recheck inside it, after that scheduling boundary.
                    if protocol.stopped or (epoch is not None and epoch != playback.epoch):
                        return False
                    if frame is not None:
                        playback.begin_send(frame)
                    await socket.send_text(message)
                    return True
                return await asyncio.wait_for(provider_send(), settings.idle_timeout_seconds)

        async def send_mark(epoch: int, samples: int) -> None:
            async with send_lock:
                async def provider_send():
                    if protocol.stopped or epoch != playback.epoch:
                        return
                    name = marks.add(time.monotonic(), samples, epoch)
                    await socket.send_text(outbound.mark(name))
                await asyncio.wait_for(provider_send(), settings.idle_timeout_seconds)

        async def model_send(packet: bytes) -> None:
            # Like provider sends, wait_for's child task may run after stop validation.
            if not protocol.stopped:
                await client.send_opus(packet)

        def timeline(event, samples=0, byte_offset=0):
            nonlocal recording_failed
            if recorder and recorder.timeline:
                try:
                    recorder.timeline.write(event, max(0, time.monotonic() - stream_start) * 1000,
                                            samples, byte_offset, playback.epoch)
                except OSError:
                    recording_failed = True
                    raise

        def record_audio(side, data):
            nonlocal recording_failed
            try:
                recorder.write(side, data)
            except OSError:
                recording_failed = True
                raise

        def recording_refs() -> dict:
            return ({"encoding": "audio/x-mulaw", "sampleRate": 8000,
                     "channels": 1, **recorder.refs} if recorder and recorder.refs else {})

        def stream_stats() -> dict:
            return {**protocol.stats(), "outboundMediaFrames": sent_frames,
                    "outboundAudioBytes": sent_bytes,
                    **input_fifo.stats(), **playback.stats(time.monotonic(), len(outbound.pending) if outbound else 0),
                    **marks.stats(), "sentAudioMs": sent_bytes / 8,
                    "inputSchedulerLateTicks": input_clock.late if input_clock else 0,
                    "inputSchedulerMissedTicks": input_clock.missed if input_clock else 0,
                    "inputMaxLatenessMs": input_clock.max_lateness_ms if input_clock else 0,
                    "outputSchedulerLateTicks": output_clock.late if output_clock else 0,
                    "outputSchedulerMissedTicks": output_clock.missed if output_clock else 0,
                    "outputMaxLatenessMs": output_clock.max_lateness_ms if output_clock else 0,
                    "interruptionMode": energy.mode, "interruptionCandidates": energy.candidates,
                    "interruptionConfirmed": energy.confirmed, "interruptionClears": clear_count,
                    "clearMaxSendLatencyMs": clear_latency_ms,
                    "firstInputOffsetMs": first_input_ms, "firstOutputOffsetMs": first_output_ms,
                    "timelineTruncated": bool(recorder and recorder.timeline and recorder.timeline.truncated),
                    "recordingFailed": recording_failed,
                    "bridgeDurationSeconds": round(time.monotonic() - started, 3)}

        async def cleanup() -> None:
            nonlocal recording_failed, reason
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            input_fifo.clear()
            try:
                timeline("output_discarded", playback.samples + (len(outbound.pending) if outbound else 0))
            except OSError:
                recording_failed = True
            playback.clear(len(outbound.pending) if outbound else 0, termination=True)
            if outbound:
                outbound.pending = b""
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
                try:
                    recorder.close()
                except OSError as error:
                    recording_failed = True
                    log_event("media_failed", logging.ERROR, call_id=call_id, stage="recording",
                              error_type=error_type(error))
            if recording_failed and not protocol.stopped:
                reason = "bridge_error"
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
            if settings.vih_dial_mode == "live":
                client = calls.take_prepared_model(call_id)
            await socket.accept()
            async with asyncio.timeout(settings.max_call_seconds):
                # Handshake deadline is independent of repeated connected/idle messages.
                async with asyncio.timeout(settings.ready_timeout_seconds):
                    while not protocol.started:
                        name, _ = protocol.ingest(await receive())
                        log_event("provider_event_received", call_id=call_id, stage="provider_handshake",
                                  provider_event=stream_event_summary(protocol.last_event))
                        if name == "start":
                            stream_start = time.monotonic()
                            await calls.bind_stream(call_id, protocol.last_event)
                log_event("stream_started", call_id=call_id, stage="provider_handshake")
                failed_stage = "codec"
                codec = StreamingCodec()
                failed_stage = "recording"
                recorder = _Recordings(settings.recording_dir, call_id, settings.max_call_seconds * 8000)
                failed_stage = "checkpoint"
                await calls.checkpoint_media(call_id, "", protocol.stats(), recording_refs())
                outbound = VihOutboundAudio(protocol.stream_sid)
                failed_stage = "model_connect"
                if settings.vih_dial_mode == "live":
                    log_event("model_session_reused", call_id=call_id, stage="model_connect")
                else:
                    client = PersonaPlexClient(model_connection_url(settings.personaplex_url, request.leadContext,
                                                                   settings.default_voice),
                                               settings.ready_timeout_seconds)
                    log_event("model_connecting", call_id=call_id, stage="model_connect")
                    await client.connect()
                    log_event("model_ready", call_id=call_id, stage="model_connect")
                failed_stage = "media_session"
                input_clock = Deadline(stream_start + settings.media_input_startup_ms / 1000)
                output_clock = Deadline(time.monotonic())

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
                        if name == "mark":
                            result = marks.ack(protocol.last_event["mark"]["name"], time.monotonic(), playback.epoch)
                            if result == "matched":
                                timeline("mark_matched")
                        if name == "stop":
                            log_event("provider_event_received", call_id=call_id, stage="provider_receive",
                                      provider_event=stream_event_summary(protocol.last_event))
                            reason = "provider_stop"
                            return
                        if pcm is not None:
                            if protocol.media_frames == 1:
                                log_event("media_input_started", call_id=call_id, stage="provider_receive")
                            try:
                                offset = recorder.sizes["caller"]
                                record_audio("caller", mulaw_payload(protocol.last_event["media"]["payload"]))
                                timeline("caller_arrival", len(pcm) // 2, offset)
                            except Exception:
                                failed_stage = "recording"
                                raise
                            input_fifo.append(pcm)

                async def input_writer() -> None:
                    nonlocal first_input_ms, clear_confirmed_at, clear_pending
                    while not protocol.stopped:
                        await asyncio.sleep(input_clock.delay(time.monotonic()))
                        if protocol.stopped:
                            return
                        now = time.monotonic()
                        input_clock.advance(now)
                        pcm, real = input_fifo.tick()
                        if first_input_ms is None:
                            first_input_ms = (now - stream_start) * 1000
                        timeline("model_input", real)
                        if real < 160:
                            timeline("synthetic_silence", 160 - real)
                        # PersonaPlex emits encoded silence while listening too. Queue bytes/marks
                        # alone are not agent activity. This is acoustic energy, not a speech label.
                        residual_active = (energy.mode != "off" and
                                           pcm_rms(decode_mulaw(outbound.pending)) >= OUTPUT_ACTIVITY_RMS)
                        active = not clear_pending and (playback.non_silent_pending or residual_active
                                                        or now - last_non_silent_sent < 0.4)
                        if energy.observe(pcm, real, now, active):
                            # Invalidate before awaiting the lock: even a dequeued writer cannot send old epochs.
                            timeline("output_discarded", playback.invalidatable_samples + len(outbound.pending))
                            playback.clear(len(outbound.pending))
                            marks.clear()
                            outbound.pending = b""
                            clear_confirmed_at = now
                            clear_pending = True
                            clear_requested.set()
                            output_ready.set()
                        for packet in codec.phone_to_opus(pcm):
                            if protocol.stopped:
                                return
                            await track(asyncio.wait_for(model_send(packet), settings.idle_timeout_seconds), "model_send")

                async def clear_writer() -> None:
                    nonlocal clear_count, clear_latency_ms, clear_pending
                    while not protocol.stopped:
                        await clear_requested.wait()
                        clear_requested.clear()
                        # Separate control task: a bounded slow clear never pauses caller->model input.
                        if await track(send(outbound.clear()), "provider_send"):
                            clear_count += 1
                            clear_latency_ms = max(clear_latency_ms, (time.monotonic() - clear_confirmed_at) * 1000)
                            timeline("clear")
                        clear_pending = False

                async def playback_writer() -> None:
                    nonlocal sent_frames, sent_bytes, first_output_ms, last_non_silent_sent
                    epoch = playback.epoch
                    epoch_sent = 0
                    while not protocol.stopped:
                        if not playback.frames:
                            output_ready.clear()
                            await output_ready.wait()
                            output_clock.next = max(output_clock.next, time.monotonic())
                            continue
                        if epoch != playback.epoch:
                            epoch = playback.epoch
                            epoch_sent = 0
                            output_clock.next = time.monotonic()
                        frame = playback.pop(time.monotonic())
                        await asyncio.sleep(output_clock.delay(time.monotonic()))
                        if not await track(send(frame.message, frame.epoch, frame), "provider_send"):
                            playback.complete(frame)
                            continue
                        now = time.monotonic()
                        output_clock.advance(now)
                        playback.complete(frame, now)
                        data = mulaw_payload(json.loads(frame.message)["media"]["payload"])
                        offset = recorder.sizes["agent"]
                        record_audio("agent", data)
                        timeline("provider_send", len(data), offset)
                        sent_frames += 1
                        sent_bytes += len(data)
                        if frame.epoch == playback.epoch:
                            epoch_sent += len(data)
                            marks.sent_samples = epoch_sent
                        if frame.non_silent:
                            last_non_silent_sent = now
                        if first_output_ms is None:
                            first_output_ms = (now - stream_start) * 1000
                            log_event("media_output_started", call_id=call_id, stage="provider_send")
                        if epoch_sent and epoch_sent % (160 * MARK_FRAMES) == 0 and frame.epoch == playback.epoch and not protocol.stopped:
                            await track(send_mark(frame.epoch, epoch_sent), "provider_send")

                async def downstream() -> None:
                    nonlocal text_chars, failed_stage
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
                                timeline("model_audio", len(pcm) // 2)
                                if energy.suppressed or clear_pending:
                                    playback.generated += len(pcm) // 2
                                    playback.suppressed += len(pcm) // 2
                                    timeline("output_suppressed", len(pcm) // 2)
                                    continue
                                playback.check(len(pcm) // 2, len(outbound.pending))
                                messages = outbound.encode(pcm)
                            except Exception:
                                failed_stage = "audio_decode"
                                raise
                            activity = [pcm_rms(decode_mulaw(mulaw_payload(json.loads(message)["media"]["payload"])))
                                        >= OUTPUT_ACTIVITY_RMS for message in messages] if energy.mode != "off" else None
                            playback.append(messages, time.monotonic(), non_silent=activity)
                            if messages:
                                output_ready.set()
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
                              asyncio.create_task(track(input_writer(), "model_send")),
                              asyncio.create_task(track(playback_writer(), "provider_send")),
                              asyncio.create_task(track(clear_writer(), "provider_send")),
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
            if isinstance(error, OSError) and failed_stage in {"recording", "checkpoint"}:
                recording_failed = True
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
