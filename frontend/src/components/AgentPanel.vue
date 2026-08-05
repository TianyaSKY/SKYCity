<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import {
  CONVERSATION_END_REASONS,
  INTENT_LABELS,
  ITEM_NAMES,
  MEMORY_TYPE_LABELS,
  RELATIONSHIP_AXIS_LABELS,
  TOOL_LABELS,
  useWorldStore,
} from '../stores/worldStore';
import type {
  AgentSnapshot,
  DecisionRecord,
  GodActionRequest,
  InventoryItem,
  RelationshipItem,
  WorldLocation,
} from '../types/world';

/**
 * Right-side drawer for the selected agent: a tabbed panel with 总览
 * (identity + needs + decisions), 对话 (conversation history), 记忆 (REST
 * memories), 关系 (REST relationships) and 上帝 (god interventions). The
 * 对话/记忆/关系 tabs are unchanged; 总览 and 上帝 are REST/god-actions
 * backed and refresh via the store when the agent changes or new WS events
 * arrive.
 */

const store = useWorldStore();
const activeTab = ref<'overview' | 'conversations' | 'memories' | 'relationships' | 'god'>('conversations');

const TAB_LIST = [
  { id: 'overview', label: '总览' },
  { id: 'conversations', label: '对话' },
  { id: 'memories', label: '记忆' },
  { id: 'relationships', label: '关系' },
  { id: 'god', label: '上帝' },
] as const;

const selectedAgent = computed(() =>
  store.selectedAgentId ? (store.agentById(store.selectedAgentId) ?? null) : null,
);

/** Live snapshot agent for the selection (needs/inventory/action stay live). */
const liveAgent = computed(() =>
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

/** Display name of an item (catalog label, raw id as fallback). */
function itemLabel(itemId: string): string {
  return ITEM_NAMES[itemId] ?? itemId;
}

/** "面包×1、小麦×2" from an inventory list; 空 when empty. */
function inventoryText(inventory: InventoryItem[] | undefined): string {
  if (!inventory || inventory.length === 0) return '空';
  return inventory.map((it) => `${itemLabel(it.item_id)}×${it.quantity}`).join('、');
}

/** Human-readable current action of an agent ('' = idle). */
function actionText(action: AgentSnapshot['action']): string {
  if (!action) {
    const partner = store.selectedAgentId ? store.activePartnerOf(store.selectedAgentId) : null;
    return partner ? `正在与 ${agentName(partner)} 对话` : '';
  }
  if (action.type === 'move') {
    const loc = store.locations.find((l) => l.col === action.to[0] && l.row === action.to[1]);
    return `正在前往 ${loc?.name ?? `(${action.to[0]}, ${action.to[1]})`}`;
  }
  if (action.type === 'work') {
    return `正在工作 · ${action.job_name ?? action.job_id}`;
  }
  return '正在等待';
}

/** Chinese label of a decision tool name. */
function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool;
}

/** A decision counts as successful when the run and the tool result agree. */
function decisionSuccess(d: DecisionRecord): boolean {
  return d.success && d.tool_result?.success !== false;
}

/** Decision reason: tool rejection reason, else the raw summary. */
function decisionReason(d: DecisionRecord): string {
  const reason = d.tool_result?.reason;
  return typeof reason === 'string' && reason ? reason : d.raw_summary;
}

/* ---- 上帝 tab state ---- */

const godPending = ref(false);
const godResult = ref<{ success: boolean; message: string } | null>(null);
const publicEventText = ref('');
const stockItem = ref<'bread' | 'wheat'>('bread');
const stockQty = ref(5);
const teleportTarget = ref('');

const STOCK_ITEMS = [
  { value: 'bread', label: '面包' },
  { value: 'wheat', label: '小麦' },
] as const;

/** Default teleport destination: the plaza when present, else the first location. */
function defaultTeleportTarget(): string {
  return (
    store.locations.find((l) => l.location_id === 'village_plaza')?.location_id ??
    store.locations[0]?.location_id ??
    ''
  );
}

watch(
  () => store.selectedAgentId,
  () => {
    teleportTarget.value = defaultTeleportTarget();
  },
  { immediate: true },
);

