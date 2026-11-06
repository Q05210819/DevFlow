# DevFlow 3D Private Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付私有、证据可追溯、预算持久化且可安全恢复的受控 ChatAgent。

**Architecture:** Conversation/Message、AgentRun/ToolCall 和费用账本在PostgreSQL，既有 `chat.run` Job由唯一模型Worker完整执行。LangGraph只持业务引用和有界流程状态，LangChain只处理模型消息/tool calling；图内直接调用3C检索，外部调用与checkpoint回放通过持久幂等记录分离。官方Postgres saver写入和accepted指针共用同连接、同短事务、同lease/auth fence。

**Tech Stack:** UV、LangGraph/官方Postgres saver、LangChain ChatOpenAI兼容适配、现有psycopg/PostgreSQL、3A–3C本地检索、Next.js；无子Agent、记忆服务或第二队列。

**Spec:** [已批准规格](../specs/2026-10-19-devflow-retrieval-design.md) §5–13；前置 [3C](2026-10-20-devflow-retrieval-citations.md)及其3A/3B契约。用户于2026-10-20确认本计划；沿用A，2B与3A–3C先完成。确认不授权实际云消费，也不表示第三阶段测试已获运行结果。

## Global Constraints

- Conversation创建者私有，固定显式1–3来源仓库；变更集合新建会话。每轮实际index/SHA与真实source SHA另固定；失任一参与仓库权限时历史/引用/Job/SSE一并拒绝。
- 当前云上下文仅系统提示、当前问题、本轮命中证据及本轮工具协议；旧消息/附件/事实正文/CI日志不自动加入。
- 唯一模型Worker串行执行完整chat（含HTTP等待）；图内复用父Lease/授权/推理槽，禁止enqueue并等待子模型Job。
- 每会话最多1活动run、全局聊天并发1；最多2模型请求、2次检索（含初次）、8次图节点；执行180秒、排队最长10分钟，累计预算不因重启清零。
- 固定 `qwen3.8-max`、`https://dashscope.aliyuncs.com/compatible-mode/v1`、Chat Completions；关闭thinking，`max_completion_tokens=1024`；完整JSON请求体（不含HTTP头）≤24 KiB，8192输入token仅估算目标；输出JSON≤16 KiB。
- 实际请求逐次预占1,000,000输入token+1,034输出token对应的配置费用；不硬编码价格或默认货币预算，不把10token偏差漏掉。
- 真实调用默认关闭，需本地轮换密钥、正run/月预算、币种、有效定价/参数版本和实际适配验证；本规格/计划不授权实际消费。
- SDK重试关闭；明确未发送失败最多重试1次且计入2请求总限；已发送结果未知不重发，预占不因取消/删除/回放释放。
- 每checkpoint≤64 KiB、每run≤32；终态不可恢复、accepted retired/清空；完整thread数据（含pending writes/blobs）≤24小时、全局128 MiB。
- 会话≤100/用户、消息≤200/会话、保留90天、正文+最终manifest全局256 MiB；manifest≤64 KiB；ToolCall中间结果≤128 KiB/条、≤4/run、终态24小时。
- 费用/幂等元数据≤4 KiB/条、全局32 MiB，不含提示正文；已结算且run终态保留90天后可清，月汇总保留，未知记录待对账不按年龄释放。
- RunEvent≤2 KiB仅安全状态；日志/审计不记提示、代码、原始响应、密钥、向量；tracing关闭，不直接SSE流式暴露未校验token。

## Review Focus

1. 第二轮证据被剔除却仍被引用：Task 3只允许最终实际请求manifest，完整块装配。
2. checkpoint恢复导致重复云请求/步骤预算归零：Task 2/5独立durable调用记录与累计计数。
3. saver内pipeline另提交或pending writes绕过fence：Task 4真实同连接回滚/失租测试。
4. cancel/reap使Job终态但accepted仍可恢复：Task 4/5在worker有效lease或控制器CAS撤权的原子事务内同步retire，清理只处理retired。
5. 历史、SSE或React Query缓存绕过撤权：Task 1/6全部使用相同会话scope授权。

## 文件职责、依赖与共同类型

