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

**阶段验收**：先构建 frontend/dist 并安装本机 Chrome，在 code 运行 `docker compose -f compose.yaml -f compose.test.yaml up -d --wait postgres redis`，再运行 `./backend/test-compose.ps1`。测试使用真实 HTTP、PostgreSQL、Redis 和无头浏览器。版本 0002_probe_permits 增加实时调用使用的 call_leases。CI 工作流位于 code 仓库的 .github/workflows 目录。

## 数据集管理（Phase 2.1）

登录管理端后打开「数据集管理」。组织成员可查看和导出，具有 Cat 角色的成员可创建目录、编辑草稿、导入及发布。新建目录自带 v1 草稿。发布时先核对样本数、相对基线的增删改数量和摘要，再点「确认发布」。如果别人已经编辑过草稿，服务器返回 409，请重新审核。后续修改从已发布版本复制新草稿；旧版本仍可查看。归档期间目录只读。

**样本示例**（JSON 文件使用数组；JSONL 每行一个对象）：

```json
[{"case_key":"warranty_001","input":{"query":"保修多久？"},"expected":{"answer":"一年","criteria":["明确期限"]},"category":"售后","difficulty":"easy","tags":["保修"],"split":"test","extensions":{}}]
```

input 和 expected 都是对象；附件使用 HTTPS 引用或 asset:标识，导入器不抓取附件。case_key 在版本内唯一。私有字段放 extensions。criteria 是本条样本的判定要求。可选划分 train、validation、test，缺省 test。

**文件往返**：导入页面支持 JSON、JSONL、CSV 及单工作表 XLSX。CSV/XLSX 首行为表头，列映射填写源列名到标准字段的 JSON 对象，例如 `{"问题内容":"input"}`；嵌套列需填 JSON 文本，例如 `{"query":"保修多久？"}`。导出表格将各列编码为 JSON 文本以保留 null、空字符串和嵌套值。工作簿不接受公式、宏。单次导入最多 4 MiB、5000 条、表格 32 列；嵌套深度不超过 12 层，单个嵌套字符串最多 16000 个字符。预览显示前五条，不落库；确认时重新校验，冲突或有错整批回滚。

**复用与摘要**：组织内多个项目成员可以读取相同的 dataset_version_id。摘要为 SHA-256，对八个内容字段（case_key、input、expected、category、difficulty、tags、split、extensions）按 case_key 排序后序列化为紧凑 UTF-8 JSON 数组，再排序对象键；数组内部保持顺序。导出分批读取并通过临时文件发送，会遮盖常见凭证键和 Bearer 文本；命中脱敏时重新导入的内容摘要会改变。对自由文本中的个人信息，维护者仍需在共享前复核。

**API 与迁移**：在 `/docs` 的 datasets 分组查看 12 项操作。创建草稿传 action=draft、可选 base_version_id；发布传 action=publish、draft_id 和 reviewed_digest。导入使用 multipart 的 dataset_id、draft_id、format、file 和可选 mapping、preview；导出要求 version_id 与 format。0003_dataset_versions 在 0002 后创建目录、版本和样本三张表。测试使用临时库，从空库升级、回退再升级，包含真实 Chrome 操作。复测仍使用上文的 Compose 集成验收命令。

## 评分器管理（Phase 2.2）

登录后进入「评分器管理」。先选类型、填写目录名称和标识，再选后端列出的可信实现，按表单示例填写配置，发布 v1。展开「配置字段和输入要求」可以查看该实现的具体结构。组织成员可查看，Cat 可发布与试评。选择已发布版本，填写 Case 和 Prediction，点击「试评一次」。版本区可以选择另一版，比较提示词、模型、量表、规则与子版本，或用同一份输入分别评分。

五类评分器分别是可配置的规则、隔离进程内运行的可信函数、调用兼容 HTTP 服务的 LLM 裁判、核对策略编号的业务示例，以及按权重合成多个子版本的组合评分器。单样本操作只写审计动作，结果在当前页面展示；刷新前请保存所需的校准结果。停用目录后仍可查看和试评旧版本。

