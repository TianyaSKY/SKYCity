<script lang="ts" setup>
import {computed, ref, watch} from 'vue';
import {getCompanyEmployees, getCompanyPositions, getCompanyTransactions} from '../api/client';
import {COMPANY_STATUS_LABELS, useWorldStore} from '../stores/worldStore';
import type {CompanyEmployee, CompanyPosition, CompanyTransaction} from '../types/world';

/**
 * 企业总览 (M13): 就业统计条 + 公司列表卡片 + 选中公司的详情 Tab
 * (概览/员工/招聘/库存/流水)。公司列表与公开招聘职位来自 store
 * (快照后 REST 拉取);详情数据按公司用 REST 拉取一次并缓存。
 */

const store = useWorldStore();
const collapsed = ref(false);
const selectedCompanyId = ref<string | null>(null);
const activeTab = ref<'overview' | 'employees' | 'hiring' | 'inventory' | 'ledger'>('overview');

interface CompanyDetail {
    positions: CompanyPosition[];
    employees: CompanyEmployee[];
    transactions: CompanyTransaction[];
}

/** REST detail cache per company (positions + employees + ledger). */
const details = ref<Record<string, CompanyDetail>>({});

const selectedCompany = computed(() =>
    selectedCompanyId.value ? (store.companyById(selectedCompanyId.value) ?? null) : null,
);

const selectedDetail = computed(() =>
    selectedCompanyId.value ? (details.value[selectedCompanyId.value] ?? null) : null,
);

/** 就业统计条: 全部来自 store 的 M13 getters。 */
const stats = computed(() => ({
    employed: store.employedCount,
    unemployed: store.unemployedCount,
    companies: store.companies.length,
    openings: store.openPositionCount,
    attended: store.todayShiftStats.attended,
    late: store.todayShiftStats.late,
    absent: store.todayShiftStats.absent,
    unpaid: store.unpaidWageTotal,
}));

const COMPANY_TYPE_LABELS: Record<string, string> = {
    farm: '农场',
    retail: '商店',
    workshop: '工坊',
};

function companyTypeLabel(type: string): string {
    return COMPANY_TYPE_LABELS[type] ?? type;
}

function statusLabel(status: string): string {
    return COMPANY_STATUS_LABELS[status] ?? status;
}

function statusClass(status: string): string {
    return `st-${status}`;
}

function locationName(locationId: string): string {
    return store.locations.find((l) => l.location_id === locationId)?.name ?? locationId;
}

