/**
 * Movement animation math: tweens are expressed in GAME time (world_time
 * minutes), so progress is `(now - startedAt) / (endsAt - startedAt)` and
 * the same tween stretches/shrinks automatically when the world speed
 * changes. Positions are interpolated with smoothstep easing.
 */

import type { Cell } from '../types/world';

export interface MoveTween {
  from: Cell;
  to: Cell;
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

/** Interpolated (col,row) for a tween at `now`, smoothstep-eased. */
export function computeMovePosition(tween: MoveTween, now: number): Cell {
  const t = smoothstep(moveProgress(tween, now));
  return [tween.from[0] + (tween.to[0] - tween.from[0]) * t, tween.from[1] + (tween.to[1] - tween.from[1]) * t];
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
