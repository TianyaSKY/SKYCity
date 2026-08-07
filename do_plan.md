# AI 小世界完整项目计划

项目定位：

> 一个采用 **Tiny Farm 像素素材**的 2D 上帝视角 AI 世界。每个居民由身份卡、状态、记忆、关系和真实 LLM
> 驱动，通过工具完成移动、对话、工作、消费等行为；玩家负责观察、控制时间和干预世界。

整个项目按一条原则推进：

> **每个里程碑结束后，都必须有一个可以运行、可以观察、可以验收的版本。**

---

# 一、最终技术路线

| 模块          | 技术                           |
|---------------|--------------------------------|
| Web 前端      | Vue 3、TypeScript、Vite、Pinia |
| 世界渲染      | PixiJS 8                       |
| 地图编辑      | Tiled，导出 JSON               |
| 后端          | Python、FastAPI、Pydantic      |
| LLM 框架      | OpenAI Agents SDK              |
| 工具定义      | `@function_tool`               |
| 实时通信      | WebSocket                      |
| 数据访问      | SQLAlchemy 2                   |
| 初期数据库    | SQLite                         |
| 数据迁移      | Alembic                        |
| 调度系统      | 离散事件调度器                 |
| 日志          | Loguru                         |
| 测试          | Pytest、Vitest、Playwright     |
| Python 包管理 | uv                             |

---

# 二、架构边界

## 1. LLM 负责什么

LLM 只负责：

* 根据身份、需求和记忆选择行动
* 决定去哪里
* 决定和谁说话
* 生成真正说出口的话
* 在多个目标之间做选择
* 根据行动失败结果调整下一步行为

LLM 不负责：

* 修改数据库
* 判断道路是否可通行
* 自行扣钱或增加物品
* 宣布工作完成
* 改变其他智能体状态
* 修改世界时间
* 决定工具调用一定成功

## 2. 世界引擎负责什么

世界引擎掌握唯一真实状态：

```text
LLM 产生行动意图
        ↓
@function_tool 接收参数
        ↓
ActionExecutionService 验证世界规则
        ↓
事务修改数据库
        ↓
产生 WorldEvent
        ↓
WebSocket 推送给前端
        ↓
PixiJS 播放结果
```

即使 LLM 调用：

```json
{
  "destination_id": "village_shop",
  "reason": "我饿了，想去购买面包"
}
```

系统仍然要验证：

* 当前是否正在执行其他行动
* 商店是否存在
* 地点是否开放
* 是否存在有效路径
* 移动需要多少游戏时间

## 3. 前端负责什么

前端只是世界的观察窗口：

* 展示地图和智能体
* 播放移动动画
* 显示对话气泡
* 展示身份卡、状态和关系
* 展示事件流
* 提交上帝操作
* 暂停、恢复和调整时间速度

前端不保存权威状态。

---

# 三、项目目录

