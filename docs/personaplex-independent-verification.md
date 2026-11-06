# Independent testing and fixing report — 6 November 2026

## Outcome and safety boundary

The actual tracked diff and new files were inspected against
`personaplex-integration-plan.md` and the workspace research note. The planning
file was preserved. Verification is offline/native-client evidence only, **not
model, conversational-quality, telephone-playback or production verification**.
No real `.env`/production SQLite access, provider requests, model inference,
server/GPU startup, weights, credential changes, commits or resets were performed.
Local WebSocket tests use fake servers; service tests/mock CLI use temporary DBs.

## Bugs found and fixed in this stage

1. **Continuous generated silence counted as agent activity.** The playback
   heuristic now tracks acoustic non-silence separately on queued/held frames,
   residual and recent sends. Silence packets and marks cannot alone trigger a
   caller-energy clear. The fixed centered output RMS floor is 100 PCM16 units;
   this is an uncalibrated acoustic guard, not speech/semantic classification.
   Generated noise may still qualify; extremely quiet output may be missed.
2. **A delayed clear could admit the old tail after the energy latch released.**
   Decoding continues, but suppression now lasts until both quiet release and
   provider-clear completion. Input continues during the slow control send.
3. **Scheduling race after send-lock admission.** `wait_for` creates a child task;
   a stop/epoch change could occur before the actual media/mark send began.
   Rechecks now occur inside that scheduled task. Model input has the analogous
   stop recheck, so newly scheduled synthetic input cannot start after validated
   stop. Already in-flight sends remain unretractable, as documented.
4. **Recording finalization errors were swallowed.** Close attempts every
   recording resource and exposes failures through additive `recordingFailed`.
   Write/flush/timeline failures remain safe failures and retain uncertain-call
   admission. A validated provider stop remains authoritative even on close
   failure; exception text is never logged.
5. **Input received-sample accounting excluded the overflowing arrival.** It now
   counts real samples received before rejecting excess capacity, consistently
   with generated-output accounting; no overflow data is admitted to the FIFO.
6. **Evaluation validation was permissive.** Root/case/interval unknown and
   duplicate fields, boolean version/number errors, in-root symlinks, drive/stream
   syntax, empty credentials/query/fragment markers and URL control/space
   normalization are rejected. WAV size/format/truncation/tail, finiteness and
   interval restrictions have focused regressions. Parser errors no longer echo
   rejected private argument values.
7. **A loopback WebSocket could redirect elsewhere.** Evaluation's default client
   now refuses all redirects; existing production client defaults are unchanged.
8. **Cleanup failures could look successful and allow the next model repeat.**
   Client/codec/artifact finalization failures now fail replay, all remaining
   cleanup is attempted, and any failed model run halts the remaining matrix.
   New operator idle confirmation is required before another command. Transport
   fixtures explicitly report `qualityStatus: not_evaluated_transport_only`.
9. **Windows mock CLI cleanup failed under the isolated venv.** One run completed
   its assertions but hit a temporary SQLite sharing violation during teardown.
   The helper now stops only its own still-live process tree by PID on Windows,
   covering venv launcher children, and never targets an exited/reusable PID.
   Both interpreter variants subsequently passed the complete mock CLI check.

QA changed `src/voice_harness/{media_timing,vih_media,evaluation,personaplex}.py`,
`scripts/check_mock_cli.py`, the evaluation/runbook documentation, and regression
tests. New independent test files are `tests/test_personaplex_qa.py` and
`tests/test_mock_cli.py`; existing evaluation/timing/media/WebSocket tests were
extended. No ASR, dialogue/business controller, Hindi, backend action, production
prompt/voice override or model reset was added.

## Exact verification results

Initial independent reproduction: **260 passed, 4 skipped, 2 warnings**.

Final commands from `voice-agent-harness`:

| Command | Actual outcome |
| --- | --- |
| `python -m pytest -q -rs` | **312 passed, 4 skipped, 2 warnings**, 22.21 s |
| `C:\Users\AdityaTote\AppData\Local\Temp\opencode\personaplex-qa-20261106\Scripts\python.exe -m pytest -q -rs` | **319 passed, 0 skipped, 1 warning**, 22.62 s |
| `python -m ruff check .` | Passed |
| `python -m compileall -q src tests` | Passed |
| `python scripts/check_mock_cli.py` | Passed: mock HTTP/auth/idempotency/conflict/result/restart/logging checks |
| Same mock CLI with isolated-venv Python | Passed after Windows cleanup fix |
| `git diff --check` | Passed; Git emits existing LF/CRLF conversion notices |

