# Voice Agent Harness POC

VAH is the GPU-VM gateway between an OpenClaw sales agent and ViH/PersonaPlex. A human can use the same `POST /v1/calls` contract that the future agent tool uses. VAH stores the request in SQLite before it dials and exposes the final record as raw JSON at `GET /v1/calls/{callId}`.

The default is `VIH_DIAL_MODE=mock`: it cannot phone anyone and does not need ViH, PersonaPlex, or GPU runtime packages. It returns a durable simulated result for the POC demo.

## Run the mock POC on Windows

Copy `.env.example` to `.env`; set `BRIDGE_API_KEY` to a unique value of at least 32 characters. The CLI does not load `.env` itself. In PowerShell:

```powershell
Get-Content .env | Where-Object { $_ -match '^[^#].*=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2; Set-Item -Path "Env:$name" -Value $value
}
$env:BRIDGE_API_KEY = 'replace-with-a-32-character-minimum-secret'
$env:CONTROL_HOST = '127.0.0.1'
python -m pip install -e '.[dev]'
voice-harness
```

In a second PowerShell window, load the same environment, then call it:

```powershell
$headers = @{ Authorization = "Bearer $env:BRIDGE_API_KEY"; 'Idempotency-Key' = 'manual-poc-0001' }
$payload = @{ leadId='lead_123'; customerPhone='+919876543210'; leadContext=@{customerName='Asha Rao';companyName='Example Manufacturing';customerEmail='asha@example.com';source='Meta';enquiryNotes='Interested in protection for business laptops.'} } | ConvertTo-Json -Depth 5
$created = Invoke-RestMethod http://127.0.0.1:8080/v1/calls -Method POST -Headers $headers -ContentType application/json -Body $payload
Invoke-RestMethod ("http://127.0.0.1:8080" + $created.resultUrl) -Headers $headers | ConvertTo-Json -Depth 8
```

On Linux or the GPU VM, use the reusable same-contract request file:

```bash
curl --fail --request POST http://127.0.0.1:8080/v1/calls \
  --header "Authorization: Bearer $BRIDGE_API_KEY" \
  --header 'Idempotency-Key: manual-poc-0001' \
  --header 'Content-Type: application/json' \
  --data-binary '@examples/start-call.json'
```

The result contains `callId`, `simulated`, `state`, bounded dial data, provider IDs when known, CDR, `agentText`, `customerTranscript: null`, `complete`, and correlation status. A WSS-completed call without CDR reports `correlationStatus: "not_received"`; a received matching CDR reports `matched`. Reusing the same key and canonical validated facts (including defaults) returns the existing call. Changing valid facts with that key returns 409 and never creates another attempt. See the breaking migration policy below for historical requests.

Example mock result shape:

```json
{"callId":"call_...","simulated":true,"state":"completed","dial":{"request":{"mode":"mock"},"status":"simulated","response":{"mode":"mock","accepted":true}},"provider":{"callSid":null,"campaignId":null,"streamSid":null},"cdr":null,"agentText":"","customerTranscript":null,"complete":true,"correlationStatus":"not_received"}
```

