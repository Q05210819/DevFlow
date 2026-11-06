# DevFlow 2B 版本化 Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**已裁定交接：** 开始2B前读取 [2A与2B执行裁定](../../operations/data-foundation-decisions.md)。这些既有裁定包括迁移、Windows原生进程适配、真实进程树清理/持久防护、授权来源、读取预算及RO/RW边界。2026-10-19 整分支修复新增 `0008_sync_repair`，故本计划迁移为 `0009_workspace`、`down_revision = "0008_sync_repair"`；旧 R1 引文仅是历史顺序。2A代码门槛已通过；截至2026-10-22，Task 1–5代码及独立审阅通过，进入Task 6联调。Task 5最终typecheck/lint通过，但28个浏览器用例未执行，唯一构建在编译成功后因TypeScript子进程spawn EPERM整体失败。Task 4仍有队列领取、原生Health未知及启动失败的运行时观察待完整验收解释，不能称整套测试全绿。真实fetch与Linux、前端运行及完整验收尚未完成。Git提交仍受审批服务不可用影响，保留可审阅的未提交改动。

**Goal:** 为获准仓库提供固定 commit SHA 的代码读取、搜索和可追溯引用。

**Architecture:** API/Worker共享服务自有裸Git缓存，Worker写、API只读；PostgreSQL授权/任务租约控制版本发布。Git原始对象读取，不检出或执行仓库代码。2A事实和后台任务已存在，复用其有限类型分发与权限。

**Tech Stack:** Python/UV、标准库 subprocess/threading/ctypes、Git、PostgreSQL、FastAPI、Next.js、pytest/Playwright、Compose。

**Spec:** [第二阶段规格](../specs/2026-10-13-devflow-data-foundation-design.md)；前置 [2A计划](2026-10-13-devflow-data-sync.md) 全部完成。执行方式沿用A；用户已指示开始实施。

## Global Constraints

- “对象在磁盘中存在不构成可读依据”；用户和安装授权均需满足；fork完整head每次读复核两侧已登记仓库。
- “按稳定数字仓库 ID 建服务自有裸 Git 对象库，只抓所需的显式 ref”；不 checkout/mirror/submodule/LFS/运行仓库代码/partial clone。
- “每仓库实际写进程持有操作系统排他锁，锁覆盖子进程直至退出”；完整Lease和授权版本校验后发布DB记录。
- Git token仅Worker子进程私有env；argv/URL/config/log不得含secret。隔离Git配置/helpers/hooks/proxy/协议，不执行任意命令。
- “Windows 使用受控 Job Object，Linux 使用受控进程组”；取消线程工作需终止并回收进程树，不仅取消协程。
- 抓取全局1、每库1、10分钟、最多3次；存储软预算每库2GiB/全局20GiB，正式受限文件系统提供硬配额。
- API全局最多4个Git读取、含排队5秒；tree每页200、总扫描100000；blob1MiB、500行、256KiB响应。
- 搜索字面匹配最多2000文本文件或20MiB、100结果；7天未用非当前版本可回收，活跃读取及默认/open PR版本保护。
- 路径精确UTF-8 POSIX，最大4096字节，不Unicode归一化；行号按原始LF；任何截断/未知/不可用均明确返回。
- 用户工作文件与其他worktree不被Workspace测试/清理触及；UV和现有Windows Selector不变。

## Review Focus

1. Git网络helper在父任务取消后仍活着：Task1验证完整树终止和锁/slot释放顺序。
2. 同SHA缓存被用于绕过另一个fork的权限：Task2/3按snapshot provenance复核，不以对象命中放行。
3. GC与API读同时发生：Task4先拒绝新读、等待读者并持有进程写锁，崩溃后可恢复。
4. CRLF/Unicode/极长行导致引用行号错位：Task3按LF原始字节定位并明确响应截断。
5. 浏览器切换版本时旧内容出现在新SHA下：Task5取消/清除旧query及本地选择。

## 文件职责与执行约定

`workspace/git.py`固定Git命令和对象读取，`process.py`通用有界进程生命周期，`windows.py`只封装Job Object；`models.py`版本/读pin，`service.py`解析与发布，`read.py`授权内容读取，`retention.py`回收，`routes.py`API。不创建任意shell工具。每任务RED→实现→GREEN→commit，复用2A fixtures与safe error/audit，不反复运行无关全套。

### Task 1: 受控 Git 进程与裸对象缓存

**Files:** Create `backend/src/devflow/workspace/__init__.py`, `process.py`, `windows.py`, `git.py`; tests `test_workspace_process.py`, `test_git_objects.py`; Modify `config.py`。

**Interfaces:** `GitRunner(settings).run(args:list[str],*,cwd:Path,env:dict,timeout:float,max_output:int,cancel:threading.Event)->ProcessResult` 仅内部调用；`GitCache(root,runner).fetch(repository_id:int,full_name:str,ref:str,token:SecretStr,attempt_key:str)->GitCommit`；`tree(repo_id,sha)->TreeEntries`、`blob(repo_id,oid,max_bytes)->bytes`。`GitCommit(sha,tree_oid,pin_ref)`，OID完整hex并确认对象类型。

