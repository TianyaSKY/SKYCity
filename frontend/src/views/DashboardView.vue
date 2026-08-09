<script lang="ts" setup>
import {computed, onBeforeUnmount, onMounted, ref} from 'vue';
import {getDashboardOverview, getEventStats, getLlmStats, getSnapshot} from '../api/client';
import {
    actionRemainingMinutes,
    COMPANY_STATUS_LABELS,
    taskLabelOf,
    taskPriority,
    useWorldStore,
} from '../stores/worldStore';
import type {
    AgentSnapshot,
    DashboardOverviewResponse,
    EventStatsResponse,
    LlmStatsResponse,
} from '../types/world';

const STATS_POLL_MS = 10_000;

const ACTION_GROUPS = [
    {key: 'work', label: '工作'},
    {key: 'build', label: '建造'},
    {key: 'move', label: '移动'},
    {key: 'wait', label: '等待'},
    {key: 'idle', label: '空闲'},
] as const;

type ActionGroupKey = (typeof ACTION_GROUPS)[number]['key'];

const store = useWorldStore();
const emit = defineEmits<{ (e: 'close'): void }>();

const overviewStats = ref<DashboardOverviewResponse | null>(null);
const llmStats = ref<LlmStatsResponse | null>(null);
const eventStats = ref<EventStatsResponse | null>(null);
const snapshotWorldId = ref<string | null>(null);
const statsError = ref<string | null>(null);
let timer = 0;

const snapshotReady = computed(
    () => store.worldId !== null && snapshotWorldId.value === store.worldId,
);

async function refreshStats(): Promise<void> {
    const worldId = store.worldId;
    if (!worldId) return;

    const results = await Promise.allSettled([
        getSnapshot(worldId).then((snapshot) => {
            if (store.worldId !== worldId) return;
            store.applySnapshot(snapshot);
            snapshotWorldId.value = worldId;
        }),
        getDashboardOverview(worldId).then((overview) => {
            if (store.worldId === worldId) overviewStats.value = overview;
        }),
        getLlmStats(worldId).then((stats) => {
            if (store.worldId === worldId) llmStats.value = stats;
        }),
        getEventStats(worldId).then((stats) => {
            if (store.worldId === worldId) eventStats.value = stats;
        }),
    ]);

    if (store.worldId !== worldId) return;
    statsError.value = results.some((result) => result.status === 'rejected')
        ? '部分统计数据未刷新，已显示上次成功数据'
        : null;
}

onMounted(() => {
    void refreshStats();
    timer = window.setInterval(() => void refreshStats(), STATS_POLL_MS);
});
onBeforeUnmount(() => window.clearInterval(timer));

function inConversation(agentId: string): boolean {
    return Object.values(store.activeConversations).some((conversation) =>
        conversation.agent_ids.includes(agentId),
    );
}

function fmt(value: number): string {
    return value.toLocaleString('zh-CN');
}

function fmtMaybe(value: number | null): string {
    return value === null ? '—' : fmt(value);
}

function boundedPercent(value: number, maximum: number): number {
    if (maximum <= 0) return 0;
    return Math.min(100, Math.max(0, (value / maximum) * 100));
}

function actionGroupOf(agent: AgentSnapshot): ActionGroupKey {
    return agent.action?.type ?? 'idle';
}

const worldName = computed(() => {
    const worldId = store.worldId;
    if (!worldId) return '—';
    return store.worlds.find((world) => world.world_id === worldId)?.name ?? worldId;
});

const population = computed<number | null>(() =>
    snapshotReady.value ? store.agents.length : null,
);
const busyCount = computed<number | null>(() => {
    if (!snapshotReady.value) return null;
    return store.agents.filter(
        (agent) => taskLabelOf(agent.action, store.locations, inConversation(agent.agent_id)) !== '空闲',
    ).length;
});
const idleCount = computed<number | null>(() => {
    if (population.value === null || busyCount.value === null) return null;
    return population.value - busyCount.value;
});

const needsAvg = computed<{
    satiety: number;
    energy: number;
    mood: number;
    loneliness: number;
} | null>(() => {
    if (!snapshotReady.value || store.agents.length === 0) return null;
    const average = (pick: (agent: AgentSnapshot) => number): number =>
        Math.round((store.agents.reduce((sum, agent) => sum + pick(agent), 0) / store.agents.length) * 10) /
        10;
    return {
        satiety: average((agent) => agent.satiety),
        energy: average((agent) => agent.energy),
        mood: average((agent) => agent.mood),
        loneliness: average((agent) => agent.loneliness),
    };
});

