# 世界规则 (world-rules)

> 本文件是 **程序实现契约**，不是提示词建议。引擎与 Service 层必须逐条实现，
> 不允许把规则交给 LLM 临场发挥。修改规则需同步修改本文件与对应测试。

版本：1.0.0

## 总则

- 世界引擎是唯一真值来源（见 architecture.md）。所有规则在 `ActionExecutionService`
  中校验，工具层只传递意图。
- 数值单位为游戏分钟（世界时钟）；「每小时」= 60 游戏分钟。
- 格距离一律使用曼哈顿距离。

## R1 并发行动

- 一个智能体 **同一时刻最多一个进行中的行动**。
- `move` 独占：移动中不能发起 `talk`、`work`、`buy_item`、`sell_item`、`use_item`。
- `work` 独占且不可打断（R3）。
- `wait` / `sleep` 可随时被打断（例如收到对话、上帝命令、发起 move）。
- 收到新行动时若已有进行中行动：拒绝并返回原因（工具结果中说明）。

## R2 移动中能否对话

- **不能**。`talk` 要求双方都处于空闲（无进行中行动）且互相距离 ≤ 3 格（R9）。
- 收到对话消息不打断对方当前行动；对方在下次决策时自行决定是否回应。

## R3 工作中能否被打断

- 普通行动 **不能**打断 `work`。
- 上帝命令可以打断（上帝权限高于普通规则，见 R13）；被打断时按已完成工时比例结算工资。

## R4 抢最后一个商品

- 购买在数据库事务内执行，对商店库存行加锁（`SELECT ... FOR UPDATE` 或 SQLite
  `BEGIN IMMEDIATE` + 事务级重试）。
- 第一个事务成功，后续事务返回 `库存不足`。
- 校验在服务端完成，不依赖 LLM 预先判断。

## R5 暂停时已发出的 LLM 请求

- 暂停只阻止 **新**决策请求（调度器不再派发）。
- 已发出的请求允许完成 LLM 调用，但其工具调用被拒绝，返回 `世界已暂停`。
- 被拒绝的行动不产生任何世界状态变更（幂等）。

## R6 天气对移动的影响

- 天气枚举：`clear`（晴）、`cloudy`（阴）、`rain`（雨）、`snow`（雪）。
- 移动耗时（每格）：晴/阴 2 分钟；雨 3 分钟（×1.5）；雪 4 分钟（×2.0）。
- 天气不影响其他行动耗时。

## R7 没钱能否欠款

- **不能赊账**。余额不足 → 购买失败，返回 `余额不足`。
- `debt` 只存在于关系模型（direction 字段），由显式事件（第一版无借贷工具）产生， 购买/工作不产生债务。

## R8 商店关门能否进入

- 地点开放区间 `[open_hour, close_hour)`；区间外视为关闭。
- 关闭时 `move` 到该地点被拒绝；到达门前才发现关闭 → 在门口 `wait`， 直到开门（引擎自动调度开门后的重新评估）。
- `house`、`hotel`、`plaza` 全天开放（0-24）。

## R9 对话距离

- 曼哈顿距离 ≤ 3 格（含 0：同格）才可发起/继续对话。
- 一方移动导致距离 > 3 → 对话自动中断（不产生额外事件，只记录中断原因）。

## R10 发工资时机

- `work` 开始时锁定期望工时 `duration`； **完成后一次性结算**工资 + 产物（进背包）。
- 上帝打断 → 按 `已耗时 / duration` 比例结算（向下取整到最小工资单位 1）。
- 结算通过 `Transaction` 记录，进入经济闭环服务。

## R11 饱食度 = 0

- 饱食度（0~100，越高越饱）为 0：禁止 `work`；移动速度 ×2 耗时；每游戏小时精力额外 -1。
- 触发「寻找食物」高优先级决策（调度器将其 `next_decision_at` 提前）。
- 第一版无死亡机制。

## R12 精力 = 0

