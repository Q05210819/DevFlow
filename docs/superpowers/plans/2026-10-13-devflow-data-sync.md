# DevFlow 2A 数据同步与事实视图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让已授权团队用户读取可追溯的 GitHub Issue、PR、Actions 数据和健康摘要，并可靠恢复后台同步。

**Architecture:** 复用 PostgreSQL Job、授权版本及审计；系统任务和用户私有任务分开授权。后台按有界 HTTP 单元采集规范化事实，以短事务同时提交事实、游标、后续任务。前端复用会话和 React Query。

**Tech Stack:** Python 3.12、UV、FastAPI、SQLAlchemy/PostgreSQL、HTTPX、Next.js/React/TypeScript、pytest、Playwright、Compose；不新增消息总线或模型依赖。

**Spec:** [第二阶段规格](../specs/2026-10-13-devflow-data-foundation-design.md)。后续：[2B 实施计划](2026-10-13-devflow-workspace.md)。用户 2026-10-13 指示开始实施，执行方式 A 已选定。

## Global Constraints

- “单团队多用户和私有仓库、UV 管理 Python 环境”；“首批不超过 10 个仓库，单仓库通常小于 1 GB”。
- “同一安装的后台事实同步由单一有效执行者串行请求”；“每仓库最多 1 个事实同步执行单元；默认全局最多 2 个”。
- “最多 5 次 GitHub HTTP 请求，包含页面、详情、版本复核和该单元的令牌签发”；单元请求配置1–5，10 秒 HTTP、单元15–120秒（默认120，至少5秒准备余量）、最多 3 次失败；整轮默认10,000请求或24小时，允许沿用既有有限校验范围调整整轮默认值。
- “所有当前 open 项，以及最近 90 天更新的 closed/merged 项”；CI 最近 30 天，人工回填每次最长 365 天；补漏 15 分钟、增量重叠 5 分钟、每日核对。
- “JSON 响应流设 16 MiB 上限”；远端分页 100，UI 默认 30、最多 100。
- 锁序 User（若有）→ Installation → Repository → SyncState → Job；HTTP 在事务外；所有事实/checkpoint/publication 同时复核授权、同步代次及完整 Lease。
- 安装令牌不授权用户读取，不落日志/数据库/URL/argv；原 probe Job REST/SSE 创建者隔离不变。
- 新事实 payload 只存白名单规范化字段，不存原始 HTTP/Webhook；无证据/部分数据不冒充完整或成功。
- CI 日志、第三方 CI、LLM/RAG 和 Workspace 不属于本计划的代码交付；Workspace 在 2B 完成前明确不可用。
- Windows Selector 保持；所有测试使用现有 55439 回环专用 PostgreSQL、唯一 schema；禁止操作其他项目容器、用户文件或全局 Git 配置。

## Review Focus

1. malformed `Link: next` 不能悄悄让回填变成完整：Task 1 明确错误，Task 4 保留水位。
2. 第五次请求恰好用在令牌签发或 PR 复核：Task 3/4 持久续接，不循环失败或漏目标。
3. Webhook 在同步提交期间到达：Task 3/5 证明 requested/processed 序号不丢更新。
4. 较早健康任务晚完成：Task 6 验证独立 generation 条件发布，不覆盖新结果。
5. 浏览器曾经读取成功后用户/仓库撤权：Task 7 必须先隐藏缓存内容，再清理请求和状态。

## 文件职责与公共约定

