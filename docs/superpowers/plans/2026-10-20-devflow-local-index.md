# DevFlow 3B Local Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付唯一模型 Worker、真实 Milvus BM25+dense 索引、受 fence 保护的 active 发布和有界回收。

**Architecture:** 复用同一 PostgreSQL 队列；唯一 model-capable Worker 持有 OS owner 锁并串行处理全部模型 Job。Milvus 写入和 Strong 可见性检查在事务外，PostgreSQL 中的源代次和 active 指针决定可查询性。3C/3D 直接消费这个 Worker 内的本地推理服务。

**Tech Stack:** Python/UV、Sentence Transformers/PyTorch/Transformers、Milvus/PyMilvus、PostgreSQL、现有 Worker、Linux Compose，Windows 开发行为亦验证。

**Spec:** [已批准规格](../specs/2026-10-19-devflow-retrieval-design.md) §3–6、8、11–13；前置 [3A](2026-10-20-devflow-knowledge-ingestion.md)。用户于2026-10-20确认计划，沿用 A；第二阶段先完成，之后按3A→3B推进。确认不等于实现或运行验收。

## Global Constraints

- “一个明确配置为model-capable的既有Worker进程拥有本地模型”；API/普通Worker不加载，锁在同主机容器间共享挂载。
- 模型路径为配置的本地 BAAI bge-m3 / bge-reranker-v2-m3；dense 1024维、归一化、COSINE；不下载、不 trust_remote_code、不使用稀疏/多向量模型输出。
- Embedding/Rerank batch 均为1、同时最多一次推理；首次CUDA FP16/双模型常驻只是待测配置候选，OOM不可就绪，不自动切云/换模型。
- 模型 Worker 串行处理后台编码、独立检索、完整 ChatAgent（含HTTP等待）；图内直接检索，禁止等待子模型Job。
- 交互优先；连续10个交互任务后允许1个后台批次；后台批次最多16chunk/120秒、构建24小时、失败最多3次，累计不重置。
- 失租/取消不释放真实推理槽；等线程及 CUDA 完成；无法结束则停止claim/unhealthy，进程终止后新 owner 才可能取得锁，不声称驱动立即释放资源。
- 每仓库向量最多1 active/1 building/1 retired；全局成员120,000；retired向量最多7天。向量删了不删除仍保留引用身份/正文。
- 使用真实Milvus，DB记录active才发布；不能以mock或分时运行声称16GiB/6GiB全栈可用。Milvus Standalone最低8G/推荐16G是外部资源约束。
- 无模型/Git/HTTP 操作进入DB事务；不引入daemon/RPC/第二队列。UV锁定实际可兼容依赖，研究版本不作为已验证依赖。

## Review Focus

1. 取消 coroutine 后 CUDA仍跑：Task 1 验证物理槽、owner锁与线程结束顺序。
2. 普通Worker抢走聊天，或模型Worker等自己子任务：Task 2 固定kind领取与串行合同。
3. Milvus upsert成功但query不可见：Task 3/4 Strong逐成员验证，禁止发布。
4. 同 SHA 新目标代次/旧执行者晚提交：Task 4 拒绝切active。
5. DB恢复到旧快照而Milvus已被清理：Task 5 明确索引失效，不悄悄重用同名集合。

## 文件职责与交接

新增 `backend/src/devflow/knowledge/inference.py`（唯一模型资源与有界推理）、`owner.py`（跨平台OS锁）、`milvus.py`（固定schema/有界操作）、`indexing.py`（构建/发布）、`index_retention.py`（版本回收）；沿用3A `models/schemas/access/handlers`。不新建通用任务框架。3A `SourceGrant/SourceIdentity/ChunkLocation/ChunkDraft` 是唯一来源类型。

遵循3A命令工作目录、测试隔离和 Git 不可用规则。Task 1先核对2B/3A最终 Worker/Queue 取消、kind注册、授权锁顺序；计划中的修改不是宣称已有这些能力。Task 4迁移预计 `0011_knowledge_indexes.py`→实际 `0010_knowledge_sources`；执行 `uv run --no-sync alembic heads` 后以真实单head编号，不能分叉。

### Task 1: 唯一本地模型资源与不可提前释放的推理槽

**Files:** Create `knowledge/owner.py`, `inference.py`, `tests/test_model_owner.py`, `tests/test_local_inference.py`, `tests/support/model_process.py`; Modify `backend/pyproject.toml`, `backend/uv.lock`, `backend/src/devflow/config.py`。

