# 备份与隔离恢复

数据库备份与凭据加密主密钥必须分别保管、同时可恢复。没有原 `credential_encryption_key`，数据库中的 GitHub token 无法解密；重新随机生成同名文件不能修复。本地合成数据恢复演练及证据见 [阶段验收](foundation-acceptance.md)。

## 迁移前备份

记录 Git commit、后端镜像 ID、数据库版本、Alembic revision、时间及备份负责人。对当前数据库创建一致性快照；需要逐表精确比较计数时，先停止本项目 API/Worker/gateway，避免备份前后新增写入：

```powershell
$prod = @('--env-file', '.env', '-p', 'devflow', '-f', 'infra/compose.yaml')
docker compose @prod stop gateway api worker
New-Item -ItemType Directory -Force .cache/backups | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dump = Join-Path (Get-Location) ".cache/backups/devflow-$stamp.dump"
docker compose @prod exec -T postgres pg_dump -U devflow -d devflow --format=custom --file=/tmp/devflow-backup.dump
if ($LASTEXITCODE -ne 0) { throw 'pg_dump failed' }
docker compose @prod cp postgres:/tmp/devflow-backup.dump $dump
if ($LASTEXITCODE -ne 0) { throw 'Backup copy failed' }
Get-FileHash -Algorithm SHA256 -LiteralPath $dump
docker compose @prod exec -T postgres pg_restore --list /tmp/devflow-backup.dump
```

备份先写容器文件，再 `docker compose cp`；不要在 Windows PowerShell 使用 `pg_dump -Fc > backup.dump`，文本重定向可能破坏二进制。备份属于敏感数据；`.cache` 只是临时位置，随后移入受控、加密、离机的备份存储，记录 hash 与保留期。[pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)

独立备份七个秘密文件及无秘密配置清单，尤其 Fernet 主密钥、App 私钥与签名密钥。数据库备份与密钥由不同访问授权保护；不要把秘密和 dump 提交 Git、放入镜像、放在公开报告里。验证文件权限后再恢复业务，或保持停止进行迁移。

## 恢复到新项目的空库

使用新的、此前不存在的 Compose 项目名。先检查 `docker compose ls` 和带该 project label 的容器/卷；发现同名资源就换一个新的名称，不要复用或清空它。生产配置不给 postgres 发布主机端口，因此恢复实例不占用 5432/55439/55440。

```powershell
$restoreName = 'devflow-restore-' + (Get-Date -Format 'yyyyMMddHHmmss')
$restore = @('--env-file', '.env', '-p', $restoreName, '-f', 'infra/compose.yaml')
docker ps -a --filter "label=com.docker.compose.project=$restoreName"
docker volume ls --filter "label=com.docker.compose.project=$restoreName"
# Confirm both listings are empty before proceeding.
docker compose @restore up -d --wait postgres
docker compose @restore cp $dump postgres:/tmp/devflow-backup.dump
docker compose @restore exec -T postgres pg_restore --exit-on-error --no-owner -U devflow -d devflow /tmp/devflow-backup.dump
if ($LASTEXITCODE -ne 0) { throw 'Restore failed; preserve the isolated instance for diagnosis' }
```

这只启动恢复项目的 postgres，不启动 migrate、API、Worker 或 gateway，不请求 GitHub，也不覆盖原数据卷。**不要**使用 `--clean`、删除原库或替换生产 volume。优先使用与备份相同的 PostgreSQL17 补丁镜像及应用 commit；恢复成功后，再单独验证升级迁移。[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

## 验证计数、schema 与解密

在暂停写入的源库和恢复库分别运行以下 SQL，保存输出并逐项比较：

```sql
SELECT version_num FROM alembic_version;
SELECT schemaname, sequencename, last_value FROM pg_sequences
WHERE schemaname = 'public' ORDER BY sequencename;
SELECT 'users' AS table_name, count(*) FROM users
UNION ALL SELECT 'repositories', count(*) FROM repositories
UNION ALL SELECT 'jobs', count(*) FROM jobs
UNION ALL SELECT 'run_events', count(*) FROM run_events
UNION ALL SELECT 'audit_logs', count(*) FROM audit_logs
UNION ALL SELECT 'github_credentials', count(*) FROM github_credentials;
```

例如 `docker compose @restore exec -T postgres psql -U devflow -d devflow -c 'SELECT version_num FROM alembic_version;'`。`pg_restore --list` 只能检查目录，不证明数据或加密凭据可用。

始终备份完整数据库，包含序列及其状态，不能只复制业务表。授权先后判断使用 `auth_change_order_seq`（CACHE 1、NO CYCLE）；恢复后在独立库执行下面检查，要求 `next_order > maximum_stored_order`。`nextval` 消耗一个序号属预期，不要将序列向下重置。当前迁移 head 为 `0004_jobs_events`，应与所恢复应用镜像一致。

```sql
SELECT nextval('auth_change_order_seq') AS next_order,
       greatest(coalesce((SELECT max(auth_changed_order) FROM users), 0),
                coalesce((SELECT max(started_order) FROM oauth_states), 0)) AS maximum_stored_order;
SELECT job_id, max(sequence), count(*) FROM run_events GROUP BY job_id ORDER BY job_id;
SELECT id, status, attempt, last_sequence FROM jobs ORDER BY id;
```

在恢复项目中用**备份对应的原密钥**运行仅解密验证的进程，不启动 Web 服务。下面从 PowerShell 传入脚本，复用 Compose 的镜像、秘密挂载与该项目网络；`--no-deps` 防止启动其他服务。数据库 URL 中 `postgres` 只解析为恢复项目 postgres。

```powershell
@'
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from devflow.security import CredentialCipher
cipher = CredentialCipher.from_file(Path(os.environ['CREDENTIAL_ENCRYPTION_KEY_FILE']))
engine = create_engine(os.environ['DATABASE_URL'], hide_parameters=True)
checked = 0
try:
    with engine.connect() as conn:
        rows = conn.execute(text('SELECT access_token_encrypted, refresh_token_encrypted FROM github_credentials'))
        for row in rows:
            for encrypted in row:
                if encrypted is not None:
                    cipher.decrypt(bytes(encrypted))
                    checked += 1
    print(f'decrypted_fields={checked}')
finally:
    engine.dispose()
'@ | docker compose @restore run --rm -T --no-deps api uv run --no-sync python -
if ($LASTEXITCODE -ne 0) { throw 'Credential verification failed' }
```

脚本只打印验证数量，不打印令牌。`decrypted_fields=0` 表示没有加密样本，不能证明凭据恢复；最终演练需使用合成加密凭据样本或真实受控数据中的非空字段，并比较 schema、作业状态、事件序号及审计计数。不要把恢复库的真实 token 用于 GitHub API 验证，除非另行执行受控真实验收。

## 应用回退和切换

恢复到独立库验证完成前，原数据库保持原样。只有在 schema 与目标应用兼容、计数及解密验证通过、维护窗口和切换目标明确后，才计划切换。数据库时间点回退可能丢失备份后的更改，回退镜像无法恢复被迁移删除的数据。

恢复演练结束先 `docker compose @restore stop` 保留证据；确认不再需要后只清理该确切恢复项目。不要使用全局 prune 或影响其他项目的命令。审计记录中保存操作时间、项目名、命令退出码、计数/hash及限制，不保存秘密正文。
