/**
 * AgentLayer unit tests: sprite lifecycle and same-cell fan-out.
 * Positions are world pixels; a sprite's feet anchor sits at
 * (col * AGENT_TILE_SIZE + 8, row * AGENT_TILE_SIZE + 12).
 */

import {describe, expect, it} from 'vitest';
import type {AgentSnapshot, MoveAction} from '../types/world';
import {AGENT_TILE_SIZE, AgentSprite} from './AgentSprite';
import {AgentLayer} from './AgentLayer';

const T = AGENT_TILE_SIZE;

function agent(id: string, col: number, row: number, action: AgentSnapshot['action'] = null): AgentSnapshot {
    return {
        agent_id: id,
        name: id,
        col,
        row,
        location_id: null,
        satiety: 100,
        energy: 100,
        mood: 100,
        loneliness: 0,
        money: 0,
        inventory: [],
        action,
    };
}

/** A move tween spanning cells (0,1) -> (2,1); `now` picks the midpoint. */
function moveAction(from: [number, number], to: [number, number]): MoveAction {
    return {type: 'move', from, to, path: [from, to], started_at: 0, ends_at: 100};
}

function makeLayer(): { layer: AgentLayer; spriteOf: (id: string) => AgentSprite } {
    const layer = new AgentLayer(() => '#ff0000', (x, y) => ({x, y}));
    return {
        layer,
        spriteOf: (id: string): AgentSprite => {
            const sprite = (layer.container.children as AgentSprite[]).find((s) => s.label === id);
            if (!sprite) throw new Error(`no sprite for ${id}`);
            return sprite;
        },
    };
}

describe('AgentLayer', () => {
    it('sync creates one sprite per agent and destroys removed ones', () => {
        const {layer, spriteOf} = makeLayer();
        layer.sync([agent('a', 1, 1), agent('b', 2, 2)]);
        expect(layer.container.children.length).toBe(2);
        layer.sync([agent('a', 1, 1)]);
        expect(layer.container.children.length).toBe(1);
        expect(spriteOf('a').position.x).toBe(1 * T + 8);
        expect(spriteOf('a').position.y).toBe(1 * T + 12);
        layer.clear();
        expect(layer.container.children.length).toBe(0);
    });

    it('update snaps stationary sprites to their cell', () => {
        const {layer, spriteOf} = makeLayer();
        layer.sync([agent('a', 3, 4)]);
        layer.update(0, 0);
        expect(spriteOf('a').position.x).toBe(3 * T + 8);
        expect(spriteOf('a').position.y).toBe(4 * T + 12);
    });

    it('two agents on the same cell fan out symmetrically around it', () => {
        const {layer, spriteOf} = makeLayer();
        layer.sync([agent('a', 1, 1), agent('b', 1, 1)]);
        layer.update(0, 0);
        const ax = spriteOf('a').position.x;
        const bx = spriteOf('b').position.x;
        expect(ax).not.toBe(bx);
        // Centered on the cell anchor, one on each side.
        expect((ax + bx) / 2).toBe(1 * T + 8);
        // Within the tile: no sprite leaves the cell.
        expect(Math.abs(ax - (1 * T + 8))).toBeLessThanOrEqual(T / 2);
        expect(Math.abs(bx - (1 * T + 8))).toBeLessThanOrEqual(T / 2);
    });

    it('three agents on the same cell keep the middle one centered', () => {
        const {layer, spriteOf} = makeLayer();
        layer.sync([agent('a', 2, 2), agent('b', 2, 2), agent('c', 2, 2)]);
        layer.update(0, 0);
        const xs = ['a', 'b', 'c'].map((id) => spriteOf(id).position.x).sort((m, n) => m - n);
        expect(xs[1]).toBe(2 * T + 8);
        expect(xs[2] - xs[1]).toBe(xs[1] - xs[0]);
    });

    it('a single agent on a cell stays exactly centered (no offset)', () => {
        const {layer, spriteOf} = makeLayer();
        layer.sync([agent('a', 5, 5)]);
        layer.update(0, 0);
        expect(spriteOf('a').position.x).toBe(5 * T + 8);
    });

    it('moving agents keep their interpolated position (no fan-out)', () => {
        const {layer, spriteOf} = makeLayer();
        // b walks through cell (1,1) while a waits there.
        layer.sync([
            agent('a', 1, 1),
            agent('b', 0, 1, moveAction([0, 1], [2, 1])),
        ]);
        layer.update(50, 0); // tween midpoint -> (1,1)
        expect(spriteOf('b').position.x).toBe(1 * T + 8);
        expect(spriteOf('b').position.y).toBe(1 * T + 12);
        // a is alone on its cell: no offset either.
        expect(spriteOf('a').position.x).toBe(1 * T + 8);
    });
});
