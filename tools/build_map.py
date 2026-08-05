"""Deterministic Tiny World map generator.

Emits Tiled 1.10-compatible files under world_data/maps/:

    tileset.png     - 12x11 grid, 16px tiles, no margin/spacing (from kenney pack)
    tiny_farm.tsj   - external tileset definition
    markers.png     - 1x1 magenta marker tile (collision / navigation layers)
    markers.tsj     - external marker tileset
    tiny_world.tmj  - 64x40 town map

Layers (bottom -> top): ground, ground_detail, buildings, decorations_low,
foreground, collision, navigation, then object layers locations,
interactables, spawn_points.

Run:  uv run --with pillow python tools/build_map.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MAP_DIR = ROOT / "world_data" / "maps"
MAP_DIR.mkdir(parents=True, exist_ok=True)

W, H = 64, 40
TILE = 16
SEED = 20260804

# ---- Tile ids (1-based gid = id + 1) ---------------------------------------
GRASS = [106, 107, 94, 119]          # solid grass
GRASS_DARK = 94
DIRT = [0, 12, 24, 36]               # soil with grass corners
DIRT_FLOWER = [1, 13, 25, 37]        # soil with red details
GRASS_SOIL_T = [60, 61, 62, 63]      # grass top, soil bottom (unused w/o flips)
TREES = [64, 65, 66, 67, 68]
BUSHES = [27, 39, 15, 3]
PLANTS = [52, 53, 54, 55, 56]        # small plants / flowers
CROPS = [4, 5, 6, 7, 8, 16, 17, 18, 19, 20]
DETAILS = [26, 38, 55, 5, 6, 17, 28, 40]  # ground_detail sprinkles
FENCE = 69
FENCE_END = 70
FENCE_GATE = 71
POND = 100
POND2 = 101
WELL = 112
FOUNTAIN = 120
BARN = 48          # big red barn (front)
HOUSE_SHOP = 126   # big building (front, windows)
HOUSE_SMALL = 125
HOUSE_TOPS = [96, 97, 98, 99]        # top-down houses
HOUSE_FRONTS = [108, 109, 110, 111]  # front-view houses with doors
ROOF_TOPS = [102, 103, 104, 114, 115, 116]
HOUSE_ROOFS = [72, 73, 74, 75, 76]   # top-down roofs w/ white trim


def gid(tile_id: int) -> int:
    """Tileset starts at firstgid=1."""
    return tile_id + 1


MARKER = 133  # gid of the marker tile in markers.tsj (firstgid=133)


# ---- Layout -----------------------------------------------------------------
# Row/col coordinates in tiles. Cells are (col, row).

MAIN_ROAD_ROWS = [20, 21]                     # full-width horizontal road
VERT_ROAD_COLS = [30, 31]                     # full-height vertical road
SHOP_ROAD_ROW = 12                            # cols 16..44
SHOP_ROAD_COLS = list(range(16, 45))
SHOP_SPUR_COL = 23                            # rows 13..19 (connects to main road)
FARM_ROAD_ROW = 24                            # cols 30..45
FARM_ROAD_COLS = list(range(30, 46))
FARM_SPUR_COLS = [46, 47]                     # rows 24..34 (to wangfang home)
WANGFANG_ROAD_ROW = 34                        # cols 38..47
WANGFANG_ROAD_COLS = list(range(38, 48))
LINXIA_PATH_COLS = [17]                       # rows 22..26
CHENYU_PATH_ROW = 19                          # cols 12..16
ZHANGMING_PATH_COLS = [40]                    # rows 11..12
PLAZA = ((28, 18), (36, 23))                  # inclusive corners (col,row)
POND_CELLS = [(6, 32), (7, 32), (6, 33), (7, 33)]
FIELD = ((43, 26), (51, 31))                  # crop field corners
FENCE_TOP_ROW = 25                            # cols 43..51
FENCE_BOTTOM_ROW = 32                         # cols 43..51
# The wangfang farm spur (cols 46-47) crosses both fence rows; these cells
# are gates (walkable) instead of fence posts, or the farm is unreachable.
FENCE_GATE_CELLS = {(46, 25), (47, 25), (46, 32), (47, 32)}

BUILDINGS: dict[str, tuple[int, int]] = {
    # location_id -> (col, row, tile)
    "village_shop": (23, 12, HOUSE_SHOP),
    "village_farm": (47, 24, BARN),
    "town_hall": (28, 8, HOUSE_TOPS[0]),
    "town_hall_2": (29, 8, HOUSE_TOPS[1]),
    "linxia_home": (18, 26, HOUSE_FRONTS[0]),
    "zhangming_home": (40, 10, HOUSE_FRONTS[1]),
    "chenyu_home": (12, 18, HOUSE_FRONTS[2]),
    "wangfang_home": (48, 34, HOUSE_FRONTS[3]),
}

LOCATION_ANCHORS: dict[str, tuple[int, int]] = {
    "village_shop": (23, 12),
    "village_farm": (47, 24),
    "village_plaza": (32, 20),
    "town_hall": (29, 8),
    "linxia_home": (18, 26),
    "zhangming_home": (40, 10),
    "chenyu_home": (12, 18),
    "wangfang_home": (48, 34),
}

LOCATIONS: dict[str, dict] = {
    "village_plaza": {
        "name": "村庄广场", "location_type": "plaza", "capacity": 30,
        "open_hour": 0, "close_hour": 24,
    },
    "village_shop": {
        "name": "村庄杂货店", "location_type": "store", "capacity": 8,
        "open_hour": 8, "close_hour": 20,
    },
    "village_farm": {
        "name": "晨露农场", "location_type": "farm", "capacity": 12,
        "open_hour": 6, "close_hour": 18,
    },
    "town_hall": {
        "name": "小镇政务厅", "location_type": "office", "capacity": 10,
        "open_hour": 9, "close_hour": 17,
    },
    "linxia_home": {
        "name": "林夏的家", "location_type": "house", "capacity": 4,
        "open_hour": 0, "close_hour": 24,
    },
    "zhangming_home": {
        "name": "张明的家", "location_type": "house", "capacity": 4,
        "open_hour": 0, "close_hour": 24,
    },
    "chenyu_home": {
        "name": "陈宇的家", "location_type": "house", "capacity": 4,
        "open_hour": 0, "close_hour": 24,
    },
    "wangfang_home": {
        "name": "王芳的家", "location_type": "house", "capacity": 4,
        "open_hour": 0, "close_hour": 24,
    },
}

SPAWN_POINTS: dict[str, dict] = {
    "spawn_linxia": {"agent_id": "agent_linxia", "col": 18, "row": 27, "direction": "down"},
    "spawn_zhangming": {"agent_id": "agent_zhangming", "col": 40, "row": 11, "direction": "down"},
    "spawn_chenyu": {"agent_id": "agent_chenyu", "col": 12, "row": 19, "direction": "down"},
    "spawn_wangfang": {"agent_id": "agent_wangfang", "col": 48, "row": 35, "direction": "down"},
    "spawn_laozhang": {"agent_id": "agent_laozhang", "col": 30, "row": 9, "direction": "down"},
}

INTERACTABLES: dict[str, dict] = {
    "shop_counter": {"object_type": "store_counter", "location_id": "village_shop",
                     "col": 23, "row": 13},
    "farm_field": {"object_type": "farm_field", "location_id": "village_farm",
                   "col": 47, "row": 26},
    "well": {"object_type": "well", "location_id": "village_plaza",
             "col": 28, "row": 22},
    "fountain": {"object_type": "fountain", "location_id": "village_plaza",
                 "col": 32, "row": 20},
    "town_hall_desk": {"object_type": "service_desk", "location_id": "town_hall",
                       "col": 28, "row": 9},
}


def is_road(c: int, r: int) -> bool:
    if r in MAIN_ROAD_ROWS:
        return True
    if c in VERT_ROAD_COLS:
        return True
    if r == SHOP_ROAD_ROW and c in SHOP_ROAD_COLS:
        return True
    if c == SHOP_SPUR_COL and 13 <= r <= 19:
        return True
    if r == FARM_ROAD_ROW and c in FARM_ROAD_COLS:
        return True
    if c in FARM_SPUR_COLS and 24 <= r <= 34:
        return True
    if r == WANGFANG_ROAD_ROW and c in WANGFANG_ROAD_COLS:
        return True
    if c in LINXIA_PATH_COLS and 22 <= r <= 26:
        return True
    if r == CHENYU_PATH_ROW and 12 <= c <= 16:
        return True
    if c in ZHANGMING_PATH_COLS and 11 <= r <= 12:
        return True
    return False


def in_plaza(c: int, r: int) -> bool:
    (c0, r0), (c1, r1) = PLAZA
    return c0 <= c <= c1 and r0 <= r <= r1


def in_field(c: int, r: int) -> bool:
    (c0, r0), (c1, r1) = FIELD
    return c0 <= c <= c1 and r0 <= r <= r1


def build_layers() -> dict:
    rng = random.Random(SEED)

    def blank() -> list[list[int]]:
        return [[0] * W for _ in range(H)]

    ground = blank()
    detail = blank()
    buildings = blank()
    decor = blank()
    foreground = blank()
    collision = blank()
    navigation = blank()

    # --- ground: grass everywhere -----------------------------------------
    for r in range(H):
        for c in range(W):
            ground[r][c] = gid(GRASS[(c * 7 + r * 13) % len(GRASS)])

    # --- roads + plaza: soil ----------------------------------------------
    for r in range(H):
        for c in range(W):
            if is_road(c, r) or in_plaza(c, r):
                ground[r][c] = gid(DIRT[(c + r) % len(DIRT)])
    # plaza center ring uses flower soil
    for r in range(19, 23):
        for c in range(29, 35):
            if (c + r) % 3 == 0:
                ground[r][c] = gid(DIRT_FLOWER[(c + r) % len(DIRT_FLOWER)])

    # --- crop field (soil base) -------------------------------------------
    (c0, r0), (c1, r1) = FIELD
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            ground[r][c] = gid(DIRT[(c * 3 + r) % len(DIRT)])

    # --- buildings --------------------------------------------------------
    for loc_id, (c, r, tile) in BUILDINGS.items():
        buildings[r][c] = gid(tile)

    # --- water, well, fountain -------------------------------------------
    for (c, r), t in zip(POND_CELLS, [POND, POND2, POND, POND2]):
        decor[r][c] = gid(t)
    decor[22][28] = gid(WELL)
    decor[20][32] = gid(FOUNTAIN)

    # --- fences (gate cells stay walkable for the farm spur) ---------------
    for r in (FENCE_TOP_ROW, FENCE_BOTTOM_ROW):
        for c in range(43, 52):
            decor[r][c] = gid(FENCE_GATE if (c, r) in FENCE_GATE_CELLS else FENCE)

    # --- trees & bushes ---------------------------------------------------
    tree_cells = set()
    occupied = set()
    for bid, (c, r, _t) in BUILDINGS.items():
        occupied.add((c, r))
    for (c, r) in POND_CELLS:
        occupied.add((c, r))
    occupied.update((32, 20), (28, 22))

    candidates = []
    for r in range(2, H - 2):
        for c in range(2, W - 2):
            if (c, r) in occupied or is_road(c, r) or in_plaza(c, r) or in_field(c, r):
                continue
            if r in (FENCE_TOP_ROW, FENCE_BOTTOM_ROW) and 42 <= c <= 52:
                continue
            candidates.append((c, r))
    rng.shuffle(candidates)
    for c, r in candidates[:70]:
        if rng.random() < 0.5:
            tile = rng.choice(TREES)
        else:
            tile = rng.choice(BUSHES)
        decor[r][c] = gid(tile)
        tree_cells.add((c, r))
    # bushes around plaza corners
    for c, r in [(27, 17), (37, 17), (27, 24), (36, 25)]:
        decor[r][c] = gid(rng.choice(BUSHES))
        tree_cells.add((c, r))

    # --- crops in field ---------------------------------------------------
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            if (c * 3 + r) % 4 == 0:
                decor[r][c] = gid(CROPS[(c + r) % len(CROPS)])

    # --- ground detail sprinkles -----------------------------------------
    for r in range(H):
        for c in range(W):
            if ground[r][c] in (gid(106), gid(107), gid(119)):
                if rng.random() < 0.07:
                    detail[r][c] = gid(rng.choice(DETAILS))

    # --- collision: buildings, trees, water, fences, well, fountain -------
    for bid, (c, r, _t) in BUILDINGS.items():
        collision[r][c] = MARKER
    for (c, r) in tree_cells:
        collision[r][c] = MARKER
    for (c, r) in POND_CELLS:
        collision[r][c] = MARKER
    for r in (FENCE_TOP_ROW, FENCE_BOTTOM_ROW):
        for c in range(43, 52):
            if (c, r) not in FENCE_GATE_CELLS:
                collision[r][c] = MARKER
    collision[22][28] = MARKER  # well
    collision[20][32] = MARKER  # fountain

    # --- navigation: roads, plaza, field, doors ---------------------------
    for r in range(H):
        for c in range(W):
            if is_road(c, r) or in_plaza(c, r) or in_field(c, r):
                navigation[r][c] = MARKER
    # door cells (just south of each building)
    for loc_id, (c, r, _t) in BUILDINGS.items():
        if r + 1 < H and not collision[r + 1][c]:
            navigation[r + 1][c] = MARKER

    # invariant: navigation and collision never overlap (carve nav under
    # buildings / fountain / fences that sit on road or plaza cells)
    for r in range(H):
        for c in range(W):
            if collision[r][c]:
                navigation[r][c] = 0

    return {
        "ground": ground, "ground_detail": detail, "buildings": buildings,
        "decorations_low": decor, "foreground": foreground,
        "collision": collision, "navigation": navigation,
    }


def encode_data(layer: list[list[int]]) -> list[int]:
    return [layer[r][c] for r in range(H) for c in range(W)]


def make_marker_tileset_png() -> None:
    img = Image.new("RGBA", (16, 16), (255, 0, 255, 255))
    img.save(MAP_DIR / "markers.png")


def make_tsj(path: Path, name: str, image: str, columns: int, rows: int,
             tilecount: int, tilewidth: int, tileheight: int) -> None:
    tsj = {
        "columns": columns,
        "image": image,
        "imageheight": rows * tileheight,
        "imagewidth": columns * tilewidth,
        "margin": 0,
        "name": name,
        "spacing": 0,
        "tilecount": tilecount,
        "tiledversion": "1.10.2",
        "tileheight": tileheight,
        "tiles": [],
        "tilewidth": tilewidth,
        "type": "tileset",
        "version": "1.10",
    }
    path.write_text(json.dumps(tsj, indent=2, ensure_ascii=False) + "\n")


def make_tmj(layers: dict) -> None:
    next_layer_id = 1
    next_object_id = 1
    tiled_layers = []

    tile_layers = [
        "ground", "ground_detail", "buildings", "decorations_low",
        "foreground", "collision", "navigation",
    ]
    for name in tile_layers:
        data = encode_data(layers[name])
        tiled_layers.append({
            "data": data,
            "height": H,
            "id": next_layer_id,
            "name": name,
            "opacity": 1,
            "type": "tilelayer",
            "visible": True,
            "width": W,
            "x": 0,
            "y": 0,
        })
        next_layer_id += 1

    # --- locations object layer ------------------------------------------
    loc_objects = []
    for loc_id, anchor in LOCATION_ANCHORS.items():
        meta = LOCATIONS[loc_id]
        col, row = anchor
        loc_objects.append({
            "height": TILE,
            "id": next_object_id,
            "name": loc_id,
            "properties": [
                {"name": "location_id", "type": "string", "value": loc_id},
                {"name": "name", "type": "string", "value": meta["name"]},
                {"name": "location_type", "type": "string",
                 "value": meta["location_type"]},
                {"name": "capacity", "type": "int", "value": meta["capacity"]},
                {"name": "open_hour", "type": "int", "value": meta["open_hour"]},
                {"name": "close_hour", "type": "int", "value": meta["close_hour"]},
            ],
            "rotation": 0,
            "type": "location",
            "visible": True,
            "width": TILE,
            "x": col * TILE,
            "y": row * TILE,
        })
        next_object_id += 1
    tiled_layers.append({
        "draworder": "topdown",
        "id": next_layer_id,
        "name": "locations",
        "objects": loc_objects,
        "opacity": 1,
        "type": "objectgroup",
        "visible": True,
        "x": 0,
        "y": 0,
    })
    next_layer_id += 1

    # --- interactables object layer --------------------------------------
    int_objects = []
    for obj_id, meta in INTERACTABLES.items():
        col, row = meta["col"], meta["row"]
        int_objects.append({
            "height": TILE,
            "id": next_object_id,
            "name": obj_id,
            "properties": [
                {"name": "object_id", "type": "string", "value": obj_id},
                {"name": "object_type", "type": "string",
                 "value": meta["object_type"]},
                {"name": "location_id", "type": "string",
                 "value": meta["location_id"]},
            ],
            "rotation": 0,
            "type": "interactable",
            "visible": True,
            "width": TILE,
            "x": col * TILE,
            "y": row * TILE,
        })
        next_object_id += 1
    tiled_layers.append({
        "draworder": "topdown",
        "id": next_layer_id,
        "name": "interactables",
        "objects": int_objects,
        "opacity": 1,
        "type": "objectgroup",
        "visible": True,
        "x": 0,
        "y": 0,
    })
    next_layer_id += 1

    # --- spawn_points object layer ---------------------------------------
    spawn_objects = []
    for spawn_id, meta in SPAWN_POINTS.items():
        col, row = meta["col"], meta["row"]
        spawn_objects.append({
            "height": 0,
            "id": next_object_id,
            "name": spawn_id,
            "point": True,
            "properties": [
                {"name": "spawn_id", "type": "string", "value": spawn_id},
                {"name": "agent_id", "type": "string", "value": meta["agent_id"]},
                {"name": "direction", "type": "string", "value": meta["direction"]},
            ],
            "rotation": 0,
            "type": "spawn_point",
            "visible": True,
            "width": 0,
            "x": col * TILE + TILE / 2,
            "y": row * TILE + TILE / 2,
        })
        next_object_id += 1
    tiled_layers.append({
        "draworder": "topdown",
        "id": next_layer_id,
        "name": "spawn_points",
        "objects": spawn_objects,
        "opacity": 1,
        "type": "objectgroup",
        "visible": True,
        "x": 0,
        "y": 0,
    })
    next_layer_id += 1

    tmj = {
        "compressionlevel": -1,
        "height": H,
        "infinite": False,
        "layers": tiled_layers,
        "nextlayerid": next_layer_id,
        "nextobjectid": next_object_id,
        "orientation": "orthogonal",
        "renderorder": "right-down",
        "tiledversion": "1.10.2",
        "tileheight": TILE,
        "tilesets": [
            {"firstgid": 1, "source": "tiny_farm.tsj"},
            {"firstgid": 133, "source": "markers.tsj"},
        ],
        "tilewidth": TILE,
        "type": "map",
        "version": "1.10",
        "width": W,
    }
    (MAP_DIR / "tiny_world.tmj").write_text(
        json.dumps(tmj, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    # copy packed sheet once (idempotent, deterministic)
    src = ROOT / "kenney_tiny-farm" / "Tilemap" / "tilemap_packed.png"
    if not (MAP_DIR / "tileset.png").exists():
        import shutil
        shutil.copyfile(src, MAP_DIR / "tileset.png")

    make_marker_tileset_png()
    make_tsj(MAP_DIR / "tiny_farm.tsj", "tiny_farm", "tileset.png",
             12, 11, 132, 16, 16)
    make_tsj(MAP_DIR / "markers.tsj", "markers", "markers.png",
             1, 1, 1, 16, 16)

    layers = build_layers()
    make_tmj(layers)

    # ---- self verification ----------------------------------------------
    import json as _json
    tmj = _json.loads((MAP_DIR / "tiny_world.tmj").read_text())
    assert tmj["width"] == W and tmj["height"] == H
    names = [l["name"] for l in tmj["layers"]]
    for expected in ["ground", "ground_detail", "buildings", "decorations_low",
                     "foreground", "collision", "navigation", "locations",
                     "interactables", "spawn_points"]:
        assert expected in names, f"missing layer {expected}"
    loc_layer = next(l for l in tmj["layers"] if l["name"] == "locations")
    loc_ids = {o["name"] for o in loc_layer["objects"]}
    assert {"village_shop", "village_farm", "village_plaza",
            "town_hall", "linxia_home"} <= loc_ids
    spawn_layer = next(l for l in tmj["layers"] if l["name"] == "spawn_points")
    assert len(spawn_layer["objects"]) == 5
    int_layer = next(l for l in tmj["layers"] if l["name"] == "interactables")
    assert len(int_layer["objects"]) == 5
    collision = next(l for l in tmj["layers"] if l["name"] == "collision")
    nav = next(l for l in tmj["layers"] if l["name"] == "navigation")
    assert any(v > 0 for v in collision["data"]), "collision empty"
    assert any(v > 0 for v in nav["data"]), "navigation empty"
    # navigation cells must not be collision cells
    overlap = sum(1 for a, b in zip(collision["data"], nav["data"])
                  if a > 0 and b > 0)
    assert overlap == 0, f"navigation/collision overlap: {overlap}"
    print(f"OK tiny_world.tmj: {W}x{H}, "
          f"{sum(1 for v in nav['data'] if v)} walkable cells, "
          f"{sum(1 for v in collision['data'] if v)} blocked cells")


if __name__ == "__main__":
    main()
