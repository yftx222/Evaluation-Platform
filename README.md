# AI 评测中台开发工程

分别在两个终端运行。后端需要 Python 3.12+ 和 uv，前端使用 Node 22.12+ 或 Node 24 和 npm。

**后端**（从 code/backend 运行）
```powershell
uv sync --locked --group test
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --no-access-log
```
打开 http://127.0.0.1:8000/docs 。运行 `uv run ruff check .`、`uv run ruff format --check .`、`uv run --group test mypy`、`uv run --group test pytest` 检查后端。

**前端**（从 code/frontend 运行）
```powershell
npm ci
npm run dev -- --host 127.0.0.1
```
默认地址 http://127.0.0.1:5173 。运行 `npm run typecheck` 和 `npm run build` 检查类型和构建。开发凭证只在本机环境中配置。

**Compose 与迁移**（从 code 运行）：复制 .env.example 为 .env，设置随机、仅由字母数字组成的 POSTGRES_PASSWORD。运行 `docker compose up -d --build --wait`。管理端 http://127.0.0.1:15173 ，API http://127.0.0.1:18000/docs 。migrate 一次性执行 alembic upgrade head，API 不自行迁移。GET /health 检查进程，GET /ready 分别探测 PostgreSQL 和 Redis。停止运行使用 `docker compose down`，数据保存在命名卷中。

**真实集成验收**：从 code 运行 `docker compose -f compose.yaml -f compose.test.yaml up -d --wait postgres redis`，随后运行 `./backend/test-compose.ps1`。测试只在本机回环端口创建随机命名的临时数据库，完成后删除该测试库。API 本地运行可用 EVAL_DATABASE_URL、EVAL_REDIS_URL 和 EVAL_POOL_SIZE 指定依赖。修改后端依赖后从 backend 执行 `uv export --frozen --no-dev --no-emit-project -o requirements.txt` 更新容器安装清单。

**开发登录**：只在本地运行 `backend/.venv/Scripts/python backend/dev_identity.py`。它会在工程根 .env 中登记 local-admin 的校验摘要，并将一次生成的访问令牌保存在 .local/development-token.txt。把该令牌粘贴到管理端登录框。脚本拒绝覆盖已有身份。修改 .env 后执行 `docker compose up -d --build --wait` 重载配置。本机单独启动 API 时可在 backend/.env 中设置相同的 EVAL_IDENTITIES。前端使用 Vite 代理调用本机 8000 端口；Compose 使用 Nginx 同源代理。

**登记被测服务**：运维先在 code/.env 的 EVAL_TARGETS JSON 对象中按项目 slug 批准 host、scheme、port、prefix 和 networks。示例（文档保留网段，请替换为已批准的服务）：

```dotenv
EVAL_TARGETS='{"shop":[{"scheme":"https","host":"shop.example.org","port":443,"prefix":"/evaluation","networks":["192.0.2.80/32"]}]}'
```

重载 API 后，在页面创建对应 slug，填写业务地址与只读探测地址，配置请求映射、响应字段和固定的部署版本，再发布并测试连接。Docker 中 localhost 指容器本身。测试环境专用的回环规则只在临时集成测试中注入。出站凭证由 SecretResolver 按项目、引用和版本查找；本地开发可用 EVAL_SECRETS（结构为项目 → 引用 → 版本 → 值）。正式托管和组织 OIDC 需要单独提供实现；生产模式缺少注入时拒绝启动。项目 API Key 采用 Authorization: ApiKey 前缀.秘密；管理操作仅接受组织 Bearer 身份。

**阶段验收**：先构建 frontend/dist 并安装本机 Chrome，再运行 backend/test-compose.ps1。测试使用真实 HTTP、PostgreSQL、Redis 和无头浏览器。版本 0002_probe_permits 在六张基础表之外只增加实时探测用的 call_leases；批量 Worker 尚未开发。CI 工作流按 code 为仓库根放置，当前目录未初始化 Git，云端 CI 需在接入仓库后运行。