**Interfaces:** `ModelOwner(lock_path:Path).acquire()->bool`, `.close()->None`；`LocalInference(settings).start()->ModelFingerprint`, `.embed(texts:Sequence[str])->list[list[float]]`, `.rerank(query:str,passages:Sequence[str])->list[float]`, `.drain()->None`（异步调用，模型内部单条batch1）；`ModelFingerprint(embedding:str,reranker:str,tokenizer:str,dimension:int,device:str,dtype:str)`；`ready`/`unhealthy` 只报告安全状态。

- [x] **RED:** `test_second_owner_never_loads_models` 两个真实受控进程争同锁，loader调用总数1；`test_cancel_waits_for_physical_inference` 用可阻塞线程断言取消/失租后第二次推理不开始，真实线程结束再释放槽；`test_hung_inference_stops_claim` 断言unhealthy且不释放owner；`test_local_only_load_and_dimensions` 验证本地路径/remote code关闭、batch1、1024维/归一化、OOM不可就绪。fake loader只验证生命周期，不标为真实模型通过。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_model_owner.py tests/test_local_inference.py -q`。
- [x] **Implement:** 标准库Windows文件字节锁/Linux flock固定锁文件，无unlink重建竞态；锁文件在服务目录，所有容器共享。每模型调用独立串行物理完成跟踪，async取消不取消完成等待任务；`drain`确认线程返回与需要的CUDA同步后才关闭。启动先owner、再模型指纹/仅本地加载/小自检；driver延迟导致OOM明确失败，有限重启交由运维。UV选择与Python3.12及目标CUDA/CPU兼容的稳定版本，采用项目extra `models`（Sentence Transformers/PyTorch及必要依赖），3A tokenizer共用约束；显式配置Torch源/平台marker并锁定，禁止pip侧装和浮动main。推理包在 `LocalInference.start` 内延迟导入；普通API/Worker不安装models extra也能启动且绝不加载权重，新增该启动回归断言。
- [x] **GREEN:** 上述PASS及 `uv lock --check`、`uv run --no-sync ruff check src/devflow/knowledge/owner.py src/devflow/knowledge/inference.py tests/test_model_owner.py tests/test_local_inference.py tests/support/model_process.py`。Windows/Linux锁与进程终止分别留证据；真实CUDA/双模型自检由Task5执行，未执行如实记录。
- [x] **Commit:** `feat: own and serialize local model inference in one worker`。

### Task 2: 同队列模型任务领取、串行与公平性

**Files:** Modify `jobs/queue.py`, `jobs/schemas.py`, `worker.py`, `knowledge/handlers.py`; Create `tests/integration/test_model_queue.py`, `tests/test_model_worker.py`。

**Interfaces:** 扩展 `Queue.claim(owner:UUID,*,allowed_kinds:frozenset[str]|None=None,prefer_kinds:tuple[str,...]=())->Lease|None`；None表示既有非模型kind集合，不表示全部kind；普通Worker显式排除模型kind。模型kind常量为 `knowledge.index_batch`、`knowledge.query`、`chat.run`；后两kind消费者分别在3C/3D注册，不在此写空成功handler。`Worker`持有一个 `LocalInference` 实例并注入 handlers；owner未取得/不健康不claim模型任务。

- [x] **RED:** `test_kind_filter_applies_to_candidate_and_claim` 包括claim前改kind/重试；普通worker永不领模型任务，两个模型owner进程只有一个领。`test_ten_interactive_then_one_background` 同时排队时前10个交互后后台获得机会；无后台时交互继续。`test_chat_occupies_worker_during_http` 用阻塞的未来chat测试handler，云等待期间第二任务不领取；`test_direct_retrieval_uses_parent_lease` 验证注入服务无需enqueue子Job；恢复仍为同一串行slot。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_model_queue.py tests/test_model_worker.py -q`。
- [x] **Implement:** 过滤同时用于候选选择和锁定claim，保留旧system/probe分发行为；模型Worker执行完整handler再下一claim，heartbeat独立且不占推理槽。进程内公平计数在owner生命周期累计，连续交互阈值10、后台一次≤16块；进程重启可能重置调度计数但不能重置构建deadline/重试。handler取消和Worker退出走Task1 drain；超时且线程仍活时停止claim并unhealthy，不无限await后继续领取。
- [x] **GREEN:** 聚焦测试 + `uv run --no-sync pytest tests/test_worker_execution.py tests/integration/test_queue_leases.py tests/integration/test_worker_denial_fencing.py -q`；Ruff实际改动PASS。
- [x] **Commit:** `feat: route model jobs through the existing serial worker`。

