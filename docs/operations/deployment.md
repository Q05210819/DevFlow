# 部署与升级

本文提供 Linux Compose 生产配置及 localhost 开发覆盖的操作步骤。一期执行证据见 [一期验收](foundation-acceptance.md)，新增同步、健康和镜像/恢复证据见 [2A 验收](data-foundation-acceptance.md)；真实 App 登录、公网 TLS 签发及外部部署为 **NOT EXECUTED**。

## 输入与秘密文件

需要 Docker Engine、Compose **2.24.4+**、足够的持久磁盘，以及自己的 HTTPS 域名（DNS 指向主机、80/443 可达）。生产只发布 Caddy 的 80/443；API、Worker、前端和 PostgreSQL 在项目网络内通信。数据库不公开端口。固定版本见 [本地开发](local-development.md)，App 注册见 [GitHub App 设置](github-app-setup.md)。

从根目录复制 `.env.example` 为 `.env`。填写真实 `PUBLIC_BASE_URL=https://你的域名`、GitHub ID/安装列表；`DEVFLOW_SECRETS_DIR` 填主机绝对目录，建议位于仓库外。`DEVFLOW_IMAGE_TAG` 用本次发布的 Git commit，构建日志也记录 commit。生产 Compose 强制 `APP_ENV=production`，不从 `.env` 接受降级设置。

秘密只在运行时挂载，无 build args 或镜像环境变量承载秘密。目录包含以下九个文件，文件名没有扩展名：

| 文件 | 精确内容 |
| --- | --- |
| `postgres_password` | 独立随机数据库密码（建议 URL 安全的随机字符） |
| `database_url` | `postgresql+psycopg://devflow:URL编码后的密码@postgres:5432/devflow`；密码与上项一致 |
| `github_client_secret` | GitHub App Client secret 的原文 |
| `github_private_key` | GitHub 下载的 RSA PEM 私钥，保留换行 |
| `github_webhook_secret` | 与 GitHub Webhooks 设置一致的随机字符串 |
| `credential_encryption_key` | Fernet key：32 个随机字节经 URL-safe Base64 编码，通常 44 个 ASCII 字符 |
| `app_signing_key` | 独立签名密钥；去除首尾空白后至少 32 字节。可用 32 随机字节的 64 字符 hex 文本，程序使用文本原始字节，不按 Fernet 解码 |
| `minio_access_key` | Milvus 对象存储的独立 root 用户名；20 字符随机 hex，容器内文件不得换行 |
| `minio_secret_key` | 与上项配对的独立 root 密码；至少 8 字符的随机文本，容器内文件不得换行 |

MinIO 用 `MINIO_ROOT_USER_FILE`/`MINIO_ROOT_PASSWORD_FILE` 读取文件；Milvus 本身不支持凭据 `_FILE`，Compose 用最小的 `sh -eu -c` 包装读取同一对挂载文件后 `exec milvus run standalone`。凭据不会写入跟踪文件或静态容器环境。两个本地密钥必须独立生成。下面的 Python 经 UV 执行，只写文件，不输出密钥；**只用于首次生成，不覆盖既有文件**。先将 `DEVFLOW_SECRETS_DIR` 设置为自己的绝对目录。Client secret 和 PEM 从 GitHub 设置写入对应文件；生成的 webhook secret 则填写到 GitHub Webhooks 设置，保持两端相同。秘密不能发到聊天或写进命令行参数。

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
    'minio_access_key': secrets.token_hex(10).encode(),
    'minio_secret_key': secrets.token_urlsafe(32).encode(),
}
if any((p / name).exists() for name in values):
    raise SystemExit('Existing secret file: no files were changed')
for name, value in values.items():
    with (p / name).open('xb') as out:
        out.write(value)
