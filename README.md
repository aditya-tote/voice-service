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
$payload = @{ leadId='lead_123'; customerPhone='+919876543210'; script='Introduce the approved offer and ask for a follow-up.'; context=@{persona='Sales representative';goal='Book a follow-up';tone='Calm and concise';facts=@('Asked about pricing');limits=@('Do not promise a discount')}; voiceName='NATF2.pt'; metadata=@{source='manual-poc'} } | ConvertTo-Json -Depth 5
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

The result contains `callId`, `simulated`, `state`, bounded dial data, provider IDs when known, CDR, `agentText`, `customerTranscript: null`, `complete`, and correlation status. Reusing the same key and identical body returns the existing call. Changing the body with that key returns 409 and never creates another attempt.

Example mock result shape:

```json
{"callId":"call_...","simulated":true,"state":"completed","dial":{"request":{"mode":"mock"},"status":"simulated","response":{"mode":"mock","accepted":true}},"provider":{"callSid":null,"campaignId":null,"streamSid":null},"cdr":null,"agentText":"","customerTranscript":null,"complete":true,"correlationStatus":"pending"}
```

## Linux / GPU VM

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[runtime,dev]'
set -a; . ./.env; set +a
.venv/bin/voice-harness
```

Use one worker. `GET /health/ready` verifies runtime packages and the local PersonaPlex TCP port only; it does not prove a model can produce audio. Run `voice-harness-probe NATF2.pt` on the GPU VM before a controlled phone test.

The probe streams paced silence continuously while receiving model output and decoding it back to PCM. Its output deadline is 30 seconds after the handshake by default; `voice-harness-probe NATF2.pt --timeout 45` changes it. A timeout reports sent Opus packets, received audio, decoded bytes and text frame counts. Silence does not guarantee a spoken response, so inspect model logs if audio arrives without text. The earlier 60 ms input burst was insufficient for a streaming test.

## Enabling ViH only after provider confirmation

`BRIDGE_API_KEY` authorizes the agent/manual caller. `VIH_UKEY` is only sent in ViH's OBD request. `VIH_CDR_SECRET` is separate and validates ViH's CDR callback. Never reuse them.

Set `ENABLE_LIVE_DIALS=true` and `VIH_DIAL_MODE=live` after ViH confirms the HTTPS OBD endpoint, service number, streaming template, approved campaign type, phone format, CDR authentication, and a returned call or campaign ID field. Configure that field with `VIH_RESPONSE_CALL_ID_PATH` or `VIH_RESPONSE_CAMPAIGN_ID_PATH`; this correlates calls that never open a stream. For this POC, `VIH_WSS_VALUE_MODE=confirmed_dynamic`, `MEDIA_AUTH_MODE=query`, and `VIH_WSS_VALUE` must exactly be `wss://your-public-origin/calls/{callId}?token={token}`. VAH blocks startup without these values. It sends one recipient with `sendnow=0`, `filetype=2`, and provider retry `0`; an uncertain submission is `dial_unknown` and is never automatically redialed.

The guide does not establish whether `msisdnlist[].WSS` accepts a full dynamic URL. ViH must confirm this account supports it. A static WSS reference needs an additional provider-specific correlation adapter; `/stream` currently rejects connections.

Register `https://your-public-origin/webhooks/vih/cdr` with ViH. Choose bearer authentication if ViH can send `Authorization: Bearer <VIH_CDR_SECRET>`; otherwise use the confirmed query-token callback URL. Do not use `BRIDGE_API_KEY` as a provider credential. Set `RECORDING_DIR=recordings` to retain both audio directions. Recordings contain audio sent/received at the bridge; playback marks provide separate acknowledgement, and audio queued when the call ends may not have played.

The provided ViH guide documents JSON WebSocket events with Base64 G.711 mu-law 8 kHz media. VAH includes a strict adapter for that protocol and the existing PersonaPlex codec boundary. A live stream is intentionally not enabled until ViH confirms how an OBD call maps to the public WSS path/credential and how its CDR is authenticated. Customer transcription needs a separate STT provider, so the POC returns `customerTranscript: null`.

## Routes

| Route | Authentication | Purpose |
|---|---|---|
| `POST /v1/calls` | Bridge bearer + `Idempotency-Key` | Agent-tool-shaped start request; mock completes locally. |
| `GET /v1/calls/{id}` | Bridge bearer | Durable raw JSON result. |
| `POST /webhooks/vih/cdr` | Confirmed provider bearer/query secret | Stores/reconciles terminal CDR; disabled by default. |
| `WS /calls/{callId}` | One-use query media token | ViH JSON/Base64 μ-law stream, forwarded to local PersonaPlex. |

