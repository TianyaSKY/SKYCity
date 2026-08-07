# 任务拆分清单

基于 `do_plan.md`，按「一条纵向链路」原则拆分为可执行任务。 每个里程碑都有「可运行、可观察、可验收」的成果。 阶段性任务顺序参考
do_plan.md 第九节：M0 → M1 → M2 → ... → M9。

---

## 里程碑 0：素材与地图规范

成果：Tiled 中可查看的小镇地图 + 素材清单。

- [x] T0-1 在 Tiled 中新建地图，规整 16×16 瓦片网格
- [x] T0-2 用 Tiny Farm 素材铺出基础地面 (`ground`, `ground_detail`)
- [x] T0-3 摆放建筑物 (`buildings`) 与装饰 (`decorations_low`, `foreground`)
- [x] T0-4 标记可通行区 (`navigation`) 与碰撞区 (`collision`)
- [x] T0-5 创建 `locations` 对象层：住宅、商店、农场、广场、工作地点（含 `location_id`/`name`/`location_type`/`capacity`/
  `open_hour`/`close_hour`）
- [x] T0-6 创建 `interactables` 对象层（如 `shop_counter`）
- [x] T0-7 创建 `spawn_points` 对象层（智能体出生点）
- [x] T0-8 导出独立 `.tsj` Tileset + `tiny_world.tmj`
- [x] T0-9 编写 `asset-manifest.json`（alias/file/tile 大小/许可证）
- [x] T0-10 编写 `docs/map-specification.md`（图层、对象层、命名规范）
- [x] T0-11 验收：导出的 JSON 能被程序解析，碰撞/可行走已标记，ID 稳定

---

## 里程碑 1：项目骨架与地图显示

成果：浏览器打开能看到并交互的小镇地图。

### 后端

- [x] T1-1 用 uv 初始化 backend 工程 (`pyproject.toml`)
- [x] T1-2 初始化 FastAPI + Limestack/启动入口 (`app/main.py`)
- [x] T1-3 初始化配置系统 (`app/config/settings.py` + `.env.example`)
- [x] T1-4 接入 Loguru 日志
- [x] T1-5 创建 `/health` 接口
- [x] T1-6 接入 SQLAlchemy 2 + SQLite (`app/database/session.py`)
- [x] T1-7 初始化 Alembic 迁移
- [x] T1-8 统一异常处理 + CORS
- [x] T1-9 创建世界配置加载器（读取 tmx/tmj + asset-manifest）

### 前端

- [x] T1-10 初始化 Vue 3 + Vite + TS (`frontend/package.json`)
- [x] T1-11 初始化 Pinia
- [x] T1-12 创建世界主界面 (`src/views`)
- [x] T1-13 初始化 PixiJS Application (`src/pixi/WorldRenderer.ts`)
- [x] T1-14 资源加载器 (`AssetLoader.ts`)
- [x] T1-15 解析 Tiled JSON (`TiledMapLoader.ts`)
- [x] T1-16 渲染瓦片图层
- [x] T1-17 整数倍缩放 + `nearest` 采样
- [x] T1-18 摄像机拖动/缩放 (`CameraController.ts`)
- [x] T1-19 点击地点、显示鼠标所在瓦片坐标
- [x] T0-20 前端连通后端 `/health`
- [x] T1-21 验收：显示小镇、拖动缩放、点击地点、看到瓦片坐标

---

## 里程碑 2：世界状态、时间与事件系统

成果：不依赖 LLM 的世界基础设施，运动闭环跑通。

- [x] T2-1 核心领域模型：`World`, `WorldClock`, `Location`, `AgentIdentity`, `AgentState`, `WorldEvent`,
  `ScheduledAction` (`app/domain/`)
- [x] T2-2 数据表：`worlds`, `agents`, `agent_states`, `locations`, `scheduled_actions`, `world_events`
  (`app/database/models/`)
- [x] T2-3 世界时钟（游戏分钟计，支持暂停/恢复/1×/2×/5×/10×） (`app/world_engine/clock.py`)
- [x] T2-4 离散事件调度器（按 `execute_at` 取事件） (`app/world_engine/scheduler.py`)
- [x] T2-5 事件总线 + 统一事件协议（`event_id`/`sequence`/`world_time`/`type`/`payload`） (`event_bus.py`)
- [x] T2-6 World 引擎装配 (`app/world_engine/engine.py`)
- [x] T2-7 第一批事件类型：`world_snapshot`, `world_time_changed`, `world_paused/resumed`, `agent_state_changed`,
  `agent_move_started/completed`, `world_event_created`
