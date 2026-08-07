/**
 * M9 unit tests for the world store: snapshot overwrite semantics, sequence
 * dedupe, the eventText mapping for every WS event type the app consumes
 * (Chinese labels + state updates), and the time/opening getters.
 *
 * Pure store logic — the api and websocket modules are mocked so no network
 * call or WS socket is ever created.
 */
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {createPinia, setActivePinia} from 'pinia';
import {getLocationDetail as getLocationDetailApi} from '../api/client';
import {actionRemainingMinutes, TOOL_LABELS, taskLabelOf, taskPriority, useWorldStore} from './worldStore';
import type {
    AgentSnapshot,
    LocationDetail,
    WorldEventEnvelope,
    WorldLocation,
    WorldSnapshotPayload,
} from '../types/world';

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

const AGENT_LINXIA: AgentSnapshot = {
    agent_id: 'agent_linxia',
    name: '林夏',
    col: 3,
    row: 4,
    location_id: 'village_plaza',
    satiety: 50,
    energy: 80,
    mood: 100,
    loneliness: 30,
    money: 50,
    inventory: [{item_id: 'bread', quantity: 1}],
    action: null,
};

const AGENT_ZHANGMING: AgentSnapshot = {
    agent_id: 'agent_zhangming',
    name: '张明',
    col: 6,
    row: 7,
    location_id: 'village_plaza',
    satiety: 40,
    energy: 70,
    mood: 100,
    loneliness: 20,
    money: 20,
    inventory: [],
    action: null,
};

const LOCATION_SHOP: WorldLocation = {
    location_id: 'village_shop',
    name: '村口商店',
    location_type: 'shop',
    capacity: 5,
    open_hour: 8,
    close_hour: 18,
    col: 5,
    row: 5,
    open: true,
};

const LOCATION_HOUSE: WorldLocation = {
    location_id: 'village_house',
    name: '林夏的家',
    location_type: 'house',
    capacity: 2,
    open_hour: 0,
    close_hour: 24,
    col: 2,
    row: 2,
    open: true,
};

function baseSnapshot(): WorldSnapshotPayload {
    return {
        world: {
            world_id: 'world_e2e',
            world_time: 600,
            speed: 1,
            paused: false,
            weather: 'clear',
            day: 1,
        },
        agents: [AGENT_LINXIA, AGENT_ZHANGMING],
        locations: [LOCATION_SHOP, LOCATION_HOUSE],
        structures: [],
        crops: [],
        stores: [],
        latest_sequence: 0,
    };
}

function env(
    sequence: number,
    type: string,
    payload: Record<string, unknown>,
    worldTime = 600,
): WorldEventEnvelope {
    return {
        event_id: `ev_${sequence}`,
        sequence,
        world_id: 'world_e2e',
        world_time: worldTime,
        type: type as WorldEventEnvelope['type'],
        payload,
    };
}

describe('applySnapshot', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
    });

    it('overwrites stale state: agents replaced, events/bubbles/conversations cleared, latestSequence set', () => {
        // Dirty the store with incremental-only state that a snapshot must sweep away.
        store.applyEvent(env(1, 'world_speed_changed', {speed: 5}));
        store.applyEvent(env(2, 'conversation_started', {
            conversation_id: 'conv_stale',
            agent_ids: ['agent_linxia', 'agent_zhangming'],
        }));
        store.applyEvent(env(3, 'conversation_message', {
            conversation_id: 'conv_stale',
            from_agent_id: 'agent_linxia',
            to_agent_id: 'agent_zhangming',
            message: '旧的对话',
        }));
        expect(store.events.length).toBeGreaterThan(0);
        expect(store.bubbles.length).toBe(1);
        expect(Object.keys(store.activeConversations)).toHaveLength(1);

        store.applySnapshot({
            world: {
                world_id: 'world_fresh',
                world_time: 720,
                speed: 2,
                paused: true,
                weather: 'rain',
                day: 3,
            },
            agents: [{...AGENT_LINXIA, money: 99}],
            locations: [LOCATION_SHOP],
            structures: [],
            crops: [],
            stores: [],
        latest_sequence: 100,
        });

        expect(store.worldId).toBe('world_fresh');
        expect(store.worldTime).toBe(720);
        expect(store.speed).toBe(2);
        expect(store.paused).toBe(true);
        expect(store.weather).toBe('rain');
        expect(store.day).toBe(3);
        expect(store.latestSequence).toBe(100);
        // Agents are replaced wholesale: the stale second agent is gone.
        expect(store.agents).toHaveLength(1);
        expect(store.agents[0].agent_id).toBe('agent_linxia');
        expect(store.agents[0].money).toBe(99);
        // Incremental history is swept away.
        expect(store.events).toEqual([]);
        expect(store.bubbles).toEqual([]);
        expect(store.activeConversations).toEqual({});
        // Colors are seeded for the surviving agents.
        expect(store.agentColors['agent_linxia']).toBeTruthy();
    });

    it('hydrates structures from the payload (M14)', () => {
        store.applySnapshot({
            world: {
                world_id: 'world_e2e',
                world_time: 600,
                speed: 1,
                paused: false,
                weather: 'clear',
                day: 1,
            },
            agents: [AGENT_LINXIA],
            locations: [LOCATION_SHOP],
            stores: [],
        latest_sequence: 0,
            structures: [
                {
                    col: 5,
                    row: 6,
                    blueprint_id: 'bp_fence',
                    owner_agent_id: 'agent_linxia',
                    status: 'building',
                    built_at: null
                },
                {
                    col: 3,
                    row: 3,
                    blueprint_id: 'bp_house',
                    owner_agent_id: 'agent_zhangming',
                    status: 'built',
                    built_at: 720
                },
            ],
            crops: [],
        });
        expect(store.structures).toEqual([
            {
                col: 5,
                row: 6,
                blueprint_id: 'bp_fence',
                owner_agent_id: 'agent_linxia',
                status: 'building',
                built_at: null
            },
            {
                col: 3,
                row: 3,
                blueprint_id: 'bp_house',
                owner_agent_id: 'agent_zhangming',
                status: 'built',
                built_at: 720
            },
        ]);
    });

    it('hydrates crops from the payload (M15)', () => {
        store.applySnapshot({
            world: {
                world_id: 'world_e2e',
                world_time: 600,
                speed: 1,
                paused: false,
                weather: 'clear',
                day: 1,
            },
            agents: [AGENT_LINXIA],
            locations: [LOCATION_SHOP],
            stores: [],
        latest_sequence: 0,
            structures: [],
            crops: [
                {
                    col: 10,
                    row: 8,
                    item_id: 'wheat_seed',
                    planted_by: 'agent_linxia',
                    planted_at: 500,
                    stage: 2,
                    next_stage_at: 700
                },
                {
                    col: 11,
                    row: 8,
                    item_id: 'carrot_seed',
                    planted_by: 'agent_zhangming',
                    planted_at: 400,
                    stage: 0,
                    next_stage_at: 420
                },
            ],
        });
        expect(store.crops).toEqual([
            {
                col: 10,
                row: 8,
                item_id: 'wheat_seed',
                planted_by: 'agent_linxia',
                planted_at: 500,
                stage: 2,
                next_stage_at: 700
            },
            {
                col: 11,
                row: 8,
                item_id: 'carrot_seed',
                planted_by: 'agent_zhangming',
                planted_at: 400,
                stage: 0,
                next_stage_at: 420
            },
        ]);
    });
});

