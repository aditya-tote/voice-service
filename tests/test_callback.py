import asyncio

import httpx

from voice_harness import callback


def test_callback_retries_5xx_and_is_idempotently_addressed(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(500 if len(calls) == 1 else 204)

    monkeypatch.setattr(callback.httpx, "AsyncClient", lambda **_: Client())
    original_sleep = asyncio.sleep
    monkeypatch.setattr(callback.asyncio, "sleep", lambda _: original_sleep(0))
    assert asyncio.run(callback.send_callback("http://controller", "x" * 32, "ses_x", {"x": 1}))
    assert len(calls) == 2
    assert calls[0][0] == "http://controller/sessions/ses_x/transcript"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer " + "x" * 32