### 注册实现

在 backend/app/plugins/evaluators.py 增加随部署分发的工厂，使用 register 声明种类、实现键、Pydantic 配置模型、按输入路径声明的要求和编辑示例。实现异步 evaluate(case, prediction, context)，返回 context.result 生成的 EvaluationResult。规则和业务插件只使用传入材料，Judge 通过受限客户端评分。Code 新函数加入 scoring_functions.py 的可信分派及配置注册，保持纯函数，不能从用户数据导入或运行代码。新增源码依赖时把它纳入 BUILD 的摘要清单。修改构建前保留可重放的旧部署；单个部署缺少历史构建时返回 VERSION_UNAVAILABLE。

规则可选 nonempty、exists、exact、normalized、includes、excludes、schema、at_least、contains_all。JSON Schema 的本地子集支持 type、required、properties、additionalProperties（布尔）、items、enum。其他关键词在发布时被拒绝。Business 示例读取 case.extensions.policy_id、prediction.structured_output.policy_id，以及 materials.approved_policy_ids 字符串数组。材料缺失返回 SKIPPED。Composite 按 position 有序，每个版本在一次试评中最多执行一次。

### Judge 开发配置

运维在受控部署环境 EVAL_JUDGE_ACCOUNTS 中维护稳定账户 ID 到 endpoint、destination、models、max_parallel、rpm 的映射。同一供应商计费账户可绑定多个密钥版本，仍使用同一个账户 ID。密钥引用位于 EVAL_SECRETS 的 judge:<账户标识> → 引用 → 版本。本地 .env 已被 Git 忽略。正式部署通过注入的 SecretResolver 取值。轮换时新增密钥引用版本，发布新的评分器版本。目的主机、路径、地址范围由部署端配置。裁判只能选择该账户允许的模型。

本机 DeepSeek 试评配置使用 deepseek-ecommerce、judge-api/v1 与 deepseek-flash。1 个同时调用、每 60 秒最多 6 次是本地试评配置。额度只统计经过本平台、连接同一 PostgreSQL 的试评调用，电商客服直接访问供应商的流量不参与计数。正式额度待压力测试。响应没有模型 revision 时只可观察差异，无法证明训练权重相同。模型未提供成本时返回 null。

新增迁移 0004_evaluators 在 0003 后执行，创建 evaluators、evaluator_versions、composite_members，创建外键、唯一约束和冻结触发器。已发布版本及其子关系只读；迁移降级会删除这三张表。使用 /docs 的 evaluators 分组查看接口。校准脚本 backend/scripts/calibrate_deepseek.py 从已登录的本机开发环境读取开发令牌，调用同一 API，复用两个校准版本并发起三次真实请求，会消耗模型额度。常规测试使用本地受控供应商，不需要账密。

## 接入不同协议的项目（Phase 2.3）

在项目的「版本」页选择接入协议与构建，填写业务地址、只读探测地址和部署标识，然后发布版本。「测试连接」只向探测地址发送 GET。项目分类与协议可以独立选择，例如 RAG 项目可以使用 HTTP 或 Chat Completions 接口。管理端填写完整 URL；API 也支持把公共前缀放在 base_url，endpoint 与 probe_endpoint 都使用相对路径。

|协议与构建|发送的内容|读取的内容|
|---|---|---|
|http / 1|沿用 request_map、method、answer_path 和 structured_path|若服务有额外数据，可在 options 中填写 usage_path、observed_path、extensions_path，分别指向统一用量、模型信息和项目私有对象|
|openai_compatible / 1|模型取自版本 options.model。有非空消息历史时原样发送 messages；否则把 query 转为一条用户消息。支持 temperature、top_p、max_tokens、max_completion_tokens、seed、stop、response_format|读取首个 choice 的 message.content 及可选 message.parsed。不同供应商可用 options.parsed_path 指定相对 message 的对象路径，revision_path 指定相对响应根的修订号路径。prompt/completion_tokens 映射为 input/output_tokens|
|agent / 1|发送 query、messages、context、attachments 中已提供的字段、parameters 及本项目的 extensions|读取 final_answer、structured_output、usage、observed、extensions 和可选 events。可用 options.answer_field 和 structured_field 改字段路径|

