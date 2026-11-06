# 2A 数据基础决策

日期：2026-10-19。范围为已批准的 [2A 规格](../superpowers/specs/2026-10-13-devflow-data-foundation-design.md)，不覆盖 [一期决策](foundation-decisions.md)。

| 决策 | 原因与边界 |
| --- | --- |
| 复用 PostgreSQL Job、租约和审计 | 无消息总线或外部新服务；系统 sync/health 与创建者私有 probe 分开授权。 |
| HTTP 在短事务外，提交有完整 scope/epoch/Lease 栅栏 | 事实、集合、checkpoint 和后续任务原子提交；撤权、旧 owner 或旧代次不得发布。 |
| 每单元五次请求，持久分页/目标/版本复核阶段 | 令牌签发也计数；正常 Worker 重启依靠数据库续接，不依赖内存页游标。 |
| Issue 与 PR 身份分离，PR 采集末尾复核 base/head | Issue ID 不能充当 PR ID；文件绑定精确 base/head，旧 review commit 保留原证据。 |
| Actions 保存 run 的每个 attempt 与对应 jobs | 最新成功不改写旧失败，其他 SHA 的成功不能证明当前 PR Head。 |
| 规范化白名单 Fact/FactCollection | 不保存原始 provider/Webhook 字典；缺失、截断、未知与空集合分别表示。 |
| 独立健康 generation 和请求序号 | health-v1 汇总实际观察群体与覆盖，较旧结果不能覆盖新 current；失败保留旧生成时间。 |
| 浏览器共享授权 scope、取消信号及私有 query key | 已知拒绝同步隐藏整个仓库内容，再清理缓存；普通 fact_not_found 保持局部错误。 |
| PR 文件渲染额外比较父 base/head | 不混合不同版本；显式重新确认父版本，不自动无限刷新。 |
| 真实 API/Worker/PG + 仅外部 HTTP 合成的验收 | 保留真实 OAuth state/cookies、授权和持久化；普通 Worker 任务 stop/start 与容器服务恢复分别记录。 |
| 真实栈 Playwright 串行 | 复用既有固定31848代理边界，各 helper 独立 schema，避免端口争用；不增加共享测试服务。 |
| 保留既有 App 过期用户令牌认证流程 | 当前 App 设置下不需 OAuth scope 迁移；安装令牌按仓库/流缩权，令牌格式视为不透明。 |

2A 不提供健康总分、GitHub 完整事件历史、CI 日志、第三方 CI、模型/RAG、Git 克隆、文件浏览或代码搜索。Workspace 明确不可用，迁移 `0009_workspace` 是未实施的 2B 工作，依赖本轮 `0008_sync_repair`。真实 App、私有安装、外部投递、TLS 和接近1GB仓库验收均为 **NOT EXECUTED**；本地证据见 [2A 验收](data-foundation-acceptance.md)。

## 整分支修复裁定（2026-10-19，独立复审通过）

- 每单元 HTTP 请求数硬上限为 5，配置只接受 1–5（包括令牌签发）。规格 §6.1 的整轮 10000 请求/24 小时是可配置默认值，不能据此扩大单元上限；整轮既有校验范围保持不变。
- 单元时长接受 15–120 秒，保留 10 秒 HTTP 截止并预留 5 秒准备余量。原可接受的 10–14 秒配置现在启动校验失败；15 秒下经过 2 秒准备仍能真实预留请求并正常续接。异常缓慢准备仍按原有单元边界续接。
- `RepositorySync.next_reconcile_at` 经 `0008_sync_repair` 持久化每日尝试截止，`last_reconciled_at` 仍仅表示全部流成功。三个流完成 initial/reconcile 尝试（包括 partial/permission）后推进每日截止；全范围周期尝试推进 `next_due_at`；目标任务和有限人工回填均不推进。失败/预算耗尽不推进，既有显式重试/管理员恢复规则不变。新增字段的 NULL 对已存在仓库表示到期，升级后可能多一次保守全量核对；不能用历史成功时间代替尝试调度，也不把调度信息藏进目标 checkpoint。
- 到期需求在活动任务期间也登记；持久待处理 reconcile/registration 合并每日需求，任意无目标、非回填的全范围需求合并周期需求。registration 本身执行完整 reconcile。较弱周期任务不能吞掉较强每日核对；捕获输入不改写，较新需求和单个活动事实任务约束保留。
- 目标 checkpoint 的阶段负责目标执行；成功目标不重写整流状态、缺口原因、覆盖区间、水位或历史成功时间。目标新发现的缺口可降低无错误的覆盖状态，已有缺口不被替换；目标重试/租约回收失败只影响任务状态。事实、集合及其原因仍正常发布；sync remaining 表示本次执行，目标执行不宣称全量 enumeration complete。facts 与冻结 health 读取相同保留证据，无公共 DTO 形状变更。
- 迁移新增一列是独立调度截止所必需，并非为了分离已有 checkpoint 与覆盖概念。非 NULL 截止不允许降级丢弃；未使用该列的库可往返。2B 新迁移改为 `0009_workspace`，依赖 `0008_sync_repair`；以下旧裁定中的 0008_workspace/0007 仍保留其历史语境，当前顺序以本段为准。


