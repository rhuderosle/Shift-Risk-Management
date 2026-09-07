from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["open", "mitigating", "closed"]


class RiskIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    area: str = "General"
    source: str = "manual"
    owner: str = ""
    shift: str = "1"
    severity: int = Field(default=3, ge=1, le=5)
    likelihood: int = Field(default=3, ge=1, le=5)
    status: Status = "open"
    impact_units: float = 0.0
    downtime_minutes: float = 0.0
    action: str = ""


class RiskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    area: Optional[str] = None
    owner: Optional[str] = None
    shift: Optional[str] = None
    severity: Optional[int] = Field(default=None, ge=1, le=5)
    likelihood: Optional[int] = Field(default=None, ge=1, le=5)
    status: Optional[Status] = None
    impact_units: Optional[float] = None
    downtime_minutes: Optional[float] = None
    action: Optional[str] = None


class ChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list)


class IngestBatch(BaseModel):
    """Payload for automated data aggregation from upstream systems (MES, tickets, alarms)."""

    source: str = "integration"
    risks: list[RiskIn]
