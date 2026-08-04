"""Event envelope domain model (event-protocol.md §1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorldEventEnvelope(BaseModel):
    """The one envelope every event travels in (WS push, HTTP response, replay)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    sequence: int = Field(ge=1)
    world_id: str
    world_time: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
