# DevFlow

面向单团队的私有仓库事实工作台：GitHub 登录、成员允许列表、仓库发现与接入、只读 Issue/PR/Actions 后台同步、可追溯健康事实、访问检查、撤权和审计。2B 增加按固定 SHA 准备的只读代码 Workspace、目录/文件/字面搜索及来源行链接；只抓显式 ref 到服务自有裸对象库，不 checkout、不运行仓库代码、不修改 GitHub 内容、不调用模型。运行环境与外部验收仍有未执行门槛，见验收记录。

本地合成 GitHub 联调与真实 GitHub 验收分别记录。真实 App、私有仓库与外部域名尚未提供，真实接入和外部部署未执行；不能据离线测试认定整个平台全部验收完成。

## 文档

- [本地开发与固定依赖版本](docs/operations/local-development.md)
- [GitHub App、回调和秘密配置](docs/operations/github-app-setup.md)
- [Compose 部署与启动顺序](docs/operations/deployment.md)
- [备份与独立数据库恢复](docs/operations/backup-restore.md)
- [阶段验收记录与未执行项目](docs/operations/foundation-acceptance.md)
- [2A 数据同步验收与运维](docs/operations/data-foundation-acceptance.md)
- [2A 数据基础决策](docs/operations/data-foundation-decisions.md)
- [Workspace 存储、软预算与物理守卫恢复](docs/operations/workspace-storage.md)
- [知识导入与检索验收记录（含 3B 部署资源与未执行门槛）](docs/operations/knowledge-acceptance.md)

## 本地启动

需要 UV、Python 3.12.13、Node 24.21.0、PostgreSQL 17.11 和 Git >=2.43（包含 CA 信任链）。API/Worker 的 `WORKSPACE_CACHE_ROOT` 必须指向同一个服务自有绝对路径；生产 API 只读、Worker 可写。以下命令从仓库根目录执行；Windows 使用 `npm.cmd`，Linux 使用 `npm`。

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

## 3B 本地索引与模型运行时

知识索引使用真实 Milvus（standalone：etcd + MinIO + Milvus）。生产 Compose 已固定这组服务及卷，数据库只保存索引版本、成员与可用性状态；向量在 Milvus，二者必须一起备份。概览在普通未就绪状态（无目标、构建中、失败、旧目标）返回 200 并带 `index_status`/`index_reason`；只有数据库仍声明 active 版本 ready、而只读探测发现集合或向量缺失时，才返回 409 `index_not_ready`，原因包括 `collection_missing`、`vectors_missing`。模型运行时指纹与索引存储指纹的比较由 model-worker/查询路径负责，API 概览只校验存储中的指纹形状与向量可见性。任何情况下都要重建索引，不能把状态改成 ready 蒙混过去。

Milvus standalone 至少预留 **8 GiB** 内存，**16 GiB** 更稳妥；加载 segment 和索引时低于该值可能被 OOM kill。etcd/MinIO/Milvus 数据分别在 `milvus-etcd`、`milvus-minio`、`milvus-data` 卷，另需为 WAL、segment 增长和备份留出磁盘余量。不要 `down -v`、不要全局 `docker prune`；备份/恢复要同时覆盖 PostgreSQL 与 Milvus 三卷并保持一致时间点。

模型推理只在独立 `model-worker` 镜像中启用可选 `models` 依赖；API 和普通 Worker 不安装、不加载 torch。模型文件由部署者提供，不得硬编码生产宿主路径：

```powershell
# .env: absolute host dir + explicit device/dtype, never implicit.
# DEVFLOW_MODEL_DIR=/srv/devflow-models
# MODEL_EMBEDDING_PATH=/models/bge-m3
# MODEL_RERANKER_PATH=/models/bge-reranker-v2-m3
# MODEL_DEVICE=cuda   # or cpu, only as an explicit measured choice
# MODEL_DTYPE=float16 # or float32/bfloat16
docker compose --env-file .env -p devflow -f infra/compose.yaml build api frontend gateway model-worker
docker compose --env-file .env -p devflow -f infra/compose.yaml up -d
```

