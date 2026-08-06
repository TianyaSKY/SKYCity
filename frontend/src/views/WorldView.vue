<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { apiBase } from '../api/client';
import { loadMapBundle } from '../pixi/AssetLoader';
import { loadBlueprintCatalog } from '../pixi/StructureLayer';
import { loadCropCatalog } from '../pixi/CropLayer';
import { AgentLayer } from '../pixi/AgentLayer';
import { CameraController } from '../pixi/CameraController';
import { WorldRenderer } from '../pixi/WorldRenderer';
import { useWorldStore } from '../stores/worldStore';
import type { BlueprintCatalog, CropCatalog } from '../types/world';
import type { ParsedWorldConfig } from '../types/tiled';
import EventStream from '../components/EventStream.vue';
import HealthIndicator from '../components/HealthIndicator.vue';
import LocationPanel from '../components/LocationPanel.vue';
import AgentPanel from '../components/AgentPanel.vue';
import SpeechBubble from '../components/SpeechBubble.vue';
import WorldClockBar from '../components/WorldClockBar.vue';
import LocationLabels from '../components/LocationLabels.vue';
import AgentNameplates from '../components/AgentNameplates.vue';
import TaskBoard from '../components/TaskBoard.vue';
import StockPanel from '../components/StockPanel.vue';

const DEFAULT_AGENT_COLOR = '#9ee6b0';

const store = useWorldStore();
const host = ref<HTMLElement | null>(null);

/** Static world-space anchor (px) per location for the label overlay. */
const locationAnchors = ref<{ location_id: string; x: number; y: number }[]>([]);

let renderer: WorldRenderer | null = null;
let camera: CameraController | null = null;
let agentLayer: AgentLayer | null = null;
let worldConfig: ParsedWorldConfig | null = null;
let blueprints: BlueprintCatalog | null = null;
let cropCatalog: CropCatalog | null = null;
let bobTime = 0;

const tileLabel = computed(() =>
  store.pointerTile ? `(${store.pointerTile.col}, ${store.pointerTile.row})` : '—',
);

onMounted(async () => {
  void store.checkHealth();
  if (!host.value) return;
  try {
    renderer = await WorldRenderer.create(host.value);
    const bundle = await loadMapBundle(apiBase);
    worldConfig = bundle.config;
    renderer.renderWorld(bundle.config, bundle.texture);
    // Labels sit above each building's top edge, centered on its origin tile.
    locationAnchors.value = bundle.config.locations.map((loc) => ({
      location_id: loc.location_id,
      x: (loc.col + 0.5) * bundle.config.tileSize,
      y: loc.row * bundle.config.tileSize - 2,
    }));

    camera = new CameraController(renderer.app, renderer.world);
    camera.attach();
    centerMap();

    // Agent layer sits above every tile layer, below the DOM HUD.
    agentLayer = new AgentLayer(
      (agentId) => store.agentColors[agentId] ?? DEFAULT_AGENT_COLOR,
      (x, y) => renderer!.worldToScreen(x, y),
    );
    renderer.world.addChild(agentLayer.container);
    renderer.app.ticker.add(tick);

    store.mapLoaded = true;
    store.mapError = null;

    // Fetch the blueprint catalog once; the structure layer renders from it
    // as soon as it lands (and again whenever store.structures changes).
    void loadBlueprintCatalog(apiBase)
      .then((catalog) => {
        blueprints = catalog;
        renderer?.updateStructures(store.structures, catalog);
      })
      .catch(() => {
        // The map keeps working without structures; the watch below stays a
        // no-op until a catalog becomes available.
      });

    // Fetch the crop catalog once; the crop layer renders from it as soon as
    // it lands (and again whenever store.crops changes).
    void loadCropCatalog(apiBase)
      .then((catalog) => {
        cropCatalog = catalog;
        renderer?.updateCrops(store.crops, catalog);
      })
      .catch(() => {
        // The map keeps working without crops; the watch below stays a no-op
        // until a catalog becomes available.
      });

    const canvas = renderer.app.canvas;
    canvas.addEventListener('pointermove', handlePointerMove);
    canvas.addEventListener('pointerleave', handlePointerLeave);
    canvas.addEventListener('click', handleClick);

    // Bootstrap the live world: pick/create a world, seed from snapshot, connect WS.
    await store.ensureWorld();
  } catch (err) {
    store.mapError = err instanceof Error ? err.message : String(err);
  }
});

