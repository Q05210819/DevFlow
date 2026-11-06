# DevFlow 3A Knowledge Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付仓库绑定上传、固定 Workspace SHA 导入、确定性切块和可追溯来源预览。

**Architecture:** PostgreSQL 保存规范正文、来源、目标代次和逐项覆盖，既有 Job 执行有界导入。Workspace 提供授权且被 pin 保护的原始对象读取；知识模块不抓 Git，不把 UI 预览当完整文件。3B 消费本阶段已完成的不可变 chunk 清单。

**Tech Stack:** Python 3.12、UV、SQLAlchemy/PostgreSQL、FastAPI、现有 Job/Lease、仅本地 Transformers tokenizer、Next.js、pytest/Playwright。

**Spec:** [用户已确认的第三阶段规格](../specs/2026-10-19-devflow-retrieval-design.md)，尤其 §3–5、9–12；前置 [2B Workspace](2026-10-13-devflow-workspace.md)。用户于2026-10-20确认本计划，按既有 A 方式实施。确认不表示第三阶段已实现或授权云消费；2B 完成并核对实际接口后开始。

## Global Constraints

**2026-10-23 执行入口记录：** 第二阶段完整代码审阅发现的问题已修复并通过一次独立复审；原生运行、完整后端、浏览器/build、容器与恢复验收仍未通过。遵循用户“继续可用的开发工作”的指示，在核对最终 Workspace/Queue/Worker/授权接口及唯一源码 head `0009_workspace` 后，先实施本阶段可逆的来源与数据库基础。该执行顺序调整不豁免第二阶段验收，不表示已合并、部署或授权云消费。

- “管理员需同时有该仓库访问权”；管理员角色、安装权限、磁盘对象存在均不代替用户权限。Repository ID 使用 UUID，GitHub 数字 ID 不是本地 ID。
- 不执行仓库代码、不获取 LFS、不跟随 symlink/gitlink、不接收 shell/宿主路径；不实现 URL 抓取、压缩包、PDF/OCR。
- 单文件 1 MiB；UTF-8；每扫描最多 2,000 个允许文本文件、32 MiB；单 chunk 512 个 Embedding tokenizer token、8 KiB，重叠最多 64 token，前缀计入限制。
- 每仓库最多 10,000 chunk、全局 40,000；正文逻辑总量 128 MiB（含保留修订）；Document 元数据 8 KiB、Chunk 元数据 2 KiB。清单是成员行，不是无限 JSON。
- 构建总截止 24 小时；分批 Job 最多 16 chunk/120 秒、失败最多 3 次；恢复不能重置累计进度/截止。
- 原文按 LF 定位，CRLF 不加行；预览最多 500 行/256 KiB，超长行使用 UTF-8 安全字节区间与有界续读。
- UV 管理依赖与锁文件；不预先指定未经验证的版本。只从本地模型目录加载 tokenizer，不下载权重、不加载模型、不信任远程代码。
- `/api/v1`、现有 CSRF/幂等/安全错误；列表默认 30、最多 100；RunEvent 最多 2 KiB，日志不含代码/提示词/密钥。
- 第三阶段不实现事实库重新采集、CI 日志、其他 Agent、写操作、Memory/Report/MCP；旧计划或忽略的准备稿不是设计依据。

## Review Focus

1. 同 SHA 上传被替换：Task 1/3 验证目标代次递增且旧修订立即退出新检索。
2. UI 预览截断被误作完整导入：Task 2 使用原始 blob 分段接口并验证超长行。
3. fork 来源在 base 下被“洗成”普通文件：Task 1/2 保存实际来源及全部依赖权限。
4. 两个导入争抢容量或旧 Job 晚完成：Task 3 原子容量准入及完整 lease fence。
5. 删除与历史引用/预览并发：Task 5 使逻辑失效先于清理，pin 不延长撤权效力。

## 文件职责、阶段入口与共同约定

本计划创建 `backend/src/devflow/knowledge/`：`models.py` 持久来源/导入/容量，`schemas.py` 严格 DTO，`access.py` 多来源权限，`sources.py` Workspace/上传读取，`chunking.py` 纯切块，`service.py` 事务，`handlers.py` Job，`routes.py` API，`retention.py` 正文保留。测试支持集中在 `backend/tests/support/knowledge_env.py`；后续三份计划复用，不各造模型或授权模拟层。

在 Task 1 的实现入口核对 2B 最终 `WorkspaceReader`、读 pin/物理清理 guard、`Queue._owned`、Worker 注册/取消、RepositoryPolicy 与 migration head。预计 `0009_workspace` 在 `0008_sync_repair` 后；`uv run --no-sync alembic heads` 必须显示一个实际 head，才创建计划中的 `0010_knowledge_sources.py`，`down_revision` 指向该实际 head；若编号已用，按实际 head 顺延并同步后三计划，禁止声称未落地迁移已存在。

下列是**本阶段拟新增的契约**，不是声称 2B 已实现。所有 Python 路径相对 `backend/src/devflow/`，测试路径相对 `backend/`；命令在 backend 执行，前端命令在 frontend 执行，Windows 用 `npm.cmd`、Linux 用 `npm`。需要数据库的测试只用现有隔离测试库；服务未提供时记录未执行，不能改连用户库。

