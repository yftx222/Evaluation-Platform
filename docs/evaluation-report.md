# 评测报告与运行对比

在「批量评测」选择已结束的运行，点击「查看评测报告」。先看成功率与覆盖率，再看类别、难度或标签中的差异。「问题样本」允许按低分、超时、评分错误、组合子节点失败、轨迹缺失及项目阈值筛选。点击样本可以打开该次执行的评分、Trace 时间线和 Evidence。

## 指标如何计算

|指标|分子|分母|
|---|---|---|
|样本成功率|SUCCEEDED 样本数|本 Run 全部样本数|
|评分执行成功率|OK|OK + ERROR|
|评分覆盖率|OK|样本数 × 冻结计划中不同评分器版本数|
|评分通过率|OK 且通过|OK|

直接调用 Phase 3 的计算函数。SKIPPED 独立展示，缺少评分记录也会降低覆盖率。每个评分器版本的平均分只统计 OK；按标签分组时，同一条样本可以属于多个组，不能把各标签的样本数相加作为总数。分母为零时返回 null，前端显示不可获取。

Run 耗时从开始到结束。Case 耗时从第一次开始执行到终结，包括等待及重试；尚未开始就取消的样本没有耗时。延迟和单样本 Token 均显示有效观测数、P50 和 P95。Adapter / Judge 耗时取有发送记录的 Attempt 延迟，模型、工具、检索耗时取上报事件。重复执行同一逻辑步骤统计重试，配额等待另列。交付未知计数取 Attempt 的 delivery_state。

每个样本选择正式预测所属 Trace；没有正式预测时选择这条样本最新的 Trace。展示 COMPLETE / PARTIAL / MISSING / 未采集的原始数量。summary.trace_mode 为冻结的 disabled、callback 或 embedded。完整率分子为 COMPLETE，分母为进入采集窗口的样本数，即三个封口状态数量之和。关闭采集、执行前取消或无历史采集记录时显示 NOT_COLLECTED，排除在分母之外。零分母返回 0/0 与 null。列表和导出采用同一条样本状态。没有声明支持的事件能力按不适用展示；声明支持但没有耗时观测时返回不可获取。

## Token 和估算成本

优先读取正式 Prediction 的输入与输出 Token。二者均缺失时才回看选定的 COMPLETE Trace，按其 MODEL_CALL 求和，每一侧均须所有模型调用上报该侧数值。Judge 按不同评分版本的正式成功结果读取供应商用量；优先取 total_tokens，也接受完整的 prompt_tokens + completion_tokens。没有 Judge 的评分计划在此项消耗为零。报告分别展示已知部分和覆盖样本数，只有所有样本两侧及计划 Judge 用量齐备时才给出运行总 Token。

部署方可以通过 `EVAL_REPORT_POLICIES` 配置一个以 ProjectVersion UUID 为键的 JSON 对象。每项可设置 max_case_tokens、max_case_ms、currency、target 和 judges。target 价格项包含 provider、model、revision、input_per_million、output_per_million；judges 按 EvaluatorVersion UUID 指定同结构价格。计价单位为每百万 Token 的指定货币金额。该配置只由服务端读取，创建 Task 时随快照冻结。部署配置后续变更不会重新给既有 Task 定价。未配置时不假设任何供应商报价或告警阈值。

本地配置示例中两个价格都是演示值。将外层 UUID 换成目标项目版本编号，并从部署方核实价格、币种和模型修订后再用于真实报告。

```json
{
  "00000000-0000-4000-8000-000000000001": {
    "max_case_tokens": 4000,
    "max_case_ms": 10000,
    "currency": "USD",
    "target": {
      "provider": "example-provider",
      "model": "example-model",
      "revision": "example-revision",
      "input_per_million": 1,
      "output_per_million": 2
    }
  }
}
```

估算限于正式结果，须能匹配实际模型和 revision，且输入、输出都由该正式结果提供。任一计划产品缺失或未匹配就不展示 Run 总估价。失败及重试产生的供应商账单无法通过该估价还原。Trace 的 Token 回退只用于用量统计。

## 运行对比

一次可选择 2 至 5 个不同的已终结 Run，第一个作为基线。比较项包括项目版本与部署、Adapter build 和指纹、数据集版本与内容摘要、评分依赖 DAG、Judge 模型/修订、Prompt/Rubric、执行策略、Trace 能力以及实际观测模型。配置相同但无法核实所用模型修订时，也只展示两边原始指标。

允许直接比较时，按冻结样本 ID 配对。新增失败表示基线执行成功、计划评分均完成且通过，当前出现执行或评分失败；已修复方向相反。退化按同一样本的同一评分器版本逐项比较，只要有一个有效分数下降就会命中。失败与退化可以同时出现。运行汇总、分类均分和通过率可显示差值，分母缺失时差值也为空。结论是观测到的变化，无法单凭差值解释原因。

## 接口与导出

以下路径前缀为 /api/v1/reports。JSON 查询沿用 data、request_id 信封。项目成员需要读取权限。本项目 results:read API Key 可读单个报告、分组和样本；对比、使用基线筛选和导出需要组织用户身份。未终结返回 409 RUN_NOT_FINISHED，越权按既有策略返回 401/403/404。

|GET 路径|输入|返回|
|---|---|---|
|/runs/{run_id}|路径 UUID|summary、process、usage、conditions、limits|
|/runs/{run_id}/metrics|dimension=category/difficulty/tag，evaluator_version_id 可选，offset、limit|分组样本数、均分、四项比率、next_offset|
|/compare|重复传 run_id，首项为基线；dimension、offset、limit|各运行报告、分组原始值、条件差异、允许时的差值与配对统计|
|/runs/{run_id}/bad-cases|signal、threshold（0–1，默认 0.5）、baseline_id、offset、limit|样本、状态、线索、最低分、耗时、Token、Trace ID、证据链接和下一页|
|/runs/{run_id}/export|format=csv/json，可带同样的筛选条件|附件；不指定 signal 时导出全部样本|

signal 包括 any、all、case_failed、timed_out、score_error、low_score、child_failed、trace_partial、trace_missing、token_limit、latency_limit、new_failure、fixed、regressed。最后三项要求可比较的基线，缺基线返回 422 BASELINE_REQUIRED，条件不同返回 409 INCOMPARABLE_RUNS。能力声明差异作为 trace_capabilities 返回，禁止直接计算完整率差值。all 包含所有样本，any 包含失败样本或至少一个 fixed 以外的原因。分页每页最多 100 条。导出的 JSON 包含汇总报告和样本数据，CSV 用 record_type 区分汇总指标行和样本行。导出只选取公开指标及引用，沿用已有 CSV 公式注入防护；不包含提示词、凭证、原始预测或事件输入输出。

聚合与分位数在 PostgreSQL 中计算。API 每次只取当前分组或样本页，模型汇总最多显示 100 组并报告是否截断。导出使用数据库游标，每批 128 行，输出超过 1 MiB 后转临时文件。无需新增表或迁移。

### 运行监控

结构化 http_request 日志包含路由模板、状态码和 elapsed_ms。按报告或时间线模板可统计含鉴权与数据库查询在内的服务响应延迟，按 TraceWrite 模板与 401 可统计凭证拒绝；已解析的批次还记录 accepted、duplicate、rejected。请求大小或身份导致无法解析批次时，拒绝事件数未知。完整、部分和缺失轨迹计数来自报告的真实数据库聚合。日志不包含实际 URL、请求体、凭证或样本内容。