新增 `backend/src/devflow/chat/`：`models.py` 持久实体，`schemas.py` DTO，`service.py` 私有提交/读取/终态，`budget.py` 账本，`context.py` 装配，`provider.py` 固定云适配，`checkpoints.py` 官方saver薄包装，`graph.py` 扁平图，`handlers.py` 任务入口，`retention.py` 有界清理，`routes.py` API。不建立通用Agent平台。3C的 `QuerySelection/QueryScope/RetrievalContext/EvidenceManifest` 原样复用。

执行前复核实际2B及3A–C接口，不读取旧忽略候选稿代替规格。预计迁移 `0013_private_chat.py` 在0012之后，Task 1先 `uv run --no-sync alembic heads`，实际单head不符就顺延并修正引用。官方saver自身schema通过Task 4显式部署setup建立，不手工复制其迁移SQL。

命令/隔离测试库/逐文件commit/Git不可用处理同3A。Task 3用UV选择兼容Python3.12、现有psycopg、LangGraph和LangChain的稳定发布版并写锁；源码研究版本不是已验证安装版本。Task 4再按实际锁定版本核对saver签名/SQL，不允许部署浮动main。前端动代码先遵循 `frontend/AGENTS.md`。

### Task 1: 私有会话、持久run和Job可见性

**Files:** Create `chat/__init__.py`, `models.py`, `schemas.py`, `service.py`, `backend/migrations/versions/0013_private_chat.py`, `tests/support/chat_env.py`, `tests/integration/test_chat_privacy.py`; Modify `jobs/schemas.py`, `jobs/service.py`, `jobs/events.py`, `backend/migrations/env.py`。

**Interfaces:** `ChatService.create(actor:Actor,selected_repository_ids:tuple[UUID,...],key:str)->ConversationView`；`submit(actor:Actor,conversation_id:UUID,selection:QuerySelection,question:str,key:str)->RunReceipt(run_id:UUID,job_id:UUID,message_id:UUID)`；`history(actor,conversation_id,*,cursor=None,limit=30)->MessagePage`；`authorize(actor,conversation_id,*,fresh:bool)->SourceGrant`。selection.selected集合必须等于conversation集合，indexes允许本轮选对应active/target。

Conversation创建是同步短元数据事务，返回ConversationView：仍要求CSRF、幂等、当前用户/来源授权和容量检查，不执行模型/Git/导入/索引。只有耗时内容操作提交Job，不新增 `chat.conversation_create`。

- [x] **RED:** `test_conversation_is_private_and_scope_immutable` 猜ID/另一用户/admin旁观拒绝；更换selected集合冲突而非悄改会话；仅source-only fork的选择也被保留。`test_one_active_run_and_durable_counts` 并发提交至多1活动run、幂等key复用/冲突、200消息/100会话边界且归档不绕过。`test_all_participating_scopes_protect_history_job_and_sse` 撤任一repo，历史/引用入口/Job及回放均拒绝。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_chat_privacy.py -q`。
- [x] **Implement:** 表Conversation、Message、AgentRun、ToolCall、BudgetLedger/RequestReceipt及成员关系；逻辑请求键(run_id,kind,ordinal)唯一。AgentRun包含稳定thread UUID、graph版本、冻结scope、累计nodes/retrieval/model requests、首次执行时间/deadline、queued deadline、accepted config/retired、terminal reason；预算不借用Job每attempt重置的request_count。Message问题与答案独立有界；多仓权限检查复用3C，不用Job.anchor代替全部来源。创建Job与run/message/事件同事务，不在审计存正文。所有私有job visibility有限kind分支共用ChatService.authorize，不另造SSE。
- [x] **GREEN:** 聚焦测试、`uv run --no-sync pytest tests/integration/test_job_visibility.py tests/integration/test_sse.py tests/integration/test_migrations.py -q`；Ruff实际改动；迁移保留先前数据，含内容数据的downgrade拒绝静默删除。
- [x] **Commit:** `feat: persist private conversations and durable agent runs`。

### Task 2: 请求预占、dispatch与未知结果账本

**Files:** Create `chat/budget.py`, `tests/integration/test_chat_budget.py`; Modify `chat/models.py`, `schemas.py`, `backend/src/devflow/config.py`。

**Interfaces:** `BudgetService.reserve(lease:Lease,run_id:UUID,call_key:str,request_hash:str,parameter_version:str)->Reservation`；`mark_dispatched(lease,receipt_id:UUID)->None`；`settle(lease,receipt_id,usage:ProviderUsage)->None`；`mark_unknown(lease,receipt_id)->None`。`Reservation(receipt_id:UUID,amount:Decimal,currency:str,pricing_version:str,input_cap:int,output_cap:int)`；`ProviderUsage(prompt_tokens:int,completion_tokens:int,request_id:str,complete:bool)`，reasoning为包含关系，不重复加总。回放先查receipt状态，不能再次reserve/dispatch同逻辑call。

- [x] **RED:** `test_reserve_full_caps_atomically` 断言 `input_cap==1_000_000`、`output_cap==1034`、向上取最小货币单位，两并发不能超run/月余额；配置缺失/零/负/过期/不匹配币种或模式拒绝。`test_unknown_keeps_reservation_after_cancel_delete_replay`：dispatched后崩溃、缺usage、timeout不能释放/再发；dispatched前先落库的崩溃也保守unknown。`test_usage_does_not_double_count_reasoning`；`test_month_boundary_does_not_move_old_reservation` UTC跨月按原桶结算；超配置上界停后续请求并记对账异常。记录4KiB/全局32MiB边界、90天只清已结算终态、未知不清。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_chat_budget.py -q`。
- [x] **Implement:** Decimal金额、独立run/月桶累计，固定全局预算准入锁先于3A授权/Job/run/ledger顺序；所有状态跃迁CAS幂等，检查完整Lease/DB clock并复核。单请求reserve输入完整上下文+输出偏差，只有可信usage释放确认未用额；免费/缓存折扣不作为准入保证。记录请求hash/价格参数版本/状态/金额，不保存prompt。HTTP在事务外，标dispatched成功之后才可发；发送未知且旧lease失效不能污染run，下一合法恢复识别dispatched无可信结果为unknown，不自动重发。真实云仍默认关闭。
- [x] **GREEN:** 聚焦测试 +现有Job事务/lease回归PASS；Ruff。这里只用确定性假计价/usage证明账本，不宣称供应商账单绝对上限或真实计费已验证。
- [x] **Commit:** `feat: reserve and reconcile durable chat call budgets`。

