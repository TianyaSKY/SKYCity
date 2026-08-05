<script setup lang="ts">
import { computed, ref } from 'vue';
import {
  CONVERSATION_END_REASONS,
  INTENT_LABELS,
  MEMORY_TYPE_LABELS,
  RELATIONSHIP_AXIS_LABELS,
  useWorldStore,
} from '../stores/worldStore';
import type { RelationshipItem } from '../types/world';

/**
 * Right-side drawer for the selected agent: a tabbed panel with 对话
 * (conversation history), 记忆 (REST memories) and 关系 (REST
 * relationships). The conversation tab renders the same content the old
 * ConversationPanel showed; the two new tabs are REST-backed and refresh
 * via the store when the agent changes or new WS events arrive.
 */

const store = useWorldStore();
const activeTab = ref<'conversations' | 'memories' | 'relationships'>('conversations');

const TAB_LIST = [
  { id: 'conversations', label: '对话' },
  { id: 'memories', label: '记忆' },
  { id: 'relationships', label: '关系' },
] as const;

const selectedAgent = computed(() =>
  store.selectedAgentId ? (store.agentById(store.selectedAgentId) ?? null) : null,
);

function intentLabel(intent: string): string {
  return INTENT_LABELS[intent] ?? intent;
}

function endReasonLabel(reason: string | null): string {
  return reason ? (CONVERSATION_END_REASONS[reason] ?? reason) : '';
}

function timeOf(worldTime: number | null): string {
  if (worldTime == null) return '—';
  const hours = Math.floor(worldTime / 60) % 24;
  const minutes = worldTime % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

function agentName(agentId: string): string {
  return store.agentById(agentId)?.name ?? agentId;
}

/** CSS class of the memory type chip (color-coded). */
const MEMORY_TYPE_COLORS: Record<string, string> = {
  working: 'working',
  episodic: 'episodic',
  semantic: 'semantic',
};

function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_LABELS[type] ?? type;
}

function memoryTypeClass(type: string): string {
  return MEMORY_TYPE_COLORS[type] ?? 'default';
}

/**
 * Importance rendered as 0–5 stars (★ filled); raw value in the tooltip.
 * Backend importance is a 0–1 float, so it is scaled by 5.
 */
function importanceStars(importance: number): string {
  const filled = Math.max(0, Math.min(5, Math.round(importance * 5)));
  return '★'.repeat(filled) + '☆'.repeat(5 - filled);
}

const RELATIONSHIP_AXES = ['familiarity', 'trust', 'affection', 'resentment', 'debt'] as const;
type AxisKey = (typeof RELATIONSHIP_AXES)[number];

function axisValue(rel: RelationshipItem, axis: AxisKey): number {
  return rel[axis];
}

function axisLabel(axis: AxisKey): string {
  return RELATIONSHIP_AXIS_LABELS[axis] ?? axis;
}

/** Bar fill width: raw value clamped to 0–100 (negative values show empty). */
function barWidth(value: number): string {
  return `${Math.max(0, Math.min(100, value))}%`;
}
</script>

