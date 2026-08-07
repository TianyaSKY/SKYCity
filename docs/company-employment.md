# 企业与正式工作系统 (company-employment)

版本：1.0.0（第一版）

> 本文件是企业与正式工作系统的详细规则契约。程序实现契约以 `world-rules.md`
> R21–R35 为准；本文档给出实体定义、流程细节与实现边界。
> 规则由引擎与 Service 层执行，不允许交给 LLM 临场发挥。

## 1. 目标与边界

第一版目标是建立一个可靠、可扩展、由真实 LLM 居民参与决策的正式就业闭环：

```text
企业发布职位 → 居民了解招聘信息 → 居民自主决定应聘 → 企业管理者审核申请
→ 建立正式劳动关系 → 系统生成工作班次 → 居民自主决定是否准时上班
→ 员工完成工作 → 企业支付工资 → 企业获得产出或营业收入
→ 企业继续招聘、经营或停业
```

居民是否应聘、是否上班、是否请假、是否辞职以及管理者是否录用，由真实 LLM 根据身份、目标、关系和现实条件决定。世界引擎负责验证、执行与记账。

坚持现有边界：

```text
LLM 产生意图
世界引擎验证并执行
前端观察和展示
```

## 2. 第一版范围

### 已实现（v1.0.0）

1. 企业拥有独立资金（`companies.money`）。
2. 企业拥有岗位（`positions`）与招聘（`job_openings`）。
3. 企业发布招聘职位（种子数据 + 离职/解雇后自动重开）。
4. 居民申请职位（`apply`，活跃申请唯一约束防重复）。
5. 企业经理审核申请（`review`，仅 `manager_agent_id` 有权限）。
6. 录用后建立正式劳动合同（`employment_contracts`）。
7. 系统自动生成下一班次（`work_shifts`，幂等）。
8. 员工签到开始工作（`start_shift`，窗口 `SHIFT_EARLY_WINDOW`/`SHIFT_LATE_LIMIT`，见 `backend/app/config/gameplay.py`）。
9. 迟到计算与缺勤判定（调度器 `formal_shift_absence_check`）。
10. 班次完成结算：工资从企业账户转入员工账户，产物进入企业库存。
11. 企业余额不足时欠薪（`unpaid_wage` / `unpaid_wage_total`），不凭空发钱； 资金到位后自动补发（`wage_repaid`）。
12. 员工辞职（`resign`）与经理解雇（`terminate`）：取消未来班次、释放名额、重开招聘。
13. 请假流程（`request_leave` / `review_leave_request`）：准假不判缺勤、不发工资。
14. 招聘暂停/恢复（`pause_recruitment` / `resume_recruitment`）。
15. 企业停业/恢复（`suspend_company` / `resume_company`）：停业停止招聘与排班。
16. 上帝注资（`inject_company_money`）入企业账户并立即补发欠薪。
17. 商店销售收入进入所属企业账户；商店收购从企业账户支付（不足拒绝）。
18. 连续欠薪天数统计（`consecutive_loss_days`）。
19. 企业、合同、班次进入存档（schema v2）与 V1 存档兼容迁移。
20. 前端企业总览/详情、居民职业卡、就业统计与 WS 事件映射。

### 暂不实现（后续版本）

企业贷款、银行、税收、股权发行、企业收购、多层管理职位、复杂绩效奖金、 员工晋升、劳动仲裁、工会、跨企业自动采购、动态市场价格、创业流程、
企业间合同、合同挂起（`employment_suspended`）、破产状态自动流转。

## 3. 实体模型

### 3.1 Company（`companies`）

| 字段                                          | 说明                                                                 |
|-----------------------------------------------|----------------------------------------------------------------------|
| `company_id`                                  | 企业 ID，与 `world_id` 联合主键                                      |
| `name` / `company_type`                       | 名称 / 类型（farm、retail…）                                         |
| `location_id`                                 | 企业地点                                                             |
| `owner_agent_id` / `manager_agent_id`         | 所有者 / 经理（经理拥有审核权限）                                    |
| `money`                                       | 企业独立余额，与老板个人余额严格分离                                 |
| `status`                                      | `active` / `suspended` / `closed` / `bankrupt`（v1 只使用 `active`） |
| `founded_at` / `suspended_at` / `closed_at`   | 时间戳                                                               |
| `consecutive_loss_days` / `unpaid_wage_total` | 连续欠薪天数 / 累计欠薪总额                                          |

