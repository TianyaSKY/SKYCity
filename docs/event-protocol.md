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

| 字段         | 类型   | 说明                                                        |
|--------------|--------|-------------------------------------------------------------|
| `event_id`   | string | 全局唯一，单调（`evt_` + 6 位序号）                         |
| `sequence`   | int    | 世界内单调递增序号（从 1 开始，永不复用）                   |
| `world_id`   | string | 世界 ID                                                     |
| `world_time` | int    | 事件发生时世界时间（游戏分钟）                              |
| `type`       | string | 事件类型（见 §3）                                           |
| `payload`    | object | 类型相关的载荷                                              |
| `trace_id`   | string | 溯源 ID（M8）：调度→观察→LLM→工具→Service→事务→事件→WS 同值 |

## 2. 排序与去重

- 消费者按 `sequence` 升序处理；`world_time` 相同也以 sequence 定序。
- 前端记录已处理的最大 sequence；收到 ≤ 已处理的 → 丢弃（重放去重）； 收到跳跃（gap）→ 触发全量快照重拉。
- WebSocket 连接建立后 **先发完整快照**（type=`world_snapshot`），再推增量。

## 3. 事件类型（第一批）

| type                    | 载荷要点                                                                                                       | 触发方                                                                                 |
|-------------------------|----------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| `world_snapshot`        | 完整世界状态（agents/clock/weather/locations/structures）                                                      | 连接建立/重连                                                                          |
| `world_time_changed`    | `{world_time}`                                                                                                 | 时钟推进                                                                               |
| `world_paused`          | `{reason?}`                                                                                                    | 暂停                                                                                   |
| `world_resumed`         | `{}`                                                                                                           | 恢复                                                                                   |
| `world_speed_changed`   | `{speed}`                                                                                                      | 调速                                                                                   |
| `agent_state_changed`   | `{agent_id, state: {...}}`                                                                                     | 状态/需求变化                                                                          |
| `agent_move_started`    | `{agent_id, from: [c,r], to: [c,r], duration_minutes, speed_multiplier}`                                       | 移动开始                                                                               |
| `agent_move_completed`  | `{agent_id, at: [c,r]}`                                                                                        | 移动完成                                                                               |
| `agent_wait_started`    | `{agent_id, minutes, reason}`                                                                                  | 等待开始                                                                               |
| `agent_wait_completed`  | `{agent_id}`                                                                                                   | 等待结束                                                                               |
| `agent_sleep_started`   | `{agent_id, minutes, ends_at, reason, place, fee}`                                                             | 睡觉开始（R14：有家在家、无家在旅店；`fee`=旅店房费，家睡为 0）                        |
| `agent_sleep_completed` | `{agent_id, at: [c,r]}`                                                                                        | 睡觉结束                                                                               |
| `world_event_created`   | `{agent_id?, text, importance}`                                                                                | 世界内叙事事件                                                                         |
| `conversation_message`  | `{from_agent_id, to_agent_id, message, intent}`                                                                | 对话消息                                                                               |
| `conversation_started`  | `{a, b}`                                                                                                       | 会话开始                                                                               |
| `conversation_ended`    | `{a, b, reason}`                                                                                               | 会话结束                                                                               |
| `agent_talked`          | `{from_agent_id, message}`                                                                                     | 气泡显示                                                                               |
| `work_started`          | `{agent_id, job_id, job_name, duration_minutes, ends_at, reason}`                                              | 工作开始（R3 不可打断）                                                                |
| `work_completed`        | `{agent_id, job_id, job_name, wage, products: [{item_id, quantity}], energy_spent}`                            | 工作完成结算（工资+产物进背包，R10）                                                   |
| `item_purchased`        | `{agent_id, item_id, item_name, quantity, unit_price, total}`                                                  | 购买商品（R4/R7）                                                                      |
| `item_sold`             | `{agent_id, item_id, item_name, quantity, unit_price, total}`                                                  | 出售商品                                                                               |
| `item_used`             | `{agent_id, item_id, item_name, satiety_before, satiety_after, mood_before, mood_after}`                       | 使用（食用）食物 / 心情物品（M12 追加 mood 字段）                                      |
| `money_changed`         | `{agent_id, amount, balance, reason}`                                                                          | 金钱变动（amount 带符号）                                                              |
| `inventory_changed`     | `{agent_id, items: [{item_id, quantity}]}`                                                                     | 背包变化（完整列表）                                                                   |
| `needs_changed`         | `{agent_id, satiety, energy, mood, loneliness}`                                                                | 需求变化（每小时节奏 R14 / 进食后 / 对话缓解孤单，M12 追加 mood，R21 追加 loneliness） |
| `store_restocked`       | `{store_id, restocked: [{item_id, quantity}]}`                                                                 | 商店开门补货（R15）                                                                    |
| `store_price_changed`   | `{store_id, item_id, item_name, sell_price, promo}`                                                            | 商店促销/恢复原价（M12，随每日补货结算）                                               |
| `memory_created`        | `{agent_id, memory_id, memory_type, text, importance}`                                                         | 新记忆写入（working/episodic/semantic，M6）                                            |
| `relationship_changed`  | `{source_agent_id, target_agent_id, deltas: {familiarity, trust, affection, resentment, debt}, values: {...}}` | 关系数值变化（系统计算，M6；deltas 为本次增量，values 为钳制后的新值）                 |
| `daily_reflection`      | `{agent_id, summary}`                                                                                          | 每日反思完成（23:30 游戏时间，M6）                                                     |

