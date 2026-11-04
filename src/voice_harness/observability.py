"""Allowlisted operational JSON logs. Never serialize payloads or exception text."""

import json
import logging
import math
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
call_id_context: ContextVar[str | None] = ContextVar("call_id", default=None)

EVENTS = frozenset({
    "api_request_finished", "api_request_failed", "call_created", "call_replayed",
    "call_rejected", "call_start_failed", "call_start_finished", "runtime_check_failed", "dial_started",
    "dial_finished", "dial_failed", "cdr_received", "media_authorized", "media_rejected",
    "stream_started", "model_connecting", "model_ready", "media_progress", "media_failed",
    "media_input_started", "media_output_started", "model_text_started",
    "media_finished", "media_checkpoint", "service_starting", "service_stopped", "service_failed",
    "configuration_invalid", "recovery_detected",
})
ENUMS = {
    "route": {"/v1/calls", "/v1/calls/{call_id}", "/webhooks/vih/cdr", "/health/live",
              "/health/ready", "/sessions", "/sessions/{session_id}", "unknown"},
    "method": {"GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS", "OTHER"},
    "state": {"created", "dialing", "dial_submitted", "dial_unknown", "connecting", "live",
              "stream_ended", "completed", "failed", "recovery_required"},
    "stage": {"startup", "shutdown", "service", "call_start", "runtime", "obd", "http", "media_auth",
              "provider_handshake", "codec", "recording", "model_connect", "provider_receive",
              "model_send", "model_receive", "audio_decode", "provider_send", "checkpoint",
              "cleanup", "database", "media_session"},
    "reason": {"gpu_unavailable", "runtime_not_ready", "runtime_dependencies_missing",
               "model_unreachable", "simulated", "submitted", "failed", "unknown",
               "timeout", "network", "http_error", "response_too_large", "provider_stop",
               "provider_disconnect", "call_timeout", "service_shutdown", "bridge_error",
               "invalid_configuration", "unexpected_error"},
    "error_type": {"TimeoutError", "OSError", "ImportError", "ValueError", "TypeError",
                   "RuntimeError", "RepositoryError", "VihProtocolError", "PersonaPlexError",
                   "AudioError", "WebSocketDisconnect", "CancelledError", "HTTPError",
                   "DatabaseError", "Exception"},
    "dial_mode": {"mock", "live"},
    "error_code": {"invalid_json", "invalid_sequence", "stream_mismatch", "stop_mismatch",
                   "unsupported_media_format", "invalid_audio_payload", "invalid_event",
                   "invalid_model_frame", "model_handshake_failed", "transcript_limit",
                   "audio_limit", "unclassified"},
    "config_field": {"BRIDGE_API_KEY", "CONTROLLER_API_KEY", "CONTROL_HOST", "MEDIA_HOST",
                     "MEDIA_AUTH_MODE", "PERSONAPLEX_URL", "PUBLIC_STREAM_BASE_URL",
                     "CONTROLLER_CALLBACK_BASE_URL", "ALLOWED_VOICES", "VIH_DIAL_MODE",
                     "VIH_PHONE_FORMAT", "VIH_CDR_AUTH_MODE", "VIH_CDR_SECRET",
                     "VIH_WSS_VALUE_MODE", "VIH_WSS_VALUE"},
}
COUNTERS = frozenset({
    "duration_ms", "inbound_media_frames", "inbound_audio_bytes", "outbound_media_frames",
    "outbound_audio_bytes", "sequence_gaps", "agent_text_chars", "recovered_calls",
    "control_port", "media_port",
})
BOOLEANS = frozenset({"simulated", "duplicate", "matched", "recovery_required", "recording_enabled"})


def safe_fields(fields: dict) -> dict:
    safe = {}
    for key, value in fields.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            safe[key] = value
        elif key in {"call_id", "request_id"} and isinstance(value, str):
            pattern = r"call_[a-f0-9]{36}" if key == "call_id" else r"[a-f0-9]{32}"
            if re.fullmatch(pattern, value):
                safe[key] = value
        elif (
            (key == "status_code" and type(value) is int and 100 <= value <= 599)
            or (key in COUNTERS and type(value) in {int, float} and math.isfinite(value) and value >= 0)
            or (key in BOOLEANS and type(value) is bool)
        ):
            safe[key] = value
    return safe


def error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name in ENUMS["error_type"]:
        return name
    if isinstance(error, OSError):
        return "OSError"
    if any(base.__name__ == "DatabaseError" for base in type(error).__mro__):
        return "DatabaseError"
    return "Exception"


def error_code(error: BaseException) -> str:
    # Exact known implementation errors map to fixed codes; unknown text is discarded.
    codes = {
        "invalid JSON event": "invalid_json",
        "invalid sequenceNumber": "invalid_sequence",
        "non-monotonic sequence": "invalid_sequence",
        "streamSid mismatch": "stream_mismatch",
        "event is not for active stream": "stream_mismatch",
        "stop call/account mismatch": "stop_mismatch",
        "unsupported media format": "unsupported_media_format",
        "invalid base64 media payload": "invalid_audio_payload",
        "empty media payload": "invalid_audio_payload",
        "ViH requires text JSON events": "invalid_event",
        "unsupported event": "invalid_event",
        "malformed model frame": "invalid_model_frame",
        "unexpected model frame": "invalid_model_frame",
        "PersonaPlex returned a malformed frame": "invalid_model_frame",
        "PersonaPlex connection or ready handshake failed": "model_handshake_failed",
        "PersonaPlex returned an invalid ready handshake": "model_handshake_failed",
        "agent transcript limit exceeded": "transcript_limit",
        "model audio frame too large": "audio_limit",
        "recording/audio duration limit exceeded": "audio_limit",
    }
    return codes.get(str(error), "unclassified")


def config_field(error: BaseException) -> str | None:
    # Return a known variable name only, never its value or the validation message.
    return next((key for key in sorted(ENUMS["config_field"]) if key in str(error)), None)


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Deliberately ignore record.msg, args, exc_info, stack_info and arbitrary extras.
        data = getattr(record, "safe_event", None)
        event = data.get("event") if isinstance(data, dict) else None
        if not isinstance(event, str):
            event = None
        result = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": logging.getLevelName(record.levelno) if record.levelno in {10, 20, 30, 40, 50} else "ERROR",
            "event": event if event in EVENTS else "dependency_log",
        }
        if event in EVENTS:
            result.update(safe_fields(data))
        else:
            component = record.name.split(".", 1)[0]
            result["component"] = component if component in {"uvicorn", "httpx", "httpcore", "websockets", "asyncio"} else "other"
        return json.dumps(result, separators=(",", ":"), allow_nan=False)


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    fields.setdefault("request_id", request_id_context.get())
    fields.setdefault("call_id", call_id_context.get())
    logging.getLogger("voice_harness").log(
        level, "", extra={"safe_event": {"event": event, **safe_fields(fields)}}
    )


def configure_logging(level: str = "INFO") -> None:
    # A dedicated formatter also protects dependency logs containing URLs or tracebacks.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logger = logging.getLogger("voice_harness")
    logger.handlers.clear()
    logger.propagate = True
    logger.setLevel(level if level in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "websockets", "asyncio"):
        dependency = logging.getLogger(name)
        dependency.handlers.clear()
        dependency.propagate = True
        dependency.setLevel(logging.WARNING)
    logging.captureWarnings(True)
