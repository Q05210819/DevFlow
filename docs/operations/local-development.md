# 本地开发

所有命令从仓库根目录运行。Python 安装、依赖管理及业务命令统一使用 UV；不手工维护第二套 pip 环境。Windows 使用 PowerShell、`npm.cmd`。

## 固定版本

2026-10-01 打包基线：Python **3.12.13**、UV **0.11.25**、Node **24.21.0**、PostgreSQL **17.11**、Caddy **2.11.4**。镜像 tag 和 SHA256 见 [后端 Dockerfile](../../infra/backend.Dockerfile)、[前端 Dockerfile](../../infra/frontend.Dockerfile)、[Compose](../../infra/compose.yaml)。

| 后端直接依赖 | uv.lock 版本 |
| --- | --- |
| FastAPI / Uvicorn | 0.142.1 / 0.54.0 |
| SQLAlchemy / Alembic / Psycopg | 2.1.1 / 1.20.0 / 3.3.6 |
| Pydantic Settings / HTTPX | 2.15.0 / 0.28.1 |
| Pydantic（传递依赖）/ HTTPX2（开发依赖） | 2.13.5 / 2.13.1 |
| PyJWT / cryptography | 2.15.1 / 50.0.1 |
| pytest / pytest-asyncio / Ruff | 9.1.1 / 1.4.0 / 0.16.9 |

| 前端直接依赖 | package-lock.json 版本 |
| --- | --- |
| Next.js / React / React DOM | 16.3.7 / 19.3.0 / 19.3.0 |
| TanStack Query | 5.104.0 |
| Tailwind / PostCSS 插件 | 4.3.3 / 4.3.3 |
| TypeScript / Playwright | 5.9.3 / 1.63.0 |
| ESLint / eslint-config-next | 9.39.5 / 16.3.7 |
| @types/node / @types/react / @types/react-dom | 26.6.3 / 19.3.0 / 19.3.0 |

升级需要单独更新并审查锁文件与镜像 digest。`--locked` 和 `npm ci` 按已提交版本安装，不要在排错时删除锁文件。[UV 项目管理](https://docs.astral.sh/uv/guides/projects/)

## 安装与无凭据启动

安装 UV 0.11.25 与 Node 24.21.0 后：

```powershell
uv python install 3.12.13
uv --directory backend sync --locked
npm.cmd --prefix frontend ci
Copy-Item .env.example .env
uv --directory backend run --env-file ../.env --no-sync uvicorn devflow.app:create_app --factory --host 127.0.0.1 --port 8000 --loop asyncio:SelectorEventLoop --no-access-log
```

另一个终端运行 `npm.cmd --prefix frontend run dev -- --hostname 127.0.0.1 --port 3000`，浏览器访问 `http://127.0.0.1:3000`。开发服务器把 `/api/*` 原样代理到本机 8000。健康接口直接访问 `http://127.0.0.1:8000/health/live`；未配置数据库时 readiness 503 属预期。没有真实 GitHub App 无法完成真实登录，页面不会自动绕过认证。

`--env-file ../.env` 由 UV 在 backend 目录读取；Settings 不会自动加载仓库 `.env`。Windows Psycopg 异步连接必须使用 SelectorEventLoop，保留 Uvicorn 的 `--loop asyncio:SelectorEventLoop`。所有环境保留 `--no-access-log`，防止 OAuth 查询参数进入原始 URL 日志。Bootstrap 和 Worker 也必须使用兼容事件循环。

## 数据库、迁移及真实本地登录

完整服务使用 [部署说明](deployment.md) 的秘密文件和本地 Compose 覆盖：

```powershell
docker compose --env-file .env -p devflow-local -f infra/compose.yaml -f infra/compose.dev.yaml config --quiet
docker compose --env-file .env -p devflow-local -f infra/compose.yaml -f infra/compose.dev.yaml up -d --build
```

访问 `http://localhost:8080`；只绑定 `127.0.0.1:8080`，数据库只绑定 `127.0.0.1:55440`。覆盖文件固定 `APP_ENV=development`，本地使用独立非 Secure Cookie；不要用于外网。Compose 需 **2.24.4+**，`!override` 替换生产端口，避免仍暴露 80/443。[Compose 合并规则](https://docs.docker.com/reference/compose-file/merge/)

运行主机 API 时可只启动该项目的 postgres，再在 `.env` 设置主机数据库 URL、五个绝对 `_FILE` 路径、App 身份和安装列表。主机 URL 使用 `127.0.0.1:55440`；容器秘密中的 URL 使用 `postgres:5432`。随后：

```powershell
uv --directory backend run --env-file ../.env --no-sync alembic upgrade head
uv --directory backend run --env-file ../.env --no-sync python -m devflow.bootstrap
uv --directory backend run --env-file ../.env --no-sync python -m devflow.worker
```

回调必须与浏览器 origin 相符，详见 [GitHub App 设置](github-app-setup.md)。GitHub webhook 不能直接投递 loopback；临时转发只用于开发，正式验收需要可达 HTTPS 入口。

## 检查命令

测试数据库是独立 `devflow-test` 项目，固定库/用户名 `devflow_test`、端口 **55439**。启动前检查端口归属；不要更改其他 Docker 项目，也不要用生产库运行测试。

```powershell
docker compose -f infra/compose.test.yaml up -d --wait
uv --directory backend run --no-sync ruff check .
uv --directory backend run --no-sync ruff format --check .
uv --directory backend run --no-sync pytest -q -m 'not live_github'
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
Push-Location frontend
npx.cmd --no-install playwright install chromium
npm.cmd run test:e2e
Pop-Location
```

CI 使用隔离 PostgreSQL schema、合成 GitHub transport/秘密，无需真实 App 凭据。测试失败不会自动改用 SQLite。后端生产镜像保留 `/app/backend/src` 与 `/app/backend/migrations` 相对关系；前端运行 standalone `server.js`。[Next standalone](https://nextjs.org/docs/app/api-reference/config/next-config-js/output)

## 当前验收边界

本地完整测试、容器与恢复演练的命令和实际结果见 [阶段验收](foundation-acceptance.md)。浏览器联调使用真实 API、Worker 和 PostgreSQL，仅 GitHub HTTP 使用合成 transport；测试端口 31847/31848 必须空闲。真实 App、私有仓库及公网 HTTPS 配置尚未提供，真实接入为**未执行**。