## Linux / GPU VM

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[runtime,dev]'
set -a; . ./.env; set +a
.venv/bin/voice-harness
```

Use one worker. `GET /health/ready` verifies runtime packages and the local PersonaPlex TCP port only; it does not prove a model can produce audio. Run `voice-harness-probe NATF2.pt` on the GPU VM before a controlled phone test.

## Logs and call troubleshooting

VAH writes structured JSON events to standard output with a UTC `timestamp`, `level`, `event`, and applicable `call_id`, `stage`, `state`, `reason`, `error_type` or fixed `error_code`, plus safe counters such as `duration_ms`. HTTP responses include a generated `X-Request-ID`; its `request_id` links the request outcome to call creation and dialing logs. Use the `call_id` returned by `POST /v1/calls` to follow a call through dialing, WSS, PersonaPlex, and completion or recovery.

Set `LOG_LEVEL=INFO` for normal operation; no extra diagnostic environment setting is needed. It includes masked ViH OBD request/response summaries (approved field values, presence flags, response classification, HTTP status and duration) and WebSocket connection/event summaries (safe route, authentication-presence flags, fixed event metadata and close code). These make an accepted-but-not-connected call diagnosable without exposing provider traffic. `LOG_LEVEL=DEBUG` adds checkpoint counters every two seconds.

By default, raw provider request bodies, response messages, URLs, provider call IDs, campaign IDs, WebSocket tokens, audio payloads, scripts, prompts, phone numbers, headers, credentials, exception text and incoming media events are excluded from logs. For temporary debugging, set `LOG_PROVIDER_DESTINATION=true`: `obd_request.provider_request` adds `debug_phone` and `debug_wss_url`. The URL shows the host and call path, with its query replaced by `token=[redacted]`; credentials and tokens remain hidden. These logs contain personal data: disable the flag after debugging and redact the phone before sharing publicly. Dependency messages and tracebacks become generic component/severity events. Set `WARNING` or `ERROR` to reduce output.

On the GPU VM, retain a live copy while running the service:

```bash
set -a; . ./.env; set +a
.venv/bin/voice-harness 2>&1 | tee -a voice-harness.log
```

If VAH is run through `systemd`, inspect the same events with:

```bash
journalctl -u voice-harness -f -o cat
```

Use the logged `stage` and `reason` to locate a failure: `runtime` means the local audio/model prerequisite check; `obd` is the ViH dial request; `media_auth` and `provider_handshake` are the ViH WebSocket lifecycle; and `model_connect` is the PersonaPlex connection. The durable raw result remains available from `GET /v1/calls/{callId}`.

The probe streams paced silence continuously while receiving model output and decoding it back to PCM. Its output deadline is 30 seconds after the handshake by default; `voice-harness-probe NATF2.pt --timeout 45` changes it. A timeout reports sent Opus packets, received audio, decoded bytes and text frame counts. Silence does not guarantee a spoken response, so inspect model logs if audio arrives without text. The earlier 60 ms input burst was insufficient for a streaming test.

## Enabling ViH only after provider confirmation

`BRIDGE_API_KEY` authorizes the agent/manual caller. `VIH_UKEY` is only sent in ViH's OBD request. `VIH_CDR_SECRET` is separate and validates ViH's CDR callback. Never reuse them.

Set `ENABLE_LIVE_DIALS=true` and `VIH_DIAL_MODE=live` after ViH confirms the HTTPS OBD endpoint, service number, streaming template, approved campaign type, phone format, and the dynamic WSS value. For this POC, `VIH_WSS_VALUE_MODE=confirmed_dynamic`, `MEDIA_AUTH_MODE=query`, and `VIH_WSS_VALUE` must exactly be `wss://your-public-origin/calls/{callId}?token={token}`. A valid provider WSS `stop` event completes the call and releases its GPU reservation; CDR is not required for audio streaming. It sends one recipient with `sendnow=0`, `filetype=2`, and provider retry `0`; an uncertain submission is `dial_unknown` and is never automatically redialed.

The latest provider payload supplied for this integration uses `msisdnlist[].param1` for the complete per-call WSS URL and template ID `200`, replacing the earlier PDF's `WSS` field. Set `VIH_TEMPLATE_ID=200` for that account; the ID remains configurable. VAH sends the phone number from the agent request and campaign type, credentials and service number from the environment. It sends `retryatmpt="0"` and `retryduration="15"`. Scheduling is not exposed: `sendnow="0"`, so the unused example `schddate` is omitted. Verify the account template connects to the exact supplied path and query token in a controlled call. A fixed dashboard WSS reference instead needs a provider-specific correlation adapter; `/stream` currently rejects connections.

The provider confirmed ten-digit Indian national numbers for `phoneno`. Set `VIH_PHONE_FORMAT=india_national` as in `.env.example`: the agent still sends E.164 (`+91` plus ten digits), and VAH strips the country code only for the outbound ViH request. Other country codes are rejected in this mode.

CDR is optional. For the WSS-only audio demo, use:

```dotenv
VIH_CDR_AUTH_MODE=disabled
VIH_CDR_SECRET=
VIH_RESPONSE_CALL_ID_PATH=
VIH_RESPONSE_CAMPAIGN_ID_PATH=
```