- [x] T2-8 Repository 层：agent/world/inventory/memory/event (`app/repositories/`)
- [x] T2-9 WebSocket 接口 `WS /ws/worlds/{world_id}`（连接先发快照再推增量） (`app/api/websocket.py`)
- [x] T2-10 Schema：actions/events/snapshots/websocket (`app/schemas/`)
- [x] T2-11 动作执行服务：验证世界规则后事务修改 (`action_execution_service.py`)
- [x] T2-12 世界 API：`POST/GET /api/worlds`、snapshot、pause、resume、speed (`app/api/worlds.py`)
- [x] T2-13 前端 WebSocket 客户端 + 断线重连 + 按 `sequence` 处理/检测遗漏
- [x] T2-14 前端按开始/结束位置播放移动动画 (`MovementAnimator.ts`)
- [x] T2-15 前端显示世界时间 + 事件流（`stores/`）
- [x] T2-16 验收：测试 API 造移动 → 后端排事件 → WS 推开始 → 平滑移动 → 完成更新位置 → 刷新后位置保持

---

## 里程碑 3：第一个真实 LLM 智能体

成果：一个智能体由 LLM 驱动自主移动/等待的纵向闭环。

- [x] T3-1 智能体身份卡配置数据 (`world_data/identities/`)
- [x] T3-2 智能体工厂 (`agent_factory.py`)
- [x] T3-3 上下文 `AgentToolContext`（world_id/agent_id/service，agent_id 服务端注入） (`context.py`)
- [x] T3-4 提示词模板 (`instructions.py`)
- [x] T3-5 首批工具 `move`, `wait`（用 `@function_tool`） (`tools/movement.py`)
- [x] T3-6 观察构建（时间/天气/状态/需求/地点/可见地点人物/可做事/记忆/上次工具结果） (`observation_service.py`)
- [x] T3-7 决策服务（一次 `Runner.run` + 最多一次有效行动；`tool_choice=required`、`parallel_tool_calls=False`、限制最大轮数）
  (`agent_decision_service.py`)
- [x] T3-8 LLM 运行记录表（agent_id/world_time/model/tokens/latency/tool/result/success/error）
- [x] T3-9 工具失败后下一次决策可调整
- [x] T3-10 LLM 故障自动降级为等待，不崩溃（`wait` 兜底 + 重试/退避）
- [x] T3-11 验收：智能体依身份状态选择移动/等待；不移动到不存在地点；地图同步动画；每次决策有记录

---

## 里程碑 4：多智能体与对话

成果：3~5 个智能体并行自主运行并相遇对话。

- [x] T4-1 新增 `talk` 工具（含 `intent: TalkIntent`） (`tools/conversation.py`)
- [x] T4-2 对话机制：验证距离 → `conversation_message` 事件 → 前端气泡 → 对方观察加入消息 → 提高对方决策优先级 →
  自行决定是否回应（不在工具内直接跑对方 LLM） (`conversation_service.py`)
- [x] T4-3 防无限对聊：同对冷却 / 会话最大轮数 / 需求衰减 / 目标优先 / 重复检测
- [x] T4-4 前端：对话气泡、历史面板、点击人物看最近交流、当前交谈对象高亮、起止状态
- [x] T4-5 调度器支持多个智能体并行决策（每智能体 `is_deciding`/`next_decision_at` 防并发）
- [x] T4-6 验收：只能和附近说话；对方记住消息并能回应/忽略/离开；不无限自循环；3~5 个角色并行自主

---

## 里程碑 5：工作、金钱和消费闭环

成果：资源驱动产生有意义选择的完整经济闭环。

- [x] T5-1 领域对象：`Item`, `Inventory`, `Store`, `StoreProduct`, `Job`, `Employment`, `Transaction`
- [x] T5-2 数据表：`items`, `inventories`, `inventory_items`, `stores`, `store_products`, `jobs`, `employments`,
  `transactions`
- [x] T5-3 物品/地点/工作/商店种子数据 (`world_data/`)
- [x] T5-4 新增工具 `buy_item`, `sell_item`, `work`, `use_item` (`tools/commerce.py`, `daily_life.py`)
- [x] T5-5 农场行为统一抽象为 `work(job_id)`（浇水/播种/收获/值班/送货/修栅栏）— 不为每个工种建独立工具
- [x] T5-6 经济闭环服务：工作领工资/产物 → 商店收购 → 购买食物 → 使用降饥饿 → 库存减少
- [x] T5-7 并发兜底：库存/余额数据库事务 + 锁（数件同买的场景） (`locks.py`, `unit_of_work.py`)
- [x] T5-8 验收：饥饿→去商店→钱不够→去工作→领工资→购食→使用→饥饿下降 的自主链

---

## 里程碑 6：记忆、关系与持续人格

成果：角色跨多次调用保持记忆与稳定人格。

