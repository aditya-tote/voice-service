"""Small durable store for outbound-call intent and provider evidence."""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .observability import log_event


class RepositoryError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status, self.detail = status, detail


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CallRepository:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            os.chmod(path, 0o600)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
              id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
              request_json TEXT NOT NULL, state TEXT NOT NULL, simulated INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, dial_status TEXT,
              dial_request TEXT, dial_response TEXT, call_sid TEXT UNIQUE, campaign_id TEXT UNIQUE, stream_sid TEXT UNIQUE, cdr_json TEXT,
              disposition TEXT, agent_text TEXT NOT NULL DEFAULT '', stream_reason TEXT,
              recovery_required INTEGER NOT NULL DEFAULT 0, gpu_lease INTEGER NOT NULL DEFAULT 0,
              media_token_digest TEXT, stream_stats TEXT, recordings TEXT, stream_active INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS provider_events (
              event_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
              call_id TEXT REFERENCES calls(id), received_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(calls)")}
        for name, definition in {
            "media_token_digest": "TEXT", "stream_stats": "TEXT", "recordings": "TEXT",
            "stream_active": "INTEGER NOT NULL DEFAULT 0", "dial_request": "TEXT", "campaign_id": "TEXT",
        }.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS calls_campaign_id_unique ON calls(campaign_id) WHERE campaign_id IS NOT NULL")
        # Dialing is persisted before the provider POST. A leased, still-created
        # call crashed during model preparation and cannot have placed a call.
        self.connection.execute(
            "UPDATE calls SET state='failed', gpu_lease=0, recovery_required=0, "
            "media_token_digest=NULL, dial_status='runtime_not_ready', updated_at=? "
            "WHERE state='created' AND gpu_lease=1 AND dial_status IS NULL",
            (_now(),),
        )
        recovered = self.connection.execute(
            "UPDATE calls SET state='recovery_required', recovery_required=1, gpu_lease=1, stream_active=0, media_token_digest=NULL, updated_at=? "
            "WHERE gpu_lease=1 OR state IN ('dialing','dial_unknown','dial_submitted','connecting','live','stream_ended')",
            (_now(),),
        )
        self.connection.commit()
        if recovered.rowcount:
            log_event("recovery_detected", logging.WARNING, stage="database", recovered_calls=recovered.rowcount)

    @staticmethod
    def request_hash(request: dict[str, Any]) -> str:
        encoded = json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def create_or_replay(self, key: str, request: dict[str, Any], simulated: bool) -> tuple[dict[str, Any], bool]:
        digest = self.request_hash(request)
        found = self.connection.execute("SELECT * FROM calls WHERE idempotency_key=?", (key,)).fetchone()
        if found:
            if found["request_hash"] != digest:
                raise RepositoryError(409, "Idempotency-Key was already used with a different request")
            return dict(found), True
        now, identifier = _now(), "call_" + secrets.token_hex(18)
        self.connection.execute(
            "INSERT INTO calls (id,idempotency_key,request_hash,request_json,state,simulated,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (identifier, key, digest, json.dumps(request, separators=(",", ":")), "created", simulated, now, now),
        )
        self.connection.commit()
        return dict(self.connection.execute("SELECT * FROM calls WHERE id=?", (identifier,)).fetchone()), False

    def get(self, call_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row:
            raise RepositoryError(404, "call not found")
        return dict(row)

    def update(self, call_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        names = ", ".join(f"{name}=?" for name in fields)
        json_fields = {"request_json", "dial_request", "dial_response", "cdr_json", "stream_stats", "recordings"}
        values = [json.dumps(value, separators=(",", ":")) if name in json_fields and isinstance(value, (dict, list)) else value for name, value in fields.items()]
        values.append(call_id)
        if self.connection.execute(f"UPDATE calls SET {names} WHERE id=?", values).rowcount != 1:
            raise RepositoryError(404, "call not found")
        self.connection.commit()

    def acquire_gpu(self, call_id: str) -> bool:
        active = self.connection.execute("SELECT id FROM calls WHERE gpu_lease=1").fetchone()
        if active and active["id"] != call_id:
            return False
        self.update(call_id, gpu_lease=1)
        return True

    def release_gpu(self, call_id: str) -> None:
        self.update(call_id, gpu_lease=0)

    def set_media_token(self, call_id: str, token: str) -> None:
        self.update(call_id, media_token_digest=hashlib.sha256(token.encode()).hexdigest())

    def claim_media(self, call_id: str, token: str) -> dict[str, Any]:
        call = self.get(call_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not call["media_token_digest"] or not secrets.compare_digest(call["media_token_digest"], digest):
            raise RepositoryError(401, "invalid media credential")
        if call["stream_active"] or call["state"] not in {"dialing", "dial_submitted", "dial_unknown", "connecting"}:
            raise RepositoryError(409, "call media is not available")
        self.update(call_id, media_token_digest=None, stream_active=1, state="live", gpu_lease=1)
        return self.get(call_id)

    def bind_stream(self, call_id: str, start: dict[str, Any]) -> None:
        call = self.get(call_id)
        stream_sid = start.get("streamSid")
        call_sid = start.get("start", {}).get("callSid")
        if not isinstance(stream_sid, str) or not stream_sid:
            raise RepositoryError(422, "provider start lacks streamSid")
        for column, value in (("stream_sid", stream_sid), ("call_sid", call_sid)):
            if value and call[column] and call[column] != value:
                raise RepositoryError(409, "provider ID conflicts with existing call")
        self.bind_provider_ids(call_id, call_sid=call_sid, stream_sid=stream_sid)
        self.update(call_id, state="live", stream_active=1)

    def bind_provider_ids(self, call_id: str, *, call_sid: str | None = None, campaign_id: str | None = None, stream_sid: str | None = None) -> None:
        call = self.get(call_id)
        values = {"call_sid": call_sid, "campaign_id": campaign_id, "stream_sid": stream_sid}
        for column, value in values.items():
            if value and call[column] and call[column] != value:
                raise RepositoryError(409, "provider ID conflicts with existing call")
        self.update(call_id, **{column: value or call[column] for column, value in values.items()})
        call = self.get(call_id)
        # A terminal CDR can arrive before the stream start reaches VAH. Reconcile only exact provider IDs.
        events = self.connection.execute(
            "SELECT event_key,payload_json FROM provider_events WHERE kind='cdr' AND call_id IS NULL ORDER BY received_at"
        ).fetchall()
        for event in events:
            payload = json.loads(event["payload_json"])
            if self._matching_call(payload) == call_id:
                self.connection.execute("UPDATE provider_events SET call_id=? WHERE event_key=?", (call_id, event["event_key"]))
                self._apply_cdr(call_id, payload)

    def finish_media(self, call_id: str, reason: str, agent_text: str, stats: dict[str, Any], recordings: dict[str, Any]) -> None:
        call = self.get(call_id)
        has_cdr = bool(call["cdr_json"])
        # A validated stop is terminal evidence for this stream. A bare socket close
        # cannot prove the phone call ended; retain admission until reconciled.
        terminal = has_cdr or reason == "provider_stop"
        self.update(call_id, stream_active=0, agent_text=agent_text[:32768], stream_reason=reason[:256],
                    stream_stats=stats, recordings=recordings,
                    state="completed" if terminal else "recovery_required",
                    gpu_lease=0 if terminal else 1, recovery_required=0 if terminal else 1,
                    media_token_digest=None)

    def _matching_call(self, payload: dict[str, Any]) -> str | None:
        columns = {"callSid": "call_sid", "streamSid": "stream_sid", "campaignId": "campaign_id"}
        matched = set()
        for field, column in columns.items():
            if payload.get(field):
                found = self.connection.execute(f"SELECT id FROM calls WHERE {column}=?", (payload[field],)).fetchone()
                if found:
                    matched.add(found["id"])
        if len(matched) != 1:
            return None
        identifier = matched.pop()
        call = self.get(identifier)
        if any(payload.get(field) and call[column] and payload[field] != call[column]
               for field, column in columns.items()):
            return None
        return identifier

    def _apply_cdr(self, call_id: str, payload: dict[str, Any]) -> None:
        call = self.get(call_id)
        # Keep conflicting terminal evidence in provider_events; first accepted outcome wins.
        if call["cdr_json"]:
            self.connection.commit()
            return
        terminal = not call["stream_active"]
        self.update(call_id, cdr_json=payload, disposition=payload.get("disposition"),
                    call_sid=call["call_sid"] or payload.get("callSid"),
                    stream_sid=call["stream_sid"] or payload.get("streamSid"),
                    campaign_id=call["campaign_id"] or payload.get("campaignId"),
                    state="completed" if terminal else call["state"], gpu_lease=0 if terminal else 1,
                    media_token_digest=None if terminal else call["media_token_digest"],
                    recovery_required=0 if terminal else call["recovery_required"])

    def save_cdr(self, payload: dict[str, Any]) -> tuple[str | None, bool]:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        # Delivery timestamps change on retransmit; identity is the provider attempt/outcome.
        identity = {key: value for key, value in payload.items() if key != "webhookTimestamp"}
        event_key = hashlib.sha256(json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        duplicate = self.connection.execute("SELECT call_id FROM provider_events WHERE event_key=?", (event_key,)).fetchone()
        if duplicate:
            return duplicate["call_id"], True
        call_id = self._matching_call(payload)
        self.connection.execute(
            "INSERT INTO provider_events(event_key,kind,payload_json,call_id,received_at) VALUES(?,?,?,?,?)",
            (event_key, "cdr", raw, call_id, _now()),
        )
        if call_id:
            self._apply_cdr(call_id, payload)
        else:
            self.connection.commit()
        return call_id, False

    def result(self, call_id: str) -> dict[str, Any]:
        row = self.get(call_id)
        state = row["state"]
        has_cdr = bool(row["cdr_json"])
        request = json.loads(row.pop("request_json"))
        for name in ("dial_request", "dial_response", "cdr_json", "stream_stats", "recordings"):
            if row.get(name):
                row[name] = json.loads(row[name])
        return {
            "callId": row.pop("id"), "request": request, "simulated": bool(row.pop("simulated")),
            "state": row.pop("state"), "createdAt": row.pop("created_at"), "updatedAt": row.pop("updated_at"),
            "dial": {"request": row.pop("dial_request"), "status": row.pop("dial_status"), "response": row.pop("dial_response")},
            "provider": {"callSid": row.pop("call_sid"), "campaignId": row.pop("campaign_id"), "streamSid": row.pop("stream_sid")},
            "cdr": row.pop("cdr_json"), "disposition": row.pop("disposition"),
            "agentText": row.pop("agent_text"), "customerTranscript": None,
            "streamReason": row.pop("stream_reason"), "recoveryRequired": bool(row.pop("recovery_required")),
            "streamStats": row.pop("stream_stats"), "recordings": row.pop("recordings"),
            "complete": state in {"completed", "failed"},
            "correlationStatus": "matched" if has_cdr else (
                "not_received" if state in {"completed", "failed"} else "pending"
            ),
        }