例如向项目发布 API 的 adapter 字段传入以下对象。先在部署的 EVAL_TARGETS 中为项目批准对应主机、路径和网段。认证由 SecretResolver 按项目 slug 和固定版本解析。

```json
{
  "kind": "openai_compatible", "build": "1",
  "base_url": "https://inference.example.org/v1",
  "endpoint": "chat/completions", "probe_endpoint": "models",
  "options": {"model": "service-model-v3"},
  "secret_ref": "inference", "secret_version": "v2"
}
```

服务明确返回的私有 extensions 对象放进统一响应的 extensions[项目标识]。协议层优先把 x-request-id 存为 observed.request_id，Chat 响应体的 id 单独存在 extensions._chat_completion_id。缺失的用量和修订号留空。Agent 的内嵌事件随单次响应保存在 extensions._agent_events；调用 collect_trace(response.model_dump()) 获得按 sequence 排序的事件。各事件需提供 source_event_key、sequence（从 1 开始）、event_type、带时区的 occurred_at。可附 parent_key、duration_ms、input、output、metadata、extensions、input_tokens、output_tokens。事件来源键和序号在同一份响应内分别唯一。事件有误时采集返回 INVALID_TRACE，已取得的预测可单独使用。

### 新增自定义 Adapter

1. 在 backend/app/plugins 中实现受信 Mapping 子类。request_body 将 UnifiedRequest 转为业务请求体，normalize 将服务响应转为 UnifiedResponse。需要特殊内嵌事件时实现 trace。为专属 options 定义 extra=forbid 的 Contract 模型。
2. 创建 Implementation，传入映射类、选项模型、输入要求、能力和配置示例。额外影响行为的 Python 模块放入 dependencies。映射钩子只处理业务数据，公共 HTTPAdapter 负责网络调用，组合后的实例满足 BaseAdapter。
   首次发布时用 implementation.fingerprint() 生成摘要，在源码中固定该值。注册方式是 `registry.register(kind, build, implementation, release_digest="已核对的摘要")`，也可直接用固定摘要作为 build。注册时会核对源代码。请求、响应、错误分类、重试标志或依赖行为发生变化，都要另发 build，并运行合同测试。已发布的编号和摘要永久配对。升级共享传输或运行依赖时，对受影响的插件分别发版，保留旧部署和依赖锁用于历史复现。
3. 通过 GET /api/v1/adapters/implementations 核对实现声明。在 tests/test_adapter_protocols.py 的参数化合同测试中加入该实现和本地 HTTP 响应，运行 backend/test-compose.ps1。echo 是仅随测试加载的完整自定义示例。

普通 HTTP 新发布使用 build 2。项目版本保存实现摘要，每次使用版本时都与部署核对。以前的普通 HTTP build 1 曾对应两套错误处理规则。历史快照含 Phase 2 专有的 base_url 或 options 时，可按已固定的 Phase 2 实现读取。两项都缺少时，平台在发送请求前返回 VERSION_UNAVAILABLE。历史记录仍可查看；继续评测可用相同配置另发一个 build 2 项目版本。需要重放原行为时，应先核实原部署并使用其归档镜像。历史 Chat 和 Agent build 1 按已知的发布摘要读取。

业务调用每次向受限目的地发送一个请求，透传 Idempotency-Key。429 映射为 TARGET_RATE_LIMIT、5xx 为可重试 TARGET_HTTP_ERROR，均不在 Adapter 内重试。真实项目是否按该键去重，仍应由项目方联调验证。普通模型响应可以不含 Trace；Agent 的正式 TraceWrite 回调归入 Phase 4。

