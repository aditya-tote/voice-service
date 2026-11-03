"""Outbound side-effect, correlation, and recovery regressions."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_vih_media import start_event
from test_vih_poc import body, settings

from voice_harness import vih_client
from voice_harness.call_service import CallService
from voice_harness.controller_app import create_controller_app
from voice_harness.models import StartCall
from voice_harness.session_store import SessionStore
from voice_harness.vih_client import DialResult, VihClient, _safe_response, dial_payload


def live_settings(tmp_path):
    return settings(tmp_path, media_auth_mode="query", vih_dial_mode="live",
                    enable_live_dials=True, vih_obd_url="https://vih.example/dial",
                    vih_ukey="provider-secret-key", vih_service_no="service",
                    vih_template_id="wss-template", vih_campaign_type="3",
                    vih_wss_value_mode="confirmed_dynamic",
                    vih_wss_value="wss://bridge.example.com/calls/{callId}?token={token}",
                    vih_response_call_id_path="/callSid", vih_cdr_auth_mode="bearer",
                    vih_cdr_secret="c" * 32)


def test_provider_phone_format_keeps_agent_request_canonical(tmp_path):
    config = settings(tmp_path, vih_phone_format="india_national")
    assert dial_payload(config, "+919876543210")["msisdnlist"][0]["phoneno"] == "9876543210"
    with pytest.raises(ValueError):
        dial_payload(config, "+12025550123")
    assert dial_payload(settings(tmp_path), "+919876543210")["msisdnlist"][0]["phoneno"] == "+919876543210"


@pytest.mark.asyncio
async def test_admission_before_await_prevents_concurrent_dials(tmp_path, monkeypatch):
    calls = CallService(live_settings(tmp_path))
    checking, proceed = asyncio.Event(), asyncio.Event()
    dial_count = 0

    async def ready():
        checking.set()
        await proceed.wait()
        return True

    async def dial(*_):
        nonlocal dial_count
        dial_count += 1
        return DialResult("unknown", {"error": "timeout"})

    monkeypatch.setattr(calls, "_runtime_ready", ready)
    monkeypatch.setattr(calls.vih, "dial", dial)
    request = StartCall.model_validate(body())
    first = asyncio.create_task(calls.start(request, "concurrent-one"))
    await checking.wait()
    second = await calls.start(request, "concurrent-two")
    assert second.state == "failed"
    assert calls.result(second.callId)["dial"]["status"] == "gpu_unavailable"
    proceed.set()
    created = await first
    assert dial_count == 1 and created.state == "dial_unknown"
    replay = await calls.start(request, "concurrent-one")
    assert replay.callId == created.callId and dial_count == 1
    calls.repository.connection.close()
    restarted = CallService(live_settings(tmp_path))
    assert restarted.result(created.callId)["state"] == "recovery_required"
    third = await restarted.start(request, "concurrent-three")
    assert restarted.result(third.callId)["dial"]["status"] == "gpu_unavailable"
    restarted.repository.connection.close()


@pytest.mark.asyncio
async def test_model_unavailable_never_dials(tmp_path, monkeypatch):
    calls = CallService(live_settings(tmp_path))

    async def ready():
        return False

    async def forbidden(*_):
        raise AssertionError("unready model must not trigger a phone call")

    monkeypatch.setattr(calls, "_runtime_ready", ready)
    monkeypatch.setattr(calls.vih, "dial", forbidden)
    created = await calls.start(StartCall.model_validate(body()), "unready-model")
    assert created.state == "failed"
    assert calls.repository.get(created.callId)["gpu_lease"] == 0
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_cdr_arriving_before_http_response_is_reconciled(tmp_path, monkeypatch):
    calls = CallService(live_settings(tmp_path))

    async def ready():
        return True

    async def dial(*_):
        assert calls.cdr({"event": "cdr", "callSid": "CA-fast", "disposition": "NO_ANSWER"})[0] is None
        return DialResult("submitted", {"body": {"callSid": "CA-fast"}})

    monkeypatch.setattr(calls, "_runtime_ready", ready)
    monkeypatch.setattr(calls.vih, "dial", dial)
    created = await calls.start(StartCall.model_validate(body()), "fast-no-answer")
    result = calls.result(created.callId)
    assert result["complete"] and result["disposition"] == "NO_ANSWER"
    assert calls.repository.get(created.callId)["gpu_lease"] == 0
    calls.repository.connection.close()


@pytest.mark.asyncio
async def test_early_stream_provider_ids_survive_missing_dial_response_id(tmp_path, monkeypatch):
    calls = CallService(live_settings(tmp_path))

    async def ready():
        return True

    async def dial(phone, wss):
        parsed = urlsplit(wss)
        call_id = parsed.path.rsplit("/", 1)[1]
        await calls.claim_media(call_id, parse_qs(parsed.query)["token"][0])
        await calls.bind_stream(call_id, start_event())
        return DialResult("submitted", {"body": {"accepted": True}})

    monkeypatch.setattr(calls, "_runtime_ready", ready)
    monkeypatch.setattr(calls.vih, "dial", dial)
    created = await calls.start(StartCall.model_validate(body()), "early-stream-ids")
    result = calls.result(created.callId)
    assert result["state"] == "live" and result["provider"]["callSid"] == "CA-test"
    assert calls.repository.get(created.callId)["gpu_lease"] == 1
    calls.repository.connection.close()


def test_recovery_cdr_clears_recovery_flag_and_conflicting_ids_do_not_release(tmp_path):
    config = settings(tmp_path)
    service = CallService(config)
    first, _ = service.repository.create_or_replay("recovery-first", body(), False)
    second, _ = service.repository.create_or_replay("recovery-second", body(), False)
    service.repository.update(first["id"], state="dial_submitted", gpu_lease=1, call_sid="CA-first", stream_sid="MZ-first")
    service.repository.update(second["id"], call_sid="CA-second", stream_sid="MZ-second")
    service.repository.connection.close()
    service = CallService(config)
    # Contradictory stream/call identities must not complete either call.
    service.cdr({"event": "cdr", "callSid": "CA-first", "streamSid": "MZ-second", "disposition": "ANSWERED"})
    assert not service.result(first["id"])["complete"]
    assert service.repository.get(first["id"])["gpu_lease"] == 1
    service.cdr({"event": "cdr", "callSid": "CA-first", "streamSid": "MZ-first", "disposition": "NO_ANSWER"})
    result = service.result(first["id"])
    assert result["complete"] and result["recoveryRequired"] is False
    assert service.repository.get(first["id"])["gpu_lease"] == 0
    service.repository.connection.close()


def test_raw_cdr_preserves_provider_field_names_and_nulls(tmp_path):
    config = settings(tmp_path, vih_cdr_auth_mode="bearer", vih_cdr_secret="c" * 32)
    service = CallService(config)
    call, _ = service.repository.create_or_replay("raw-cdr-test", body(), False)
    service.repository.update(call["id"], call_sid="CA-raw")
    client = TestClient(create_controller_app(SessionStore(config), service))
    payload = {"event": "cdr", "callSid": "CA-raw", "disposition": "NO_ANSWER",
               "from": "+911111111111", "callAnswerTime": None}
    response = client.post("/webhooks/vih/cdr", headers={"Authorization": "Bearer " + "c" * 32}, json=payload)
    assert response.status_code == 200
    assert service.result(call["id"])["cdr"] == payload
    assert client.post("/webhooks/vih/cdr", headers={"Authorization": "Bearer " + "c" * 32,
                                                  "Content-Length": "invalid"}, content="{}").status_code == 400
    service.repository.connection.close()


def test_response_redacts_tokens_inside_escaped_urls_and_arbitrary_fields():
    token = "private-media-token"
    url = "wss://bridge.example/calls/id?token=" + token
    raw = json.dumps({"echo": url.replace("/", "\\/"), "credentialEcho": token}).encode()
    safe = _safe_response(raw, (url, token))
    assert token not in json.dumps(safe)


@pytest.mark.asyncio
async def test_orphan_campaign_cdr_replays_and_stream_replay_retains_lease(tmp_path):
    service = CallService(settings(tmp_path))
    record, _ = service.repository.create_or_replay("campaign-orphan", body(), False)
    identifier = record["id"]
    payload = {"event": "cdr", "callSid": "CA-orphan", "campaignId": "CAMP-one",
               "disposition": "NO_ANSWER"}
    assert service.cdr(payload)[0] is None
    service.repository.bind_provider_ids(identifier, campaign_id="CAMP-one")
    assert service.result(identifier)["complete"]
    assert service.result(identifier)["provider"]["callSid"] == "CA-orphan"
    record, _ = service.repository.create_or_replay("stream-orphan", body(), False)
    identifier = record["id"]
    service.repository.update(identifier, state="dial_submitted", gpu_lease=1)
    service.repository.set_media_token(identifier, "private-once")
    service.cdr({"event": "cdr", "callSid": "CA-test", "streamSid": "MZ-test", "disposition": "ANSWERED"})
    await service.claim_media(identifier, "private-once")
    await service.bind_stream(identifier, start_event())
    assert service.repository.get(identifier)["gpu_lease"] == 1
    assert not service.result(identifier)["complete"]
    await service.finish_media(identifier, "provider_stop", "Agent text", {}, {})
    assert service.result(identifier)["complete"] and service.repository.get(identifier)["gpu_lease"] == 0
    # A differing terminal delivery is retained without changing the original outcome.
    service.cdr({"event": "cdr", "callSid": "CA-test", "streamSid": "MZ-test", "disposition": "FAILED"})
    assert service.result(identifier)["disposition"] == "ANSWERED"
    service.repository.connection.close()


@pytest.mark.parametrize("scenario", ["server_error", "oversized", "timeout", "array"])
def test_real_http_results_are_bounded_and_never_retried(tmp_path, monkeypatch, scenario):
    seen = []
    payload = {"server_error": b'{"error":"unavailable"}', "oversized": b"x" * 10000,
               "timeout": b"{}", "array": b'[{"callSid":"CA-array"}]'}[scenario]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers["Content-Length"])))
            if scenario == "timeout":
                time.sleep(1.5)
            self.send_response(503 if scenario == "server_error" else 200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = settings(tmp_path, vih_obd_url=f"http://127.0.0.1:{server.server_port}")
    # A local HTTP stand-in is never a live deploy configuration.
    object.__setattr__(config, "vih_dial_mode", "live")
    if scenario == "timeout":
        monkeypatch.setattr(vih_client, "HTTP_DEADLINE_SECONDS", 1.0)
    try:
        result = asyncio.run(VihClient(config).dial(body()["customerPhone"], "test-stream"))
        assert result.status == ("submitted" if scenario == "array" else "unknown") and len(seen) == 1
        assert len(json.dumps(result.body)) < 8500
        if scenario == "array":
            assert VihClient(config).response_id("/0/callSid", result.body) == "CA-array"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