- 精力为 0：强制休息，禁止 `move`/`work`；`wait` 每小时恢复 `WAIT_ENERGY_PER_HOUR` 精力（见 `backend/app/config/gameplay.py`，当前为 1）。
- 精力恢复 > 20 后才允许其他行动。

## R13 上帝命令与规则

- 上帝命令 **可以**违反普通规则（瞬移、改库存、发钱、改天气等）。
- 但必须：经过 `GodActionService` → 写审计记录 → 产生 `WorldEvent` → 推送前端 → 进入受影响角色的观察/记忆。
- 上帝不能创建不存在的 item/location id。

## R14 需求节奏（第一版默认值）

> 所有数值的单一来源是 `backend/app/config/gameplay.py`，本表仅作说明；改动配置即全局生效。

| 需求   | 变化                                                                                 |
|--------|--------------------------------------------------------------------------------------|
| 饱食度 | 每小时 -1；使用食物按物品效果恢复                                                    |
| 精力   | 每小时 -1；`work` 每小时额外 -4（按 job 强度）；`wait` 每小时 +1；`sleep` 每小时 +2 |
| 心情   | 每小时 -1；`sleep` 每小时 +3；`wait` 每小时 +2；使用心情物品按效果恢复（M12）        |
| 孤单   | 每小时 +1；与智能体对话每条消息 -1（R21，`LONELINESS_RELIEF`）                        |
| 金钱   | 初始 50；工资/交易改变                                                               |

- 上表为当前 `backend/app/config/gameplay.py` 的取值快照（睡眠恢复约为 `wait` 的 2 倍），改动配置即全局生效。

- `sleep` 是独立行动（action_type=`sleep`）：60~480 分钟，可打断，完成后恢复空闲并重新调度决策。
- **睡觉地点规则（R14 扩展）**：有家的智能体 **只能在家睡觉**（`sleep` 要求当前在 自己的家）；没有家的智能体
  **只能在小镇旅店**（`village_hotel`，24 小时开放）睡觉， 每次入睡收取 `HOTEL_NIGHTLY_FEE=85` 金币房费（当前值，见 `backend/app/config/gameplay.py`；入住即扣，记
  `hotel_fee` 流水 +
  `money_changed` 事件；余额不足拒绝，不赊账 R7）。 在错误地点睡觉被拒绝（`有家必须回家睡觉（当前不在家）` /
  `没有家的智能体需要去小镇旅店睡觉`），智能体需先 `move` 回家/旅店再睡。
- **夜间睡觉引导**：22:00–07:00 且精力 ≤ 40 的空闲智能体，每小时触发一次 高优先级决策（`needs_boost`），引导其回家/去旅店睡觉；LLM
  提示词与观察 文本同步说明睡觉地点与房费。
- 心情 ≤ 20 触发高优先级决策（同 R11/R12 机制，调度 `agent_decide` 提前）。

- R15 商店补货与地点容量

- 商店每天开门时按 `store_products` 配置补货至上限。
- `initial_stock`（默认 = stock_cap）：纯收购品（如小麦/鲜花，restock_daily=0 且 initial_stock=0）从 0
  起步，容量只用于吸收智能体产出；货架商品从满库存起步。
- 地点容量已满：`move` 进入被拒绝（返回 `地点已满`），到达门口后等待。
- M12 促销：商店每日开门时按 `_promo_roll`（确定性哈希，20% 概率）对每个商品 打折 20%（下限 1 金币），次日恢复
  `base_sell_price`；价格变化发布
  `store_price_changed`。

## R16 事件顺序

- 同一 `world_time` 的事件按 `sequence` 升序。
- 所有状态变更必须产生 `WorldEvent` 并可追溯（trace_id，见 M8）。

## R17 存档

- 存档包含：世界时间/天气/身份与状态/背包/商店库存/关系/记忆/未完成行动/ 待执行事件/随机种子/配置版本（map 只记录版本号）。
- 恢复后世界必须从存档时间继续运行，事件序列继续递增。

