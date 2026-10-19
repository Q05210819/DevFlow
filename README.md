# DevFlow

面向单团队的私有仓库事实工作台：GitHub 登录、成员允许列表、仓库发现与接入、只读 Issue/PR/Actions 后台同步、可追溯健康事实、访问检查、撤权和审计。2A 保留一期私有检查及 REST/SSE 流程；不调用模型、不克隆代码、不修改 GitHub 内容。代码 Workspace 属于后续 2B，目前不可用。

本地合成 GitHub 联调与真实 GitHub 验收分别记录。真实 App、私有仓库与外部域名尚未提供，真实接入和外部部署未执行；不能据离线测试认定整个平台全部验收完成。

## 文档

- [本地开发与固定依赖版本](docs/operations/local-development.md)
- [GitHub App、回调和秘密配置](docs/operations/github-app-setup.md)
- [Compose 部署与启动顺序](docs/operations/deployment.md)
- [备份与独立数据库恢复](docs/operations/backup-restore.md)
- [阶段验收记录与未执行项目](docs/operations/foundation-acceptance.md)
- [2A 数据同步验收与运维](docs/operations/data-foundation-acceptance.md)
- [2A 数据基础决策](docs/operations/data-foundation-decisions.md)

## 本地启动

需要 UV、Python 3.12.13、Node 24.21.0 和 PostgreSQL 17.11。以下命令从仓库根目录执行；Windows 使用 `npm.cmd`，Linux 使用 `npm`。

```powershell
Copy-Item .env.example .env
uv --directory backend sync --locked
npm.cmd --prefix frontend ci
uv --directory backend run --env-file ../.env --no-sync uvicorn devflow.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --loop asyncio:SelectorEventLoop
```

另一个终端运行 `npm.cmd --prefix frontend run dev -- --hostname 127.0.0.1 --port 3000`。访问 `http://127.0.0.1:3000`；开发服务器把 `/api/*` 代理到本机 8000。`PUBLIC_BASE_URL` 必须与浏览器实际 origin 完全一致。`/health/live` 不依赖数据库；`/health/ready` 在数据库不可达或迁移不匹配时返回 503。

完整登录需要按 GitHub App 文档设置数据库、App 身份、安装允许列表和五个秘密文件；没有真实 App 时不会绕过认证。配置好 `.env` 后显式迁移、首次引导，再在独立终端启动 Worker：

```powershell
uv --directory backend run --env-file ../.env --no-sync alembic upgrade head
uv --directory backend run --env-file ../.env --no-sync python -m devflow.bootstrap
uv --directory backend run --env-file ../.env --no-sync python -m devflow.worker
```

API/Worker 不自动迁移。引导只在用户表为空时创建 `BOOTSTRAP_ADMIN_GITHUB_ID` 对应的管理员。保留 `--no-access-log`，避免 OAuth 查询参数进入访问日志；Windows 数据库异步入口使用 SelectorEventLoop。秘密只放未跟踪的本机文件或运行时挂载文件，不提交到仓库。

Worker 也可通过包入口 `uv --directory backend run --env-file ../.env devflow-worker` 启动，不依赖 API 进程存活。默认并发 4、尝试超时 120 秒、租约 30 秒、心跳 10 秒；最多尝试 3 次，退避 10/30 秒并遵守 GitHub 限流截止。`JOB_MAX_ATTEMPTS` 可设为 1–3，提交时持久化。

Ctrl+C 或 Linux SIGTERM 停止领取，最多等待 30 秒后取消未完成处理；存活或重启后的 Worker 按数据库租约回收，旧 owner/generation 无法写回。结构化日志记录领取、心跳、失败与回收计数，不包含仓库元数据、令牌或原始上游错误。

## 测试

先确认专用测试端口 `127.0.0.1:55439` 的归属，再启动测试 PostgreSQL；不操作其他项目容器或业务数据库。

```powershell
docker compose -p devflow-test -f infra/compose.test.yaml up -d --wait postgres
uv --directory backend run ruff check .
uv --directory backend run ruff format --check .
uv --directory backend run pytest -m "not live_github" -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
Push-Location frontend
npx.cmd --no-install playwright install chromium
npm.cmd run test:e2e
Pop-Location
```

后端和真实浏览器联调只使用 `devflow_test` 数据库/用户及随机独立 schema，结束后清理自身 schema。`TEST_DATABASE_URL` 仅允许上述专用 loopback 数据库，不能指定生产库。全量 Playwright 包含页面交互测试和 `full-flow.spec.ts`：后者启动真实 API、独立 Worker 任务与 PostgreSQL，仅替换 GitHub HTTP transport；OAuth state/cookie、会话、权限及作业持久化均使用生产实现。浏览器服务使用 31847/31848 两个本地端口，不需要真实 GitHub 秘密。

`data-flow.spec.ts` 同样读取真实 API，验证收集的 Issue/PR/CI、缺失补丁、健康事实及仓库撤权后的缓存清理。真实栈用例串行运行，避免两个自有 helper 争用 31848；各 helper 拥有自己的 schema，正常停止、父进程 EOF 和启动失败都清理自身资源。后端 `test_data_sync_flow.py` 还验证分页限流、正常 Worker 重启后续接与不完整轮次保留旧水位。该重启证据是普通 Worker 生命周期，不是强杀进程崩溃验收。

仅运行跨模块验收：

```powershell
uv --directory backend run --no-sync pytest tests/integration/test_full_flow.py tests/integration/test_data_sync_flow.py -q
npm.cmd --prefix frontend run test:e2e -- tests/full-flow.spec.ts tests/data-flow.spec.ts
```

停止测试数据库使用 `docker compose -p devflow-test -f infra/compose.test.yaml stop`，保留测试卷。UV 缓存受限时可使用仓库内 `.cache/uv`；浏览器缓存可通过 `PLAYWRIGHT_BROWSERS_PATH` 指定本机目录。
