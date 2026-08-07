/**
 * CompanyPanel inventory tab (M16): the warehouse table renders
 * 物品/总量/预留/可用 from the store cache (WS company_inventory_changed).
 */
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {createPinia, setActivePinia} from 'pinia';
import {flushPromises, mount} from '@vue/test-utils';
import CompanyPanel from './CompanyPanel.vue';
import {getCompanyEmployees} from '../api/client';
import {useWorldStore} from '../stores/worldStore';

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
    getJobOpenings: vi.fn(async () => []),
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
    getSnapshot: vi.fn(),
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

describe('CompanyPanel inventory tab', () => {
    beforeEach(() => {
        setActivePinia(createPinia());
    });

    it('非空库存显示 物品/总量/预留/可用（可用 = 总量 − 预留）', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        const store = useWorldStore();
        store.companies = [
            {
                company_id: 'company_morning_farm',
                name: '晨露农场',
                company_type: 'farm',
                location_id: 'village_farm',
                manager_agent_id: 'agent_zhangming',
                money: 800,
                status: 'active',
                employee_count: 1,
                open_vacancies: 1,
                unpaid_wage_total: 0,
            },
        ];
        store.companyInventories = {
            company_morning_farm: [
                {item_id: 'wheat', item_name: '小麦', quantity: 10, reserved_quantity: 4, available_quantity: 6},
                {item_id: 'bread', item_name: '面包', quantity: 20, reserved_quantity: 0, available_quantity: 20},
            ],
        };
        const wrapper = mount(CompanyPanel, {global: {plugins: [pinia]}});
        await flushPromises();

        const inventoryTab = wrapper.findAll('button.cp-tab').find((b) => b.text() === '库存');
        expect(inventoryTab).toBeTruthy();
        await inventoryTab!.trigger('click');
        await flushPromises();

        const rows = wrapper.findAll('tbody tr');
        expect(rows).toHaveLength(2);
        const wheatRow = rows[0].text();
        expect(wheatRow).toContain('小麦');
        expect(wheatRow).toContain('10'); // 总量
        expect(wheatRow).toContain('4');  // 预留
        expect(wheatRow).toContain('6');  // 可用
        const breadRow = rows[1].text();
        expect(breadRow).toContain('面包');
        expect(breadRow).toContain('20');
        expect(breadRow).toContain('0');
    });

    it('空仓库显示占位文案', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        const store = useWorldStore();
        store.companies = [
            {
                company_id: 'company_morning_farm',
                name: '晨露农场',
                company_type: 'farm',
                location_id: 'village_farm',
                manager_agent_id: 'agent_zhangming',
                money: 800,
                status: 'active',
                employee_count: 1,
                open_vacancies: 1,
                unpaid_wage_total: 0,
            },
        ];
        store.companyInventories = {company_morning_farm: []};
        const wrapper = mount(CompanyPanel, {global: {plugins: [pinia]}});
        await flushPromises();
        await wrapper.findAll('button.cp-tab').find((b) => b.text() === '库存')!.trigger('click');
        await flushPromises();
        expect(wrapper.text()).toContain('仓库暂无货物');
    });

    it('入职后员工列表自动重载（details 缓存过期保鲜）', async () => {
        const pinia = createPinia();
        setActivePinia(pinia);
        const store = useWorldStore();
        store.worldId = 'world_e2e';
        store.companies = [
            {
                company_id: 'company_morning_farm',
                name: '晨露农场',
                company_type: 'farm',
                location_id: 'village_farm',
                manager_agent_id: 'agent_zhangming',
                money: 800,
                status: 'active',
                employee_count: 0,
                open_vacancies: 2,
                unpaid_wage_total: 0,
            },
        ];
        const getCompanyEmployeesMock = vi.mocked(getCompanyEmployees);
        getCompanyEmployeesMock.mockResolvedValue([]);
        const wrapper = mount(CompanyPanel, {global: {plugins: [pinia]}});
        await flushPromises();
        expect(getCompanyEmployeesMock).toHaveBeenCalledTimes(1);
        // 权威计数变化（入职 ×2）→ 缓存列表长度失配 → 强制重载
        getCompanyEmployeesMock.mockResolvedValue([
            {employment_id: 'emp_1', agent_name: '老张', position_id: 'position_farm_worker', attendance_score: 100, unpaid_wage: 0},
            {employment_id: 'emp_2', agent_name: '林夏', position_id: 'position_farm_worker', attendance_score: 100, unpaid_wage: 0},
        ] as never);
        store.companies[0].employee_count = 2;
        await flushPromises();
        expect(getCompanyEmployeesMock).toHaveBeenCalledTimes(2);
        await wrapper.findAll('button.cp-tab').find((b) => b.text() === '员工')!.trigger('click');
        await flushPromises();
        expect(wrapper.text()).toContain('老张');
        expect(wrapper.text()).toContain('林夏');
    });
});