### Task 3: 真实 Milvus schema、写入和两路检索边界

**Files:** Create `knowledge/milvus.py`, `tests/test_milvus_contract.py`, `tests/integration/test_milvus_index.py`; Modify `config.py`, `backend/pyproject.toml`, `backend/uv.lock`, `infra/compose.test.yaml`, `infra/compose.dev.yaml`。

**Interfaces:** `MilvusIndex.ensure_schema(fingerprint:IndexFingerprint)->None`；`upsert(index_id:UUID,rows:Sequence[IndexRow])->None`；`verify_visible(index_id:UUID,chunk_ids:Sequence[UUID])->bool`；`search(query_text:str,query_vector:list[float],filters:tuple[IndexFilter,...],*,limit:int=30)->RankedCandidates`；`delete_version(index_id:UUID)->None`。`IndexFilter(repository_id:UUID,index_version_id:UUID)`；`IndexRow(chunk_id:UUID,repository_id:UUID,text:str,dense:list[float])`；`RankedCandidates(bm25:tuple[RankedHit,...],dense:tuple[RankedHit,...])`；`RankedHit(chunk_id:UUID,index_version_id:UUID,rank:int,score:float)`不返回正文。`IndexFingerprint`含模型/tokenizer/analyzer/chunking/schema版本。

- [x] **RED:** `test_equal_filters_for_bm25_and_dense` 检查两路同server-side过滤、用户值不能拼表达式注入；`test_stable_primary_key_and_dim` upsert重复不增成员、不同版本不覆盖、维度非1024拒绝；真实 `test_strong_visibility_requires_all_members` 缺一个成员/尚未load不能ready；中英、snake_case、CamelCase、路径、错误码检索fixture记录分词行为。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_milvus_contract.py -q`；隔离真实Milvus提供后 `uv run --no-sync pytest tests/integration/test_milvus_index.py -q`，缺服务是未执行而非mock通过。
- [x] **Implement:** UV锁定兼容Milvus server的PyMilvus，固定schema：稳定版本+chunk主键、repository/index UUID、chunk ID、1024 dense/COSINE、text及原生BM25 function/sparse索引。中文analyzer固定带版本；`IndexRow.text` 使用3A已包含路径/标题前缀且整体受512token/8KiB约束的embedding_text，不二次添加无界前缀；返回预览仍用规范正文。限制搜索5秒、参数/返回数量，构建create/load/index可见性在构建deadline内有界；Strong校验按≤16成员批次检查精确ID，禁止仅比较总数。服务名称、镜像tag/digest与客户端版本在实现时核对官方契约，不宣称当前某版本已测。
- [x] **GREEN:** 契约与真实隔离Milvus测试分别记录；`uv lock --check`、Ruff；compose检查不写入正式数据卷。
- [x] **Commit:** `feat: add versioned Milvus dense and BM25 storage`。

### Task 4: 分批构建、原子 active 发布及向量回收

**Files:** Create `knowledge/indexing.py`, `index_retention.py`, `backend/migrations/versions/0011_knowledge_indexes.py`, `tests/integration/test_index_publication.py`, `tests/integration/test_index_retention.py`; Modify `knowledge/models.py`, `schemas.py`, `handlers.py`, `routes.py`, `backend/migrations/env.py`。

**Interfaces:** `IndexBuilder.request(actor:Actor,import_id:UUID,key:str)->IndexReceipt(index_id:UUID,job_id:UUID)`；`run_batch(lease:Lease)->None`；`publish(lease:Lease,index_id:UUID,visibility:VisibilityProof)->bool`；`IndexRetention.sweep(lease:Lease,*,limit:int=100)->RetentionResult`。`VisibilityProof(index_id:UUID,member_digest:str,fingerprint:IndexFingerprint,observed_at:datetime)` 是内部实际Strong检查结果，客户端不可提交。3A导入完成生成同代次构建需求，构建kind沿用Task2。

3A交接澄清：IndexBuilder消费该 import 的完整当前目标成员清单，以及 composed target SHA/scopes/原始完整授权 epochs；不能把触发上传 mutation 的 sha=None 或单个新增文档当作整个索引。若3A在0010后已增加导入状态迁移，本任务先核对唯一实际 head，再为 index migration 顺延编号与 down_revision；上列0011是原计划占位，绝不覆盖已存在迁移或声称本任务迁移已落地。

- [x] **RED:** `test_active_requires_current_generation_full_fence_and_visible_members` 参数化用户/安装/仓库epoch、owner/generation/attempt/expiry、同SHA新generation、源失效；`test_external_write_then_db_rollback_is_unpublished` 留孤立版本；`test_batch_restart_preserves_counts_deadline_attempts`；`test_vector_retention_caps_and_reference_survival` 断言每库1/1/1、全局120000、retired7天、消息仍保留正文、active不能为腾空间删除。两并发build只有当前允许版本可进入building。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_index_publication.py tests/integration/test_index_retention.py -q`。
- [x] **Implement:** 创建 `IndexVersion`、`IndexMember`、期望digest/完成计数/授权与目标代次，给Corpus active指针加FK；请求/重建入口同3A要求admin+repo访问，导入自动需求保留该授权来源。分批编码/upsert使用Task1/3，外部操作事务外。发布按3A ordered锁+完整Lease复核→匹配所有清单、规则与源状态→切active→旧active retired→Job/事件原子提交。查询选择依赖target generation，不只SHA；新SHA目标未就绪允许明确选择仍active旧SHA，同SHA新代次禁止偷偷用旧上传。清理在有效cleanup lease下先标不可查再删Milvus版本；网络失败重试幂等、孤立写不能复活active，清理不得删building的合法在途批次。保留历史索引来源标识，不把元数据计作可查询向量版本。
- [x] **GREEN:** 聚焦测试、migration保留数据回归、3A代次测试PASS；Ruff。Milvus部分写/不可见用真实Task3 fixture重演一例；假client只覆盖竞争故障路径。
- [x] **Commit:** `feat: publish and retire indexes under source and job fencing`。

