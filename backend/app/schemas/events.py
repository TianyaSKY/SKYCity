"""Event query schema (gap recovery endpoint)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.event import WorldEventEnvelope


class EventsQuery(BaseModel):
    """Query params for GET /api/worlds/{id}/events."""

    after_sequence: int = Field(default=0, ge=0)


class EventsResponse(BaseModel):
    """List of envelopes after a sequence, ascending."""

    events: list[WorldEventEnvelope] = Field(default_factory=list)
