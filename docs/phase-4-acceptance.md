# Phase 4 审查修复验收

基线为 `402e207dbb989f2e656e33b054b2e868a00ea679`。该提交的 GitHub Actions 显示，同一浏览器用例先从 Bad Case 路由收到 200，后收到 500。日志保留了路由和 HTTP 状态，未记录该次请求的底层异常。[基线 CI](https://github.com/yftx222/Evaluation-Platform/actions/runs/45185931571)

## 修复项与验证方法

|问题|处理与回归依据|
|---|---|
|P0 · Bad Case|基线的 all、any SQL 分支已不引用 `:signal`。新实现统一使用带类型的 signal 参数和固定 WHERE，比较标志仍按基线和同版本评分器计算。逐个检查全部 14 个选项，有无基线各一次，按每页一条读取，对照 JSON 和 CSV 中的样本 ID、顺序及错误码。|
|P0 · 样本编号|基线序列化会先把 UUID 变为字符串，再对整份响应执行文本脱敏。用尾段为 a13800138000 的合法样本 UUID 实测，旧实现会用 [REDACTED] 替换其中数字，无法通过 UUID 响应校验。修复保留内部编号和平台生成的证据链接，对样本名称等自由文本照常脱敏。回归通过数据库插入这样的样本，调用真实分页和证据 API。|
|P1 · SDK|每个请求一个发送任务。上下文退出在原截止时间内等待队列和封口回执，`finish()` 可以立即通知，等待者彼此独立。业务异常和取消继续传播。超时或已知丢失标记为本地 PARTIAL。客户端用 confirmed_status 区分服务端实际确认的状态。测试用事件屏障控制最后一批、封口回执和取消时点，验证重试键、单次封口、凭证清除及业务响应的先后顺序。[接入契约](trace-sdk-guide.md)|
|P1 · 采集覆盖|0011 按 Run 冻结的能力为新 Adapter Attempt 分配轨迹。disabled 不分配，未进入窗口的样本也归入 NOT_COLLECTED。报告的完整率只使用进入采集窗口的样本。列表、筛选、导出使用相同口径。能力不同禁止计算对比差值。浏览器用真实 API 核对关闭、回调及内嵌三种展示。|
|P2 · 交付文档|README 提供七份开发与接入文档和本次验收记录的入口。|

本轮未新增 REST 路径。汇总返回 `summary.trace_mode`，样本的 `trace_status` 可返回 NOT_COLLECTED，完整率继续返回 numerator、denominator、value。零分母时 value 为 null。回调和内嵌的 Trace 持久化状态仍为 OPEN、COMPLETE、PARTIAL、MISSING。[报告接口](evaluation-report.md)

数据库迁移 `0011_trace_allocation` 升降级均只替换现有分配函数。空库测试覆盖升、降、再升。带数据测试使用前一版本的函数生成真实历史空轨迹，升级后核对原 Trace 仍可按 ID 读取、历史报告与评分可读、关闭采集的样本从缺失统计中排除。Worker 启动时校验最低版本 0011。

## 变更文件

|范围|仓库路径|
|---|---|
|后端|backend/app/api/reports.py、backend/app/application/report/{analysis,metrics}.py、backend/app/application/runner/runs.py、backend/app/contracts/reports.py、backend/app/worker/scheduler.py|
|数据库与 SDK|backend/migrations/versions/0011_trace_allocation.py、backend/trace_sdk/{client.py,pyproject.toml}|
|前端|frontend/src/features/evaluation/RunReport.tsx|
|测试|backend/tests/{test_reports,test_infrastructure,test_batch_console,test_trace_timeline,test_trace_write,test_trace_sdk_lifecycle,phase42_shop_service}.py|
|文档|README.md、docs 下的 trace-data-model、trace-write-api、trace-event-schema、trace-sdk-guide、evidence-model、evaluation-report、trace-security-and-retention、phase-4-acceptance 共八份 Markdown|

## 三档压力测试

使用真实 PostgreSQL、Redis、独立 API 和 asyncio Worker。每条合成样本通过本地 HTTP Agent 回调 5 个事件，包括检索、工具和模型调用。每档读 20 次时间线和 20 次报告。整项 225.104 秒，通过。每对延迟数据为 P50 / P95，单位毫秒。

|样本 / 事件|端到端秒|事件/秒|批量上报|SQL 写入|时间线分页|报告聚合|
|---|---:|---:|---:|---:|---:|---:|
|100 / 500|12.881|38.82|16.885 / 26.371|0.818 / 1.571|6.525 / 8.765|35.752 / 47.629|
|500 / 2,500|64.449|38.79|17.216 / 25.124|0.808 / 1.521|6.689 / 8.807|23.513 / 28.829|
|1,000 / 5,000|129.623|38.57|17.363 / 27.179|0.812 / 1.277|6.248 / 7.135|33.317 / 38.966|

|样本|回调并发峰值|重复事件 / 拒绝冲突批数|数据库增长（字节）|API CPU 秒 / 采样内存峰值 MiB|Worker CPU 秒 / 采样内存峰值 MiB|
|---|---:|---:|---:|---:|---:|
|100|2|20 / 4|2,695,168|1.72 / 115.98|5.92 / 95.50|
|500|2|100 / 20|8,724,480|7.75 / 117.63|27.19 / 95.91|
|1,000|2|200 / 40|8,101,888|15.16 / 119.27|55.55 / 96.31|

每档按设计注入冲突，分别占 108、540、1,080 次批请求的 3.70%。吞吐量包含整个 Run 的执行时间。库增长受 PostgreSQL 页复用影响，内存为进程采样值。

## 真实接入和归档

电商客服项目使用自己的模型配置处理一条合成问候，独立 Worker 收到正式答案并完成评分。结果为 COMPLETE、4 个事件、1 次真实模型调用、2 条已验证证据。将 110,870 字节的 pg_dump 恢复到新库，8 个读取结果一致，跨项目访问被拒，归档后的证据按 SOURCE_ARCHIVED 返回，解除归档后恢复可读。该专项耗时 11.102 秒。真实订单、线上检索、其他生产 Agent、公网流量和异地恢复时间未在本轮测量。

## 最终验收结果

|检查|本轮实际结果|
|---|---|
|全量 Pytest|119 项通过，0 失败、0 错误、0 跳过，245.76 秒。包含 3 项 Playwright。|
|单元与 SDK|另行执行 JSON Pointer 与并发 SDK 生命周期共 2 项，通过。与全量计数重叠。|
|API 与 PostgreSQL|真实库中核对批量上报、重放、乱序、冲突、父链、失效凭证和租约、封口竞争、证据权限、Bad Case 筛选、导出及带历史数据迁移。全量中的这些用例通过。|
|前后端|Ruff、Ruff Format（107 个文件）、Mypy（93 个源文件）、TypeScript 和 Vite 生产构建通过。浏览器核对三种模式的计数与完整率分母，并完成报告下钻、导出和 SSE 重连。|
|Docker|PostgreSQL、Redis、API、前端及独立 Worker 均正常启动。API /ready 返回两个依赖 ok，前端 HTTP 200，迁移任务退出 0，数据库版本为 0011_trace_allocation。真实 Agent 和压测分别验证 API 与独立 Worker 的协同。|
|性能与恢复|三档压测 1 项通过。真实电商客服与 pg_dump/pg_restore 合计 1 项通过。独立专项未计入上面的 119 项。|

本地满足本阶段验收条件。首次加回归时，试图改写已存在的样本 UUID 被数据库的不可变约束正确阻止，内嵌测试也曾在 Run 终结前请求报告；最终测试改为在插入时生成目标 UUID，并等待正常的 Worker 收敛流程。保留旧约束和断言。Starlette TestClient 有一条依赖弃用提示。修复分支接受独立的 Ubuntu GitHub Actions 验证，远端结果以对应提交的 verify 为准。[修复分支 CI](https://github.com/yftx222/Evaluation-Platform/actions/workflows/check.yml?query=branch%3Afix%2Fphase4-review)

## 复现命令

以下命令从仓库根运行。依赖按 README 安装，本机使用独立 Compose 项目 `eval-phase4-check`，API、前端另取回环端口和独立镜像名。测试入口通过 Compose 取得实际数据库和 Redis 地址，并为每项集成测试创建临时库。正式凭证保存在本机。

```powershell
Push-Location backend
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
.venv/Scripts/mypy
.venv/Scripts/python -m alembic heads
.venv/Scripts/python -m pytest -q tests/test_trace_sdk_lifecycle.py tests/test_evidence.py::test_json_pointer
Pop-Location
Push-Location frontend
npm.cmd run typecheck
npm.cmd run build
Pop-Location
$env:COMPOSE_PROJECT_NAME = 'eval-phase4-check'
docker compose -f compose.yaml -f compose.test.yaml --profile scheduler up -d --build --wait
$env:PYTEST_ADDOPTS = '--tb=short'
./backend/test-compose.ps1
$env:PYTEST_ADDOPTS = 'tests/phase44_report_load.py --tb=short'
./backend/test-compose.ps1
$env:EVAL_SHOP_CODE = (Resolve-Path '../../15_pj_电商客服/code').Path
$env:EVAL_ARCHIVE_DRILL = '1'
$env:PYTEST_ADDOPTS = 'tests/phase42_real_agent.py --tb=short'
./backend/test-compose.ps1
```

原始 JUnit 在项目工作区的 `文档/阶段验收`。运行时给 PYTEST_ADDOPTS 增加 `--junitxml=../../文档/阶段验收/文件名.xml` 即可复核原始计数和测量值。phase44_archive.py 由真实客服用例在归档演练开关开启时调用。
