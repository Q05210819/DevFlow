# 知识导入、本地索引与检索引用（3A–3C）验收记录

更新日期：2026-11-05
范围：3A Task 1–5、3B Task 1–5 与 3C Task 1–5 的本地可执行证据。未执行项保持 NOT EXECUTED，不以受控 fixture 代替真实 Git、模型推理或云端验收。

## 环境与边界

- 后端：Python 3.12.13、UV 0.11.25、真实 PostgreSQL 17（测试库 `127.0.0.1:55439/devflow_test`）、pytest / Ruff。
- 数据库 fixture：每个测试创建独立 schema 并执行 Alembic 到实际 head；不连接用户库。
- 授权与 GitHub HTTP：真实本地授权/迁移/队列，GitHub 传输使用受控 mock；不代表真实 GitHub App 验收。
- Workspace 原始字节：测试使用 `MemoryCache` 受控对象缓存，不代表原生 Git 进程或 Linux 卷权限验收。
- Tokenizer：自动化测试使用确定性字符 tokenizer fixture；本地真实 `bge-m3` tokenizer 只在既有 Task 2 证据中单独记录，本记录不重新声明模型推理通过。
- 云模型：未调用；云费用开关保持关闭。
- 3A 完成时的唯一迁移 head：`0013_knowledge_cleanup`；3B 当前 head 见下方 3B 节。

## 本轮执行命令与结果

在 `backend/` 下执行（工作树 `D:/python/DevFlow/.worktrees/data-foundation`）：

| 命令 | 结果 |
| --- | --- |
| `uv run --no-sync pytest tests/integration/test_knowledge_routes.py -q` | 18 passed in 23.79s |
| `uv run --no-sync pytest tests/integration/test_knowledge_retention.py -q` | 9 passed |
| `uv run --no-sync pytest tests/integration/test_knowledge_ingestion_flow.py -q` | 2 passed |
| `uv run --no-sync pytest tests/integration/test_knowledge_routes.py tests/integration/test_knowledge_sources.py tests/integration/test_migrations.py -q` | 54 passed in 46.76s |
| `uv run --no-sync pytest tests/integration/test_knowledge_routes.py tests/integration/test_knowledge_retention.py tests/integration/test_knowledge_ingestion_flow.py tests/integration/test_migrations.py tests/test_config.py -q` | 51 passed in 59.96s（稳定聚焦集，含整体审阅回归） |
| `uv run --no-sync pytest tests/integration/test_knowledge_import_jobs.py tests/integration/test_knowledge_sources.py tests/integration/test_knowledge_source_reads.py -q` | 78 passed in 110.83s（稳定聚焦集，含整体审阅回归） |
| `uv run --no-sync pytest tests/integration/test_knowledge_routes.py tests/integration/test_knowledge_retention.py tests/integration/test_knowledge_ingestion_flow.py tests/integration/test_knowledge_source_reads.py tests/integration/test_knowledge_import_jobs.py tests/integration/test_knowledge_sources.py tests/integration/test_migrations.py tests/test_config.py -q` | 129 passed in 164.81s（3A 合并聚焦集） |
| `uv run --no-sync pytest tests/test_knowledge_chunking.py tests/test_config.py tests/integration -k "knowledge or heartbeat" -q` | 单次 188 passed, 1 failed, 613 deselected；失败项为既有 timing-sensitive 测试，见下方说明 |
| `uv run --no-sync pytest "tests/integration/test_workspace_reads.py::test_native_reader_cancel_joins_supervisor_and_acknowledges_physical_drain" -q` | 1 passed in 3.52s（隔离重跑） |
| `uv run --no-sync pytest tests/integration/test_migrations.py tests/integration/test_queue_leases.py tests/integration/test_worker_recovery.py tests/integration/test_worker_denial_fencing.py tests/integration/test_workspace_flow.py tests/integration/test_workspace_retention_history.py -q` | 65 passed in 112.53s |
| `npm.cmd --prefix frontend run test:e2e -- tests/knowledge-ingestion.spec.ts` | 9 passed in 12.6s（含分页重置、轮询、删除清除缓存、应用内切仓晚响应） |
| `npm.cmd --prefix frontend run typecheck` | PASS |
| `npm.cmd --prefix frontend run lint` | PASS |
| `CODEBUDDY_SAFE_DELETE_ENABLED=0 npm.cmd --prefix frontend run build` | PASS |
| `uv run --no-sync ruff check .` | All checks passed |
| `uv run --no-sync ruff format --check .` | 178 files already formatted |
| `uv run --no-sync alembic heads` | `0013_knowledge_cleanup (head)` |