function timeOf(worldTime: number): string {
    const hours = Math.floor(worldTime / 60) % 24;
    const minutes = worldTime % 60;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

const TX_LABELS: Record<string, string> = {
    initial_capital: '初始资金',
    sale_income: '销售收入',
    material_purchase: '原料采购',
    wholesale_sale: '批发销售',
    wage_payment: '工资支出',
    god_injection: '神谕注资',
};

function txLabel(type: string): string {
    return TX_LABELS[type] ?? type;
}

/** 今日收入: 本游戏日内的正额经营流水之和 (神谕注资不计入)。 */
function todayIncome(companyId: string): number {
    const txs = details.value[companyId]?.transactions;
    if (!txs) return 0;
    const dayStart = Math.floor(store.worldTime / 1440) * 1440;
    return txs
        .filter((t) => t.world_time >= dayStart && t.amount > 0 && t.type !== 'god_injection')
        .reduce((sum, t) => sum + t.amount, 0);
}

function positionTitleOf(companyId: string, positionId: string): string {
    const position = details.value[companyId]?.positions.find((p) => p.position_id === positionId);
    return position?.title ?? store.jobOpenings.find((o) => o.position_id === positionId)?.title ?? positionId;
}

async function loadDetail(companyId: string, force = false): Promise<void> {
    if (!store.worldId || (!force && details.value[companyId])) return;
    try {
        const [positions, employees, transactions] = await Promise.all([
            getCompanyPositions(store.worldId, companyId),
            getCompanyEmployees(store.worldId, companyId),
            getCompanyTransactions(store.worldId, companyId),
        ]);
        details.value[companyId] = {positions, employees, transactions};
    } catch {
        // Keep the panel usable with list-level data until the next trigger.
    }
}

function selectCompany(companyId: string): void {
    selectedCompanyId.value = companyId;
    void loadDetail(companyId);
}

let lastWorldId: string | null = null;
watch(
    () => [store.worldId, store.companies] as const,
    ([worldId, list]) => {
        // Switching worlds invalidates details cached for the previous world.
        if (worldId !== lastWorldId) {
            details.value = {};
            selectedCompanyId.value = null;
            lastWorldId = worldId;
        }
        for (const c of list) {
            const cached = details.value[c.company_id];
            // details 只加载一次：入职/辞职后 employees 列表会过期，与权威
            // employee_count 不一致时强制重载（员工 tab 数据保鲜）。
            if (!cached || cached.employees.length !== c.employee_count) {
                void loadDetail(c.company_id, true);
            }
        }
        if (!selectedCompanyId.value && list.length > 0) selectedCompanyId.value = list[0].company_id;
    },
    {immediate: true, deep: true},
);
</script>

<template>
    <aside aria-label="企业总览" class="company-panel">
        <header class="cp-head" @click="collapsed = !collapsed">
            <span class="cp-title">企业总览</span>
            <button :title="collapsed ? '展开' : '折叠'" class="cp-toggle">{{ collapsed ? '▸' : '▾' }}</button>
        </header>
        <template v-if="!collapsed">
            <div class="cp-stats">
                <div class="cp-stats-row">
                    <span class="cs-item">正式就业 <b>{{ stats.employed }}</b></span>
                    <span class="cs-item">失业 <b>{{ stats.unemployed }}</b></span>
                    <span class="cs-item">企业 <b>{{ stats.companies }}</b></span>
                    <span class="cs-item">开放职位 <b>{{ stats.openings }}</b></span>
                </div>
                <div class="cp-stats-row">
                    <span class="cs-item">今日出勤 <b>{{ stats.attended }}</b></span>
                    <span class="cs-item late">迟到 <b>{{ stats.late }}</b></span>
                    <span class="cs-item absent">缺勤 <b>{{ stats.absent }}</b></span>
                    <span class="cs-item warn">欠薪 <b>{{ stats.unpaid }}</b></span>
                </div>
            </div>

            <p v-if="store.companies.length === 0" class="cp-empty">暂无企业</p>
            <div v-else class="cp-list">
                <div
                    v-for="c in store.companies"
                    :key="c.company_id"
                    :class="{ active: selectedCompanyId === c.company_id }"
                    class="cp-company"
                    @click="selectCompany(c.company_id)"
                >
                    <div class="cp-c-head">
                        <span class="cp-c-name">{{ c.name }}</span>
                        <span :class="statusClass(c.status)" class="cp-c-status">{{ statusLabel(c.status) }}</span>
                    </div>
                    <div class="cp-c-meta">{{ companyTypeLabel(c.company_type) }} · {{
                            locationName(c.location_id)
                        }}
                    </div>
                    <div class="cp-c-stats">
                        <span>余额 {{ c.money }}</span>
                        <span>员工 {{ c.employee_count }}</span>
                        <span>招聘 {{ c.open_vacancies }}</span>
                        <span class="warn">欠薪 {{ c.unpaid_wage_total }}</span>
                        <span>今日收入 {{ todayIncome(c.company_id) }}</span>
                    </div>
                </div>
            </div>

            <template v-if="selectedCompany">
                <nav class="cp-tabs">
                    <button
                        v-for="tab in [
              { id: 'overview', label: '概览' },
              { id: 'employees', label: '员工' },
              { id: 'hiring', label: '招聘' },
              { id: 'inventory', label: '库存' },
              { id: 'ledger', label: '流水' },
            ]"
                        :key="tab.id"
                        :class="{ active: activeTab === tab.id }"
                        class="cp-tab"
                        @click="activeTab = tab.id as typeof activeTab"
                    >
                        {{ tab.label }}
                    </button>
                </nav>

                <!-- 概览: 余额/员工/欠薪/状态/经理 + 今日收入 -->
                <div v-if="activeTab === 'overview'" class="cp-detail">
                    <div class="ov-row"><span>余额</span><b>{{ selectedCompany.money }}</b></div>
                    <div class="ov-row"><span>员工数</span><b>{{ selectedCompany.employee_count }}</b></div>
                    <div class="ov-row"><span>欠薪总额</span><b class="warn">{{ selectedCompany.unpaid_wage_total }}</b>
                    </div>
                    <div class="ov-row"><span>状态</span><b>{{ statusLabel(selectedCompany.status) }}</b></div>
                    <div class="ov-row"><span>经理</span><b>{{
                            selectedCompany.manager_agent_id ? (store.agentById(selectedCompany.manager_agent_id)?.name ?? selectedCompany.manager_agent_id) : '—'
                        }}</b></div>
                    <div class="ov-row"><span>今日收入</span><b>{{ todayIncome(selectedCompany.company_id) }}</b></div>
                </div>

                <!-- 员工: 姓名/岗位/出勤评分/欠薪 -->
                <div v-if="activeTab === 'employees'" class="cp-detail">
                    <p v-if="!selectedDetail || selectedDetail.employees.length === 0" class="cp-empty">暂无在职员工</p>
                    <table v-else class="cp-table">
                        <thead>
                        <tr>
                            <th>姓名</th>
                            <th>岗位</th>
                            <th class="num">出勤</th>
                            <th class="num">欠薪</th>
                        </tr>
                        </thead>
                        <tbody>
                        <tr v-for="e in selectedDetail.employees" :key="e.employment_id">
                            <td>{{ e.agent_name }}</td>
                            <td>{{ positionTitleOf(selectedCompany.company_id, e.position_id) }}</td>
                            <td class="num">{{ e.attendance_score }}</td>
                            <td :class="{ warn: e.unpaid_wage > 0 }" class="num">{{ e.unpaid_wage }}</td>
                        </tr>
                        </tbody>
                    </table>
                </div>

                <!-- 招聘: 岗位容量 + 公开招聘 -->
                <div v-if="activeTab === 'hiring'" class="cp-detail">
                    <div v-if="selectedDetail && selectedDetail.positions.length > 0" class="hiring-block">
                        <div class="hiring-title">岗位</div>
                        <div v-for="p in selectedDetail.positions" :key="p.position_id" class="pos-item">
                            <div class="pos-head">
                                <span class="pos-name">{{ p.title }}</span>
                                <span :class="{ full: p.vacancies === 0 }" class="pos-vac">
                  {{ p.vacancies }}/{{ p.capacity }} 空缺
                </span>
                            </div>
                            <div class="pos-meta">每班 {{ p.wage_per_shift }} 金币 · {{
                                    timeOf(p.shift_start_minute)
                                }}–{{ timeOf(p.shift_end_minute) }}
                            </div>
                            <p class="pos-desc">{{ p.description }}</p>
                        </div>
                    </div>
                    <div class="hiring-block">
                        <div class="hiring-title">公开招聘</div>
                        <p v-if="store.jobOpenings.filter((o) => o.company_id === selectedCompany?.company_id).length === 0"
                           class="cp-empty">
                            暂无公开招聘
                        </p>
                        <div
                            v-for="o in store.jobOpenings.filter((x) => x.company_id === selectedCompany?.company_id)"
                            :key="o.opening_id"
                            class="pos-item"
                        >
                            <div class="pos-head">
                                <span class="pos-name">{{ o.title }}</span>
                                <span class="pos-vac">{{ o.vacancies }} 名额</span>
                            </div>
                            <div class="pos-meta">每班 {{ o.wage_per_shift }} 金币 · {{
                                    timeOf(o.shift_start_minute)
                                }}–{{ timeOf(o.shift_end_minute) }}
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 库存: 仓库总量/预留/可用（store 缓存，WS 事件实时替换） -->
                <div v-if="activeTab === 'inventory'" class="cp-detail">
                    <p v-if="!store.companyInventories[selectedCompany.company_id] || store.companyInventories[selectedCompany.company_id].length === 0"
                       class="cp-empty">
                        仓库暂无货物
                    </p>
                    <table v-else class="cp-table">
                        <thead>
                        <tr>
                            <th>物品</th>
                            <th class="num">总量</th>
                            <th class="num">预留</th>
                            <th class="num">可用</th>
                        </tr>
                        </thead>
                        <tbody>
                        <tr
                            v-for="row in store.companyInventories[selectedCompany.company_id]"
                            :key="row.item_id"
                        >
                            <td>{{ row.item_name }}</td>
                            <td class="num">{{ row.quantity }}</td>
                            <td :class="{ warn: row.reserved_quantity > 0 }" class="num">{{ row.reserved_quantity }}</td>
                            <td class="num">{{ row.available_quantity }}</td>
                        </tr>
                        </tbody>
                    </table>
                    <p class="cp-note">预留 = 已签到班次锁定的原料；可用 = 总量 − 预留。</p>
                </div>

                <!-- 流水: 类型/时间/金额/说明 -->
                <div v-if="activeTab === 'ledger'" class="cp-detail">
                    <p v-if="!selectedDetail || selectedDetail.transactions.length === 0" class="cp-empty">暂无流水</p>
                    <div v-else class="tx-list">
                        <div v-for="t in selectedDetail.transactions" :key="t.transaction_id" class="tx-item">
                            <span class="tx-time">{{ timeOf(t.world_time) }}</span>
                            <span class="tx-type">{{ txLabel(t.type) }}</span>
                            <span :class="t.amount >= 0 ? 'pos' : 'neg'" class="tx-amount">
                {{ t.amount >= 0 ? `+${t.amount}` : t.amount }}
              </span>
                            <span class="tx-reason">{{ t.reason }}</span>
                        </div>
                    </div>
                </div>
            </template>
        </template>
    </aside>
