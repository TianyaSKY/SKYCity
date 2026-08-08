<script lang="ts" setup>
import {computed, onBeforeUnmount, onMounted, ref} from 'vue';
import {getEventStats, getLlmStats, getSnapshot} from '../api/client';
import {taskLabelOf, useWorldStore} from '../stores/worldStore';
import type {AgentSnapshot, EventStatsResponse, LlmStatsResponse} from '../types/world';

const STATS_POLL_MS = 10_000;

const store = useWorldStore();
const emit = defineEmits<{ (e: 'close'): void }>();

const llmStats = ref<LlmStatsResponse | null>(null);
const eventStats = ref<EventStatsResponse | null>(null);
const statsError = ref<string | null>(null);
let timer = 0;

async function refreshStats(): Promise<void> {
    if (!store.worldId) return;
    try {
        const [snap, llm, events] = await Promise.all([
            getSnapshot(store.worldId),
            getLlmStats(store.worldId),
            getEventStats(store.worldId),
        ]);
        store.applySnapshot(snap); // 复用 WS 快照动作：居民/经济区块因此保持实时
        llmStats.value = llm;
        eventStats.value = events;
        statsError.value = null;
    } catch {
        statsError.value = '统计数据获取失败';
    }
}

onMounted(() => {
    void refreshStats();
    timer = window.setInterval(() => void refreshStats(), STATS_POLL_MS);
});
onBeforeUnmount(() => window.clearInterval(timer));

function inConversation(agentId: string): boolean {
    return Object.values(store.activeConversations).some((c) => c.agent_ids.includes(agentId));
}

function fmt(n: number): string {
    return n.toLocaleString('zh-CN');
}

// --------------------------------------------------------------------------- #
// 居民统计（全部 store 驱动）
// --------------------------------------------------------------------------- #

const population = computed(() => store.agents.length);
const busyCount = computed(() =>
    store.agents.filter(
        (a) => taskLabelOf(a.action, store.locations, inConversation(a.agent_id)) !== '空闲',
    ).length,
);
const idleCount = computed(() => population.value - busyCount.value);
const moneyTotal = computed(() => store.agents.reduce((sum, a) => sum + a.money, 0));
const moneyAvg = computed(() =>
    population.value === 0 ? 0 : Math.round(moneyTotal.value / population.value),
);
const richest = computed<{name: string; money: number} | null>(() =>
    population.value === 0
        ? null
        : store.agents.reduce(
              (best, a) => (a.money > best.money ? a : best),
              store.agents[0],
          ),
);
const poorest = computed<{name: string; money: number} | null>(() =>
    population.value === 0
        ? null
        : store.agents.reduce(
              (worst, a) => (a.money < worst.money ? a : worst),
              store.agents[0],
          ),
);
const needsAvg = computed<{satiety: number; energy: number; mood: number; loneliness: number} | null>(() => {
    if (population.value === 0) return null;
    const mean = (pick: (a: AgentSnapshot) => number): number =>
        Math.round((store.agents.reduce((sum, a) => sum + pick(a), 0) / population.value) * 10) / 10;
    return {
        satiety: mean((a) => a.satiety),
        energy: mean((a) => a.energy),
        mood: mean((a) => a.mood),
        loneliness: mean((a) => a.loneliness),
    };
});

// --------------------------------------------------------------------------- #
// 经济运行（store 驱动）
// --------------------------------------------------------------------------- #

const companyMoneyTotal = computed(() => store.companies.reduce((sum, c) => sum + c.money, 0));
const totalMoney = computed(() => moneyTotal.value + companyMoneyTotal.value);
const stockCount = computed(() => store.stocks.length);
const avgStockPrice = computed(() =>
    stockCount.value === 0
        ? 0
        : Math.round(store.stocks.reduce((sum, s) => sum + s.price, 0) / stockCount.value),
);
const dayBusinessTotal = computed(() =>
    store.stocks.reduce((sum, s) => sum + s.day_business, 0),
);

