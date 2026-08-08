# 智能体提示词与工具约定 (agent-prompt)

版本：1.0.0

## 1. 提示词结构

每次决策的提示词按以下顺序组装（observation_service）：

1. **身份卡**（静态，缓存）—— 姓名/年龄/职业/背景/价值观/长期目标/说话风格/性格五因素。
2. **世界现状** —— 日期与时刻、天气、当前地点（含开放状态）。
3. **自身状态与需求** —— 饱食度/精力/孤单/金钱/背包摘要、进行中行动、上次工具结果。
4. **可见信息** —— 当前地点可见人物（姓名+正在做什么）、可见地点（每个地点标注营业状态与
   从当前位置出发的路程耗时，雨雪天更慢）、当前可做的事。
5. **相关记忆** —— 加权检索的少量记忆（工作记忆 + 情节 + 语义印象，限长）。
6. **收到的对话** —— 最近的 incoming 消息（若有）。
7. **行动接口** —— 可用工具清单（由 SDK 从 `@function_tool` 生成）。

约束：

- 观察文本必须限长（默认 ≤ 2000 字符），超长截断，不重复发送完整静态说明。
- **不提供**：其他地点的秘密、完整数据库、未观察到的信息。
- 相同观察短时缓存（M8），命中缓存不重复调用 LLM。

## 2. 决策策略

- 一次决策 = 一次 `Runner.run` + 最多一次有效世界行动。
- `ModelSettings(tool_choice="required", parallel_tool_calls=False)` +
  `tool_use_behavior="stop_on_first_tool"`：第一个工具执行完即结束回合， 模型无法在同一决策内连环行动；最大轮数（默认
  4）仅作为畸形工具调用的兜底。
- 工具失败（规则拒绝）不重试同一调用；结果写入观察（下次决策可见）， LLM 自行调整策略（T3-9）。
- LLM 故障/超时 → 降级为 `wait` 10~30 游戏分钟（T8-4），世界不崩溃。

## 3. 工具约定

第一版工具（均为 `@function_tool`，Agents SDK 官方装饰器）：

```text
move(destination_id, reason)   # 路程耗时在观察的【可见地点】标注（每步 2 分钟，雨雪天更慢）
wait(minutes)
sleep(minutes, reason)   # 60~480 分钟，每小时 +2 精力 / +3 心情（值见 backend/app/config/gameplay.py）；
                         # 有家必须在家睡觉，无家必须去小镇旅店(village_hotel)睡（每晚 85 金币）
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

- `agent_id` 由服务端注入 `AgentToolContext`， **不作为工具参数**交给模型 （防止角色冒充他人）。
- 工具层不写 SQL、不操作 ORM、不发 WebSocket、不改关系、不调度下一次 LLM。
- 工具返回结构化 JSON 字符串（成功/失败 + 原因 + 影响），供观察使用。
- 失败必须可读：`{"success": false, "reason": "库存不足"}`，禁止吞异常。
- `move` 只接受地图中存在的 `location_id`；`work` 只接受存在的 `job_id`。
- `talk.message` 必须是角色真正说出口的话（禁止心理活动旁白）。

## 4. 农场行为抽象

浇水/播种/收获/商店值班/送货/修栅栏统一为 `work(job_id)`， **不为每个工种 创建独立工具**（工具表面膨胀约束）。工作差异由 job
定义（duration/工资/产物）。

## 5. 对话行为

- A 对 B 说话：A 调用 `talk` → 系统验证距离与状态 → 产生 `conversation_message`
  事件 → 前端气泡 → 消息进入 B 的 incoming 队列 → 提高 B 决策优先级 → B 自行决定回应/忽略/离开。
- **不在 talk 工具内直接运行对方 LLM**。
- 防无限对聊（T4-3）：同对冷却、会话最大轮数、需求衰减、目标优先、重复检测。

## 6. LLM 运行记录

每次决策落库（`llm_runs` 表）：agent_id、world_time、model、input_tokens、
output_tokens、latency、selected_tool、tool_arguments、tool_result、success、 error_type、trace_id。

- **不保存**模型隐藏推理（reasoning）。
- 只保存可审计的输入摘要、工具调用与结果。

## 7. 每日反思

- 每天结束时独立调用（单独限频，可用更强模型），产出： 今天发生了什么 / 目标进展 / 关系变化 / 明日重点。
- 反思结果写入语义记忆，不产生世界状态变更。

## 8. 企业与正式工作工具（M13，R21–R35）

### 8.1 普通居民/员工工具

```text
apply_job(opening_id, reason)                      # 申请职位
withdraw_job_application(application_id, reason)   # 撤回申请
start_shift(shift_id, reason)                      # 签到开始班次
request_leave(shift_id, reason)                    # 请假
resign_job(employment_id, reason)                  # 辞职
```

### 8.2 企业经理工具

```text
review_job_application(application_id, decision, reason)   # accept | reject
review_leave_request(request_id, decision, reason)         # approve | reject
terminate_employment(employment_id, reason)                # 解雇
pause_recruitment(position_id, reason)                     # 暂停招聘
resume_recruitment(position_id, reason)                    # 恢复招聘
purchase_company_goods(buyer_company_id, seller_company_id, item_id, reason, quantity=1)  # 跨企业采购
stock_store(company_id, store_id, item_id, reason, quantity=1)  # 仓库货物上架
```

### 8.3 约定

- 工具只传意图参数（opening_id / shift_id / reason / decision）。 **禁止 LLM 传入**：工资金额、企业余额、合同状态、实际签到时间、
  工资是否支付成功、岗位剩余人数 —— 全部由服务端确定。
- `purchase_company_goods` / `stock_store` **禁止 LLM 传入**：单价、余额、库存数量上限 —— 价格与可采购量由服务器固定规则决定；
  `manager_agent_id` 由服务端注入。
- `agent_id` / `manager_agent_id` 由服务端从 `AgentToolContext` 注入， 不作为工具参数（防止冒充他人，同 §3）。
- 经理工具校验：只有 `company.manager_agent_id` 可操作本企业资源； 引擎做硬性校验，决策由真实 LLM 作出。
- 工具失败返回可读原因（`{"success": false, "reason": "岗位已满"}`）， 不吞异常；失败不重试同一调用（同 §2 T3-9）。

### 8.4 观察内容

员工观察追加：

```text
【正式职业】
企业：晨露农场
岗位：农场工人
合同状态：在职
每班工资：60金币
出勤评分：92
未支付工资：0

【今天班次】
时间：08:00–12:00
状态：尚未签到
距离开始：45分钟
工作地点：晨露农场
```

求职者观察追加：

```text
【公开招聘】
晨露农场：农场工人，60金币/班，剩余2个名额
村庄杂货店：商店店员，90金币/班，剩余1个名额

【我的申请】
晨露农场：等待审核
```

企业经理观察追加：

```text
【企业经营】
企业余额：800
员工人数：1/2
今日应付工资：60
欠薪：0
今日收入：0

【待审核事项】
求职申请1条
请假申请0条

【待审核求职申请】
申请人：林夏
职位：农场工人
申请理由：希望获得稳定收入
当前职业：无
出勤历史：暂无
与你的关系：熟悉度25，好感18
```

观察必须控制长度，只提供与当前决策相关的数据（同 §1 限长约束）。
