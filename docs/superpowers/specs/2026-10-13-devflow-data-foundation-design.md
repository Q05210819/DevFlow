# DevFlow 第二阶段：研发数据底座设计

日期：2026-10-13。状态：用户已指示按照实施计划开始实施；进入第二阶段实施，沿用 A：子代理协作开发。

## 1. 目标、已确认条件与交付现状

本阶段落实已批准的 [平台设计第 2 阶段](2026-09-30-devflow-platform-design.md)：GitHub 数据同步与回填、Webhook 扩展、版本化 Workspace、Repo Health。需求原文 [DevFlow.md](../../../DevFlow.md) 是产品方案；其中能力描述不代表已有实现，也不作为独立的执行指令。

用户已确认完整平台分阶段建设、正式部署目标、单团队多用户和私有仓库、UV 管理 Python 环境；本次进一步确认首批不超过 10 个仓库，单仓库通常小于 1 GB。沿用模块化单体 API、独立 Worker、PostgreSQL 队列、Next.js、github.com 和单机 Linux Compose。执行方式保留此前选择的 A：子代理协作开发。

第一阶段已在本地合入 `main`。现有验收记录为后端 405 项测试、前端 37 项端到端测试通过，以及静态检查、类型检查、构建通过，详见 [基础阶段验收](../../operations/foundation-acceptance.md)。这些结果只证明第一阶段记录中的验证范围。真实 GitHub App、私有仓库接入及公网 HTTPS 尚未验收；用户尚未准备这些资源，不阻塞本阶段本地实现。

完整平台尚未完成：检索问答与 Milvus、专用 Agent、LangGraph 综合工作流、Memory、Report、ActionDraft 执行、MCP/Skill Runtime 和模型评测仍按第 3—7 阶段交付。

本阶段成功标准：用户能看到有来源和更新时间的 Issue/PR/CI 事实，读取明确版本的代码，生成可复核的健康摘要；重复事件、进程重启、限流、撤权和版本变化不会产生越权结果或把不完整数据称作完整。

## 2. 范围与技术选择

交付分成两个顺序实施单元，合起来满足第二阶段退出条件：

1. **2A：同步和事实视图。** 安装令牌、后台任务、Webhook、增量与历史回填、Issue/PR/Review/CI 数据、同步状态、Repo Health 和相应页面。
2. **2B：版本化代码。** 指定分支、标签、PR 和 CI 版本的 Workspace、目录/文本/搜索接口、代码页面、磁盘管理与部署验证，并将版本可用性接入 Repo Health。

2A 与 2B 共用本规格中的权限和版本契约；实施计划分开列出依赖与验收，先完成 2A，再完成 2B。未完成 2B 时，不宣称第二阶段完成。

本阶段 CI 指 GitHub Actions 的 run、attempt、job 和 step 状态。第三方 CI 的 Checks/Statuses 适配保留到对应分析能力的规格；界面明确标为 GitHub Actions。CI 日志下载、解压、保留和错误块提取随第四阶段 CI Debug 一并设计，本阶段提供准确的 run/attempt/job 来源链接，不生成根因分析。知识库导入和 RAG 随第三阶段交付。

| 选择 | 收益与代价 | 决定 |
| --- | --- | --- |
| Webhook + 周期补漏 + PostgreSQL 分批任务 | 复用现有设施；需要持久游标和重启恢复 | 采用 |
| 仅按需直接调用 GitHub | 接入简单，但重复请求多，无法提供可靠回填及后台健康概览 | 不作为数据底座 |
| 新增消息总线和独立同步服务 | 可独立扩容，但增加部署、恢复和权限边界 | 当前规模不引入 |

Workspace 使用服务管理的裸 Git 对象库，通过固定 SHA 读取原始对象。与完整工作目录相比，它避免执行仓库构建、过滤器或依赖安装；与逐文件 REST 调用相比，它适合有界代码搜索。Git 是运行时工具，Python 继续使用 UV 锁文件；无需 GitPython、Redis 或 Celery。LangChain、LangGraph、Milvus 和模型 SDK 留在首次使用它们的阶段。