- `github/client.py` 保留 OAuth/metadata 请求；新增受限响应读取和安装令牌签发。`github/data.py` 只提供固定只读端点；`github/installations.py` 只缓存内存令牌。
- `sync/models.py` 保存仓库调度与分流 checkpoint；`sync/scope.py` 是后台授权；`sync/service.py` 是事务提交和预算；`sync/scheduler.py` 是需求合并/领取；`sync/collectors.py` 分发，`sync/issues.py`、`pulls.py`、`actions.py` 各管一种采集。
- `facts/models.py` 使用规范化 `Fact` 与 `FactCollection`，而非原始响应仓库：逻辑实体在 `facts/schemas.py` 的类型联合中区分。共同字段支持索引，子数据存严格 DTO 的 JSON；该选择保留规格的所有实体身份/版本关系而减少重复 CRUD。
- `health/models.py`/`service.py` 只保存和计算确定性摘要；`facts/routes.py`、`sync/routes.py`、`health/routes.py` 承载仓库授权资源。
- DTO 为 Pydantic `extra=forbid`；接口下面的参数均为关键字优先，UUID 指平台 ID，GitHub ID 为正整数，时间为带时区 UTC。
- 每任务先写关键失败测试并记录 RED，再实现、运行聚焦测试及受影响回归、Ruff，提交确切文件。最终 Task 8 跑完整套件；不每次编辑重复全量测试。
- UV 命令从 `backend/` 运行：`uv run --no-sync ...`；Node 使用仓库 `.cache/node/node-v24.21.0-win-x64`，前端用 `npm.cmd`；新工作区独立 `uv sync --locked` 和 `npm ci`。

### Task 1: 有界 GitHub 只读客户端与安装令牌

**Files:** Modify `backend/src/devflow/github/client.py`, `github/schemas.py`, `errors.py`; Create `github/data.py`, `github/installations.py`, `backend/tests/test_github_data.py`, `test_installation_tokens.py`.

**Interfaces:** `GitHubClient.create_installation_token(installation_id:int, repository_id:int, permissions:dict[str,str]) -> InstallationToken`; DTO 字段 `token:SecretStr, expires_at:datetime, permissions:dict[str,str]`。`InstallationTokens(client).get(installation_id, repository_id, permissions, *, auth_version:tuple[int,int], before_request:Callable[[],Awaitable[None]]) -> SecretStr`，缓存 miss/刷新在 HTTP 前调用预算回调。

`GitHubDataClient(client).read(token:SecretStr, full_name:str, endpoint:DataEndpoint, *, target:int|None=None, attempt:int|None=None, page:int=1, filters:dict|None=None) -> DataResponse`。`DataResponse` 为 `items:list[dict] | None, item:dict | None, next_page:int|None, total_count:int|None`。有限 endpoint 为 issues/issue/issue_comments/pulls/pull/pull_files/reviews/review_comments/runs/run/attempt/jobs/repository；repository 用于独立验证父仓库，ref 解析在 2B 扩展，不开放任意 URL。

