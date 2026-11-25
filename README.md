# DevFlow

DevFlow 是一个自托管的团队仓库工作台：通过 GitHub App 接入组织仓库，把仓库元数据、Issue / Pull Request / Actions 事实、只读代码快照、知识库检索与基于证据的私有问答集中到一个 Web 界面。它面向希望在内网或自托管环境中统一浏览、检索和审计多个 GitHub 仓库的团队。

## 功能概览

- **GitHub App 登录与成员白名单**：OAuth 登录、HttpOnly 会话 Cookie；只有管理员预先加入且处于启用状态的成员才能登录，管理员可维护成员并查看审计日志。
- **仓库接入与用户级授权**：管理员按 GitHub installation 注册仓库；后续所有仓库读取都经过用户级授权检查，远端观测不会覆盖较新的本地授权状态。
- **持久化后台任务**：访问检查、同步、工作区准备、知识导入/索引/查询、健康快照等以 Job 形式排队，支持租约、心跳、重试、取消与 SSE 事件流。
- **仓库事实同步**：按时间窗口同步 Issue、Pull Request、Actions run / attempt 等 GitHub 事实，并保留来源链接。
- **只读代码工作区**：服务端维护 bare Git 缓存，向上层提供仓库 tree、file 与 search 的只读访问，不授予写权限。
- **确定性仓库健康快照**：按规则版本生成可复现的仓库健康检查结果。
- **仓库知识库与混合检索**：导入仓库文档或上传文件，分块并构建带版本的 Milvus 索引；查询时使用 dense 向量 + BM25 sparse 混合检索、RRF 融合与本地 rerank，并返回 citation。
- **私有对话**：仅创建者可见的会话，基于 LangGraph 的受限 agent 流程；云端模型默认关闭，支持运行/月度预算与数据保留策略。

## 架构与目录结构

```text
Browser
   │  HTTP(S)
   ▼
gateway (Caddy)
   ├─ /api/*, /health/* ─────► api (FastAPI, :8000)
   └─ 其他路径 ──────────────► frontend (Next.js, :3000)
                                   │ 同源请求 /api/v1/*
                                   ▼
                              api ───────────► PostgreSQL 17（Job 队列 / 租约）
                                   ├──► worker (devflow.worker) ──► GitHub API / 本地 bare Git 缓存
                                   └──► model-worker（模型类 Job）──► 本地 embedding / rerank 模型
                                                └──────────────────► Milvus standalone (etcd + MinIO)
```

| 目录 | 职责 |
| --- | --- |
| `backend/` | Python 包 `devflow`。FastAPI app factory 在 `src/devflow/app.py`，路由按业务域拆分（`auth`、`repositories`、`sync`、`facts`、`workspace`、`health`、`knowledge`、`chat`、`jobs`、`audit`）；独立 Worker 入口为 `src/devflow/worker.py`；Alembic 迁移位于 `backend/migrations/`。 |
| `frontend/` | Next.js App Router 前端。页面在 `src/app/`，功能组件在 `src/features/`，同源 API 客户端在 `src/lib/api.ts`；开发环境通过 rewrite 代理 `/api/*`。 |
| `infra/` | 部署与本地编排：`compose.yaml`、开发覆盖文件 `compose.dev.yaml`、`backend.Dockerfile`、`frontend.Dockerfile`、`Caddyfile`，以及可选的 `chat.env.example`。 |
| `.github/workflows/` | CI workflow（`ci.yml`）。 |
| `.env.example` | 环境变量样例；复制为 `.env` 后使用。 |

## 技术栈

| 层 | 技术与版本 |
| --- | --- |
| 后端语言 | Python `3.12.13`（`requires-python = ">=3.12,<3.13"`） |
| Web 框架 | FastAPI `>=0.142.1`、Uvicorn `>=0.54.0`、Pydantic Settings `>=2.15.0` |
| 数据库 | PostgreSQL `17.11`；SQLAlchemy `>=2.1.1`（asyncio）、Psycopg `>=3.3.6`、Alembic `>=1.20.0` |
| 向量检索 | Milvus `v2.6.26` standalone（etcd `v3.5.25` + MinIO）；`pymilvus==2.6.17`；dense HNSW/COSINE + sparse BM25；1024 维向量 |
| LLM / Agent | LangGraph `>=1.2.20`、LangGraph Postgres checkpoint `>=3.2.0`、LangChain OpenAI `>=1.7.4` |
| 本地模型 | `transformers >=5.20.2`、`tokenizers >=0.23.3`；可选 extra `models` 提供 `sentence-transformers >=6.2.0` 与 CPU 版 `torch >=2.15.0` |
| 前端 | Next.js `16.3.7`、React `19.3.0`、TypeScript `5.9.3`、TanStack Query `5.104.0`、Tailwind CSS `4.3.3` |
| 前端工具链 | Node `24.21.0`、ESLint `9.39.5`、Playwright `1.63.0` |
| 容器与网关 | Docker Compose（开发覆盖文件要求 `>=2.24.4`）、Caddy `2.11.4`、uv `0.11.25` |