- [x] T6-1 记忆领域模型：工作记忆 / 情节记忆 / 语义印象 (`domain/memory.py`, `memory_repository.py`)
- [x] T6-2 加权检索（实体+关键词+重要性+新近度+未解决度） (`memory_service.py`)
- [x] T6-3 记忆写入（只记观察到的信息，不记未观察秘密）
- [x] T6-4 关系模型（方向性：familiarity/trust/affection/resentment/debt） (`domain/relationship.py`)
- [x] T6-5 关系更新原则：系统按事件/意图算变化，LLM 只提供行为 (`relationship_service.py`)
- [x] T6-6 每日反思（独立调用、单独限频，产出当日总结/进展/关系/明日重点）
- [x] T6-7 前端：记忆面板、关系面板
- [x] T6-8 验收：提及真实事件；记住借钱/欺骗/帮助影响；重启后记忆仍在；多天运行保持人设与语言风格

---

## 里程碑 7：上帝视角与干预系统

成果：玩家可观察并干预世界的完整界面。

- [x] T7-1 观察 API：时间天气/地点人物/身份卡/需求目标行动/背包资金/关系/记忆/最近LLM调用/事件流 (`app/api/agents.py`,
  `events.py`)
- [x] T7-2 上帝命令结构：`command_id/type/target/parameters/reason` (`god_actions.py`, `schemas/`)
- [x] T7-3 上帝操作服务：暂停/恢复/调速/改天气/发扣钱/生成物品/召集/移动/公共事件/改商店库存 (`god_action_service.py`)
- [x] T7-4 所有干预：过 Service → 审计记录 → WorldEvent → 推前端 → 进角色观察/记忆
- [x] T7-5 前端主界面新布局：顶部时间/天气/速度/暂停；右侧身份卡/状态需求/记忆关系/LLM记录/上帝干预；底部事件流
- [x] T7-6 验收：不开日志即可知谁在哪、在做什么、行动成败、刚发生什么、干预后果

---

## 里程碑 8：稳定性、成本与可观测性

成果：可长时间稳定运行而不散架。

- [x] T8-1 全局信号量限制并发 LLM 请求数
- [x] T8-2 每智能体决策控制字段：`is_deciding`, `next_decision_at`, `last_decision_at`, `consecutive_failures`,
  `daily_token_usage`, `daily_call_count`
- [x] T8-3 严格 LLM 触发时机（行动完成/重要对话/计划打断/需求阈值/重要事件/定时重评）
- [x] T8-4 故障降级：超时重试一次 → 仍败 wait10~30分钟 → 连续败延长下次决策 → 恢复后正常
- [x] T8-5 成本控制：观察文本限长/少量记忆检索/静态说明缓存/经济模型跑普通行动/强模型跑反思/相同观察短时缓存/每智能体
  Token 统计/每世界预算/暂停后禁止新请求
- [x] T8-6 每决策 `trace_id` 贯穿：调度→观察→LLM→工具→Service→事务→WorldEvent→WS
- [x] T8-7 验收：单智能体不并发决策；超时不阻塞世界；工具不重复扣款；WS 重连拿最新快照；无永久卡死；可按智能体查次数/错误率/消耗；所有状态变化可追溯

---

## 里程碑 9：测试、存档与正式版本

成果：可稳定保存/恢复/演示的完整版本。

- [x] T9-1 单元测试：路径/余额/库存验证、工作奖励、物品使用、关系变化、记忆评分、事件调度、世界时钟
- [x] T9-2 集成测试：LLM工具→Service→DB、移动事件→WS、购买并发、对话投递、上帝审计、存档恢复
- [x] T9-3 LLM 测试：Mock Provider `FakeLLMProvider`（`tools` 假实现）；保留少量真实模型冒烟测试
- [x] T9-4 前端测试：地图初始化/WS更新Store/动画完成/身份卡切换/上帝提交/断线恢复/快照覆盖陈旧状态（Vitest + Playwright）
- [x] T9-5 存档实现：世界时间/天气/身份状态/背包/商店库存/关系/记忆/未完成行动/待执行事件/随机种子/配置版本；地图只记版本
- [x] T9-6 可重放事件：初始快照 + 按 sequence 的事件序列
- [x] T9-7 存档 API + 存档恢复测试
- [x] T9-8 最终验收：建世界→初始化 3~5 智能体→LLM 自主行动→多游戏日→移动/对话/工作/消费/关系变化→暂停干预→退出保存→重启恢复→查看完整事件与
  LLM 记录

---

## 里程碑 14：建造系统（M14）

成果：智能体用木材在地图上建造栅栏/建筑，环境真实改变且持久化。

契约：`docs/world-rules.md` R22（建造规则）、`docs/event-protocol.md` §3（build 事件）。

