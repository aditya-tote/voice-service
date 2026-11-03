"""Authenticated media entry point and bounded full-duplex call supervision."""

import asyncio
import logging
import time
from contextlib import suppress

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .audio import AudioError, RawPcm8kAdapter, StreamingCodec
from .auth import bearer_value
from .callback import send_callback
from .personaplex import PersonaPlexClient, PersonaPlexError, connection_url
from .session_store import SessionStore, StoreError

logger = logging.getLogger(__name__)


def create_media_app(store: SessionStore) -> FastAPI:
    app = FastAPI(title="Voice Harness Media", docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket("/calls/{session_id}")
    async def call(socket: WebSocket, session_id: str) -> None:
        token = (
            bearer_value(socket.headers.get("authorization"))
            if store.settings.media_auth_mode == "header"
            else socket.query_params.get("token")
        )
        try:
            session = await store.claim(session_id, token)
        except StoreError as error:
            await socket.close(4401 if error.status == 401 else 4409)
            return
        started = time.monotonic()
        text: list[str] = []
        text_chars = 0
        reason = "provider_disconnect"
        client = None
        codec = None
        tasks: list[asyncio.Task] = []
        lead_id = session.brief.leadId if session.brief else "unknown"
        idle_timeout = getattr(store.settings, "idle_timeout_seconds", 60.0)
        close_timeout = getattr(store.settings, "close_timeout_seconds", 5.0)

        async def cleanup() -> None:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if client:
                    with suppress(Exception):
                        await asyncio.wait_for(client.close(), close_timeout)
                with suppress(Exception):
                    await asyncio.wait_for(socket.close(), close_timeout)
                if codec is not None and hasattr(codec, "close"):
                    with suppress(Exception):
                        codec.close()
            finally:
                await store.end(session_id, reason)
            payload = {
                "sessionId": session_id,
                "leadId": lead_id,
                "durationSeconds": int(time.monotonic() - started),
                "hangupReason": reason,
                "agentText": "".join(text),
                "customerTranscript": None,
            }
            try:
                delivered = await asyncio.wait_for(
                    send_callback(
                        store.settings.controller_callback_base_url,
                        store.settings.controller_api_key,
                        session_id,
                        payload,
                    ),
                    20.0,
                )
            except Exception:  # noqa: BLE001 - delivery failure must not retain call resources
                delivered = False
            if not delivered:
                logger.warning("Transcript callback failed for session %s", session_id)
            text.clear()

        try:
            await socket.accept()
            if session.brief is None:
                raise PersonaPlexError("claimed session brief is unavailable")
            adapter = RawPcm8kAdapter(store.settings.max_frame_bytes)
            codec = StreamingCodec()
            client = PersonaPlexClient(
                connection_url(store.settings.personaplex_url, session.brief),
                store.settings.ready_timeout_seconds,
            )
            await client.connect()

            async def upstream() -> None:
                while True:
                    message = await asyncio.wait_for(socket.receive(), idle_timeout)
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(message.get("code", 1000))
                    frame = message.get("bytes")
                    if frame is None:
                        raise AudioError("binary PCM frames required")
                    for packet in codec.phone_to_opus(adapter.validate_inbound(frame)):
                        await asyncio.wait_for(client.send_opus(packet), idle_timeout)

            async def downstream() -> None:
                nonlocal text_chars
                while True:
                    frame = await asyncio.wait_for(client.recv(), idle_timeout)
                    if not isinstance(frame, bytes) or len(frame) < 2:
                        raise PersonaPlexError("malformed model frame")
                    if frame[0] == 2:
                        piece = frame[1:].decode("utf-8")
                        if text_chars + len(piece) > store.settings.max_transcript_chars:
                            raise PersonaPlexError("model transcript limit exceeded")
                        text.append(piece)
                        text_chars += len(piece)
                    elif frame[0] == 1:
                        pcm = codec.opus_to_phone(frame[1:])
                        frame_size = store.settings.max_frame_bytes // 2 * 2
                        for offset in range(0, len(pcm), frame_size):
                            await asyncio.wait_for(
                                socket.send_bytes(pcm[offset : offset + frame_size]), idle_timeout
                            )
                    else:
                        raise PersonaPlexError("unexpected model frame")

            tasks.extend([asyncio.create_task(upstream()), asyncio.create_task(downstream())])
            async with asyncio.timeout(getattr(store.settings, "max_call_seconds", 3600.0)):
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
        except Exception:  # noqa: BLE001 - sanitize upstream exceptions containing brief URLs
            # Upstream exception text can contain the prompt-bearing URL.
            reason = "bridge_error"
        finally:
            finishing = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(finishing)
            except asyncio.CancelledError:
                await finishing
                raise

    return app
