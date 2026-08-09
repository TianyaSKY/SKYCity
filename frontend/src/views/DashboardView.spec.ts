/**
 * 数据看板 (DashboardView): 快照、运营总览、LLM 与事件四源独立刷新；
 * 局部失败保留上次成功数据，所有可见运营指标均来自稳定合同。
 */
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {createPinia, setActivePinia} from 'pinia';
import {flushPromises, mount} from '@vue/test-utils';
import DashboardView from './DashboardView.vue';
import {getDashboardOverview, getEventStats, getLlmStats, getSnapshot} from '../api/client';
import {useWorldStore} from '../stores/worldStore';
import type {
    CompanyInfo,
    DashboardOverviewResponse,
    EventStatsResponse,
    LlmStatsResponse,
    WorldSnapshotPayload,
} from '../types/world';

const fixtureSnapshot: WorldSnapshotPayload = {
    world: {world_id: 'w1', world_time: 480, speed: 1, paused: false, weather: 'sunny', day: 1},
    agents: [
        {
            agent_id: 'agent_linxia',
            name: '林夏',
            col: 10,
            row: 10,
            location_id: 'loc_plaza',
            satiety: 80,
            energy: 70,
            mood: 90,
            loneliness: 20,
            money: 150,
            inventory: [],
            action: null,
        },
        {
            agent_id: 'agent_zhangming',
            name: '张明',
            col: 12,
            row: 12,
            location_id: 'loc_plaza',
            satiety: 60,
            energy: 50,
            mood: 40,
            loneliness: 80,
            money: 50,
            inventory: [],
            action: {type: 'wait', ends_at: 600, reason: '休息'},
        },
    ],
    locations: [],
    structures: [],
    crops: [],
    stores: [],
    latest_sequence: 1,
};

const fixtureOverview: DashboardOverviewResponse = {
    treasury: {
        balance: 500,
        public_work_budget_remaining: 120,
        public_work_escrow: 40,
    },
    attendance_today: {attended: 1, late: 1, absent: 0},
    llm_today: {
        calls: 4,
        token_usage: 600,
        token_budget: 1000,
        token_remaining: 400,
        deciding_agents: 1,
    },
    need_thresholds: {
        satiety_lte: 60,
        energy_lte: 50,
        mood_lte: 40,
        loneliness_gte: 80,
    },
};

const fixtureLlm: LlmStatsResponse = {
    total_calls: 3,
    total_input_tokens: 600,
    total_output_tokens: 60,
    failed_calls: 1,
    error_rate: 0.3333,
    avg_latency_ms: 1000,
    by_agent: [
        {agent_id: 'agent_linxia', calls: 2, input_tokens: 300, output_tokens: 30, failed: 1, avg_latency_ms: 1000},
        {agent_id: 'agent_zhangming', calls: 1, input_tokens: 300, output_tokens: 30, failed: 0, avg_latency_ms: 1000},
    ],
    by_model: [
        {model: 'm1', calls: 2, input_tokens: 300, output_tokens: 30},
        {model: 'm2', calls: 1, input_tokens: 300, output_tokens: 30},
    ],
};

const fixtureEvents: EventStatsResponse = {
    total: 3,
    latest_sequence: 3,
    by_type: [
        {type: 'agent_wait_started', count: 2},
        {type: 'agent_wait_completed', count: 1},
    ],
};

const fixtureCompanies: CompanyInfo[] = [
    {
        company_id: 'coop_harvest',
        name: '丰收合作社',
        company_type: 'farm',
        location_id: 'loc_farm',
        manager_agent_id: 'agent_linxia',
        money: 300,
        status: 'active',
        employee_count: 1,
        open_vacancies: 2,
        unpaid_wage_total: 25,
    },
];

vi.mock('../api/client', () => ({
    checkHealth: vi.fn(),
    createWorld: vi.fn(),
    getAgentDetail: vi.fn(),
    getAgentEmployment: vi.fn(async () => ({employment: null, shifts: []})),
    getAgentShifts: vi.fn(async () => []),
    getCompanies: vi.fn(async () => fixtureCompanies),
    getCompany: vi.fn(),
    getCompanyEmployees: vi.fn(async () => []),
    getCompanyInventory: vi.fn(async () => []),
    getCompanyPositions: vi.fn(async () => []),
    getCompanyTransactions: vi.fn(async () => []),
    getConversations: vi.fn(),
    getDashboardOverview: vi.fn(async () => fixtureOverview),
    getDecisions: vi.fn(),
    getEventStats: vi.fn(async () => fixtureEvents),
    getJobOpenings: vi.fn(async () => []),
    getLlmStats: vi.fn(async () => fixtureLlm),
    getLocationDetail: vi.fn(async () => ({
        location_id: '',
        name: '',
        location_type: '',
        col: 0,
        row: 0,
        capacity: 0,
        open_hour: 0,
        close_hour: 24,
        open: true,
        occupants: [],
        products: [],
        jobs: [],
    })),
    getMemories: vi.fn(),
    getRelationships: vi.fn(),
    getSnapshot: vi.fn(async () => fixtureSnapshot),
    getStocks: vi.fn(async () => ({stocks: [], holdings: []})),
    listWorlds: vi.fn(),
    pauseWorld: vi.fn(),
    postAgentAction: vi.fn(),
    postGodAction: vi.fn(),
    resumeWorld: vi.fn(),
    setSpeed: vi.fn(),
}));

vi.mock('../websocket/client', () => ({
    getWorldSocket: vi.fn(() => ({connect: vi.fn(), close: vi.fn()})),
}));

