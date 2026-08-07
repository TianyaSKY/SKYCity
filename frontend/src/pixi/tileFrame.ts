/**
 * Texture frame of a gid inside a tileset image — the same margin/spacing/
 * columns math the tile layer renderer uses (see TileLayerRenderer).
 * Shared by the structure layer (M14) and the crop layer (M15), which both
 * draw individual gids as sprites from the tiny_farm tileset.
 * Returns null for gid 0 and any gid outside the tileset's range (e.g. the
 * 133 marker), which render nothing.
 */

import {Rectangle} from 'pixi.js';
import type {ResolvedTileset} from '../types/tiled';

export function tileFrameRect(
    gid: number,
    tileSize: number,
    tileset: ResolvedTileset,
): Rectangle | null {
    const local = gid - tileset.firstGid;
    if (local < 0 || local >= tileset.tilecount) return null;
    const margin = tileset.margin ?? 0;
    const spacing = tileset.spacing ?? 0;
    const columns = tileset.columns;
    const sx = margin + (local % columns) * (tileSize + spacing);
    const sy = margin + Math.floor(local / columns) * (tileSize + spacing);
    return new Rectangle(sx, sy, tileSize, tileSize);
}
