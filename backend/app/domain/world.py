"""World snapshot domain models (clock + full state payload)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.agent import AgentSnapshot
from app.domain.location import LocationSnapshot


class StructureSnapshot(BaseModel):
    """One tile of a placed blueprint on the map overlay (M14, R22).

    ``status`` is "building" (materials pre-deducted, completion pending) or
    "built". ``built_at`` is the world time of completion (None while
    building). Clients render ``blueprint_id`` via the blueprint catalog;
    the tileset gids live there, not in the snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    col: int
    row: int
    blueprint_id: str
    owner_agent_id: str
    status: str
    built_at: int | None = None


class CropSnapshot(BaseModel):
    """One planted crop (M15, R23).

    ``stage`` is the 0-based growth index; ``next_stage_at`` is the world
    time the current stage ends (None on the final, harvestable stage).
    Clients render the stage gid via the crop catalog.
    """

    model_config = ConfigDict(extra="forbid")

    col: int
    row: int
    item_id: str
    planted_by: str
    planted_at: int
    stage: int
    next_stage_at: int | None = None


class WorldSnapshot(BaseModel):
    """The clock/weather part of a snapshot payload."""

    model_config = ConfigDict(extra="forbid")

    world_id: str
    world_time: int
    speed: int
    paused: bool
    weather: str
    day: int


class StoreProductSnapshot(BaseModel):
    """One product on a store's shelf (M18 snapshot extension)."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    sell_price: int
    buy_price: int
    stock: int
    stock_cap: int


class StoreSnapshot(BaseModel):
    """One store in the world snapshot (M18): company stores and personal
    shops alike; personal shops carry owner_agent_id and name."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    name: str | None = None
    location_id: str
    owner_agent_id: str | None = None
    company_id: str | None = None
    products: list[StoreProductSnapshot] = Field(default_factory=list)


class WorldSnapshotPayload(BaseModel):
    """Full state sent once per WS connection (type=world_snapshot)."""

    model_config = ConfigDict(extra="forbid")

    world: WorldSnapshot
    agents: list[AgentSnapshot] = Field(default_factory=list)
    locations: list[LocationSnapshot] = Field(default_factory=list)
    structures: list[StructureSnapshot] = Field(default_factory=list)
    crops: list[CropSnapshot] = Field(default_factory=list)
    stores: list[StoreSnapshot] = Field(default_factory=list)
    latest_sequence: int = 0