describe('applyEvent sequence dedupe', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
    });

    it('ignores events with sequence <= latestSequence', () => {
        store.applyEvent(env(1, 'world_speed_changed', {speed: 10}));
        expect(store.latestSequence).toBe(1);
        expect(store.speed).toBe(10);
        expect(store.events).toHaveLength(1);

        // Replay of the same sequence is dropped (even with a different payload).
        store.applyEvent(env(1, 'world_speed_changed', {speed: 5}));
        expect(store.latestSequence).toBe(1);
        expect(store.speed).toBe(10);
        expect(store.events).toHaveLength(1);

        // Stale (lower) sequences are dropped too.
        store.applyEvent(env(0, 'world_paused', {}));
        expect(store.latestSequence).toBe(1);
        expect(store.paused).toBe(false);

        // A genuinely newer sequence still applies.
        store.applyEvent(env(2, 'world_paused', {}));
        expect(store.latestSequence).toBe(2);
        expect(store.paused).toBe(true);
        expect(store.events).toHaveLength(2);
        expect(store.events[0].text).toBe('世界暂停');
    });
});

describe('applyEvent event mapping', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
    });

    it('world_time_changed: syncs the clock, no stream line', () => {
        store.applyEvent(env(1, 'world_time_changed', {world_time: 650}, 601));
        // Envelope time 601 is applied, then the payload wins.
        expect(store.worldTime).toBe(650);
        expect(store.events).toHaveLength(0);
    });

    it('world_paused / world_resumed: toggles paused + Chinese lines', () => {
        store.applyEvent(env(1, 'world_paused', {}));
        expect(store.paused).toBe(true);
        expect(store.events[0].text).toBe('世界暂停');

        store.applyEvent(env(2, 'world_resumed', {}));
        expect(store.paused).toBe(false);
        expect(store.events[0].text).toBe('世界恢复运行');
    });

    it('world_speed_changed: updates speed + line', () => {
        store.applyEvent(env(1, 'world_speed_changed', {speed: 10}));
        expect(store.speed).toBe(10);
        expect(store.events[0].text).toBe('世界速度调整为 10×');
    });

    it('agent_state_changed: patches the agent, no stream line', () => {
        store.applyEvent(env(1, 'agent_state_changed', {
            agent_id: 'agent_linxia',
            state: {satiety: 90, money: 33, col: 5, row: 5, location_id: 'village_shop'},
        }));
        const linxia = store.agents[0];
        expect(linxia.satiety).toBe(90);
        expect(linxia.money).toBe(33);
        expect(linxia.col).toBe(5);
        expect(linxia.row).toBe(5);
        expect(linxia.location_id).toBe('village_shop');
        expect(store.events).toHaveLength(0);
    });

    it('agent_move_started: sets a move action + line with destination name', () => {
        store.applyEvent(env(1, 'agent_move_started', {
            agent_id: 'agent_linxia',
            from: [3, 4],
            to: [5, 5],
            path: [[3, 4], [4, 4], [5, 5]],
            ends_at: 602,
            duration_minutes: 2,
        }));
        expect(store.agents[0].action).toEqual({
            type: 'move',
            from: [3, 4],
            to: [5, 5],
            path: [[3, 4], [4, 4], [5, 5]],
            started_at: 600,
            ends_at: 602,
            reason: null,
        });
        expect(store.events[0].text).toBe('林夏 出发前往 村口商店');
    });

    it('agent_move_started: malformed path falls back to from/to only', () => {
        store.applyEvent(env(1, 'agent_move_started', {
            agent_id: 'agent_linxia',
            from: [3, 4],
            to: [5, 5],
            path: [[3, 4], 'junk'],
            ends_at: 602,
            duration_minutes: 2,
        }));
        expect(store.agents[0].action).toEqual({
            type: 'move',
            from: [3, 4],
            to: [5, 5],
            started_at: 600,
            ends_at: 602,
            reason: null,
        });
    });

    it('agent_move_completed: snaps agent to destination, clears action', () => {
        store.applyEvent(env(1, 'agent_move_completed', {agent_id: 'agent_linxia', at: [5, 5]}));
        const linxia = store.agents[0];
        expect(linxia.col).toBe(5);
        expect(linxia.row).toBe(5);
        expect(linxia.location_id).toBe('village_shop');
        expect(linxia.action).toBeNull();
        expect(store.events[0].text).toBe('林夏 到达 村口商店');
    });

    it('agent_wait_started / agent_wait_completed: wait action + lines', () => {
        store.applyEvent(env(1, 'agent_wait_started', {
            agent_id: 'agent_linxia',
            minutes: 5,
            ends_at: 605,
        }));
        expect(store.agents[0].action).toEqual({type: 'wait', ends_at: 605, reason: null});
        expect(store.events[0].text).toBe('林夏 开始等待（5 分钟）');

        store.applyEvent(env(2, 'agent_wait_completed', {agent_id: 'agent_linxia', at: [3, 4]}));
        expect(store.agents[0].action).toBeNull();
        expect(store.events[0].text).toBe('林夏 结束等待');
    });

    it('world_event_created: surfaces the payload text verbatim', () => {
        store.applyEvent(env(1, 'world_event_created', {text: '村口来了一个神秘商人'}));
        expect(store.events[0].text).toBe('村口来了一个神秘商人');
    });

    it('conversation lifecycle: start registers, message bubbles, end unregisters', () => {
        store.applyEvent(env(1, 'conversation_started', {
            conversation_id: 'conv_1',
            agent_ids: ['agent_linxia', 'agent_zhangming'],
        }));
        expect(store.activeConversations['conv_1']).toEqual({
            agent_ids: ['agent_linxia', 'agent_zhangming'],
        });
        expect(store.events[0].text).toBe('林夏 和 张明 开始交谈');

        store.applyEvent(env(2, 'conversation_message', {
            conversation_id: 'conv_1',
            from_agent_id: 'agent_linxia',
            to_agent_id: 'agent_zhangming',
            message: '你好呀',
        }));
        expect(store.bubbles).toHaveLength(1);
        expect(store.bubbles[0].agent_id).toBe('agent_linxia');
        expect(store.bubbles[0].text).toBe('你好呀');
        expect(store.events[0].text).toBe('林夏 对 张明 说：你好呀');

        store.applyEvent(env(3, 'conversation_ended', {
            conversation_id: 'conv_1',
            reason: 'max_turns',
        }));
        // The registry entry is removed; the ended pair came from the registry.
        expect(store.activeConversations['conv_1']).toBeUndefined();
        expect(store.events[0].text).toBe('林夏 和 张明 的对话结束（已达上限）');
    });

    it('memory_created: records a truncated line', () => {
        store.applyEvent(env(1, 'memory_created', {
            agent_id: 'agent_linxia',
            memory_type: 'episodic',
            text: '今天在农田里种下了新的小麦种子',
        }));
        expect(store.events[0].text).toBe('林夏 记下了：今天在农田里种下了新的小麦种子');
    });

    it('relationship_changed: picks the largest delta axis + direction', () => {
        store.applyEvent(env(1, 'relationship_changed', {
            source_agent_id: 'agent_linxia',
            target_agent_id: 'agent_zhangming',
            deltas: {familiarity: 2, trust: 5},
        }));
        expect(store.events[0].text).toBe('林夏 对 张明 的信任提升了');
    });

    it('daily_reflection: reflection line', () => {
        store.applyEvent(env(1, 'daily_reflection', {
            agent_id: 'agent_linxia',
            summary: '今天收获颇丰',
        }));
        expect(store.events[0].text).toBe('林夏 的今日反思：今天收获颇丰');
    });

    it('work_started: job + duration line', () => {
        store.applyEvent(env(1, 'work_started', {
            agent_id: 'agent_linxia',
            job_id: 'job_farm',
            job_name: '种田',
            duration_minutes: 10,
            ends_at: 610,
        }));
        expect(store.events[0].text).toBe('林夏 开始工作（种田，10 分钟）');
    });

    it('work_started: sets the live work action (start/end derived)', () => {
        store.applyEvent(env(1, 'work_started', {
            agent_id: 'agent_linxia',
            job_id: 'job_farm',
            job_name: '种田',
            duration_minutes: 10,
            ends_at: 610,
        }));
        const action = store.agents[0].action;
        expect(action).not.toBeNull();
        expect(action?.type).toBe('work');
        if (action?.type === 'work') {
            expect(action.job_id).toBe('job_farm');
            expect(action.job_name).toBe('种田');
            expect(action.started_at).toBe(600);
            expect(action.ends_at).toBe(610);
        }
    });

    it('work_completed: clears the live work action', () => {
        store.applyEvent(env(1, 'work_started', {
            agent_id: 'agent_linxia',
            job_id: 'job_farm',
            job_name: '种田',
            duration_minutes: 10,
            ends_at: 610,
        }));
        store.applyEvent(env(2, 'work_completed', {
            agent_id: 'agent_linxia',
            job_id: 'job_farm',
            job_name: '种田',
            wage: 30,
            products: [],
            energy_spent: 10,
        }));
        expect(store.agents[0].action).toBeNull();
    });

    it('work_completed: wage + catalog item products line', () => {
        store.applyEvent(env(1, 'work_completed', {
            agent_id: 'agent_linxia',
            job_id: 'job_farm',
            job_name: '种田',
            wage: 30,
            products: [
                {item_id: 'wheat', quantity: 1},
                {item_id: 'egg', quantity: 2},
            ],
            energy_spent: 10,
        }));
        expect(store.events[0].text).toBe('林夏 完成工作，获得 30 金币 与产物：小麦×1、鸡蛋×2');
    });

    it('item_purchased: buy line with explicit name and total', () => {
        store.applyEvent(env(1, 'item_purchased', {
            agent_id: 'agent_linxia',
            item_id: 'bread',
            item_name: '面包',
            quantity: 2,
            unit_price: 6,
            total: 12,
        }));
        expect(store.events[0].text).toBe('林夏 购买 面包×2（12 金币）');
    });

    it('item_sold: sell line with catalog name', () => {
        store.applyEvent(env(1, 'item_sold', {
            agent_id: 'agent_linxia',
            item_id: 'wheat',
            item_name: '小麦',
            quantity: 3,
            unit_price: 3,
            total: 9,
        }));
        expect(store.events[0].text).toBe('林夏 出售 小麦×3（9 金币）');
    });

    it('item_used: use line with satiety before/after', () => {
        store.applyEvent(env(1, 'item_used', {
            agent_id: 'agent_linxia',
            item_id: 'bread',
            item_name: '面包',
            satiety_before: 80,
            satiety_after: 100,
        }));
        expect(store.events[0].text).toBe('林夏 使用了 面包（饱食度 80 → 100）');
    });

    it('money_changed: syncs the balance + signed delta line', () => {
        store.applyEvent(env(1, 'money_changed', {agent_id: 'agent_linxia', amount: -5, balance: 45}));
        expect(store.agents[0].money).toBe(45);
        expect(store.events[0].text).toBe('林夏 的金币变化 -5（当前 45）');
    });

    it('inventory_changed: replaces inventory, no stream line', () => {
        store.applyEvent(env(1, 'inventory_changed', {
            agent_id: 'agent_linxia',
            items: [
                {item_id: 'wheat', quantity: 4},
                {item_id: 'apple', quantity: 2},
            ],
        }));
        expect(store.agents[0].inventory).toEqual([
            {item_id: 'wheat', quantity: 4},
            {item_id: 'apple', quantity: 2},
        ]);
        expect(store.events).toHaveLength(0);
    });

    it('needs_changed: patches satiety/energy/mood/loneliness, no stream line', () => {
        store.applyEvent(env(1, 'needs_changed', {
            agent_id: 'agent_linxia',
            satiety: 90,
            energy: 25,
            mood: 55,
            loneliness: 40
        }));
        expect(store.agents[0].satiety).toBe(90);
        expect(store.agents[0].energy).toBe(25);
        expect(store.agents[0].mood).toBe(55);
        expect(store.agents[0].loneliness).toBe(40);
        expect(store.events).toHaveLength(0);
    });

    it('store_restocked: restock line with location name + items', () => {
        store.applyEvent(env(1, 'store_restocked', {
            store_id: 'village_shop',
            restocked: [
                {item_id: 'bread', quantity: 5},
                {item_id: 'milk', quantity: 3},
            ],
        }));
        expect(store.events[0].text).toBe('村口商店补货完成（面包×5、牛奶×3）');
    });

    it('store_price_changed: promo and price-restore lines', () => {
        store.applyEvent(env(1, 'store_price_changed', {
            store_id: 'village_shop',
            item_id: 'bread',
            item_name: '面包',
            sell_price: 10,
            promo: true,
        }));
        expect(store.events[0].text).toBe('村口商店的面包促销：10 金币');
        store.applyEvent(env(2, 'store_price_changed', {
            store_id: 'village_shop',
            item_id: 'bread',
            item_name: '面包',
            sell_price: 12,
            promo: false,
        }));
        expect(store.events[0].text).toBe('村口商店的面包恢复原价：12 金币');
    });

    it('god_action_applied: intervention line with command + target + reason', () => {
        store.applyEvent(env(1, 'god_action_applied', {
            command_type: 'change_weather',
            target_id: 'agent_linxia',
            reason: '玩家干预',
        }));
        expect(store.events[0].text).toBe('上帝干预：改变天气 林夏（玩家干预）');
    });

    it('weather_changed: syncs store weather + line', () => {
        store.applyEvent(env(1, 'weather_changed', {weather: 'rain'}));
        expect(store.weather).toBe('rain');
        expect(store.events[0].text).toBe('天气变为 雨');
    });

    it('god_teleport: snaps the agent to the destination + line', () => {
        store.applyEvent(env(1, 'god_teleport', {
            agent_id: 'agent_linxia',
            to: [5, 5],
            location_id: 'village_shop',
        }));
        const linxia = store.agents[0];
        expect(linxia.col).toBe(5);
        expect(linxia.row).toBe(5);
        expect(linxia.location_id).toBe('village_shop');
        expect(linxia.action).toBeNull();
        expect(store.events[0].text).toBe('林夏 被传送到了 村口商店');
    });

    it('item_spawned: god-grant line with catalog item', () => {
        store.applyEvent(env(1, 'item_spawned', {
            agent_id: 'agent_linxia',
            item_id: 'apple',
            item_name: '苹果',
            quantity: 3,
        }));
        expect(store.events[0].text).toBe('林夏 获得了 苹果×3（上帝）');
    });

    it('store_stock_changed: stock line', () => {
        store.applyEvent(env(1, 'store_stock_changed', {item_id: 'bread', quantity: 20}));
        expect(store.events[0].text).toBe('商店库存：bread → 20');
    });
});

