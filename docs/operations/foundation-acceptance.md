# 第一阶段验收记录

2026-10-01，本地离线应用联调、Linux 容器运行及独立数据库恢复检查通过。**真实 GitHub App、私有仓库、公网 TLS 签发、远程 GitHub Actions 和外部部署均未执行**，因此不声明全部真实接入验收完成。工程取舍保留在 [决策记录](foundation-decisions.md)。

## 2026-10-13 最终审查修复与新验证

本节是新证据；后续各节保留 **2026-10-01 历史记录**。最终全分支审查的 I1–I4/M1–M4 已处理，实施提交为 `cfe00094a6c55abe38ae97420380817f9cb350e1`，父提交为 `18eba997cd81e35e529230533653ef587f42da24`。控制器最终限域复审仍待结论，不能把本节解释为真实外部验收通过。

本次修复启动校验输入脱敏、注销 User→Session 锁序、服务端请求 UUID 与 HTTP 审计关联、终态失败中文说明、失败分页恢复、终态 REST 尝试次数优先级、测试 schema 异常清理和真实生效的 TTL 配置。认证期限固定 600/28800 秒，权限正缓存为 0–60 秒（默认 60，0 禁止复用）；详见 [本地开发](local-development.md) 和决策 40–41。

主工作树 `D:/python/DevFlow/.worktrees/foundation`，UV 缓存 `.cache/uv`；复用已锁定依赖、Python 3.12.13、UV 0.11.25、Node 24.21.0 和 Chromium 1243。后端专用测试 PostgreSQL 为 loopback 55439/devflow_test，每项仅清理自己生成的 schema。锁文件无差异，没有依赖升级。

| 2026-10-13 实际命令 | 结果（退出 0） | 本工作树日志 |
| --- | --- | --- |
| `uv --directory backend run ruff check .` | All checks passed | `.cache/final-fix/ruff-check.log` |
| `uv --directory backend run ruff format --check .` | 90 files already formatted | `.cache/final-fix/ruff-format.log` |
| `uv --directory backend run pytest -m 'not live_github' -q --tb=short --basetemp ../.cache/final-fix/pytest-full -p no:cacheprovider` | **405 passed，153.11 秒** | `.cache/final-fix/backend-full.log` |
| `npm.cmd --prefix frontend run typecheck` | 通过 | `.cache/final-fix/frontend-typecheck.log` |
| `npm.cmd --prefix frontend run lint` | 通过 | `.cache/final-fix/frontend-lint.log` |
| `npm.cmd --prefix frontend run build` | 通过，7 个页面生成 | `.cache/final-fix/frontend-build.log` |
| `npm.cmd --prefix frontend run test:e2e` | **37 passed，27.5 秒** | `.cache/final-fix/frontend-test-e2e.log` |

新增回归先 RED 后 GREEN：设置/清理和服务回归证明原始问题；13 项浏览器检查全部先失败，修复后 13 passed（19.5 秒）。最终聚焦后端 16 passed（12.02 秒），包括真实 Uvicorn、Worker/bootstrap 启动失败、真实 PostgreSQL 注销/停用并发、事务回滚、HTTP/流响应审计作用域和非默认缓存 TTL。初次测试沙箱临时目录 ACL 阻断后改用获准本地测试执行；完整后端开始前 Ruff 捕获的一处换行已修正。这些失败与通过分别保存在 `.cache/final-fix/backend-red*.log`、`backend-green*.log`、`frontend-red.log`、`frontend-green.log`，不把环境失败计为产品 RED。

### 新镜像与本地运行

使用本工作树 `infra/compose.yaml` + `compose.dev.yaml`，绝对 `--env-file D:/python/DevFlow/.worktrees/frontend/.cache/task12b2/config.env`，项目仅 `devflow-acceptance`。确认 8080/55440 无监听后，`config --quiet`、`build api frontend`、`up -d --wait` 均退出 0；生产配置也单独 `config --quiet` 成功。构建时产品文件与上述实施提交一致，后续仅文档变更；新镜像 API 中 **42 个 Python 源文件 SHA256** 与本工作树完全一致，导出的 **79 个构建上下文文件** 也逐字节一致（`.cache/final-fix/context-hashes.json`）。

