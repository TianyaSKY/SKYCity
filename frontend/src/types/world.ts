/**
 * World runtime types aligned with the backend event protocol
 * (docs/event-protocol.md): agent snapshots, world clock state,
 * WebSocket event envelopes, and world list entries.
 */

import type {MapLocation} from './tiled';

/** A cell coordinate pair [col, row]. */
export type Cell = [number, number];

/** Agent action state carried in snapshots and state updates. */
export interface MoveAction {
    type: 'move';
    from: Cell;
    to: Cell;
    /** Full waypoint list from -> ... -> to (BFS path, backend-computed).
     * Absent in snapshots from pre-path saves; animators fall back to from/to. */
    path?: Cell[];
    started_at: number;
    ends_at: number;
    reason?: string | null;
}

export interface WaitAction {
    type: 'wait';
    ends_at: number;
    reason?: string | null;
}

/** An in-flight work shift (backend AgentActionWork; R10 settles at ends_at). */
export interface WorkAction {
    type: 'work';
    job_id: string;
    job_name?: string | null;
    started_at: number;
    ends_at: number;
    reason?: string | null;
}

/** An in-flight build job (backend AgentActionBuild; M14 settles at ends_at). */
export interface BuildAction {
    type: 'build';
    blueprint_id: string;
    col: number;
    row: number;
    started_at: number;
    ends_at: number;
    reason?: string | null;
}

export type AgentAction = MoveAction | WaitAction | WorkAction | BuildAction | null;

/** One inventory entry of an agent (item catalog keyed by item_id). */
export interface InventoryItem {
    item_id: string;
    quantity: number;
}

/** One agent as reported by the world snapshot. */
export interface AgentSnapshot {
    agent_id: string;
    name: string;
    col: number;
    row: number;
    location_id: string | null;
    satiety: number;
    energy: number;
    mood: number;
    loneliness: number;
    money: number;
    inventory: InventoryItem[];
    action: AgentAction;
}

/** World clock / global state block of a snapshot. */
export interface WorldClockState {
    world_id: string;
    world_time: number;
    speed: number;
    paused: boolean;
    weather: string;
    day: number;
}

/** Location as reported by the snapshot: MapLocation plus the open flag. */
export interface WorldLocation extends MapLocation {
    open: boolean;
}

/** One agent currently inside a location (location detail endpoint). */
export interface LocationOccupant {
    agent_id: string;
    name: string;
}

/** One product a store sells/buys, with live stock (location detail endpoint). */
export interface LocationProduct {
    item_id: string;
    name: string;
    sell_price: number;
    buy_price: number;
    stock: number;
}

/** One work offer at a location (location detail endpoint). */
export interface LocationJob {
    job_id: string;
    name: string;
    wage: number;
    duration_minutes: number;
}

/** Full location detail: snapshot fields + occupants + products + jobs. */
export interface LocationDetail {
    location_id: string;
    name: string;
    location_type: string;
    col: number;
    row: number;
    capacity: number;
    open_hour: number;
    close_hour: number;
    open: boolean;
    occupants: LocationOccupant[];
    products: LocationProduct[];
    jobs: LocationJob[];
}

/** Payload of the initial world_snapshot event / GET snapshot response. */
export interface WorldSnapshotPayload {
    world: WorldClockState;
    agents: AgentSnapshot[];
    locations: WorldLocation[];
    /** Agent-built structures (fences/houses/flower beds; M14). */
    structures: StructureSnapshot[];
    /** Planted crops (single-cell; M15). */
    crops: CropSnapshot[];
    latest_sequence: number;
}

/** One planted crop as reported by the world snapshot (M15). */
export interface CropSnapshot {
    col: number;
    row: number;
    /** Seed item id; keys the crop catalog (crops/crops.json). */
    item_id: string;
    planted_by: string;
    planted_at: number;
    /** 0-based growth index; the final stage is harvestable. */
    stage: number;
    next_stage_at: number | null;
}

/** Payload of the WS crop_planted event (M15). */
export interface CropPlantedPayload {
    agent_id: string;
    col: number;
    row: number;
    item_id: string;
    item_name: string;
    stage: number;
    next_stage_at: number | null;
}

/** Payload of the WS crop_grown event (M15). */
export interface CropGrownPayload {
    col: number;
    row: number;
    item_id: string;
    stage: number;
}