```text
ai-tiny-world/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   │
│   │   ├── api/
│   │   │   ├── worlds.py
│   │   │   ├── agents.py
│   │   │   ├── events.py
│   │   │   ├── god_actions.py
│   │   │   └── websocket.py
│   │   │
│   │   ├── agents/
│   │   │   ├── agent_factory.py
│   │   │   ├── context.py
│   │   │   ├── instructions.py
│   │   │   └── tools/
│   │   │       ├── movement.py
│   │   │       ├── conversation.py
│   │   │       ├── commerce.py
│   │   │       └── daily_life.py
│   │   │
│   │   ├── domain/
│   │   │   ├── agent.py
│   │   │   ├── world.py
│   │   │   ├── location.py
│   │   │   ├── item.py
│   │   │   ├── job.py
│   │   │   ├── memory.py
│   │   │   ├── relationship.py
│   │   │   └── event.py
│   │   │
│   │   ├── schemas/
│   │   │   ├── actions.py
│   │   │   ├── events.py
│   │   │   ├── snapshots.py
│   │   │   └── websocket.py
│   │   │
│   │   ├── services/
│   │   │   ├── agent_decision_service.py
│   │   │   ├── observation_service.py
│   │   │   ├── action_execution_service.py
│   │   │   ├── conversation_service.py
│   │   │   ├── memory_service.py
│   │   │   ├── relationship_service.py
│   │   │   └── god_action_service.py
│   │   │
│   │   ├── world_engine/
│   │   │   ├── engine.py
│   │   │   ├── clock.py
│   │   │   ├── scheduler.py
│   │   │   ├── event_bus.py
│   │   │   └── locks.py
│   │   │
│   │   ├── repositories/
│   │   │   ├── agent_repository.py
│   │   │   ├── world_repository.py
│   │   │   ├── inventory_repository.py
│   │   │   ├── memory_repository.py
│   │   │   └── event_repository.py
│   │   │
│   │   ├── database/
│   │   │   ├── models/
│   │   │   ├── session.py
│   │   │   └── unit_of_work.py
│   │   │
│   │   └── config/
│   │       └── settings.py
│   │
│   ├── migrations/
│   ├── tests/
│   ├── pyproject.toml
│   └── .env.example
│
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── stores/
│   │   ├── views/
│   │   ├── pixi/
│   │   │   ├── WorldRenderer.ts
│   │   │   ├── AssetLoader.ts
│   │   │   ├── TiledMapLoader.ts
│   │   │   ├── AgentSprite.ts
│   │   │   ├── MovementAnimator.ts
│   │   │   ├── CameraController.ts
│   │   │   └── layers/
│   │   ├── websocket/
│   │   └── types/
│   ├── public/
│   │   └── assets/
│   └── package.json
│
├── world_data/
│   ├── maps/
│   ├── identities/
│   ├── locations/
│   ├── items/
│   ├── jobs/
│   └── seed/
│
├── assets/
│   ├── tiny_farm/
│   ├── characters/
│   ├── portraits/
│   ├── ui/
│   └── licenses/
│
├── docs/
│   ├── architecture.md
│   ├── world-rules.md
│   ├── map-specification.md
│   ├── agent-prompt.md
│   └── event-protocol.md
│
└── README.md
```

---

# 四、第一版明确范围

第一版包含：

```text
1 个 Tiny Farm 小镇
3～5 个智能体
5～8 个地点
10～20 种物品
2～4 种工作
真实 LLM 决策
移动、对话、工作、购买、使用物品、等待
时间系统
事件调度
短期与重要记忆
人物关系
上帝干预
存档和恢复
```

第一版暂时不做：

* 3D
* 玩家化身
* 多人联机
* 多世界服务器集群
* 战斗系统
* 婚姻和生育系统
* 自由建造
* 实时生成地图
* 向量数据库
* 数百个智能体
* 让 LLM 自由编写或执行代码
* 一个调用中让两个 LLM 无限对话

---

# 五、里程碑总览

| 里程碑 | 可运行成果                      |
|--------|---------------------------------|
| M0     | Tiny Farm 地图和素材规范完成    |
| M1     | 前后端骨架运行，地图显示        |
| M2     | 世界状态、时钟和 WebSocket 打通 |
| M3     | 第一个真实 LLM 智能体可以移动   |
| M4     | 多智能体可以见面和对话          |
| M5     | 工作、金钱、商店和消费闭环      |
| M6     | 记忆、关系和持续行为            |
| M7     | 完整上帝视角与世界干预          |
| M8     | 稳定性、成本控制和可观测性      |
| M9     | 存档、测试、打包和首个正式版本  |

依赖关系：

```text
M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9
```

---

# 里程碑 M0：素材与地图规范

## 目标

把 Tiny Farm 从“图片资源”变成程序可以理解的世界。

## 工作内容

在 Tiled 中建立第一张地图，并统一图层名称：

```text
ground
ground_detail
buildings
decorations_low
collision
navigation
locations
interactables
spawn_points
foreground
```

对象层定义：

### `locations`