- [x] T14-1 blueprint 配置 + 加载器：`world_data/blueprints/`（footprint/tile_gids/blocking/materials/duration），资产清单注册
- [x] T14-2 `tile_structures` 表 + Alembic 迁移（world_id/col/row/blueprint_id/owner_agent_id/built_at，UNIQUE
  (world_id,col,row)）
- [x] T14-3 `build(col, row, blueprint_id, reason)` 工具 + 位置/材料/空闲校验（R22.1~22.3，材料预扣进「建造中」状态）
- [x] T14-4 连通性校验：effective_walkable 增量 BFS，所有 location 锚点 + spawn 点互相可达（R22.4）
- [x] T14-5 寻路接入 effective_walkable：阻塞格不可通行、起点被阻塞拒绝出发（R22.6）
- [x] T14-6 事件与持久化：`build_started`/`structure_built`/`structure_removed`，快照含 structures，存档含
  tile_structures（R17 扩展）
- [x] T14-7 前端动态结构层：Pixi 于 decorations_low 之上渲染，快照全量画、事件增量改
- [x] T14-8 上帝命令：`remove_structure`（+ `build_structure`），走审计 → 事件 → 记忆（R13）
- [x] T14-9 测试与验收：堵路被拒 / 材料不足 / 并发占格 / 上帝打断退材料 / 存档恢复结构仍在

---

## 里程碑 0 前置必读文档

写代码前先落地规则（do_plan.md 第八节要求），影响后续所有校验逻辑：

- [x] D-1 `docs/world-rules.md`
  ：一次能否做两件事、移动中能否对话、工作时可否打断、抢最后一个商品、暂停时已发请求、天气影响、无力欠款、关门可否进、对话距离、发工资时机、饥饿=100、精力=0、上帝命令是否违规则
- [x] D-2 `docs/architecture.md`：三大边界（LLM只出意图 / 引擎唯一真值 / 前端只观察）
- [x] D-3 `docs/agent-prompt.md`：提示词与工具约定
- [x] D-4 `docs/event-protocol.md`：既有事件协议文档化

---

## 里程碑 15：作物种植（M15）

成果：智能体买种子在农田种植，作物按世界时钟生长，成熟后收获卖钱——环境被持续改变。

契约：`docs/world-rules.md` R23（种植与收获）、`docs/event-protocol.md` §3（crop 事件）。

- [x] T15-1 契约文档：world-rules R23（种植区/占用/生长/收获规则）+ event-protocol `crop_planted`/`crop_grown`/
  `crop_harvested`
- [x] T15-2 `crops` 表 + 迁移 `m15_crops`（world_id/col/row/seed_item_id/planted_by/planted_at/stage/next_stage_at，PK=
  (world_id,col,row)）
- [x] T15-3 种子物品（wheat_seed/carrot_seed/strawberry_seed + 收获物 flower）+ 商店配置 + `world_data/crops/crops.json`
  加载器（阶段分钟/gid/产物）
- [x] T15-4 `plant(col, row, item_id, reason)` 工具 + 校验（R23：farm_field 种植区、格空闲（无作物/无结构物）、持有种子、空闲、距离≤3）
- [x] T15-5 生长调度：scheduler 回调逐阶段推进（plant 时调度 crop_grow，到点再调度下一段），`crop_grown` 事件
- [x] T15-6 `harvest(col, row, reason)` 工具 + 结算：最终阶段才可收，产物 + fertilizer yield_bonus 加成，清格
- [x] T15-7 事件/快照/存档：crops 进快照与 R17 存档（回调行随存档走，恢复后 load_due 续跑）
- [x] T15-8 前端 CropLayer：按阶段 gid 渲染（复用 StructureLayer 纹理矩形逻辑），store 状态 + 三事件
- [x] T15-9 上帝命令：`set_crop_stage` / `remove_crop`（审计 → 事件 → 记忆，R13）
- [x] T15-10 测试与验收：种→长→收闭环、肥料加成、作物/结构物互斥、存档恢复、fake provider 自主链路（买种→种→收→卖）+ M14
  建造自主链路冒烟

---

## 建议执行顺序（竖切而非横铺）

```
1. M0 地图  →  2. M1 地图显示  →  3. T2-16 手动移动闭环
4. M2 WS+事件 →  5. M3 第一个 LLM 智能体（move/wait）
6. M4 第二个智能体 + talk  →  7. M5 经济闭环
8. M6 记忆关系  →  9. M7 上帝界面  →  10. M8 稳定性  →  11. M9 测试存档
12. M14 建造系统（T14-1 → T14-9 竖切） →  13. M15 作物种植（T15-1 → T15-10 竖切）
```

优先打通第一条纵向链路（地图→状态→移动→WS→LLM→工具→验证→动画），再横向扩展。