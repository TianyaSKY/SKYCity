/**
 * Movement animation math: tweens are expressed in GAME time (world_time
 * minutes), so progress is `(now - startedAt) / (endsAt - startedAt)` and
 * the same tween stretches/shrinks automatically when the world speed
 * changes. A move follows the backend BFS `path` waypoint list: each segment
 * gets an equal share of the span (the backend charges MINUTES_PER_STEP per
 * path step, uniform for all 8 directions). Positions are interpolated with
 * smoothstep easing over the whole journey.
 */

import type {Cell} from '../types/world';

export interface MoveTween {
    from: Cell;
    to: Cell;
    /** Waypoints from == path[0] through to == path[path.length - 1]. */
    path: Cell[];
    startedAt: number;
    endsAt: number;
}

/** Smoothstep easing: 0 at t<=0, 1 at t>=1, zero slope at both ends. */
export function smoothstep(t: number): number {
    const x = Math.min(1, Math.max(0, t));
    return x * x * (3 - 2 * x);
}

/** Clamped linear progress (0..1) of a game-time move at world time `now`. */
export function moveProgress(tween: MoveTween, now: number): number {
    const span = tween.endsAt - tween.startedAt;
    if (span <= 0) return 1;
    return Math.min(1, Math.max(0, (now - tween.startedAt) / span));
}

/** Interpolated (col,row) for a tween at `now`, smoothstep-eased.
 * Follows `path` segment by segment; falls back to the from->to chord when
 * the path is missing or degenerate. */
export function computeMovePosition(tween: MoveTween, now: number): Cell {
    const t = smoothstep(moveProgress(tween, now));
    const path = tween.path.length >= 2 ? tween.path : [tween.from, tween.to];
    const segments = path.length - 1;
    if (segments <= 0) return path[0] ?? tween.to;
    const seg = Math.min(segments - 1, Math.floor(t * segments));
    const u = Math.min(1, t * segments - seg);
    const a = path[seg];
    const b = path[seg + 1];
    return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
}

/** Registry of per-agent tweens used by the agent layer. */
export class MovementAnimator {
    private readonly tweens = new Map<string, MoveTween>();

    setTween(agentId: string, tween: MoveTween): void {
        this.tweens.set(agentId, tween);
    }

    clearTween(agentId: string): void {
        this.tweens.delete(agentId);
    }

    hasTween(agentId: string): boolean {
        return this.tweens.has(agentId);
    }

    /** Interpolated position at `now`, or null when no tween is active. */
    getPosition(agentId: string, now: number): Cell | null {
        const tween = this.tweens.get(agentId);
        return tween ? computeMovePosition(tween, now) : null;
    }

    clear(): void {
        this.tweens.clear();
    }
}