- [x] **RED:** 添加 `test_token_is_repository_and_permission_scoped`，断言 token POST body 恰为目标 ID 与 read 权限、201 成功且 SecretStr repr 不泄漏；`test_stream_cap_applies_without_content_length` 断言超过 16 MiB 抛安全 `github_response_too_large`；`test_malformed_next_link_is_not_completion` 断言 next host/path/page 非法抛 `github_unavailable`。缓存测试断言 epoch/permissions 改变重新签发，提前 60 秒刷新，预算回调仅在真实 HTTP 前调用；失效响应不被缓存。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_github_data.py tests/test_installation_tokens.py -q`，因新方法缺失失败。
- [x] **Implement:** 抽取现有 App JWT 构建复用；为 HTTP 增加受限 streaming、JSON body 和显式允许状态，不改变 OAuth 错误语义。数据端点从服务端固定映射拼接，验证 full_name 两段及数字目标，白名单 query；不跟随 redirect，保留 rate-limit deadline。安装访问失败不能错误地要求用户重新登录；403 权限与限流分开，404 不推断删除。Token 缓存按安装/仓库/权限/epoch 隔离并限制条目数为 128。不在本任务安装新依赖。
- [x] **GREEN:** 上述测试加 `tests/test_github_client.py tests/test_secret_storage.py tests/test_app_github.py`，全部通过；Ruff check/format。
- [x] **Commit:** `feat: add bounded GitHub data and installation token clients`。

### Task 2: 系统任务、事实模型与迁移

**Files:** Create `sync/__init__.py`, `sync/models.py`, `sync/schemas.py`, `facts/__init__.py`, `facts/models.py`, `facts/schemas.py`, `health/__init__.py`, `health/models.py`, migration `0005_data_foundation.py`; Modify `jobs/models.py`, `jobs/schemas.py`, `jobs/queue.py`, `migrations/env.py`, `audit/service.py`; Test `tests/integration/test_sync_models.py`, existing migration/private-job tests.

**Interfaces:** Job 新 kind `repository.sync`、`repository.health`，system=`requested_by is None`，旧 probe 必须有人类 creator；`SyncResult(repository_id,generation,continued:bool)` 和 `HealthResult(snapshot_id,generation)` 只供内部队列使用，用户 `JobView` 仍为 probe。

`RepositorySync`：repository PK、generation、requested/processed sequence、active_job_id、status、round_started_at、request_count、next_due_at、last_reconciled_at、health_generation、published_health_generation、current_health_id。`SyncCursor`：repo+stream 唯一，stream issues/pulls/actions；checkpoint JSON、covered_from/until、completed_watermark、status/error、last_success_at。进度用 `SyncCheckpoint` 严格 DTO，持久保存 stream、mode、page、window、pending_targets、target/stage、child_page、captured_sha、recaptures、captured_sequence。

`Fact`：UUID、repo、kind、source_id、version_key（无版本用空串）、number/parent_source_id、state、source_updated_at/observed_at、source_url、unavailable、payload，唯一 repo+kind+source_id+version_key。kind issue/pull/comment/review/review_comment/pr_file/ci_run/ci_job；payload 为对应 DTO 白名单。PR 文件 version_key 包含 base/head，CI run 包含 attempt；comment 的种类防 ID 混用。`FactCollection`：repo+parent_kind/ID+collection+version 唯一，complete/reason/observed_at。`HealthSnapshot`：repo+generation、rule_version、generated_at、manifest/result JSON。未来代码不得直接将任意上游 dict 写入 Fact。

- [x] **RED:** `test_upgrade_preserves_private_probe_and_system_uniqueness` 创建旧 probe、升级、断言旧结果/事件可读，重复系统 key 触发唯一约束、不同仓库可用同 key、system probe/有 creator 的 sync 被约束拒绝。`test_versions_preserve_old_ci_attempt_and_pr_files` 同一源 ID 两版本共存。`test_system_denial_reaches_terminal_state` NULL creator 不跳过失败事务。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_sync_models.py -q`。
- [x] **Implement:** 新迁移从 0004 演进，旧 unique 保留并新增 system partial unique index，status/attempt 不放宽。Queue.complete 按 Job kind 验证结果，失败/恢复更新安全审计；不把内部类型放进旧 API。用 DTO 校验源 SHA、ID、时间、有限枚举/可空字段，允许 GitHub 未来未知状态以明确 unknown 表示。定义后台相关安全 audit/error allowlist。
- [x] **GREEN:** 聚焦测试、`test_migrations.py test_queue_leases.py test_job_visibility.py test_sse.py test_worker_denial_fencing.py`，Alembic `command.check` 无模型漂移，Ruff。
- [x] **Commit:** `feat: persist versioned facts and system sync jobs`。

### Task 3: 调度、授权隔离和有界 checkpoint 提交

**Files:** Create `sync/scope.py`, `sync/service.py`, `sync/scheduler.py`, `sync/handlers.py`, `tests/support/sync_env.py`, `tests/integration/test_sync_engine.py`; Modify `jobs/queue.py`, `worker.py`, `config.py`, `errors.py`, `tests/test_worker_execution.py`.

