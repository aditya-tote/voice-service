import asyncio
import json
import os
import struct
import sys
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from voice_harness import evaluation
from voice_harness.personaplex import build_outbound_prompt, model_connection_url

MANIFEST = Path(__file__).resolve().parents[1] / "examples/personaplex-eval/manifest.json"


def fixtures(tmp_path):
    root = tmp_path / "fixtures"
    evaluation.generate_fixtures(root)
    return root


def test_dry_run_no_network_native_or_service_imports(tmp_path, monkeypatch, capsys):
    root = fixtures(tmp_path)
    before = set(sys.modules)
    def forbidden(*args, **kwargs):
        raise AssertionError("no network in dry run")
    monkeypatch.setattr(asyncio, "run", forbidden)
    output = tmp_path / "results"
    assert evaluation.main(["--manifest", str(MANIFEST), "--fixture-root", str(root), "--output", str(output),
                            "--voices", "NATF2.pt", "NATM1.pt", "--repeats", "3"]) == 0
    added = set(sys.modules) - before
    assert not added.intersection({"sphn", "numpy", "soxr", "voice_harness.call_service", "voice_harness.repository"})
    summaries = list(output.glob("*/summary.json"))
    assert len(summaries) == 6
    summary = json.loads(summaries[0].read_text())
    assert summary["status"] == "dry_run_not_evaluated" and summary["variant"] == "baseline"
    assert summary["qualityStatus"] == "not_evaluated_transport_only"
    assert all(value is None for value in summary["humanScores"].values())
    assert len(summary["promptHash"]) == 64 and summary["effectiveSampling"] == "unknown_stock_server"
    stdout = capsys.readouterr().out
    assert "text_prompt" not in stdout and "Test Customer" not in stdout and "ws://" not in stdout


def test_baseline_exact_identity_and_shared_budget(tmp_path):
    root = fixtures(tmp_path)
    _, cases = evaluation.read_manifest(MANIFEST, root)
    case = cases[0]
    prompt = build_outbound_prompt(case["facts"], now=case["instant"])
    # Frozen against pre-integration HEAD, not another call into the refactored function.
    assert evaluation.digest(prompt.encode()) == "ce05e823a84e8b73f2274afe606466153dde039849bbe7c6abcc15e7aaae3bec"
    url = model_connection_url("ws://127.0.0.1:8998/api/chat", case["facts"], "NATF2.pt", now=case["instant"])
    assert parse_qs(urlsplit(url).query)["text_prompt"] == [prompt]
    assert evaluation.budgeted_prompt_url("ws://127.0.0.1:8998/api/chat", prompt, "NATF2.pt") == url
    assert case["instant"] == datetime.fromisoformat("2026-11-06T04:30:00+00:00")


@pytest.mark.parametrize("extra", [["--run-model"], ["--confirm-model-idle"],
                                  ["--run-model", "--confirm-model-idle", "--dry-run"],
                                  ["--personaplex-url", "ws://remote.example/api/chat"],
                                  ["--personaplex-url", "ws://secret@localhost/api/chat"],
                                  ["--personaplex-url", "ws://localhost/api/chat?key=secret"],
                                  ["--repeats", "101"]])
def test_execution_gates_reject_before_network(tmp_path, extra, capsys):
    root = fixtures(tmp_path)
    assert evaluation.main(["--manifest", str(MANIFEST), "--fixture-root", str(root),
                            "--output", str(tmp_path / "output"), *extra]) == 2
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize("change", [
    {"wav": "../tone.wav"}, {"trailingSilenceSeconds": 4}, {"language": "hi"},
    {"intervals": [{"kind": "activity", "start": 2, "end": 1}]},
    {"intervals": [{"kind": "activity", "start": 1, "end": 4}]},
    {"kind": "consented_english_speech"}, {"preparedAt": "2026-11-06T00:00:00"},
])
def test_invalid_manifest_inputs(tmp_path, change):
    root = fixtures(tmp_path)
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0].update(change)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        evaluation.read_manifest(path, root)


