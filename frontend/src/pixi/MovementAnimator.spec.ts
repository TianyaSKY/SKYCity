/**
 * MovementAnimator path-following: tweens interpolate along the backend BFS
 * waypoint list (each segment gets an equal share of the game-time span),
 * and degenerate/missing paths fall back to the from->to chord.
 */
import {describe, expect, it} from 'vitest';
import {computeMovePosition, smoothstep, type MoveTween} from './MovementAnimator';

function tween(path: MoveTween['path'], startedAt = 0, endsAt = 10): MoveTween {
    return {
        from: path[0],
        to: path[path.length - 1],
        path,
        startedAt,
        endsAt,
    };
}

describe('computeMovePosition', () => {
    it('follows the waypoint list, hitting each waypoint in order', () => {
        const tw = tween([[0, 0], [0, 5], [5, 5]]);
        expect(computeMovePosition(tw, 0)).toEqual([0, 0]);
        // smoothstep(0.5) == 0.5 -> exactly the middle waypoint.
        expect(computeMovePosition(tw, 5)).toEqual([0, 5]);
        expect(computeMovePosition(tw, 10)).toEqual([5, 5]);
    });

    it('interpolates within a segment with smoothstep of total progress', () => {
        const tw = tween([[0, 0], [0, 5], [5, 5]]);
        // t=0.25 -> smoothstep 0.15625; first of two segments -> u=0.3125.
        const pos = computeMovePosition(tw, 2.5);
        expect(pos[0]).toBe(0);
        expect(pos[1]).toBeCloseTo(5 * 0.3125, 10);
        // t=0.75 -> smoothstep 0.84375; second segment -> u=0.6875.
        const pos2 = computeMovePosition(tw, 7.5);
        expect(pos2[0]).toBeCloseTo(5 * 0.6875, 10);
        expect(pos2[1]).toBe(5);
    });

    it('a two-point path behaves like a plain chord', () => {
        const tw = tween([[3, 4], [5, 5]]);
        const pos = computeMovePosition(tw, 5);
        expect(pos).toEqual([
            3 + 2 * smoothstep(0.5),
            4 + 1 * smoothstep(0.5),
        ]);
    });

    it('missing path falls back to from->to', () => {
        const tw: MoveTween = {from: [0, 0], to: [4, 4], path: [], startedAt: 0, endsAt: 10};
        const pos = computeMovePosition(tw, 5);
        expect(pos).toEqual([4 * smoothstep(0.5), 4 * smoothstep(0.5)]);
    });

    it('single-cell path stays put', () => {
        const tw = tween([[7, 7]]);
        expect(computeMovePosition(tw, 5)).toEqual([7, 7]);
        expect(computeMovePosition(tw, 20)).toEqual([7, 7]);
    });
});