| `god_action_applied` | `{command_id, command_type, target_id, parameters, reason, result}` | 神谕指令已应用（M7；每个
god-action 的第一个事件，target_id 为受影响的智能体，无目标时为 null） | | `weather_changed` | `{weather}` |
天气变化（clear/cloudy/rain/snow，M7） | | `god_teleport` | `{agent_id, to: [col,row], location_id, reason}` |
神谕传送（M7；取消当前行动并落地到地点锚点格） | | `item_spawned` | `{agent_id, item_id, item_name, quantity}` | 神谕赐物（M7；仅
god spawn，与 `inventory_changed` 同发） | | `store_stock_changed` | `{store_id, item_id, quantity}` |
商店库存被神谕设定为绝对值（M7） | | `stock_price_changed` | `{stock_id, stock_name, price, prev_price, day_business}` |
每小时行情（含经营统计；价格未变也发，M10） | | `stock_bought` |
`{agent_id, stock_id, stock_name, shares, unit_price, total}` | 买入股票（随 `money_changed`，M10） | | `stock_sold` |
`{agent_id, stock_id, stock_name, shares, unit_price, total}` | 卖出股票（随 `money_changed`，M10） | | `dividend_paid` |
`{stock_id, stock_name, div_per_share, payouts: [{agent_id, shares, amount}]}` | 每日分红（M10；金额经 `money_changed`
逐人到账） | | `money_transferred` | `{from_agent_id, to_agent_id, amount, reason}` | 智能体间转账 (M11;双方余额经各自的
`money_changed` 到账) | | `item_given` | `{from_agent_id, to_agent_id, item_id, item_name, quantity, reason}` | 智能体间赠物
(M11;双方背包经各自的 `inventory_changed` 到账) | | `build_started` |
`{agent_id, col, row, blueprint_id, duration_minutes, ends_at, materials: [{item_id, quantity}]}` |
建造开始，材料已预扣（R22.2） | | `structure_built` | `{agent_id, col, row, blueprint_id, owner_agent_id}` |
建造完成落格（R22.5） | | `structure_removed` | `{col, row, blueprint_id, removed_by}` | 结构被移除（仅上帝，R13 管道） | |
`crop_planted` | `{agent_id, col, row, item_id, item_name, stage, next_stage_at}` | 播种完成，种子已扣（R23.4） | |
`crop_grown` | `{col, row, item_id, stage}` | 作物进入下一生长阶段（R23.5） | | `crop_harvested` |
`{agent_id, col, row, item_id, item_name, products: [{item_id, quantity}]}` | 收获完成，产物进背包、清格（R23.6） |

（M5 追加：`work_started` / `work_completed` / `item_purchased` / `item_sold` /
`item_used` / `money_changed` / `inventory_changed` / `needs_changed` /
`store_restocked`；M6 追加：`memory_created` /
`relationship_changed` / `daily_reflection`；M7 追加：`god_action_applied` /
`weather_changed` / `god_teleport` / `item_spawned` / `store_stock_changed`； M9 追加：`world_saved` / `world_restored`
；M10 追加：`stock_price_changed` /
`stock_bought` / `stock_sold` / `dividend_paid`；M11 追加：`money_transferred` / `item_given`。）

说明（M7）：神谕发钱/扣款复用 `money_changed`（`{agent_id, amount, balance,
reason}`，amount 带符号），公开事件复用 `world_event_created`（无
`agent_id` 即公开：所有智能体各记一条 episodic 0.6 记忆），不新增事件类型。

M12 追加：`store_price_changed`；`needs_changed` / `item_used` 载荷扩展 mood 字段。

M14 追加：`build_started` / `structure_built` / `structure_removed`；
`world_snapshot` 载荷扩展 structures 列表。

M15 追加：`crop_planted` / `crop_grown` / `crop_harvested`；
`world_snapshot` 载荷扩展 crops 列表。 M13（企业/正式工作，R21–R35）追加：

