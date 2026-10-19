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

2A 不提供健康总分、GitHub 完整事件历史、CI 日志、第三方 CI、模型/RAG、Git 克隆、文件浏览或代码搜索。Workspace 明确不可用，迁移 `0008_workspace` 是未实施的 2B 工作。真实 App、私有安装、外部投递、TLS 和接近1GB仓库验收均为 **NOT EXECUTED**；本地证据见 [2A 验收](data-foundation-acceptance.md)。


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

## 2B R1–R10交接裁定（尚未实施）

以下是已裁定的2B约束，来源为2026-10-15控制器preflight台账及其澄清。保留它们便于下一计划执行；不表示2B代码、测试或进程安全已完成。下一迁移是0008_workspace，依赖2A0007_health_requests。原preflight与crash-lifecycle-decision-prep.md仍在本地SDD目录供执行者详细阅读。

> Ruling R1: workspace migration is0008_workspace with down_revision finalized2A0007_health_requests; Task8 of2A updates tracked plan reference before2B — prevents duplicate revision — no schema change beyond planned sequence.

> Ruling R2: authorize narrow native CreateProcessW adapter in windows.py retaining process/thread handles and atomic Job association with documented startup attributes; contained suspended launch resumes only after verified setup, any unsupported/failing containment fails closed. Linux retains synchronousPopen; threaded runner contract unchanged — CPythonPopen discards required primary-thread handle — small platform adapter departs literalPopen wording only onWindows.

> Ruling R4: low-level ProcessResult carries bounded binary stdout plus safe status, never rawstderr; TreeEntries includes validated mode/OID/rawpath plus complete/truncated/scanned/reason within100000cap. fetch/tree/blob propagate caller cancel and monotonic absolute deadline; Task2/3 own final public DTOs beforeUI. Empty path accepted only as root — prevents unbounded lists/reset timeouts and incompatible consumers — explicit small internal metadata additions.

> Ruling R5: content deduplication by numericrepo+SHA never deduplicates public snapshot provenance/auth epochs. Public snapshot records bind source selection and source/base repo scopes, idempotency actor+repo+selection; forkhead checks bothregistered authorizedrepos perread — fixedSHA alone is not authority — may create multiple small records for sameobjects.

> Ruling R7: Task4 may narrowly edit git/process/read/config to wire budget monitoring/sharedlocks/pins. Health freezes known available/unprepared/unavailable evidence without eagerfetching; eviction retainsidentity — one lifecycle implementation and bounded traffic — availability may remainunknown untilprepare.

> Ruling R8/R9: rawobjectfixtures/localfetch use explicit test-only injected boundary, production remains fixedHTTPSgithub.com. Linux focused tests can use owned isolated test runtime withGit/devdeps beforeTask6 production packaging, use sandbox escalation ifneeded; final productionRO/RWchecks stillrequired. RealApp/privatefork/near1GB/TLS/hosthardquota remainNOTEXECUTED untilactualevidence — no fake externalacceptance — additional testimage cost.

> Ruling R10: extend existingCursorCodec with explicit authorizedmax default100 and workspacepurpose usage200; allpriorendpoint capsremain100 and existing cursorpurposebinding remains — treepages need200 — narrowpaginationextension withcrosspurpose/maxregressions.

> Ruling R3/R6: adopt the bounded fixed per-operation supervisor plus shared durable physical-cleanup guard described in crash-lifecycle-decision-prep.md; extend planned reader-pin model rather than create second registry/queue. Ordinary API/Worker parent crash triggers owned-pipeEOF or monotonicdeadline, whole-tree termination/reaping and nonce-fenced directDB drain acknowledgement; oneactiveGit treeperadmittedread,4globalreadslots,1globalfetch/GCslot. Supervisor owns shared/exclusive repoOSguard onprecreatedfiles (APIread-only). Expiry ends authority, never proves physicaldeath; reserved-before-activation canCASrevoke, activeuncertaincleanup retainscapacity/GCexclusion untilpositiveproof. CrossPIDnamespace absence isnotproof. Exceptional supervisor/DB-ack failure uses bounded same-incarnation reconciliation or verifiedoffline owneddeployment stopbefore scopedguardrecovery — parent-ownedlocks andTTLalone permitstalewriter/GC overlap — costs onefixedstdlibhelper pluscommonpinfields and may require owneddeployment downtime inexceptional cases. NoDaemon/Dockersocket/newservice/privileges/genericcommandAPI. Existing600s/5s deadlines includeadmission andcannotresetperGitcall; boundedcleanup <=2s beginswithindeadline, inabilitytoproveexit returnssafeerrorwhileguardremainsclosed. Do notclaim literalOSlocksurvivesownerdeath; durableguardmaintainseffectiveexclusion untiltreeproof. Task1 verifiesOSlifecycle; Task2/3/4 addDBguards/admission/GC, onlycompleteintegrationcanclaimcrashsafety.

> Ruling R5/R4 clarification: authorize base repository first. A missing/inaccessible snapshot within an otherwise authorized base uses a distinct local workspace_not_found error; unavailable fork source uses fork_source_unavailable. Base-repository denial keeps existing repository denial semantics. This lets the UI hide denied full-head code while preserving still-authorized base PR metadata, and avoids treating every workspace404 as revocation of the whole repository — follows spec fork metadata/full-code distinction — one finite error distinction shared byTasks2/3/5. Foreign source authorization is still checked on every read and no guessed snapshot/SHA becomes readable.
