# 2A 数据同步与 2B Workspace 验收记录

2026-10-22 更新：2B 代码和测试已进入运行时验收，当前失败与未执行门槛见文末。下列2026-10-19结果是2A历史证据，不能替代当前Workspace镜像、浏览器或恢复验收。

日期：2026-10-19。范围：Issue、PR、Actions 事实同步、授权读取及 health-v1。本文记录新增证据，[一期验收](foundation-acceptance.md) 的历史命令和结果保持原样。Task8本地验证已完成；随后整分支审查发现四项P2，本轮修复完整后端699项通过，独立限定范围复审确认R1–R4及M1全部解决、无新增问题。2B Workspace 未实施、页面明确不可用。

## 执行环境与命令

工作区 `D:/python/DevFlow/.worktrees/data-foundation`，基础提交 `c4d60797a47989f0dee15fcd0d5c2300e8583620`。UV0.11.25、Python3.12.13、Node24.21.0、Next16.3.7；锁文件未改。专用 `devflow-test-postgres-1` 经标签、服务和loopback55439核对为本项目测试容器，停机后只重启该目标；测试按随机 schema 隔离并清理。

所有 UV 命令从 backend 运行，设置 `UV_CACHE_DIR=D:/python/DevFlow/.cache/uv`；Node 使用预备缓存，Chromium 使用既有 `PLAYWRIGHT_BROWSERS_PATH`。命令仅移除当前进程继承的 NO_COLOR 以避免 FORCE_COLOR 冲突。完整日志在忽略目录 `.cache/task8-20261019`，精确结果在下表登记。

| 检查 | 结果 |
| --- | --- |
| `uv sync --locked` | 44 resolved / 43 checked，退出0 |
| 新 `test_data_sync_flow.py` RED / GREEN | RED2失败（未支持安装token请求）；GREEN2通过，18.18秒 |
| 新真实浏览器 `tests/data-flow.spec.ts` | 1通过，17.9秒 |
| `uv run --no-sync ruff check .` / `ruff format --check .` | 退出0，130文件已格式化 |
| `uv run --no-sync pytest -q --tb=short --basetemp ../.cache/task8-20261019/pytest-full -p no:cacheprovider` | 670通过，361.86秒，无警告；覆盖一期和2A后端 |
| `npm.cmd run typecheck` / `lint` / `build` | 全部退出0，生产路由编译成功 |
| `npm.cmd run test:e2e` | 最终64通过，1.4分钟；修正后先运行真实栈用例2通过，26.9秒；最终typecheck/lint再次退出0 |
| Compose production/dev `config --quiet`、`build api frontend gateway` | 退出0，后端/前端/网关均构建成功；Worker/migrate复用后端镜像 |
| 新隔离 Compose 迁移、readiness及服务恢复 | 退出0；0007_health_requests；API/Worker重启后ready200；本项目PG停止503、恢复200；最终项目stop成功保留卷 |

首次后端 RED 因临时目录父目录缺失有2个setup error；修正路径后才得到2个行为失败。旧/新跨模块合跑曾1失败/6通过（26.85秒）：一期断言假设每条job审计都属于私有probe；正常注册同步现已产生系统Job审计。修正为保留probe生命周期检查，同时只允许同仓库、无创建者的sync/health Job关联。没有关闭注册同步或放松生产授权。

第一次完整浏览器运行有1失败/63通过（1.4分钟）：一期审计页面的通用job.succeeded定位器匹配五条合法记录。定位改为该probe UUID所在行的成功单元格；未修改审计或生产页面。该失败与后续复验结果分别保留。

## 验收行为

本节及上表为Task8在 `400bf871b31f3dc698e2fdebdd2a20966eab237f` 前完成的历史验证；整分支修复结果见文末新增记录。