```json
{
  "location_id": "village_shop",
  "name": "村庄杂货店",
  "location_type": "store",
  "capacity": 8,
  "open_hour": 8,
  "close_hour": 20
}
```

### `spawn_points`

```json
{
  "spawn_id": "agent_linxia_home",
  "agent_id": "agent_linxia",
  "direction": "down"
}
```

### `interactables`

```json
{
  "object_id": "shop_counter",
  "object_type": "store_counter",
  "location_id": "village_shop"
}
```

同时建立素材清单：

```json
{
  "alias": "tiny_farm_tiles",
  "file": "tiny_farm/tileset.png",
  "tile_width": 16,
  "tile_height": 16,
  "license": "项目素材对应许可证"
}
```

## 产物

* `tiny_world.tmj`
* 独立 `.tsj` Tileset
* Tiny Farm PNG
* `asset-manifest.json`
* `map-specification.md`
* 一张能在 Tiled 中完整查看的小镇地图

Tiled 官方建议将 Tileset 保存为独立文件，方便多个地图复用。 ([Tiled Documentation][5])

## 验收标准

* 地图可正常导出 JSON
* 所有地点都有稳定 ID
* 碰撞区与可行走区已经标记
* 至少有住宅、商店、农场、广场和工作地点
* 不依赖程序代码也能看懂地图结构

---

# 里程碑 M1：项目骨架与地图显示

## 目标

建立完整前后端工程，浏览器能够显示 Tiny Farm 地图。

## 后端任务

* 初始化 FastAPI
* 初始化配置系统
* 接入 Loguru
* 创建 `/health`
* 创建数据库连接
* 初始化 Alembic
* 设置统一异常处理
* 设置 CORS
* 创建世界配置加载器

## 前端任务

* 初始化 Vue 3、Vite、TypeScript
* 初始化 Pinia
* 创建世界主界面
* 初始化 PixiJS `Application`
* 加载 Tiny Farm 资源
* 解析 Tiled JSON
* 渲染瓦片图层
* 实现整数倍缩放
* 设置 `nearest` 像素采样
* 实现基础摄像机拖动和缩放

PixiJS 支持通过资源 Manifest 和 Bundle 管理成组素材，适合后续按地图或界面分包加载。 ([PixiJS][6])

## 验收标准

打开网页后能够：

* 看见完整小镇地图
* 拖动画面
* 放大和缩小
* 点击地图地点
* 查看鼠标所在瓦片坐标
* 前端可以访问后端 `/health`

此阶段还没有智能体，也不调用 LLM。

---

# 里程碑 M2：世界状态、时间与事件系统

## 目标

建立不依赖 LLM 的世界基础设施。

## 核心领域模型

```text
World
WorldClock
Location
AgentIdentity
AgentState
WorldEvent
ScheduledAction
```

## 数据库表

```text
worlds
agents
agent_states
locations
scheduled_actions
world_events
```

## 世界时钟

世界时间使用游戏分钟：

```python
world_time = 8 * 60  # 08:00
```

不把真实时间当成游戏时间。

支持：

```text
暂停
恢复
1×
2×
5×
10×
```

## 离散事件调度

事件示例：

```json
{
  "event_type": "movement_completed",
  "execute_at": 510,
  "agent_id": "agent_linxia",
  "payload": {
    "destination_id": "village_shop"
  }
}
```

世界引擎不需要每帧检查所有角色，而是取出已到执行时间的事件。

## WebSocket 事件

统一协议：

```json
{
  "event_id": "evt_001",
  "sequence": 81,
  "world_id": "world_001",
  "world_time": 510,
  "type": "agent_move_started",
  "payload": {}
}
```

第一批事件类型：

```text
world_snapshot
world_time_changed
world_paused
world_resumed
agent_state_changed
agent_move_started
agent_move_completed
world_event_created
```

## 前端任务

* 建立 WebSocket 客户端
* 自动断线重连
* 按 `sequence` 处理事件
* 检测遗漏事件
* 根据开始和结束位置播放移动
* 显示世界时间
* 显示事件流

