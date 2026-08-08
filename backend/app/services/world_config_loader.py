"""World configuration loader.

Parses and validates the Tiled town map (tiny_world.tmj) plus the asset
manifest (asset-manifest.json), resolving external tilesets and images by
relative path, and produces a :class:`ParsedWorldConfig` consumed by the
backend world engine and /health.

Agent spawn points are NOT read from the map: each agent's spawn (and optional
home) lives in its character card (world_data/identities/*.json), the single
source of truth. The map's spawn_points object layer is decorative and is
regenerated from those cards by tools/build_map.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict

from app.config.settings import Settings

# Map generator contract (world_data/maps): tiles are 16px, and the collision /
# navigation layers are marker layers that only ever contain gid 133 (or 0).
TILE_SIZE = 16
MARKER_GID = 133

_EXPECTED_LOCATION_IDS = {
    "village_shop",
    "village_farm",
    "village_plaza",
    "town_hall",
    "village_hotel",
    "village_bakery",
    "linxia_home",
    "zhangming_home",
    "chenyu_home",
    "wangfang_home",
}


class WorldConfigError(Exception):
    """Raised when world data is missing, unreadable, or structurally invalid."""


# --------------------------------------------------------------------------- #
# Manifest models
# --------------------------------------------------------------------------- #


class ManifestAsset(BaseModel):
    """One entry of asset-manifest.json."""

    model_config = ConfigDict(extra="ignore")

    alias: str
    file: str
    kind: str


class AssetManifest(BaseModel):
    """Root of world_data/asset-manifest.json."""

    model_config = ConfigDict(extra="ignore")

    manifest_version: str
    map_version: str
    tile_width: int
    tile_height: int
    assets: list[ManifestAsset]


# --------------------------------------------------------------------------- #
# Parsed world structures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TilesetInfo:
    """An external tileset resolved from the map's tileset references."""

    name: str
    firstgid: int
    tile_count: int
    image: str  # absolute path to the tileset image, "" when none


@dataclass(frozen=True)
class LocationDef:
    location_id: str
    name: str
    location_type: str
    capacity: int
    open_hour: int
    close_hour: int
    col: int
    row: int


@dataclass(frozen=True)
class InteractableDef:
    object_id: str
    object_type: str
    location_id: str | None
    col: int
    row: int


@dataclass(frozen=True)
class SpawnPointDef:
    spawn_id: str
    agent_id: str
    direction: str
    col: int
    row: int
    # Home location declared on the character card; None when the agent has
    # no home (it spawns standing at the spawn cell instead).
    home_id: str | None = None


