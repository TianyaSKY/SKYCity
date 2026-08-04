"""Location snapshot domain model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LocationSnapshot(BaseModel):
    """One location as exposed in snapshots."""

    model_config = ConfigDict(extra="forbid")

    location_id: str
    name: str
    location_type: str
    col: int
    row: int
    capacity: int
    open_hour: int
    close_hour: int
    open: bool
