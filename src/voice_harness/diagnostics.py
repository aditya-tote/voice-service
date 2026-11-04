"""Bounded provider diagnostics: shape and approved values, never raw content."""

import os
import re
from urllib.parse import parse_qs, urlsplit

STATUS_VALUES = {"accepted", "success", "ok", "error", "failed", "failure", "rejected",
                 "pending", "queued", "invalid", "true", "false", "other"}
REASON_CODES = {"accepted", "authentication_rejected", "template_rejected",
                "service_number_rejected", "phone_number_rejected", "insufficient_balance",
                "approval_pending", "rejected", "unclassified"}
SUMMARY_ENUMS = {
    "sourcetype": {"1"}, "sendnow": {"0", "1"}, "campaigntype": {"3", "4"},
    "filetype": {"2"}, "retryatmpt": {"0", "1", "2"}, "retryduration": {"0", "15", "30", "60"},
    "ukey": {"[redacted]"}, "serviceno": {"[redacted]"}, "phoneno": {"[redacted]"},
    "param1": {"[redacted]"}, "phone_format": {"india_national", "e164", "other"},
    "wss_scheme": {"wss", "ws", "other"}, "wss_route": {"/calls/{call_id}", "unknown"},
    "status": STATUS_VALUES, "value": STATUS_VALUES, "reason_code": REASON_CODES,
    "body_type": {"object", "array", "string", "number", "boolean", "null", "unknown"},
    "event": {"connected", "start", "stop", "dtmf", "mark", "media", "cdr", "unknown"},
    "encoding": {"audio/x-mulaw", "other"},
}
SUMMARY_BOOLS = {"ukey_present", "service_number_present", "phone_present", "param1_present",
                 "token_present", "wss_matches_public_base", "message_present", "error_present",
                 "call_id_present", "campaign_id_present", "stream_id_present", "account_id_present",
                 "custom_parameters_present", "payload_present", "reason_present"}
SUMMARY_NUMBERS = {"recipient_count", "phone_digits", "sequence_number", "sample_rate",
                   "bit_depth", "bit_rate", "item_count"}
REQUEST_KEYS = {"sourcetype", "sendnow", "campaigntype", "filetype", "ivrtemplateid", "retryatmpt",
                "retryduration", "ukey", "serviceno", "phoneno", "param1", "recipient_count",
                "phone_digits", "phone_format", "wss_scheme", "wss_route", "ukey_present",
                "service_number_present", "phone_present", "param1_present", "token_present",
                "wss_matches_public_base", "debug_phone", "debug_wss_url"}
RESPONSE_KEYS = {"status", "value", "reason_code", "body_type", "message_present", "error_present",
                 "call_id_present", "campaign_id_present", "item_count"}
EVENT_KEYS = {"event", "sequence_number", "encoding", "sample_rate", "bit_depth", "bit_rate",
              "call_id_present", "stream_id_present", "account_id_present", "custom_parameters_present",
              "payload_present", "reason_present"}