完整过滤套件在多次整包运行中出现过不同的既有 timing-sensitive 失败：2B 原生 Workspace 取消/排空、Worker 心跳启动、并发容量准入。每次失败项单独重跑均通过（分别为 3.52s、2.22s、2.41s），且与 Task 4/5 改动文件无直接关系；聚焦测试集稳定为 48 passed 与 77 passed。该环境级不稳定不能被表述为已解决，也不能据此声明 2B 或整套后端门槛已关闭。

## 覆盖的行为

- 上传暂存、准入、替换、删除和只读预览路由；14 项路由测试通过。
- 删除立即拒绝正在/随后预览；retention 与 pin 只延迟物理回收，不延长读取权限。
- 未过期 retention 保护正文，活动语料/accepted upload 不可回收；过期且无读者时正文与 chunk 可回收，revision 身份、哈希、`body_bytes` 和 provenance 保留，内部状态为 `expired` 或既有 `deleted`。
- 单批 ≤100 候选；受保护首页不会阻塞后续页，游标回绕后继续推进。
- 128 MiB 容量满返回 `storage_exhausted`。
- producer 每仓库最多一个非终态 cleanup Job；成功批次 60 秒冷却；终态失败保留原 Job 并执行 1 小时 PG 冷却；撤权仓库仍可清理。
- 上传 → 替换 → 固定 Workspace SHA 导入（含排除的凭据文件，状态 `ready_partial`）→ 旧上传来源预览仍可读且 `is_current=false` → 撤权后预览拒绝；新语料不包含旧上传正文。
- 迁移保留既有 Job 行；存在 `knowledge.cleanup` Job 时拒绝降级，删除后可降级到 `0012_knowledge_stages`。
- 浏览器流程修复：知识页在空 Workspace ID 下不再请求 `/workspaces/` 与 `/knowledge/paths`；此前这类 404 会触发仓库级 `access.revoke()`，造成切换仓库后误显示“仓库不可访问”。E2E 切换仓库用例现已稳定通过。
- 独立审查修复（Task 5）：producer 改为有界精确路径扫描，不再因粗粒度 Workspace 谓词永久跳过被新范围丢弃的旧路径；`expired` 无正文修订可在同一身份下重新导入/暂存并恢复正文与容量记账（`deleted` 仍不复活）；心跳配置强制大于 5 秒维护事务窗口；成功冷却从任务完成时刻起算。复审确认四项修复均有效，未发现新缺陷。
- 独立审查修复（Task 4）：写入成功后重置文档分页并恢复第一页，避免代次变化后的 `invalid_cursor` 死循环；知识语料状态进入自动轮询；空路径选择在前端禁用提交、后端拒绝，且预览与提交使用相同的显式空文档集合；删除/写入后失效文档详情缓存，避免旧预览残留；`0012` 降级在存在 keyed stage 时拒绝丢弃重放信息；上传路由在缓冲前校验文件名/UTF-8；新增应用内切仓 + 延迟旧响应的浏览器测试。
- 整体审阅修复（3A）：文档修订历史改为签名 keyset 分页（默认 30、上限 100，`revisions_next_cursor`），显式 `revision_id` 不受当前页限制；已回收的已发布修订保留身份并以 `availability`/`source_expired` 明示正文缺失，不再从历史消失；无声明大小的 Workspace 条目以 `unknown_size` 跳过且不占用字节预算；概览与文档详情仅暴露当前语料依赖集内的来源，未授权 fork 来源返回 404；新发布清单之外的旧修订在同一 fenced 事务内清除 `searchable`；未发布修订的文档不进入概览。

## 未执行项（NOT EXECUTED）