</template>

<style scoped>
.company-panel {
    width: 300px;
    height: 100%;
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

.cp-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 7px 12px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    user-select: none;
    cursor: pointer;
}

.cp-title {
    flex: 1;
    font-weight: 600;
    color: #ffe082;
    font-size: 12px;
}

.cp-toggle {
    background: none;
    border: none;
    color: rgba(205, 232, 213, 0.55);
    font-size: 12px;
    cursor: pointer;
    padding: 0;
}

.cp-stats {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 6px 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 11px;
}

.cp-stats-row {
    display: flex;
    justify-content: space-between;
    gap: 4px;
}

.cs-item {
    color: rgba(205, 232, 213, 0.7);
    white-space: nowrap;
}

.cs-item b {
    color: #e8f5e9;
}

.cs-item.late b {
    color: #ffd54f;
}

.cs-item.absent b,
.cs-item.warn b {
    color: #ff8a65;
}

.cp-empty {
    margin: 0;
    padding: 12px 8px;
    text-align: center;
    color: rgba(205, 232, 213, 0.5);
}

.cp-note {
    margin: 0;
    padding: 0 8px 10px;
    text-align: center;
    color: rgba(205, 232, 213, 0.45);
    font-size: 11px;
    line-height: 1.5;
}

.cp-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 6px;
    overflow-y: auto;
    min-height: 0;
    scrollbar-width: thin;
    scrollbar-color: rgba(255, 255, 255, 0.35) rgba(0, 0, 0, 0.25);
}