- 实际 bootstrap、允许名单、OAuth start/state/callback/cookie、发现/接入、HMAC签名webhook和UUID去重，经真实API进入正常调度器及Worker，在PG持久化两条Issue、不同身份的PR及评论/评审/文件、CI两个attempt/jobs、同步覆盖和独立健康快照。
- 真实429/Retry-After使page2暂停；数据库保留checkpoint、不提前推进水位/已处理序号。正常停止普通Worker任务，新增内容需求后启动新Worker，续接分页并追平序号、保留唯一事实，每执行单元不超过五次HTTP。此项是普通Worker生命周期证据，不能替代进程强杀验收。
- 后续Actions403留下permission_missing，保留既有事实及成功水位，健康覆盖明确不足。completed表示捕获需求已处理，不保证所有流完整。
- 浏览器只拦截外部GitHub同意跳转，读取真实API结果，显示缺失patch、完整SHA、精确CI attempt/history、health事实和Workspace不可用。实际签名移除仓库后，已加载私有Issue的刷新收到404；旧正文及导航随已知拒绝隐藏，后续sync/health/Actions均拒绝。未伪造事实或权限响应。
- 既有 owned helper 正常停止、父stdin EOF/启动失败清理保持有效；schema生成与删除受专用DB校验。固定浏览器端口31847/31848按单worker串行，避免跨spec争用。未新增产品测试后门或服务。

## 同步恢复与证据判读