describe('applyEvent M10 stock events', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(async () => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
        // applySnapshot fires loadStocks(); let the mock's empty payload land
        // first so the manual quote seeding below is not overwritten.
        await Promise.resolve();
        store.stocks = [
            {
                stock_id: 'stock_village_shop',
                name: '晨露商店',
                price: 100,
                prev_price: 100,
                day_business: 0,
                last_div_per_share: 0,
                source: 'store',
                company_id: 'village_shop',
            },
        ];
    });

    it('stock_price_changed: quote row + panel price update; zero delta stays silent', () => {
        store.applyEvent(env(1, 'stock_price_changed', {
            stock_id: 'stock_village_shop',
            stock_name: '晨露商店',
            price: 102,
            prev_price: 100,
            day_business: 3,
        }));
        expect(store.stocks[0].price).toBe(102);
        expect(store.stocks[0].prev_price).toBe(100);
        expect(store.stocks[0].day_business).toBe(3);
        expect(store.events[0].text).toBe('晨露商店 股价 102（+2）');

        // Unchanged price still syncs the panel but adds no stream line.
        store.applyEvent(env(2, 'stock_price_changed', {
            stock_id: 'stock_village_shop',
            stock_name: '晨露商店',
            price: 102,
            prev_price: 102,
            day_business: 3,
        }));
        expect(store.stocks[0].prev_price).toBe(102);
        expect(store.events).toHaveLength(1);
    });

    it('stock_bought: holding grows + event line', () => {
        store.applyEvent(env(1, 'stock_bought', {
            agent_id: 'agent_linxia',
            stock_id: 'stock_village_shop',
            stock_name: '晨露商店',
            shares: 2,
            unit_price: 20,
            total: 40,
        }));
        expect(store.holdings['agent_linxia']['stock_village_shop']).toBe(2);
        expect(store.events[0].text).toBe('林夏 买入 晨露商店 2股 @20（共40金币）');
    });

    it('stock_sold: holding shrinks, removed at zero', () => {
        store.holdings = {agent_linxia: {stock_village_shop: 2}};
        store.applyEvent(env(1, 'stock_sold', {
            agent_id: 'agent_linxia',
            stock_id: 'stock_village_shop',
            stock_name: '晨露商店',
            shares: 1,
            unit_price: 20,
            total: 20,
        }));
        expect(store.holdings['agent_linxia']['stock_village_shop']).toBe(1);
        expect(store.events[0].text).toBe('林夏 卖出 晨露商店 1股 @20（得20金币）');

        store.applyEvent(env(2, 'stock_sold', {
            agent_id: 'agent_linxia',
            stock_id: 'stock_village_shop',
            stock_name: '晨露商店',
            shares: 1,
            unit_price: 20,
            total: 20,
        }));
        expect(store.holdings['agent_linxia']['stock_village_shop']).toBeUndefined();
    });

    it('dividend_paid: dividend event line', () => {
        store.applyEvent(env(1, 'dividend_paid', {
            stock_id: 'stock_village_shop',
            stock_name: '晨露商店',
            div_per_share: 2,
            payouts: [{agent_id: 'agent_linxia', shares: 2, amount: 4}],
        }));
        expect(store.events[0].text).toBe('晨露商店 每股分红 2 金币');
    });

    it('manager_profit_paid: manager profit share line', () => {
        store.applyEvent(env(1, 'manager_profit_paid', {
            company_id: 'company_morning_farm',
            company_name: '晨露农场',
            manager_agent_id: 'agent_zhangming',
            amount: 20,
            profit: 100,
        }));
        expect(store.events[0].text).toBe('晨露农场 给经理张明分成 20 金币');
    });
});

