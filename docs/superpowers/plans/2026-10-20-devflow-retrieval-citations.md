# DevFlow 3C Retrieval and Citations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在明确选入且当前授权的来源范围内，提供版本固定、混合召回、本地重排、精确引用的独立检索。

**Architecture:** PostgreSQL 冻结有效 index/代次和来源权限，Milvus只返回候选ID/分数；DB复核后才读正文和重排。独立查询通过模型 Worker 的 `knowledge.query` Job；同一检索函数供3D父Job直接调用。引用manifest由服务端生成，历史预览与当前active检索分别授权。

**Tech Stack:** 3A/3B现有组件、FastAPI、Next.js/React Query、pytest/Playwright；无新检索服务或第二关键词库。

**Spec:** [已批准规格](../specs/2026-10-19-devflow-retrieval-design.md) §4–5、7、9–12；前置 [3A](2026-10-20-devflow-knowledge-ingestion.md) / [3B](2026-10-20-devflow-local-index.md)。用户于2026-10-20确认本计划，执行方式A不变；2B及3A/3B先完成，确认不等于运行验收。

## Global Constraints

- 最多3个明确选入的来源仓库，按实际去重集合计数；实际source/base/依赖都必须在集合内且有权，fork源可以只参与授权而无需独立索引。
- 授权短缓存最长60秒；提交和执行前fresh验证，正文读前后检查本地epoch；无法验证拒绝，已观察撤权立即失效。
- 问题最多2 KiB UTF-8、256个Embedding tokenizer token；检索20秒，单次Milvus5秒。
- BM25/dense相同server-side过滤，各30候选；RRF `k=60`；前20本地rerank；补取最多1次、每路最多60。
- rerank batch1，query+passage含special tokens≤1024；禁止静默截断；最终≤6条、同文件≤2条、重复片段去重。
- 版本变化最多整体重取1次，再变返回 `version_changed`；无有效证据 `no_evidence`，基础设施故障 `retrieval_unavailable`，不能混同。
- manifest≤64 KiB；私有query结果≤128 KiB/条、保留1小时、每用户最多20条，超额回收最旧已完成结果。
- 预览≤500行/256 KiB；长行UTF-8安全区间+原始行/字节/partial/cursor；不生成可抓取的模型URL。
- 30题以上合成固定集，Recall@6≥0.85，分组nDCG@6记录；真实模型质量与fake算法测试分开，不声称真实私库达标。
- 沿用UV、Job/Lease、同源CSRF/幂等、private/no-store和安全日志；外部tracing关闭，CI原始日志不进入检索或云上下文。

## Review Focus

1. 用户有fork权限但只选base：Task 1/2仍排除真实fork内容，提示显式加入。
2. 同SHA目标generation变了：Task 1不能默认回退旧上传；明确旧SHA仅在仍active时可查。
3. 候选返回后撤权/换active：Task 2/3禁止读取、发布混合版本证据。
4. 长行引用、重叠chunk、同文档不同来源混淆：Task 3/5以精确身份和原始区间处理。
5. 浏览器旧查询晚到或SSE回放：Task 4不得在另一用户/仓库范围显示旧证据。

## 文件职责与共同接口

新增 `backend/src/devflow/knowledge/retrieval.py`（冻结范围/召回/重排）、`citations.py`（manifest/预览）、`queries.py`（私有查询持久化/Job）；扩展既有models/schemas/routes/handlers。测试复用3A `knowledge_env`；实际Milvus使用3B fixture。遵循3A命令目录、隔离数据库、逐文件commit和Git不可用规则。

`QuerySelection` 包含 `selected_repository_ids:tuple[UUID,...]` 和 `indexes:tuple[IndexSelection,...]`；后者只描述真正检索的索引，每项 `IndexSelection(repository_id:UUID,sha:str,target:Literal['target','active'])`。两集合不得混为一谈：选择base+fork，indexes可以只有base，fork源SHA来自成员的 `SourceIdentity`。