### Task 3: 完整请求体装配与固定云协议适配

**Files:** Create `chat/context.py`, `provider.py`, `tests/test_chat_context.py`, `tests/test_chat_provider.py`; Modify `backend/pyproject.toml`, `backend/uv.lock`, `chat/schemas.py`。

**Interfaces:** `ContextAssembler.assemble(question:str,current:EvidenceSet,*,previous:EvidenceSet|None=None,tool_exchange:ToolExchange|None=None)->PreparedCall`；`PreparedCall(messages:list,tools:list,manifest:EvidenceManifest,body:bytes,request_hash:str,parameter_version:str)`；`ChatProvider.prepare(...)`负责生成最终发送body；`ChatProvider.invoke(prepared:PreparedCall,*,deadline:datetime)->ProviderReply`。`ProviderReply(answer:AnswerDraft|None,tool_query:str|None,usage:ProviderUsage|None,request_id:str|None)`；`AnswerDraft(conclusion:str,citation_ids:list[str],insufficient_evidence:bool,next_steps:list[str])`；ToolExchange仅当前run上一模型tool_call ID/arguments与匹配结果，不含历史会话。

- [x] **RED:** `test_limit_counts_actual_json_body_including_tools_and_escaping` 多字节、引号/反斜杠、工具schema、系统提示共同计入`len(body)<=24576`，捕获真实HTTP transport收到的body并与预检hash一致；必须部分超限或无完整证据可装入时context_limit、调用次数0。`test_second_context_prefers_new_and_drops_old_citations` 新rank优先→补旧未重复，≤6/每文件≤2，必须tool协议保留，最终manifest不含被剔除ID。`test_cloud_body_has_no_old_history_logs_or_unselected_source`；`test_provider_fixed_chat_contract` 路由chat/completions、模型/base URL、enable_thinking=false、max_completion_tokens=1024、SDK retry0、无tracing/search/cache-create，Responses不被自动选择；畸形/超16KiB/未知引用输出拒绝，不自动修补请求。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_chat_context.py tests/test_chat_provider.py -q`。
- [x] **Implement:** 使用LangChain消息/标准function calling，唯一工具 `search_repository_evidence(query)`，schema不带repo/权限参数。先固定must-have协议，再按完整块贪心尝试装入，稳定证据身份ID由3C产生；不截断片段却复用原引用。最终HTTP序列化只有一个权威出口：若LangChain/SDK会重序列化，使用其httpx transport发送前边界校验实际bytes并拒绝超限，且预算绑定的hash来自同一body；不可只数messages再声称请求合规。实现时锁定LangGraph、checkpoint-postgres、LangChain/OpenAI适配兼容版本，不安装未使用Agent包。输出解析与usage校验分开：答案无效也可能已计费，照常结算可信usage。
- [x] **GREEN:** 上述测试以无网络mock transport捕获最终请求PASS；`uv lock --check`、Ruff。实际供应商接受参数/usage/task-call兼容性留Task6单列未执行门槛，mock绝不解锁真实云开关。
- [x] **Commit:** `feat: assemble bounded evidence-only chat requests`。

### Task 4: 官方saver的同连接fence与完整thread保留

**Files:** Create `chat/checkpoints.py`, `chat/retention.py`, `chat/handlers.py`（本任务只注册cleanup，Task5添加run入口）, `tests/integration/test_chat_checkpoints.py`, `tests/integration/test_chat_retention.py`; Modify `chat/service.py`, `models.py`, `worker.py`, `docs/operations/knowledge-acceptance.md`。

**Interfaces:** `FencedPostgresSaver(conn:psycopg.AsyncConnection,lease:Lease,run_id:UUID,grant:SourceGrant)` 包装锁定版官方AsyncPostgresSaver的 `aget_tuple/aput/aput_writes` 和必要读取；签名以实际包为准，禁止改变official参数语义。稳定thread=run.thread_id、根checkpoint_ns=""；恢复只能来自DB accepted config，HTTP不接受任意checkpoint ID。`ChatService.finish(session,lease:Lease,run_id:UUID,outcome:RunOutcome)->None`在调用者短fenced事务内最终提交Message/Job/事件并retire清空accepted；`retire_controlled_run(session,job_id:UUID,expected:JobFence,outcome:RunOutcome)->None`是现有cancel/recover_expired事务的有限chat hook，`JobFence(status:str,owner:UUID|None,generation:int,attempt:int)`校验控制器看到的原状态并CAS撤销旧执行权；它不是saver写权限或再次执行入口。`ChatRetention.sweep(lease:Lease,*,limit:int=100)->RetentionResult`。

- [x] **RED:** 真实PostgreSQL `test_aput_and_accepted_rollback_together` 注入accepted更新失败，官方rows/blobs及accepted全回滚；`test_aput_writes_is_fenced` 验证旧owner/generation/attempt/到期/撤权一项不符即拒绝；`test_expiry_while_waiting_for_lock` 用DB时钟复核；`test_pipeline_and_savepoint_do_not_commit_outer_transaction` 分别覆盖官方有pipeline/无pipeline路径。`test_exact_checkpoint_load_includes_legal_pending_writes` 同ID后合法pending写入恢复可见，旧lease写不可见；禁止attempt namespace。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_chat_checkpoints.py tests/integration/test_chat_retention.py -q`；没有真实隔离PG不得用in-memory saver替代。
- [x] **Implement:** 独立mutex覆盖完整显式事务与该raw connection全部操作；先按既有User/所有Installation/Repository→Job→run锁顺序做raw psycopg完整fence，再调用官方aput/aput_writes；aput接受的返回config同事务写accepted，提交前复核expiry。不能SQLAlchemy检查后让saver另借连接，也不能拿其内部lock再递归死锁；不包graph/model调用。锁定包的官方schema通过受控 `uv run --no-sync python -m devflow.chat.checkpoints --setup` 显式部署步骤建立，启动/请求路径不隐式迁移；隔离测试schema与部署schema配置一致且受控。
- [x] **Implement/retention:** 状态仅业务ID/标记/计数，使用无pickle fallback的受控序列化；每checkpoint64KiB、每run32，写前有界序列化/写后同事务核算关联rows/blobs/pending实际逻辑字节，超限回滚为state_limit。全局128MiB计数和官方同事务更新；活跃accepted不可删。正常worker终态在有效原Job fence下把run不可恢复、accepted retired且清空；控制器取消/到期回收则走上面的全范围有序锁+原状态/owner/generation CAS hook，同事务撤旧执行权并retire，无需已失效来源重新授权成功，但禁止读取来源/发布回答/重置预算/再次发云。随后独立 `chat.cleanup` lease复核终态/retired并调用官方thread删除接口；删除中断可重做，覆盖全部pending/blobs，不删独立Message/manifest/未知费用。终态最长24小时；容量满先清最早终态，仍满拒绝新run。
- [x] **GREEN:** 两测试文件PASS；另断言 `test_terminal_retire_precedes_thread_delete`、`test_cleanup_protects_active_and_unknown_budget`、64KiB/32/128MiB边界及writer/cleanup竞争。文档记录实际锁定版saver/psycopg和运行结果；源码推断不是测试结果；Ruff。
- [x] **Commit:** `feat: fence official graph persistence and retire terminal threads`。

