import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from voice_harness.call_service import CallService
from voice_harness.config import Settings
from voice_harness.controller_app import create_controller_app
from voice_harness.session_store import SessionStore
from voice_harness.vih_client import VihClient, redacted_payload
from voice_harness.vih_protocol import VihProtocolError, VihStream, pcm_to_mulaw_messages


def settings(tmp_path, **changes):
    values = {
        "control_host": "127.0.0.1", "control_port": 8080, "media_host": "127.0.0.1", "media_port": 8081,
        "public_stream_base_url": "wss://bridge.example.com", "bridge_api_key": "a" * 32, "media_auth_mode": "header",
        "session_ttl_seconds": 30, "max_stored_sessions": 2, "personaplex_url": "ws://127.0.0.1:8998/api/chat",
        "allowed_voices": frozenset({"NATF2.pt"}), "database_path": str(tmp_path / "calls.sqlite3"),
    }
    values.update(changes)
    return Settings(**values)


def body():
    return {"leadId": "lead", "customerPhone": "+919876543210", "script": "Introduce our product.",
            "context": {"persona": "sales", "goal": "book", "tone": "calm", "facts": [], "limits": []},
            "voiceName": "NATF2.pt", "metadata": {"source": "manual-poc"}}


def test_mock_call_is_durable_idempotent_and_safe(tmp_path):
    config = settings(tmp_path)
    app = create_controller_app(SessionStore(config), CallService(config))
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + "a" * 32, "Idempotency-Key": "manual-poc-0001"}
    first = client.post("/v1/calls", headers=headers, json=body())
    assert first.status_code == 202 and first.json()["simulated"]
    second = client.post("/v1/calls", headers=headers, json=body())
    assert second.json()["callId"] == first.json()["callId"]
    changed = body(); changed["script"] = "different"
    assert client.post("/v1/calls", headers=headers, json=changed).status_code == 409
    result = client.get(first.json()["resultUrl"], headers=headers).json()
    assert result["complete"] and result["customerTranscript"] is None and "ukey" not in str(result).lower()
    # A new process sees the same result from SQLite.
    restarted = create_controller_app(SessionStore(config), CallService(config))
    assert TestClient(restarted).get(first.json()["resultUrl"], headers=headers).json()["callId"] == first.json()["callId"]


def test_cdr_auth_and_idempotent_correlation(tmp_path):
    secret = "c" * 32
    config = settings(tmp_path, vih_cdr_auth_mode="bearer", vih_cdr_secret=secret)
    service = CallService(config)
    response = TestClient(create_controller_app(SessionStore(config), service)).post(
        "/v1/calls", headers={"Authorization": "Bearer " + "a" * 32, "Idempotency-Key": "manual-poc-0002"}, json=body()
    )
    call_id = response.json()["callId"]
    service.repository.update(call_id, call_sid="CA1")
    client = TestClient(create_controller_app(SessionStore(config), service))
    payload = {"event": "cdr", "callSid": "CA1", "disposition": "ANSWERED", "webhookTimestamp": "first"}
    assert client.post("/webhooks/vih/cdr", json=payload).status_code == 401
    accepted = client.post("/webhooks/vih/cdr", headers={"Authorization": "Bearer " + secret}, json=payload)
    assert accepted.json()["callId"] == call_id
    payload["webhookTimestamp"] = "retry"
    assert client.post("/webhooks/vih/cdr", headers={"Authorization": "Bearer " + secret}, json=payload).json()["duplicate"]


def test_vih_mu_law_protocol_is_strict_and_uses_160_byte_output():
    stream = VihStream()
    stream.ingest('{"event":"connected"}')
    stream.ingest('{"event":"start","sequenceNumber":"1","streamSid":"MZ1","start":{"streamSid":"MZ1","callSid":"CA1","accountSid":"AC1","mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"bitDepth":8,"bitRate":64}}}')
    payload = base64.b64encode(b"\xff" * 160).decode()
    name, pcm = stream.ingest('{"event":"media","sequenceNumber":"2","streamSid":"MZ1","media":{"payload":"' + payload + '"}}')
    assert name == "media" and len(pcm) == 320
    output = pcm_to_mulaw_messages(b"\0\0" * 200, "MZ1")
    assert len(output) == 1 and len(base64.b64decode(__import__("json").loads(output[0])["media"]["payload"])) == 160
    try:
        stream.ingest('{"event":"media","sequenceNumber":"3","streamSid":"MZ1","media":{"payload":"bad!"}}')
        assert False
    except VihProtocolError:
        pass


def test_live_vih_client_serializes_documented_fields_against_loopback(tmp_path):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"campaign":"ok"}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        # Deliberately bypasses the production HTTPS/live interlock only to exercise a local HTTP stand-in.
        config = settings(tmp_path, vih_obd_url=f"http://127.0.0.1:{server.server_port}", vih_ukey="u-key", vih_service_no="service", vih_template_id="template", vih_wss_value="confirmed")
        object.__setattr__(config, "vih_dial_mode", "live")
        result = asyncio.run(VihClient(config).dial("+919876543210"))
    finally:
        server.shutdown(); thread.join()
    assert result.status == "submitted"
    assert seen["sendnow"] == "0" and seen["retryatmpt"] == "0" and seen["filetype"] == "2"
    assert seen["msisdnlist"] == [{"phoneno": "+919876543210", "WSS": "confirmed"}]


def test_redacted_dial_request_never_retains_dynamic_wss_token():
    redacted = redacted_payload({"ukey": "secret", "msisdnlist": [{"phoneno": "+91", "WSS": "wss://x/?token=one-use"}]})
    assert "one-use" not in str(redacted) and redacted["ukey"] == "[redacted]"


def test_live_mode_rejects_static_or_disabled_calling_configuration(tmp_path):
    with pytest.raises(ValueError, match="ENABLE_LIVE_DIALS"):
        settings(tmp_path, vih_dial_mode="live")
    with pytest.raises(ValueError, match="confirmed dynamic"):
        settings(
            tmp_path, vih_dial_mode="live", enable_live_dials=True, vih_obd_url="https://vih.example",
            vih_ukey="u", vih_service_no="service", vih_template_id="template", vih_campaign_type="3",
            vih_wss_value_mode="confirmed_static", vih_wss_value="static", vih_cdr_auth_mode="bearer",
            vih_cdr_secret="c" * 32, vih_response_call_id_path="/callSid",
        )
