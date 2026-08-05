"""Seed loader: reads world_data economy JSON into plain structures (M5).

The engine calls these at world creation to seed per-world items, stores and
jobs. Results are cached per directory; the loaded structures are frozen
dicts so mutating a seed can never leak into the cache.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config.settings import get_settings


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=4)
def load_items(world_data_dir: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Item seeds: (item_id, name, category, hunger_restore, base_price)."""
    base = world_data_dir or Path(get_settings().world_data_dir)
    data = _load_json(base / "items" / "items.json")
    items = tuple(
        {
            "item_id": str(entry["item_id"]),
            "name": str(entry.get("name") or entry["item_id"]),
            "category": str(entry.get("category") or "material"),
            "hunger_restore": int(entry.get("hunger_restore") or 0),
            "base_price": int(entry.get("base_price") or 0),
        }
        for entry in data.get("items", [])
    )
    return items


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
                "products": [
                    {
                        "item_id": str(product["item_id"]),
                        "sell_price": int(product.get("sell_price") or 0),
                        "buy_price": int(product.get("buy_price") or 0),
                        "stock_cap": int(product.get("stock_cap") or 0),
                        "restock_daily": int(product.get("restock_daily") or 0),
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