## 快速开始

### 1. 前置依赖

- Docker Engine + Docker Compose，Compose 版本需 `>=2.24.4`（开发覆盖文件使用 `!override` 替换端口）。
- Git `>=2.43`（工作区 Git 操作要求）。
- 仅在本地开发后端时需要：uv `0.11.25`、Python `3.12.13`。
- 仅在本地开发前端时需要：Node `24.21.0` 与 npm。

### 2. 配置

```bash
cp .env.example .env
```

Compose 要求以下变量非空（`compose.yaml` 中使用 `${VAR:?}` 强制校验）：

| 变量 | 说明 |
| --- | --- |
| `PUBLIC_BASE_URL` | 站点 origin。生产环境必须是 HTTPS；开发环境只允许 loopback HTTP。 |
| `BOOTSTRAP_ADMIN_GITHUB_ID` | 首个管理员的 GitHub 数字 ID。 |
| `GITHUB_APP_ID` | GitHub App ID。 |
| `GITHUB_CLIENT_ID` | GitHub App OAuth client ID。 |
| `GITHUB_ALLOWED_INSTALLATION_IDS` | 允许的 installation ID，JSON 数组，例如 `[1]`。 |
| `DEVFLOW_SECRETS_DIR` | 存放 secret 文件的绝对目录，不能放在仓库内。 |

`.env.example` 中的凭据类值均为占位符，必须替换；`PUBLIC_BASE_URL` 需按部署环境修改（生产为真实 HTTPS origin）。不要提交真实密钥。`APP_ENV` 在 Compose 的 production 服务中固定为 `production`，开发覆盖文件改为 `development`。

**Secret 文件**：完整栈需要 `DEVFLOW_SECRETS_DIR` 下的九个文件，每个文件的内容就是密钥本身：

| 文件 | 内容 |
| --- | --- |
| `database_url` | PostgreSQL 连接串，必须以 `postgresql+psycopg://` 开头；其中的口令需与 `postgres_password` 一致 |
| `postgres_password` | PostgreSQL 用户 `devflow` 的口令 |
| `github_client_secret` | GitHub App 的 OAuth client secret |
| `github_private_key` | GitHub App 私钥（PEM） |
| `github_webhook_secret` | Webhook 签名密钥 |
| `credential_encryption_key` | 加密存储 GitHub 凭据的 Fernet key |
| `app_signing_key` | 会话与游标签名密钥 |
| `minio_access_key` | MinIO root user |
| `minio_secret_key` | MinIO root password |

### 3. 方式一：Docker Compose

在仓库根目录执行：

```bash
# 开发栈（gateway 只监听 127.0.0.1:8080）
docker compose --env-file .env -f infra/compose.yaml -f infra/compose.dev.yaml up -d --build

# 生产栈（PUBLIC_BASE_URL 必须是真实 HTTPS origin，gateway 绑定 80/443）
docker compose --env-file .env -f infra/compose.yaml up -d --build
```

生产 Compose 同样会启动 `model-worker`（该服务在生产文件中没有 profile），因此需要先准备模型目录并显式设置 `MODEL_DEVICE` / `MODEL_DTYPE`，否则该容器启动即失败。

开发栈默认不启动 Milvus 三件套；需要知识库检索时追加 `--profile milvus`，再需要本地模型 worker 时追加 `--profile models`：

```bash
docker compose --env-file .env -f infra/compose.yaml -f infra/compose.dev.yaml \
  --profile milvus --profile models up -d --build
```

