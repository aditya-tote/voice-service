"""Provider diagnostics preserve debugging signal without provider payloads."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_vih_poc import body, settings

from voice_harness.call_service import CallService
from voice_harness.diagnostics import (
    obd_request_summary,
    obd_response_summary,
    safe_summary,
    stream_event_summary,
)
from voice_harness.observability import SafeJsonFormatter, log_event
from voice_harness.vih_media import create_vih_media_app


def test_obd_summaries_keep_fixed_diagnostics_and_discard_provider_content():
    request = {
        "ukey": "provider-secret", "sourcetype": "1", "sendnow": "0", "campaigntype": "4",
        "filetype": "2", "serviceno": "7971000000", "ivrtemplateid": "200",
        "retryatmpt": "0", "retryduration": "15",
        "msisdnlist": [{
            "phoneno": "9876543210",
            "param1": "wss://bridge.example.com/calls/call_" + "a" * 36 + "?token=one-use-token",
        }],
    }
    request_summary = obd_request_summary(request, "wss://bridge.example.com")
    assert request_summary == {
        "sourcetype": "1", "sendnow": "0", "campaigntype": "4", "filetype": "2",
        "ivrtemplateid": "200", "retryatmpt": "0", "retryduration": "15",
        "ukey": "[redacted]", "serviceno": "[redacted]", "phoneno": "[redacted]",
        "param1": "[redacted]", "ukey_present": True, "service_number_present": True,
        "phone_present": True, "param1_present": True, "recipient_count": 1, "phone_digits": 10,
        "phone_format": "india_national", "wss_scheme": "wss", "wss_route": "/calls/{call_id}",
        "token_present": True, "wss_matches_public_base": True,
    }

    response_summary = obd_response_summary({
        "status": "SUCCESS", "value": "accepted", "message": "campaign https://vih.example/secret",
        "error": "invalid ukey provider-secret", "callSid": "CA-private", "campaignId": "CMP-private",
    })
    assert response_summary == {
        "status": "success", "value": "accepted", "body_type": "object", "message_present": True,
        "error_present": True, "call_id_present": True, "campaign_id_present": True,
        "reason_code": "authentication_rejected",
    }
    rendered = json.dumps({"request": request_summary, "response": response_summary})
    for private in ("provider-secret", "7971000000", "9876543210", "one-use-token", "vih.example", "CA-private"):
        assert private not in rendered


def test_stream_summary_and_log_formatter_revalidate_nested_provider_data():
    event = {
        "event": "media", "sequenceNumber": "2", "streamSid": "MZ-private",
        "media": {"payload": "base64-audio-private"},
        "start": {"callSid": "CA-private", "accountSid": "AC-private", "customParameters": {"token": "x"}},
    }
    assert stream_event_summary(event) == {
        "event": "media", "sequence_number": 2, "stream_id_present": True, "call_id_present": True,
        "account_id_present": True, "custom_parameters_present": True, "payload_present": True,
        "reason_present": False,
    }
    injected = safe_summary("provider_response", {
        "body_type": "object", "status": "success", "message_present": True,
        "error": "provider-secret", "url": "https://vih.example/?token=one-use-token",
    })
    assert injected == {"body_type": "object", "status": "success", "message_present": True}

    record = logging.LogRecord("voice_harness", logging.INFO, __file__, 1, "raw provider payload", (), None)
    record.safe_event = {
        "event": "obd_response",
        "provider_response": {
            **injected, "message": "https://vih.example/?token=one-use-token",
            "campaignId": "CMP-private", "nested": {"ukey": "provider-secret"},
        },
    }
    rendered = SafeJsonFormatter().format(record)
    assert json.loads(rendered)["provider_response"] == injected
    for private in ("provider-secret", "one-use-token", "vih.example", "CMP-private", "raw provider payload"):
        assert private not in rendered


def test_log_event_drops_raw_nested_request_echoes(caplog):
    caplog.set_level(logging.INFO, logger="voice_harness")
    log_event("obd_request", provider_request={
        "phoneno": "[redacted]", "param1": "[redacted]", "recipient_count": 1,
        "request": {"phoneno": "9876543210", "ukey": "provider-secret"},
        "prompt": "sales script", "url": "wss://bridge.example.com/?token=one-use-token",
    })
    safe = next(record.safe_event for record in caplog.records if record.name == "voice_harness")
    assert safe["provider_request"] == {"phoneno": "[redacted]", "param1": "[redacted]", "recipient_count": 1}
    rendered = json.dumps(safe)
    for private in ("9876543210", "provider-secret", "sales script", "one-use-token", "bridge.example.com"):
        assert private not in rendered


@pytest.mark.parametrize(
    ("path", "expected_route", "expected_code"),
    [
        ("/calls/call_" + "a" * 36 + "?token=not-a-real-token", "/calls/{call_id}", "call_not_found"),
        ("/stream?token=not-a-real-token", "/stream", "static_endpoint_disabled"),
        ("/unknown/path?token=not-a-real-token", "unknown", None),
    ],
)
def test_media_app_logs_all_connection_attempt_routes_without_tokens(
    tmp_path, caplog, path, expected_route, expected_code,
):
    app = create_vih_media_app(CallService(settings(tmp_path, media_auth_mode="query")))
    caplog.set_level(logging.INFO, logger="voice_harness")
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect), client.websocket_connect(path):
        pass

    events = [record.safe_event for record in caplog.records if record.name == "voice_harness"]
    attempt = next(event for event in events if event["event"] == "media_connection_attempt")
    assert attempt == {
        "event": "media_connection_attempt", "stage": "media_auth", "route": expected_route,
        "auth_mode": "query", "query_token_present": True, "authorization_present": False,
        "token_present": True,
        **({"call_id": "call_" + "a" * 36} if expected_route == "/calls/{call_id}" else {}),
    }
    assert any(event["event"] == "media_connection_closed" and event["route"] == expected_route for event in events)
    if expected_code:
        rejected = next(event for event in events if event["event"] == "media_rejected")
        assert rejected["error_code"] == expected_code
    rendered = json.dumps(events)
    assert "not-a-real-token" not in rendered


@pytest.mark.parametrize("case", ["missing_token", "wrong_token", "unavailable"])
def test_media_rejection_logs_fixed_reason_without_credentials(tmp_path, caplog, case):
    calls = CallService(settings(tmp_path, media_auth_mode="query"))
    call, _ = calls.repository.create_or_replay("diagnostic-call", body(), False)
    calls.repository.update(call["id"], state="recovery_required" if case == "unavailable" else "dial_submitted")
    calls.repository.set_media_token(call["id"], "private-real-token")
    path = "/calls/" + call["id"]
    if case != "missing_token":
        path += "?token=" + ("private-real-token" if case == "unavailable" else "wrong-private-token")
    caplog.set_level(logging.INFO, logger="voice_harness")
    with TestClient(create_vih_media_app(calls)) as client, pytest.raises(WebSocketDisconnect), client.websocket_connect(path):
        pass
    records = [record for record in caplog.records if record.name == "voice_harness"]
    rejected = next(record.safe_event for record in records if record.safe_event["event"] == "media_rejected")
    assert rejected["error_code"] == ("media_unavailable" if case == "unavailable" else "invalid_media_token")
    assert rejected["call_id"] == call["id"]
    rendered = "\n".join(SafeJsonFormatter().format(record) for record in records)
    assert "private-real-token" not in rendered and "wrong-private-token" not in rendered
    calls.repository.connection.close()


def test_media_http_and_excessive_query_do_not_leak_or_break_logging(tmp_path, caplog):
    calls = CallService(settings(tmp_path))
    caplog.set_level(logging.INFO, logger="voice_harness")
    with TestClient(create_vih_media_app(calls)) as client:
        assert client.get("/private/9876543210?ukey=provider-secret").status_code == 404
        query = "&".join("field" + str(index) + "=private-data" for index in range(101))
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/unknown?" + query):
            pass
    records = [record for record in caplog.records if record.name == "voice_harness"]
    assert any(record.safe_event["event"] == "media_http_response" and record.safe_event["status_code"] == 404
               for record in records)
    assert any(record.safe_event["event"] == "media_connection_attempt" for record in records)
    rendered = "\n".join(SafeJsonFormatter().format(record) for record in records)
    assert all(private not in rendered for private in ("9876543210", "provider-secret", "private-data"))
    calls.repository.connection.close()