- 真实 GitHub App、私有仓库安装、公网 Webhook/HTTPS。
- 原生 Git 进程、Linux 容器卷、Docker Compose、强杀恢复、备份恢复。
- 真实本地 `bge-m3` tokenizer/embedding 推理与模型质量；本 3A 记录不声明 3B 索引或 3C 检索质量，见下方 3B、3C 节。
- 云模型调用、费用上限、密钥配置与真实 Qwen 兼容 API 验收。
- 3A 整体审阅复审：原审阅者已于 2026-11-02 确认 **GREEN / 交付（本地范围）**，I-1、I-2、M1–M3、M5 均 CONFIRMED，M4 按设计确认，无新的 Critical/Important。复审遗留的三项非阻塞观察记录在 `.superpowers/sdd/2026-10-20-devflow-knowledge-ingestion/3a-review.md`。本记录中的外部门槛（真实 GitHub/Webhook、原生 Git、Docker/Linux、恢复、真实 BGE 推理、云模型）仍为 NOT EXECUTED。
- Git 提交/合并：本轮改动仍未提交，按既有 Git 审批限制保持工作区状态。

## 3B 本地索引、重启恢复与部署资源（2026-11-04）

本节记录 3B Task 1–5 的本地实现与验收证据。真实隔离 Milvus 使用一次性集合，结束后删除；没有连接生产数据库、生产 Milvus 或 GitHub App，也没有云模型调用。唯一迁移 head 为 `0016_one_index_build`。

### 环境与边界

- Windows 11 主机；后端 Python 3.12.13、UV 0.11.25；专用测试 PostgreSQL 17.11（`127.0.0.1:55439/devflow_test`）。
- Docker Engine 29.9.2、Compose 5.6.1；真实隔离 Milvus standalone 为 `milvusdb/milvus:v2.6.26@sha256:f43e0cd0...`（容器 `devflow-milvus-check-milvus-1`，配套 MinIO/etcd），客户端 PyMilvus 2.6.17，server build `2.6-20261023-b8d2ca4240`。
- 3B 聚焦测试中的向量来自确定性 fixture；它们证明 schema、写入、Strong 可见性、重启和恢复判定，不证明 BGE 语义质量。
- 真实本地模型权重未提供，`sentence-transformers`/`torch` 真实推理与设备指标为 **NOT EXECUTED**；没有用 fake loader 冒充真实模型通过。

### 执行命令与结果

在 `backend/` 下执行（工作树 `D:/python/DevFlow/.worktrees/data-foundation`）；Compose 命令在仓库根执行：

| 命令 | 结果 |
| --- | --- |
| `uv lock --check` | Resolved 84 packages；EXIT 0（CPU-only PyTorch 锁） |
| `uv run --no-sync pytest tests/test_milvus_contract.py tests/test_index_availability.py tests/test_model_owner.py tests/test_local_inference.py tests/test_model_worker.py -q` | 123 passed in 5.77s |
| `TEST_MILVUS_URI=http://127.0.0.1:19530 TEST_MILVUS_RESTART_COMMAND="docker restart devflow-milvus-check-milvus-1" uv run --no-sync pytest tests/integration/test_local_index_flow.py -q` | 2 passed in 35.65s（含真实 Milvus 服务重启） |
| `TEST_MILVUS_URI=http://127.0.0.1:19530 uv run --no-sync pytest tests/integration/test_index_publication.py tests/integration/test_index_retention.py -q` | 22 passed in 37.12s（含真实 Milvus 部分写入/不可发布用例与 I-5/I-8 回归） |
| `uv run --no-sync pytest tests/integration/test_model_runtime.py -q` | 4 skipped；缺少 `TEST_MODEL_*` 真实模型配置，NOT EXECUTED |
| `uv run --no-sync pytest tests/integration/test_knowledge_routes.py tests/integration/test_knowledge_retention.py tests/integration/test_knowledge_ingestion_flow.py tests/integration/test_knowledge_source_reads.py tests/integration/test_knowledge_import_jobs.py tests/integration/test_knowledge_sources.py tests/integration/test_migrations.py tests/test_config.py -q` | 131 passed, 1 failed；失败为已知并发容量准入 flake，单独重跑 1 passed in 3.30s，见下方说明 |
| `uv run --no-sync ruff check .` / `ruff format --check .` | All checks passed / 201 files already formatted |
| `uv run --no-sync alembic heads` | `0016_one_index_build (head)` |
| `docker compose --project-directory .. -f ../infra/compose.test.yaml config --quiet` | EXIT 0 |
| 生产 `docker compose -f infra/compose.yaml config --quiet`（合成占位值、非真实秘密） | EXIT 0 |
| dev 覆盖默认服务 | postgres、migrate、api、frontend、gateway、worker；不启动 Milvus/model-worker |
| dev 覆盖 `--profile milvus --profile models` | 追加 etcd、minio、milvus、model-worker |
| 实际构建 `runtime` 与 `model-runtime` 目标镜像，并在容器内检查导入边界（Windows Docker 29.9.2） | 均 EXIT 0；镜像分别约 816 MB / 9.91 GB；`torch`/`sentence-transformers` 只在模型镜像可导入。该模型镜像使用旧 CUDA 锁；改用 CPU-only 索引后的重新构建为 **NOT EXECUTED** |

