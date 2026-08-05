"""World REST schemas (create/list/get/pause/resume/speed)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateWorldRequest(BaseModel):
    """POST /api/worlds body; name is optional, autonomous joins the LLM loop."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128)
    autonomous: bool = False


class CreateWorldResponse(BaseModel):
    """201 response for a new world."""

    world_id: str
    world_time: int
    speed: int
    paused: bool
    autonomous: bool


class WorldInfo(BaseModel):
    """One world in GET /api/worlds and GET /api/worlds/{id}."""

    world_id: str
    name: str
    world_time: int
    speed: int
    paused: bool
    autonomous: bool


class OkResponse(BaseModel):
    """Generic {"ok": true} mutation response."""

    ok: Literal[True] = True


class SpeedRequest(BaseModel):
    """POST /api/worlds/{id}/speed body; only 1/2/5/10 are valid."""

    model_config = ConfigDict(extra="forbid")

    speed: Literal[1, 2, 5, 10]


class AutonomousRequest(BaseModel):
    """POST /api/worlds/{id}/autonomous body; enables the LLM decision loop."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
