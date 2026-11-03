"""Durable outbound-call WebSocket lifecycle; provider framing stays isolated."""

import asyncio
import json
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
from .models import Brief, StartCall
from .personaplex import PersonaPlexClient, prompt_for
from .repository import RepositoryError
from .vih_protocol import VihOutboundAudio, VihProtocolError, VihStream, mulaw_payload

MEDIA_CHECKPOINT_SECONDS = 2.0


def model_connection_url(base: str, request: StartCall) -> str:
    brief = Brief(leadId=request.leadId, voiceName=request.voiceName,
                  **request.context.model_dump())
    split = urlsplit(base)
    # The approved script accompanies the structured facts and behavioral limits.
    prompt = prompt_for(brief) + "\n\nApproved call script:\n" + request.script
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

    @app.websocket("/calls/{call_id}")
    async def call(socket: WebSocket, call_id: str) -> None:
        token = (bearer_value(socket.headers.get("authorization"))
                 if settings.media_auth_mode == "header" else socket.query_params.get("token"))
        try:
            request = await calls.claim_media(call_id, token or "")
        except RepositoryError as error:
            await socket.close(4401 if error.status == 401 else 4409)
            return

        protocol = VihStream(settings.max_frame_bytes)
        text: list[str] = []
        text_chars = 0
        reason = "provider_disconnect"
        client = codec = recorder = outbound = None
        tasks: list[asyncio.Task] = []
        started = time.monotonic()
        sent_frames = sent_bytes = 0
        send_lock = asyncio.Lock()

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
            await calls.finish_media(call_id, reason, "".join(text), stream_stats(), recording_refs())
            text.clear()

        try:
            await socket.accept()
            async with asyncio.timeout(settings.max_call_seconds):
                # Handshake deadline is independent of repeated connected/idle messages.
                async with asyncio.timeout(settings.ready_timeout_seconds):
                    while not protocol.started:
                        name, _ = protocol.ingest(await receive())
                        if name == "start":
                            await calls.bind_stream(call_id, protocol.last_event)
                codec = StreamingCodec()
                recorder = _Recordings(settings.recording_dir, call_id, settings.max_call_seconds * 8000)
                await calls.checkpoint_media(call_id, "", protocol.stats(), recording_refs())
                outbound = VihOutboundAudio(protocol.stream_sid)
                client = PersonaPlexClient(model_connection_url(settings.personaplex_url, request),
                                           settings.ready_timeout_seconds)
                await client.connect()

                async def upstream() -> None:
                    nonlocal reason
                    while True:
                        name, pcm = protocol.ingest(await receive())
                        if name == "stop":
                            reason = "provider_stop"
                            return
                        if pcm is not None:
                            recorder.write("caller", mulaw_payload(protocol.last_event["media"]["payload"]))
                            for packet in codec.phone_to_opus(pcm):
                                await asyncio.wait_for(client.send_opus(packet), settings.idle_timeout_seconds)

                async def downstream() -> None:
                    nonlocal text_chars, sent_frames, sent_bytes
                    while True:
                        frame = await asyncio.wait_for(client.recv(), settings.idle_timeout_seconds)
                        if not isinstance(frame, bytes) or len(frame) < 2:
                            raise VihProtocolError("malformed model frame")
                        if frame[0] == 2:
                            piece = frame[1:].decode("utf-8")
                            if text_chars + len(piece) > settings.max_transcript_chars:
                                raise VihProtocolError("agent transcript limit exceeded")
                            text.append(piece)
                            text_chars += len(piece)
                        elif frame[0] == 1:
                            pcm = codec.opus_to_phone(frame[1:])
                            if len(pcm) > 80000:
                                raise VihProtocolError("model audio frame too large")
                            messages = outbound.encode(pcm)
                            for message in messages:
                                if protocol.stopped:
                                    return
                                await send(message)
                                # Retain the audio sent toward telephony, not an unplayed model buffer.
                                data = mulaw_payload(json.loads(message)["media"]["payload"])
                                recorder.write("agent", data)
                                sent_frames += 1
                                sent_bytes += len(data)
                            if messages and not protocol.stopped:
                                await send(outbound.mark(f"audio-{outbound.chunk}"))
                        else:
                            raise VihProtocolError("unexpected model frame")

                async def checkpoint() -> None:
                    while True:
                        await asyncio.sleep(MEDIA_CHECKPOINT_SECONDS)
                        recorder.flush()
                        await calls.checkpoint_media(call_id, "".join(text), stream_stats(), recording_refs())

                tasks.extend((asyncio.create_task(upstream()), asyncio.create_task(downstream()),
                              asyncio.create_task(checkpoint())))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            reason = "call_timeout"
        except asyncio.CancelledError:
            reason = "service_shutdown"
            raise
        except Exception:  # noqa: BLE001 - upstream errors can include prompt/token URLs
            reason = "bridge_error"
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
        await socket.close(4403)

    return app