Chat Completions 字段参考 [OpenAI API Reference](https://developers.openai.com/api/reference/resources/chat)。各兼容服务实际支持的模型参数以其部署契约为准。

## Phase 3.1 评测方案与运行记录

在 API 的 `/docs` 中打开 evaluations 分组。先准备项目的一个固定部署版本、已发布的数据集版本和一个或多个已发布的顶层评分器版本。以项目 W/A 的组织令牌调用 `POST /api/v1/evaluations/tasks`，提交 name、project_version_id、dataset_version_id 和 evaluator_version_ids 数组。可用 policy 调整本方案的并发、超时和尝试次数。

用返回的 Task ID 调用 `POST /api/v1/evaluations/tasks/{t}/runs`，传空 JSON 对象和 Idempotency-Key 请求头。同键重发可找回原 Run，换键会新建一次运行。通过 `GET /api/v1/evaluations/runs/{r}` 查真实计数，`GET /api/v1/evaluations/runs/{r}/results` 分页查样本。暂停、恢复和取消分别向 Run 下的 pause、resume、cancel 发送 POST。

Alembic 0005_evaluation_runs 接在 0004_evaluators 后，随现有 Compose migrate 服务升级。3.1 创建的 Run 会停在 QUEUED；独立 Worker 在 3.2 接入。专项验收使用原有 backend/test-compose.ps1，在随机临时数据库执行。

## Phase 3.2 独立调度 Worker

从 code 运行 `docker compose --profile scheduler up -d --build --scale worker=2 --wait`。通过 `docker compose --profile scheduler ps worker` 查看两个进程。正常停止使用 `docker compose --profile scheduler stop worker`。本机开发也可在 backend 目录运行 `.venv/Scripts/python -m app.worker.main`。

迁移 0006_worker_scheduler 增加共享 Case 容量与调度索引。Worker 轮询 PostgreSQL，也订阅 Redis 的 evaluations:wake。每进程初始 4 个槽位，全局初始 16 个，项目、Run、Target、Judge 各自使用原有配置。3.2 已能领取、续租和回收真实执行记录；默认停在 CREATED 尝试的阶段交接点，正式业务调用与评分在 3.3 接入。

## Phase 3.3 执行样本与评分

使用上面的 Compose 命令启动 Worker 后，已排队的 Run 会开始调用其冻结的项目版本。项目出站配置、开发密钥和 Judge 账户需要在 Worker 环境中生效。首次接入项目可沿用默认单次尝试，再按实际服务能力设置超时和重试。按 Run 的 results 查询每条预测、来源 Attempt、正式分数和错误原因。Run 详情的 metrics 含四项指标及各自分母。

EVAL_TRACE_TARGET_VERSIONS 和 EVAL_VERIFIED_IDEMPOTENT_VERSIONS 是独立的项目版本 UUID 数组，默认均为空。前者授予受信目标接收短期轨迹凭证的能力，后者登记已经验证的对端去重能力。完整 Trace 上报属于后续阶段。

## 批量评测（Phase 3.4）

从 code 运行 `docker compose --profile scheduler up -d --build --scale worker=2 --wait`。打开 http://localhost:15173，登录后进入「批量评测」。

1. 选项目并新建任务，选择已发布的项目版本、数据集版本和一个或多个评分器版本。预览页面显示评分顺序和权重，确认后配置冻结。
2. 点击「启动运行」。同一任务可运行多次，每次有独立进度和结果。详情中可暂停、恢复或取消，浏览器关闭后 Worker 继续执行。
3. 展开样本查看脱敏输入、实际预测、尝试和评分。按状态筛选或翻页。归档任务后仍可查阅历史。

集成测试使用 `backend/test-compose.ps1`。在隔离的 Compose 测试环境中，设置 `PYTEST_ADDOPTS=tests/phase3_load.py` 可运行分级压测，设置 `EVAL_BENCH_REPORT` 可指定输出 JSON 的位置。停服演练仅限项目 eval-phase34-check，设置 `PYTEST_ADDOPTS=tests/phase3_outage.py`，与常规回归分开执行。
