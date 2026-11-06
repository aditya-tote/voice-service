"""Runs only where the GPU runtime's websockets dependency is installed."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

websockets = pytest.importorskip("websockets")

from voice_harness.models import Brief
from voice_harness.personaplex import PersonaPlexClient, PersonaPlexError, connection_url


def test_real_websocket_personaplex_protocol():
    seen = {}

    async def handler(socket):
        query = parse_qs(urlsplit(socket.request.path).query)
        seen.update(query)
        await socket.send(b"\x00")
        assert await socket.recv() == b"\x01opus"
        await socket.send(b"\x01answer")
        await socket.send(b"\x02hello")

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        brief = Brief(
            leadId="l", persona="p", goal="g", tone="t", facts=[], limits=[], voiceName="NATF2.pt"
        )
        client = PersonaPlexClient(connection_url(f"ws://127.0.0.1:{port}/api/chat", brief), 1)
        await client.connect()
        await client.send_opus(b"opus")
        assert await client.recv() == b"\x01answer"
        assert await client.recv() == b"\x02hello"
        await client.close()
        server.close()
        await server.wait_closed()

    asyncio.run(run())
    assert seen["voice_prompt"] == ["NATF2.pt"] and seen["text_prompt"]


def test_handshake_timeout_is_redacted():
    async def handler(socket):
        await asyncio.sleep(1)

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = PersonaPlexClient(f"ws://127.0.0.1:{port}/api/chat?text_prompt=secret", 0.01)
        with pytest.raises(PersonaPlexError) as error:
            await client.connect()
        assert "secret" not in str(error.value)
        server.close()
        await server.wait_closed()

    asyncio.run(run())


def test_evaluation_client_refuses_redirect_before_any_second_connection():
    async def run():
        contacted = []
        async def unexpected(socket):
            contacted.append(True)
            await socket.send(b"\x00")
        async with websockets.serve(unexpected, "127.0.0.1", 0) as target:
            target_port = target.sockets[0].getsockname()[1]
            def redirect(connection, request):
                response = connection.respond(302, "redirect")
                response.headers["Location"] = f"ws://127.0.0.1:{target_port}/api/chat"
                return response
            async with websockets.serve(unexpected, "127.0.0.1", 0, process_request=redirect) as server:
                port = server.sockets[0].getsockname()[1]
                client = PersonaPlexClient(f"ws://127.0.0.1:{port}/api/chat", 1, allow_redirects=False)
                try:
                    with pytest.raises(PersonaPlexError):
                        await client.connect()
                finally:
                    await client.close()
                assert not contacted and client.socket is None
    asyncio.run(run())