| type                           | 载荷要点                                                                                               | 触发方                          | 状态   |
|--------------------------------|--------------------------------------------------------------------------------------------------------|---------------------------------|--------|
| `company_created`              | `{company_id, name, company_type, initial_money}`                                                      | 企业播种/创建                   | 待实现 |
| `company_status_changed`       | `{company_id, old_status, new_status, reason?}`                                                        | 暂停/恢复/停业                  | 已实现 |
| `company_money_changed`        | `{company_id, amount, balance, reason}`                                                                | 企业资金变动（amount 带符号）   | 已实现 |
| `company_inventory_changed`    | `{company_id, items: [{item_id, quantity, reserved_quantity}]}`                                        | 企业库存变化（完整列表）        | 已实现 |
| `job_opening_created`          | `{opening_id, company_id, position_id, vacancies}`                                                     | 发布招聘                        | 已实现 |
| `job_opening_closed`           | `{opening_id, company_id, position_id, reason?}`                                                       | 关闭招聘（含招聘暂停）          | 已实现 |
| `job_application_submitted`    | `{application_id, opening_id, company_id, position_id, agent_id, reason}`                              | 居民申请                        | 已实现 |
| `job_application_withdrawn`    | `{application_id, agent_id}`                                                                           | 撤回申请                        | 已实现 |
| `job_application_accepted`     | `{application_id, company_id, position_id, agent_id, manager_agent_id, reason, employment_id}`         | 录用（随 `employment_started`） | 待实现 |
| `job_application_rejected`     | `{application_id, company_id, position_id, agent_id, manager_agent_id, reason}`                        | 拒绝申请                        | 已实现 |
| `employment_started`           | `{application_id, company_id, position_id, agent_id, manager_agent_id, employment_id, employee_count, open_vacancies}` | 建立合同                        | 已实现 |
| `employment_resigned`          | `{employment_id, company_id, agent_id, reason, employee_count, open_vacancies}`                                        | 员工辞职                        | 已实现 |
| `employment_terminated`        | `{employment_id, company_id, agent_id, manager_agent_id, reason, employee_count, open_vacancies}`                      | 经理解雇                        | 已实现 |
| `employment_suspended`         | `{employment_id, company_id, agent_id, reason?}`                                                       | 合同挂起                        | 待实现 |
| `shift_scheduled`              | `{shift_id, employment_id, company_id, agent_id, scheduled_start, scheduled_end}`                      | 班次生成                        | 已实现 |
| `shift_upcoming`               | `{shift_id, employment_id, company_id, agent_id, scheduled_start, scheduled_end, minutes_until_start}` | 班前 60 分钟提醒                | 已实现 |
| `shift_started`                | `{shift_id, employment_id, company_id, agent_id, late_minutes, ends_at}`                               | 签到开始                        | 已实现 |
| `shift_late`                   | `{shift_id, agent_id, late_minutes}`                                                                   | 迟到（随 `shift_started`）      | 待实现 |
| `shift_completed`              | `{shift_id, employment_id, company_id, agent_id, worked_minutes, products: [{item_id, quantity}]}`     | 班次完成                        | 已实现 |
| `shift_absent`                 | `{shift_id, employment_id, company_id, agent_id}`                                                      | 缺勤判定                        | 已实现 |
| `shift_leave_requested`        | `{shift_id, employment_id, company_id, agent_id, reason}`                                              | 请假申请                        | 已实现 |
| `shift_leave_approved`         | `{shift_id, agent_id, manager_agent_id, reason}`                                                       | 准假（班次转 `leave`）          | 已实现 |
| `shift_leave_rejected`         | `{shift_id, agent_id, manager_agent_id, reason}`                                                       | 拒绝请假                        | 已实现 |
| `shift_cancelled`              | `{shift_id, employment_id, company_id, agent_id, reason?}`                                             | 班次取消（辞职/解雇/停业/行动中断） | 已实现 |
| `wage_paid`                    | `{shift_id, employment_id, company_id, agent_id, wage_due, wage_paid, company_balance}`                | 足额支付                        | 已实现 |
| `wage_unpaid`                  | `{shift_id, employment_id, company_id, agent_id, wage_due, wage_paid: 0, company_balance}`             | 欠薪                            | 已实现 |
| `wage_repaid`                  | `{shift_id?, employment_id, company_id, agent_id, amount}`                                             | 补发欠薪                        | 已实现 |
| `company_sale_completed`       | `{company_id, store_id, item_id, quantity, unit_price, total}`                                         | 商店售出入企业账户              | 已实现 |
| `company_production_completed` | `{company_id, shift_id, consumed: [{item_id, quantity}], products: [{item_id, quantity}]}`             | 正式工作产物入库（含原料消耗）  | 已实现 |
| `company_purchase_completed`   | `{company_id, seller_company_id, item_id, quantity, unit_price, total}`                                | 跨企业固定价采购                | 已实现 |
| `company_store_stocked`        | `{company_id, store_id, item_id, quantity, stock_after}`                                               | 企业仓库货物上架                | 已实现 |

企业事件 payload 必须包含明确关联 ID（company_id / employment_id / shift_id / agent_id / amount），并保留信封字段
`world_id` / `world_time` / `sequence` /
`trace_id`（同一事务内事件共享 trace_id）。

## 4. 事件持久化

- 所有事件写入 `world_events` 表（可追溯、可重放）。
- 存档 = 初始快照 + 按 sequence 的事件序列（重放不依赖 LLM）。
- `GET /api/worlds/{id}/events?after_sequence=N` 用于补齐遗漏。

## 5. 事件产生规则

- 一切世界状态变更必须产生事件（trace 可溯）。
- 同一事务内产生的事件共享 `trace_id` 与 `world_time`，sequence 递增。
- 前端仅消费事件渲染，不反向推导状态。