## 已执行2A裁定原文

以下保留控制器执行台账中的全部实际 Ruling 行及理由/成本；历史待开始措辞属于当时裁定语境，当前状态以各任务报告及本次验收为准。来源为本地忽略的2A progress.md，2026-10-19整理，不将历史失败或不确定诊断删除。

> Ruling: Treat the explicit start instruction as authority to prepare the missing detailed plans and execute them continuously — higher-priority user direction takes precedence over a repeated skill approval handoff — risk is reversible task-plan adjustment, not external publication.

> Ruling: Use the established project-local ignored worktree convention — isolate existing DevFlow.md/.idea changes and reuse prior development method — costs an extra environment only.

> Ruling: Store normalized logical facts with typed payloads in Fact/FactCollection instead of ten near-identical CRUD tables — preserves required source identity/version/index fields and minimizes duplicated persistence — if SQL queries become awkward, split a domain later without changing API.

> Ruling: Display repository Markdown as escaped source text for this stage — no existing renderer dependency; preserves content safely — rich formatting remains absent, not content.

> Ruling: include explicit Fact.parent_kind paired with parent_source_id — Issue and PR discussions must preserve canonical parent identity and PR issue_id differs from PR id — extra indexed field avoids ambiguous joins.

> Ruling: Task2 pr_file has no provider numeric file ID; use parent PR ID source_id and base/head plus SHA256(exact UTF8 filename) version_key, preserving exact path payload — prevents sibling-file uniqueness collisions. Task2 pending START until Task1 review gate passes.

> Ruling: Persist unprocessed sync inputs in SyncDemand(repository_id, sequence composite PK, reason, typed target/backfill) rather than overwrite active checkpoint — mid-pass webhook/backfill demands must survive restart and coalescing — costs one small journal table; no second runnable queue. Finite reasons registration/refresh/webhook/periodic/reconcile/backfill.

> Ruling: Store per-attempt request_count/attempt_started_at on Job and reset during claim, while RepositorySync retains round accounting — reconstructing SyncWork must not reset hard budgets — costs two internal columns and atomic request reservations.

> Ruling: request_sync is transaction-only with validated input; optional Queue Settings and sync-only global admission lock precede ordered scope locks; sync handlers own transactional completion and return None, probe gets a Lease adapter — preserves old probe behavior while fenced facts/checkpoints/continuation commit together — integration tests must cover both dispatch paths.

> Ruling: Task4 collector steps perform at most one HTTP and return durable checkpoint transitions; handler loops under the shared budget, including token mint — avoids losing PR final rechecks at the fifth-request boundary — additional short commits are acceptable for restart safety. Current client uses unconditional reads; unexpected304 cannot be interpreted as completion.

> Ruling: BackfillRange extends SyncWindow with nonempty unique selected streams (default all three) and replaces SyncDemandInput.backfill type — Task5 stream-scoped backfill must not lose intent at the journal boundary — JSON column unchanged; adds a small DTO/test extension before Task3 integration.

> Ruling: Use durable checkpoint target_started_at and database observation times to fence completed-child cleanup rather than add unbounded seen-member lists or command-only CollectionInput fields — safe absent-member detection across paged/restarted traversal — commit must explicitly match collection parent/version, including PR filename-bearing version keys; Task3/4 test this behavior.

> Ruling: Task4 may add bounded finite normalization_reasons fields to strict normalized payload DTOs with tests — required unknown/truncated/missing-field evidence has no field in Task2 DTOs yet — small additive payload schema change; no raw provider blobs.

> Ruling: Capture at most100 contiguous demand-journal entries per round; distinct backfill ranges/streams serialize without destructive merging, with captured_sequence ending before deferred demand — bounded input/fairness without dropping targets — busy repositories may need more rounds, still within explicit scheduling.

