/**
 * Global world UI state: backend health, map load status, pointer tile,
 * selected location, the live world clock, agents, locations, the event
 * stream, and the WebSocket connection lifecycle.
 *
 * The store is display/observation only — it never makes authoritative
 * decisions. Snapshots overwrite everything; incremental events are applied
 * strictly in ascending sequence order (duplicates ignored).
 */

import { defineStore } from 'pinia';
import {
  checkHealth,
  createWorld,
  getSnapshot,
  listWorlds,
  pauseWorld,
  postAgentAction,
  resumeWorld,
  setSpeed,
} from '../api/client';
import type { AgentActionRequest } from '../api/client';
import { getWorldSocket } from '../websocket/client';
import type { WorldSocketStatus } from '../websocket/client';
import type { MapLocation } from '../types/tiled';
import type {
  ActionResponse,
  AgentSnapshot,
  Cell,
  WorldEventEnvelope,
  WorldEventItem,
  WorldEventType,
  WorldListItem,
  WorldLocation,
  WorldSnapshotPayload,
} from '../types/world';

export interface HealthInfo {
  status: string;
  map_version: string;
}

export interface TileCoord {
  col: number;
  row: number;
}

const EVENT_CAP = 300;
const AGENT_PALETTE = [
  '#e57373',
  '#64b5f6',
  '#81c784',
  '#ffb74d',
  '#ba68c8',
  '#4dd0e1',
  '#f06292',
  '#aed581',
  '#ff8a65',
  '#7986cb',
];

let ensureRetryTimer: number | null = null;

/** Map raw backend weather tokens onto short Chinese labels. */
export const WEATHER_LABELS: Record<string, string> = {
  sunny: '晴',
  clear: '晴',
  cloudy: '阴',
  overcast: '阴',
  rain: '雨',
  rainy: '雨',
  snow: '雪',
  windy: '风',
};

function agentName(agents: AgentSnapshot[], agentId?: unknown): string {
  if (typeof agentId !== 'string' || !agentId) return '';
  return agents.find((a) => a.agent_id === agentId)?.name ?? agentId;
}

function locationNameAt(locations: WorldLocation[], cell: unknown): string {
  if (!Array.isArray(cell) || typeof cell[0] !== 'number' || typeof cell[1] !== 'number') return '';
  const loc = locations.find((l) => l.col === cell[0] && l.row === cell[1]);
  return loc ? loc.name : `(${cell[0]}, ${cell[1]})`;
}

/** Build a readable Chinese line for the event stream ('' = not shown). */
function eventText(
  env: WorldEventEnvelope,
  agents: AgentSnapshot[],
  locations: WorldLocation[],
): string {
  const p = env.payload as Record<string, unknown>;
  switch (env.type) {
    case 'world_time_changed':
    case 'agent_state_changed':
      return '';
    case 'world_paused':
      return '世界暂停';
    case 'world_resumed':
      return '世界恢复运行';
    case 'world_speed_changed':
      return `世界速度调整为 ${String(p.speed)}×`;
    case 'agent_move_started': {
      const name = agentName(agents, p.agent_id);
      return `${name} 出发前往 ${locationNameAt(locations, p.to)}`;
    }
    case 'agent_move_completed': {
      const name = agentName(agents, p.agent_id);
      return `${name} 到达 ${locationNameAt(locations, p.at)}`;
    }
    case 'agent_wait_started': {
      const name = agentName(agents, p.agent_id);
      return `${name} 开始等待（${String(p.minutes ?? '?')} 分钟）`;
    }
    case 'agent_wait_completed': {
      const name = agentName(agents, p.agent_id);
      return `${name} 结束等待`;
    }
    case 'world_event_created':
      return typeof p.text === 'string' ? p.text : '';
    case 'conversation_message': {
      const from = agentName(agents, p.from_agent_id);
      const to = agentName(agents, p.to_agent_id);
      return `${from} → ${to}: ${typeof p.message === 'string' ? p.message : ''}`;
    }
    case 'conversation_started': {
      const a = agentName(agents, p.a);
      const b = agentName(agents, p.b);
      return `${a} 与 ${b} 开始交谈`;
    }
    case 'conversation_ended': {
      const a = agentName(agents, p.a);
      const b = agentName(agents, p.b);
      return `${a} 与 ${b} 结束交谈`;
    }
    case 'agent_talked': {
      const from = agentName(agents, p.from_agent_id);
      return `${from}: ${typeof p.message === 'string' ? p.message : ''}`;
    }
    default:
      return `[${env.type}]`;
  }
}

