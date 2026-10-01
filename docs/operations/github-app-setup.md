# GitHub App 设置

本文针对 github.com、一期只读仓库元数据检查。用户授权、App 安装范围、允许安装列表及 DevFlow 成员/仓库启用状态须同时满足；平台管理员不会自动获得仓库访问权。真实 App、私有测试仓库及公网 HTTPS 入口尚未配置，以下真实接入验收**未执行**。[用户与 App 权限交集](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)

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
| Repository permissions | 仅 Metadata: Read-only；不申请 Contents、Issues、PR、Actions、Checks 或任何写权限 |
| Organization / Account permissions | 保持 No access |

回调请求会明确匹配环境的 `redirect_uri`，不要混用 localhost、127.0.0.1、不同端口或生产域名。GitHub 允许注册多个回调；开发和生产应分别核对。登录默认返回 `/repositories`；允许 return_path 仅为 `/`、`/repositories`、`/jobs/<UUID>`、`/admin/users`、`/admin/audit`。用户不需要手填 state 或 PKCE。[回调说明](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-user-authorization-callback-url)、[刷新令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)

三类生命周期事件 `installation`、`installation_repositories`、`github_app_authorization` 在启用 webhooks 时默认投递，不能手动勾选订阅。平台以原始请求体 `X-Hub-Signature-256` 验签，并按 `X-GitHub-Delivery` 去重；撤销 action 包括 `revoked`，安装/仓库撤权会失效本地权限；恢复事件不能直接授予访问。[事件定义](https://docs.github.com/en/webhooks/webhook-events-and-payloads)、[签名验证](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

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
4. 移除仓库安装权限、停用/卸载安装、撤销用户 App 授权，分别检查 GitHub Recent deliveries 和平台权限拒绝；旧结果不能继续读取。
5. 记录环境、时间、观察值及证据；重启恢复和备份演练见 [备份与恢复](backup-restore.md)。

上述路由与 [已批准 API 合同](../superpowers/specs/2026-09-30-devflow-foundation-design.md#7-数据表与-api-边界) 对齐。Task 12A 基线尚未合入全部路由与 Worker，实际注册/redirect_uri/webhook action 的联调须由 Task 12B 核对。不能将离线 transport、浏览器拦截或镜像构建报告成真实 GitHub 接入成功。