- [x] **RED:** 临时Git仓库含symlink/gitlink/CRLF/恶意配置；断言读取原始对象、不执行hook/textconv、不跟随symlink；进程fixture派生阻塞子进程，取消/timeout后所有PID退出再释放slot。secret不在argv/config/log；不同writer同库被OS锁阻止，旧attempt不能改published ref。
- [x] **Run RED:** `uv run --no-sync pytest tests/test_workspace_process.py tests/test_git_objects.py -q`。
- [x] **Implement:** 固定绝对git可执行路径、argv数组shell=False、trusted空配置/模板/hooks、deny协议且fetch只允许HTTPS github.com、禁用replace/lazy fetch/auto maintenance。Windows通过ctypes受控Job Object，不允许子进程先逃逸再加入；Linux新进程组。stdout/stderr均有界，原始stderr不输出。同步Popen在线程执行且deadline含排队。实际writer OS锁直到helper回收；唯一attempt refs、完整对象校验、immutable pin；错误只返回安全码。
- [ ] **GREEN:** 上述测试Windows本地+Linux容器子进程行为，Git版本功能检查；Ruff。
- [ ] **Commit:** `feat: add bounded cross-platform Git object runner`。

### Task 2: 版本解析、后台准备与租约发布

**Files:** Create `workspace/models.py`, `schemas.py`, `service.py`, `handlers.py`, migration `0009_workspace.py` (`down_revision = "0008_sync_repair"`); Modify jobs kind/result、worker、migration env、github/data；Test `tests/integration/test_workspace_prepare.py`。

**Interfaces:** `WorkspaceSelection` union `{kind:'branch'|'tag',name}` / `{kind:'pull',number,side:'base'|'head'}` / `{kind:'ci',run_id,attempt}`；`WorkspaceService.request(actor,repo,selection,key)->WorkspaceView`、`prepare(lease:Lease)->WorkspaceResult`、`get(actor,repo,workspace_id)->WorkspaceView`。记录WorkspaceVersion(id,repo,source_repo_id,commit_sha,tree_oid,source JSON,auth epochs,status,pin_ref,last_used_at,error)；public DTO不含pin_ref/磁盘路径/token。新增system kind `repository.workspace`，独立10分钟时限和全局1领取。

- [x] **RED:** branch移动仍保持原SHA；unknown SHA不接纳；PR两侧与CI attempt对应、fork任一侧撤权拒绝；fetch ref变化返回version_changed、不回退main；旧lease/旧请求不得发布，崩溃pin孤立可恢复。重复key同selection复用，不同selection冲突。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_workspace_prepare.py -q`。
- [x] **Implement:** fresh用户权限准入、安装scope仅后台读取，GitHub ref/tag peel/PR/CI事实解析固定SHA后抓指定ref；CI无法证明/抓取目标SHA时version_unavailable。后台实际scope验证两侧epoch，目标SHA必须与对象一致。DB发布与job完成/事件同事务fence，先immutable pin后DB记录。Repo+SHA内容可复用，但每个snapshot来源授权记录独立，不能共用不同权限的公开ID。
- [x] **GREEN:** 聚焦测试和system queue/migration/用户读取隔离回归；Ruff。
- [ ] **Commit:** `feat: prepare authorized immutable code versions`。

### Task 3: 授权目录、片段、搜索与引用 API

**Files:** Create `workspace/read.py`, `workspace/routes.py`, `tests/integration/test_workspace_reads.py`; Modify `schemas.py`, `app.py`, `pagination.py`（仅确有公共能力需要时）。

**Interfaces:** `WorkspaceReader(factory,policy,cache).tree(actor,repo,id,path='',cursor=None,limit=200)->TreePage`；`file(...,path,start_line=1,line_count=500)->FileView`；`search(...,query,path='')->SearchView`。证据字段repo/workspace/full SHA/path/blob OID/line范围；结果附complete/truncated/scanned/skipped/reason。routes GET/POST workspaces、GET workspace详情和tree/file/search。

- [x] **RED:** 路径`../`、absolute、backslash、NUL、无效UTF8、4097字节拒绝；大小写不同不能命中；CRLF按LF，1MiB/500行/256KiB边界，二进制/LFS/gitlink类型正确。搜索达到2000/20MiB/100上限显式truncated；cap前未找到不宣称全仓无匹配。返回前epoch变更拒绝，GC pin在所有退出路径释放。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_workspace_reads.py -q`。
- [x] **Implement:** 查授权版本→DB读pin→有界原始tree/blob→再次验证epoch→输出，finally释放pin。目录cursor绑定repo/id/sha/path/user，安全编码源链接。只允许字面搜索（query1..256 UTF8字节），标准库匹配，response不执行HTML；完整blob过大即too_large，不偷偷截文件。全局四读取槽跨API进程以DB读pin计数advisory事务锁准入，所有等待含5秒总截止。
- [x] **GREEN:** 聚焦测试、授权/分页/API回归；Ruff。
- [ ] **Commit:** `feat: browse and search code with exact version citations`。