## 验收标准

不用 LLM，通过测试 API 可以：

1. 创建移动任务。
2. 后端安排完成事件。
3. WebSocket 推送移动开始。
4. 人物在地图上平滑移动。
5. 完成后更新数据库位置。
6. 刷新页面后位置仍然正确。

---

# 里程碑 M3：第一个真实 LLM 智能体

## 目标

打通完整纵向链路：

```text
身份卡
→ 观察
→ LLM
→ 工具
→ 世界验证
→ 数据变化
→ WebSocket
→ 地图动画
```

## 智能体身份卡

```json
{
  "id": "agent_linxia",
  "name": "林夏",
  "age": 24,
  "occupation": "农场帮工",
  "background": "希望攒钱经营自己的花圃",
  "values": ["诚实", "稳定", "友谊"],
  "long_term_goals": ["存下 2000 金币", "建立自己的花圃"],
  "speaking_style": "温和、简短",
  "personality": {
    "openness": 0.65,
    "conscientiousness": 0.82,
    "extraversion": 0.48,
    "agreeableness": 0.78,
    "emotional_stability": 0.61
  }
}
```

## 首批工具

只开放两个：

```python
@function_tool
async def move(...):
    ...

@function_tool
async def wait(...):
    ...
```

这里统一使用 SDK 官方名称 `@function_tool`，不自行定义另一套 `@tool`。SDK 会从 Python 签名、类型和 docstring 生成工具定义。
([OpenAI GitHub Pages][4])

## 运行上下文

```python
@dataclass(slots=True)
class AgentToolContext:
    world_id: str
    agent_id: str
    action_service: ActionExecutionService
```

`agent_id` 由服务端注入，不作为工具参数交给模型，避免角色冒充其他人。

## 观察构建

每次只提供：

* 当前时间
* 当前天气
* 当前状态
* 当前需求
* 当前地点
* 可见地点
* 可见人物
* 当前可以做的事情
* 相关记忆
* 最近一次工具结果

不提供完整数据库和其他地点的秘密信息。

## 决策策略

```text
一次决策
=
一次 Runner.run
+
最多一次有效世界行动
```

配置：

```python
ModelSettings(
    tool_choice="required",
    parallel_tool_calls=False,
)
```

并限制最大轮数，防止工具循环。

## LLM 运行记录

保存：

```text
agent_id
world_time
model
input_tokens
output_tokens
latency
selected_tool
tool_arguments
tool_result
success
error_type
```

不保存模型隐藏推理，只保存可审计的输入摘要、工具调用和结果。

## 验收标准

一个智能体能够：

* 根据身份和状态选择移动或等待
* 不能移动到不存在的地点
* 工具失败后可以在下一次决策中调整
* 地图同步播放移动
* 每次决策都有记录
* LLM 故障时自动降级为等待，不导致世界崩溃

---

# 里程碑 M4：多智能体与对话

## 目标

让 3～5 个智能体在同一个世界内相遇和交流。

## 新增工具

```python
@function_tool
async def talk(
    ctx,
    target_agent_id: str,
    message: str,
    intent: TalkIntent,
) -> str:
    ...
```

## 对话机制

A 对 B 说话时：

```text
A 调用 talk
→ 系统验证距离
→ 创建 conversation_message 事件
→ 前端显示气泡
→ B 的观察中加入收到的消息
→ 提高 B 的下一次决策优先级
→ B 自己决定是否回应
```

不要在一个工具中直接运行 B 的 LLM。

## 防止无限对聊

加入：

* 同一对角色对话冷却时间
* 单次会话最大轮数
* 连续对话后的需求衰减
* 工作和饥饿等目标重新获得优先级
* 相同内容重复检测

## 前端任务

* 对话气泡
* 对话历史面板
* 点击人物显示最近交流
* 当前交谈对象高亮
* 对话开始和结束状态

## 验收标准

