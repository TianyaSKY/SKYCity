"""Action request/response schemas (manual test API, no LLM in M2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.event import WorldEventEnvelope


class ActionRequest(BaseModel):
    """POST /api/worlds/{id}/agents/{agent_id}/actions body."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["move", "wait", "talk"]
    destination_id: str | None = None
    minutes: int | None = Field(default=None, ge=1)
    reason: str | None = None
    target_agent_id: str | None = None
    message: str | None = Field(default=None, max_length=200)
    intent: Literal["greet", "chat", "ask", "offer", "leave"] | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "ActionRequest":
        if self.action_type == "move" and not self.destination_id:
            raise ValueError("move requires destination_id")
        if self.action_type == "talk" and not self.target_agent_id:
            raise ValueError("talk requires target_agent_id")
        if self.action_type == "talk" and not self.message:
            raise ValueError("talk requires message")
        return self


class ActionSuccess(BaseModel):
    """200 response: the action was accepted, envelope describes it."""

    success: Literal[True] = True
    event: WorldEventEnvelope


class ActionRejected(BaseModel):
    """409 response: a world rule rejected the action (R1/R6/R8/pause)."""

    success: Literal[False] = False
    reason: str