`QueryScope(actor:Actor,grant:SourceGrant,selected_repository_ids:tuple[UUID,...],indexes:tuple[FrozenIndex,...])`；`FrozenIndex(repository_id:UUID,index_version_id:UUID,sha:str,index_generation:int,target_generation:int,member_digest:str)`。index_generation是被查索引的代次，target_generation是冻结时Corpus目标代次，显式选择旧SHA时二者可能不同。`RetrievalContext(lease:Lease,scope:QueryScope,deadline:datetime,refresh_used:bool=False,reserve_attempt:Callable[[bool],Awaitable[None]]|None=None)` 只由服务端创建，不作为HTTP输入。可选callback参数表示是否为版本变化后的整体重取；3D用它持久扣除同run检索次数，独立查询不需要该回调。

### Task 1: 显式来源选择与有效索引冻结

**Files:** Create `knowledge/retrieval.py`, `tests/integration/test_retrieval_scope.py`; Modify `knowledge/schemas.py`, `access.py`。

**Interfaces:** `RetrievalService.freeze(actor:Actor,selection:QuerySelection,*,fresh:bool=True)->QueryScope`；`revalidate(scope:QueryScope,*,fresh:bool=False)->None`。调用3A `KnowledgeAccess`；只接受3B DB active有效版本，不用Milvus存在性授权。

- [x] **RED:** `test_fork_authorization_does_not_expand_selection`：base A成员来自B、有B权限但只选A仍不可纳入；A+B允许且B无需索引；A/B任一失权拒绝。`test_target_generation_not_sha_is_freshness`：同SHA新上传generation待发布返回index_not_ready；新SHA待建时显式active旧SHA可查，默认target不可回退；旧索引retired后不得新检索。`test_scope_three_limit_counts_source_only_repositories`：去重后的4仓库拒绝，索引仓库必须在显式集合内。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_retrieval_scope.py -q`。
- [x] **Implement:** 严格DTO拒绝空scope/重复或歧义选择，不允许用户传filter/epoch/任意index UUID来规避选择；安装授权不是用户scope。冻结每个真实index身份/目标代次，附实际来源信息但不补仓库；必要来源未选提示需显式加入，不泄露无权源详情。fresh验证事务外，获取/读后本地epoch比对；排序锁复用3A。
- [x] **GREEN:** 聚焦测试及3A `test_knowledge_sources.py`、3B `test_index_publication.py` PASS；Ruff实际改动。
- [x] **Commit:** `feat: freeze explicitly selected authorized retrieval scopes`。

### Task 2: 混合召回、DB复核、RRF与本地重排

**Files:** Modify `knowledge/retrieval.py`, `schemas.py`; Create `tests/test_retrieval_ranking.py`, `tests/integration/test_retrieval_pipeline.py`。

**Interfaces:** `RetrievalService.retrieve(context:RetrievalContext,question:str)->EvidenceSet`；`EvidenceSet(scope:QueryScope,status:Literal['evidence','no_evidence'],evidence:tuple[Evidence,...],coverage:tuple[CoverageView,...])`；`Evidence(identity:SourceIdentity,chunk_id:UUID,index_version_id:UUID,location:ChunkLocation,text:str,content_hash:str,rank:int)`。重用3B `LocalInference.embed/rerank`、`MilvusIndex.search`；不同父Job可传相同类型，函数绝不提交子Job。

- [x] **RED:** `test_rrf_k60_dedup_then_top20` 用已知rank断言 `sum(1/(60+rank))`、稳定chunk-ID tie-break；`test_identical_filters_and_one_refill` 断言两路30、过滤后至多一次60，未经DB授权正文reader调用次数0。`test_question_and_pair_limits` 2KiB/256及1024边界、不得静默truncate；`test_version_changed_once_then_stop` 验证第一次换active整体重取、再变报version_changed；`test_timeout_is_not_no_evidence` 模型/Milvus故障与真正空集合区别。结果断言len≤6、每精确文件身份≤2、重复区间不重复。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_retrieval_ranking.py tests/integration/test_retrieval_pipeline.py -q`。
- [x] **Implement:** deadline同时限制排队后单次检索全流程，Milvus子调用≤5秒；初始检索及版本变化的整体重取开始前各调用一次 `reserve_attempt(restarted)`（如提供），避免图内重取绕过3D累计2次预算；候选过滤补取仍属于同次检索。先query本地编码，两路只拿ID，再join IndexMember/Chunk/Revision检查目标及授权/实际来源集合后取正文。RRF以同index/chunk身份合并；去重不能抹掉不同来源权限。rerank query+passage含special tokens超过1024时明确拒绝该不可用证据并要求按3A边界重建，不能临时截断后保留旧定位。最多整体重取1次且沿用剩余20秒；最终发布者再次revalidate。图内调用沿用父Lease/owner物理槽，不再申请子队列槽。
- [x] **GREEN:** 聚焦测试、3B两路契约与模型Worker测试PASS；Ruff。实际模型的rank不是fake测试预期的固定常数。
- [x] **Commit:** `feat: retrieve and rerank bounded version-checked evidence`。

