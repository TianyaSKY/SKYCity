/**
 * Backend API client. The backend serves world_data as static files at
 * /assets/world_data, exposes GET /health, and — since M2 — the world
 * runtime REST API (GET/POST /api/worlds..., pause/resume/speed, agent
 * actions) plus the world WebSocket endpoint.
 */

import type {
    ActionResponse,
    AgentDetail,
    AgentEmploymentResponse,
    CompanyEmployee,
    CompanyInfo,
    CompanyInventoryItem,
    CompanyPosition,
    CompanyTransaction,
    ConversationSummary,
    DecisionRecord,
    GodActionRequest,
    GodActionResult,
    JobOpening,
    LocationDetail,
    MemoryItem,
    RelationshipItem,
    StocksResponse,
    WorkShiftInfo,
    WorldListItem,
    WorldSnapshotPayload,
} from '../types/world';

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

/** Create a new autonomous world (LLM agents act on their own). */
export function createWorld(name: string): Promise<WorldListItem> {
    return requestJson<WorldListItem>('/api/worlds', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, autonomous: true}),
    });
}

/** Fetch one world's list entry. */
export function getWorld(id: string): Promise<WorldListItem> {
    return requestJson<WorldListItem>(`/api/worlds/${encodeURIComponent(id)}`);
}

/** Permanently delete a world; its state (agents/events/llm_runs/saves) cascades server-side. */
export async function deleteWorld(id: string): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}`, {
        method: 'DELETE',
    });
}

/** Fetch a full world snapshot. */
export function getSnapshot(id: string): Promise<WorldSnapshotPayload> {
    return requestJson<WorldSnapshotPayload>(`/api/worlds/${encodeURIComponent(id)}/snapshot`);
}

/** M10: 全部股票行情 + 全量持仓。 */
export function getStocks(worldId: string): Promise<StocksResponse> {
    return requestJson<StocksResponse>(`/api/worlds/${encodeURIComponent(worldId)}/stocks`);
}

/** M13: 世界内全部企业列表。 */
export function getCompanies(worldId: string): Promise<CompanyInfo[]> {
    return requestJson<CompanyInfo[]>(`/api/worlds/${encodeURIComponent(worldId)}/companies`);
}

/** M13: 单个企业详情。 */
export function getCompany(worldId: string, companyId: string): Promise<CompanyInfo> {
    return requestJson<CompanyInfo>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}`,
    );
}

/** M13: 企业的岗位列表（含已招满/空缺）。 */
export function getCompanyPositions(worldId: string, companyId: string): Promise<CompanyPosition[]> {
    return requestJson<CompanyPosition[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}/positions`,
    );
}

/** M13: 企业在职员工列表。 */
export function getCompanyEmployees(worldId: string, companyId: string): Promise<CompanyEmployee[]> {
    return requestJson<CompanyEmployee[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}/employees`,
    );
}

/** M13: 企业资金流水（最近的，新在前）。 */
export function getCompanyTransactions(worldId: string, companyId: string): Promise<CompanyTransaction[]> {
    return requestJson<CompanyTransaction[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}/transactions`,
    );
}

/** M16: 企业仓库库存（总量/预留/可用）。 */
export function getCompanyInventory(worldId: string, companyId: string): Promise<CompanyInventoryItem[]> {
    return requestJson<CompanyInventoryItem[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}/inventory`,
    );
}

export interface PurchaseCompanyGoodsRequest {
    manager_agent_id: string;
    seller_company_id: string;
    item_id: string;
    quantity?: number;
    reason?: string;
}

export interface StockStoreRequest {
    manager_agent_id: string;
    store_id: string;
    item_id: string;
    quantity?: number;
    reason?: string;
}

/** M16: 经理按固定价跨企业采购（manager_agent_id 在 body 中）。 */
export function purchaseCompanyGoods(
    worldId: string,
    companyId: string,
    body: PurchaseCompanyGoodsRequest,
): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}/purchase`,
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)},
    );
}

/** M16: 经理把仓库货物上架到自有商店（manager_agent_id 在 body 中）。 */
export function stockStore(
    worldId: string,
    companyId: string,
    body: StockStoreRequest,
): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
        `/api/worlds/${encodeURIComponent(worldId)}/companies/${encodeURIComponent(companyId)}/stock`,
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)},
    );
}

/** M13: 世界内全部公开招聘职位。 */
export function getJobOpenings(worldId: string): Promise<JobOpening[]> {
    return requestJson<JobOpening[]>(`/api/worlds/${encodeURIComponent(worldId)}/job-openings`);
}

/** M13: 一位居民的正式雇佣信息 + 最近班次。 */
export function getAgentEmployment(worldId: string, agentId: string): Promise<AgentEmploymentResponse> {
    return requestJson<AgentEmploymentResponse>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}/employment`,
    );
}

