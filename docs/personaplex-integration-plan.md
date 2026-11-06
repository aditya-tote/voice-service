# PersonaPlex telephone integration: implementation handoff

Status: **planning only**, 2026-11-06. No application implementation, inference, live call, dependency installation, credential inspection or database access was performed in this stage. The repository was initially clean. No applicable `AGENTS.md` was found in the repository, parent workspace, or ancestor directories.

## 1. Objective and non-negotiable boundaries

Implement a small, testable telephone transport improvement and a repeatable **English-only** quality-evaluation workflow. Preserve telephone time/silence, pace playback, measure buffering, and offer conservative **opt-in** clearing of queued playback during sustained caller activity. Do not replace PersonaPlex's learned turn-taking with a business dialogue controller.

Research context: [workspace research note](../../docs/research/personaplex-prompting-architecture.md), [NVIDIA project](https://research.nvidia.com/labs/adlr/personaplex/), [exact paper](https://research.nvidia.com/labs/adlr/files/personaplex/personaplex_preprint.pdf). Reviewed upstream source commit: `3428dfd95309a7f3c84fd93259ded0f810d1ff91`; the deployed checkpoint/fork still needs operator identification.

Keep these facts explicit in code/doc comments and evaluation reports:

- Native input/output is simultaneous speech-to-speech, with 24 kHz mono audio transported as streaming Opus. Generated kind-2 text is **agent text**, not caller ASR.
- Conversation does not require added ASR or speech-gating VAD. Silence and overlap must reach the model. An optional energy heuristic controls **telephone playback only**, not inference or business actions.
- Voice and text conditioning precede the ready handshake. Preserve the ready-before-dial and same-WebSocket handoff. Never reset/reconnect the model per turn or interruption.
- Stock server has no general live system-update/tool-result interface. Reviewed temperature/top-k query assignments are commented out. Do not introduce fictitious controls.
- Shorter prompts are an A/B hypothesis, not a proven improvement. Keep the current production prompt **byte-for-byte unchanged for the same inputs and clock** by default.
- Published latency is not a deployment SLO. Silence-only probe success is not business compliance.

Out of scope: Hindi/Hinglish support, fine-tuning, ASR, semantic interruption classification, echo cancellation, CRM/calendar/email, opt-out persistence, real hangup APIs, a dialogue state machine, upstream server patches, new infrastructure services, new externally exposed routes, and arbitrary per-call prompt/voice overrides.

Preserve auth/token consumption, safe logging, request/body/transport limits, single-GPU admission, durable idempotency/replay, pre-dial preparation, provider-stop authority, uncertain-call recovery/no-redial, existing CDR correlation, `customerTranscript: null`, and every outbound policy requirement. The facts-only request contract stays unchanged.

## 2. Verified local starting point

| File | Relevant current behavior |
| --- | --- |
| `src/voice_harness/personaplex.py` | `OUTBOUND_POLICY`, quoted rendered opening, approved product explanation, IST preparation clock and JSON lead facts. Prompt `<7000` characters; exact encoded URL **and** origin-form GET line `<8190` bytes. `model_connection_url` is active; legacy `connection_url` serves the neutral probe. |
| `src/voice_harness/call_service.py` | Validates exact URL before saving/admission/dial. Live preparation completes before ViH dial; prepared socket reused. Already logs preparation-to-ready duration. |
| `src/voice_harness/vih_media.py` | Independent provider-receive and model-receive tasks, but each also sends directly to its destination. No input clock or output pacing. Marks per decoded bundle; ack count only. `clear` only on failure cleanup. Optional raw per-direction recordings have no common timeline. |
| `src/voice_harness/vih_protocol.py` | Strict sequence/stream/account validation; no trusted media timestamp contract. Stateful G.711 output residual produces 160-byte/20 ms packets. `clear()` discards that residual. |
| `src/voice_harness/audio.py` | Stateful soxr 8k↔24k and sphn Opus; 960-sample/40 ms writer chunks. Model frame is 80 ms, which is a different boundary. |
| `src/voice_harness/observability.py` | Explicit event/field allowlists: adding a field at a call site alone will silently omit it. |
| `tests/test_vih_media.py` | Full service/media/repository path using model/codec/socket doubles and temporary databases. Extend rather than bypass this path. |

Parent-reported baseline (not rerun by this planning agent): `python -m pytest -q` → **211 passed, 4 skipped, 2 warnings**; `python -m ruff check .` passed. Native codec/WebSocket checks may skip without runtime dependencies. Existing root `docs/api/voice-service.verification.json` is mock evidence only; do not relabel it live proof.

## 3. Architecture and defaults

Use one small transport module, `src/voice_harness/media_timing.py`, with clock-injectable primitives. No generic event bus, plugin system, worker service, or extra audio dependency is needed. Keep provider JSON framing in `vih_protocol.py` and native codecs in `audio.py`.

```text
provider reader -> bounded PCM FIFO -> 20 ms input clock -> existing codec -> model
                          | caller energy observation (optional; no ASR)
model reader -> existing decoder -> bounded 20 ms playback FIFO -> paced provider writer
                          |                      ^
                          +----------------------| clear/epoch invalidation
checkpoint task -> bounded stats + optional private timeline metadata
```

Proposed settings (service-owned, no API fields):

| Environment variable | Default / validation | Purpose |
| --- | --- | --- |
| `MEDIA_INPUT_STARTUP_MS` | `60`; integer 0–200, multiple of 20 | Small, fixed local jitter allowance before input clock starts. |
| `MEDIA_INPUT_MAX_BUFFER_MS` | `1000`; integer 100–2000, multiple of 20, greater than startup | Bounded caller PCM buffer. Overflow fails the bridge; do not silently drop speech or stretch time. |
| `MEDIA_OUTPUT_MAX_BUFFER_MS` | `400`; integer 100–2000, multiple of 20 | Bounded generated-audio playback buffer. Include queued/residual duration when checking; overflow fails safely rather than dropping arbitrary words. |
| `MEDIA_INTERRUPTION_MODE` | `off`; enum `off` / `sustained_energy` | Additional playback clearing disabled unless explicitly enabled after listening tests. |
| `MEDIA_INTERRUPTION_RMS` | `1000`; finite number 100–12000 PCM16 units | Experiment starting point, **not** a speech/echo classifier. |
| `MEDIA_INTERRUPTION_SUSTAIN_MS` | `400`; integer 300–1500, multiple of 20 | Require sustained energy, not one loud packet. |
| `MEDIA_INTERRUPTION_RELEASE_MS` | `300`; integer 100–1000, multiple of 20 | Quiet period to end playback suppression and rearm. |

Keep 20 ms frame size, 200 ms mark cadence, 40 ms allowed below-threshold gap during detection, and 1000 ms clear cooldown as named constants rather than more configuration knobs. Reject NaN/infinity, malformed values, and invalid cross-field combinations at startup. No flag turns off safety limits. New settings affect the ViH bridge only; do not accidentally change legacy helpers.

### 3.1 Input timeline contract

1. Start local input timeline at validated provider `start`, after model readiness. The first tick is due at `start + startup_ms`; each tick represents exactly 160 PCM16 samples at 8 kHz. Preserve initial silence even if no media arrives.
2. The provider reader validates/records events promptly and appends decoded PCM to a **sample-count-bounded** FIFO. Accept variable payload sizes, concatenate/split on sample boundaries, and never gate on energy. A transport sequence gap is only a diagnostic: non-media events also consume sequence numbers, so it is **not** a silence duration.
3. At each tick consume up to 160 available real samples; zero-pad the shortage. Feed that complete frame through the existing stateful codec and send every resulting Opus packet. Do this while the agent is speaking and during caller pauses. Never flush/recreate the codec per tick/turn.
4. Use absolute monotonic deadlines, not `sleep(0.02)` added after work. Never catch up with a burst of elapsed frames. For a missed scheduler deadline of one frame or more, count missed time and rebase the next deadline to `now + 20 ms`; do not fast-forward inference. Expose this degraded condition. Test the policy explicitly; it cannot make an overloaded VM real-time.
5. Missing audio is conservatively represented as synthetic silence. Without provider capture timestamps, late real payloads cannot be placed exactly at their historical sample position. Do not claim packet-loss reconstruction or exact telephone capture timing. Keep FIFO order, bound backlog, and report arrival-clock timing quality. Do not add undocumented timestamp parsing in this revision.
6. Retain provider-reader idle timeout independently. Synthetic silence must not hide a dead provider, reset inactivity deadlines, or keep a stopped call alive. All producer/writer waits remain bounded and cancellable.

### 3.2 Output pacing and queue bounds

1. Model reader continues receiving agent text/audio independently of provider playback. Decode every Opus packet with the existing stateful decoder. Queue decoded PCM/aligned G.711 frames without awaiting playback sleeps. Bound queue duration before admission, including any residual fragment, and fail with a fixed safe error if exceeded.
2. Send 160-byte G.711 packets no faster than one every 20 ms using absolute monotonic deadlines. The first available packet may send immediately; after an idle gap restart the schedule, never burst to catch up. On event-loop stalls, rebase and record lateness/missed ticks rather than emitting many packets together.
3. Provider reader, input clock, model reader and playback writer all continue independently. A slow output send must not stop inbound caller audio until a documented bounded queue/time limit makes the bridge fail. Do not replace a queue with an unbounded list or block the provider reader behind output backpressure.
4. Serialize media, marks and clear on the existing send lock or an equivalent single-writer mechanism. Use a playback generation/epoch so a writer holding a previously dequeued frame cannot send stale audio **after** clear. A send already in flight may precede clear; it cannot be retracted locally. Recheck `protocol.stopped` after acquiring the lock.
5. On validated `stop`, cancel all new tasks, discard both FIFOs and output residual, send no further media/marks/clear, close resources, and preserve provider-stop authority. Failure/shutdown keeps existing clear-on-cleanup and recovery lease behavior. Never pad/flush residual output at termination.

### 3.3 Mark tracking and metrics (do not overclaim playback)

Send a bounded, service-generated mark approximately every 200 ms of transmitted audio. Track local send time, cumulative sent samples and epoch in a ledger capped at 64 entries. Missing acks must not grow memory or deadlock media; evict oldest metadata and count unconfirmed/evicted marks. Provider protocol's existing total ack counter remains compatible.

Match acks to known marks; count unknown, duplicate and invalidated-epoch acks without failing the call. On clear, invalidate outstanding marks and reset the current-epoch queued estimate. Do not interpret late cleared marks as proof audio played. Provider-specific mark semantics must be confirmed before claiming an audible playback boundary.

Persist additive, finite, bounded numeric fields in existing `streamStats`; no SQLite migration or new API endpoint. At minimum include:

- Input real samples received/sent, synthetic-silence samples, FIFO current/high-water duration, scheduler late/missed ticks, maximum lateness, input timing basis (`arrival_clock`).
- Generated audio duration, sent duration, local output queue current/high-water duration, oldest queued age/max age, pacing lateness, discarded duration by clear/suppression/termination, overflow count.
- Marks sent/matched/unknown/invalidated/evicted, last/max ack round-trip, outstanding sent audio duration estimate. Label estimates as **provider acknowledgement estimates**, not measured speaker latency.
- Interruption candidates/confirmed/clear count, clear send latency from heuristic confirmation, suppressed output duration, and active mode. No caller transcript or semantic label.
- First input/output offsets relative to stream start. Connection-to-ready is already separate in `CallService`; retain it. Speech-end→speech-onset and caller-interruption→audible-stop require annotated audio/listening and are not derivable exactly from these counters.

Extend `observability.py` allowlists with explicit safe fields/events/stages/error codes only. Log periodic aggregates, not per-packet payloads or raw mark names. Test the formatter, not just `caplog` raw attributes. Never log prompt text, full URL/query, agent text, caller facts, audio or exception strings.

### 3.4 Optional sustained-energy playback interruption

This is a bounded heuristic, **not detection of a genuine semantic interruption**. Default `off` means native model behavior plus paced playback only. Do not clear on every media packet, agent text, DTMF, a single threshold crossing, or ordinary short backchannels.

- Observe real caller PCM at 20 ms consumption boundaries. Synthetic silence releases/resets activity; it does not count as speech. Measure RMS with stdlib integer/float operations (no VAD/ASR dependency); avoid DC-offset false positives by centering samples. Detection time is local consumed-audio time, not trusted capture time.
- Candidate requires RMS threshold for the configured sustained duration, tolerating at most a total 40 ms quiet gap within that candidate. Reset when the allowed gap is exceeded. Require pending/in-flight/recently sent agent audio; idle caller speech does not clear anything. Do not rely on missing acks to declare agent audio active forever; recent-audio activity is bounded by local sent/queued duration estimates.
- On confirmation, atomically increment output epoch, discard queued PCM/G.711 and `VihOutboundAudio.pending`, invalidate mark ledger, and issue exactly one prioritized provider `clear`. Keep caller→model audio flowing. Do **not** reconnect/reset PersonaPlex, reset Opus/soxr, or inject a new prompt.
- Continue decoding subsequent model audio to preserve codec state, but discard output until the caller is quiet for release_ms. This prevents the old model tail immediately refilling telephony's queue. No maximum-duration business mute; suppression lasts only while measured activity persists, bounded by existing call/idle limits. On release start with an empty residual and reset playback pacing.
- Rearm only after release and cooldown. The clear control path cannot be blocked by a full audio FIFO. Fail clear send using existing bounded provider-send failure handling, not silent success.
- Known tradeoff: long “uh-huh”, noise, speaker echo, and loud music can cross this threshold; quiet/short urgent corrections can be missed. No PCM-energy method proves caller intent. Defaults are calibration hypotheses; leave disabled for production until consented headset/phone tests establish acceptable false clears and missed interruptions. Do not advertise acoustic echo cancellation.

### 3.5 Private timeline evidence

Retain existing optional raw `.ulaw` recordings and byte-count semantics. When `RECORDING_DIR` is nonempty, add one private `<call_id>-timeline.jsonl` sidecar and additive recording reference. No recording/sidecar when disabled. Use exclusive creation, existing call-ID validation, POSIX 0600 / private directory, and document Windows ACLs.

Sidecar contains only schema/version, relative monotonic offsets, direction/event enum, raw recording byte offset/sample count, local epoch, and events for synthetic-silence ranges, model audio arrival, provider send, clear and mark match. No prompt, transcript, credentials, raw event JSON or arbitrary provider strings. Record caller **arrival** separately from model-input timeline; record agent **sent** audio, not imagined playback. Keep existing caller and agent recordings concatenated; alignment is in metadata, not padding these historical files.

Bound sidecar rows/size (hard cap 16 MiB per call and bounded row schema); on metadata cap, stop sidecar collection, set `timelineTruncated: true` and keep safe aggregate stats. Normal file-open/write errors follow existing recording failure behavior. Flush/checkpoint and close during cleanup; test partial creation and failure paths. Sidecars are private diagnostic artifacts, not a public result dump. No automatic upload or retention daemon.

## 4. Reproducible prompt / voice / telephone-audio evaluation

Implement `src/voice_harness/evaluation.py` and the module CLI `python -m voice_harness.evaluation`. It must **not** instantiate `CallService`, open the configured database, import/load `.env`, dial ViH, or require production bearer/provider credentials. Default behavior is fixture generation / validation / dry run only. Actual loopback model access requires both `--run-model` and `--confirm-model-idle`; refuse non-loopback endpoints and overlapping runs. These commands are proposed interfaces to implement, not existing functionality.

### Evaluation inputs and ownership

- Check in a small text manifest plus generator in `examples/personaplex-eval/`. Generate synthetic 8 kHz mono PCM16 WAV silence/tone/noise fixtures locally; mark them **transport-only**, never speech-quality evidence. Do not ship real callers, copyrighted speech or binary audio blobs.
- A manifest scenario accepts an operator-supplied, consented English caller WAV, strict sample format/duration, synthetic lead facts, fixed timezone-aware preparation instant, annotated caller activity/interruption/backchannel intervals and a required trailing-silence duration. Relative paths stay within an explicitly supplied fixture root; reject traversal, oversized files, malformed WAV, invalid intervals, inconsistent audio length and unresolved placeholders. Bound total call duration to 600 seconds per case and repeat/matrix size (e.g. at most 100 runs).
- Supply a human recording script/checklist for initial silence/hello, available flow, busy callback, details-now (not meeting consent), declined offer vs firm opt-out/denied enquiry, agreed future session, unknown facts, name/number correction, Indian English intelligibility, hesitation/long pause, short acknowledgement, sustained interruption, echo/noise, and a longer multi-turn call. One fixture can cover several rubric items; no fake proof from generated tones. Fixed replay is non-adaptive; interactive tests remain necessary.
- `baseline` calls the existing `build_outbound_prompt` with fixed facts/time. An evaluation-only `concise_v1` is a repository-owned template preserving **all** opening/WAIT/pitch/order, busy, offer/decline/firm-refusal, qualification/known-false-zero skipping, facts-as-data, AI-truthfulness, details-now, clock/IST, no-tools/actions and closing requirements. Refactor only enough to share trusted opening/facts/clock rendering without altering baseline output; test exact baseline equality. If that cannot be done confidently, ship baseline evaluation first and report candidate explicitly unavailable rather than weakening policy.
- No production `PROMPT_VARIANT`, arbitrary `--prompt-file`, API `script`, voice override, or model-result injection. Evaluation variants selected from a fixed enum; evaluation voices from a checked-in safe `.pt` filename allowlist (start `NATF2.pt`, `NATM1.pt`). Allowlist is distinct from proving the files are installed; operator verifies stock-server assets. Do not add temperature/top-k URL knobs.
- Assemble exact candidate/voice URL through a shared budget-checked helper. Both variants must satisfy `<7000` characters and `<8190` URL/GET bytes, including Unicode/percent expansion. Dry-run reports only counts, hashes and fixed variant IDs, never full prompt URLs. Production rejection still precedes save/admission/connection/dial and preserves canonical replay ordering.

### Execution and artifacts

- Replay caller WAV in real time through the same input timing/codec primitives; preserve initial/inter-utterance/trailing silence and concurrent model output. Include enough trailing silence (manifest minimum 5 seconds, usually 10+) to avoid truncating the answer. Bound each run by input duration plus readiness/close deadlines, and close on every exception/cancellation.
- Decode model output through the telephone path, use the same output scheduler for a local fake provider sink, and write private agent PCM16 WAV plus timestamp JSONL and generated **agent** text JSON. Include discarded/cleared frames in timing metadata but never claim they were audible. Manifest intervals can drive deterministic **test** interruption events; do not expose a live script controller.
- Write a private summary with manifest/audio hashes, fixed clock, facts/prompt hashes, variant, voice, repeat index, supplied operator model/checkpoint/server revision label, Python/runtime package versions, audio format, actual timing stats, clipping/RMS/silence fractions and run status. Log requested seed/sampling as `not_set` and effective sampling as `unknown_stock_server` unless independently verified; repeats provide repeatability metadata, not guaranteed deterministic inference.
- Separate connect→ready, ready→first generated audio, local scheduled playback, and manually annotated speech latency. Do not count silence bytes as spoken speech, equate raw receipt to playback, or use agent text as a caller transcript. Record missing manual scores as null/unrated, not zero or pass. Model output/prompt artifacts may contain PII and must remain private/ignored by git.
- Provide a CSV/JSON rubric template and blinded run IDs: exact opening/flow, factual accuracy/names/numbers, no fabricated price/tool/action, opt-out handling, correction handling, interruption recovery, premature takeover/backchannels, intelligibility/naturalness and response relevance. Use at least 3 repeats per voice/variant/case for an initial comparison; report per-case results, failures and human ratings, not one selected clip. Compare candidate to baseline with same inputs/voices; no automatic production promotion.

## 5. File-level implementation order

1. **`src/voice_harness/media_timing.py` (new), `tests/test_media_timing.py` (new):** bounded sample FIFOs, absolute clock/deadline behavior, output epochs/mark ledger and opt-in energy state machine. Inject monotonic clock/sleep or expose pure tick methods for deterministic tests. No FastAPI/native runtime imports in pure primitives.
2. **`src/voice_harness/config.py`, `.env.example`, `tests/test_core.py` / new focused config tests:** add settings above with defaults, parsing/ranges and finite checks. Edit example documentation only, never the user's `.env` or credentials.
3. **`src/voice_harness/vih_media.py`, `vih_protocol.py` only as needed:** split provider reading/input clock/model reading/playback writing, wire bounded failures/cancellation, keep authoritative stop and existing service lifecycle. Add optional timeline to recording helper or a small dedicated helper if this keeps the bridge comprehensible. Avoid unrelated repository/main/legacy changes.
4. **`src/voice_harness/observability.py`, `tests/test_logging.py`, `tests/test_provider_logging.py`:** allowlisted aggregates/fixed errors and redaction tests. New additive stats ride existing checkpoints/results; no repository schema migration.
5. **`tests/test_vih_media.py`, `tests/test_model_preparation.py`, `tests/test_security_regressions.py`, `tests/test_codec_runtime.py`:** integrate deterministic burst/gap/clear/stop/failure tests with real service/repository and fake sockets/models; native runtime continuity tests remain separate.
6. **`src/voice_harness/evaluation.py`, minimal budget/prompt helper in `personaplex.py` if necessary, `tests/test_evaluation.py` (new), `examples/personaplex-eval/` (new):** safe dry-run default, generated fixtures, opt-in replay, protected artifacts and rubric. Keep scope to one module unless a clearly shared primitive warrants extraction. Baseline production behavior and facts-only API untouched.
7. **`README.md`, `docs/personaplex-vm-runbook.md` (new), `.gitignore`:** document architecture, exact settings/commands, limitations, consent/retention, rollout and rollback; ignore generated recordings/evaluation artifacts. Correct README's existing “caller-VAD/model-reset policy” wording: model reset is **not** required/desirable here. Link this plan/runbook without rewriting unrelated docs.

Implementation agent should report any justified name/default changes against this plan. Independent testing/fixing agent must inspect the resulting diff and tests, not merely rerun the implementation agent's summary. Neither agent is authorized by this plan to access secrets, configured databases or live providers.

## 6. Acceptance tests and release gates

### Mandatory offline (CI/local, no provider/GPU)

- Input N 20 ms ticks produces exactly N×160 phone samples into the codec, including absent initial media, gaps and partial payloads. Real data order is preserved; zeros only fill shortages. Outbound speech never pauses input. Variable media sizes, max capacity boundary, sequence gaps with marks/DTMF, and missing provider timestamps are covered.
- Burst output is paced at ≥20 ms between local sends in a fake-clock test; an event-loop stall/idle gap never causes catch-up bursts. FIFO high-water/age and termination-discard stats are exact. Overflow is bounded, logged safely and retains uncertain-call lease.
- Disabled interruption never clears on caller activity. Enabled mode does not clear on sub-threshold noise, DC offset, brief backchannel or speech while output idle. Sustained tone triggers only as a heuristic; tests must not label it semantic speech. Test 40 ms gap tolerance, release/cooldown, quiet caller, duplicate detections and false-positive limitations.
- A confirmed event clears pending/residual/in-flight-dequeued epochs atomically; stale frames cannot be sent after clear. Decode continues while output is suppressed, caller input continues, model connects exactly once, and no codec/model reset is called. Rearm/resume starts empty. Clear works with a full output FIFO and fails safely on send timeout.
- Marks match monotonically within an epoch; unknown/duplicate/missing/evicted/late-postclear acks stay bounded and never become false playback proof. Do not need provider acks to keep input running.
- Valid stop races against queued media, clear and model failure: **no media/mark/clear after validated stop**, all tasks/resources finish, and stop remains authoritative. Disconnect, malformed frame, scheduler/queue failure, cancellation and file failure preserve existing recovery semantics. No background task survives teardown.
- Raw recording sizes/semantics remain compatible; disabled recording creates no files. Sidecar private/exclusive path behavior, offset accuracy, truncation cap, flush/checkpoint, partial creation cleanup and failure tests pass.
- Prompt baseline exact equality at fixed facts/time; all outbound policy and forbidden API override regressions pass. Candidate uses same facts/clock/opening constraints; budget edges include Unicode and exact encoded GET size. Budget rejection remains pre-save/pre-dial; replay still precedes rebuilding prompt/time.
- Evaluation dry-run imports no native runtime, connects nowhere, and opens no service database. Model run needs both explicit flags; rejects external/credential URLs, invalid voice/variant, unsafe fixture path/oversized WAV, bad intervals and invalid matrix sizes. Fake client/sink tests cover duplex, trailing silence, private output, hashes, unrated scores, failures and cleanup. Stdout/errors/summary logs do not expose secrets or URLs.
- Full existing pytest, Ruff, compile and mock CLI checks pass. Report exact skip reasons; mock/codec tests cannot establish conversational quality.

### Native-runtime / controlled VM gate (operator approval required)

- Install optional runtime in the **project venv** and run native codec and local WebSocket tests without silently skipping. Confirm 8k↔24k conversion, continuous Opus over silence→tone→silence, clipping metrics and bounded buffering. Do not change 40 ms codec chunking without measured evidence.
- Probe separately validates handshake/audio/text only. Evaluation must use the actual production baseline prompt as well as any candidate and identify deployed stock/fork/checkpoint. Do not run probe/evaluation while an admitted call owns the one model slot.
- Obtain consented English caller recordings and score blinded repeated prompt/voice runs. Treat missing fixtures/listeners/GPU as **not evaluated**, not pass. Manually annotate speech and actual telephone playback for end-to-end latencies; report distributions, measurement method and failures, not NVIDIA benchmark numbers as observed results.
- Controlled real telephone test is a separate operator action: echo/noise, initial silence, short backchannels, quiet corrections and sustained overlap; confirm ViH mark/clear behavior on that account. Enable sustained-energy only after false-clear/missed-clear review. No guaranteed numerical speech-quality/latency improvement is claimed by this change.

## 7. Commands for implementation and independent verification

From `voice-agent-harness`, without loading `.env` (these are the existing safe test interfaces; fixtures use temporary DBs):

```powershell
python -m pytest -q
python -m ruff check .
python -m compileall -q src tests
python scripts/check_mock_cli.py
```

The mock CLI starts **its own mock-only process with a temporary database**, never the configured live service. It currently inherits some environment settings; implementation/testing should sanitize provider-specific inherited values for that helper if needed rather than read production configuration or relax validation. Do not run `voice-harness` directly in this stage.

Focused commands after implementation:

```powershell
python -m pytest -q tests/test_media_timing.py tests/test_evaluation.py tests/test_vih_media.py tests/test_model_preparation.py tests/test_security_regressions.py
python -m voice_harness.evaluation --generate-fixtures --output artifacts/personaplex-eval/fixtures
python -m voice_harness.evaluation --manifest examples/personaplex-eval/manifest.json --fixture-root artifacts/personaplex-eval/fixtures --variants baseline concise_v1 --voices NATF2.pt NATM1.pt --repeats 3 --dry-run --output artifacts/personaplex-eval/dry-run
```

Generator and checked-in manifest must agree on filenames; generated transport-only cases cannot pass the speech-quality gate. Do not publish generated/private artifacts. If candidate is deferred, command must clearly report unavailable and documentation must show `--variants baseline` as the executable first gate.

### GPU VM runbook content to ship (commands are operator-only, not executed here)

Use Python 3.12+, one harness worker, an independently provisioned loopback PersonaPlex server/model, installed allowlisted voice assets, and existing confirmed ViH/Nginx configuration only when phone tests are separately approved. Do not install stock model GPU dependencies into the harness venv or assert that harness runtime extras install the server/checkpoint. Model weights/license/access token, checkpoint revision, GPU suitability, stock server launch and upstream dependency lock belong to the operator's separately validated model deployment; do not invent those values.

Safe isolated runtime test setup on Linux (no live service or model access):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[runtime,dev]'
.venv/bin/python -m pytest -q -rs
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q src tests
.venv/bin/python scripts/check_mock_cli.py
.venv/bin/python -m voice_harness.evaluation --generate-fixtures --output artifacts/personaplex-eval/fixtures
.venv/bin/python -m voice_harness.evaluation --manifest examples/personaplex-eval/manifest.json --fixture-root artifacts/personaplex-eval/fixtures --variants baseline concise_v1 --voices NATF2.pt NATM1.pt --repeats 3 --dry-run --output artifacts/personaplex-eval/dry-run
```

Use explicit environment only for probe; no need to source the credential file. Values below are non-production placeholders and **cannot dial**:

```bash
export CONTROL_HOST=127.0.0.1 MEDIA_HOST=127.0.0.1
export BRIDGE_API_KEY=probe-local-only-not-a-production-key-00000000
export VIH_DIAL_MODE=mock ENABLE_LIVE_DIALS=false
export PUBLIC_STREAM_BASE_URL=wss://bridge.example.com
export PERSONAPLEX_URL=ws://127.0.0.1:8998/api/chat
export ALLOWED_VOICES=NATF2.pt,NATM1.pt DEFAULT_VOICE=NATF2.pt
export MEDIA_INPUT_STARTUP_MS=60 MEDIA_INPUT_MAX_BUFFER_MS=1000
export MEDIA_OUTPUT_MAX_BUFFER_MS=400 MEDIA_INTERRUPTION_MODE=off
export MEDIA_INTERRUPTION_RMS=1000 MEDIA_INTERRUPTION_SUSTAIN_MS=400
export MEDIA_INTERRUPTION_RELEASE_MS=300
```

Only after confirming the model is idle, its deployment identity, consent/fixtures and explicit authorization for local model inference:

```bash
.venv/bin/voice-harness-probe NATF2.pt --timeout 45
.venv/bin/python -m voice_harness.evaluation --manifest examples/personaplex-eval/manifest.json --fixture-root artifacts/personaplex-eval/fixtures --variants baseline concise_v1 --voices NATF2.pt NATM1.pt --repeats 3 --personaplex-url ws://127.0.0.1:8998/api/chat --model-label OPERATOR_VERIFIED_CHECKPOINT_AND_SERVER_REVISION --run-model --confirm-model-idle --output artifacts/personaplex-eval/model-runs
```

The generator's synthetic fixtures are suitable for transport smoke checks only. Replace/select the manifest's documented consented speech cases before quality scoring. Do not claim a real model run from `--dry-run` or a fake model client. Service launch instructions must be separated from these safe commands: after draining/reconciling calls, the operator can use their existing protected environment/service unit and `.venv/bin/voice-harness` (one process). Do not copy the probe's fake key into a real deployment, change confirmed provider settings, or issue a sample real dial as an automated acceptance test. Any live-call invocation requires a separately authorized destination and stable idempotency key.

Rollout: complete offline checks → native runtime checks → baseline voice/prompt replay → controlled phone test with interruption **off** → optional heuristic A/B after calibration. Retain current production prompt/voice by default. Drain/reconcile before deployment/restart; never clear a lease to make an evaluation slot. Rollback is a drained deployment rollback; disabling `MEDIA_INTERRUPTION_MODE` restores native-only interruption handling without reconnecting active calls. Changing environment affects new bridge sessions only; no mid-call dynamic settings API is introduced.

## 8. Risks and required final handoff

1. **No trusted capture clock:** arrival-clock FIFO plus zeros preserves local inference cadence, not exact packet-loss reconstruction; late audio and scheduler stalls are observable limitations.
2. **False/missed energy interruption and echo:** opted out by default; longer backchannels/noise can still trigger. Require human/telephone validation; no semantic guarantee.
3. **Playback semantics:** mark ack timing may include network/clear behavior. Local sent audio and raw recordings are not proof of what the caller heard.
4. **Pacing/queue latency:** 60 ms startup and codec/network buffers add delay; 400 ms output cap can expose bursty/overloaded deployments as bridge failures. Report high-water/overflow data before adjusting bounded settings, never silently remove limits.
5. **Concurrency regression:** cancellation, stale frame after clear and stop/failure races need deterministic tests and real service-path coverage.
6. **Prompt compliance:** preserved instructions are not enforced speech. Candidate must not weaken any business rule, and cannot be promoted from transport tests or one attractive clip.
7. **Privacy/artifact growth:** recordings/agent text and even local evaluation evidence may contain personal information. Private files, caps, consent, retention and no commits/uploads are required.
8. **Unavailable runtime/model/fixtures:** report skipped/not-run checks precisely. No global installs, live calls, server startup, weight downloads, configuration credential changes or automatic provider access are authorized by this plan.

Implementation report must name changed files/settings/defaults, confirm unchanged facts-only contract/prompt default/safety semantics, give exact test outcomes and skip reasons, list runnable commands and unperformed model/phone gates, and describe remaining limitations. Independent testing/fixing stage must scrutinize timing assumptions, queue/clear/stop races, mark truthfulness, baseline policy fidelity, budget validation, file privacy and safe no-network defaults; fix within scope and rerun checks. Neither stage may label the integration production-quality or Hindi-capable without separate evidence.