No webhook registration is needed for this mode. If completion reporting is needed later, register `https://your-public-origin/webhooks/vih/cdr` with ViH and configure bearer or query authentication with a distinct `VIH_CDR_SECRET`. CDR reception works even when the dial response has no IDs: unmatched reports are stored in `provider_events` and logged with `matched=false`, without changing any call reservation. To reconcile calls such as `NO_ANSWER` that never opened WSS, configure a confirmed `VIH_RESPONSE_CALL_ID_PATH` or `VIH_RESPONSE_CAMPAIGN_ID_PATH`. Never guess those paths or match by phone number alone. Without CDR, a submitted call that never opens WSS cannot be automatically finalized: verify its outcome with ViH before manual recovery. A restart preserves the reservation and marks it recovery-required; it does not resolve the provider outcome. Completed WSS-only results keep `cdr` and `disposition` null rather than inventing an outcome. Do not use `BRIDGE_API_KEY` as a provider credential. Set `RECORDING_DIR=recordings` to retain both audio directions. Recordings contain audio sent/received at the bridge; playback marks provide separate acknowledgement, and audio queued when the call ends may not have played.

The provided ViH guide documents JSON WebSocket events with Base64 G.711 mu-law 8 kHz media. VAH includes a strict adapter for that protocol and the existing PersonaPlex codec boundary. A live stream is intentionally not enabled until ViH confirms how an OBD call maps to the public WSS path/credential. Customer transcription needs a separate STT provider, so the POC returns `customerTranscript: null`.

### Facts-only call contract (breaking revision 2.0.0)

`POST /v1/calls` now accepts exactly `leadId`, `customerPhone`, and required `leadContext`. Extra top-level/nested fields are forbidden even if null. `script`, old `context`, `metadata`, `voiceName`, policy/persona and clock/timezone overrides return 422. See the [full example](examples/start-call.json), [minimal example](examples/start-call-minimal.json), and [API contract](../docs/api/voice-service-contract.md).

| Field | Required / bounds |
|---|---|
| `leadId` | Trimmed nonblank string, <=4,096 characters; never sent to model. |
| `customerPhone` | E.164 `^\+[1-9][0-9]{7,14}$`; never sent to model. Provider normalization remains separate. |
| `leadContext.customerName` | Required nonblank string, 1–120 characters. |
| `leadContext.companyName` | Required nonblank string, 1–160 characters. |
| `leadContext.customerEmail` | Required ASCII mailbox, 3–254 characters; local part <=64, one @, dotted DNS domain, labels <=63/no edge hyphens. Common + tags/subdomains accepted; no whitespace, controls, quoted/Unicode forms or edge/consecutive local dots. Case preserved; syntax only. |
| `leadContext.source` | Required nonblank string, 1–64; open vocabulary, including `Unknown`. |
| `leadContext.productInterest` | Optional non-null nonblank string, 1–160; defaults to `managed endpoint security`. |
| `leadContext.enquiryNotes` | Optional null or nonblank string, 1–400. |
| `leadContext.decisionMaker` | Optional strict boolean or null; strings/numbers rejected. |
| `leadContext.existingSecuritySolution` | Optional null or nonblank string, 1–160; explicit `None` text is a known fact. |
| `leadContext.deviceCount` | Optional strict integer >=0 or null; booleans/fractions/numeric strings rejected. Zero is known. |

All strings are trimmed. Model-bound strings reject control characters, including embedded newlines. Optional fields other than interest default to null. Facts are safely serialized separately from fixed policy and are treated as untrusted data; this is guidance, not a guarantee of model compliance.

The service owns Sky's fixed Skysecure Technologies outbound flow: name/time/source enquiry follow-up and availability WAIT; busy manual callback request; if available, explanation of Palo Alto Networks Cortex XDR managed endpoint security with implementation, deployment and 24/7 managed monitoring **before** offering a demo/detailed discussion/assessment. Different interest remains a customer fact; other offerings need manual seller follow-up. A declined offer permits one alternate-requirement question; firm refusal/opt-out or denied enquiry closes immediately. Acceptance captures session type/date/time, then optional permission-based qualification one question per turn, skipping known facts (including false/zero) and avoiding confirmation of known name/company/email. Requested meetings are not booked: “Our team will confirm meeting details.” No STT, calendar, email, seller routing, or telephone hangup tool has been added. The single closing thanks the customer and says “Goodbye. Please hang up now.” Provider `stop`/CDR completes the actual call.