const needTracks = computed(() => {
    const thresholds = overviewStats.value?.need_thresholds;
    const countAtRisk = (
        threshold: number | null,
        predicate: (agent: AgentSnapshot, value: number) => boolean,
    ): number | null => {
        if (!snapshotReady.value) return null;
        if (threshold === null) return store.agents.length === 0 ? 0 : null;
        return store.agents.filter((agent) => predicate(agent, threshold)).length;
    };
    const average = needsAvg.value;
    return [
        {
            key: 'satiety',
            label: '饱食',
            average: average?.satiety ?? null,
            threshold: thresholds?.satiety_lte ?? null,
            operator: '≤',
            atRisk: countAtRisk(thresholds?.satiety_lte ?? null, (agent, value) => agent.satiety <= value),
        },
        {
            key: 'energy',
            label: '精力',
            average: average?.energy ?? null,
            threshold: thresholds?.energy_lte ?? null,
            operator: '≤',
            atRisk: countAtRisk(thresholds?.energy_lte ?? null, (agent, value) => agent.energy <= value),
        },
        {
            key: 'mood',
            label: '心情',
            average: average?.mood ?? null,
            threshold: thresholds?.mood_lte ?? null,
            operator: '≤',
            atRisk: countAtRisk(thresholds?.mood_lte ?? null, (agent, value) => agent.mood <= value),
        },
        {
            key: 'loneliness',
            label: '孤独',
            average: average?.loneliness ?? null,
            threshold: thresholds?.loneliness_gte ?? null,
            operator: '≥',
            atRisk: countAtRisk(
                thresholds?.loneliness_gte ?? null,
                (agent, value) => agent.loneliness >= value,
            ),
        },
    ].map((track) => ({
        ...track,
        progress: boundedPercent(track.average ?? 0, 100),
    }));
});

const actionGroups = computed(() => {
    if (!snapshotReady.value) return [];
    return ACTION_GROUPS.map((group) => ({
        ...group,
        count: store.agents.filter((agent) => actionGroupOf(agent) === group.key).length,
    }));
});

const residentRows = computed(() => {
    if (!snapshotReady.value) return [];
    return store.agents
        .map((agent) => {
            const conversation = inConversation(agent.agent_id);
            return {
                agent,
                actionLabel: taskLabelOf(agent.action, store.locations, conversation),
                remainingMinutes: actionRemainingMinutes(agent.action, store.worldTime),
                priority: taskPriority(agent.action, conversation),
            };
        })
        .sort(
            (left, right) =>
                left.priority - right.priority || left.agent.name.localeCompare(right.agent.name, 'zh-CN'),
        );
});

const residentMoney = computed<number | null>(() =>
    snapshotReady.value ? store.agents.reduce((sum, agent) => sum + agent.money, 0) : null,
);
const cooperativeMoney = computed<number | null>(() =>
    snapshotReady.value ? store.companies.reduce((sum, company) => sum + company.money, 0) : null,
);
const richest = computed<AgentSnapshot | null>(() => {
    if (!snapshotReady.value || store.agents.length === 0) return null;
    return store.agents.reduce((best, agent) => (agent.money > best.money ? agent : best), store.agents[0]);
});
const poorest = computed<AgentSnapshot | null>(() => {
    if (!snapshotReady.value || store.agents.length === 0) return null;
    return store.agents.reduce((worst, agent) => (agent.money < worst.money ? agent : worst), store.agents[0]);
});
const debtorCount = computed<number | null>(() =>
    snapshotReady.value ? store.agents.filter((agent) => agent.money < 0).length : null,
);

const financialSummary = computed(() => {
    const treasury = overviewStats.value?.treasury.balance;
    if (residentMoney.value === null || cooperativeMoney.value === null || treasury === undefined) return null;
    return {
        residents: residentMoney.value,
        cooperatives: cooperativeMoney.value,
        treasury,
        total: residentMoney.value + cooperativeMoney.value + treasury,
    };
});

const employmentSummary = computed(() => {
    if (!snapshotReady.value) return null;
    return {
        employed: store.employedCount,
        unemployed: store.unemployedCount,
        openPositions: store.openPositionCount,
        unpaidWages: store.unpaidWageTotal,
    };
});
const attendanceToday = computed(() => overviewStats.value?.attendance_today ?? null);

const supplySummary = computed(() => {
    if (!snapshotReady.value) return null;
    const products = store.stores.flatMap((shop) => shop.products);
    const stock = products.reduce((sum, product) => sum + product.stock, 0);
    const capacity = products.reduce((sum, product) => sum + product.stock_cap, 0);
    return {
        shops: store.stores.length,
        cooperativeShops: store.stores.filter((shop) => shop.owner_agent_id === null).length,
        residentStalls: store.stores.filter((shop) => shop.owner_agent_id !== null).length,
        skus: products.length,
        stock,
        capacity,
        saturation: boundedPercent(stock, capacity),
    };
});

const productionSummary = computed(() => {
    if (!snapshotReady.value) return null;
    return {
        built: store.structures.filter((structure) => structure.status === 'built').length,
        building: store.structures.filter((structure) => structure.status === 'building').length,
        growing: store.crops.filter((crop) => crop.next_stage_at !== null).length,
        harvestable: store.crops.filter((crop) => crop.next_stage_at === null).length,
    };
});

const shareSummary = computed(() => {
    if (!snapshotReady.value) return null;
    const shareCount = store.stocks.length;
    return {
        shareCount,
        averageUnitPrice:
            shareCount === 0
                ? null
                : Math.round(
                      store.stocks.reduce((sum, share) => sum + share.unit_price, 0) / shareCount,
                  ),
        operatingVolume: store.stocks.reduce((sum, share) => sum + share.operating_volume, 0),
    };
});

