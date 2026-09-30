# DevFlow AI 第一阶段平台基础设计

日期：2026-09-30。状态：用户已确认书面规格，进入实施计划编写。上位设计：[平台架构](2026-09-30-devflow-platform-design.md)。本文件把已确认的第一阶段范围细化为可实施、可验收的边界；数值为已接受的工程默认值。

## 1. 第一阶段目标

交付一条真实、只读的完整链路：允许的团队成员登录 → 发现本人有权访问的已授权仓库 → 管理员接入仓库 → 用户提交仓库访问检查 → Worker 执行 → 页面展示持久事件与结果 → 重启恢复与权限撤销得到正确处理。

检查结果包含 GitHub 仓库 ID、名称、是否私有、默认分支和检查时间，不宣称完成健康分析。第一阶段不调用模型，不克隆代码，不同步 Issue/PR/CI，不创建向量索引，不实现通用 DAG 或 GitHub 写操作。完整技术栈仍保留在平台路线中。

## 2. 工程结构与环境

```text
DevFlow/
  DevFlow.md
  docs/superpowers/specs/
  docs/superpowers/plans/
  backend/
    pyproject.toml
    uv.lock
    .python-version
    src/devflow/
      app.py
      config.py
      db.py
      auth/
      github/
      repositories/
      jobs/
      audit/
      worker.py
    migrations/
    tests/
  frontend/
    package.json
    package-lock.json
    src/app/
    src/features/
    tests/
  infra/
    compose.yaml
    compose.dev.yaml
    backend.Dockerfile
    frontend.Dockerfile
    Caddyfile
  .env.example
  .gitignore
  README.md
```

这是实施时创建的结构，不要求现在生成空目录。后端为一个 Python 3.12 包，UV 是唯一 Python 依赖管理入口；精确补丁版本在依赖解析通过后写入 `.python-version`。Node.js 使用满足 Next.js 要求的受支持 LTS 版本，实际选定版本写入版本文件与镜像配置。

一期 Python 依赖覆盖 FastAPI、Uvicorn、Pydantic Settings、SQLAlchemy、Alembic、PostgreSQL 异步驱动、HTTPX、GitHub App JWT 签名及凭据加密；开发组包含 pytest、pytest-asyncio 与 Ruff。使用成熟密码学库，不自行实现加密算法。前端使用 Next.js、React、TypeScript、Tailwind、TanStack Query，关键用户流程使用 Playwright 验证。

