"""Bounded arrival-clock telephony primitives; no native audio or service imports.

Energy is an optional playback heuristic, never ASR or a semantic turn detector.
Provider acknowledgement estimates are not evidence of audible playback.
"""

import math
import struct
from collections import OrderedDict, deque
from dataclasses import dataclass

FRAME_SECONDS = 0.020
FRAME_SAMPLES = 160
MARK_FRAMES = 10
MARK_LIMIT = 64
ENERGY_GAP_MS = 40
CLEAR_COOLDOWN_SECONDS = 1.0
OUTPUT_ACTIVITY_RMS = 100  # Acoustic non-silence only; uncalibrated, not generated speech detection.


def pcm_rms(pcm: bytes) -> float:
    values = [value[0] for value in struct.iter_unpack("<h", pcm)]
    mean = sum(values) / len(values) if values else 0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)) if values else 0


class MediaTimingError(ValueError):
    pass


class SampleFIFO:
    def __init__(self, maximum_ms: int):
        self.limit = maximum_ms * 8
        self.data = bytearray()
        self.received = self.real_sent = self.silence = self.high_water = self.overflows = 0

    @property
    def samples(self):
        return len(self.data) // 2

    def append(self, pcm: bytes):
        if len(pcm) % 2:
            raise MediaTimingError("unaligned input PCM")
        self.received += len(pcm) // 2
        if self.samples + len(pcm) // 2 > self.limit:
            self.overflows += 1
            raise MediaTimingError("input audio buffer overflow")
        self.data.extend(pcm)
        self.high_water = max(self.high_water, self.samples)

    def tick(self) -> tuple[bytes, int]:
        count = min(FRAME_SAMPLES, self.samples)
        pcm = bytes(self.data[:count * 2])
        del self.data[:count * 2]
        self.real_sent += count
        self.silence += FRAME_SAMPLES - count
        return pcm + b"\0\0" * (FRAME_SAMPLES - count), count

    def clear(self):
        self.data.clear()

    def stats(self):
        return {"inputRealSamplesReceived": self.received, "inputRealSamplesSent": self.real_sent,
                "inputSyntheticSilenceSamples": self.silence, "inputQueueMs": self.samples / 8,
                "inputQueueHighWaterMs": self.high_water / 8, "inputOverflows": self.overflows,
                "inputTimingBasis": "arrival_clock"}


class Deadline:
    """Keep the nominal media phase through jitter; rebase after a full-frame stall.

    Small late wakes must shorten the next wait instead of permanently slowing
    the sample clock. A real missed frame starts a new schedule rather than a
    bulk catch-up loop. Each writer still yields between individual frames.
    """

    def __init__(self, first: float):
        self.next = first
        self.late = self.missed = 0
        self.max_lateness_ms = 0.0

    def delay(self, now: float) -> float:
        return max(0.0, self.next - now)

    def advance(self, now: float):
        lateness = max(0.0, now - self.next)
        if lateness > 0.001:
            self.late += 1
        self.missed += int((lateness + 1e-9) / FRAME_SECONDS)
        self.max_lateness_ms = max(self.max_lateness_ms, lateness * 1000)
        if lateness + 1e-9 >= FRAME_SECONDS:
            self.next = now + FRAME_SECONDS
        else:
            # Re-anchoring every tick to now + 20 ms accumulated normal sleep and
            # send overhead. A 1 ms late wake then consumed only ~48 frames/sec,
            # overflowing a healthy 50-frame/sec caller's bounded input queue.
            self.next += FRAME_SECONDS


@dataclass(frozen=True)
class PlaybackFrame:
    message: str
    epoch: int
    arrived: float
    non_silent: bool = False


class PlaybackFIFO:
    """Includes a dequeued/in-flight frame until complete; residual checked at admission."""

    def __init__(self, maximum_ms: int):
        self.limit = maximum_ms * 8
        self.frames = deque()
        self.held = None
        self.in_flight = None
        self.clear_in_flight = 0
        self.epoch = self.samples = self.high_water = self.overflows = 0
        self.max_age_ms = 0.0
        self.discarded_clear = self.discarded_termination = self.suppressed = 0
        self.generated = 0

    def check(self, new_samples: int, residual: int):
        self.generated += new_samples
        if self.samples + residual + new_samples > self.limit:
            self.overflows += 1
            raise MediaTimingError("output audio buffer overflow")
        self.high_water = max(self.high_water, self.samples + residual + new_samples)

    def append(self, messages: list[str], now: float, *, non_silent: list[bool] | None = None):
        flags = non_silent if non_silent is not None else [False] * len(messages)
        if len(flags) != len(messages):
            raise MediaTimingError("invalid output activity metadata")
        self.frames.extend(PlaybackFrame(message, self.epoch, now, active)
                           for message, active in zip(messages, flags, strict=True))
        self.samples += len(messages) * FRAME_SAMPLES

    @property
    def non_silent_pending(self):
        return bool(self.held and self.held.non_silent) or any(frame.non_silent for frame in self.frames)

    def pop(self, now: float):
        frame = self.frames.popleft()
        self.held = frame
        self.max_age_ms = max(self.max_age_ms, max(0, now - frame.arrived) * 1000)
        return frame

    def complete(self, frame: PlaybackFrame, now: float | None = None):
        if frame.epoch == self.epoch:
            self.samples -= FRAME_SAMPLES
            if now is not None:
                self.max_age_ms = max(self.max_age_ms, max(0, now - frame.arrived) * 1000)
        if self.held == frame:
            self.held = None
        if self.in_flight == frame:
            self.in_flight = None

    def begin_send(self, frame: PlaybackFrame):
        if frame.epoch == self.epoch:
            self.in_flight = frame

    @property
    def invalidatable_samples(self):
        return self.samples - (FRAME_SAMPLES if self.in_flight else 0)

    def clear(self, residual: int = 0, *, termination=False):
        discarded = self.samples + residual
        if termination:
            self.discarded_termination += discarded
        else:
            # An in-flight send may complete before clear. It cannot be called discarded.
            in_flight = FRAME_SAMPLES if self.in_flight else 0
            self.clear_in_flight += in_flight
            self.discarded_clear += discarded - in_flight
        self.epoch += 1
        self.frames.clear()
        self.held = None
        self.in_flight = None
        self.samples = 0

    def stats(self, now: float, residual: int = 0):
        oldest = self.held or (self.frames[0] if self.frames else None)
        age = max(0, now - oldest.arrived) * 1000 if oldest else 0
        return {"generatedAudioMs": self.generated / 8, "outputQueueMs": (self.samples + residual) / 8,
                "outputQueueHighWaterMs": self.high_water / 8, "outputOldestAgeMs": age,
                "outputMaxAgeMs": max(age, self.max_age_ms), "outputOverflows": self.overflows,
                "outputDiscardedClearMs": self.discarded_clear / 8,
                "outputInFlightAtClearMs": self.clear_in_flight / 8,
                "outputDiscardedTerminationMs": self.discarded_termination / 8,
                "outputSuppressedMs": self.suppressed / 8}