const llmToday = computed(() => overviewStats.value?.llm_today ?? null);
const llmBudgetProgress = computed<number | null>(() => {
    const today = llmToday.value;
    if (!today || today.token_budget === null) return null;
    return boundedPercent(today.token_usage, today.token_budget);
});

const eventRows = computed(() => {
    const stats = eventStats.value;
    if (!stats || stats.total === 0 || stats.by_type.length === 0) return [];
    const maximum = Math.max(...stats.by_type.map((row) => row.count));
    if (maximum <= 0) return [];
    return stats.by_type.map((row) => ({
        ...row,
        percent: (row.count / stats.total) * 100,
        width: boundedPercent(row.count, maximum),
    }));
});
</script>

<template>
    <div class="dashboard">
        <header class="dash-header">
            <div class="dash-title">
                <h1>数据看板</h1>
                <p class="dash-kicker">村庄实时运营</p>
            </div>
            <div class="dash-actions">
                <button class="dash-btn" @click="refreshStats()">刷新</button>
                <button class="dash-btn primary" @click="emit('close')">返回</button>
            </div>
        </header>

        <main v-if="store.worldId" class="dashboard-content">
            <div class="status-band" aria-label="当前世界状态">
                <div class="status-item">
                    <span class="status-label">世界</span>
                    <strong>{{ worldName }}</strong>
                </div>
                <div class="status-item">
                    <span class="status-label">ID</span>
                    <strong class="data-value">{{ store.worldId }}</strong>
                </div>
                <div class="status-item">
                    <span class="status-label">游戏时间</span>
                    <strong class="data-value">{{ store.timeLabel }}</strong>
                </div>
                <div class="status-item">
                    <span class="status-label">天气</span>
                    <strong>{{ store.weatherLabel }}</strong>
                </div>
                <div class="status-item">
                    <span class="status-label">状态</span>
                    <strong :class="store.paused ? 'state-paused' : 'state-running'">
                        {{ store.paused ? '已暂停' : '运行中' }}
                    </strong>
                </div>
                <div class="status-item">
                    <span class="status-label">速度</span>
                    <strong class="data-value">×{{ store.speed }}</strong>
                </div>
            </div>

            <p v-if="statsError" class="dash-error" role="status">{{ statsError }}</p>

            <div class="pulse-grid" aria-label="关键脉搏">
                <article class="pulse-card">
                    <p class="pulse-label">人口</p>
                    <template v-if="population !== null">
                        <strong class="pulse-value">{{ fmt(population) }}</strong>
                        <span class="pulse-detail">忙碌 {{ fmt(busyCount ?? 0) }} / 空闲 {{ fmt(idleCount ?? 0) }}</span>
                    </template>
                    <span v-else class="pulse-unavailable">实时数据未加载</span>
                </article>
                <article class="pulse-card">
                    <p class="pulse-label">劳动</p>
                    <template v-if="employmentSummary">
                        <strong class="pulse-value">{{ fmt(employmentSummary.employed) }} / {{ fmt(employmentSummary.openPositions) }}</strong>
                        <span class="pulse-detail">在岗 / 空缺</span>
                    </template>
                    <span v-else class="pulse-unavailable">实时数据未加载</span>
                </article>
                <article class="pulse-card finance-pulse">
                    <p class="pulse-label">资金</p>
                    <template v-if="financialSummary">
                        <strong class="pulse-value">{{ fmt(financialSummary.total) }}</strong>
                        <span class="pulse-detail">居民 {{ fmt(financialSummary.residents) }} · 合作社 {{ fmt(financialSummary.cooperatives) }} · 金库 {{ fmt(financialSummary.treasury) }}</span>
                    </template>
                    <span v-else class="pulse-unavailable">实时数据未加载</span>
                </article>
                <article class="pulse-card">
                    <p class="pulse-label">供给</p>
                    <template v-if="supplySummary">
                        <strong class="pulse-value">{{ fmt(supplySummary.shops) }} 家</strong>
                        <span class="pulse-detail">库存饱和度 {{ supplySummary.saturation.toFixed(1) }}%</span>
                    </template>
                    <span v-else class="pulse-unavailable">实时数据未加载</span>
                </article>
                <article class="pulse-card ai-pulse">
                    <p class="pulse-label">AI 今日</p>
                    <template v-if="llmToday">
                        <strong class="pulse-value">{{ fmt(llmToday.calls) }} 次</strong>
                        <span class="pulse-detail">
                            {{ fmt(llmToday.token_usage) }} Token / {{ llmToday.token_budget === null ? '不限额' : fmt(llmToday.token_budget) }}
                        </span>
                    </template>
                    <span v-else class="pulse-unavailable">实时数据未加载</span>
                </article>
                <article class="pulse-card event-pulse">
                    <p class="pulse-label">事件</p>
                    <template v-if="eventStats">
                        <strong class="pulse-value">{{ fmt(eventStats.total) }}</strong>
                        <span class="pulse-detail">最新序号 #{{ fmt(eventStats.latest_sequence) }}</span>
                    </template>
                    <span v-else class="pulse-unavailable">实时数据未加载</span>
                </article>
            </div>

            <div class="dash-grid" aria-label="村庄运营详情">
                <section class="dash-section residents-section" aria-labelledby="residents-title">
                    <div class="section-heading">
                        <p class="section-kicker">民生监测</p>
                        <h2 id="residents-title">居民状态</h2>
                    </div>
                    <template v-if="snapshotReady">
                        <div class="need-tracks" aria-label="居民需求轨道">
                            <div v-for="track in needTracks" :key="track.key" class="need-track">
                                <div class="track-heading">
                                    <span>{{ track.label }}</span>
                                    <strong class="data-value">{{ track.average === null ? '—' : track.average.toFixed(1) }}</strong>
                                </div>
                                <div class="track-note">
                                    <span v-if="track.threshold !== null" class="threshold-note">
                                        阈值 {{ track.operator }} {{ fmt(track.threshold) }} · {{ track.atRisk ?? 0 }} 人
                                    </span>
                                    <span v-else-if="track.atRisk !== null" class="threshold-note">
                                        风险人数 {{ track.atRisk }} 人 · 实时阈值未加载
                                    </span>
                                    <span v-else>实时数据未加载</span>
                                </div>
                                <div
                                    class="meter-track"
                                    role="progressbar"
                                    :aria-label="`${track.label}群体平均值`"
                                    :aria-valuenow="track.average ?? 0"
                                    aria-valuemin="0"
                                    aria-valuemax="100"
                                    :aria-valuetext="track.average === null ? `${track.label}暂无居民数据` : `${track.label} ${track.average.toFixed(1)}`"
                                >
                                    <span
                                        class="meter-fill"
                                        :class="{alert: (track.atRisk ?? 0) > 0}"
                                        :style="{width: `${track.progress}%` }"
                                    />
                                </div>
                            </div>
                        </div>

                        <div class="resident-summary">
                            <div>
                                <h3>行动分布</h3>
                                <div class="action-grid">
                                    <div v-for="group in actionGroups" :key="group.key" class="action-count">
                                        <span>{{ group.label }}</span>
                                        <strong class="data-value">{{ fmt(group.count) }}</strong>
                                    </div>
                                </div>
                            </div>
                            <dl class="ledger-kv resident-assets">
                                <div>
                                    <dt>最富</dt>
                                    <dd>{{ richest ? `${richest.name} · ${fmt(richest.money)}` : '—' }}</dd>
                                </div>
                                <div>
                                    <dt>最穷</dt>
                                    <dd>{{ poorest ? `${poorest.name} · ${fmt(poorest.money)}` : '—' }}</dd>
                                </div>
                                <div>
                                    <dt>负债居民</dt>
                                    <dd>{{ fmtMaybe(debtorCount) }}</dd>
                                </div>
                            </dl>
                        </div>

                        <h3 class="table-title">居民行动</h3>
                        <div v-if="residentRows.length > 0" class="table-scroll resident-table-scroll">
                            <table class="dash-table">
                                <thead>
                                    <tr>
                                        <th scope="col">居民</th>
                                        <th scope="col">当前行动</th>
                                        <th scope="col">剩余游戏分钟</th>
                                        <th scope="col">资金</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="row in residentRows" :key="row.agent.agent_id">
                                        <td>{{ row.agent.name }}</td>
                                        <td>{{ row.actionLabel }}</td>
                                        <td>{{ row.remainingMinutes === null ? '—' : fmt(row.remainingMinutes) }}</td>
                                        <td>{{ fmt(row.agent.money) }}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                        <p v-else class="empty">暂无居民</p>
                    </template>
                    <p v-else class="empty">实时数据未加载</p>
                </section>

                <section class="dash-section employment-section" aria-labelledby="employment-title">
                    <div class="section-heading">
                        <p class="section-kicker">劳动组织</p>
                        <h2 id="employment-title">就业与合作社</h2>
                    </div>
                    <template v-if="employmentSummary">
                        <div class="compact-kpis">
                            <div><span>在岗</span><strong>{{ fmt(employmentSummary.employed) }}</strong></div>
                            <div><span>待业</span><strong>{{ fmt(employmentSummary.unemployed) }}</strong></div>
                            <div><span>空缺</span><strong>{{ fmt(employmentSummary.openPositions) }}</strong></div>
                            <div><span>未发工资</span><strong>{{ fmt(employmentSummary.unpaidWages) }}</strong></div>
                        </div>
                        <div class="attendance-ledger">
                            <h3>今日考勤</h3>
                            <template v-if="attendanceToday">
                                <span>到岗 <strong>{{ fmt(attendanceToday.attended) }}</strong></span>
                                <span>迟到 <strong class="threshold-value">{{ fmt(attendanceToday.late) }}</strong></span>
                                <span>缺勤 <strong class="threshold-value">{{ fmt(attendanceToday.absent) }}</strong></span>
                            </template>
                            <span v-else class="muted">实时数据未加载</span>
                        </div>
                        <h3 class="table-title">合作社</h3>
                        <div v-if="store.companies.length > 0" class="table-scroll company-table-scroll">
                            <table class="dash-table">
                                <thead>
                                    <tr>
                                        <th scope="col">合作社</th>
                                        <th scope="col">状态</th>
                                        <th scope="col">资金</th>
                                        <th scope="col">员工</th>
                                        <th scope="col">空缺</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="company in store.companies" :key="company.company_id">
                                        <td>{{ company.name }}</td>
                                        <td>{{ COMPANY_STATUS_LABELS[company.status] ?? company.status }}</td>
                                        <td>{{ fmt(company.money) }}</td>
                                        <td>{{ fmt(company.employee_count) }}</td>
                                        <td>{{ fmt(company.open_vacancies) }}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                        <p v-else class="empty">暂无合作社</p>
                    </template>
                    <p v-else class="empty">实时数据未加载</p>
                </section>

                <section class="dash-section commerce-section" aria-labelledby="commerce-title">
                    <div class="section-heading">
                        <p class="section-kicker">物资与产出</p>
                        <h2 id="commerce-title">商业、生产与份额</h2>
                    </div>
                    <template v-if="supplySummary && productionSummary && shareSummary">
                        <div class="supply-track">
                            <div class="track-heading">
                                <span>货架库存</span>
                                <strong class="data-value">{{ fmt(supplySummary.stock) }} / {{ fmt(supplySummary.capacity) }}</strong>
                            </div>
                            <div
                                class="meter-track stock-meter"
                                role="progressbar"
                                aria-label="货架库存饱和度"
                                :aria-valuenow="supplySummary.saturation"
                                aria-valuemin="0"
                                aria-valuemax="100"
                                :aria-valuetext="`库存 ${fmt(supplySummary.stock)} / ${fmt(supplySummary.capacity)}，饱和度 ${supplySummary.saturation.toFixed(1)}%`"
                            >
                                <span class="meter-fill blue-fill" :style="{width: `${supplySummary.saturation}%`}" />
                            </div>
                            <p class="meter-caption">库存饱和度 {{ supplySummary.saturation.toFixed(1) }}%</p>
                        </div>
                        <div class="three-column-ledger">
                            <div><span>合作社店铺</span><strong>{{ fmt(supplySummary.cooperativeShops) }}</strong></div>
                            <div><span>居民摊位</span><strong>{{ fmt(supplySummary.residentStalls) }}</strong></div>
                            <div><span>货架 SKU</span><strong>{{ fmt(supplySummary.skus) }}</strong></div>
                        </div>
                        <dl class="ledger-kv">
                            <div><dt>已建 / 建造中</dt><dd>{{ fmt(productionSummary.built) }} / {{ fmt(productionSummary.building) }}</dd></div>
                            <div><dt>成长中 / 可收获</dt><dd>{{ fmt(productionSummary.growing) }} / {{ fmt(productionSummary.harvestable) }}</dd></div>
                            <div><dt>份额种类</dt><dd>{{ fmt(shareSummary.shareCount) }}</dd></div>
                            <div><dt>平均认购单价</dt><dd>{{ fmtMaybe(shareSummary.averageUnitPrice) }}</dd></div>
                            <div><dt>当日经营合计</dt><dd>{{ fmt(shareSummary.operatingVolume) }}</dd></div>
                        </dl>
                    </template>
                    <p v-else class="empty">实时数据未加载</p>
                </section>

                <section class="dash-section finance-section" aria-labelledby="finance-title">
                    <div class="section-heading">
                        <p class="section-kicker">公共账本</p>
                        <h2 id="finance-title">村庄财政</h2>
                    </div>
                    <template v-if="overviewStats">
                        <div class="finance-ledger">
                            <div class="money-card">
                                <span>金库余额</span>
                                <strong>{{ fmt(overviewStats.treasury.balance) }}</strong>
                            </div>
                            <div class="money-card">
                                <span>公共工程剩余额</span>
                                <strong>{{ fmt(overviewStats.treasury.public_work_budget_remaining) }}</strong>
                            </div>
                            <div class="money-card">
                                <span>公共工程托管额</span>
                                <strong>{{ fmt(overviewStats.treasury.public_work_escrow) }}</strong>
                            </div>
                        </div>
                        <dl v-if="financialSummary" class="ledger-kv total-ledger">
                            <div><dt>居民资金</dt><dd>{{ fmt(financialSummary.residents) }}</dd></div>
                            <div><dt>合作社资金</dt><dd>{{ fmt(financialSummary.cooperatives) }}</dd></div>
                            <div><dt>村庄资金合计</dt><dd>{{ fmt(financialSummary.total) }}</dd></div>
                        </dl>
                        <p v-else class="empty">资金合计等待居民与合作社快照</p>
                    </template>
                    <p v-else class="empty">实时数据未加载</p>
                </section>

                <section class="dash-section ai-section" aria-labelledby="ai-title">
                    <div class="section-heading">
                        <p class="section-kicker">智能体调度</p>
                        <h2 id="ai-title">AI 调度与累计质量</h2>
                    </div>
                    <div class="daily-llm">
                        <h3>当日 LLM 调度</h3>
                        <template v-if="llmToday">
                            <div class="three-column-ledger">
                                <div><span>调用</span><strong>{{ fmt(llmToday.calls) }}</strong></div>
                                <div><span>Token</span><strong>{{ fmt(llmToday.token_usage) }}</strong></div>
                                <div><span>正在决策</span><strong>{{ fmt(llmToday.deciding_agents) }}</strong></div>
                            </div>
                            <template v-if="llmToday.token_budget !== null && llmBudgetProgress !== null">
                                <div class="track-heading budget-heading">
                                    <span>预算进度</span>
                                    <strong class="data-value">{{ fmt(llmToday.token_usage) }} / {{ fmt(llmToday.token_budget) }} Token</strong>
                                </div>
                                <div
                                    class="meter-track budget-meter"
                                    role="progressbar"
                                    aria-label="当日 LLM Token 预算进度"
                                    :aria-valuenow="llmBudgetProgress"
                                    aria-valuemin="0"
                                    aria-valuemax="100"
                                    :aria-valuetext="`已使用 ${fmt(llmToday.token_usage)} / ${fmt(llmToday.token_budget)} Token，剩余 ${fmtMaybe(llmToday.token_remaining)} Token`"
                                >
                                    <span class="meter-fill gold-fill" :style="{width: `${llmBudgetProgress}%`}" />
                                </div>
                                <p class="meter-caption">剩余 {{ fmtMaybe(llmToday.token_remaining) }} Token</p>
                            </template>
                            <p v-else class="unlimited-budget">不限额</p>
                        </template>
                        <p v-else class="empty">实时数据未加载</p>
                    </div>

                    <template v-if="llmStats">
                        <h3 class="table-title">累计质量</h3>
                        <div class="quality-kpis">
                            <div><span>累计调用</span><strong>{{ fmt(llmStats.total_calls) }}</strong></div>
                            <div><span>输入 Token</span><strong>{{ fmt(llmStats.total_input_tokens) }}</strong></div>
                            <div><span>输出 Token</span><strong>{{ fmt(llmStats.total_output_tokens) }}</strong></div>
                            <div><span>失败调用</span><strong>{{ fmt(llmStats.failed_calls) }}</strong></div>
                            <div><span>错误率</span><strong>{{ (llmStats.error_rate * 100).toFixed(1) }}%</strong></div>
                            <div><span>平均延迟</span><strong>{{ fmt(llmStats.avg_latency_ms) }} ms</strong></div>
                        </div>

                        <h3 class="table-title">按智能体</h3>
                        <div v-if="llmStats.by_agent.length > 0" class="table-scroll llm-table-scroll">
                            <table class="dash-table">
                                <thead>
                                    <tr>
                                        <th scope="col">智能体</th>
                                        <th scope="col">次数</th>
                                        <th scope="col">输入</th>
                                        <th scope="col">输出</th>
                                        <th scope="col">失败</th>
                                        <th scope="col">平均延迟</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="row in llmStats.by_agent" :key="row.agent_id">
                                        <td>{{ store.agentById(row.agent_id)?.name ?? row.agent_id }}</td>
                                        <td>{{ fmt(row.calls) }}</td>
                                        <td>{{ fmt(row.input_tokens) }}</td>
                                        <td>{{ fmt(row.output_tokens) }}</td>
                                        <td>{{ fmt(row.failed) }}</td>
                                        <td>{{ fmt(row.avg_latency_ms) }} ms</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                        <p v-else class="empty">暂无记录</p>

                        <h3 class="table-title">按模型</h3>
                        <div v-if="llmStats.by_model.length > 0" class="table-scroll llm-table-scroll">
                            <table class="dash-table">
                                <thead>
                                    <tr>
                                        <th scope="col">模型</th>
                                        <th scope="col">次数</th>
                                        <th scope="col">输入</th>
                                        <th scope="col">输出</th>
                                    </tr>
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
                        </div>
                        <p v-else class="empty">暂无记录</p>
                    </template>
                    <p v-else class="empty">实时数据未加载</p>
                </section>

                <section class="dash-section events-section" aria-labelledby="events-title">
                    <div class="section-heading">
                        <p class="section-kicker">世界留痕</p>
                        <h2 id="events-title">事件构成</h2>
                    </div>
                    <template v-if="eventStats">
                        <div class="compact-kpis event-kpis">
                            <div><span>累计事件</span><strong>{{ fmt(eventStats.total) }}</strong></div>
                            <div><span>最新序号</span><strong>#{{ fmt(eventStats.latest_sequence) }}</strong></div>
                        </div>
                        <ul v-if="eventRows.length > 0" class="event-list">
                            <li v-for="row in eventRows" :key="row.type" class="event-row">
                                <div class="event-head">
                                    <span class="event-type">{{ row.type }}</span>
                                    <span class="event-count">{{ fmt(row.count) }} · {{ row.percent.toFixed(1) }}%</span>
                                </div>
                                <div
                                    class="event-bar-track"
                                    role="progressbar"
                                    :aria-label="`${row.type} 事件比例`"
                                    :aria-valuenow="row.percent"
                                    aria-valuemin="0"
                                    aria-valuemax="100"
                                    :aria-valuetext="`${row.type} ${fmt(row.count)} 件，占 ${row.percent.toFixed(1)}%`"
                                >
                                    <span class="event-bar" :style="{width: `${row.width}%`}" />
                                </div>
                            </li>
                        </ul>
                        <p v-else class="empty">暂无事件</p>
                    </template>
                    <p v-else class="empty">实时数据未加载</p>
                </section>
            </div>
        </main>

        <main v-else class="dashboard-empty">尚未选择世界</main>
    </div>
