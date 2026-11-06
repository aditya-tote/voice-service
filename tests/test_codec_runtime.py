import pytest

numpy = pytest.importorskip("numpy")
pytest.importorskip("soxr")
pytest.importorskip("sphn")

from voice_harness.audio import StreamingCodec


def test_streaming_codec_accumulates_phone_frames_and_round_trips_opus():
    codec = StreamingCodec()
    # Enough continuous tone to cross resampler, Opus and Ogg buffering boundaries.
    samples = (numpy.sin(2 * numpy.pi * 440 * numpy.arange(16000) / 8000) * 12000).astype("<i2")
    output = []
    packet_count = 0
    for offset in range(0, len(samples), 160):
        for packet in codec.phone_to_opus(samples[offset : offset + 160].tobytes()):
            packet_count += 1
            output.append(codec.opus_to_phone(packet))
    decoded = numpy.frombuffer(b"".join(output), dtype="<i2")
    assert packet_count > 1
    assert 12000 < decoded.size <= 17000
    assert numpy.sqrt(numpy.mean(decoded.astype(numpy.float64) ** 2)) > 1000
    assert codec.pending.size < 960
    codec.close()
    assert codec.pending.size == 0


def test_continuous_silence_tone_silence_keeps_codec_and_resamplers():
    codec = StreamingCodec()
    identities = tuple(id(value) for value in (codec.writer, codec.reader, codec.to_model, codec.to_phone))
    tone = (numpy.sin(2 * numpy.pi * 440 * numpy.arange(8000) / 8000) * 6000).astype("<i2")
    samples = numpy.concatenate((numpy.zeros(8000, dtype="<i2"), tone, numpy.zeros(16000, dtype="<i2")))
    output = []
    try:
        for offset in range(0, samples.size, 160):
            for packet in codec.phone_to_opus(samples[offset:offset + 160].tobytes()):
                output.append(codec.opus_to_phone(packet))
        assert tuple(id(value) for value in (codec.writer, codec.reader, codec.to_model, codec.to_phone)) == identities
        decoded = numpy.frombuffer(b"".join(output), dtype="<i2").astype(numpy.float64)
        assert 24000 < decoded.size <= 34000
        assert numpy.max(numpy.abs(decoded)) < 32767
        assert numpy.sqrt(numpy.mean(decoded ** 2)) > 500
        assert numpy.sqrt(numpy.mean(decoded[-4000:] ** 2)) < 100
    finally:
        codec.close()
