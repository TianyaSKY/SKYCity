/**
 * World WebSocket placeholder — wired up in M2 for agent/event streaming.
 */

export interface WorldSocketMessage {
  type: string;
  [key: string]: unknown;
}

export function createWorldSocket(_url?: string): WebSocket | null {
  return null;
}