**Interfaces:** `BackgroundScope(repository_id, github_repository_id, installation_id, installation_version, repository_version, full_name)`；`SyncService(factory,settings).snapshot(lease:Lease)->SyncWork`、`reserve_request(lease,work)->None`、`commit_page(lease,work,*,facts:list[FactInput],collections:list[CollectionInput],checkpoint:SyncCheckpoint,complete:bool)->bool`、`continue_job(lease,work)->bool`。每接口自行短事务，返回 false 表示失去发布权，不把 revoked 重新 promote。`SyncWork` 持有 scope、generation、captured_sequence、cursor。

`request_sync(db:AsyncSession, repository_id:UUID, *, reason:str,target:SyncTarget|None=None,backfill:BackfillRange|None=None)->RepositorySync` 属调用者事务。`SyncScheduler(factory,settings).tick()->int` 持久安排到期仓库。Worker handler 接收完整 Lease；原 probe 适配回旧函数，不扩大权限。`sync_env` 基于 repo_env，提供真实注册仓库、fake data HTTP、Queue/SyncService/Scheduler 和请求计数。

- [x] **RED:** `test_event_during_pass_survives_completion` 断言较新 requested_seq 未被清掉且后续任务原子存在；`test_expired_lease_cannot_write_facts_or_cursor` 比较数据库无变化；`test_installation_serialization_across_two_queue_instances` 同安装最多一有效 sync、其他安装可并发且全局不超过 2。`test_fifth_request_continues_without_retry` 断言 5 调用后成功续接、页内目标保留；`test_round_budget_survives_restart` 第 10,001 请求和 24 小时之后无 HTTP。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_sync_engine.py -q`。
- [x] **Implement:** request sequence/代次及后续 job 原子处理；global sync admission 用 PostgreSQL advisory transaction lock，按安装/仓库/SyncState/Job 顺序锁，原 probe claim 不参与新锁回路。claim 等待不占租约。request pre-reservation 先复核权属并计数；每单元默认 5 次和 120 秒，剩余不足一个 10 秒 HTTP 时正常续接。单元失败仍最多 3 次，最终失败更新 sync 状态和安全错误。每日/15 分钟调度、公平按 due 排序、断点恢复；重新回填不重置另一在途任务。用 Pydantic 配置校验所有界限，不把 query 参数当预算。
- [x] **GREEN:** 聚焦测试、Worker/Queue/Job transaction/recovery/denial 既有回归；增加请求或 checkpoint 提交失败整体回滚断言；Ruff。
- [x] **Commit:** `feat: schedule lease-fenced resumable repository sync`。

### Task 4: Issue、PR、Actions 采集与覆盖

**Files:** Create `sync/collectors.py`, `sync/issues.py`, `sync/pulls.py`, `sync/actions.py`, `facts/normalize.py`, `tests/test_fact_normalization.py`, `tests/integration/test_fact_sync.py`; Modify `sync/handlers.py`, `github/data.py`, sync fixture。

**Interfaces:** `collect_step(work:SyncWork, client:GitHubDataClient, token:SecretStr)->CollectedPage` 按 stream 分发；HTTP 调用前由 handler 的 budget wrapper 统一 reserve，collector 不拥有队列事务。`CollectedPage(facts,collections,checkpoint,complete)`；`normalize_fact(kind:str, payload:dict, context:FactContext)->FactInput`，只输出白名单字段；上下文提供 repo、parent、version、observed_at，不能由 payload 覆盖。

- [x] **RED:** fixtures 含旧 open、新 closed、PR issue entry、分页评论删除、force-push、3000 files、旧 review commit、CI attempt 1/2、密集 1000 条窗口、旧 run 重跑。断言 `test_pr_issue_id_is_not_pr_id`、`test_force_push_never_publishes_mixed_file_version`、`test_attempt_jobs_do_not_overwrite_old_attempt`、`test_incomplete_traversal_never_removes_children`、`test_dense_ci_window_reports_unresolved_gap`、`test_window_failure_does_not_advance_watermark`。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_fact_normalization.py tests/integration/test_fact_sync.py -q`。
- [x] **Implement:** Issue all-open 和 updated-window 独立扫描；PR updated sort 截止，无虚构 since；页内目标/各子集合/PR前后校验全部 checkpoint。PR变化每轮最多3次，文件上限/缺 patch 显式记录。CI created 窗口递归二分到1秒，触顶保留缺口；既有未结束/近期和 target run 重新读取，attempt从1到已知最新逐个读取，不用 latest jobs 覆盖。只有完整枚举的同父同版本子集合才标记消失成员；404先独立验证父仓库/权限。按流处理permission_missing；304只在持有完整对应缓存时允许，否则普通重新抓取。规范化body、作者、labels/assignees、review位置、run/jobs/steps，未知值/超限内容附reason。
- [x] **GREEN:** 聚焦测试及 Task1–3 回归，验证重复页幂等、跨页重启、第五次请求/最后复核预算耗尽；Ruff。
- [x] **Commit:** `feat: synchronize versioned GitHub issues pulls and actions`。

