"""World snapshot domain models (clock + full state payload)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.agent import AgentSnapshot
from app.domain.location import LocationSnapshot


class WorldSnapshot(BaseModel):
    """The clock/weather part of a snapshot payload."""

    model_config = ConfigDict(extra="forbid")

    world_id: str
    world_time: int
    speed: int
    paused: bool
    weather: str
    day: int


class WorldSnapshotPayload(BaseModel):
    """Full state sent once per WS connection (type=world_snapshot)."""

    model_config = ConfigDict(extra="forbid")

    world: WorldSnapshot
    agents: list[AgentSnapshot] = Field(default_factory=list)
    locations: list[LocationSnapshot] = Field(default_factory=list)
    latest_sequence: int = 0