// --------------------------------------------------------------------------- #
// 事件统计
// --------------------------------------------------------------------------- #

const maxEventCount = computed(() =>
    eventStats.value ? Math.max(0, ...eventStats.value.by_type.map((r) => r.count)) : 0,
);
</script>

<template>
    <div class="dashboard">
        <header class="dash-header">
            <h1>数据看板</h1>
            <div class="dash-meta">
                <span v-if="store.worldId" class="dash-world">{{ store.worldId }}</span>
                <span>{{ store.timeLabel }}</span>
            </div>
            <div class="dash-actions">
                <button class="dash-btn" @click="refreshStats()">刷新</button>
                <button class="dash-btn primary" @click="emit('close')">返回</button>
            </div>
        </header>
        <p v-if="statsError" class="dash-error">{{ statsError }}</p>

        <main class="dash-grid">
            <!-- 居民统计 -->
            <section class="dash-card">
                <h2>居民统计</h2>
                <div class="num-row">
                    <div class="num-card"><span class="num-label">人口</span><span class="num">{{ population }}</span></div>
                    <div class="num-card"><span class="num-label">忙碌</span><span class="num">{{ busyCount }}</span></div>
                    <div class="num-card"><span class="num-label">空闲</span><span class="num">{{ idleCount }}</span></div>
                </div>
                <dl class="kv">
                    <div class="kv-row"><dt>居民资金</dt><dd>{{ fmt(moneyTotal) }}</dd></div>
                    <div class="kv-row"><dt>人均资金</dt><dd>{{ fmt(moneyAvg) }}</dd></div>
                    <div class="kv-row">
                        <dt>最富</dt>
                        <dd>{{ richest ? `${richest.name} · ${fmt(richest.money)}` : '—' }}</dd>
                    </div>
                    <div class="kv-row">
                        <dt>最穷</dt>
                        <dd>{{ poorest ? `${poorest.name} · ${fmt(poorest.money)}` : '—' }}</dd>
                    </div>
                    <div class="kv-row">
                        <dt>平均需求</dt>
                        <dd v-if="needsAvg">
                            饱食 {{ needsAvg.satiety.toFixed(1) }} / 精力 {{ needsAvg.energy.toFixed(1) }} /
                            心情 {{ needsAvg.mood.toFixed(1) }} / 孤独 {{ needsAvg.loneliness.toFixed(1) }}
                        </dd>
                        <dd v-else>—</dd>
                    </div>
                </dl>
                <h3 class="sub">就业</h3>
                <dl class="kv">
                    <div class="kv-row"><dt>在岗 / 待业</dt><dd>{{ store.employedCount }} / {{ store.unemployedCount }}</dd></div>
                    <div class="kv-row"><dt>开放职位</dt><dd>{{ store.openPositionCount }}</dd></div>
                    <div class="kv-row">
                        <dt>今日考勤</dt>
                        <dd>到岗 {{ store.todayShiftStats.attended }} / 迟到 {{ store.todayShiftStats.late }} / 缺勤 {{ store.todayShiftStats.absent }}</dd>
                    </div>
                    <div class="kv-row"><dt>未发工资</dt><dd>{{ fmt(store.unpaidWageTotal) }}</dd></div>
                </dl>
            </section>

            <!-- 经济运行 -->
            <section class="dash-card">
                <h2>经济运行</h2>
                <dl class="kv">
                    <div class="kv-row"><dt>居民资金</dt><dd>{{ fmt(moneyTotal) }}</dd></div>
                    <div class="kv-row"><dt>企业资金</dt><dd>{{ fmt(companyMoneyTotal) }}</dd></div>
                    <div class="kv-row"><dt>资金合计</dt><dd>{{ fmt(totalMoney) }}</dd></div>
                    <div class="kv-row"><dt>企业数</dt><dd>{{ store.companies.length }}</dd></div>
                    <div class="kv-row"><dt>员工 / 开放职位</dt><dd>{{ store.employedCount }} / {{ store.openPositionCount }}</dd></div>
                </dl>
                <h3 class="sub">股票</h3>
                <dl class="kv">
                    <div class="kv-row"><dt>股票数</dt><dd>{{ stockCount }}</dd></div>
                    <div class="kv-row"><dt>平均股价</dt><dd>{{ fmt(avgStockPrice) }}</dd></div>
                    <div class="kv-row"><dt>今日营业合计</dt><dd>{{ fmt(dayBusinessTotal) }}</dd></div>
                </dl>
                <h3 class="sub">世界</h3>
                <dl class="kv">
                    <div class="kv-row"><dt>商店</dt><dd>{{ store.stores.length }}</dd></div>
                    <div class="kv-row"><dt>作物</dt><dd>{{ store.crops.length }}</dd></div>
                    <div class="kv-row"><dt>结构物</dt><dd>{{ store.structures.length }}</dd></div>
                </dl>
            </section>

            <!-- LLM 用量 -->
            <section class="dash-card">
                <h2>LLM 用量</h2>
                <template v-if="llmStats">
                    <div class="num-row">
                        <div class="num-card"><span class="num-label">决策次数</span><span class="num">{{ fmt(llmStats.total_calls) }}</span></div>
                        <div class="num-card"><span class="num-label">输入 Token</span><span class="num">{{ fmt(llmStats.total_input_tokens) }}</span></div>
                        <div class="num-card"><span class="num-label">输出 Token</span><span class="num">{{ fmt(llmStats.total_output_tokens) }}</span></div>
                        <div class="num-card"><span class="num-label">错误率</span><span class="num">{{ (llmStats.error_rate * 100).toFixed(1) }}%</span></div>
                        <div class="num-card"><span class="num-label">平均延迟</span><span class="num">{{ fmt(llmStats.avg_latency_ms) }} ms</span></div>
                    </div>
                    <h3 class="sub">按智能体</h3>
                    <table v-if="llmStats.by_agent.length > 0" class="dash-table">
                        <thead>
                            <tr><th>智能体</th><th>次数</th><th>输入</th><th>输出</th><th>失败</th><th>平均延迟</th></tr>
                        </thead>
                        <tbody>
                            <tr v-for="row in llmStats.by_agent" :key="row.agent_id">
                                <td>{{ store.agentById(row.agent_id)?.name ?? row.agent_id }}</td>
                                <td>{{ fmt(row.calls) }}</td>
                                <td>{{ fmt(row.input_tokens) }}</td>
                                <td>{{ fmt(row.output_tokens) }}</td>
                                <td>{{ row.failed }}</td>
                                <td>{{ fmt(row.avg_latency_ms) }} ms</td>
                            </tr>
                        </tbody>
                    </table>
                    <p v-else class="empty">暂无记录</p>
                    <h3 class="sub">按模型</h3>
                    <table v-if="llmStats.by_model.length > 0" class="dash-table">
                        <thead>
                            <tr><th>模型</th><th>次数</th><th>输入</th><th>输出</th></tr>
                        </thead>
                        <tbody>
                            <tr v-for="row in llmStats.by_model" :key="row.model">
                                <td>{{ row.model }}</td>
                                <td>{{ fmt(row.calls) }}</td>
                                <td>{{ fmt(row.input_tokens) }}</td>
                                <td>{{ fmt(row.output_tokens) }}</td>
                            </tr>
                        </tbody>
                    </table>
                    <p v-else class="empty">暂无记录</p>
                </template>
                <p v-else class="empty">加载中…</p>
            </section>

            <!-- 事件统计 -->
            <section class="dash-card">
                <h2>事件统计</h2>
                <template v-if="eventStats">
                    <div class="num-row">
                        <div class="num-card"><span class="num-label">事件总数</span><span class="num">{{ fmt(eventStats.total) }}</span></div>
                        <div class="num-card"><span class="num-label">最新序号</span><span class="num">{{ fmt(eventStats.latest_sequence) }}</span></div>
                    </div>
                    <ul v-if="eventStats.by_type.length > 0" class="event-list">
                        <li v-for="row in eventStats.by_type" :key="row.type" class="event-row">
                            <div class="event-head">
                                <span class="event-type">{{ row.type }}</span>
                                <span class="event-count">{{ row.count }} · {{ (row.count / eventStats.total * 100).toFixed(1) }}%</span>
                            </div>
                            <div class="event-bar-track">
                                <div
                                    class="event-bar"
                                    :style="{width: `${row.count / maxEventCount * 100}%`}"
                                />
                            </div>
                        </li>
                    </ul>
                    <p v-else class="empty">暂无事件</p>
                </template>
                <p v-else class="empty">加载中…</p>
            </section>
        </main>
    </div>