### Task 4: 容量、保留和 Health 版本可用性

**Files:** Create `workspace/retention.py`, `tests/integration/test_workspace_retention.py`; Modify workspace service/models、worker、health service/schemas。

**Interfaces:** `WorkspaceRetention(factory,cache,settings).sweep()->RetentionResult`；`WorkspaceReader` 使用有截止的DBpin；health manifest新增版本availability。容量准入和临时pack监测属于fetch生命周期，不另造存储服务。

- [x] **RED:** 默认branch/open PR/活跃reader不可删除；旧未用7天snapshot先unavailable再回收；崩溃pin过期和恢复；2GiB/20GiB软预算阻止新抓取，残留temporary计入；删除路径越界拒绝；DB指向缺失对象显示unavailable。health更新代次保护不变。
- [x] **Run RED:** `uv run --no-sync pytest tests/integration/test_workspace_retention.py -q`。
- [x] **Implement:** 使用repo OS写锁；按R27使用既有unavailable状态与保留pin表达可恢复清理，拒新pin、保护未完成发布和活跃读者，再移除ref并安全GC，永久保留证据元数据。清理只对服务生成根内路径，验证resolve和内部链接；未引用attempt按物理清理确认、任务撤权和宽限期清理。空间不足返回storage_exhausted，文档区分软预算超调与部署硬配额。
- [ ] **GREEN:** 聚焦测试及prepare/read/health回归，竞争测试使用受控屏障不重复压力循环；Ruff。
- [ ] **Commit:** `feat: bound workspace storage and preserve active evidence`。

Task 4审阅结论：重要问题I1（清理命令绕过非GC guard检查）已修复并通过原审阅者复核，7项原生拒绝用例通过。回归证据仍按分段保留：137通过/1失败/18环境setup错误，后续Health18通过及领取单例1通过；权限修复相关回归32通过/1失败/1跳过，Health诊断单例通过但原未知原因未确认。因此GREEN总门槛保留待验收，未提交Git。轻微问题M1（未知检查原因分类）交整阶段审阅处理。运维边界见[存储与恢复说明](../../operations/workspace-storage.md)。

### Task 5: 固定版本代码界面

**Files:** Create `frontend/src/app/(app)/repositories/[repositoryId]/code/page.tsx`, `src/features/repositories/workspace.tsx`, `tests/workspace.spec.ts`; Modify repository types/queries/nav/health。

- [x] **编写行为测试（未执行RED）：** 浏览器选择branch/tag/PR/CI→准备→tree→file/search；固定SHA和line引用，显示LFS/大文件/截断/版本失效/fork权限。`switching_version_hides_previous_file_before_new_sha` 与late response/撤权/分页错误；检查typed POST，无shell/ref表达式。R29记录Playwright启动被拒、零用例执行，不将环境错误计为RED。
- [ ] **Run RED:** `npm.cmd run test:e2e -- tests/workspace.spec.ts`。
- [x] **Implement:** 复用2A repo scope，query keys包含workspace/sha/path/range/query；变更selection先清理所有旧代码状态，按Task2/3 DTO渲染。默认参数来自API，用户不可提升服务端预算。精确路径使用URLSearchParams，字面代码转义显示，SHA换行和窄屏导航样式已实现；实际360px布局尚未运行验证。独立静态审阅通过。
- [ ] **GREEN:** 聚焦E2E及repo/session/health回归，typecheck/lint/build。
- [ ] **Commit:** `feat: add versioned code browser and search UI`。

### Task 6: Git 运行时与完整第二阶段验收

**Files:** Modify `infra/backend.Dockerfile`, `infra/compose.yaml`, `infra/compose.dev.yaml`, `.env.example`（按实际位置）、操作文档/README；Create `tests/integration/test_workspace_flow.py`, `frontend/tests/workspace-flow.spec.ts`；更新 `data-foundation-acceptance.md`。

- [ ] **RED:** 真实API/Worker/PostgreSQL+服务自有Git fixture，准备PR版本→读取→force-push→旧引用仍固定→撤权阻断；容器检查API RO卷不能写、Worker可写、重启恢复与缺对象可解释。
- [ ] **Implement:** 镜像安装受支持Git/CA；Worker RW/API RO挂同源volume，配置软预算和宿主硬配额指引；Windows本地/Compose两种测试运行步骤。备份恢复后校验对象，不宣称Git缓存不可丢。保留真实GitHub/fork/近1GB抓取/TLS外部NOT EXECUTED项。
- [ ] **Verify:** UV锁文件/完整pytest/Ruff；前端typecheck/lint/build/完整Playwright；镜像、隔离Compose、迁移与进程恢复；记录确切命令及结果。
- [ ] **Review/Commit:** 完整第二阶段whole-branch review，处理有证据的问题并再次验证受影响范围；提交 `test: verify versioned workspace and phase two delivery`。按用户既有本地集成偏好完成可审阅整合；不推送、不部署到外部环境，不称第3—7阶段完成。