企业创建时写入 `initial_capital` 流水（`CompanyTransaction`）。

### 3.2 Position（`positions`）

岗位 = 企业长期存在的职位定义，与招聘分离：

| 字段                                      | 说明                                           |
|-------------------------------------------|------------------------------------------------|
| `job_id`                                  | 引用现有 `Job`（工作如何执行）                 |
| `capacity`                                | 岗位容量（最大合同数）                         |
| `wage_per_shift`                          | 每班工资                                       |
| `shift_start_minute` / `shift_end_minute` | 班次时段（当日分钟数）                         |
| `working_days_json`                       | 工作日，`[0,1,2,3,4]` = 周一至周五，`% 7` 计算 |
| `status`                                  | `active` / `paused` / `closed`                 |

### 3.3 JobOpening（`job_openings`）

招聘 = 当前是否正在招人：

- `vacancies`：空缺数，随录用递减、随离职递增；为 0 时状态转 `filled`。
- `status`：`open` / `filled` / `paused` / `closed`。

### 3.4 JobApplication（`job_applications`）

- 状态：`submitted` → `accepted` / `rejected` / `withdrawn`。
- 约束：同一居民同一招聘最多一条活跃申请（部分唯一索引只约束 submitted/reviewing 状态，撤回或拒绝后可重新申请）； 录用时若申请人已有
  active/on_leave 合同则拒绝（一个居民最多一份正式工作）； 接受申请与创建合同在同一事务中完成。

### 3.5 EmploymentContract（`employment_contracts`）

- 状态：`active` / `resigned`（v1）；`pending` / `on_leave` / `suspended` /
  `terminated` / `ended` 预留给后续版本。
- `wage_per_shift`：录用时从岗位快照，合同存续期内不变。
- 统计字段：`attendance_score`（初始 100，迟到 `ATTENDANCE_LATE_PENALTY`，缺勤 `ATTENDANCE_ABSENT_PENALTY`，下限 0）、
  `completed_shifts` / `late_shifts` / `absent_shifts`、`unpaid_wage`。
- 第一版限制：一个居民最多一份 active 正式合同。

### 3.6 WorkShift（`work_shifts`）

- 状态：`scheduled` → `in_progress` / `late` → `completed`；
  `scheduled` → `absent`；`scheduled` → `cancelled`（辞职时）。
- `payroll_status`：`paid` / `unpaid`。
- `late_minutes = max(actual_start - scheduled_start, 0)`。
- `wage_due = wage_per_shift * min(worked_minutes, scheduled_minutes) // scheduled_minutes`
  （完成比例向下取整，最小单位 1）。
- 每个班次独立记录，不在合同上只累计数字。

### 3.7 CompanyInventory（`company_inventories`）

- `(world_id, company_id, item_id)` 联合主键；`quantity` / `reserved_quantity`。
- 正式工作产物入库（`handle_shift_completed`）；临时工作产物仍进个人背包。
- 库存不允许负数；`reserved_quantity` 是已签到班次锁定的原料（R37），消耗时与
  `quantity` 同步扣减。

### 3.8 CompanyTransaction（`company_transactions`）

- 类型：`initial_capital` / `wage_payment` / `sale_income` / `material_purchase` /
  `refund` / `god_injection` / `operating_expense`（v1 使用前两类）。
- 与居民 `Transaction` 分离；工资支付产生一对对应流水，同一 `trace_id`：

```text
CompanyTransaction: type=wage_payment, amount=-90
Transaction:        type=work_wage,     amount=+90
```

### 3.9 LeaveRequest（`leave_requests`）

班次请假申请（计划 §5 模型清单未列，实现时补充）：

- 状态：`pending` → `approved` / `rejected` / `cancelled` / `expired`。
- 同一班次最多一条 pending 申请；批准后班次转 `leave`（不判缺勤、不发工资）； 缺勤判定时 pending 申请转 `expired`；辞职时
  pending 申请转 `cancelled`。

## 4. 第一版企业配置

