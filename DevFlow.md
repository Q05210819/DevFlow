**DevFlow AI 多智能体研发协作项目**

**想要实现得效果**：
DevFlow AI 能够帮助用户分析issue、审查PR、排查CI失败的研发写作助手，当用户提出问题，它会自己调用相关工具，查代码、翻日志、找项目文档，再把分析结果和依据整理给用户

产品设计界面如下图

![img](https://cdn.xiaolincoding.com/devflow/project-introduction-57cfaca3.png)

**DevFlow AI 是什么**：

**构建数据底座**：能够同步GitHub上的Issue、PR、CI、代码、知识库等全链路数据，打破信息孤岛，为智能分析提供基础

**智能知识检索（RAG）**：将研发数据转化为可检索的知识，让Agent在回前先“查阅”项目档案，基于真实证据而不是凭空猜测

**工具调用与执行**：它不是只会聊天的机器人，而是会使用工具的助手。能够根据任务自动选择工具，比如查CI、分析PR，并给出可落地的建议

他不是要代替工程师，而是要成为用户的得力助手，通过自动化处理繁琐的上下文收集和分析工作，让用户可以将精力聚焦于核心的研发决策

**DevFlow AI解决什么样的问题场景**：

在团队开发中，判断一次代码改动能不能合并，往往要先看 PR 改了什么，再翻 Issue 里的需求、Review 评论和 CI 自动化测试结果。遇到报错，还得查日志、搜代码、找项目文档。

DevFlow AI 就是为这类研发协作场景设计的。它会根据问题调用工具，收集相关代码、日志和项目资料，完成第一轮分析，再把风险、依据和处理建议整理给用户。

让 Agent 帮忙查资料、做分析，工程师核对结果，再做最终决策。

**DevFlow AI具体能做什么**：

1.PR审查：从检查计划到审查结果：Agent按计划检查代码改动和相关证据，再整理风险与合入建议

2.多Agent协作：先列计划，再分工分析：提出版本检查问题后，系统先列执行计划，确认执行后，再汇总各个Agent的分析结果

3.工作流记录：查看分工、结论与待确认项：在工作流记录中，可以看到各个Agent的输出、汇总结论，以及Observer提醒的证据缺口

工程师拿到的是一份集中整理的分析建议，包含风险，依据和待确认项，再核对结果、做最终决策

除了这类综合分析，也可以单独进行 Issue 分诊、PR 审查、CI 排障和项目知识问答

**DevFlow AI 项目核心功能有哪些**：

1. **仓库健康全景（Repo Health）：** 能够同步 GitHub 仓库状态，一键生成包含 Issues、PRs、CI 状态在内的项目健康摘要，让你快速掌握全局背景。
2. **Issue 智能分诊（Issue Agent）：** 自动分析新问题的分类、优先级、复杂度，推荐负责人并提取行动项，告别模糊的需求描述。
3. **PR 风险审查（PR Review Agent）：** 深度理解代码变更，识别 Auth、Security 等高风险路径，给出「建议合入」或「暂缓」的明确决策建议。
4. **CI 智能排障（CI Debug Agent）：** 面对失败的 CI 日志，能自动定位根因、提取关键错误块，并给出具体的修复步骤，不再需要人肉翻日志。
5. **多 Agent 协同决策（Workflow）：** 面对「PR 能不能合」这种复杂问题，Planner、PR Agent、CI Agent 和 Observer 会分工协作，像专家团队一样给出综合结论。
6. **超长对话与记忆（Memory）：** 具备长时记忆能力，能理解长上下文，自动沉淀关键信息，让 Agent 在长对话中依然「不健忘」。
7. **自动周报与沉淀（Report）：** 根据一周的研发活动自动生成周报，并将报告回写入知识库，让团队的历史产出变成可检索的资产。
8. **安全草稿机制（Safety Draft）：** 在涉及评论、关闭 Issue 等写操作时，只生成草稿供人工确认，绝不擅自执行高风险动作，确保系统安全可控。

**DevFlow AI 项目使用什么语言和技术栈**：

DevFlow AI 的后端和 Agent 主要使用 **Python**，前端使用 **Next.js、React 和 TypeScript**。

核心技术栈包括：**FastAPI、LangChain、LangGraph、PostgreSQL、Milvus、MCP、SSE、Ragas 和 Docker Compose**。

从前端提交问题，到后端调用工具、检索证据、组织多个 Agent 协作，再到页面展示结果，整条链路都有对应实现





**项目架构**

DevFlow AI 使用 **Python Agent 后端 + Next.js 前端**，从用户交互到数据存储，可以分成五层：

![img](https://cdn.xiaolincoding.com/devflow/project-architecture-7e32fa15.png)

1. **用户层：**你在前端选择仓库和会话，提交要分析的问题。
2. **应用服务层：**FastAPI 校验请求、确定仓库与会话范围，并通过 SSE 把执行进度持续推给页面。
3. **智能协作层：**ChatAgent 选择工具；需要综合分析时，Planner 拆任务，Orchestrator 按依赖调度，Observer 检查证据缺口与冲突，Synthesis 汇总结论。
4. **知识与工具层：**GitHub 工具读取研发信息，Workspace 工具搜索代码，RAG 检索历史资料，Memory 提供经过确认的项目经验；MCP 和 Skill 用于扩展工具与复用流程。
5. **数据基础层：**PostgreSQL 保存结构化数据和运行记录，Milvus 保存向量索引，Git 工作区提供对应版本的代码。

整体各层的分工：**上层负责交互与请求处理，中间负责 Agent 调度，下层提供工具、知识和数据。**

**全局链路**

把一次研发问答分成了五个环节

![img](https://cdn.xiaolincoding.com/devflow/request-flow-d5d7fbd3.png)

1. **① 用户输入：**从一个具体的研发问题开始，比如「这个 PR 能不能合？」。
2. **② 接口与上下文：**请求进入 FastAPI Chat API，由 ContextAssembler 把仓库信息、历史对话、相关记忆和证据组织起来，让 Agent 知道当前在讨论哪个项目、什么问题。
3. **③ 对话 Agent 与工具调用：**图中的 ConversationAgent（对话 Agent）判断需要查什么，再调用仓库健康概况、研发资料检索、Issue 分析、PR Review、CI Debug 等工具，收集判断所需的信息。
4. **④ 多 Agent 协作分析：**遇到需要综合判断的问题，Planner 拆解任务，专用 Agent 分工分析，Observer 检查证据、冲突和安全条件，Synthesis 汇总工程结论。简单查询则可以通过工具调用直接完成。
5. **⑤ 输出与记忆：**返回结论、依据和下一步建议，同时保存会话与工具调用记录。可复用的项目经验经过确认后，再参与后续记忆与知识检索。

整条链路可以简单理解为：**用户提问 → 组装上下文 → Agent 调工具 → 按需协作分析 → 返回答案并保留记录。**

**项目亮点**

- **研发数据分流**：当前代码通过 Workspace 工具读取和搜索，Issue、PR、CI 的状态查数据库或 GitHub API，历史文档与讨论再走 RAG。把不同来源分开处理，减少拿过期文档解释当前代码、用历史讨论判断最新状态的问题。
- **RAG 工程化检索**：按照文档结构切分内容，通过向量与关键词混合召回、RRF 融合和 Rerank 重排筛选证据，回答保留引用，并按仓库隔离检索范围。检索没找到内容时，还能通过召回测试检查切分、排序和过滤出了什么问题。
- **Agent Loop 与执行约束**：ChatAgent 通过原生 Tool Calling 选择工具，接收结果后继续判断下一步。系统同时限制最大步数和重复调用，并处理工具错误，让多轮执行有明确的停止条件。
- **专用 Agent 分工**：IssueAgent 负责分类、优先级和行动项，PRReviewAgent 结合改动与研发证据分析风险，CIDebugAgent 提取失败日志中的关键错误。不同任务分别设计 Prompt、输入证据和 Pydantic 输出结构，方便单独验证和调整。
- **多 Agent 任务调度**：Planner 拆解任务，Orchestrator 按依赖调度，可独立的任务并行执行；Observer 检查证据缺口和结论冲突，Synthesis 汇总建议。依赖任务失败时跳过后续任务，重规划也有次数上限，避免工作流反复空转。
- **上下文预算与长期记忆**：最近对话、历史摘要、工具结果和检索证据分别组织，根据 Token 压力逐步压缩上下文。值得跨会话复用的经验先进入候选区，经过人工批准后再参与召回，减少错误结论被长期沿用的风险。
- **MCP 与 Skill 扩展**：通过 MCP 接入外部工具，通过 Skill Runtime 组织可复用的操作流程。学习时可以沿着一次调用，理解外部能力怎样接入 ChatAgent、怎样校验输入，以及调用结果怎样回到后续分析中。
- **安全草稿与人工确认**：评论、创建或关闭 Issue、添加标签等写操作，先生成 ActionDraft，经过权限校验和人工确认后再调用 GitHub API，并用 AuditLog 记录执行结果。模型提出操作，系统负责检查执行条件。
- **流式进度与运行轨迹**：通过 SSE 展示规划、工具调用和任务进度，后端保存 AgentRun、WorkflowRun、TaskRun。回答出错时，可以沿着记录检查是工具参数有误、检索证据不足，还是多个任务的结论出现了冲突。
- **Agent Eval 与回归验证**：用固定评测集、RAGAS、逐题证据和硬规则检查回答与执行过程。改了 Prompt、检索配置或工具逻辑后，再用同一组问题复测，把改善、退步和失败原因都留下来。



**项目技术栈**

| 模块       | 主要技术与职责                                               |
| :--------- | :----------------------------------------------------------- |
| 前端交互   | Next.js、React、TypeScript、Tailwind CSS、React Query：展示对话、研发数据、分析结果和任务进度。 |
| 后端服务   | Python、FastAPI、Pydantic、SQLAlchemy、Alembic、SSE：处理 API、校验结构化数据、管理数据库并推送执行事件。 |
| Agent 编排 | LangChain、LangGraph、原生 Tool Calling：组织模型、消息、工具、状态流转和停止条件；通过 OpenAI 兼容接口接入支持相应能力的模型。 |
| 知识与证据 | PostgreSQL、Milvus、Embedding、混合检索、RRF、Rerank：管理事实与检索知识；GitHub REST API、Workspace 工具读取研发状态与代码。 |
| 能力扩展   | MCP、Skill Runtime：接入外部工具，把稳定的操作流程组织成可复用能力。 |
| 质量与部署 | Ragas、pytest、pytest-asyncio、Docker Compose：评测回答与执行行为、验证关键链路、组织本地服务部署 |