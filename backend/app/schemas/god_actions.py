"""God action request/response schemas (M7 god view)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GodActionRequest(BaseModel):
    """POST /api/worlds/{world_id}/god-actions body."""

    model_config = ConfigDict(extra="forbid")

    command_type: str
    target_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=256)


class GodActionResponse(BaseModel):
    """200 response: audit id, success flag, outcome summary + event stream."""

    command_id: str
    success: bool
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
