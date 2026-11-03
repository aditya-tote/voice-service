"""Independent boundary checks for controller and media-session security."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_harness.auth import matches
from voice_harness.config import Settings
from voice_harness.controller_app import create_controller_app
from voice_harness.models import Brief
from voice_harness.session_store import SessionStore, StoreError


def settings():
    return Settings(
        control_host="127.0.0.1",
        control_port=8080,
        media_host="127.0.0.1",
        media_port=8081,
        public_stream_base_url="wss://bridge.example.com",
        bridge_api_key="a" * 40,
        media_auth_mode="header",
        session_ttl_seconds=300,
        max_stored_sessions=10,
        personaplex_url="ws://127.0.0.1:8998/api/chat",
        allowed_voices=frozenset({"NATF2.pt"}),
        controller_callback_base_url="http://127.0.0.1:9000",
        controller_api_key="b" * 40,
    )


def brief():
    return Brief(
        leadId="lead",
        persona="Representative",
        goal="Schedule follow-up",
        tone="Calm",
        facts=[],
        limits=["No discounts"],
        voiceName="NATF2.pt",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"control_host": "0.0.0.0"},
        {"media_host": "100.64.0.1"},
        {"personaplex_url": "ws://remote.example.com/api/chat"},
        {"public_stream_base_url": "wss:///"},
        {"public_stream_base_url": "wss://user:password@example.com"},
        {"public_stream_base_url": "wss://example.com/#secret"},
        {"session_ttl_seconds": 0},
        {"max_stored_sessions": -1},
        {"control_port": 0},
        {"media_port": 70000},
    ],
)
def test_unsafe_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(settings(), **changes)


def test_control_credentials_and_response_redaction():
    config = settings()
    with TestClient(create_controller_app(SessionStore(config))) as client:
        assert client.post("/sessions", json=brief().model_dump()).status_code == 401
        assert client.get("/health/live?token=" + config.bridge_api_key).status_code == 401
        headers = {"Authorization": "Bearer " + config.bridge_api_key}
        response = client.post("/sessions", headers=headers, json=brief().model_dump())
        assert response.status_code == 201
        assert response.headers.get("cache-control") == "no-store"
        created = response.json()
        assert "token=" not in created["streamUrl"]
        assert created["streamHeaders"]["Authorization"] != headers["Authorization"]
        status = client.get("/sessions/" + created["sessionId"], headers=headers)
        assert status.status_code == 200
        assert not {"brief", "streamHeaders", "token", "persona"}.intersection(status.json())
        assert client.get("/docs").status_code in {401, 404}
        assert client.get("/openapi.json").status_code in {401, 404}


@pytest.mark.asyncio
async def test_chunked_body_cannot_bypass_limit():
    app = create_controller_app(SessionStore(settings()))

    async def chunks():
        for _ in range(9):
            yield b" " * 8192

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/sessions",
            content=chunks(),
            headers={
                "Authorization": "Bearer " + settings().bridge_api_key,
                "Content-Type": "application/json",
            },
        )
    assert result.status_code == 413


@pytest.mark.asyncio
async def test_concurrent_claims_allow_exactly_one_gpu_call():
    store = SessionStore(settings())
    created = [await store.create(brief()) for _ in range(2)]
    tokens = [item.streamHeaders["Authorization"].split(" ", 1)[1] for item in created]
    results = await asyncio.gather(
        *(store.claim(item.sessionId, token) for item, token in zip(created, tokens)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, StoreError) for item in results) == 1
    winner = next(item for item in results if not isinstance(item, StoreError))
    await store.end(winner.id, "test_finished")
    ended = store.items[winner.id]
    assert not ended.token_digest
    assert ended.brief is None
    assert store.live_id is None


def test_non_ascii_credential_is_rejected_without_server_error():
    assert not matches("a" * 40, "\u2603" * 40)


@pytest.mark.asyncio
async def test_expired_session_cannot_be_claimed_and_stored_can_be_deleted():
    store = SessionStore(settings())
    created = await store.create(brief())
    store.items[created.sessionId].expires = 0
    with pytest.raises(StoreError) as error:
        await store.claim(created.sessionId, created.streamHeaders["Authorization"][7:])
    assert error.value.status == 404
    fresh = await store.create(brief())
    await store.delete(fresh.sessionId)
    with pytest.raises(StoreError):
        await store.status(fresh.sessionId)