上面合并 3A 聚焦集里的 `test_concurrent_capacity_admission[repository]` 是此前已记录的并发/timing 敏感用例；本次整包运行时失败，单独重跑通过（3.30s）。该不稳定项仍按既有口径记录，不据整包结果声明后端全绿。

### 重启与恢复判定

- 小语料发布后，真实 Milvus 服务重启（`docker restart`）再以新客户端/`MilvusIndex` 实例重载 schema；Strong 可见性、BM25/dense 搜索结果与重启前成员完全一致，可用性为 `ready`。这是 Milvus 服务重启证据，不是 Worker 容器强杀证据。
- 同一测试还覆盖“新适配器无进程内构建缓存”的 Worker 重启语义；真实 Worker 容器/进程重启仍为 **NOT EXECUTED**。
- 数据库仍指向该版本但 Milvus 集合缺失时，只读探测返回 `stale/collection_missing`，概览抛出 409 `index_not_ready`，且不会创建同名集合。普通未就绪状态（无目标、构建中、失败）不是 409：概览返回 200 并带 `index_status`/`index_reason`。
- 向量被回收时探测返回 `stale/vectors_missing` 并触发同样的 409。模型运行时指纹与存储指纹的比较由 model-worker/查询路径调用 `IndexAvailabilityService.check(fingerprint=...)`，目前只用确定性 fixture 覆盖 `stale/fingerprint_mismatch`；真实 BGE 指纹比较为 **NOT EXECUTED**。
- Task 4 的真实 Milvus 用例验证：首批 16 个成员写入后版本仍为 `building` 且未发布；完整清单在 Strong 可见性通过前不可查，第二批完成后才原子切换 active。
- 数据库层保证每仓库最多一个未完成（`queued`/`building`）索引版本：`0016_one_index_build` 先在迁移中把历史重复候选收敛为 `superseded`，再创建部分唯一索引；显式请求和导入自动入队都会在语料锁事务内 supersede 旧候选。过期执行者（owner/attempt/generation 不匹配）只拒绝自己，不会把已恢复 owner 仍在构建的候选标记为 superseded。上述行为均有聚焦回归测试覆盖。

### 部署配置与镜像

- API、普通 Worker、migrate 使用同一基础运行时镜像，不安装也不导入 `torch`/`sentence-transformers`；3A tokenizer 依赖（`tokenizers`、`transformers`）仍可导入。
- 实际构建了 `runtime` 目标镜像 `devflow-backend:t5-runtime-check`（`sha256:8811749371b5...`，约 816 MB）：镜像内 `import torch` 与 `import sentence_transformers` 均为 `ModuleNotFoundError`；`tokenizers 0.23.3`、`transformers 5.20.2` 与 `devflow.app`/`devflow.worker` 导入成功，Git 2.47.3。
- 历史证据：实际构建过 `model-runtime` 目标镜像 `devflow-backend-models:t5-check`（`sha256:e471708a5bd5...`，约 9.91 GB）：镜像内 `torch 2.15.0+cu132`、`sentence-transformers 6.2.0`、`transformers 5.20.2`、`tokenizers 0.23.3` 与 `libgomp` 可导入，CPU 张量运算通过；镜像未内置模型权重。本机未透传 GPU，`torch.cuda.is_available()` 为 `False`，真实 CUDA 推理仍为 **NOT EXECUTED**。该镜像来自旧 CUDA 锁，不能作为当前锁的验证证据。
- 当前锁配置 `pytorch-cpu` 显式索引后，`torch 2.15.0+cpu`（macOS 为 `2.15.0`）不再携带 `cuda-toolkit`、`nvidia-*` 或 `triton`；`uv lock --check` 通过。用当前锁重新构建模型镜像、检查其实际大小与导入边界为 **NOT EXECUTED**；基础 API/Worker 镜像不受影响。
- `model-worker` 使用独立 `model-runtime` 构建目标（`devflow-backend-models`），仅该镜像安装可选 `models` extra；Compose 固定 `WORKER_ROLE=model`、`WORKER_CONCURRENCY=1`、共享 `model-owner` 卷和只读 `/models` 挂载。
- `MODEL_DEVICE`/`MODEL_DTYPE` 空值会让 model worker 启动失败，不会静默选择 CPU；CPU 或分时共享必须显式配置并另行测量。
- Milvus standalone 建议至少 8 GiB、16 GiB 更稳妥；`milvus-etcd`、`milvus-minio`、`milvus-data` 与 PostgreSQL 必须按同一时间点备份。卷容量、WAL、OOM、备份/恢复演练均为 **NOT EXECUTED**。

