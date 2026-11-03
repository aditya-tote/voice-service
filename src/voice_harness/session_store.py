import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

from .auth import matches
from .config import Settings
from .models import Brief, CreatedSession, SessionState, SessionStatus


class StoreError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


@dataclass
class Session:
    id: str
    brief: Brief | None
    token_digest: str | None
    expires: float
    state: SessionState = SessionState.STORED
    ended_at: datetime | None = None
    hangup_reason: str | None = None


class SessionStore:
    def __init__(self, settings: Settings):
        self.settings, self.items, self.lock = settings, {}, asyncio.Lock()
        self.live_id = None

    def _purge(self) -> None:
        now = time.monotonic()
        self.items = {
            key: item
            for key, item in self.items.items()
            if item.expires > now or item.state == SessionState.LIVE
        }

    def _status(self, item: Session) -> SessionStatus:
        return SessionStatus(
            sessionId=item.id,
            state=item.state,
            expiresAt=datetime.fromtimestamp(
                time.time() + max(0, item.expires - time.monotonic()), UTC
            ),
            endedAt=item.ended_at,
            hangupReason=item.hangup_reason,
        )

    async def create(self, brief: Brief) -> CreatedSession:
        if brief.voiceName not in self.settings.allowed_voices:
            raise StoreError(422, "voiceName is not allowed")
        async with self.lock:
            self._purge()
            if len(self.items) >= self.settings.max_stored_sessions:
                raise StoreError(503, "session capacity reached")
            identifier, token = "ses_" + secrets.token_hex(18), secrets.token_urlsafe(32)
            item = Session(
                identifier,
                brief,
                hashlib.sha256(token.encode()).hexdigest(),
                time.monotonic() + self.settings.session_ttl_seconds,
            )
            self.items[identifier] = item
            url = self.settings.public_stream_base_url.rstrip("/") + "/calls/" + identifier
            headers = {"Authorization": "Bearer " + token}
            if self.settings.media_auth_mode == "query":
                url += "?" + urlencode({"token": token})
                headers = None
            return CreatedSession(
                sessionId=identifier,
                streamUrl=url,
                streamHeaders=headers,
                expiresAt=self._status(item).expiresAt,
            )

    async def status(self, identifier: str) -> SessionStatus:
        async with self.lock:
            self._purge()
            item = self.items.get(identifier)
            if not item:
                raise StoreError(404, "session not found")
            return self._status(item)

    async def delete(self, identifier: str) -> None:
        async with self.lock:
            self._purge()
            item = self.items.get(identifier)
            if not item:
                raise StoreError(404, "session not found")
            if item.state != SessionState.STORED:
                raise StoreError(409, "only stored sessions may be deleted")
            del self.items[identifier]

    async def claim(self, identifier: str, token: str | None) -> Session:
        async with self.lock:
            self._purge()
            item = self.items.get(identifier)
            if not item:
                raise StoreError(404, "session not found")
            if item.state != SessionState.STORED:
                raise StoreError(409, "session is not available")
            if not item.token_digest or not matches(
                item.token_digest, hashlib.sha256((token or "").encode()).hexdigest()
            ):
                raise StoreError(401, "invalid media credential")
            if self.live_id:
                raise StoreError(409, "GPU already has a live call")
            item.state, self.live_id = SessionState.LIVE, identifier
            return item

    async def end(self, identifier: str, reason: str) -> Session | None:
        async with self.lock:
            item = self.items.get(identifier)
            if not item:
                return None
            if item.state != SessionState.LIVE:
                return item
            item.state, item.ended_at, item.hangup_reason = (
                SessionState.ENDED,
                datetime.now(UTC),
                reason,
            )
            if self.live_id == identifier:
                self.live_id = None
            item.expires = time.monotonic() + self.settings.session_ttl_seconds
            item.brief, item.token_digest = None, None
            return item
