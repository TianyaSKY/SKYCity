# 任务拆分清单

基于 `do_plan.md`，按「一条纵向链路」原则拆分为可执行任务。
每个里程碑都有「可运行、可观察、可验收」的成果。
阶段性任务顺序参考 do_plan.md 第九节：M0 → M1 → M2 → ... → M9。

---

## 里程碑 0：素材与地图规范

成果：Tiled 中可查看的小镇地图 + 素材清单。

- [ ] T0-1 在 Tiled 中新建地图，规整 16×16 瓦片网格
- [ ] T0-2 用 Tiny Farm 素材铺出基础地面 (`ground`, `ground_detail`)
- [ ] T0-3 摆放建筑物 (`buildings`) 与装饰 (`decorations_low`, `foreground`)
- [ ] T0-4 标记可通行区 (`navigation`) 与碰撞区 (`collision`)
- [ ] T0-5 创建 `locations` 对象层：住宅、商店、农场、广场、工作地点（含 `location_id`/`name`/`location_type`/`capacity`/`open_hour`/`close_hour`）
- [ ] T0-6 创建 `interactables` 对象层（如 `shop_counter`）
- [ ] T0-7 创建 `spawn_points` 对象层（智能体出生点）
- [ ] T0-8 导出独立 `.tsj` Tileset + `tiny_world.tmj`
- [ ] T0-9 编写 `asset-manifest.json`（alias/file/tile 大小/许可证）
- [ ] T0-10 编写 `docs/map-specification.md`（图层、对象层、命名规范）
- [ ] T0-11 验收：导出的 JSON 能被程序解析，碰撞/可行走已标记，ID 稳定

---

## 里程碑 1：项目骨架与地图显示

成果：浏览器打开能看到并交互的小镇地图。

### 后端
- [ ] T1-1 用 uv 初始化 backend 工程 (`pyproject.toml`)
- [ ] T1-2 初始化 FastAPI + Limestack/启动入口 (`app/main.py`)
- [ ] T1-3 初始化配置系统 (`app/config/settings.py` + `.env.example`)
- [ ] T1-4 接入 Loguru 日志
- [ ] T1-5 创建 `/health` 接口
- [ ] T1-6 接入 SQLAlchemy 2 + SQLite (`app/database/session.py`)
- [ ] T1-7 初始化 Alembic 迁移
- [ ] T1-8 统一异常处理 + CORS
- [ ] T1-9 创建世界配置加载器（读取 tmx/tmj + asset-manifest）

### 前端
- [ ] T1-10 初始化 Vue 3 + Vite + TS (`frontend/package.json`)
- [ ] T1-11 初始化 Pinia
- [ ] T1-12 创建世界主界面 (`src/views`)
- [ ] T1-13 初始化 PixiJS Application (`src/pixi/WorldRenderer.ts`)
- [ ] T1-14 资源加载器 (`AssetLoader.ts`)
- [ ] T1-15 解析 Tiled JSON (`TiledMapLoader.ts`)
- [ ] T1-16 渲染瓦片图层
- [ ] T1-17 整数倍缩放 + `nearest` 采样
- [ ] T1-18 摄像机拖动/缩放 (`CameraController.ts`)
- [ ] T1-19 点击地点、显示鼠标所在瓦片坐标
- [ ] T0-20 前端连通后端 `/health`
- [ ] T1-21 验收：显示小镇、拖动缩放、点击地点、看到瓦片坐标

---

## 里程碑 2：世界状态、时间与事件系统

成果：不依赖 LLM 的世界基础设施，运动闭环跑通。

- [ ] T2-1 核心领域模型：`World`, `WorldClock`, `Location`, `AgentIdentity`, `AgentState`, `WorldEvent`, `ScheduledAction` (`app/domain/`)
- [ ] T2-2 数据表：`worlds`, `agents`, `agent_states`, `locations`, `scheduled_actions`, `world_events` (`app/database/models/`)
- [ ] T2-3 世界时钟（游戏分钟计，支持暂停/恢复/1×/2×/5×/10×）(`app/world_engine/clock.py`)
- [ ] T2-4 离散事件调度器（按 `execute_at` 取事件）(`app/world_engine/scheduler.py`)
- [ ] T2-5 事件总线 + 统一事件协议（`event_id`/`sequence`/`world_time`/`type`/`payload`）(`event_bus.py`)
- [ ] T2-6 World 引擎装配 (`app/world_engine/engine.py`)
- [ ] T2-7 第一批事件类型：`world_snapshot`, `world_time_changed`, `world_paused/resumed`, `agent_state_changed`, `agent_move_started/completed`, `world_event_created`
- [ ] T2-8 Repository 层：agent/world/inventory/memory/event (`app/repositories/`)
- [ ] T2-9 WebSocket 接口 `WS /ws/worlds/{world_id}`（连接先发快照再推增量）(`app/api/websocket.py`)
- [ ] T2-10 Schema：actions/events/snapshots/websocket (`app/schemas/`)
- [ ] T2-11 动作执行服务：验证世界规则后事务修改 (`action_execution_service.py`)
- [ ] T2-12 世界 API：`POST/GET /api/worlds`、snapshot、pause、resume、speed (`app/api/worlds.py`)
- [ ] T2-13 前端 WebSocket 客户端 + 断线重连 + 按 `sequence` 处理/检测遗漏
- [ ] T2-14 前端按开始/结束位置播放移动动画 (`MovementAnimator.ts`)
- [ ] T2-15 前端显示世界时间 + 事件流（`stores/`）
- [ ] T2-16 验收：测试 API 造移动 → 后端排事件 → WS 推开始 → 平滑移动 → 完成更新位置 → 刷新后位置保持