每任务 commit 步骤只在 Git 审批能力恢复且任务检查通过后执行：`git add -- <该任务实际文件>`，`git commit -m "<列出的消息>"`；逐文件检查 diff，不夹带第二阶段或他人改动。能力仍不可用时留下未提交记录，不反复重试或绕过。计划编写不运行任何以下命令。

**确定的路径规则 `knowledge-paths-v1`（Task 2实现）：**

- 默认文档是根目录 basename 为 `README`、`README.md`、`README.txt`、`README.rst` 的文件，以及 `docs/` 下 `.md/.txt/.rst`；basename/扩展名允许ASCII大小写变体，实际路径身份仍精确区分大小写、不Unicode归一化。
- 代码仅在用户明确选择的源码目录内，后缀固定允许 `.py,.pyi,.js,.mjs,.cjs,.jsx,.ts,.tsx,.go,.rs,.java,.kt,.c,.h,.cc,.cpp,.hpp,.cs,.rb,.php,.sh,.sql,.vue,.svelte`（ASCII casefold匹配）。无后缀或其他后缀记unsupported，不猜语言/执行解析器。用户选择目录不能绕过下列排除。
- 任意以`.`开头的路径组件排除，明确包括`.git/.env*`及隐藏配置。目录组件按ASCII casefold排除 `node_modules,vendor,venv,env,__pycache__,dist,build,out,target,coverage,htmlcov`；隐藏的`.venv/.next/.nuxt`已由前条覆盖。
- 文件basename按ASCII casefold排除 `id_rsa,id_dsa,id_ecdsa,id_ed25519,credentials,credentials.json,credentials.yml,credentials.yaml,secrets.json,secrets.yml,secrets.yaml`；后缀排除 `.pem,.key,.p12,.pfx,.keystore,.jks`。symlink/gitlink/LFS pointer、不支持编码/含NUL正文、超限文件仍按来源规则拒绝，不依后缀放行。
- 上传规则独立固定为单个`.md/.txt`，绝不因Workspace支持`.rst`或源码而开放上传该类型。排除项有稳定原因码和规则版本；不允许请求覆盖server allowlist。规则变更须新版本/目标代次及重建。这只是范围筛选，**不能识别所有正文中嵌入的秘密**，界面/文档不能称其为秘密检测保证。

### Task 1: 来源、目标代次和共享授权基础

**Files:** Create `knowledge/__init__.py`, `models.py`, `schemas.py`, `access.py`, `service.py`; `backend/migrations/versions/0010_knowledge_sources.py`; `backend/tests/support/knowledge_env.py`, `tests/integration/test_knowledge_sources.py`; Modify `backend/migrations/env.py`，以及真实回执所必需的 `jobs/schemas.py`、`jobs/queue.py` 有限类型/入队支持；执行 handler/Worker 注册仍属 Task 3。

**Interfaces:**
- Consume `Actor(id,github_id,role,auth_version)`、`Lease(job_id,owner,generation,attempt)`、`RepositoryPolicy.require(actor,repo_id,fresh=True)` 与 `local()` 授权 epoch；不得把 tuple 的位置扩散到消费者。
- Produce `SourceGrant(actor:Actor, repository_epochs:tuple[RepositoryEpoch,...])`；`RepositoryEpoch(repository_id:UUID,installation_id:int,user_version:int,installation_version:int,repository_version:int)`；`KnowledgeAccess.verify(actor,repository_ids:tuple[UUID,...],*,fresh:bool)->SourceGrant`、`lock(session:AsyncSession,grant:SourceGrant)->None`。
- Produce `SourceIdentity(repository_id:UUID,source_repository_id:UUID,dependency_repository_ids:tuple[UUID,...],document_id:UUID,revision_hash:str,workspace_id:UUID|None,sha:str|None,path:str|None)`；`ChunkLocation(start_line:int,end_line:int,start_byte:int,end_byte:int)`。SHA/hash 不接受短值，路径遵守 Workspace 规则。
- Produce共享DTO `CoverageView(repository_id:UUID,sha:str|None,target_generation:int,included:int,skipped:int,unsupported:int,over_limit:int,unscanned:bool,complete:bool)`、`RetentionResult(scanned:int,deleted:int,retained:int)`；3B/3C/3D引用这些定义，不重命名复制。
- `KnowledgeService.request_import(actor:Actor,request:ImportRequest,idempotency_key:str)->ImportReceipt(import_id:UUID,job_id:UUID,target_generation:int)`；`ImportRequest` 是 workspace（repository/workspace/path scopes）或 upload（repository ID 必填、document ID 可选、filename、上传暂存 ID）的判别联合，不接受任意 URL/path。

实施顺序澄清：Task 1 回执必须关联真实 `knowledge.import` Job，所需有限 kind/input/result 定义和入队支持随本任务落地，不伪造 ID 或添加占位 handler。Task 3 实现实际执行；中间任务不单独部署。阶段入口核对最终 claim/dispatch，按实际契约处理最小注册依赖。

