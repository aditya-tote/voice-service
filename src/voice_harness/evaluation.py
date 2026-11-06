"""English-only, isolated PersonaPlex replay. Default: no network/native imports.

Never loads service configuration, .env, CallService, or a database. Synthetic
fixtures test transport only. No automatic quality pass or production promotion.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import random
import re
import struct
import sys
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .media_timeline import Timeline
from .media_timing import Deadline, PlaybackFIFO, SampleFIFO
from .models import LeadContext
from .personaplex import budgeted_prompt_url, build_outbound_prompt, validate_model_budget
from .vih_protocol import VihOutboundAudio, decode_mulaw, mulaw_payload

VOICES = ("NATF2.pt", "NATM1.pt")
VARIANTS = ("baseline",)  # concise_v1 deliberately unavailable: no weakened business policy.
MAX_WAV_BYTES = 9_601_024
MODEL_LOCK_DIRECTORY = Path(__file__).resolve().parents[2] / "artifacts" / "personaplex-eval"
RUBRIC = ("opening_flow", "facts_names_numbers", "no_fabricated_actions", "opt_out",
          "corrections", "interruption_recovery", "backchannels_takeover", "intelligibility",
          "naturalness", "relevance", "manually_annotated_speech_latency_ms")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_directory(path: Path):
    # Refuse symlink components, including existing parents. Windows needs operator ACLs.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("unsafe artifact directory")
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("artifact directory must be private")


def private_file(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def write_json(path: Path, value):
    with private_file(path) as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False).encode())


def generate_fixtures(output: Path):
    private_directory(output)
    rng = random.Random(20261106)  # Fixture noise only, not a model seed.
    for name in ("silence", "tone", "noise"):
        values = []
        for sample in range(8 * 8000):
            active = 8000 <= sample < 2 * 8000
            value = (int(2000 * math.sin(2 * math.pi * 440 * sample / 8000)) if name == "tone"
                     else rng.randint(-1500, 1500) if name == "noise" else 0) if active else 0
            values.append(value)
        with private_file(output / f"{name}.wav") as stream, wave.open(stream, "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            wav.writeframes(struct.pack(f"<{len(values)}h", *values))


def loopback_url(value: str):
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(char in value for char in "?#"):
        raise ValueError("evaluation requires a clean loopback model endpoint")
    split = urlsplit(value)
    if (split.scheme not in {"ws", "wss"} or split.hostname not in {"localhost", "127.0.0.1", "::1"}
            or split.username is not None or split.password is not None or split.query or split.fragment):
        raise ValueError("evaluation requires a clean loopback model endpoint")
    _ = split.port  # Validate malformed ports before any connection.


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest field")
        result[key] = value
    return result


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("invalid manifest fields")


def reject_symlinks(path: Path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("unsafe fixture path")


def read_manifest(path: Path, fixture_root: Path):
    if path.stat().st_size > 262144:
        raise ValueError("manifest too large")
    raw = path.read_bytes()
    manifest = json.loads(raw, object_pairs_hook=strict_object)
    fields(manifest, {"version", "cases"})
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("invalid manifest version")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("invalid case count")
    reject_symlinks(fixture_root.absolute())
    root = fixture_root.resolve(strict=True)
    result, ids = [], set()
    for case in cases:
        fields(case, {"id", "kind", "language", "wav", "trailingSilenceSeconds", "preparedAt", "leadContext"},
               {"consentConfirmed", "intervals"})
        if not isinstance(case["id"], str) or not re.fullmatch(r"[a-z0-9_-]{1,48}", case["id"]):
            raise ValueError("invalid case ID")
        if case["id"] in ids:
            raise ValueError("duplicate case ID")
        ids.add(case["id"])
        if case.get("kind") not in {"transport_only", "consented_english_speech"}:
            raise ValueError("invalid fixture kind")
        if "consentConfirmed" in case and type(case["consentConfirmed"]) is not bool:
            raise ValueError("invalid consent flag")
        if case.get("language") != "en" or (case["kind"] == "consented_english_speech"
                                            and case.get("consentConfirmed") is not True):
            raise ValueError("English consented speech required")
        if not isinstance(case["wav"], str) or any(char in case["wav"] for char in "\\:"):
            raise ValueError("unsafe fixture path")
        relative = PurePosixPath(case["wav"])
        if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".wav":
            raise ValueError("unsafe fixture path")
        reject_symlinks(root / relative)
        audio_path = (root / relative).resolve(strict=True)
        if not audio_path.is_relative_to(root) or audio_path.stat().st_size > MAX_WAV_BYTES:
            raise ValueError("unsafe or oversized fixture")
        audio_raw = audio_path.read_bytes()
        with wave.open(str(audio_path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 8000, "NONE"):
                raise ValueError("fixture must be mono 8 kHz PCM16 WAV")
            count = wav.getnframes()
            if not 1 <= count <= 600 * 8000:
                raise ValueError("fixture duration out of range")
            pcm = wav.readframes(count)
            if len(pcm) != count * 2:
                raise ValueError("truncated WAV")
        duration = count / 8000
        trailing = case.get("trailingSilenceSeconds")
        if type(trailing) not in {int, float} or not math.isfinite(trailing) or not 5 <= trailing < duration:
            raise ValueError("invalid trailing silence")
        if any(pcm[-int(trailing * 8000) * 2:]):
            raise ValueError("trailing silence must be PCM zero")
        intervals = case.get("intervals", [])
        if not isinstance(intervals, list) or len(intervals) > 100:
            raise ValueError("invalid intervals")
        previous = 0
        for interval in intervals:
            fields(interval, {"kind", "start", "end"})
            if interval.get("kind") not in {"activity", "backchannel", "interruption", "noise", "echo"}:
                raise ValueError("invalid interval kind")
            start, end = interval.get("start"), interval.get("end")
            if (any(type(value) not in {int, float} or not math.isfinite(value) for value in (start, end))
                    or not previous <= start < end <= duration - trailing):
                raise ValueError("invalid interval range/order")
            previous = end
        instant = datetime.fromisoformat(case["preparedAt"])
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("timezone-aware fixed clock required")
        facts = LeadContext.model_validate(case["leadContext"])
        serialized = facts.model_dump_json()
        if any(marker in serialized.upper() for marker in ("TODO", "REPLACE_ME", "<PLACEHOLDER>", "[NAME]")):
            raise ValueError("unresolved facts placeholder")
        result.append({"id": case["id"], "kind": case["kind"], "pcm": pcm, "facts": facts,
                       "instant": instant, "duration": duration, "audioHash": digest(audio_raw),
                       "factsHash": digest(serialized.encode()), "intervals": intervals})
    return digest(raw), result


def versions():
    result = {"python": sys.version.split()[0]}
    for name in ("voice-agent-harness", "websockets", "numpy", "soxr", "sphn"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "not_installed"
    return result


async def replay(case, url, output: Path, *, client_factory=None, codec_factory=None):
    """Real-time duplex loopback, shared codec/FIFOs/deadlines, no provider connection."""
    if client_factory is None:
        from .personaplex import PersonaPlexClient
        # A loopback server redirect must never turn explicit local consent into remote access.
        client_factory = lambda endpoint, timeout: PersonaPlexClient(endpoint, timeout, allow_redirects=False)
    if codec_factory is None:
        from .audio import StreamingCodec
        codec_factory = StreamingCodec
    client, codec, tasks = client_factory(url, 45), None, []
    fifo, playback = SampleFIFO(1000), PlaybackFIFO(400)
    outbound = VihOutboundAudio("evaluation-local")
    wake = asyncio.Event()
    text, text_size = [], 0
    input_clock, output_clock = Deadline(0), Deadline(0)
    started = time.monotonic()
    ready = first_audio = None
    sent_samples = clipping = silent = sum_squares = 0
    status = "failed"
    timeline = wav_stream = wav = None

    def stamp(event, samples=0, byte_offset=0):
        timeline.write(event, (time.monotonic() - started) * 1000, samples, byte_offset, playback.epoch)

    async def input_writer():
        pcm = case["pcm"]
        for offset in range(0, len(pcm), 320):
            await asyncio.sleep(input_clock.delay(time.monotonic()))
            input_clock.advance(time.monotonic())
            fifo.append(pcm[offset:offset + 320])
            frame, real = fifo.tick()
            stamp("model_input", real, offset)
            for packet in codec.phone_to_opus(frame):
                await asyncio.wait_for(client.send_opus(packet), 10)
        await asyncio.sleep(input_clock.delay(time.monotonic()))

    async def reader():
        nonlocal text_size, first_audio
        while True:
            frame = await asyncio.wait_for(client.recv(), 60)
            if not isinstance(frame, bytes) or len(frame) < 2:
                raise ValueError("invalid model frame")
            if frame[0] == 2:
                piece = frame[1:].decode("utf-8")
                text_size += len(piece)
                if text_size > 32768:
                    raise ValueError("agent text limit")
                text.append(piece)
            elif frame[0] == 1:
                pcm = codec.opus_to_phone(frame[1:])
                if len(pcm) > 80000:
                    raise ValueError("model frame limit")
                if pcm and first_audio is None:
                    first_audio = time.monotonic()
                stamp("model_audio", len(pcm) // 2)
                playback.check(len(pcm) // 2, len(outbound.pending))
                playback.append(outbound.encode(pcm), time.monotonic())
                wake.set()
            else:
                raise ValueError("invalid model frame")

    async def writer():
        nonlocal sent_samples, clipping, silent, sum_squares
        while True:
            if not playback.frames:
                wake.clear()
                await wake.wait()
                output_clock.next = max(output_clock.next, time.monotonic())
                continue
            frame = playback.pop(time.monotonic())
            await asyncio.sleep(output_clock.delay(time.monotonic()))
            output_clock.advance(time.monotonic())
            data = mulaw_payload(json.loads(frame.message)["media"]["payload"])
            pcm = decode_mulaw(data)
            wav.writeframesraw(pcm)
            stamp("provider_send", 160, sent_samples * 2)
            sent_samples += 160
            for (value,) in struct.iter_unpack("<h", pcm):
                clipping += abs(value) >= 32000
                silent += abs(value) < 100
                sum_squares += value * value
            playback.complete(frame, time.monotonic())

    try:
        timeline = Timeline(output / "timeline.jsonl")
        wav_stream = private_file(output / "agent.wav")
        wav = wave.open(wav_stream, "wb")  # noqa: SIM115 - closed in cancellation-safe finally below
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        async with asyncio.timeout(case["duration"] + 60):
            await client.connect()
            ready = time.monotonic()
            codec = codec_factory()
            input_clock.next = ready + 0.060
            output_clock.next = ready
            tasks = [asyncio.create_task(input_writer()), asyncio.create_task(reader()), asyncio.create_task(writer())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[0] not in done:
                raise ValueError("model replay ended early")
            status = "completed_unrated"
    except Exception:  # noqa: BLE001 - model exceptions can contain private prompt URLs
        # Do not serialize exceptions: client errors may contain conditioning URLs.
        status = "failed"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if timeline:
            try:
                stamp("output_discarded", playback.samples + len(outbound.pending))
            except Exception:  # noqa: BLE001 - private artifact failure is not successful evidence
                status = "failed"
        playback.clear(len(outbound.pending), termination=True)
        try:
            await asyncio.wait_for(client.close(), 5)
        except Exception:  # noqa: BLE001 - close failures cannot be successful model evidence
            status = "failed"
        if codec:
            try:
                codec.close()
            except Exception:  # noqa: BLE001 - attempt all remaining file cleanup too
                status = "failed"
        for resource in (wav, wav_stream, timeline):
            if resource:
                try:
                    resource.close()
                except Exception:  # noqa: BLE001 - close every resource; serialize no private errors
                    status = "failed"
        write_json(output / "agent-text.json", {"kind": "generated_agent_text_not_caller_asr", "text": "".join(text)})
    return {"status": status, "connectToReadyMs": (ready - started) * 1000 if ready else None,
            "readyToFirstGeneratedAudioMs": (first_audio - ready) * 1000 if first_audio else None,
            "sentAudioMs": sent_samples / 8, **fifo.stats(), **playback.stats(time.monotonic()),
            "inputMissedTicks": input_clock.missed, "outputMissedTicks": output_clock.missed,
            "timelineTruncated": bool(timeline and timeline.truncated),
            "outputRmsPcm16": math.sqrt(sum_squares / sent_samples) if sent_samples else None,
            "outputClippingFraction": clipping / sent_samples if sent_samples else None,
            "outputNearSilenceFraction": silent / sent_samples if sent_samples else None}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes rejected values, which may contain a private URL.
        self.exit(2, "Evaluation arguments rejected; use --help for supported options.\n")


def parser():
    result = SafeArgumentParser(description=__doc__)
    result.add_argument("--generate-fixtures", action="store_true")
    result.add_argument("--manifest", type=Path)
    result.add_argument("--fixture-root", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--variants", nargs="+", choices=VARIANTS, default=["baseline"])
    result.add_argument("--voices", nargs="+", choices=VOICES, default=["NATF2.pt"])
    result.add_argument("--repeats", type=int, default=3)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--run-model", action="store_true")
    result.add_argument("--confirm-model-idle", action="store_true")
    result.add_argument("--personaplex-url", default="ws://127.0.0.1:8998/api/chat")
    result.add_argument("--model-label")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    lock = None
    try:
        if args.run_model != args.confirm_model_idle or (args.run_model and args.dry_run):
            raise ValueError("model execution requires both flags and no dry-run")
        loopback_url(args.personaplex_url)
        if args.run_model and (not args.model_label or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", args.model_label)):
            raise ValueError("operator deployment label required")
        if args.generate_fixtures:
            if args.run_model or args.manifest:
                raise ValueError("fixture generation is separate from replay")
            generate_fixtures(args.output)
            print(json.dumps({"status": "generated_transport_only", "count": 3}))
            return 0
        if not args.manifest or not args.fixture_root:
            raise ValueError("manifest and fixture root required")
        manifest_hash, cases = read_manifest(args.manifest, args.fixture_root)
        if not 1 <= args.repeats <= 20 or len(cases) * len(args.voices) * len(args.variants) * args.repeats > 100:
            raise ValueError("evaluation matrix limit")
        if len(args.voices) != len(set(args.voices)) or len(args.variants) != len(set(args.variants)):
            raise ValueError("duplicate evaluation matrix entry")
        private_directory(args.output)
        if args.run_model:
            lock_dir = MODEL_LOCK_DIRECTORY
            private_directory(lock_dir)
            lock = lock_dir / ".model.lock"
            try:
                lock_stream = private_file(lock)
            except FileExistsError:
                lock = None  # Never remove another run's lock.
                raise ValueError("another evaluation owns the model lock") from None
            lock_stream.close()
        summaries = []
        halted = False
        runtime = versions()
        for case in cases:
            if halted:
                break
            prompt = build_outbound_prompt(case["facts"], now=case["instant"])
            for voice in args.voices:
                if halted:
                    break
                url = budgeted_prompt_url(args.personaplex_url, prompt, voice)
                url_bytes, line_bytes = validate_model_budget(prompt, url)
                for repeat in range(args.repeats):
                    if halted:
                        break
                    run_id = uuid.uuid4().hex
                    directory = args.output / run_id
                    private_directory(directory)
                    summary = {"version": 1, "runId": run_id, "caseId": case["id"], "fixtureKind": case["kind"],
                               "manifestHash": manifest_hash, "audioHash": case["audioHash"],
                               "factsHash": case["factsHash"], "promptHash": digest(prompt.encode()),
                               "preparedAt": case["instant"].isoformat(), "variant": "baseline", "voice": voice,
                               "repeatIndex": repeat, "modelLabel": args.model_label if args.run_model else "not_run",
                               "versions": runtime, "format": "mono_PCM16_8000Hz_telephone_path",
                               "promptCharacters": len(prompt), "urlBytes": url_bytes, "requestLineBytes": line_bytes,
                               "requestedSeed": "not_set", "requestedSampling": "not_set",
                                "effectiveSampling": "unknown_stock_server", "status": "dry_run_not_evaluated",
                                "annotationIntervals": case["intervals"],
                                "humanScores": dict.fromkeys(RUBRIC),
                                "qualityStatus": ("not_evaluated_transport_only" if case["kind"] == "transport_only"
                                                  else "unrated")}
                    if args.run_model:
                        summary.update(asyncio.run(replay(case, url, directory)))
                    write_json(directory / "summary.json", summary)
                    summaries.append(summary)
                    # A failed close may leave the single model slot uncertain. Never
                    # automatically connect the next repeat; require new idle confirmation.
                    halted = args.run_model and summary["status"] == "failed"
        write_json(args.output / f"index-{uuid.uuid4().hex}.json", summaries)
        print(json.dumps({"status": "model_runs_unrated" if args.run_model else "dry_run_not_evaluated",
                          "runs": len(summaries), "failed": sum(row["status"] == "failed" for row in summaries),
                          "concise_v1": "unavailable_policy_preservation_deferred"}))
        return 1 if any(row["status"] == "failed" for row in summaries) else 0
    except Exception:  # noqa: BLE001 - never print input paths, facts, URLs or exception strings
        print("Evaluation rejected or failed; check manifest, private paths, limits and explicit model consent.", file=sys.stderr)
        return 2
    finally:
        if lock:
            lock.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
