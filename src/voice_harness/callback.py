import asyncio
from urllib.parse import urljoin

import httpx


async def send_callback(base: str, api_key: str, session_id: str, payload: dict) -> bool:
    url = urljoin(base.rstrip("/") + "/", f"sessions/{session_id}/transcript")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0), trust_env=False
    ) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    url, headers={"Authorization": f"Bearer {api_key}"}, json=payload
                )
                if response.status_code < 500:
                    return response.is_success
            except httpx.RequestError:
                pass
            if attempt < 2:
                await asyncio.sleep(0.2 * (2**attempt))
    return False
