/**
 * World WebSocket client: connects to /ws/worlds/{world_id}, applies
 * sequence-based dedup and gap recovery, and auto-reconnects with
 * exponential backoff (1s, 2s, 4s, … capped at 10s).
 *
 * The first message over the wire is always a world_snapshot; the client
 * treats it as a full reset. Any later sequence gap triggers a REST
 * snapshot re-fetch to resync.
 */

import {getSnapshot, wsUrl} from '../api/client';
import type {WorldEventEnvelope, WorldSnapshotPayload} from '../types/world';

export type WorldSocketStatus = 'connecting' | 'connected' | 'disconnected';

export interface WorldSocketHandlers {
    onEvent: (envelope: WorldEventEnvelope) => void;
    onSnapshot: (payload: WorldSnapshotPayload) => void;
    onStatus: (status: WorldSocketStatus, detail?: string) => void;
}

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 10000;

export class WorldSocketClient {
    private ws: WebSocket | null = null;
    private worldId: string | null = null;
    private handlers: WorldSocketHandlers | null = null;
    private lastSequence = 0;
    private retryDelay = RECONNECT_BASE_MS;
    private reconnectTimer: number | null = null;
    private closedByUser = true;
    private destroyed = false;

    /**
     * Connect (or re-point handlers when already on the same world — needed
     * because HMR remounts refresh the store closure).
     */
    connect(worldId: string, handlers: WorldSocketHandlers): void {
        if (this.ws && this.worldId === worldId && this.ws.readyState <= WebSocket.OPEN) {
            this.handlers = handlers;
            return;
        }
        this.close();
        this.worldId = worldId;
        this.handlers = handlers;
        this.lastSequence = 0;
        this.closedByUser = false;
        this.open();
    }

    /** Graceful close: stops reconnects and clears the socket. */
    close(): void {
        this.closedByUser = true;
        if (this.reconnectTimer != null) {
            window.clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        const ws = this.ws;
        this.ws = null;
        if (ws) {
            ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
            try {
                ws.close();
            } catch {
                // Already closed.
            }
        }
        this.handlers?.onStatus('disconnected');
    }

    destroy(): void {
        this.destroyed = true;
        this.close();
    }

    private open(): void {
        if (this.destroyed || this.closedByUser || !this.worldId || !this.handlers) return;
        this.handlers.onStatus('connecting');
        let ws: WebSocket;
        try {
            ws = new WebSocket(wsUrl(this.worldId));
        } catch {
            this.scheduleReconnect();
            return;
        }
        this.ws = ws;
        ws.onopen = () => {
            this.retryDelay = RECONNECT_BASE_MS;
            this.handlers?.onStatus('connected');
        };
        ws.onmessage = (ev: MessageEvent) => this.handleMessage(ev);
        ws.onclose = () => {
            if (this.ws === ws) this.ws = null;
            this.handlers?.onStatus('disconnected');
            if (!this.closedByUser) this.scheduleReconnect();
        };
        ws.onerror = () => {
            // onclose always follows; nothing to do here beyond leaving the socket alone.
        };
    }

    private handleMessage(ev: MessageEvent): void {
        let raw: unknown;
        try {
            raw = JSON.parse(String(ev.data));
        } catch {
            return; // non-JSON frame — ignore
        }
        const env = raw as WorldEventEnvelope;
        if (!env || typeof env !== 'object' || typeof env.sequence !== 'number' || typeof env.type !== 'string') {
            return;
        }
        if (env.type === 'world_snapshot') {
            const payload = (env.payload ?? {}) as unknown as WorldSnapshotPayload;
            this.lastSequence = payload.latest_sequence ?? env.sequence;
            this.handlers?.onSnapshot(payload);
            return;
        }
        if (env.sequence <= this.lastSequence) return; // replay / duplicate
        if (env.sequence > this.lastSequence + 1) {
            // Sequence gap — the stream is incomplete; resync from a full snapshot.
            this.lastSequence = env.sequence;
            void this.fetchSnapshotAndReset();
            return;
        }
        this.lastSequence = env.sequence;
        this.handlers?.onEvent(env);
    }

    private async fetchSnapshotAndReset(): Promise<void> {
        if (!this.worldId || !this.handlers) return;
        try {
            const payload = await getSnapshot(this.worldId);
            this.lastSequence = payload.latest_sequence;
            this.handlers.onSnapshot(payload);
        } catch {
            // Snapshot fetch failed; the next reconnect will resync anyway.
        }
    }

    private scheduleReconnect(): void {
        if (this.destroyed || this.closedByUser || this.reconnectTimer != null) return;
        const delay = Math.min(this.retryDelay, RECONNECT_MAX_MS);
        this.reconnectTimer = window.setTimeout(() => {
            this.reconnectTimer = null;
            this.open();
        }, delay);
        this.retryDelay = Math.min(this.retryDelay * 2, RECONNECT_MAX_MS);
    }
}

/** Module-level singleton so HMR remounts never open a second socket. */
let singleton: WorldSocketClient | null = null;

export function getWorldSocket(): WorldSocketClient {
    if (!singleton) singleton = new WorldSocketClient();
    return singleton;
}
