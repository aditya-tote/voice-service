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


def test_absolute_deadlines_rebase_after_a_full_frame_stall_without_burst():
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


@pytest.mark.parametrize("wake_jitter,work_ms", [
    (0.001, 0), (0.0007, 1.2), (0.002, 3), (0.015, 1), (0.019, 0),
])
def test_small_wake_and_send_overhead_do_not_accumulate_clock_drift(wake_jitter, work_ms):
    clock = Deadline(0)
    now = 0.0
    for _ in range(15_000):  # Five minutes of media, without wall-clock sleeps.
        now += clock.delay(now) + wake_jitter
        now += work_ms / 1000
        clock.advance(now)
    assert now == pytest.approx(299.98 + wake_jitter + work_ms / 1000, abs=1e-7)
    assert clock.next == pytest.approx(300, abs=1e-7)
    assert clock.missed == 0


def test_100ms_provider_packets_do_not_fill_the_input_buffer_under_normal_jitter():
    # Reproduces the VM's 800-byte mu-law packets: 100 ms / 800 PCM16 samples.
    fifo = SampleFIFO(1000)
    clock = Deadline(.060)
    next_packet = now = 0.0
    ticks = 9000  # Three minutes; the old scheduler overflowed in ~20 seconds.
    for index in range(ticks):
        now += clock.delay(now) + (.0007 if index % 3 else .0015)
        while next_packet <= now:
            fifo.append(struct.pack("<h", 42) * 800)
            next_packet += .100
        clock.advance(now)
        pcm, real = fifo.tick()
        assert pcm == struct.pack("<h", 42) * 160 and real == 160
        now += .001  # Codec/send work must not become permanent extra frame time.
    assert fifo.overflows == fifo.silence == 0
    assert fifo.high_water / 8 <= 200
    assert fifo.real_sent == ticks * 160
    assert fifo.received - fifo.real_sent == fifo.samples
    assert now < 180.1


def test_paced_output_does_not_accumulate_a_backlog_from_send_overhead():
    fifo = PlaybackFIFO(400)
    clock = Deadline(0)
    next_generated = now = 0.0
    sends = []
    for _ in range(9000):
        now += clock.delay(now) + .001
        while next_generated <= now:
            fifo.check(640, 0)  # Model emits four 20 ms frames every 80 ms.
            fifo.append(["frame"] * 4, now)
            next_generated += .080
        frame = fifo.pop(now)
        sends.append(now)
        now += .0015  # Provider send/mark work is compensated on the next wait.
        fifo.complete(frame, now)
        clock.advance(now)
    assert fifo.overflows == 0
    assert fifo.high_water / 8 <= 100
    assert now < 180.01
    assert all(b - a >= .010 - 1e-9 for a, b in pairwise(sends))


def test_partial_frame_lateness_has_bounded_phase_correction():
    clock = Deadline(0)
    clock.advance(.015)
    assert clock.next == pytest.approx(.020)  # Compensate jitter without resetting phase.
    assert clock.delay(.015) == pytest.approx(.005)
    clock.advance(.020)
    assert clock.next == pytest.approx(.040)
    clock.advance(.059)
    assert clock.delay(.059) == pytest.approx(.001)  # Still a positive cooperative wait.


def test_repeated_real_stalls_remain_visible_instead_of_unbounded_catchup():
    clock = Deadline(0)
    for _ in range(100):
        now = clock.next + .105
        clock.advance(now)
        assert clock.delay(now) == pytest.approx(.020)
    assert clock.missed == 500
    assert clock.late == 100


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
                         "input_queue_ms": 120, "input_late_ticks": 15, "input_max_lateness_ms": 1.5,
                         "output_queue_high_water_ms": 400, "media_buffer_overflows": 1,
                         "interruption_clears": 2, "input_queue_high_water_ms": float("nan"),
                         "prompt": "secret", "mark_name": "customer-name"}
    output = json.loads(SafeJsonFormatter().format(record))
    assert output["input_missed_ticks"] == 3 and output["interruption_clears"] == 2
    assert output["input_queue_ms"] == 120 and output["input_late_ticks"] == 15
    assert output["input_max_lateness_ms"] == 1.5
    assert "secret" not in json.dumps(output) and "input_queue_high_water_ms" not in output
    assert error_code(MediaTimingError("output audio buffer overflow")) == "output_buffer_overflow"