/** Payload of the WS crop_harvested event (M15). */
export interface CropHarvestedPayload {
    agent_id: string;
    col: number;
    row: number;
    item_id: string;
    item_name: string;
    products: InventoryItem[];
}

/** Payload of the WS work_started event. */
export interface WorkStartedPayload {
    agent_id: string;
    job_id: string;
    job_name: string;
    duration_minutes: number;
    ends_at: number;
    reason?: string | null;
}

/** Payload of the WS work_completed event. */
export interface WorkCompletedPayload {
    agent_id: string;
    job_id: string;
    job_name: string;
    wage: number;
    products: InventoryItem[];
    energy_spent: number;
}

/** Payload of the WS item_purchased event. */
export interface ItemPurchasedPayload {
    agent_id: string;
    item_id: string;
    item_name: string;
    quantity: number;
    unit_price: number;
    total: number;
}

/** Payload of the WS item_sold event. */
export interface ItemSoldPayload {
    agent_id: string;
    item_id: string;
    item_name: string;
    quantity: number;
    unit_price: number;
    total: number;
}

/** Payload of the WS item_used event. */
export interface ItemUsedPayload {
    agent_id: string;
    item_id: string;
    item_name: string;
    satiety_before: number;
    satiety_after: number;
    mood_before: number;
    mood_after: number;
}

/** Payload of the WS money_changed event. */
export interface MoneyChangedPayload {
    agent_id: string;
    /** Signed delta: positive = gained, negative = spent. */
    amount: number;
    balance: number;
    reason?: string | null;
}

/** Payload of the WS inventory_changed event. */
export interface InventoryChangedPayload {
    agent_id: string;
    items: InventoryItem[];
}

/** Payload of the WS needs_changed event. */
export interface NeedsChangedPayload {
    agent_id: string;
    satiety: number;
    energy: number;
    mood: number;
    loneliness: number;
}

/** Payload of the WS store_restocked event. */
export interface StoreRestockedPayload {
    store_id: string;
    restocked: InventoryItem[];
}

/** Payload of the WS store_price_changed event (M12 D5 promos). */
export interface StorePriceChangedPayload {
    store_id: string;
    item_id: string;
    item_name: string;
    sell_price: number;
    promo: boolean;
}

/** One agent-built structure as reported by the world snapshot (M14). */
export interface StructureSnapshot {
    /** Anchor cell of the blueprint (the cell the builder chose). */
    col: number;
    row: number;
    blueprint_id: string;
    owner_agent_id: string;
    status: 'building' | 'built';
    built_at: number | null;
}

/** Payload of the WS build_started event (M14). */
export interface BuildStartedPayload {
    agent_id: string;
    col: number;
    row: number;
    blueprint_id: string;
    duration_minutes: number;
    ends_at: number;
    materials: InventoryItem[];
    reason?: string | null;
}

/** Payload of the WS structure_built event (M14). */
export interface StructureBuiltPayload {
    agent_id: string;
    col: number;
    row: number;
    blueprint_id: string;
    owner_agent_id: string;
}

/** Payload of the WS structure_removed event (M14). */
export interface StructureRemovedPayload {
    col: number;
    row: number;
    blueprint_id: string;
    removed_by: string;
}

/** One blueprint entry in the catalog served at blueprints/blueprints.json (M14). */
export interface Blueprint {
    blueprint_id: string;
    name: string;
    /** Cell offsets [dcol, drow] relative to the structure anchor cell. */
    footprint: Cell[];
    /** Per-footprint-cell tile gids: "dcol,drow" → gids (tiny_farm tileset, firstGid 1). */
    tile_gids: Record<string, number[]>;
    blocking: boolean;
    /** Paving blueprints (R24) turn a non-walkable cell into a walkable one. */
    paving?: boolean;
    /** Materials required to build: item_id → quantity. */
    materials: Record<string, number>;
    duration_minutes: number;
    description: string;
}

/** Blueprint catalog JSON (M14, backend static mount, same base as maps). */
export interface BlueprintCatalog {
    version: string;
    blueprints: Blueprint[];
}

/** One crop definition in the catalog served at crops/crops.json (M15). */
export interface CropDefinition {
    seed_item_id: string;
    name: string;
    /** Growth stages: [minutes until this stage, tile gid]; 0-based index. */
    stages: [number, number][];
    yield: InventoryItem[];
    description: string;
}

/** Crop catalog JSON (M15, backend static mount, same base as maps). */
export interface CropCatalog {
    version: string;
    plant_radius: number;
    farm_field_id: string;
    crops: CropDefinition[];
}