describe('applyEvent M11 transfer events', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
    });

    it('money_transferred: 事件行', () => {
        store.applyEvent(env(1, 'money_transferred', {
            from_agent_id: 'agent_linxia',
            to_agent_id: 'agent_zhangming',
            amount: 30,
        }));
        expect(store.events[0].text).toBe('林夏 转账 30 金币给 张明');
    });

    it('item_given: 事件行', () => {
        store.applyEvent(env(1, 'item_given', {
            from_agent_id: 'agent_linxia',
            to_agent_id: 'agent_zhangming',
            item_id: 'bread',
            item_name: '面包',
            quantity: 2,
        }));
        expect(store.events[0].text).toBe('林夏 把 面包×2 给了 张明');
    });
});

describe('applyEvent M14 structure events', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
    });

    it('build_started: adds an in-progress (building) structure row', () => {
        store.applyEvent(env(1, 'build_started', {
            agent_id: 'agent_linxia',
            col: 5,
            row: 6,
            blueprint_id: 'bp_fence',
            duration_minutes: 30,
            ends_at: 510,
            materials: [{item_id: 'wood', quantity: 1}],
            reason: '围菜园',
        }));
        expect(store.structures).toEqual([
            {
                col: 5,
                row: 6,
                blueprint_id: 'bp_fence',
                owner_agent_id: 'agent_linxia',
                status: 'building',
                built_at: null
            },
        ]);
        expect(store.events[0].text).toBe('林夏 开始建造bp_fence');
    });

    it('structure_built: upserts the structure at its anchor cell', () => {
        store.applyEvent(env(1, 'structure_built', {
            agent_id: 'agent_linxia',
            col: 5,
            row: 6,
            blueprint_id: 'bp_fence',
            owner_agent_id: 'agent_linxia',
        }));
        expect(store.structures).toEqual([
            {col: 5, row: 6, blueprint_id: 'bp_fence', owner_agent_id: 'agent_linxia', status: 'built', built_at: null},
        ]);
        expect(store.events[0].text).toBe('林夏 建成了bp_fence');
    });

    it('structure_built: replaces an existing structure at the same cell', () => {
        store.applyEvent(env(1, 'structure_built', {
            agent_id: 'agent_linxia',
            col: 5,
            row: 6,
            blueprint_id: 'bp_fence',
            owner_agent_id: 'agent_linxia',
        }));
        store.applyEvent(env(2, 'structure_built', {
            agent_id: 'agent_zhangming',
            col: 5,
            row: 6,
            blueprint_id: 'bp_house',
            owner_agent_id: 'agent_zhangming',
        }));
        expect(store.structures).toEqual([
            {
                col: 5,
                row: 6,
                blueprint_id: 'bp_house',
                owner_agent_id: 'agent_zhangming',
                status: 'built',
                built_at: null
            },
        ]);
        expect(store.structures).toHaveLength(1);
    });

    it('structure_removed: deletes the structure at the anchor cell', () => {
        store.applyEvent(env(1, 'structure_built', {
            agent_id: 'agent_linxia',
            col: 5,
            row: 6,
            blueprint_id: 'bp_fence',
            owner_agent_id: 'agent_linxia',
        }));
        store.applyEvent(env(2, 'structure_removed', {
            col: 5,
            row: 6,
            blueprint_id: 'bp_fence',
            removed_by: 'agent_zhangming',
        }));
        expect(store.structures).toEqual([]);
        expect(store.events[0].text).toBe('bp_fence 被拆除了');
    });

    it('structure_built / structure_removed: malformed payloads are ignored defensively', () => {
        store.applyEvent(env(1, 'structure_built', {blueprint_id: 'bp_fence'}));
        store.applyEvent(env(2, 'structure_removed', {}));
        expect(store.structures).toEqual([]);
        // Malformed events still flow through without throwing.
        expect(store.latestSequence).toBe(2);
    });
});