@pytest.mark.asyncio
async def test_fake_duplex_replay_cleanup_private_outputs_and_unrated_status(tmp_path):
    # Shortened in-memory case only: manifest validator enforces >=5s trailing silence.
    output = tmp_path / "run"
    output.mkdir()
    case = {"pcm": b"\0\0" * 160 * 4, "duration": .08}
    instances = []
    class Client:
        def __init__(self, *args):
            self.frames = asyncio.Queue()
            self.sent = 0
            self.closed = False
            instances.append(self)
        async def connect(self):
            await self.frames.put(b"\x02agent only")
        async def send_opus(self, packet):
            self.sent += 1
            await self.frames.put(b"\x01audio")
        async def recv(self):
            return await self.frames.get()
        async def close(self):
            self.closed = True
    class Codec:
        def __init__(self):
            self.closed = False
            instances.append(self)
        def phone_to_opus(self, pcm):
            assert len(pcm) == 320
            return [b"opus"]
        def opus_to_phone(self, packet):
            return b"\0\0" * 160
        def close(self):
            self.closed = True
    result = await evaluation.replay(case, "not connected", output, client_factory=Client, codec_factory=Codec)
    assert result["status"] == "completed_unrated"
    assert result["inputRealSamplesSent"] == 640 and result["sentAudioMs"] > 0
    assert instances[0].sent == 4 and all(instance.closed for instance in instances)
    assert json.loads((output / "agent-text.json").read_text())["text"] == "agent only"
    assert "caller_asr" in (output / "agent-text.json").read_text()
    assert result["connectToReadyMs"] is not None


def test_explicit_model_flags_use_exclusive_lock_and_preserve_other_owner(tmp_path, monkeypatch):
    root = fixtures(tmp_path)
    lock_dir = tmp_path / "locks"
    evaluation.private_directory(lock_dir)
    monkeypatch.setattr(evaluation, "MODEL_LOCK_DIRECTORY", lock_dir)
    invoked = []
    async def fake_replay(case, url, output):
        assert (lock_dir / ".model.lock").exists()
        invoked.append(case["id"])
        return {"status": "completed_unrated"}
    monkeypatch.setattr(evaluation, "replay", fake_replay)
    args = ["--manifest", str(MANIFEST), "--fixture-root", str(root), "--output", str(tmp_path / "runs"),
            "--run-model", "--confirm-model-idle", "--model-label", "fake-test-client-only", "--repeats", "1"]
    assert evaluation.main(args) == 0
    assert invoked == ["synthetic-tone-transport"]
    assert not (lock_dir / ".model.lock").exists()
    (lock_dir / ".model.lock").write_bytes(b"other-owner")
    assert evaluation.main(args) == 2
    assert (lock_dir / ".model.lock").read_bytes() == b"other-owner"
    assert len(invoked) == 1


@pytest.mark.asyncio
async def test_replay_failed_connect_closes_resources_and_hides_url(tmp_path):
    class Client:
        closed = False
        def __init__(self, *args):
            pass
        async def connect(self):
            raise RuntimeError("ws://private/?text_prompt=secret")
        async def close(self):
            Client.closed = True
    result = await evaluation.replay({"pcm": b"\0\0" * 160, "duration": .02}, "not-connected", tmp_path,
                                     client_factory=Client, codec_factory=lambda: None)
    assert result["status"] == "failed" and Client.closed
    assert "secret" not in json.dumps(result)
    assert json.loads((tmp_path / "agent-text.json").read_text())["text"] == ""


def test_generator_is_exclusive_and_invalid_wav_and_matrix_reject(tmp_path):
    root = fixtures(tmp_path)
    with pytest.raises(FileExistsError):
        evaluation.generate_fixtures(root)
    (root / "tone.wav").write_bytes(b"not-a-wav")
    assert evaluation.main(["--manifest", str(MANIFEST), "--fixture-root", str(root),
                            "--output", str(tmp_path / "output")]) == 2


