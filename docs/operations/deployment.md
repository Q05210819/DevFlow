# 部署与升级

本文提供 Linux Compose 生产配置及 localhost 开发覆盖的操作步骤。Task 12A 已准备打包；完整后端合入前不要把构建成功理解为可用平台验收。Task 12B 仍需执行空库启动、重启、Worker 租约回收、浏览器联调及恢复演练。未执行真实 App 登录或外部部署。

## 输入与秘密文件

需要 Docker Engine、Compose **2.24.4+**、足够的持久磁盘，以及自己的 HTTPS 域名（DNS 指向主机、80/443 可达）。生产只发布 Caddy 的 80/443；API、Worker、前端和 PostgreSQL 在项目网络内通信。数据库不公开端口。固定版本见 [本地开发](local-development.md)，App 注册见 [GitHub App 设置](github-app-setup.md)。

从根目录复制 `.env.example` 为 `.env`。填写真实 `PUBLIC_BASE_URL=https://你的域名`、GitHub ID/安装列表；`DEVFLOW_SECRETS_DIR` 填主机绝对目录，建议位于仓库外。`DEVFLOW_IMAGE_TAG` 用本次发布的 Git commit，构建日志也记录 commit。生产 Compose 强制 `APP_ENV=production`，不从 `.env` 接受降级设置。

秘密只在运行时挂载，无 build args 或镜像环境变量承载秘密。目录包含以下七个文件，文件名没有扩展名：

| 文件 | 精确内容 |
| --- | --- |
| `postgres_password` | 独立随机数据库密码（建议 URL 安全的随机字符） |
| `database_url` | `postgresql+psycopg://devflow:URL编码后的密码@postgres:5432/devflow`；密码与上项一致 |
| `github_client_secret` | GitHub App Client secret 的原文 |
| `github_private_key` | GitHub 下载的 RSA PEM 私钥，保留换行 |
| `github_webhook_secret` | 与 GitHub Webhooks 设置一致的随机字符串 |
| `credential_encryption_key` | Fernet key：32 个随机字节经 URL-safe Base64 编码，通常 44 个 ASCII 字符 |
| `app_signing_key` | 独立签名密钥；去除首尾空白后至少 32 字节。可用 32 随机字节的 64 字符 hex 文本，程序使用文本原始字节，不按 Fernet 解码 |

两个本地密钥必须独立生成。下面的 Python 经 UV 执行，只写文件，不输出密钥；**只用于首次生成，不覆盖既有文件**。先将 `DEVFLOW_SECRETS_DIR` 设置为自己的绝对目录。Client secret 和 PEM 从 GitHub 设置写入对应文件；生成的 webhook secret 则填写到 GitHub Webhooks 设置，保持两端相同。秘密不能发到聊天或写进命令行参数。

```powershell
$env:DEVFLOW_SECRETS_DIR = 'D:/devflow-secrets'
@'
import os, secrets
from pathlib import Path
from cryptography.fernet import Fernet
p = Path(os.environ['DEVFLOW_SECRETS_DIR'])
p.mkdir(parents=True, exist_ok=True)
password = secrets.token_urlsafe(32)
values = {
    'postgres_password': password.encode(),
    'database_url': f'postgresql+psycopg://devflow:{password}@postgres:5432/devflow'.encode(),
    'credential_encryption_key': Fernet.generate_key(),
    'app_signing_key': secrets.token_hex(32).encode(),
    'github_webhook_secret': secrets.token_hex(32).encode(),
}
if any((p / name).exists() for name in values):
    raise SystemExit('Existing secret file: no files were changed')
for name, value in values.items():
    with (p / name).open('xb') as out:
        out.write(value)
'@ | uv --directory backend run --no-sync python -
```

Linux 使用同一 Python 代码的 heredoc：`uv --directory backend run --no-sync python - <<'PY'`，把上述 Python 正文放在中间，以单独的 `PY` 行结束。先 `export DEVFLOW_SECRETS_DIR=/srv/devflow-secrets`。备份加密密钥后再写入 GitHub client secret/PEM；真实启动前七个文件必须齐全。