.cp-list::-webkit-scrollbar {
    width: 10px;
}

.cp-list::-webkit-scrollbar-track {
    background: rgba(0, 0, 0, 0.25);
    border-radius: 5px;
}

.cp-list::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.35);
    border-radius: 5px;
}

.cp-list::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.55);
}

.cp-company {
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.04);
    padding: 6px 8px;
    cursor: pointer;
}

.cp-company:hover {
    border-color: rgba(255, 224, 130, 0.6);
}

.cp-company.active {
    border-color: rgba(255, 224, 130, 0.9);
    background: rgba(255, 224, 130, 0.08);
}

.cp-c-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
}

.cp-c-name {
    flex: 1;
    font-weight: 600;
    color: #e8f5e9;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.cp-c-status {
    padding: 0 6px;
    border-radius: 999px;
    font-size: 10px;
    flex-shrink: 0;
}

.cp-c-status.st-active {
    background: rgba(76, 175, 80, 0.25);
    border: 1px solid rgba(76, 175, 80, 0.7);
    color: #c8e6c9;
}

.cp-c-status.st-suspended,
.cp-c-status.st-paused {
    background: rgba(255, 138, 101, 0.2);
    border: 1px solid rgba(255, 138, 101, 0.7);
    color: #ffccbc;
}

