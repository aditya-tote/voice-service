# Input pacing drift hotfix

## Failure and diagnosis

A supplied telephone test ended after about 25 seconds with
`input_buffer_overflow`, an input queue high-water mark of 1000 ms and
`recovery_required`. Earlier high-water marks rose from 500 ms to 840 ms. Output
buffering stayed below its cap and no interruption clears occurred. These logs
identify the bridge's input-overflow shutdown, not a prompt-driven hangup or the
model's `torch.load` warning. No provider terminal event established a normal
completion in those logs.

The initial `Deadline.advance()` used
`max(previous_deadline + 20ms, actual_time + 20ms)` on every frame. This always
carried ordinary positive wake/send overhead into the next frame's schedule.
A 1 ms late wake turned a nominal 20 ms interval into 21 ms repeatedly, making
the consumer slower than real-time telephone input. The original short pacing
tests enforced a strict minimum 20 ms interval and did not catch long-run drift.

## Change

- Preserve the previous deadline's nominal 20 ms phase under routine jitter.
- Compensate sub-frame lateness with a shorter positive wait, yielding between
  frames instead of sending an accumulated backlog in a synchronous loop.
- Rebase to actual time + 20 ms after a full-frame-or-longer stall, while retaining
  lateness/missed-tick counters. An overloaded VM is still observable and bounded.
- Apply the shared fix to live input, live playback and evaluation clocks.
- Keep the 1000 ms input / 400 ms output caps, overflow failure behavior, prompt,
  voice, codec, model session and interruption defaults unchanged.
- Log current input queue, late ticks and maximum lateness as safe numeric fields.

No new VM setting, model restart or upstream PersonaPlex patch is required. This
fix does not reconstruct lost packets, correct provider sample-clock skew, drop
speech, or guarantee against real CPU/network/model overload.

## Regression coverage

Deterministic tests simulate five minutes of small wake/send overhead, three
minutes of 100 ms provider packets feeding a 20 ms consumer, and three minutes of
80 ms generated chunks with provider-send overhead. They also cover bounded
partial-frame correction and repeated true stalls. Real calls/model generation
were not performed as part of this hotfix; confirm deployment behavior on the VM.

### Executed checks before independent review

- Native-runtime suite: **328 passed, zero skipped**, one existing framework
  deprecation warning; Ruff and diff whitespace checks passed.
- The same deterministic 100 ms input replay overflowed the old scheduler after
  **18.701 seconds** at its unchanged 1000 ms cap. The fixed scheduler completed
  **180.042 seconds**, with input high-water **160 ms** and no overflow.
- A separate **45-second real-clock** local queue test, with 100 ms input packets
  and small synchronous codec/send-work delays, had no overflow: input high-water
  **200 ms**, final queue **100 ms**, one genuine missed tick. This is a transport
  timing test, not a model or telephone call.
- An earlier stress test with two extra `asyncio.sleep(.001)` calls per cycle did
  overflow on this Windows host. A follow-up measurement of 100 such sleeps found
  **15.541 ms mean / 16.292 ms p95** rather than the requested 1 ms. Two such waits
  exceed the entire 20 ms work budget. This overloaded variant is **not a pass**;
  real stalls/overload remain bounded failures rather than being hidden by larger
  buffers or unlimited catch-up. Linux VM behavior still needs a controlled test.

Independent focused review then passed **30 timing tests** and **15 additional
boundary checks**, including exact 20 ms stalls and large monotonic clock origins.
An in-memory negative control using the old algorithm failed all eight selected
drift/overflow regressions. The reviewer found no additional defect and made no
source changes. No production/model/VM verification is implied.

## Deployment and recovery

Use the code-only hotfix archive or updated source in the existing harness
directory, preserving `.env` and SQLite. Drain/reconcile calls before stopping or
restarting the harness. Restart only the harness after installing the update;
the existing PersonaPlex model process can remain running and idle.

Do not increase buffers merely to conceal this drift. Leave
`MEDIA_INTERRUPTION_MODE=off`. On a controlled test, inspect current queue values
over time, not just the all-time high-water mark. Ordinary jitter should not cause
steady queue growth; real stalls or provider buffering still warrant investigation.

The failed call remains recovery-required until its provider outcome is resolved.
Confirm termination with the provider and use the existing targeted recovery/CDR
workflow. Do not blindly clear a GPU reservation or create repeated new attempts.
