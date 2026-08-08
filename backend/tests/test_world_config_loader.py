"""Acceptance tests for the world config loader and /health endpoint."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.services import world_config_loader as loader
from app.services.world_config_loader import (
    ParsedWorldConfig,
    WorldConfigError,
    load_world_config,
)


@pytest.fixture(scope="module")
def world() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture()
def fake_world_data(tmp_path: Path) -> Path:
    """A copy of the real map + manifest with an empty identities/ dir, so
    character-card loading can be exercised against a writable tree."""
    src = Path(get_settings().world_data_dir)
    shutil.copyfile(src / "asset-manifest.json", tmp_path / "asset-manifest.json")
    (tmp_path / "maps").mkdir()
    for rel in ("tiny_world.tmj", "tiny_farm.tsj", "markers.tsj"):
        shutil.copyfile(src / "maps" / rel, tmp_path / "maps" / rel)
    (tmp_path / "identities").mkdir()
    return tmp_path


def _write_card(data_dir: Path, agent_id: str, **fields) -> None:
    card = {
        "id": agent_id,
        "name": "测试员",
        "spawn": {"col": 33, "row": 20, "direction": "down"},
    }
    card.update(fields)
    (data_dir / "identities" / f"{agent_id}.json").write_text(
        json.dumps(card, ensure_ascii=False), encoding="utf-8"
    )


def test_map_dimensions(world: ParsedWorldConfig) -> None:
    assert (world.width, world.height) == (64, 40)
    assert world.tile_size == 16


def test_map_version(world: ParsedWorldConfig) -> None:
    assert world.map_version == "1.1.0"


def test_tile_layers_present(world: ParsedWorldConfig) -> None:
    assert set(world.tile_layers) == {
        "ground",
        "ground_detail",
        "buildings",
        "decorations_low",
        "foreground",
        "collision",
        "navigation",
    }


def test_locations(world: ParsedWorldConfig) -> None:
    ids = {loc.location_id for loc in world.locations}
    assert ids == {
        "village_shop",
        "village_farm",
        "village_plaza",
        "town_hall",
        "village_hotel",
        "village_bakery",
        "carpenter_shop",
        "flower_garden",
        "linxia_home",
        "zhangming_home",
        "chenyu_home",
        "wangfang_home",
        "zhoushen_home",
        "limujiang_home",
        "sunshen_home",
        # M18 plaza stalls.
        "stall_plaza_1",
        "stall_plaza_2",
        "stall_plaza_3",
        # M19 gathering spots.
        "forest",
        "river_bank",
    }
    # every location exposes the full property set
    for loc in world.locations:
        assert loc.location_type
        assert loc.capacity > 0
        assert 0 <= loc.open_hour < loc.close_hour <= 24


def test_spawn_points(world: ParsedWorldConfig) -> None:
    assert len(world.spawn_points) == 9
    by_id = {spawn.agent_id: spawn for spawn in world.spawn_points}
    for spawn in world.spawn_points:
        assert spawn.spawn_id and spawn.agent_id and spawn.direction
    # spawn + home come from the character cards (single source of truth)
    assert by_id["agent_touzi"].col == 33 and by_id["agent_touzi"].row == 20
    assert by_id["agent_linxia"].col == 18 and by_id["agent_linxia"].row == 27
    assert by_id["agent_linxia"].home_id == "linxia_home"
    assert by_id["agent_touzi"].home_id is None
    assert by_id["agent_laozhang"].home_id is None


def test_spawns_come_from_cards_not_map(fake_world_data: Path) -> None:
    """The map's spawn_points layer is decorative: the character cards decide
    who spawns and where (the copied real map carries 6 spawn objects)."""
    _write_card(fake_world_data, "agent_newbie", name="新手")
    world = loader._load_world_cached(fake_world_data, "tiny_world")
    assert [s.agent_id for s in world.spawn_points] == ["agent_newbie"]
    assert world.spawn_points[0].col == 33
    assert world.spawn_points[0].row == 20


def test_card_without_spawn_rejected(fake_world_data: Path) -> None:
    _write_card(fake_world_data, "agent_nospawn", spawn=None)
    with pytest.raises(WorldConfigError, match="spawn"):
        loader._load_world_cached(fake_world_data, "tiny_world")


def test_card_id_mismatch_rejected(fake_world_data: Path) -> None:
    card = {
        "id": "agent_other",
        "name": "错位",
        "spawn": {"col": 33, "row": 20, "direction": "down"},
    }
    (fake_world_data / "identities" / "agent_newbie.json").write_text(
        json.dumps(card, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(WorldConfigError, match="id 与文件名不一致"):
        loader._load_world_cached(fake_world_data, "tiny_world")


def test_interactables(world: ParsedWorldConfig) -> None:
    assert len(world.interactables) == 12  # 9 map + 3 M19 gathering spots
    for obj in world.interactables:
        assert obj.object_id and obj.object_type


def test_walkable_cells_non_empty(world: ParsedWorldConfig) -> None:
    assert len(world.walkable_cells) > 0


def test_walkable_and_collision_disjoint(world: ParsedWorldConfig) -> None:
    assert not (world.walkable_cells & world.collision_cells)
    assert len(world.collision_cells) > 0


def test_gid_to_tileset(world: ParsedWorldConfig) -> None:
    assert world.gid_to_tileset[1] == "tiny_farm"
    assert world.gid_to_tileset[133] == "markers"
    # tiny_farm covers gids 1..132 (132 tiles)
    assert world.gid_to_tileset[132] == "tiny_farm"


def test_health_endpoint() -> None:
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "map_version": "1.1.0"}