### Task 5: 扁平 ChatAgent、累计限制与终态收尾

**Files:** Create `chat/graph.py`, `tests/integration/test_chat_graph.py`, `tests/integration/test_chat_recovery.py`; Modify `chat/handlers.py`, `service.py`, `jobs/queue.py`, `jobs/schemas.py`, `worker.py`。

**Interfaces:** `ChatRunHandler.run(lease:Lease)->None`；`ChatGraph.execute(lease:Lease,run_id:UUID)->RunOutcome`；`RunOutcome(status:Literal['answered','no_evidence','failed','cancelled'],answer_call_id:UUID|None,error_code:str|None)`。有限节点为 `retrieve_initial → assemble_initial → model_first → (retrieve_more → assemble_final → model_final) → validate`；无证据直接本地终止。图结束后handler通过Task4 finish发布终态，避免终态retired后graph再尝试写checkpoint。

- [x] **RED:** `test_direct_retrieval_never_submits_child_job` 捕获同父lease/scope和唯一owner；`test_two_calls_two_retrievals_eight_nodes` 第二次模型要求工具时停止，重放节点也累计；`test_no_evidence_does_not_reserve_cloud`；`test_queue_and_execution_deadlines_survive_restart` 首次执行180秒/队列10分钟，新的attempt不得续时。`test_known_call_reused_unknown_call_not_replayed` 成功结果复用前重新鉴权/版本验证，已dispatched未知保留预占并终止model_call_outcome_unknown。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_chat_graph.py tests/integration/test_chat_recovery.py -q`。
- [x] **Implement:** 整个handler在3B串行model-worker，初次及可选再次检索直接 `RetrievalService.retrieve(RetrievalContext(parent_lease,...),query)`；传入 `reserve_attempt(restarted)` 在每次初始/整体重取前原子递增run检索数，并记录同run是否已因版本变化重取。全局chat1由owner及DB准入约束共同保证。每节点/请求的递增在独立短fenced事务持久化，deadline/次数不放在可回退checkpoint中当权威。ToolCall最多4条、每条128KiB，保存检索manifest/已校验模型结果，graph仅存其ID；发云/发布前再次验证scope/source/active，同轮版本变化最多整体重取1次且不得突破2次检索/2模型/180秒，无法重取直接version_changed。增加 `test_version_refresh_consumes_second_retrieval` 断言初次+重取用尽预算后工具不能偷跑第三次。初次模型toolquery仍受3C问题边界，固定repo集合不接受模型扩张。未知citation返回answer_validation_failed，不追加修补请求。
- [x] **Implement/terminal integration:** 页面断开不取消；保留既有Queue原子取消/过期回收，不新增terminal-only reclaim Job、状态或队列。正常worker仅有效lease可finish；控制器cancel/recover_expired在既有全范围有序锁下检查预期status/owner/generation/attempt并CAS撤销旧执行权，若本次确实进入终态则同事务调用Task4 hook retire/清空accepted；仍可合法retry的run保持受保护恢复点。已失权范围只允许拒绝收尾，不读内容/发布回答/改预算/发云。最后attempt耗尽同样覆盖，旧executor任何写被新generation或终态拒绝；hook失败整个终态事务回滚，不靠handler finally补救。模型线程未结束仍依3B占槽，停止claim/unhealthy，不以lease到期代替物理完成。
- [x] **GREEN:** 聚焦测试及3B `test_model_queue.py/test_model_worker.py`、既有Worker cancel/reap/denial回归PASS；新增 `test_cancel_expiry_and_final_attempt_retire_atomically` 覆盖所有终态路径，旧executor不能写checkpoint/发布/释放费用。Ruff；无真实云的mock节点测试不能标云协议通过。
- [x] **Commit:** `feat: run bounded recoverable evidence-based chat jobs`。

### Task 6: 私有聊天界面、存储清理与第三阶段验收

**Files:** Create `chat/routes.py`, `tests/integration/test_chat_routes.py`, `tests/integration/test_chat_flow.py`, `tests/integration/test_chat_provider_live.py`; `frontend/src/features/chat/types.ts`, `queries.ts`, `chat-page.tsx`, `frontend/src/app/(app)/conversations/page.tsx`, `frontend/src/app/(app)/conversations/[conversationId]/page.tsx`, `frontend/tests/private-chat.spec.ts`, `frontend/tests/chat-flow.spec.ts`; Modify `app.py`, `jobs/routes.py`, `jobs/service.py`, `chat/retention.py`, `frontend/src/features/session/shell.tsx`, `docs/operations/knowledge-acceptance.md`, `README.md`, `infra/compose.yaml`, `infra/backend.Dockerfile`。

**Interfaces:** POST/GET `/conversations`；GET/POST `/conversations/{id}/messages`；GET `/conversations/{id}/messages/{message_id}/citations/{citation_id}`；POST `/conversations/{id}/deletions`返回有界Job。共享3C `CitationService.preview`；在既有Job路由接入 POST `/jobs/{id}/cancel`、`JobService.cancel(actor:Actor,job_id:UUID,idempotency_key:str)->JobView`（入口核对最终2B已有取消签名后复用/窄扩展，不声称当前已存在），使用Task4/5原子chat收尾，幂等且限创建者。SSE只进度，最终答案从私有REST读取。`ChatRetention.sweep`扩展Message/ToolCall/receipt保留，不增加后台daemon。

- [ ] **RED:** `test_chat_routes_history_not_context` 旧消息UI可见但新请求仅当前问题/本轮证据；跨用户/任一来源撤权/引用/SSE回放拒绝；cloud缺配置返回model_not_configured，本地搜索可用。`test_chat_retention_preserves_unknown_receipts`：90天/256MiB、200消息、100会话、ToolCall4×128KiB/24小时、费用4KiB/32MiB各边界；删除正文及时消除中间结果，不释放未知预占或擦掉请求身份，月汇总保留。前端 `test_scope_versions_citations_and_cache_revocation` 检验固定范围、每轮SHA、缺口、取消/断线、晚响应和退出缓存清除。
- [ ] **Run RED:** backend `uv run --no-sync pytest tests/integration/test_chat_routes.py tests/integration/test_chat_flow.py tests/integration/test_chat_retention.py -q`；frontend `npm.cmd run test:e2e -- tests/private-chat.spec.ts tests/chat-flow.spec.ts`。
- [ ] **Implement:** 输入框明确当前问题会发阿里云、历史/附件不自动回传，不诱导用户在聊天输入密钥。保留现有安全文本渲染，所有manifest/响应有界；页30/最多100，private/no-store，未知配置/预算/来源/推理忙错误均安全可解释。容量不足先清允许过期数据，仍不足拒新，不删未过期消息凑空间；run终态24小时清中间结果与checkpoint，最终message独立。清理Job分批100、幂等、权限/fence保护，不占本地模型槽。
- [ ] **GREEN:** 聚焦pytest/Ruff、`uv lock --check`；前端typecheck/lint/build及上述Playwright；迁移/全第三阶段流程与已有授权/Job/Workspace回归按受影响范围一次完成。A方式整体审阅，记录确切命令/结果，不将计划勾选等同测试证据。
- [ ] **真实接入门槛（未执行）:** `test_chat_provider_live.py` 默认禁用，缺明确启用、已轮换本地凭据、正run/月预算、有效价格和专门受限合成请求时拒绝，不读取用户仓库。单独预算授权后验证一次Chat/tool协议往返、实际参数与usage/截断、请求体捕获安全性；记录仍不能证明供应商普遍遵约或最终账单绝对上限。真实Milvus/双本地模型/云/正式Linux全栈资源、备份恢复和真实私库质量分别标实际通过或NOT EXECUTED，不以mock/分时结果合并声称全栈通过。
- [ ] **Commit/交付:** `test: verify private chat and phase three delivery`。提交仍受Git审批可用性约束；不推送/部署外部环境，不把第4–7阶段记为完成。

## 3C 交付契约（2026-11-05，供 3D 复用）

本节由 3C Task 5 交付，只记录当前已实现的接口与语义；不改变上方 3D 任务范围，也不表示 3D 已执行或验收。

### Scope 冻结与重校验

- `RetrievalService(factory, policy, *, access=None, inference=None, index=None)`；`freeze(actor, selection: QuerySelection, *, fresh=True) -> QueryScope` 先做 fresh 授权，再在同一有序锁事务内固定每个索引。
- `QuerySelection(selected_repository_ids: tuple[UUID,...], indexes: tuple[IndexSelection,...])`，最多 3 个仓库；`IndexSelection(repository_id, sha: 40-hex, target: Literal["target","active"])`。`indexes` 必须非空且是 `selected_repository_ids` 的子集；source-only fork 只进入 `selected_repository_ids`。
- `QueryScope(actor, grant, selected_repository_ids, indexes, selection)`；`FrozenIndex(repository_id, index_version_id, sha, index_generation, target_generation, member_digest)`。`scope.selection` 保留用户原始选择，版本变化重冻结时不得扩为模型或 Job 推断的新集合。
- `revalidate(scope, *, fresh=False) -> None` 重新验证授权与每个冻结索引；`fresh=True` 用于发布/发云前的实时撤权检查。
- `QueryScope` 只能由服务端创建，不能作为 HTTP 输入反序列化。

### 检索

- `RetrievalContext(lease, scope, deadline, refresh_used=False, reserve_attempt=None)`；`retrieve(context, question: str) -> EvidenceSet`。
- question 超过 2048 UTF-8 字节或 256 token 拒绝；检索未配置或超时抛 `retrieval_unavailable`；版本变化且本上下文未刷新过时，按 `scope.selection` 重新冻结一次并以 `refresh_used=True` 重试，再次变化抛 `version_changed`。
- `EvidenceSet(scope, status: Literal["evidence","no_evidence"], evidence: tuple[Evidence,...], coverage: tuple[CoverageView,...])`；`Evidence(identity, chunk_id, index_version_id, location, text, content_hash, rank)`，rank 从 1 连续。
- 最终证据 ≤6 条、每 `(document_id, revision_hash)` ≤2 条，并剔除重复 chunk 与重叠字节区间；`coverage` 每仓库记录 included/skipped/unsupported/over_limit/unscanned/complete。
- 3D 图内直接调用 `retrieve`，不新增检索队列或子模型 Job；`reserve_attempt(restarted: bool)` 可在每次初始/整体重取前原子计量。

### Manifest 与预览

- `CitationService(factory, policy, *, public_base_url=None, reader=None)`；`manifest(evidence: EvidenceSet) -> EvidenceManifest` 由服务端证据身份生成 `citation_id`（SHA-256(identity+location) 的确定性前缀，rank、顺序和模型输出不参与）和 `url`。
- `EvidenceManifest(entries: tuple[CitationEntry,...])`；`CitationEntry(citation_id, identity: SourceIdentity, location: ChunkLocation, url, content_hash, rank)`。`validate_ids(manifest, ids)` 对未知引用抛 `answer_validation_failed` (422)。
- `CitationService.preview(actor, manifest, citation_id, *, cursor=None) -> SourcePreview`；`SourcePreview(identity, text|None, location|None, is_current, partial, next_cursor, reason: Literal["source_expired","source_unavailable"]|None)`。未知 citation ID 抛 404；续读只接受服务端游标。
- 3D 的消息引用预览应把自己持久化的最终 manifest 交给同一 `preview`，不得用模型正文或重新检索结果替代。

### Job 可见性

- `knowledge.query` Job 的 `requested_by` 必须存在；Job 读取与 SSE 只对创建者可见，且 query Job 使用 `KnowledgeQueryRepository` 的全部参与仓库（按 ordinal），不以 `Job.repository_id` 锚点代替完整来源集合。
- 非创建者、任一参与仓库撤权或查询已回收均返回 404；不存在只按 anchor 授权的回退路径。
- `QueryView(query_id, job_id, status, selection, question, manifest|None, result|None, error_code, created_at, completed_at)`；结果正文仅在 Job 终态后可读；`KnowledgeQuery.reclaimed_at` 非空后查询与引用均 404。
- query 结果保留 1 小时、每用户活动查询上限 20 条；Job 可见性不随结果回收而放宽。3D 的 `chat.run` 应复用同一 creator-private 可见性与完整参与来源校验口径。
