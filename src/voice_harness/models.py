import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionState(StrEnum):
    STORED = "stored"
    LIVE = "live"
    ENDED = "ended"


class CallContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    persona: str = Field(min_length=1, max_length=4096)
    goal: str = Field(min_length=1, max_length=4096)
    tone: str = Field(min_length=1, max_length=4096)
    facts: list[str] = Field(default_factory=list, max_length=50)
    limits: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("facts", "limits")
    @classmethod
    def bounded_items(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 4096 for item in values):
            raise ValueError("items must be nonempty strings of at most 4096 characters")
        return values


class StartCall(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True,
        json_schema_extra={"anyOf": [
            {"required": ["script"], "properties": {"script": {"type": "string"}}},
            {"required": ["context"], "properties": {"context": {"type": "object"}}},
        ]},
    )
    leadId: str = Field(min_length=1, max_length=4096)
    customerPhone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    script: str | None = Field(default=None, min_length=1, max_length=24000)
    context: CallContext | None = None
    voiceName: str = Field(min_length=1, max_length=128)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=25)

    @model_validator(mode="after")
    def instructions_required(self) -> Self:
        if self.script is None and self.context is None:
            raise ValueError("Provide a nonempty script or context (or both)")
        return self

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", key) or len(item) > 1024 for key, item in value.items()):
            raise ValueError("metadata must contain safe keys and bounded string values")
        return value


class StartCallResponse(BaseModel):
    callId: str
    state: str
    simulated: bool
    resultUrl: str
    createdAt: datetime


class Cdr(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["cdr"]
    callSid: str = Field(min_length=1, max_length=256)
    streamSid: str | None = Field(default=None, max_length=256)
    accountSid: str | None = Field(default=None, max_length=256)
    from_: str | None = Field(default=None, alias="from", max_length=64)
    to: str | None = Field(default=None, max_length=64)
    direction: str | None = Field(default=None, max_length=32)
    disposition: Literal["ANSWERED", "NO_ANSWER", "BUSY", "FAILED", "CANCELLED"]
    disconnectReason: str | None = Field(default=None, max_length=512)
    durationSeconds: int | None = Field(default=None, ge=0, le=86400)
    retryAttempt: int | None = Field(default=None, ge=0, le=2)
    callStartTime: str | None = Field(default=None, max_length=64)
    callAnswerTime: str | None = Field(default=None, max_length=64)
    callEndTime: str | None = Field(default=None, max_length=64)
    channelId: str | None = Field(default=None, max_length=256)
    campaignId: str | None = Field(default=None, max_length=256)
    webhookTimestamp: str | None = Field(default=None, max_length=64)
    customParameters: dict[str, Any] | None = None


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    leadId: str = Field(min_length=1, max_length=4096)
    persona: str = Field(min_length=1, max_length=4096)
    goal: str = Field(min_length=1, max_length=4096)
    tone: str = Field(min_length=1, max_length=4096)
    facts: list[str] = Field(max_length=50)
    limits: list[str] = Field(max_length=50)
    voiceName: str = Field(min_length=1, max_length=128)

    @field_validator("facts", "limits")
    @classmethod
    def valid_items(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 4096 for item in values):
            raise ValueError("items must be nonempty strings of at most 4096 characters")
        return values


class CreatedSession(BaseModel):
    sessionId: str
    streamUrl: str
    streamHeaders: dict[str, str] | None = None
    expiresAt: datetime


class SessionStatus(BaseModel):
    sessionId: str
    state: SessionState
    expiresAt: datetime
    endedAt: datetime | None = None
    hangupReason: str | None = None