`model-worker` 还需要设置 `DEVFLOW_MODEL_DIR`（宿主机绝对目录）、`MODEL_DEVICE`、`MODEL_DTYPE`，并保证模型文件对容器内 UID `10001` 可读。启用云端对话时，将 `infra/chat.env.example` 复制为 `infra/chat.env` 并填写完整价格表（也可用 `DEVFLOW_CHAT_ENV_FILE` 指向其他路径）。

### 4. 方式二：本地开发

1. 启动一个可访问的 PostgreSQL（开发覆盖文件会把 Compose 的 postgres 发布到 `127.0.0.1:55440`）：

   ```bash
   docker compose --env-file .env -f infra/compose.yaml -f infra/compose.dev.yaml up -d postgres
   ```

2. 在仓库根目录 `.env` 中设置主机进程使用的连接串（Compose 内则使用挂载的 `database_url` secret）：

   ```dotenv
   DATABASE_URL=postgresql+psycopg://devflow:URL_ENCODED_PASSWORD@127.0.0.1:55440/devflow
   ```

   同时配置 `CREDENTIAL_ENCRYPTION_KEY_FILE` 等 secret 文件路径；主机进程不会读取 `/run/secrets/`。

3. 安装依赖并初始化数据库：

   ```bash
   uv --directory backend sync --locked
   uv --directory backend run --env-file ../.env --no-sync alembic upgrade head
   uv --directory backend run --env-file ../.env --no-sync python -m devflow.bootstrap
   ```

4. 启动 API 与 Worker（两个终端）：

   ```bash
   uv --directory backend run --env-file ../.env --no-sync uvicorn devflow.app:create_app \
     --factory --host 0.0.0.0 --port 8000 --loop asyncio:SelectorEventLoop --no-access-log

   uv --directory backend run --env-file ../.env --no-sync python -m devflow.worker
   ```

5. 启动前端：

   ```bash
   npm --prefix frontend ci
   npm --prefix frontend run dev
   ```

   浏览器打开 `http://127.0.0.1:3000`。开发模式下 Next.js 将 `/api/:path*` rewrite 到 `DEVFLOW_API_ORIGIN`（未设置时默认 `http://127.0.0.1:8000`）。该变量仅对本地开发/测试生效，已作为注释项列在 `.env.example` 中。

### 5. 访问地址与端口

| 入口 | 地址 |
| --- | --- |
| 开发 gateway（Compose 覆盖文件） | `http://localhost:8080` |
| 生产 gateway | `https://<your-host>`（宿主机 80/443） |
| 前端本地开发 | `http://127.0.0.1:3000` |
| API 本地开发 | `http://127.0.0.1:8000`；健康检查 `/health/live`、`/health/ready` |
| PostgreSQL（开发） | `127.0.0.1:55440` |
| Milvus（开发 `milvus` profile） | `127.0.0.1:19531`（容器内 gRPC `19530`）；健康检查 `127.0.0.1:9092`（容器内 `9091`） |

### 6. 首次初始化

- **迁移与管理员**：Compose 的 `migrate` 服务会先执行 `alembic upgrade head`，再执行 `python -m devflow.bootstrap`；`api`、`worker`、`model-worker` 均等待其成功。本地开发需手动执行上述两条命令。
- **首个管理员**：`devflow.bootstrap` 只在用户表为空时创建 `BOOTSTRAP_ADMIN_GITHUB_ID` 对应的管理员；后续成员由管理员在管理页添加。
- **知识索引**：不会在启动时自动构建，需通过 API 发起导入与索引（`POST /api/v1/repositories/{repository_id}/knowledge/imports`、`/indexes`）。
- **云端对话（可选）**：启用前需单独部署 LangGraph checkpoint schema：

  ```bash
  uv --directory backend run --env-file ../.env --no-sync python -m devflow.chat.checkpoints --setup
  ```

## 常用命令

| 用途 | 命令 |
| --- | --- |
| 后端依赖 | `uv --directory backend sync --locked` |
| 后端 lint | `uv --directory backend run --no-sync ruff check .` |
| 后端格式检查 | `uv --directory backend run --no-sync ruff format --check .` |
| 后端测试（CI 使用） | `uv --directory backend run --no-sync pytest -q -m 'not live_github and not live_chat'` |
| 数据库迁移 | `uv --directory backend run --env-file ../.env --no-sync alembic upgrade head` |
| 启动 Worker | `uv --directory backend run --env-file ../.env --no-sync python -m devflow.worker` |
| 前端依赖 | `npm --prefix frontend ci` |
| 前端开发 | `npm --prefix frontend run dev` |
| 前端构建 | `npm --prefix frontend run build`（standalone 输出，容器内由 `node server.js` 启动） |
| 前端类型检查 | `npm --prefix frontend run typecheck` |
| 前端 lint | `npm --prefix frontend run lint` |
| 前端 E2E | `npm --prefix frontend run test:e2e` |
| Compose 配置校验 | `docker compose --env-file .env -f infra/compose.yaml config --quiet` |
| 构建镜像 | `docker compose --env-file .env -f infra/compose.yaml build api frontend gateway` |