Linux 文件型 Compose secrets 使用绑定挂载，`uid/gid/mode` 不会替你调整主机文件权限。将秘密目录设为仅部署管理员可进入（例如 root `0700`），在该目录内设置容器能读取的文件权限（例如 root `0444`），并验证 UID10001 的 API/Worker 与 postgres 用户实际可读；不要把目录对本机其他用户开放。Windows 使用目录 ACL 仅授权当前部署账号及 Docker Desktop 所需身份。不要直接对真实 secret 执行 `cat`/`Get-Content` 或输出完整 Compose 配置。[Compose secrets 限制](https://docs.docker.com/reference/compose-file/services/#secrets)

镜像入口 `sh -eu` 读取 `/run/secrets/database_url`，仅向当前进程导出 `DATABASE_URL` 并 `exec` 最终命令；Settings 不存在 `DATABASE_URL_FILE` 选项。其他五个应用秘密由 `_FILE` 指向 `/run/secrets/*`。密钥和连接串不会出现在 `docker inspect` 的静态环境配置中；主机及容器管理员仍具备读取运行时秘密的能力。

## 首次部署

以下是待完整后端合入后执行的步骤，不代表已经运行成功：

```bash
docker compose --env-file .env -p devflow -f infra/compose.yaml config --quiet
docker compose --env-file .env -p devflow -f infra/compose.yaml build api frontend gateway
docker compose --env-file .env -p devflow -f infra/compose.yaml up -d
docker compose --env-file .env -p devflow -f infra/compose.yaml ps -a
curl --fail https://你的域名/health/live
curl --fail https://你的域名/health/ready
```

启动顺序：postgres 健康 → migrate 执行 `alembic upgrade head` 和 `python -m devflow.bootstrap` 成功退出 → API、Worker → gateway。迁移失败会阻止依赖服务启动；不要通过移除 depends_on 强行启动。[Compose 启动依赖](https://docs.docker.com/compose/how-tos/startup-order/)

`BOOTSTRAP_ADMIN_GITHUB_ID` 是用户数字 ID；bootstrap 仅在 users 为空时创建首任管理员与审计，不会把数据库已有用户改成管理员。不要删除 users 来修复登录。API readiness 验证数据库及镜像中迁移 head；存活 200 不等于就绪 200。

API 和 Worker 使用同一个后端镜像（UID10001）；前端 standalone 使用 node 用户；gateway 使用 UID10001，监听 8080/8443，宿主机映射 80/443。Caddy 的 `/data` 持久保存证书，`/config` 也挂持久卷。PostgreSQL 使用 postgres 用户和项目独立数据卷。

Caddy 原样转发 `/api/*`、`/health/*` 到 API，其余到前端。`flush_interval -1` 立即发送流数据，未设置会截断长 SSE 的整体请求时限。没有启用网关访问日志；API 始终使用 `--no-access-log`。增加外部代理时必须继续避免记录 OAuth query、Cookie、Authorization，且保留 SSE 心跳。[Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)

Caddy 的 `http.log.error` 即使关闭访问日志也会在上游故障时记录完整 URI，因此显式排除该命名空间。使用 API 的结构化 request_id/状态码日志与健康检查定位请求；网关本身的启动、TLS 等运行日志保留。网关上游故障只观察状态码和健康状态，不保存请求原文。[Caddy 全局日志配置](https://caddyserver.com/docs/caddyfile/options#log)

## 开发覆盖

见 [本地开发](local-development.md)。生产命令**不要**添加 `compose.dev.yaml`。该覆盖只开放 loopback 8080/55440，并启用开发 Cookie。合成密钥可验证配置和启动，但不能实现真实 GitHub 登录；不得在真实环境使用 CI 假 ID/假秘密。

## 升级、回退与维护

1. 记录当前 Git commit、镜像 ID、迁移 revision，并按 [备份与恢复](backup-restore.md) 备份数据库和对应密钥。
2. 构建新 tag。先停止本项目 API、Worker、gateway，避免迁移时继续写入；postgres 保持运行。
3. 显式运行一次 `docker compose --env-file .env -p devflow -f infra/compose.yaml run --rm migrate`，确认退出 0。它只启动所需 postgres，不启动 Worker。
4. 用 `up -d --force-recreate` 重建本项目服务，检查 migrate、readiness、登录和一次只读检查。

应用回退只在旧镜像仍兼容当前 schema 时使用旧 tag，并通过 `up -d --no-build` 启动。破坏性迁移需要恢复到**独立数据库**验证，不能只切回镜像、盲目降级 schema 或覆盖生产卷。常规停止用 `stop`；不要 `down -v`。禁止为本项目清理而执行全局 `docker system prune` 或操作其他项目容器/卷。
