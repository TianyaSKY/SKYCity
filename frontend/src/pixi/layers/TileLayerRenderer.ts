/**
 * Renders a single tile layer (number[][] of raw gids) into a PIXI texture by
 * blitting each tile frame from the tileset image onto an off-screen canvas.
 * gid 0 (and any gid outside the tileset's range, e.g. the 133 marker) draws
 * nothing.
 */

import { Texture } from 'pixi.js';
import type { ResolvedTileset } from '../../types/tiled';

export interface TileLayerRenderOptions {
  tileSize: number;
  tileset: ResolvedTileset;
  /** Source image backing the tileset texture (for drawImage). */
  image: CanvasImageSource;
}

export function renderTileLayerToTexture(
  layerData: number[][],
  opts: TileLayerRenderOptions,
): Texture | null {
  const { tileSize, tileset, image } = opts;
  if (tileset.tilewidth !== tileSize || tileset.tileheight !== tileSize) {
    throw new Error(
      `Tileset tile size ${tileset.tilewidth}x${tileset.tileheight} does not match map tile size ${tileSize}`,
    );
  }

  const height = layerData.length;
  const width = height === 0 ? 0 : layerData[0].length;
  if (width === 0 || height === 0) return null;

  const canvas = document.createElement('canvas');
  canvas.width = width * tileSize;
  canvas.height = height * tileSize;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  const margin = tileset.margin ?? 0;
  const spacing = tileset.spacing ?? 0;
  const columns = tileset.columns;

  for (let row = 0; row < height; row++) {
    const line = layerData[row];
    for (let col = 0; col < width; col++) {
      const gid = line[col] ?? 0;
      if (gid === 0) continue;
      const local = gid - tileset.firstGid;
      if (local < 0 || local >= tileset.tilecount) continue; // marker (133) or unknown gid
      const sx = margin + (local % columns) * (tileSize + spacing);
      const sy = margin + Math.floor(local / columns) * (tileSize + spacing);
      ctx.drawImage(
        image,
        sx,
        sy,
        tileSize,
        tileSize,
        col * tileSize,
        row * tileSize,
        tileSize,
        tileSize,
      );
    }
  }

  const texture = Texture.from(canvas);
  texture.source.scaleMode = 'nearest';
  return texture;
}