@pytest.mark.parametrize("location,field,value", [
    ("root", "unknown", "ignored-policy"), ("root", "version", True),
    ("case", "unknown", "ignored-policy"), ("case", "id", 3),
    ("case", "consentConfirmed", 1), ("case", "trailingSilenceSeconds", True),
    ("case", "trailingSilenceSeconds", float("nan")),
    ("case", "trailingSilenceSeconds", float("inf")),
    ("case", "wav", "C:tone.wav"), ("case", "wav", "sub\\tone.wav"),
    ("interval", "unknown", True), ("interval", "start", False),
    ("interval", "end", float("inf")), ("interval", "end", float("nan")),
])
def test_strict_manifest_schema_types_and_finiteness(tmp_path, location, field, value):
    root = fixtures(tmp_path)
    manifest = json.loads(MANIFEST.read_text())
    target = (manifest if location == "root" else manifest["cases"][0] if location == "case"
              else manifest["cases"][0]["intervals"][0])
    target[field] = value
    path = tmp_path / "strict.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        evaluation.read_manifest(path, root)


def test_manifest_duplicate_fields_rejected(tmp_path):
    root = fixtures(tmp_path)
    path = tmp_path / "duplicate.json"
    path.write_text(MANIFEST.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ValueError, match="duplicate"):
        evaluation.read_manifest(path, root)


@pytest.mark.parametrize("value", ["ws://@localhost/api/chat", "ws://localhost/api/chat?",
                                   "ws://localhost/api/chat#", "ws://local\nhost/api/chat",
                                   "ws://127.0.0.1:99999/api/chat", "ws://127.0.0.1/api/ chat"])
def test_loopback_endpoint_requires_clean_authority_and_raw_url(value):
    with pytest.raises(ValueError):
        evaluation.loopback_url(value)


@pytest.mark.parametrize("arguments", [["--repeats", "ws://private/?secret=token"],
                                       ["--voices", "ws://private/?secret=token"],
                                       ["--unknown", "ws://private/?secret=token"]])
def test_parser_does_not_echo_rejected_private_argument_values(tmp_path, capsys, arguments):
    with pytest.raises(SystemExit) as error:
        evaluation.main(["--output", str(tmp_path), *arguments])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "secret" not in output.err and "ws://" not in output.err


def test_in_root_fixture_symlink_is_rejected_without_resolving_it(tmp_path, monkeypatch):
    root = fixtures(tmp_path)
    # Windows developer mode/admin privileges are not required: simulate the lstat result.
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == root / "tone.wav" or original(self))
    with pytest.raises(ValueError, match="unsafe"):
        evaluation.read_manifest(MANIFEST, root)


@pytest.mark.parametrize("channels,width,rate", [(2, 2, 8000), (1, 1, 8000), (1, 2, 24000)])
def test_wav_format_must_be_exact(tmp_path, channels, width, rate):
    root = fixtures(tmp_path)
    with wave.open(str(root / "tone.wav"), "wb") as wav:
        wav.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0" * (8 * rate * channels * width))
    with pytest.raises(ValueError, match="PCM16"):
        evaluation.read_manifest(MANIFEST, root)


def test_wav_oversize_truncation_and_nonzero_tail_rejected(tmp_path):
    root = fixtures(tmp_path)
    path = root / "tone.wav"
    original = path.read_bytes()
    with path.open("ab") as stream:
        stream.truncate(evaluation.MAX_WAV_BYTES + 1)
    with pytest.raises(ValueError, match="oversized"):
        evaluation.read_manifest(MANIFEST, root)
    path.write_bytes(original[:-2])
    with pytest.raises(ValueError, match="truncated"):
        evaluation.read_manifest(MANIFEST, root)
    path.write_bytes(original[:-2] + struct.pack("<h", 1))
    with pytest.raises(ValueError, match="trailing"):
        evaluation.read_manifest(MANIFEST, root)