## R18 股票市场（M10）

- R18.1 交易：买入/卖出为即时行动，要求智能体空闲（R1），无地点要求； 余额不足/持股不足拒绝（不赊账，R7）；交易不改变股价。
- R18.2 股价：每笔经营事件（商店售出/工作完成）对应股票 +1（下限 1）； 每小时叠加确定性噪声 [-2, +2]（hashlib 公式，进程间稳定）；每股每小时
  发布一次 `stock_price_changed`（价格未变也发，前端静默）。
- R18.3 分红：每日 00:00 按当日经营数 `div_per_share = max(1, day_business // 5)`
  （无经营则 0 不发）；按持仓发放并记 `dividend` 流水；`prev_price` 更新为 收盘价，`day_business` 清零。
- R18.4 上帝：可设定任意股价（走 GodActionService 审计 + 事件）。
- R18.5 存档：股票价格/经营计数/持仓随存档（R17）；旧存档恢复时自动补种市场。

## R19 智能体间转账与赠物（M11）

- R19.1 即时行动，发起者要求空闲（R1），无地点要求以外的限制：目标必须距离 ≤ 3 格（曼哈顿，同 R9）；接收方无需空闲。
- R19.2 不赊账/不超持（R7）：余额不足/物品不足拒绝；条件 UPDATE 防并发；不能转给自己。
- R19.3 流水与事件：`transfer` / `item_gift` 交易流水（双方各一条），
  `money_transferred` / `item_given` 事件，余额/背包经 `money_changed` /
  `inventory_changed` 到账。
- R19.4 记忆：双方各记一条 episodic（无金额阈值）。
- R19.5 转账与赠物不改变股价；随存档（R17，`transactions`/`inventories` 已覆盖，无新表）。

## R20 消费与心情（M12）

- R20.1 心情维度：0~100，每小时基础 -1；`sleep` 每小时 +20、`wait` 每小时 +2 （上限 100）；心情 ≤ 20 触发高优先级决策（同
  R11/R12）。
- R20.2 非食物心情物品可 `use_item`：蜂蜜 (+15)/草莓 (+10)/蜡烛 (+8)/陶罐 (+12)/ 花种 (+15) 恢复心情；`satiety_restore` 与
  `mood_restore` 均为 0 的物品仍拒绝 （`该物品不是食物`）。
- R20.3 工具与肥料效果：结算工资时按持有量求和——`tool_rake` 工资 +20%、
  `fertilizer` 每件产物 +1；无持有则与旧行为一致。
- R20.4 每日生活开销：00:00 每个智能体扣 5 金币（余额下限 0，不赊账 R7）， 记 `upkeep` 流水并发布 `money_changed`
  （reason=每日生活开销）。
- R20.5 促销：见 R15（确定性 20% 日概率、20% 折扣、恢复基准价）。
- R20.6 赠物关系：`item_given` 后送礼方对收礼方 affection +3 / familiarity +2， 收礼方对送礼方 familiarity +2（
  `relationship_changed` 自动发布）。

## R21 孤单维度

- 孤单 0~100，越高越孤单：每小时 +1（上限 100）；与智能体成功对话 （每条投递的消息）双方各缓解 10（下限 0），并即时发布
  `needs_changed`。
- 孤单 ≥ 80 触发「寻找社交」高优先级决策（同 R11/R12 机制，调度
  `agent_decide` 提前）。
- 第一版孤单不禁止任何行动，仅影响决策优先级；无死亡机制。

## R22 建造系统（M14）

- R22.1 `build` 是独占行动：发起者要求空闲（R1）；建造中不能发起
  `move`/`talk`/`work` 等；与 `work` 同性质不可打断（R3），上帝可打断 并按比例退还材料（R22.2）。
- R22.2 材料预扣：发起时按 blueprint `materials` 从背包扣除进入「建造中」状态， 完成时落格（写 `tile_structures`）；上帝打断按
  `剩余耗时 / duration` 比例退还 （向下取整，每类材料最小退 1 件；已完成的建造不退还）。
