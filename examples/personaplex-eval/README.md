# English-only evaluation fixtures

Generate fixtures with `python -m voice_harness.evaluation --generate-fixtures --output artifacts/personaplex-eval/fixtures`.
The checked-in manifest references `tone.wav`: one second initial silence, one
second tone, six seconds silence. The silence/noise alternatives are also
**transport-only**, never intelligibility, policy or conversation evidence.
No real caller audio is shipped. Artifacts are private and ignored by git.

## Consented human recordings

Create a separate private manifest using the same schema. Set `kind` to
`consented_english_speech`, `language` to `en`, and `consentConfirmed` to true
only after obtaining consent. Use synthetic lead facts, a fixed timezone-aware
`preparedAt`, and relative WAV paths beneath the explicit fixture root. Record
mono PCM16 8 kHz; retain initial/inter-utterance silence and at least 5 seconds
(usually 10+) exact-zero trailing silence. Duration is at most 600 seconds.
Intervals are ordered, non-overlapping seconds, ending before trailing silence;
allowed kinds: activity, backchannel, interruption, noise, echo. These are human
annotations, not caller ASR or a live dialogue controller.
Root/case/interval objects reject unknown or duplicate fields. Fixture paths must
be relative forward-slash paths without traversal, drive/stream syntax or symlink
components (even symlinks resolving inside the fixture root).

Record distinct English cases (including Indian English) for:

- Initial silence/hello; available → approved pitch → session offer.
- Busy → callback request, ambiguity and IST clarification.
- “More details now” without consent to a future meeting.
- Declined offer versus firm refusal/opt-out and denied enquiry.
- Explicit future demo/discussion/assessment, unknown facts, names/numbers corrected.
- Known false decision maker, no solution and zero devices; do not re-ask known facts.
- Hesitation/long pause; short “uh-huh”; sustained correction during agent speech.
- Echo/noise, quiet corrections and a longer multi-turn conversation.

Replay is non-adaptive: it cannot know what the agent just said. Follow repeated
replays with separately authorized interactive headset/telephone tests. Model
text is generated **agent text**, not caller transcription. No real backend
booking/email/opt-out/hangup action is performed by prompting.

## Blinded human scoring

Use at least three repeats per case/voice/variant, all failures included. Give
listeners randomized run IDs and clips without voice/variant labels; keep the
private mapping for analysis. `summary.json` provides a JSON rubric with null
scores (unrated, never zero/pass): opening/flow, facts/names/numbers, fabricated
actions, opt-out, corrections, interruption recovery, backchannels/takeover,
intelligibility, naturalness, relevance and manually annotated speech latency.
Use 1–5 ratings with written failure notes in a separate private scoring file;
record listener ID, case ID, run ID, measurement method and clip boundaries.
Report per-case distributions, missing ratings and failures, not a best clip.
Locally sent samples/mark RTT are not audible telephone latency. No automatic
production promotion. `concise_v1` is deliberately unavailable until all existing
business guardrails can be preserved and independently reviewed; baseline uses
the exact production prompt and fixed facts/clock.