export type WorldEventType =
    | 'world_snapshot'
    | 'world_time_changed'
    | 'world_paused'
    | 'world_resumed'
    | 'world_speed_changed'
    | 'agent_state_changed'
    | 'agent_move_started'
    | 'agent_move_completed'
    | 'agent_wait_started'
    | 'agent_wait_completed'
    | 'world_event_created'
    | 'conversation_message'
    | 'conversation_started'
    | 'conversation_ended'
    | 'agent_talked'
    | 'work_started'
    | 'work_completed'
    | 'item_purchased'
    | 'item_sold'
    | 'item_used'
    | 'money_changed'
    | 'inventory_changed'
    | 'needs_changed'
    | 'store_restocked'
    | 'store_price_changed'
    | 'memory_created'
    | 'relationship_changed'
    | 'daily_reflection'
    | 'god_action_applied'
    | 'weather_changed'
    | 'god_teleport'
    | 'item_spawned'
    | 'store_stock_changed'
    | 'stock_price_changed'
    | 'stock_bought'
    | 'stock_sold'
    | 'dividend_paid'
    | 'manager_profit_paid'
    | 'money_transferred'
    | 'item_given'
    | 'build_started'
    | 'structure_built'
    | 'structure_removed'
    | 'crop_planted'
    | 'crop_grown'
    | 'crop_harvested'
    | (string & {});

/** Uniform envelope wrapping every event (HTTP, WS, replay). */
export interface WorldEventEnvelope<TPayload = Record<string, unknown>> {
    event_id: string;
    sequence: number;
    world_id: string;
    world_time: number;
    type: WorldEventType;
    payload: TPayload;
    trace_id?: string | null;
}

/** Entry in the world list endpoint. */
export interface WorldListItem {
    world_id: string;
    name: string;
    world_time: number;
    speed: number;
    paused: boolean;
}

/** Response of an agent action POST (200 or 409). */
export interface ActionResponse {
    success: boolean;
    event?: WorldEventEnvelope;
    reason?: string;
}

/** Identity card of an agent (GET .../agents/{agent_id} → identity). */
export interface AgentIdentity {
    id: string;
    name: string;
    age: number;
    occupation: string;
    background: string;
    values: string[];
    long_term_goals: string[];
    speaking_style: string;
    personality: Record<string, unknown>;
}

/** Full agent detail (GET .../agents/{agent_id}): identity card + live state. */
export interface AgentDetail {
    agent_id: string;
    name: string;
    identity: AgentIdentity;
    col: number;
    row: number;
    location_id: string | null;
    satiety: number;
    energy: number;
    mood: number;
    loneliness: number;
    money: number;
    inventory: InventoryItem[];
    action: AgentAction;
    is_deciding: boolean;
    consecutive_failures: number;
}

/** One LLM decision record (GET .../decisions entry, recent first). */
export interface DecisionRecord {
    run_id: string;
    agent_id: string;
    world_id: string;
    world_time: number;
    model: string;
    input_tokens: number;
    output_tokens: number;
    latency_ms: number;
    tool_name: string;
    tool_arguments: Record<string, unknown>;
    tool_result: { success?: boolean; reason?: string; event?: WorldEventEnvelope };
    success: boolean;
    error_type: string | null;
    trace_id: string;
    raw_summary: string;
    created_at: string | null;
}

/** Body of POST .../god-actions. */
export interface GodActionRequest {
    command_type: string;
    target_id: string | null;
    parameters: Record<string, unknown>;
    reason: string;
}

/** Response of POST .../god-actions. */
export interface GodActionResult {
    command_id: string;
    success: boolean;
    result: object | null;
    events: WorldEventEnvelope[];
}

/** Payload of the WS god_action_applied event. */
export interface GodActionAppliedPayload {
    command_id: string;
    command_type: string;
    target_id: string | null;
    parameters: Record<string, unknown>;
    reason: string;
    result: object | null;
}

/** Payload of the WS weather_changed event. */
export interface WeatherChangedPayload {
    weather: string;
}

/** Payload of the WS god_teleport event. */
export interface GodTeleportPayload {
    agent_id: string;
    to: Cell;
    location_id: string;
    reason: string;
}

/** Payload of the WS item_spawned event. */
export interface ItemSpawnedPayload {
    agent_id: string;
    item_id: string;
    item_name: string;
    quantity: number;
}