</template>

<style scoped>
.dashboard {
    position: fixed;
    inset: 0;
    background: #07150e;
    color: #cde8d5;
    overflow-y: auto;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

.dash-header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 14px 16px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    position: sticky;
    top: 0;
    background: #07150e;
    z-index: 10;
}

.dash-header h1 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
}

.dash-meta {
    display: flex;
    gap: 10px;
    font-size: 13px;
    opacity: 0.8;
}

.dash-meta .dash-world {
    font-family: 'SF Mono', Menlo, Consolas, monospace;
}

.dash-actions {
    margin-left: auto;
    display: flex;
    gap: 8px;
}

.dash-btn {
    padding: 7px 16px;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 10px;
    background: rgba(20, 40, 30, 0.8);
    color: #cde8d5;
    font-size: 13px;
    cursor: pointer;
}

.dash-btn:hover { background: rgba(30, 60, 45, 0.9); }
.dash-btn.primary { background: rgba(46, 92, 68, 0.9); }

.dash-error {
    margin: 10px 16px 0;
    color: #ff9b9b;
    font-size: 13px;
}

.dash-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 12px;
    padding: 12px 16px 24px;
}

.dash-card {
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 14px 16px;
}

.dash-card h2 {
    margin: 0 0 12px;
    font-size: 15px;
    font-weight: 600;
}