> Ruling: Permit number-only pull SyncTarget hints and enforce per-kind target validation — PR issue_comment lacks the canonical PR ID — Task4 resolves identity before normalization, adding bounded detail work.

> Ruling: Add RepositoryCommandReceipt for actor/repository/operation/key and normalized hash — consumed demand rows cannot enforce durable HTTP idempotency — one small receipt table in0006, finite operations.

> Ruling: Backfill accepts aware offsets normalized UTC, rejects future ends, and caps duration365days inclusive — clear API boundary consistent with administrator UTC request — client must send explicit zoned times.

> Ruling: Explicit refresh resume=true plus admin/fresh repo authorization is required after budget_exhausted; preserve checkpoint while renewing finite round budget — periodic/webhook events must not silently reset exhaustion — adds an explicit recovery action to UI/API.

> Ruling: push/create/delete advance monotonic version-hint sequence instead of retaining unbounded ref/SHA history — sufficient durable invalidation hint while2B freshly resolves authorized sources — may conservatively invalidate unrelated ref resolution work.

> Ruling: Task6 persists separate health requested/processed sequence and active job, with common manual/automatic enqueue; allocate health generation only on freeze — fact-sync identity cannot safely represent health demand — three state columns and migration0007_health_requests, shifting2B workspace migration to0008.

> Ruling: Health publish returns valid fenced completion even for historical-only snapshots; collection cursors bind captured version and details page children separately — preserves newer current pointer and avoids mixed-page versions — callers must distinguish completion from pointer advancement.

> Ruling: Expose retry_at for sync/health pending retries separately from periodic next_due_at — bounded frontend polling must respect persisted rate-limit deferral — one additive safe DTO field, not raw job data. Fact-not-found remains distinct from repository authorization denial on all child routes.

> Ruling: Default PR passes with separate all-open enumeration may use state=closed for the updated-window scan, avoiding routine duplicate PR collection; manual bounded backfill still includes matching open PRs using state=all unless separately covered — preserves approved all-open/recent-closed and explicit backfill scope — provider state transitions still require duplicate handling.

> Ruling: Persist a bounded typed per-PR recapture ledger for changed PRs across target resets within a round — repeated pages/state transitions must not reset3 recaptures — small checkpoint metadata bounded by validated request budget (tighter size cap must report explicit gap), no source payloads.

> Ruling: Task5's normal Worker integration must repair sync-unaware shared test transport or explicitly prevent due sync in probe-only tests, with causal coverage, not suppress errors or extend timeouts — confirmed fixture defect is now exercised by required wiring — slight test-scope extension. Retain unresolved historical timing evidence for Task8 with safe ID/kind/DBtime diagnostics on next affected run.

> Ruling: Task5 registration with a changed authorization binding retires an old-epoch active sync Job under existing ordered scope locks, with terminal event/audit; clear active pointer and schedule fresh-generation work using new binding, retaining unprocessed journal/backfills — otherwise old queued/running scope blocks fresh registration and later fails permission_revoked — authorized narrow scheduler integration extension. budget_exhausted still requires admin resume; ordinary same-binding failure budget preservation remains. Old completed coverage remains historical evidence, not a fresh success. Test queued/running binding recovery, new demand retention and old-lease publication fence.

> Ruling: Task5 migration supports empty-schema downgrade/upgrade roundtrip but refuses before losing newreceipts/versionhints/eventvalues or incompatiblephase2data; neverdelete retainedrows to make downgradepass — preserves spec conservative backup/forwardfix rollback contract — populatedphase2 may requirecompatiblebackup ratherthanAlembicdowngrade.

> Ruling: Task6 health aggregates all eligible persisted facts inSQL, butmanifest has atmost100openPRversion entries(number/sourceIDascending) and200dedupCIrun/latest-attempt candidates(updatedatDESC NULLSLAST/sourceID/attemptDESC). Eachlist exposes selectionorder,totalcandidatecount,returnedcount,truncated; omittedgroupsremainunknown/unlisted, nevernoCI/success. Aggregateprovenance includesfrozenat, syncgeneration/requested/processedsequence, streamcoverage, min/maxobservedat, ruleversion andpopulationcounts; noindividualIssueinventory — boundedresponses/memory while preserving deterministicobservedcounts — exact perIssue historicalreplay isnotprovided andsourcecoverage limitationsremainexplicit. Latestdefaultbranch runperworkflow chosencreatedat thenrunID, withlatestattempt, sooldrerun updatedtimecannotdisplace newercreatedrun. ExactPRhead matching unchanged.