/** Run one god command; the feedback line shows the verdict. */
async function runGodAction(body: GodActionRequest): Promise<void> {
  if (godPending.value) return;
  godPending.value = true;
  godResult.value = null;
  try {
    const result = await store.submitGodAction(body);
    let message = result.success ? '指令已执行' : '指令被世界拒绝';
    if (result.success && result.result && typeof result.result === 'object') {
      const reason = (result.result as Record<string, unknown>).reason;
      if (typeof reason === 'string' && reason) message = reason;
    }
    godResult.value = { success: result.success, message };
  } catch (err) {
    godResult.value = { success: false, message: err instanceof Error ? err.message : String(err) };
  } finally {
    godPending.value = false;
  }
}

function grantMoney(): void {
  if (!store.selectedAgentId) return;
  void runGodAction({
    command_type: 'grant_money',
    target_id: store.selectedAgentId,
    parameters: { amount: 50 },
    reason: '玩家干预',
  });
}

function deductMoney(): void {
  if (!store.selectedAgentId) return;
  void runGodAction({
    command_type: 'deduct_money',
    target_id: store.selectedAgentId,
    parameters: { amount: 50 },
    reason: '玩家干预',
  });
}

function spawnBread(): void {
  if (!store.selectedAgentId) return;
  void runGodAction({
    command_type: 'spawn_item',
    target_id: store.selectedAgentId,
    parameters: { item_id: 'bread', quantity: 1 },
    reason: '玩家干预',
  });
}

function teleportAgent(): void {
  const locationId = teleportTarget.value || defaultTeleportTarget();
  if (!store.selectedAgentId || !locationId) return;
  void runGodAction({
    command_type: 'teleport',
    target_id: store.selectedAgentId,
    parameters: { location_id: locationId },
    reason: '玩家干预',
  });
}

function sendPublicEvent(): void {
  const text = publicEventText.value.trim();
  if (!text) return;
  void runGodAction({
    command_type: 'public_event',
    target_id: null,
    parameters: { text },
    reason: '玩家干预',
  });
  publicEventText.value = '';
}

function setStoreStock(): void {
  void runGodAction({
    command_type: 'change_store_stock',
    target_id: null,
    parameters: { item_id: stockItem.value, quantity: stockQty.value },
    reason: '玩家干预',
  });
}

const stockTarget = ref('');
const stockPrice = ref(1);

function setStockPrice(): void {
  if (!stockTarget.value) return;
  void runGodAction({
    command_type: 'change_stock_price',
    target_id: null,
    parameters: { stock_id: stockTarget.value, price: stockPrice.value },
    reason: '玩家干预',
  });
}

