# 智能体提示词与工具约定 (agent-prompt)

版本：1.0.0

## 1. 提示词结构

每次决策的提示词按以下顺序组装（observation_service）：

1. **身份卡**（静态，缓存）—— 姓名/年龄/职业/背景/价值观/长期目标/说话风格/性格五因素。
2. **世界现状** —— 日期与时刻、天气、当前地点（含开放状态）。
3. **自身状态与需求** —— 饥饿/精力/金钱/背包摘要、进行中行动、上次工具结果。
4. **可见信息** —— 当前地点可见人物（姓名+正在做什么）、可见地点、当前可做的事。
5. **相关记忆** —— 加权检索的少量记忆（工作记忆 + 情节 + 语义印象，限长）。
6. **收到的对话** —— 最近的 incoming 消息（若有）。
7. **行动接口** —— 可用工具清单（由 SDK 从 `@function_tool` 生成）。

约束：

- 观察文本必须限长（默认 ≤ 2000 字符），超长截断，不重复发送完整静态说明。
- **不提供**：其他地点的秘密、完整数据库、未观察到的信息。
- 相同观察短时缓存（M8），命中缓存不重复调用 LLM。

## 2. 决策策略

- 一次决策 = 一次 `Runner.run` + 最多一次有效世界行动。
- `ModelSettings(tool_choice="required", parallel_tool_calls=False)`；
  最大轮数限制（默认 4），防止工具自循环。
- 工具失败（规则拒绝）不重试同一调用；结果写入观察（下次决策可见），
  LLM 自行调整策略（T3-9）。
- LLM 故障/超时 → 降级为 `wait` 10~30 游戏分钟（T8-4），世界不崩溃。

## 3. 工具约定

第一版工具（均为 `@function_tool`，Agents SDK 官方装饰器）：

```text
move(destination_id, reason)
wait(minutes)
talk(target_agent_id, message, intent)
buy_item(item_id, quantity, reason)
sell_item(item_id, quantity, reason)
work(job_id, reason)
use_item(item_id, reason)
```

统一结构：

```python
@function_tool
async def some_action(
    ctx: RunContextWrapper[AgentToolContext],
    ...,
) -> str:
    result = await ctx.context.action_service.some_action(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        ...,
    )
    return result.model_dump_json()
```

规则：

- `agent_id` 由服务端注入 `AgentToolContext`，**不作为工具参数**交给模型
  （防止角色冒充他人）。
- 工具层不写 SQL、不操作 ORM、不发 WebSocket、不改关系、不调度下一次 LLM。
- 工具返回结构化 JSON 字符串（成功/失败 + 原因 + 影响），供观察使用。
- 失败必须可读：`{"success": false, "reason": "库存不足"}`，禁止吞异常。
- `move` 只接受地图中存在的 `location_id`；`work` 只接受存在的 `job_id`。
- `talk.message` 必须是角色真正说出口的话（禁止心理活动旁白）。

## 4. 农场行为抽象

浇水/播种/收获/商店值班/送货/修栅栏统一为 `work(job_id)`，**不为每个工种
创建独立工具**（工具表面膨胀约束）。工作差异由 job 定义（duration/工资/产物）。

## 5. 对话行为

- A 对 B 说话：A 调用 `talk` → 系统验证距离与状态 → 产生 `conversation_message`
  事件 → 前端气泡 → 消息进入 B 的 incoming 队列 → 提高 B 决策优先级 →
  B 自行决定回应/忽略/离开。
- **不在 talk 工具内直接运行对方 LLM**。
- 防无限对聊（T4-3）：同对冷却、会话最大轮数、需求衰减、目标优先、重复检测。

## 6. LLM 运行记录

每次决策落库（`llm_runs` 表）：agent_id、world_time、model、input_tokens、
output_tokens、latency、selected_tool、tool_arguments、tool_result、success、
error_type、trace_id。

- **不保存**模型隐藏推理（reasoning）。
- 只保存可审计的输入摘要、工具调用与结果。

## 7. 每日反思

- 每天结束时独立调用（单独限频，可用更强模型），产出：
  今天发生了什么 / 目标进展 / 关系变化 / 明日重点。
- 反思结果写入语义记忆，不产生世界状态变更。
