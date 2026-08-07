# 架构 (architecture)

版本：1.0.0

## 1. 三大边界

```
┌──────────────┐    意图      ┌──────────────┐   事件     ┌──────────────┐
│    LLM 层     │ ──────────▶ │   世界引擎     │ ─────────▶ │    前端       │
│ 只出意图与话术 │  工具调用     │ 唯一真值来源    │  WorldEvent │ 只观察与展示   │
└──────────────┘             └──────────────┘            └──────────────┘
```

### LLM 负责（且只负责）

- 根据身份、需求、记忆选择行动；
- 决定去哪、和谁说话、说什么；
- 多目标权衡；根据失败结果调整下一步。

### LLM 不负责

- 修改数据库 / 扣钱 / 加物品 / 宣布工作完成 / 改他人状态 / 改世界时间；
- 判断路径是否可通行 / 商店是否开门 / 库存是否足够；
- 决定工具调用一定成功。

### 世界引擎负责

- 验证世界规则（world-rules.md）→ 事务修改数据库 → 产生 `WorldEvent` → WebSocket 推送。
- 引擎是权威状态；前端不做任何权威判断。

### 前端负责

- 渲染地图与智能体、播放动画、显示气泡/面板/事件流；
- 提交上帝操作、暂停/恢复/调速；
- **不保存权威状态**；WS 重连后以快照为准。

## 2. 模块划分（backend）

```
app/
├── main.py                 FastAPI 入口（lifespan 装配世界引擎）
├── api/                    HTTP + WebSocket 路由（薄层，只做参数与序列化）
├── agents/                 LLM 智能体：agent_factory / context / instructions / tools/
├── domain/                 纯领域模型（无 IO）：agent, world, location, item, job,
│                           memory, relationship, event
├── schemas/                Pydantic：actions / events / snapshots / websocket
├── services/               应用服务：decision / observation / action_execution /
│                           conversation / memory / relationship / god_action
├── world_engine/           时钟 clock / 调度器 scheduler / 事件总线 event_bus /
│                           引擎 engine / 锁 locks
├── repositories/           数据访问：agent / world / inventory / memory / event
├── database/               SQLAlchemy 2 session / models / unit_of_work
└── config/                 settings（pydantic-settings）
```

## 3. 请求/数据流（一次决策）

```
调度器(时间到)
  → 检查 is_deciding / next_decision_at / 世界未暂停 / 触发时机(M8)
  → 构建观察 observation_service（限长）
  → Runner.run（Agents SDK，tool_choice=required, parallel_tool_calls=False,
                单工具循环上限）
  → @function_tool 收到意图参数
  → ActionExecutionService.validate_and_execute（规则 + 事务）
  → 写 WorldEvent（带 trace_id, sequence）
  → 事件总线 → WS 推送 → 前端动画
  → 决策记录落库（model/tokens/latency/tool/result/success/error）
```

## 4. 事务与并发

- 世界状态变更一律在 `unit_of_work` 内完成（提交成功才算生效）。
- 购买等竞争操作使用行锁 + 事务重试（locks.py，见 R4）。
- 单个智能体不并发决策：`is_deciding` 标志 + `next_decision_at` 门控。

## 5. 事件协议

统一事件（详见 event-protocol.md）：

```json
{
  "event_id": "evt_001",
  "sequence": 81,
  "world_id": "world_001",
  "world_time": 510,
  "type": "agent_move_started",
  "payload": {},
  "trace_id": "trc_xxx"
}
```

## 6. 前端结构

```
frontend/src/
├── api/          REST 客户端（worlds/agents/events/god）
├── websocket/    WS 客户端：重连、sequence 去重、快照覆盖
├── stores/       Pinia：worldStore / agentStore / eventStore
├── pixi/         WorldRenderer / AssetLoader / TiledMapLoader /
│                 AgentSprite / MovementAnimator / CameraController / layers/
├── components/   身份卡、状态面板、事件流、对话气泡、上帝面板
├── views/        世界主界面
└── types/        TS 类型（与 backend schemas 对齐）
```

## 7. 时间模型

- 世界时间 = 游戏分钟整数；初始 `8 * 60`（08:00）。
- 真实时间 ≠ 游戏时间。时钟按 `speed`（1/2/5/10×）将真实秒转换为游戏分钟。
- 时钟推进由调度器驱动（每 tick 检查到期事件），不逐帧轮询所有角色。

## 8. 配置

- `backend/.env` → `app/config/settings.py`（pydantic-settings）： DATABASE_URL、CORS_ORIGINS、WORLD_MAP_PATH、LLM
  模型/密钥、并发上限、预算等。
- 地图与素材通过 `world_data/asset-manifest.json` 加载（不硬编码路径）。