## 3. 可见功能与交互

仓库列表增加同步状态和进入详情的入口。详情页包含概览、Issues、PRs、GitHub Actions、代码五个区域；每个区域独立展示加载、空数据、部分覆盖、旧快照、限流和失败状态。

- 概览显示最后成功采集时间、覆盖范围、打开的 Issue/PR 数量、近期活动和 CI 结果。用户可刷新事实并生成新的健康摘要；处理中显示持久进度。
- Issue 列表与详情显示标题、正文、状态、标签、负责人、评论、源链接和更新时间。
- PR 列表与详情显示 draft/merged 状态、base/head SHA、变更文件摘要、讨论、Review 及 review comments；区分当前版本与旧提交上的审查。
- Actions 列表与详情按 run/attempt 展示工作流、事件、分支、head SHA、job/step 状态和 GitHub 链接。无 Actions 数据不等于构建通过。
- 代码页面先选择分支/标签、PR base/head 或 CI head；解析后始终显示完整 SHA。目录、文件片段和文本搜索都绑定同一版本。引用包含仓库 ID、SHA、路径和行号。
- 管理员可请求额外历史范围，查看同步错误和存储占用；管理员身份本身不授予仓库内容权限。

列表使用服务端分页与稳定游标，默认每页 30、最多 100。源 Markdown 以安全方式渲染，不执行 HTML；仓库正文、代码和日志链接始终视为数据。浏览器不直接请求私有 GitHub API，不接收令牌、磁盘路径或内部后台任务输入。

## 4. 授权、凭据和任务身份

### 4.1 用户读取

所有事实、Repo Health、Workspace 元数据及内容接口，继续要求平台用户有效、仓库启用、安装允许且可用、用户本人当前可访问仓库。复用 `RepositoryPolicy`、授权版本和最长 60 秒的权限缓存；提交刷新、回填或 Workspace 请求时强制新鲜校验。

读取缓存数据不能跳过用户权限。读取内容前后核对本地授权版本；已观察到的撤权立即生效，外部未推送的变更受现有短缓存上限约束。权限无法验证时拒绝继续交付数据。浏览器在退出、撤权或仓库切换时清除相应内容缓存；API 响应不进入公共 HTTP 缓存。

### 4.2 后台同步

安装令牌只用于已登记仓库的后台只读同步。每次签发限制为目标 `repository_ids` 和所需的读取权限，内存缓存不超过令牌过期时间并提前刷新；不写入数据库、URL、命令行、Git 配置或日志。安装令牌可收窄仓库和权限，其生命周期按返回的 `expires_at` 处理。[GitHub 安装令牌说明](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)

新增小型后台仓库范围对象，只携带仓库、安装和相应授权版本，不能伪造用户 `Actor`。开始远端操作和提交数据时均检查安装/仓库状态。后台任务不依赖最初登记管理员继续登录；单个用户离开团队不会删除其他合法用户的数据。安装被移除、暂停或仓库被禁用后，旧任务不能继续发布。恢复沿用第一阶段的管理员重新验证登记流程，后台同步不自行解除撤权。

现有用户访问检查任务仍为创建者私有，原 REST/SSE 可见性保持。新增系统同步任务没有必填的用户创建者，使用明确的系统身份和数据库幂等约束；人工刷新另记审计发起者。仓库共享进度通过仓库同步资源读取，不放开现有私有任务查询接口。

GitHub App 在现有 Metadata: read 基础上增加 Issues、Pull requests、Actions、Contents 的 read 权限。用户和安装必须完成新权限授权；缺少某权限时相应数据流显示 `permission_missing`，不误报为正常空数据，也不阻止有权限的数据流。此阶段不申请 GitHub 内容写权限。

