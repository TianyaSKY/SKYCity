/**
 * Planted-crop layer (M15): one sprite per crop at its (col, row) cell,
 * drawn from the same tiny_farm tileset texture the visual tile layers and
 * structures use. The world container adds it after the tile layers at the
 * same z-order as structures (above decorations_low) and WorldView adds the
 * agent layer afterwards (so crops stay below agents). The sprite gid follows
 * the crop's current growth stage; crops are single-cell and walkable, so no
 * alpha or footprint handling is needed.
 */

import type {Texture as TextureType} from 'pixi.js';
import {Container, Sprite, Texture} from 'pixi.js';
import {apiBase, mapAssetUrl} from '../api/client';
import type {ResolvedTileset} from '../types/tiled';
import type {CropCatalog, CropSnapshot} from '../types/world';
import {tileFrameRect} from './tileFrame';

/** Fetch the crop catalog from the backend static mount (same base as maps). */
export async function loadCropCatalog(base: string = apiBase): Promise<CropCatalog> {
    const url = mapAssetUrl('crops/crops.json', base);
    const res = await fetch(url);
    if (!res.ok) {
        throw new Error(`Failed to fetch ${url}: ${res.status} ${res.statusText}`);
    }
    return (await res.json()) as CropCatalog;
}

/** Owns the crop sprites; clears and redraws from store + catalog. */
export class CropLayer {
    readonly container: Container;

    constructor(
        private readonly texture: TextureType,
        private readonly tileSize: number,
        private readonly tileset: ResolvedTileset,
    ) {
        this.container = new Container();
    }

    /** Clear every sprite and redraw all crops from the given state. */
    render(crops: CropSnapshot[], catalog: CropCatalog): void {
        for (const child of [...this.container.children]) {
            child.destroy({children: true});
        }
        const bySeed: Record<string, CropCatalog['crops'][number]> = {};
        for (const def of catalog.crops) bySeed[def.seed_item_id] = def;

        for (const crop of crops) {
            const def = bySeed[crop.item_id];
            if (!def) continue; // catalog lagging behind the store — skip
            // Clamp to the last known stage so a crop the catalog has not caught
            // up with renders as harvestable instead of vanishing.
            const stage = Math.min(Math.max(0, Math.floor(crop.stage)), def.stages.length - 1);
            const gid = def.stages[stage]?.[1];
            const frame = gid === undefined ? null : tileFrameRect(gid, this.tileSize, this.tileset);
            if (!frame) continue;
            const sprite = new Sprite(new Texture({source: this.texture.source, frame}));
            sprite.position.set(crop.col * this.tileSize, crop.row * this.tileSize);
            this.container.addChild(sprite);
        }
    }
}
