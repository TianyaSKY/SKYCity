# 升级、恢复与版本发布

## 数据库迁移

从 0.2.0 起，启动会先执行 Alembic `upgrade head`，成功后才开放 API。
也可以先手动迁移（本地命令在 `backend/` 执行）：

```bash
uv sync --frozen
uv run alembic current
uv run alembic upgrade head
uv run alembic current
```

迁移基线 `0001` 固定了本次发布的表结构，覆盖新库、当前未标记旧库，以及原 `session.py` 中的已知补字段逻辑。不会删除世界、居民、存档或事件。不支持任意历史版本或手工修改过的数据库；缺失其他必要字段时会报错。旧库中重复的有效雇佣或个人摊位也会阻止迁移，须先核查业务数据。

SQLite 迁移在事务和写锁内执行。失败会回滚结构修改和版本标记；不要通过 `alembic stamp head` 跳过错误。基线拒绝 `downgrade base`，恢复旧版本应使用升级前的备份。

每次模型变更必须增加独立 revision，不能修改已经交付的基线：

```bash
uv run alembic revision --autogenerate -m describe_change
# 审核生成的 upgrade/downgrade，必要时显式迁移数据
uv run alembic upgrade head
uv run alembic check
uv run pytest tests/test_migrations.py -q
```

离线 SQL 生成暂不支持；基线需要在线检查旧库结构。

## Docker 升级

先在旧服务仍能正常启动时检查健康状态并记录版本。停止服务后复制数据库，避免在写入期间复制 SQLite 文件：

```bash
curl -f http://localhost:8000/health
mkdir -p backups
docker compose stop skycity
docker compose cp skycity:/data/ai_tiny_world.db ./backups/ai_tiny_world.before-upgrade.db
```

每次升级使用独立备份文件名，并记录对应的 Git tag/commit、Compose 配置和模型配置。保留备份，不运行 `docker compose down -v`，该命令会删除数据卷。

切换到经过 CI 验证的版本后：

```bash
git checkout v0.2.0
# 该 tag 需要维护者在发布验证通过后创建
docker compose build --pull
docker compose run --rm --no-deps skycity alembic upgrade head
docker compose up -d --wait --wait-timeout 120
curl -f http://localhost:8000/health
docker compose exec -T skycity alembic current
```

配置真实模型时，在所有 Compose 命令中使用相同的 `--env-file backend/.env` 参数。验收健康接口的 `app_version` 和 `database_revision`，并确认已有世界可访问。

## 恢复旧版本

确认备份文件存在且与旧版本匹配，再停止服务并恢复：

```bash
docker compose stop skycity
docker compose cp ./backups/ai_tiny_world.before-upgrade.db skycity:/data/ai_tiny_world.db
git checkout <升级前的-tag-或-commit>
docker compose build
docker compose up -d --wait --wait-timeout 120
```

恢复操作会覆盖当前数据库，丢弃备份之后的进展，因此先另存失败升级后的数据库供排查。不要混用旧应用与新库。容器已不存在时，应先用旧版本配置 `docker compose create skycity`，再复制备份。

## 发布流程

当前 0.2.0 仍为待发布版本，不自动创建或推送 tag。

1. 更新 `backend/pyproject.toml`、`frontend/package.json` 和 lockfile 中的版本，补充 `CHANGELOG.md` 并确定发布日期。
2. 执行 `python3 tools/check_release.py`；执行完整后端/前端测试和交付验收。
3. PR 的 `Delivery checks` 三个 job 必须全部通过。维护者需在 GitHub 分支保护中将它们设为必需检查，workflow 文件本身不会启用分支保护。
4. 合并后创建并推送匹配版本的 tag（如 `v0.2.0`）。Tag workflow 重跑完整 CI，校验 tag 与项目版本一致，再创建 Draft Release。
5. 审核 Release Notes，补充数据库兼容性、备份要求和验收结果，再手动发布 Release。

Release workflow 不会在测试失败时创建发布草稿。源码发布以 Git tag 为准，当前 Docker 仍从源码构建。
