"""ViH JSON framing and streaming G.711 mu-law/PCM16LE conversion."""

import base64
import binascii
import json
import re
import struct
from dataclasses import dataclass, field


class VihProtocolError(ValueError):
    pass


def decode_mulaw(data: bytes) -> bytes:
    """Decode ITU G.711 mu-law without the removed Python audioop dependency."""
    result = bytearray()
    for value in data:
        value = ~value & 255
        magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
        sample = 132 - magnitude if value & 128 else magnitude - 132
        result.extend(struct.pack("<h", sample))
    return bytes(result)


def encode_mulaw(pcm: bytes) -> bytes:
    if len(pcm) % 2:
        raise VihProtocolError("PCM must be 16-bit aligned")
    result = bytearray()
    for (sample,) in struct.iter_unpack("<h", pcm):
        sample >>= 2
        mask = 127 if sample < 0 else 255
        sample = min(abs(sample), 8159) + 33
        segment = max(0, sample.bit_length() - 6)
        result.append((127 if segment >= 8 else
                       (segment << 4) | ((sample >> (segment + 1)) & 15)) ^ mask)
    return bytes(result)


def mulaw_payload(payload: str) -> bytes:
    try:
        data = base64.b64decode(payload.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise VihProtocolError("invalid base64 media payload") from error
    if not data:
        raise VihProtocolError("empty media payload")
    return data


def mulaw_to_pcm(payload: str) -> bytes:
    return decode_mulaw(mulaw_payload(payload))


@dataclass
class VihOutboundAudio:
    """Preserve residual audio across model frames; emit aligned 20 ms packets."""

    stream_sid: str
    pending: bytes = b""
    chunk: int = 0

    def encode(self, pcm: bytes) -> list[str]:
        self.pending += encode_mulaw(pcm)
        return self._drain()

    def _drain(self) -> list[str]:
        messages = []
        while len(self.pending) >= 160:
            data, self.pending = self.pending[:160], self.pending[160:]
            self.chunk += 1
            messages.append(json.dumps({
                "event": "media", "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(data).decode("ascii"), "chunk": self.chunk},
            }, separators=(",", ":")))
        return messages

    def flush(self) -> list[str]:
        if self.pending:
            self.pending += b"\xff" * (160 - len(self.pending))
        return self._drain()

    def mark(self, name: str) -> str:
        if not name or len(name) > 128:
            raise VihProtocolError("invalid mark name")
        return json.dumps({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    def clear(self) -> str:
        self.pending = b""
        return json.dumps({"event": "clear", "streamSid": self.stream_sid})


def pcm_to_mulaw_messages(pcm: bytes, stream_sid: str) -> list[str]:
    """Single-block convenience helper. Live streams use VihOutboundAudio."""
    return VihOutboundAudio(stream_sid).encode(pcm)


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise VihProtocolError(f"{label} must be an object")
    return value


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
        raise VihProtocolError(f"invalid {label}")
    return value


@dataclass
class VihStream:
    max_frame_bytes: int = 16384
    stream_sid: str | None = None
    call_sid: str | None = None
    account_sid: str | None = None
    connected: bool = False
    started: bool = False
    stopped: bool = False
    sequence: int = 0
    sequence_gaps: int = 0
    media_frames: int = 0
    audio_bytes: int = 0
    dtmf_count: int = 0
    mark_count: int = 0
    last_event: dict = field(default_factory=dict)

    def ingest(self, message: str) -> tuple[str, bytes | None]:
        if not isinstance(message, str) or len(message.encode("utf-8")) > self.max_frame_bytes:
            raise VihProtocolError("text JSON event required within frame limit")
        try:
            event = _object(json.loads(message), "event")
        except (json.JSONDecodeError, RecursionError) as error:
            raise VihProtocolError("invalid JSON event") from error
        name = event.get("event")
        if name not in {"connected", "start", "media", "stop", "dtmf", "mark"}:
            raise VihProtocolError("unsupported event")
        if self.stopped:
            raise VihProtocolError("event after stop")
        if name == "connected":
            if self.connected:
                raise VihProtocolError("duplicate connected event")
            self.connected = True
            self.last_event = event
            return name, None
        if not self.connected:
            raise VihProtocolError("connected event required first")
        value = event.get("sequenceNumber")
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
            raise VihProtocolError("invalid sequenceNumber")
        sequence = int(value)
        if sequence <= self.sequence:
            raise VihProtocolError("non-monotonic sequence")
        self.sequence_gaps += sequence - self.sequence - 1
        self.sequence = sequence
        if name == "start":
            if self.started:
                raise VihProtocolError("duplicate start event")
            start = _object(event.get("start"), "start")
            stream_sid = _identifier(event.get("streamSid"), "streamSid")
            if start.get("streamSid") != stream_sid:
                raise VihProtocolError("streamSid mismatch")
            self.call_sid = _identifier(start.get("callSid"), "callSid")
            self.account_sid = _identifier(start.get("accountSid"), "accountSid")
            format_ = _object(start.get("mediaFormat"), "mediaFormat")
            if (format_.get("encoding") != "audio/x-mulaw"
                    or type(format_.get("sampleRate")) is not int
                    or format_["sampleRate"] != 8000
                    or format_.get("bitDepth") != 8 or format_.get("bitRate") != 64):
                raise VihProtocolError("unsupported media format")
            self.started, self.stream_sid = True, stream_sid
        else:
            if not self.started or event.get("streamSid") != self.stream_sid:
                raise VihProtocolError("event is not for active stream")
            if name == "media":
                media = _object(event.get("media"), "media")
                payload = media.get("payload")
                if not isinstance(payload, str):
                    raise VihProtocolError("media payload required")
                encoded = mulaw_payload(payload)
                self.media_frames += 1
                self.audio_bytes += len(encoded)
                self.last_event = event
                return name, decode_mulaw(encoded)
            if name == "stop":
                stop = _object(event.get("stop"), "stop")
                if stop.get("callSid") != self.call_sid or stop.get("accountSid") != self.account_sid:
                    raise VihProtocolError("stop call/account mismatch")
                self.stopped = True
            elif name == "dtmf":
                digit = _object(event.get("dtmf"), "dtmf").get("digit")
                if not isinstance(digit, str) or not re.fullmatch(r"[0-9A-D*#]", digit):
                    raise VihProtocolError("invalid dtmf digit")
                self.dtmf_count += 1
            elif name == "mark":
                label = _object(event.get("mark"), "mark").get("name")
                if not isinstance(label, str) or not label or len(label) > 128:
                    raise VihProtocolError("invalid mark label")
                self.mark_count += 1
        self.last_event = event
        return name, None

    def stats(self) -> dict:
        return {"inboundMediaFrames": self.media_frames, "inboundAudioBytes": self.audio_bytes,
                "sequenceGaps": self.sequence_gaps, "dtmfCount": self.dtmf_count,
                "markAcknowledgements": self.mark_count}
