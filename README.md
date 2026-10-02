# SKYCity / AI Tiny World（AI 小世界）

一个采用 **Tiny Farm 像素素材**的 2D 上帝视角 AI 世界。居民由身份卡、状态、记忆、关系和 LLM 驱动，通过工具完成移动、对话、工作、消费、经营等行为；玩家负责观察、控制时间和干预世界。

## 适合直接交付的运行方式

推荐使用 Docker。前端会在构建阶段打包，并由 FastAPI 直接托管，因此最终只需要暴露 **8000** 一个端口。

### 1. 无 LLM Key 直接体验

```bash
docker compose up --build -d
```

打开：

```text
http://localhost:8000
```

未配置 `OPENAI_API_KEY` 时，系统自动使用内置 fake provider，仍可体验完整世界流程。

### 2. 启用真实 LLM

复制配置模板：

```bash
cp backend/.env.example backend/.env
```

官方 OpenAI：

```env
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=
LLM_MODEL=gpt-4o-mini
LLM_REFLECT_MODEL=gpt-4o-mini
```

第三方 OpenAI 兼容服务：

```env
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://your-provider.example/v1
LLM_MODEL=你的模型名
LLM_REFLECT_MODEL=你的模型名
```

然后：

```bash
docker compose --env-file backend/.env up --build -d
```

> 不要把真实密钥提交到 Git。项目已忽略 `backend/.env`。

## 本地开发

### 后端

要求 Python 3.12+ 与 uv。

```bash
cd backend
uv sync
cp .env.example .env
uv run uvicorn app.main:app --port 8000
```

### 前端

要求 Node.js 20+，推荐 Node.js 22。

```bash
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。

开发环境下 Vite 会自动代理以下后端路径：

- `/api`
- `/health`
- `/assets/world_data`
- `/ws`

因此默认不需要配置 `VITE_API_BASE`。

## 功能一览

- 64×40 瓦片小镇地图（Tiled JSON），PixiJS 整数倍缩放渲染
- 世界时钟：暂停 / 恢复 / 1× / 2× / 5× / 10×
- 多居民 LLM 自主决策：移动、等待、对话、工作、购买、出售、使用物品等
- 对话气泡、历史面板、交谈高亮、防无限对聊
- 合作社经济闭环、正式雇佣、个人摊位、合作社份额
- 睡眠、精力、心情、饱食、孤独等生活状态
- 工作记忆 / 情节记忆 / 语义记忆与方向性关系
- 每日反思与 LLM 调用稳定性控制
- 上帝视角干预与审计
- 存档 / 恢复 / 事件重放
- 数据看板与 LLM / 事件统计

## 常用配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./ai_tiny_world.db` | SQLite 数据库地址 |
| `WORLD_DATA_DIR` | `../world_data` | 世界静态数据目录 |
| `FRONTEND_DIST_DIR` | `../frontend/dist` | 构建后的前端目录；存在时由 FastAPI 托管 |
| `LOG_DIR` | `logs` | 日志目录 |
| `OPENAI_API_KEY` | 空 | 真实 LLM 密钥 |
| `OPENAI_BASE_URL` | 空 | 第三方 OpenAI 兼容服务地址；官方 OpenAI 保持为空 |
| `LLM_PROVIDER` | `auto` | `auto` / `openai` / `fake` |
| `LLM_MODEL` | `gpt-4o-mini` | 普通决策模型 |
| `LLM_REFLECT_MODEL` | `gpt-4o-mini` | 每日反思模型 |
| `LLM_USE_RESPONSES` | `false` | 是否使用 Responses API |
| `LLM_MAX_CONCURRENT` | `2` | 全局 LLM 并发上限 |
| `WORLD_DAILY_TOKEN_BUDGET` | `0` | 每世界每日 Token 预算，0 为不限 |

