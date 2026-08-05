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
  deleteWorld as deleteWorldApi,
  getAgentDetail,
  getConversations,
  getDecisions,
  getLocationDetail,
  getMemories,
  getRelationships,
  getSnapshot,
  getStocks,
  listWorlds,
  pauseWorld,
  postAgentAction,
  postGodAction,
  resumeWorld,
  setSpeed,
} from '../api/client';
import type { AgentActionRequest } from '../api/client';
import { getWorldSocket } from '../websocket/client';
import type { WorldSocketStatus } from '../websocket/client';
import type { MapLocation } from '../types/tiled';
import type {
  ActionResponse,
  AgentDetail,
  AgentSnapshot,
  Cell,
  ConversationMessage,
  ConversationSummary,
  DecisionRecord,
  GodActionResult,
  GodActionRequest,
  InventoryItem,
  LocationDetail,
  MemoryItem,
  RelationshipItem,
  StockItem,
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

/** Map backend memory types onto Chinese labels. */
export const MEMORY_TYPE_LABELS: Record<string, string> = {
  working: '工作记忆',
  episodic: '情节记忆',
  semantic: '语义记忆',
};

/** Map backend relationship axes onto Chinese labels. */
export const RELATIONSHIP_AXIS_LABELS: Record<string, string> = {
  familiarity: '熟悉度',
  trust: '信任',
  affection: '好感',
  resentment: '怨恨',
  debt: '债务',
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

/** Map backend decision tool names onto Chinese labels. */
export const TOOL_LABELS: Record<string, string> = {
  move: '移动',
  wait: '等待',
  talk: '对话',
  work: '工作',
  buy_item: '购买',
  sell_item: '出售',
  use_item: '使用',
  buy_stock: '买入股票',
  sell_stock: '卖出股票',
  transfer_money: '转账',
  give_item: '赠物',
};

/** Map backend god-action command types onto Chinese labels. */
export const GOD_COMMAND_LABELS: Record<string, string> = {
  pause: '暂停世界',
  resume: '恢复世界',
  set_speed: '调速',
  change_weather: '改变天气',
  grant_money: '发放金钱',
  deduct_money: '扣除金钱',
  spawn_item: '生成物品',
  teleport: '传送',
  public_event: '公共事件',
  change_store_stock: '修改库存',
  change_stock_price: '调整股价',
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

/**
 * Item catalog labels (mirror of world_data/items/items.json). Economy events
 * like work_completed products and store_restocked carry only item_id, so the
 * frontend resolves the Chinese display name from this catalog.
 */
export const ITEM_NAMES: Record<string, string> = {
  bread: '面包',
  apple: '苹果',
  milk: '牛奶',
  vegetable_box: '蔬菜盒',
  carrot: '胡萝卜',
  strawberry: '草莓',
  egg: '鸡蛋',
  honey: '蜂蜜',
  fish: '鲜鱼',
  wheat: '小麦',
  wood: '木材',
  fertilizer: '肥料',
  flower_seed: '花种',
  rope: '麻绳',
  cloth: '布料',
  tool_rake: '耙子',
  pottery: '陶罐',
  candle: '蜡烛',
};

/** Display name of an item: explicit payload name > catalog label > raw id. */
function itemLabel(itemId: unknown, explicitName?: unknown): string {
  if (typeof explicitName === 'string' && explicitName) return explicitName;
  if (typeof itemId === 'string' && ITEM_NAMES[itemId]) return ITEM_NAMES[itemId];
  return typeof itemId === 'string' ? itemId : '';
}

/** "小麦×1、鸡蛋×2" from an item list; '' when empty or malformed. */
function itemsText(list: unknown): string {
  if (!Array.isArray(list)) return '';
  const parts: string[] = [];
  for (const entry of list) {
    if (!entry || typeof entry !== 'object') continue;
    const rec = entry as Record<string, unknown>;
    if (typeof rec.item_id !== 'string') continue;
    const qty = typeof rec.quantity === 'number' ? rec.quantity : 1;
    parts.push(`${itemLabel(rec.item_id)}×${qty}`);
  }
  return parts.join('、');
}

/** Signed money delta for display: +30 / -5; '' when missing. */
function signedAmount(amount: unknown): string {
  if (typeof amount !== 'number') return '';
  return amount >= 0 ? `+${amount}` : String(amount);
}

/** Truncate a string to maxLen characters, appending '…' when cut. */
function truncate(text: string, maxLen: number): string {
  return text.length > maxLen ? `${text.slice(0, maxLen)}…` : text;
}

/**
 * Human-readable text for a relationship_changed delta: picks the axis with
 * the largest absolute delta and reports its direction. Falls back to a
 * generic line when no known axis changed.
 */
function relationshipDeltaText(p: Record<string, unknown>): string {
  const deltas = p.deltas as Record<string, unknown> | undefined;
  if (!deltas || typeof deltas !== 'object') return '';
  let bestKey = '';
  let bestAbs = 0;
  let bestDelta = 0;
  for (const [key, raw] of Object.entries(deltas)) {
    if (typeof raw !== 'number' || raw === 0) continue;
    if (Math.abs(raw) > bestAbs) {
      bestKey = key;
      bestAbs = Math.abs(raw);
      bestDelta = raw;
    }
  }
  if (!bestKey) return '';
  const label = RELATIONSHIP_AXIS_LABELS[bestKey] ?? bestKey;
  return `${label}${bestDelta > 0 ? '提升了' : '下降了'}`;
}

function agentName(agents: AgentSnapshot[], agentId?: unknown): string {
  if (typeof agentId !== 'string' || !agentId) return '';
  return agents.find((a) => a.agent_id === agentId)?.name ?? agentId;
}

function locationNameAt(locations: WorldLocation[], cell: unknown): string {
  if (!Array.isArray(cell) || typeof cell[0] !== 'number' || typeof cell[1] !== 'number') return '';
  const loc = locations.find((l) => l.col === cell[0] && l.row === cell[1]);
  return loc ? loc.name : `(${cell[0]}, ${cell[1]})`;
}

function locationNameById(locations: WorldLocation[], locationId: unknown): string {
  if (typeof locationId !== 'string' || !locationId) return '';
  return locations.find((l) => l.location_id === locationId)?.name ?? '';
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
    case 'work_started': {
      const name = agentName(agents, p.agent_id);
      const job = typeof p.job_name === 'string' ? p.job_name : '工作';
      const mins = typeof p.duration_minutes === 'number' ? p.duration_minutes : '?';
      return `${name} 开始工作（${job}，${mins} 分钟）`;
    }
    case 'work_completed': {
      const name = agentName(agents, p.agent_id);
      const wage = typeof p.wage === 'number' ? p.wage : 0;
      const products = itemsText(p.products);
      return products
        ? `${name} 完成工作，获得 ${wage} 金币 与产物：${products}`
        : `${name} 完成工作，获得 ${wage} 金币`;
    }
    case 'item_purchased': {
      const name = agentName(agents, p.agent_id);
      const item = itemLabel(p.item_id, p.item_name);
      const qty = typeof p.quantity === 'number' ? p.quantity : 1;
      const total =
        typeof p.total === 'number'
          ? p.total
          : typeof p.unit_price === 'number'
            ? p.unit_price * qty
            : 0;
      return `${name} 购买 ${item}×${qty}（${total} 金币）`;
    }
    case 'item_sold': {
      const name = agentName(agents, p.agent_id);
      const item = itemLabel(p.item_id, p.item_name);
      const qty = typeof p.quantity === 'number' ? p.quantity : 1;
      const total =
        typeof p.total === 'number'
          ? p.total
          : typeof p.unit_price === 'number'
            ? p.unit_price * qty
            : 0;
      return `${name} 出售 ${item}×${qty}（${total} 金币）`;
    }
    case 'item_used': {
      const name = agentName(agents, p.agent_id);
      const item = itemLabel(p.item_id, p.item_name);
      const before = typeof p.hunger_before === 'number' ? p.hunger_before : '?';
      const after = typeof p.hunger_after === 'number' ? p.hunger_after : '?';
      return `${name} 使用了 ${item}（饥饿 ${before} → ${after}）`;
    }
    case 'money_changed': {
      const name = agentName(agents, p.agent_id);
      const amount = signedAmount(p.amount);
      const balance = typeof p.balance === 'number' ? p.balance : '?';
      return `${name} 的金币变化 ${amount}（当前 ${balance}）`;
    }
    case 'store_restocked': {
      // store_id doubles as the location id of the shop (village_shop).
      const storeName = locationNameById(locations, p.store_id) || '杂货店';
      const restocked = itemsText(p.restocked);
      return restocked ? `${storeName}补货完成（${restocked}）` : `${storeName}补货完成`;
    }
    case 'store_price_changed': {
      const name = locationNameById(locations, p.store_id) || '杂货店';
      const item = itemLabel(p.item_id, p.item_name);
      return p.promo ? `${name}的${item}促销：${p.sell_price} 金币` : `${name}的${item}恢复原价：${p.sell_price} 金币`;
    }
    case 'memory_created': {
      const name = agentName(agents, p.agent_id);
      const text = typeof p.text === 'string' ? truncate(p.text, 30) : '';
      return text ? `${name} 记下了：${text}` : `${name} 记下了一条记忆`;
    }
    case 'relationship_changed': {
      const source = agentName(agents, p.source_agent_id);
      const target = agentName(agents, p.target_agent_id);
      const delta = relationshipDeltaText(p);
      return delta ? `${source} 对 ${target} 的${delta}` : `${source} 对 ${target} 的关系发生了变化`;
    }
    case 'daily_reflection': {
      const name = agentName(agents, p.agent_id);
      const summary = typeof p.summary === 'string' ? truncate(p.summary, 30) : '';
      return `${name} 的今日反思：${summary}`;
    }
    case 'god_action_applied': {
      const commandType = typeof p.command_type === 'string' ? p.command_type : '';
      const command = GOD_COMMAND_LABELS[commandType] ?? commandType;
      const targetId = typeof p.target_id === 'string' ? p.target_id : '';
      const target = targetId ? agentName(agents, targetId) : '';
      const reason = typeof p.reason === 'string' && p.reason ? p.reason : '';
      return `上帝干预：${command}${target ? ` ${target}` : ''}（${reason}）`;
    }
    case 'weather_changed': {
      const label = typeof p.weather === 'string' ? (WEATHER_LABELS[p.weather] ?? '') : '';
      return label ? `天气变为 ${label}` : '天气发生了变化';
    }
    case 'god_teleport': {
      const name = agentName(agents, p.agent_id);
      const place =
        locationNameById(locations, p.location_id) ||
        locationNameAt(locations, p.to) ||
        (typeof p.location_id === 'string' ? p.location_id : '');
      return `${name} 被传送到了 ${place}`;
    }
    case 'item_spawned': {
      const name = agentName(agents, p.agent_id);
      const item = itemLabel(p.item_id, p.item_name);
      const qty = typeof p.quantity === 'number' ? p.quantity : 1;
      return `${name} 获得了 ${item}×${qty}（上帝）`;
    }
    case 'store_stock_changed': {
      const item = typeof p.item_id === 'string' ? p.item_id : '';
      const qty = typeof p.quantity === 'number' ? p.quantity : 0;
      return `商店库存：${item} → ${qty}`;
    }
    case 'stock_price_changed': {
      const price = typeof p.price === 'number' ? p.price : 0;
      const prev = typeof p.prev_price === 'number' ? p.prev_price : 0;
      const delta = price - prev;
      if (delta === 0) return ''; // 无变化不刷屏
      const sign = delta > 0 ? '+' : '';
      return `${typeof p.stock_name === 'string' ? p.stock_name : p.stock_id} 股价 ${price}（${sign}${delta}）`;
    }
    case 'stock_bought': {
      const name = agentName(agents, p.agent_id);
      return `${name} 买入 ${p.stock_name} ${p.shares}股 @${p.unit_price}（共${p.total}金币）`;
    }
    case 'stock_sold': {
      const name = agentName(agents, p.agent_id);
      return `${name} 卖出 ${p.stock_name} ${p.shares}股 @${p.unit_price}（得${p.total}金币）`;
    }
    case 'dividend_paid':
      return `${p.stock_name} 每股分红 ${p.div_per_share} 金币`;
    case 'money_transferred': {
      const from = agentName(agents, p.from_agent_id);
      const to = agentName(agents, p.to_agent_id);
      const amount = typeof p.amount === 'number' ? p.amount : 0;
      return `${from} 转账 ${amount} 金币给 ${to}`;
    }
    case 'item_given': {
      const from = agentName(agents, p.from_agent_id);
      const to = agentName(agents, p.to_agent_id);
      const item = itemLabel(p.item_id, p.item_name);
      const qty = typeof p.quantity === 'number' ? p.quantity : 1;
      return `${from} 把 ${item}×${qty} 给了 ${to}`;
    }
    // inventory_changed / needs_changed carry no stream text; they only sync
    // agent state in applyEvent.
    case 'inventory_changed':
    case 'needs_changed':
      return '';
    default:
      return `[${env.type}]`;
  }
}

function locationIdAt(locations: WorldLocation[], cell: Cell): string | null {
  return locations.find((l) => l.col === cell[0] && l.row === cell[1])?.location_id ?? null;
}

/** Human-readable current task of an agent (e.g. "工作中 · 农场劳作"). */
export function taskLabelOf(
  action: AgentSnapshot['action'],
  locations: WorldLocation[],
  inConversation = false,
): string {
  if (inConversation) return '对话中';
  if (!action) return '空闲';
  if (action.type === 'move') {
    const loc = locations.find((l) => l.col === action.to[0] && l.row === action.to[1]);
    return `前往 ${loc?.name ?? `(${action.to[0]}, ${action.to[1]})`}`;
  }
  if (action.type === 'wait') return '等待中';
  if (action.type === 'work') return `工作中 · ${action.job_name ?? action.job_id}`;
  return '空闲';
}

/** Remaining game minutes until the in-flight action ends (null when idle). */
export function actionRemainingMinutes(
  action: AgentSnapshot['action'],
  worldTime: number,
): number | null {
  if (!action) return null;
  return Math.max(0, action.ends_at - worldTime);
}

/** Sort priority for the task board: busiest first. */
export function taskPriority(action: AgentSnapshot['action'], inConversation: boolean): number {
  if (inConversation) return 0;
  if (!action) return 4;
  if (action.type === 'work') return 1;
  if (action.type === 'move') return 2;
  return 3;
}

export const useWorldStore = defineStore('world', {
  state: () => ({
    health: null as HealthInfo | null,
    healthOk: false,
    mapLoaded: false,
    mapError: null as string | null,
    pointerTile: null as TileCoord | null,
    selectedLocation: null as MapLocation | null,
    /** REST detail of the selected location (occupants/products/jobs). */
    locationDetail: null as LocationDetail | null,
    /** Game minutes since midnight; 480 = 08:00. */
    worldTime: 480,
    connection: 'disconnected' as WorldSocketStatus,
    worldId: null as string | null,
    worlds: [] as WorldListItem[],
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
    /** Memory cache for the selected agent (newest first; REST-backed). */
    memories: [] as MemoryItem[],
    /** Relationship cache for the selected agent (REST-backed). */
    relationships: [] as RelationshipItem[],
    /** Full REST detail (identity card + state) for the selected agent. */
    agentDetail: null as AgentDetail | null,
    /** LLM decision history for the selected agent (REST-backed, recent first). */
    recentDecisions: [] as DecisionRecord[],
    /** Conversations currently in progress, by conversation id. */
    activeConversations: {} as Record<string, { agent_ids: [string, string] }>,
    /** Live speech bubbles (one per agent, newest wins; capped). */
    bubbles: [] as BubbleItem[],
    /** M10: town stock quotes (REST-loaded, WS-updated). */
    stocks: [] as StockItem[],
    /** M10: shares per agent per stock (agent_id → stock_id → shares). */
    holdings: {} as Record<string, Record<string, number>>,
  }),
  getters: {
    agentById: (state) => (agentId: string): AgentSnapshot | undefined =>
      state.agents.find((a) => a.agent_id === agentId),
    timeLabel(state): string {
      const hours = Math.floor(state.worldTime / 60) % 24;
      const minutes = state.worldTime % 60;
      return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
    },
    isOpen: (state) => (locationId: string): boolean => {
      const loc = state.locations.find((l) => l.location_id === locationId);
      if (!loc) return false;
      // Mirrors backend is_location_open (R8): houses and plazas never
      // close; everything else honours [open_hour, close_hour) against the
      // world clock (worldTime = minutes since midnight).
      if (loc.location_type === 'house' || loc.location_type === 'plaza') return true;
      const hour = (state.worldTime % 1440) / 60;
      return loc.open_hour <= hour && hour < loc.close_hour;
    },
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
      this.locationDetail = null;
      if (location) void this.fetchLocationDetail(location.location_id);
    },

    /** Fetch the selected location's detail; stale responses are dropped. */
    async fetchLocationDetail(locationId: string): Promise<void> {
      if (!this.worldId) return;
      try {
        const detail = await getLocationDetail(this.worldId, locationId);
        if (this.selectedLocation?.location_id === locationId) this.locationDetail = detail;
      } catch {
        // Selection stays; the panel shows base info until a refetch.
      }
    },

    /** Full state overwrite from a world snapshot (WS initial or REST resync). */
    applySnapshot(payload: WorldSnapshotPayload): void {
      this.worldId = payload.world.world_id;
      this.worldTime = payload.world.world_time;
      this.speed = payload.world.speed;
      this.paused = payload.world.paused;
      this.weather = payload.world.weather;
      this.day = payload.world.day;
      this.agents = payload.agents.map((a) => ({
        ...a,
        inventory: Array.isArray(a.inventory) ? a.inventory : [],
      }));
      this.locations = payload.locations.map((l) => ({ ...l }));
      this.latestSequence = payload.latest_sequence;
      // A snapshot supersedes any incremental history accumulated so far.
      this.events = [];
      // Active conversations and bubbles are incremental-only; a resync
      // loses them until fresh events arrive (the REST cache survives).
      this.activeConversations = {};
      this.bubbles = [];
      this.ensureAgentColors(this.agents);
      // M10: snapshots arrive on connect/reconnect/ensureWorld, so a REST
      // refresh naturally supersedes any stale incremental stock state.
      void this.loadStocks();
    },

    /** M10: fetch all quotes + holdings (REST full state). */
    async loadStocks(): Promise<void> {
      if (!this.worldId) return;
      try {
        const data = await getStocks(this.worldId);
        this.stocks = data.stocks;
        const map: Record<string, Record<string, number>> = {};
        for (const h of data.holdings) (map[h.agent_id] ??= {})[h.stock_id] = h.shares;
        this.holdings = map;
      } catch {
        // Best-effort; the next snapshot or WS events will refresh the panel.
      }
    },

    /** M10: apply a signed share delta to one agent's holding (WS events). */
    patchHolding(agentId: string, stockId: string, delta: number): void {
      const next = (this.holdings[agentId]?.[stockId] ?? 0) + delta;
      if (next <= 0) {
        if (this.holdings[agentId]) delete this.holdings[agentId][stockId];
      } else {
        (this.holdings[agentId] ??= {})[stockId] = next;
      }
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
        case 'work_started':
          this.startAgentWork(env, p);
          break;
        case 'work_completed':
          this.completeAgentWork(p);
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
        case 'money_changed':
          // The balance is authoritative from the same transaction that spent
          // or earned it; keep store state in sync even without a follow-up
          // agent_state_changed.
          this.patchAgentMoney(p.agent_id as string, p.balance);
          break;
        case 'inventory_changed':
          this.replaceAgentInventory(p.agent_id as string, p.items);
          break;
        case 'needs_changed':
          this.patchAgentNeeds(p.agent_id as string, p.hunger, p.energy, p.mood);
          break;
        case 'memory_created':
          // Panels are REST-backed; a fresh memory for the selected agent
          // just refetches so the 记忆 tab stays live.
          if (this.selectedAgentId && p.agent_id === this.selectedAgentId) {
            void this.fetchMemories(this.selectedAgentId);
          }
          break;
        case 'relationship_changed':
          if (this.selectedAgentId && p.source_agent_id === this.selectedAgentId) {
            void this.fetchRelationships(this.selectedAgentId);
          }
          break;
        case 'daily_reflection':
          // Stream text only; reflections are not part of the memory list.
          break;
        case 'weather_changed':
          // The dropdown and clock bar read store.weather; keep it in sync.
          if (typeof p.weather === 'string') this.weather = p.weather;
          break;
        case 'god_teleport':
          // Teleports are instant: snap the agent to the destination so the
          // map reflects the intervention immediately (idempotent with any
          // follow-up agent_move/agent_state events).
          if (typeof p.agent_id === 'string' && Array.isArray(p.to) && typeof p.to[0] === 'number' && typeof p.to[1] === 'number') {
            this.teleportAgent(p.agent_id, [p.to[0], p.to[1]], typeof p.location_id === 'string' ? p.location_id : null);
          }
          break;
        case 'stock_price_changed': {
          const s = this.stocks.find((x) => x.stock_id === p.stock_id);
          if (s) {
            s.price = Number(p.price);
            s.prev_price = Number(p.prev_price);
            s.day_business = Number(p.day_business ?? 0);
          }
          break;
        }
        case 'stock_bought':
          this.patchHolding(String(p.agent_id), String(p.stock_id), Number(p.shares) || 0);
          break;
        case 'stock_sold':
          this.patchHolding(String(p.agent_id), String(p.stock_id), -(Number(p.shares) || 0));
          break;
        case 'dividend_paid':
          break; // 金额经 money_changed 到账, 面板显示不依赖此事件
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

      // The open location panel tracks live occupants/store stock: refetch
      // the selected location's detail whenever an event can change it.
      const selected = this.selectedLocation;
      if (selected) {
        switch (env.type) {
          case 'agent_move_completed':
          case 'agent_teleported':
          case 'item_purchased':
          case 'item_sold':
          case 'store_restocked':
          case 'store_price_changed':
            void this.fetchLocationDetail(selected.location_id);
            break;
          default:
            break;
        }
      }
    },

    /** Bootstrap: pick the first world (or create one), seed, and connect. */
    async ensureWorld(): Promise<void> {
      if (this.worldId && (this.connection === 'connected' || this.connection === 'connecting')) {
        return;
      }
      try {
        await this.refreshWorlds();
        let worlds = this.worlds;
        if (worlds.length === 0) {
          await createWorld('晨露村庄');
          await this.refreshWorlds(); // the create response has no name; refetch
          worlds = this.worlds;
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

    /** Reload the world list (best-effort; keeps the previous list on error). */
    async refreshWorlds(): Promise<void> {
      try {
        this.worlds = await listWorlds();
      } catch {
        // transient backend hiccup — the list is only used for the switcher
      }
    },

    /** Join another world: fetch its snapshot and re-point the WebSocket. */
    async switchWorld(worldId: string): Promise<void> {
      if (worldId === this.worldId && this.connection !== 'disconnected') return;
      const snapshot = await getSnapshot(worldId);
      this.worldId = worldId;
      this.applySnapshot(snapshot);
      this.mapError = null;
      this.connect(worldId);
    },

    /** Create a brand-new world and join it. */
    async createNewWorld(name: string): Promise<void> {
      const created = await createWorld(name);
      await this.refreshWorlds();
      await this.switchWorld(created.world_id);
    },

    /** Delete a world; when it is the active one, join the next available. */
    async deleteWorld(worldId: string): Promise<void> {
      await deleteWorldApi(worldId);
      await this.refreshWorlds();
      if (this.worldId === worldId) {
        const next = this.worlds[0];
        if (next) {
          await this.switchWorld(next.world_id);
        } else {
          this.worldId = null;
          await this.ensureWorld(); // creates a fresh world
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

    /** Select an agent (opens the agent panel) or clear the selection. */
    selectAgent(agentId: string | null): void {
      if (agentId === this.selectedAgentId) return;
      this.selectedAgentId = agentId;
      this.conversations = [];
      this.memories = [];
      this.relationships = [];
      this.agentDetail = null;
      this.recentDecisions = [];
      if (agentId) {
        void this.fetchConversations(agentId);
        void this.fetchMemories(agentId);
        void this.fetchRelationships(agentId);
        void this.fetchAgentDetail(agentId);
        void this.fetchDecisions(agentId);
      }
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

    /** (Re)fetch memories for an agent into the cache (if still selected). */
    async fetchMemories(agentId: string): Promise<void> {
      if (!this.worldId) return;
      try {
        const list = await getMemories(this.worldId, agentId);
        if (this.selectedAgentId === agentId) this.memories = list;
      } catch {
        // Selection stays; the panel shows its empty state until a refetch.
      }
    },

    /** (Re)fetch relationships for an agent into the cache (if still selected). */
    async fetchRelationships(agentId: string): Promise<void> {
      if (!this.worldId) return;
      try {
        const list = await getRelationships(this.worldId, agentId);
        if (this.selectedAgentId === agentId) this.relationships = list;
      } catch {
        // Selection stays; the panel shows its empty state until a refetch.
      }
    },

    /** (Re)fetch the full agent detail (identity card + live state, if still selected). */
    async fetchAgentDetail(agentId: string): Promise<void> {
      if (!this.worldId) return;
      try {
        const detail = await getAgentDetail(this.worldId, agentId);
        if (this.selectedAgentId === agentId) this.agentDetail = detail;
      } catch {
        // Selection stays; the panel shows its loading/empty state.
      }
    },

    /** (Re)fetch recent LLM decisions for an agent into the cache (if still selected). */
    async fetchDecisions(agentId: string, limit = 10): Promise<void> {
      if (!this.worldId) return;
      try {
        const list = await getDecisions(this.worldId, agentId, limit);
        if (this.selectedAgentId === agentId) this.recentDecisions = list;
      } catch {
        // Selection stays; the panel shows its empty state until a refetch.
      }
    },

    /**
     * Submit a god intervention command. The returned events are applied
     * locally so the UI reflects the intervention immediately; WS delivery
     * of the same envelopes is deduped by sequence.
     */
    async submitGodAction(body: GodActionRequest): Promise<GodActionResult> {
      if (!this.worldId) throw new Error('未连接世界');
      const result = await postGodAction(this.worldId, body);
      for (const env of result.events ?? []) {
        this.applyEvent(env);
      }
      return result;
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

    /** Replace an agent's inventory with the authoritative item list. */
    replaceAgentInventory(agentId: string, items: unknown): void {
      const agent = this.agents.find((a) => a.agent_id === agentId);
      if (!agent || !Array.isArray(items)) return;
      const cleaned: InventoryItem[] = [];
      for (const entry of items) {
        if (!entry || typeof entry !== 'object') continue;
        const rec = entry as Record<string, unknown>;
        if (typeof rec.item_id !== 'string' || typeof rec.quantity !== 'number') continue;
        cleaned.push({ item_id: rec.item_id, quantity: rec.quantity });
      }
      agent.inventory = cleaned;
    },

    /** Sync hunger/energy/mood from a needs_changed event. */
    patchAgentNeeds(agentId: string, hunger: unknown, energy: unknown, mood: unknown): void {
      const agent = this.agents.find((a) => a.agent_id === agentId);
      if (!agent) return;
      if (typeof hunger === 'number') agent.hunger = hunger;
      if (typeof energy === 'number') agent.energy = energy;
      if (typeof mood === 'number') agent.mood = mood;
    },

    /** Sync money from a money_changed event's authoritative balance. */
    patchAgentMoney(agentId: string, balance: unknown): void {
      const agent = this.agents.find((a) => a.agent_id === agentId);
      if (!agent || typeof balance !== 'number') return;
      agent.money = balance;
    },

    /** Snap an agent to a cell instantly (god teleport); clears any in-flight action. */
    teleportAgent(agentId: string, cell: Cell, locationId: string | null): void {
      const agent = this.agents.find((a) => a.agent_id === agentId);
      if (!agent) return;
      agent.col = cell[0];
      agent.row = cell[1];
      agent.location_id = locationId ?? locationIdAt(this.locations, cell);
      agent.action = null;
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

    /** Reflect an in-flight work shift (R3: exclusive until work_completed). */
    startAgentWork(env: WorldEventEnvelope, p: Record<string, unknown>): void {
      const agent = this.agents.find((a) => a.agent_id === p.agent_id);
      if (!agent) return;
      const endsAt = typeof p.ends_at === 'number' ? p.ends_at : env.world_time;
      const duration = typeof p.duration_minutes === 'number' ? p.duration_minutes : 0;
      agent.action = {
        type: 'work',
        job_id: typeof p.job_id === 'string' ? p.job_id : '',
        job_name: typeof p.job_name === 'string' ? p.job_name : null,
        started_at: endsAt - duration,
        ends_at: endsAt,
        reason: typeof p.reason === 'string' ? p.reason : null,
      };
    },

    /** Clear the work action; wage/products arrive via money/inventory events. */
    completeAgentWork(p: Record<string, unknown>): void {
      const agent = this.agents.find((a) => a.agent_id === p.agent_id);
      if (!agent) return;
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