* 角色只能和附近人物说话
* 对方能记住刚收到的消息
* 对方可以回应、忽略或离开
* 对话不会无限自循环
* 3～5 个角色可以并行自主运行

---

# 里程碑 M5：工作、金钱和消费闭环

## 目标

让资源有限，从而产生有意义的选择。

## 新增领域对象

```text
Item
Inventory
Store
StoreProduct
Job
Employment
Transaction
```

## 新增数据库表

```text
items
inventories
inventory_items
stores
store_products
jobs
employments
transactions
```

## 新增工具

```text
buy_item
sell_item
work
use_item
```

农场行为第一版统一抽象成 `work(job_id)`：

```text
浇水
播种
收获
商店值班
送货
修理栅栏
```

不要立即给每个工作都创建一个独立工具，否则工具表面会迅速膨胀。

## 最小经济闭环

```text
智能体工作
→ 获得工资或产物
→ 商店收购产物
→ 智能体购买食物
→ 使用食物降低饥饿
→ 商店库存减少
```

## 并发规则

购买最后一件商品时：

* 数据库事务加锁
* 第一个事务成功
* 后一个返回库存不足
* 不能只依靠 LLM 预先判断

## 验收标准

至少可以产生这条自主行为链：

```text
智能体发现饥饿
→ 去商店
→ 发现钱不够
→ 去工作
→ 获得工资
→ 返回商店购买
→ 使用食物
→ 饥饿下降
```

---

# 里程碑 M6：记忆、关系与持续人格

## 目标

让角色不是每次调用都像刚出生。

## 记忆类型

### 工作记忆

最近若干事件：

```text
刚刚购买失败
张明邀请我去市场
今天上午需要完成浇水
```

### 情节记忆

具体发生过的重要事件：

```text
张明在第 2 天借给我 50 金币
我因为迟到被扣除了工资
```

### 语义印象

角色逐渐形成的长期判断：

```text
张明通常愿意帮助我
杂货店下午经常缺少面包
```

## 第一版检索方式

不引入向量数据库，采用加权检索：

```text
总分 =
实体匹配
+ 关键词匹配
+ 重要性
+ 新近程度
+ 未解决程度
```

等记忆量明显增大，再考虑 Embedding 和向量索引。

## 关系模型

```json
{
  "source_agent_id": "agent_linxia",
  "target_agent_id": "agent_zhangming",
  "familiarity": 35,
  "trust": 18,
  "affection": 12,
  "resentment": 0,
  "debt": 50
}
```

关系是有方向的：

```text
林夏信任张明
≠
张明同样信任林夏
```

## 关系更新原则

LLM 不直接返回：

```json
{"trust_change": 20}
```

系统根据事件、意图和规则计算关系变化，LLM 只负责角色行为。

## 每日反思

每天结束时为每个角色产生简短总结：

```text
今天发生了什么
哪些目标有进展
哪些关系发生变化
明天最重要的事情是什么
```

反思调用应独立限频，不和普通行动共用无限预算。

## 验收标准

* 角色能提到此前真实发生的事件
* 不会记住未观察到的信息
* 对借钱、欺骗、帮助等事件产生持续影响
* 重启服务后记忆仍然存在
* 同一角色在多天运行后仍保持身份和语言风格

---

# 里程碑 M7：上帝视角与干预系统

## 目标

完成玩家真正使用的核心界面。

## 观察功能

玩家可以查看：

* 世界时间和天气
* 地点及在场人物
* 智能体身份卡
* 当前需求
* 当前目标
* 当前行动
* 背包和资金
* 人际关系
* 近期记忆
* 最近一次 LLM 工具调用
* 世界事件流

## 上帝操作

第一版包含：

```text
暂停世界
恢复世界
调整速度
改变天气
发放或扣除金钱
生成物品
召集居民
移动智能体
创建公共事件
改变商店库存
```

## 上帝命令结构

