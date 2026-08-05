<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue';
import { useWorldStore } from '../stores/worldStore';
import type { WorldLocation } from '../types/world';

/**
 * DOM overlay of clickable name chips above every building on the map.
 * Anchors are static world-space points computed once from the parsed map
 * (location col/row); each frame the chip is repositioned through the
 * camera transform, like SpeechBubble. The chip shows the location name and
 * a live open/closed dot for locations with business hours, and is
 * color-coded by location type so buildings read at a glance.
 */

const props = defineProps<{
  /** World-space anchor per location id (px). */
  anchors: { location_id: string; x: number; y: number }[];
  locations: WorldLocation[];
  /** World px -> canvas CSS px. */
  worldToScreen: (x: number, y: number) => { x: number; y: number };
}>();

const store = useWorldStore();
const els = new Map<string, HTMLElement>();

function setEl(locationId: string, el: unknown): void {
  if (el instanceof HTMLElement) els.set(locationId, el);
  else els.delete(locationId);
}

let raf = 0;

function frame(): void {
  for (const anchor of props.anchors) {
    const el = els.get(anchor.location_id);
    if (!el) continue;
    const pos = props.worldToScreen(anchor.x, anchor.y);
    el.style.transform = `translate(${pos.x}px, ${pos.y}px) translate(-50%, -100%)`;
  }
  raf = requestAnimationFrame(frame);
}

onMounted(() => {
  raf = requestAnimationFrame(frame);
});

onBeforeUnmount(() => {
  cancelAnimationFrame(raf);
  els.clear();
});

/** Locations with real business hours get an open/closed dot; 24h ones don't. */
function hasHours(loc: WorldLocation): boolean {
  return loc.open_hour > 0 || loc.close_hour < 24;
}

function select(loc: WorldLocation): void {
  store.selectAgent(null);
  store.selectLocation(loc);
}
</script>

<template>
  <div class="loc-labels" aria-hidden="true">
    <div
      v-for="loc in locations"
      :key="loc.location_id"
      :ref="(el) => setEl(loc.location_id, el)"
      class="loc-chip"
      :class="[
        `type-${loc.location_type}`,
        { selected: store.selectedLocation?.location_id === loc.location_id },
      ]"
      :title="hasHours(loc) ? (loc.open ? '营业中' : '已关门') : ''"
      @click="select(loc)"
    >
      <span v-if="hasHours(loc)" class="open-dot" :class="loc.open ? 'open' : 'closed'" />
      <span class="loc-name">{{ loc.name }}</span>
    </div>
  </div>
</template>

<style scoped>
.loc-labels {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 4;
}
.loc-chip {
  position: absolute;
  left: 0;
  top: 0;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 8px 2px;
  border-radius: 999px;
  background: rgba(8, 18, 12, 0.88);
  border: 1px solid rgba(205, 232, 213, 0.45);
  color: #e8f5e9;
  font-size: 11px;
  line-height: 1.35;
  white-space: nowrap;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.45);
  pointer-events: auto;
  cursor: pointer;
  user-select: none;
  will-change: transform;
}
.loc-chip:hover {
  filter: brightness(1.25);
}
.loc-chip.selected {
  background: rgba(255, 224, 130, 0.18);
  border-color: #ffe082;
  color: #ffe082;
}
.open-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.open-dot.open {
  background: #4caf50;
  box-shadow: 0 0 4px #4caf50;
}
.open-dot.closed {
  background: #e53935;
  box-shadow: 0 0 4px #e53935;
}
/* Location-type identity: border tint + name color. */
.loc-chip.type-store {
  border-color: rgba(255, 183, 77, 0.75);
  color: #ffe0b2;
}
.loc-chip.type-store .loc-name {
  color: #ffb74d;
}
.loc-chip.type-farm {
  border-color: rgba(129, 199, 132, 0.75);
  color: #c8e6c9;
}
.loc-chip.type-farm .loc-name {
  color: #81c784;
}
.loc-chip.type-office {
  border-color: rgba(100, 181, 246, 0.75);
  color: #bbdefb;
}
.loc-chip.type-office .loc-name {
  color: #64b5f6;
}
.loc-chip.type-house {
  border-color: rgba(179, 157, 219, 0.75);
  color: #e8def8;
}
.loc-chip.type-house .loc-name {
  color: #b39ddb;
}
.loc-chip.type-plaza {
  border-color: rgba(77, 208, 225, 0.75);
  color: #b2ebf2;
}
.loc-chip.type-plaza .loc-name {
  color: #4dd0e1;
}
.loc-chip.selected .loc-name {
  color: #ffe082;
}
</style>
