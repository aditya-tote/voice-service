"""Drive real-time model input while checking audio decoding and text output."""

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass

from .audio import StreamingCodec
from .config import Settings
from .models import Brief
from .personaplex import PersonaPlexClient, connection_url


class ProbeError(RuntimeError):
    pass


@dataclass
class ProbeStats:
    phone_frames_sent: int = 0
    opus_packets_sent: int = 0
    audio_packets_received: int = 0
    decoded_pcm_bytes: int = 0
    text_frames: int = 0

    def summary(self) -> str:
        return (
            f"phoneFramesSent={self.phone_frames_sent}, opusPacketsSent={self.opus_packets_sent}, "
            f"audioPacketsReceived={self.audio_packets_received}, "
            f"decodedPcmBytes={self.decoded_pcm_bytes}, textFrames={self.text_frames}"
        )


async def run_probe(
    settings: Settings, voice: str, *, timeout_seconds: float = 30, frame_seconds: float = 0.02
) -> ProbeStats:
    if voice not in settings.allowed_voices:
        raise ProbeError("voice is not in ALLOWED_VOICES")
    if not 0 < timeout_seconds <= 600 or not 0 < frame_seconds <= 1:
        raise ProbeError("timeout must be within 0-600 seconds and frame interval within 0-1 seconds")
    brief = Brief(
        leadId="probe", persona="A friendly assistant starting a brief microphone test",
        goal="Start speaking now. Say hello and ask how you can help.", tone="calm",
        facts=[], limits=["Keep the greeting brief"], voiceName=voice,
    )
    client = PersonaPlexClient(
        connection_url(settings.personaplex_url, brief), settings.ready_timeout_seconds
    )
    codec = None
    tasks: list[asyncio.Task] = []
    stats = ProbeStats()
    try:
        await client.connect()
        print("PersonaPlex ready handshake received", flush=True)
        codec = StreamingCodec()

        async def send_audio() -> None:
            while True:
                # 160 PCM16 samples at 8 kHz are 20 ms. Keep the stream open and paced.
                for packet in codec.phone_to_opus(b"\x00\x00" * 160):
                    await client.send_opus(packet)
                    stats.opus_packets_sent += 1
                stats.phone_frames_sent += 1
                await asyncio.sleep(frame_seconds)

        async def receive_output() -> None:
            while not (stats.decoded_pcm_bytes and stats.text_frames):
                frame = await client.recv()
                if not isinstance(frame, bytes) or not frame or frame[0] not in {1, 2}:
                    raise ProbeError("unexpected model frame")
                if len(frame) < 2:
                    continue
                if frame[0] == 1:
                    stats.audio_packets_received += 1
                    if stats.audio_packets_received == 1:
                        print("PersonaPlex audio frame received; checking decoded PCM", flush=True)
                    stats.decoded_pcm_bytes += len(codec.opus_to_phone(frame[1:]))
                else:
                    if frame[1:].decode("utf-8").strip():
                        stats.text_frames += 1
                        if stats.text_frames == 1:
                            print("PersonaPlex text frame received", flush=True)

        tasks = [asyncio.create_task(send_audio()), asyncio.create_task(receive_output())]
        async with asyncio.timeout(timeout_seconds):
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        if not (stats.decoded_pcm_bytes and stats.text_frames):
            raise ProbeError("model did not return decoded audio and text")
        print("PersonaPlex Opus and text frames received", flush=True)
        print(stats.summary(), flush=True)
        return stats
    except TimeoutError:
        raise ProbeError(
            f"Timed out after {timeout_seconds:g}s of continuous audio: {stats.summary()}. "
            "Check PersonaPlex server logs and that no other session is using the model. "
            "Silence may not elicit a spoken/text response; this result alone does not prove "
            "the model is unavailable."
        ) from None
    except ProbeError:
        raise
    except Exception as error:  # noqa: BLE001 - model exceptions can carry prompt URLs
        raise ProbeError(f"Probe failed ({type(error).__name__}): {stats.summary()}") from None
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if codec is not None:
            with suppress(Exception):
                codec.close()
        with suppress(Exception):
            await asyncio.wait_for(client.close(), settings.close_timeout_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("voice", help="an ALLOWED_VOICES .pt filename")
    parser.add_argument("--timeout", type=float, default=30, help="output-check deadline in seconds")
    args = parser.parse_args()
    try:
        asyncio.run(run_probe(Settings.from_env(), args.voice, timeout_seconds=args.timeout))
    except (ProbeError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