> Ruling: Task7 does not run wholefrontend suite solely for genericTDD wording afterrequired39casegate; Task8 ownswholefrontend/realstack fixtureintegration and finalfullrun — respects explicitplan and developerinstruction against unnecessarybroadening/repeat — any new concreteconcern still getsfocusedvalidation. If alreadyrunning preserveactualresult ratherthanhidefailure.

> Ruling: after focusedrealstackpairpasses andfinalfixtureedits, runonefinalfullfrontend suite toclose explicitTask8gate — priorfullfailed andtestchanged, so justified — do notrepeatoncegreen. Composeisolatedstart/migrationgreen; recovery/stop/report stillpending. Usercloudscopeconfirmed inphase3prepartifacts, noactualAPIcall.

## 2B R1–R10交接裁定（历史交接）

以下是已裁定的2B约束，来源为2026-10-15控制器preflight台账及其澄清。保留它们便于下一计划执行；不表示2B代码、测试或进程安全已完成。当前下一迁移是0009_workspace，依赖2A0008_sync_repair；下方 R1 引文是修复前的历史顺序。原preflight与crash-lifecycle-decision-prep.md仍在本地SDD目录供执行者详细阅读。

> Ruling R1: workspace migration is0008_workspace with down_revision finalized2A0007_health_requests; Task8 of2A updates tracked plan reference before2B — prevents duplicate revision — no schema change beyond planned sequence.

> Ruling R2: authorize narrow native CreateProcessW adapter in windows.py retaining process/thread handles and atomic Job association with documented startup attributes; contained suspended launch resumes only after verified setup, any unsupported/failing containment fails closed. Linux retains synchronousPopen; threaded runner contract unchanged — CPythonPopen discards required primary-thread handle — small platform adapter departs literalPopen wording only onWindows.

> Ruling R4: low-level ProcessResult carries bounded binary stdout plus safe status, never rawstderr; TreeEntries includes validated mode/OID/rawpath plus complete/truncated/scanned/reason within100000cap. fetch/tree/blob propagate caller cancel and monotonic absolute deadline; Task2/3 own final public DTOs beforeUI. Empty path accepted only as root — prevents unbounded lists/reset timeouts and incompatible consumers — explicit small internal metadata additions.

> Ruling R5: content deduplication by numericrepo+SHA never deduplicates public snapshot provenance/auth epochs. Public snapshot records bind source selection and source/base repo scopes, idempotency actor+repo+selection; forkhead checks bothregistered authorizedrepos perread — fixedSHA alone is not authority — may create multiple small records for sameobjects.

> Ruling R7: Task4 may narrowly edit git/process/read/config to wire budget monitoring/sharedlocks/pins. Health freezes known available/unprepared/unavailable evidence without eagerfetching; eviction retainsidentity — one lifecycle implementation and bounded traffic — availability may remainunknown untilprepare.

> Ruling R8/R9: rawobjectfixtures/localfetch use explicit test-only injected boundary, production remains fixedHTTPSgithub.com. Linux focused tests can use owned isolated test runtime withGit/devdeps beforeTask6 production packaging, use sandbox escalation ifneeded; final productionRO/RWchecks stillrequired. RealApp/privatefork/near1GB/TLS/hosthardquota remainNOTEXECUTED untilactualevidence — no fake externalacceptance — additional testimage cost.

> Ruling R10: extend existingCursorCodec with explicit authorizedmax default100 and workspacepurpose usage200; allpriorendpoint capsremain100 and existing cursorpurposebinding remains — treepages need200 — narrowpaginationextension withcrosspurpose/maxregressions.