| 企业                 | 地点             | 岗位                                   | 容量 | 班次        | 每班工资 | 初始资金 |
|----------------------|------------------|----------------------------------------|------|-------------|----------|----------|
| 晨露农场（farm）     | `village_farm`   | 农场工人（`job_farm_production`）      | 2    | 08:00–12:00 | 60       | 800      |
| 村庄杂货店（retail） | `village_shop`   | 商店店员（`job_shop_attendant`）       | 1    | 09:00–17:00 | 90       | 1000     |
| 晨露面包坊（workshop）| `village_bakery` | 面包师（`job_bakery_bake`）            | 1    | 13:00–17:00 | 60       | 300      |
| 小镇旅店（hotel）    | `village_hotel`  | 客房服务员（`job_hotel_service`）      | 1    | 12:00–16:00 | 80       | 400      |
| 巧木工坊（workshop） | `carpenter_shop` | 木工师傅（`job_carpentry`）            | 1    | 10:00–14:00 | 60       | 300      |
| 晨露花圃（farm）     | `flower_garden`  | 花匠（`job_flower_gardening`）         | 1    | 08:00–12:00 | 60       | 300      |

- 种子文件：`world_data/companies/companies.json`（企业 + 岗位 + 工作日 + 采购规则）。
- 播种幂等：`ensure_seeded` 只创建缺失的企业/岗位/招聘，可重复调用。
- **生产链（M16 已实现）**：晨露农场正式班次产出 10 小麦/班 → 面包坊按固定价 6 金币/件
  从农场采购小麦，面包师班次消耗 10 小麦产出 20 面包/班 → 杂货店按固定价 6 金币/件
  从面包坊采购面包并上架 → 居民以 12 金币/件零售购买，收入进杂货店企业账户。
  配方与 `formal_only` 标记在 `world_data/jobs/jobs.json`，采购规则在
  `world_data/companies/companies.json` 的 `procurement` 列表；面包不再自动补货
  （`restock_daily = 0`）。
- **M17 延伸链（新经理企业）**：巧木工坊班次产出 1 耙子/班 → 杂货店按 18 金币/件采购并上架
  （零售 35）；晨露花圃班次产出 5 鲜花/班 → 杂货店按 6 金币/件采购并上架（零售 12）。
  旅店为纯服务企业，无产物。三家新企业经理：周婶（agent_zhoushen）、李木匠（agent_limujiang）、
  孙婶（agent_sunshen），角色卡与地图（建筑/小路/出生点）由 `tools/build_map.py` 从
  `world_data/identities/` 派生。

## 5. 流程规则

### 5.1 申请（apply_job / API）

服务端校验顺序：

1. 世界未暂停。
2. 招聘存在且 `status == open`，`vacancies > 0`。
3. 智能体存在。
4. `(world_id, opening_id, agent_id)` 唯一（IntegrityError → 已经申请过该职位）。

通过后创建 `submitted` 申请，发布 `job_application_submitted`。

### 5.2 审核（review_job_application / API）

- 仅 `company.manager_agent_id` 可审核本企业申请；申请须仍为 `submitted`。
- `reject`：状态转 `rejected`，发布 `job_application_rejected`。
- `accept`：
    1. 申请人无 active/on_leave 合同；
    2. 招聘仍 `open` 且 `vacancies > 0`；
    3. 申请转 `accepted`，`vacancies -= 1`（归零转 `filled`）；
    4. 同事务创建 `EmploymentContract`（active）；
    5. 生成下一班次；
    6. 发布 `employment_started`。

### 5.3 班次生成（_create_next_shift）

- 触发点：录用、班次完成、缺勤判定后；每个合同始终持有"下一班次"。
- 规则：从当天起向前找第一个 `start > 当前时间` 且 `weekday ∈ working_days`
  的日期；同 `(employment_id, scheduled_start)` 已存在则复用（幂等）。
- 生成时注册 `formal_shift_absence_check`（`scheduled_start + 120`）， 发布 `shift_scheduled`。

### 5.4 签到（start_shift / API）

- 班次属于当前居民；状态为 `scheduled`；合同 `active`； 企业、岗位、智能体存在；居民无进行中行动（R1）； 居民位于企业地点；
  `scheduled_start - 30 ≤ now ≤ scheduled_start + 120`。
- 成功：`actual_start = now`，`late_minutes` 计算，迟到记 `late_shifts`、
  `attendance_score -= 2`；`agent.action_type = formal_work`，
  `action_data = {shift_id, company_id}`；调度 `formal_shift_completed`（结束时）与
  `formal_shift_absence_check`；发布 `shift_started`。

### 5.5 请假（request_leave / review_leave_request）

