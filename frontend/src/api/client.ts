/**
 * Backend API client. The backend serves world_data as static files at
 * /assets/world_data, exposes GET /health, and — since M2 — the world
 * runtime REST API (GET/POST /api/worlds..., pause/resume/speed, agent
 * actions) plus the world WebSocket endpoint.
 */

import type { ActionResponse, WorldListItem, WorldSnapshotPayload } from '../types/world';

export interface HealthResponse {
  status: string;
  map_version: string;
}

export const apiBase: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

/** Join a world_data-relative path onto the backend static mount. */
export function mapAssetUrl(rel: string, base: string = apiBase): string {
  return `${base.replace(/\/+$/, '')}/assets/world_data/${rel.replace(/^\/+/, '')}`;
}

export async function checkHealth(): Promise<HealthResponse> {
  const res = await fetch(`${apiBase.replace(/\/+$/, '')}/health`);
  if (!res.ok) {
    throw new Error(`Health check failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as HealthResponse;
}

function baseUrl(): string {
  return apiBase.replace(/\/+$/, '');
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${baseUrl()}${path}`, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string; reason?: string; error?: string };
      detail = body.reason ?? body.error ?? (typeof body.detail === 'string' ? body.detail : detail);
    } catch {
      // Non-JSON error body; keep the status text.
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** List existing worlds. */
export function listWorlds(): Promise<WorldListItem[]> {
  return requestJson<WorldListItem[]>('/api/worlds');
}

/** Create a new world. */
export function createWorld(name: string): Promise<WorldListItem> {
  return requestJson<WorldListItem>('/api/worlds', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
}

/** Fetch one world's list entry. */
export function getWorld(id: string): Promise<WorldListItem> {
  return requestJson<WorldListItem>(`/api/worlds/${encodeURIComponent(id)}`);
}

/** Fetch a full world snapshot. */
export function getSnapshot(id: string): Promise<WorldSnapshotPayload> {
  return requestJson<WorldSnapshotPayload>(`/api/worlds/${encodeURIComponent(id)}/snapshot`);
}

/** Pause the world clock. */
export function pauseWorld(id: string): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}/pause`, { method: 'POST' });
}

/** Resume the world clock. */
export function resumeWorld(id: string): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}/resume`, { method: 'POST' });
}

/** Change the world speed multiplier (1|2|5|10). */
export function setSpeed(id: string, speed: number): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}/speed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speed }),
  });
}

export interface AgentActionRequest {
  action_type: 'move' | 'wait' | string;
  destination_id?: string;
  reason?: string;
}

/**
 * Submit an agent action. Resolves with {success, event} on 200; rejects
 * with an AgentActionError carrying {success:false, reason} on 409.
 */
export async function postAgentAction(
  id: string,
  agentId: string,
  action: AgentActionRequest,
): Promise<ActionResponse> {
  const res = await fetch(`${baseUrl()}/api/worlds/${encodeURIComponent(id)}/agents/${encodeURIComponent(agentId)}/actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(action),
  });
  const body = (await res.json().catch(() => null)) as ActionResponse | null;
  if (!res.ok) {
    if (res.status === 409) {
      throw new AgentActionError(body?.reason ?? 'Action rejected', body ?? { success: false });
    }
    throw new Error(body?.reason ?? `Action failed: ${res.status} ${res.statusText}`);
  }
  return body ?? { success: true };
}

/** Error raised when the world engine rejects an agent action (HTTP 409). */
export class AgentActionError extends Error {
  readonly response: ActionResponse;

  constructor(message: string, response: ActionResponse) {
    super(message);
    this.name = 'AgentActionError';
    this.response = response;
  }
}

/** WebSocket URL for a world (http(s) base -> ws(s) endpoint). */
export function wsUrl(worldId: string): string {
  const wsBase = baseUrl().replace(/^http/, 'ws');
  return `${wsBase}/ws/worlds/${encodeURIComponent(worldId)}`;
}
