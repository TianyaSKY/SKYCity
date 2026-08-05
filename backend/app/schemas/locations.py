"""Location detail response schema: base fields + occupants + store products + jobs."""

from __future__ import annotations

from pydantic import BaseModel


class LocationOccupantInfo(BaseModel):
    agent_id: str
    name: str


class LocationProductInfo(BaseModel):
    item_id: str
    name: str
    sell_price: int
    buy_price: int
    stock: int


class LocationJobInfo(BaseModel):
    job_id: str
    name: str
    wage: int
    duration_minutes: int


class LocationDetail(BaseModel):
    location_id: str
    name: str
    location_type: str
    col: int
    row: int
    capacity: int
    open_hour: int
    close_hour: int
    open: bool
    occupants: list[LocationOccupantInfo]
    products: list[LocationProductInfo]
    jobs: list[LocationJobInfo]
