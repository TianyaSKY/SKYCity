/**
 * World runtime types aligned with the backend event protocol
 * (docs/event-protocol.md): agent snapshots, world clock state,
 * WebSocket event envelopes, and world list entries.
 */

import type { MapLocation } from './tiled';

/** A cell coordinate pair [col, row]. */
export type Cell = [number, number];

/** Agent action state carried in snapshots and state updates. */
export interface MoveAction {
  type: 'move';
  from: Cell;
  to: Cell;
  started_at: number;
  ends_at: number;
  reason?: string | null;
}

export interface WaitAction {
  type: 'wait';
  ends_at: number;
  reason?: string | null;
}

export type AgentAction = MoveAction | WaitAction | null;

/** One inventory entry of an agent (item catalog keyed by item_id). */
export interface InventoryItem {
  item_id: string;
  quantity: number;
}

/** One agent as reported by the world snapshot. */
export interface AgentSnapshot {
  agent_id: string;
  name: string;
  col: number;
  row: number;
  location_id: string | null;
  hunger: number;
  energy: number;
  money: number;
  inventory: InventoryItem[];
  action: AgentAction;
}

/** World clock / global state block of a snapshot. */
export interface WorldClockState {
  world_id: string;
  world_time: number;
  speed: number;
  paused: boolean;
  weather: string;
  day: number;
}

/** Location as reported by the snapshot: MapLocation plus the open flag. */
export interface WorldLocation extends MapLocation {
  open: boolean;
}

/** Payload of the initial world_snapshot event / GET snapshot response. */
export interface WorldSnapshotPayload {
  world: WorldClockState;
  agents: AgentSnapshot[];
  locations: WorldLocation[];
  latest_sequence: number;
}

/** Payload of the WS work_started event. */
export interface WorkStartedPayload {
  agent_id: string;
  job_id: string;
  job_name: string;
  duration_minutes: number;
  ends_at: number;
  reason?: string | null;
}

/** Payload of the WS work_completed event. */
export interface WorkCompletedPayload {
  agent_id: string;
  job_id: string;
  job_name: string;
  wage: number;
  products: InventoryItem[];
  energy_spent: number;
}

/** Payload of the WS item_purchased event. */
export interface ItemPurchasedPayload {
  agent_id: string;
  item_id: string;
  item_name: string;
  quantity: number;
  unit_price: number;
  total: number;
}

/** Payload of the WS item_sold event. */
export interface ItemSoldPayload {
  agent_id: string;
  item_id: string;
  item_name: string;
  quantity: number;
  unit_price: number;
  total: number;
}

/** Payload of the WS item_used event. */
export interface ItemUsedPayload {
  agent_id: string;
  item_id: string;
  item_name: string;
  hunger_before: number;
  hunger_after: number;
}

/** Payload of the WS money_changed event. */
export interface MoneyChangedPayload {
  agent_id: string;
  /** Signed delta: positive = gained, negative = spent. */
  amount: number;
  balance: number;
  reason?: string | null;
}

/** Payload of the WS inventory_changed event. */
export interface InventoryChangedPayload {
  agent_id: string;
  items: InventoryItem[];
}

/** Payload of the WS needs_changed event. */
export interface NeedsChangedPayload {
  agent_id: string;
  hunger: number;
  energy: number;
}

/** Payload of the WS store_restocked event. */
export interface StoreRestockedPayload {
  store_id: string;
  restocked: InventoryItem[];
}

export type WorldEventType =
  | 'world_snapshot'
  | 'world_time_changed'
  | 'world_paused'
  | 'world_resumed'
  | 'world_speed_changed'
  | 'agent_state_changed'
  | 'agent_move_started'
  | 'agent_move_completed'
  | 'agent_wait_started'
  | 'agent_wait_completed'
  | 'world_event_created'
  | 'conversation_message'
  | 'conversation_started'
  | 'conversation_ended'
  | 'agent_talked'
  | 'work_started'
  | 'work_completed'
  | 'item_purchased'
  | 'item_sold'
  | 'item_used'
  | 'money_changed'
  | 'inventory_changed'
  | 'needs_changed'
  | 'store_restocked'
  | 'memory_created'
  | 'relationship_changed'
  | 'daily_reflection'
  | (string & {});

/** Uniform envelope wrapping every event (HTTP, WS, replay). */
export interface WorldEventEnvelope<TPayload = Record<string, unknown>> {
  event_id: string;
  sequence: number;
  world_id: string;
  world_time: number;
  type: WorldEventType;
  payload: TPayload;
  trace_id?: string | null;
}

/** Entry in the world list endpoint. */
export interface WorldListItem {
  world_id: string;
  name: string;
  world_time: number;
  speed: number;
  paused: boolean;
}

/** Response of an agent action POST (200 or 409). */
export interface ActionResponse {
  success: boolean;
  event?: WorldEventEnvelope;
  reason?: string;
}

/** Readable event line pushed into the store's event stream. */
export interface WorldEventItem {
  sequence: number;
  worldTime: number;
  type: WorldEventType;
  text: string;
  agentId?: string | null;
  importance?: number;
}

/** One chat line inside a conversation (REST detail + WS conversation_message). */
export interface ConversationMessage {
  from_agent_id: string;
  to_agent_id: string;
  message: string;
  intent: string;
  sent_at: number;
}

/** One conversation of an agent (GET .../conversations response entry, newest first). */
export interface ConversationSummary {
  conversation_id: string;
  other_agent_id: string;
  started_at: number | null;
  ended_at: number | null;
  end_reason: string | null;
  messages: ConversationMessage[];
}

/** Payload of the WS conversation_started event. */
export interface ConversationStartedPayload {
  conversation_id: string;
  agent_ids: [string, string];
}

/** Payload of the WS conversation_message event. */
export interface ConversationMessagePayload {
  conversation_id: string;
  from_agent_id: string;
  to_agent_id: string;
  message: string;
  intent: string;
}

/** Payload of the WS conversation_ended event. */
export interface ConversationEndedPayload {
  conversation_id: string;
  reason: string;
}

/** Discriminated payload of the three conversation WS event types. */
export type ConversationEventPayload =
  | ConversationStartedPayload
  | ConversationMessagePayload
  | ConversationEndedPayload;

/** One memory of an agent (GET .../memories response entry, newest first). */
export interface MemoryItem {
  memory_id: string;
  /** working 工作记忆 | episodic 情节记忆 | semantic 语义记忆. */
  memory_type: string;
  text: string;
  importance: number;
  created_at: number;
  recall_count: number;
}

/** One relationship of an agent (GET .../relationships response entry). */
export interface RelationshipItem {
  source_agent_id: string;
  target_agent_id: string;
  target_name: string;
  familiarity: number;
  trust: number;
  affection: number;
  resentment: number;
  debt: number;
  updated_at: number;
}

/** Payload of the WS memory_created event. */
export interface MemoryCreatedPayload {
  agent_id: string;
  memory_id: string;
  memory_type: string;
  text: string;
  importance: number;
}

/** Payload of the WS relationship_changed event. */
export interface RelationshipChangedPayload {
  source_agent_id: string;
  target_agent_id: string;
  /** Signed deltas per axis (familiarity/trust/affection/resentment/debt). */
  deltas: Record<string, number>;
  /** Absolute values after applying the delta. */
  values: Record<string, number>;
}

/** Payload of the WS daily_reflection event. */
export interface DailyReflectionPayload {
  agent_id: string;
  summary: string;
}