```json
{
  "command_id": "cmd_001",
  "command_type": "grant_money",
  "target_id": "agent_linxia",
  "parameters": {
    "amount": 100
  },
  "reason": "玩家干预"
}
```

所有干预都必须：

* 经过 Service
* 产生审计记录
* 产生世界事件
* 推送给前端
* 进入受影响角色的观察或记忆

## 前端布局

```text
┌──────────────────────────────────────────┐
│ 时间、天气、速度、暂停                    │
├────────────────────────┬─────────────────┤
│                        │ 智能体身份卡     │
│                        │ 状态与需求       │
│      PixiJS 地图       │ 记忆与关系       │
│                        │ LLM 决策记录     │
│                        │ 上帝干预         │
├────────────────────────┴─────────────────┤
│ 世界事件流 / 对话记录                     │
└──────────────────────────────────────────┘
```

## 验收标准

玩家不打开后端日志，也可以理解：

* 谁在哪里
* 正在做什么
* 为什么大致这样做
* 行动是否成功
* 世界刚刚发生了什么
* 玩家干预造成了什么后果

---

# 里程碑 M8：稳定性、成本和可观测性

## 目标

让世界可以持续运行，而不是演示五分钟就散架。

## LLM 并发控制

使用全局信号量：

```text
最多 N 个 LLM 请求同时执行
```

每个智能体还需要：

```text
is_deciding
next_decision_at
last_decision_at
consecutive_failures
daily_token_usage
daily_call_count
```

## 调用触发原则

只有这些情况调用 LLM：

* 当前行动完成
* 收到重要对话
* 计划被打断
* 需求达到阈值
* 重要世界事件发生
* 定时重新评估

以下情况不调用：

* 每一动画帧
* 每一个游戏分钟
* 单纯更新坐标
* 饥饿值增加 1
* 人物正在长时间工作

## 故障降级

```text
超时
→ 重试一次

仍失败
→ wait 10～30 游戏分钟

连续失败
→ 延长下一次决策时间

服务恢复
→ 正常加入调度
```

## 成本控制

* 限制观察文本长度
* 只检索少量相关记忆
* 不重复发送完整身份卡之外的静态世界说明
* 普通行动使用较经济模型
* 重要反思或复杂对话才使用更强模型
* 对相同观察结果设置短时缓存
* 记录每个智能体的 Token 使用
* 设置每个世界的调用预算
* 世界暂停后禁止产生新决策请求

## 可观测性

每次决策关联一个 `trace_id`：

```text
调度事件
→ 观察构建
→ LLM 请求
→ 工具调用
→ Service 执行
→ 数据库事务
→ WorldEvent
→ WebSocket
```

Agents SDK 的 `Runner` 负责智能体和工具循环，官方还提供运行追踪能力，适合辅助检查模型选择了什么工具及调用过程。
([OpenAI GitHub Pages][7])

## 验收标准

* 单个智能体不会重复并发决策
* 请求超时不会阻塞整个世界
* 工具重复提交不会重复扣钱
* WebSocket 重连后能获取最新快照
* 长时间模拟过程中没有行动永久卡死
* 可以按智能体查询调用次数、错误率和消耗
* 所有世界状态变化都能追溯到事件或命令

---

# 里程碑 M9：测试、存档与首个正式版本

## 目标

形成一个可以稳定保存、恢复和演示的完整版本。

## 后端测试

### 单元测试

```text
路径验证
余额验证
库存验证
工作奖励
物品使用
关系变化
记忆评分
事件调度
世界时钟
```

### 集成测试

```text
LLM 工具调用 → Service → 数据库
移动事件 → WebSocket
购买事务并发
对话消息投递
上帝操作审计
存档恢复
```

### LLM 测试

测试环境使用 Mock Provider：

```python
class FakeLLMProvider:
    async def decide(...):
        return predefined_decision
```

不能让所有自动化测试都调用真实模型，否则测试会慢、贵且不稳定。

保留少量真实模型冒烟测试：

* 能调用合法工具
* 能识别可见地点
* 不把心理活动写进 `talk.message`
* 工具失败后能调整行为