| 服务 | 实际 image ID | 本次状态 |
| --- | --- | --- |
| API / Worker / migrate | `sha256:f8a2a20e5112fbd1faf4576dee02b0c46628b725320f7210fb3f12f952ec9ef7` | 新建，三服务同一镜像，UID 10001 |
| frontend | `sha256:58fd59e2efd9c8c628c5ce02ed5eacb727227c4ee91261080fd8c158ad10903d` | 新建，user node |
| gateway | `sha256:4aab1a0b72d7d9187486f1faf19e425458296a77b40985d06b2679e1add90a11` | 复用 10 月 1 日未改动镜像 |

实测 gateway `/health/live`、`/health/ready`、`/` 和实际 JS 资源均 200；无会话 `/api/v1/session` 为 401。新镜像的 API 默认入口、Worker 和 bootstrap 在无效生产 HTTP origin 下均退出 **1**，捕获 stdout/stderr 中已知合成秘密匹配 **0**，只记录布尔结果；健康配置下 API 可用，Worker 正常停止退出 **0**，重启成功。migrate 在保留数据库上退出 0，没有重新播种或修改 schema。

新 backend/frontend 合并导出归档的 **31 个成员/层** 与 79 个上下文文件扫描 **8 个合成模式，0 匹配**；3 份新构建/运行日志扫描 **10 个模式，0 匹配**。日志和有限样本扫描不代表穷尽任意秘密。详细证据在 `.cache/final-fix/{image-build.log,context-build.log,image-scan.log,runtime-start.log,runtime-check.log,runtime-summary.json,runtime-services.log}`，探测脚本为同目录 `runtime.py`、`scan.py`。

最后只执行本项目 `stop`，六服务均 exited，Worker/migrate/Postgres/gateway 退出 0，API/frontend 停止后的记录为 143；保留 postgres-data、caddy-data、caddy-config 三卷。最终状态见 `.cache/final-fix/runtime-final-state.jsonl` 和 `runtime-stop.log`；未停止专用测试数据库或其他项目。没有 schema/加密变更，**备份恢复演练未重复**，下方非空恢复和旧镜像扫描保持 10 月 1 日历史归属。真实 GitHub App、私有仓库、公网 TLS、远程 CI 和外部部署继续 **NOT EXECUTED**。

## 2026-10-01 历史源码、环境与镜像

应用源码为已独立审查的 `b20dc951bf7da7f70317627207b5c7bad0249c07`，包含 Worker 审计租约围栏、SSE 和真实浏览器联调；网关另包含本次 Compose 构建中移除无用文件能力的修复。随后仅更新运维记录及 CI，未改动应用源码或锁文件。若后续变更产品源码，以下镜像证据不能自动覆盖新版本。

主机 Windows PowerShell，Docker Desktop Linux engine；Python 3.12.13、UV 0.11.25、Node 24.21.0、PostgreSQL 17.11、Caddy 2.11.4。完整依赖和固定 digest 见 [本地开发](local-development.md)。所有秘密均为本次生成的合成文件，位于被 Git/Docker 忽略的 `.cache/task12b2/secrets`。

| 镜像 tag | 实际 image ID | 实际运行 UID |
| --- | --- | --- |
| `devflow-backend:acceptance` | `sha256:4c8398e98f097ad204e812328b89b0e243877ecba776dffa4cc9866ceaa00266` | API/Worker 10001 |
| `devflow-frontend:acceptance` | `sha256:6da5cce9930cc6ecdac3a30fcc142ba399ac3e02ad3fcb982c9664c1d1640183` | 1000 |
| `devflow-gateway:acceptance` | `sha256:4aab1a0b72d7d9187486f1faf19e425458296a77b40985d06b2679e1add90a11` | 10001 |

PostgreSQL 实际 UID 999。验收使用此前无同名容器、卷或网络的 `devflow-acceptance`，只绑定 `127.0.0.1:8080/55440`；恢复项目 `devflow-restore-20261001-12b2` 不发布端口。已有测试库 55439 和其他 Docker 项目未停止、覆盖或清理。

## 应用检查

以下是同一应用源码的实际本地完整结果；UV 使用共享本地缓存，Windows 前端命令使用 `npm.cmd`。