<template>
  <aside v-if="store.selectedAgentId" class="agent-panel">
    <header class="agent-header">
      <span class="agent-title">{{ selectedAgent?.name ?? store.selectedAgentId }}</span>
      <button class="close-btn" title="取消选择" @click="store.selectAgent(null)">×</button>
    </header>
    <nav class="agent-tabs">
      <button
        v-for="tab in TAB_LIST"
        :key="tab.id"
        class="agent-tab"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 对话 tab: same content as the former ConversationPanel -->
    <div v-if="activeTab === 'conversations'" class="agent-body">
      <p v-if="store.conversations.length === 0" class="agent-empty">暂无对话记录</p>
      <div v-for="conv in store.conversations" :key="conv.conversation_id" class="conv-item">
        <div class="conv-item-head">
          <span class="other-name">{{ agentName(conv.other_agent_id) }}</span>
          <span class="conv-times">
            {{ timeOf(conv.started_at) }}<template v-if="conv.ended_at != null"> – {{ timeOf(conv.ended_at) }}</template>
          </span>
          <span class="badge" :class="conv.ended_at != null ? 'ended' : 'active'">
            {{ conv.ended_at != null ? '已结束' : '对话中' }}
          </span>
        </div>
        <div v-if="conv.ended_at != null" class="conv-end-reason">
          {{ endReasonLabel(conv.end_reason) }}
        </div>
        <div class="conv-msgs">
          <div v-for="(m, i) in conv.messages" :key="i" class="conv-msg">
            <span class="msg-name" :class="{ self: m.from_agent_id === store.selectedAgentId }">
              {{ agentName(m.from_agent_id) }}
            </span>
            <span v-if="m.intent" class="msg-intent">{{ intentLabel(m.intent) }}</span>
            <span class="msg-text">{{ m.message }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 记忆 tab -->
    <div v-if="activeTab === 'memories'" class="agent-body">
      <p v-if="store.memories.length === 0" class="agent-empty">还没有记忆</p>
      <div v-for="m in store.memories" :key="m.memory_id" class="mem-item">
        <div class="mem-item-head">
          <span class="mem-chip" :class="memoryTypeClass(m.memory_type)">{{ memoryTypeLabel(m.memory_type) }}</span>
          <span class="mem-time">{{ timeOf(m.created_at) }}</span>
          <span class="mem-stars" :title="`重要度 ${m.importance}`">{{ importanceStars(m.importance) }}</span>
        </div>
        <div class="mem-text">{{ m.text }}</div>
        <div class="mem-foot">回忆 {{ m.recall_count }} 次</div>
      </div>
    </div>

    <!-- 关系 tab -->
    <div v-if="activeTab === 'relationships'" class="agent-body">
      <p v-if="store.relationships.length === 0" class="agent-empty">还没有关系</p>
      <div v-for="rel in store.relationships" :key="rel.target_agent_id" class="rel-item">
        <div class="rel-target">{{ rel.target_name }}</div>
        <div v-for="axis in RELATIONSHIP_AXES" :key="axis" class="rel-axis">
          <span class="rel-axis-label">{{ axisLabel(axis) }}</span>
          <span class="rel-axis-bar">
            <i class="rel-axis-fill" :class="`fill-${axis}`" :style="{ width: barWidth(axisValue(rel, axis)) }" />
          </span>
          <span class="rel-axis-value">{{ axisValue(rel, axis) }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.agent-panel {
  display: flex;
  flex-direction: column;
  width: 300px;
  max-height: 100%;
  border-radius: 12px;
  background: rgba(6, 12, 9, 0.82);
  border: 1px solid rgba(255, 255, 255, 0.1);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
  color: #cde8d5;
  font-size: 12px;
  overflow: hidden;
  pointer-events: auto;
  backdrop-filter: blur(4px);
}
.agent-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  user-select: none;
}
.agent-title {
  flex: 1;
  font-weight: 600;
  color: #ffe082;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.close-btn {
  border: none;
  background: rgba(255, 255, 255, 0.08);
  color: #cde8d5;
  width: 20px;
  height: 20px;
  border-radius: 6px;
  line-height: 1;
  cursor: pointer;
  font-size: 14px;
}
.close-btn:hover {
  background: rgba(229, 57, 53, 0.4);
}
.agent-tabs {
  display: flex;
  gap: 4px;
  padding: 6px 8px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.agent-tab {
  flex: 1;
  border: none;
  background: transparent;
  color: rgba(205, 232, 213, 0.6);
  padding: 5px 0 7px;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  font-size: 12px;
}
.agent-tab.active {
  color: #ffe082;
  border-bottom-color: #ffe082;
}
.agent-tab:hover {
  color: #cde8d5;
}
.agent-body {
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.agent-empty {
  margin: 0;
  padding: 18px 8px;
  text-align: center;
  color: rgba(205, 232, 213, 0.55);
}

/* 对话 tab */
.conv-item {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.04);
  padding: 8px 10px;
}
.conv-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.other-name {
  font-weight: 600;
  color: #e8f5e9;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-times {
  flex: 1;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: rgba(205, 232, 213, 0.6);
  white-space: nowrap;
}
.badge {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  flex-shrink: 0;
}
.badge.active {
  background: rgba(76, 175, 80, 0.3);
  border: 1px solid rgba(76, 175, 80, 0.8);
  color: #c8e6c9;
}
.badge.ended {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.14);
  color: rgba(205, 232, 213, 0.75);
}
.conv-end-reason {
  margin-top: 4px;
  color: rgba(205, 232, 213, 0.55);
  font-size: 11px;
}
.conv-msgs {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.conv-msg {
  display: flex;
  align-items: baseline;
  gap: 6px;
  line-height: 1.4;
}
.msg-name {
  font-weight: 600;
  flex-shrink: 0;
}
.msg-name.self {
  color: #9fd8ae;
}
.msg-intent {
  flex-shrink: 0;
  padding: 0 5px;
  border-radius: 999px;
  background: rgba(255, 224, 130, 0.14);
  color: #ffe082;
  font-size: 10px;
}
.msg-text {
  color: rgba(255, 255, 255, 0.88);
  word-break: break-word;
}

/* 记忆 tab */
.mem-item {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.04);
  padding: 8px 10px;
}
.mem-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mem-chip {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  flex-shrink: 0;
}
.mem-chip.working {
  background: rgba(100, 181, 246, 0.18);
  border: 1px solid rgba(100, 181, 246, 0.7);
  color: #bbdefb;
}
.mem-chip.episodic {
  background: rgba(255, 183, 77, 0.18);
  border: 1px solid rgba(255, 183, 77, 0.7);
  color: #ffe0b2;
}
.mem-chip.semantic {
  background: rgba(186, 104, 200, 0.18);
  border: 1px solid rgba(186, 104, 200, 0.7);
  color: #e1bee7;
}
.mem-chip.default {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.2);
  color: #cde8d5;
}
.mem-time {
  flex: 1;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: rgba(205, 232, 213, 0.6);
}
.mem-stars {
  color: #ffd54f;
  font-size: 11px;
  letter-spacing: 1px;
  flex-shrink: 0;
}
.mem-text {
  margin-top: 6px;
  color: rgba(255, 255, 255, 0.88);
  word-break: break-word;
  line-height: 1.4;
}
.mem-foot {
  margin-top: 4px;
  color: rgba(205, 232, 213, 0.5);
  font-size: 11px;
}

/* 关系 tab */
.rel-item {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.04);
  padding: 8px 10px;
}
.rel-target {
  font-weight: 600;
  color: #e8f5e9;
  margin-bottom: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rel-axis {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 3px;
}
.rel-axis-label {
  width: 38px;
  flex-shrink: 0;
  color: rgba(205, 232, 213, 0.65);
  font-size: 11px;
}
.rel-axis-bar {
  flex: 1;
  height: 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  overflow: hidden;
}
.rel-axis-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
}
.rel-axis-fill.familiarity {
  background: #64b5f6;
}
.rel-axis-fill.trust {
  background: #81c784;
}
.rel-axis-fill.affection {
  background: #ffb74d;
}
.rel-axis-fill.resentment {
  background: #e57373;
}
.rel-axis-fill.debt {
  background: #b0bec5;
}
.rel-axis-value {
  width: 34px;
  flex-shrink: 0;
  text-align: right;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.8);
}
</style>