### Task 5: Webhook、登记和同步控制 API

**Files:** Modify `github/webhooks.py`, `github/models.py`, `repositories/service.py`, `app.py`; Create migration `0006_content_webhooks.py`, `sync/routes.py`, `tests/integration/test_content_webhooks.py`, `test_sync_routes.py`。

**Interfaces:** `SyncTarget(kind,source_id,number,attempt)` 字段可空但按kind校验；Webhook parser 为有限类型，不保存正文。`GET /repositories/{id}/sync -> SyncView`；`POST .../sync/refresh`、`POST .../sync/backfills` 返回同资源202；backfill `{start:datetime,end:datetime,streams:list[Stream]}`。`SyncView` 含 requested/processed sequence、generation、status、streams、error_code、next_due_at，stream含coverage/last_success/remaining。

- [x] **RED:** `test_receipt_and_demand_rollback_together`、`test_duplicate_delivery_enqueues_once`、`test_content_for_unregistered_repo_does_not_register`、`test_removal_blocks_ongoing_content_publication`；控制API断言 CSRF/idempotency、管理员仍需repo权限、365天及UTC时间边界、同key不同payload冲突。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_content_webhooks.py tests/integration/test_sync_routes.py -q`。
- [x] **Implement:** 扩展事件 allowlist 和回执schema；先保留同步生命周期失效，再将content hint在同一事务标脏。push/create/delete更新版本提示，repository rename/transfer保守失效并要求重新验证绑定，不能沿旧名称请求其他仓库。登记事务合并初始同步需求。API复用actor、policy fresh、CSRF、CursorCodec/幂等和安全审计，worker tick连接真实采集handler。GET不做远端内容写入。
- [x] **GREEN:** 上述测试和现有完整 webhooks/repository routes/access tests，新增API抛错不破坏原登记/访问检查；Ruff。
- [x] **Commit:** `feat: trigger repository sync from webhooks and user requests`。

### Task 6: 授权事实 API 与确定性健康摘要

**Files:** Create `facts/service.py`, `facts/routes.py`, `health/schemas.py`, `health/service.py`, `health/routes.py`, `tests/integration/test_facts_routes.py`, `test_repository_health.py`; Modify `app.py`, `worker.py`, `sync/service.py`, `repositories/schemas.py`/`service.py`。

**Interfaces:** `FactService(factory,policy,codec).list(actor,repo,kind,*,cursor=None,limit=30,filters=None)->FactPage`；`detail(actor,repo,kind,number_or_id,*,attempt=None)->FactDetail`。`FactPage(items,next_cursor,coverage)`，每item为严格领域DTO；`FactDetail(item,collections,coverage)`。子集合分页用同resource加 `collection` 与cursor，不返回无限评论。`GET /repositories/{id}` 返回已授权RepositoryView+sync概要；仓库列表embed轻量sync状态以免N次轮询。

`HealthService(factory,policy,settings).request(actor,repo,key)->HealthView`、`freeze(lease)->FrozenHealth`、`publish(lease,frozen)->bool`、`latest(actor,repo)->HealthView`。`HealthView(snapshot:HealthSnapshotView|None,refresh_status,error_code)`；snapshot含ID/generation/health-v1/generated_at/thresholds/counts/coverage/manifest/workspace，2A workspace=`unavailable`。路由匹配规格§9。

- [x] **RED:** 事实列表越权/游标跨用户仓库filter重放/后续页失败，断言limit默认30、最大100。健康输入有不同SHA的成功/失败、旧attempt、unknown和不足30天覆盖；断言不把别的SHA成功算当前成功，缺失与0分开。`test_old_health_publish_cannot_replace_new`、`test_failed_refresh_keeps_last_snapshot_time`、`test_revocation_during_fact_read_blocks_response`。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_facts_routes.py tests/integration/test_repository_health.py -q`。
- [x] **Implement:** 读取前policy+读取后local epoch复核；目的绑定keyset游标，区分`fact_not_found`与仓库权限404。源URL仅https github.com目标仓库相关路径；DTO安全输出。health-v1在短一致事务读输入并分配独立generation，计算open/unassigned/draft/stale7days及7/30天可覆盖活动计数、Actions分类，保留输入版本清单。只有新generation推进current指针，失败保留旧snapshot。活动计数是已采集记录的创建/更新时间统计，不虚构完整历史事件日志，并在UI命名中明确。
- [x] **GREEN:** 聚焦测试、权限/分页/审计回归，健康并发+事务回滚；Ruff。
- [x] **Commit:** `feat: expose authorized facts and repository health snapshots`。