| 命令 | 结果 |
| --- | --- |
| `uv --directory backend sync --locked`；`npm.cmd --prefix frontend ci` | 安装成功，锁文件未改动；后续无依赖变更，复用安装 |
| `uv --directory backend run ruff check .` | 通过 |
| `uv --directory backend run ruff format --check .` | 88 个文件已格式化 |
| `uv --directory backend run pytest -m 'not live_github' -q --tb=short` | 389 passed，137.31 秒，无跳过 |
| `npm.cmd --prefix frontend run typecheck`；`... run lint`；`... run build` | 全部退出 0 |
| `npm.cmd --prefix frontend run test:e2e` | 24 passed，26.5 秒 |

完整流程使用真实 PostgreSQL 隔离 schema、真实 FastAPI factory、普通 Worker 和真实浏览器。只有 GitHub HTTP transport 为合成实现；浏览器保留真实 state/PKCE/nonce/回调流程，通过拦截外部授权跳转完成离线登录。流程覆盖管理员引导、成员允许名单、发现/接入、提交 202、Worker 终态、持久 SSE 顺序、REST 与审计、撤权后原页面清除私密结果。原生 EventSource 的 `access_lost` 空 JSON 帧也已验证。

已复现并修复受保护 REST 数据库故障从 500 到受控 503 的分类，普通编程错误仍为 500。浏览器首次运行的跳转拦截和隐藏接入面板定位问题均为测试夹具修正；完整流程没有虚构最初的产品失败。现有 Node 颜色变量、ESLint 版本弃用和 unrs-resolver 生命周期提示已记录，不影响本次退出码。

## 容器运行与故障演练

实际命令公共参数如下，`config.env` 只有合成环境的非秘密身份配置及秘密目录路径：

```powershell
$acceptance = @('--env-file', '.cache/task12b2/config.env', '-p', 'devflow-acceptance', '-f', 'infra/compose.yaml', '-f', 'infra/compose.dev.yaml')
docker compose @acceptance config --quiet
docker compose @acceptance build api frontend gateway
docker compose @acceptance up -d --wait
docker compose @acceptance ps -a
curl.exe --fail http://localhost:8080/health/ready
```

| 实测检查 | 结果与边界 |
| --- | --- |
| 空库迁移与 bootstrap | 启动前 public 表数 0；migrate 退出 0，revision `0004_jobs_events`，首任管理员 1 人 |
| 网关实际流量 | `/health/live`、`/health/ready`、前端 `/` 和真实静态 JS 均 200；无会话 `/api/v1/session` 为 401；无效 OAuth state 为安全 400 |
| API 重启 | `docker compose @acceptance restart api` 后网关 readiness 200 |
| Linux Worker 入口和信号 | 命令为 `uv run --no-sync python -m devflow.worker`；`stop worker` 后实际状态 exited、退出码 0，随后重启成功 |
| 缺秘密启动 | `run --rm -T --no-deps -e APP_ENV=production -e PUBLIC_BASE_URL=https://devflow.invalid -e GITHUB_CLIENT_SECRET_FILE=/run/secrets/absent api` 退出 1，固定缺秘密校验命中 |
| 数据库中断 | 仅 `stop postgres` 本验收项目，网关 readiness 503；`up -d --wait postgres` 后 200 |
| schema 不匹配 | 仅本验收库临时将 alembic_version 改为 `0003_webhook_deliveries`，readiness 503；finally 恢复 `0004_jobs_events` 后 200 |
| 生产/开发 Compose | 两种最终配置 `config --quiet` 均退出 0；没有输出展开的秘密配置 |

首次实际六服务启动发现 Caddy `exec /usr/bin/caddy: operation not permitted`，虽然 `up --wait` 返回 0。`getcap` 确认上游带 `cap_net_bind_service=ep`；同一镜像正常能力下可执行，丢弃全部能力时复现失败。内部端口为 8080/8443，构建时 `setcap -r /usr/bin/caddy` 后在非 root、`cap_drop ALL`、`no-new-privileges` 下执行及实际 HTTP 均成功。CI 增加同等限制的 `caddy version` 运行检查，保留最小权限。