/** M13: 一位居民的班次记录（最近的，新在前）。 */
export function getAgentShifts(worldId: string, agentId: string): Promise<WorkShiftInfo[]> {
    return requestJson<WorkShiftInfo[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}/shifts`,
    );
}

/** Pause the world clock. */
export function pauseWorld(id: string): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}/pause`, {method: 'POST'});
}

/** Resume the world clock. */
export function resumeWorld(id: string): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}/resume`, {method: 'POST'});
}

/** Change the world speed multiplier (1|2|5|10). */
export function setSpeed(id: string, speed: number): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(`/api/worlds/${encodeURIComponent(id)}/speed`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({speed}),
    });
}

/** Fetch one agent's conversation history, newest first (limit <= 0 = server default). */
export function getConversations(
    worldId: string,
    agentId: string,
    limit = 20,
): Promise<ConversationSummary[]> {
    const query = limit > 0 ? `?limit=${limit}` : '';
    return requestJson<ConversationSummary[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}/conversations${query}`,
    );
}

/** Fetch one agent's memories, newest first (limit <= 0 = server default). */
export function getMemories(
    worldId: string,
    agentId: string,
    limit = 30,
): Promise<MemoryItem[]> {
    const query = limit > 0 ? `?limit=${limit}` : '';
    return requestJson<MemoryItem[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}/memories${query}`,
    );
}

/** Fetch one agent's relationships (one entry per target agent). */
export function getRelationships(worldId: string, agentId: string): Promise<RelationshipItem[]> {
    return requestJson<RelationshipItem[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}/relationships`,
    );
}

/** Fetch one agent's full detail: identity card plus live state. */
export function getAgentDetail(worldId: string, agentId: string): Promise<AgentDetail> {
    return requestJson<AgentDetail>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}`,
    );
}

/** Fetch one location's full detail: occupants + store products + jobs. */
export function getLocationDetail(worldId: string, locationId: string): Promise<LocationDetail> {
    return requestJson<LocationDetail>(
        `/api/worlds/${encodeURIComponent(worldId)}/locations/${encodeURIComponent(locationId)}`,
    );
}

/** Fetch one agent's LLM decision history, recent first (limit <= 0 = server default). */
export function getDecisions(worldId: string, agentId: string, limit = 10): Promise<DecisionRecord[]> {
    const query = limit > 0 ? `?limit=${limit}` : '';
    return requestJson<DecisionRecord[]>(
        `/api/worlds/${encodeURIComponent(worldId)}/agents/${encodeURIComponent(agentId)}/decisions${query}`,
    );
}

export interface AgentActionRequest {
    action_type: 'move' | 'wait' | 'talk' | string;
    destination_id?: string;
    target_agent_id?: string;
    message?: string;
    intent?: 'greet' | 'chat' | 'ask' | 'offer' | 'leave' | string;
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
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(action),
    });
    const body = (await res.json().catch(() => null)) as ActionResponse | null;
    if (!res.ok) {
        if (res.status === 409) {
            throw new AgentActionError(body?.reason ?? 'Action rejected', body ?? {success: false});
        }
        throw new Error(body?.reason ?? `Action failed: ${res.status} ${res.statusText}`);
    }
    return body ?? {success: true};
}

/**
 * Submit a god intervention command (pause/resume/speed/weather/money/
 * items/teleport/public events/store stock). Rejects with a
 * GodActionError carrying the server detail on 400/404/5xx; resolves with
 * the command verdict on 200 (success may still be false when the world
 * rejected the command).
 */
export async function postGodAction(worldId: string, body: GodActionRequest): Promise<GodActionResult> {
    const res = await fetch(`${baseUrl()}/api/worlds/${encodeURIComponent(worldId)}/god-actions`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    let parsed: GodActionResult | null = null;
    try {
        parsed = (await res.json()) as GodActionResult;
    } catch {
        // Non-JSON body; keep the status text as the detail.
    }
    if (!res.ok) {
        const errBody = parsed as unknown as { detail?: string; reason?: string; error?: string } | null;
        const detail =
            errBody?.reason ??
            errBody?.error ??
            (typeof errBody?.detail === 'string' ? errBody.detail : `${res.status} ${res.statusText}`);
        throw new GodActionError(res.status, detail);
    }
    return parsed ?? {command_id: '', success: false, result: null, events: []};
}

/** Error raised when the god-actions endpoint rejects a command (HTTP 400/404/5xx). */
export class GodActionError extends Error {
    readonly status: number;

    constructor(status: number, message: string) {
        super(message);
        this.name = 'GodActionError';
        this.status = status;
    }
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