`DEVFLOW_MODEL_DIR` 以只读方式挂到 `/models`，UID 10001 必须可读。`model-worker` 固定 `WORKER_ROLE=model`、并发 1，并通过共享 `model-owner` 卷上的 OS 锁保证同一时刻只有一个进程加载模型；不要横向扩容该服务。启动自检有界（默认 1800 秒），停止会先排空在途推理。运行日志显式记录 `starting/loading`、`ready`、`not_ready/owner_locked` 及 `unhealthy` 原因（`model_path_missing`、`model_load_failed`、`out_of_memory`、`startup_timeout`、`self_check_failed`）；空 `MODEL_DEVICE`/`MODEL_DTYPE` 会在启动时拒绝，不会静默按 CPU 运行。

CPU 或与其他服务分时共享 GPU/内存的配置必须显式选择，并单独测量冷热延迟、峰值 RAM/VRAM 与并存服务；这类实验不能称为同机全栈通过。真实 BGE/CUDA 推理、Linux 镜像运行和 30 题检索质量仍为 **NOT EXECUTED**，后者属于 3C 验收。

## 测试

先确认专用测试端口 `127.0.0.1:55439` 的归属，再启动测试 PostgreSQL；不操作其他项目容器或业务数据库。

```powershell
docker compose -p devflow-test -f infra/compose.test.yaml up -d --wait postgres
uv --directory backend run ruff check .
uv --directory backend run ruff format --check .
uv --directory backend run pytest tests/ -m "not live_github" -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
Push-Location frontend
npx.cmd --no-install playwright install chromium
npm.cmd run test:e2e
Pop-Location
```

后端和真实浏览器联调只使用 `devflow_test` 数据库/用户及随机独立 schema；各夹具按自身清理证据删除或保留 schema，进程结束本身不代表清理完成。`TEST_DATABASE_URL` 仅允许上述专用 loopback 数据库，不能指定生产库。全量 Playwright 包含页面交互测试和 `full-flow.spec.ts`：后者启动真实 API、独立 Worker 任务与 PostgreSQL，仅替换 GitHub HTTP transport；OAuth state/cookie、会话、权限及作业持久化均使用生产实现。浏览器服务使用 31847/31848 两个本地端口，不需要真实 GitHub 秘密。

`data-flow.spec.ts` 同样读取真实 API，验证收集的 Issue/PR/CI、缺失补丁、健康事实及仓库撤权后的缓存清理。真实栈用例串行运行，避免两个自有 helper 争用 31848。事实 helper 的已验证正常停止、父进程 EOF 和设置失败路径会删除自身 schema；Workspace helper 在运行清理确认后才删除 schema，不确定时连同目录保留，成功路径也保留对象与守卫证据目录，详见[Workspace 验收与保留记录](docs/operations/data-foundation-acceptance.md#2b-task-6-运行时验收2026-10-22)。后端 `test_data_sync_flow.py` 还验证分页限流、正常 Worker 重启后续接与不完整轮次保留旧水位。该重启证据是普通 Worker 生命周期，不是强杀进程崩溃验收。

仅运行跨模块验收：

```powershell
uv --directory backend run --no-sync pytest tests/integration/test_full_flow.py tests/integration/test_data_sync_flow.py -q
npm.cmd --prefix frontend run test:e2e -- tests/full-flow.spec.ts tests/data-flow.spec.ts
```

停止测试数据库使用 `docker compose -p devflow-test -f infra/compose.test.yaml stop`，保留测试卷。UV 缓存受限时可使用仓库内 `.cache/uv`；浏览器缓存可通过 `PLAYWRIGHT_BROWSERS_PATH` 指定本机目录。

Workspace 合成传输联调使用 `uv --directory backend run --no-sync pytest tests/integration/test_workspace_flow.py -q -p no:cacheprovider`；可用浏览器环境中的真实 API 流程使用 `npm.cmd --prefix frontend run test:e2e -- tests/workspace-flow.spec.ts`。两者复用普通 Worker、PostgreSQL 和真实原生 Git 读取/守卫，仅替换 GitHub HTTP 与对象传输。它们不证明真实 Git fetch、私有 GitHub/fork 或 Linux 容器可用。当前 R29 下浏览器与生产 build 不重试；恢复环境后再执行。测试的自有对象与守卫证据保留在忽略的 `.superpowers/workspace-fixtures/<uuid>`，不应笼统删除未知历史目录。
