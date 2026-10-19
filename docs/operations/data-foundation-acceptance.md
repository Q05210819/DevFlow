# 2A 数据同步验收与运维

日期：2026-10-19。范围：Issue、PR、Actions 事实同步、授权读取及 health-v1。本文记录新增证据，[一期验收](foundation-acceptance.md) 的历史命令和结果保持原样。2A 代码实现与本地验证已完成，独立任务及整分支审查仍由控制器完成；2B Workspace 未实施、页面明确不可用。

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

- 实际 bootstrap、允许名单、OAuth start/state/callback/cookie、发现/接入、HMAC签名webhook和UUID去重，经真实API进入正常调度器及Worker，在PG持久化两条Issue、不同身份的PR及评论/评审/文件、CI两个attempt/jobs、同步覆盖和独立健康快照。
- 真实429/Retry-After使page2暂停；数据库保留checkpoint、不提前推进水位/已处理序号。正常停止普通Worker任务，新增内容需求后启动新Worker，续接分页并追平序号、保留唯一事实，每执行单元不超过五次HTTP。此项是普通Worker生命周期证据，不能替代进程强杀验收。
- 后续Actions403留下permission_missing，保留既有事实及成功水位，健康覆盖明确不足。completed表示捕获需求已处理，不保证所有流完整。
- 浏览器只拦截外部GitHub同意跳转，读取真实API结果，显示缺失patch、完整SHA、精确CI attempt/history、health事实和Workspace不可用。实际签名移除仓库后，已加载私有Issue的刷新收到404；旧正文及导航随已知拒绝隐藏，后续sync/health/Actions均拒绝。未伪造事实或权限响应。
- 既有 owned helper 正常停止、父stdin EOF/启动失败清理保持有效；schema生成与删除受专用DB校验。固定浏览器端口31847/31848按单worker串行，避免跨spec争用。未新增产品测试后门或服务。

## 同步恢复与证据判读

1. 先看 `/health/ready`，确认数据库与镜像迁移一致；Worker也须持续存活。只读GET不创建任务，接入和签名事件/刷新才登记需求。
2. 默认收集所有当前open Issue/PR及近90天更新的closed/merged、近30天CI；15分钟补漏、5分钟增量重叠、每日核对。每单元最多五次HTTP（含签发/复核）、120秒；HTTP10秒；整轮10000请求/24小时，最多三次失败。单安装串行、单仓库一个单元、默认全局两个单元。
3. 限流查看 `retry_at`/Retry-After；不要手工推进checkpoint、调时钟或循环刷新绕过退避。Worker正常重启使用相同DB和配置；无内存状态才可续接。异常退出由租约到期回收，历史强杀测试与本次普通重启证据分开。
4. `permission_missing` 先核对App权限、安装批准、选定仓库及本地绑定；按 [App流程](github-app-setup.md#已安装-app-增加权限) 批准后重新验证/接入，再发起明确刷新。恢复事件不会授予权限。
5. `budget_exhausted` 停止自动尝试。管理员核对范围/预算后在概览点击“恢复同步”，或带CSRF、同Origin、唯一Idempotency-Key调用 `POST /api/v1/repositories/{id}/sync/refresh`，正文 `{"resume":true}`。普通成员仅可 `resume:false` 刷新。
6. 管理员回填 `POST .../sync/backfills`，正文含UTC aware `start/end`、`streams:[issues|pulls|actions]`，每次最多365天、不得未来结束。不能把不连续覆盖合并为完整区间；检查各流coverage和remaining，不能用最老事实推断完整历史。同逻辑重试保留Idempotency-Key，修改输入使用新key。
7. 健康 `POST .../health/refresh` 正文 `{}`，同样需CSRF/Origin/Idempotency-Key。快照保留生成时间、规则/阈值、实际覆盖及来源manifest；未知不是零，记录创建/更新计数不是完整事件历史，最新CI也不是PR合并许可。失败保留上次成功快照；独立health generation控制发布。

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

迁移head为 `0007_health_requests`，后续2B必须从 `0008_workspace` 开始。含二期证据的降级有数据保护拒绝；回退应停止本项目写入者、备份DB及密钥，在独立库恢复并匹配镜像/revision验证，参见 [部署](deployment.md#2a-同步与迁移操作)。新空库迁移/服务重启不能证明含数据生产降级或备份恢复；未追加这些执行声明。结束使用本项目 `stop`，保留卷；不执行全局清理或down-v。

## 未执行项目与阶段边界

真实GitHub App创建/权限批准、真实OAuth/私有安装、外部Webhook投递、公网TLS/部署、fork/SAML行为、接近1GB真实仓库性能、宿主机硬配额验收均为 **NOT EXECUTED**（用户尚无App）。官方资料研究、合成transport、镜像构建和本地恢复不等于这些外部验收。

2A交付只读事实与健康；2B仍需安全Git进程/对象、不可变SHA版本、tree/blob/search、保留/空间预算、代码浏览及其容器/恢复验收。后续模型配置属于第三阶段，不是本次交付；无模型密钥读取或模型API调用。
