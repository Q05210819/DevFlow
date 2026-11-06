# DevFlow 第三阶段：检索问答规格

日期：2026-10-19。状态：**用户已于2026-10-19确认本书面规格，授权准备3A–3D实施计划；尚未实施，也不授权实际云消费。第二阶段继续执行。** 本阶段基于已批准的[平台设计](2026-09-30-devflow-platform-design.md)；当前第二阶段仍按既有计划执行，2A/2B 的完成状态不由本规格改变。

## 1. 目标、用户决定与工程假设

用户要建设完整研发平台，第三阶段交付能够追溯来源的仓库文档/代码问答。用户应能看到检索覆盖、证据版本和缺口，点击引用得到当时采用的资料，撤权后不能借历史结果继续读取仓库内容。

已确定：单团队、多用户、私有仓库；首批不超过 **10 个仓库**、通常每个小于 1 GB；UV、模块化单体 API、独立 Worker、PostgreSQL 持久队列、Next.js、Milvus、单机 Linux Compose。团队人数、目标并发及正式主机硬件尚未知，不从仓库数量推断这些数值。

用户选定聊天模型 `qwen3.8-max` 和 Base URL `https://dashscope.aliyuncs.com/compatible-mode/v1`；本地 BAAI Embedding/Rerank。选用已有 bge-m3（密集 1024 维）及 bge-reranker-v2-m3，理由是混合中英资料适配，性能与代码检索质量仍需验证。[BAAI Embedding 模型卡](https://huggingface.co/BAAI/bge-m3)、[BAAI Reranker 模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3)

仓库资料发送范围固定为：**本轮明确选择且用户当前有权访问的仓库中，检索命中的文档和代码片段**。不发送 CI 原始日志。第一版云上下文只有系统提示、当前问题、本轮命中证据及本轮工具协议消息；不自动附加旧会话、日志、附件、事实库正文或未命中文件。历史消息可在平台本地查看，每轮问题需能独立理解；界面明确这一行为。历史摘要和长期记忆留在后续阶段。

下文具体限制是可审阅的工程默认，非用户容量或 SLA 承诺。离线合成固定集先验证行为；真实私有仓库、供应商账户及用户主机的运行验收另列，不阻塞无密钥实现和本地 fixture 验证。

## 2. 范围、方案与顺序交付

选择 PostgreSQL 保存规范正文、来源和有效索引指针，Milvus 同时承担 BM25 与密集向量召回；RRF 融合后在本地重排。相比 PostgreSQL 关键词+Milvus 向量两套检索，它复用已要求的 Milvus 原生全文能力，少维护一套词法索引；代价是 Milvus 故障时两路均不可用，中文/代码分词和额外资源必须实测。不上 Elasticsearch，也不以轻量替代库冒充 Milvus。[Milvus 全文检索](https://milvus.io/docs/full-text-search.md)

| 单元 | 交付契约 | 退出条件 |
| --- | --- | --- |
| 3A 文档和来源 | 仓库绑定的 Markdown/UTF-8 文本上传；固定 Workspace SHA 文档/代码导入；切块、来源预览、覆盖与导入进度 | 幂等、版本/行号、类型/容量限制、删除/替换及权限回归正确 |
| 3B 本地模型与索引 | 模型文件指纹、真实 Milvus BM25+dense、分批构建、active 发布及清理 | 小样本真实索引查询/重启持久化、失败恢复、过期执行者隔离与资源记录 |
| 3C 检索引用 | 限定仓库/SHA查询、RRF、本地重排、证据列表和引用预览 | 固定集回溯来源；无结果、过期/缺失索引及跨仓库隔离符合契约 |
| 3D 私有问答 | Conversation/Message、受控 ChatAgent、LangGraph恢复、持久预算、私有 Job/SSE和界面 | 引用约束、云发送范围、会话隔离、取消/恢复/撤权；真实接入状态如实列出 |

代码来源依赖第二阶段 2B 的已发布 Workspace，第三阶段不另做 Git 抓取器。Issue/PR/CI 最新状态继续通过事实服务读取；不将向量中的历史文本作为当前状态。专用 Agent、CI 日志下载、Planner/综合工作流、写操作、ActionDraft、Memory、Report、MCP/Skill 不在此阶段实现。

## 3. 资料导入与版本

每仓库维护一个知识语料配置：显式选定的 WorkspaceVersion/完整 SHA、文档路径范围、代码路径范围以及已发布上传文档修订。管理员需同时有该仓库访问权，才可修改共享语料配置、导入、替换或删除资料；其他获准用户可查询。管理员角色本身不授予内容权限。

首次默认选择默认分支已解析的 SHA；由操作者在导入预览中确认路径清单。默认包含根 README 与 `docs/` 的 `.md/.txt/.rst`，代码只在选中的源码目录内按文本扩展名导入。默认排除依赖、构建产物、二进制、隐藏配置、`.env*`、凭据/私钥类路径。扩展名/排除规则版本化，规则不被描述为能识别所有嵌入式秘密。扫描/路径校验继承 Workspace 规则，不执行仓库代码、不获取 LFS、不跟随 symlink 或 gitlink，不接收 shell/宿主路径。

上传只接受单个 `.md` 或 `.txt` UTF-8 文件，必须绑定一个已授权仓库，界面标明该文档将供该仓库获准成员使用。以稳定 document ID+修订 hash 表示来源，不能伪装成 GitHub 文件。替换创建新修订，旧引用保留原修订身份；原始 URL 只作为显示属性，不用于抓取。没有 URL 爬虫、压缩包、PDF/OCR 或自由文件系统导入。

切块使用标题/空行/代码行边界与确定性 token 窗口；不引入 AST/多语言解析器。保留原始 UTF-8 正文、LF 行号及必要字节偏移；CRLF 不产生额外行。超过窗口的单行继续按 token 拆分并保留字节位置；引用展示覆盖的原始行，不能把规范化检索文本的行数当原文件行数。切块配置、tokenizer 指纹改变必须新建索引版本。

| 项目 | 默认上限与处理 |
| --- | --- |
| 单文件 | 1 MiB 原文；正文必须有效 UTF-8；超限/不支持类型按项记录，不偷偷读取前半段称完整 |
| 每次扫描 | 最多 2,000 个允许文本文件、32 MiB 总原文；稳定路径顺序，到限标明未扫描范围 |
| 单 chunk | 最多 512 个 Embedding tokenizer token、8 KiB UTF-8；重叠最多 64 token；标题/路径前缀计入上限 |
| 活跃语料 | 每仓库最多 10,000 chunk；全局最多 40,000；正文逻辑总量最多 128 MiB（含保留修订） |
| 元数据 | 单 Document 元数据 8 KiB、Chunk 元数据 2 KiB；源清单按成员行保存，禁止无限 JSON 列表 |
| 一次构建 | 最多 24 小时；分批 Job，每批最多 16 chunk、120 秒；失败最多 3 次，进度/总截止时间不随重启重置 |

导入结果逐项保存 included/skipped/unsupported/over_limit 和汇总覆盖。允许发布有明确范围的部分语料，但状态为 `ready_partial`，不称完整仓库索引；安全校验失败、源版本无法验证或写入不完整则不发布。删除/上传替换会先使旧修订不再参与新检索；索引清理延迟不延迟这一数据库失效。

每仓库只开放一个当前检索索引版本及其固定 SHA。语料配置另有单调递增的目标代次：路径调整、上传替换/删除即使不改变 SHA，也会产生新代次。查询目标代次尚未发布时返回 `index_not_ready`，不能因 SHA 相同而静默使用旧上传修订。导入不同的新 SHA 时，操作者仍可明确查询当前 active 的旧 SHA，并看到旧版本状态；默认新目标查询必须等待新版本。新版本发布后旧版本退出检索，仅供保留期内的历史引用预览。此限制降低多 SHA 同时索引的成本；Workspace 仍可读取其他获准 SHA，不能悄悄换 main 来满足知识查询。

## 4. 持久化与索引发布

| 数据 | 最小契约 |
| --- | --- |
| DocumentRevision / Chunk | 仓库、源类型/身份/修订、正文及hash、采集时间、可用状态、分块指纹、原始定位；Workspace来源另带snapshot/SHA/path及依赖仓库集合 |
| IndexVersion / 成员 | 仓库、源清单版本、模型/tokenizer/analyzer/分块指纹、collection/schema、期望/完成计数、状态、构建Job/代次；成员绑定不可变Chunk，DB保存active指针 |
| Conversation / Message | 创建者、固定显式仓库集合、创建时间、消息顺序和状态；问题、回答、结果manifest只在私有授权接口读取 |
| AgentRun / ToolCall | Job/run关联、稳定graph thread、授权/索引版本清单、accepted checkpoint、累计步骤/调用/费用、deadline、安全终态原因及逻辑调用幂等标识 |
| BudgetLedger | run及平台UTC月桶的预占/结算/未知费用、价格配置版本；正金额和币种明确，重试/恢复共用同一账本 |

复用 Job、RunEvent、AuditLog，不增加第二套队列。IndexVersion采用 building→ready/ready_partial→retired，失败为 failed；外部写入到 Milvus不能当作发布。

发布流程固定：DB保存源清单/请求代次 → 分批编码和幂等写入 Milvus → 校验所有清单成员、完成索引加载、用 Strong查询验证可见 → 短事务按既有授权/Job顺序锁定并核对授权版本、源请求代次、lease owner/generation/expiry → 切换active指针。旧任务即使成功写入 Milvus也不能推进active。失败留下不可查询的孤立版本，由有界清理任务移除；不宣称跨数据库原子事务。[Milvus一致性](https://milvus.io/docs/consistency.md)

Milvus每条记录含 index_version、repository、chunk ID、密集向量和 BM25文本；主键由版本+chunk稳定派生。维度固定1024，使用归一化dense向量与COSINE；BM25使用官方中文analyzer作为第一版固定基线，路径/标题加入词法文本但引用正文保持原样。分词测试必须覆盖中英混合、snake_case、CamelCase、路径与错误码；未达固定集要求时调整analyzer版本并重建，不能宣称中文默认分词已经达标。[Analyzer](https://milvus.io/docs/analyzer-overview.md)

每仓库最多同时保留1个active、1个building、1个retired版本的向量数据；全局向量成员（含这些版本）最多120,000。retired向量最多保留7天，更早到达版本数量上限时可清理；历史引用不依赖这些向量继续存在。IndexVersion来源标识、引用manifest和所需正文分别按会话保留期保存，不能把仅保留标识的历史版本算作仍可检索的向量版本。未被保留消息引用且无读者pin的旧正文可回收；消息到期后引用只保留标识并显示 `source_expired`，显式源删除/撤权立即优先生效。当前预览只用有过期时间的短pin，不长期pin整个Git对象库。达到容量先清理允许回收的旧数据，再拒绝新构建，不删除active数据或未到期引用正文凑空间。向量字节限制不等同Milvus实际磁盘/内存；部署另设卷容量和告警。

## 5. 权限、查询和引用契约

复用 RepositoryPolicy：用户有效、仓库启用、安装允许且可用、用户本人有权；短缓存最长60秒，无法验证时拒绝。提交索引/查询/问答执行前要求新鲜验证；内容读取前后检查本地授权版本。观察到撤权立即失效，不能承诺未通知的GitHub变更瞬时可见。

一次查询固定当前用户、明确仓库集合、实际检索的索引所属仓库/SHA/active版本和依赖权限。明确集合可以包含只作为fork真实来源的仓库；该来源无需再建一份索引，仍由原base索引的成员保存实际来源SHA。界面区分检索的索引与允许参与的来源仓库，不自动补充任何仓库。跨仓库最多3个且必须显式选择；fork PR完整head代码继续要求base/head双侧权限，不能只按向量的base仓库字段授权。选择范围与权限检查是两个独立条件：Chunk/manifest保存真实source_repository_id及所有依赖仓库，真实来源和base均须在本轮明确选择集合内；具备fork权限不会自动把fork加入集合。界面展示base与head身份，用户只选A而资料实际来自B时不检索/发送该内容，明确提示需显式加入B。最多3仓库按这个去重后的实际选择集合计数，依赖授权不能扩大它；该规则同样用于独立检索和图内检索。单用户撤权不删除团队资料，安装/仓库撤权则停止该源发布。

本地流程：

1. 校验范围并冻结DB版本清单；目标SHA没有对应有效索引时返回状态，不自动扩展范围。
2. 本地编码问题；BM25与dense采用相同的服务端repository/index_version过滤，各取30条，只先取得ID/排名/分数。
3. join DB复核chunk成员、修订状态、索引有效性、依赖权限，之后读取规范正文。以chunk ID去重，RRF `k=60`，取前20本地重排。[RRF](https://milvus.io/docs/rrf-ranker.md)
4. 重排batch固定1，query+passage（含特殊token）最多1024 token；无法装入的内容按导入边界进一步切块或拒绝，不静默截断。最终最多6条证据、每文件最多2条；去掉重复片段。
5. 云发送及最终结果发布前再验证授权/源/active版本。版本变化最多整体重取一次，仍变化则 `version_changed`；不把不同版本的候选拼成一个来源。

检索请求问题上限2 KiB UTF-8且最多256个Embedding tokenizer token；超限明确拒绝。一次检索20秒、Milvus单次5秒；网络/模型故障返回 `retrieval_unavailable`，不能当作无答案。无有效候选返回 `no_evidence`；有候选但不足支持问题时回答证据不足，分数不是事实概率。过滤剔除后的补取最多1次，每路至多60，仍不足就如实返回。

证据manifest由服务端生成，每条含短citation ID、repository、document revision/chunk/hash、index version、源URL和精确定位；整个manifest最多64 KiB。模型只允许引用当前manifest里的ID。未知ID/格式错误不发布成有效引用，返回 `answer_validation_failed`，不自动追加云请求修补。URL由服务端来源构造，模型生成链接不触发抓取。

代码预览固定SHA/path/LF行号，上传文档固定修订hash/行号；单次预览最多500行/256 KiB，返回实际范围与是否仍有正文。单条原始行超过响应预算时，按保存的字节位置返回UTF-8边界安全的区间，附原始行号、字节范围、partial标记与有界续读游标，不为展示一条引用扩展整条长行。导入读取完整允许文件应复用Workspace授权/pin下的有界原始blob分段读取，不能把UI预览的截断内容误当完整文件。历史引用与当前active检索区别明确：历史引用可以读取仍被保留且来源权限有效的旧修订，并标为旧版；删除/回收/撤权分别显示不可用原因。引用存在不能证明推理正确，质量验收另检查结论是否被片段支持。

## 6. 本地推理、Worker与资源

采用本地 Sentence Transformers 的 SentenceTransformer/CrossEncoder路径；锁定PyTorch/Transformers等必要依赖及模型文件hash，设置仅从配置的本地目录加载，不信任远程模型代码、不默认下载权重。只用bge-m3密集输出，不引入FlagEmbedding的稀疏/多向量功能。[BAAI Embedding](https://huggingface.co/BAAI/bge-m3)、[BAAI Reranker](https://huggingface.co/BAAI/bge-reranker-v2-m3)

一个明确配置为model-capable的既有Worker进程拥有本地模型；API和其他Worker不加载模型。该进程启动时必须获得服务自有目录里的OS排他owner锁，在释放锁之前保持唯一模型拥有者；同主机多个容器必须共享锁所在挂载，不允许各用私有锁文件。拿不到锁则不加载模型、不claim模型任务，并显示未就绪。锁只约束DevFlow进程，不承诺阻止其他软件使用GPU。

模型任务继续使用同一PostgreSQL队列，通过任务类型过滤由模型Worker领取；该Worker串行处理后台编码、独立检索和完整ChatAgent Job。图内检索直接调用共享本地检索服务，复用父Job的完整lease、权限范围及推理槽，禁止创建子模型Job并等待它完成。完整聊天Job（包括云HTTP等待）占用这一个串行处理位置，整个run仍受180秒执行窗口约束；接受这段时间其他模型任务排队的吞吐代价。普通Worker不领取这些任务，不增RPC或第二队列。交互检索/问答优先，后台编码每批最多16chunk后让出领取机会；连续10个交互任务后允许1个后台批次，避免饥饿。没有新的模型daemon/RPC/队列。

Embedding与Rerank的batch均固定1，单次推理最多一个；首次以CUDA FP16、两个模型小输入常驻试验为配置候选，启动需完成受限自检，OOM则不可就绪，不自动切到云端或悄悄换模型。CPU或分时加载作为显式运维配置，只在测量后启用，不提前承诺延迟。

推理在线程中执行时，取消协程不能释放物理推理槽：超时/失租只停止接收结果并标记任务；必须等待原推理线程结束/同步CUDA操作后才处理下一次推理。若线程无法结束，Worker停止claim并报告unhealthy，由进程管理器终止整个进程；新进程须等owner锁真正可取得再加载。OS锁持有到进程退出或所有推理已结束，绝不能仅因Job租约到期就转交。驱动释放GPU资源的延迟仍可能导致新进程OOM，应明确失败而非无限重启。

**与当前实现衔接的明确工作项：**本规格没有检查正在变动的第二阶段Worker文件，不能声称已有任务类型claim过滤、OS owner锁、推理线程完成等待或停止claim语义。这四项是3B必须新增并验证的接入点；现有async任务超时机制不能直接充当推理取消。锁持有期间无模型/Git/HTTP操作进入数据库事务。

本机已有证据约15.80 GiB主机内存和6141 MiB GPU；两模型权重大小不等于加载/推理峰值。Milvus官方Standalone内存要求最低8G、推荐16G，整个平台还包括OS/WSL、数据库、API/Worker和前端。真实Milvus单容器用于小样本验证；正式Compose部署及备份恢复另验收，不把分时试验宣称全栈并发通过。本地只读资源采样不等于运行验收；[Milvus先决条件](https://milvus.io/docs/prerequisite-docker.md)

## 7. 私有会话与ChatAgent

Conversation只对创建者可见；创建时固定显式仓库集合，更换集合新建会话。每轮另固定本轮实际使用的知识索引SHA及其来源SHA；不能用旧消息隐式改写范围。同一会话最多1个活动run，全局聊天并发1，用户可取消Job；断开页面不取消。任何参与仓库失权时，原跨仓库回答不再开放，历史消息、引用和Job/SSE都执行相同检查。

ChatAgent为扁平显式LangGraph：授权/预算→初次检索→ContextAssembler→模型→可选一次再检索→最终模型回答→校验/发布。只有一个工具 `search_repository_evidence(query)`，输入不包含可变仓库/权限表达式；服务端限制在本轮可信范围，并最多提供一次补充检索。初次检索无证据时直接返回本地缺口结果，不花云费用。需要任意工具、外部写入或多Agent规划的问题返回阶段能力边界。

使用LangChain模型消息与标准tool calling；最多2个模型请求、总计2次检索（含初次）、8次图节点执行，整个run执行窗口180秒（排队不计入执行，排队最长10分钟）。初次云调用若请求工具，第二次只允许最终回答；重复工具请求拒绝并给出停止原因。图节点限制、逻辑工具调用数、模型请求数和deadline均在AgentRun累计，不因重试恢复清零。

Chat Completions接入固定已选Base URL/模型，不自动路由Responses API。输出用受校验结构：结论文本、使用的citation IDs、证据不足、下一步建议；无需生成“必定成功”的答案。完整序列化JSON请求体（不含HTTP头）硬上限24 KiB；8,192输入token只作为装配估算目标，不冒充已证明的供应商token硬上限。首版候选明确关闭thinking、发送 `max_completion_tokens=1024`（含推理与回答），并计入官方允许的最多10token偏差；输出JSON另限16 KiB。不用BGE tokenizer或字节数加猜测开销冒充聊天模板计量。实际适配器参数与usage仍须验证，未验证不开放真实调用。[官方模型限制](https://help.aliyun.com/zh/model-studio/qwen3-8-max)、[Chat 参数与 usage](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)。SDK自动重试关闭；明确未发送请求的瞬时失败最多重试1次，仍计入总请求上限；已发送但结果未知不自动重发。[兼容层限制](https://docs.langchain.com/oss/python/integrations/chat/openai)

上下文装配按确定顺序保留完整证据块，不截断块后仍沿用完整引用。首轮按检索排名依次尝试加入，完整请求JSON超过24 KiB则跳过该块，最终仍最多6块/每文件最多2块；必须材料自身超限或没有一块能装入时返回context_limit，不发送云请求。发生第二次检索时，按新结果排名优先、再补首轮未重复块重建上下文，保留必要的assistant tool-call及匹配tool-result协议消息，重新应用相同上限。citation ID按证据身份稳定映射；最终允许引用manifest只包含最后一次模型请求实际收到的证据，先前被剔除的ID不能引用。

用户问题/工具资料都是不可信内容，不能改变权限、工具白名单、预算或引用范围。输入框明确告知当前主动输入的问题会发送至阿里云，旧消息和附件不会自动附加；平台不主动获取或附加 CI 原始日志。输入和输出Markdown安全渲染。首次只推送安全进度事件，不直接SSE转发未校验的模型token；回答发布后由私有REST获取，已有事件游标回放/去重行为保持。

## 8. 恢复：官方saver与Job使用同一fence

每AgentRun稳定thread UUID、根namespace为空，不采用attempt namespace。使用官方PostgreSQL saver；当前研究核对LangGraph1.2.16、checkpoint-postgres3.2.0、checkpoint4.3.0，最终由UV锁定版本复核，不把研究中的main分支当部署版本。根图不依赖自定义attempt namespace隔离；指定checkpoint读取仍包含后续合法pending writes，所以写入事务fencing同时覆盖aput与aput_writes。此结论基于源码核查，最终锁定版本仍需真实数据库测试。

适配层绑定可信Job owner/generation，包装 `aput` 与 `aput_writes`：独占同一psycopg raw connection，独立mutex覆盖完整短事务；先开显式事务，按既有授权/Job锁顺序检查lease，再调用官方保存，`aput`在同事务推进accepted config，复核期限后提交。其他同连接读取也通过该mutex；不得在SQLAlchemy连接校验后让saver池另借连接。官方内部pipeline的Sync不会提交显式BEGIN，缺少pipeline支持时内层事务仅为savepoint。所有图/model/HTTP操作在事务外。[官方saver](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py)、[PostgreSQL协议](https://www.postgresql.org/docs/18/protocol-flow.html#PROTOCOL-FLOW-EXT-QUERY)

正常恢复接受指定checkpoint及此前有效lease已提交的pending writes；不是冻结在accepted指针更新时间的字节快照。恢复前检查accepted三元组存在、用户/源权限、图版本、预算；缺失明确报错。图回放可重执行节点，必须通过独立ToolCall/费用账本处理：已成功持久记录的相同逻辑模型调用可在权限/证据复核后复用；已发出而结果未知记录outcome_unknown、保留预占，不自动再发请求。失租旧进程已发出的云请求可能完成和计费，但不能写checkpoint或发布结果。无外部调用恰好一次保证。[官方replay](https://docs.langchain.com/oss/python/langgraph/use-time-travel)

扁平图不采用子图、DeltaChannel、动态Send或人工interrupt。图状态只保存问题/Message/ToolCall/证据清单的业务ID、流程标记和少量计数，不嵌入规范正文、整份manifest或完整LangChain消息历史；提示词在模型节点内重新鉴权后由业务表有界组装，已完成模型结果持久化到业务记录后图只保存结果引用。每checkpoint序列化最多64 KiB、每run最多32个checkpoint；超限终止为 `state_limit`。进入终态的run不再恢复；仅可恢复的活动run及其accepted点受到删除保护。正常worker发布终态时，原Job事务须持仍有效的完整lease fence，将run标记不可恢复、将恢复指针标记retired并清空accepted config。现有控制器取消/过期回收路径则在完整范围的既定有序锁下，校验预期Job状态和owner/generation，以CAS撤销旧执行权，并在同一终态事务完成上述retire/清空；它不要求过期worker恢复有效lease，也不要求已撤销的来源权限重新有效，但仅能拒绝/收尾，不得读取来源、发布回答、重置预算或再次调用云模型。通过现有Queue终态路径的窄chat hook衔接，不新增收尾队列或terminal-only reclaim状态；后续受控清理任务在自己的有效lease下复核目标仍是终态/retired，再通过官方saver线程删除接口幂等删除该thread的checkpoints、pending writes和关联blobs；中断后重复清理，不恢复retired线程。终态图数据最长保留24小时，总逻辑容量128 MiB同时计入上述关联写入和blobs，达到上限先清理最早终态线程，活动数据不能删除，无法腾出则拒绝新run。独立Message/manifest及未知费用预占不随thread删除。最终Message与证据manifest独立保留，checkpoint不是会话历史数据库。

## 9. 费用启用与存储边界

真实云调用默认关闭，只有操作者在本地/部署秘密配置中注入已轮换密钥，并配置**正的每run预算、正的UTC月度平台预算、币种、有效定价版本及预占上界策略**才可启用。没有默认货币金额，本规格不授权任何实际消费；不要求在聊天中提供密钥。价格按配置版本计费，失效/缺失/无法覆盖本次输入输出上界则拒绝调用。

每次模型请求先在短事务预占输入上界+输出上界的最坏费用，同时检查run和月桶剩余额度；然后在事务外发请求。当前候选不要求先实现未经证明的精确tokenizer，而按官方完整上下文上限预占1,000,000输入token及1,034输出token的配置费用，每个实际请求单独准入，可信usage到达后释放确认未使用的额度。这会要求单次预算容纳远高于小问题实际费用的预占；预算不足明确拒绝，不能悄悄提高金额。官方资料中的价格只用于研究，不成为默认消费授权或硬编码价格。此上界依赖供应商遵守已公布限额及配置价格覆盖实际模式；保留价格版本/有效期、参数版本及超界停止对账规则，不承诺供应商账单绝对上限。定价/usage适配须覆盖实际模式的全部可计费token，不能把缺失的推理token等项当成零。得到可信usage后结算，缺失usage或结果未知保守保留预占。并发预占、月底跨桶、重启和取消不能重复释放额度或让账本变负。用Decimal/最小货币单位，不用二进制浮点金额。费用上限是平台的调用准入控制，供应商额外计费和未知账单需要运维对账，不能承诺等同最终账单。

Conversation最多100个/用户、200条Message/会话；单问题按第5节限制，单回答最多16 KiB、manifest64 KiB。默认历史最长90天；达到会话数量上限要求归档/删除，归档不绕过存储上限。消息正文+manifest全局逻辑预算256 MiB，达到时清理已超过保留期的历史；仍不足拒绝新消息，不能静默删除未过期历史。资料、消息和checkpoint的逻辑上限不等于PostgreSQL物理磁盘上限，需额外预算索引/WAL/备份。

中间检索manifest与经校验的模型结果归属AgentRun的ToolCall记录，每条最多128 KiB、每run最多4条（至多两次检索和两次模型调用），终态后最多24小时清理；最终Message只保留需要展示的回答与最终manifest。会话删除及时删除正文及中间结果，但不能释放未知云预占或擦除仍需对账的请求身份。费用/幂等记录另存请求hash、参数/价格版本、状态及金额，不保存提示正文，每条最多4 KiB、全局逻辑预算32 MiB；已结算且对应run终态的记录保留90天后可清理，月份汇总保持，未知记录待人工对账、不能按年龄释放。容量不能满足新记录时拒绝新云调用，不增加隐式重发。

列表每页30、最多100；私有查询结果最多保留1小时，每条最多128 KiB、每用户最多20条，超额回收最旧已完成结果。RunEvent单条最多2 KiB，只含阶段/计数/安全错误码。通用日志、审计不记录原始提示词、代码、密钥、模型原始响应或向量；外部tracing默认关闭。

## 10. API与界面

继续同源 `/api/v1` 和现有CSRF/幂等规则，导入、索引、检索和问答等耗时POST提交有界Job；仅创建固定来源集合的会话元数据可在完成用户/来源授权、幂等及容量检查后以短事务同步返回ConversationView，不执行模型、Git或内容处理。共享导入进度通过仓库资源读取，用户检索/问答Job保持创建者私有。

| 资源 | 行为 |
| --- | --- |
| `/repositories/{id}/knowledge` | 当前目标SHA、active版本、覆盖、计数、构建状态、失效原因；不返回内部路径 |
| `/repositories/{id}/knowledge/imports` | 管理员+仓库授权提交Workspace路径范围或单文档上传；POST返回任务/导入ID |
| `/repositories/{id}/knowledge/documents/{id}` | 修订列表/替换/删除及授权预览；正文仍受定位和字节上限 |
| `/knowledge/queries`、`/{id}` | 提交明确来源仓库集合、实际索引仓库/SHA选择和问题；返回私有Job，完成后获取至多6条证据与覆盖/状态 |
| `/conversations`、`/{id}/messages` | 私有会话列表、创建及本地历史；POST消息创建AgentRun/Job；固定仓库集合不可悄改 |
| `/conversations/{id}/messages/{id}/citations/{citation_id}` | 重新鉴权的版本化来源预览，不接受模型任意URL |

知识页面显示索引SHA/更新时间、部分覆盖/排除项/未就绪状态；查询页面独立展示命中，不要求先启用云模型。会话页面显示当前仓库范围、每轮版本、进度、引用与缺口，注明历史不会自动回传。云未配置显示 `model_not_configured`，仍可用本地检索。权限撤销/退出/切换清除相关React Query缓存，私有数据不进入公共HTTP缓存。

主要安全错误包含 `index_not_ready`、`version_changed`、`source_expired`、`source_unavailable`、`retrieval_unavailable`、`model_not_configured`、`budget_exhausted`、`model_call_outcome_unknown`、`inference_busy`、`storage_exhausted`、`answer_validation_failed`、`context_limit`、`state_limit`。没有证据是正常业务结果，不混同基础设施故障。

## 11. 验收与未执行边界

合成固定集先包含至少30个问题/已知来源：中文、英文、混合、代码标识符、路径、无答案、旧SHA及两个隔离仓库。正确引用身份/定位、未知引用拒绝、越权不泄露、云发送manifest符合允许范围要求全部通过。每个可回答问题的gold为预先标注的不可变文档/SHA及原始证据区间；命中一个相关区间计一次，重叠chunk不重复计分。Recall@6按各问题命中gold数/gold总数后宏平均，至少一个gold的可回答题计入分母；无答案题另统计拒答与错误命中，不混入召回率。初始目标为Recall@6≥0.85，nDCG@6按同一gold相关性并去重后记录分组结果；这些是小型固定集门槛，不代表真实仓库质量。比较BM25、dense、RRF、RRF+rerank，重排退化必须定位并调整配置后重测，不能只报告平均提升。

| 验收组 | 必须证明 |
| --- | --- |
| 导入/版本 | 重复批次、替换/删除、路径与行号、超长行、二进制/超限、部分覆盖；新SHA未就绪不回退 |
| 索引一致性 | Milvus部分写入/不可见、DB发布失败、旧任务晚完成、active切换、撤权和重启；DB恢复后与Milvus版本不匹配明确失效 |
| 授权/私有性 | 跨用户/仓库、fork双侧权限、缓存/引用/历史/Job/SSE回放同样拒绝越权；后台安装权限不是用户权限；有权但未显式选入的fork源也不能检索/发送 |
| Worker资源 | 两模型Worker争抢同一owner锁仅一个加载；独立检索/完整chat仅模型Worker领取且图内直接检索不等待子Job；真实推理未完成时失租不释放槽；取消/挂起进程/重启路径在Windows与Linux分别验证 |
| checkpoint | 同事务saver/accepted回滚；失租后的aput及aput_writes拒绝；合法pending writes与显式ID replay分别验证；无graph/model长事务；终态expiry清空accepted并删全thread，活动run保护 |
| 预算/恢复 | 并发预占、跨月、缺usage、超时/未知调用、进程中断；run费用与步骤不因checkpoint回放归零，不自动重发未知请求 |
| 问答/UI | 固定来源回答、无证据、本地历史不回传、工具范围/次数、无效引用、取消/断线/恢复、配置缺失、数据缓存清除 |
| 工程 | UV锁一致、迁移升级保留第一/二阶段数据；相应Ruff/pytest、前端type/lint/build与Playwright、镜像/Compose验证 |

真实Milvus及模型试验依次验证小样本、重启持久化、冷/热延迟、峰值RAM/VRAM/OOM，再做有预算真实云协议/usage/tool-call兼容验收；全栈同时运行、正式Linux并发和备份恢复另行执行。资源不足可分窗口试验，但验收报告不能合并声称全栈通过；官方Milvus版本/SDK/配置、模型指纹及实际依赖在实施时锁定。

**本规格形成时上述第三阶段测试全部 NOT EXECUTED。** 未验证事项包括真实用户仓库召回质量、云账户权限/余额/计费、已轮换秘密配置、实际token参数/usage兼容与预算上界契约、16GiB/6GiB全栈可行性、正式主机/并发目标、saver事务运行结果和模型取消实效。真实消费只在明确费用配置后发生；当前不需要额外供应商/模型选择或向用户索取秘密。

## 12. 交付边界

本文件是已确认的第三阶段书面规格；按3A→3B→3C→3D形成独立分段计划，避免把导入、索引、检索和问答写成一个巨型实施批次。第二阶段完成后必须核对Workspace/Worker最终接口再实施其消费者，不重新实现Git或任务队列。本次用户确认已覆盖新增产品行为，实施计划仍按既有A执行方式交接；第一至第七阶段总体目标保持不变。

## 13. 供应商与研究依据

- 本地选择：`D:/python/models/BAAI/bge-m3` 和 `D:/python/models/BAAI/bge-reranker-v2-m3`。M3密集输出1024维、两者支持多语言；首版主动限制片段与批量，不以模型理论最大长度作为运行默认。已有文件可用不代表模型加载或真实评测已通过。
- 云端选择：`qwen3.8-max` 与 `https://dashscope.aliyuncs.com/compatible-mode/v1`。官方北京兼容入口资料支持这一配置，账户权限、余额和实际参数接受性尚未验证。[Base URL](https://help.aliyun.com/en/model-studio/base-url)、[模型资料](https://help.aliyun.com/zh/model-studio/qwen3-8-max)。
- 预算上界是工程推论：已发布上下文容量1,000,000 token覆盖有效输入，官方max_completion_tokens含推理与回答且允许最多10 token偏差，因此在当前参数1024下预占1034输出。未找到适用于该模型和Chat工具模板的已验证预计数接口；不借用OpenSearch或Embedding tokenizer替代。每次真实请求记录请求hash、参数版本、价格版本、预占、dispatched状态与usage，价格由操作者配置。[Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)、[官方计费](https://help.aliyun.com/zh/model-studio/model-pricing)。
- 研究中的LangGraph/psycopg源码仅用于约束设计，部署不得使用浮动main/master依赖。最终UV锁定后必须证明显式事务内官方saver写入与accepted更新共同回滚、失租写入拒绝；不得以源码推断代替运行验收。[官方持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[Psycopg pipeline](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html)。

当前Git提交因自动审批服务模型容量不足而未执行，本规格作为工作区文件保存；用户已确认规格，研究与审阅不等于第三阶段已经开发或测试通过。

独立规格审阅提出的三项缺口已在本规格修订：显式串行chat/直接图内检索；fork真实来源也必须明确选入；终态不可恢复及完整thread清理。长行预览、确定装配、评测单位和中间记录上限同时明确；这些设计约束已经用户确认，尚未成为实现或测试结果。