function teleportLocations(): WorldLocation[] {
  return store.locations;
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

    <!-- 总览 tab: identity card + needs + inventory + current action + decisions -->
    <div v-if="activeTab === 'overview'" class="agent-body">
      <div v-if="store.agentDetail" class="ov-card">
        <div class="ov-identity-head">
          <span class="ov-name">{{ store.agentDetail.identity.name }}</span>
          <span class="ov-meta">{{ store.agentDetail.identity.age }} 岁 · {{ store.agentDetail.identity.occupation }}</span>
        </div>
        <p class="ov-background">{{ store.agentDetail.identity.background }}</p>
        <div class="ov-section-title">价值观</div>
        <div class="ov-chips">
          <span v-for="v in store.agentDetail.identity.values" :key="v" class="ov-chip">{{ v }}</span>
        </div>
        <div class="ov-section-title">长期目标</div>
        <ul class="ov-goals">
          <li v-for="g in store.agentDetail.identity.long_term_goals" :key="g">{{ g }}</li>
        </ul>
        <div class="ov-section-title">说话风格</div>
        <p class="ov-speaking">{{ store.agentDetail.identity.speaking_style }}</p>
      </div>
      <p v-else class="agent-empty">身份信息加载中…</p>

      <div v-if="liveAgent" class="ov-card">
        <div class="ov-section-title">状态</div>
        <div class="need-row">
          <span class="need-label">饥饿</span>
          <span class="need-bar"><i class="need-fill hunger" :style="{ width: barWidth(liveAgent.hunger) }" /></span>
          <span class="need-value">{{ liveAgent.hunger }}</span>
        </div>
        <div class="need-row">
          <span class="need-label">精力</span>
          <span class="need-bar"><i class="need-fill energy" :style="{ width: barWidth(liveAgent.energy) }" /></span>
          <span class="need-value">{{ liveAgent.energy }}</span>
        </div>
        <div class="need-row">
          <span class="need-label">金钱</span>
          <span class="need-bar"><i class="need-fill money" :style="{ width: barWidth(liveAgent.money) }" /></span>
          <span class="need-value">{{ liveAgent.money }}</span>
        </div>
        <div class="need-row">
          <span class="need-label">心情</span>
          <span class="need-bar"><i class="need-fill mood" :style="{ width: barWidth(liveAgent.mood ?? 100) }" /></span>
          <span class="need-value">{{ liveAgent.mood ?? 100 }}</span>
        </div>
        <div class="ov-section-title">物品</div>
        <p class="ov-inventory">{{ inventoryText(liveAgent.inventory) }}</p>
        <div class="ov-section-title">当前行为</div>
        <p class="ov-action">{{ actionText(liveAgent.action) || '空闲' }}</p>
      </div>

      <div class="ov-card">
        <div class="ov-section-title">最近决策</div>
        <p v-if="store.recentDecisions.length === 0" class="agent-empty">暂无决策记录</p>
        <div v-for="d in store.recentDecisions.slice(0, 10)" :key="d.run_id" class="dec-item">
          <span class="dec-time">{{ timeOf(d.world_time) }}</span>
          <span class="dec-tool">{{ toolLabel(d.tool_name) }}</span>
          <span class="dec-ok" :class="decisionSuccess(d) ? 'ok' : 'fail'">{{ decisionSuccess(d) ? '✓' : '✗' }}</span>
          <span class="dec-reason">{{ decisionReason(d) }}</span>
          <span class="dec-model">{{ d.model }}</span>
        </div>
      </div>
    </div>

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

    <!-- 上帝 tab: interventions for the selected agent + world-level commands -->
    <div v-if="activeTab === 'god'" class="agent-body">
      <div class="ov-card">
        <div class="ov-section-title">干预 {{ selectedAgent?.name ?? '' }}</div>
        <div class="god-btns">
          <button class="god-btn" :disabled="godPending" @click="grantMoney">发放 50 金币</button>
          <button class="god-btn" :disabled="godPending" @click="deductMoney">扣除 50 金币</button>
          <button class="god-btn" :disabled="godPending" @click="spawnBread">生成面包×1</button>
        </div>
        <div class="god-row">
          <select v-model="teleportTarget" class="god-select" :disabled="godPending">
            <option v-for="loc in teleportLocations()" :key="loc.location_id" :value="loc.location_id">
              {{ loc.name }}
            </option>
          </select>
          <button class="god-btn" :disabled="godPending || !defaultTeleportTarget()" @click="teleportAgent">传送</button>
        </div>
      </div>

      <div class="ov-card">
        <div class="ov-section-title">世界</div>
        <div class="god-row">
          <input
            v-model="publicEventText"
            class="god-input"
            placeholder="公共事件内容…"
            :disabled="godPending"
            @keyup.enter="sendPublicEvent"
          />
          <button class="god-btn" :disabled="godPending || !publicEventText.trim()" @click="sendPublicEvent">发送</button>
        </div>
        <div class="god-row">
          <select v-model="stockItem" class="god-select" :disabled="godPending">
            <option v-for="it in STOCK_ITEMS" :key="it.value" :value="it.value">{{ it.label }}</option>
          </select>
          <input v-model.number="stockQty" class="god-qty" type="number" min="1" :disabled="godPending" />
          <button class="god-btn" :disabled="godPending" @click="setStoreStock">设置</button>
        </div>
        <div class="god-row">
          <select v-model="stockTarget" class="god-select" :disabled="godPending">
            <option v-for="s in store.stocks" :key="s.stock_id" :value="s.stock_id">{{ s.name }}</option>
          </select>
          <input v-model.number="stockPrice" class="god-qty" type="number" min="1" :disabled="godPending" />
          <button class="god-btn" :disabled="godPending || !stockTarget" @click="setStockPrice">调价</button>
        </div>
      </div>

      <p v-if="godResult" class="god-result" :class="godResult.success ? 'ok' : 'fail'">
        {{ godResult.success ? '✓ ' : '✗ ' }}{{ godResult.message }}
      </p>
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

/* 总览 tab */
.ov-card {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.04);
  padding: 8px 10px;
}
.ov-identity-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ov-name {
  font-weight: 600;
  font-size: 13px;
  color: #ffe082;
}
.ov-meta {
  color: rgba(205, 232, 213, 0.65);
  font-size: 11px;
}
.ov-background {
  margin: 6px 0 0;
  color: rgba(255, 255, 255, 0.82);
  line-height: 1.5;
  word-break: break-word;
}
.ov-section-title {
  margin-top: 8px;
  font-size: 11px;
  color: rgba(205, 232, 213, 0.6);
  font-weight: 600;
}
.ov-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.ov-chip {
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(100, 181, 246, 0.14);
  border: 1px solid rgba(100, 181, 246, 0.5);
  color: #bbdefb;
  font-size: 11px;
}
.ov-goals {
  margin: 4px 0 0;
  padding-left: 16px;
  color: rgba(255, 255, 255, 0.82);
  line-height: 1.5;
}
.ov-speaking {
  margin: 4px 0 0;
  color: rgba(255, 255, 255, 0.82);
  line-height: 1.5;
  font-style: italic;
}
.need-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
}
.need-row:first-of-type {
  margin-top: 2px;
}
.need-label {
  width: 32px;
  flex-shrink: 0;
  color: rgba(205, 232, 213, 0.7);
  font-size: 11px;
}
.need-bar {
  flex: 1;
  height: 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  overflow: hidden;
}
.need-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
}
.need-fill.hunger {
  background: #ffb74d;
}
.need-fill.energy {
  background: #81c784;
}
.need-fill.money {
  background: #ffd54f;
}
.need-fill.mood {
  background: #ba68c8;
}
.need-value {
  width: 28px;
  flex-shrink: 0;
  text-align: right;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.85);
}
.ov-inventory,
.ov-action {
  margin: 4px 0 0;
  color: rgba(255, 255, 255, 0.85);
  line-height: 1.5;
  word-break: break-word;
}
.dec-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 6px;
  line-height: 1.4;
}
.dec-item:first-of-type {
  margin-top: 2px;
}
.dec-time {
  flex-shrink: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: rgba(205, 232, 213, 0.6);
}
.dec-tool {
  flex-shrink: 0;
  padding: 0 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  font-size: 11px;
}
.dec-ok {
  flex-shrink: 0;
  font-weight: 700;
}
.dec-ok.ok {
  color: #81c784;
}
.dec-ok.fail {
  color: #e57373;
}
.dec-reason {
  flex: 1;
  color: rgba(255, 255, 255, 0.8);
  word-break: break-word;
  font-size: 11px;
}
.dec-model {
  flex-shrink: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
  color: rgba(205, 232, 213, 0.5);
}