function selectFixtureWorld(): void {
    const store = useWorldStore();
    store.worldId = 'w1';
    store.worlds = [
        {world_id: 'w1', name: '测试村', world_time: 480, speed: 1, paused: false},
    ];
}

describe('DashboardView', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        setActivePinia(createPinia());
    });

    it('请求四项统计并渲染运营脉搏、阈值、财政、考勤和行动表', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        selectFixtureWorld();
        const wrapper = mount(DashboardView);
        await flushPromises();

        expect(getSnapshot).toHaveBeenCalledWith('w1');
        expect(getDashboardOverview).toHaveBeenCalledWith('w1');
        expect(getLlmStats).toHaveBeenCalledWith('w1');
        expect(getEventStats).toHaveBeenCalledWith('w1');

        expect(wrapper.text()).toContain('村庄实时运营');
        expect(wrapper.text()).toContain('测试村');
        expect(wrapper.text()).toContain('人口');
        expect(wrapper.text()).toContain('忙碌 1 / 空闲 1');
        expect(wrapper.text()).toContain('资金');
        expect(wrapper.text()).toContain('1,000');
        expect(wrapper.text()).toContain('阈值 ≤ 60 · 1 人');
        expect(wrapper.text()).toContain('阈值 ≥ 80 · 1 人');
        expect(wrapper.text()).toContain('金库余额');
        expect(wrapper.text()).toContain('公共工程剩余额');
        expect(wrapper.text()).toContain('到岗');
        expect(wrapper.text()).toContain('迟到');
        expect(wrapper.text()).toContain('林夏');
        expect(wrapper.text()).toContain('张明');
        expect(wrapper.text()).toContain('剩余游戏分钟');
        expect(wrapper.findAll('.resident-table-scroll tbody tr')).toHaveLength(2);
        expect(wrapper.findAll('.company-table-scroll tbody tr')).toHaveLength(1);
        expect(wrapper.text()).toContain('当日 LLM 调度');
        expect(wrapper.text()).toContain('600 / 1,000 Token');
        expect(wrapper.text()).toContain('剩余 400 Token');
        expect(wrapper.findAll('.ai-section .dash-table tbody tr')).toHaveLength(4);
        expect(wrapper.findAll('.event-row')).toHaveLength(2);
        expect(wrapper.text()).toContain('66.7%');
        wrapper.unmount();
    });

    it('不限额时明确显示不限额且不渲染预算进度条', async () => {
        vi.mocked(getDashboardOverview).mockResolvedValueOnce({
            ...fixtureOverview,
            llm_today: {
                ...fixtureOverview.llm_today,
                token_budget: null,
                token_remaining: null,
            },
        });
        const pinia = createPinia();
        setActivePinia(pinia);
        selectFixtureWorld();
        const wrapper = mount(DashboardView);
        await flushPromises();

        expect(wrapper.text()).toContain('不限额');
        expect(wrapper.find('.budget-meter').exists()).toBe(false);
        wrapper.unmount();
    });

    it('运营总览失败时保留其他成功统计和上次总览数据', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        selectFixtureWorld();
        const wrapper = mount(DashboardView);
        await flushPromises();

        vi.mocked(getDashboardOverview).mockRejectedValueOnce(new Error('overview unavailable'));
        await wrapper.find('.dash-btn').trigger('click');
        await flushPromises();

        expect(getDashboardOverview).toHaveBeenCalledTimes(2);
        expect(wrapper.text()).toContain('部分统计数据未刷新，已显示上次成功数据');
        expect(wrapper.text()).toContain('600 / 1,000 Token');
        expect(wrapper.text()).toContain('33.3%');
        expect(wrapper.text()).toContain('agent_wait_started');
        wrapper.unmount();
    });

    it('LLM 与事件空数据时显示稳定空状态，不计算事件比例', async () => {
        vi.mocked(getLlmStats).mockResolvedValueOnce({
            total_calls: 0,
            total_input_tokens: 0,
            total_output_tokens: 0,
            failed_calls: 0,
            error_rate: 0.0,
            avg_latency_ms: 0,
            by_agent: [],
            by_model: [],
        });
        vi.mocked(getEventStats).mockResolvedValueOnce({
            total: 0,
            latest_sequence: 0,
            by_type: [],
        });
        const pinia = createPinia();
        setActivePinia(pinia);
        selectFixtureWorld();
        const wrapper = mount(DashboardView);
        await flushPromises();

        expect(wrapper.text()).toContain('0.0%');
        expect(wrapper.text()).toContain('暂无记录');
        expect(wrapper.text()).toContain('暂无事件');
        expect(wrapper.findAll('.event-row')).toHaveLength(0);
        wrapper.unmount();
    });

    it('未选择世界时不请求数据并给出明确空状态', async () => {
        const wrapper = mount(DashboardView);
        await flushPromises();

        expect(wrapper.text()).toContain('尚未选择世界');
        expect(getSnapshot).not.toHaveBeenCalled();
        expect(getDashboardOverview).not.toHaveBeenCalled();
        expect(getLlmStats).not.toHaveBeenCalled();
        expect(getEventStats).not.toHaveBeenCalled();
        wrapper.unmount();
    });

    it('返回按钮发出 close', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        selectFixtureWorld();
        const wrapper = mount(DashboardView);
        await flushPromises();

        await wrapper.find('.dash-btn.primary').trigger('click');
        expect(wrapper.emitted('close')).toHaveLength(1);
        wrapper.unmount();
    });
});