.dash-card .sub {
    margin: 14px 0 6px;
    font-size: 12px;
    font-weight: 600;
    opacity: 0.7;
}

.num-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
}

.num-card {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 8px 12px;
    min-width: 88px;
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.num-card .num-label {
    font-size: 11px;
    opacity: 0.7;
}

.num-card .num {
    font-family: 'SF Mono', Menlo, Consolas, monospace;
    font-size: 18px;
    color: #e8f5ec;
}

.kv {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.kv-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 13px;
}

.kv-row dt {
    opacity: 0.75;
}

.kv-row dd {
    margin: 0;
    font-family: 'SF Mono', Menlo, Consolas, monospace;
    text-align: right;
}

.dash-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}

.dash-table th,
.dash-table td {
    text-align: right;
    padding: 5px 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.dash-table th:first-child,
.dash-table td:first-child {
    text-align: left;
}

.dash-table th {
    opacity: 0.7;
    font-weight: 500;
}

.dash-table td {
    font-family: 'SF Mono', Menlo, Consolas, monospace;
}

.event-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 320px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.event-head {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    margin-bottom: 3px;
}

.event-type {
    font-family: 'SF Mono', Menlo, Consolas, monospace;
}

.event-count {
    opacity: 0.75;
    font-family: 'SF Mono', Menlo, Consolas, monospace;
}

.event-bar-track {
    height: 6px;
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.06);
    overflow: hidden;
}

.event-bar {
    height: 100%;
    border-radius: 3px;
    background: linear-gradient(90deg, #3f8f5f, #6fc48a);
}

.empty {
    font-size: 13px;
    opacity: 0.6;
    margin: 8px 0;
}
</style>