`DATABASE_PATH` holds requests and outcomes with SQLite WAL. On restart in-flight live records become `recovery_required`; VAH never auto-redials. The CDR callback is idempotent. A CDR with no known `callSid`/`streamSid` is retained as an orphan rather than guessed from the phone number.

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
| `MEDIA_AUTH_MODE` | `header` | Legacy session media mode; live ViH requires `query`. |
| `SESSION_TTL_SECONDS`, `MAX_STORED_SESSIONS` | `300`, `1000` | Legacy in-memory session bounds. |
| `PERSONAPLEX_URL`, `ALLOWED_VOICES` | local URL, required | Local model endpoint and allowlisted `.pt` voice names. |
| `DATABASE_PATH` | `voice-harness.sqlite3` | SQLite WAL file for durable POC state. |
| `VIH_DIAL_MODE`, `ENABLE_LIVE_DIALS` | `mock`, `false` | Mock is safe; both must explicitly enable live dialing. |
| `VIH_OBD_URL`, `VIH_UKEY`, `VIH_SERVICE_NO`, `VIH_TEMPLATE_ID` | empty | Account-specific OBD endpoint and credentials/settings. |
| `VIH_CAMPAIGN_TYPE` | empty | Account-approved `3` promotional or `4` transactional purpose. |
| `VIH_PHONE_FORMAT` | `e164` | Confirm the account format. `e164` sends the agent's `+91...` unchanged; `india_national` sends the ten national digits and rejects non-Indian numbers. The agent API always accepts E.164. |
| `VIH_WSS_VALUE_MODE`, `VIH_WSS_VALUE` | `unconfirmed`, empty | Live requires `confirmed_dynamic` and exact `wss://origin/calls/{callId}?token={token}`. Keep braces unquoted in `.env`; `set -a; . ./.env; set +a` preserves them. |
| `VIH_CDR_AUTH_MODE`, `VIH_CDR_SECRET` | `disabled`, empty | Confirmed provider bearer or query authentication, with a distinct 32+ character secret. |
| `VIH_RESPONSE_CALL_ID_PATH`, `VIH_RESPONSE_CAMPAIGN_ID_PATH` | empty | Hypothetical, account-confirmed JSON Pointer such as `/data/callSid` or `/data/campaignId`; do not assume these paths from the guide. One must be set for live `NO_ANSWER` correlation. |
| `RECORDING_DIR` | empty | Optional service-account-only directory for caller/agent μ-law recordings. |
| `MAX_FRAME_BYTES`, `MAX_TRANSCRIPT_CHARS` | `16384`, `32768` | Per-provider-frame and stored agent-text limits. |
| `PERSONAPLEX_READY_TIMEOUT_SECONDS`, `IDLE_TIMEOUT_SECONDS`, `CLOSE_TIMEOUT_SECONDS`, `MAX_CALL_SECONDS` | `15`, `60`, `5`, `1800` | Model handshake, media inactivity, teardown, and call duration deadlines. |
| `CONTROLLER_CALLBACK_BASE_URL`, `CONTROLLER_API_KEY` | legacy defaults | Existing `/sessions` bridge callback only; new `/v1/calls` results are polled and do not use it. |

Legacy session and controller settings apply only to isolated compatibility helpers; the deployed CLI disables `/sessions`. New calls do not call a controller or an agent callback. A future agent can poll and consume the same raw result returned in this POC.

Live agent text, recording references, and counters are checkpointed every two seconds and saved again at cleanup. An abrupt process failure can lose the most recent checkpoint interval and cannot resume a live phone connection. In-flight calls retain their reservation after restart until an authoritative CDR reconciles them; do not clear that reservation and redial while the provider outcome remains unknown. Conflicting and unmatched CDRs remain in SQLite `provider_events` for review. SQLite and recording files are private on POSIX; configure service-account ACLs on Windows. Set a retention policy before longer use.

The OBD HTTP client has a 15-second overall deadline, bounded response storage, and no automatic retry. GPU admission occurs before dialing; readiness checks prove package availability and a TCP listener, so perform the model probe on the GPU VM too.

Playback `mark` acknowledgements are counted, and `clear` framing is supported and used during failure teardown. Full interruption handling still needs a caller-VAD/model-reset policy; it is not enabled automatically. No audio is emitted after the provider's `stop`, so queued audio and a residual final fragment are discarded at call termination.

Local verification: 69 tests passed, 2 runtime-dependent test modules skipped, Ruff and compile checks passed, and the actual CLI/HTTP/restart smoke check passed. Native Opus/resampling and real ViH/PersonaPlex interoperability still require the GPU VM. See `../docs/qa/voice-harness-poc-verification.json` for coverage and limits.

Nginx must proxy `/calls/` to `MEDIA_HOST:MEDIA_PORT` and `/webhooks/vih/cdr` to the actual `CONTROL_HOST:CONTROL_PORT`; edit the example's loopback target if the control listener is bound to the GPU VM Tailscale address.
