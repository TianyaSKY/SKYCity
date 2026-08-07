<script lang="ts" setup>
import {computed, onBeforeUnmount, onMounted, ref} from 'vue';
import {actionRemainingMinutes, taskLabelOf, taskPriority, useWorldStore,} from '../stores/worldStore';
import type {AgentSnapshot} from '../types/world';

/**
 * 任务一览: every resident's current task in one column — name, live task
 * label, remaining game minutes and wallet. Busy agents float to the top
 * (work > move > wait > conversation > idle). A 1s real-time tick refreshes
 * the remaining-time column; positions/needs stay store-driven.
 */

const store = useWorldStore();

const tick = ref(0);
let timer = 0;

onMounted(() => {
    timer = window.setInterval(() => {
        tick.value += 1;
    }, 1000);
});

onBeforeUnmount(() => {
    window.clearInterval(timer);
});

function inConversation(agentId: string): boolean {
    return Object.values(store.activeConversations).some((c) => c.agent_ids.includes(agentId));
}

const busyCount = computed(() =>
    store.agents.filter((a) => taskLabelOf(a.action, store.locations, inConversation(a.agent_id)) !== '空闲').length,
);

const sortedAgents = computed(() =>
    [...store.agents].sort((a, b) => {
        const pa = taskPriority(a.action, inConversation(a.agent_id));
        const pb = taskPriority(b.action, inConversation(b.agent_id));
        if (pa !== pb) return pa - pb;
        return a.name.localeCompare(b.name, 'zh');
    }),
);

function remainingText(agent: AgentSnapshot): string {
    const minutes = actionRemainingMinutes(agent.action, store.worldTime);
    if (minutes == null) return '';
    if (minutes === 0) return '即将完成';
    return `${minutes} 分钟`;
}

function taskClass(agent: AgentSnapshot): string {
    if (inConversation(agent.agent_id)) return 'task-talk';
    if (!agent.action) return 'task-idle';
    return `task-${agent.action.type}`;
}

function select(agent: AgentSnapshot): void {
    store.selectLocation(null);
    store.selectAgent(agent.agent_id);
}
</script>

<template>
    <aside aria-label="任务一览" class="task-board">
        <header class="tb-head">
            <span class="tb-title">任务一览</span>
            <span :class="{ busy: busyCount > 0 }" class="tb-count">{{ busyCount }}/{{
                    store.agents.length
                }} 忙碌</span>
        </header>
        <p v-if="store.agents.length === 0" class="tb-empty">世界还没有居民</p>
        <ul v-else class="tb-list">
            <li
                v-for="agent in sortedAgents"
                :key="agent.agent_id"
                :class="{ selected: store.selectedAgentId === agent.agent_id }"
                class="tb-row"
                @click="select(agent)"
            >
                <span :style="{ background: store.agentColors[agent.agent_id] }" class="tb-dot"/>
                <span class="tb-name">{{ agent.name }}</span>
                <span :class="taskClass(agent)" class="tb-task">
          {{ taskLabelOf(agent.action, store.locations, inConversation(agent.agent_id)) }}
        </span>
                <span class="tb-remaining">{{ remainingText(agent) }}</span>
                <span class="tb-money">{{ agent.money }}</span>
            </li>
        </ul>
    </aside>
</template>

<style scoped>
.task-board {
    width: 236px;
    max-height: min(46vh, 460px);
    display: flex;
    flex-direction: column;
    border-radius: 10px;
    background: rgba(6, 12, 9, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
    color: #cde8d5;
    font-size: 11.5px;
    pointer-events: auto;
    backdrop-filter: blur(4px);
    overflow: hidden;
}

.tb-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 7px 12px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    user-select: none;
}

.tb-title {
    flex: 1;
    font-weight: 600;
    color: #ffe082;
    font-size: 12px;
}

.tb-count {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10.5px;
    color: rgba(205, 232, 213, 0.55);
}

.tb-count.busy {
    color: #ffd54f;
}

.tb-empty {
    margin: 0;
    padding: 14px 8px;
    text-align: center;
    color: rgba(205, 232, 213, 0.5);
}

.tb-list {
    list-style: none;
    margin: 0;
    padding: 4px 6px 6px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1px;
}

.tb-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 6px;
    border-radius: 6px;
    cursor: pointer;
    line-height: 1.4;
}

.tb-row:hover {
    background: rgba(255, 255, 255, 0.06);
}

.tb-row.selected {
    background: rgba(255, 224, 130, 0.12);
}

.tb-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
}

.tb-name {
    font-weight: 600;
    flex-shrink: 0;
    color: #e8f5e9;
    max-width: 64px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.tb-task {
    flex: 1;
    min-width: 0;
    color: rgba(205, 232, 213, 0.8);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.tb-task.task-work {
    color: #ffd54f;
}

.tb-task.task-talk {
    color: #f48fb1;
}

.tb-task.task-move {
    color: #9fd8ae;
}

.tb-task.task-idle {
    color: rgba(205, 232, 213, 0.45);
}

.tb-remaining {
    flex-shrink: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
    color: rgba(205, 232, 213, 0.6);
}

.tb-money {
    flex-shrink: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
    color: #ffd54f;
    opacity: 0.85;
    min-width: 22px;
    text-align: right;
}
</style>