The prompt builder renders the opening as a complete quoted sentence with the customer's name, actual greeting and source, rather than leaving bracketed placeholders for the model to resolve. It supplies a literal company/product explanation before the session offer. A request for details **now** must be answered from approved facts; it does not imply consent to schedule a future meeting. The model is instructed to ask for the customer's preferred time, never invent available slots or email delivery.

These are prompt improvements, not a deterministic dialogue controller. If a recording or `agentText` shows a support-style greeting, skipped pitch or fabricated slot despite the correct prompt arriving, that is a model instruction-following failure; HTTP/handshake success and unit tests do not establish conversation compliance. The short probe uses a neutral microphone-test greeting and checks transport only. Strict production sequencing requires customer speech recognition, explicit dialogue state and controlled utterance generation/playback; those components are not implemented here.

`DEFAULT_VOICE=NATF2.pt` must be a safe `.pt` filename in `ALLOWED_VOICES`. The service samples actual preparation time in `Asia/Kolkata`, including ISO date/time and greeting: morning 05:00–11:59, afternoon 12:00–16:59, evening otherwise (including night). Windows without IANA data uses a stdlib fixed UTC+05:30 fallback for this India-only modern clock. Relative slots use that preparation timestamp; default IST, clarify ambiguous/past slots. Meta means Facebook/Instagram form; WhatsApp/Google/Website/LinkedIn use natural channel names. Unknown/unrecognized source uses generic enquiry follow-up, without inventing a channel.

The entire assembled prompt must be strictly `<7000` characters. The exact percent-encoded ASCII URL **and** origin-form `GET <path>?<query> HTTP/1.1\r\n` must each be strictly `<8190` bytes, based on the recorded PersonaPlex parser limit. Unicode and punctuation can expand; fields are never silently truncated. Either budget failure returns generic `422: leadContext exceeds model prompt or transport budget` **before** saving, admission, model connection or dial in both mock/live modes. The 64 KiB JSON body cap also applies. A short probe does not validate this full prompt's inference behavior.

Live calls build the artifact once and initialize that exact prompt URL and configured voice on PersonaPlex **before** sending the ViH dial request. The same ready WebSocket, including its preparation-time clock, is handed to the media bridge; a missing live preparation fails without reconnecting after answer. `PERSONAPLEX_PREPARE_TIMEOUT_SECONDS` (default 120) bounds this preparation. Failure marks the saved call `failed` / `runtime_not_ready`, releases admission, and does not call ViH. Preparation occurs before the telephone rings; this is not a guarantee of audio output.

`POST /v1/calls` waits for preparation plus the provider submission. Give Postman and any HTTP proxy a timeout longer than that combined time (for the default preparation limit, allow at least 150 seconds). If the client times out, retry with the **same** idempotency key/body to find the existing attempt; a new key could create another call. A ready model session waits up to `SESSION_TTL_SECONDS` (default 300) for the provider stream. If that window expires, the model closes and its token is invalidated, but the telephone reservation remains `recovery_required` until its provider outcome is resolved. Matched terminal CDRs, definitive local dial failure, and shutdown also clean up unclaimed model connections.

### Breaking migration and replay

| After cutover | Behavior |
|---|---|
| New valid facts/new key | Normal submission. |
| Same canonical facts/key, later clock/voice/policy configuration | Existing ID/current state, before clock sampling or prompt generation; no redial. Omitted interest and explicit default normalize identically. |
| Old script/context POST, even a historical replay | 422 before replay lookup. |
| New valid body/historical old key | 409; no row rewrite. |
| GET historical call ID | Original raw historical request/evidence remains readable. |

No request JSON/hash backfill or database migration is needed. Drain/reconcile old in-flight calls, model preparations and provider streams before deploying; old payloads cannot satisfy new media parsing and ready sessions are process-local. Restart invalidates tokens and retains uncertain-call recovery. Coordinate OpenClaw/tool-schema rollout: map known name/company/email/source into required facts, omit unknown optional facts or send supported nulls, inject bearer credentials externally, and keep stable attempt keys/call IDs. The producer adapter is external to this repository. If an old response/call ID is missing, reconcile through the pre-cutover/operator workflow before switching; obsolete POST cannot recover it and a fresh key must not bypass uncertainty.

## Routes