执行计划必须把确切路径、函数契约、依赖版本选择和验收命令落到任务中。版本以真实安装解析和锁文件为准，不把未经解析的版本组合写成兼容性结论。安装与构建遵守 [UV 官方项目管理](https://docs.astral.sh/uv/guides/projects/) 和 [容器建议](https://docs.astral.sh/uv/guides/integration/docker/)。

## 3. 身份与会话

- 仅接入 github.com 的 GitHub App 用户授权流程，使用 OAuth `state` 与 PKCE S256。后端交换凭据，浏览器不持有 GitHub token。[GitHub 授权流程](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- `state` 与发起登录的浏览器绑定，10 分钟过期、原子消费一次；错误或重复回调不能创建会话。登录后仅跳转到服务器允许的站内路径。
- 用户以不可变 GitHub ID 为键。由部署配置中的 `BOOTSTRAP_ADMIN_GITHUB_ID` 引导首任管理员，不采用“第一个登录者成为管理员”。引导只在首次数据库配置时执行，不在每次启动时覆盖成员管理结果。
- 管理员以 GitHub ID 添加允许成员；成员首次成功登录后补全展示信息。角色为 `admin`、`member`。停用用户立即使其会话失效；不能停用或降级最后一个有效管理员。
- 会话采用 256 位随机不透明 token，数据库仅存哈希，固定有效期 8 小时；注销使服务器会话失效。生产 Cookie 使用 `HttpOnly`、`Secure`、`SameSite=Lax`、`Path=/`、无 Domain 的 `__Host-devflow_session`。
- 本地 HTTP 开发仅允许 loopback 地址，并使用独立 cookie 名；生产配置缺 HTTPS 或必要秘密时启动失败。
- 修改状态的浏览器请求校验同源 Origin 和绑定会话的 CSRF token。OAuth 回调由 state/PKCE 保护；GitHub Webhook 由签名保护，不错误地要求浏览器 CSRF token。

## 4. GitHub 接入与权限规则

部署配置限定可使用的 `GITHUB_ALLOWED_INSTALLATION_IDS`。一期不提供任意团队自助注册或任意 GitHub 主机配置。管理员在 GitHub 完成 App 安装后，平台通过服务器调用核验安装关系；URL 参数本身不是授权证据。

一期只需仓库元数据读取权限和授权生命周期事件。第二阶段再按实际接口补充 Contents、Issues、Pull requests、Actions、Checks 等读取权限；写权限在相关功能交付时单独申请。

仓库访问条件为：有效平台成员 AND 本平台已启用仓库 AND 允许的 App 安装覆盖该仓库 AND 用户访问令牌可以访问该仓库。平台管理员同样遵守条件。GitHub 用户令牌只拥有用户与 App 的权限交集，可用于检查安装及其可访问仓库。[GitHub 权限语义](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)

接入前的候选仓库发现不要求“本平台已启用”，但仍要求有效成员、允许的安装和本人可访问，只返回接入选择所需元数据。注册成功后的所有业务接口才同时要求本平台已启用仓库，避免把接入流程与已接入资源授权混淆。

权限正向缓存最长 60 秒，键包含用户 ID、仓库 ID 和本地授权版本。成员停用、仓库停用和已处理的撤权事件立即使相应缓存失效。GitHub 返回 401/403/404 时不保留成功权限结论；过期缓存不能在网络失败时继续授权。因限流或网络问题无法验证返回可重试的服务不可用状态，不把它伪装成无权或成功。

仓库列表、创建任务、读取任务、读取结果和 SSE 都校验权限。提交检查和 Worker 开始检查时绕过缓存重新验证。运行记录一期仅创建者可见；管理员只能看到审计中的脱敏调度元数据，不能绕过仓库权限查看结果。

用户 token/refresh token 加密保存在数据库；加密主密钥及 App 私钥从部署秘密挂载，不入库、不进入镜像或日志。安装令牌按过期时间短期缓存。用户令牌刷新按用户串行化，刷新令牌失效时要求重新授权，绝不改用安装令牌替用户访问。限流、网络超时与服务配置错误按依赖暂不可用或配置故障处理，不误判为用户撤权。

生命周期 Webhook 校验原始请求体 HMAC SHA-256，以 delivery ID 去重，持久记录后处理；接收 `installation`、`installation_repositories`、`github_app_authorization` 的相关变更。乱序或不确定的安装状态通过 GitHub 重新核验后再启用，不能因旧事件恢复已撤销权限。无效签名拒绝；重复有效事件不重复改变状态。

一期撤销类事件在请求内同一事务完成去重记录、凭据/安装/仓库失效和审计，然后返回成功；数据库不可用返回 503，不提前确认。新增或恢复类事件仅标记需要重新核验，后续仓库发现或注册时在线核验后才启用，不在 Webhook 内盲目提升权限。无效签名只记录脱敏拒绝审计，不写入有效 delivery 去重空间。

## 5. 持久任务与恢复

一期只有 `repository.access_probe` 作业。输入为平台仓库 UUID，由服务器解析 GitHub 仓库 ID 和安装 ID；不接受任意 URL、任意命令或用户指定工具名。Worker 以创建者身份验证并读取仓库元数据。

`Job` 包含 `id`、`kind`、`repository_id`、`requested_by`、`input`、`status`、`attempt`、`max_attempts`、`available_at`、`lease_owner`、`lease_generation`、`lease_expires_at`、`result`、`error_code`、`idempotency_key`、`input_hash` 和 UTC 时间字段。

状态为 `queued → running → succeeded/failed`；可重试失败或租约过期进入 `retry_wait`，到期后重新成为可领取任务。每次领取增加 `attempt` 与 `lease_generation`；最多 3 次尝试（含首次）。第一次与第二次失败的退避分别为 10 秒、30 秒；GitHub 限流时还须遵守服务端给出的下一可用时间。撤权、仓库删除和输入无效不自动重试。

Worker 使用 PostgreSQL 行锁和 `SKIP LOCKED` 领取任务；事务中修改状态，提交后再发起网络请求。该机制是本项目对 PostgreSQL 队列适用能力的选择，不承诺一般查询的一致快照。[PostgreSQL SELECT 文档](https://www.postgresql.org/docs/current/sql-select.html)

租约为 30 秒，心跳间隔 10 秒；单次检查尝试最多运行 120 秒。时间判断使用数据库时钟。心跳、完成、失败提交必须同时匹配 `lease_owner`、`lease_generation`、未过期租约及当前状态。丢失租约的 Worker 不能继续提交结果或事件。重新领取前原子确认旧租约已过期，最后一次尝试过期后明确失败。

默认 Worker 并发为 4，可配置。任务执行语义是至少一次，访问检查因只读可以安全重复。不能把这套重试策略直接套到后续 GitHub 写操作；写操作须使用独立的草稿执行和远端核对设计。

提交接口要求 `Idempotency-Key`。唯一范围为创建者、作业类型与 key：相同 key 和相同输入返回原作业，不重复入队；相同 key、不同输入返回 409。返回既有任务前仍检查当前权限。

任务状态、对应事件及该状态要求的审计记录在同一数据库事务提交。API 返回 202 前作业必须已持久化。页面断开、API 重启都不能使已提交作业消失。

## 6. 事件、SSE 与结果

`RunEvent` 使用 `(job_id, sequence)` 唯一键，每个作业内序号从 1 单调递增。事件在状态变更的同一事务内分配序号；序号分配锁定对应作业，防止并发写入冲突。事件内容使用白名单结构，不能包含凭据或原始 GitHub 响应。

SSE 路由先校验会话、作业创建者和仓库权限，再发送任何结果。支持 `Last-Event-ID` 和初次连接的 `after` 游标，二者同时存在时以前者为准。游标缺失从头读取；非整数、负数或超过当前最大序号返回 400。游标仅针对当前作业。

服务端每 1 秒检查新增事件，每 15 秒发送心跳；客户端按事件序号去重。权限最迟每 60 秒重新验证，成员停用或已处理撤权在下一次读取循环关闭连接。验证失败后不继续发送缓存结果；关闭前仅发送不含私有数据的认证/权限错误事件。

运行结束先送达剩余事件及终态事件，再关闭连接。客户端收到终态停止重连；REST 详情始终能返回持久终态，已消费终态游标的连接立即结束。任务事件一期不自动清理，避免引入无法回放的隐式保留窗口；上线时监测容量，保留策略在后续容量设计中确定。

浏览器不传 GitHub 凭据；SSE 使用同源平台会话。反向代理关闭 SSE 缓冲并设置可容纳心跳的连接超时。前端提供连接中、重连中、执行成功、失败与重新授权状态。

## 7. 数据表与 API 边界

所有业务主键为 UUID，GitHub 原始 ID 使用整数单独保存并建立必要唯一约束；数据库时间统一 UTC。跨记录归属由外键约束，不靠前端提交关联关系。

| 表 | 责任 |
| --- | --- |
| users | GitHub ID、角色、启用状态和展示信息；允许名单由此承载 |
| oauth_states / sessions | 一次性登录事务和服务端会话；秘密 token 仅存哈希，PKCE verifier 短期受保护保存 |
| github_credentials | 用户加密凭据、过期时间、刷新状态 |
| github_installations / repositories | 经核验的安装、平台启用仓库及授权版本 |
| webhook_deliveries | GitHub 事件去重键、处理状态、最少必要字段；不留任意原始秘密 |
| jobs / run_events | 队列、租约、结果与可重放事件 |
| audit_logs | 行为、主体、目标、结果、request/job ID、UTC 时间和脱敏原因 |

API 统一位于 `/api/v1`，健康检查除外。错误体为 `{"error":{"code":"...","message":"...","request_id":"..."}}`；预期业务错误不泄露堆栈。无会话返回 401；无权或不可见的仓库/任务返回 404；成员管理角色不足返回 403；平台依赖故障返回 503。

| 接口 | 合同 |
| --- | --- |
| `GET /health/live` | 仅证明进程存活，不连接 GitHub |
| `GET /health/ready` | 检查数据库连接和所需迁移版本；失败 503 |
| `GET /api/v1/auth/github/start`、`GET /api/v1/auth/github/callback` | 发起与完成登录，校验 state/PKCE/允许名单 |
| `GET /api/v1/session`、`POST /api/v1/auth/logout` | 当前用户与 CSRF token、注销会话 |
| `GET /api/v1/github/repositories` | 当前用户在允许安装下可接入的仓库，完整处理 GitHub 分页 |
| `POST /api/v1/repositories` | 管理员提交 `installation_id`、`github_repository_id`，服务器验证后幂等注册 |
| `GET /api/v1/repositories` | 仅列出平台启用且当前用户有权访问的仓库 |
| `POST /api/v1/repositories/{repository_id}/access-checks` | 重新授权校验后创建作业，返回 202、job ID 和状态 |
| `GET /api/v1/jobs/{job_id}`、`GET /api/v1/jobs/{job_id}/events` | 创建者读取作业或 SSE，均需仓库权限 |
| `GET /api/v1/admin/users`、`POST /api/v1/admin/users`、`PATCH /api/v1/admin/users/{user_id}` | 查看、加入允许名单、调整角色/启用状态，保留至少一位有效管理员 |
| `GET /api/v1/admin/audit-logs` | 管理员分页查询脱敏审计 |
| `POST /api/v1/github/webhooks` | 验签、去重、持久接收、处理授权生命周期变化 |

本地列表统一采用 `limit`（默认 20，最大 100）和不透明 `cursor` 分页，排序包含稳定唯一键。GitHub 仓库发现接口需要将上游分页映射到不透明游标，不能把只返回第一页当作完整仓库清单。

## 8. 审计与可观测性

记录登录成功/拒绝、注销、成员变更、仓库接入与失效、权限拒绝、Webhook 验证/处理结果、作业提交、重试及终态。敏感状态变更和审计同事务；失败请求在独立受控事务记录，不因业务回滚而丢失。

日志使用结构化字段 `request_id`、`job_id`、`actor_id`、`action`、`outcome`，禁止 token、Cookie、私钥、authorization code、PKCE verifier 和完整敏感响应。审计错误原因使用固定错误码。

没有身份的请求只记录必要的安全元数据，不能伪造 actor。一期不宣称审计日志可以抵抗数据库管理员篡改。运行状态、失败计数、队列等待时长和 Worker 心跳应可供运维读取。

## 9. 前端与部署

一期页面仅包括登录页、已接入仓库列表、管理员可用仓库选择、访问检查详情、成员管理及审计列表。空列表解释未安装、无权限或尚未接入；检查页面展示时间、状态和失败原因，不展示不存在的 AI 能力。

Compose 服务包括 HTTPS 入口、前端、API、Worker、PostgreSQL 和一次性迁移任务。API 与 Worker 使用同一后端镜像；迁移单独执行且成功后再启动依赖服务，不能每个副本自行竞争迁移。Milvus 在第三阶段加入，不作为一期健康检查前置条件。

生产镜像与运行用户遵循最小权限，依赖和镜像版本固定；生产启动验证秘密是否缺失。PostgreSQL 仅在内部网络开放，数据使用持久卷；开发覆盖文件可把端口绑定到 `127.0.0.1`。备份至少包含数据库及恢复凭据所需的独立密钥备份流程，两者访问权限分离。

发布文档区分迁移前备份、应用回退和数据恢复。破坏性迁移不能仅靠回退镜像恢复数据。GitHub App 配置、真实秘密及 HTTPS 域名为部署输入，不写入仓库。

## 10. 验收矩阵

| 领域 | 必须验证的行为 |
| --- | --- |
| 环境 | 新环境按 UV/npm 锁文件安装；后端检查、前端类型与构建检查通过；Compose 可从空库启动 |
| 登录 | 允许成员成功；陌生用户、错误/过期/重复 state 被拒绝；并发首次登录不会制造多位引导管理员；过期与注销会话失效 |
| CSRF | 跨源修改请求或缺失 CSRF 被拒绝；正常 OAuth 回调和签名 Webhook 不受错误拦截 |
| 权限 | 用户 A 无法列表、查看、创建或订阅只属于 B 的私有仓库；管理员不能绕过内容权限；同仓库其他用户不能读取 A 的作业 |
| 撤权 | 本地停用或已知撤权立即拒绝新读取；远端未通知撤权最迟在 60 秒授权窗口到期时阻断；提交和 Worker 开始检查均实时验证 |
| 分页 | 仓库和审计超过一页仍可完整遍历，无跨用户游标数据泄露 |
| 幂等 | 重复提交同 key 同输入只产生一份作业；不同输入返回 409；重复 Webhook 不重复处理 |
| 队列 | 两个 Worker 不能同时持有有效租约；强杀后过期任务恢复；旧 lease generation 无法写回；三次尝试后明确失败 |
| SSE | 断线和 API 重启后按游标有序回放；前端去重；非法游标失败；终态不无限重连；检查权限前不发送事件 |
| 事务 | 作业状态、事件和必需审计同成同败，不出现成功但没有终态事件的记录 |
| 秘密 | 日志、API 错误、事件、审计、构建产物不包含测试植入的 token/私钥/会话等哨兵值 |
| 运维 | readiness 可反映数据库/迁移故障；空库启动、迁移、重启和数据库备份恢复演练通过 |
| 真实接入 | 在已配置 App 与真实私有仓库上完成登录、发现、注册、检查和撤权冒烟测试 |

单元与契约测试使用可控 GitHub 响应及时间；数据库并发、锁、迁移和恢复必须在真实 PostgreSQL 上验证，不以 SQLite 替代。关键浏览器流程覆盖登录状态、权限失败、检查完成与断线恢复。

外部凭据尚未配置时，可以完成实现与离线/容器验证，但真实 GitHub 验收必须标记未执行；不得将 mock 成功报告为真实集成成功。第一阶段完成要求验收矩阵全部通过，未满足时分别报告代码验证与部署验证状态。

## 11. 进入实施计划的条件

用户已确认本文件与平台架构文件。已使用 `superpowers:writing-plans` 编写 [第一阶段实施计划](../plans/2026-09-30-devflow-foundation.md)，逐任务明确文件、接口、失败测试、实现及验证命令。计划只覆盖本阶段，其他阶段在对应设计完成后再细化；用户审阅计划并选择执行方式后进入实现。

本轮不初始化项目、安装依赖或部署服务。Git 初始化与首次文档提交纳入第一个实施任务，避免在书面设计审阅前生成工程骨架。
