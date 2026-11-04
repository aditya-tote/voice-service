"""The ViH OBD HTTP boundary.  It deliberately does not infer response fields."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import Settings
from .observability import error_type, log_event

HTTP_DEADLINE_SECONDS = 15.0


@dataclass(frozen=True)
class DialResult:
    status: str
    body: dict[str, Any]
    request: dict[str, Any] | None = None


def provider_phone(settings: Settings, phone: str) -> str:
    if settings.vih_phone_format == "india_national":
        if not re.fullmatch(r"\+91[0-9]{10}", phone):
            raise ValueError("india_national requires an Indian +91 number with ten national digits")
        return phone[3:]
    return phone


def dial_payload(settings: Settings, phone: str, wss_value: str | None = None) -> dict[str, Any]:
    return {
        "ukey": settings.vih_ukey, "sourcetype": "1", "sendnow": "0", "campaigntype": settings.vih_campaign_type,
        "filetype": "2", "serviceno": settings.vih_service_no, "ivrtemplateid": settings.vih_template_id,
        "retryatmpt": "0", "retryduration": "15",
        "msisdnlist": [{"phoneno": provider_phone(settings, phone), "param1": wss_value or settings.vih_wss_value}],
    }


def redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact(payload)


def _safe_response(content: bytes, secrets: tuple[str, ...]) -> Any:
    text = content.decode("utf-8", "replace")
    try:
        return _redact(json.loads(text), secrets)
    except (ValueError, RecursionError):
        return {"text": _redact(text, secrets)}


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if not isinstance(value, dict):
        return value
    return {
        key: "[redacted]" if key.lower() in {"ukey", "token", "authorization", "password", "wss", "param1"} else _redact(item, secrets)
        for key, item in value.items()
    }


class VihClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def response_id(self, path: str, body: dict[str, Any]) -> str | None:
        value: Any = body.get("body", body)
        for part in path.lstrip("/").split("/"):
            if not part:
                continue
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                return None
        return value if isinstance(value, str) and value else None

    async def dial(self, phone: str, wss_value: str | None = None) -> DialResult:
        started = time.monotonic()
        log_event("dial_started", stage="obd", dial_mode=self.settings.vih_dial_mode)
        if self.settings.vih_dial_mode == "mock":
            log_event("dial_finished", stage="obd", reason="simulated", simulated=True)
            return DialResult("simulated", {"mode": "mock", "accepted": True}, {"mode": "mock"})
        payload = dial_payload(self.settings, phone, wss_value)
        redacted = redacted_payload(payload)
        try:
            async with asyncio.timeout(HTTP_DEADLINE_SECONDS), httpx.AsyncClient(
                trust_env=False, timeout=httpx.Timeout(10, connect=3)
            ) as client:
                oversized = False
                async with client.stream("POST", self.settings.vih_obd_url, json=payload) as response:
                    content = b""
                    async for chunk in response.aiter_bytes(chunk_size=1024):
                        content += chunk
                        if len(content) > 8192:
                            content = content[:8192]
                            oversized = True
                            break
                stream_url = payload["msisdnlist"][0]["param1"]
                token = parse_qs(urlsplit(stream_url).query).get("token", [""])[0]
                body = _safe_response(content, (self.settings.vih_ukey, self.settings.vih_wss_value,
                                                stream_url, token))
                log_event("dial_finished" if response.is_success and not oversized else "dial_failed",
                          logging.INFO if response.is_success and not oversized else logging.ERROR,
                          stage="obd", status_code=response.status_code,
                          reason="response_too_large" if oversized else "submitted" if response.is_success else "http_error",
                          duration_ms=round((time.monotonic() - started) * 1000, 2))
                # A non-2xx response after POST can still have created a campaign; keep it unknown.
                return DialResult("submitted" if response.is_success and not oversized else "unknown",
                                  {"httpStatus": response.status_code, "body": body,
                                   "truncated": oversized}, redacted)
        except (httpx.TimeoutException, TimeoutError):
            log_event("dial_failed", logging.ERROR, stage="obd", reason="timeout",
                      error_type="TimeoutError", duration_ms=round((time.monotonic() - started) * 1000, 2))
            return DialResult("unknown", {"error": "timeout"}, redacted)
        except httpx.HTTPError as error:
            log_event("dial_failed", logging.ERROR, stage="obd", reason="network",
                      error_type=error_type(error), duration_ms=round((time.monotonic() - started) * 1000, 2))
            return DialResult("unknown", {"error": "network"}, redacted)
