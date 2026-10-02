# SKYCity 部署与交付指南

本文面向拿到项目后需要直接部署、验收和维护的用户。

## 1. 推荐部署架构

生产交付采用单容器：

```text
Browser
  |
  | HTTP / WebSocket :8000
  v
FastAPI
  ├─ REST API /api/*
  ├─ WebSocket /ws/*
  ├─ world_data /assets/world_data/*
  └─ frontend/dist/*
```

前端在 Docker 构建阶段执行 `npm ci && npm run build`，运行时不需要 Node.js。

## 2. 首次部署

```bash
git clone https://github.com/TianyaSKY/SKYCity.git
cd SKYCity
docker compose up --build -d
```

健康检查：

```bash
curl http://localhost:8000/health
```

期望返回：

```json
{"status":"ok","app_version":"0.2.0","database_revision":"0001","map_version":"..."}
```

然后访问 `http://localhost:8000`。

## 3. 配置真实模型

```bash
cp backend/.env.example backend/.env
```

官方 OpenAI 只需要填写：

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=
```

第三方兼容服务需要同时填写 `OPENAI_BASE_URL`。

启动：

```bash
docker compose --env-file backend/.env up --build -d
```

注意：

- 不要把 `backend/.env` 提交到版本库。
- 官方 OpenAI Key 不应搭配未知第三方 `OPENAI_BASE_URL`。
- 第三方服务是否兼容 tool calling、reasoning 参数和 Responses API，需要由服务商自行保证。
- 若第三方仅兼容 Chat Completions，保持 `LLM_USE_RESPONSES=false`。

## 4. 数据持久化

Compose 使用名为 `skycity-data` 的 volume，并把数据库和日志放到 `/data`。

查看 volume：

```bash
docker volume ls
```

查看容器状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f skycity
```

## 5. 备份

升级前建议至少备份 SQLite 数据库。

查找 volume：

```bash
docker volume inspect skycity_skycity-data
```

停止服务后复制数据库，避免备份正在写入的 SQLite 文件：

```bash
mkdir -p backups
docker compose stop skycity
docker compose cp skycity:/data/ai_tiny_world.db ./backups/ai_tiny_world.before-upgrade.db
docker compose start skycity
```

每次备份使用独立文件名并记录当前版本。

## 6. 升级

0.2.0 起引入 Alembic：启动会迁移到当前版本的数据库修订，失败时拒绝启动。升级前停止服务并备份，再切换到经过验证的发布 tag。

详细命令、旧库兼容范围、失败恢复和版本发布流程见 [升级指南](upgrading.md)。

健康接口返回 `app_version` 和 `database_revision`，用于核对升级结果。

## 7. 验收清单

交付时建议逐项确认：

- `GET /health` 返回 200。
- 浏览器能打开首页。
- 可创建世界。
- 地图和角色素材正常加载。
- 页面显示 WebSocket 已连接。
- 暂停、恢复、调速可用。
- fake provider 模式可正常运行。
- 配置真实 LLM 后至少完成一次受控决策。
- 容器重启后数据库仍存在。
- 日志写入 `/data/logs`。

## 8. 常见故障

### 页面能打开但没有实时变化

检查：

```bash
docker compose logs -f skycity
```

同时在浏览器开发者工具确认 `/ws/worlds/<id>` WebSocket 已建立。

### 页面资源 404

确认镜像构建阶段 `npm run build` 成功；生产容器内应存在：

```text
/app/frontend/dist/index.html
```

### LLM 不工作但世界还能运行

`LLM_PROVIDER=auto` 时，如果没有有效 `OPENAI_API_KEY`，会回退到 fake provider。

### 第三方模型接口报 404

多数情况是第三方只实现 `/chat/completions`。保持：

```env
LLM_USE_RESPONSES=false
```

### SQLite locked

项目已设置 SQLite busy timeout，但单实例 SQLite 仍不适合多进程横向扩容。当前 Docker 交付建议保持单容器、单 Uvicorn worker。

## 9. 生产环境边界

当前版本适合：

- 单机演示
- 课程 / 科研展示
- 小规模长期运行
- 单实例私有部署

若要进入多用户公网生产环境，建议进一步补充：

- 登录与权限体系
- CSRF / 鉴权 / 限流
- 正式数据库迁移
- PostgreSQL
- 多租户隔离
- HTTPS 反向代理
- 指标监控与告警
- 自动化发布流水线