.cp-c-meta {
    margin-top: 2px;
    color: rgba(205, 232, 213, 0.55);
    font-size: 10.5px;
}

.cp-c-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 10px;
    margin-top: 3px;
    color: rgba(205, 232, 213, 0.75);
    font-size: 10.5px;
}

.cp-c-stats .warn {
    color: #ff8a65;
}

.cp-tabs {
    display: flex;
    gap: 2px;
    padding: 5px 6px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.cp-tab {
    flex: 1;
    border: none;
    background: transparent;
    color: rgba(205, 232, 213, 0.6);
    padding: 4px 0 6px;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    font-size: 11.5px;
}

.cp-tab.active {
    color: #ffe082;
    border-bottom-color: #ffe082;
}

.cp-detail {
    overflow-y: auto;
    padding: 6px 8px 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-height: 0;
    scrollbar-width: thin;
    scrollbar-color: rgba(255, 255, 255, 0.35) rgba(0, 0, 0, 0.25);
}

.cp-detail::-webkit-scrollbar {
    width: 10px;
}

.cp-detail::-webkit-scrollbar-track {
    background: rgba(0, 0, 0, 0.25);
    border-radius: 5px;
}

.cp-detail::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.35);
    border-radius: 5px;
}

.cp-detail::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.55);
}

.ov-row {
    display: flex;
    justify-content: space-between;
    padding: 3px 4px;
    border-radius: 6px;
    font-size: 11.5px;
}

.ov-row span {
    color: rgba(205, 232, 213, 0.7);
}

.ov-row b {
    color: #e8f5e9;
}

.ov-row b.warn {
    color: #ff8a65;
}

.cp-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
}

.cp-table th {
    text-align: left;
    font-weight: 500;
    color: rgba(205, 232, 213, 0.5);
    padding: 3px 4px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.cp-table th.num,
.cp-table td.num {
    text-align: right;
}

.cp-table td {
    padding: 3px 4px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}

.cp-table td.warn {
    color: #ff8a65;
}

.hiring-title {
    color: rgba(205, 232, 213, 0.5);
    font-size: 11px;
    padding: 2px 0;
}

.pos-item {
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 5px 7px;
    background: rgba(255, 255, 255, 0.03);
}

.pos-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
}

.pos-name {
    flex: 1;
    font-weight: 600;
    color: #e8f5e9;
}

.pos-vac {
    font-size: 10.5px;
    color: #9fd8ae;
    flex-shrink: 0;
}

.pos-vac.full {
    color: rgba(205, 232, 213, 0.5);
}

.pos-meta {
    margin-top: 2px;
    color: rgba(205, 232, 213, 0.6);
    font-size: 10.5px;
}

.pos-desc {
    margin: 2px 0 0;
    color: rgba(205, 232, 213, 0.45);
    font-size: 10.5px;
    line-height: 1.4;
}

.tx-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.tx-item {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 3px 4px;
    border-radius: 6px;
    font-size: 11px;
}

.tx-item:nth-child(odd) {
    background: rgba(255, 255, 255, 0.03);
}

.tx-time {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10.5px;
    color: rgba(205, 232, 213, 0.55);
    flex-shrink: 0;
}

.tx-type {
    color: rgba(205, 232, 213, 0.8);
    flex-shrink: 0;
}

.tx-amount {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10.5px;
    flex-shrink: 0;
}

.tx-amount.pos {
    color: #9fd8ae;
}

.tx-amount.neg {
    color: #ff8a65;
}

.tx-reason {
    flex: 1;
    min-width: 0;
    color: rgba(205, 232, 213, 0.55);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
</style>