---

## 里程碑 3：第一个真实 LLM 智能体

成果：一个智能体由 LLM 驱动自主移动/等待的纵向闭环。

- [ ] T3-1 智能体身份卡配置数据 (`world_data/identities/`)
- [ ] T3-2 智能体工厂 (`agent_factory.py`)
- [ ] T3-3 上下文 `AgentToolContext`（world_id/agent_id/service，agent_id 服务端注入）(`context.py`)
- [ ] T3-4 提示词模板 (`instructions.py`)
- [ ] T3-5 首批工具 `move`, `wait`（用 `@function_tool`）(`tools/movement.py`)
- [ ] T3-6 观察构建（时间/天气/状态/需求/地点/可见地点人物/可做事/记忆/上次工具结果）(`observation_service.py`)
- [ ] T3-7 决策服务（一次 `Runner.run` + 最多一次有效行动；`tool_choice=required`、`parallel_tool_calls=False`、限制最大轮数）(`agent_decision_service.py`)
- [ ] T3-8 LLM 运行记录表（agent_id/world_time/model/tokens/latency/tool/result/success/error）
- [ ] T3-9 工具失败后下一次决策可调整
- [ ] T3-10 LLM 故障自动降级为等待，不崩溃（`wait` 兜底 + 重试/退避）
- [ ] T3-11 验收：智能体依身份状态选择移动/等待；不移动到不存在地点；地图同步动画；每次决策有记录

---

## 里程碑 4：多智能体与对话

成果：3~5 个智能体并行自主运行并相遇对话。

- [ ] T4-1 新增 `talk` 工具（含 `intent: TalkIntent`）(`tools/conversation.py`)
- [ ] T4-2 对话机制：验证距离 → `conversation_message` 事件 → 前端气泡 → 对方观察加入消息 → 提高对方决策优先级 → 自行决定是否回应（不在工具内直接跑对方 LLM）(`conversation_service.py`)
- [ ] T4-3 防无限对聊：同对冷却 / 会话最大轮数 / 需求衰减 / 目标优先 / 重复检测
- [ ] T4-4 前端：对话气泡、历史面板、点击人物看最近交流、当前交谈对象高亮、起止状态
- [ ] T4-5 调度器支持多个智能体并行决策（每智能体 `is_deciding`/`next_decision_at` 防并发）
- [ ] T4-6 验收：只能和附近说话；对方记住消息并能回应/忽略/离开；不无限自循环；3~5 个角色并行自主

---

## 里程碑 5：工作、金钱和消费闭环

成果：资源驱动产生有意义选择的完整经济闭环。

- [ ] T5-1 领域对象：`Item`, `Inventory`, `Store`, `StoreProduct`, `Job`, `Employment`, `Transaction`
- [ ] T5-2 数据表：`items`, `inventories`, `inventory_items`, `stores`, `store_products`, `jobs`, `employments`, `transactions`
- [ ] T5-3 物品/地点/工作/商店种子数据 (`world_data/`)
- [ ] T5-4 新增工具 `buy_item`, `sell_item`, `work`, `use_item` (`tools/commerce.py`, `daily_life.py`)
- [ ] T5-5 农场行为统一抽象为 `work(job_id)`（浇水/播种/收获/值班/送货/修栅栏）— 不为每个工种建独立工具
- [ ] T5-6 经济闭环服务：工作领工资/产物 → 商店收购 → 购买食物 → 使用降饥饿 → 库存减少
- [ ] T5-7 并发兜底：库存/余额数据库事务 + 锁（数件同买的场景）(`locks.py`, `unit_of_work.py`)
- [ ] T5-8 验收：饥饿→去商店→钱不够→去工作→领工资→购食→使用→饥饿下降 的自主链

---

## 里程碑 6：记忆、关系与持续人格

成果：角色跨多次调用保持记忆与稳定人格。

- [ ] T6-1 记忆领域模型：工作记忆 / 情节记忆 / 语义印象 (`domain/memory.py`, `memory_repository.py`)
- [ ] T6-2 加权检索（实体+关键词+重要性+新近度+未解决度）(`memory_service.py`)
- [ ] T6-3 记忆写入（只记观察到的信息，不记未观察秘密）
- [ ] T6-4 关系模型（方向性：familiarity/trust/affection/resentment/debt）(`domain/relationship.py`)
- [ ] T6-5 关系更新原则：系统按事件/意图算变化，LLM 只提供行为 (`relationship_service.py`)
- [ ] T6-6 每日反思（独立调用、单独限频，产出当日总结/进展/关系/明日重点）
- [ ] T6-7 前端：记忆面板、关系面板
- [ ] T6-8 验收：提及真实事件；记住借钱/欺骗/帮助影响；重启后记忆仍在；多天运行保持人设与语言风格