### Task 5: 部署资源、重启恢复与 3B 验收

**Files:** Create `tests/integration/test_local_index_flow.py`, `tests/integration/test_model_runtime.py`; Modify `infra/backend.Dockerfile`, `infra/compose.yaml`, `infra/compose.dev.yaml`, `infra/compose.test.yaml`, `docs/operations/knowledge-acceptance.md`, `README.md`；配置示例只编辑已批准的公开example文件，不读取秘密文件。

**Interfaces:** 无新业务接口；提供固定部署配置的普通worker/model-worker角色、共享owner锁挂载、只读本地模型目录和Milvus卷。API镜像不加载模型；使用extra安装只在模型worker镜像/运行环境启用，3A tokenizer依赖仍可用。

- [x] **RED:** `test_index_restart_and_restore_mismatch`：真实小语料构建→查→重启Milvus/Worker→相同成员；恢复DB但缺Milvus集合或fingerprint不同返回index_not_ready/失效原因。`test_runtime_owner_and_memory_report` 在明确选择的真实runtime配置记录冷/热延迟、峰值RAM/VRAM、设备/dtype/指纹/并存服务；取消线程、挂起/终止owner、新owner启动Windows和Linux分别测，不从平台A推断B。
- [x] **Run RED:** 服务与模型由执行阶段按隔离资源提供后 `uv run --no-sync pytest tests/integration/test_local_index_flow.py tests/integration/test_model_runtime.py -q`；所需资源缺失为NOT EXECUTED，不能降级fake并保留同一验收标签。
- [x] **Implement:** 接入有界启动自检、明确未就绪/锁忙/OOM错误与健康状态；配置CPU/分时加载必须显式选择且另测。模型路径映射至用户配置，Windows现有目录仅作开发输入，不硬编码生产宿主路径。记录Milvus8G/16G要求、卷容量/WAL/备份警戒；分时试验不能称同机全栈通过。
- [x] **GREEN/交付:** `uv lock --check`、本阶段聚焦pytest/Ruff、隔离 `docker compose -f infra/compose.test.yaml config`（仓库根）及实际镜像/持久化验证；记录真实30题质量由3C负责，真实账户/云不在本阶段。A方式任务与整体审阅后交接 [3C](2026-10-20-devflow-retrieval-citations.md)，仍未执行的资源门槛显式保留。
- [x] **Commit:** `test: verify local index lifecycle and deployment resources`。