- R22.3 位置校验：发起者距锚点格曼哈顿距离 ≤ 3（同 R9）；footprint 所有格必须 **可行走且未被占用**——有 navigation 标记、无
  collision 标记、无既有
  `tile_structures`、非 location 锚点格、非 spawn 格。
- R22.4 连通性不变式：`blocking` 建筑放置前校验
  `effective_walkable = static_walkable − 已阻塞格 − 新 footprint 格`， 要求所有 location 锚点与 spawn 点在
  effective_walkable 上互相可达（BFS）； 不可达 → 拒绝（`会堵住村庄`）。
- R22.5 所有权：建成后归建造者（`owner_agent_id`）；v1 仅上帝可拆除（走 R13 管道）。
- R22.6 寻路：`find_path` 使用 effective_walkable（静态集合 − 阻塞结构）， 与 R22.4 同一数据源，保证寻路不穿过建筑；移动起点被阻塞格覆盖时拒绝出发。

## R23 种植与收获（M15）

- R23.1 `plant` 是独占行动：发起者要求空闲（R1）；锚点格距发起者曼哈顿 距离 ≤ 3（同 R9）。
- R23.2 种植区：仅 `farm_field` interactable 半径 ≤ 4 的可行走格可种植 （v1 配置值，见 crops.json `plant_radius`）；不可种在
  location 锚点/spawn 格。
- R23.3 占用：目标格必须 **无作物且无结构物**（`crops` 与 `tile_structures`
  互斥，跨表检查）；PK= (world_id,col,row) 防并发占格。
- R23.4 种子：种植扣除 1 粒种子（背包持有校验）；种子来自商店（R15 补货）。
- R23.5 生长：阶段由世界时钟推进（scheduler 回调，暂停即停）；每阶段到点 发布 `crop_grown` 并调度下一阶段；阶段时长与瓦片 gid
  见 crops.json。 回调带 `next_stage_at` 幂等守卫：作物被移除或阶段被上帝改写后，过期回调 直接跳过。
- R23.6 收获：仅最终阶段可 `harvest`（未成熟返回 `作物还没成熟`）；产物 = crops.json 配置 + 持有 `fertilizer` 的 yield_bonus
  求和（同 M12 C4）； 收获清格、产物进背包、发布 `crop_harvested`。
- R23.7 作物不挡路：不参与 R22.4 连通性校验、不进 effective_walkable。
- R23.8 上帝：可 `set_crop_stage`（改阶段并重排生长回调）/ `remove_crop`
  （清格，无退还），走 R13 审计管道。
- R23.9 存档：`crops` 行 + 未触发的 `crop_grow` 回调行随 R17 存档；恢复后 生长从 `next_stage_at` 续跑。

## 企业与正式工作（R21–R35，详见 company-employment.md）

## R21 企业账户独立性

- 企业拥有独立余额（`companies.money`），与老板/经理个人余额严格分离， 任何逻辑不得合并两者。
- 企业资金变化必须写 `CompanyTransaction` 流水并发布事件；企业余额不得为负。
- 企业创建时写入 `initial_capital` 流水；第一版不赊账、不贷款、无银行。

## R22 Job 与 Position 分离

- `Job`（临时工作定义）与 `Position`（企业正式岗位）并存；岗位通过 `job_id`
  引用 Job，继承其执行方式（地点、时长、精力消耗、产物）。
- 临时工作（`work` 行动）结算走 `EconomyService`：工资进个人背包、产物进个人背包。
- 正式工作（`formal_work` 行动）结算走班次完成处理器：工资从企业账户支付、 产物进 `CompanyInventory`。

## R23 招聘与岗位容量

- `JobOpening.vacancies` 为当前空缺；录用时 `-1`（归零转 `filled`）， 离职时 `+1`（转回 `open`，无招聘则新建）。
- 岗位已满（`vacancies <= 0`）拒绝新申请；容量并发安全依赖唯一约束 + 事务内校验（同一名额不能录用两人）。
- 接受申请与创建合同必须在同一事务内完成，杜绝重复建合同。