def _debug_stream_url(value: object) -> str | None:
    """Keep only a canonical media address; discard all query values and credentials."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "wss" or url.username is not None or url.password is not None
                or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", url.netloc)
                or not re.fullmatch(r"/calls/call_[a-f0-9]{36}", url.path)):
            return None
        return f"wss://{url.netloc}{url.path}?token=[redacted]"
    except ValueError:
        return None


def safe_summary(kind: str, value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    allowed = {"provider_request": REQUEST_KEYS, "provider_response": RESPONSE_KEYS,
               "provider_event": EVENT_KEYS}.get(kind, set())
    result = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if key in {"debug_phone", "debug_wss_url"}:
            if os.getenv("LOG_PROVIDER_DESTINATION", "false").lower() != "true":
                continue
            if key == "debug_phone" and isinstance(item, str) and re.fullmatch(r"\+?[0-9]{8,15}", item):
                result[key] = item
            elif key == "debug_wss_url":
                sanitized = _debug_stream_url(item)
                if sanitized is not None:
                    result[key] = sanitized
            continue
        if (
            (key in SUMMARY_ENUMS and isinstance(item, str) and item in SUMMARY_ENUMS[key])
            or (key == "ivrtemplateid" and isinstance(item, str) and re.fullmatch(r"[0-9]{1,6}", item))
            or (key in SUMMARY_BOOLS and type(item) is bool)
            or (key in SUMMARY_NUMBERS and type(item) is int and 0 <= item <= 1_000_000)
        ):
            result[key] = item
    return result


def obd_request_summary(payload: dict, expected_base: str) -> dict:
    recipients = payload.get("msisdnlist", [])
    recipient = recipients[0] if isinstance(recipients, list) and recipients and isinstance(recipients[0], dict) else {}
    phone = recipient.get("phoneno", "")
    stream = recipient.get("param1", "")
    result = {key: payload.get(key) for key in
              ("sourcetype", "sendnow", "campaigntype", "filetype", "ivrtemplateid", "retryatmpt", "retryduration")}
    result.update(ukey="[redacted]", serviceno="[redacted]", phoneno="[redacted]", param1="[redacted]",
                  ukey_present=bool(payload.get("ukey")), service_number_present=bool(payload.get("serviceno")),
                  phone_present=bool(phone), param1_present=bool(stream),
                  recipient_count=len(recipients) if isinstance(recipients, list) else 0,
                  phone_digits=sum(character.isdigit() for character in phone) if isinstance(phone, str) else 0,
                  phone_format="india_national" if isinstance(phone, str) and re.fullmatch(r"[0-9]{10}", phone)
                  else "e164" if isinstance(phone, str) and re.fullmatch(r"\+[0-9]{8,15}", phone) else "other")
    try:
        url = urlsplit(stream)
        expected = urlsplit(expected_base)
        result.update(wss_scheme=url.scheme if url.scheme in {"wss", "ws"} else "other",
                      wss_route="/calls/{call_id}" if re.fullmatch(r"/calls/call_[a-f0-9]{36}", url.path) else "unknown",
                      token_present=bool(parse_qs(url.query).get("token", [""])[0]),
                      wss_matches_public_base=(url.scheme, url.netloc) == (expected.scheme, expected.netloc))
    except (TypeError, ValueError):
        result.update(wss_scheme="other", wss_route="unknown", token_present=False, wss_matches_public_base=False)
    if os.getenv("LOG_PROVIDER_DESTINATION", "false").lower() == "true":
        result.update(debug_phone=phone, debug_wss_url=stream)
    return safe_summary("provider_request", result)


def obd_response_summary(body: object) -> dict:
    shape = ("object" if isinstance(body, dict) else "array" if isinstance(body, list)
             else "string" if isinstance(body, str) else "boolean" if type(body) is bool
             else "number" if type(body) in {int, float} else "null" if body is None else "unknown")
    layers = [body] if isinstance(body, dict) else []
    for _ in range(2):
        next_layer = next((layers[-1][key] for key in ("data", "result", "body")
                           if isinstance(layers[-1].get(key), dict)), None) if layers else None
        if next_layer is None:
            break
        layers.append(next_layer)
    result = {"body_type": shape, "message_present": False, "error_present": False,
              "call_id_present": False, "campaign_id_present": False}
    if isinstance(body, list):
        result["item_count"] = len(body)
    texts = []
    for layer in layers:
        for key in ("status", "value"):
            item = layer.get(key)
            if isinstance(item, str):
                normalized = item.strip().lower()
                result[key] = normalized if normalized in STATUS_VALUES else "other"
                texts.append(normalized)
        for key in ("message", "error", "errorMessage", "text"):
            if key in layer:
                result["error_present" if key in {"error", "errorMessage"} else "message_present"] = True
                if isinstance(layer[key], str):
                    texts.append(layer[key].lower())
        result["call_id_present"] |= any(bool(layer.get(key)) for key in ("callSid", "callid", "callId", "call_id"))
        result["campaign_id_present"] |= any(bool(layer.get(key)) for key in ("campaignId", "campaignid", "campaign_id"))
    # These are hints from recognized phrases, not an assertion about dial outcome.
    text = " ".join(texts)
    patterns = {
        "insufficient_balance": ("insufficient balance", "low balance", "no balance"),
        "authentication_rejected": ("invalid ukey", "invalid api key", "invalid credentials", "authentication failed", "unauthorized"),
        "template_rejected": ("invalid template", "template not found", "invalid ivrtemplateid", "template not assigned"),
        "service_number_rejected": ("invalid service", "service number not assigned"),
        "phone_number_rejected": ("invalid phone", "invalid msisdn", "invalid mobile", "invalid number"),
        "approval_pending": ("pending approval", "awaiting approval"),
    }
    result["reason_code"] = next((code for code, phrases in patterns.items() if any(phrase in text for phrase in phrases)),
                                 "accepted" if result.get("value") == "accepted" else
                                 "rejected" if result.get("status") in {"error", "failed", "failure", "rejected"} else "unclassified")
    return safe_summary("provider_response", result)


def stream_event_summary(event: dict) -> dict:
    name = event.get("event")
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    stop = event.get("stop") if isinstance(event.get("stop"), dict) else {}
    media = event.get("media") if isinstance(event.get("media"), dict) else {}
    format_ = start.get("mediaFormat") if isinstance(start.get("mediaFormat"), dict) else {}
    result = {"event": name if isinstance(name, str) and name in SUMMARY_ENUMS["event"] else "unknown",
              "stream_id_present": bool(event.get("streamSid") or start.get("streamSid")),
              "call_id_present": bool(start.get("callSid") or stop.get("callSid")),
              "account_id_present": bool(start.get("accountSid") or stop.get("accountSid")),
              "custom_parameters_present": bool(start.get("customParameters")),
              "payload_present": bool(media.get("payload")), "reason_present": bool(stop.get("reason"))}
    sequence = event.get("sequenceNumber")
    if isinstance(sequence, str) and re.fullmatch(r"[0-9]{1,6}", sequence):
        result["sequence_number"] = int(sequence)
    for source, target in (("sampleRate", "sample_rate"), ("bitDepth", "bit_depth"), ("bitRate", "bit_rate")):
        result[target] = format_.get(source)
    if "encoding" in format_:
        result["encoding"] = "audio/x-mulaw" if format_["encoding"] == "audio/x-mulaw" else "other"
    return safe_summary("provider_event", result)