- 员工为未开始的班次申请请假（仅 `scheduled` 班次；同一班次一条 pending 申请）。
- 经理（`manager_agent_id`）审批：`approve` → 班次转 `leave`、`wage_due = 0`、 不判缺勤，并生成下一空槽班次；`reject` → 班次保持
  `scheduled`。
- 事件：`shift_leave_requested` / `shift_leave_approved` / `shift_leave_rejected`； 请假申请提升经理决策优先级。
- 缺勤判定时 pending 申请转 `expired`（不能依赖 LLM 主动撤销）。

### 5.6 缺勤判定（调度器）

- `scheduled_start + 120` 时班次仍为 `scheduled` → 转 `absent`，
  `wage_due = 0`；合同 `absent_shifts += 1`、`attendance_score -= 10`； 发布 `shift_absent`；随后生成下一班次。
- 不依赖 LLM 主动承认缺勤。

### 5.6 班次完成（调度器 `formal_shift_completed`）

1. 班次处于 `in_progress` / `late` 才结算（幂等：其余状态直接返回）。
2. 行动守卫：若居民当前行动不是该班次的 `formal_work`（行动被中断/清除，如上帝传送），
   班次转 `cancelled`（`absence_reason = 行动被中断，班次取消`）、全额释放预留原料、
   发布 `shift_cancelled`，**不发工资、不产出、不续排**。
3. `worked_minutes = actual_end - actual_start`，计算 `wage_due`（比例向下取整）。
4. 按配方结算（R37）：先消耗已预留原料（`min(reserved, qty)`），再按 `products` 产出进入
   `CompanyInventory`；无配方的旧存档回退到 `job.products_json`（只产出、不消耗）。
5. 工资结算（见 5.7）。
6. 班次转 `completed`；合同 `completed_shifts += 1`； 清空居民行动状态；发布
   `shift_completed` + 工资事件（仅 `wage_due > 0` 时）+ `company_inventory_changed` +
   `company_production_completed`；生成下一班次；调度居民下次决策。

### 5.7 工资结算（PayrollService 职责，v1 内嵌于班次完成）

- `company.money >= wage_due`：企业扣款、员工入账、班次 `paid`、 双流水（同一 `trace_id`）、发布 `wage_paid`。
- 不足：班次 `unpaid`、`contract.unpaid_wage += wage_due`、
  `company.unpaid_wage_total += wage_due`、发布 `wage_unpaid`。
- 不赊账、不凭空发钱；企业余额不出现负数。

### 5.8 辞职（resign_job / API）

- 仅员工本人；合同须为 active/on_leave。
- 合同转 `resigned`；未来 `scheduled` 班次全部转 `cancelled`（发布 `shift_cancelled`）；
  招聘名额 +1（无招聘则新建；企业停业期间新建/恢复的招聘保持 `paused`，恢复经营后重新开放并发布
  `job_opening_created`）；发布 `employment_resigned`（含权威 `employee_count` / `open_vacancies`）。
- 欠薪不因辞职消失。

### 5.9 解雇（terminate_employment / API）

- 仅企业经理；不能解雇他企业员工；不能重复终止。
- 合同转 `terminated`；未来班次与 pending 请假转 `cancelled`（班次发布 `shift_cancelled`）；
  名额恢复（停业期间同上保持 `paused`）；发布 `employment_terminated`（含权威
  `employee_count` / `open_vacancies`）。
- 欠薪不因解雇消失。

### 5.10 招聘暂停/恢复（pause / resume_recruitment）

- 暂停：岗位转 `paused`，其 open 招聘转 `paused`（不接受新申请），发布
  `job_opening_closed`；恢复：岗位与有余额的招聘转回 `open`，发布
  `job_opening_created`。

### 5.11 企业停业/恢复（suspend / resume_company）

- 停业：`suspended` + `suspended_at`；招聘暂停；未来 scheduled 班次全部取消（每个取消班次发布
  `shift_cancelled`）； 进行中班次照常完成但不生成下一班次；发布
  `company_status_changed`。
- 恢复：`active`；招聘恢复（仅对非 open 且有余额的招聘重新开放并发布 `job_opening_created`）；
  为没有 scheduled 班次的 active 合同重新生成班次。

### 5.12 上帝注资（inject_company_money）

- 企业余额 += 金额，写 `god_injection` 流水 + `company_money_changed`； 资金足够时同事务补发全部可覆盖欠薪（`wage_repaid`）。