describe('applyEvent M15 crop events', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
    });

    it('crop_planted: adds the crop at the payload stage + line', () => {
        store.applyEvent(env(1, 'crop_planted', {
            agent_id: 'agent_linxia',
            col: 10,
            row: 8,
            item_id: 'wheat_seed',
            item_name: '小麦',
            stage: 0,
            next_stage_at: 615,
        }));
        expect(store.crops).toEqual([
            {
                col: 10,
                row: 8,
                item_id: 'wheat_seed',
                planted_by: 'agent_linxia',
                planted_at: 600,
                stage: 0,
                next_stage_at: 615
            },
        ]);
        expect(store.events[0].text).toBe('林夏 种下了小麦');
    });

    it('crop_planted: replaces an existing crop at the same cell (upsert)', () => {
        store.applyEvent(env(1, 'crop_planted', {
            agent_id: 'agent_linxia',
            col: 10,
            row: 8,
            item_id: 'wheat_seed',
            item_name: '小麦',
            stage: 0,
            next_stage_at: 615,
        }));
        store.applyEvent(env(2, 'crop_planted', {
            agent_id: 'agent_zhangming',
            col: 10,
            row: 8,
            item_id: 'carrot_seed',
            item_name: '胡萝卜',
            stage: 0,
            next_stage_at: 620,
        }));
        expect(store.crops).toHaveLength(1);
        expect(store.crops[0].item_id).toBe('carrot_seed');
        expect(store.crops[0].planted_by).toBe('agent_zhangming');
    });

    it('crop_grown: advances the stage at the cell + line', () => {
        store.applyEvent(env(1, 'crop_planted', {
            agent_id: 'agent_linxia',
            col: 10,
            row: 8,
            item_id: 'wheat_seed',
            item_name: '小麦',
            stage: 0,
            next_stage_at: 615,
        }));
        store.applyEvent(env(2, 'crop_grown', {
            col: 10,
            row: 8,
            item_id: 'wheat_seed',
            stage: 1,
            next_stage_at: 700,
        }));
        expect(store.crops[0].stage).toBe(1);
        expect(store.crops[0].next_stage_at).toBe(700);
        expect(store.events[0].text).toBe('（10,8）的小麦种子长到了阶段1');
    });

    it('crop_harvested: removes the crop at the cell + line', () => {
        store.applyEvent(env(1, 'crop_planted', {
            agent_id: 'agent_linxia',
            col: 10,
            row: 8,
            item_id: 'wheat_seed',
            item_name: '小麦',
            stage: 3,
            next_stage_at: null,
        }));
        store.applyEvent(env(2, 'crop_harvested', {
            agent_id: 'agent_linxia',
            col: 10,
            row: 8,
            item_id: 'wheat_seed',
            item_name: '小麦',
            products: [{item_id: 'wheat', quantity: 2}],
        }));
        expect(store.crops).toEqual([]);
        expect(store.events[0].text).toBe('林夏 收获了小麦');
    });

    it('crop events: malformed payloads are ignored defensively', () => {
        store.applyEvent(env(1, 'crop_planted', {agent_id: 'agent_linxia'}));
        store.applyEvent(env(2, 'crop_grown', {}));
        store.applyEvent(env(3, 'crop_harvested', {}));
        expect(store.crops).toEqual([]);
        // Malformed events still flow through without throwing.
        expect(store.latestSequence).toBe(3);
    });
});

describe('getters', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(() => {
        setActivePinia(createPinia());
        store = useWorldStore();
    });

    it('timeLabel: minutes-since-midnight -> HH:MM', () => {
        store.worldTime = 480;
        expect(store.timeLabel).toBe('08:00');
        store.worldTime = 1439;
        expect(store.timeLabel).toBe('23:59');
        store.worldTime = 0;
        expect(store.timeLabel).toBe('00:00');
    });

    it('isOpen: honors open_hour/close_hour against worldTime', () => {
        store.locations = [LOCATION_SHOP, LOCATION_HOUSE];

        // 08:00 -> hour 8: open (8 <= 8 < 18).
        store.worldTime = 480;
        expect(store.isOpen('village_shop')).toBe(true);
        // 17:59 -> hour 17: open.
        store.worldTime = 1079;
        expect(store.isOpen('village_shop')).toBe(true);
        // 18:00 -> hour 18: closed ([open_hour, close_hour)).
        store.worldTime = 1080;
        expect(store.isOpen('village_shop')).toBe(false);
        // 05:00 -> hour 5: closed.
        store.worldTime = 300;
        expect(store.isOpen('village_shop')).toBe(false);
        // 00:00 next day (worldTime 1440 % 1440 = 0): closed.
        store.worldTime = 1440;
        expect(store.isOpen('village_shop')).toBe(false);

        // Houses never close.
        store.worldTime = 1080;
        expect(store.isOpen('village_house')).toBe(true);
        store.worldTime = 0;
        expect(store.isOpen('village_house')).toBe(true);

        // Unknown location -> closed.
        expect(store.isOpen('nowhere')).toBe(false);
    });
});

describe('location detail', () => {
    // The module is mocked by vi.mock above; the binding is the vi.fn itself.
    const getLocationDetailMock = vi.mocked(getLocationDetailApi);

    beforeEach(() => {
        getLocationDetailMock.mockReset();
    });

    async function flush(): Promise<void> {
        await Promise.resolve();
        await Promise.resolve();
    }

    it('selectLocation fetches the detail and stores it', async () => {
        const store = useWorldStore();
        store.worldId = 'world_e2e';
        const detail = {
            location_id: 'village_shop',
            name: '村口商店',
            location_type: 'shop',
            col: 5,
            row: 5,
            capacity: 5,
            open_hour: 8,
            close_hour: 18,
            open: true,
            occupants: [{agent_id: 'agent_linxia', name: '林夏'}],
            products: [{item_id: 'bread', name: '面包', sell_price: 5, buy_price: 3, stock: 12}],
            jobs: [{job_id: 'job_shop_attendant', name: '商店值班', wage: 60, duration_minutes: 240}],
        };
        getLocationDetailMock.mockResolvedValue(detail);

        store.selectLocation(LOCATION_SHOP);
        await flush();

        expect(getLocationDetailMock).toHaveBeenCalledWith('world_e2e', 'village_shop');
        expect(store.locationDetail).toEqual(detail);
    });

    it('drops stale detail when the selection changed mid-flight', async () => {
        const store = useWorldStore();
        store.worldId = 'world_e2e';
        const {promise: shopResponse, resolve: resolveShop} = Promise.withResolvers<LocationDetail>();
        getLocationDetailMock
            .mockImplementationOnce(() => shopResponse)
            .mockResolvedValue({...baseShopDetail(), location_id: 'village_house', name: '林夏的家'});

        store.selectLocation(LOCATION_SHOP);
        store.selectLocation(LOCATION_HOUSE);
        await flush();
        // The house fetch resolved while the shop request is still in flight.
        expect(store.locationDetail).toEqual({...baseShopDetail(), location_id: 'village_house', name: '林夏的家'});

        // The slow shop response lands late; it must not overwrite the house.
        resolveShop({...baseShopDetail(), location_id: 'village_shop'});
        await flush();
        expect(store.locationDetail?.location_id).toBe('village_house');
    });

    it('refetches the selected location on arrival/purchase/restock events only', async () => {
        const store = useWorldStore();
        store.worldId = 'world_e2e';
        getLocationDetailMock.mockResolvedValue(baseShopDetail());
        store.selectLocation(LOCATION_SHOP);
        await flush();
        getLocationDetailMock.mockClear();

        store.applyEvent(env(1, 'agent_move_completed', {agent_id: 'agent_linxia', at: [5, 5]}));
        await flush();
        expect(getLocationDetailMock).toHaveBeenCalledTimes(1);
        expect(getLocationDetailMock).toHaveBeenCalledWith('world_e2e', 'village_shop');

        store.applyEvent(env(2, 'item_purchased', {agent_id: 'agent_linxia', item_id: 'bread', quantity: 1}));
        await flush();
        expect(getLocationDetailMock).toHaveBeenCalledTimes(2);

        store.applyEvent(env(3, 'needs_changed', {agent_id: 'agent_linxia', satiety: 40}));
        await flush();
        expect(getLocationDetailMock).toHaveBeenCalledTimes(2);

        // No selection -> no refetch traffic.
        store.selectLocation(null);
        await flush();
        getLocationDetailMock.mockClear();
        store.applyEvent(env(4, 'agent_move_completed', {agent_id: 'agent_linxia', at: [5, 5]}));
        await flush();
        expect(getLocationDetailMock).not.toHaveBeenCalled();
    });
});