/* 上帝 tab */
.god-btns {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 4px;
}
.god-btn {
  border: 1px solid rgba(255, 224, 130, 0.35);
  background: rgba(255, 224, 130, 0.1);
  color: #ffe082;
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
}
.god-btn:hover:not(:disabled) {
  background: rgba(255, 224, 130, 0.22);
}
.god-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.god-row {
  display: flex;
  gap: 6px;
  margin-top: 7px;
  align-items: center;
}
.god-select,
.god-qty {
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
  color: #cde8d5;
  font-size: 11px;
  padding: 3px 6px;
  border-radius: 6px;
  outline: none;
}
.god-select {
  flex: 1;
  min-width: 0;
}
.god-select option {
  background: #0d1a12;
  color: #cde8d5;
}
.god-qty {
  width: 52px;
  flex-shrink: 0;
}
.god-input {
  flex: 1;
  min-width: 0;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
  color: #cde8d5;
  font-size: 11px;
  padding: 3px 6px;
  border-radius: 6px;
  outline: none;
}
.god-input::placeholder {
  color: rgba(205, 232, 213, 0.45);
}
.god-result {
  margin: 0;
  font-size: 11px;
  padding: 5px 8px;
  border-radius: 6px;
  word-break: break-word;
}
.god-result.ok {
  background: rgba(129, 199, 132, 0.12);
  border: 1px solid rgba(129, 199, 132, 0.4);
  color: #a5d6a7;
}
.god-result.fail {
  background: rgba(229, 115, 115, 0.12);
  border: 1px solid rgba(229, 115, 115, 0.4);
  color: #ef9a9a;
}
</style>
