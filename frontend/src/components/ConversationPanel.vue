<script setup lang="ts">
import { computed, ref } from 'vue';
import { CONVERSATION_END_REASONS, INTENT_LABELS, useWorldStore } from '../stores/worldStore';

/**
 * Right-side conversation history panel for the selected agent: header with
 * the agent's name, a collapsible list of their conversations (newest
 * first), chat lines per message, start/end times (world clock HH:MM) and
 * an ended badge with the reason label. Live events append to open
 * conversations; ended ones flip to the gray badge in place.
 */

const store = useWorldStore();
const collapsed = ref(false);

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
</script>

<template>
  <aside v-if="store.selectedAgentId" class="conv-panel">
    <header class="conv-header" @click="collapsed = !collapsed">
      <span class="caret">{{ collapsed ? '▸' : '▾' }}</span>
      <span class="conv-title">对话 · {{ selectedAgent?.name ?? store.selectedAgentId }}</span>
      <button class="close-btn" title="取消选择" @click.stop="store.selectAgent(null)">×</button>
    </header>
    <div v-if="!collapsed" class="conv-body">
      <p v-if="store.conversations.length === 0" class="conv-empty">暂无对话记录</p>
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
  </aside>
</template>

<style scoped>
.conv-panel {
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
.conv-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  cursor: pointer;
  user-select: none;
}
.caret {
  color: #9fd8ae;
  font-size: 10px;
}
.conv-title {
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
.conv-body {
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.conv-empty {
  margin: 0;
  padding: 18px 8px;
  text-align: center;
  color: rgba(205, 232, 213, 0.55);
}
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
</style>