Worker **强杀及租约回收**证据来自 [真实 PostgreSQL 子进程恢复测试](../../backend/tests/integration/test_worker_recovery.py)，已包含在上述 389 项完整检查中：终止仅该测试拥有的 Worker PID，以明确过去的 SQL 租约时间推进恢复，替代进程执行第二次尝试；持久事件顺序为 queued/running/retry_wait/running/succeeded，旧 owner/generation 写回失败且审计不变。这是实际进程/数据库证据，**不是 Compose 内带真实 GitHub 作业的强杀验收**；本次 Compose 另验证 Linux 模块入口和正常停止信号。未增加生产 provider 绕过接口。

## 备份与隔离恢复

使用实际 ORM、`append_event` 和 `write_audit` 在事务中建立合成成员、私有仓库、终态作业及相关事件/审计，另存非空加密 token 和 OAuth state。该播种仅建立恢复样本，不冒充一次真实 GitHub 检查。备份前停止本项目 gateway/API/Worker，冻结计数。

```powershell
$restore = @('--env-file', '.cache/task12b2/config.env', '-p', 'devflow-restore-20261001-12b2', '-f', 'infra/compose.yaml')
docker compose @acceptance exec -T postgres pg_dump -U devflow -d devflow --format=custom --file=/tmp/devflow-backup.dump
docker compose @acceptance cp postgres:/tmp/devflow-backup.dump .cache/task12b2/devflow-backup.dump
docker compose @restore cp .cache/task12b2/devflow-backup.dump postgres:/tmp/devflow-backup.dump
docker compose @restore exec -T postgres pg_restore --exit-on-error --no-owner -U devflow -d devflow /tmp/devflow-backup.dump
```

恢复项目此前不存在，实际恢复前再次确认 public 表数 0；未使用 `--clean` 或覆盖已有库。二进制 dump 的 SHA256 为 `6d0983a8989aa341f7018c9a7fb216ec5319ab94361c4b557377f37627abbcf8`。

| 比较项 | 源库 | 恢复库 |
| --- | --- | --- |
| users / repositories / jobs | 2 / 1 / 1 | 2 / 1 / 1 |
| run_events / audit_logs / github_credentials / oauth_states | 3 / 5 / 1 / 1 | 3 / 5 / 1 / 1 |
| schema | 0004_jobs_events | 0004_jobs_events |
| 作业状态 / attempt / last_sequence | succeeded / 1 / 3 | succeeded / 1 / 3 |
| 事件序号与状态 | 1 queued、2 running、3 succeeded | 完全一致 |
| 原 Fernet key 解密成功字段数 | 2 | 2 |
| auth_change_order_seq.last_value / 最大已存授权顺序 | 3 / 3 | 3 / 3；nextval 为 4，大于 3 |

验证进程只打印计数，不打印 token；源库与恢复库的完整校验 JSON 一致（恢复库另有 `next_order=4`）。详细通用操作与顺序 SQL 见 [备份与恢复](backup-restore.md)。最后仅停止两个验收项目，保留 acceptance 的 postgres/caddy-data/caddy-config 三个卷和 restore 的 postgres 卷；未执行全局清理。

## 秘密、日志及未执行项目

实际 Docker 上下文通过 scratch `COPY . /context` 导出，检查 79 个文件；三个最终 `docker image save` 归档的 50 个成员/镜像层全部扫描七个合成秘密及唯一排除哨兵，匹配数 0。检查 8 份实际构建/运行日志共 11 个秘密模式，匹配数 0；29 条应用 HTTP 结构化记录只有允许字段，状态覆盖 200/400/401/503。此证据针对本次植入样本和运行，不代表任意未来秘密扫描器的完整性。

CI 已包含固定依赖安装、Ruff 检查及格式检查、完整后端与浏览器测试、镜像构建和限制能力后的网关执行检查；**远程 GitHub Actions 未执行**。公网 HTTPS/TLS、真实 GitHub OAuth、安装私有仓库和实际 webhook 撤权仍为 **NOT EXECUTED**。需要部署者在自己的环境配置 App ID、Client ID/secret、RSA 私钥、webhook secret、允许安装 ID、首任管理员数字 ID、匹配的回调 origin、可达 HTTPS 域名和专用私有仓库；秘密不应提交仓库或发送到聊天。下一步按 [GitHub App 设置](github-app-setup.md) 的人工步骤记录真实证据。
