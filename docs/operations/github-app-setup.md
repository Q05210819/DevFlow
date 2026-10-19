# GitHub App 设置

本文针对 github.com、2A 只读 Issue、PR、Actions 同步与健康事实，也保留一期访问检查。用户授权、App 安装范围、允许安装列表及 DevFlow 成员/仓库启用状态须同时满足；平台管理员不会自动获得仓库访问权。用户尚无真实 App；以下创建、私有仓库及公网 HTTPS 接入验收均为 **NOT EXECUTED**。[用户与 App 权限交集](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)

## 创建与安装

在个人或组织设置的 Developer settings → GitHub Apps → New GitHub App 创建。按实际目标选择安装可见范围；安装到其他账号/组织需要该目标允许安装。Homepage URL 可以是项目介绍页，不是 OAuth 回调。[注册 App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)

| 设置 | 填写值 |
| --- | --- |
| Callback URL | `<PUBLIC_BASE_URL>/api/v1/auth/github/callback` |
| Request user authorization during installation | 不勾选；从 DevFlow `GET /api/v1/auth/github/start` 发起登录，以建立浏览器绑定 state/PKCE |
| Expire user authorization tokens | 保持启用；用户 token 默认 8 小时，refresh token 6 个月，和平台会话是不同凭据 |
| Device Flow | 不启用 |
| Setup URL / Redirect on update | 留空 / 不启用 |
| Webhooks | Active，URL 为 `<PUBLIC_BASE_URL>/api/v1/github/webhooks`，设置随机 secret，启用 SSL verification |
| Repository permissions | Metadata、Issues、Pull requests、Actions、Contents：Read-only；不申请 Checks、Administration、Workflows 或任何写权限 |
| Organization / Account permissions | 保持 No access |

回调请求会明确匹配环境的 `redirect_uri`，不要混用 localhost、127.0.0.1、不同端口或生产域名。GitHub 允许注册多个回调；开发和生产应分别核对。登录默认返回 `/repositories`；允许 return_path 仅为 `/`、`/repositories`、`/jobs/<UUID>`、`/admin/users`、`/admin/audit`。用户不需要手填 state 或 PKCE。[回调说明](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-user-authorization-callback-url)、[刷新令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)

