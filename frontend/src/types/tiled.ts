/**
 * Tiled map format types (the subset the renderer consumes), plus the
 * parsed, renderer-ready world configuration.
 */

export interface TiledProperty {
  name: string;
  type: 'string' | 'int' | 'float' | 'bool' | 'color' | 'file' | 'object';
  value: string | number | boolean;
}

export interface TiledObject {
  id?: number;
  name: string;
  type: string;
  x: number;
  y: number;
  width?: number;
  height?: number;
  point?: boolean;
  properties?: TiledProperty[];
}

export interface TiledBaseLayer {
  name: string;
  visible?: boolean;
  opacity?: number;
}

export interface TiledTileLayer extends TiledBaseLayer {
  type: 'tilelayer';
  width: number;
  height: number;
  /** Row-major gids; 0 = empty, top 3 bits carry flip flags. */
  data: number[];
}

export interface TiledObjectLayer extends TiledBaseLayer {
  type: 'objectgroup';
  objects: TiledObject[];
}

export type TiledLayer = TiledTileLayer | TiledObjectLayer;

/** Reference to an external tileset file as found in a Tiled map. */
export interface TiledTilesetRef {
  firstgid: number;
  source: string;
}

/** External tileset file body (.tsj). */
export interface TiledTileset {
  name?: string;
  image: string;
  imagewidth: number;
  imageheight: number;
  tilewidth: number;
  tileheight: number;
  tilecount: number;
  columns: number;
  margin?: number;
  spacing?: number;
}

export interface TiledMap {
  width: number;
  height: number;
  tilewidth: number;
  tileheight: number;
  infinite?: boolean;
  layers: TiledLayer[];
  tilesets: TiledTilesetRef[];
}

/** A tileset resolved against its map/tsj URLs. */
export interface ResolvedTileset extends TiledTileset {
  firstGid: number;
  /** Absolute URL of the tileset image. */
  imageUrl: string;
}

export interface MapLocation {
  location_id: string;
  name: string;
  location_type: string;
  capacity: number;
  open_hour: number;
  close_hour: number;
  col: number;
  row: number;
}

export interface MapInteractable {
  object_id: string;
  object_type: string;
  location_id: string;
  col: number;
  row: number;
}

export interface MapSpawnPoint {
  spawn_id: string;
  agent_id: string;
  direction: string;
  col: number;
  row: number;
}

/** Parsed, renderer-ready world configuration. */
export interface ParsedWorldConfig {
  width: number;
  height: number;
  tileSize: number;
  /** Visual tile layers keyed by layer name; raw gids, 0 = empty. */
  tileLayers: Record<string, number[][]>;
  locations: MapLocation[];
  interactables: MapInteractable[];
  spawnPoints: MapSpawnPoint[];
  /** "c,r" keys of walkable cells (navigation marker, no collision). */
  walkableCells: Set<string>;
  /** "c,r" keys of collision cells. */
  collisionCells: Set<string>;
  /** Resolved tilesets (firstGid 1 = visual, 133 = markers). */
  tilesets: ResolvedTileset[];
}
