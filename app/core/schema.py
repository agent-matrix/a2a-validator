from __future__ import annotations

from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------
# Planning schema
# ---------------------------

class Health(BaseModel):
    score: Optional[float] = None
    status: Optional[str] = None
    last_checked: Optional[str] = None  # or use datetime if preferred


class RecentCheck(BaseModel):
    check: str
    result: str
    latency_ms: Optional[float] = None
    ts: Optional[str] = None  # or use datetime if preferred


class PlanContext(BaseModel):
    """
    Context is permissive: accept any extra keys from Guardian (or future sources).
    Known fields are typed below; unknown fields pass through.
    """
    model_config = ConfigDict(extra="allow")

    # Common identifiers
    app_id: Optional[str] = None
    entity_uid: Optional[str] = None

    # Known structured bits
    symptoms: Optional[List[str]] = None
    lkg: Optional[str] = None
    lkg_version: Optional[str] = None
    health: Optional[Health] = None
    recent_checks: Optional[List[RecentCheck]] = None


class PlanConstraints(BaseModel):
    max_steps: int = Field(default=3, ge=1, le=10)
    risk: Literal["low", "medium", "high"] = "low"


class PlanRequest(BaseModel):
    # default to "plan" and only allow that value for now
    mode: Literal["plan"] = "plan"
    context: PlanContext
    constraints: PlanConstraints = Field(default_factory=PlanConstraints)


class PlanResponse(BaseModel):
    plan_id: str
    steps: List[str]
    risk: str
    explanation: str


# ---------------------------
# Chat (kept for compatibility; router uses its own flexible model)
# ---------------------------

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=512)


class ChatResponse(BaseModel):
    answer: str
    sources: List[str] = Field(default_factory=list)