/** Payload of the WS store_stock_changed event. */
export interface StoreStockChangedPayload {
    store_id: string;
    item_id: string;
    quantity: number;
}

/** One listed stock quote (GET .../stocks entry, M10). */
export interface StockItem {
    stock_id: string;
    name: string;
    price: number;
    prev_price: number;
    day_business: number;
    last_div_per_share: number;
    source: string;
    company_id: string;
}

/** One holding row (GET .../stocks → holdings, M10). */
export interface StockHolding {
    agent_id: string;
    stock_id: string;
    shares: number;
}

/** Response of GET .../stocks (M10): all quotes + all holdings. */
export interface StocksResponse {
    stocks: StockItem[];
    holdings: StockHolding[];
}

/** Payload of the WS stock_price_changed event (M10). */
export interface StockPriceChangedPayload {
    stock_id: string;
    stock_name: string;
    price: number;
    prev_price: number;
    day_business: number;
}

/** Payload of the WS stock_bought event (M10). */
export interface StockBoughtPayload {
    agent_id: string;
    stock_id: string;
    stock_name: string;
    shares: number;
    unit_price: number;
    total: number;
}

/** Payload of the WS stock_sold event (M10). */
export interface StockSoldPayload {
    agent_id: string;
    stock_id: string;
    stock_name: string;
    shares: number;
    unit_price: number;
    total: number;
}

/** Payload of the WS dividend_paid event (M10). */
export interface DividendPaidPayload {
    stock_id: string;
    stock_name: string;
    div_per_share: number;
    payouts: { agent_id: string; shares: number; amount: number }[];
}

/** Payload of the WS money_transferred event (M11). */
export interface MoneyTransferredPayload {
    from_agent_id: string;
    to_agent_id: string;
    amount: number;
    reason?: string | null;
}

/** Payload of the WS item_given event (M11). */
export interface ItemGivenPayload {
    from_agent_id: string;
    to_agent_id: string;
    item_id: string;
    item_name: string;
    quantity: number;
    reason?: string | null;
}

/** Readable event line pushed into the store's event stream. */
export interface WorldEventItem {
    sequence: number;
    worldTime: number;
    type: WorldEventType;
    text: string;
    agentId?: string | null;
    importance?: number;
}

/** One chat line inside a conversation (REST detail + WS conversation_message). */
export interface ConversationMessage {
    from_agent_id: string;
    to_agent_id: string;
    message: string;
    intent: string;
    sent_at: number;
}

/** One conversation of an agent (GET .../conversations response entry, newest first). */
export interface ConversationSummary {
    conversation_id: string;
    other_agent_id: string;
    started_at: number | null;
    ended_at: number | null;
    end_reason: string | null;
    messages: ConversationMessage[];
}

/** Payload of the WS conversation_started event. */
export interface ConversationStartedPayload {
    conversation_id: string;
    agent_ids: [string, string];
}

/** Payload of the WS conversation_message event. */
export interface ConversationMessagePayload {
    conversation_id: string;
    from_agent_id: string;
    to_agent_id: string;
    message: string;
    intent: string;
}

/** Payload of the WS conversation_ended event. */
export interface ConversationEndedPayload {
    conversation_id: string;
    reason: string;
}

/** Discriminated payload of the three conversation WS event types. */
export type ConversationEventPayload =
    | ConversationStartedPayload
    | ConversationMessagePayload
    | ConversationEndedPayload;

/** One memory of an agent (GET .../memories response entry, newest first). */
export interface MemoryItem {
    memory_id: string;
    /** working 工作记忆 | episodic 情节记忆 | semantic 语义记忆. */
    memory_type: string;
    text: string;
    importance: number;
    created_at: number;
    recall_count: number;
}

/** One relationship of an agent (GET .../relationships response entry). */
export interface RelationshipItem {
    source_agent_id: string;
    target_agent_id: string;
    target_name: string;
    familiarity: number;
    trust: number;
    affection: number;
    resentment: number;
    debt: number;
    updated_at: number;
}

/** Payload of the WS memory_created event. */
export interface MemoryCreatedPayload {
    agent_id: string;
    memory_id: string;
    memory_type: string;
    text: string;
    importance: number;
}

/** Payload of the WS relationship_changed event. */
export interface RelationshipChangedPayload {
    source_agent_id: string;
    target_agent_id: string;
    /** Signed deltas per axis (familiarity/trust/affection/resentment/debt). */
    deltas: Record<string, number>;
    /** Absolute values after applying the delta. */
    values: Record<string, number>;
}