## 前端测试

* 地图是否正确初始化
* WebSocket 事件是否更新 Store
* 智能体动画是否完成
* 身份卡是否正确切换
* 上帝操作是否提交正确
* 断线是否恢复
* 快照是否覆盖陈旧状态

## 存档

存档包含：

```text
世界时间
天气
智能体身份与状态
背包
商店库存
关系
记忆
未完成行动
待执行事件
随机种子
世界配置版本
```

地图素材不复制进每个存档，只记录地图版本。

## 可重放事件

保留：

```text
初始快照
+
按 sequence 排序的世界事件
```

用于排查：

* 人物为何突然没钱
* 某次购买为何重复
* 某个角色为何瞬移
* 重启后状态为何不一致

## 验收标准

最终版本能够完成：

1. 创建一个新世界。
2. 初始化 3～5 个智能体。
3. 由真实 LLM 自主行动。
4. 连续经历多个游戏日。
5. 发生移动、对话、工作、消费和关系变化。
6. 玩家能够暂停和干预。
7. 退出后保存。
8. 重启后恢复相同状态。
9. 查看完整事件和 LLM 行动记录。

---

# 六、核心 API 规划

## 世界接口

```http
POST /api/worlds
GET  /api/worlds/{world_id}
GET  /api/worlds/{world_id}/snapshot
POST /api/worlds/{world_id}/pause
POST /api/worlds/{world_id}/resume
POST /api/worlds/{world_id}/speed
```

## 智能体接口

```http
GET /api/worlds/{world_id}/agents
GET /api/worlds/{world_id}/agents/{agent_id}
GET /api/worlds/{world_id}/agents/{agent_id}/memories
GET /api/worlds/{world_id}/agents/{agent_id}/relationships
GET /api/worlds/{world_id}/agents/{agent_id}/decisions
```

## 事件接口

```http
GET /api/worlds/{world_id}/events
GET /api/worlds/{world_id}/events?after_sequence=100
```

## 上帝接口

```http
POST /api/worlds/{world_id}/god-actions
```

## WebSocket

```http
WS /ws/worlds/{world_id}
```

连接建立后，先返回完整快照，再推送增量事件。

---

# 七、第一版工具清单

```text
move
talk
buy_item
sell_item
work
use_item
wait
```

每个工具都遵守相同结构：

```python
@function_tool
async def some_action(
    ctx: RunContextWrapper[AgentToolContext],
    ...
) -> str:
    result = await ctx.context.action_service.some_action(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        ...
    )
    return result.model_dump_json()
```

工具层不应：

* 直接写 SQL
* 直接操作 ORM Model
* 自己发送 WebSocket
* 自己修改关系
* 自己调度下一个 LLM
* 捕获并吞掉全部异常

---

# 八、必须提前写下来的世界规则

在开发前创建 `docs/world-rules.md`，至少回答这些问题：

```text
一个智能体能否同时执行两个行动？
移动中能否对话？
工作中能否被打断？
两个角色同时买最后一个商品怎么办？
世界暂停时已发出的 LLM 请求怎么办？
天气如何影响移动？
角色没钱时能否欠款？
商店关门后能否进入？
对话距离是多少？
一次工作何时发工资？
饥饿到 100 会发生什么？
精力为 0 会发生什么？
上帝命令是否可以违反普通规则？
```

这些规则必须由程序实现，不要留给 Prompt 临场发挥。

---

# 九、推荐开工顺序

真正编写代码时，不要先批量创建几十个空文件。按下面顺序做第一条纵向链路：

```text
1. M0：完成 Tiled 地图
2. 加载地图到 PixiJS
3. 创建一个智能体状态
4. 手动触发一次移动
5. 用 WebSocket 播放移动
6. 接入 OpenAI Agents SDK
7. 创建 move 和 wait 工具
8. 让 LLM 触发同一套移动逻辑
9. 再加入第二个智能体
10. 再实现 talk
```