2026-10-23 暂存澄清：Task 1 提供内部有界 `stage_upload` 和最小 UploadStage 关系，正文只存 canonical DocumentRevision 并计入 128 MiB。暂存绑定 actor/repository/document/revision/filename，PG 创建后 24 小时到期，只能被一个导入消费；已有回执重放仍需当前授权。暂存本身不失效当前资料、不公开未完成修订。`published_at` 标识修订曾被 Task 3 原子发布，普通预览拒绝未发布修订；相同正文复用不抹掉历史发布记录。Task 4 负责 HTTP，Task 5 负责有界清理。通用多源权限锁允许获准成员读取；写入入口另要求管理员，锁内保持当前角色、身份和授权版本匹配。

2026-10-22接口准备裁定：真实kind需同步修改ORM和迁移中的kind/creator约束；普通claim在LIMIT之前只选择已具备handler的有限kind，Task 3加入导入kind时同时交付真实handler。导入Job的requested_by为发起管理员，Import另保存Actor身份/auth_version及来源epoch关系；每批读取重新验证该用户的多来源权限，提交时核对当前用户仍启用且为管理员、原授权版本和全部来源绑定，并持完整lease。共享语料与进度不表示后台可只靠安装权限执行。既有仅解析ProbeResult的Job GET/SSE入口明确仅允许access_probe，知识任务在那里返回404；共享进度由knowledge资源提供，不提前扩大私有Job DTO或增加取消API。最终2B接口仍需在正式入口复核。