### Task 7: 仓库事实与健康界面

**Files:** Create routes `frontend/src/app/(app)/repositories/[repositoryId]/` 下 `layout.tsx`, `page.tsx`, `issues/page.tsx`, `issues/[number]/page.tsx`, `pulls/page.tsx`, `pulls/[number]/page.tsx`, `actions/page.tsx`, `actions/[runId]/attempts/[attempt]/page.tsx`; Create feature `types.ts`, `queries.ts`, `repository-detail.tsx`, `repository-overview.tsx`, `issues.tsx`, `pulls.tsx`, `actions.tsx`; Modify repository-list、session/provider、globals.css；Create `tests/facts-fixtures.ts`, `repository-facts.spec.ts`, `repository-health.spec.ts`。

**Interfaces:** 从后端 Task5/6 DTO逐字段映射TS类型。query key统一 `['repository-content',userId,repositoryId,resource,...identity]`，无keepPreviousData；`RepositoryScope`提供授权详情和query前缀，子路由受已确认身份控制。

- [x] **RED:** Playwright页面测试新入口/列表/详情；校验ID/PR SHA/CI attempt、部分覆盖、空数据与unknown、刷新失败旧时间、第二页失败重试、CSRF+同逻辑幂等键。`test_revoked_cached_repository_is_hidden` 和用户/仓库切换后late response不可渲染；普通fact404不可清掉整个repo。先读 frontend/AGENTS.md 与 installed Next动态路由/布局文档。
- [x] **Run RED:** `npm.cmd run test:e2e -- tests/repository-facts.spec.ts tests/repository-health.spec.ts`，新页面缺失失败。
- [x] **Implement:** 薄async路由await params，领域组件复用现有panel/error/pagination。正文作为转义纯文本保留换行，说明暂不排版Markdown；不引入解析器或执行HTML。概览显示sync状态和health事实、管理员UTC回填表单。轮询仅可见页面、2秒起、最多5分钟或3次传输失败后停止并可手动恢复，尊重retry time；离开repo、退出、身份变化取消并清理私有cache。完整SHA可换行，小屏可用，button/input关联label，错误通过可访问region展示。
- [x] **GREEN:** 聚焦E2E及现有session/repositories/final-regressions，typecheck/lint/build。
- [x] **Commit:** `feat: add repository facts sync and health screens`。