class MarkLedger:
    def __init__(self):
        self.pending = OrderedDict()
        self.retired = OrderedDict()
        self.sent = self.matched = self.unknown = self.invalidated = self.evicted = self.duplicate = 0
        self.invalidated_marks = 0
        self.last_rtt = self.max_rtt = 0.0
        self.acked_samples = self.sent_samples = 0

    def _retire(self, name, state):
        self.retired[name] = state
        while len(self.retired) > MARK_LIMIT:
            self.retired.popitem(last=False)

    def add(self, now: float, samples: int, epoch: int) -> str:
        self.sent += 1
        name = f"audio-{epoch}-{self.sent}"
        self.sent_samples = samples
        self.pending[name] = (now, samples, epoch)
        if len(self.pending) > MARK_LIMIT:
            old, _ = self.pending.popitem(last=False)
            self._retire(old, "evicted")
            self.evicted += 1
        return name

    def ack(self, name: str, now: float, epoch: int) -> str:
        item = self.pending.pop(name, None)
        if item is None:
            state = self.retired.get(name)
            if state == "invalidated":
                self.invalidated += 1
                return "invalidated"
            if state == "matched":
                self.duplicate += 1
                return "duplicate"
            self.unknown += 1
            return "unknown"
        sent, samples, generation = item
        if generation != epoch:
            self.invalidated += 1
            return "invalidated"
        self.matched += 1
        self.acked_samples = max(self.acked_samples, samples)
        self.last_rtt = max(0, now - sent) * 1000
        self.max_rtt = max(self.max_rtt, self.last_rtt)
        self._retire(name, "matched")
        return "matched"

    def clear(self):
        self.invalidated_marks += len(self.pending)
        for name in self.pending:
            self._retire(name, "invalidated")
        self.pending.clear()
        self.acked_samples = self.sent_samples = 0

    def stats(self):
        return {"marksSent": self.sent, "marksMatched": self.matched, "marksUnknown": self.unknown,
                "marksDuplicate": self.duplicate, "marksInvalidatedAcks": self.invalidated,
                "marksInvalidated": self.invalidated_marks,
                "marksEvicted": self.evicted, "markLastRttMs": self.last_rtt,
                "markMaxRttMs": self.max_rtt,
                "providerAckOutstandingEstimateMs": max(0, self.sent_samples - self.acked_samples) / 8}


class EnergyInterruption:
    def __init__(self, mode="off", threshold=1000, sustain_ms=400, release_ms=300):
        self.mode, self.threshold = mode, threshold
        self.sustain_ms, self.release_ms = sustain_ms, release_ms
        self.active_ms = self.gap_ms = self.quiet_ms = 0
        self.suppressed = False
        self.last_clear = -math.inf
        self.candidates = self.confirmed = 0

    def observe(self, pcm: bytes, real_samples: int, now: float, agent_active: bool) -> bool:
        if self.mode == "off":
            return False
        rms = pcm_rms(pcm[:real_samples * 2])
        loud = real_samples == FRAME_SAMPLES and rms >= self.threshold
        if self.suppressed:
            self.quiet_ms = 0 if loud else self.quiet_ms + 20
            if self.quiet_ms >= self.release_ms:
                self.suppressed = False
                self.active_ms = self.gap_ms = self.quiet_ms = 0
            return False
        if not agent_active or now - self.last_clear < CLEAR_COOLDOWN_SECONDS:
            self.active_ms = self.gap_ms = 0
            return False
        if loud:
            if not self.active_ms:
                self.candidates += 1
            self.active_ms += 20
        elif self.active_ms:
            self.gap_ms += 20
            if self.gap_ms > ENERGY_GAP_MS:
                self.active_ms = self.gap_ms = 0
        if self.active_ms >= self.sustain_ms:
            self.confirmed += 1
            self.suppressed = True
            self.last_clear = now
            self.quiet_ms = 0
            return True
        return False