> Ruling R3/R6: adopt the bounded fixed per-operation supervisor plus shared durable physical-cleanup guard described in crash-lifecycle-decision-prep.md; extend planned reader-pin model rather than create second registry/queue. Ordinary API/Worker parent crash triggers owned-pipeEOF or monotonicdeadline, whole-tree termination/reaping and nonce-fenced directDB drain acknowledgement; oneactiveGit treeperadmittedread,4globalreadslots,1globalfetch/GCslot. Supervisor owns shared/exclusive repoOSguard onprecreatedfiles (APIread-only). Expiry ends authority, never proves physicaldeath; reserved-before-activation canCASrevoke, activeuncertaincleanup retainscapacity/GCexclusion untilpositiveproof. CrossPIDnamespace absence isnotproof. Exceptional supervisor/DB-ack failure uses bounded same-incarnation reconciliation or verifiedoffline owneddeployment stopbefore scopedguardrecovery — parent-ownedlocks andTTLalone permitstalewriter/GC overlap — costs onefixedstdlibhelper pluscommonpinfields and may require owneddeployment downtime inexceptional cases. NoDaemon/Dockersocket/newservice/privileges/genericcommandAPI. Existing600s/5s deadlines includeadmission andcannotresetperGitcall; boundedcleanup <=2s beginswithindeadline, inabilitytoproveexit returnssafeerrorwhileguardremainsclosed. Do notclaim literalOSlocksurvivesownerdeath; durableguardmaintainseffectiveexclusion untiltreeproof. Task1 verifiesOSlifecycle; Task2/3/4 addDBguards/admission/GC, onlycompleteintegrationcanclaimcrashsafety.

> Ruling R5/R4 clarification: authorize base repository first. A missing/inaccessible snapshot within an otherwise authorized base uses a distinct local workspace_not_found error; unavailable fork source uses fork_source_unavailable. Base-repository denial keeps existing repository denial semantics. This lets the UI hide denied full-head code while preserving still-authorized base PR metadata, and avoids treating every workspace404 as revocation of the whole repository — follows spec fork metadata/full-code distinction — one finite error distinction shared byTasks2/3/5. Foreign source authorization is still checked on every read and no guessed snapshot/SHA becomes readable.

## 整分支审查期间补充裁定原文

这些裁定沿用前文的范围与成本说明；涉及真实外部验收的项目仍未执行。

> Task 2: /root/data_task2 read-only preparation while Task1 review runs; no implementation START issued. Ruling: include explicit Fact.parent_kind paired with parent_source_id — Issue and PR discussions must preserve canonical parent identity and PR issue_id differs from PR id — extra indexed field avoids ambiguous joins.

> Task8 Oct19 full backend verification: uv locked alreadygreen; fullpytest670passed361.86s, no warnings/failures; Ruff130filesgreen; no backendeditafterrunstart, no repeatneeded. Firstfullfrontend64run63pass1fail because legacyprobeauditlocator matchesnewlegitimatejob.succeeded rows; exactprobeUUID locatorcorrection underway. Ruling: after focusedrealstackpairpasses andfinalfixtureedits, runonefinalfullfrontend suite toclose explicitTask8gate — priorfullfailed andtestchanged, so justified — do notrepeatoncegreen. Composeisolatedstart/migrationgreen; recovery/stop/report stillpending. Usercloudscopeconfirmed inphase3prepartifacts, noactualAPIcall.

> Whole2AreviewSTART/root/data_whole_review(gpt-6-astra high) at400bf87, baseline8dd1a4c, officialpackage735264bytes13commits. Reviewertracingcross-taskcontracts; no repeatedsuites. Spec6.1 expresslyallowsvalidatedchanges todefaultwhole-round10000requests/24h, so higherroundlimitsalonenotprovenviolation. Ruling: retainhardupperbound5HTTPrequests perunit asplanGlobalConstraints/retaineddecisionstate, allowinglowerconfig; genericdefault-adjustmenttextdoesnotjustifyexpandingthisprotocol — leastscopepreservesreviewedboundedunitbehavior — cost ifwrongoperatorneedsnewreviewtoincreaseunitcap. Concreteacceptedunitseconds10livenessriskunderreview, nofixclaimyet.

> Ruling: acceptreview'sexplicitdeclinedjudgmentsasstage/evidencelimits, notsilentdiscard — Workspacebelongsapproved2B; liveApp/private/fork/SAML/Webhook/TLSandnear1GB/hardquota/newpopulatedrestore/downgradeawaitactualresources; historicalheartbeatrootscannotberetroactivelyclaimed; retainedschemahasnouniquelyprovenownership; CIlogs/thirdparty/model/RAG/healthscoreorautomaticmergeoutside2A — cost is thoseclaimsremainunverified/deferredandmustnotbecalledcompleteplatform. Productionunit5clarificationretainedabove; whole-rounddefaultsremainadjustable.