- [x] **RED:** 在 `test_knowledge_sources.py` 写 `test_target_generation_and_idempotency`：同 key/同规范请求复用 receipt，不同请求冲突；同 SHA 替换上传 `generation == old + 1` 且 `old_revision.searchable is False`。写 `test_admin_without_repository_access_denied`、`test_fork_identity_survives_dedup`：同正文 hash 不合并权限身份，实际 source/依赖保持。支持 fixture `knowledge_env` 使用真实隔离 PostgreSQL 和受控 provider/Workspace 边界，提供创建 actor/repo/version、撤权和时钟推进。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_knowledge_sources.py -q`；预期缺失新 schema/service 或行为断言失败，不接受环境错误作为 RED。
- [x] **Implement:** 新表 `KnowledgeCorpus`（target generation/SHA/path-rule version）、`KnowledgeImport`（deadline/cursor/counts/status）、`ImportMember`（逐项状态/原因）、`Document`、`DocumentRevision`、`Chunk`、源依赖、引用保留/短读pin关系和容量计数行。先预留 `active_index_version_id` 可空 UUID，3B 加外键；不在此创建虚假 Milvus 状态。唯一键限制重复成员和修订；hash 不替代来源身份。授权新鲜验证在事务外；事务先容量准入锁（确有需要时），再 User→所有 Installation 按 ID→所有 Repository 按 UUID→Job→业务行；最后再验期限。保持与既有单仓库锁顺序一致，安装/仓库重绑定拒绝旧 proof。
- [x] **GREEN:** 上述命令全部 PASS；`uv run --no-sync pytest tests/integration/test_migrations.py tests/integration/test_repository_access.py -q`；迁移测试新增保留第一/二阶段数据和拒绝破坏性 downgrade 的断言。`uv run --no-sync ruff check src/devflow/knowledge tests/support/knowledge_env.py tests/integration/test_knowledge_sources.py migrations/versions/0010_knowledge_sources.py`。
- [x] **Commit:** `feat: add versioned knowledge sources and target generations`。

### Task 2: 原始来源读取、切块与精确预览

**Files:** Create `knowledge/sources.py`, `chunking.py`; `tests/test_knowledge_chunking.py`, `tests/integration/test_knowledge_source_reads.py`; Modify `knowledge/schemas.py`, `backend/pyproject.toml`, `backend/uv.lock`；若 2B 无所需原始读取，仅窄改最终 `workspace/read.py` 和对应测试。

**Interfaces:**
- `SourceReader.read(actor:Actor,identity:SourceIdentity)->SourceBytes(identity:SourceIdentity,body:bytes)`，内部通过已授权 Workspace pin 获取完整允许 blob，上传读规范修订；不调用 UI `file()` 拼接已截断文本。
- `LocalChunker(tokenizer_path:Path).split(source:SourceBytes,title:str)->list[ChunkDraft]`；`ChunkDraft(text:str,embedding_text:str,location:ChunkLocation,content_hash:str,token_count:int,tokenizer_fingerprint:str,chunking_version:str)`。
- `SourceReader.preview(actor,identity,*,start_line:int=1,line_count:int=500,cursor:str|None=None)->SourcePreview`：精确身份/原始行字节范围、partial、next_cursor、旧版/不可用原因。游标绑定 actor/来源/修订，不允许任意 offset 跨源复用。

预览所需短修订读 pin 的获取/释放随本任务实现，复用 Task 1 关系行；Task 5 增加正文清理和对应竞争验证，不延后当前预览必需的保护。

2026-10-23 实现细节裁定：首次 Workspace 导入先用已冻结描述符经受控 raw adapter 读取原始正文，再计算 SHA256 `revision_hash` 并形成完整 SourceIdentity；不以 commit SHA/blob OID 冒充正文 hash。Embedding 前缀固定为按 `path,title` 键序、`ensure_ascii=False`、紧凑分隔符序列化的 JSON，加两个 LF；上传 path 为 null，标题保留原文、有效 UTF-8、无 NUL、最多 8 KiB。前缀及 special tokens 仍计入整体 512 token/8 KiB，不适配则明确失败；策略纳入版本指纹。

短修订 pin 为 PG 时间 30 秒；预览总单调预算 5 秒，包括授权、pin、正文和最终校验，既有物理退出收尾额度不改。游标 15 分钟有效，现有 CursorCodec 使用固定 envelope `limit=1`，将完整来源身份、原 start_line/line_count 和严格整数到期时间签名绑定；不扩大公共 codec 的 200 上限。SourcePreview 包含 identity、text、location、is_current、partial、next_cursor、reason；已授权且确认正文过期/来源删除时分别返回 `source_expired`/`source_unavailable`，正文、位置和游标为 null，partial=false。原文为空与不可用不同。未发布/暂存来源与缺失来源统一安全 404；权限、基础设施错误、identity/version race 和非法游标仍走安全错误。过期正文不自动从 Git 重新获取，历史正文可读不代表仍可检索。

2026-10-23 来源读取交接裁定（R13–R15）：新增内部 `SourceReader.read_import(actor, import_id, *, path=None)->bytes`，从已受理的导入读取完整原始正文；Workspace 必须是已选且允许的精确路径，上传必须绑定 consumed stage/import/member/document/revision 且 path=None。读前后核对新鲜管理员权限、原始来源 epochs、目标代次、导入状态和截止时间；正常 identity read/preview 不开放未发布正文。Stage 消费后的内部读取受导入原始截止控制，Task5 在导入活动期保留其 consumed stage 关系。Task3 才创建真实 Document/Revision 并形成完整 SourceIdentity。

Workspace `source_version` 使用共享 helper：对恰含 `version=knowledge-workspace-source-v1`、`repository_id`、`source_repository_id`、去重排序的 `dependency_repository_ids`、`workspace_id`、`sha`、`path` 的对象，以 `sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False` 生成 UTF-8 JSON 后取 SHA256。UUID 用规范字符串，路径不归一化；它不包含正文 hash/Document ID/代次/授权 epoch，也不代替精确来源比较。上传沿用正文 hash。`is_current` 仅当可用且已发布、searchable 的修订属于当前 target_generation 的 ready/ready_partial 导入 included 成员时为真；通过关系判断，不按时间戳排序，与3B active索引状态分别展示。

- [x] **RED:** `test_chunk_limits_include_prefix_and_special_tokens` 断言每块**路径+标题前缀、正文及special tokens合计**token≤512、embedding_text UTF-8≤8192、overlap≤64，前缀自身装不下则明确失败；`test_path_rules_v1` 参数化上面完整允许/排除集合，显式选择依赖/隐藏目录仍排除、规则版本持久化、Workspace `.rst`允许但上传不允许。`test_crlf_and_long_line_roundtrip` 验证拼回引用覆盖原文区间、CRLF 行号与 UTF-8 多字节续读；`test_raw_read_not_preview` 用 >256 KiB 但≤1 MiB 单行证明完整允许文件被读取，预览≤262144 字节/500 行且 partial。路径 traversal、4097 字节路径、symlink/gitlink/LFS pointer、无效 UTF-8、二进制、1 MiB+1 均显式拒绝/逐项排除，文件正文不进入日志。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_knowledge_chunking.py tests/integration/test_knowledge_source_reads.py -q`。
- [x] **Implement:** 使用本地 `AutoTokenizer` fast offsets，`local_files_only=True,trust_remote_code=False`，不导入推理模型。任务入口用 UV 选择兼容 Python/现有锁的 Transformers/tokenizers 版本，更新并锁定；不从研究稿照搬版本。版本化默认 README/docs `.md/.txt/.rst`、选定源码扩展名、隐藏/凭据/依赖/构建排除；先稳定路径排序再扫描。UTF-8 原文位置为权威，前缀只在检索文本中；标题/空行/代码行边界优先，再按 token 窗口拆长行；不可表示的单片明确拒绝。单次 split 最多生成10,000个 draft；超过则在继续分配前明确返回 over-limit，不能成功返回截断正文，每次迭代必须前进。Task3仍独立核对累计容量。Workspace 原始分段读取复用其 1 MiB、总 deadline、读 pin 与进程 guard，不增加 Git runner 或放宽 API 读取槽。2B UI维持完整LF行预览，首条超长行可能无body/null范围；本阶段的byte区间/cursor在knowledge预览实现，不把2B该响应误报为源文件为空，也不要求2B提前实现bytecursor。
- [x] **GREEN:** 上述测试 PASS；`uv run --no-sync pytest tests/integration/test_workspace_reads.py -q`（仅 2B 最终已存在后）；`uv lock --check`；`uv run --no-sync ruff check src/devflow/knowledge tests/test_knowledge_chunking.py tests/integration/test_knowledge_source_reads.py`。真实本地 tokenizer fixture 独立记录文件指纹；小型 deterministic tokenizer 测试不冒充实际 BGE 切分验证。
- [x] **Commit:** `feat: import bounded source bytes with exact chunk locations`。

