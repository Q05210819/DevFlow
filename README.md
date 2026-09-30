# DevFlow

第一阶段平台基础正在实现中。目前可启动 FastAPI 进程并访问存活接口；数据库和 GitHub App 功能将在后续任务接入。

## 本地启动

安装 [UV](https://docs.astral.sh/uv/) 并确保可用 Python 3.12.13。在仓库根目录执行：

```powershell
Copy-Item .env.example .env
uv --directory backend sync --locked
uv --directory backend run --env-file ../.env uvicorn devflow.app:create_app --factory --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/health/live`，应返回 `{"status":"ok"}`。该接口不连接数据库或 GitHub。每个响应带独立的 `X-Request-ID`，错误响应用同一个 ID。其余 API 将统一位于 `/api/v1`。

运行测试和静态检查：

```powershell
uv --directory backend run pytest -q
uv --directory backend run ruff check .
uv --directory backend run ruff format --check .
```

本地没有 GitHub App 时保留 `.env.example` 中的 GitHub 配置注释即可。后续接入时，先创建 GitHub App 并获得 App ID、Client ID、允许的安装 ID 与引导管理员 GitHub ID，再将五项秘密写入独立文件，设置对应的 `*_FILE` 路径。`GITHUB_ALLOWED_INSTALLATION_IDS` 是 JSON 整数数组，例如 `[123456]`。生产环境必须使用 HTTPS 的 `PUBLIC_BASE_URL`，配置数据库、上述身份值及非空秘密文件；开发和测试环境的 HTTP URL 只允许 loopback 主机。

敏感配置应放在未跟踪的 `.env` 或部署秘密文件中。日志只记录固定字段，不记录请求查询、Cookie、Authorization 或设置对象。UV 锁文件、解释器版本文件和 `.env.example` 已纳入版本管理。
