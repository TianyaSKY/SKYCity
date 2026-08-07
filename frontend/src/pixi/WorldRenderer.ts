/**
 * Owns the PIXI application and the world scene: one sprite per visual tile
 * layer inside a single scaled/panned world container, plus a location
 * highlight overlay and the agent-built structure layer. Camera (pan/zoom) is
 * driven by CameraController.
 */

import type {Texture} from 'pixi.js';
import {Application, Container, Graphics, Sprite} from 'pixi.js';
import type {ParsedWorldConfig} from '../types/tiled';
import type {BlueprintCatalog, CropCatalog, CropSnapshot, StructureSnapshot} from '../types/world';
import {CropLayer} from './CropLayer';
import {StructureLayer} from './StructureLayer';
import {renderTileLayerToTexture} from './layers/TileLayerRenderer';

export const VISUAL_LAYERS = ['ground', 'ground_detail', 'buildings', 'decorations_low', 'foreground'] as const;

export class WorldRenderer {
    readonly app: Application;
    /** Scaled + panned world container; everything world-space lives here. */
    readonly world: Container;

    private readonly highlight: Graphics;
    private readonly layerSprites = new Map<string, Sprite>();
    private structureLayer: StructureLayer | null = null;
    private cropLayer: CropLayer | null = null;

    private constructor(app: Application) {
        this.app = app;
        this.world = new Container();
        this.highlight = new Graphics();
        this.highlight.visible = false;
        this.world.addChild(this.highlight);
        app.stage.addChild(this.world);
    }

    static async create(host: HTMLElement): Promise<WorldRenderer> {
        const app = new Application();
        await app.init({
            antialias: false,
            background: 0x0a2e1c,
            resizeTo: host,
        });
        const renderer = new WorldRenderer(app);
        host.appendChild(app.canvas);
        app.canvas.style.width = '100%';
        app.canvas.style.height = '100%';
        app.canvas.style.display = 'block';
        return renderer;
    }

    renderWorld(config: ParsedWorldConfig, texture: Texture): void {
        const tileset = config.tilesets.find((t) => t.firstGid === 1);
        if (!tileset) throw new Error('Missing visual tileset (firstGid = 1) in parsed map');
        const image = texture.source.resource as CanvasImageSource;

        for (const sprite of this.layerSprites.values()) {
            sprite.destroy({children: true});
        }
        this.layerSprites.clear();
        this.structureLayer?.container.destroy({children: true});
        this.structureLayer = null;
        this.cropLayer?.container.destroy({children: true});
        this.cropLayer = null;
        this.clearHighlight();

        for (const name of VISUAL_LAYERS) {
            const data = config.tileLayers[name];
            if (!data) continue;
            const layerTexture = renderTileLayerToTexture(data, {
                tileSize: config.tileSize,
                tileset,
                image,
            });
            if (!layerTexture) continue;
            const sprite = new Sprite(layerTexture);
            this.world.addChild(sprite);
            this.layerSprites.set(name, sprite);
        }

        // Structures and crops sit above the tile layers (incl. decorations_low)
        // and below the agent layer, which WorldView adds to this.world after
        // this call.
        this.structureLayer = new StructureLayer(texture, config.tileSize, tileset);
        this.world.addChild(this.structureLayer.container);
        this.cropLayer = new CropLayer(texture, config.tileSize, tileset);
        this.world.addChild(this.cropLayer.container);

        // Re-add so the highlight stays above every layer.
        this.world.addChild(this.highlight);
    }

    /** Re-render the agent-built structure layer from store + blueprint catalog. */
    updateStructures(structures: StructureSnapshot[], blueprints: BlueprintCatalog): void {
        this.structureLayer?.render(structures, blueprints);
    }

    /** Re-render the planted-crop layer from store + crop catalog. */
    updateCrops(crops: CropSnapshot[], catalog: CropCatalog): void {
        this.cropLayer?.render(crops, catalog);
    }

    setHighlight(col: number, row: number, tileSize: number): void {
        this.highlight.clear();
        this.highlight.rect(col * tileSize, row * tileSize, tileSize, tileSize);
        this.highlight.fill({color: 0xffe14d, alpha: 0.4});
        this.highlight.visible = true;
    }

    clearHighlight(): void {
        this.highlight.visible = false;
    }

    worldToScreen(x: number, y: number): { x: number; y: number } {
        return {
            x: x * this.world.scale.x + this.world.position.x,
            y: y * this.world.scale.y + this.world.position.y,
        };
    }

    screenToWorld(x: number, y: number): { x: number; y: number } {
        return {
            x: (x - this.world.position.x) / this.world.scale.x,
            y: (y - this.world.position.y) / this.world.scale.y,
        };
    }
}