### 5.13 事件与流水

所有状态变更发布事件（见 event-protocol §3 企业事件）并写 `world_events`； 所有资金变化写流水（`CompanyTransaction` /
`Transaction`）；同一事务内事件共享
`trace_id`。

## 6. 服务层划分

| 服务                       | 职责                                                                                    | 状态                            |
|----------------------------|-----------------------------------------------------------------------------------------|---------------------------------|
| `CompanyEmploymentService` | 播种、申请、审核、签到、辞职、解雇、请假、班次完成、缺勤、停业、招聘管理（v1 聚合实现） | 已实现                          |
| `PayrollService`           | 工资结算、欠薪、补发、双流水（已独立文件）                                              | 已实现                          |
| `CompanyService`           | 企业创建/状态/余额                                                                      | 已并入 CompanyEmploymentService |
| `RecruitmentService`       | 发布/关闭招聘、申请、审核、空缺校验                                                     | 已并入 CompanyEmploymentService |
| `EmploymentService`        | 合同创建/查询/辞职/解雇、冲突检查                                                       | 已并入 CompanyEmploymentService |
| `ShiftService`             | 生成班次、提醒、签到、迟到、缺勤、请假、完成                                            | 已并入 CompanyEmploymentService |
| `CompanyInventoryService`  | 入库、扣减、查询、事件                                                                  | 已并入 CompanyEmploymentService |

`EconomyService` 继续负责临时工作与居民购买/出售；正式工作（`action_type =
formal_work`）与临时工作（`work`）互不干扰。v1 暂不物理拆分，逻辑边界如上。

## 7. LLM 集成（工具契约见 agent-prompt.md §8）

| 工具                                                                                 | 角色 | 状态   |
|--------------------------------------------------------------------------------------|------|--------|
| `apply_job(opening_id, reason)`                                                      | 居民 | 已接入 |
| `withdraw_job_application(application_id, reason)`                                   | 居民 | 已接入 |
| `start_shift(shift_id, reason)`                                                      | 员工 | 已接入 |
| `request_leave(shift_id, reason)`                                                    | 员工 | 已接入 |
| `resign_job(employment_id, reason)`                                                  | 员工 | 已接入 |
| `review_job_application(application_id, decision, reason)`                           | 经理 | 已接入 |
| `review_leave_request(request_id, decision, reason)`                                 | 经理 | 已接入 |
| `terminate_employment(employment_id, reason)`                                        | 经理 | 已接入 |
| `pause_recruitment(position_id, reason)` / `resume_recruitment(position_id, reason)` | 经理 | 已接入 |

禁止 LLM 传入：工资金额、企业余额、合同状态、实际签到时间、支付结果、 岗位剩余人数 —— 全部由服务端确定。

## 8. 存档

- `SCHEMA_VERSION = 2`：保存 companies、positions、job_openings、
  job_applications、employment_contracts、work_shifts、leave_requests、
  company_inventories、company_transactions（save_service 已实现）。
- 恢复时：班次/合同/申请等全局主键表换新 ID 并重映射引用 （shift.employment_id、申请.opening_id、调度器 payload 的
  shift_id）， 恢复后缺勤检查与完成回调仍命中；`ensure_seeded` 幂等补种种子企业与商店绑定。
- V1 存档迁移（`normalize_save_payload`）：保留旧工作累计历史；按种子重建 企业；旧工作历史不转正式合同；商店按种子绑定企业；未完成旧
  `work` 行动 按临时工作恢复。

## 9. 日志

统一 `loguru`；关键日志必须含上下文（world/shift/company/agent/trace）：

```python
logger.info(
    "Payroll completed world={} shift={} company={} agent={} due={} paid={} status={} trace={}",
    world_id, shift_id, company_id, agent_id, wage_due, wage_paid, payroll_status, trace_id,
)
```

不得使用 `print`，不得吞掉工资、合同或班次异常。

## 10. 最重要的实现原则

```text
临时工作与正式工作并存        Job 与 Position 分离
工作历史与劳动合同分离        企业余额与个人余额分离
班次计划与实际行动分离        正式工作产物归企业
工资必须来自企业账户          企业销售收入进入企业账户
居民和经理决策使用真实 LLM    世界规则不能交给 LLM 执行
所有资金变化必须有流水        所有状态变化必须有事件
所有调度处理器必须幂等        存档恢复不能重复发工资
```