> Wholefixmilestone: tracedallSyncCursorconsumers; existingcheckpointstage/mode+evidencefields planned sufficient, no migration/DTOshapechange. BehavioralRED9failed2passed fordeadline/backfill/coalescing/historicalgaps/targetonly/config; prior10tmppathsetuperrors2cachewarningsseparate. Ruling: unitminimum15s preserves10sHTTPplus5ssetupmargin, reject10..14sandtestminimumrealreservation — avoidsprovablyunusable10ssettingwithoutHTTPprotocolchange — costformerlyacceptedshortunitconfigsnowfailstartupvalidation. Finalminimumprogressprobeincludes2ssimulatedsetup.

> WholefixsecondaryRED: partialinitialfullpassfollowedbyschedulertickimmediatelyqueuedreconcile(1behavioralfailure). Ruling: addRepositorySync.next_reconcile_at via0008_sync_repair, advanceafterallthreeinitial/reconcilestreamsfinishincludingpartial/permissionbutkeeplast_reconciled_atassuccess-only; nullupgradeddeadlineisdueforoneconservativereconcile. Periodicfullattemptsadvancenext_due_at,targeted/backfillneither;failed/budgetexhaustedremainstoppedunderexistingretry/resumepolicy. Pendingreconcilecoalescesdaily, pendingfullscopeexcludesboundedbackfillandcoalescesperiodic — separateattemptschedulingfromevidencetruth — costsoneDDLrevisionandpossibleoneextrareconcileperexistingrepoonupgrade;2Bworkspacebecomes0009. Rootupdated2Bscratchcontext/Task2brief; implementerownsalltrackedheadchecks/docs/planupdatesandmigrationtests.

## 2B 执行期间的补充裁定

以下整理至2026-10-22，供后续实现、审查和运维使用。裁定不是验收结果：Task 1–5代码审阅已通过，进入Task 6联调；Task 5最终typecheck/lint通过，浏览器测试未执行、唯一正式构建因spawn EPERM失败。Task 4的重要权限缺口已修复并复核，轻微原因分类问题留整阶段审阅。队列领取及原生Health/启动的未解释观察、真实fetch、Linux与完整运行时验收仍待完成。当前迁移顺序为 `0008_sync_repair` → `0009_workspace`。用户已批准第三阶段规格和3A–3D计划，仍按2B完成后再实施第三阶段的顺序推进。

