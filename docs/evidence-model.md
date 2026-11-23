# Phase 4.3 评分证据

在「运行详情 → 样本详情」打开某条评分的「查看评分证据」。例如，非空规则给了 1 分，展开后可以看到这次预测的 `/answer` 和脱敏摘要。若成功的结束事件明确上报它产生了这个字段，还可以点击「定位轨迹事件」查看事件本身。这个事件说明答案从哪里来，评分器直接读取的内容仍是正式预测。

## 保存与查找

沿用 Phase 2 的 `EvaluationResult.evidence: list[str]` 和不可变的 `evaluation_scores.evidence`。Worker 在写正式 Score 的同一个租约事务内解析引用。引用的固定顺序决定 `evidence_id = <score UUID>.<从 0 起的序号>`。已有 Score 直接按原数组解析，无需回填。阶段 2 的试评分没有正式执行编号，保持原有试评返回格式。

|评分器提交的引用|绑定来源和范围|
|---|---|
|`prediction:/answer`|CaseExecution 上唯一正式预测和写入它的 Adapter Attempt。|
|`case_input:/query`、`expected:/answer`|取本次 Run 固定的 DatasetVersion 和对应样本，返回 case_id 和 dataset_version_id。|
|`trace_event:<event UUID>#/output`|事件必须属于同一项目、同一 CaseExecution、正式预测对应 Attempt 的已封口 Trace，指针基于事件详情 JSON。|
|`external:policy/A`|当前支持 policy 这一种登记来源，A 必须在 Run 快照的 approved_policy_ids 中。只证明登记的标识，不下载外部文档。|

JSON Pointer 支持根、对象字段、数组下标和 `~0` / `~1` 转义；字段缺失与 JSON null 分开处理。摘要上限 240 个字符。敏感路径、邮箱、凭证或无法安全摘要的内容会被隐藏。无效指针、虚构事件、其他样本的事件及未登记的来源均不能形成可访问链接。校验失败只保存类型和原因，原评分的分数、状态和理由保留。

平台可以附加 `output_event:<UUID>#/output/output_ref`。它检查该预测 Attempt 的封口轨迹，只选择 `SUCCEEDED` 的 `FINAL_OUTPUT`，其 `output.output_ref` 必须与评分引用的预测指针一致。兼容 `prediction.answer` 和 `prediction:/answer` 两种声明，相同声明出现多次时不据此定位。平台生成的引用带 `metadata.relation=prediction_provenance`，评分器不能自行声明这种关系。缺少对应事件时，预测字段本身依然可读。

Rule、Code、Business、LLM Judge、Composite 的正式评分走同一个写入口。Composite 返回每个子评分的 score_id、evaluator_version_id、position 和原始 weight。子评分可继续向下查看，共用同一叶子的父评分保留各自的权重。

## 查询接口

以下 GET 均返回 `{data,request_id}`。读取资格与源项目的 Trace 完全一致，组织用户需要项目 R 或更高权限，项目 API Key 需要本项目的 `results:read`。TraceWrite 凭证用于写入，不能作为查询身份。

|路径（前缀 /api/v1）|data|
|---|---|
|/evidence/{evidence_id}|单条证据，包含评分与版本编号、来源类型和标识、Attempt/Trace/Event、指针、摘要、状态、原因、metadata 和创建时间。|
|/evaluations/scores/{score_id}/evidence|本条评分的 items 数组和 children 子评分列表。|
|/case-executions/{execution_id}/evidence|scores 数组，按评分分组。|

状态为 `AVAILABLE`、`REDACTED` 或 `EVIDENCE_UNAVAILABLE`。已知引用暂不可读时 HTTP 200 携带原因，如 SOURCE_ARCHIVED、SOURCE_MISSING 或 INVALID_POINTER。单条证据编号不存在时 HTTP 404。未登录及过期身份返回 401，项目或范围无权时按既有鉴权返回 403 或 404。

## 归档与恢复边界

迁移 `0010_trace_archives` 新增 `trace_archives(trace_id PK/FK, archived_at)`。部署方完成归档核验后可登记已封口的 Trace。在线事件接口对有权限的调用者返回 410；Evidence 保留原 score_id、trace_id 和 event_id，隐藏摘要并标记 SOURCE_ARCHIVED。达到保留天数只代表可归档，不能自动当成已经归档。标记表用于在线访问控制；复制到冷介质、校验备份及恢复演练由归档流程负责。本阶段没有定时搬迁和清理任务。

数据库沿用评分、预测、已发布样本和 TraceEvent 的不可变约束。新评分器版本、新数据集版本和当前草稿都不会改变已有 Evidence 的定位。直接引用复用 Score 的创建时间，摘要在授权读取时计算。
