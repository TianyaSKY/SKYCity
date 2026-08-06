"""Agent snapshot domain models, including the action discriminator."""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class AgentActionMove(BaseModel):
    """An in-flight move (R1 exclusive; R6 duration already reflected in ends_at)."""

    # populate_by_name so the engine can construct with `from_=` while the wire
    # format keeps the `from` key (contract-compatible serialization).
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["move"]
    from_: list[int] = Field(alias="from")
    to: list[int]
    started_at: int
    ends_at: int
    reason: str | None = None


class AgentActionWait(BaseModel):
    """An in-flight wait (R1 interruptible)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["wait"]
    ends_at: int
    reason: str | None = None


class AgentActionWork(BaseModel):
    """An in-flight work shift (R3 exclusive; R10 settles at ends_at).

    ``job_name`` is resolved at snapshot time so clients can render a human
    label without a second lookup.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["work"]
    job_id: str
    job_name: str | None = None
    started_at: int
    ends_at: int
    reason: str | None = None


AgentAction = Union[AgentActionMove, AgentActionWait, AgentActionWork]


class InventoryEntrySnapshot(BaseModel):
    """One stack in an agent's inventory (M5 contract: item_id + quantity)."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    quantity: int


class AgentSnapshot(BaseModel):
    """One agent as exposed in snapshots and agent_state_changed events."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str
    col: int
    row: int
    location_id: str | None = None
    satiety: int
    energy: int
    mood: int
    loneliness: int
    money: int
    action: AgentAction | None = None
    inventory: list[InventoryEntrySnapshot] = Field(default_factory=list)
