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
  getConversations,
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
  ConversationMessage,
  ConversationSummary,
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

/** How long a speech bubble stays on screen (real milliseconds). */
export const BUBBLE_TTL_MS = 4000;
/** Hard cap on buffered bubble items (one per agent, so usually tiny). */
export const BUBBLE_CAP = 30;

/** Map backend conversation intent tokens onto Chinese labels. */
export const INTENT_LABELS: Record<string, string> = {
  greet: '打招呼',
  chat: '闲聊',
  ask: '询问',
  offer: '提议',
  leave: '告别',
};

/** Map backend conversation end reasons onto Chinese labels. */
export const CONVERSATION_END_REASONS: Record<string, string> = {
  leave: '对方告别',
  distance: '距离过远',
  max_turns: '已达上限',
  duplicate: '内容重复',
  cooldown_expired: '冷却结束',
  both_busy: '双方忙碌',
};

/** A live speech bubble; at most one per agent (newest wins). */
export interface BubbleItem {
  id: string;
  conversation_id: string;
  agent_id: string;
  text: string;
  at_seq: number;
  at_ms: number;
}

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

/**
 * The two participants of a conversation from an event payload:
 * new shape {agent_ids: [a, b]}, with a legacy {a, b} fallback.
 */
function toAgentPair(p: Record<string, unknown>): [string, string] | null {
  if (
    Array.isArray(p.agent_ids) &&
    typeof p.agent_ids[0] === 'string' &&
    typeof p.agent_ids[1] === 'string'
  ) {
    return [p.agent_ids[0], p.agent_ids[1]];
  }
  if (typeof p.a === 'string' && typeof p.b === 'string') return [p.a, p.b];
  return null;
}

