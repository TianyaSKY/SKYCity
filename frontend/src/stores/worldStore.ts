/**
 * Global world UI state: backend health, map load status, pointer tile,
 * selected location, and the (static for M1) world clock in game minutes.
 */

import { defineStore } from 'pinia';
import { checkHealth } from '../api/client';
import type { MapLocation } from '../types/tiled';

export interface HealthInfo {
  status: string;
  map_version: string;
}

export interface TileCoord {
  col: number;
  row: number;
}

export const useWorldStore = defineStore('world', {
  state: () => ({
    health: null as HealthInfo | null,
    healthOk: false,
    mapLoaded: false,
    mapError: null as string | null,
    pointerTile: null as TileCoord | null,
    selectedLocation: null as MapLocation | null,
    /** Game minutes since midnight; 480 = 08:00. */
    worldTimeMinutes: 480,
  }),
  actions: {
    async checkHealth(): Promise<void> {
      try {
        this.health = await checkHealth();
        this.healthOk = this.health.status === 'ok';
      } catch {
        this.health = null;
        this.healthOk = false;
      }
    },
    setPointerTile(tile: TileCoord | null): void {
      this.pointerTile = tile;
    },
    selectLocation(location: MapLocation | null): void {
      this.selectedLocation = location;
    },
  },
});