### Task 8: 2A 集成验收与部署指引

**Files:** Modify `tests/support/browser_server.py`, `tests/support/full_flow.py`, `frontend/tests/full-flow-setup.ts`（按需）；Create `backend/tests/integration/test_data_sync_flow.py`, `frontend/tests/data-flow.spec.ts`, `docs/operations/data-foundation-acceptance.md`, `data-foundation-decisions.md`; Update GitHub App/local-development/deployment docs、README、Compose环境配置。

**Interfaces:** 用现有隔离fixture接入mock GitHub + 真实PG/API/Worker + 浏览器；不是由前端mock证明后端。记录2A完成、2B剩余、真实接入NOT EXECUTED，不覆盖第一阶段验收证据。

- [x] **RED:** 集成场景登记→Webhooks→sync→Issue/PR/CI→health，期间重启Worker、限流、撤权，证明事实/水位/权限；浏览器读取真实API响应并展示缺口。
- [x] **Implement:** 完善最小测试种子与owned进程清理；更新App只读权限/事件列表、新权限获批步骤、回填/预算/恢复与迁移回退操作。Docker保持锁文件、无外部新服务。
- [x] **Verify:** `uv sync --locked`、`uv run --no-sync ruff check .`、`ruff format --check .`、`pytest -q`；`npm.cmd run typecheck`、`lint`、`build`、`test:e2e`；构建API/Worker/前端镜像，隔离Compose迁移/服务恢复冒烟。精确记录命令/计数/错误/未执行外部项。
- [ ] **Review/Commit:** task review后完成本计划whole-branch review及必要修复；提交 `test: verify data sync and document phase two operations`，再进入2B，不将未实现的代码工作区称为完成。

2026-10-19：本地完整后端670通过、最终前端64通过，锁文件/静态/构建及隔离容器0007迁移、API/Worker重启和本项目PG503→200恢复均完成。独立任务/whole-branch审查仍由控制器完成，未在此预先标记。实际失败与修复、容器退出码、保留的无归属测试schema及所有外部NOT EXECUTED见 [2A验收](../../operations/data-foundation-acceptance.md)；实际裁定及2B交接见 [决策记录](../../operations/data-foundation-decisions.md)。

2026-10-19 整分支修复：R1–R4合并处理，独立限定范围复审已通过。独立周期/每日需求可在活动任务期间合并，target/backfill不移动截止；新增0008_sync_repair.next_reconcile_at区分全量尝试截止与成功证据，partial/permission终结尝试也延后每日重试，失败/预算耗尽保留既有恢复规则。目标成功保留全流覆盖/错误/时间，目标新缺口仍显式公开，snapshot/queue只按执行checkpoint推进；公共DTO形状不变。请求硬上限5、最小时长15秒；补充可选1000项recapture上限及open→closed再访问测试。此修订取代相关旧实现描述，历史670/64及0007容器证据保留；本轮准确验证见验收新增记录。2B迁移现为0009_workspace，依赖0008_sync_repair。

最终后端699通过（391.22秒）、Ruff131文件通过、唯一head0008；验证基线400bf87后的冻结修复工作树，准确源文件manifest见验收记录。此前git暂存及一次重试遇自动审批模型容量故障、命令未执行；不是安全否决，未绕过检查。随后独立复审核对16文件冻结校验与测试日志，R1–R4及M1全部关闭、无新增问题，2A代码审查门槛已通过。

代码审查及修复验收已通过；复审后的提交请求仍因自动审批服务容量不足未执行，因此此复合勾选项保留未完成。代码审查门槛已满足，2B可基于控制器保存的已审查快照继续可用开发；Git提交与最终本地合并待服务恢复，不绕过审批。