| Route | Authentication | Purpose |
|---|---|---|
| `POST /v1/calls` | Bridge bearer + `Idempotency-Key` | Agent-tool-shaped start request; mock completes locally. |
| `GET /v1/calls/{id}` | Bridge bearer | Durable raw JSON result. |
| `POST /webhooks/vih/cdr` | Confirmed provider bearer/query secret | Optional: stores/reconciles terminal CDR; disabled by default. |
| `WS /calls/{callId}` | One-use query media token | ViH JSON/Base64 μ-law stream, forwarded to local PersonaPlex. |

`DATABASE_PATH` holds requests and outcomes with SQLite WAL. A valid WSS `stop` completes a call. On a disconnect, timeout, model/bridge failure, or restart, VAH marks the call `recovery_required`, retains its GPU reservation, and never auto-redials. An optional CDR can reconcile that uncertain state; a CDR with no known `callSid`/`streamSid` is retained as an orphan rather than guessed from the phone number.

## Checks

```text
python -m pytest -q
python -m ruff check .
python -m compileall -q src tests
python scripts/check_mock_cli.py
```

## Environment reference

| Variable | Default | Meaning |
|---|---|---|
| `CONTROL_HOST`, `CONTROL_PORT` | `127.0.0.1`, `8080` | Private control listener; use a Tailscale IP on the GPU VM. |
| `MEDIA_HOST`, `MEDIA_PORT` | `127.0.0.1`, `8081` | Loopback ViH WebSocket listener reached only through Nginx. |
| `PUBLIC_STREAM_BASE_URL` | `wss://bridge.example.com` | Public TLS origin used in the dynamic ViH WSS URL. |
| `BRIDGE_API_KEY` | required | At least 32-character caller/agent bearer secret. |
| `LOG_LEVEL` | `INFO` | Structured stdout log threshold: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Logs intentionally omit payloads, phone numbers, scripts, audio, tokens, URLs, and credentials. |
| `MEDIA_AUTH_MODE` | `header` | Legacy session media mode; live ViH requires `query`. |
| `SESSION_TTL_SECONDS`, `MAX_STORED_SESSIONS` | `300`, `1000` | Legacy in-memory session bounds. |
| `PERSONAPLEX_URL`, `ALLOWED_VOICES` | local URL, required | Local model endpoint and allowlisted `.pt` voice names. |
| `DEFAULT_VOICE` | `NATF2.pt` | Service-owned outbound voice; safe filename and allowlist membership validated at startup. |
| `DATABASE_PATH` | `voice-harness.sqlite3` | SQLite WAL file for durable POC state. |
| `VIH_DIAL_MODE`, `ENABLE_LIVE_DIALS` | `mock`, `false` | Mock is safe; both must explicitly enable live dialing. |
| `VIH_OBD_URL`, `VIH_UKEY`, `VIH_SERVICE_NO`, `VIH_TEMPLATE_ID` | example URL/template supplied; credentials empty | Account-specific OBD endpoint and credentials/settings. `.env.example` uses the supplied CreateOBDCampaignPost endpoint and template `200`; set explicitly for your account. |
| `VIH_CAMPAIGN_TYPE` | empty | Account-approved `3` promotional or `4` transactional purpose. |
| `VIH_PHONE_FORMAT` | `e164` | Confirm the account format. `e164` sends the agent's `+91...` unchanged; `india_national` sends the ten national digits and rejects non-Indian numbers. The agent API always accepts E.164. |
| `VIH_WSS_VALUE_MODE`, `VIH_WSS_VALUE` | `unconfirmed`, empty | Live requires `confirmed_dynamic` and exact `wss://origin/calls/{callId}?token={token}`. Keep braces unquoted in `.env`; `set -a; . ./.env; set +a` preserves them. |
| `VIH_CDR_AUTH_MODE`, `VIH_CDR_SECRET` | `disabled`, empty | Optional CDR callback. Configure confirmed bearer or query authentication with a distinct 32+ character secret only when ViH completion reporting is enabled. |
| `VIH_RESPONSE_CALL_ID_PATH`, `VIH_RESPONSE_CAMPAIGN_ID_PATH` | empty | Optional, account-confirmed JSON Pointer such as `/data/callSid` or `/data/campaignId`; do not assume these paths from the guide. Configure one with CDR to reconcile `NO_ANSWER` calls that never open WSS. |
| `RECORDING_DIR` | empty | Optional service-account-only directory for caller/agent μ-law recordings. |
| `MAX_FRAME_BYTES`, `MAX_TRANSCRIPT_CHARS` | `16384`, `32768` | Per-provider-frame and stored agent-text limits. |
| `PERSONAPLEX_READY_TIMEOUT_SECONDS`, `IDLE_TIMEOUT_SECONDS`, `CLOSE_TIMEOUT_SECONDS`, `MAX_CALL_SECONDS` | `15`, `60`, `5`, `1800` | Model handshake, media inactivity, teardown, and call duration deadlines. |
| `CONTROLLER_CALLBACK_BASE_URL`, `CONTROLLER_API_KEY` | legacy defaults | Existing `/sessions` bridge callback only; new `/v1/calls` results are polled and do not use it. |

