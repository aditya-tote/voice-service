"""Tests for the intentionally allowlisted operational log boundary."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from voice_harness.call_service import CallService
from voice_harness.config import Settings
from voice_harness.controller_app import create_controller_app
from voice_harness.observability import SafeJsonFormatter, log_event
from voice_harness.session_store import SessionStore


def run_logging_process(code, **environment):
    env = os.environ.copy()
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), **environment)
    return subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True,
                          check=False, timeout=15)


def test_configured_stdout_sanitizes_dependency_debug_and_exceptions():
    process = run_logging_process('''
import logging
from voice_harness.observability import configure_logging, log_event
configure_logging("DEBUG")
log_event("model_ready", stage="model_connect", call_id="call_" + "a" * 36,
          token="media-secret", phone="+919876543210", prompt="private-script")
logging.getLogger("websockets.client").debug("wss://host?token=media-secret")
try:
    raise RuntimeError("https://host?ukey=provider-secret&prompt=private-script")
except RuntimeError:
    logging.getLogger("uvicorn.error").exception("Bearer bridge-secret +919876543210")
''')
    assert process.returncode == 0 and not process.stderr
    events = [json.loads(line) for line in process.stdout.splitlines()]
    assert events[0]["event"] == "model_ready"
    assert events[1]["event"] == "dependency_log" and events[1]["component"] == "uvicorn"
    for private in ("media-secret", "provider-secret", "bridge-secret", "private-script", "919876543210",
                    "https://", "wss://", "Traceback", "RuntimeError"):
        assert private not in process.stdout


def test_cli_invalid_configuration_reports_field_without_traceback():
    process = run_logging_process("from voice_harness.main import run; run()", BRIDGE_API_KEY="short-secret")
    assert process.returncode == 1 and not process.stderr
    event = json.loads(process.stdout)
    assert event["event"] == "configuration_invalid" and event["config_field"] == "BRIDGE_API_KEY"
    assert "short-secret" not in process.stdout and "Traceback" not in process.stdout


def format_record(name: str, message: str, *, level: int = logging.INFO, extra: dict | None = None,
                  exc_info=None) -> dict:
    record = logging.LogRecord(name, level, __file__, 1, message, (), exc_info)
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return json.loads(SafeJsonFormatter().format(record))


def test_structured_event_keeps_only_safe_trace_fields():
    call_id = "call_" + "a" * 36
    request_id = "b" * 32
    event = format_record(
        "voice_harness",
        "raw message must not be rendered",
        extra={
            "safe_event": {
                "event": "dial_finished",
                "call_id": call_id,
                "request_id": request_id,
                "stage": "obd",
                "state": "dial_submitted",
                "status_code": 202,
                "duration_ms": 41.5,
                "simulated": False,
                "customerPhone": "+919876543210",
                "script": "the secret sales script",
                "authorization": "Bearer bridge-secret",
                "wss": "wss://tunnel.example/calls/x?token=one-use-token",
                "payload": {"ukey": "provider-secret"},
            }
        },
    )

    assert event["event"] == "dial_finished"
    assert event["call_id"] == call_id
    assert event["request_id"] == request_id
    assert event["stage"] == "obd"
    assert event["state"] == "dial_submitted"
    assert event["status_code"] == 202
    assert event["duration_ms"] == 41.5
    assert event["simulated"] is False
    rendered = json.dumps(event)
    for secret in ("919876543210", "sales script", "bridge-secret", "one-use-token", "provider-secret"):
        assert secret not in rendered


def test_dependency_log_hides_messages_tracebacks_and_urls():
    try:
        raise RuntimeError("provider returned https://vih.example/?ukey=provider-secret")
    except RuntimeError as error:
        event = format_record(
            "httpx", "POST https://vih.example/?token=one-use-token", level=logging.ERROR,
            exc_info=(type(error), error, error.__traceback__),
        )

    assert event["event"] == "dependency_log"
    assert event["component"] == "httpx"
    rendered = json.dumps(event)
    assert "one-use-token" not in rendered
    assert "provider-secret" not in rendered
    assert "vih.example" not in rendered
    assert "RuntimeError" not in rendered


def test_log_event_drops_invalid_ids_and_unknown_values(caplog):
    caplog.set_level(logging.INFO, logger="voice_harness")
    log_event(
        "media_finished",
        call_id="call_this-is-not-a-real-id",
        request_id="short",
        stage="provider_receive",
        state="completed",
        reason="provider_stop",
        recording_enabled=True,
        status_code=200,
        prompt="never log this",
    )

    record = next(record for record in caplog.records if record.name == "voice_harness")
    safe = record.safe_event
    assert safe["event"] == "media_finished"
    assert safe["stage"] == "provider_receive"
    assert safe["state"] == "completed"
    assert safe["reason"] == "provider_stop"
    assert safe["recording_enabled"] is True
    assert safe["status_code"] == 200
    assert "call_id" not in safe
    assert "request_id" not in safe
    assert "prompt" not in safe


def test_start_call_http_request_emits_traceable_safe_event(tmp_path, caplog):
    config = Settings(
        control_host="127.0.0.1", control_port=8080, media_host="127.0.0.1", media_port=8081,
        public_stream_base_url="wss://bridge.example.com", bridge_api_key="a" * 32,
        media_auth_mode="header", session_ttl_seconds=30, max_stored_sessions=2,
        personaplex_url="ws://127.0.0.1:8998/api/chat", allowed_voices=frozenset({"NATF2.pt"}),
        database_path=str(tmp_path / "calls.sqlite3"),
    )
    client = TestClient(create_controller_app(SessionStore(config), CallService(config)))
    body = {
        "leadId": "lead-123", "customerPhone": "+919876543210",
        "leadContext": {"customerName": "PrivateName", "companyName": "PrivateCompany",
                        "customerEmail": "private@example.com", "source": "Meta",
                        "enquiryNotes": "Never emit this approved conversation fact."},
    }
    caplog.set_level(logging.INFO, logger="voice_harness")
    response = client.post(
        "/v1/calls", json=body,
        headers={"Authorization": "Bearer " + "a" * 32, "Idempotency-Key": "logging-http-0001"},
    )

    assert response.status_code == 202
    request_id = response.headers["X-Request-ID"]
    created = next(
        record.safe_event for record in caplog.records
        if record.name == "voice_harness" and record.safe_event["event"] == "call_created"
    )
    assert created["call_id"] == response.json()["callId"]
    assert created["state"] == "created"
    assert created["simulated"] is True
    assert created["request_id"] == request_id
    finished = next(
        record.safe_event for record in caplog.records
        if record.name == "voice_harness" and record.safe_event["event"] == "api_request_finished"
    )
    assert finished["request_id"] == request_id
    rendered = "\n".join(record.getMessage() + str(record.safe_event) for record in caplog.records)
    assert "919876543210" not in rendered
    assert "approved conversation" not in rendered
    assert "Bearer " + "a" * 32 not in rendered


def test_http_lifecycle_logs_success_replay_client_errors_and_unhandled_error_without_leaks(tmp_path, caplog):
    config = Settings(
        control_host="127.0.0.1", control_port=8080, media_host="127.0.0.1", media_port=8081,
        public_stream_base_url="wss://bridge.example.com", bridge_api_key="a" * 32,
        media_auth_mode="header", session_ttl_seconds=30, max_stored_sessions=2,
        personaplex_url="ws://127.0.0.1:8998/api/chat", allowed_voices=frozenset({"NATF2.pt"}),
        database_path=str(tmp_path / "calls.sqlite3"),
    )
    service = CallService(config)
    app = create_controller_app(SessionStore(config), service)

    headers = {"Authorization": "Bearer " + "a" * 32, "Idempotency-Key": "logging-http-0002"}
    body = {
        "leadId": "lead-123", "customerPhone": "+919876543210",
        "leadContext": {"customerName": "PrivateName", "companyName": "PrivateCompany",
                        "customerEmail": "private@example.com", "source": "Meta",
                        "enquiryNotes": "private notes"},
    }
    caplog.set_level(logging.INFO, logger="voice_harness")
    client = TestClient(app, raise_server_exceptions=False)
    first = client.post("/v1/calls", headers=headers, json=body)
    replay = client.post("/v1/calls", headers=headers, json=body)
    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.headers["X-Request-ID"] != replay.headers["X-Request-ID"]
    assert client.post("/v1/calls", headers={"Idempotency-Key": "logging-http-0002"}, json=body).status_code == 401
    assert client.post("/v1/calls", headers=headers, json={}).status_code == 422
    invalid = {**body, "leadContext": {**body["leadContext"], "decisionMaker": "not-a-boolean"}}
    assert client.post("/v1/calls", headers=headers, json=invalid).status_code == 422

    async def fail_start(*_):
        raise RuntimeError("https://secret.example/?token=one-use-token")

    service.start = fail_start
    failed_headers = {**headers, "Idempotency-Key": "logging-http-0003"}
    assert client.post("/v1/calls", headers=failed_headers, json=body).status_code == 500

    events = [record.safe_event for record in caplog.records if record.name == "voice_harness"]
    assert any(event["event"] == "api_request_finished" and event["status_code"] == 202 for event in events)
    assert any(event["event"] == "call_replayed" for event in events)
    replay_event = next(event for event in events if event["event"] == "call_replayed")
    assert replay_event["request_id"] == replay.headers["X-Request-ID"]
    failed = [event for event in events if event["event"] == "api_request_failed"]
    assert {401, 422, 500}.issubset({event["status_code"] for event in failed})
    assert any(event.get("error_type") == "RuntimeError" for event in failed)
    assert all(len(event["request_id"]) == 32 for event in events if "request_id" in event)
    rendered = "\n".join(record.getMessage() + str(record.safe_event) for record in caplog.records)
    for secret in ("919876543210", "PrivateName", "PrivateCompany", "private@example.com", "private notes",
                   "one-use-token", "secret.example", "Bearer " + "a" * 32):
        assert secret not in rendered