1. 先看 `/health/ready`，确认数据库与镜像迁移一致；Worker也须持续存活。只读GET不创建任务，接入和签名事件/刷新才登记需求。
2. 默认收集所有当前open Issue/PR及近90天更新的closed/merged、近30天CI；15分钟补漏、5分钟增量重叠、每日核对。每单元HTTP请求配置1–5次（含签发/复核），时长15–120秒、默认120秒；HTTP10秒；整轮10000请求/24小时是可配置默认值，最多三次失败。单安装串行、单仓库一个单元、默认全局两个单元。目标事件与有界回填不移动两个补漏截止；活动任务期间到期也合并一次需求，较强每日核对不会被增量周期任务吞掉。
3. 限流查看 `retry_at`/Retry-After；不要手工推进checkpoint、调时钟或循环刷新绕过退避。Worker正常重启使用相同DB和配置；无内存状态才可续接。异常退出由租约到期回收，历史强杀测试与本次普通重启证据分开。
4. `permission_missing` 先核对App权限、安装批准、选定仓库及本地绑定；按 [App流程](github-app-setup.md#已安装-app-增加权限) 批准后重新验证/接入，再发起明确刷新。恢复事件不会授予权限。
5. `budget_exhausted` 停止自动尝试。管理员核对范围/预算后在概览点击“恢复同步”，或带CSRF、同Origin、唯一Idempotency-Key调用 `POST /api/v1/repositories/{id}/sync/refresh`，正文 `{"resume":true}`。普通成员仅可 `resume:false` 刷新。
6. 管理员回填 `POST .../sync/backfills`，正文含UTC aware `start/end`、`streams:[issues|pulls|actions]`，每次最多365天、不得未来结束。不能把不连续覆盖合并为完整区间；检查各流coverage和remaining，不能用最老事实推断完整历史。同逻辑重试保留Idempotency-Key，修改输入使用新key。
7. 健康 `POST .../health/refresh` 正文 `{}`，同样需CSRF/Origin/Idempotency-Key。快照保留生成时间、规则/阈值、实际覆盖及来源manifest；未知不是零，记录创建/更新计数不是完整事件历史，最新CI也不是PR合并许可。失败保留上次成功快照；独立health generation控制发布。
8. 目标成功只更新本次事实/集合，不建立全流覆盖，不清除别处遗留的 source_limit/permission/partial，也不更新全流历史成功时间。新发现的目标缺口可降低原无缺口状态，已有缺口不替换；检查 remaining 与集合原因了解本次执行。每日完整尝试可因部分/权限缺口而完成并推进下次尝试截止，但 `last_reconciled_at` 仍只在所有流完整时更新，避免持续缺口导致立即循环全量扫描。

## 容器及回退边界

使用全新项目 `devflow-data-acceptance-20261019` 与独立卷、合成秘密目录，loopback8080/55440在启动前确认空闲。未使用一期卷或测试PG55439。production只做配置检查；实际本地服务使用dev覆盖，未连接GitHub或申请TLS。API挂载路径核对全部来自新的Task8合成目录，未输出秘密内容。

实际Compose命令使用 `--env-file <Task8绝对合成config.env>`、`-p devflow-data-acceptance-20261019 -f infra/compose.yaml -f infra/compose.dev.yaml`。依次执行build、`up -d --wait`、`ps -a`、`exec -T postgres psql -U devflow -d devflow -tAc 'SELECT version_num FROM alembic_version'`、API内 `uv run --no-sync alembic heads`、`stop worker`、`up -d --wait worker`、`restart api`；然后仅停止此项目postgres，确认ready503，在finally恢复postgres并确认ready200，最后 `stop`。

| 镜像tag data-acceptance-20261019 | 实际容器Image ID |
| --- | --- |
| backend（API/Worker相同） | sha256:c46c607e4e9d3c4997974cc4f53b9e8b6948ce9fa7fa3379fdbedb82aabfd685 |
| frontend | sha256:720f55f3f9fc588647af440d44b172a55a49c4636890f1d5202153cc29d87fd1 |
| gateway | sha256:aeca5cc37220187392dca08fbc70e4ca2f3e7881ea656c3135cf19be891d20e1 |

迁移服务退出0，数据库revision与镜像唯一head均为0007_health_requests；网关ready200和前端login200。Worker首次stop退出0，恢复后仍Running、RestartCount0。保留两条安全worker.failure/internal_error/count1日志：UTC03:31:52.350与03:31:57.354，均处于本项目PG停止03:31:49.137至重新ready03:31:57.375之间；与注入的DB故障相关，恢复后ready200。这是预期运行故障日志，不能描述为Compose“无错误”；测试无警告/失败的结论另行成立。此容器库无已注册GitHub仓库，因此没有容器内数据采集/有用任务完成声明。

最终六个自有服务均停止，Worker/gateway/migrate/postgres退出0，API/frontend因SIGTERM退出143，无OOM。测试端口31847/31848与本地烟测8080/55440均已释放，三个项目卷保留；专用测试PG55439保持healthy供审查使用。最终只读盘点发现 `test_bd4fc060a340474ca38bf0f7b9cfb7cf` 命名空间，缺少运行前基线，归属未证实；控制器要求保留，未删除。它不是已证实的Task8泄漏，也不能算全局清理成功。没有test_browser_残留，实际helper清理标记和自身schema删除测试均通过。

上述Task8镜像及容器库的历史head为 `0007_health_requests`；本轮源码head为 `0008_sync_repair`，后续2B必须从 `0009_workspace` 开始。旧镜像不包含本轮修复，未重建、部署或重复Compose烟测。新增每日截止为NULL的既有仓库会保守核对一次；非NULL截止拒绝降级丢弃。含二期证据的其他降级保护仍有效；回退应停止本项目写入者、备份DB及密钥，在独立库恢复并匹配镜像/revision验证，参见 [部署](deployment.md#2a-同步与迁移操作)。新空库迁移/服务重启不能证明含数据生产降级或备份恢复；未追加这些执行声明。结束使用本项目 `stop`，保留卷；不执行全局清理或down-v。

## 未执行项目与阶段边界

真实GitHub App创建/权限批准、真实OAuth/私有安装、外部Webhook投递、公网TLS/部署、fork/SAML行为、接近1GB真实仓库性能、宿主机硬配额验收均为 **NOT EXECUTED**（用户尚无App）。官方资料研究、合成transport、镜像构建和本地恢复不等于这些外部验收。

2A交付只读事实与健康；2B仍需安全Git进程/对象、不可变SHA版本、tree/blob/search、保留/空间预算、代码浏览及其容器/恢复验收。后续模型配置属于第三阶段，不是本次交付；无模型密钥读取或模型API调用。

## 整分支修复验证记录（2026-10-19）

修复基线 `400bf871b31f3dc698e2fdebdd2a20966eab237f`。R1目标任务挤占周期/每日补漏、R2目标成功伪造全流覆盖、R3十秒单元不能预留请求、R4单元配置超过五次，合并为一次修复。新增持久化字段只服务独立每日尝试截止，迁移head0008_sync_repair；公共DTO形状、授权/完整Lease、HTTP10秒截止未变。可选M1增加1000项recapture上限及open→closed同PR预算复用测试。

失败与诊断保留如下，不能据后续通过回溯声称其原因已证明：

- 初次受限运行：1行为失败、10个临时目录权限setup error、2个缓存权限警告；随后授权本地运行得到9失败/2通过（7.90秒），真实复现四类问题。
- 首次聚焦GREEN：11通过（8.60秒）。拆开配置参数后临时恢复原上界，独立RED4失败/7通过（0.63秒），分别为6/100次、10/14秒未拒绝，随后恢复修复。
- 初次受影响批次：5失败/168通过（134.37秒）；四个原有目标采集缺口被过宽保留逻辑隐藏，现允许新缺口降低原无错误状态，同时保留已有无关缺口；一个真实Worker测试因registration与每日核对重复入队产生第二健康需求，现registration明确全量reconcile并参与合并。
- 独立每日尝试截止的RED1失败（1.21秒）：partial全量完成后立即重复核对；迁移RED2失败（1.33秒）：缺少列/降级保护。后续95项聚焦批次中相关迁移及M1均通过。
- 上述95项批次整体为94通过/1失败（70.78秒）：现有3000文件用例观察到0文件；原fixture已清理，无法重建当时任务状态，原因未证实。定向8项通过（19.20秒），随后3次仅该诊断用例分别通过（12.15/12.08/11.91秒），不将其称为已证明的时钟问题或flaky。采集测试helper现要求无活动任务才允许结束，并在领取失败但仍有活动任务时输出安全状态、可领取/租约/DB时间、请求数、代次及需求序号；这是防止模糊结束与后续诊断措施，不是原失败原因的证明。
- 最终受影响批次233通过/1失败（189.93秒）；3000文件诊断用例通过。失败的 `test_real_worker_heartbeat_commits_while_provider_is_pending` 已成功完成probe与heartbeat，但probe-only夹具只延后周期截止、没有延后新增每日截止，导致正常sync/health任务违背其“仅probe”断言。仅给该夹具补齐每日截止，保留断言与生产调度；随后整文件3通过（7.28秒），Ruff/check format131文件通过。无须再重复此前233项通过用例，最终全后端覆盖全部修复。

最终 `uv run --no-sync pytest -q --tb=short`：**699通过，391.22秒，无失败/警告，退出0**；代码自运行开始未改。Ruff/check format均通过（131文件），`alembic heads` 唯一0008_sync_repair，`git diff --check`通过（仅既有CRLF规范化提示）。完整日志 `.cache/whole-fix-20261019/backend-full.log`。验证对象是基线400bf871b31f3dc698e2fdebdd2a20966eab237f之后本轮11个后端文件的冻结工作树；本地 `whole-fix-source-manifest.txt` 保存逐文件SHA256，manifest SHA256为 `a8b0883ac441a1604c23c213093a9e01d4f024d10897e7294a028351a4e344db`。

提交前，正常授权的本地git暂存命令及一次同范围重试曾因自动审批模型容量不足而未执行；这不是不安全判定，未绕过审批。随后独立审查核对全部16文件SHA256与完整测试日志，批准冻结修复；本记录只追加审查结果，生产源码与已验证版本一致。前端源码/DTO形状未改，沿用历史64项结果；本轮不重复前端全套或Compose构建，不声称旧镜像含新修复。

## 2B Task 6 运行时验收（2026-10-22）

本轮复用真实API、普通Worker、专用PostgreSQL55439、真实Git对象读取和Windows supervisor。有限合成provider提供branch、annotated/lightweight tag、commit、PR及精确CI attempt；测试生成自有loose objects，唯一构造注入是 `workspace.handlers.GitCache` 的测试factory，仅截获固定fetch并复制这些对象。API、Health、retention使用生产构造器和同一自有root；没有手工prepare、伪造guard回调或替换PG授权。**合成transfer不是实际Git fetch。**

新增 `backend/tests/integration/test_workspace_flow.py` 覆盖PR固定A→普通Worker停止/重新启动→移动到B后仍读A、幂等重放、多个选择、缺commit503、签名撤权后拒绝所有旧读取。首次完整两例通过（40.84秒）；后续受影响批次及单次诊断再次出现原生读取启动失败，因此当前不能标记整体PASS。最初provider scope、LF预期等夹具错误及其修复，见Task6报告；不算生产缺陷。

受影响批次为2失败、3通过、5错误（50.28秒）：四个旧测试setup Win5、一个helper目录自拒，以及新flow的503和未drained guard。R32只对 `test_data_sync_flow.py`、`test_full_flow.py` 显式选择既有自有目录fixture，helper只将新建UUID目录改为继承Windows权限（Linux0700），未改ACL、全局fixture或启动方式。随后一次诊断为1失败、7通过、1 teardown错误（41.75秒），旧测试和两个Python helper通过，新flow第一branch读取失败。

该次read进入真实operation时尚余4.969秒，未到active_ready；在五秒截止加两秒清理窗口耗尽后记录guard_unavailable，HTTP为attempt_timeout503。这仅定位到启动/激活阶段，尚不能区分启动、导入、Job对象或DB bridge内部失败。时限和断言未放宽，未反复重跑。之前Task4的Queue.claim None与Health unknown/启动失败同样没有被本轮证明原因，不能笼统归为flaky。

原失败schema `test_debc4031cd964df7a4c156ae68fd73ed` 已被旧共享fixture自动DROP，缓存 `105411adad8041618c6fc3f9328d085a` 保留；reserved操作 `665dde0d9df04cfaba59d50dfd8f0f88` 没有物理证明。不能重建schema或用新测试通过回填历史清理。新的模块私有fixture在未drained时保留schema；诊断schema `test_workspace_flow_ed0d669d1af2410eb192ce9dae1c7bf9` 与目录 `0937ec2d9d1a48af87e5a938c90eabff` 已保留，operation `fcdbdf4ee729432fab9b3efc45a75cdb` 仍reserved、无identity/proof。所有路径均位于本工作树 `.superpowers/workspace-fixtures`；manifest保存nonce/runtime/已确认PID创建身份。未执行离线恢复，不以PID消失推断全树退出，不清理其他资源。

`frontend/tests/workspace-flow.spec.ts` 已写真实后端流程，定位代码入口、准备状态、固定SHA、文件/搜索citation及撤权后隐藏，包含窄屏检查；只替换外部OAuth同意跳转。typecheck/lint通过；浏览器 **NOT EXECUTED**。Task5原构建编译完成但TS子进程spawn EPERM使整体失败；Playwright→Next启动曾两次EPERM，无恢复证据，不再启动或改用替代通道。R30允许且本轮已通过的Python helper生命周期回归不启动Next/浏览器。

镜像/卷候选及实际验收步骤见[部署](deployment.md#2b-workspace-运行时配置已准备容器验收待执行)，恢复边界见[备份恢复](backup-restore.md#workspace-对象与固定版本恢复)。唯一源码迁移head为 `0009_workspace`。Docker/Linux、镜像构建与系统库兼容、实际API RO/Worker RW探针、容器迁移/强杀恢复、对象备份恢复、真实fetch、私有/fork/App、近1GB、TLS及宿主硬配额均 **NOT EXECUTED**。旧Task8停止的0007镜像/卷不是当前验收。Git审批容量问题未恢复，未提交/合并；第三至第七阶段未完成。

R33经独立观测审查后仅运行一次branch准备+首次file读取：1通过（9.99秒）。临时子进程检查点仅附现有ready/closed帧，原启动命令、callback、PG锁和5+2秒截止不变；运行后finally精确恢复三个原文件并核对SHA256。该read在parent start后0.875秒收到ready，真实导入/连接/授权锁/commit及退出完成；两个新guard均physical/drained、新schema已查证删除。此前失败没有复现，原因仍未解释，不改为PASS或用此次证明覆盖旧资源。完整backend suite仍暂缓，未增加其他运行。

R34随后恢复一次原代码完整backend验证，命令 `uv run --locked --offline pytest -q -p no:cacheprovider --tb=short --maxfail=1`。结果为 **1 collection error（0.15秒），退出1**：扫描既有 `backend/pytest-cache-files-9lttsmdf` 时WinError5，尚未执行任何测试。按失败即停要求停止，未删除/修改该目录、绕权限、忽略路径或重跑。最终UV锁检查通过、唯一head0009；Ruff check/format152文件通过（仅获准格式化test_git_objects.py三处，AST相同）；前端typecheck/lint通过。Task6以DONE_WITH_CONCERNS交独立审阅，完整后端、浏览器/build、Docker/Linux及前述实际运行门槛没有通过声明。

R35核对全部后端测试均位于tests/后，明确完整收集目录并保留原非live限制：`uv run --locked --offline pytest tests/ -m 'not live_github' -q -p no:cacheprovider --tb=short --maxfail=1`。唯一修正运行在第一个 `test_app_factory_auth.py::test_factory_routes_share_lifespan_services_and_database` 的默认tmp_path setup处失败：pytest-of-Q Win5，**1 error（1.67秒），退出1**，未执行测试主体。未改ACL/全局fixture、未重跑，旧不可读cache目录未碰。README正常命令同步明确tests/并保留not live_github。这两个失败分别是收集/设置障碍，不是行为RED或完整测试结果；此前未知guard和未执行门槛继续保留。

R36在静态盘点后只修9文件14个函数的显式目录依赖，改用既有workspace_tmp_path；AST证明仅参数/路径变量变化，现有13模块选择点、断言和生产权限未变。随后完整非live验收87通过、1失败（65.97秒）：事实采集helper遇queued任务，其保存的available_at比随后PG时钟晚6.791543秒。该路径只由enqueue的PG当时now写queued时间，证明本次PG墙钟观察倒序；不推广为所有历史None或native错误的原因。

R37仅在事实采集drive中，将本仓库active Job且sync/queued/attempt0/无租约的新单元安排为固定已到期；shared claim_fact_sync和调度/重试/全部断言不变。Ruff通过后直接继续完整非live验收，58通过、1失败（24.05秒），在更早的重新绑定用例fresh leaseNone处停止。该新失败没有时间对照，原因未证实；collector模块尚未到达，局部修复运行验证待完成。没有追加重跑、扩大fixture或产品修复，正式完整验收仍未通过。

R38停止反复启动整套，只在该webhook用例freshNone时增加安全字段诊断assert，不改时间/调度。随后按顺序运行完整test_fact_sync.py与该单例，**24通过（41.24秒）**，验证R37局部安排；rebinding失败未复现，原因仍未解释。最后Ruff check/format152文件全部通过。此24项是限定验证，不能替代完整pytest通过；原native失败、浏览器/build、Docker/Linux及其他未执行门槛原样保留。Task6最终以 **DONE_WITH_CONCERNS** 冻结交独立审阅，27文件范围，没有生产backend/src改动或遗留临时诊断代码。

## 2B 整候选修复与当前门槛（2026-10-23）

整候选评审 I1–I3 与 M1 已合并为一次代码修复，待范围限定的复审：retention 改为每仓库各最多100个版本/写guard的候选页并循环推进，永久历史不再触发总数熔断；共享pin、当前分支、open PR、仍可发布的attempt继续由数据库完整证据保护。ready代码视图在无关同步轮询时保留目录/行范围/搜索，显式重新确认与拒绝/身份不符仍隐藏旧内容。原生回归按Windows/Linux实际身份字段断言；Health区分 `verification_unavailable` 与 `verification_limit`，未知可用性和原预算不变。

新增真实PG聚焦回归先得到5失败/2通过（10.54秒），修复后7通过（28.88秒）；扩展边界用例时分别遇到冻结DTO赋值和Fact缺少必填source_url的夹具错误，已修正，最终 **11通过（30.62秒）**。测试直接安排自有旧元数据，Git检查/删除边界使用替身，不是原生进程、实际pin删除或完整Workspace验收。正常前端typecheck/lint和3个变更Python文件的Ruff/格式检查通过。新增4个浏览器用例未执行；相关3个原生用例只完成collect-only，Linux实际执行仍 **NOT EXECUTED**。详尽命令、结果和文件哈希保存在 `.superpowers/sdd/2026-10-13-devflow-workspace/whole-fix-report.md` 及同目录source manifest。

数据库连接已恢复，WSL2后端已确认，但墙钟稳定性未恢复为已证明状态。保存的R41观测为1通过/1失败（29.22秒），失败activation实际判断 `guard_valid=false` 时仍剩4.157秒单调时钟预算；另一次同一PG连接32个采样、3.172秒跨度中出现单次 **-10.151906秒** 倒退。这些证据不证明OS/VM根因，不解释所有历史Queue/native失败或exit3221225477，也不授权放宽生产时间/租约检查。

**G1 原生/最终Workspace流程、G2 完整后端、G3 浏览器/构建/360px、G4 部署与恢复继续OPEN。** 本轮未重启完整suite、旧诊断、Docker/Linux、Node/Next构建或Playwright启动链。没有提交/合并/部署、云端或第三阶段实现声明；聚焦通过不能关闭Task6或Phase2B。

## 2B 门槛复测（2026-11-02）

3A 整体复审确认 GREEN/交付后，按交接顺序复测 G1–G4。仅使用专用测试库 `127.0.0.1:55439/devflow_test`；未提交、未合并、未部署，未使用真实 GitHub App 或云模型。

| 门槛 | 命令 | 结果 |
| --- | --- | --- |
| G1 原生 Workspace 流程 | `uv run --no-sync pytest tests/integration/test_workspace_flow.py -q -p no:cacheprovider` | 2 passed in 41.19s |
| G1 原生读取 | `uv run --no-sync pytest tests/integration/test_workspace_reads.py::test_real_git_reads_use_production_database_guard_and_drain tests/integration/test_workspace_reads.py::test_native_reader_cancel_joins_supervisor_and_acknowledges_physical_drain -q` | 2 passed in 9.21s |
| G2 完整后端 | `uv run --no-sync pytest tests/ -m 'not live_github' -q -p no:cacheprovider --tb=short --maxfail=1` | **467 passed, 1 failed**（545.34s），停在 `test_sync_engine.py::test_failed_round_refresh_preserves_budget_until_explicit_resume[False-time]`；`env.queue.fail(lease, ...)` 收到 `None` |
| G2 失败项隔离复跑 | 同上节点（4 个参数组合） | **2 failed, 2 passed**（4.09s）；`[False-time]`/`[True-time]` 在 `unreachable_collector` 触发 `Exhausted round must not reach a collector or HTTP` |
| G3 前端 typecheck/lint | `npm.cmd --prefix frontend run typecheck` / `run lint` | PASS |
| G3 前端构建 | `npm.cmd --prefix frontend run build`（未使用任何安全绕过环境变量） | PASS |
| G3 浏览器/360px | `npm.cmd --prefix frontend run test:e2e -- tests/workspace-flow.spec.ts` | 修复定位器后 **1 passed (23.9s)**，用例内含 `setViewportSize({width:360,height:800})` |
| G4 Compose 静态语法 | `docker compose -f infra/compose.test.yaml config --quiet` | PASS |
| G4 生产 Compose 静态语法 | 使用仅用于插值的虚构非秘密值执行 `docker compose -f infra/compose.yaml config --quiet` | PASS；未启动服务、未读取/创建任何秘密 |
| G4 真实 Linux/镜像/卷/强杀/恢复 | — | **NOT EXECUTED**：无 `.env` 与部署秘密目录，生产 Compose 直接 `config` 因缺少必填变量失败；未构建/启动生产栈 |

G2 失败原因已量化而非猜测：同一时刻宿主机时钟为 `1793612048.9`–`1793612049.2`，测试 PostgreSQL `clock_timestamp()` 为 `1793612034.47`，**PG 墙钟比宿主慢约 14.4 秒**。该用例用宿主 Python 时钟写入 `round_started_at = now - 24h`，而 `reserve_request` 用 PG 时钟加 10 秒余量判断整轮 24 小时预算；14.4 秒偏差使 `now + 10s > round_started_at + 24h` 不成立，因此未在 collector 前抛出 `budget_exhausted`。完整套件中同一节点的 `lease is None` 是既有 Queue 领取不稳定，与 3A 改动无关。未放宽生产时限、未修改测试以强制通过；G2 仍为 **OPEN**。

G3 首次运行失败（两次，120s 超时等待“版本来源”）。根因是 `workspace-flow.spec.ts` 用 `getByLabel("版本来源", { exact: true })` 匹配包裹 `<select>` 的隐式标签；Playwright 的 label 文本包含选项文本，精确匹配数量为 0（用最小页面复现：exact=0、loose=1）。已改为 `getByRole("combobox", { name: ..., exact: true })`（同样修正“PR 侧”），仅测试定位器变化，产品代码未改；修复后通过。G3 的浏览器/构建/360px 门槛因此可视为本轮通过，但真实部署环境下的浏览器验收仍不包含在内。

**G1 本轮四项原生用例通过；G2 仍 OPEN（环境时钟偏差 + 既有 Queue 不稳定）；G3 本轮通过；G4 仅静态语法通过，真实 Linux/镜像/卷/强杀/恢复仍 NOT EXECUTED。** 2B 整体不能标记为完整验收，3B 的真实 Milvus/模型资源门槛也尚未开始。