> 注意：当前仓库快照未包含 `backend/tests/` 与 `frontend/tests/`。CI 检测到对应目录缺失时会跳过 pytest / Playwright 并输出 `::notice`，目录补回后自动恢复执行；本地运行上述测试命令同样需要测试目录存在。`live_github` 与 `live_chat` 标记的用例会访问真实 GitHub 或消耗云端预算，因此在 CI 中始终排除。

## 配置项说明

以下变量的默认值与校验来自 `backend/src/devflow/config.py`（应用进程），或来自 `infra/compose.yaml` 与前端配置；仅由 Compose 或前端读取的变量在说明中单独标注。

### 基础与安全

| 变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `APP_ENV` | 是 | `development` / `test` / `production` | 无 |
| `PUBLIC_BASE_URL` | 是 | 站点 HTTP(S) origin；production 必须 HTTPS | 无 |
| `DATABASE_URL` | 主机进程必填 | 必须是 `postgresql+psycopg://`；Compose 通过 secret 注入 | 无 |
| `BOOTSTRAP_ADMIN_GITHUB_ID` | production 必填 | 首个管理员 GitHub 数字 ID | 无 |
| `DEVFLOW_SECRETS_DIR` | Compose 必填 | secret 文件所在绝对目录 | 无 |
| `DEVFLOW_IMAGE_TAG` | 否 | Compose 构建镜像 tag | `local` |
| `DEVFLOW_API_ORIGIN` | 否 | 前端开发服务器 rewrite `/api/:path*` 的目标；仅 `NODE_ENV=development` 生效 | `http://127.0.0.1:8000` |
| `OAUTH_STATE_TTL_SECONDS` | 否 | OAuth state 有效期 | `600` |
| `SESSION_TTL_SECONDS` | 否 | 会话有效期 | `28800` |
| `PERMISSION_CACHE_TTL_SECONDS` | 否 | 权限缓存时间 | `60` |

### GitHub 集成

| 变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `GITHUB_APP_ID` | production 必填 | GitHub App ID | 无 |
| `GITHUB_CLIENT_ID` | production 必填 | OAuth client ID | 无 |
| `GITHUB_ALLOWED_INSTALLATION_IDS` | production 必填 | 允许的 installation ID（JSON 数组） | `[]` |
| `GITHUB_CLIENT_SECRET_FILE` | 主机进程 | client secret 文件路径；Compose 固定为 `/run/secrets/github_client_secret` | 无 |
| `GITHUB_PRIVATE_KEY_FILE` | 主机进程 | App 私钥文件路径；Compose 固定为 `/run/secrets/github_private_key` | 无 |
| `GITHUB_WEBHOOK_SECRET_FILE` | 主机进程 | webhook 密钥文件路径；Compose 固定为 `/run/secrets/github_webhook_secret` | 无 |
| `CREDENTIAL_ENCRYPTION_KEY_FILE` | 主机进程 | 凭据加密 key 文件路径；Compose 固定为 `/run/secrets/credential_encryption_key` | 无 |
| `APP_SIGNING_KEY_FILE` | 主机进程 | 会话/游标签名 key 文件路径；Compose 固定为 `/run/secrets/app_signing_key` | 无 |

### 任务、同步与工作区