Legacy session and controller settings apply only to isolated compatibility helpers; the deployed CLI disables `/sessions`. New calls do not call a controller or an agent callback. A future agent can poll and consume the same raw result returned in this POC.

Live agent text, recording references, and counters are checkpointed every two seconds and saved again at cleanup. A WSS `stop` is the normal completion signal for the audio-only flow. An abrupt process failure can lose the most recent checkpoint interval and cannot resume a live phone connection. Calls without a clean `stop` retain their reservation and require recovery; an optional authoritative CDR can reconcile them. Do not clear that reservation and redial while the provider outcome remains unknown. Conflicting and unmatched CDRs remain in SQLite `provider_events` for review. SQLite and recording files are private on POSIX; configure service-account ACLs on Windows. Set a retention policy before longer use.

The OBD HTTP client has a 15-second overall deadline, bounded response storage, and no automatic retry. GPU admission occurs before dialing; readiness checks prove package availability and a TCP listener, so perform the model probe on the GPU VM too.

Caller audio now enters a bounded arrival-clock FIFO: independent 20 ms ticks send 160 samples, padding shortages with silence even while the agent speaks. Generated audio is independently bounded and paced in 160-byte/20 ms telephone packets. Scheduler stalls are reported and rebased, never caught up in bursts. No trusted provider capture timestamp is available: this is not packet-loss reconstruction. Overflow fails the bridge safely rather than dropping arbitrary words.

Marks are sent about every 200 ms of sent audio with a capped ledger. Ack RTT and outstanding audio are **provider acknowledgement estimates**, not measured audible playback. Optional `MEDIA_INTERRUPTION_MODE=sustained_energy` conservatively clears/suppresses telephone playback during sustained caller energy; it is **off by default**, not semantic VAD/ASR or echo cancellation. Short backchannels, quiet corrections and echo require listening tests. PersonaPlex, Opus and resamplers remain continuous: no per-turn/interruption model reset or reconnect. Provider stop remains authoritative; queued/residual audio is discarded, never flushed. A send already in flight cannot be retracted.

When recording is enabled, existing raw concatenated `.ulaw` semantics remain unchanged; a private capped 16 MiB timeline JSONL aligns caller arrival, model input/synthetic silence, model audio arrival, sent agent audio and clear/mark events. It does not prove what the caller heard. Recording off creates no sidecar. Protect Windows directories with private ACLs and establish consent/retention before collecting audio.

See the [VM setup and rollout runbook](docs/personaplex-vm-runbook.md), [integration plan](docs/personaplex-integration-plan.md) and [English evaluation instructions](examples/personaplex-eval/README.md). `python -m voice_harness.evaluation` defaults to isolated no-network dry runs without `.env`, service database or native codec imports. `baseline` is byte-identical to the production prompt; `concise_v1` is explicitly deferred rather than weakening policy. Synthetic fixtures are transport-only. No conversational quality or latency improvement is established by offline tests.

Local contract verification: see `../docs/api/voice-service.verification.json` for mock contract checks and measured prompt/URL/request-line budgets. `../docs/qa/voice-harness-poc-verification.json` preserves the earlier POC verification baseline. Native Opus/resampling and real ViH/PersonaPlex interoperability still require the GPU VM.

Nginx must proxy `/calls/` to `MEDIA_HOST:MEDIA_PORT`. Proxy `/webhooks/vih/cdr` to the actual `CONTROL_HOST:CONTROL_PORT` only when CDR is enabled; edit the example's loopback target if the control listener is bound to the GPU VM Tailscale address.