### Task 3: 私有查询、服务端manifest与来源预览

**Files:** Create `knowledge/citations.py`, `queries.py`, `backend/migrations/versions/0012_knowledge_queries.py`, `tests/integration/test_knowledge_queries.py`, `tests/integration/test_knowledge_citations.py`; Modify `knowledge/models.py`, `schemas.py`, `handlers.py`, `routes.py`, `jobs/service.py`, `jobs/events.py`, `jobs/schemas.py`, `worker.py`, `backend/migrations/env.py`。

**Interfaces:** `QueryService.submit(actor:Actor,selection:QuerySelection,question:str,key:str)->QueryReceipt(query_id:UUID,job_id:UUID)`、`run(lease:Lease)->None`、`get(actor:Actor,query_id:UUID)->QueryView`；`CitationService.manifest(evidence:EvidenceSet)->EvidenceManifest`、`preview(actor:Actor,manifest:EvidenceManifest,citation_id:str,*,cursor:str|None=None)->SourcePreview`、`validate_ids(manifest,ids:Sequence[str])->None`。`EvidenceManifest(entries:tuple[CitationEntry,...])`；每Entry有短citation_id+Evidence精确身份/位置/服务端URL；正文只在受限结果中，不嵌进事件。

- [x] **RED:** `test_query_private_job_and_sse` 两用户、猜ID、回放游标、撤任一参与仓库均拒绝；`test_citation_identity_stable_and_unknown_rejected` 相同身份/区间稳定ID、碰撞检测、不接受未知ID或模型URL；`test_old_citation_survives_vector_gc_but_not_source_revoke` 历史修订可预览、已删source_unavailable、到期source_expired。`test_query_storage_caps` manifest64KiB、结果128KiB、1小时/每用户20条，21st仅回收最旧已完成，不删active读者数据。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_knowledge_queries.py tests/integration/test_knowledge_citations.py -q`。
- [x] **Implement:** 实际单head预检后建立0012（依赖实际3Bhead）、Query/result与有界scope成员行。Job `knowledge.query` 创建者私有，在JobService/Events的有限kind分支检查所有选入仓库，不只anchor repository；handler由3B唯一模型worker执行。提交/执行fresh，读结果/引用前后验证epoch，发布完整Lease+scope+active事务。manifest短ID由不可变证据身份hash派生并检测碰撞，URL由Git来源构造或上传修订路由。保留旧正文调用3A `retain_sources(owner_kind='query',expires_at=+1h)`；清理有界且不清除有效Job权限审计。
- [x] **GREEN:** 聚焦测试 + `uv run --no-sync pytest tests/integration/test_job_visibility.py tests/integration/test_sse.py tests/integration/test_migrations.py -q`；Ruff。测试迁移保留先前阶段数据。
- [x] **Commit:** `feat: persist private queries and exact source citations`。

### Task 4: 独立检索和版本化引用界面

**Files:** Create `frontend/src/features/knowledge/search-page.tsx`, `citation-preview.tsx`, `frontend/src/app/(app)/knowledge/page.tsx`, `frontend/tests/knowledge-search.spec.ts`; Modify `knowledge/routes.py`, `frontend/src/features/knowledge/types.ts`, `queries.ts`, `knowledge-page.tsx`, `frontend/src/features/session/shell.tsx`。

**Interfaces:** POST `/knowledge/queries`→QueryReceipt，GET `/knowledge/queries/{id}`→QueryView；GET `/knowledge/queries/{id}/citations/{citation_id}`→SourcePreview。DTO区分 `selected_repository_ids` 与 `indexes`，UI复用同一QuerySelection供3D选择器；source-only仓库无需假造索引。

- [x] **RED:** `npm.cmd run test:e2e -- tests/knowledge-search.spec.ts` 前先写：`test_source_only_fork_selection_is_explicit` 显示base/head及源SHA、未选B不能检索A的B片段；`test_search_without_cloud_configuration` 本地查询正常、云未配不阻止；`test_old_result_cannot_replace_new_scope` 晚响应、返回导航、退出/撤权清空；长行续读只显示返回区间/partial、旧版提示与coverage可见。
- [x] **Implement:** 读取本地Next文档，列表30/100、结果最多6、不显示分数为事实概率。显示target/active SHA、部分范围/排除/未就绪原因，选择旧SHA必须明确。private/no-store；query keys绑定actor+选入scope+index/版本+query ID；缓存清除包含query/citation/job相关范围，取消浏览器请求不等于取消后端Job。引用仅服务端预览，不执行URL抓取/HTML。
- [x] **GREEN:** 同E2E命令PASS；`npm.cmd run typecheck`、`npm.cmd run lint`、`npm.cmd run build`；后端引用路由聚焦测试PASS。
- [x] **Commit:** `feat: show independent retrieval and versioned evidence`。

### Task 5: 固定集质量、权限端到端与 3C 验收

**Files:** Create `backend/tests/fixtures/knowledge/quality.json`, `backend/tests/fixtures/knowledge/repos/`（仅合成文本）, `backend/tests/test_retrieval_metrics.py`, `backend/tests/integration/test_retrieval_quality.py`, `backend/tests/integration/test_retrieval_flow.py`, `frontend/tests/retrieval-flow.spec.ts`; Modify `docs/operations/knowledge-acceptance.md`。

**Interfaces:** 测试内 `score_questions(gold,results)->QualityReport`；gold是不可变文档/SHA/原始区间，不能用实现产生的chunk IDs倒推gold。真实质量runner复用Task2服务，不另写检索算法。

- [x] **RED:** `test_macro_recall_and_ndcg_deduplicate_intervals`：两个可答问题命中比1/2与1/1得macro0.75，重叠chunk不加分，无答案题不入Recall分母但统计误命中/拒答。固定集≥30题覆盖中文/英文/混合/代码标识/路径/无答案/旧SHA/隔离仓库。`test_retrieval_flow_revocation_and_version_switch` 真PG/真实Milvus模式证明旧执行者、fork漏选、源删除、重启、引用及SSE均无越权。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_retrieval_metrics.py tests/integration/test_retrieval_flow.py -q`；真实模型/Milvus环境就绪后 `uv run --no-sync pytest tests/integration/test_retrieval_quality.py -q`；frontend `npm.cmd run test:e2e -- tests/retrieval-flow.spec.ts`。
- [x] **Implement:** 固定标注先于测量；比较BM25/dense/RRF/RRF+rerank，按同一gold/去重规则记录每组Recall@6和nDCG@6。真实RRF+rerank目标≥0.85；重排退化具体定位语言/标识符/analyzer或切块并版本化重建复测，不仅报整体平均。引用身份/定位/未知ID拒绝/权限/选入范围全部必须正确，样本不支持的结论标缺口。
- [x] **GREEN/交付:** 上述可用环境检查PASS并记录真实模型/索引fingerprints；缺硬件不得以fake rank宣布质量达标。完成UV锁/Ruff及本阶段聚焦前端验证、A方式任务与整体审阅；真实私有仓库、正式并发/硬件仍外部NOT EXECUTED。向 [3D](2026-10-20-devflow-private-chat.md)交付Scope、retrieve、manifest/preview和Job可见性契约。
- [x] **Commit:** `test: verify retrieval quality and authorization boundaries`。
