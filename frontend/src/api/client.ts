/**
 * Backend API client. The backend serves world_data as static files at
 * /assets/world_data and exposes GET /health.
 */

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