function locationIdAt(locations: WorldLocation[], cell: Cell): string | null {
  return locations.find((l) => l.col === cell[0] && l.row === cell[1])?.location_id ?? null;
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
    worldTime: 480,
    connection: 'disconnected' as WorldSocketStatus,
    worldId: null as string | null,
    speed: 1,
    paused: false,
    weather: 'sunny',
    day: 1,
    agents: [] as AgentSnapshot[],
    locations: [] as WorldLocation[],
    events: [] as WorldEventItem[],
    latestSequence: 0,
    agentColors: {} as Record<string, string>,
  }),
  getters: {
    agentById: (state) => (agentId: string): AgentSnapshot | undefined =>
      state.agents.find((a) => a.agent_id === agentId),
    timeLabel(state): string {
      const hours = Math.floor(state.worldTime / 60) % 24;
      const minutes = state.worldTime % 60;
      return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
    },
    isOpen: (state) => (locationId: string): boolean =>
      state.locations.find((l) => l.location_id === locationId)?.open ?? false,
    weatherLabel(state): string {
      return WEATHER_LABELS[state.weather] ?? WEATHER_LABELS.sunny;
    },
    dayLabel(state): string {
      return `第 ${state.day} 天`;
    },
  },
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

    /** Full state overwrite from a world snapshot (WS initial or REST resync). */
    applySnapshot(payload: WorldSnapshotPayload): void {
      this.worldId = payload.world.world_id;
      this.worldTime = payload.world.world_time;
      this.speed = payload.world.speed;
      this.paused = payload.world.paused;
      this.weather = payload.world.weather;
      this.day = payload.world.day;
      this.agents = payload.agents.map((a) => ({ ...a }));
      this.locations = payload.locations.map((l) => ({ ...l }));
      this.latestSequence = payload.latest_sequence;
      // A snapshot supersedes any incremental history accumulated so far.
      this.events = [];
      this.ensureAgentColors(this.agents);
    },

    /** Apply one incremental event (strictly ascending sequence). */
    applyEvent(env: WorldEventEnvelope): void {
      if (env.sequence <= this.latestSequence) return; // duplicate / replay
      this.latestSequence = env.sequence;
      this.worldTime = env.world_time;
      const p = env.payload as Record<string, unknown>;

      switch (env.type) {
        case 'world_time_changed':
          if (typeof p.world_time === 'number') this.worldTime = p.world_time;
          break;
        case 'world_paused':
          this.paused = true;
          break;
        case 'world_resumed':
          this.paused = false;
          break;
        case 'world_speed_changed':
          if (typeof p.speed === 'number') this.speed = p.speed;
          break;
        case 'agent_state_changed':
          this.patchAgent(p.agent_id as string, p.state as Record<string, unknown>);
          break;
        case 'agent_move_started':
          this.startAgentMove(env, p);
          break;
        case 'agent_move_completed':
          this.completeAgentMove(p);
          break;
        case 'agent_wait_started':
          this.startAgentWait(p);
          break;
        case 'agent_wait_completed':
          this.completeAgentWait(p);
          break;
        default:
          break;
      }

      const text = eventText(env, this.agents, this.locations);
      if (text) {
        this.events.unshift({
          sequence: env.sequence,
          worldTime: env.world_time,
          type: env.type as WorldEventType,
          text,
          agentId: typeof p.agent_id === 'string' ? p.agent_id : (typeof p.from_agent_id === 'string' ? p.from_agent_id : null),
          importance: typeof p.importance === 'number' ? p.importance : undefined,
        });
        if (this.events.length > EVENT_CAP) this.events.length = EVENT_CAP;
      }
    },

    /** Bootstrap: pick the first world (or create one), seed, and connect. */
    async ensureWorld(): Promise<void> {
      if (this.worldId && (this.connection === 'connected' || this.connection === 'connecting')) {
        return;
      }
      try {
        let worlds: WorldListItem[];
        try {
          worlds = await listWorlds();
        } catch {
          worlds = [];
        }
        if (worlds.length === 0) {
          worlds = [await createWorld('晨露村庄')];
        }
        const world = worlds[0];
        this.worldId = world.world_id;
        const snapshot = await getSnapshot(world.world_id);
        this.applySnapshot(snapshot);
        this.mapError = null;
        this.connect(world.world_id);
      } catch (err) {
        this.connection = 'disconnected';
        this.mapError = err instanceof Error ? err.message : String(err);
        // Backend may still be booting — retry in a moment.
        if (ensureRetryTimer == null) {
          ensureRetryTimer = window.setTimeout(() => {
            ensureRetryTimer = null;
            void this.ensureWorld();
          }, 2000);
        }
      }
    },

    connect(worldId: string): void {
      this.worldId = worldId;
      getWorldSocket().connect(worldId, {
        onSnapshot: (payload) => this.applySnapshot(payload),
        onEvent: (env) => this.applyEvent(env),
        onStatus: (status) => {
          this.connection = status;
        },
      });
    },

    disconnect(): void {
      getWorldSocket().close();
      this.connection = 'disconnected';
    },

    async setSpeed(speed: number): Promise<void> {
      if (!this.worldId) return;
      try {
        await setSpeed(this.worldId, speed);
        this.speed = speed;
      } catch {
        // The server will surface the real value via world_speed_changed.
      }
    },

    async togglePause(): Promise<void> {
      if (!this.worldId) return;
      const next = !this.paused;
      try {
        if (next) await pauseWorld(this.worldId);
        else await resumeWorld(this.worldId);
        this.paused = next;
      } catch {
        // The server is authoritative; ignore optimistic-update failures.
      }
    },

    /** Submit an agent action; returns the engine's verdict. */
    async submitAgentAction(agentId: string, action: AgentActionRequest): Promise<ActionResponse> {
      if (!this.worldId) throw new Error('未连接世界');
      return postAgentAction(this.worldId, agentId, action);
    },

    patchAgent(agentId: string, patch: Record<string, unknown> | undefined): void {
      if (!agentId || !patch) return;
      const agent = this.agents.find((a) => a.agent_id === agentId);
      if (!agent) return;
      if (typeof patch.hunger === 'number') agent.hunger = patch.hunger;
      if (typeof patch.energy === 'number') agent.energy = patch.energy;
      if (typeof patch.money === 'number') agent.money = patch.money;
      if (typeof patch.col === 'number') agent.col = patch.col;
      if (typeof patch.row === 'number') agent.row = patch.row;
      if (typeof patch.location_id === 'string' || patch.location_id === null) agent.location_id = patch.location_id;
      if (patch.action !== undefined) agent.action = patch.action as AgentSnapshot['action'];
    },

    startAgentMove(
      env: WorldEventEnvelope,
      p: Record<string, unknown>,
    ): void {
      const agent = this.agents.find((a) => a.agent_id === p.agent_id);
      if (!agent || !Array.isArray(p.from) || !Array.isArray(p.to)) return;
      const from: Cell = [p.from[0] as number, p.from[1] as number];
      const to: Cell = [p.to[0] as number, p.to[1] as number];
      const endsAt = typeof p.ends_at === 'number' ? p.ends_at : env.world_time + 1;
      const duration = typeof p.duration_minutes === 'number' ? p.duration_minutes : Math.max(1, endsAt - env.world_time);
      agent.action = {
        type: 'move',
        from,
        to,
        started_at: endsAt - duration,
        ends_at: endsAt,
        reason: typeof p.reason === 'string' ? p.reason : null,
      };
    },

    completeAgentMove(p: Record<string, unknown>): void {
      const agent = this.agents.find((a) => a.agent_id === p.agent_id);
      if (!agent) return;
      if (Array.isArray(p.at)) {
        agent.col = p.at[0] as number;
        agent.row = p.at[1] as number;
        agent.location_id = locationIdAt(this.locations, [agent.col, agent.row]);
      }
      agent.action = null;
    },

    startAgentWait(p: Record<string, unknown>): void {
      const agent = this.agents.find((a) => a.agent_id === p.agent_id);
      if (!agent) return;
      agent.action = {
        type: 'wait',
        ends_at: typeof p.ends_at === 'number' ? p.ends_at : 0,
        reason: typeof p.reason === 'string' ? p.reason : null,
      };
    },

    completeAgentWait(p: Record<string, unknown>): void {
      const agent = this.agents.find((a) => a.agent_id === p.agent_id);
      if (!agent) return;
      if (Array.isArray(p.at)) {
        agent.col = p.at[0] as number;
        agent.row = p.at[1] as number;
        agent.location_id = locationIdAt(this.locations, [agent.col, agent.row]);
      }
      agent.action = null;
    },

    ensureAgentColors(agents: AgentSnapshot[]): void {
      let next = Object.keys(this.agentColors).length;
      for (const agent of agents) {
        if (this.agentColors[agent.agent_id]) continue;
        this.agentColors[agent.agent_id] = AGENT_PALETTE[next % AGENT_PALETTE.length];
        next += 1;
      }
    },
  },
});