/** Build a readable Chinese line for the event stream ('' = not shown). */
function eventText(
  env: WorldEventEnvelope,
  agents: AgentSnapshot[],
  locations: WorldLocation[],
  endedPair: [string, string] | null = null,
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
      return `${from} 对 ${to} 说：${typeof p.message === 'string' ? p.message : ''}`;
    }
    case 'conversation_started': {
      const pair = toAgentPair(p);
      if (!pair) return '';
      const a = agentName(agents, pair[0]);
      const b = agentName(agents, pair[1]);
      return `${a} 和 ${b} 开始交谈`;
    }
    case 'conversation_ended': {
      // conversation_ended carries no agent ids; the pair is looked up from
      // the active-conversation registry before it is removed.
      const pair = endedPair ?? toAgentPair(p);
      if (!pair) return '';
      const a = agentName(agents, pair[0]);
      const b = agentName(agents, pair[1]);
      const label =
        typeof p.reason === 'string' ? (CONVERSATION_END_REASONS[p.reason] ?? '') : '';
      return label ? `${a} 和 ${b} 的对话结束（${label}）` : `${a} 和 ${b} 的对话结束`;
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
    /** Agent the user clicked on the canvas (null = no selection). */
    selectedAgentId: null as string | null,
    /** Conversation history cache for the selected agent (newest first). */
    conversations: [] as ConversationSummary[],
    /** Conversations currently in progress, by conversation id. */
    activeConversations: {} as Record<string, { agent_ids: [string, string] }>,
    /** Live speech bubbles (one per agent, newest wins; capped). */
    bubbles: [] as BubbleItem[],
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
    /** Other participant of an active conversation involving the agent (if any). */
    activePartnerOf: (state) => (agentId: string): string | null => {
      for (const conv of Object.values(state.activeConversations)) {
        if (conv.agent_ids[0] === agentId) return conv.agent_ids[1];
        if (conv.agent_ids[1] === agentId) return conv.agent_ids[0];
      }
      return null;
    },
    /** Newest live bubble for an agent (one per agent, so the only one). */
    bubbleForAgent: (state) => (agentId: string): BubbleItem | null =>
      state.bubbles.find((b) => b.agent_id === agentId) ?? null,
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
      // Active conversations and bubbles are incremental-only; a resync
      // loses them until fresh events arrive (the REST cache survives).
      this.activeConversations = {};
      this.bubbles = [];
      this.ensureAgentColors(this.agents);
    },

    /** Apply one incremental event (strictly ascending sequence). */
    applyEvent(env: WorldEventEnvelope): void {
      if (env.sequence <= this.latestSequence) return; // duplicate / replay
      this.latestSequence = env.sequence;
      this.worldTime = env.world_time;
      const p = env.payload as Record<string, unknown>;

      // conversation_ended carries no agent ids; capture the pair from the
      // active registry before the switch removes it (for the event text).
      const endedPair =
        env.type === 'conversation_ended' && typeof p.conversation_id === 'string'
          ? (this.activeConversations[p.conversation_id]?.agent_ids ?? null)
          : null;

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
        case 'conversation_started': {
          const convId = typeof p.conversation_id === 'string' ? p.conversation_id : '';
          const pair = toAgentPair(p);
          if (convId && pair) {
            this.activeConversations[convId] = { agent_ids: pair };
            this.ensureConversationInCache(convId, pair, env.world_time);
          }
          break;
        }
        case 'conversation_message': {
          const convId = typeof p.conversation_id === 'string' ? p.conversation_id : '';
          const from = typeof p.from_agent_id === 'string' ? p.from_agent_id : '';
          const to = typeof p.to_agent_id === 'string' ? p.to_agent_id : '';
          const text = typeof p.message === 'string' ? p.message : '';
          if (convId && from && text) {
            this.pushBubble({ conversation_id: convId, agent_id: from, text, at_seq: env.sequence });
            if (this.selectedAgentId && (from === this.selectedAgentId || to === this.selectedAgentId)) {
              this.appendConversationMessage(convId, {
                from_agent_id: from,
                to_agent_id: to,
                message: text,
                intent: typeof p.intent === 'string' ? p.intent : '',
                sent_at: env.world_time,
              });
            }
          }
          break;
        }
        case 'conversation_ended': {
          const convId = typeof p.conversation_id === 'string' ? p.conversation_id : '';
          if (convId) {
            delete this.activeConversations[convId];
            this.endConversationInCache(
              convId,
              env.world_time,
              typeof p.reason === 'string' ? p.reason : '',
            );
          }
          break;
        }
        default:
          break;
      }

      const text = eventText(env, this.agents, this.locations, endedPair);
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

    /** Select an agent (opens the conversation panel) or clear the selection. */
    selectAgent(agentId: string | null): void {
      if (agentId === this.selectedAgentId) return;
      this.selectedAgentId = agentId;
      this.conversations = [];
      if (agentId) void this.fetchConversations(agentId);
    },

    /** (Re)fetch conversation history for an agent into the cache (if still selected). */
    async fetchConversations(agentId: string): Promise<void> {
      if (!this.worldId) return;
      try {
        const list = await getConversations(this.worldId, agentId);
        if (this.selectedAgentId === agentId) this.conversations = list;
      } catch {
        // Selection stays; the panel shows its empty state until a refetch.
      }
    },

    /** Remove one bubble (used by the overlay for manual dismissal). */
    dismissBubble(id: string): void {
      const idx = this.bubbles.findIndex((b) => b.id === id);
      if (idx >= 0) this.bubbles.splice(idx, 1);
    },

    /** Drop bubbles older than BUBBLE_TTL_MS (called every animation frame). */
    pruneBubbles(now: number = Date.now()): void {
      if (this.bubbles.length === 0) return;
      const keep = this.bubbles.filter((b) => now - b.at_ms < BUBBLE_TTL_MS);
      if (keep.length !== this.bubbles.length) this.bubbles = keep;
    },

    /** Register a bubble for one agent; newest message replaces the previous. */
    pushBubble(b: { conversation_id: string; agent_id: string; text: string; at_seq: number }): void {
      const prevIdx = this.bubbles.findIndex((x) => x.agent_id === b.agent_id);
      if (prevIdx >= 0) this.bubbles.splice(prevIdx, 1);
      this.bubbles.push({ ...b, id: `${b.conversation_id}:${b.agent_id}`, at_ms: Date.now() });
      if (this.bubbles.length > BUBBLE_CAP) {
        this.bubbles.splice(0, this.bubbles.length - BUBBLE_CAP);
      }
    },

    /** Insert a live (in-progress) conversation into the selected agent's cache. */
    ensureConversationInCache(convId: string, pair: [string, string], startedAt: number): void {
      const mine = this.selectedAgentId;
      if (!mine || (pair[0] !== mine && pair[1] !== mine)) return;
      if (this.conversations.some((c) => c.conversation_id === convId)) return;
      this.conversations.unshift({
        conversation_id: convId,
        other_agent_id: pair[0] === mine ? pair[1] : pair[0],
        started_at: startedAt,
        ended_at: null,
        end_reason: null,
        messages: [],
      });
    },

    /** Append a live message to the selected agent's cached conversation. */
    appendConversationMessage(convId: string, msg: ConversationMessage): void {
      const mine = this.selectedAgentId;
      if (!mine || (msg.from_agent_id !== mine && msg.to_agent_id !== mine)) return;
      let conv = this.conversations.find((c) => c.conversation_id === convId);
      if (!conv) {
        conv = {
          conversation_id: convId,
          other_agent_id: msg.from_agent_id === mine ? msg.to_agent_id : msg.from_agent_id,
          started_at: null,
          ended_at: null,
          end_reason: null,
          messages: [],
        };
        this.conversations.unshift(conv);
      }
      conv.messages.push(msg);
    },

    /** Mark a cached conversation as ended (badge + reason, live update). */
    endConversationInCache(convId: string, endedAt: number, reason: string): void {
      const conv = this.conversations.find((c) => c.conversation_id === convId);
      if (!conv) return;
      conv.ended_at = endedAt;
      conv.end_reason = reason || null;
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