/** Payload of the WS daily_reflection event. */
export interface DailyReflectionPayload {
    agent_id: string;
    summary: string;
}

/** One company (GET .../companies entry, M13). */
export interface CompanyInfo {
    company_id: string;
    name: string;
    company_type: string;
    location_id: string;
    manager_agent_id: string | null;
    money: number;
    status: string;
    employee_count: number;
    open_vacancies: number;
    unpaid_wage_total: number;
}

/** One position of a company (GET .../companies/{id}/positions entry, M13). */
export interface CompanyPosition {
    position_id: string;
    company_id: string;
    job_id: string;
    job_name: string;
    title: string;
    description: string;
    capacity: number;
    filled: number;
    vacancies: number;
    wage_per_shift: number;
    shift_start_minute: number;
    shift_end_minute: number;
    working_days: number[];
    status: string;
}

/** One active employment contract of a company (GET .../employees entry, M13). */
export interface CompanyEmployee {
    employment_id: string;
    company_id: string;
    position_id: string;
    job_id: string;
    agent_id: string;
    agent_name: string;
    status: string;
    hired_at: number;
    started_at: number;
    ended_at: number | null;
    wage_per_shift: number;
    attendance_score: number;
    performance_score: number;
    completed_shifts: number;
    late_shifts: number;
    absent_shifts: number;
    unpaid_wage: number;
    termination_reason: string | null;
}

/** One ledger row of a company (GET .../transactions entry, M13). */
export interface CompanyTransaction {
    transaction_id: string;
    company_id: string;
    type: string;
    amount: number;
    balance_after: number;
    related_agent_id: string | null;
    related_item_id: string | null;
    quantity: number | null;
    reference_type: string | null;
    reference_id: string | null;
    reason: string;
    world_time: number;
    trace_id: string;
}

/** One warehouse row of a company (GET .../inventory entry, M16). */
export interface CompanyInventoryItem {
    item_id: string;
    item_name: string;
    quantity: number;
    reserved_quantity: number;
    available_quantity: number;
}

/** One open job posting (GET .../job-openings entry, M13). */
export interface JobOpening {
    opening_id: string;
    company_id: string;
    company_name: string;
    position_id: string;
    title: string;
    description: string;
    location_id: string;
    vacancies: number;
    wage_per_shift: number;
    shift_start_minute: number;
    shift_end_minute: number;
}

/** Employment info of one agent (GET .../agents/{id}/employment, M13). */
export interface EmploymentInfo {
    employment_id: string;
    company_id: string;
    position_id: string;
    job_id: string;
    agent_id: string;
    status: string;
    hired_at: number;
    started_at: number;
    ended_at: number | null;
    wage_per_shift: number;
    attendance_score: number;
    performance_score: number;
    completed_shifts: number;
    late_shifts: number;
    absent_shifts: number;
    unpaid_wage: number;
    termination_reason: string | null;
}

/** One work shift of an agent (GET .../agents/{id}/shifts entry, M13). */
export interface WorkShiftInfo {
    shift_id: string;
    employment_id: string;
    company_id: string;
    position_id: string;
    agent_id: string;
    scheduled_start: number;
    scheduled_end: number;
    actual_start: number | null;
    actual_end: number | null;
    status: string;
    late_minutes: number;
    worked_minutes: number;
    wage_due: number;
    wage_paid: number;
    payroll_status: string;
    output_json: { item_id: string; quantity: number }[] | null;
    absence_reason: string | null;
}

/** Response of GET .../agents/{id}/employment (M13): contract + recent shifts. */
export interface AgentEmploymentResponse {
    employment: EmploymentInfo | null;
    shifts: WorkShiftInfo[];
}

/**
 * Latest shift of an agent as tracked from WS events (display-only, M13).
 * The backend serializes a formal shift as action_type "formal_work" which
 * is not part of the snapshot action, so the frontend keeps this light cache
 * to drive the map marker, next-shift hints and attendance stats.
 */
export interface AgentShiftInfo {
    shift_id: string;
    employment_id: string;
    company_id: string;
    position_id: string;
    agent_id: string;
    scheduled_start: number;
    scheduled_end: number;
    status: string;
    late_minutes?: number;
    worked_minutes?: number;
    products?: { item_id: string; quantity: number }[];
}
