import asyncio
import sys

from .config import Settings
from .models import Brief
from .personaplex import PersonaPlexClient, connection_url


async def _run() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: voice-harness-probe NATF2.pt")
    settings, voice = Settings.from_env(), sys.argv[1]
    if voice not in settings.allowed_voices:
        raise SystemExit("voice is not in ALLOWED_VOICES")
    brief = Brief(
        leadId="probe",
        persona="Brief test",
        goal="Say hello",
        tone="calm",
        facts=[],
        limits=[],
        voiceName=voice,
    )
    client = PersonaPlexClient(
        connection_url(settings.personaplex_url, brief), settings.ready_timeout_seconds
    )
    await client.connect()
    print("PersonaPlex ready handshake received")
    codec = StreamingCodec()
    for _ in range(3):
        for packet in codec.phone_to_opus(b"\x00\x00" * 160):
            await client.send_opus(packet)
    audio, text = False, False
    try:
        while not (audio and text):
            frame = await asyncio.wait_for(client.recv(), 30)
            audio |= frame[0] == 1 and len(frame) > 1
            text |= frame[0] == 2 and len(frame) > 1
    finally:
        await client.close()
    if not (audio and text):
        raise RuntimeError("PersonaPlex did not return both Opus and text")
    print("PersonaPlex Opus and text frames received")


def main() -> None:
    asyncio.run(_run())


from .audio import StreamingCodec
