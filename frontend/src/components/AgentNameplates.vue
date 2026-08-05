<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue';
import { taskLabelOf, useWorldStore } from '../stores/worldStore';
import type { AgentSnapshot } from '../types/world';

/**
 * DOM overlay of nameplate chips above every agent: name in the agent's
 * color plus the live task label (工作/前往/等待/对话/空闲). Hidden while a
 * speech bubble occupies the same slot; clicking selects the agent.
 */

const props = defineProps<{
  agents: AgentSnapshot[];
  agentScreenPos: (agentId: string) => { x: number; y: number } | null;
  bubbles: { agent_id: string }[];
}>();

const store = useWorldStore();
const els = new Map<string, HTMLElement>();

function setEl(agentId: string, el: unknown): void {
  if (el instanceof HTMLElement) els.set(agentId, el);
  else els.delete(agentId);
}

let raf = 0;

function frame(): void {
  for (const agent of props.agents) {
    const el = els.get(agent.agent_id);
    if (!el) continue;
    // A live bubble owns the head slot; hide the nameplate until it clears.
    if (props.bubbles.some((b) => b.agent_id === agent.agent_id)) {
      el.style.visibility = 'hidden';
      continue;
    }
    const pos = props.agentScreenPos(agent.agent_id);
    if (!pos) {
      el.style.visibility = 'hidden';
      continue;
    }
    el.style.visibility = 'visible';
    el.style.transform = `translate(${pos.x}px, ${pos.y}px) translate(-50%, -100%) translateY(-22px)`;
  }
  raf = requestAnimationFrame(frame);
}

onMounted(() => {
  raf = requestAnimationFrame(frame);
});

onBeforeUnmount(() => {
  cancelAnimationFrame(raf);
  els.clear();
});

function inConversation(agentId: string): boolean {
  return Object.values(store.activeConversations).some((c) => c.agent_ids.includes(agentId));
}

/** Task chip class: colored by what the agent is doing. */
function taskClass(agent: AgentSnapshot): string {
  if (inConversation(agent.agent_id)) return 'task-talk';
  if (!agent.action) return 'task-idle';
  return `task-${agent.action.type}`;
}
</script>

<template>
  <div class="nameplate-overlay" aria-hidden="true">
    <div
      v-for="agent in agents"
      :key="agent.agent_id"
      :ref="(el) => setEl(agent.agent_id, el)"
      class="nameplate"
      @click="store.selectAgent(agent.agent_id)"
    >
      <span class="np-name" :style="{ color: store.agentColors[agent.agent_id] }">
        {{ agent.name }}
      </span>
      <span class="np-task" :class="taskClass(agent)">{{ taskLabelOf(agent.action, store.locations, inConversation(agent.agent_id)) }}</span>
    </div>
  </div>
</template>

<style scoped>
.nameplate-overlay {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 3;
}
.nameplate {
  position: absolute;
  left: 0;
  top: 0;
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  max-width: 150px;
  padding: 1px 7px 2px;
  border-radius: 999px;
  background: rgba(8, 18, 12, 0.82);
  border: 1px solid rgba(255, 255, 255, 0.14);
  font-size: 10.5px;
  line-height: 1.35;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  box-shadow: 0 2px 5px rgba(0, 0, 0, 0.4);
  pointer-events: auto;
  cursor: pointer;
  user-select: none;
  will-change: transform;
}
.nameplate:hover {
  border-color: rgba(255, 224, 130, 0.8);
}
.np-name {
  font-weight: 600;
  flex-shrink: 0;
}
.np-task {
  color: rgba(205, 232, 213, 0.75);
  overflow: hidden;
  text-overflow: ellipsis;
}
.np-task.task-work {
  color: #ffd54f;
}
.np-task.task-talk {
  color: #f48fb1;
}
.np-task.task-move {
  color: #9fd8ae;
}
.np-task.task-idle {
  color: rgba(205, 232, 213, 0.5);
}
</style>
