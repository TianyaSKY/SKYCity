"""Save/restore/replay REST schemas (M9)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SaveResponse(BaseModel):
    """201 response for POST /api/worlds/{world_id}/save."""

    save_id: str
    world_id: str
    created_at: int  # world_time the save was taken at


class RestoreRequest(BaseModel):
    """POST /api/worlds/restore body."""

    model_config = ConfigDict(extra="forbid")

    save_id: str


class RestoreResponse(BaseModel):
    """201 response: the restored world starts running (paused always False)."""

    world_id: str
    save_id: str
    world_time: int
    speed: int
    paused: bool
    autonomous: bool


class SaveInfo(BaseModel):
    """One entry in GET /api/saves (newest first)."""

    save_id: str
    world_id: str
    created_at: int