'@ | uv --directory backend run --no-sync python -
```

Linux 使用同一 Python 代码的 heredoc：`uv --directory backend run --no-sync python - <<'PY'`，把上述 Python 正文放在中间，以单独的 `PY` 行结束。先 `export DEVFLOW_SECRETS_DIR=/srv/devflow-secrets`。备份加密密钥后再写入 GitHub client secret/PEM；真实启动前九个文件必须齐全，MinIO 凭据文件同时供 Milvus 读取。

Linux 文件型 Compose secrets 使用绑定挂载，`uid/gid/mode` 不会替你调整主机文件权限。将秘密目录设为仅部署管理员可进入（例如 root `0700`），在该目录内设置容器能读取的文件权限（例如 root `0444`），并验证 UID10001 的 API/Worker 与 postgres 用户实际可读；不要把目录对本机其他用户开放。Windows 使用目录 ACL 仅授权当前部署账号及 Docker Desktop 所需身份。不要直接对真实 secret 执行 `cat`/`Get-Content` 或输出完整 Compose 配置。[Compose secrets 限制](https://docs.docker.com/reference/compose-file/services/#secrets)

镜像入口 `sh -eu` 读取 `/run/secrets/database_url`，仅向当前进程导出 `DATABASE_URL` 并 `exec` 最终命令；Settings 不存在 `DATABASE_URL_FILE` 选项。其他五个应用秘密由 `_FILE` 指向 `/run/secrets/*`。密钥和连接串不会出现在 `docker inspect` 的静态环境配置中；主机及容器管理员仍具备读取运行时秘密的能力。

## 首次部署

部署到自己的主机时执行以下步骤；示例域名须替换为实际配置：

```bash
docker compose --env-file .env -p devflow -f infra/compose.yaml config --quiet
docker compose --env-file .env -p devflow -f infra/compose.yaml build api frontend gateway model-worker
docker compose --env-file .env -p devflow -f infra/compose.yaml up -d
docker compose --env-file .env -p devflow -f infra/compose.yaml ps -a
curl --fail https://你的域名/health/live
curl --fail https://你的域名/health/ready
```

启动顺序：postgres 健康 → migrate 执行 `alembic upgrade head` 和 `python -m devflow.bootstrap` 成功退出 → API、Worker → gateway。Milvus 栈（etcd → MinIO → Milvus）另行健康启动；`model-worker` 等待 postgres、migrate 和 Milvus 健康后才启动。迁移失败会阻止依赖服务启动；不要通过移除 depends_on 强行启动。[Compose 启动依赖](https://docs.docker.com/compose/how-tos/startup-order/)

`BOOTSTRAP_ADMIN_GITHUB_ID` 是用户数字 ID；bootstrap 仅在 users 为空时创建首任管理员与审计，不会把数据库已有用户改成管理员。不要删除 users 来修复登录。API readiness 验证数据库及镜像中迁移 head；存活 200 不等于就绪 200。

API、Worker 和 migrate 使用同一个后端镜像（UID10001）；`model-worker` 使用单独的 `devflow-backend-models` 镜像（同一 UID，额外安装可选模型依赖）；前端 standalone 使用 node 用户；gateway 使用 UID10001，监听 8080/8443，宿主机映射 80/443。Caddy 的 `/data` 持久保存证书，`/config` 也挂持久卷。PostgreSQL 使用 postgres 用户和项目独立数据卷。

Caddy 上游二进制带 `cap_net_bind_service=ep`，与容器 `cap_drop: [ALL]` 同用会导致 Linux 拒绝执行。构建时移除不需要的文件能力，保留非 root、全部能力丢弃及 `no-new-privileges`；内部高端口无需该能力。启动后必须同时检查 `ps -a` 和实际网关 HTTP，不能只看 `up --wait` 的退出码：没有 healthcheck 的服务可能在命令返回后立即失败。

Worker 生命周期审计的 `actor_id` 为 `null`（系统），保留 `job_id` 和资源 ID；原提交者由 `jobs.requested_by` 及 `job.submitted` 审计关联。正常 SIGTERM 停止领取并最多等待 30 秒；未完成任务保留租约，后续 Worker 按数据库时间回收。强杀恢复会增加尝试次数，旧租约不能写回状态、事件或作业审计。

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

## 2A 同步与迁移操作

2A 当前源码迁移 head 是 `0008_sync_repair`，在规范化事实、同步游标、内容事件与独立健康请求之上增加每日修复尝试截止时间；升级的 NULL 截止使已有仓库保守核对一次。旧一期镜像与此 schema 不保证兼容。迁移 0005/0006/0007 的降级保护会拒绝丢弃已有二期证据，0008 也拒绝丢弃非 NULL 修复截止；空库往返测试不代表含数据生产库可以降级。回退前备份数据库及对应密钥，停止本项目写入者，在独立数据库恢复并使用匹配源码/镜像和 revision 验证；保留当前卷作为恢复依据。Task8 容器证据仍是旧 head0007 镜像，未含本轮修复；本轮没有重建/部署镜像，没有新增含数据备份恢复或生产降级证据。后续2B从0009_workspace开始。

API、Worker、migrate 共用锁定后端镜像；`docker compose build api frontend gateway` 同时准备 Worker 所用镜像，不添加外部服务。SYNC_* 设置来自 `.env.example`，所有后端进程保持一致。单安装请求串行、单仓库一个执行单元，默认全局两个同步单元；通用 Worker 并发四不等于四个事实采集单元。`SYNC_UNIT_REQUESTS` 仅接受1–5次 HTTP（含安装令牌），`SYNC_UNIT_SECONDS` 仅接受15–120秒、默认120秒，单次 HTTP10秒；10–14秒不能提供准备余量，启动时拒绝。整轮10000请求/24小时是可配置默认值，并非新的硬上限，沿用现有有限校验范围；修改预算应先在隔离项目验证。

处理限流、源权限不足、预算恢复及覆盖缺口的具体步骤见 [2A 运维](data-foundation-acceptance.md#同步恢复与证据判读)。部署升级检查应增加一次真实事实读取及健康覆盖检查；存活、容器 Running 或同步 completed 都不能证明数据完整。

## 2B Workspace 运行时（配置已准备，容器验收待执行）

后端 build/runtime 同用固定 Python3.12.13 slim-trixie，以使用满足最低2.43的发行版 Git；不混用两个发行版的虚拟环境。运行镜像安装 Git 和 CA，构建步骤拒绝过低版本。2026-10-22只读核对的 [Docker Hub 官方 Python 页面](https://hub.docker.com/layers/library/python/3.12.13-slim-trixie/images/sha256-03980e32f4d1c7477a7dcc75a4686361cb24696291cab9df50fcd87ab2e6c201) 显示 index digest `sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b`；[Debian trixie Git](https://packages.debian.org/trixie/git) 为2.47.3系列。[Bookworm backports 更新期](https://backports.debian.org/Instructions/)截至2026-08-09，因此不新增该源。元数据核对不证明镜像构建、系统库兼容或 Linux 运行通过；本轮未拉取/构建镜像。

Compose 的 `workspace-data` 是本项目专用卷，挂入 `/var/lib/devflow/workspace`：API `:ro`，Worker RW，migrate 不挂缓存。镜像先以10001:10001创建0750根目录，空命名卷首次复制镜像目录元数据；现有卷必须另行核对所有者，不能给运行容器增加 root/特权来绕过权限。dev 覆盖继承同一卷和权限，不另建缓存。API 不创建 lock 文件，首次准备必须由 Worker 完成。

环境恢复后，在新且经标签核对为空的项目中完成下列操作并记录命令/退出码；不要复用旧Task8卷：

1. 用绝对合成 `--env-file`、唯一 `-p`、production与dev两个文件运行 `config --quiet`、`build api frontend gateway`、`up -d --wait`。这些命令当前 **NOT EXECUTED**。记录每个实际 image ID、Git版本和CA文件可读性。
2. 比较源 `uv run --no-sync alembic heads`、镜像 head 和该项目PG的 `alembic_version`，必须都是当前唯一head（Workspace从0009开始），不能沿用旧0007证据。验证migrate退出0、ready200与普通Worker重启；ready不证明有用任务完成。
3. inspect API/Worker Mounts，确认同一 `workspace-data` source且 API RW=false、Worker RW=true。通过 Worker UID10001在已登记自有根写一个唯一探针，再从API读取；API以相同UID尝试新写入必须失败且无文件，不能仅凭 YAML 声明判定只读。仅删除已登记的Worker探针。
4. 准备真实固定版本后重启API/Worker，验证旧SHA、pin、tree/file和PG记录。进程强杀另记原始容器/namespace/creation身份及nonce，确认全树退出/physical drain或按[离线恢复](workspace-storage.md#exceptional-physical-guard-recovery)保守保留；普通重启不是强杀证据。
5. 按[备份恢复](backup-restore.md#workspace-对象与固定版本恢复)在全新库/卷验证匹配对象及缺对象路径；最终仅stop本项目并保留卷/证据，不down-v、不全局prune。

`WORKSPACE_REPOSITORY_BYTES`/`WORKSPACE_TOTAL_BYTES` 默认2GiB/20GiB，只是软监控。命名卷没有自动硬配额。正式部署需专用受限文件系统/受限卷，记录实际mount、配额及临时pack/repack余量，验证超额写入失败且没有发布ready版本。没有实际执行则硬配额、近1GB、真实私有/fork、TLS和Linux/fetch均保留NOT EXECUTED。

Windows本地联调使用 `.env.example` 中两个绝对路径变量、既有UV环境和Selector loop；API与Worker使用同一路径，不能指向用户checkout。`test_workspace_flow.py`仅用专用PG55439和测试生成对象/秘密，无需真实App。Linux完整流程必须在有权限的实际Linux环境另行执行，不能通过换启动通道绕过当前Docker/Playwright限制。

## 3B 索引与模型运行时资源（配置已实现，真实部署待执行）

生产 Compose 固定 `etcd`、`minio`、`milvus`（Milvus v2.6.26 standalone）和独立 `model-worker`。API、普通 Worker 与 migrate 镜像不安装 torch；只有 `model-runtime` 构建目标（镜像 `devflow-backend-models:${DEVFLOW_IMAGE_TAG}`）安装 `models` extra，3A tokenizer 依赖仍保留在基础后端镜像。`model-worker` 固定 `WORKER_ROLE=model`、`WORKER_CONCURRENCY=1`，使用只读模型目录和共享 `model-owner` 卷；不要为该服务增加副本，OS 锁只允许一个 owner 加载模型。

主机最低建议：Milvus standalone **8 GiB** 内存，**16 GiB** 更稳妥；同时为模型 worker 的 CPU 或 GPU 预留内存/显存。BGE-M3 与 reranker 的实际峰值取决于设备、dtype、批大小与并存服务，必须用真实模型和明确选择的配置测量；不得从 CPU 结果推断 CUDA，也不得把分时共享实验称为同机全栈通过。模型目录由 `DEVFLOW_MODEL_DIR` 映射为只读 `/models`，容器内路径用 `MODEL_EMBEDDING_PATH`/`MODEL_RERANKER_PATH`/`KNOWLEDGE_TOKENIZER_PATH` 配置；UID 10001 必须可读。`MODEL_DEVICE`/`MODEL_DTYPE` 必须显式提供，空值或缺失会让 model worker 在启动时拒绝，不会静默选择 CPU。CUDA 主机的 NVIDIA runtime/设备挂载属于宿主配置，本仓库未提供或验证。锁文件通过显式 `pytorch-cpu` 索引解析 `torch 2.15.0+cpu`（macOS 为普通 `2.15.0`，其余平台标记为 `+cpu`），不再包含 `cuda-toolkit`、`nvidia-*` 或 `triton`；此前记录的约 9.9 GB `model-runtime` 镜像来自旧 CUDA 锁，使用当前锁重新构建后的实际大小与导入边界为 **NOT EXECUTED**，需为镜像与模型文件重新预留并测量磁盘。

数据与备份：`milvus-etcd`（元数据）、`milvus-minio`（对象/WAL/segment）和 `milvus-data`（查询数据）必须与 PostgreSQL 在同一恢复点备份，操作步骤和恢复核对见[备份恢复](backup-restore.md#milvus-向量与-postgresql-的一致性恢复)。只恢复 PostgreSQL 的典型结果是：数据库仍指向 ready 版本但探测发现集合/向量缺失，概览返回 409 `index_not_ready`，原因可能是 `collection_missing` 或 `vectors_missing`；若数据库本身已无可用的 active/target，则概览返回 200 并带 `not_indexed`/`stale` 及原因。两者都必须重建索引或恢复到匹配时间点，不能手工改状态绕过。为 WAL、segment 增长和备份预留磁盘；不要 `down -v`、不要全局 `docker system prune`。

健康与故障判读：`model-worker` 启动有界自检（`MODEL_STARTUP_TIMEOUT_SECONDS`，默认 1800 秒），停止时先排空在途推理（`MODEL_DRAIN_TIMEOUT_SECONDS`，默认 120 秒，Compose 停止宽限 150 秒）。日志状态为 `starting/loading` → `ready`；另一 owner 存活时为 `not_ready/owner_locked`；失败为 `unhealthy` 并带 `model_path_missing`、`model_load_failed`、`out_of_memory`、`startup_timeout`、`self_check_failed` 等原因。索引可用性另见 `not_indexed/building/ready/stale` 及原因：普通未就绪状态由概览以 200 + `index_status`/`index_reason` 暴露；只有数据库声明 ready 而只读存储探测不成立时才返回 409 `index_not_ready`。与加载模型运行指纹的比对属于 model-worker/查询路径，尚未接入真实模型验收。

截至 2026-11-04，`runtime` 与 `model-runtime` 两个后端目标镜像已在 Windows Docker 上实际构建并检查导入边界；Linux 上的镜像运行、模型目录权限、CUDA 推理、峰值 RAM/VRAM/冷热延迟、owner 挂起/终止和 Milvus 卷级恢复均为 **NOT EXECUTED**；真实 Milvus 服务重启与查询证据见[知识验收](knowledge-acceptance.md)。真实 30 题检索质量由 3C 负责。
