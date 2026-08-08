/**
 * 数据看板 (DashboardView): 居民/经济区块由快照驱动，LLM 用量与事件统计
 * 区块来自聚合端点；轮询刷新 + 返回按钮发 close。
 */
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {createPinia, setActivePinia} from 'pinia';
import {flushPromises, mount} from '@vue/test-utils';
import DashboardView from './DashboardView.vue';
import {getEventStats, getLlmStats, getSnapshot} from '../api/client';
import {useWorldStore} from '../stores/worldStore';
import type {EventStatsResponse, LlmStatsResponse, WorldSnapshotPayload} from '../types/world';

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

vi.mock('../api/client', () => ({
    checkHealth: vi.fn(),
    createWorld: vi.fn(),
    getAgentDetail: vi.fn(),
    getAgentEmployment: vi.fn(async () => ({employment: null, shifts: []})),
    getAgentShifts: vi.fn(async () => []),
    getCompanies: vi.fn(async () => []),
    getCompany: vi.fn(),
    getCompanyEmployees: vi.fn(async () => []),
    getCompanyInventory: vi.fn(async () => []),
    getCompanyPositions: vi.fn(async () => []),
    getCompanyTransactions: vi.fn(async () => []),
    getConversations: vi.fn(),
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

describe('DashboardView', () => {
    beforeEach(() => {
        setActivePinia(createPinia());
    });

    it('四区块渲染：居民/经济来自快照，LLM 与事件来自聚合端点', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        const store = useWorldStore();
        store.worldId = 'w1';
        const wrapper = mount(DashboardView);
        await flushPromises();

        expect(getSnapshot).toHaveBeenCalledWith('w1');
        expect(getLlmStats).toHaveBeenCalledWith('w1');
        expect(getEventStats).toHaveBeenCalledWith('w1');

        // 居民统计
        expect(wrapper.text()).toContain('人口');
        expect(wrapper.text()).toContain('林夏');
        expect(wrapper.text()).toContain('张明');
        // 忙碌 1（张明 wait）/ 空闲 1（林夏无 action）
        const nums = wrapper.findAll('.num-card');
        const populationCard = nums.find((n) => n.text().includes('人口'))!.text();
        const busyCard = nums.find((n) => n.text().includes('忙碌'))!.text();
        const idleCard = nums.find((n) => n.text().includes('空闲'))!.text();
        expect(populationCard).toContain('2');
        expect(busyCard).toContain('1');
        expect(idleCard).toContain('1');
        // 居民资金 150 + 50 = 200
        expect(wrapper.text()).toContain('200');

        // LLM 用量
        expect(wrapper.text()).toContain('决策次数');
        expect(wrapper.text()).toContain('3');
        expect(wrapper.text()).toContain('600');
        expect(wrapper.text()).toContain('60');
        expect(wrapper.text()).toContain('33.3%');
        expect(wrapper.text()).toContain('1,000 ms'); // toLocaleString('zh-CN')
        const agentRows = wrapper.findAll('.dash-table tbody tr');
        expect(agentRows.length).toBe(4); // 按智能体 2 行 + 按模型 2 行
        expect(agentRows[0].text()).toContain('林夏');
        expect(agentRows[0].text()).toContain('2');

        // 事件统计
        expect(wrapper.text()).toContain('事件总数');
        expect(wrapper.text()).toContain('最新序号');
        const eventItems = wrapper.findAll('.event-row');
        expect(eventItems.length).toBe(2);
        expect(eventItems[0].text()).toContain('agent_wait_started');
        expect(eventItems[0].text()).toContain('66.7%');
        expect(wrapper.text()).not.toContain('暂无事件');
        wrapper.unmount();
    });

    it('返回按钮发出 close', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        const store = useWorldStore();
        store.worldId = 'w1';
        const wrapper = mount(DashboardView);
        await flushPromises();
        await wrapper.find('.dash-btn.primary').trigger('click');
        expect(wrapper.emitted('close')).toHaveLength(1);
        wrapper.unmount();
    });

    it('LLM 空数据时显示 0 与 暂无记录，不报错', async () => {
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
        const pinia = createPinia();
        setActivePinia(pinia);
        const store = useWorldStore();
        store.worldId = 'w1';
        const wrapper = mount(DashboardView);
        await flushPromises();
        expect(wrapper.text()).toContain('0.0%');
        expect(wrapper.text()).toContain('暂无记录');
        wrapper.unmount();
    });
});
