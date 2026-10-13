# DevFlow AI 第一阶段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付允许团队成员登录、接入本人可访问的私有仓库、提交只读检查、查看持久进度并验证重启恢复的可部署平台基础。

**Architecture:** 一个 Python 包分别启动 FastAPI API 和独立 Worker；PostgreSQL 持久化身份、授权、作业、事件及审计。Next.js 通过同源 REST/SSE 访问后端，GitHub App 用户授权与安装范围共同约束仓库访问。

**Tech Stack:** Python 3.12、UV、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、Psycopg 3、HTTPX、PyJWT、cryptography、PostgreSQL、Next.js、React、TypeScript、Tailwind、TanStack Query、pytest、Playwright、Docker Compose。

**Spec:** [第一阶段平台基础设计](../specs/2026-09-30-devflow-foundation-design.md)；上位约束见 [平台架构设计](../specs/2026-09-30-devflow-platform-design.md)。两份规格已经用户确认。

**Status:** 2026-10-13，按用户确认的 A（Subagent-driven）完成一期本地实现、验证与独立最终复审。405 项后端测试、37 项浏览器测试及受影响镜像验证通过；真实外部验收仍未执行，见 [验收记录](../../operations/foundation-acceptance.md)。复选框保留这一边界，不声明整个平台或真实部署已完成。

## Global Constraints

- 单团队、多用户、多仓库、支持私有仓库；仅接入 github.com。用户角色为 `admin`、`member`，管理员不自动获得仓库内容权限。
- 后端为一个 Python 3.12 包，UV 是唯一 Python 依赖管理入口；Windows PowerShell 开发、Linux Compose 部署；提交 `uv.lock`、`package-lock.json` 和解释器版本文件。
- `state` 与发起登录的浏览器绑定，10 分钟过期、原子消费一次；会话采用 256 位随机不透明 token，数据库仅存哈希，固定有效期 8 小时。
- 生产 Cookie 使用 `HttpOnly`、`Secure`、`SameSite=Lax`、`Path=/`、无 Domain 的 `__Host-devflow_session`。
- `BOOTSTRAP_ADMIN_GITHUB_ID` 仅用于首次数据库引导；不能停用或降级最后一个有效管理员。
- 权限正向缓存最长 60 秒；已知撤权失效缓存；提交检查和 Worker 开始检查时绕过缓存重新验证；一期运行记录仅创建者可见。
- 一期只有 `repository.access_probe` 作业，不调用模型、不克隆代码、不同步 Issue/PR/CI、不创建向量索引、不实现通用 DAG 或 GitHub 写操作。
- 最多 3 次尝试（含首次）；第一次与第二次失败的退避分别为 10 秒、30 秒；还须遵守 GitHub 下一可用时间。
- 租约为 30 秒，心跳间隔 10 秒；单次检查尝试最多运行 120 秒；默认 Worker 并发为 4；时间判断使用数据库时钟。
- 作业、对应事件及必需审计同事务提交；事件 `(job_id, sequence)` 唯一，序号从 1 单调递增；旧租约不能写回。
- SSE 每 1 秒检查新增事件，每 15 秒发送心跳；权限最迟每 60 秒重新验证；本地停用或已处理撤权在下一读取循环关闭连接。
- 列表 `limit` 默认 20、最大 100；不透明 `cursor` 分页；跨用户不可复用游标。
- 无会话 401；无权或不可见资源 404；成员管理角色不足 403；依赖故障 503；幂等键输入冲突 409；非法 SSE 游标 400。
- 数据库并发、锁、迁移和恢复使用真实 PostgreSQL 验证；不得以 SQLite 或 mock 替代这些检查。
- 凭据、Cookie、私钥、authorization code、PKCE verifier 不得进入响应、日志、事件、审计或构建产物；真实 GitHub 验证未运行就标记未执行。

## Review Focus

1. GitHub 403 可能是限流，刷新失败可能是配置/网络故障；不得误停用用户或使用安装令牌兜底。任务 3、5、8 覆盖分类和重试时间。
2. OAuth 回调、凭据刷新、最后管理员修改存在跨进程竞争；只能成功消费一次，不得让迟到写入恢复撤销权限。任务 2、3、4、6 覆盖并发与版本保护。
3. 安装分页过滤后可能为空但仍有下一页；仓库改名或同名重建不能改变数字 ID 绑定。任务 5 覆盖两层分页和身份校验。
4. 超时 Worker 仍可能返回；租约重新领取后，旧持有者不能覆盖结果、事件和审计。任务 7、8 覆盖条件更新与原子性。
5. SSE 开始或重连时权限可能刚撤销，终态连接关闭也可能触发浏览器自动重连；不能先发内容再授权或无限重连。任务 9、11、12 覆盖。

## 执行约定与版本策略

所有命令默认从实际工作区根目录执行，路径均相对该目录；Windows 使用 `npm.cmd` 代替 `npm`。`uv --directory backend` 同时切换工作目录，避免 `--project` 下测试相对路径含义不同。Python 业务、测试及运维入口全部通过 `uv run`。

执行时保留现有文件。先检查 Git 状态；当前只有原始方案和设计文档，无 Git。任务 1 初始化 Git、提交文档基线，再按 `using-git-worktrees` 判断是否需要隔离，优先使用可用的原生 worktree 工具。实际工作树路径由工具返回，不能硬编码旧路径。提交使用现有 Git 身份，缺失时不虚构作者；缺少提交身份不阻止已授权的本地实现与测试。

每个任务按“失败测试 → 最小实现 → 定向验证 → 提交”执行。表中的测试名和断言是必须写入真实测试的合同，`result` 等变量由同一测试的 setup/action 得到，不是需要新增的框架。只为有行为的配置校验写测试，不为目录或静态文案创建镜像测试。阶段性验证失败先定位原因，不跳过或掩盖失败。

文件清单中的 `{a,b}.py` 是明确列举的多个文件。为减少重复，Python 业务短路径如 `auth/models.py`、`app.py` 相对 `backend/src/devflow/`，`tests/` 相对 `backend/`，前端任务的 `app/` 相对 `frontend/src/`、`src/` 相对 `frontend/`。`pyproject.toml` 指 `backend/pyproject.toml`；`README.md`、`.env.example` 均位于仓库根目录。创建与修改仅限列出的责任域。

