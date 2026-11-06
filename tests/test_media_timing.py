import json
import logging
import struct
from itertools import pairwise

import pytest
from test_vih_poc import settings

from voice_harness.media_timeline import Timeline
from voice_harness.media_timing import (
    Deadline,
    EnergyInterruption,
    MarkLedger,
    MediaTimingError,
    PlaybackFIFO,
    SampleFIFO,
)
from voice_harness.observability import SafeJsonFormatter, error_code

LOUD = struct.pack("<hh", -2000, 2000) * 80
ZERO = b"\0\0" * 160


def test_sample_clock_splits_partial_frames_and_preserves_silence():
    fifo = SampleFIFO(100)
    assert fifo.tick() == (ZERO, 0)
    fifo.append(struct.pack("<h", 42) * 240)
    assert fifo.tick() == (struct.pack("<h", 42) * 160, 160)
    assert fifo.tick() == (struct.pack("<h", 42) * 80 + b"\0\0" * 80, 80)
    assert fifo.real_sent + fifo.silence == 3 * 160
    fifo.append(ZERO * 5)
    with pytest.raises(MediaTimingError, match="overflow"):
        fifo.append(b"\0\0")
    assert fifo.samples == 800 and fifo.overflows == 1
    assert fifo.received == 1041  # Includes the rejected real sample, not synthetic padding.
    with pytest.raises(MediaTimingError, match="unaligned"):
        fifo.append(b"x")


def test_absolute_deadlines_never_burst_after_stall_or_late_send():
    clock = Deadline(0)
    sends = []
    now = 0
    for index in range(10):
        now += clock.delay(now)
        if index == 3:
            now += 0.105
        sends.append(now)
        clock.advance(now)
    assert all(b - a >= 0.020 - 1e-9 for a, b in pairwise(sends))
    assert clock.missed == 5 and clock.late == 1
    assert clock.max_lateness_ms == pytest.approx(105)


def test_playback_counts_dequeued_frame_residual_and_invalidates_epochs():
    queue = PlaybackFIFO(100)
    queue.check(800, 0)
    queue.append(["frame"] * 5, 0)
    held = queue.pop(0.020)
    with pytest.raises(MediaTimingError, match="overflow"):
        queue.check(1, 0)
    assert queue.samples == 800
    queue.clear(10)
    assert held.epoch != queue.epoch
    queue.append(["new"], 1)
    queue.complete(held)
    assert queue.samples == 160
    assert queue.discarded_clear == 810
    queue.clear(20, termination=True)
    assert queue.discarded_termination == 180
    assert queue.stats(2)["outputMaxAgeMs"] == 20


def test_mark_metadata_is_capped_and_cleared_acks_are_not_playback_proof():
    ledger = MarkLedger()
    names = [ledger.add(index / 5, (index + 1) * 1600, 0) for index in range(100)]
    assert len(ledger.pending) == 64 and ledger.evicted == 36
    assert ledger.ack(names[0], 21, 0) == "unknown"
    assert ledger.ack(names[-1], 21, 0) == "matched"
    assert ledger.ack(names[-1], 22, 0) == "duplicate"
    assert ledger.stats()["providerAckOutstandingEstimateMs"] == 0
    ledger.clear()
    assert ledger.ack(names[-2], 22, 1) == "invalidated"
    assert len(ledger.pending) == 0 and len(ledger.retired) <= 64
    assert ledger.stats()["providerAckOutstandingEstimateMs"] == 0


def test_clear_distinguishes_unretractable_in_flight_audio_from_discarded_queue():
    queue = PlaybackFIFO(100)
    queue.append(["first", "second"], 0)
    held = queue.pop(0)
    queue.begin_send(held)
    assert queue.invalidatable_samples == 160
    queue.clear(10)
    queue.complete(held, .5)
    assert queue.stats(.5)["outputDiscardedClearMs"] == 170 / 8
    assert queue.stats(.5)["outputInFlightAtClearMs"] == 20