SDK 默认可走 Responses API，而大量第三方兼容服务只实现 `/chat/completions`，因此默认使用 `LLM_USE_RESPONSES=false`。

## 测试

后端：

```bash
cd backend
uv run pytest tests/ -q
```

前端：

```bash
cd frontend
npm ci
npm run test
npm run build
```

浏览器冒烟测试：

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
```

E2E 需要先启动本地后端 `127.0.0.1:8000` 和前端 `127.0.0.1:5173`。

真实模型冒烟测试 `backend/tests/test_llm_smoke.py` 在没有 `OPENAI_API_KEY` 时会跳过。

## 添加新智能体

每个智能体由 `world_data/identities/agent_xxx.json` 定义。新增角色后重新创建世界即可播种。

示例：

```json
{
  "id": "agent_xxx",
  "name": "名字",
  "age": 30,
  "occupation": "职业",
  "background": "背景故事",
  "values": [],
  "long_term_goals": [],
  "speaking_style": "说话风格",
  "personality": {
    "openness": 0.5,
    "conscientiousness": 0.5,
    "extraversion": 0.5,
    "agreeableness": 0.5,
    "emotional_stability": 0.5
  },
  "initial_money": 600,
  "spawn": {"col": 33, "row": 20, "direction": "down"}
}
```

如需同步 Tiled 地图中的可视化出生点/住宅：

```bash
uv run --with pillow python tools/build_map.py
```

## 数据升级注意

当前项目仍没有正式数据库迁移框架。代码会对部分旧字段执行启动时兼容补丁，但跨大版本升级前仍建议备份数据库。

全新交付推荐直接使用 Docker volume。升级已有实例前请先备份：

- SQLite 数据库
- `/data/logs`（如需保留排障记录）
- 自定义 `world_data`

更完整的部署、升级、备份与排障说明见 `docs/deployment.md`。

## 目录结构

```text
backend/       FastAPI + SQLAlchemy + 世界引擎 + LLM 智能体
frontend/      Vue 3 + Vite + Pinia + PixiJS 8
world_data/    地图、角色卡、物品、工作、商店、合作社等种子数据
tools/         地图生成器
docs/          架构、世界规则、协议、部署文档
Dockerfile     前后端一体化生产镜像
docker-compose.yml
```

## 文档

- `docs/deployment.md` — 面向交付的部署、升级、备份与排障
- `docs/architecture.md` — 架构边界
- `docs/world-rules.md` — 世界规则
- `docs/event-protocol.md` — 事件协议
- `docs/map-specification.md` — 地图规范
- `docs/agent-prompt.md` — 智能体提示词与工具约定
- `docs/company-employment.md` — 合作社 / 雇佣系统

素材：Kenney Tiny Farm（CC0）。


## 升级与发布

当前待发布版本：**0.2.0**。应用启动时自动执行 Alembic 迁移；升级前停止服务并备份数据库。

- [升级、恢复和版本发布](docs/upgrading.md)
- [版本变更记录](CHANGELOG.md)
- [交付验证结果](docs/delivery-validation.md)

GitHub Actions 自动验证后端、前端、Docker、E2E 和重启持久化；所有检查通过后才能发布。
隔离的 Docker 验收使用独立项目及端口，强制 fake provider：

```bash
export SKYCITY_PORT=18000 LLM_PROVIDER=fake OPENAI_API_KEY= OPENAI_BASE_URL=
docker compose -p skycity-delivery up --build -d --wait --wait-timeout 120
python3 tools/verify_delivery.py
cd frontend
E2E_BASE_URL=http://127.0.0.1:18000 E2E_API_URL=http://127.0.0.1:18000 npm run test:e2e
cd ..
# 仅删除此次验收项目的数据卷
docker compose -p skycity-delivery down -v
```

本机 E2E 首次运行需在 `frontend/` 执行 `npx playwright install chromium`。
`SKYCITY_PORT` 可修改宿主机端口，默认仍为 8000。
