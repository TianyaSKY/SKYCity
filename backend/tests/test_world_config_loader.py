"""Acceptance tests for the world config loader and /health endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.services.world_config_loader import ParsedWorldConfig, load_world_config


@pytest.fixture(scope="module")
def world() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def test_map_dimensions(world: ParsedWorldConfig) -> None:
    assert (world.width, world.height) == (64, 40)
    assert world.tile_size == 16


def test_map_version(world: ParsedWorldConfig) -> None:
    assert world.map_version == "1.0.0"


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
        "linxia_home",
        "zhangming_home",
        "chenyu_home",
        "wangfang_home",
    }
    # every location exposes the full property set
    for loc in world.locations:
        assert loc.location_type
        assert loc.capacity > 0
        assert 0 <= loc.open_hour < loc.close_hour <= 24


def test_spawn_points(world: ParsedWorldConfig) -> None:
    assert len(world.spawn_points) == 5
    for spawn in world.spawn_points:
        assert spawn.spawn_id and spawn.agent_id and spawn.direction


def test_interactables(world: ParsedWorldConfig) -> None:
    assert len(world.interactables) == 5
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
    assert response.json() == {"status": "ok", "map_version": "1.0.0"}
