"""Seed loader: reads world_data economy JSON into plain structures (M5).

The engine calls these at world creation to seed per-world items, stores and
jobs. Results are cached per directory; the loaded structures are frozen
dicts so mutating a seed can never leak into the cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config.settings import get_settings


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class BlueprintDef:
    """One buildable structure blueprint (M14, R22).

    ``footprint`` is a tuple of (dcol, drow) offsets from the anchor cell the
    builder chooses; ``tile_gids`` maps each "dcol,drow" offset to the tileset
    gids the frontend renders for that cell (first gid is the default).
    ``blocking`` structures subtract from effective_walkable (R22.4/R22.6).
    """

    blueprint_id: str
    name: str
    footprint: tuple[tuple[int, int], ...]
    tile_gids: dict[str, tuple[int, ...]] = field(default_factory=dict)
    blocking: bool = False
    materials: dict[str, int] = field(default_factory=dict)
    duration_minutes: int = 30
    description: str = ""


@dataclass(frozen=True)
class CropDef:
    """One plantable crop (M15, R23).

    ``stages`` is a tuple of (minutes, gid) per growth stage — the stage
    lasts ``minutes`` world-minutes and renders the tileset ``gid``. The
    final stage is harvestable (R23.6). ``yield_items`` is the harvest
    product list before fertilizer bonuses.
    """

    seed_item_id: str
    name: str
    stages: tuple[tuple[int, int], ...]
    yield_items: tuple[tuple[str, int], ...]
    description: str = ""

    @property
    def total_minutes(self) -> int:
        """World minutes until the crop reaches the harvestable final stage.

        The final stage's own duration is the mature-sitting state (it lasts
        until harvest), so it is not part of the time-to-ripen.
        """
        return sum(minutes for minutes, _gid in self.stages[:-1])

    @property
    def stage_gids(self) -> tuple[int, ...]:
        return tuple(gid for _minutes, gid in self.stages)


@lru_cache(maxsize=4)
def load_items(world_data_dir: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Item seeds: (item_id, name, category, satiety_restore, base_price)."""
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "items" / "items.json")
    items = tuple(
        {
            "item_id": str(entry["item_id"]),
            "name": str(entry.get("name") or entry["item_id"]),
            "category": str(entry.get("category") or "material"),
            "satiety_restore": int(entry.get("satiety_restore") or 0),
            # M12: mood restore / work wage bonus % / extra yield per unit.
            "mood_restore": int(entry.get("mood_restore") or 0),
            "work_bonus": int(entry.get("work_bonus") or 0),
            "yield_bonus": int(entry.get("yield_bonus") or 0),
            "base_price": int(entry.get("base_price") or 0),
        }
        for entry in data.get("items", [])
    )
    return items


@lru_cache(maxsize=4)
def load_blueprints(world_data_dir: Path | None = None) -> tuple[BlueprintDef, ...]:
    """Blueprint seeds (M14): static build recipes — no per-world state."""
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "blueprints" / "blueprints.json")
    blueprints: list[BlueprintDef] = []
    for row in data.get("blueprints", []):
        blueprints.append(
            BlueprintDef(
                blueprint_id=str(row["blueprint_id"]),
                name=str(row.get("name") or row["blueprint_id"]),
                footprint=tuple(tuple(int(v) for v in cell) for cell in row.get("footprint", [[0, 0]])),
                tile_gids={
                    key: tuple(int(gid) for gid in gids)
                    for key, gids in (row.get("tile_gids") or {}).items()
                },
                blocking=bool(row.get("blocking") or False),
                materials={str(k): int(v) for k, v in (row.get("materials") or {}).items()},
                duration_minutes=int(row.get("duration_minutes") or 30),
                description=str(row.get("description") or ""),
            )
        )
    return tuple(blueprints)