/** Frame loop: advance agent tweens against the store's game time. */
function tick(): void {
  if (!agentLayer) return;
  bobTime += 1 / 60;
  agentLayer.update(store.worldTime, bobTime);
  store.pruneBubbles();
}

/** Reflect conversation highlights + selection on every sprite. */
function applyHighlights(): void {
  agentLayer?.applyHighlights(store.activeConversations, store.selectedAgentId);
}

watch(
  () => store.selectedLocation,
  (loc) => {
    if (!renderer || !worldConfig) return;
    if (loc) renderer.setHighlight(loc.col, loc.row, worldConfig.tileSize);
    else renderer.clearHighlight();
  },
);

watch(
  () => store.agents,
  (agents) => {
    agentLayer?.sync(agents);
    applyHighlights();
  },
  { deep: true },
);

// Re-render structures on snapshot hydration and on the M14 build events
// (structure_built upserts / structure_removed deletes mutate this array).
watch(
  () => store.structures,
  (structures) => {
    if (blueprints && renderer) renderer.updateStructures(structures, blueprints);
  },
  { deep: true },
);

// Re-render crops on snapshot hydration and on the M15 crop events
// (crop_planted upserts / crop_grown updates / crop_harvested deletes).
watch(
  () => store.crops,
  (crops) => {
    if (cropCatalog && renderer) renderer.updateCrops(crops, cropCatalog);
  },
  { deep: true },
);

watch(
  () => [store.activeConversations, store.selectedAgentId] as const,
  () => applyHighlights(),
);

function centerMap(): void {
  if (!renderer || !worldConfig || !camera) return;
  const mapW = worldConfig.width * worldConfig.tileSize;
  const mapH = worldConfig.height * worldConfig.tileSize;
  const viewW = renderer.app.canvas.clientWidth;
  const viewH = renderer.app.canvas.clientHeight;
  const zoom = camera.zoom;
  renderer.world.position.set(
    Math.max(0, (viewW - mapW * zoom) / 2),
    Math.max(0, (viewH - mapH * zoom) / 2),
  );
}