def test_acoustic_output_activity_tracks_queued_and_held_frames_not_silence():
    queue = PlaybackFIFO(100)
    queue.append(["encoded silence", "non-silent energy"], 0, non_silent=[False, True])
    assert queue.non_silent_pending
    silent = queue.pop(0)
    queue.complete(silent)
    active = queue.pop(.02)
    assert queue.non_silent_pending  # Includes held/in-flight, not only the deque.
    queue.complete(active)
    assert not queue.non_silent_pending
    queue.append(["encoded silence"] * 5, .04)
    assert not queue.non_silent_pending
    queue.clear()
    assert not queue.non_silent_pending


@pytest.mark.parametrize("mode,pcm,active", [("off", LOUD, True), ("sustained_energy", ZERO, True),
                                          ("sustained_energy", struct.pack("<h", 12000) * 160, True),
                                          ("sustained_energy", LOUD, False)])
def test_energy_never_clears_when_disabled_dc_quiet_or_agent_idle(mode, pcm, active):
    energy = EnergyInterruption(mode)
    assert not any(energy.observe(pcm, 160, index * .02, active) for index in range(100))
    assert not energy.suppressed


def test_short_backchannel_gap_budget_release_and_cooldown():
    energy = EnergyInterruption("sustained_energy")
    assert not any(energy.observe(LOUD, 160, i * .02, True) for i in range(10))
    for i in range(3):
        assert not energy.observe(ZERO, 0, .2 + i * .02, True)
    assert energy.active_ms == 0
    now = .3
    for i in range(19):
        assert not energy.observe(LOUD, 160, now, True)
        now += .02
        if i in {3, 8}:
            assert not energy.observe(ZERO, 0, now, True)
            now += .02
    assert energy.observe(LOUD, 160, now, True)
    assert energy.suppressed and energy.confirmed == 1
    for _ in range(14):
        now += .02
        assert not energy.observe(ZERO, 0, now, True)
    assert energy.suppressed
    now += .02
    assert not energy.observe(ZERO, 0, now, True)
    assert not energy.suppressed
    assert not any(energy.observe(LOUD, 160, now + i * .02, True) for i in range(20))


@pytest.mark.parametrize("changes", [
    {"media_input_startup_ms": 21}, {"media_input_startup_ms": 200, "media_input_max_buffer_ms": 200},
    {"media_output_max_buffer_ms": 2020}, {"media_interruption_mode": "vad"},
    {"media_interruption_rms": float("nan")}, {"media_interruption_rms": float("inf")},
    {"media_interruption_sustain_ms": 280}, {"media_interruption_release_ms": 80},
])
def test_media_settings_reject_invalid_values(tmp_path, changes):
    with pytest.raises(ValueError, match="MEDIA_"):
        settings(tmp_path, **changes)


def test_timeline_exclusive_private_capped_and_safe_schema(tmp_path):
    path = tmp_path / "timeline.jsonl"
    timeline = Timeline(path, limit=200)
    timeline.write("caller_arrival", 20, 160, 800, 0)
    for _ in range(20):
        timeline.write("synthetic_silence", 40, 160)
    timeline.flush()
    assert timeline.truncated and path.stat().st_size <= 200
    with pytest.raises(FileExistsError):
        Timeline(path)
    with pytest.raises(ValueError):
        timeline.write("secret customer name", 0)
    with pytest.raises(ValueError):
        timeline.write("clear", float("nan"))
    timeline.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["byteOffset"] == 800
    assert set(rows[0]) == {"v", "event", "offsetMs", "samples", "byteOffset", "epoch"}


def test_formatter_only_allows_explicit_numeric_media_aggregates():
    record = logging.LogRecord("voice_harness", 20, "", 1, "secret", (), None)
    record.safe_event = {"event": "media_progress", "input_missed_ticks": 3,
                         "output_queue_high_water_ms": 400, "media_buffer_overflows": 1,
                         "interruption_clears": 2, "input_queue_high_water_ms": float("nan"),
                         "prompt": "secret", "mark_name": "customer-name"}
    output = json.loads(SafeJsonFormatter().format(record))
    assert output["input_missed_ticks"] == 3 and output["interruption_clears"] == 2
    assert "secret" not in json.dumps(output) and "input_queue_high_water_ms" not in output
    assert error_code(MediaTimingError("output audio buffer overflow")) == "output_buffer_overflow"