function baseShopDetail(): LocationDetail {
    return {
        location_id: 'village_shop',
        name: '村口商店',
        location_type: 'shop',
        col: 5,
        row: 5,
        capacity: 5,
        open_hour: 8,
        close_hour: 18,
        open: true,
        occupants: [],
        products: [],
        jobs: [],
    };
}

describe('task label helpers', () => {
    const locations: WorldLocation[] = [LOCATION_SHOP, LOCATION_HOUSE];

    it('TOOL_LABELS: 企业/招聘/班次工具显示中文标签', () => {
        expect(TOOL_LABELS['review_job_application']).toBe('审核申请');
        expect(TOOL_LABELS['withdraw_job_application']).toBe('撤回申请');
        expect(TOOL_LABELS['apply_job']).toBe('求职申请');
        expect(TOOL_LABELS['resign_job']).toBe('辞职');
        expect(TOOL_LABELS['terminate_employment']).toBe('解雇');
        expect(TOOL_LABELS['start_shift']).toBe('签到上班');
        expect(TOOL_LABELS['purchase_company_goods']).toBe('企业采购');
        expect(TOOL_LABELS['stock_store']).toBe('上架货架');
    });

    it('taskLabelOf: idle / move-to-named-location / wait / work / conversation', () => {
        expect(taskLabelOf(null, locations)).toBe('空闲');
        expect(taskLabelOf({type: 'move', from: [3, 4], to: [5, 5], started_at: 600, ends_at: 610}, locations)).toBe(
            '前往 村口商店',
        );
        expect(taskLabelOf({type: 'wait', ends_at: 610}, locations)).toBe('等待中');
        expect(taskLabelOf({
            type: 'work',
            job_id: 'job_farm',
            job_name: '种田',
            started_at: 600,
            ends_at: 610
        }, locations)).toBe(
            '工作中 · 种田',
        );
        // Job name may be missing on stale snapshots; fall back to the raw id.
        expect(taskLabelOf({type: 'work', job_id: 'job_farm', started_at: 600, ends_at: 610}, locations)).toBe(
            '工作中 · job_farm',
        );
        // An idle agent in an active conversation reads as 对话中.
        expect(taskLabelOf(null, locations, true)).toBe('对话中');
    });

    it('actionRemainingMinutes: clamps to 0, null when idle', () => {
        expect(actionRemainingMinutes({type: 'wait', ends_at: 610}, 605)).toBe(5);
        expect(actionRemainingMinutes({type: 'work', job_id: 'job_farm', started_at: 600, ends_at: 610}, 615)).toBe(0);
        expect(actionRemainingMinutes(null, 600)).toBeNull();
    });

    it('taskPriority: conversation > work > move > wait > idle', () => {
        expect(taskPriority(null, true)).toBe(0);
        expect(taskPriority({type: 'work', job_id: 'job_farm', started_at: 600, ends_at: 610}, false)).toBe(1);
        expect(taskPriority({type: 'move', from: [3, 4], to: [5, 5], started_at: 600, ends_at: 610}, false)).toBe(2);
        expect(taskPriority({type: 'wait', ends_at: 610}, false)).toBe(3);
        expect(taskPriority(null, false)).toBe(4);
    });
});