### 未执行项（NOT EXECUTED）

- 真实 BAAI/BGE-M3 embedding 与 reranker 权重加载/推理、CUDA、冷/热延迟、峰值 RAM/VRAM 和并存服务测量；镜像内的包导入与 CPU 张量运算已验证，但 `test_runtime_owner_and_memory_report` 未运行。
- 真实模型 owner 的取消线程、挂起/终止及新 owner 启动：Windows 与 Linux 均未执行，不能互相推断。
- Linux 上的镜像运行、模型目录 UID10001 权限、真实 Worker/model-worker 容器生命周期与强杀恢复。
- Milvus 8 GiB/16 GiB 内存门槛下的 OOM 行为、卷容量/WAL 监控、Milvus 卷级备份与恢复。
- 生产部署、真实 GitHub App、真实账户和云端调用；真实检索质量由 3C 的 38 题固定集负责，其真实模型运行仍为 NOT EXECUTED（见 3C 节）。
- Git 提交/合并：3B 改动仍未提交，按既有 Git 审批限制保持工作区状态。

## 3C 检索质量、权限端到端与引用契约（2026-11-05）

本节记录 3C Task 1–5 的本地实现与验收证据。真实隔离 Milvus 使用一次性集合（`quality_<hex>`），结束后删除；PostgreSQL 仅使用专用测试库；没有连接生产数据库、生产 Milvus 或 GitHub App，也没有云模型调用。唯一迁移 head 为 `0017_knowledge_queries`。

### 环境与边界

- Windows 11 主机；后端 Python 3.12.13、UV 0.11.25；专用测试 PostgreSQL 17.11（`127.0.0.1:55439/devflow_test`）。
- 真实隔离 Milvus standalone（容器 `devflow-milvus-check-milvus-1`，`127.0.0.1:19530`，PyMilvus 2.6.17）；测试集合在结束时删除。
- 固定集共 38 题：zh 6、en 6、mixed 5、code 5、path 5、no_answer 4、old_sha 4、isolated 3。gold 只使用仓库、源 SHA、路径和原始行区间，不含实现产生的 chunk ID 或 index version ID。
- 真实模型权重未提供：`TEST_MODEL_EMBEDDING_PATH`、`TEST_MODEL_RERANKER_PATH`、`TEST_MODEL_DEVICE`、`TEST_MODEL_DTYPE`、`TEST_MODEL_TOKENIZER_PATH` 均未设置，因此真实 BM25/dense/RRF/RRF+rerank 质量运行、真实模型指纹与索引指纹均为 **NOT EXECUTED**。没有用 fake ranker 或 fixture 向量宣布 ≥0.85 质量目标。
- 权限端到端用例使用真实 PostgreSQL 与真实 Milvus；模型向量为确定性 fixture，只验证权限、版本、引用与可见性边界。

### 执行命令与结果

在 `backend/` 下执行（工作树 `D:/python/DevFlow/.worktrees/data-foundation`）：

