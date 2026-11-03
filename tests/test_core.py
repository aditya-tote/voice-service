import asyncio

from fastapi.testclient import TestClient

from voice_harness.config import Settings
from voice_harness.controller_app import create_controller_app
from voice_harness.models import Brief
from voice_harness.personaplex import connection_url
from voice_harness.session_store import SessionStore, StoreError


def settings():
    return Settings(
        "127.0.0.1",
        8080,
        "127.0.0.1",
        8081,
        "wss://bridge.example.com",
        "a" * 32,
        "header",
        30,
        2,
        "ws://127.0.0.1:8998/api/chat",
        frozenset({"NATF2.pt"}),
        "http://127.0.0.1:9000",
        "b" * 32,
    )


def brief():
    return Brief(
        leadId="lead",
        persona="seller",
        goal="book",
        tone="calm",
        facts=["fact"],
        limits=["limit"],
        voiceName="NATF2.pt",
    )


def test_api_auth_and_redaction():
    store = SessionStore(settings())
    client = TestClient(create_controller_app(store))
    assert client.post("/sessions").status_code == 401
    response = client.post(
        "/sessions", headers={"Authorization": "Bearer " + "a" * 32}, json=brief().model_dump()
    )
    assert response.status_code == 201 and response.json()["streamHeaders"][
        "Authorization"
    ].startswith("Bearer ")
    item = response.json()
    status = client.get(
        "/sessions/" + item["sessionId"], headers={"Authorization": "Bearer " + "a" * 32}
    )
    assert status.status_code == 200 and "token" not in status.text and "seller" not in status.text
    assert (
        client.post(
            "/sessions", headers={"Authorization": "Bearer " + "a" * 32}, content="x" * 65537
        ).status_code
        == 413
    )


def test_claim_is_one_use_and_one_live():
    async def run():
        store = SessionStore(settings())
        first = await store.create(brief())
        second = await store.create(brief())
        token = first.streamHeaders["Authorization"][7:]
        await store.claim(first.sessionId, token)
        try:
            await store.claim(first.sessionId, token)
            assert False
        except StoreError as error:
            assert error.status == 409
        await store.end(first.sessionId, "done")
        await store.claim(second.sessionId, second.streamHeaders["Authorization"][7:])

    asyncio.run(run())


def test_personaplex_url_encodes_prompt_and_voice():
    url = connection_url("ws://127.0.0.1:8998/api/chat", brief())
    assert "voice_prompt=NATF2.pt" in url and "text_prompt=" in url and "seller" in url