## 5. 事实模型与版本契约

仅保存功能需要的字段及规范化正文，不持久保存整份 Webhook、HTTP headers 或任意原始响应。所有事实绑定本平台 repository UUID，并保留 GitHub 数字 ID、源 URL、源更新时间、采集时间和删除/不可用状态。

| 数据 | 身份和必需版本信息 |
| --- | --- |
| Issue | repository + GitHub issue ID；number、state、title/body、labels/assignees、源时间 |
| PullRequest | repository + GitHub PR ID；另存 issue ID/number；base/head 仓库 ID、ref、SHA、draft/merged、merge commit SHA |
| Comment / Review | repository + 类型 + GitHub ID；父 Issue/PR、作者、body、状态、commit SHA；review comment 另存 path、side、line/original_line |
| PRVersion / PRFile | PR + base/head SHA 的采集版本；文件路径、旧路径、状态、增删统计；patch 可缺失或截断，明确标记 |
| CIRunAttempt | repository + run ID + attempt；workflow ID/path、event、branch、head SHA、status/conclusion、源时间 |
| CIJob | repository + job ID；所属 run/attempt、head SHA、job 状态和 step 序号/名称/状态 |
| RepositorySync / SyncCursor | 仓库调度状态、待处理序号、同步代次；分数据流的窗口、页进度、已完成水位、覆盖与错误 |
| WorkspaceVersion | repository + commit SHA；获准来源、对象完整性、授权代次、最后使用时间、可用/待准备/失效状态 |
| HealthSnapshot | repository + snapshot ID；规则版本、采集范围、输入版本清单、计数和解释 |

