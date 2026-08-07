/**
 * Agent-built structure layer (M14): one sprite per footprint cell of every
 * structure, drawn from the same tiny_farm tileset texture the visual tile
 * layers use. The world container adds it after the tile layers (so it sits
 * above decorations_low) and WorldView adds the agent layer afterwards (so
 * structures stay below agents). Building (in-progress) structures render at
 * 50% alpha; built structures at 100%.
 */

import type {Texture as TextureType} from 'pixi.js';
import {Container, Sprite, Texture} from 'pixi.js';
import {apiBase, mapAssetUrl} from '../api/client';
import type {ResolvedTileset} from '../types/tiled';
import type {BlueprintCatalog, StructureSnapshot} from '../types/world';
import {tileFrameRect} from './tileFrame';

/** Fetch the blueprint catalog from the backend static mount (same base as maps). */
export async function loadBlueprintCatalog(base: string = apiBase): Promise<BlueprintCatalog> {
    const url = mapAssetUrl('blueprints/blueprints.json', base);
    const res = await fetch(url);
    if (!res.ok) {
        throw new Error(`Failed to fetch ${url}: ${res.status} ${res.statusText}`);
    }
    return (await res.json()) as BlueprintCatalog;
}

/** Owns the structure sprites; clears and redraws from store + catalog. */
export class StructureLayer {
    readonly container: Container;

    constructor(
        private readonly texture: TextureType,
        private readonly tileSize: number,
        private readonly tileset: ResolvedTileset,
    ) {
        this.container = new Container();
    }

    /** Clear every sprite and redraw all structures from the given state. */
    render(structures: StructureSnapshot[], blueprints: BlueprintCatalog): void {
        for (const child of [...this.container.children]) {
            child.destroy({children: true});
        }
        const byId: Record<string, BlueprintCatalog['blueprints'][number]> = {};
        for (const bp of blueprints.blueprints) byId[bp.blueprint_id] = bp;

        for (const structure of structures) {
            const blueprint = byId[structure.blueprint_id];
            if (!blueprint) continue; // catalog lagging behind the store — skip
            const alpha = structure.status === 'building' ? 0.5 : 1;
            for (const [dc, dr] of blueprint.footprint) {
                const gids = blueprint.tile_gids[`${dc},${dr}`];
                if (!gids || gids.length === 0) continue;
                const frame = tileFrameRect(gids[0], this.tileSize, this.tileset);
                if (!frame) continue;
                const sprite = new Sprite(new Texture({source: this.texture.source, frame}));
                sprite.position.set(
                    (structure.col + dc) * this.tileSize,
                    (structure.row + dr) * this.tileSize,
                );
                sprite.alpha = alpha;
                this.container.addChild(sprite);
            }
        }
    }
}
