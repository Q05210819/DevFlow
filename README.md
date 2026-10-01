# DevFlow

第一阶段平台基础正在实现中。已提供 FastAPI 存活/就绪检查、PostgreSQL 身份与审计基础；GitHub 登录功能将在后续任务接入。

## 本地启动

安装 [UV](https://docs.astral.sh/uv/) 并确保可用 Python 3.12.13。在仓库根目录执行：

```powershell
Copy-Item .env.example .env
uv --directory backend sync --locked
uv --directory backend run --env-file ../.env uvicorn devflow.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --loop asyncio:SelectorEventLoop
```

访问 `http://127.0.0.1:8000/health/live`，应返回 `{"status":"ok"}`。该接口不连接数据库或 GitHub。每个响应带独立的 `X-Request-ID`，错误响应用同一个 ID。其余 API 将统一位于 `/api/v1`。

`/health/ready` 通过实际数据库连接检查迁移 head，未配置、连接失败或迁移不匹配返回 503。启动不会迁移或引导管理员。配置 `.env` 中的 `DATABASE_URL`（`postgresql+psycopg://...`）后显式执行：

```powershell
uv --directory backend run --env-file ../.env alembic upgrade head
uv --directory backend run --env-file ../.env python -m devflow.bootstrap
```

引导要求设置正整数 `BOOTSTRAP_ADMIN_GITHUB_ID`；仅用户表为空时创建管理员，之后不会覆盖成员角色或启用状态。Windows 的 Psycopg 异步连接要求 SelectorEventLoop，上面的 Uvicorn 命令、引导 CLI 和 Worker CLI 均已配置。

数据库迁移完成并配置 GitHub App 与凭据加密秘密后，在另一个终端启动独立 Worker：

```powershell
uv --directory backend run --env-file ../.env python -m devflow.worker
```

也可使用包入口 `uv --directory backend run --env-file ../.env devflow-worker`。Worker 不依赖 API 进程存活，只执行 `repository.access_probe` 的 GitHub 用户授权元数据读取。`WORKER_CONCURRENCY` 默认 4；每次尝试默认最多 120 秒，租约 30 秒，每 10 秒独立续期。最多尝试 3 次（`JOB_MAX_ATTEMPTS` 可设为 1–3，提交时持久化），重试默认等待 10/30 秒，并遵守 GitHub 限流截止。

停止时按 Ctrl+C，Linux 也支持 SIGTERM：停止领取后最多等待 30 秒，再取消剩余处理，未完成租约由存活或重启后的 Worker 回收。强杀后同样依靠数据库租约回收；恢复仍消耗既定尝试预算，旧 owner/generation 无法写回。结构化日志提供领取等待、心跳、失败与回收计数，不包含仓库元数据、令牌或原始上游错误。

运行测试和静态检查：

```powershell
docker compose -p devflow-test -f infra/compose.test.yaml up -d --wait postgres
uv --directory backend run pytest -q
uv --directory backend run ruff check .
uv --directory backend run ruff format --check .
```

测试 PostgreSQL 固定为 17.11（镜像包含 digest），只暴露 `127.0.0.1:55439`；启动前确保该端口闲置。默认测试连接为 `postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test`。`TEST_DATABASE_URL` 仅允许相同 loopback、端口、库名和用户，拒绝额外连接参数；不会使用应用 `DATABASE_URL`。每项数据库测试创建随机 schema 并仅清理自身 schema。不要将此测试库用于业务数据。

在独立测试库检查命令行迁移（PowerShell）：

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test'
uv --directory backend run alembic upgrade head
uv --directory backend run alembic check
Remove-Item Env:DATABASE_URL
```

停用测试服务使用 `docker compose -p devflow-test -f infra/compose.test.yaml stop`；仅影响该独立项目并保留测试卷。UV 缓存受权限限制时可加 `--cache-dir .cache/uv`，不更改全局配置。

本地没有 GitHub App 时保留 `.env.example` 中的 GitHub 配置注释即可。后续接入时，先创建 GitHub App 并获得 App ID、Client ID、允许的安装 ID 与引导管理员 GitHub ID，再将五项秘密写入独立文件，设置对应的 `*_FILE` 路径。`GITHUB_ALLOWED_INSTALLATION_IDS` 是 JSON 整数数组，例如 `[123456]`。生产环境必须使用 HTTPS 的 `PUBLIC_BASE_URL`，配置数据库、上述身份值及非空秘密文件；开发和测试环境的 HTTP URL 只允许 loopback 主机。

敏感配置应放在未跟踪的 `.env` 或部署秘密文件中。日志只记录固定字段，不记录请求查询、Cookie、Authorization 或设置对象。UV 锁文件、解释器版本文件和 `.env.example` 已纳入版本管理。
