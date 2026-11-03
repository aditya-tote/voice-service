"""PersonaPlex's intentionally isolated, version-sensitive 0/1/2 protocol."""

import logging
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Brief


class PersonaPlexError(RuntimeError):
    pass


def prompt_for(brief: Brief) -> str:
    def block(name: str, values: str | list[str]) -> str:
        body = values if isinstance(values, str) else "\n".join(f"- {value}" for value in values)
        return f"{name}:\n{body}"

    return "\n\n".join(
        [
            block("Persona", brief.persona),
            block("Goal", brief.goal),
            block("Tone", brief.tone),
            block("Facts", brief.facts),
            block("Limits", brief.limits),
        ]
    )


def connection_url(base_url: str, brief: Brief) -> str:
    split = urlsplit(base_url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query.update({"voice_prompt": brief.voiceName, "text_prompt": prompt_for(brief)})
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), ""))


class PersonaPlexClient:
    """Uses the optional `websockets` dependency to protect FastAPI from upstream details."""

    def __init__(self, url: str, ready_timeout: float):
        self.url, self.ready_timeout, self.socket = url, ready_timeout, None

    async def connect(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:
            raise PersonaPlexError("Install voice-agent-harness[runtime] on the GPU VM") from error
        import asyncio

        quiet_logger = logging.getLogger("voice_harness.private_model")
        quiet_logger.disabled = True
        try:
            self.socket = await asyncio.wait_for(
                connect(
                    self.url,
                    max_size=16384,
                    max_queue=8,
                    close_timeout=5,
                    proxy=None,
                    logger=quiet_logger,
                ),
                self.ready_timeout,
            )
            frame = await asyncio.wait_for(self.socket.recv(), self.ready_timeout)
        except Exception:  # noqa: BLE001 - do not disclose prompt URLs in a chained exception
            with suppress(Exception):
                await self.close()
            raise PersonaPlexError("PersonaPlex connection or ready handshake failed") from None
        if frame != b"\x00":
            await self.close()
            raise PersonaPlexError("PersonaPlex returned an invalid ready handshake")

    async def send_opus(self, payload: bytes) -> None:
        if not self.socket:
            raise PersonaPlexError("PersonaPlex is not connected")
        await self.socket.send(b"\x01" + payload)

    async def recv(self) -> bytes:
        if not self.socket:
            raise PersonaPlexError("PersonaPlex is not connected")
        frame = await self.socket.recv()
        if not isinstance(frame, bytes) or not frame:
            raise PersonaPlexError("PersonaPlex returned a malformed frame")
        return frame

    async def close(self) -> None:
        if self.socket:
            await self.socket.close()
            self.socket = None
