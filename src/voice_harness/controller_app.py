import asyncio
import json
import logging
import time
from importlib.util import find_spec
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .auth import bearer_value, matches
from .call_service import CallService
from .models import Brief, Cdr, CreatedSession, SessionStatus, StartCall, StartCallResponse
from .observability import error_type, log_event, request_id_context
from .session_store import SessionStore, StoreError


def create_controller_app(store: SessionStore, calls: CallService | None = None, include_legacy: bool = True) -> FastAPI:
    calls = calls or CallService(store.settings)
    app = FastAPI(
        title="Voice Harness Control API", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        if request.url.path == "/webhooks/vih/cdr":
            return await call_next(request)
        if not matches(
            store.settings.bridge_api_key, bearer_value(request.headers.get("authorization"))
        ):
            # Reject before reading an untrusted body.
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        received = 0
        chunks = []
        try:
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > 65536:
                        return JSONResponse(
                            {"detail": "request body exceeds 64 KiB"}, status_code=413
                        )
                    chunks.append(chunk)
        except TimeoutError:
            return JSONResponse({"detail": "request body timeout"}, status_code=408)
        request._body = b"".join(chunks)  # Starlette replays this bounded cached body downstream.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        identifier = uuid4().hex
        context = request_id_context.set(identifier)
        started = time.monotonic()
        status_code = 500
        failure = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = identifier
            return response
        except Exception as error:
            failure = error_type(error)
            raise
        finally:
            # Only the router's fixed template is eligible for logging, never the URL.
            route = getattr(request.scope.get("route"), "path", "unknown")
            log_event("api_request_failed" if status_code >= 400 else "api_request_finished",
                      logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO,
                      request_id=identifier, route=route, method=request.method,
                      call_id=request.path_params.get("call_id"), status_code=status_code,
                      stage="http", error_type=failure,
                      duration_ms=round((time.monotonic() - started) * 1000, 2))
            request_id_context.reset(context)

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        if not matches(store.settings.bridge_api_key, bearer_value(authorization)):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def raise_store(error: StoreError):
        raise HTTPException(error.status, error.detail)

    def raise_repository(error) -> None:
        raise HTTPException(error.status, error.detail)

    @app.post("/v1/calls", status_code=202, response_model=StartCallResponse, dependencies=[Depends(authorize)])
    async def start_call(
        request: StartCall, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")
    ) -> StartCallResponse:
        if not idempotency_key or not (8 <= len(idempotency_key) <= 128) or not idempotency_key.isascii() or not idempotency_key.isprintable():
            raise HTTPException(400, "Idempotency-Key must be 8-128 visible ASCII characters")
        try:
            return await calls.start(request, idempotency_key)
        except Exception as error:
            if hasattr(error, "status"):
                raise_repository(error)
            raise

    @app.get("/v1/calls/{call_id}", dependencies=[Depends(authorize)])
    async def call_result(call_id: str) -> dict:
        try:
            return calls.result(call_id)
        except Exception as error:
            if hasattr(error, "status"):
                raise_repository(error)
            raise

    @app.post("/webhooks/vih/cdr", status_code=200)
    async def vih_cdr(request: Request, authorization: str | None = Header(default=None)) -> dict:
        settings = store.settings
        supplied = bearer_value(authorization) if settings.vih_cdr_auth_mode == "bearer" else request.query_params.get("token")
        if settings.vih_cdr_auth_mode == "disabled" or not matches(settings.vih_cdr_secret, supplied):
            raise HTTPException(401, "Unauthorized")
        try:
            declared_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            raise HTTPException(400, "invalid Content-Length") from None
        if declared_length > 65536:
            raise HTTPException(413, "request body exceeds 64 KiB")
        chunks, received = [], 0
        try:
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > 65536:
                        raise HTTPException(413, "request body exceeds 64 KiB")
                    chunks.append(chunk)
        except TimeoutError:
            raise HTTPException(408, "request body timeout") from None
        raw = b"".join(chunks)
        if len(raw) > 65536:
            raise HTTPException(413, "request body exceeds 64 KiB")
        try:
            payload = json.loads(raw)
            cdr = Cdr.model_validate(payload)
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid CDR JSON") from None
        if cdr.event != "cdr":
            raise HTTPException(422, "event must be cdr")
        call_id, duplicate = calls.cdr(payload)
        await calls.release_model_if_terminal(call_id)
        return {"accepted": True, "duplicate": duplicate, "callId": call_id}

    @app.post(
        "/sessions",
        status_code=201,
        response_model=CreatedSession,
        dependencies=[Depends(authorize)],
    )
    async def create(brief: Brief) -> CreatedSession:
        if not include_legacy:
            raise HTTPException(404, "legacy sessions are disabled")
        try:
            return await store.create(brief)
        except StoreError as error:
            raise_store(error)

    @app.get(
        "/sessions/{session_id}", response_model=SessionStatus, dependencies=[Depends(authorize)]
    )
    async def status(session_id: str) -> SessionStatus:
        if not include_legacy:
            raise HTTPException(404, "legacy sessions are disabled")
        try:
            return await store.status(session_id)
        except StoreError as error:
            raise_store(error)

    @app.delete("/sessions/{session_id}", status_code=204, dependencies=[Depends(authorize)])
    async def delete(session_id: str) -> Response:
        if not include_legacy:
            raise HTTPException(404, "legacy sessions are disabled")
        try:
            await store.delete(session_id)
            return Response(status_code=204)
        except StoreError as error:
            raise_store(error)

    @app.get("/health/live", dependencies=[Depends(authorize)])
    async def live() -> dict:
        return {"status": "live"}

    @app.get("/health/ready", dependencies=[Depends(authorize)])
    async def ready() -> JSONResponse:
        runtime_ready = all(
            find_spec(name) is not None for name in ("websockets", "numpy", "soxr", "sphn")
        )
        model_reachable = False
        url = urlsplit(store.settings.personaplex_url)
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    url.hostname, url.port or (443 if url.scheme == "wss" else 80)
                ),
                1.0,
            )
            writer.close()
            await writer.wait_closed()
            model_reachable = True
        except (OSError, TimeoutError):
            pass
        ready = runtime_ready and model_reachable
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "personaplex": "tcp-reachable" if model_reachable else "unavailable",
                "audioRuntime": runtime_ready,
            },
            status_code=200 if ready else 503,
        )

    return app
