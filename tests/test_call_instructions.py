"""Persist either instruction source and prefer the script in the model prompt."""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_vih_poc import body, settings

from voice_harness.call_service import CallService
from voice_harness.controller_app import create_controller_app
from voice_harness.models import StartCall
from voice_harness.session_store import SessionStore
from voice_harness.vih_media import model_connection_url


@pytest.mark.parametrize("source", ["script", "context", "both"])
def test_instruction_sources_survive_api_storage_replay_and_model_prompt(tmp_path, source):
    payload = body()
    payload["script"] = "Confirm identity before discussing the enquiry."
    payload["context"]["limits"] = ["Never invent prices."]
    if source == "script":
        payload.pop("context")
    elif source == "context":
        payload.pop("script")
    config = settings(tmp_path)
    calls = CallService(config)
    headers = {"Authorization": "Bearer " + config.bridge_api_key,
               "Idempotency-Key": "instructions-test-001"}
    try:
        with TestClient(create_controller_app(SessionStore(config), calls)) as client:
            response = client.post("/v1/calls", json=payload, headers=headers)
            assert response.status_code == 202
            replay = client.post("/v1/calls", json=payload, headers=headers)
            assert replay.json()["callId"] == response.json()["callId"]
            stored = client.get(response.json()["resultUrl"], headers=headers).json()["request"]
        request = StartCall.model_validate(stored)
        query = parse_qs(urlsplit(model_connection_url(
            "ws://model/api/chat?existing=keep", request
        )).query)
        prompt = query["text_prompt"][0]
        assert query["voice_prompt"] == ["NATF2.pt"]
        assert query["existing"] == ["keep"]
        assert "Goodbye. Please hang up now." in prompt
        assert "Do not claim you disconnected the call." in prompt
        if "script" in payload:
            assert prompt.count(payload["script"]) == 1
        else:
            assert "Approved call script:" not in prompt
        if source == "context":
            assert "Never invent prices." in prompt
            assert payload["context"]["goal"] in prompt
        else:
            assert "Persona:" not in prompt
            assert "Never invent prices." not in prompt
        if "context" in payload:
            assert stored["context"] == payload["context"]
    finally:
        calls.repository.connection.close()


@pytest.mark.parametrize("instructions", [
    {}, {"script": None}, {"context": None}, {"script": None, "context": None},
    {"script": ""}, {"script": "  "}, {"context": {}},
    {"script": "x" * 24001},
])
def test_invalid_or_missing_instructions_never_create_a_call(tmp_path, instructions):
    payload = body()
    payload.pop("script")
    payload.pop("context")
    payload.update(instructions)
    config = settings(tmp_path)
    calls = CallService(config)
    try:
        with TestClient(create_controller_app(SessionStore(config), calls)) as client:
            response = client.post("/v1/calls", json=payload, headers={
                "Authorization": "Bearer " + config.bridge_api_key,
                "Idempotency-Key": "invalid-instructions-001",
            })
        assert response.status_code == 422
        assert calls.repository.connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    finally:
        calls.repository.connection.close()
