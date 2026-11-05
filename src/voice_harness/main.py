import asyncio
import logging
from os import getenv

import uvicorn

from .call_service import CallService
from .config import Settings
from .controller_app import create_controller_app
from .observability import config_field, configure_logging, error_type, log_event
from .session_store import SessionStore
from .vih_media import create_vih_media_app


async def serve(settings: Settings) -> None:
    log_event("service_starting", control_port=settings.control_port, media_port=settings.media_port,
              dial_mode=settings.vih_dial_mode, recording_enabled=bool(settings.recording_dir))
    store = SessionStore(settings)
    calls = CallService(settings)
    control = uvicorn.Server(
        uvicorn.Config(
            create_controller_app(store, calls, include_legacy=False),
            host=settings.control_host,
            port=settings.control_port,
            workers=1,
            log_level="warning",
            log_config=None,
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
            log_config=None,
            access_log=False,
            ws_max_size=settings.max_frame_bytes,
            ws_max_queue=8,
            timeout_keep_alive=15,
        )
    )
    try:
        await asyncio.gather(control.serve(), media.serve())
    finally:
        await calls.close()
        log_event("service_stopped", stage="shutdown")


def run() -> None:
    configure_logging(getenv("LOG_LEVEL", "INFO").upper())
    try:
        settings = Settings.from_env()
    except Exception as error:  # noqa: BLE001 - raw configuration errors may contain secrets
        log_event("configuration_invalid", logging.ERROR, stage="startup", error_type=error_type(error),
                  reason="invalid_configuration", config_field=config_field(error))
        raise SystemExit(1) from None
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - never print an unsanitized startup traceback
        log_event("service_failed", logging.ERROR, stage="service", error_type=error_type(error))
        raise SystemExit(1) from None