function screenPoint(e: PointerEvent | MouseEvent): { x: number; y: number } {
  const canvas = renderer?.app.canvas;
  if (!canvas) return { x: 0, y: 0 };
  const rect = canvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function tileAt(e: PointerEvent | MouseEvent): { col: number; row: number } | null {
  if (!camera || !worldConfig) return null;
  const p = screenPoint(e);
  const w = camera.screenToWorld(p.x, p.y);
  return { col: Math.floor(w.x / worldConfig.tileSize), row: Math.floor(w.y / worldConfig.tileSize) };
}

function handlePointerMove(e: PointerEvent): void {
  const tile = tileAt(e);
  if (!worldConfig) {
    store.setPointerTile(null);
    return;
  }
  if (!tile || tile.col < 0 || tile.row < 0 || tile.col >= worldConfig.width || tile.row >= worldConfig.height) {
    store.setPointerTile(null);
    return;
  }
  store.setPointerTile(tile);
}

function handlePointerLeave(): void {
  store.setPointerTile(null);
}

/** Screen position for the bubble overlay (null while the sprite is absent). */
function bubbleScreenPos(agentId: string): { x: number; y: number } | null {
  return agentLayer?.agentScreenPos(agentId) ?? null;
}

function handleClick(e: MouseEvent): void {
  if (!camera?.wasTap()) return;
  const tile = tileAt(e);
  if (!tile || !worldConfig) {
    store.selectAgent(null);
    store.selectLocation(null);
    return;
  }
  // Clicking an agent selects it (conversation panel + talk highlight).
  const agent = store.agents.find((a) => a.col === tile.col && a.row === tile.row);
  if (agent) {
    store.selectLocation(null);
    store.selectAgent(agent.agent_id);
    return;
  }
  // Otherwise keep the location click behavior (deselect the agent).
  store.selectAgent(null);
  const loc = worldConfig.locations.find((l) => l.col === tile.col && l.row === tile.row) ?? null;
  store.selectLocation(loc);
}

onBeforeUnmount(() => {
  const canvas = renderer?.app.canvas;
  canvas?.removeEventListener('pointermove', handlePointerMove);
  canvas?.removeEventListener('pointerleave', handlePointerLeave);
  canvas?.removeEventListener('click', handleClick);
  camera?.detach();
  agentLayer?.clear();
  renderer?.app.ticker.remove(tick);
  renderer?.app.destroy(true, { children: true, texture: true });
  store.disconnect();
  renderer = null;
  camera = null;
  agentLayer = null;
  worldConfig = null;
  blueprints = null;
  cropCatalog = null;
});
</script>

<template>
  <div class="world-view">
    <div ref="host" class="canvas-host" />
    <div class="hud hud-top-left">
      <HealthIndicator />
    </div>
    <div class="hud hud-top-center">
      <WorldClockBar />
    </div>
    <div class="hud hud-top-right">
      <span class="tile-readout">tile {{ tileLabel }}</span>
    </div>
    <div class="hud hud-top-left" style="top: 44px">
      <TaskBoard />
    </div>
    <div class="hud hud-top-right" style="top: 44px">
      <StockPanel />
    </div>
    <div class="hud hud-bottom">
      <EventStream />
    </div>
    <SpeechBubble :bubbles="store.bubbles" :agent-screen-pos="bubbleScreenPos" />
    <LocationLabels
      :anchors="locationAnchors"
      :locations="store.locations"
      :world-to-screen="(x, y) => (renderer ? renderer.worldToScreen(x, y) : { x, y })"
    />
    <AgentNameplates
      :agents="store.agents"
      :agent-screen-pos="bubbleScreenPos"
      :bubbles="store.bubbles"
    />
    <div class="hud hud-right">
      <AgentPanel />
    </div>
    <LocationPanel class="hud hud-bottom-left" />
    <div v-if="!store.mapLoaded && !store.mapError" class="status-banner">正在加载世界…</div>
    <div v-if="store.mapError" class="status-banner error">{{ store.mapError }}</div>
  </div>
</template>

<style scoped>
.world-view {
  position: fixed;
  inset: 0;
  overflow: hidden;
  background: #07150e;
}
.canvas-host {
  position: absolute;
  inset: 0;
}
.canvas-host :deep(canvas) {
  touch-action: none;
}
.hud {
  position: absolute;
  z-index: 10;
  pointer-events: none;
}
.hud-top-left {
  top: 12px;
  left: 12px;
}
.hud-top-center {
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
}
.hud-top-center :deep(*) {
  pointer-events: auto;
}
.hud-top-right {
  top: 12px;
  right: 12px;
}
.hud-top-right :deep(*) {
  pointer-events: auto;
}
.hud-bottom {
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
}
.hud-bottom-left {
  bottom: 12px;
  left: 12px;
}
.hud-right {
  top: 56px;
  right: 12px;
  bottom: 56px;
  max-width: 320px;
}
.tile-readout {
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.55);
  color: #cde8d5;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.status-banner {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 5;
  padding: 8px 16px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.6);
  color: #cde8d5;
}
.status-banner.error {
  color: #ff9b9b;
}
</style>
