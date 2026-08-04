/**
 * Parses a Tiled map JSON (plus resolved tilesets) into a renderer-ready
 * ParsedWorldConfig.
 *
 * Contract: walkable cell = navigation layer has the marker tile AND the
 * collision layer has none; the marker gid is 133 (or derived from the
 * "markers" tileset when one is provided).
 */

import type {
  MapInteractable,
  MapLocation,
  MapSpawnPoint,
  ParsedWorldConfig,
  ResolvedTileset,
  TiledMap,
  TiledObject,
} from '../types/tiled';

const MARKER_GID = 133;
const VISUAL_LAYER_NAMES = ['ground', 'ground_detail', 'buildings', 'decorations_low', 'foreground'];
/** Tiled stores horizontal/vertical/diagonal flip flags in the top 3 bits. */
const GID_FLAGS_MASK = 0x1fffffff;

export function parseTiledMap(
  rawJson: TiledMap,
  tilesets: readonly ResolvedTileset[] = [],
): ParsedWorldConfig {
  const tileSize = rawJson.tilewidth;
  const markerGid =
    tilesets.find((t) => t.imageUrl.toLowerCase().includes('marker'))?.firstGid ?? MARKER_GID;

  const tileLayers: Record<string, number[][]> = {};
  const collisionCells = new Set<string>();
  const walkableCells = new Set<string>();

  for (const layer of rawJson.layers) {
    if (layer.type !== 'tilelayer') continue;
    const data = layer.data.map((gid) => gid & GID_FLAGS_MASK);
    const rows: number[][] = [];
    for (let r = 0; r < rawJson.height; r++) {
      rows.push(data.slice(r * rawJson.width, (r + 1) * rawJson.width));
    }
    if (VISUAL_LAYER_NAMES.includes(layer.name)) {
      tileLayers[layer.name] = rows;
    } else if (layer.name === 'collision') {
      collectMarkerCells(rows, markerGid, collisionCells);
    } else if (layer.name === 'navigation') {
      collectMarkerCells(rows, markerGid, walkableCells, collisionCells);
    }
  }

  const locations: MapLocation[] = [];
  const interactables: MapInteractable[] = [];
  const spawnPoints: MapSpawnPoint[] = [];

  for (const layer of rawJson.layers) {
    if (layer.type !== 'objectgroup') continue;
    for (const obj of layer.objects) {
      if (obj.type === 'location') {
        locations.push({
          location_id: propString(obj, 'location_id') || obj.name,
          name: propString(obj, 'name') || obj.name,
          location_type: propString(obj, 'location_type'),
          capacity: propInt(obj, 'capacity'),
          open_hour: propInt(obj, 'open_hour'),
          close_hour: propInt(obj, 'close_hour'),
          col: Math.round(obj.x / tileSize),
          row: Math.round(obj.y / tileSize),
        });
      } else if (obj.type === 'interactable') {
        interactables.push({
          object_id: propString(obj, 'object_id') || obj.name,
          object_type: propString(obj, 'object_type'),
          location_id: propString(obj, 'location_id'),
          col: Math.round(obj.x / tileSize),
          row: Math.round(obj.y / tileSize),
        });
      } else if (obj.type === 'spawn_point') {
        spawnPoints.push({
          spawn_id: propString(obj, 'spawn_id') || obj.name,
          agent_id: propString(obj, 'agent_id'),
          direction: propString(obj, 'direction'),
          col: Math.round(obj.x / tileSize),
          row: Math.round(obj.y / tileSize),
        });
      }
    }
  }

  return {
    width: rawJson.width,
    height: rawJson.height,
    tileSize,
    tileLayers,
    locations,
    interactables,
    spawnPoints,
    walkableCells,
    collisionCells,
    tilesets: [...tilesets],
  };
}

function collectMarkerCells(
  rows: number[][],
  markerGid: number,
  target: Set<string>,
  exclude?: Set<string>,
): void {
  for (let r = 0; r < rows.length; r++) {
    const row = rows[r];
    for (let c = 0; c < row.length; c++) {
      if (row[c] !== markerGid) continue;
      const key = `${c},${r}`;
      if (!exclude?.has(key)) target.add(key);
    }
  }
}

function propString(obj: TiledObject, name: string): string {
  const p = obj.properties?.find((x) => x.name === name);
  return typeof p?.value === 'string' ? p.value : '';
}

function propInt(obj: TiledObject, name: string): number {
  const p = obj.properties?.find((x) => x.name === name);
  return typeof p?.value === 'number' ? Math.trunc(p.value) : 0;
}