| 命令 | 结果 |
| --- | --- |
| `TEST_MILVUS_URI=http://127.0.0.1:19530 uv run --no-sync pytest tests/test_retrieval_metrics.py tests/integration/test_retrieval_quality.py tests/integration/test_retrieval_flow.py -q` | 6 passed, 1 skipped in 12.17s |
| `TEST_MILVUS_URI=http://127.0.0.1:19530 uv run --no-sync pytest tests/integration/test_retrieval_quality.py -q -rs` | 1 skipped；原因明示缺少 5 个 `TEST_MODEL_*`，真实质量运行 NOT EXECUTED |
| `TEST_MILVUS_URI=http://127.0.0.1:19530 uv run --no-sync pytest tests/test_retrieval_metrics.py tests/test_retrieval_ranking.py tests/integration/test_knowledge_citations.py tests/integration/test_retrieval_pipeline.py tests/integration/test_retrieval_scope.py tests/integration/test_retrieval_flow.py tests/integration/test_retrieval_quality.py -q` | 20 passed, 1 skipped in 32.35s（3C 聚焦回归） |
| `uv lock --check` | Resolved 84 packages；EXIT 0 |
| `uv run --no-sync ruff check .` / `uv run --no-sync ruff format --check .` | All checks passed / 222 files already formatted |
| `uv run --no-sync alembic heads` | `0017_knowledge_queries (head)` |

在 `frontend/` 下执行：

| 命令 | 结果 |
| --- | --- |
| `npm.cmd run lint` | PASS |
| `npm.cmd run typecheck` | PASS |
| `npm.cmd run test:e2e -- tests/retrieval-flow.spec.ts` | 3 passed in 4.7s |

### 覆盖的行为

- 指标口径：macro Recall@6 / nDCG@6 只对可答题取平均；重叠 chunk 覆盖同一 gold 区间不重复加分；无答案题不进入 Recall 分母，但误命中与正确拒答分别计数；SHA 或路径不一致不算命中；越权仓库或隔离标记计为 leak。
- 固定集：38 题覆盖中文、英文、混合、代码标识、路径、无答案、旧 SHA 与隔离仓库；旧 SHA 题只选择 active 索引；隔离题只选择基础仓库并检查隔离标记不泄漏。
- 权限端到端（真实 PG + 真实 Milvus）：非创建者读取查询、引用与 SSE 均 404；fork 仓库的独占内容不会被基础仓库结果漏选；源删除后查询与引用撤销；旧执行者与过期 lease 无法继续发布；服务重启后权限与版本判断不退化；代次切换会以 `version_changed` 拒绝旧索引且不返回陈旧证据。
- 引用契约：预览只使用服务端 manifest 中的 identity/location；manifest 之外的 citation ID 不提供预览入口并返回 404；撤权预览不返回正文；`is_current`/`partial` 与续读游标由服务端给出。
- 前端契约：查询提交的 `indexes` 只包含真实有索引的仓库，source-only fork 仅出现在 `selected_repository_ids`；`version_changed` 时清除旧结果并提示重新选择范围。

### 未执行项（NOT EXECUTED）

- 真实 BAAI/BGE-M3 embedding/reranker/tokenizer 权重加载与推理、CUDA、真实模型/索引指纹，以及四条检索路径的 `macro_recall_at_6`/`macro_ndcg_at_6` 与 RRF+rerank ≥0.85 目标：全部 NOT EXECUTED。
- 真实私有仓库、正式并发/容量/硬件测量、生产部署与正式验收。
- 浏览器测试拦截 `/api/v1/...`，只证明 UI 契约，不证明真实前后端联调。
- Git 提交/合并：3C 改动仍未提交，按既有 Git 审批限制保持工作区状态。

## 3D 私有对话数据基础（Task 4：官方 saver fence 与 thread 清理，2026-11-06）

本节记录 3D Task 4 的本地实现与验收证据：官方 LangGraph checkpoint saver 的同连接 fence、accepted config、容量核算与终态 thread 清理。未连接生产数据库、GitHub App、Milvus 或云模型；没有真实模型调用。

### 环境与边界

- Windows 11；后端 Python 3.12.13、UV 0.11.25；专用测试 PostgreSQL 17.11（`127.0.0.1:55439/devflow_test`）。唯一迁移 head 为 `0020_chat_retention`。
- 实际锁定并运行的官方包：`langgraph 1.2.20`、`langgraph-checkpoint-postgres 3.2.0`、`langgraph-checkpoint 4.3.1`、`psycopg 3.3.6`。本文的 saver 行为描述来自该安装版本与真实隔离 PostgreSQL 测试，不是对上游源码的推断。
- 官方 saver schema 只通过受控命令 `uv run --no-sync python -m devflow.chat.checkpoints --setup` 部署；测试与部署都使用 autocommit raw psycopg 连接。启动和请求路径不隐式建表；测试没有用 in-memory saver 替代官方包。