</template>

<style scoped>
.dashboard {
    position: fixed;
    inset: 0;
    overflow-y: auto;
    background: #07150e;
    color: #e9f3e9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

.dash-header {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 14px 20px;
    border-bottom: 1px solid #274c39;
    background: #07150e;
}

.dash-title {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
}

.dash-kicker,
.section-kicker,
.pulse-label,
.status-label {
    margin: 0;
    color: #73c9da;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
}

.dash-header h1,
.section-heading h2 {
    margin: 0;
    color: #e9f3e9;
    font-family: ui-rounded, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    font-weight: 750;
}

.dash-header h1 {
    font-size: 23px;
    letter-spacing: 0.03em;
}

.dash-actions {
    display: flex;
    gap: 8px;
}

.dash-btn {
    min-height: 34px;
    padding: 7px 14px;
    border: 1px solid #274c39;
    border-radius: 4px;
    background: #0e2118;
    color: #e9f3e9;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
}

.dash-btn:hover {
    border-color: #73c9da;
    background: #163124;
}

.dash-btn.primary {
    border-color: #a7df83;
    background: #274c39;
    color: #e9f3e9;
}

.dash-btn:focus-visible {
    outline: 2px solid #73c9da;
    outline-offset: 2px;
}

.dashboard-content {
    padding: 14px 20px 28px;
}

.status-band {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    border: 1px solid #274c39;
    background: #0e2118;
}

.status-item {
    display: flex;
    flex: 1 1 130px;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
    padding: 9px 12px;
    border-right: 1px solid #274c39;
}

.status-item:last-child {
    border-right: 0;
}

.status-item strong {
    overflow: hidden;
    color: #e9f3e9;
    font-size: 13px;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.status-item .state-running {
    color: #a7df83;
}

.status-item .state-paused {
    color: #f6ce6d;
}

.data-value,
.pulse-value,
.compact-kpis strong,
.three-column-ledger strong,
.money-card strong,
.quality-kpis strong,
.action-count strong,
.ledger-kv dd,
.dash-table td,
.event-count {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
}

.dash-error {
    margin: 10px 0 0;
    border-left: 3px solid #f18a71;
    padding: 7px 10px;
    background: color-mix(in srgb, #f18a71 12%, transparent);
    color: #f18a71;
    font-size: 13px;
}

.pulse-grid {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 8px;
    margin: 12px 0;
}

.pulse-card {
    display: flex;
    min-height: 92px;
    flex-direction: column;
    justify-content: space-between;
    gap: 7px;
    border: 1px solid #274c39;
    padding: 10px 11px;
    background: #0e2118;
}

.pulse-value {
    color: #a7df83;
    font-size: 19px;
    line-height: 1;
}

.finance-pulse .pulse-value,
.finance-pulse .pulse-label {
    color: #f6ce6d;
}

.ai-pulse .pulse-value,
.ai-pulse .pulse-label,
.event-pulse .pulse-label {
    color: #73c9da;
}

.pulse-detail,
.pulse-unavailable {
    color: #b8caba;
    font-size: 11px;
    line-height: 1.45;
}

.pulse-unavailable,
.muted {
    color: #8da995;
}

.dash-grid {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 12px;
}

.dash-section {
    min-width: 0;
    border: 1px solid #274c39;
    padding: 15px;
    background: #0e2118;
}

.residents-section,
.commerce-section,
.ai-section {
    grid-column: span 7;
}

.employment-section,
.finance-section,
.events-section {
    grid-column: span 5;
}

.section-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 13px;
    border-bottom: 1px solid #274c39;
    padding-bottom: 9px;
}

.section-heading h2 {
    font-size: 17px;
}

.section-heading .section-kicker {
    color: #a7df83;
}

.need-tracks {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px 14px;
}

.need-track,
.supply-track,
.daily-llm {
    border-top: 1px solid #274c39;
    padding-top: 9px;
}

.track-heading,
.event-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    font-size: 12px;
}

.track-heading strong {
    color: #e9f3e9;
}

.track-note,
.meter-caption,
.unlimited-budget {
    margin: 3px 0 6px;
    color: #8da995;
    font-size: 11px;
}

.threshold-note,
.threshold-value {
    color: #f18a71;
}

.meter-track,
.event-bar-track {
    height: 8px;
    overflow: hidden;
    border: 1px solid #274c39;
    background: #07150e;
}

.meter-fill,
.event-bar {
    display: block;
    height: 100%;
    background: #a7df83;
}

.meter-fill.alert {
    background: #f18a71;
}

.blue-fill,
.event-bar {
    background: #73c9da;
}

.gold-fill {
    background: #f6ce6d;
}

.resident-summary {
    display: grid;
    grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
    gap: 14px;
    margin-top: 15px;
}

.resident-summary h3,
.attendance-ledger h3,
.daily-llm h3,
.table-title {
    margin: 0 0 8px;
    color: #b8caba;
    font-size: 12px;
    font-weight: 700;
}

.action-grid,
.three-column-ledger,
.compact-kpis,
.quality-kpis {
    display: grid;
    gap: 6px;
}

.action-grid {
    grid-template-columns: repeat(5, minmax(0, 1fr));
}

.action-count,
.three-column-ledger > div,
.compact-kpis > div,
.quality-kpis > div {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 3px;
    border: 1px solid #274c39;
    padding: 7px 8px;
    background: #10271c;
}

.action-count span,
.three-column-ledger span,
.compact-kpis span,
.quality-kpis span,
.money-card span {
    color: #b8caba;
    font-size: 10px;
}

.action-count strong,
.three-column-ledger strong,
.compact-kpis strong,
.quality-kpis strong {
    color: #e9f3e9;
    font-size: 15px;
}

.ledger-kv {
    display: flex;
    flex-direction: column;
    gap: 0;
    margin: 0;
    border-top: 1px solid #274c39;
}

.ledger-kv > div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    border-bottom: 1px solid #274c39;
    padding: 7px 0;
    font-size: 12px;
}