依赖选择：Python 限定 `>=3.12,<3.13`；SQLAlchemy 2、Pydantic 2、Psycopg 3。Node.js 选 24 LTS；PostgreSQL 选 17 的执行时受支持补丁版；Next.js/React 使用执行时官方稳定兼容组合，Tailwind 4、TanStack Query 5。在对应初始化任务解析、验证并记录精确版本，镜像固定实际 tag 和 digest，不使用浮动 `latest`。本计划不假称这些依赖已经安装解析。[Node 发布状态](https://nodejs.org/en/about/previous-releases)、[Next.js 安装要求](https://nextjs.org/docs/app/getting-started/installation)、[PostgreSQL 支持周期](https://www.postgresql.org/support/versioning/)

UV 缓存若受执行环境限制，可用工作区内忽略目录 `.cache/uv`，不更改全局 UV 配置或关闭 TLS 校验。GitHub、Docker 引擎和下载权限通过正常工具权限机制处理，不能用 mock 成功替代受阻的真实验收。

配置名固定为 `APP_ENV`、`PUBLIC_BASE_URL`、`DATABASE_URL`、`BOOTSTRAP_ADMIN_GITHUB_ID`、`GITHUB_APP_ID`、`GITHUB_CLIENT_ID`、`GITHUB_ALLOWED_INSTALLATION_IDS`（JSON 整数数组），以及 `GITHUB_CLIENT_SECRET_FILE`、`GITHUB_PRIVATE_KEY_FILE`、`GITHUB_WEBHOOK_SECRET_FILE`、`CREDENTIAL_ENCRYPTION_KEY_FILE`、`APP_SIGNING_KEY_FILE`。Settings 使用对应 snake_case 字段；由文件加载的内存秘密仅暴露 SecretStr/受控 bytes。测试在隔离临时目录生成合成秘密；生产校验必填值，不添加“跳过认证”的环境开关。

## 文件职责与任务依赖

| 任务 | 文件域 | 可验收交付物 | 前置 |
| --- | --- | --- | --- |
| 1 | `backend/` 元数据、`config.py`、`app.py`、`errors.py`、`logging.py` | 可安装包、配置校验、存活接口 | 无 |
| 2 | `db.py`、`bootstrap.py`、`auth/models.py`、`audit/`、`pagination.py`、迁移和数据库测试夹具 | 身份/审计持久化、引导管理员、readiness | 1 |
| 3 | `github/client.py`、`github/credentials.py`、`github/schemas.py`、`security.py` | 可测试的 GitHub 边界和凭据轮换 | 2 |
| 4 | `auth/` 会话、服务、依赖、路由与用户管理 | 登录、注销、CSRF、成员权限 | 3 |
| 5 | `repositories/`、GitHub 安装模型 | 候选仓库、接入、授权隔离与完整分页 | 4 |
| 6 | `github/webhooks.py`、生命周期迁移 | 撤权、验签和事件去重 | 5 |
| 7 | `jobs/` 模型、提交与查询服务 | 幂等持久提交、可授权读取的结果和事件 | 6 |
| 8 | `jobs/queue.py`、`jobs/handlers.py`、`worker.py` | 可重试、可恢复、阻止旧租约写回的只读检查 | 6、7 |
| 9 | `jobs/events.py`、SSE 路由 | 有权限约束的事件回放 | 8 |
| 10 | 前端基础、登录、成员与审计页面 | 会话与管理交互 | 4、6 |
| 11 | 前端仓库、检查详情和 SSE Hook | 端到端用户交互 | 9、10 |
| 12 | 镜像、Compose、CI、运维文档、浏览器联调夹具 | 可复现部署、恢复演练和验收记录 | 11 |

默认按编号执行。任务 10 的前端工作可在后端接口稳定后与任务 7–9 并行；共享配置、依赖锁文件和迁移由单一任务负责人修改，不并发争用。每个 Python 包创建所需 `__init__.py`；不预建二期至七期的空模块。

## Task 1: UV 项目与可验证的 API 启动

**Files:** 创建 `.gitignore`、`.env.example`、`README.md`、`backend/pyproject.toml`、`backend/uv.lock`、`backend/.python-version`、`backend/src/devflow/{__init__,app,config,errors,logging}.py`、`backend/tests/{conftest,test_health,test_config,test_logging}.py`。

**Interfaces:** 产出 `Settings`（环境、公开 URL、数据库 URL、GitHub App/Client ID、secret 文件路径、允许安装 ID、bootstrap ID、会话/作业数值配置）、`create_app(settings: Settings | None = None) -> FastAPI`、`AppError(code: str, status: int, message: str, retry_at: datetime | None = None)`。应用异常转换为规格规定的安全错误体；HTTP 请求带独立 `request_id`。没有数据库时存活接口仍能工作。

固定错误码至少包括 `unauthenticated`、`forbidden`、`resource_not_found`、`validation_error`、`idempotency_conflict`、`github_rate_limited`、`github_unavailable`、`github_configuration_error`、`reauth_required`、`attempt_timeout`；Worker 仅对 rate_limited/unavailable/attempt_timeout 自动重试。FastAPI 参数校验保持 422，但转换为共同错误体，去掉可能携带秘密的原始 input。

- [x] **1. 建立版本管理与测试运行环境。** 保存并提交已批准的原始方案、规格和本计划；按上述工作树约定准备实现位置。执行 `uv init --package --name devflow --python 3.12 --vcs none backend`，调整到 `src/devflow`；加入一期运行依赖及测试工具，精确 pin 选定 Python 补丁版。使用 `uv add` 维护元数据和锁文件；忽略 `.venv`、`.cache`、`.env`、私钥、生成报告和 Node 构建目录。
- [x] **2. 写失败测试。** `test_live_does_not_call_github`: `assert response.status_code == 200; assert github_calls == []`；`test_production_rejects_http_or_missing_secrets`: `with pytest.raises(ValidationError)`；`test_development_http_requires_loopback`: 非 loopback 的 HTTP URL 必须失败；`test_error_and_log_redaction`: `assert all(secret not in output for secret in sentinel_secrets)`，包括 URL 查询中的 OAuth code、Cookie 和 Authorization。
- [x] **3. 验证失败。** `uv --directory backend run pytest tests/test_health.py tests/test_config.py tests/test_logging.py -q`；预期因未实现的 app/config/脱敏行为失败，不能因缺 pytest 或错误解释器失败。
- [x] **4. 实现并验证。** 构建 app factory、`GET /health/live`、统一异常与结构化日志；显式区分 development/test/production，秘密用 `SecretStr` 或受控文件读取，不记录设置对象。重复上述命令并运行 `uv --directory backend run ruff check .`、`uv --directory backend run ruff format --check .`，均应成功；`uv --directory backend sync --locked` 不改变锁文件。
- [x] **5. 提交。** 检查实际差异，仅暂存本任务路径，提交 `chore: initialize uv backend and application runtime`。

## Task 2: PostgreSQL、审计与身份基础

**Files:** 创建 `backend/src/devflow/db.py`、`bootstrap.py`、`pagination.py`、`auth/models.py`、`auth/types.py`、`github/models.py`、`audit/models.py`、`audit/service.py`、`backend/alembic.ini`、`backend/migrations/env.py`、`backend/migrations/versions/0001_identity_audit.py`、`infra/compose.test.yaml`、`backend/tests/test_pagination.py`、`backend/tests/integration/test_{migrations,bootstrap,audit}.py`；修改 `app.py`、测试 `conftest.py` 与 `test_health.py`。

**Interfaces:** `create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]`；`bootstrap_admin(factory, github_id: int) -> None`；`write_audit(session: AsyncSession, entry: AuditEntry) -> UUID` 只 flush、不 commit。`Actor(id: UUID, github_id: int, role: Literal['admin','member'], auth_version: int)` 是认证后身份值。`AuditEntry` 包含 actor ID（可空）、action、resource type/ID、outcome、request/job ID、固定 reason code。

共同类型在此定义：`Role = Literal['admin','member']` 放在 auth/types.py；`Page[T](items: list[T], next_cursor: str | None)`、`encode_cursor(actor_id: UUID, purpose: str, position: dict, limit: int) -> str`、`decode_cursor(token: str, actor_id: UUID, purpose: str, limit: int) -> dict` 放在 pagination.py。游标使用 APP_SIGNING_KEY 的标准 HMAC 校验，绑定用户、用途与 limit；本地列表以 `(created_at,id)` 稳定排序，不使用无序 OFFSET。

- [x] **1. 写失败测试和真实数据库夹具。** `test_migrate_empty_database`: `assert current_revision == script_head`；`test_bootstrap_is_once_and_concurrent_safe`: 两个连接并发引导后 `assert admin_count == 1`，配置改变后再执行不覆盖已有角色；`test_audit_rolls_back_with_mutation`: 强制事务异常后 `assert new_users == [] and new_audits == []`；`test_ready_detects_schema_mismatch`: 连接失败或 revision 不匹配返回 503；`test_cursor_rejects_other_actor_or_purpose`: 篡改签名或跨用户/用途使用时 `assert error.status == 400`。
- [x] **2. 验证失败。** `docker compose -p devflow-test -f infra/compose.test.yaml up -d --wait postgres`；`uv --directory backend run pytest tests/integration/test_migrations.py tests/integration/test_bootstrap.py tests/integration/test_audit.py tests/test_health.py tests/test_pagination.py -q`。测试配置仅连接独立测试数据库，每项使用隔离 schema；拒绝指向非测试库，不删除开发/生产数据。
- [x] **3. 实现数据基础。** 创建 users、oauth_states、sessions、github_credentials、audit_logs 表及外键/唯一约束，GitHub ID 用 BigInteger，UTC 时间用带时区列。凭据表带 `version`、`needs_reauth`；用户带 `auth_version`；会话保存 cookie 哈希和 expiry。bootstrap 使用事务级 advisory lock，用户表已有记录则不改写；通过 `python -m devflow.bootstrap` 显式执行，不在登录或每次 API 启动时执行。
- [x] **4. 验证事务与连接生命周期。** 每个请求、Worker 作业和心跳用独立 AsyncSession；补齐 app lifespan 与 readiness 的数据库/schema 检查。重复第 2 步测试，运行 `uv --directory backend run alembic check`（配置指向测试库），预期无未生成的模型差异。审计只允许固定字段，拒绝透传任意外部响应。
- [x] **5. 提交。** `feat: add postgres persistence identity and audit foundation`，包含基础测试数据库的显式启动说明。

## Task 3: GitHub 客户端与凭据轮换

**Files:** 创建 `backend/src/devflow/security.py`、`github/client.py`、`github/schemas.py`、`github/credentials.py`、`backend/tests/test_github_client.py`、`tests/test_secret_storage.py`、`tests/integration/test_credential_refresh.py`、`tests/fixtures/github/` 下实际使用的脱敏 JSON；修改 `github/models.py` 仅限与 0001 已声明字段一致的服务访问，修改 `app.py` 接受可注入 GitHubClient。

**Interfaces:** `TokenSet(access_token: SecretStr, refresh_token: SecretStr | None, expires_at: datetime | None, refresh_expires_at: datetime | None)`；`GitHubRepository(id: int, full_name: str, private: bool, default_branch: str | None)`；`UpstreamPage[T](items: list[T], next_page: int | None)`。`GitHubClient(http: httpx.AsyncClient, settings: Settings)` 提供异步 `exchange_code(code, verifier) -> TokenSet`、`refresh_token(refresh) -> TokenSet`、`get_user(token) -> GitHubUser`、`list_installations(token, page: int) -> UpstreamPage[GitHubInstallationInfo]`、`list_repositories(token, installation_id: int, page: int) -> UpstreamPage[GitHubRepository]`、`get_installation(installation_id: int) -> GitHubInstallationInfo`。`CredentialService(factory, github, cipher).get_user_token(actor: Actor) -> SecretStr` 在必要时串行刷新。

github/schemas.py 同时定义 `GitHubUser(id: int, login: str, avatar_url: str | None)`、`GitHubInstallationInfo(id: int, app_id: int, account_id: int, suspended_at: datetime | None)`。Token 参数统一 SecretStr。app factory 向后兼容扩展为 `create_app(settings: Settings | None = None, *, github: GitHubClient | None = None) -> FastAPI`；未注入时构建真实客户端，测试使用同一接口的 MockTransport 客户端。

| 方法 | 官方端点及凭据 |
| --- | --- |
| get_user | `GET /user`，用户 token |
| list_installations | `GET /user/installations`，用户 token |
| list_repositories | `GET /user/installations/{installation_id}/repositories`，用户 token |
| get_installation | `GET /app/installations/{installation_id}`，App JWT |
| exchange_code / refresh_token | `POST https://github.com/login/oauth/access_token`，分别为 code + PKCE verifier、`grant_type=refresh_token` + refresh token |

依据：[安装与仓库接口](https://docs.github.com/en/rest/apps/installations)、[App 安装核验](https://docs.github.com/en/rest/apps/apps#get-an-installation-for-the-authenticated-app)、[刷新凭据](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)。刷新成功后必须一起替换 token 对，不能继续使用旧 access token。[限流分类](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit) 先于一般权限错误分类。

- [x] **1. 写失败测试。** `test_github_rate_limit_classification`: primary 403、secondary 403/429、普通权限 403 分别映射可重试错误/不可见；`assert retry_at >= provider_deadline`，无等待头的 secondary limit 至少等待 60 秒。`test_refresh_failure_classification`: `bad_refresh_token` 要求重授权，`incorrect_client_credentials` 为配置故障，timeout/5xx 为暂不可用；`assert installation_fallback_calls == 0`。`test_cross_process_refresh_once`: 两个真实数据库连接在屏障后争用，`assert refresh_requests == 1` 且新 token 对原子保存；迟到失败不能清除新版本。`test_ciphertext_and_logs_hide_tokens`: 数据库密文和输出不含哨兵明文。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/test_github_client.py tests/test_secret_storage.py tests/integration/test_credential_refresh.py -q`；HTTPX MockTransport 提供明确响应，不连接真实 GitHub。
- [x] **3. 实现官方端点边界。** REST 固定 `https://api.github.com`，OAuth 固定 `https://github.com/login/oauth/access_token`；App JWT 使用 PyJWT 与部署私钥。验证安装响应 ID、app ID、suspended 状态；请求 JSON/权限错误结构受控解析。HTTP 每次调用 10 秒总超时，客户端不隐式循环重试，由任务层决策。分页仅提取可信 `Link rel=next` 中的页码，不跟随任意主机链接。
- [x] **4. 实现并发刷新并验证。** Fernet 加密凭据；凭据行锁覆盖重新读取、有限时刷新和原子替换；各进程共用数据库锁，获取锁后先检查新版本。终态凭据错误条件更新当前版本；超时不清空凭据；若远端已轮换但响应丢失，后续明确无效时要求重新授权。重复第 2 步测试，增加等待刷新期间撤权/旧 token 请求失败不覆盖新版本的断言。
- [x] **5. 提交。** `feat: add github client and safe credential rotation`。

## Task 4: 登录、会话与成员管理

**Files:** 创建 `backend/src/devflow/auth/{schemas,service,dependencies,routes,admin}.py`、`backend/tests/test_csrf.py`、`tests/integration/test_{auth,sessions,members}.py`；修改 `security.py`、`app.py`，挂载规格中的 auth/session/admin user 路由。

**Interfaces:** `AuthService(factory, github, cipher, settings).begin_login(return_path: str) -> LoginStart`；`.complete_login(code: str, state: str, browser_nonce: str) -> SessionIssue`；`.resolve_session(raw_token: str) -> Actor`；`.logout(raw_token: str) -> None`。`LoginStart(authorize_url: str, browser_nonce: SecretStr)`、`SessionIssue(raw_token: SecretStr, expires_at: datetime)` 和 `UserView(id: UUID, github_id: int, login: str | None, role: Role, enabled: bool)` 在 auth/schemas.py 定义。FastAPI `require_actor(request: Request) -> Actor`、`require_admin(actor: Actor) -> Actor` 统一引用；`update_member(actor: Actor, target_id: UUID, role: Role | None, enabled: bool | None) -> UserView` 事务化管理。GET session 返回 `{user: UserView, csrf_token: str}`；GET admin/users 使用任务 2 的 `Page[UserView]`。

响应补充约定：OAuth start 为 302、成功 callback 为 303；未允许成员登录 403；logout 204；新增允许成员 201、重复 GitHub ID 409；成员修改 200、最后管理员约束冲突 409。POST repositories 新注册 201、已存在且仍可访问时 200。所有拒绝分支仍使用共同错误体。

- [x] **1. 写失败测试。** `test_oauth_state_is_single_use_and_browser_bound`: 错误浏览器/重复回调无 session，10 分钟边界失效；`test_login_requires_allowlist`: 未允许的 GitHub ID 无 session。`test_session_expires_and_logout_invalidates`: 第 8 小时与注销后返回 401；`test_last_admin_concurrent_changes`: 两个连接并发降级/停用后 `assert active_admin_count >= 1`。`test_member_disabled_while_login_inflight`: 回调不能重新启用被停用成员。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/test_csrf.py tests/integration/test_auth.py tests/integration/test_sessions.py tests/integration/test_members.py -q`。允许名单、PKCE、clock 边界用确定输入，数据库并发用屏障，不用长 sleep。
- [x] **3. 实现登录与会话。** 一次性 state、浏览器 nonce、短期加密 PKCE verifier；用原子消费阻止双回调，身份核对使用 GitHub ID。生成 32 字节随机 session token，数据库存 SHA-256；会话 CSRF 使用独立 `APP_SIGNING_KEY` 的标准 HMAC，按用途区分，不复用凭据加密密钥。加入 GET session、POST logout，生产 cookie 与本地开发 cookie 严格分开。
- [x] **4. 实现权限修改并验证。** 用户管理事务使用与 bootstrap 协调的管理员集合 advisory lock；停用会话失效、增加 auth_version、写审计。Origin 精确匹配公开 origin；CSRF 对浏览器写操作生效，OAuth callback/Webhook 使用各自认证。增加 `assert csrf_missing.status_code == 403`，重复第 2 步并核对 cookie 属性、站外 return_path 拒绝及安全错误体；Webhook 的端到端认证断言由任务 6 负责。
- [x] **5. 提交。** `feat: add github login sessions and member administration`。

## Task 5: 仓库发现、接入和统一授权

**Files:** 创建 `backend/src/devflow/repositories/{models,schemas,policy,service,routes}.py`、`backend/migrations/versions/0002_repositories.py`、`backend/tests/integration/test_{repository_discovery,repository_access}.py`；扩展 `tests/test_pagination.py`；修改 `github/models.py` 增加安装表、`app.py`、迁移模型导入。

**Interfaces:** 消费任务 2 的 Page/cursor 和任务 3 的 GitHubClient/CredentialService。`RepositoryView(id: UUID, github_repository_id: int, installation_id: int, full_name: str, private: bool, default_branch: str | None)`；`CandidateRepository` 在 repositories/schemas.py 定义，字段为 RepositoryView 去掉平台 id。`RepositoryService.discover(actor: Actor, limit: int, cursor: str | None) -> Page[CandidateRepository]`、`.register(actor: Actor, installation_id: int, github_repository_id: int) -> RepositoryView`、`.list_enabled(actor: Actor, limit: int, cursor: str | None) -> Page[RepositoryView]`。`RepositoryPolicy.require(actor: Actor, repository_id: UUID, *, fresh: bool = False) -> RepositoryView` 是所有仓库业务调用入口。

- [x] **1. 写失败测试。** `test_discovery_follows_both_pagination_levels`: 允许安装在第 2 页、目标仓库也在第 2 页，`assert target_id in discovered_ids`；过滤空页仍继续。`test_cursor_bound_to_actor_and_query`: B 使用 A 游标或篡改用途/limit 时 400 且无数据。`test_admin_cannot_bypass_github_access`: App 可访问但用户列表无目标 ID，`assert response.status_code == 404`；安装令牌零次兜底。`test_numeric_id_binding`: 同名不同 ID 拒绝，原 ID 改名保持同一平台记录。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/test_pagination.py tests/integration/test_repository_discovery.py tests/integration/test_repository_access.py -q`。
- [x] **3. 实现发现与注册。** user installations 与 installation repositories 两层分页，固定上游 `per_page=100`；应用游标签名并绑定 actor、用途、limit、允许安装配置版本、上游页与页内偏移，不包含 token。安装配置白名单、在线安装核验、用户列表中的仓库数字 ID 缺一不可。仓库表区分平台 enabled 和远端 available 状态；唯一 GitHub repo ID，允许转移/改名时更新经重新核验的归属，不重复建同一仓库。
- [x] **4. 实现统一策略并验证。** 本地身份、安装及仓库版本每次从数据库读取；正向缓存最多 60 秒，撤权版本变化立即失效，fresh 必查远端。限流/超时不给旧缓存续期；普通 403/404 对外不可见。注册仅 admin，幂等重复返回同一 UUID。补充 `test_cache_expiry_and_local_version_invalidation`、`test_user_list_filters_private_repositories`，重复第 2 步；分页后的输出仍再次按授权过滤，不能泄露不可见项计数。
- [x] **5. 提交。** `feat: add repository onboarding and user scoped authorization`。

## Task 6: Webhook 撤权与生命周期一致性

**Files:** 创建 `backend/src/devflow/github/webhooks.py`、`backend/migrations/versions/0003_webhook_deliveries.py`、`backend/tests/integration/test_webhooks.py`；修改 `github/models.py`、`auth/service.py`、`repositories/policy.py`、`app.py`。

**Interfaces:** `handle_webhook(raw_body: bytes, signature: str, delivery_id: str, event_name: str) -> None`：先验证原始字节签名，再解析允许字段，在一个事务中去重、失效和写审计；有效处理/重复均返回 204。模型只保存白名单字段与状态，不保存无边界原文。

- [x] **1. 写失败测试。** `test_bad_signature_does_not_reserve_delivery`: 无效签名 401，`assert valid_delivery_count == 0`，同 ID 后续有效事件可处理；`test_duplicate_delivery_is_atomic`: 并发有效重复只有一份失效事件。`test_uninstall_or_repository_remove_invalidates_access`: `assert policy_denied`；`test_user_revocation_cannot_be_undone_by_late_refresh`: 刷新或登录迟到不能恢复被撤销版本；`test_old_restore_requires_live_verification`: 旧新增事件不能直接激活授权。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/integration/test_webhooks.py tests/test_csrf.py tests/integration/test_credential_refresh.py -q`。
- [x] **3. 实现生命周期处理。** 接受规格规定三类事件，相关 action 显式分支；`suspend/deleted`、移除仓库和用户撤权立即增加数据库授权版本；用户撤权同时使凭据/会话失效。`created/unsuspend/added` 仅要求在线复核；未知但有效事件记录忽略结果。关联判断使用 installation ID、repository ID 或 GitHub user ID，不使用显示名。
- [x] **4. 验证事务失败和跨进程失效。** 注入数据库失败返回 503 且不确认 delivery；有效事件无需浏览器 CSRF；两套独立策略缓存实例在本地版本更新后都拒绝访问。重复第 2 步，审计拒绝路径不得存签名秘密、token 或原始 payload。
- [x] **5. 提交。** `feat: handle github authorization lifecycle webhooks`。

## Task 7: 幂等作业提交与持久事件

**Files:** 创建 `backend/src/devflow/jobs/{models,schemas,service,routes}.py`、`backend/migrations/versions/0004_jobs_events.py`、`backend/tests/integration/test_{job_submission,job_visibility,job_transactions}.py`；修改 `app.py`、迁移模型导入。

**Interfaces:** `JobStatus = Literal['queued','running','retry_wait','succeeded','failed']`；`ProbeResult(github_repository_id: int, full_name: str, private: bool, default_branch: str | None, checked_at: datetime)`；`JobView(id: UUID, repository_id: UUID, requested_by: UUID, status: JobStatus, attempt: int, max_attempts: int, result: ProbeResult | None, error_code: str | None, last_sequence: int)`，实际 JSON 还包括规格的创建/更新时间。`JobService.submit_probe(actor: Actor, repository_id: UUID, idempotency_key: str) -> JobView`、`.get_visible(actor: Actor, job_id: UUID) -> JobView`；`append_event(session: AsyncSession, job: Job, status: JobStatus, error_code: str | None = None) -> RunEvent` 不 commit。

- [x] **1. 写失败测试。** `test_concurrent_submission_idempotency`: 同用户/类型/key/输入并发提交只有一个 job，`assert first.id == second.id`；不同 repo 同 key 返回 409。`test_idempotency_does_not_bypass_reauthorization`: 权限撤销后重放同 key 也返回 404。`test_only_creator_can_read_job`: 同仓库另一用户或无仓库权限管理员读取返回 404。`test_state_event_audit_commit_together`: 注入事件或审计写入故障后 `assert created_jobs == created_events == created_audits == 0`。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/integration/test_job_submission.py tests/integration/test_job_visibility.py tests/integration/test_job_transactions.py -q`。
- [x] **3. 实现提交、模型与约束。** `Idempotency-Key` 必填、1–128 个可见 ASCII 字符；input 固定为平台 repo UUID，使用确定的 JSON 序列化计算 input_hash。唯一约束覆盖 requested_by/kind/key；并发冲突后读取原记录、核对 hash 与权限。202 返回前提交事务，重复相同请求仍返回 202 和原作业。`jobs` 保存规格全部租约字段并增加 `last_sequence`；建立到期候选 `(status,available_at,created_at,id)` 与运行租约过期索引；JobView 不暴露租约持有者或内部 input_hash。
- [x] **4. 实现事件与验证。** 锁住作业行后递增 last_sequence，写 `RunEvent` 和 AuditLog。事件名统一 `job.status`，payload 只包含 `status`、`attempt`、`error_code`，结果通过 REST 获取。一期终态事件是 status 为 succeeded/failed 的事件，不另造第二套序号。所有新增测试通过，并检查先授权再读取既有幂等记录及结果的顺序。
- [x] **5. 提交。** `feat: persist idempotent repository check jobs and events`。

## Task 8: Worker 租约、重试和只读执行

**Files:** 创建 `backend/src/devflow/jobs/queue.py`、`jobs/handlers.py`、`backend/src/devflow/worker.py`、`backend/tests/integration/test_{queue_leases,worker_recovery,probe_handler}.py`；修改 `jobs/models.py` 仅补充与已声明 schema 一致的索引映射、`pyproject.toml` 的 Worker 入口、`README.md`。

**Interfaces:** `Lease(job_id: UUID, owner: UUID, generation: int, attempt: int)`；`Queue(factory).claim(owner: UUID) -> Lease | None`、`.heartbeat(lease: Lease) -> bool`、`.complete(lease: Lease, result: ProbeResult) -> bool`、`.fail(lease: Lease, code: str, retryable: bool, retry_at: datetime | None = None) -> bool`、`.recover_expired() -> int` 均为 async。`run_probe(job_id: UUID, factory, policy, github, credentials) -> ProbeResult`；`run_worker(settings: Settings, *, stop: asyncio.Event, github: GitHubClient | None = None) -> None`，CLI 为 `uv run python -m devflow.worker`。

- [x] **1. 写失败测试。** `test_two_workers_claim_once`: 两连接同时领取，`assert len(valid_leases) == 1`；`test_stale_generation_cannot_complete`: 租约过期重新领取后 `assert await queue.complete(old_lease, result) is False`，事件与结果不变；`test_retry_budget_and_deadline`: attempt 依次 1/2/3，10/30 秒退避，第三次失败终态 failed；`test_probe_rechecks_creator_authorization`: 入队后撤权、执行前拒绝且不返回 metadata。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/integration/test_queue_leases.py tests/integration/test_worker_recovery.py tests/integration/test_probe_handler.py -q`。通过 SQL 设置过期租约与 available_at 构造边界；进程恢复测试用子进程/容器，不仅模拟函数返回。
- [x] **3. 实现租约状态机。** claim 使用有界短事务 `FOR UPDATE SKIP LOCKED`，领取 queued 或到期 retry_wait，增加 attempt/generation、写 running 事件/审计；网络请求在提交后执行。所有写回带 job ID、owner、generation、running 状态与有效租约条件。heartbeat 用独立 session，不能与处理器共享 AsyncSession。回收先使旧租约失效，再写 retry_wait 或最终 failed 及事件/审计。
- [x] **4. 实现 Worker 并验证恢复。** 并发 4、租约 30 秒、心跳 10 秒、每次执行 120 秒；失败重试时间取退避与 GitHub 限流截止的较晚者。租约丢失立即取消本地协程；优雅停止停止领取并有界等待，超出等待后让租约自然回收。CLI 同时处理 Linux 信号与 Windows KeyboardInterrupt。run_probe 重新读取创建者 enabled/auth_version、fresh 检查仓库，使用用户 token 获取安装仓库列表中的目标 ID 和 metadata；响应后再次检查本地授权是否已失效。强杀旧 Worker 后新 Worker 恢复，旧进程返回不能写回；退出后无需 API 存活。结构化日志记录心跳、队列等待与失败计数，不增加新的公开监控接口。
- [x] **5. 提交。** 定向测试全部通过，提交 `feat: execute recoverable repository checks with fenced leases`。

## Task 9: 可回放且可撤权的 SSE

**Files:** 创建 `backend/src/devflow/jobs/events.py`、`backend/tests/integration/test_sse.py`；修改 `jobs/routes.py`、`jobs/schemas.py`。

**Interfaces:** `EventView(sequence: int, status: JobStatus, attempt: int, error_code: str | None)`；`read_events(actor: Actor, job_id: UUID, after: int, limit: int = 100) -> list[EventView]`；`stream_job_events(request: Request, actor: Actor, job_id: UUID, after: int) -> AsyncIterator[str]`。路由在创建 StreamingResponse 前完成授权和游标校验；stream 内每轮重新检查会话和本地授权版本。

- [x] **1. 写失败测试。** `test_sse_auth_before_headers_or_data`: 无权/过期会话在发送任何事件前 404/401；`test_sse_replay_and_header_priority`: after=0、Last-Event-ID=2 时 `assert received_sequences == [3,4]`；`test_sse_rejects_invalid_cursor`: 负数、非整数、超出当前 last_sequence 返回 400。`test_sse_local_revoke_and_expiry`: 本地撤权下一读取循环结束、远端权限到 60 秒必须复核；`test_terminal_cursor_closes_without_replay`: 已消费终态不再发送旧事件。
- [x] **2. 验证失败。** `uv --directory backend run pytest tests/integration/test_sse.py -q`。测试读取流而不等到无限响应结束；时间等待可注入受控 scheduler，授权时间仍按数据库记录校验，不靠扩大超时过测试。
- [x] **3. 实现流协议。** `id: <sequence>`、`event: job.status`、`data: <JSON>`；每 1 秒读持久事件、每 15 秒发注释心跳。禁止长时间持有数据库事务或连接；每次查询后释放 session。断开只停止投递，不取消 Job。读取与投递前检查最新本地授权，远端授权过期时先复核再投递。
- [x] **4. 验证终态和错误控制。** 权限失效发无 id、无私有数据的 `event: access_lost` 后关闭；终态发完关闭，已消费终态立即关闭。增加 API 实例重建后仍能回放数据库事件的测试，断言旧游标不触发重复分析。重复第 2 步；检查 Content-Type、Cache-Control 和无缓冲要求。
- [x] **5. 提交。** `feat: stream durable job events with authorization checks`。

## Task 10: 前端会话与管理页面

**Files:** 创建 `frontend/{package.json,package-lock.json,.node-version,next.config.ts,tsconfig.json,eslint.config.mjs,postcss.config.mjs,playwright.config.ts}`、`frontend/src/app/{layout.tsx,globals.css,page.tsx}`、`app/login/page.tsx`、`app/(app)/layout.tsx`、`app/(app)/admin/users/page.tsx`、`app/(app)/admin/audit/page.tsx`、`src/lib/{api.ts,types.ts}`、`src/features/session/{provider.tsx,use-session.ts}`、`src/features/admin/{users.tsx,audit.tsx}`、`frontend/tests/{session,admin}.spec.ts`。后端创建 `audit/routes.py`、`tests/integration/test_audit_visibility.py`，修改 `app.py`。

**Interfaces:** 前端 `api<T>(path: string, init?: RequestInit): Promise<T>` 自动携带同源 cookie，写请求加入 CSRF，传递 AbortSignal；`useSession()` 返回后端 session DTO，登出取消在途查询、清除全部 QueryClient 缓存并关闭事件连接。管理页面依赖 task 4 的用户接口和 `GET /api/v1/admin/audit-logs`；审计分页使用共同 `Page<AuditView>`，AuditView 为 task 2 AuditEntry 加 id 与 created_at。

- [x] **1. 建立前端工具链并写失败测试。** 使用 Node 24 LTS 的选定补丁版；安装稳定兼容 Next.js/React、Tailwind 4、TanStack Query 5、TypeScript、ESLint 与 Playwright，提交 lockfile。定义 `typecheck`、`lint`、`build`、`test:e2e` npm 脚本。`session.spec.ts`: 未登录仅看到登录入口、登录后显示当前用户、登出后 `expect(private_cached_text).not.toBeVisible()`；`admin.spec.ts`: member 不显示管理入口且直达被拒绝、最后管理员操作错误有明确提示。
- [x] **2. 验证失败。** `npm --prefix frontend run test:e2e -- tests/session.spec.ts tests/admin.spec.ts`；首次执行先安装锁定版本匹配的 Playwright Chromium。API 使用浏览器 route fixtures；这些测试不宣称真实 GitHub 登录已验证。
- [x] **3. 实现页面与审计 API。** 本阶段使用清晰的功能界面：固定导航、表格、空态与状态消息，中文文案，支持键盘与窄屏；不做未确认的品牌视觉重设计。GET session 不缓存跨用户私有数据；服务端页面不做公共静态预渲染私有响应。用户/审计列表分页，审计仅管理员可读，失败 reason 使用安全枚举。
- [x] **4. 验证边界与构建。** 后端 `uv --directory backend run pytest tests/integration/test_audit_visibility.py -q`，断言普通用户 403、admin 仅见脱敏数据。重复第 2 步并运行 `npm --prefix frontend run typecheck`、`npm --prefix frontend run lint`、`npm --prefix frontend run build`，均应成功。开发代理仅将 `/api` 转到固定 loopback FastAPI，生产走同源 HTTPS 入口。
- [x] **5. 提交。** `feat: add authenticated frontend and team administration`。

## Task 11: 仓库与检查页面

**Files:** 创建 `frontend/src/app/(app)/repositories/page.tsx`、`app/(app)/jobs/[jobId]/page.tsx`、`src/features/repositories/{repository-list.tsx,connect-repository.tsx}`、`src/features/jobs/{job-view.tsx,use-job-events.ts}`、`frontend/tests/{repositories,job-stream}.spec.ts`；修改前端 `lib/types.ts`、导航。

**Interfaces:** `useJobEvents(jobId: string, initialSequence: number, terminal: boolean)` 管理 EventSource、最后序号、去重、关闭与重连；使用 task 9 的 job.status/access_lost 协议，task 7 JobView 是最终状态来源。创建检查时生成一次 UUID 幂等键，同一次请求重试复用该键，用户主动再次检查才生成新键。

- [x] **1. 写失败测试。** `repositories.spec.ts`: admin 可遍历候选并接入，member 只能看已授权仓库；空态分别显示未接入/无权限/暂不可用。`job-stream.spec.ts`: 事件 `[1,2,2,3]` 仅显示一次序号 2；断线后从最后序号恢复；`expect(connection_attempts_after_terminal).toBe(0)`；access_lost 后关闭流并移除私有结果；404 和暂时网络错误不能共用永久重试逻辑。
- [x] **2. 验证失败。** `npm --prefix frontend run test:e2e -- tests/repositories.spec.ts tests/job-stream.spec.ts`。
- [x] **3. 实现交互。** 仓库卡片/表格只渲染服务端返回的授权列表；接入不接受手填任意 API URL。检查页先取 JobView，终态直接显示结果、不建立 SSE；非终态通过 `after` 建立流。网络错误时主动关闭原 EventSource，先用 REST 分类权限和暂时故障，再按 1/2/4 秒有界重建并携带本地最后序号 `after`；服务端仍独立支持 Last-Event-ID 优先级。收到 terminal 后关闭流并刷新 REST。
- [x] **4. 验证重连和撤权。** EventSource 网络错误后通过详情接口区分 401/403/404 与临时故障，永久拒绝停止重连，临时故障有限退避并提供手动恢复。页面卸载关闭流、注销清空缓存、权限丢失清除结果；测试让旧详情请求在注销后迟到返回，`expect(private_result).not.toBeVisible()`。重复第 2 步和前端 typecheck/lint/build；360px 与桌面视口检查表格、按钮、错误消息可读。
- [x] **5. 提交。** `feat: add repository access checks and recoverable progress UI`。

## Task 12: 部署、CI 与阶段验收

**Files:** 创建 `infra/{compose.yaml,compose.dev.yaml,backend.Dockerfile,frontend.Dockerfile,Caddyfile}`、`.dockerignore`、`.github/workflows/ci.yml`、`docs/operations/{local-development,github-app-setup,deployment,backup-restore,foundation-acceptance}.md`、`backend/tests/integration/test_full_flow.py`、`backend/tests/support/{__init__,browser_server}.py`、`frontend/tests/full-flow.spec.ts`；修改 `.env.example`、`README.md`、Playwright config、测试数据库配置。实际版本解析结果记录在 `docs/operations/local-development.md`。

**Interfaces:** Compose 服务固定命名 `gateway`、`frontend`、`api`、`worker`、`postgres`、`migrate`；migrate 完成 `alembic upgrade head` 和一次性 bootstrap 后退出。backend image 同时供 API/Worker 使用，API 入口 `uv run --no-sync uvicorn devflow.app:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log --loop asyncio:SelectorEventLoop`，Worker 入口 `uv run --no-sync python -m devflow.worker`。`foundation-acceptance.md` 每项记录命令、环境、结果和限制；未执行写明未执行，不预填通过。

- [x] **1. 写跨模块失败测试与部署清单。** `test_full_flow`: 允许用户 → 登录 → 发现/注册 → 提交 → Worker → REST 终态与审计，断言 `status == 'succeeded'`、检查 repo ID 正确、事件有序。`full-flow.spec.ts` 使用真实 FastAPI/Worker/PostgreSQL，测试专用 browser_server 在测试目录注入 HTTPX GitHub transport，浏览器 OAuth 跳转由 Playwright 拦截回测试 callback；生产 app 不增加测试登录接口。断言真实前端和后端字段兼容，撤权后页面移除结果。
- [x] **2. 验证失败并构建部署配置。** `uv --directory backend run pytest tests/integration/test_full_flow.py -q`、`npm --prefix frontend run test:e2e -- tests/full-flow.spec.ts` 首次应因缺联调行为失败。添加多阶段镜像，后端 `uv sync --locked --no-dev`、前端 `npm ci` 与 standalone 输出；镜像 non-root、秘密只运行时挂载。API/Worker 等待 postgres healthy 与 migrate successful；gateway 将 `/api/*`、`/health/*` 原样转后端，其余到前端，SSE 关闭缓冲。[Compose 启动依赖](https://docs.docker.com/compose/how-tos/startup-order/)
- [x] **3. 运行完整离线与容器验证。** 执行下方命令组；CI 使用隔离 PostgreSQL 服务、合成凭据和 mock GitHub，不依赖真实秘密。配置任务故意遗漏秘密时应启动失败；数据库断开或 schema 不匹配时 readiness 为 503。检查镜像层/构建上下文/日志无植入秘密，扫描 `.gitignore`、`.dockerignore` 和 build args 防止把真实 `.env` 打包。
- [x] **4a. 本地恢复演练完成。** 2026-10-01 已完成独立 Compose 空库启动、API 重启、非空备份及独立恢复，核对计数、凭据解密和授权序列；Worker 强杀/租约回收由真实 PostgreSQL 子进程测试覆盖，Compose 另验证 Linux Worker 入口及正常停止。2026-10-13 修复后重建受影响镜像并更新本地运行证据；未变更 schema/crypto，恢复证据保持原日期，详见 [验收记录](../../operations/foundation-acceptance.md)。
- [ ] **4b. 真实外部验收未执行。** 缺少真实 GitHub App、域名与私有仓库；真实登录/检查/webhook 撤权、公网 TLS、远程 CI 和外部部署仍为 NOT EXECUTED，不据本地检查标记全部真实验收通过。
- [x] **5. 实现与最终独立复审完成。** 部署与联调实现及全阶段审查修复已提交；2026-10-13 独立限域复审核对 `18eba99..70dd30c`，I1–I4/M1–M4 全部解决，未发现新增回归。随后按用户选择本地快进合并至 `main`，主目录重新验证 405 项后端、37 项浏览器测试及静态检查/构建通过；已清理 foundation 工作树和已合并分支。本地产物保留，未推送或外部部署，详见验收记录。

### 全阶段验证命令

以下在实现完成、测试数据库与合成测试配置就绪后运行；测试标记 `live_github` 默认显式跳过，真实接入另行执行。

```powershell
uv --directory backend sync --locked
uv --directory backend run ruff check .
uv --directory backend run ruff format --check .
uv --directory backend run pytest -m "not live_github" -q
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run test:e2e
docker compose -p devflow-acceptance -f infra/compose.yaml -f infra/compose.dev.yaml config --quiet
docker compose -p devflow-acceptance -f infra/compose.yaml -f infra/compose.dev.yaml up -d --build --wait
```

通过标准：命令退出 0、无失败测试、migrate 正常退出、服务健康、无缺失锁文件变更；真实 GitHub 和恢复演练有独立证据。`compose config` 仅使用 `--quiet`，不把含秘密的完整展开配置输出到日志。验收清理只停止该独立项目，默认保留 volumes；删除数据必须先核实目标并获得相应授权。

## 规格覆盖与交付边界

| 规格章节/验收 | 实现任务 |
| --- | --- |
| 工程结构、UV/Node、配置与版本 | 1、10、12 |
| 身份、会话、成员与 CSRF | 2、3、4、10 |
| GitHub 接入、凭据、权限与分页 | 3、5、6 |
| 持久任务、幂等、恢复、事务 | 2、7、8 |
| 事件与 SSE、断线及撤权 | 6、9、11 |
| 数据表、API、审计和日志 | 1–9、10 的审计 API |
| 前端、迁移、部署与运维 | 10、11、12 |
| 真实 PostgreSQL、真实 GitHub、备份恢复验收 | 2、8、12 |

实施前只需要审阅本计划并确定执行方式，不需要提前提交模型密钥。真实接入前按运维文档配置 App ID、Client ID/secret、私钥、Webhook secret、允许安装 ID、首任管理员 GitHub ID、回调域名及测试私有仓库；秘密留在本机/部署环境，不发到聊天记录。

建议采用 **Subagent-driven**：任务逐个由实施者完成并独立审查，最后做全阶段审查。此阶段跨权限、凭据、租约和 SSE 边界，分任务审查更容易发现遗漏。也可选择 **Native**：由主代理在本会话顺序实现，最后统一独立审查；上下文开销较低，但中途独立审查较少。用户选择后按该方式执行，不重复询问已确认的架构和规格。

## 计划自审记录

2026-09-30，编写者完成规格覆盖、步骤可执行性、接口类型、五项 Review Focus 和文档比例自审。已修正公共分页能力的前置依赖、迁移顺序、缺少定义的 DTO，以及前端联调所需的客户端注入接口；明确了 GitHub 临时故障与凭据失效的区别。

文档结构检查通过：12 项任务、60 个未执行步骤；每项含文件、接口、测试及验证命令；内部链接、UTF-8、代码围栏和占位扫描通过。没有实现产品代码、解析项目依赖或运行应用测试。这些检查仅证明计划文档已准备好供审阅；实际完成状态以执行时的证据为准。