### 执行命令与结果

在 `backend/` 下执行（工作树 `D:/python/DevFlow/.worktrees/data-foundation`）：

| 命令 | 结果 |
| --- | --- |
| `APP_ENV=test PUBLIC_BASE_URL=http://127.0.0.1:8000 DATABASE_URL=postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test uv run --no-sync python -m devflow.chat.checkpoints --setup` | EXIT 0；测试库 public schema 出现 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations` 四张官方表 |
| `uv run --no-sync pytest tests/integration/test_chat_checkpoints.py tests/integration/test_chat_retention.py -q` | 10 passed in 17.79s |
| `uv run --no-sync pytest tests/integration/test_chat_budget.py tests/integration/test_chat_privacy.py tests/integration/test_job_submission.py tests/integration/test_job_transactions.py tests/integration/test_job_visibility.py tests/integration/test_migrations.py tests/integration/test_worker_denial_fencing.py tests/integration/test_worker_recovery.py -q` | 89 passed in 93.75s |
| `uv run --no-sync ruff check src tests migrations` / `ruff format --check src tests migrations` | All checks passed / 243 files already formatted |
| `uv run --no-sync alembic heads` | `0020_chat_retention (head)` |

### 覆盖的行为

- 原子性：`aput` 在官方 rows/blobs 写入后、accepted config 更新处注入失败时，整个显式事务回滚，官方三表计数保持 0 且 `accepted_config` 仍为 NULL；官方 saver 的 pipeline 与 savepoint 两条路径都不能提交外层事务。
- 同连接 fence：一次 `aput`/`aget_tuple`/`aput_writes` 在一个独立 mutex 内完成；raw psycopg 事务先按 User → 全部 Installation → Repository → Job → AgentRun 顺序加锁并复核 owner/generation/attempt/status/租约到期；旧 owner、旧 generation、旧 attempt、已到期与来源撤权均被拒绝，且不产生官方行。
- 锁等待时钟：在等待 Job 行锁期间把租约改为已到期，持有锁后的 DB 时钟复核仍拒绝写入，说明判断不依赖进程内时间。
- 恢复边界：`aget_tuple` 只接受 DB accepted config 中的 checkpoint ID；省略 ID 时读取 accepted checkpoint；任意其他 ID 与任何非空 `checkpoint_ns`（attempt namespace）返回 `validation_error`。同 ID 后合法 pending writes 可见；旧 lease 写入被拒绝，新 generation 可以继续追加。
- 容量：每次 checkpoint 序列化上限 64 KiB、每 run 最多 32 个 checkpoint、全局 128 MiB；写前检查与写后同事务按官方 rows/blobs/pending 实际逻辑字节核算，超限回滚为 `state_limit`。全局满时只驱逐更早的已终态且 accepted 已 retire/清空的 thread；活跃或未知状态不被删除。
- 清理顺序：正常终态先在同一事务 retire 并清空 accepted，再由独立 `chat.cleanup` lease 复核后调用官方 `adelete_thread`；中断可重做，覆盖 checkpoints/blobs/writes。Message、manifest、receipt 与预算账本不在删除范围。writer 与 cleanup 竞争时，旧 writer 的 lease 由共享 advisory lock 与终态条件拒绝。
- 数据库语义：`accepted_config` 使用 `JSONB(none_as_null=True)`，retire 后是 SQL NULL 而不是 JSON `'null'`，清理候选谓词因此可靠；新的 `0020_chat_retention` 迁移同时创建 `chat_checkpoint_accounts` 并登记 `chat.cleanup` Job kind。

### 未执行项（NOT EXECUTED）

- 真实云模型调用/真实 Agent graph 运行中的 checkpoint 写入；Task 4 只验证 saver 层与 Job/retention 集成，没有发出云请求。
- 真实 24 小时终态等待与生产容量水位；测试用谓词/边界构造覆盖，不是墙钟 24 小时运行。
- 生产部署、真实 GitHub App、生产数据库、真实 Milvus、正式备份恢复与正式验收。
- 本次没有重跑完整的 1280 项后端测试集；上述聚焦与相关回归结果不能表述为整套后端或生产验收通过。
- Git 提交/合并按既有审批限制执行。