## R24 求职申请

- 同一居民同一招聘最多一条 **活跃**申请（submitted/reviewing）；撤回或拒绝后 可重新申请（SQLite 部分唯一索引
  `uq_job_application_active_opening_agent` 强制）。
- 申请状态：`submitted` → `accepted` / `rejected` / `withdrawn`；同一申请只能处理一次。
- 已关闭（非 `open`）招聘不接受申请。
- 一个居民最多持有一份 active/on_leave 合同；录用前校验，冲突则拒绝。

## R25 经理审核权限

- 只有 `company.manager_agent_id` 可以审核本企业申请。
- 审核决策（accept/reject）由真实 LLM 作出；引擎只做硬性校验 （申请有效、有空缺、企业正常、权限正确、无重复合同）。

## R26 班次生成

- 班次只为 `active` 合同生成；只在岗位工作日（`working_days_json`，`% 7`）生成。
- 同一 `(employment_id, scheduled_start)` 不重复生成（幂等，存档恢复后同样成立）。
- 生成时注册缺勤检查（`scheduled_start + 120`）并发布 `shift_scheduled`。

## R27 签到窗口与迟到

- 允许提前 30 分钟、允许迟到 120 分钟：
  `scheduled_start - 30 ≤ 世界时间 ≤ scheduled_start + 120`，窗口外拒绝。
- 要求：班次属于该居民、合同 `active`、居民空闲（R1）、居民位于企业地点。
- `late_minutes = max(actual_start - scheduled_start, 0)`；迟到记合同
  `late_shifts + 1`、`attendance_score - 2`（下限 0）。

## R28 缺勤判定

- `scheduled_start + 120` 时班次仍为 `scheduled` → 自动转 `absent`，
  `wage_due = 0`；合同 `absent_shifts + 1`、`attendance_score - 10`（下限 0）。
- 由调度器判定，不依赖 LLM 主动承认；判定后生成下一班次。

## R28.5 请假

- 员工可对未开始的班次（`scheduled`）申请请假；同一班次最多一条待审批申请。
- 经理审批：准假 → 班次转 `leave`（不判缺勤、`wage_due = 0`、不发工资）， 并生成下一空槽班次；拒绝 → 班次保持 `scheduled`。
- 缺勤判定时待审批申请自动转 `expired`；辞职时转 `cancelled`。
- 请假事件与求职申请一样提升经理决策优先级。

## R29 工资支付

- `wage_due = wage_per_shift × min(worked, scheduled) // scheduled`（向下取整， 最小单位 1）；只按实际工作分钟支付，不做带薪假、不做绩效奖金。
- 企业余额充足：企业扣款、员工入账、班次 `paid`，`CompanyTransaction`
  （`wage_payment`，负额）与 `Transaction`（`work_wage`，正额）使用同一
  `trace_id`。
- 余额不足：班次 `unpaid`，欠薪记入合同 `unpaid_wage` 与企业
  `unpaid_wage_total`，不凭空发钱；欠薪不因辞职/合同终止消失。
- 支付处理器必须幂等：班次状态非 `in_progress`/`late` 直接返回， 同一班次绝不重复支付。

## R30 正式工作产物

- 正式班次完成的产物全部进入 `CompanyInventory`，不进个人背包；产量按
  `world_data/jobs/jobs.json` 的配方（`inputs`/`products`）结算，旧存档
  无配方的 job 回退到 `job.products_json` 兼容路径。
- 库存变化随 `company_inventory_changed` 事件发布（完整列表，含预留数量）。

## R31 辞职

- 仅员工本人可辞职；合同转 `resigned`，未来 `scheduled` 班次转 `cancelled`。
- 招聘空缺恢复并重开；欠薪保留；双方获得记忆；发布 `employment_resigned`。