@lru_cache(maxsize=4)
def load_jobs(world_data_dir: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Job seeds: (job_id, name, location_id, interactable_id, duration, wage,
    energy cost, products)."""
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "jobs" / "jobs.json")
    jobs = tuple(
        {
            "job_id": str(entry["job_id"]),
            "name": str(entry.get("name") or entry["job_id"]),
            "location_id": str(entry.get("location_id") or ""),
            "interactable_id": str(entry.get("interactable_id") or ""),
            "duration_minutes": int(entry.get("duration_minutes") or 0),
            "wage": int(entry.get("wage") or 0),
            "energy_cost_per_hour": int(entry.get("energy_cost_per_hour") or 0),
            "products": list(entry.get("products") or []),
        }
        for entry in data.get("jobs", [])
    )
    return jobs


@lru_cache(maxsize=4)
def load_stores(world_data_dir: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Store seeds from stores/*.json, each with its products list."""
    base = world_data_dir or Path(get_settings().world_data_dir)
    stores_dir = base / "stores"
    stores: list[dict[str, Any]] = []
    for path in sorted(stores_dir.glob("*.json")):
        data = _load_json(path)
        stores.append(
            {
                "store_id": str(data.get("store_id") or path.stem),
                "location_id": str(data.get("location_id") or path.stem),
                "company_id": data.get("company_id") or None,
                "products": [
                    {
                        "item_id": str(product["item_id"]),
                        "sell_price": int(product.get("sell_price") or 0),
                        "buy_price": int(product.get("buy_price") or 0),
                        "stock_cap": int(product.get("stock_cap") or 0),
                        "restock_daily": int(product.get("restock_daily") or 0),
                        # M15: pure agent-produce sinks (e.g. wheat) start
                        # empty so the shop can actually absorb sales; shop
                        # goods default to full stock (R15).
                        "initial_stock": (
                            int(product["initial_stock"])
                            if "initial_stock" in product
                            else int(product.get("stock_cap") or 0)
                        ),
                    }
                    for product in data.get("products", [])
                ],
            }
        )
    return tuple(stores)


@lru_cache(maxsize=4)
def load_stocks(world_data_dir: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Stock seeds (M10): listed town companies with base prices.

    Each entry: (stock_id, name, company_id, source, base_price,
    outstanding_shares). ``source`` is "store" or "job" — it selects which
    business events move the price (item_purchased vs work_completed).
    """
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "stocks" / "stocks.json")
    stocks = tuple(
        {
            "stock_id": str(entry["stock_id"]),
            "name": str(entry.get("name") or entry["stock_id"]),
            "company_id": str(entry.get("company_id") or ""),
            "source": str(entry.get("source") or "store"),
            "base_price": int(entry.get("base_price") or 0),
            "outstanding_shares": int(entry.get("outstanding_shares") or 0),
        }
        for entry in data.get("stocks", [])
    )
    return stocks


@lru_cache(maxsize=4)
def load_crops(world_data_dir: Path | None = None) -> tuple[CropDef, ...]:
    """Crop seeds (M15): plantable seeds -> growth stages + harvest yield.

    Also carries the planting-zone config (R23.2): ``plant_radius`` around
    the ``farm_field`` interactable.
    """
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "crops" / "crops.json")
    return tuple(
        CropDef(
            seed_item_id=str(row["seed_item_id"]),
            name=str(row.get("name") or row["seed_item_id"]),
            stages=tuple(
                (int(minutes), int(gid)) for minutes, gid in row.get("stages", [])
            ),
            yield_items=tuple(
                (str(entry["item_id"]), int(entry.get("quantity") or 1))
                for entry in row.get("yield", [])
            ),
            description=str(row.get("description") or ""),
        )
        for row in data.get("crops", [])
    )


@lru_cache(maxsize=4)
def load_crop_config(world_data_dir: Path | None = None) -> dict[str, Any]:
    """Crop zone config (R23.2): plant_radius + farm_field_id."""
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "crops" / "crops.json")
    return {
        "plant_radius": int(data.get("plant_radius") or 4),
        "farm_field_id": str(data.get("farm_field_id") or "farm_field"),
    }