@pytest.mark.asyncio
async def test_duplex_replay_cancel_closes_every_resource_and_leaves_no_tasks(tmp_path):
    connected = asyncio.Event()
    closed = []
    class Client:
        def __init__(self, *args):
            pass
        async def connect(self):
            connected.set()
        async def send_opus(self, packet):
            pass
        async def recv(self):
            await asyncio.Future()
        async def close(self):
            closed.append("client")
    class Codec:
        def phone_to_opus(self, pcm):
            return []
        def close(self):
            closed.append("codec")
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(evaluation.replay({"pcm": b"\0\0" * 8000, "duration": 1},
                                               "not-connected", tmp_path, client_factory=Client, codec_factory=Codec))
    await connected.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["client", "codec"]
    assert not (set(asyncio.all_tasks()) - before)
    with wave.open(str(tmp_path / "agent.wav"), "rb") as wav:
        assert wav.getframerate() == 8000
    if os.name == "posix":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["timeline", "client", "codec"])
async def test_replay_close_failure_is_not_reported_completed(tmp_path, monkeypatch, resource):
    class BrokenTimeline(evaluation.Timeline):
        def close(self):
            super().close()
            if resource == "timeline":
                raise OSError("private path")
    monkeypatch.setattr(evaluation, "Timeline", BrokenTimeline)
    class Client:
        def __init__(self, *args):
            pass
        async def connect(self):
            pass
        async def send_opus(self, packet):
            pass
        async def recv(self):
            await asyncio.Future()
        async def close(self):
            if resource == "client":
                raise OSError("private model url")
    class Codec:
        def phone_to_opus(self, pcm):
            return []
        def close(self):
            if resource == "codec":
                raise OSError("private path")
    result = await evaluation.replay({"pcm": b"\0\0" * 160, "duration": .02}, "not-connected", tmp_path,
                                     client_factory=Client, codec_factory=Codec)
    assert result["status"] == "failed"


def test_failed_model_run_stops_matrix_before_next_connection(tmp_path, monkeypatch):
    root = fixtures(tmp_path)
    monkeypatch.setattr(evaluation, "MODEL_LOCK_DIRECTORY", tmp_path / "locks")
    runs = []
    async def failed_replay(case, url, output):
        runs.append(case["id"])
        return {"status": "failed"}
    monkeypatch.setattr(evaluation, "replay", failed_replay)
    result = evaluation.main(["--manifest", str(MANIFEST), "--fixture-root", str(root),
                              "--output", str(tmp_path / "output"), "--run-model", "--confirm-model-idle",
                              "--model-label", "fake-client-only", "--repeats", "3",
                              "--voices", "NATF2.pt", "NATM1.pt"])
    assert result == 1 and len(runs) == 1
    assert len(list((tmp_path / "output").glob("*/summary.json"))) == 1
    assert not (tmp_path / "locks/.model.lock").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["overflow", "send_timeout"])
async def test_fake_duplex_failures_are_bounded_and_close_resources(tmp_path, monkeypatch, failure):
    original_wait = asyncio.wait_for
    async def fast_deadlines(awaitable, timeout):
        return await original_wait(awaitable, .02 if timeout == 10 else timeout)
    monkeypatch.setattr(asyncio, "wait_for", fast_deadlines)
    closed = []
    class Client:
        def __init__(self, *args):
            pass
        async def connect(self):
            pass
        async def send_opus(self, packet):
            if failure == "send_timeout":
                await asyncio.Future()
        async def recv(self):
            if failure == "overflow":
                return b"\x01large-burst"
            await asyncio.Future()
        async def close(self):
            closed.append("client")
    class Codec:
        def phone_to_opus(self, pcm):
            return [b"opus"]
        def opus_to_phone(self, packet):
            return b"\0\0" * 3201
        def close(self):
            closed.append("codec")
    before = set(asyncio.all_tasks())
    result = await evaluation.replay({"pcm": b"\0\0" * 8000, "duration": 1}, "not-connected", tmp_path,
                                     client_factory=Client, codec_factory=Codec)
    assert result["status"] == "failed" and closed == ["client", "codec"]
    assert result["outputOverflows"] == (1 if failure == "overflow" else 0)
    assert result["outputQueueMs"] == 0 and not (set(asyncio.all_tasks()) - before)