### Task 3: 有界导入 Job、覆盖与失效

2026-10-23 删除交接裁定（R21）：本任务提供 deletion 判别请求并复用 `knowledge.import` 真实 handler/回执。准入原子标记 Document.deleted、清空上传 accepted pointer、使全部相关修订不可检索/预览、递增代次并生成剩余完整语料的 Job；当前管理员同时证明被删除来源和组合目标依赖权限。上传及 Workspace 文档都可删除，上传替换仍仅限上传。Workspace 使用已有 base/实际来源/精确路径的稳定 Document tombstone，在后续 SHA/范围扫描中保持排除并逐项记录，不能自动复活或把删除当作 Git 文件写入；不新增恢复 API。Task5 再负责正文回收及 pin/历史保留竞争验证。

**Files:** Create `knowledge/handlers.py`, `tests/integration/test_knowledge_import_jobs.py`; Modify `knowledge/service.py`, `models.py`、`jobs/schemas.py`、`jobs/queue.py`、`worker.py`。

**Interfaces:** `KnowledgeImportHandler.run(lease:Lease)->None` 自己原子完成 Job/事件/进度；`KnowledgeService.import_batch(lease:Lease)->ImportProgress(import_id:UUID,status:str,processed:int,complete:bool)`；`completed_chunks(import_id:UUID)->AsyncIterator[Chunk]` 仅完整有效清单给 3B。Job kind `knowledge.import`；上传正文在有界暂存修订，Job.input 只有 ID/目标代次，不塞正文。

2026-10-23 扫描边界裁定（R18）：本任务拥有固定 SHA 的有界元数据分页/正文前 size 检查接点，可窄改 `workspace/read.py`、`git.py` 及对应测试，复用现有授权、pin、物理 guard、5秒期限和4个读取槽。已有树元数据上限16MiB/100,000记录不变；允许恢复时重复读取这个有界不可变元数据前缀，不允许重读或重复计数已确认正文/chunk。分页须区分 exhausted/truncated，稳定续读，第2,001个合格候选与32MiB剩余额度检查先于正文读取；不新增Git runner或无界整树清单。

2026-10-23 完整语料组合裁定（R19）：每个成功导入的成员清单覆盖全部当前已接受上传，以及当前选定 Workspace/范围的文件，而非仅本次变更。新增仅用于上传的 `Document.accepted_revision_id`，在成功准入时设置/替换、删除时清空，验证修订属于同一文档；暂存不移动指针，未发布但已接受的目标正文受保留保护。保留原 operation 来源字段，另冻结有界 composed target Workspace/SHA/scopes；按固定 target_generation 分页发现已接受上传，不按时间戳选最新版。新增有限 member_kind 区分上传 UUID 与 Workspace 原路径，不能通过加前缀超出4096字节身份上限。

新准入在事务外证明 base 与 proposed Workspace 全部依赖权限，在既定有序锁内重查观察到的代次/选择；变化返回 conflict，不在锁内补权限。保留 fork 目标的上传也须有 fork 权限；新 Workspace 不继承已移除来源。原始 epochs 冻结整个新组合范围；重放保持原 receipt/代次/grant。旧 delta-only 导入须明确区分并要求重新导入，不能悄然扩大授权或作为完整清单。Task3 可窄改 `knowledge/sources.py`，增加绑定当前导入/内部成员/accepted pointer 的内部读取以处理已接受未发布正文，复用短 pin 与读前后权限/期限 fence；不借用他人 Actor、不再次消费旧 stage，普通预览仍拒绝未发布正文。Schema 用实际0010之后的新迁移，不改已审阅0010。

组合回归须覆盖：A完成后B保留A+B；A未发布即B（含不同管理员）；A1→A2→B不复活A1；保留fork目标时准入/发布撤权；Workspace S1→S2保留上传且移除S1专属路径；证明期间目标漂移和原回执重放；部分覆盖不得因上传被误报完整；已接受修订在旧导入被替代/旧stage清理后仍受保护；摘要碰撞不复用不同原始身份；3B式消费者仅得到当前完整policy绑定清单。只在 canonical-ready 后供3B使用 composed target SHA/scopes/epochs，不把上传 mutation 的 sha=None 当作索引目标。