Default-interpreter skips are explicit, **not passes**:

- `tests/test_codec_runtime.py:3`: missing `numpy` (module collection skip).
- `tests/test_websocket_integration.py:8`: missing `websockets` (module skip).
- `tests/test_lead_validation_regressions.py:66`: missing `websockets.asyncio.server`.
- `tests/test_model_preparation.py:231`: missing `websockets.asyncio.server`.

Warnings: existing Starlette/httpx TestClient deprecation in both environments;
the default environment also has the existing AnyIO BlockingPortal alias warning.

Native dependencies were installed non-editably **only** in the approved-temp
venv, with `python -m venv <that-directory>` then its Python
`-m pip install ".[runtime,dev]"`. No default-interpreter package installation.
Native versions: NumPy 2.5.4, soxr 1.1.0, sphn 0.2.1, websockets 15.0.1.
Native silence→tone→silence resampling/streaming Opus continuity and local fake
WebSocket checks ran without skips; this does not run a model.

Baseline was additionally compared directly against `git show
HEAD:src/voice_harness/personaplex.py` at fixed time for three synthetic fact sets
(ordinary, known false/zero, and Unicode). All prompt bytes matched exactly.
Existing encoded URL/request-line boundary and pre-dial rejection tests passed.

Executed evaluation CLI commands, from `src` (no service configuration):

```powershell
python -m voice_harness.evaluation --generate-fixtures --output ../artifacts/personaplex-eval/qa-independent-fixtures
python -m voice_harness.evaluation --manifest ../examples/personaplex-eval/manifest.json --fixture-root ../artifacts/personaplex-eval/qa-independent-fixtures --variants baseline --voices NATF2.pt NATM1.pt --repeats 3 --dry-run --output ../artifacts/personaplex-eval/qa-independent-dry-run
python -m voice_harness.evaluation --manifest ../examples/personaplex-eval/manifest.json --fixture-root ../artifacts/personaplex-eval/qa-independent-fixtures --variants baseline --voices NATF2.pt NATM1.pt --repeats 3 --dry-run --output ../artifacts/personaplex-eval/qa-independent-final-dry-run
```

Generation produced three transport-only fixtures. Each dry run reported six
baseline/voice/repeat runs, zero failures, `dry_run_not_evaluated`, null ratings
and `concise_v1: unavailable_policy_preservation_deferred`. Artifacts are ignored;
they are not deployment/quality evidence and must not enter the code ZIP.

## VM commands, settings and remaining gates

The executable commands are in [the VM runbook](personaplex-vm-runbook.md): Python
3.12+ isolated venv, `pip install -e '.[runtime,dev]'`, pytest/Ruff/compile/mock CLI,
then fixture generation and **baseline-only** dry run. Its separate operator-only
probe/model commands use the actual CLI, require authorized idle model access,
and must not run while a production admission/recovery lease owns the model.

Keep production service-owned defaults:

```ini
MEDIA_INPUT_STARTUP_MS=60
MEDIA_INPUT_MAX_BUFFER_MS=1000
MEDIA_OUTPUT_MAX_BUFFER_MS=400
MEDIA_INTERRUPTION_MODE=off
MEDIA_INTERRUPTION_RMS=1000
MEDIA_INTERRUPTION_SUSTAIN_MS=400
MEDIA_INTERRUPTION_RELEASE_MS=300
```

No settings/ranges or original business policy were relaxed. The extra output
non-silence guard, stricter evaluation fail-closed behavior and visible recording
finalization errors are bounded QA refinements, not new dialogue functionality.
`concise_v1` remains deferred; interval annotations are not a live controller.
The local evaluation lock is not production or cross-checkout admission.

Not performed: real PersonaPlex inference, checkpoint/voice-asset verification,
consented speech/listening/repeated quality scoring, Linux VM deployment or actual
telephone mark/clear/echo/latency tests. Arrival-clock gaps are not capture-clock
reconstruction; local sends/provider acks are not audible playback proof.
POSIX private-mode behavior is implemented but this host is Windows; owner-only
ACLs and Linux filesystem behavior need operator validation. Keep interruption
off pending consented calibration. No model or telephone quality/latency gain is
claimed, and no automatic production promotion is authorized.
