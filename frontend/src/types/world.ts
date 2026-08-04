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