.ledger-kv dt {
    color: #b8caba;
}

.ledger-kv dd {
    margin: 0;
    color: #e9f3e9;
    text-align: right;
}

.resident-assets {
    align-self: end;
}

.table-title {
    margin-top: 15px;
}

.table-scroll {
    overflow: auto;
    border: 1px solid #274c39;
    background: #07150e;
}

.resident-table-scroll {
    max-height: 300px;
}

.company-table-scroll {
    max-height: 260px;
}

.llm-table-scroll {
    max-height: 210px;
}

.dash-table {
    width: 100%;
    min-width: 520px;
    border-collapse: collapse;
    font-size: 12px;
}

.dash-table th,
.dash-table td {
    padding: 7px 8px;
    border-bottom: 1px solid #274c39;
    text-align: right;
    white-space: nowrap;
}

.dash-table th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: #10271c;
    color: #b8caba;
    font-size: 10px;
    font-weight: 700;
}

.dash-table th:first-child,
.dash-table td:first-child,
.dash-table td:nth-child(2) {
    text-align: left;
}

.dash-table td {
    color: #e9f3e9;
}

.compact-kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
}

.attendance-ledger {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 12px;
    margin-top: 13px;
    border-top: 1px solid #274c39;
    border-bottom: 1px solid #274c39;
    padding: 9px 0;
    color: #b8caba;
    font-size: 12px;
}

