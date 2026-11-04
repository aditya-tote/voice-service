"""Call-intent service shared by a manual caller and a future agent tool."""

import asyncio
import logging
import secrets
from datetime import datetime
from importlib.util import find_spec
from urllib.parse import urlsplit

from .config import Settings
from .models import StartCall, StartCallResponse
from .observability import call_id_context, error_type, log_event
from .repository import CallRepository, RepositoryError
from .vih_client import VihClient, provider_phone


class CallService:
    def __init__(self, settings: Settings, repository: CallRepository | None = None) -> None:
        self.settings = settings
        self.repository = repository or CallRepository(settings.database_path)
        self.vih = VihClient(settings)

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
        if request.voiceName not in self.settings.allowed_voices:
            raise RepositoryError(422, "voiceName is not allowed")
        if self.settings.vih_dial_mode == "live":
            try:
                provider_phone(self.settings, request.customerPhone)
            except ValueError as error:
                raise RepositoryError(422, str(error)) from None
        raw = request.model_dump(mode="json")
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
                    if not await self._runtime_ready():
                        self.repository.update(record["id"], state="failed", dial_status="runtime_not_ready", gpu_lease=0)
                        log_event("call_rejected", logging.ERROR, stage="runtime", reason="runtime_not_ready", state="failed")
                        final = self.repository.get(record["id"])
                        log_event("call_start_finished", state=final["state"], simulated=False)
                        return StartCallResponse(callId=record["id"], state=final["state"], simulated=False,
                                                 resultUrl=f"/v1/calls/{record['id']}", createdAt=datetime.fromisoformat(final["created_at"]))
                    token = secrets.token_urlsafe(32)
                    self.repository.set_media_token(record["id"], token)
                    wss = self.settings.vih_wss_value.format(callId=record["id"], token=token)
                self.repository.update(record["id"], state="dialing")
                result = await self.vih.dial(request.customerPhone, wss if self.settings.vih_dial_mode == "live" else None)
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
        final = self.repository.get(record["id"])
        log_event("call_start_finished", state=final["state"], simulated=bool(final["simulated"]))
        return StartCallResponse(callId=record["id"], state=final["state"], simulated=bool(final["simulated"]),
                                 resultUrl=f"/v1/calls/{record['id']}", createdAt=datetime.fromisoformat(final["created_at"]))

    def result(self, call_id: str) -> dict:
        return self.repository.result(call_id)

    def cdr(self, payload: dict) -> tuple[str | None, bool]:
        call_id, duplicate = self.repository.save_cdr(payload)
        log_event("cdr_received", call_id=call_id, duplicate=duplicate, matched=call_id is not None)
        return call_id, duplicate

    async def claim_media(self, call_id: str, token: str) -> StartCall:
        call = self.repository.claim_media(call_id, token)
        return StartCall.model_validate(__import__("json").loads(call["request_json"]))

    async def bind_stream(self, call_id: str, start: dict) -> None:
        self.repository.bind_stream(call_id, start)

    async def finish_media(self, call_id: str, reason: str, agent_text: str, stats: dict, recordings: dict) -> None:
        self.repository.finish_media(call_id, reason, agent_text, stats, recordings)

    async def checkpoint_media(self, call_id: str, agent_text: str, stats: dict, recordings: dict) -> None:
        self.repository.update(call_id, agent_text=agent_text[:self.settings.max_transcript_chars],
                               stream_stats=stats, recordings=recordings)

    async def _runtime_ready(self) -> bool:
        if any(find_spec(name) is None for name in ("websockets", "numpy", "soxr", "sphn")):
            log_event("runtime_check_failed", logging.ERROR, stage="runtime", reason="runtime_dependencies_missing")
            return False
        url = urlsplit(self.settings.personaplex_url)
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(url.hostname, url.port or (443 if url.scheme == "wss" else 80)), 1.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, TimeoutError) as error:
            log_event("runtime_check_failed", logging.ERROR, stage="runtime", reason="model_unreachable",
                      error_type=error_type(error))
            return False
