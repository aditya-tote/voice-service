# PersonaPlex VM setup and release gates (English only)

These commands do **not** install a model server or weights. Use Linux Python
3.12+, one harness worker, an independently provisioned loopback PersonaPlex
server, and operator-verified allowlisted voice assets. Identify the deployed
checkpoint, stock/fork server revision and GPU capacity separately; the harness
runtime extra is only its client/codec. Model access/license, GPU drivers, upstream
dependency lock and server launch belong to that separately validated deployment.
Do not invent sampling knobs: reviewed stock-server temperature/top-k query
assignments are commented out. Appendix A's released checkpoint differs from the
main paper's experiment; benchmark latency is not a telephone deployment SLO.

## 1. Isolated offline/native checks — no credentials or model access

From the checked-out `voice-agent-harness` directory (do not source `.env`):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[runtime,dev]'
.venv/bin/python -m pytest -q -rs
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q src tests
.venv/bin/python scripts/check_mock_cli.py
umask 077
.venv/bin/python -m voice_harness.evaluation --generate-fixtures --output artifacts/personaplex-eval/fixtures
.venv/bin/python -m voice_harness.evaluation --manifest examples/personaplex-eval/manifest.json --fixture-root artifacts/personaplex-eval/fixtures --variants baseline --voices NATF2.pt NATM1.pt --repeats 3 --dry-run --output artifacts/personaplex-eval/dry-run
```

The mock CLI starts only its own temporary mock service/database and sanitizes
inherited provider/model/recording settings; it never enables live dialing. Tests
use temporary databases, never the production database. Native codec and local
WebSocket tests must run without dependency skips on the VM before release.
Fixture generation is exclusive: use a new directory if already generated.
No native packages are needed for generator/dry run. No scores means **unrated**,
not pass. `concise_v1` is unavailable pending independently reviewed preservation
of every existing policy; the executable gate uses baseline only.

## 2. Service-owned transport settings

Only for the existing protected production service environment, after draining
and reconciliation; do not overwrite its existing confirmed provider settings:

```ini
MEDIA_INPUT_STARTUP_MS=60
MEDIA_INPUT_MAX_BUFFER_MS=1000
MEDIA_OUTPUT_MAX_BUFFER_MS=400
MEDIA_INTERRUPTION_MODE=off
MEDIA_INTERRUPTION_RMS=1000
MEDIA_INTERRUPTION_SUSTAIN_MS=400
MEDIA_INTERRUPTION_RELEASE_MS=300
```

Startup is 0–200 ms; input/output maxima 100–2000 ms (input must exceed startup).
Sustain is 300–1500 ms, release 100–1000 ms; all are multiples of 20 ms. RMS must
be finite 100–12000 PCM16 units. Modes: `off` / `sustained_energy`. No request
fields override these settings. They affect new ViH bridge sessions only.
20 ms framing, 200 ms marks, total 40 ms detection gap allowance and 1000 ms
clear cooldown are constants. The native codec's 40 ms writer chunk is unchanged.

Input clock starts at validated provider start plus the fixed startup allowance;
shortages become silence, not a VAD gate. Late payloads retain FIFO order but
cannot be placed at historical capture time without trusted timestamps.
Input and output producers/readers/writers are independent; a separate clear
control task cannot be blocked by a full audio queue or pause model input.
All tasks are bounded by call lifetime and cancellable; network sends retain
existing timeouts. The nominal media interval is 20 ms. Routine wake/send jitter
is compensated against the previous deadline instead of accumulating as drift;
phase correction shortens the next wait and still yields between frames. A stall
of a full 20 ms or more rebases to now + 20 ms rather than sending a backlog in a burst. This is not a
guarantee that every individual inter-send interval is at least 20 ms.
The 400 ms output cap may expose a bursty or overloaded deployment as a safe
bridge failure; inspect overflow/high-water data, do not remove bounds silently.

The energy heuristic centers PCM to reject DC offset and needs sustained real
caller samples plus pending/recent agent activity. It clears queued/residual
telephone audio once, invalidates stale epochs/marks, and suppresses output until
quiet **and the pending provider clear has completed**. Decoding and caller input
continue; no model/Opus/soxr reset or new prompt. Continuous generated silence and
marks do not qualify as agent activity: queued/held/residual or recently sent
output needs centered RMS >=100 PCM16 units. This fixed acoustic non-silence floor
is also an uncalibrated hypothesis, not speech detection; generated noise can
qualify and extremely quiet speech can be missed.
Long acknowledgements/noise/music/echo can false-clear; short/quiet urgent speech
can be missed. This is not semantic speech classification or echo cancellation.
Keep it **off** until consented phone/headset tests establish acceptable tradeoffs.

`streamStats` adds real/synthetic input samples, arrival-clock FIFO current/high
water, scheduler late/missed/max lateness, generated/sent/discarded/suppressed
duration, output queue age/size, marks matched/unknown/duplicate/invalidated/evicted,
RTT, provider-ack outstanding estimate, energy candidates/confirms/clears, clear
send latency and first input/output offsets. Periodic logs use only explicit
numeric aggregate allowlists, never audio, agent text, facts, prompt or URLs.
Marks after clear do not establish playback. In-flight sends cannot be retracted.
`outputInFlightAtClearMs` distinguishes unretractable audio from locally discarded
queue/residual samples; it does not assert whether the caller heard that audio.
`recordingFailed` exposes final recording/timeline I/O failures too; validated
provider stop remains authoritative even if recording finalization fails.

## 3. Local model checks — operator-only; not run during implementation

First drain/reconcile all admitted calls and independently confirm the single
model slot is idle. Never clear an admission/recovery lease to create capacity.
The evaluation-exclusive lock prevents overlapping evaluation commands in this
checkout; it is **not** production service admission or a cross-checkout lock.
Do not remove a stale lock until its owning process is confirmed stopped.

Use a **separate shell**, explicit probe-only configuration and no production
credential file. The following key is fake and must never enter a live service:

```bash
export CONTROL_HOST=127.0.0.1 MEDIA_HOST=127.0.0.1
export BRIDGE_API_KEY=probe-local-only-not-a-production-key-00000000
export VIH_DIAL_MODE=mock ENABLE_LIVE_DIALS=false
export PUBLIC_STREAM_BASE_URL=wss://bridge.example.com
export PERSONAPLEX_URL=ws://127.0.0.1:8998/api/chat
export ALLOWED_VOICES=NATF2.pt,NATM1.pt DEFAULT_VOICE=NATF2.pt
.venv/bin/voice-harness-probe NATF2.pt --timeout 45
```

Probe proves only protocol/ready/audio/text with silence, not business compliance.
Voice/text prefill happens before readiness; keep this time separate from speech
turn latency. Native model audio is mono 24 kHz streaming Opus. Generated text
is **agent text, not caller ASR**. The stock API supplies no general live
tool-result/context-update interface; spoken bookings/actions are not backend
transactions.

Only with explicit inference authorization, idle confirmation, verified deployment
label and installed voice assets:

```bash
.venv/bin/python -m voice_harness.evaluation --manifest examples/personaplex-eval/manifest.json --fixture-root artifacts/personaplex-eval/fixtures --variants baseline --voices NATF2.pt NATM1.pt --repeats 3 --personaplex-url ws://127.0.0.1:8998/api/chat --model-label OPERATOR_VERIFIED_CHECKPOINT_AND_SERVER_REVISION --run-model --confirm-model-idle --output artifacts/personaplex-eval/model-runs
```

That manifest is synthetic **transport-only**. Follow the [recording/scoring
instructions](../examples/personaplex-eval/README.md) to prepare a separate private
consented English manifest before quality scoring. Each case retains initial,
inter-utterance and >=5 s (usually 10+) trailing silence. Fixed replay is
non-adaptive; interactive tests remain necessary. Actual replay concurrently
sends input and decodes output through the shared codec, bounded FIFOs, telephone
packetizer and scheduler into a local fake sink; it never contacts ViH.
Agent WAV is concatenated locally sent audio; timeline offsets describe gaps,
arrival and discarded termination samples, not imagined audible playback.
The current evaluation path runs native-only interruption handling (off);
intervals are annotations, not automatic semantic scores or a live controller.
Manifest root/case/interval fields are strict (including duplicate-key rejection).
Fixture paths use relative forward-slash paths with no symlink components.
Local model evaluation refuses redirects as well as credential/query-bearing or
non-loopback endpoints, so a loopback redirect cannot silently contact a remote
server.
Any failed model replay stops the remaining matrix rather than opening another
session with potentially uncertain model ownership. Review the failure and
independently confirm idle before a new command; no evaluation lock is production
admission.

Artifacts include private agent WAV/text/timeline and summary with input/facts/
prompt hashes, fixed time, versions, blinded ID, voice/repeat, model identity,
actual timing and clipping/RMS/near-silence metrics, failure status and null human
rubric. Requested seed/sampling are `not_set`; effective sampling is
`unknown_stock_server`, not fabricated deterministic inference. WAV energy is
not spoken quality. Score at least three repeats/case/voice; include failures,
missing ratings and distributions. Annotate speech and actual telephone playback
separately for end-to-end latency. No automatic production promotion.

## 4. Production deployment and controlled telephone gate

After offline, native and baseline replay gates, use the operator's **existing
protected environment/service unit**, existing confirmed ViH/Nginx configuration,
and `.venv/bin/voice-harness` with one process. Only that production launch should
load its protected existing environment; this project does not load `.env`
automatically. Do not replace keys with probe values or overwrite confirmed
settings. No automated sample dial is part of these checks. A real call needs
separate authorization, destination consent and a stable idempotency key.

Drain/reconcile before restart/deploy/rollback. Preserve uncertain-call recovery
and no-redial; never reset the DB/lease to bypass it. Validate telephone initial
silence, echo/noise, pauses, quiet corrections, backchannels and sustained overlap
with interruption **off** first. Confirm this account's ViH mark/clear semantics.
Only then consider an explicitly calibrated energy A/B. Disabling the heuristic
restores native-only behavior for new sessions; do not reconnect active calls.
Keep existing production prompt/voice defaults. No Hindi, tools/ASR, calendar,
CRM, opt-out database or real hangup implementation is introduced.

### Input-buffer-overflow pacing hotfix

The initial pacing implementation reset every deadline from the actual wake/send
time. Small positive scheduling delays therefore accumulated, making a healthy
100 ms-packet input outpace the nominal 20 ms consumer until its 1000 ms buffer
filled. The [pacing hotfix](personaplex-pacing-hotfix.md) preserves the nominal
phase under ordinary jitter; it does not raise limits, drop caller speech or
disable overflow safety. Real sustained overload can still overflow a queue.
`media_progress` now includes `input_queue_ms`, `input_late_ticks` and
`input_max_lateness_ms`, alongside queue high-water/missed-tick counters.

Deploy after draining/reconciling calls, retain the existing environment and DB,
and restart the single harness process. PersonaPlex does not need a restart for
this client-side change. A failed attempt left `recovery_required` must be
reconciled against the provider's actual outcome before another attempt; never
release all reservations or restart merely to bypass an uncertain call.

## Privacy and limitations

`RECORDING_DIR` remains opt-in. Existing raw `.ulaw` files preserve byte semantics;
exclusive private timeline sidecars cap at 16 MiB and set `timelineTruncated` on
cap. Disabled recording creates neither audio nor sidecar. The timeline contains
only fixed events/numeric offsets/counts/epoch, no provider strings or transcripts.
Evaluation artifacts are private/exclusive and ignored; do not commit/upload
them. POSIX files are 0600/private directories; on Windows configure owner-only
ACLs separately. Establish consent, retention and manual deletion procedures.
No retention/upload daemon is added. Model/listening/telephone gates were not
performed by the implementation stage; no quality or latency improvement or
production verification is claimed.
