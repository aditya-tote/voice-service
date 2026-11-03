import re
from dataclasses import dataclass
from ipaddress import ip_address
from os import getenv
from urllib.parse import urlparse


def _private_host(value: str) -> bool:
    try:
        address = ip_address(value)
        return address.is_loopback or (
            address.version == 4 and address in __import__("ipaddress").ip_network("100.64.0.0/10")
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    control_host: str
    control_port: int
    media_host: str
    media_port: int
    public_stream_base_url: str
    bridge_api_key: str
    media_auth_mode: str
    session_ttl_seconds: int
    max_stored_sessions: int
    personaplex_url: str
    allowed_voices: frozenset[str]
    # Legacy bridge callback settings.  New call results are durably polled.
    controller_callback_base_url: str = "http://127.0.0.1:9000"
    controller_api_key: str = ""
    max_frame_bytes: int = 16384
    max_transcript_chars: int = 32768
    ready_timeout_seconds: float = 15.0
    idle_timeout_seconds: float = 60.0
    close_timeout_seconds: float = 5.0
    max_call_seconds: int = 1800
    database_path: str = "voice-harness.sqlite3"
    vih_dial_mode: str = "mock"
    enable_live_dials: bool = False
    vih_obd_url: str = ""
    vih_ukey: str = ""
    vih_service_no: str = ""
    vih_template_id: str = ""
    vih_campaign_type: str = ""
    vih_wss_value_mode: str = "unconfirmed"
    vih_wss_value: str = ""
    vih_cdr_auth_mode: str = "disabled"
    vih_cdr_secret: str = ""
    vih_response_call_id_path: str = ""
    vih_response_campaign_id_path: str = ""
    recording_dir: str = ""
    vih_phone_format: str = "e164"

    def __post_init__(self) -> None:
        if not _private_host(self.control_host) or self.media_host not in {"127.0.0.1", "::1"}:
            raise ValueError(
                "CONTROL_HOST must be Tailscale/loopback and MEDIA_HOST must be loopback"
            )
        if len(self.bridge_api_key) < 32:
            raise ValueError("BRIDGE_API_KEY must be at least 32 characters")
        if self.controller_api_key and len(self.controller_api_key) < 32:
            raise ValueError("CONTROLLER_API_KEY must be empty or at least 32 characters")
        if self.media_auth_mode not in {"header", "query"}:
            raise ValueError("MEDIA_AUTH_MODE must be header or query")
        if (
            any(
                value < 1
                for value in (
                    self.control_port,
                    self.media_port,
                    self.session_ttl_seconds,
                    self.max_stored_sessions,
                    self.max_frame_bytes,
                    self.max_transcript_chars,
                    self.max_call_seconds,
                )
            )
            or min(
                self.ready_timeout_seconds, self.idle_timeout_seconds, self.close_timeout_seconds
            )
            <= 0
        ):
            raise ValueError("ports, limits, and timeouts must be positive")
        if self.control_port > 65535 or self.media_port > 65535:
            raise ValueError("ports must not exceed 65535")
        if self.max_frame_bytes < 2:
            raise ValueError("MAX_FRAME_BYTES must be at least 2")
        parsed = urlparse(self.personaplex_url)
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("PERSONAPLEX_URL cannot contain credentials or a fragment")
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("PERSONAPLEX_URL must be a loopback ws(s) URL")
        public = urlparse(self.public_stream_base_url)
        if (
            public.scheme != "wss"
            or not public.hostname
            or public.username
            or public.password
            or public.path not in {"", "/"}
            or public.query
            or public.fragment
        ):
            raise ValueError("PUBLIC_STREAM_BASE_URL must be a clean wss origin")
        callback = urlparse(self.controller_callback_base_url)
        if self.controller_api_key and (
            callback.scheme not in {"http", "https"}
            or not callback.hostname
            or callback.username
            or callback.password
            or callback.query
            or callback.fragment
        ):
            raise ValueError("CONTROLLER_CALLBACK_BASE_URL must be an http(s) URL")
        if not self.allowed_voices or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+\.pt", voice) for voice in self.allowed_voices
        ):
            raise ValueError("ALLOWED_VOICES must contain safe .pt filenames")
        if self.vih_dial_mode not in {"mock", "live"}:
            raise ValueError("VIH_DIAL_MODE must be mock or live")
        if self.vih_phone_format not in {"e164", "india_national"}:
            raise ValueError("VIH_PHONE_FORMAT must be e164 or india_national")
        if self.vih_cdr_auth_mode not in {"disabled", "bearer", "query"}:
            raise ValueError("VIH_CDR_AUTH_MODE must be disabled, bearer, or query")
        if self.vih_cdr_auth_mode != "disabled" and len(self.vih_cdr_secret) < 32:
            raise ValueError("VIH_CDR_SECRET must be at least 32 characters when CDR is enabled")
        if self.vih_wss_value_mode not in {"unconfirmed", "confirmed_static", "confirmed_dynamic"}:
            raise ValueError("VIH_WSS_VALUE_MODE must be unconfirmed, confirmed_static, or confirmed_dynamic")
        if self.enable_live_dials:
            obd = urlparse(self.vih_obd_url)
            if (
                self.vih_dial_mode != "live"
                or obd.scheme != "https"
                or not obd.hostname
                or not self.vih_ukey
                or not self.vih_service_no
                or not self.vih_template_id
                or self.vih_campaign_type not in {"3", "4"}
                or self.vih_wss_value_mode == "unconfirmed"
                or not self.vih_wss_value
                or self.vih_cdr_auth_mode == "disabled"
                or (not self.vih_response_call_id_path.startswith("/") and not self.vih_response_campaign_id_path.startswith("/"))
            ):
                raise ValueError("live dialing requires confirmed ViH URL, credentials, WSS, and CDR auth")
        if self.vih_dial_mode == "live" and not self.enable_live_dials:
            raise ValueError("VIH_DIAL_MODE=live requires ENABLE_LIVE_DIALS=true")
        if self.vih_wss_value_mode == "confirmed_dynamic" and (
            "{callId}" not in self.vih_wss_value or "{token}" not in self.vih_wss_value
        ):
            raise ValueError("confirmed dynamic WSS value must contain {callId} and {token}")
        if self.vih_wss_value_mode == "confirmed_dynamic":
            try:
                rendered = self.vih_wss_value.format(callId="call", token="token")
            except (KeyError, ValueError):
                raise ValueError("VIH_WSS_VALUE dynamic template has unsupported placeholders") from None
            expected = self.public_stream_base_url.rstrip("/") + "/calls/call?token=token"
            if self.media_auth_mode != "query" or rendered != expected:
                raise ValueError("dynamic ViH WSS must exactly be PUBLIC_STREAM_BASE_URL/calls/{callId}?token={token} with query auth")
        if self.enable_live_dials and self.vih_wss_value_mode != "confirmed_dynamic":
            raise ValueError("this POC supports live ViH only with confirmed dynamic WSS correlation")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            control_host=getenv("CONTROL_HOST", "127.0.0.1"),
            control_port=int(getenv("CONTROL_PORT", "8080")),
            media_host=getenv("MEDIA_HOST", "127.0.0.1"),
            media_port=int(getenv("MEDIA_PORT", "8081")),
            public_stream_base_url=getenv("PUBLIC_STREAM_BASE_URL", "wss://bridge.example.com"),
            bridge_api_key=getenv("BRIDGE_API_KEY", ""),
            media_auth_mode=getenv("MEDIA_AUTH_MODE", "header"),
            session_ttl_seconds=int(getenv("SESSION_TTL_SECONDS", "300")),
            max_stored_sessions=int(getenv("MAX_STORED_SESSIONS", "1000")),
            personaplex_url=getenv("PERSONAPLEX_URL", "ws://127.0.0.1:8998/api/chat"),
            allowed_voices=frozenset(filter(None, getenv("ALLOWED_VOICES", "").split(","))),
            controller_callback_base_url=getenv(
                "CONTROLLER_CALLBACK_BASE_URL", "http://127.0.0.1:9000"
            ),
            controller_api_key=getenv("CONTROLLER_API_KEY", ""),
            max_frame_bytes=int(getenv("MAX_FRAME_BYTES", "16384")),
            max_transcript_chars=int(getenv("MAX_TRANSCRIPT_CHARS", "32768")),
            ready_timeout_seconds=float(getenv("PERSONAPLEX_READY_TIMEOUT_SECONDS", "15")),
            idle_timeout_seconds=float(getenv("IDLE_TIMEOUT_SECONDS", "60")),
            close_timeout_seconds=float(getenv("CLOSE_TIMEOUT_SECONDS", "5")),
            max_call_seconds=int(getenv("MAX_CALL_SECONDS", "1800")),
            database_path=getenv("DATABASE_PATH", "voice-harness.sqlite3"),
            vih_dial_mode=getenv("VIH_DIAL_MODE", "mock"),
            enable_live_dials=getenv("ENABLE_LIVE_DIALS", "false").lower() == "true",
            vih_obd_url=getenv("VIH_OBD_URL", ""),
            vih_ukey=getenv("VIH_UKEY", ""),
            vih_service_no=getenv("VIH_SERVICE_NO", ""),
            vih_template_id=getenv("VIH_TEMPLATE_ID", ""),
            vih_campaign_type=getenv("VIH_CAMPAIGN_TYPE", ""),
            vih_wss_value_mode=getenv("VIH_WSS_VALUE_MODE", "unconfirmed"),
            vih_wss_value=getenv("VIH_WSS_VALUE", ""),
            vih_cdr_auth_mode=getenv("VIH_CDR_AUTH_MODE", "disabled"),
            vih_cdr_secret=getenv("VIH_CDR_SECRET", ""),
            vih_response_call_id_path=getenv("VIH_RESPONSE_CALL_ID_PATH", ""),
            vih_response_campaign_id_path=getenv("VIH_RESPONSE_CAMPAIGN_ID_PATH", ""),
            recording_dir=getenv("RECORDING_DIR", ""),
            vih_phone_format=getenv("VIH_PHONE_FORMAT", "e164"),
        )
