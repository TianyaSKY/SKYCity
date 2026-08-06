# 事件协议 (event-protocol)

版本：1.0.0

## 1. 统一事件信封

所有事件（HTTP 返回、WebSocket 推送、存档重放）使用同一信封：

```json
{
  "event_id": "evt_000081",
  "sequence": 81,
  "world_id": "world_001",
  "world_time": 510,
  "type": "agent_move_started",
  "payload": {},
  "trace_id": "trc_000001"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | string | 全局唯一，单调（`evt_` + 6 位序号） |
| `sequence` | int | 世界内单调递增序号（从 1 开始，永不复用） |
| `world_id` | string | 世界 ID |
| `world_time` | int | 事件发生时世界时间（游戏分钟） |
| `type` | string | 事件类型（见 §3） |
| `payload` | object | 类型相关的载荷 |
| `trace_id` | string | 溯源 ID（M8）：调度→观察→LLM→工具→Service→事务→事件→WS 同值 |

## 2. 排序与去重

- 消费者按 `sequence` 升序处理；`world_time` 相同也以 sequence 定序。
- 前端记录已处理的最大 sequence；收到 ≤ 已处理的 → 丢弃（重放去重）；
  收到跳跃（gap）→ 触发全量快照重拉。
- WebSocket 连接建立后**先发完整快照**（type=`world_snapshot`），再推增量。

## 3. 事件类型（第一批）

| type | 载荷要点 | 触发方 |
|---|---|---|
| `world_snapshot` | 完整世界状态（agents/clock/weather/locations） | 连接建立/重连 |
| `world_time_changed` | `{world_time}` | 时钟推进 |
| `world_paused` | `{reason?}` | 暂停 |
| `world_resumed` | `{}` | 恢复 |
| `world_speed_changed` | `{speed}` | 调速 |
| `agent_state_changed` | `{agent_id, state: {...}}` | 状态/需求变化 |
| `agent_move_started` | `{agent_id, from: [c,r], to: [c,r], duration_minutes, speed_multiplier}` | 移动开始 |
| `agent_move_completed` | `{agent_id, at: [c,r]}` | 移动完成 |
| `agent_wait_started` | `{agent_id, minutes, reason}` | 等待开始 |
| `agent_wait_completed` | `{agent_id}` | 等待结束 |
| `world_event_created` | `{agent_id?, text, importance}` | 世界内叙事事件 |
| `conversation_message` | `{from_agent_id, to_agent_id, message, intent}` | 对话消息 |
| `conversation_started` | `{a, b}` | 会话开始 |
| `conversation_ended` | `{a, b, reason}` | 会话结束 |
| `agent_talked` | `{from_agent_id, message}` | 气泡显示 |
| `work_started` | `{agent_id, job_id, job_name, duration_minutes, ends_at, reason}` | 工作开始（R3 不可打断） |
| `work_completed` | `{agent_id, job_id, job_name, wage, products: [{item_id, quantity}], energy_spent}` | 工作完成结算（工资+产物进背包，R10） |
| `item_purchased` | `{agent_id, item_id, item_name, quantity, unit_price, total}` | 购买商品（R4/R7） |
| `item_sold` | `{agent_id, item_id, item_name, quantity, unit_price, total}` | 出售商品 |
| `item_used` | `{agent_id, item_id, item_name, satiety_before, satiety_after, mood_before, mood_after}` | 使用（食用）食物 / 心情物品（M12 追加 mood 字段） |
| `money_changed` | `{agent_id, amount, balance, reason}` | 金钱变动（amount 带符号） |
| `inventory_changed` | `{agent_id, items: [{item_id, quantity}]}` | 背包变化（完整列表） |
| `needs_changed` | `{agent_id, satiety, energy, mood}` | 需求变化（每小时节奏 R14 / 进食后，M12 追加 mood） |
| `store_restocked` | `{store_id, restocked: [{item_id, quantity}]}` | 商店开门补货（R15） |
| `store_price_changed` | `{store_id, item_id, item_name, sell_price, promo}` | 商店促销/恢复原价（M12，随每日补货结算） |
| `memory_created` | `{agent_id, memory_id, memory_type, text, importance}` | 新记忆写入（working/episodic/semantic，M6） |
| `relationship_changed` | `{source_agent_id, target_agent_id, deltas: {familiarity, trust, affection, resentment, debt}, values: {...}}` | 关系数值变化（系统计算，M6；deltas 为本次增量，values 为钳制后的新值） |
| `daily_reflection` | `{agent_id, summary}` | 每日反思完成（23:30 游戏时间，M6） |

| `god_action_applied` | `{command_id, command_type, target_id, parameters, reason, result}` | 神谕指令已应用（M7；每个 god-action 的第一个事件，target_id 为受影响的智能体，无目标时为 null） |
| `weather_changed` | `{weather}` | 天气变化（clear/cloudy/rain/snow，M7） |
| `god_teleport` | `{agent_id, to: [col,row], location_id, reason}` | 神谕传送（M7；取消当前行动并落地到地点锚点格） |
| `item_spawned` | `{agent_id, item_id, item_name, quantity}` | 神谕赐物（M7；仅 god spawn，与 `inventory_changed` 同发） |
| `store_stock_changed` | `{store_id, item_id, quantity}` | 商店库存被神谕设定为绝对值（M7） |
| `stock_price_changed` | `{stock_id, stock_name, price, prev_price, day_business}` | 每小时行情（含经营统计；价格未变也发，M10） |
| `stock_bought` | `{agent_id, stock_id, stock_name, shares, unit_price, total}` | 买入股票（随 `money_changed`，M10） |
| `stock_sold` | `{agent_id, stock_id, stock_name, shares, unit_price, total}` | 卖出股票（随 `money_changed`，M10） |
| `dividend_paid` | `{stock_id, stock_name, div_per_share, payouts: [{agent_id, shares, amount}]}` | 每日分红（M10；金额经 `money_changed` 逐人到账） |
| `money_transferred` | `{from_agent_id, to_agent_id, amount, reason}` | 智能体间转账(M11;双方余额经各自的 `money_changed` 到账) |
| `item_given` | `{from_agent_id, to_agent_id, item_id, item_name, quantity, reason}` | 智能体间赠物(M11;双方背包经各自的 `inventory_changed` 到账) |

（M5 追加：`work_started` / `work_completed` / `item_purchased` / `item_sold` /
`item_used` / `money_changed` / `inventory_changed` / `needs_changed` /
`store_restocked`；M6 追加：`memory_created` /
`relationship_changed` / `daily_reflection`；M7 追加：`god_action_applied` /
`weather_changed` / `god_teleport` / `item_spawned` / `store_stock_changed`；
M9 追加：`world_saved` / `world_restored`；M10 追加：`stock_price_changed` /
`stock_bought` / `stock_sold` / `dividend_paid`；M11 追加：`money_transferred` / `item_given`。）

说明（M7）：神谕发钱/扣款复用 `money_changed`（`{agent_id, amount, balance,
reason}`，amount 带符号），公开事件复用 `world_event_created`（无
`agent_id` 即公开：所有智能体各记一条 episodic 0.6 记忆），不新增事件类型。

M12 追加：`store_price_changed`；`needs_changed` / `item_used` 载荷扩展
mood 字段。

## 4. 事件持久化

- 所有事件写入 `world_events` 表（可追溯、可重放）。
- 存档 = 初始快照 + 按 sequence 的事件序列（重放不依赖 LLM）。
- `GET /api/worlds/{id}/events?after_sequence=N` 用于补齐遗漏。

## 5. 事件产生规则

- 一切世界状态变更必须产生事件（trace 可溯）。
- 同一事务内产生的事件共享 `trace_id` 与 `world_time`，sequence 递增。
- 前端仅消费事件渲染，不反向推导状态。