@dataclass(frozen=True)
class ParsedWorldConfig:
    """Validated world data, ready for gameplay logic."""

    map_version: str
    width: int
    height: int
    tile_size: int
    gid_to_tileset: dict[int, str] = field(default_factory=dict)
    tilesets: dict[str, TilesetInfo] = field(default_factory=dict)
    tile_layers: dict[str, list[list[int]]] = field(default_factory=dict)
    locations: list[LocationDef] = field(default_factory=list)
    interactables: list[InteractableDef] = field(default_factory=list)
    spawn_points: list[SpawnPointDef] = field(default_factory=list)
    walkable_cells: frozenset[tuple[int, int]] = frozenset()
    collision_cells: frozenset[tuple[int, int]] = frozenset()


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise WorldConfigError(f"World data file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorldConfigError(f"Invalid JSON in {path}: {exc}") from exc


def _typed_value(prop: dict[str, Any]) -> Any:
    """Coerce a Tiled property value according to its declared type."""
    type_name = prop.get("type", "string")
    value = prop.get("value")
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    if type_name == "bool":
        return bool(value)
    return value if isinstance(value, str) else str(value)


def _properties(obj: dict[str, Any]) -> dict[str, Any]:
    return {prop["name"]: _typed_value(prop) for prop in (obj.get("properties") or [])}


def _cell(obj: dict[str, Any], tile_size: int) -> tuple[int, int]:
    """Convert pixel x/y to tile col/row (floor division)."""
    return (int(obj["x"]) // tile_size, int(obj["y"]) // tile_size)


def _load_tilesets(
        map_dir: Path, raw_tilesets: list[dict[str, Any]]
) -> tuple[dict[str, TilesetInfo], dict[int, str]]:
    """Resolve external tilesets relative to the map file's directory.

    Returns (tilesets by name, full gid->tileset-name mapping).
    """
    tilesets: dict[str, TilesetInfo] = {}
    for entry in raw_tilesets:
        firstgid = int(entry["firstgid"])
        if "source" in entry:
            tsj_path = map_dir / entry["source"]
            tsj = _load_json(tsj_path)
            name = str(tsj.get("name") or tsj_path.stem)
            tile_count = int(tsj.get("tilecount", 1))
            image = tsj.get("image")
            image_path = str((tsj_path.parent / image).resolve()) if image else ""
        else:  # inline tileset (not used by this project, handled defensively)
            name = str(entry.get("name") or f"tileset_{firstgid}")
            tile_count = int(entry.get("tilecount", 1))
            image = entry.get("image")
            image_path = str((map_dir / image).resolve()) if image else ""

        if name in tilesets:
            raise WorldConfigError(f"Duplicate tileset name in map: {name}")
        tilesets[name] = TilesetInfo(
            name=name, firstgid=firstgid, tile_count=tile_count, image=image_path
        )

    gid_to_tileset: dict[int, str] = {}
    for name, info in tilesets.items():
        for gid in range(info.firstgid, info.firstgid + info.tile_count):
            gid_to_tileset[gid] = name
    return tilesets, gid_to_tileset


def _tile_layer_grid(
        data: list[int], width: int, height: int, layer_name: str
) -> list[list[int]]:
    if len(data) != width * height:
        raise WorldConfigError(
            f"Tile layer '{layer_name}': data length {len(data)} "
            f"does not match {width}x{height}"
        )
    return [data[row * width: (row + 1) * width] for row in range(height)]


def _marker_cells(grid: list[list[int]], width: int, height: int) -> set[tuple[int, int]]:
    """Cells holding any marker gid (the generator only writes MARKER_GID)."""
    return {
        (col, row)
        for row in range(height)
        for col in range(width)
        if grid[row][col] != 0
    }


def _check_bounds(
        col: int, row: int, width: int, height: int, what: str
) -> None:
    if not (0 <= col < width and 0 <= row < height):
        raise WorldConfigError(f"{what} at ({col},{row}) is outside the {width}x{height} map")


def _load_character_cards(world_data_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Load every character card from world_data/identities/*.json.

    A character card is the single source of truth for an agent: identity
    fields, its spawn point and (optionally) its home. Returns ``(agent_id,
    card)`` pairs sorted by agent_id (the file name wins; the card's ``id``
    field, when present, must match it).
    """
    cards_dir = world_data_dir / "identities"
    if not cards_dir.is_dir():
        raise WorldConfigError(f"identities directory not found: {cards_dir}")
    cards: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(cards_dir.glob("agent_*.json")):
        agent_id = path.stem
        raw = _load_json(path)
        if not isinstance(raw, dict):
            raise WorldConfigError(f"角色卡 {agent_id} 必须是 JSON 对象")
        card_id = raw.get("id")
        if card_id is not None and str(card_id) != agent_id:
            raise WorldConfigError(
                f"角色卡 id 与文件名不一致: {card_id!r} != {agent_id!r}"
            )
        cards.append((agent_id, raw))
    return cards


def _card_spawn_def(
        agent_id: str, card: dict[str, Any], width: int, height: int
) -> SpawnPointDef:
    """Build an agent's spawn definition from its character card."""
    spawn = card.get("spawn")
    if not isinstance(spawn, dict):
        raise WorldConfigError(f"角色卡 {agent_id} 缺少 spawn 出生点定义")
    try:
        col = int(spawn["col"])
        row = int(spawn["row"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorldConfigError(f"角色卡 {agent_id} 的 spawn.col/row 无效") from exc
    _check_bounds(col, row, width, height, f"spawn point {agent_id}")
    home_id: str | None = None
    if card.get("home") is not None:
        home_id = str(card["home"].get("location_id") or "")
        if not home_id:
            raise WorldConfigError(f"角色卡 {agent_id} 的 home 缺少 location_id")
    return SpawnPointDef(
        spawn_id=f"spawn_{agent_id.removeprefix('agent_')}",
        agent_id=agent_id,
        direction=str(spawn.get("direction") or "down"),
        col=col,
        row=row,
        home_id=home_id,
    )


def _parse_map(map_path: Path, map_version: str) -> ParsedWorldConfig:
    raw = _load_json(map_path)
    width = int(raw["width"])
    height = int(raw["height"])
    tile_size = int(raw.get("tilewidth", TILE_SIZE))

    tilesets, gid_to_tileset = _load_tilesets(map_path.parent, raw.get("tilesets") or [])

    tile_layers: dict[str, list[list[int]]] = {}
    locations: list[LocationDef] = []
    interactables: list[InteractableDef] = []
    navigation_cells: set[tuple[int, int]] = set()
    collision_cells: set[tuple[int, int]] = set()

    for layer in raw.get("layers") or []:
        layer_type = layer.get("type")
        name = str(layer.get("name", ""))

        if layer_type == "tilelayer":
            data = layer.get("data")
            if not isinstance(data, list):
                raise WorldConfigError(
                    f"Tile layer '{name}' uses unsupported data encoding "
                    f"(expected a plain gid array)"
                )
            grid = _tile_layer_grid(data, width, height, name)
            tile_layers[name] = grid
            if name == "navigation":
                navigation_cells = _marker_cells(grid, width, height)
            elif name == "collision":
                collision_cells = _marker_cells(grid, width, height)

        elif layer_type == "objectgroup":
            for obj in layer.get("objects") or []:
                col, row = _cell(obj, tile_size)
                object_type = obj.get("type")

                if object_type == "location":
                    props = _properties(obj)
                    location_id = str(props.get("location_id") or "")
                    if not location_id:
                        raise WorldConfigError(
                            f"Location object '{obj.get('name')}' is missing location_id"
                        )
                    _check_bounds(col, row, width, height, f"location {location_id}")
                    locations.append(
                        LocationDef(
                            location_id=location_id,
                            name=str(props.get("name") or location_id),
                            location_type=str(props.get("location_type") or "unknown"),
                            capacity=int(props.get("capacity") or 0),
                            open_hour=int(props.get("open_hour") or 0),
                            close_hour=int(props.get("close_hour") or 0),
                            col=col,
                            row=row,
                        )
                    )

                elif object_type == "interactable":
                    props = _properties(obj)
                    object_id = str(props.get("object_id") or obj.get("name") or "")
                    if not object_id:
                        raise WorldConfigError("Interactable object is missing object_id")
                    _check_bounds(col, row, width, height, f"interactable {object_id}")
                    interactables.append(
                        InteractableDef(
                            object_id=object_id,
                            object_type=str(props.get("object_type") or "unknown"),
                            location_id=(
                                str(props["location_id"])
                                if props.get("location_id")
                                else None
                            ),
                            col=col,
                            row=row,
                        )
                    )

                elif object_type == "spawn_point":
                    # Decorative only: spawn positions come from the character
                    # cards (see _load_world_cached); skip without noise.
                    pass

                else:
                    logger.warning(
                        "Ignoring object '{}' with unknown type '{}' in layer '{}'",
                        obj.get("name"),
                        object_type,
                        name,
                    )

    # Walkability invariant (map generator guarantees navigation ∩ collision = ∅):
    # a cell is walkable iff it has a navigation marker and no collision marker.
    walkable_cells = frozenset(navigation_cells - collision_cells)
    collision_cells = frozenset(collision_cells)

    return ParsedWorldConfig(
        map_version=map_version,
        width=width,
        height=height,
        tile_size=tile_size,
        gid_to_tileset=gid_to_tileset,
        tilesets=tilesets,
        tile_layers=tile_layers,
        locations=locations,
        interactables=interactables,
        walkable_cells=walkable_cells,
        collision_cells=collision_cells,
    )


def _load_manifest(world_data_dir: Path) -> AssetManifest:
    raw = _load_json(world_data_dir / "asset-manifest.json")
    try:
        return AssetManifest.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise WorldConfigError(f"Invalid asset-manifest.json: {exc}") from exc


# --------------------------------------------------------------------------- #
# Public entry points (singleton via lru_cache)
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=4)
def _load_world_cached(world_data_dir: Path, map_name: str) -> ParsedWorldConfig:
    """Parse once per (world_data_dir, map_name); callers share the result."""
    if not world_data_dir.is_dir():
        raise WorldConfigError(
            f"WORLD_DATA_DIR does not exist or is not a directory: {world_data_dir}"
        )
    manifest = _load_manifest(world_data_dir)
    map_path = world_data_dir / "maps" / f"{map_name}.tmj"
    if not map_path.is_file():
        raise WorldConfigError(
            f"Map file not found: {map_path} (MAP_NAME={map_name!r})"
        )
    world = _parse_map(map_path, manifest.map_version)

    # Spawn points come from the character cards (single source of truth);
    # the map's spawn_points layer is decorative and ignored.
    card_spawns = [
        _card_spawn_def(agent_id, card, world.width, world.height)
        for agent_id, card in _load_character_cards(world_data_dir)
    ]
    if card_spawns:
        world = replace(world, spawn_points=card_spawns)

    found = {loc.location_id for loc in world.locations}
    missing = _EXPECTED_LOCATION_IDS - found
    if missing:
        raise WorldConfigError(
            f"Map {map_name} is missing expected locations: {sorted(missing)}"
        )
    logger.info(
        "Loaded world {} ({}x{}, {} tileset(s), {} locations, {} interactables, "
        "{} spawn points, {} walkable cells)",
        map_name,
        world.width,
        world.height,
        len(world.tilesets),
        len(world.locations),
        len(world.interactables),
        len(world.spawn_points),
        len(world.walkable_cells),
    )
    # M17: spawn cells must live in the main walkable component — an agent
    # born on an island can never leave (pathfinding is pure-walkable).
    from collections import deque

    spawns = [(sp.col, sp.row) for sp in world.spawn_points]
    if spawns and all(s in world.walkable_cells for s in spawns):
        start = spawns[0]
        seen = {start}
        frontier: deque[tuple[int, int]] = deque([start])
        while frontier:
            col, row = frontier.popleft()
            for dcol in (-1, 0, 1):
                for drow in (-1, 0, 1):
                    if dcol == 0 and drow == 0:
                        continue
                    neighbour = (col + dcol, row + drow)
                    if neighbour in seen or neighbour not in world.walkable_cells:
                        continue
                    seen.add(neighbour)
                    frontier.append(neighbour)
        stranded = [sp.spawn_id for sp in world.spawn_points if (sp.col, sp.row) not in seen]
        if stranded:
            logger.warning(
                "Map {}: spawn points NOT connected to the main walkable "
                "network (agents can never leave): {}",
                map_name,
                sorted(stranded),
            )
    return world


def load_world_config(settings: Settings) -> ParsedWorldConfig:
    """Load (and cache) the parsed world configuration for the given settings."""
    world_data_dir = Path(settings.world_data_dir).expanduser().resolve()
    return _load_world_cached(world_data_dir, settings.map_name)
