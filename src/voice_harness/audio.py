"""Provider-neutral raw PCM test adapter and PersonaPlex Opus codec."""

from importlib.util import find_spec


class AudioError(ValueError):
    pass


class RawPcm8kAdapter:
    sample_rate = 8000

    def __init__(self, max_frame_bytes: int):
        self.max_frame_bytes = max_frame_bytes

    def validate_inbound(self, frame: bytes) -> bytes:
        if not frame or len(frame) > self.max_frame_bytes or len(frame) % 2:
            raise AudioError("expected bounded PCM16LE mono 8 kHz frame")
        return frame


class StreamingCodec:
    """Stateful 8 kHz PCM16 <-> 24 kHz sphn streaming Ogg/Opus conversion."""

    def __init__(self) -> None:
        if any(find_spec(name) is None for name in ("numpy", "soxr", "sphn")):
            raise AudioError("install voice-agent-harness[runtime] for audio conversion")
        import numpy as np
        import soxr
        import sphn

        self.np, self.sphn = np, sphn
        self.to_model = soxr.ResampleStream(8000, 24000, 1, dtype="float32", quality="HQ")
        self.to_phone = soxr.ResampleStream(24000, 8000, 1, dtype="float32", quality="HQ")
        self.writer, self.reader = sphn.OpusStreamWriter(24000), sphn.OpusStreamReader(24000)
        self.pending = np.empty(0, dtype=np.float32)

    def phone_to_opus(self, frame: bytes) -> list[bytes]:
        pcm = self.np.frombuffer(frame, dtype="<i2").astype(self.np.float32) / 32768.0
        self.pending = self.np.concatenate(
            (self.pending, self.to_model.resample_chunk(pcm, last=False))
        )
        packets: list[bytes] = []
        while self.pending.size >= 960:
            chunk, self.pending = self.pending[:960], self.pending[960:]
            produced = self.writer.append_pcm(chunk)
            if hasattr(self.writer, "read_bytes"):
                produced = self.writer.read_bytes()
            if produced:
                packets.append(bytes(produced))
        return packets

    def opus_to_phone(self, packet: bytes) -> bytes:
        decoded = self.reader.append_bytes(packet)
        if hasattr(self.reader, "read_pcm"):
            decoded = self.reader.read_pcm()
        if getattr(decoded, "size", 0) == 0:
            return b""
        samples = self.to_phone.resample_chunk(decoded, last=False)
        return self.np.clip(samples * 32767.0, -32768, 32767).astype("<i2").tobytes()

    def close(self) -> None:
        # The call has ended; discard any partial frame and release codec/audio buffers.
        self.pending = self.np.empty(0, dtype=self.np.float32)
        self.writer = self.reader = self.to_model = self.to_phone = None