## R32 解雇、暂停与停业

- `terminate_employment` 仅经理可操作、不能解雇他企业员工、不能重复终止； 未来班次取消、名额恢复；欠薪不消失。
- 企业 `suspended` 停止招聘与排班但保留企业（未来 scheduled 班次取消、 进行中班次照常完成但不续排）；恢复后重新招聘并为缺班次的合同续排。
- `bankrupt`：资不抵薪且满足破产条件（后续定义）；上帝注资（`god_injection`
  流水）可恢复经营并立即补发欠薪。

## R33 企业销售

- `Store.company_id` 绑定企业；居民购买时同一事务内：居民扣钱、商店库存减少、 居民获得商品、企业余额增加、双流水 +
  `company_sale_completed` 事件。
- 居民出售给商店：企业必须有足够资金（校验先于库存更新，避免失败事务残留 库存变更），不足则拒绝交易（不赊账 R7）；企业扣款写
  `material_purchase`
  流水 + `company_money_changed` 事件。

## R34 企业事件与流水（完整列表见 event-protocol）

- 企业/招聘/申请/合同/班次/工资/库存/销售全部有对应事件；事件 payload 必须 包含关联
  ID（company_id、employment_id、shift_id、agent_id、amount）。
- 同一事务内事件共享 `trace_id` 与 `world_time`。

## R35 存档恢复

- 存档版本为 2：保存企业、岗位、招聘、申请、合同、班次、请假、企业库存、企业流水。
- V1 存档迁移：保留旧工作历史，按种子重建企业，旧历史不转正式合同。
- 恢复后：班次不重复生成（R26 幂等）、工资不重复支付（R29 幂等）、 事件 sequence 连续；班次/合同/调度器 payload 的全局主键引用重映射后仍命中。
- M16 静态数据补种幂等：缺行的地点/岗位/商品规则按 world_data 补齐，已有行不动；
  恢复后再次 `ensure_seeded` 不产生新行。

## R36 配置化跨企业采购

- 采购规则来自 `world_data/companies/companies.json` 的 `procurement` 列表：
  `(item_id, seller_company_id, unit_price, max_quantity_per_order)`，价格由服务器固定，
  不接受议价或自定义价格。
- `purchase_company_goods` 仅买方企业经理可调用；同一事务内：卖方可用库存
  （`quantity - reserved_quantity`）条件扣减、买方可用库存增加、买方扣款卖方入账、
  双方 `material_purchase`/`wholesale_sale` 流水 + 事件。
- 任一新校验失败整单回滚；并发采购最后可用库存时恰一单成功（同 R4 的
  BEGIN IMMEDIATE + 条件 UPDATE 模式）。

## R37 正式生产原料预留与消耗

- 配方（`inputs`）来自 `world_data/jobs/jobs.json`；签到（`start_shift`）时在同一事务内
  条件 UPDATE 预留 `CompanyInventory.reserved_quantity += qty`（guard：`quantity >= qty`）；
  任一原料不足则拒绝签到，班次保持 `scheduled`、不排完成回调、预留全部回滚。
- 预留只发生在签到、消耗只发生在班次完成：`handle_shift_completed` 按
  `min(reserved, qty)` 消耗并同步扣减 `quantity` 与 `reserved_quantity`，绝不减负；
  产出按配方进入企业库存并发布 `company_production_completed`。
- 缺勤/请假/辞职/解雇/停业只作用于 `scheduled` 班次，因此不存在「已预留但永不完成」
  的路径；`formal_only` 的 job 拒绝普通 `work()` 路径。

## R38 企业仓库上架

- `stock_store` 仅企业经理可调用，商店必须绑定本企业（R33 的
  `Store.company_id`）；同一事务内仓库可用库存条件扣减、货架条件增加
  （`stock + qty <= stock_cap`），无资金转移。
- 任一条件失败整单回滚；发布 `company_store_stocked` 与仓库侧
  `company_inventory_changed`。