| 裁定 | 约束、理由及代价 |
| --- | --- |
| R11 幂等请求 | WorkspaceVersion保存请求用户、基础仓库、幂等键及规范化选择哈希。同键同选择重新授权已存来源并返回同一版本的当前状态，不重新解析移动ref；不同选择返回冲突。避免借用要求SyncDemand序号的事实同步收据，代价是少量专属字段和唯一索引。 |
| R12 来源授权 | 请求阶段使用当前用户令牌，通过固定GitHub接口解析SHA和来源，基础仓库先授权、外部来源随后重新授权。Worker独立检查安装授权及epoch；用户401与安装401分别处理。代价是有界的额外来源请求。 |
| R13 CI关联 | 只接受指定attempt的关联head SHA，并以精确branch/tag候选或唯一关联PR证明来源；不能证明、已移动、已删除或存在歧义时返回不可用。不推断实际workflow checkout，不搜索历史、不退回main、不抓任意SHA。代价是部分合法历史版本无法新建工作区；已经发布且仍获授权的固定版本可以保留。 |
| R14 解析预算 | 注解tag最多8跳，检查循环、类型和OID；请求授权与元数据解析共享30秒总截止，每次HTTP不超过10秒及剩余预算。Worker抓取仍为600秒总截止。代价是过深tag或慢响应明确不可用。 |
| R15 测试目录 | Windows Python的0700新目录会在当前沙箱产生权限自拒；仅显式使用测试专用workspace_tmp_path，在新建自有目录继承既有权限，Linux仍0700。不修改既有目录ACL、全局fixture或安装包。Task 2受影响的四个既有集成测试模块同样显式选用；代价是临时文件位置不同，必须保持测试专用。 |
| R16 传输测试 | 真实Git本地fetch helper因WinError5受阻时，使用明确的测试边界构造/复制合成对象，继续验证实际Git对象、pin、原生supervisor和数据库流程。生产只允许固定GitHub HTTPS；不添加任意URL入口。代价是真实传输与Linux仍需独立验收，合成传输不能代替它们。 |
| R17 行与片段 | 按原始LF分行，保留CR，最多1MiB文件、500请求行及256KiB完整行响应。首行即超响应预算时返回空正文、空实际行范围及response_limit，不生成未返回文本的引用。代价是超长行在2B不能预览；第三阶段的字节区间预览另行实现。 |
| R18 目录游标 | 沿用v1游标封装；purpose为 `workspace-tree:v1:<sha256>`，哈希输入是 `[基础仓库UUID, 工作区UUID, 完整SHA, 精确路径]` 的紧凑UTF-8 JSON，UUID采用小写带连字符形式，ensure_ascii=False，不做Unicode归一化。用户及页大小继续签名；位置为非bool的非负整数序号。仅Workspace允许最大200，其他端点仍100。代价是需要维持此稳定编码约定。 |
| R19 路径 | 接受相对POSIX路径中的冒号，包括字面 `C:/x`，始终不转换为宿主路径。拒绝反斜线、开头斜线、空/dot/dotdot组件、控制字符、无效UTF-8及超4096字节路径。不支持的原始名称仍计数，以有界转义名称显示，path为空且无导航/引用URL。代价是这些名称只能显示限制信息。 |
| R20 搜索 | 查询为1–256 UTF-8字节、大小写敏感的字面串，拒绝LF而允许字面CR；同一LF行多次出现仍只算一条匹配。最多2000文本文件、20MiB已检查blob、100匹配，不超读预算；只有证明耗尽才称完整。片段总量256KiB，超量匹配保留已验证引用但text为空，单独标识片段截断，不混淆搜索覆盖。代价是无多行查询，部分匹配需另行预览。 |
| R21 版本列表 | Task 2提供service，Task 3提供路由。按created_at/id倒序，默认20、最大100，游标绑定用户/基础仓库/purpose/页大小；最多检查请求条数。先授权基础库，再逐一授权去重来源，不可用来源不泄露SHA/来源，仅增加当前页unavailable_count；游标按最后检查项推进，允许空页仍有后页。整个授权/DB操作30秒，返回前复核，不吞掉认证失败。代价是返回条数可能小于页大小。 |
| R22 异常清理证明 | 每个服务进程生成runtime UUID，reservation绑定operation/nonce。Linux保留boot/PID namespace/start ticks；Windows保留PID、创建FILETIME和Job身份。活跃guard过期不能证明进程已死；Windows异常恢复仅限原runtime仍存活且持有匹配身份的正面死亡证据。跨runtime或重启后不能以PID不存在推断，必须在验证自有部署已停止后定向恢复。代价是异常状态可能需要停机。 |
| R23 孤立pin | 每次新的合法lease生成独立attempt/pin，不覆盖或把旧孤立pin直接变为公开读取依据；可复用Git对象，后续由有防护的GC清理。已发布内容复用仍保持独立来源授权并验证SHA/pin。代价是可能重新抓取并暂存孤立对象。 |
| R24 分离代码进度与验收 | 已审阅的Task 1接口允许继续可逆的后续开发，真实fetch/Linux仍是最终验收及集成门槛。符合用户继续可用开发的指示，不据此合并或宣称全阶段完成。代价是后续平台验证可能带来返工。 |
| R25 验收夹具范围 | Task 6可小范围扩展既有full_flow/browser_server、前端full-flow-setup、必要Playwright/Compose测试配置及备份恢复文档，复用现有真实栈夹具。保持2A流程，不加生产测试开关、任意URL或秘密配置。代价是共享夹具改动需要相应回归。 |
| R26 团队共享版本 | 按规格4.2/7.1，请求时重新授权当前用户；后台准备/发布依赖完整lease和安装/仓库scope，不依赖原请求人仍登录或启用。每个当前查看者独立授权，requested_by仅用于幂等和审计。曾出现的“发布检查原创建人状态”说法已撤回；这是保持已批准共享语义的澄清，不增加创建人永久所有权。 |
| R27 撤权后的维护授权 | 保留workspace-bound GC；内部Worker根据永久WorkspaceVersion及锁定Repository的数字仓库身份一致性证明缓存归属，预约和激活均校验。仅GC分支允许维护已撤权的自有缓存，不放宽读取/抓取授权，不调用provider、不公开内容、不发布pin；原有锁序、期限、全局容量和物理证明不变。撤权本身不是删除依据，仍保护当前版本、读者、未清理进程，以及已drain但尚可发布的运行attempt；孤立pin须已被持久Job状态撤销发布资格且满足宽限期和drain证明。未知归属/无法验证布局继续计容量并留待已验证的离线恢复。代价是GC专属所有权检查及竞争测试，无法自动清理陌生目录。 |
| R28 Health可用性预算 | 整次预检共享3秒且服从更早的Health任务取消，最多16个去重pin，不等待GC容量；最多401个逻辑候选按default→PR number/base/head→既有CI顺序处理。仅核对pin、commit和根tree身份，不逐项fsck或读取内容。条目区分available/unprepared/unavailable/unknown，汇总只在全部有证据可用时为available，混合可用为partial，其余区分unavailable/unknown。逻辑候选计数仅描述冻结Health证据可见集合；分别记录物理检查次数、取得确定结论的逻辑条目数及仍需验证的条目数。旧JSON缺少这些信息时保留未知，不用0伪称已经扫描。代价是大集合会有未知，局部对象身份存在也不保证所有blob完整或远程SHA最新。 |