三类生命周期事件 `installation`、`installation_repositories`、`github_app_authorization` 在启用 webhooks 时默认投递，不能手动勾选订阅。平台以原始请求体 `X-Hub-Signature-256` 验签，并按 `X-GitHub-Delivery` 去重；撤销 action 包括 `revoked`，安装/仓库撤权会失效本地权限；恢复事件不能直接授予访问。[事件定义](https://docs.github.com/en/webhooks/webhook-events-and-payloads)、[签名验证](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

选定权限后手动订阅 `issues`、`issue_comment`、`pull_request`、`pull_request_review`、`pull_request_review_comment`、`workflow_run`、`workflow_job`、`push`、`create`、`delete`、`repository`。PR 会话评论属于 issue_comment，差异评论属于 pull_request_review_comment。Contents 用于获批的 ref 事件及未来 Git 读取；2A 不克隆代码，Workspace 仍不可用。事件不是完整历史日志，保留周期补漏、回填和明确覆盖缺口。[权限矩阵](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)、[事件定义](https://docs.github.com/en/webhooks/webhook-events-and-payloads)

### 已安装 App 增加权限

未来修改注册权限时，安装账号需在更新通知或 Installed GitHub Apps 配置中批准新增仓库权限；未批准的安装保留旧权限。不要求卸载重装。新安装会在首次安装时审查权限。批准后核对实际权限与选定仓库，并通过 DevFlow 的重新接入/验证流程恢复本地绑定；通知、重新登录或 webhook 本身都不是本地授权证明。[修改权限](https://docs.github.com/en/apps/maintaining-github-apps/modifying-a-github-app-registration#changing-the-permissions-of-a-github-app)、[批准更新](https://docs.github.com/en/apps/using-github-apps/approving-updated-permissions-for-a-github-app)

后台安装令牌按单仓库和当前流所需只读权限签发，保留内存缓存；不授权浏览器读取、不持久化、不记录日志。令牌为不透明字符串，不假设前缀或固定长度。用户令牌保持上表的过期选项；现有 OAuth state/PKCE/refresh 流程不变，`offline_access` 是在 App 关闭过期时选择启用过期的条件选项，本设置不需要添加它。[安装令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)、[用户令牌刷新](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens#about-user-access-tokens-that-expire)

生成 Client secret 和 RSA Private key，按 [秘密文件说明](deployment.md#输入与秘密文件) 保存。Install App → 选择目标账号/组织 → Only select repositories → 仅选专用私有测试仓库。组织策略可能需要所有者批准。不要以“全部仓库”替代问题定位。[安装自己的 App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)

## 配置对应关系

| DevFlow 配置 | 来源 |
| --- | --- |
| `PUBLIC_BASE_URL` | 浏览器实际访问的 origin；生产 HTTPS，无额外路径 |
| `BOOTSTRAP_ADMIN_GITHUB_ID` | 首任管理员的 GitHub **用户数字 ID**；可通过 `GET /users/{username}` 的 `id` 核对 |
| `GITHUB_APP_ID` | App 设置页的数字 App ID |
| `GITHUB_CLIENT_ID` | 同一 App 的 Client ID 字符串 |
| `GITHUB_ALLOWED_INSTALLATION_IDS` | 安装数字 ID 的 JSON 数组，例如 `[123456]`；不是用户、App 或仓库 ID |
| `GITHUB_CLIENT_SECRET_FILE` | Client secret 文件的进程内绝对路径 |
| `GITHUB_PRIVATE_KEY_FILE` | RSA PEM 文件的进程内绝对路径 |
| `GITHUB_WEBHOOK_SECRET_FILE` | 与 GitHub 设置相同的 webhook secret 文件 |
| `CREDENTIAL_ENCRYPTION_KEY_FILE` | 本地独立生成的 Fernet key 文件 |
| `APP_SIGNING_KEY_FILE` | 本地独立生成的至少 32 字节签名密钥文件 |

Compose 自动映射文件到 `/run/secrets/*`；本地进程使用主机绝对路径。数据库 URL、口令和密钥生成方法见 [部署](deployment.md)。安装完成后从安装详情/API 核对数字 ID；安装回跳的 `installation_id` 参数本身不是权限证明。[安装 API](https://docs.github.com/en/rest/apps/installations)、[用户 ID API](https://docs.github.com/en/rest/users/users#get-a-user)

## 联调与人工验收

先确认 `/health/ready` 为 200，再从浏览器登录。GitHub 无法向 localhost/127.0.0.1 投递 webhook；本机临时转发只用于开发，真实验收使用外部可达的 HTTPS 地址。[Webhook 排查](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/troubleshooting-webhooks)

1. 空库引导指定用户为管理员；用另一个未被加入允许名单的账号确认登录被拒。重启不能重置成员角色。
2. 登录后发现本人可访问、且在允许安装中的私有仓库，由管理员接入；另一位无 GitHub 权限的允许成员不能看到该仓库。
3. 提交 `repository.access_probe`，核对结果中的数字仓库 ID、名称、私有状态、默认分支及检查时间；读取事件与持久终态。此检查不分析代码。
   仓库接入还会自动请求事实同步。打开“查看事实与健康”，核对 Issues、PR base/head SHA、CI run/attempt、子集合、真实覆盖与健康输入清单；手动投递内容事件后核对请求/已处理序号追平。源权限不足或缺失补丁必须显示缺口，不能解释为成功或零记录。
4. 移除仓库安装权限、停用/卸载安装、撤销用户 App 授权，分别检查 GitHub Recent deliveries 和平台权限拒绝；旧结果不能继续读取。
5. 记录环境、时间、观察值及证据；重启恢复和备份演练见 [备份与恢复](backup-restore.md)。

恢复安装、重新加入仓库等 webhook 只把资源置为待验证（`needs_verification`），不会直接恢复访问。管理员必须在仓库页面明确重新接入；后端重新验证 GitHub 权限及本地版本后恢复原仓库 UUID。单纯刷新发现列表不会恢复授权。

用户撤权和 OAuth 发起共享数据库 `auth_change_order_seq`（CACHE 1、NO CYCLE），分别记录 `users.auth_changed_order` 和 `oauth_states.started_order`。撤权前发起的迟到回调不能恢复授权；`auth_changed_at` 仅保留审计时间，不用于决定先后。备份恢复必须保留序列状态，详见 [备份与恢复](backup-restore.md)。

一期证据仍见 [一期验收](foundation-acceptance.md)；新增同步/事实接口见 [2A 规格](../superpowers/specs/2026-10-13-devflow-data-foundation-design.md) 和 [2A 验收](data-foundation-acceptance.md)。离线 transport、浏览器联调与镜像构建不能证明真实 GitHub 接入成功。