- [x] **RED:** `test_import_batch_resume_keeps_deadline_and_limits`：每次提交≤16 chunks、Job≤120 秒、总24小时与3次失败不随重启重置；`test_partial_scan_is_not_complete`：2001 合法文件/32 MiB 边界记录 included/skipped/unsupported/over_limit、未扫描范围，只有明确覆盖可进入 `ready_partial`；`test_stale_import_cannot_publish` 覆盖 lease owner/generation/attempt/expiry、失权/重新绑定/同 SHA 新代次；`test_concurrent_capacity_admission` 证明两任务不能突破10k/40k/128MiB。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_knowledge_import_jobs.py -q`。
- [x] **Implement:** 文件/成员游标持久化，扫描上限先于读取；单文件失败逐项记录，安全校验/不可验证源/不完整提交禁止发布。源读取和 tokenizer 在事务外，短提交使用 Task 1 ordered grant locks + `Queue._owned` 的完整 Lease 与 DB 时钟；提交 chunks、覆盖、续批 Job、事件一起成功或回滚。拆批失败不重读/重复计数已确认成员；容量逻辑计数并发序列化，停止后释放未使用预留。删除/替换在同事务标旧修订不可检索并递增 target generation。导入完成只说明规范语料就绪，不能显示搜索已就绪。
- [x] **GREEN:** 聚焦测试及 `uv run --no-sync pytest tests/integration/test_queue_leases.py tests/integration/test_job_transactions.py -q` PASS；`uv run --no-sync ruff check src/devflow/knowledge src/devflow/jobs src/devflow/worker.py tests/integration/test_knowledge_import_jobs.py`。
- [x] **Commit:** `feat: process knowledge imports with durable limits and fencing`。

### Task 4: 导入、修订和覆盖 API/UI

2026-10-23 上传传输裁定（R20）：新增内部 POST `/repositories/{id}/knowledge/uploads` 暂存交换，raw `application/octet-stream` 正文按 Request.stream 实际字节先限1MiB；filename/可选上传 document UUID 为单独有界 query 元数据（原 query≤8KiB）。暂存需要 CSRF、当前管理员+仓库权限和持久幂等键，同 actor/repo/key 的精确原请求在原24小时窗口复用 stage，不重复创建文档/计费正文；返回 stage ID/PG expiry，明确不是 Job。随后原 JSON imports/replacements 才返回 ImportReceipt。客户端取得 stage 后始终以原 stage UUID/准入 key 重放丢失的准入响应，包括 stage 已过期/清理时；不重新暂存。新增模型字段/约束采用 Task3 实际 head 之后迁移。过期暂存不承诺永久重放，Task5 清理仍保护活动导入关系与 accepted revision；不能依赖进程内缓存或引入第二份正文。

2026-10-27 实施记录：Task3 独立审查通过后按本计划实现。新增 `knowledge/queries.py`（有界只读投影）、`knowledge/routes.py`（六个端点）、迁移 `0012_knowledge_stages`（stage 幂等键/request_hash/purpose/document_id/body_bytes 及唯一索引）、`errors.py` 增加 `stage_expired`/`payload_too_large`。`stage_upload` 保持原签名返回 UUID，新增 `stage_upload_http` 返回 `StageReceipt`。后端 `tests/integration/test_knowledge_routes.py` 14 项通过；受影响回归 130 项通过；Ruff/format 通过；`alembic heads` 为 `0012_knowledge_stages`。前端 `typecheck`/`lint`/`build` 通过（构建需 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 以允许 Next 清理自身 `.next/turbopack` 缓存）。Playwright 规格已编写，本机无 Chromium 二进制，浏览器执行结果见验收记录。

2026-11-02 复核：安装 Playwright Chromium 后实际执行 `tests/knowledge-ingestion.spec.ts`，4 项全部通过。期间修复知识页在空 Workspace ID 下仍请求 `/workspaces/` 与 `/knowledge/paths`、404 触发仓库级 `access.revoke()` 的问题：`useRepositoryQuery` 新增受控 `enabled`，版本/路径/文档查询只在有效输入后启用。`typecheck`、`lint`、`build` 重新通过；后端路由测试 14 项通过。

2026-11-02 Task 4 独立审查：无 Critical；2 Important + 4 Minor + 1 测试缺口，已全部修复。Important：写入成功后重置分页/选择并失效概览与文档查询，修复代次变化后的 stale cursor 死循环；`0012` 降级在存在 keyed stage 时拒绝丢弃重放信息。Minor：知识语料状态纳入自动轮询；空路径选择在 UI 禁用并在 DTO 拒绝，预览使用显式空文档集合；写入后失效文档详情缓存；上传路由在缓冲前校验文件名/UTF-8。测试缺口：新增应用内切仓 + 延迟旧响应 E2E。最终 E2E 9 passed，后端 route/source/migration 54 passed，knowledge+config 188 passed（1 项既有 2B 原生 Workspace 不稳定失败，隔离重跑通过），迁移/队列/Worker/Workspace 65 passed。Task 4 任务级审查完成；复审确认七项修复均有效，无新缺陷；3A 整体审阅仍未完成。

**Files:** Create `knowledge/routes.py`, `tests/integration/test_knowledge_routes.py`; `frontend/src/features/knowledge/types.ts`, `queries.ts`, `knowledge-page.tsx`, `frontend/src/app/(app)/repositories/[repositoryId]/knowledge/page.tsx`, `frontend/tests/knowledge-ingestion.spec.ts`; Modify `backend/src/devflow/app.py`, `frontend/src/features/repositories/repository-detail.tsx`。

**Interfaces:** GET `/repositories/{id}/knowledge`；POST `/repositories/{id}/knowledge/imports`；GET `/repositories/{id}/knowledge/documents/{document_id}`（分页修订/有界预览）；POST 同文档 `/replacements`、`/deletions` 创建有界 mutation Job。所有 POST 继承 CSRF/idempotency，返回 `ImportReceipt`/Job ID；共享进度从仓库知识资源读取，不暴露他人的私有 Job。

- [x] **RED:** API 覆盖 admin+repo 双条件、同 key冲突、上传真实流量超过1MiB提前拒绝（不信 Content-Length）、filename/URL不成为抓取命令、分页30/100。`test_upload_only_md_txt_at_route_and_service` 对路由和直接service分别断言`.md/.txt`有效UTF-8允许，`.rst/.py/.pdf/.zip`及无效UTF-8拒绝，伪造MIME不能绕过；替换接口同规则。Playwright `test_preview_confirm_paths_and_shared_upload_notice` 断言确认默认完整SHA/路径清单、共享说明、部分覆盖；`test_switching_repository_clears_old_knowledge` 断言切仓/退出/撤权与晚响应不能恢复旧正文。
- [x] **Run RED:** backend `uv run --no-sync pytest tests/integration/test_knowledge_routes.py -q`；frontend `npm.cmd run test:e2e -- tests/knowledge-ingestion.spec.ts`。
- [x] **Implement:** UI 先预览并确认已解析 SHA/路径，不默默扫描全部代码；渲染规则版本、实际导入/排除/未扫描、修订与安全原因、未建索引状态，无内部磁盘路径。上传说明资料将供该仓库获准成员使用；Markdown 先用已有安全转义展示，不引入渲染器执行 HTML。查询缓存键含 actor/repo/修订，响应 private/no-store。实现前读取 `frontend/AGENTS.md` 要求的本地 Next 文档。
- [x] **GREEN:** 上述命令 PASS；frontend `npm.cmd run typecheck`、`npm.cmd run lint`、`npm.cmd run build`；backend Ruff 覆盖实际改动。
- [x] **Commit:** `feat: expose knowledge imports and revision previews`。

### Task 5: 正文保留、来源失效与 3A 交付

2026-10-23 维护入口裁定（R22）：沿用 Worker 现有60秒维护分支，为有候选资料的仓库创建真实系统 `knowledge.cleanup` Job；一次发现≤100仓库，含仅暂存/已撤权仓库，同仓库最多一个未终态 cleanup Job，DB锁/唯一键保证多 Worker/重启一致。Job kind/creator/input/result/可执行注册与实际迁移随本任务落地，单批全部类别合计≤100候选。既有队列三次尝试、完整 lease 与结果/容量/完成原子提交保持；终态失败保留原 Job，至少1小时PG冷却后才能创建新的周期维护批次，不重开或重置旧 Job，不影响导入原24小时/累计三次失败限制。正常成功批次最早60秒后继续，无候选不制造空 Job；不新增外部定时器、永久故障闩锁或运维重试 API。

2026-11-02 实施记录：新增 `knowledge/retention.py`（`KnowledgeRetention.sweep`、`retain_sources`、`KnowledgeMaintenance` producer）、严格 DTO `KnowledgeCleanupInput/Result`、迁移 `0013_knowledge_cleanup`（Job kind/creator 约束）并注册 `knowledge.cleanup` 到 Queue/Worker。`sweep` 在全局容量→仓库容量→Job→Corpus→Document→Revision 锁序下重新检查 accepted upload、活动导入成员、未消费 stage、当前语料、Workspace 预重发现路径、retention 和 pin；仅当正文/分块实际清除时同步扣减容量，revision 身份与 provenance 保留。producer 每 60 秒轮转≤100 仓库，同仓库只保留一个非终态 Job，成功后 60 秒、终态失败后 1 小时 PG 冷却，游标取最近一次成功批次结果。测试：`test_knowledge_retention.py` 9 项、`test_knowledge_ingestion_flow.py` 2 项、迁移测试 14 项通过；完整 knowledge/config 过滤套件 188 passed / 0 failed；迁移/队列/Worker/Workspace 回归 64 passed；Ruff/format 通过；实际 head 为 `0013_knowledge_cleanup`。Playwright 知识页流程已在 2026-11-02 实际执行 4/4 通过；真实本地 BGE 权重/推理、原生 Git、Docker/Linux/恢复、真实 GitHub/云模型仍为 NOT EXECUTED。

2026-11-02 独立审查：无 Critical；2 Important + 2 Minor，均已修复并加回归测试。Important 1：producer 移除粗粒度 Workspace SQL 保护，改为 100 行有界精确路径扫描与 per-repo 游标。Important 2：`materialize`/`_stage` 可在同一身份下恢复 `expired` 无正文修订（`deleted` 不复活），并恢复容量记账。Minor 1：配置拒绝心跳 ≤5 秒。Minor 2：成功冷却从完成时刻起算。Task 5 任务级审查已完成；复审确认四项修复均有效且未引入新缺陷。

2026-11-02 3A 整体审阅：结论 **NO**（无 Critical；2 Important + 5 Minor），详见 `.superpowers/sdd/2026-10-20-devflow-knowledge-ingestion/3a-review.md`。Important 1：修订历史单页无界，改为签名 keyset 分页（默认 30/上限 100）、`revisions_next_cursor`，显式 `revision_id` 可读取当前页之外的已发布修订。Important 2：已回收的已发布修订从历史消失，改为按 `published_at` 保留并以 `availability`/`source_expired` 明示。Minor：`entry.size` 为空时以 `unknown_size` 跳过；概览/详情仅暴露当前语料依赖集内的来源；发布新清单时清除清单外旧修订的 `searchable`；未发布文档不出现在概览；`KnowledgeImport.source_repository_id` 按计划保留 operation 来源，不改。回归：新增路由用例 3 个、Workspace 大小未知用例 1 个、重新导入 searchable 断言；3A 合并聚焦集 129 passed，Ruff 178 files formatted，单 head `0013_knowledge_cleanup`，前端 E2E 9 passed、typecheck/lint/build PASS。GREEN/交付保持未勾选，等待原审阅者复审确认。

2026-11-02 3A 整体复审：原审阅者确认 **GREEN / 交付（本地范围）**。I-1、I-2、M1、M2、M3、M5 六项修复均 CONFIRMED；M4 确认为计划内设计（不修改）。无新的 Critical 或 Important。复审独立复跑：聚焦新/修复用例 6 passed（14.11s）、3A 合并聚焦集 129 passed（144.10s）、Ruff knowledge 代码/测试通过、单 head `0013_knowledge_cleanup`、前端 typecheck PASS、E2E 9 passed（14.2s）。复审遗留的非阻塞观察（不改变本次结论）：(a) 概览元数据仍只按 `Document.source_repository_id` 过滤，base 来源但历史修订依赖未授权 fork 的文档可能出现在列表元数据中，详情/预览已正确拒绝，后续如需严格对称可加修订依赖 EXISTS 过滤；(b) `ImportMember.revision_id` 可空且无 DB 约束绑定 `status='included'`，当前代码路径始终写入并由 publish/completed_chunks 校验；(c) `3a-review.md` 中 I-1 的“single unbounded page”措辞与实际修复前的 20 行上限不符，属文档措辞问题。以上均不阻塞 3A 本地交付；外部门槛（真实 GitHub App/Webhook、原生 Git、Docker/Linux、恢复演练、真实 BGE 推理、云模型）继续 NOT EXECUTED。

**Files:** Create `knowledge/retention.py`, `tests/integration/test_knowledge_retention.py`, `tests/integration/test_knowledge_ingestion_flow.py`, `docs/operations/knowledge-acceptance.md`; Modify `jobs/models.py`, `jobs/queue.py`, `jobs/schemas.py`, `worker.py`, `migrations/versions/0013_knowledge_cleanup.py`（原计划的 `knowledge/models.py`/`handlers.py` 无需改动）。

**Interfaces:** `KnowledgeRetention.sweep(lease:Lease,*,limit:int=100)->RetentionResult`；`retain_sources(session,owner_kind:Literal['query','message'],owner_id:UUID,revision_ids:tuple[UUID,...],expires_at:datetime)->None` 用有截止的关系行供3C/3D保留，不保存无限JSON；`SourceReader.preview` 用短 reader pin，在 finally释放。kind `knowledge.cleanup` 复用队列。

- [x] **RED:** `test_delete_beats_retention_and_pin`：删除/撤权立即拒绝正在/随后预览；`test_retained_revision_not_deleted_for_capacity`：未过期引用正文/active corpus不可删，128MiB满返回storage_exhausted；过期无读者旧正文可回收、身份保留为source_expired。`test_ingestion_flow`：上传→替换→Workspace导入→部分覆盖→旧来源预览→撤权，全部真实PG/受控源。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_knowledge_retention.py tests/integration/test_knowledge_ingestion_flow.py -q`。
- [x] **Implement:** 单批最多100候选，DB失效先于物理清理；读者 pin短期限，回收不长期pin整个Git库。未来消息默认90天保留由3D调用，不凭空在3A创建会话。清理损坏/丢失源明确source_unavailable，与到期和删除区别；写验收文档记录命令、fixture/真实资源类别、结果和未执行项。
- [x] **GREEN/交付:** 运行本阶段全部 `test_knowledge_*.py`（使用 `uv run --no-sync pytest tests/test_knowledge_chunking.py tests/integration -k knowledge -q`）及 Ruff；迁移保留既有数据、聚焦前端流程一次通过。报告真实 Workspace/本地 tokenizer 的实际证据，缺资源写 NOT EXECUTED。按 A 方法完成任务审阅及本阶段整体审阅，再移交 [3B](2026-10-20-devflow-local-index.md)；不把3B能力列为3A已通过。
- [x] **Commit:** `test: verify knowledge source lifecycle and ingestion delivery`。