R29：Task 5首次Playwright命令在webServer创建子进程时遇到`spawn EPERM`，一次限定诊断确认Next和浏览器均未启动、没有测试执行。TypeScript与ESLint的正常本地检查可运行。根据用户持续开发、继续可用工作的要求，继续可逆的界面实现及独立代码审阅，保留真实行为测试和原断言；环境错误不算行为RED，也不冒充浏览器GREEN。正式构建按原脚本验证，若仍受阻就记录实际结果；不通过修改权限、替代启动通道或删除测试规避限制。浏览器和运行时验收仍为最终交付/集成门槛。代价是交互行为暂缺执行证据，后续验收可能要求返工。

Git审批服务曾因模型容量不足在执行前拒绝请求，尚未恢复。开发使用忽略目录内的不可变基线、完整差异及哈希清单进行任务审阅；不绕过Git审批、不把未提交状态称作已提交。其代价是额外的差异归档，待服务恢复及全部门槛通过后再按用户已授权的本地main集成方式完成。

Task6补充R30–R32：R29禁止重试已证实失败的Playwright→Next路径及替代启动绕过；既有Python browser_server生命周期回归不启动Next，允许正常运行，若出现新的权限拒绝则停止该路径。R31允许build/runtime同用官方固定Python3.12.13 slim-trixie，安装Git/CA并构建断言Git≥2.43；仅元数据核对不证明镜像可用。R32允许两个已实际Win5的旧模块显式使用R15目录fixture，helper只对新建自有UUID目录继承Windows权限、Linux保留0700；不改ACL/全局fixture/launcher。Workspace新flow使用模块私有schema fixture，未证明物理清理则保留schema和目录。一次原时限的安全阶段诊断再次失败，保留原503和guard记录，不重跑洗绿、不回填已丢失的旧schema证据；成本是验收未完成与保留待调查资源。

R33经只读观察审查，仅授权一次原启动命令下的branch准备+首次file读取，有限安全检查点只附既有ready/closed帧；必须finally精确还原并核对三个文件哈希。本次1通过不解释旧失败，临时诊断已完全还原。R34允许修复既有测试三处纯格式并以AST相等验证，随后一次完整pytest加maxfail=1，首失败即停、不追加诊断或清理。该运行在旧cache目录Win5处收集失败，未执行测试；按实际结果交审阅，成本是完整验证仍未完成。

R35核对全部跟踪测试位于backend/tests后，把完整常规命令明确指向tests/并保留not live_github/maxfail1；不读、删或修复旧不可读cache目录。这个收集修正不跳过失败测试。唯一运行在首测试默认tmp_path设置Win5处停止；禁止自行全局patch/权限绕过/重跑，保留设置失败和未执行全套事实。

R36静态盘点后授权剩余9文件14函数直接依赖既有自有目录fixture，优先共享auth_env/repo_env/github_settings边界，避免逐例失败后补丁；不全局覆盖tmp_path。R37基于本次queued可用时间与后续PG时钟倒序证据，仅授权collector本地drive安排exact新queued/attempt0/无租约任务为固定已到期；共享领取helper包含调度语义消费者，故保持不变。完整非live回归遇下一首次新失败立即停止，不把R36证据推及此前所有None/native失败。

R38停止反复全套启动；只给本次webhook freshNone边界添加白名单状态诊断，随后完整collector模块在前加该单例一次验证，保持maxfail1。24项通过后收敛报告/范围，未发现明确新产品缺陷，不再诊断或扩大修复。以完整pytest未通过、原native未解、Node/Docker等未执行的真实边界冻结供Task6独立审阅；通过不消除旧失败。
