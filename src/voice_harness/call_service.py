"""Call-intent service shared by a manual caller and a future agent tool."""

import asyncio
import logging
import secrets
import time
from contextlib import suppress
from datetime import datetime
from importlib.util import find_spec

from .config import Settings
from .models import StartCall, StartCallResponse
from .observability import call_id_context, error_type, log_event
from .personaplex import (
    BUDGET_ERROR,
    PersonaPlexClient,
    PersonaPlexError,
    PromptBudgetError,
    model_connection_url,
)
from .repository import CallRepository, RepositoryError
from .vih_client import VihClient, provider_phone


class CallService:
    def __init__(self, settings: Settings, repository: CallRepository | None = None) -> None:
        self.settings = settings
        self.repository = repository or CallRepository(settings.database_path)
        self.vih = VihClient(settings)
        self._prepared_models: dict[str, PersonaPlexClient] = {}
        self._model_expiries: dict[str, asyncio.Task] = {}

    async def start(self, request: StartCall, key: str) -> StartCallResponse:
        context = call_id_context.set(None)
        try:
            return await self._start(request, key)
        except Exception as error:
            log_event("call_start_failed", logging.WARNING if isinstance(error, RepositoryError) else logging.ERROR,
                      stage="call_start", error_type=error_type(error),
                      status_code=error.status if isinstance(error, RepositoryError) else 500)
            raise
        finally:
            call_id_context.reset(context)

    async def _start(self, request: StartCall, key: str) -> StartCallResponse:
        raw = request.model_dump(mode="json")
        existing = self.repository.lookup_replay(key, raw)
        if existing:
            call_id_context.set(existing["id"])
            log_event("call_replayed", state=existing["state"], simulated=bool(existing["simulated"]))
            return StartCallResponse(callId=existing["id"], state=existing["state"], simulated=bool(existing["simulated"]),
                                     resultUrl=f"/v1/calls/{existing['id']}", createdAt=datetime.fromisoformat(existing["created_at"]))
        if self.settings.vih_dial_mode == "live":
            try:
                provider_phone(self.settings, request.customerPhone)
            except ValueError as error:
                raise RepositoryError(422, str(error)) from None
        try:
            model_url = model_connection_url(self.settings.personaplex_url, request.leadContext, self.settings.default_voice)
        except PromptBudgetError:
            raise RepositoryError(422, BUDGET_ERROR) from None
        record, replay = self.repository.create_or_replay(key, raw, self.settings.vih_dial_mode == "mock")
        call_id_context.set(record["id"])
        log_event("call_replayed" if replay else "call_created", state=record["state"],
                  simulated=self.settings.vih_dial_mode == "mock")
        if not replay:
            if self.settings.vih_dial_mode == "live" and not self.repository.acquire_gpu(record["id"]):
                self.repository.update(record["id"], state="failed", dial_status="gpu_unavailable")
                log_event("call_rejected", logging.WARNING, reason="gpu_unavailable", state="failed")
            else:
                if self.settings.vih_dial_mode == "live":
                    try:
                        ready = await self._prepare_model(record["id"], model_url)
                    except asyncio.CancelledError:
                        await self.discard_prepared_model(record["id"])
                        self.repository.update(record["id"], state="failed", dial_status="runtime_not_ready", gpu_lease=0)
                        raise
                    if not ready:
                        self.repository.update(record["id"], state="failed", dial_status="runtime_not_ready", gpu_lease=0)
                        log_event("call_rejected", logging.ERROR, stage="runtime", reason="runtime_not_ready", state="failed")
                        final = self.repository.get(record["id"])
                        log_event("call_start_finished", state=final["state"], simulated=False)
                        return StartCallResponse(callId=record["id"], state=final["state"], simulated=False,
                                                 resultUrl=f"/v1/calls/{record['id']}", createdAt=datetime.fromisoformat(final["created_at"]))
                try:
                    if self.settings.vih_dial_mode == "live":
                        token = secrets.token_urlsafe(32)
                        self.repository.set_media_token(record["id"], token)
                        wss = self.settings.vih_wss_value.format(callId=record["id"], token=token)
                    self.repository.update(record["id"], state="dialing")
                except Exception:
                    await self.discard_prepared_model(record["id"])
                    with suppress(Exception):
                        self.repository.update(record["id"], state="failed", gpu_lease=0,
                                               dial_status="runtime_not_ready", media_token_digest=None)
                    raise
                try:
                    result = await self.vih.dial(request.customerPhone, wss if self.settings.vih_dial_mode == "live" else None)
                except (Exception, asyncio.CancelledError):
                    # Once POST may have started, cancellation cannot establish no call was placed.
                    if self.repository.get(record["id"])["state"] == "dialing":
                        self.repository.update(record["id"], state="dial_unknown", dial_status="unknown")
                    raise
                finally:
                    pending = self._prepared_models.get(record["id"])
                    if pending is not None:
                        self._model_expiries[record["id"]] = asyncio.create_task(
                            self._expire_prepared_model(record["id"], pending)
                        )
                state = {"simulated": "completed", "submitted": "dial_submitted", "failed": "failed", "unknown": "dial_unknown"}[result.status]
                # The raw provider response is bounded in VihClient and contains no request secrets.
                current = self.repository.get(record["id"])
                fields = {"dial_status": result.status, "dial_response": result.body, "dial_request": result.request}
                # A fast provider WebSocket/CDR may have moved the call beyond dialing while HTTP returned.
                if current["state"] == "dialing":
                    fields.update(state=state, gpu_lease=1 if self.settings.vih_dial_mode == "live" and state in {"dial_submitted", "dial_unknown"} else 0)
                self.repository.update(record["id"], **fields)
                if self.settings.vih_dial_mode == "live":
                    self.repository.bind_provider_ids(
                        record["id"],
                        call_sid=self.vih.response_id(self.settings.vih_response_call_id_path, result.body) if self.settings.vih_response_call_id_path else None,
                        campaign_id=self.vih.response_id(self.settings.vih_response_campaign_id_path, result.body) if self.settings.vih_response_campaign_id_path else None,
                    )
                    if not self.repository.get(record["id"])["gpu_lease"]:
                        await self.discard_prepared_model(record["id"])
        final = self.repository.get(record["id"])
        log_event("call_start_finished", state=final["state"], simulated=bool(final["simulated"]))
        return StartCallResponse(callId=record["id"], state=final["state"], simulated=bool(final["simulated"]),
                                 resultUrl=f"/v1/calls/{record['id']}", createdAt=datetime.fromisoformat(final["created_at"]))

    def result(self, call_id: str) -> dict:
        return self.repository.result(call_id)

    def cdr(self, payload: dict) -> tuple[str | None, bool]:
        call_id, duplicate = self.repository.save_cdr(payload)
        log_event("cdr_received", call_id=call_id, duplicate=duplicate, matched=call_id is not None,
                  disposition=payload.get("disposition"))
        return call_id, duplicate

    async def claim_media(self, call_id: str, token: str) -> StartCall:
        call = self.repository.claim_media(call_id, token)
        return StartCall.model_validate(__import__("json").loads(call["request_json"]))

    async def bind_stream(self, call_id: str, start: dict) -> None:
        self.repository.bind_stream(call_id, start)

    async def finish_media(self, call_id: str, reason: str, agent_text: str, stats: dict, recordings: dict) -> None:
        self.repository.finish_media(call_id, reason, agent_text, stats, recordings)
        await self.discard_prepared_model(call_id)

    async def checkpoint_media(self, call_id: str, agent_text: str, stats: dict, recordings: dict) -> None:
        self.repository.update(call_id, agent_text=agent_text[:self.settings.max_transcript_chars],
                               stream_stats=stats, recordings=recordings)

    def take_prepared_model(self, call_id: str) -> PersonaPlexClient:
        client = self._prepared_models.pop(call_id, None)
        if client is None:
            raise PersonaPlexError("Prepared PersonaPlex session is unavailable")
        expiry = self._model_expiries.pop(call_id, None)
        if expiry:
            expiry.cancel()
        return client

    async def discard_prepared_model(self, call_id: str) -> None:
        client = self._prepared_models.pop(call_id, None)
        expiry = self._model_expiries.pop(call_id, None)
        if expiry:
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        if client:
            with suppress(Exception):
                await asyncio.wait_for(client.close(), self.settings.close_timeout_seconds)

    async def release_model_if_terminal(self, call_id: str | None) -> None:
        if call_id and not self.repository.get(call_id)["gpu_lease"]:
            await self.discard_prepared_model(call_id)

    async def close(self) -> None:
        for call_id in list(self._prepared_models):
            await self.discard_prepared_model(call_id)

    async def _expire_prepared_model(self, call_id: str, client: PersonaPlexClient) -> None:
        try:
            await asyncio.sleep(self.settings.session_ttl_seconds)
            if self._prepared_models.get(call_id) is client:
                record = self.repository.get(call_id)
                if record["gpu_lease"] and not record["stream_active"] and record["state"] in {
                    "dialing", "dial_submitted", "dial_unknown"
                }:
                    self.repository.update(call_id, state="recovery_required", recovery_required=1,
                                           media_token_digest=None)
                    log_event("model_session_expired", logging.WARNING, call_id=call_id,
                              stage="model_connect", state="recovery_required")
        finally:
            if self._prepared_models.get(call_id) is client:
                self._prepared_models.pop(call_id, None)
                self._model_expiries.pop(call_id, None)
                with suppress(Exception):
                    await asyncio.wait_for(client.close(), self.settings.close_timeout_seconds)

    async def _prepare_model(self, call_id: str, model_url: str) -> bool:
        # Manual recovery may have released an older reservation without a CDR.
        for pending_id in list(self._prepared_models):
            await self.release_model_if_terminal(pending_id)
        if any(find_spec(name) is None for name in ("websockets", "numpy", "soxr", "sphn")):
            log_event("runtime_check_failed", logging.ERROR, stage="runtime", reason="runtime_dependencies_missing")
            return False
        client = PersonaPlexClient(model_url, self.settings.personaplex_prepare_timeout_seconds)
        started = time.monotonic()
        log_event("model_connecting", call_id=call_id, stage="model_connect")
        try:
            async with asyncio.timeout(self.settings.personaplex_prepare_timeout_seconds):
                await client.connect()
        except (Exception, asyncio.CancelledError) as error:
            with suppress(Exception):
                await asyncio.wait_for(client.close(), self.settings.close_timeout_seconds)
            if isinstance(error, asyncio.CancelledError):
                raise
            log_event("runtime_check_failed", logging.ERROR, call_id=call_id,
                      stage="model_connect", reason="runtime_not_ready",
                      error_type=error_type(error))
            return False
        self._prepared_models[call_id] = client
        log_event("model_ready", call_id=call_id, stage="model_connect",
                  duration_ms=round((time.monotonic() - started) * 1000, 2))
        return True