.attendance-ledger h3 {
    margin: 0 auto 0 0;
}

.attendance-ledger strong {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.three-column-ledger {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 11px 0;
}

.finance-ledger {
    display: grid;
    grid-template-columns: 1fr;
    gap: 7px;
}

.money-card {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    border-left: 3px solid #f6ce6d;
    padding: 10px 11px;
    background: #10271c;
}

.money-card strong {
    color: #f6ce6d;
    font-size: 20px;
}

.total-ledger {
    margin-top: 12px;
}

.stock-meter {
    margin-top: 6px;
}

.budget-heading {
    margin-top: 12px;
}

.unlimited-budget {
    color: #73c9da;
    font-weight: 700;
}

.quality-kpis {
    grid-template-columns: repeat(3, minmax(0, 1fr));
}

.event-kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
}

.event-list {
    display: flex;
    max-height: 360px;
    flex-direction: column;
    gap: 10px;
    margin: 13px 0 0;
    padding: 0;
    overflow-y: auto;
    list-style: none;
}

.event-row {
    border-bottom: 1px solid #274c39;
    padding-bottom: 9px;
}

.event-head {
    margin-bottom: 5px;
}

.event-type {
    overflow: hidden;
    color: #e9f3e9;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.event-count {
    flex: 0 0 auto;
    color: #b8caba;
    font-size: 11px;
}

