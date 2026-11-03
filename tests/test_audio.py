import pytest

from voice_harness.audio import AudioError, RawPcm8kAdapter, StreamingCodec


def test_raw_pcm_adapter_rejects_malformed_frames():
    adapter = RawPcm8kAdapter(8)
    assert adapter.validate_inbound(b"\x00\x01") == b"\x00\x01"
    for bad in (b"", b"\x00", b"x" * 10):
        with pytest.raises(AudioError):
            adapter.validate_inbound(bad)


def test_codec_requires_the_declared_runtime_dependencies():
    with pytest.raises(AudioError):
        StreamingCodec()
