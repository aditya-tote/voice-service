"""Independent error-boundary and actual encoded WebSocket transport checks."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_call_instructions import maximum_facts
from test_vih_poc import body, settings

from voice_harness.call_service import CallService
from voice_harness.controller_app import create_controller_app
from voice_harness.personaplex import (
    MODEL_REQUEST_LINE_LIMIT,
    PersonaPlexClient,
    PromptBudgetError,
    model_connection_url,
    validate_model_budget,
)
from voice_harness.session_store import SessionStore


@pytest.mark.parametrize("failure", ["mailbox", "missing", "nested_type", "extra", "unicode", "unicode_key"])
def test_validation_errors_exclude_raw_lead_values_and_survive_invalid_unicode(tmp_path, failure):
    config = settings(tmp_path)
    calls = CallService(config)
    payload = body()
    sentinel = "PRIVATE-LEAD-VALIDATION-SENTINEL"
    payload["leadContext"]["customerName"] = sentinel
    if failure == "mailbox":
        payload["leadContext"]["customerEmail"] = sentinel
    elif failure == "missing":
        del payload["leadContext"]["source"]
    elif failure == "nested_type":
        payload["leadContext"] = [sentinel]
    elif failure == "extra":
        payload["leadContext"]["policy"] = {"private": sentinel}
    elif failure == "unicode":
        payload["leadId"] = "\ud800"
    else:
        payload["leadContext"]["\ud800"] = sentinel

    def forbidden(*_, **__):
        raise AssertionError("invalid request must not reach preparation, persistence or dial")

    calls.repository.lookup_replay = forbidden
    calls.repository.create_or_replay = forbidden
    headers = {"Authorization": "Bearer " + config.bridge_api_key,
               "Idempotency-Key": "independent-validation", "Content-Type": "application/json"}
    try:
        with TestClient(create_controller_app(SessionStore(config), calls), raise_server_exceptions=False) as client:
            # Escaped surrogates are legal JSON syntax but not valid Unicode strings.
            response = client.post("/v1/calls", content=json.dumps(payload), headers=headers)
        assert response.status_code == 422
        assert sentinel not in response.text
        details = response.json()["detail"]
        assert details and all(set(item) == {"type", "loc", "msg"} for item in details)
        assert calls.repository.connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    finally:
        calls.repository.connection.close()


@pytest.mark.asyncio
async def test_near_limit_prebuilt_url_is_the_exact_websocket_request_target():
    pytest.importorskip("websockets.asyncio.server")
    from websockets.asyncio.server import serve

    targets = []

    async def handler(socket):
        targets.append(socket.request.path)
        await socket.send(b"\x00")
        await socket.wait_closed()

    clock = datetime(2026, 11, 5, 6, 30, 0, 123456, tzinfo=UTC)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        base = f"ws://127.0.0.1:{port}/api/chat?other=keep&other=two&text_prompt=stale"
        facts = maximum_facts()
        near = None
        for count in range(401):
            candidate = facts.model_copy(update={"enquiryNotes": "界" * count + "E" * (400 - count)})
            try:
                near = model_connection_url(base, candidate, "NATF2.pt", now=clock)
            except PromptBudgetError:
                break
        assert near is not None
        split = urlsplit(near)
        query = parse_qs(split.query)
        prompt = query["text_prompt"][0]
        sizes = validate_model_budget(prompt, near)
        assert MODEL_REQUEST_LINE_LIMIT - 9 <= max(sizes) < MODEL_REQUEST_LINE_LIMIT
        client = PersonaPlexClient(near, 2)
        try:
            await client.connect()
            assert targets == [split.path + "?" + split.query]
            assert len(f"GET {targets[0]} HTTP/1.1\r\n".encode("ascii")) == sizes[1]
            assert query["other"] == ["keep", "two"]
            assert query["voice_prompt"] == ["NATF2.pt"]
            assert ".123456+05:30" in prompt
        finally:
            await client.close()