---

## 里程碑 7：上帝视角与干预系统

成果：玩家可观察并干预世界的完整界面。

- [ ] T7-1 观察 API：时间天气/地点人物/身份卡/需求目标行动/背包资金/关系/记忆/最近LLM调用/事件流 (`app/api/agents.py`, `events.py`)
- [ ] T7-2 上帝命令结构：`command_id/type/target/parameters/reason` (`god_actions.py`, `schemas/`)
- [ ] T7-3 上帝操作服务：暂停/恢复/调速/改天气/发扣钱/生成物品/召集/移动/公共事件/改商店库存 (`god_action_service.py`)
- [ ] T7-4 所有干预：过 Service → 审计记录 → WorldEvent → 推前端 → 进角色观察/记忆
- [ ] T7-5 前端主界面新布局：顶部时间/天气/速度/暂停；右侧身份卡/状态需求/记忆关系/LLM记录/上帝干预；底部事件流
- [ ] T7-6 验收：不开日志即可知谁在哪、在做什么、行动成败、刚发生什么、干预后果

---

## 里程碑 8：稳定性、成本与可观测性

成果：可长时间稳定运行而不散架。

- [ ] T8-1 全局信号量限制并发 LLM 请求数
- [ ] T8-2 每智能体决策控制字段：`is_deciding`, `next_decision_at`, `last_decision_at`, `consecutive_failures`, `daily_token_usage`, `daily_call_count`
- [ ] T8-3 严格 LLM 触发时机（行动完成/重要对话/计划打断/需求阈值/重要事件/定时重评）
- [ ] T8-4 故障降级：超时重试一次 → 仍败 wait10~30分钟 → 连续败延长下次决策 → 恢复后正常
- [ ] T8-5 成本控制：观察文本限长/少量记忆检索/静态说明缓存/经济模型跑普通行动/强模型跑反思/相同观察短时缓存/每智能体 Token 统计/每世界预算/暂停后禁止新请求
- [ ] T8-6 每决策 `trace_id` 贯穿：调度→观察→LLM→工具→Service→事务→WorldEvent→WS
- [ ] T8-7 验收：单智能体不并发决策；超时不阻塞世界；工具不重复扣款；WS 重连拿最新快照；无永久卡死；可按智能体查次数/错误率/消耗；所有状态变化可追溯

---

## 里程碑 9：测试、存档与正式版本

成果：可稳定保存/恢复/演示的完整版本。

- [ ] T9-1 单元测试：路径/余额/库存验证、工作奖励、物品使用、关系变化、记忆评分、事件调度、世界时钟
- [ ] T9-2 集成测试：LLM工具→Service→DB、移动事件→WS、购买并发、对话投递、上帝审计、存档恢复
- [ ] T9-3 LLM 测试：Mock Provider `FakeLLMProvider`（`tools` 假实现）；保留少量真实模型冒烟测试
- [ ] T9-4 前端测试：地图初始化/WS更新Store/动画完成/身份卡切换/上帝提交/断线恢复/快照覆盖陈旧状态（Vitest + Playwright）
- [ ] T9-5 存档实现：世界时间/天气/身份状态/背包/商店库存/关系/记忆/未完成行动/待执行事件/随机种子/配置版本；地图只记版本
- [ ] T9-6 可重放事件：初始快照 + 按 sequence 的事件序列
- [ ] T9-7 存档 API + 存档恢复测试
- [ ] T9-8 最终验收：建世界→初始化 3~5 智能体→LLM 自主行动→多游戏日→移动/对话/工作/消费/关系变化→暂停干预→退出保存→重启恢复→查看完整事件与 LLM 记录

---

## 里程碑 0 前置必读文档

写代码前先落地规则（do_plan.md 第八节要求），影响后续所有校验逻辑：

- [ ] D-1 `docs/world-rules.md`：一次能否做两件事、移动中能否对话、工作时可否打断、抢最后一个商品、暂停时已发请求、天气影响、无力欠款、关门可否进、对话距离、发工资时机、饥饿=100、精力=0、上帝命令是否违规则
- [ ] D-2 `docs/architecture.md`：三大边界（LLM只出意图 / 引擎唯一真值 / 前端只观察）
- [ ] D-3 `docs/agent-prompt.md`：提示词与工具约定
- [ ] D-4 `docs/event-protocol.md`：既有事件协议文档化

---

## 建议执行顺序（竖切而非横铺）

```
1. M0 地图  →  2. M1 地图显示  →  3. T2-16 手动移动闭环
4. M2 WS+事件 →  5. M3 第一个 LLM 智能体（move/wait）
6. M4 第二个智能体 + talk  →  7. M5 经济闭环
8. M6 记忆关系  →  9. M7 上帝界面  →  10. M8 稳定性  →  11. M9 测试存档
```

优先打通第一条纵向链路（地图→状态→移动→WS→LLM→工具→验证→动画），再横向扩展。