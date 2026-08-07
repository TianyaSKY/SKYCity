# AI Tiny World（AI 小世界）

一个采用 **Tiny Farm 像素素材**的 2D 上帝视角 AI 世界。每个居民由身份卡、状态、记忆、关系和真实 LLM
驱动，通过工具完成移动、对话、工作、消费等行为；玩家负责观察、控制时间和干预世界。

项目按 10 个里程碑（M0~M9）推进，每个里程碑都有可运行、可观察、可验收的版本。完整的架构与规则契约见 `docs/`。

## 功能一览

- 64×40 瓦片小镇地图（Tiled JSON），PixiJS 整数倍缩放渲染
- 世界时钟（游戏分钟制）：暂停 / 恢复 / 1× / 2× / 5× / 10×
- 6 个智能体（每人一份角色卡，可自行添加），由 LLM 自主决策：移动、等待、对话、工作、购买、出售、使用物品
- 真实对话：气泡、历史面板、交谈高亮、防无限对聊
- 经济闭环：饱食度下降 → 工作 → 工资 → 商店购买 → 进食
- 夜间作息：精力/心情按小时消耗，睡觉大幅恢复（+40 精力 / +20 心情每小时）； 有家回自己家睡，无家去小镇旅店（每晚 15
  金币，余额不足不赊账）
- 记忆系统（工作/情节/语义记忆 + 加权检索）与方向性人际关系
- 每日反思（独立限频调用）
- 上帝视角：暂停/调速/改天气/发钱/给物品/传送/公共事件/改商店库存，全程审计
- 稳定性：LLM 并发信号量、超时重试、故障降级、Token 预算、观察缓存、trace_id 全程可溯
- 存档 / 恢复 / 事件重放

## 快速开始

### 1. 后端（FastAPI + SQLite）

```bash
cd backend
export PATH="$HOME/.local/bin:$PATH"   # 若 uv 不在 PATH
uv sync                                 # 安装依赖（自动下载 Python）
cp .env.example .env                    # 按需修改（可选）
```

```bash
cd backend
uv run uvicorn app.main:app --port 8000
```

### 2. 前端（Vue 3 + Vite + PixiJS）

```bash
cd frontend
npm install
npm run dev                             # http://localhost:5173
```

打开 http://localhost:5173 即可看到小镇。页面会自动创建/加入一个世界；想开启 LLM 自主行动，可在后端创建自主世界：

```bash
curl -X POST localhost:8000/api/worlds \
  -H 'Content-Type: application/json' \
  -d '{"name":"晨露村庄","autonomous":true}'
curl -X POST localhost:8000/api/worlds/world_001/speed \
  -H 'Content-Type: application/json' -d '{"speed":10}'
```

注意：SDK 默认走 Responses API，而第三方服务通常只实现 `/chat/completions`，因此本项目固定走 chat completions（
`LLM_USE_RESPONSES=false`）。若你的服务商明确支持 `/responses`，可改为 `true`。

| 环境变量                   | 默认          | 说明                                                |
|----------------------------|---------------|-----------------------------------------------------|
| `OPENAI_API_KEY`           | —             | 真实 LLM 密钥（第三方兼容 key 亦可）                |
| `OPENAI_BASE_URL`          | —             | 第三方 API 地址（OpenAI 兼容）；不设则连官方 OpenAI |
| `LLM_PROVIDER`             | `auto`        | `auto` / `openai` / `fake`                          |
| `LLM_MODEL`                | `gpt-4o-mini` | 普通行动模型（第三方填服务商模型名）                |
| `LLM_REFLECT_MODEL`        | `gpt-4o-mini` | 每日反思模型                                        |
| `LLM_USE_RESPONSES`        | `false`       | 是否用 Responses API（第三方兼容服务保持 `false`）  |
| `LLM_MAX_CONCURRENT`       | `2`           | 全局并发上限                                        |
| `WORLD_DAILY_TOKEN_BUDGET` | `0`           | 每世界每日 Token 预算（0=不限）                     |

## 添加新智能体

每个智能体只有一份数据：`world_data/identities/agent_xxx.json` 角色卡，包含身份（姓名/年龄/职业/背景/五因素）、`spawn`
出生点和可选的 `home`。出生点与住宅由 `tools/build_map.py` 从角色卡派生到地图，引擎也只读角色卡——加一个智能体只需写一个文件：

1. 新建 `world_data/identities/agent_xxx.json`（参考现有角色卡）：

   ```json
   {
     "id": "agent_xxx",
     "name": "名字", "age": 30, "occupation": "职业",
     "background": "背景故事", "values": [], "long_term_goals": [],
     "speaking_style": "说话风格",
     "personality": {
       "openness": 0.5, "conscientiousness": 0.5, "extraversion": 0.5,
       "agreeableness": 0.5, "emotional_stability": 0.5
     },
     "initial_money": 50,
     "spawn": { "col": 33, "row": 20, "direction": "down" },
     "home": { "location_id": "xxx_home", "name": "XXX的家", "col": 33, "row": 19 }
   }
   ```

   `home` 可省略（无家的智能体出生在出生点格）；`initial_money` 默认 50。`spawn` 必填，`id` 必须等于文件名。

2. 重新生成地图（可选，仅同步 tmj 里的可视化出生点/住宅；引擎只读角色卡，不跑也能建世界）：

   ```bash
   uv run --with pillow python tools/build_map.py
   ```

3. 重建世界让新智能体入场（智能体只在建世界时播种）：

   ```bash
   curl -X DELETE localhost:8000/api/worlds/world_001
   curl -X POST localhost:8000/api/worlds \
     -H 'Content-Type: application/json' \
     -d '{"name":"晨露村庄","autonomous":true}'
   ```

   前端无需改动：精灵、名牌、气泡都按快照动态渲染。新智能体自动使用现有的工作 / 商店 / 物品（要新工种就加
   `world_data/jobs/jobs.json`，`location_id` 必须指向地图上存在的地点）。

## 测试

```bash
cd backend && uv run pytest tests/ -q          # 123+ 个后端测试
cd frontend && npm run test                    # Vitest 单元测试
cd frontend && npm run test:e2e                # Playwright 冒烟（需前后端已启动）
```

真实模型冒烟测试（`test_llm_smoke.py`）在无 `OPENAI_API_KEY` 时自动跳过。

## 目录结构

```
backend/     FastAPI + SQLAlchemy + 世界引擎 + LLM 智能体
frontend/    Vue3 + Vite + Pinia + PixiJS 8
world_data/  地图(tmj/tsj)、角色卡(身份+出生点+家)、物品、工作、商店种子数据
tools/       地图生成器（build_map.py，确定性）
docs/        架构、世界规则、事件协议、地图规范、智能体约定
```

地图由 `tools/build_map.py` 确定性生成（固定随机种子），可直接在 Tiled 中打开 `world_data/maps/tiny_world.tmj`。

## 文档

- `docs/architecture.md` — 三大边界（LLM 只出意图 / 引擎唯一真值 / 前端只观察）
- `docs/world-rules.md` — 世界规则契约（R1~R17，程序实现）
- `docs/event-protocol.md` — 统一事件协议与全部事件类型
- `docs/map-specification.md` — 图层与对象层规范
- `docs/agent-prompt.md` — 提示词与工具约定

素材：Kenney Tiny Farm（CC0，www.kenney.nl）。