describe('applyEvent M13 company & employment events', () => {
    let store: ReturnType<typeof useWorldStore>;

    beforeEach(async () => {
        setActivePinia(createPinia());
        store = useWorldStore();
        store.applySnapshot(baseSnapshot());
        // Let the mocked loadCompanyData() land, then seed the fixtures the
        // event assertions depend on.
        await Promise.resolve();
        await Promise.resolve();
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
        store.jobOpenings = [
            {
                opening_id: 'opening_farm_1',
                company_id: 'company_morning_farm',
                company_name: '晨露农场',
                position_id: 'position_farm_worker',
                title: '农场工人',
                description: '负责播种、浇水和收获',
                location_id: 'village_farm',
                vacancies: 1,
                wage_per_shift: 60,
                shift_start_minute: 480,
                shift_end_minute: 720,
            },
        ];
    });

    it('job_application_submitted: 求职申请行（公司 + 岗位标题）', () => {
        store.applyEvent(env(1, 'job_application_submitted', {
            application_id: 'app_1',
            opening_id: 'opening_farm_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            reason: '想种田',
        }));
        expect(store.events[0].text).toBe('林夏 申请了 晨露农场·农场工人');
    });

    it('job_application_rejected: 拒绝行（审批人 + 原因）', () => {
        store.applyEvent(env(1, 'job_application_rejected', {
            application_id: 'app_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            manager_agent_id: 'agent_zhangming',
            reason: '名额已满',
        }));
        expect(store.events[0].text).toBe('林夏 的农场工人求职被 张明 拒绝（名额已满）');
    });

    it('job_application_withdrawn: 撤回行', () => {
        store.applyEvent(env(1, 'job_application_withdrawn', {
            application_id: 'app_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
        }));
        expect(store.events[0].text).toBe('林夏 撤回了对 晨露农场·农场工人 的求职申请');
    });

    it('employment_started: 入职行 + 权威计数赋值', () => {
        store.applyEvent(env(1, 'employment_started', {
            application_id: 'app_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            manager_agent_id: 'agent_zhangming',
            employment_id: 'emp_1',
            reason: '录用',
            employee_count: 3,
            open_vacancies: 0,
        }));
        expect(store.events[0].text).toBe('林夏 入职 晨露农场，担任农场工人');
        // D1: payload 赋值，而非本地 ±1（±1 会得到 2/0）
        expect(store.companies[0].employee_count).toBe(3);
        expect(store.companies[0].open_vacancies).toBe(0);
    });

    it('employment_resigned: 辞职行 + 权威计数赋值', () => {
        store.applyEvent(env(1, 'employment_resigned', {
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            agent_id: 'agent_linxia',
            reason: '另谋高就',
            employee_count: 0,
            open_vacancies: 5,
        }));
        expect(store.events[0].text).toBe('林夏 从 晨露农场 辞职（另谋高就）');
        // D1: payload 赋值（±1 会得到 0/2）
        expect(store.companies[0].employee_count).toBe(0);
        expect(store.companies[0].open_vacancies).toBe(5);
    });

    it('employment_terminated: 解雇行 + 权威计数赋值', () => {
        store.applyEvent(env(1, 'employment_terminated', {
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            agent_id: 'agent_linxia',
            manager_agent_id: 'agent_zhangming',
            reason: '违纪',
            employee_count: 1,
            open_vacancies: 4,
        }));
        expect(store.events[0].text).toBe('林夏 被 晨露农场 解雇（违纪）');
        // D1: payload 赋值（±1 会得到 0/2）
        expect(store.companies[0].employee_count).toBe(1);
        expect(store.companies[0].open_vacancies).toBe(4);
    });

    it('shift_scheduled: 排班行 + 班次缓存', () => {
        store.applyEvent(env(1, 'shift_scheduled', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            scheduled_start: 1440 + 480,
            scheduled_end: 1440 + 720,
        }));
        expect(store.events[0].text).toBe('林夏 的班次已排定（08:00–12:00）');
        expect(store.agentShifts['agent_linxia']?.status).toBe('scheduled');
    });

    it('shift_upcoming: 班次提醒行', () => {
        store.applyEvent(env(1, 'shift_upcoming', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            scheduled_start: 1920,
            scheduled_end: 2160,
            minutes_until_start: 60,
        }));
        expect(store.events[0].text).toBe('林夏 的班次即将开始（60 分钟后）');
    });

    it('shift_started: 上班行 + 出勤统计 + 上班标记', () => {
        store.applyEvent(env(1, 'shift_started', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            late_minutes: 0,
            ends_at: 2160,
        }));
        expect(store.events[0].text).toBe('林夏 开始上班');
        expect(store.isAgentOnShift('agent_linxia')).toBe(true);
        expect(store.todayShiftStats.attended).toBe(1);
        expect(store.todayShiftStats.late).toBe(0);

        store.applyEvent(env(2, 'shift_started', {
            shift_id: 'shift_2',
            employment_id: 'emp_2',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_zhangming',
            late_minutes: 15,
            ends_at: 2160,
        }));
        expect(store.events[0].text).toBe('张明 开始上班（迟到 15 分钟）');
        expect(store.todayShiftStats.attended).toBe(2);
        expect(store.todayShiftStats.late).toBe(1);
    });

    it('shift_completed: 完成行 + 产出 + 班次缓存', () => {
        store.applyEvent(env(1, 'shift_completed', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            worked_minutes: 240,
            products: [
                {item_id: 'wheat', quantity: 1},
                {item_id: 'egg', quantity: 2},
            ],
        }));
        expect(store.events[0].text).toBe('林夏 完成班次（工作 240 分钟，产出 小麦×1、鸡蛋×2）');
        expect(store.agentShifts['agent_linxia']?.status).toBe('completed');
        expect(store.isAgentOnShift('agent_linxia')).toBe(false);
    });

    it('shift_absent: 缺勤行 + 缺勤统计', () => {
        store.applyEvent(env(1, 'shift_absent', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
        }));
        expect(store.events[0].text).toBe('林夏 缺勤了班次');
        expect(store.agentShifts['agent_linxia']?.status).toBe('absent');
        expect(store.todayShiftStats.absent).toBe(1);
    });

    it('shift_leave_requested: 请假申请行', () => {
        store.applyEvent(env(1, 'shift_leave_requested', {
            request_id: 'req_1',
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            reason: '身体不适',
        }));
        expect(store.events[0].text).toBe('林夏 提交了请假申请（身体不适）');
    });

    it('shift_leave_approved: 批准行 + 班次状态置为请假', () => {
        store.applyEvent(env(1, 'shift_leave_approved', {
            request_id: 'req_1',
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            manager_agent_id: 'agent_zhangming',
            reason: '准假',
        }));
        expect(store.events[0].text).toBe('林夏 的请假申请已批准（张明）');
        expect(store.agentShifts['agent_linxia']?.status).toBe('leave');
    });

    it('shift_leave_rejected: 驳回行（班次保持排班）', () => {
        store.applyEvent(env(1, 'shift_leave_rejected', {
            request_id: 'req_1',
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            manager_agent_id: 'agent_zhangming',
            reason: '人手不够',
        }));
        expect(store.events[0].text).toBe('林夏 的请假申请被 张明 驳回（人手不够）');
    });

    it('shift_cancelled: 取消行 + 班次缓存', () => {
        store.applyEvent(env(1, 'shift_cancelled', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
        }));
        expect(store.events[0].text).toBe('林夏 的班次已取消');
        expect(store.agentShifts['agent_linxia']?.status).toBe('cancelled');
    });

    it('wage_paid: 工资行 + 余额同步', () => {
        store.applyEvent(env(1, 'wage_paid', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            wage_due: 60,
            wage_paid: 60,
            company_balance: 740,
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 向 林夏 支付工资 60 金币');
        expect(store.companies[0].money).toBe(740);
    });

    it('wage_unpaid: 欠薪行 + 欠薪总额累计', () => {
        store.applyEvent(env(1, 'wage_unpaid', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            wage_due: 60,
            wage_paid: 0,
            company_balance: 0,
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 未能支付 林夏 的工资（欠 60 金币）');
        expect(store.companies[0].unpaid_wage_total).toBe(60);
    });

    it('wage_repaid: 补发欠薪行 + 欠薪总额扣减', () => {
        store.companies[0].unpaid_wage_total = 60;
        store.applyEvent(env(1, 'wage_repaid', {
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            agent_id: 'agent_linxia',
            amount: 60,
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 向 林夏 补发欠薪 60 金币');
        expect(store.companies[0].unpaid_wage_total).toBe(0);
    });

    it('company_status_changed: 状态行 + 状态同步', () => {
        store.applyEvent(env(1, 'company_status_changed', {
            company_id: 'company_morning_farm',
            old_status: 'active',
            new_status: 'suspended',
            reason: '资金不足',
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 状态：经营中 → 停业');
        expect(store.companies[0].status).toBe('suspended');
    });

    it('company_money_changed: 资金行 + 余额同步', () => {
        store.applyEvent(env(1, 'company_money_changed', {
            company_id: 'company_morning_farm',
            amount: 120,
            balance: 920,
            reason: '商店售出 小麦×4',
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 资金变化 +120（当前 920）');
        expect(store.companies[0].money).toBe(920);
    });

    it('company_sale_completed: 售出行', () => {
        store.applyEvent(env(1, 'company_sale_completed', {
            company_id: 'company_morning_farm',
            store_id: 'village_shop',
            item_id: 'wheat',
            item_name: '小麦',
            quantity: 4,
            unit_price: 3,
            total: 12,
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 售出 小麦×4（12 金币）');
    });

    it('company_inventory_changed: 库存变化行', () => {
        store.applyEvent(env(1, 'company_inventory_changed', {
            company_id: 'company_morning_farm',
            items: [{item_id: 'wheat', quantity: 5}],
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 库存变化（小麦×5）');
    });

    it('company_inventory_changed: 替换指定企业缓存且不影响其他企业 (M16)', () => {
        store.companyInventories = {
            company_morning_farm: [
                {item_id: 'wheat', item_name: '小麦', quantity: 10, reserved_quantity: 0, available_quantity: 10},
            ],
            company_village_shop: [
                {item_id: 'bread', item_name: '面包', quantity: 20, reserved_quantity: 0, available_quantity: 20},
            ],
        };
        store.applyEvent(env(1, 'company_inventory_changed', {
            company_id: 'company_morning_farm',
            items: [
                {item_id: 'wheat', quantity: 0, reserved_quantity: 0},
                {item_id: 'bread', quantity: 20, reserved_quantity: 0},
            ],
        }));
        expect(store.companyInventories['company_morning_farm']).toEqual([
            {item_id: 'wheat', quantity: 0, reserved_quantity: 0},
            {item_id: 'bread', quantity: 20, reserved_quantity: 0},
        ]);
        // 其他企业缓存不被触碰
        expect(store.companyInventories['company_village_shop']).toEqual([
            {item_id: 'bread', item_name: '面包', quantity: 20, reserved_quantity: 0, available_quantity: 20},
        ]);
    });

    it('company_production_completed: 生产完成行（含消耗）', () => {
        store.applyEvent(env(1, 'company_production_completed', {
            company_id: 'company_village_bakery',
            shift_id: 'shift_1',
            consumed: [{item_id: 'wheat', quantity: 10}],
            products: [{item_id: 'bread', quantity: 20}],
        }));
        expect(store.events[0].text).toBe('企业 company_village_bakery 完成生产：面包×20（消耗 小麦×10）');
    });

    it('company_purchase_completed: 采购行', () => {
        store.applyEvent(env(1, 'company_purchase_completed', {
            company_id: 'company_village_bakery',
            seller_company_id: 'company_morning_farm',
            item_id: 'wheat',
            quantity: 10,
            unit_price: 6,
            total: 60,
        }));
        expect(store.events[0].text).toBe('企业 company_village_bakery 从 晨露农场 采购 小麦×10（60 金币）');
    });

    it('company_store_stocked: 上架行', () => {
        store.applyEvent(env(1, 'company_store_stocked', {
            company_id: 'company_village_shop',
            store_id: 'village_shop',
            item_id: 'bread',
            quantity: 20,
            stock_after: 20,
        }));
        expect(store.events[0].text).toBe('企业 company_village_shop 上架 面包×20 到货架');
    });

    it('job_opening_created: 发布职位行 + 空缺累计', () => {
        store.applyEvent(env(1, 'job_opening_created', {
            opening_id: 'opening_farm_2',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            vacancies: 2,
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 发布新职位 农场工人（招聘 2 人）');
        expect(store.companies[0].open_vacancies).toBe(3);
    });

    it('job_opening_closed: 关闭招聘行', () => {
        store.applyEvent(env(1, 'job_opening_closed', {
            opening_id: 'opening_farm_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            reason: '已招满',
        }));
        expect(store.events[0].text).toBe('企业 晨露农场 关闭了 农场工人 的招聘');
    });
});

describe('M13 getters', () => {
    it('employment stats aggregate from companies/openings', () => {
        setActivePinia(createPinia());
        const store = useWorldStore();
        store.agents = [AGENT_LINXIA, AGENT_ZHANGMING, {...AGENT_LINXIA, agent_id: 'agent_c', name: '王芳'}];
        store.companies = [
            {
                company_id: 'company_morning_farm',
                name: '晨露农场',
                company_type: 'farm',
                location_id: 'village_farm',
                manager_agent_id: null,
                money: 800,
                status: 'active',
                employee_count: 2,
                open_vacancies: 1,
                unpaid_wage_total: 60,
            },
            {
                company_id: 'company_village_shop',
                name: '村庄杂货店',
                company_type: 'retail',
                location_id: 'village_shop',
                manager_agent_id: null,
                money: 1000,
                status: 'active',
                employee_count: 1,
                open_vacancies: 0,
                unpaid_wage_total: 0,
            },
        ];
        store.jobOpenings = [
            {
                opening_id: 'opening_farm_1',
                company_id: 'company_morning_farm',
                company_name: '晨露农场',
                position_id: 'position_farm_worker',
                title: '农场工人',
                description: '',
                location_id: 'village_farm',
                vacancies: 1,
                wage_per_shift: 60,
                shift_start_minute: 480,
                shift_end_minute: 720,
            },
            {
                opening_id: 'opening_shop_1',
                company_id: 'company_village_shop',
                company_name: '村庄杂货店',
                position_id: 'position_shop_attendant',
                title: '商店店员',
                description: '',
                location_id: 'village_shop',
                vacancies: 3,
                wage_per_shift: 90,
                shift_start_minute: 540,
                shift_end_minute: 1020,
            },
        ];
        expect(store.employedCount).toBe(3);
        expect(store.unemployedCount).toBe(0);
        expect(store.openPositionCount).toBe(4);
        expect(store.unpaidWageTotal).toBe(60);
        expect(store.companyById('company_morning_farm')?.name).toBe('晨露农场');
        expect(store.companyById('nope')).toBeUndefined();
    });

    it('isAgentOnShift / nextShiftOf follow the WS shift cache with REST fallback', async () => {
        setActivePinia(createPinia());
        const store = useWorldStore();
        store.applySnapshot(baseSnapshot());
        await Promise.resolve();
        await Promise.resolve();
        store.worldTime = 1000;

        // Scheduled: not on shift yet, but the next shift is visible.
        store.applyEvent(env(1, 'shift_scheduled', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            scheduled_start: 1920,
            scheduled_end: 2160,
        }, 1000));
        expect(store.isAgentOnShift('agent_linxia')).toBe(false);
        expect(store.nextShiftOf('agent_linxia')?.scheduled_start).toBe(1920);

        // In progress: marked on shift; the cached scheduled shift is gone.
        store.applyEvent(env(2, 'shift_started', {
            shift_id: 'shift_1',
            employment_id: 'emp_1',
            company_id: 'company_morning_farm',
            position_id: 'position_farm_worker',
            agent_id: 'agent_linxia',
            late_minutes: 0,
            ends_at: 2160,
        }, 1000));
        expect(store.isAgentOnShift('agent_linxia')).toBe(true);
        expect(store.nextShiftOf('agent_linxia')).toBeNull();

        // REST employment fallback supplies the next scheduled shift.
        store.agentEmployment['agent_linxia'] = {
            employment: null,
            shifts: [
                {
                    shift_id: 'shift_2',
                    employment_id: 'emp_1',
                    company_id: 'company_morning_farm',
                    position_id: 'position_farm_worker',
                    agent_id: 'agent_linxia',
                    scheduled_start: 2880 + 540,
                    scheduled_end: 2880 + 780,
                    actual_start: null,
                    actual_end: null,
                    status: 'scheduled',
                    late_minutes: 0,
                    worked_minutes: 0,
                    wage_due: 0,
                    wage_paid: 0,
                    payroll_status: 'not_due',
                    output_json: null,
                    absence_reason: null,
                },
            ],
        };
        expect(store.nextShiftOf('agent_linxia')?.scheduled_start).toBe(3420);
        expect(store.employmentOf('agent_linxia')).toBeNull();
    });

    it('employmentOf returns the REST employment of an agent', () => {
        setActivePinia(createPinia());
        const store = useWorldStore();
        store.agentEmployment['agent_linxia'] = {
            employment: {
                employment_id: 'emp_1',
                company_id: 'company_morning_farm',
                position_id: 'position_farm_worker',
                job_id: 'job_farm_field',
                agent_id: 'agent_linxia',
                status: 'active',
                hired_at: 600,
                started_at: 600,
                ended_at: null,
                wage_per_shift: 60,
                attendance_score: 100,
                performance_score: 80,
                completed_shifts: 3,
                late_shifts: 0,
                absent_shifts: 0,
                unpaid_wage: 0,
                termination_reason: null,
            },
            shifts: [],
        };
        expect(store.employmentOf('agent_linxia')?.wage_per_shift).toBe(60);
        expect(store.employmentOf('agent_zhangming')).toBeNull();
    });
});
