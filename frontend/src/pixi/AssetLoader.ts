/**
 * Loads the full map bundle from the backend: the .tmj, its external .tsj
 * tilesets (resolved relative to the tmj URL), the tileset images (resolved
 * relative to each tsj URL), and a nearest-sampled texture of the primary
 * visual tileset.
 */

import { Texture, TextureSource } from 'pixi.js';
import { mapAssetUrl } from '../api/client';
import type { ParsedWorldConfig, ResolvedTileset, TiledMap, TiledTileset } from '../types/tiled';
import { parseTiledMap } from './TiledMapLoader';

export interface MapBundle {
  config: ParsedWorldConfig;
  /** Nearest-sampled texture of the primary visual tileset (firstGid 1). */
  texture: Texture;
}

/** Resolve a possibly-relative URL against a base URL (URL-join semantics). */
function joinUrl(baseUrl: string, rel: string): string {
  if (/^[a-z][a-z0-9+.-]*:/i.test(rel) || rel.startsWith('//')) return rel;
  return new URL(rel, baseUrl).toString();
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    // The backend sends CORS headers; anonymous mode keeps the canvas clean.
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`Failed to load image: ${url}`));
    img.src = url;
  });
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url}: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export async function loadMapBundle(apiBase: string): Promise<MapBundle> {
  // Global default: nearest-neighbour sampling for every texture created here.
  TextureSource.defaultOptions.scaleMode = 'nearest';

  const tmjUrl = mapAssetUrl('maps/tiny_world.tmj', apiBase);
  const rawMap = await fetchJson<TiledMap>(tmjUrl);

  const tilesets: ResolvedTileset[] = [];
  for (const ref of rawMap.tilesets) {
    const tsjUrl = joinUrl(tmjUrl, ref.source);
    const tsj = await fetchJson<TiledTileset>(tsjUrl);
    tilesets.push({ ...tsj, firstGid: ref.firstgid, imageUrl: joinUrl(tsjUrl, tsj.image) });
  }

  const config = parseTiledMap(rawMap, tilesets);

  const visual = tilesets.find((t) => t.firstGid === 1) ?? tilesets[0];
  if (!visual) throw new Error('Map declares no tilesets');
  const image = await loadImage(visual.imageUrl);
  const texture = Texture.from(image);
  texture.source.scaleMode = 'nearest';

  return { config, texture };
}