| 变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `WORKER_CONCURRENCY` | 否 | 普通 Worker 并发任务数 | `4` |
| `JOB_MAX_ATTEMPTS` | 否 | 单个 Job 最大尝试次数 | `3` |
| `JOB_LEASE_SECONDS` | 否 | Job 租约时长 | `30` |
| `JOB_HEARTBEAT_SECONDS` | 否 | 心跳间隔，必须大于 5 秒 | `10` |
| `JOB_ATTEMPT_TIMEOUT_SECONDS` | 否 | 单次尝试超时 | `120` |
| `SYNC_CONCURRENCY` | 否 | 同步并发数 | `2` |
| `SYNC_INTERVAL_SECONDS` | 否 | 周期同步间隔 | `900` |
| `SYNC_HISTORY_DAYS` | 否 | 历史同步回看天数 | `90` |
| `SYNC_ACTIONS_DAYS` | 否 | Actions 事实回看天数 | `30` |
| `WORKSPACE_GIT_EXECUTABLE` | 否 | Git 可执行文件绝对路径 | Windows `C:/Program Files/Git/cmd/git.exe`；其他 `/usr/bin/git` |
| `WORKSPACE_CACHE_ROOT` | 否 | 工作区缓存根目录，必须为绝对路径 | Windows `C:/ProgramData/DevFlow/workspace`；其他 `/var/lib/devflow/workspace` |
| `WORKSPACE_REPOSITORY_BYTES` | 否 | 单仓库软字节预算 | `2147483648`（2 GiB） |
| `WORKSPACE_TOTAL_BYTES` | 否 | 工作区总软字节预算 | `21474836480`（20 GiB） |
| `WORKSPACE_UNUSED_DAYS` | 否 | 未使用多少天后清理 | `7` |

### 本地模型与 Milvus

| 变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `DEVFLOW_MODEL_DIR` | model worker 必填 | 宿主机模型目录，只读挂载到容器 `/models` | Compose `./.models`（相对 `infra/`） |
| `MODEL_EMBEDDING_PATH` | model worker 必填 | 容器内 embedding 模型绝对路径 | Compose `/models/bge-m3` |
| `MODEL_RERANKER_PATH` | model worker 必填 | 容器内 reranker 模型绝对路径 | Compose `/models/bge-reranker-v2-m3` |
| `KNOWLEDGE_TOKENIZER_PATH` | 导入知识时必填 | 容器内 tokenizer 目录绝对路径 | Compose `/models/bge-m3` |
| `MODEL_DEVICE` | model worker 必填 | `cpu` 或 `cuda`，必须显式设置 | 无；Compose 传入空值会启动失败（代码默认 `cpu`） |
| `MODEL_DTYPE` | model worker 必填 | `float16` / `float32` / `bfloat16`，必须显式设置 | 无；Compose 传入空值会启动失败（代码默认 `float32`） |
| `MODEL_STARTUP_TIMEOUT_SECONDS` | 否 | 模型加载超时 | `1800` |
| `MODEL_DRAIN_TIMEOUT_SECONDS` | 否 | 关闭前排空推理的超时 | `120` |
| `MILVUS_URI` | 否 | Milvus HTTP(S) origin | 主机默认 `http://127.0.0.1:19530`；Compose 固定 `http://milvus:19530` |
| `MILVUS_COLLECTION` | 否 | Milvus collection 名 | `devflow_knowledge_v1` |

### 私有对话（默认关闭）

| 变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `CHAT_CLOUD_ENABLED` | 否 | 是否允许云端模型调用 | `false` |
| `DEVFLOW_CHAT_ENV_FILE` | 否 | Compose `model-worker` 读取的 chat env 文件；相对路径按 `infra/` 解析 | `./chat.env` |
| `CHAT_API_KEY_FILE` | 启用时必填 | 云端模型 key 文件路径（Compose 挂载 `/run/devflow-secrets`） | 无 |
| `CHAT_BUDGET_CURRENCY` | 启用时必填 | ISO 4217 货币代码 | 无 |
| `CHAT_RUN_BUDGET` | 启用时必填 | 单次运行预算 | 无 |
| `CHAT_MONTH_BUDGET` | 启用时必填 | 月度预算 | 无 |
| `CHAT_BUDGET_MINOR_UNIT` | 启用时必填 | 最小计费单位 | 无 |
| `CHAT_PRICING_VERSION` | 启用时必填 | 价格表版本标识 | 无 |
| `CHAT_PRICING_VALID_UNTIL` | 启用时必填 | 价格表到期时间（必须带时区） | 无 |
| `CHAT_INPUT_PRICE_PER_MILLION` | 启用时必填 | 输入 token 单价 / 百万 | 无 |
| `CHAT_OUTPUT_PRICE_PER_MILLION` | 启用时必填 | 输出 token 单价 / 百万 | 无 |

启用云端对话时，价格表必须完整，否则服务启动即失败；模型标识、调用地址与参数版本固定在代码中，不经环境变量配置。