Issue API 也返回 PR，必须按 `pull_request` 区分；issue ID 不可当作 PR ID。列表默认参数不能隐式决定产品范围，应显式指定状态与排序。[GitHub Issues API](https://docs.github.com/en/rest/issues/issues)

PR 文件、Review 和评论的集合带自己的完整性标记。采集前后重读 PR 的 base/head；若变化，当前版本标记待重采，不把旧 patch 或 Review 当成新提交的证据。旧 Review 的 `commit_id` 仍保留。文件 API 存在 3,000 文件上限，达到上限且无法证明完整时标记 `source_limit`，不能宣称完整差异。[GitHub Pull Requests API](https://docs.github.com/en/rest/pulls/pulls)

CI attempt 是独立版本：rerun 不覆盖原 attempt 的 job 状态。读取指定 attempt 的 jobs；发现更新 attempt 时另建记录。`head_sha` 只是运行关联信息，不推定等于 PR 最新 head，也不推定等于 checkout 步骤实际选择的提交。页面明确这种区别。

## 6. 同步、回填与一致性

### 6.1 默认覆盖与预算

下表是本阶段建议默认值，不是用户对容量或性能的承诺；可通过经校验的部署配置调整。

| 项目 | 默认值及行为 |
| --- | --- |
| 初次 Issue/PR 回填 | 所有当前 open 项，以及最近 90 天更新的 closed/merged 项；分别记录覆盖条件 |
| 初次 CI 回填 | 最近 30 天创建的 run，并读取其已有 attempt/job 信息；窗口外被 Webhook 指定的运行可单独同步 |
| 评论和审查 | 已纳入范围的 Issue/PR 的讨论与 Review；分页完成才标记集合完整 |
| 手动历史回填 | 管理员指定 UTC 起止区间，每次最长 365 天；可分次向前扩展，不宣称默认全历史 |
| 普通补漏 | 每 15 分钟一次；最近 5 分钟增量重叠；open 项定期重新枚举 |
| 全覆盖核对 | 每天重扫当前配置窗口和 open 项；对已纳入范围的子集合按批次核对 |
| 同步并发 | 每仓库最多 1 个事实同步执行单元；默认全局最多 2 个，Worker 总并发仍受既有配置限制 |
| 远端请求 | 同一安装的后台事实同步由单一有效执行者串行请求；分页最多 100 项；限流后持久延期 |
| 一个事实工作单元 | 最多 5 次 GitHub HTTP 请求，包含页面、详情、版本复核和该单元的令牌签发；HTTP 单次沿用 10 秒总截止，整个单元沿用 120 秒上限，失败最多 3 次 |
| 单个目标的自动补采 | 每轮最多 3 次版本变化重采；持续变动后标记待后续同步，避免无限追赶 |

按需回填不会提高用户对其他仓库的权限。超出配置或 GitHub 限制时，结果必须保留已完成范围、剩余范围和原因。正常分页延续创建下一工作单元，不算失败重试；整轮还有持久请求计数与截止时间，默认最多 10,000 次请求或 24 小时，先到者使其进入 `budget_exhausted`，由管理员显式继续，避免重启后预算重置。

### 6.2 持久调度

登记成功、人工刷新、Webhook 和 Worker 定时扫描都只产生持久同步需求。每仓库维护 `requested_sequence` 与 `processed_sequence`：采集开始固定本轮处理的序号，结束只能推进到该序号，不能清掉执行期间到达的新事件。

Webhook 验签和 delivery ID 去重沿用第一阶段。新增 `issues`、`issue_comment`、`pull_request`、`pull_request_review`、`pull_request_review_comment`、`workflow_run`、`workflow_job`、`push`、`create`、`delete`、`repository` 的相关事件。解析只提取经校验的安装、仓库、对象 ID 和事件动作；回执与标脏/入队在同一事务提交。对未登记仓库不自动登记。未知事件记录为忽略。

正文事件作为刷新提示，由 Worker 重取当前事实，避免按乱序旧 payload 回滚新状态。仓库/安装生命周期事件仍同步执行权限失效；内容处理失败不能延后撤权。删除事件只标记目标待核实：在独立证明父仓库及相应数据权限仍可读后，目标确实不存在才标记不可用；普通 403/404、缺页或失败遍历不能触发批量删除。

系统任务采用 `(repository_id, kind, idempotency_key)` 的系统专用唯一约束；用户任务保留原创建者维度。仓库同步行串行合并需求，数据库约束防止多 Worker 同时创建重复活动同步。没有独立 scheduler 服务，也没有第二套队列。

事实同步 claim 先按既定顺序锁定安装，再检查该安装是否已有有效的运行中事实任务；有则暂不领取另一个，不让排队等待消耗执行租约。所有 Worker 通过同一数据库入口执行此规则，不能用进程内 semaphore 假装跨进程串行。成功领取后释放事务锁，再串行发出该单元的 HTTP 请求。限制不覆盖 API 代表用户的权限检查、Workspace 的 Git 传输或其他类型任务的令牌签发；它是后台事实同步的调度约束，不宣称整个 App 的所有网络包严格串行。失租时取消当前调用；已发出的请求可能短暂重叠，但受单次截止时间和发布隔离约束。

### 6.3 游标与提交

区分授权版本、同步代次和 Job lease generation：分别回答“还有权限吗”“属于哪轮采集”“哪个执行尝试可以写”。每一页事实、checkpoint、后续任务和最终发布都受这三者约束。

HTTP 在事务外执行。提交采用短事务，按 Installation → Repository → SyncState → Job 顺序锁定；涉及用户发起者时先锁 User。事务中复核授权、同步代次及完整 Lease 的 owner/generation/expiry，之后同时保存事实与游标。失去租约的 Worker 即使拿到成功响应也不能写事实、推进水位、发布健康结果或后续任务。

checkpoint 不只保存页码，还保存页内待处理目标、当前目标阶段、子集合页进度和 PR 采集前的 base/head。每次远端调用都计入单元及整轮预算，在带租约校验的短事务中先预占整轮请求额度；崩溃可以保守多计一次，不能重置消耗。达到正常单元请求上限或剩余时间不足时保存进度并成功续接，不走失败重试。一页 100 个目标不要求在同一个单元内全部取完。JSON 响应流设 16 MiB 上限；超限记录明确错误和缺口，不无限缓存响应体。

处理器接收完整 Lease，队列按有限任务类型分发；结果按类型验证。保留既有五种 Job 状态和有界重试。后台失败不因 `requested_by = NULL` 而跳过终态提交或审计。共享进度放在同步记录，RunEvent 继续只包含安全状态字段。

数据流分别记录水位：只有对应范围完整遍历后才能推进“已完成”。页数据可先可见，但页面必须显示部分覆盖和实际采集时间；不做整库双份复制来制造不存在的 GitHub 全局快照。分页在源更新时不是强一致快照，通过时间重叠、目标补采及定期核对修复漏项；健康结果保留这一限制。

Issue 增量使用显式 `state=all` 与 `since`，另扫所有 open；PR 用更新时间排序、窗口截止和目标详情补采，不能假设存在与 Issue 相同的 `since` 参数。子集合重新枚举完成后才对缺失成员标记不可用；不完整遍历不改变已有成员的存在状态。

Actions 以固定 `created` 时间窗枚举，并重新检查已知未结束 run、近期 run 和 Webhook 指定 run。API 的筛选查询最多返回 1,000 条；密集窗口继续拆分，最小时间窗口仍触顶时记录覆盖缺口。补漏不能仅扫描“新创建”，否则会漏掉旧 run 的重跑。[GitHub Workflow Runs API](https://docs.github.com/en/rest/actions/workflow-runs)

尊重 `Retry-After` 和 rate-limit reset，不在进程内长时间睡眠占住租约；重试时间持久化。条件请求只在端点支持且本地拥有对应完整响应/分页状态时使用；首个页面 304 不代表整个数据流完整。采用 Webhook 和串行请求降低轮询及次级限流压力。[GitHub REST 最佳实践](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)

## 7. 版本化 Workspace

### 7.1 版本解析和授权范围

用户提交结构化选择：branch、tag、PR number + base/head 或 CI run ID + attempt；服务端从获准仓库的 GitHub 事实解析完整 commit SHA，不接受任意 Git revision 表达式、远端 URL、shell 命令或本机路径。已有获准版本可以直接按 snapshot ID 读取。

branch/tag 名只参与受校验的 ref 解析。引用变化后创建新版本，已发布旧版本仍指向原 SHA。PR 固定 base/head 两侧；CI 显示 run/attempt/head SHA 的对应关系。抓取到的 ref 与解析出的 SHA 不一致时重新解析或报告 `version_changed`，不得静默改成默认分支。

对象在磁盘中存在不构成可读依据。WorkspaceVersion 必须绑定目标仓库和经验证的 ref/PR/CI 来源。任意猜测 SHA、其他仓库对象、裸对象库中的未发布对象均不直接开放。

fork PR 的元数据遵循 base 仓库权限；完整 head 代码要求 base 和 head 仓库均已登记且用户和 App 分别有权访问，每次读代码复核两侧。传输可用 base 仓库的 PR head ref，但不能把可抓取当成源 fork 授权；验证抓取 SHA 与 PR head 一致。缺少源仓库授权时仍可显示 base 仓库允许的 PR 元数据和变更摘要，完整 head 工作区显示 `fork_source_unavailable`。这是平台首期的保守范围规则，不是声称 GitHub 总是要求两侧独立授权。[GitHub PR 本地检出说明](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/checking-out-pull-requests-locally)

### 7.2 Git 存储与进程边界

按稳定数字仓库 ID 建服务自有裸 Git 对象库，只抓所需的显式 ref；不做全 refs mirror，不初始化 submodule，不获取 LFS 对象，不执行仓库代码、hooks、构建、checkout 或 smudge/textconv。第一版保留完整所选版本所需的对象，不启用会在读取时隐式联网的 partial clone。遇到大历史以容量/时间限制明确失败，不后台无限加深。

所有 Git 命令使用参数数组和 `shell=False`，仓库 URL 由服务端已验证身份构造且固定 HTTPS github.com。隔离系统/用户 Git 配置、模板、hooks、credential helper、SSH、协议和代理环境；禁用交互、重定向和自动维护。拒绝外部对象 alternates。令牌只在 Worker 子进程私有环境中提供，通过 Git 的 `--config-env` 读取认证 header，不能出现在 argv 或落地配置。[Git 参数和配置](https://git-scm.com/docs/git)

抓取过程写入本执行尝试唯一的 ref 命名空间；旧进程不能移动已发布 ref。每仓库实际写进程持有操作系统排他锁，锁覆盖子进程直至退出；跨 Worker 不能仅凭超时租约假设旧 Git 已停止。自动 GC 关闭，清理与 fetch 使用同一写锁。

API 只读挂载对象卷，没有安装令牌，不触发懒加载抓取。使用 `ls-tree -z` 解析树、用验证过的 blob OID 调用 `cat-file` 原始读取；检查对象类型和大小，不跟随 symlink。blob 不通过宿主文件路径展开。gitlink、LFS pointer、二进制和非 UTF-8 文件显示类型及限制，不冒充完整文本。[Git ls-tree](https://git-scm.com/docs/git-ls-tree)、[Git cat-file](https://git-scm.com/docs/git-cat-file)

路径按 UTF-8 相对 POSIX 路径精确匹配，区分大小写且不做 Unicode 归一化；不支持的路径字节、控制字符、反斜杠、空/`.`/`..` 分量和超过 4,096 字节的路径明确标记，不改写成另一个文件。行号按原始 blob 的 LF 分隔计算，CRLF 不重复计行；引用使用完整 SHA 和逐段编码的路径。

Windows 本地开发保留现有 Selector 事件循环。Git 通过有界线程执行同步 `subprocess.Popen`，不调用不受该循环支持的异步 subprocess API；取消必须通知执行线程、终止并回收完整子进程树，再释放并发槽和写锁。Windows 使用受控 Job Object，Linux 使用受控进程组；分别验证超时、失租和优雅停止，包括 Git HTTP helper。取消协程不等于取消进程。

### 7.3 发布、限制与回收

抓取和校验完成后，固定 SHA 的对象引用先可用，再在带授权/同步代次/租约校验的事务中发布 WorkspaceVersion。数据库发布失败只留下不可访问的孤立对象，重试可复用校验完成的对象；文件系统操作不冒充跨系统原子事务。已发布版本不被旧任务覆盖。

| 项目 | 默认限制 |
| --- | --- |
| Workspace 抓取 | 全局最多 1 个、每仓库最多 1 个；单次 10 分钟，失败最多 3 次 |
| Git 对象缓存 | 每仓库软预算 2 GiB、全局软预算 20 GiB；计入 pack、临时数据和保留版本 |
| API Git 读取 | 全局最多 4 个子进程；一次请求 5 秒和明确输出字节上限 |
| 目录读取 | 分页最多 200 项，返回是否还有条目；扫描最多 100,000 条树项，触顶明确说明 |
| 文件正文 | blob 最大 1 MiB，每次最多 500 行、响应正文最多 256 KiB；不从大文件偷偷截一段称作完整 |
| 文本搜索 | 固定 SHA 下的字面文本搜索，最多扫描 2,000 个文本文件或 20 MiB、最多 100 条结果；到限返回 `truncated` 和扫描范围 |
| 版本缓存回收 | 未使用 7 天的非当前版本可回收；正在读取/抓取以及当前默认分支、open PR 所需版本受保护 |

这些是可调的工程默认值。Git fetch 没有在本方案中依赖一个假定的精确字节上限；进程监测软预算存在超调，正式部署必须给 Workspace 独立的受限文件系统/磁盘配额，并为临时文件预留空间。普通 Compose named volume 本身不被描述为硬配额。容量不足停止新抓取、报告 `storage_exhausted`，不删除正在使用的证据来凑空间。

按服务端生成的卷内路径回收，防路径逃逸；读者通过数据库短期使用租约 pin 住 snapshot，清理先禁止新读并等待读者退出，再删除引用/对象。读操作有 5 秒上限，崩溃后的 pin 有明确过期时间。清理状态可恢复；绝不对用户工作目录或任意请求路径递归删除。旧版本回收后引用仍保留版本标识并显示不可用，可尝试重新准备，但不承诺远端已删除版本可永久恢复。

## 8. Repo Health

第一版为确定性摘要，不依赖模型。规则版本固定为 `health-v1`：

- open Issue/PR 数、无负责人 Issue 数、draft PR 数、超过 7 天未更新的 open 项数量；阈值在输出中可见。
- 最近 7/30 天纳入覆盖范围的活动计数；不足窗口时显示实际范围。
- 默认分支最近工作流运行，以及指定 PR head 对应的 Actions 状态；按 workflow/run/attempt 分组，不用别的 SHA 上的成功覆盖当前失败或未知。
- 当前默认分支、open PR 和最近 CI 对应的代码版本是否可读取；2B 未交付前此项明确不可用。
- 数据流最后成功时间、完整性、限流/权限/预算缺口，以及结论依赖的来源链接和版本。

不生成一个掩盖缺项的“健康总分”，不自动给出可合入结论。无记录、未启用、未同步、部分覆盖、已过期和失败分别表达。CI 统计保留 success/failure/cancelled/skipped 等类别，不把未执行项折算成成功。

一次健康摘要在短的数据库一致性读取中冻结输入清单和计数，保存采集区间、分数据流进度、PR base/head、CI run/attempt/head、Workspace SHA/可用性及规则版本。冻结输入时在仓库同步行锁下分配递增的 `health_generation`，与事实同步代次分开。发布除检查授权及任务租约外，只有比当前已发布代次更新的摘要可以推进当前指针；较早冻结输入的任务晚完成时只能保留为历史记录，不能覆盖较新的结果。人工与自动健康请求共用此入口，并持久合并尚未处理的需求。失败时保留上次完成结果及其生成时间，并显示新的刷新失败；不能把它标为刚刚更新。

## 9. API、错误和运维边界

沿用 `/api/v1/repositories/{repository_id}`，新增以下仓库资源；确切 DTO 在实施计划中对应到代码文件，不增加通用可执行工具接口：

| 资源 | 行为 |
| --- | --- |
| `sync` / `sync/refresh` / `sync/backfills` | 读取分流状态、请求合并刷新、管理员创建有界历史范围 |
| `issues` / `issues/{number}` | 分页事实和讨论，含覆盖与源版本 |
| `pulls` / `pulls/{number}` | PR 详情及版本化变更、Review、评论 |
| `actions/runs` / `actions/runs/{run_id}/attempts/{attempt}` | run/attempt、jobs/steps 和对应来源 |
| `health` / `health/refresh` | 最近完成摘要、输入清单、请求新摘要 |
| `workspaces` / `workspaces/{id}/tree` / `file` / `search` | 创建或复用版本准备请求、读获准版本内容 |

写入本平台状态的 POST 继续使用现有 CSRF/同源校验；人工请求有幂等键。共享进度先用 React Query 有界轮询，页面不可见时暂停；保留现有私有 Job SSE，不为进度再造事件总线。健康和代码结果只能在仓库授权后读取，数据库 ID 不是访问能力。

新增安全错误码覆盖缺少 App 权限、源版本改变/不可用、源接口上限、同步预算耗尽、文件类型/大小限制、存储不足和 Git 操作失败。用户看到原因、最后有效版本和可重试动作；日志只含资源 ID、任务/同步代次、耗时、计数及安全错误码。HTTP 响应正文、Git stderr、令牌和私有代码不进入通用日志或 AuditLog。

部署新增 Git 运行时和 Workspace 持久卷：Worker 可写、API 只读；迁移与前端无需卷。配置指南写明 App 读取权限、Webhook 订阅、新权限重新批准、磁盘配额、软预算、版本清理、限流和恢复操作。Git 缓存视为可重建数据；数据库恢复后逐个验证引用对象存在，缺失即标为待准备，不伪装成功。

新增迁移必须从现有第一阶段 head 演进，保留旧用户 Job 及 RunEvent。生产启用前停止旧 Worker、迁移、启动新 API/Worker；旧代码不承诺读取新增任务类型。回退采用恢复兼容备份或向前修复，不能用删掉第二阶段数据的 downgrade 冒充无损回退。

## 10. 验收要求

本地验收使用可控 GitHub HTTP fixtures、真实 PostgreSQL 事务、临时服务自有 Git 仓库及 Compose；不需要真实密钥。fixture 不写入用户目录，不修改用户 Git 全局配置。测试服务和数据按项目/运行 ID 隔离，清理只触及测试创建的路径。

| 验收组 | 必须证明的行为 |
| --- | --- |
| 迁移与兼容 | 第一阶段数据库升级后原登录、仓库、访问检查、私有 Job/SSE 仍可用；新增系统任务幂等与终态正常 |
| 增量与事件 | 重复/乱序事件、执行中到达事件、同页重试、暂停重启不丢持久需求；Issue/PR ID 不混用 |
| 回填与缺口 | open/90 天/30 天边界、跨页、时间窗口拆分、API 上限、3 次失败、整轮预算和重启恢复；未完成水位不提前推进 |
| 权限 | 用户撤权、安装暂停、仓库移除/重新登记、缺权限、旧授权快照；管理员无内容特权；缓存事实和代码同样拒绝越权 |
| 租约与竞争 | 旧 Worker 返回、租约恢复、进程超时、重复提交时事实/游标/健康/Workspace 均不能被旧尝试发布 |
| PR/CI 版本 | PR force-push/base 改变、旧 Review、CI rerun、fork PR、CI SHA 不同于 PR head；无错误匹配或自动回退到 main |
| Workspace | 特殊文件名、路径穿越、symlink、gitlink、LFS、大文件/二进制、搜索截断、Git 配置污染、凭据不落 argv/日志、子进程取消回收 |
| 存储恢复 | 写入中断、孤立对象、磁盘不足、读者与清理竞争、数据库有记录而对象缺失；API 只读挂载有效 |
| UI | 有权限用户完整操作上述页面；分页错误可重试；同步中/旧快照/未知/限流有清晰提示；无跨用户或跨仓库缓存残留 |
| 交付检查 | UV 锁文件一致、Ruff/pytest、前端类型/lint/build/Playwright、镜像与 Compose 冒烟；验证新功能并覆盖受影响的第一阶段回归 |

真实接入另列待执行清单：新 App 权限获批、私有仓库首次回填、Webhook 实际投递、PR force-push、Actions rerun、fork 权限、撤权恢复、接近 1 GB 仓库的抓取与配额、正式 TLS 与备份恢复。未执行时明确记录 `NOT EXECUTED`；不以 mock 测试替代外部验收。

## 11. 从规格到实施

本规格在已批准平台设计下固定第二阶段的具体边界。2026-10-13 用户指示“按照实施计划开始实施”；据此补齐 2A、2B 逐项实施计划并连续执行，继续既定 A 执行方式，按依赖实现、测试、审查并形成第二阶段验收记录。实施计划不得把第三至第七阶段能力标成当前已完成。

沿用用户要求的 Superpowers 工作流，并以用户最新开始实施指令为执行授权；不重复要求确认已经决定的团队模式、UV、总体架构或执行方式。后续实际进度以实施计划和阶段验收记录为准。
