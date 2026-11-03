import asyncio

import uvicorn

from .call_service import CallService
from .config import Settings
from .controller_app import create_controller_app
from .session_store import SessionStore
from .vih_media import create_vih_media_app


async def serve(settings: Settings) -> None:
    store = SessionStore(settings)
    calls = CallService(settings)
    control = uvicorn.Server(
        uvicorn.Config(
            create_controller_app(store, calls, include_legacy=False),
            host=settings.control_host,
            port=settings.control_port,
            workers=1,
            log_level="info",
            access_log=False,
            timeout_keep_alive=15,
        )
    )
    media = uvicorn.Server(
        uvicorn.Config(
            create_vih_media_app(calls),
            host=settings.media_host,
            port=settings.media_port,
            workers=1,
            # Uvicorn's WebSocket info logs can include query credentials.
            log_level="warning",
            access_log=False,
            ws_max_size=settings.max_frame_bytes,
            ws_max_queue=8,
            timeout_keep_alive=15,
        )
    )
    await asyncio.gather(control.serve(), media.serve())


def run() -> None:
    asyncio.run(serve(Settings.from_env()))
