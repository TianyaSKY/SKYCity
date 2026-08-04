/**
 * Agent scene layer: owns one AgentSprite per known agent, feeds tweens to
 * the MovementAnimator from move actions, advances positions on game time,
 * and keeps sprites depth-sorted by row (higher row = closer to viewer).
 */

import { Container } from 'pixi.js';
import type { AgentSnapshot } from '../types/world';
import { AgentSprite } from './AgentSprite';
import type { AgentVisual } from './AgentSprite';
import { MovementAnimator } from './MovementAnimator';

export class AgentLayer {
  readonly container: Container;

  private readonly sprites = new Map<string, AgentSprite>();
  private readonly agentsById = new Map<string, AgentSnapshot>();
  private readonly animator = new MovementAnimator();

  constructor(private readonly colorOf: (agentId: string) => string) {
    this.container = new Container();
    this.container.sortableChildren = true;
  }

  /** Reconcile sprites with the store's agent list (called on store change). */
  sync(agents: AgentSnapshot[]): void {
    const seen = new Set<string>();
    for (const agent of agents) {
      seen.add(agent.agent_id);
      this.agentsById.set(agent.agent_id, agent);
      let sprite = this.sprites.get(agent.agent_id);
      if (!sprite) {
        sprite = new AgentSprite(this.colorOf(agent.agent_id));
        this.sprites.set(agent.agent_id, sprite);
        this.container.addChild(sprite);
      }
      if (agent.action?.type === 'move') {
        this.animator.setTween(agent.agent_id, {
          from: agent.action.from,
          to: agent.action.to,
          startedAt: agent.action.started_at,
          endsAt: agent.action.ends_at,
        });
      } else {
        this.animator.clearTween(agent.agent_id);
        sprite.setCell(agent.col, agent.row);
      }
    }
    for (const [agentId, sprite] of this.sprites) {
      if (seen.has(agentId)) continue;
      this.animator.clearTween(agentId);
      this.agentsById.delete(agentId);
      this.sprites.delete(agentId);
      sprite.destroy({ children: true });
    }
  }

  /**
   * Advance all tweens to world time `now` and re-sort by depth.
   * `bobTime` is a real-time clock used for the waiting idle bob.
   */
  update(now: number, bobTime: number): void {
    for (const [agentId, sprite] of this.sprites) {
      const agent = this.agentsById.get(agentId);
      let visual: AgentVisual = 'idle';
      if (agent) {
        if (this.animator.hasTween(agentId)) {
          visual = 'moving';
          const pos = this.animator.getPosition(agentId, now);
          if (pos) sprite.setCellFrac(pos[0], pos[1]);
        } else {
          sprite.setCell(agent.col, agent.row);
          if (agent.action?.type === 'wait') visual = 'waiting';
        }
      }
      sprite.setVisual(visual);
      if (visual === 'waiting') {
        sprite.y += Math.sin(bobTime * 3 + agentId.length) * 1.5;
      }
    }
    this.sortByRow();
  }

  clear(): void {
    this.animator.clear();
    for (const sprite of this.sprites.values()) {
      sprite.destroy({ children: true });
    }
    this.sprites.clear();
    this.agentsById.clear();
  }

  private sortByRow(): void {
    const children = this.container.children as AgentSprite[];
    children.sort((a, b) => a.position.y - b.position.y);
    for (let i = 0; i < children.length; i++) children[i].zIndex = i;
    this.container.sortChildren();
  }
}