## CI 与部署

- **CI**：`.github/workflows/ci.yml`，在 `push`、`pull_request`、`workflow_dispatch` 时触发，运行于 `ubuntu-24.04`，超时 30 分钟。使用 PostgreSQL `17.11` service（宿主机端口 `55439`）。
- **CI 步骤**：
  1. 校验锁定的 backend / frontend 依赖（`uv sync --locked`、`npm ci`）。
  2. 后端 `ruff check`、`ruff format --check`；`backend/tests/` 存在时运行 `pytest -q -m 'not live_github and not live_chat'`，否则跳过并输出 `::notice`。
  3. 前端 `typecheck`、`lint`、`build`；`frontend/tests/` 存在时安装 Playwright Chromium 并执行 E2E，否则跳过并输出 `::notice`。
  4. 生成合成的 secret 文件（仅用于打包验证，不是真实凭据）。
  5. 校验生产与开发两份 Compose 配置，构建 `api`、`frontend`、`gateway` 镜像，并断言容器以非 root 运行且包含预期文件。
- **镜像**：
  - `infra/backend.Dockerfile` 多阶段构建，运行时 UID `10001`，暴露 `8000`；`api` 以 `uvicorn devflow.app:create_app --factory` 启动，入口脚本从 `/run/secrets/database_url` 读取连接串。`model-runtime` target 安装可选 `models` extra（torch / sentence-transformers），供 `model-worker` 使用。
  - `infra/frontend.Dockerfile` 使用 Node `24.21.0` 构建 Next.js standalone 输出，运行时 `node server.js`，暴露 `3000`。
  - `gateway` 由 Compose 内联构建 Caddy `2.11.4`，按 `infra/Caddyfile` 将 `/api/*`、`/health/*` 转发到 `api:8000`，其余请求转发到 `frontend:3000`。
- **部署**：生产使用 `docker compose -f infra/compose.yaml up -d --build`；TLS 由 Caddy 根据 `PUBLIC_BASE_URL` 提供，宿主机暴露 80/443。数据库、Milvus、MinIO、etcd 均为项目私有服务，生产 Compose 不向宿主机发布这些端口。

## 已知限制与注意事项

- **依赖 Docker 与较新的 Compose**：完整栈需要 Docker Compose `>=2.24.4`；PostgreSQL、Milvus、etcd、MinIO 都以容器运行。
- **Git 版本与共享工作区**：工作区要求 Git `>=2.43`；API 和 Worker 必须使用同一个 owned 绝对缓存根目录（Compose 中 API 只读、Worker 可写挂载同一卷）。
- **Secret 必须是文件**：应用不从不安全的环境变量读取密钥；`DEVFLOW_SECRETS_DIR` 中的文件权限与内容需要自行保护，且不要放在仓库目录内。
- **生产必须 HTTPS 且 Origin 匹配**：`PUBLIC_BASE_URL` 在 production 下必须是 HTTPS origin；浏览器的写请求还要求 `Origin` 与 `PUBLIC_BASE_URL` 完全一致并携带 CSRF token，否则拒绝。
- **模型 worker 是单实例角色**：通过本地 owner lock 保证同一时间只有一个进程加载模型；不要横向扩容 `model-worker`。`MODEL_DEVICE` / `MODEL_DTYPE` 必须显式选择，Compose 传入空值会直接启动失败，不会静默回退到 CPU。
- **云端对话默认关闭**：开启需要完整的价格表、预算、有效密钥文件，并单独部署 LangGraph checkpoint schema；模型与调用地址固定在代码中。
- **Milvus 的启动策略不同**：生产 Compose 始终启动 Milvus 三件套；开发栈需要显式加 `--profile milvus`（本地模型再加 `--profile models`）。
- **当前快照不含测试与文档**：仓库中没有 `backend/tests/`、`frontend/tests/` 与 `docs/`；CI 在测试目录缺失时跳过 pytest 与 Playwright 并输出 `::notice`，目录补回后自动恢复执行。仓库也未包含 LICENSE 文件。
- **本地文件系统假设**：工作区缓存与模型 owner lock 都是本地路径；当前 Compose 编排面向单机部署（或具备共享卷语义的单组主机）。Windows 主机进程使用 `asyncio.SelectorEventLoop`（psycopg 要求），示例命令已包含 `--loop asyncio:SelectorEventLoop`。