.empty {
    margin: 10px 0 0;
    color: #8da995;
    font-size: 13px;
}

.dashboard-empty {
    display: grid;
    min-height: calc(100vh - 63px);
    place-items: center;
    color: #b8caba;
    font-size: 15px;
}

@media (max-width: 1100px) {
    .pulse-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .dash-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .dash-section,
    .residents-section,
    .commerce-section,
    .ai-section,
    .employment-section,
    .finance-section,
    .events-section {
        grid-column: span 1;
    }
}

@media (max-width: 700px) {
    .dash-header,
    .dash-title,
    .status-band {
        align-items: flex-start;
    }

    .dash-header {
        flex-wrap: wrap;
        padding: 12px 14px;
    }

    .dash-title {
        flex-direction: column;
        gap: 2px;
    }

    .dash-actions {
        width: 100%;
    }

    .dash-btn {
        flex: 1;
    }

    .dashboard-content {
        padding: 10px 12px 22px;
    }

    .status-band {
        flex-direction: column;
    }

    .status-item {
        width: 100%;
        flex: 0 0 auto;
        border-right: 0;
        border-bottom: 1px solid #274c39;
    }

    .status-item:last-child {
        border-bottom: 0;
    }

    .pulse-grid,
    .dash-grid,
    .need-tracks,
    .resident-summary,
    .compact-kpis,
    .quality-kpis {
        grid-template-columns: 1fr;
    }

    .dash-section,
    .residents-section,
    .commerce-section,
    .ai-section,
    .employment-section,
    .finance-section,
    .events-section {
        grid-column: span 1;
    }

    .action-grid {
        grid-template-columns: repeat(5, minmax(0, 1fr));
    }

    .three-column-ledger {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .dash-section {
        padding: 12px;
    }
}
</style>
