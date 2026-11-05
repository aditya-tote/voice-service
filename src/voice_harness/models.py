import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator


class SessionState(StrEnum):
    STORED = "stored"
    LIVE = "live"
    ENDED = "ended"


class LeadContext(BaseModel):
    """Bounded customer facts, never caller-supplied conversation policy."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customerName: str = Field(min_length=1, max_length=120)
    companyName: str = Field(min_length=1, max_length=160)
    customerEmail: str = Field(
        min_length=3, max_length=254,
        description="ASCII mailbox: one @; local part <=64, common unquoted characters and + tags, "
        "no edge/consecutive dots; dotted DNS domain with labels <=63 and no edge hyphens. "
        "No whitespace/controls, quoted or Unicode mailboxes. Syntax only, not delivery verification; case preserved.",
    )
    source: str = Field(min_length=1, max_length=64, description="Open vocabulary; Unknown is allowed.")
    productInterest: str = Field(default="managed endpoint security", min_length=1, max_length=160)
    enquiryNotes: str | None = Field(default=None, min_length=1, max_length=400)
    decisionMaker: StrictBool | None = None
    existingSecuritySolution: str | None = Field(default=None, min_length=1, max_length=160)
    deviceCount: StrictInt | None = Field(default=None, ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def no_controls(cls, value):
        if isinstance(value, str) and any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("lead facts must not contain control characters")
        return value

    @field_validator("customerEmail")
    @classmethod
    def mailbox(cls, value: str) -> str:
        local, separator, domain = value.partition("@")
        labels = domain.split(".")
        if (
            not separator or len(local) > 64
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or len(labels) < 2
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)
        ):
            raise ValueError("customerEmail must be a supported ASCII mailbox")
        return value


class StartCall(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    leadId: str = Field(min_length=1, max_length=4096)
    customerPhone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    leadContext: LeadContext = Field(description="Brief customer facts only. Strings are trimmed; no control characters. "
                                  "Optional booleans/integers are strict. Extra fields are forbidden.")


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
