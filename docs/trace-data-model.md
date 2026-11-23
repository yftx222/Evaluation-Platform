# Phase 4.1 · 一次尝试的一条轨迹

Worker 领取样本并创建 ADAPTER Attempt 时，数据库在同一事务分配独立的 Trace UUID，建立 OPEN 记录。从迁移 0011_trace_allocation 起，仅在 Run 快照采集方式为 callback 或 embedded 时分配。disabled 尝试的 trace_id 为空，样本标记 NOT_COLLECTED。缺少能力声明的旧版本沿用 callback。重试拥有新的 Attempt 和 Trace。EVALUATOR 尝试沿用已有评分记录。迁移前的历史尝试保留原貌。

|表|可查询的核心字段|约束|
|---|---|---|
|traces|id、project_id、run_id、execution_id、attempt_id、source_type、source_name、status、close_reason、started_at、ended_at、last_event_at、event_count、schema_version、created_at、updated_at|attempt_id 唯一。Run 与 Project、Execution 与 Run、Attempt 与 Execution 各有复合外键。Attempt 的 (trace_id,id) 反向引用同一条轨迹。按项目和创建时间、Run 和 Execution 建索引。|
|trace_events|id、trace_id、event_key、sequence、parent_event_key、parent_event_id、event_type、name、status、started_at、ended_at、duration_ms、received_at、schema_version、content_digest|同轨的 event_key 和 sequence 各自唯一。(trace_id,parent_event_id) 只允许同轨父节点。按轨迹、类型和序号查询。待解析父键有过滤索引。|

事件的 input、output、attributes、resource 保存为 JSONB。每条事件的时间区分来源端时间与数据库接收时间。主轨 started_at 为创建尝试的时间，ended_at 为服务端封口时间。source_name 来自 Run 冻结的项目 slug，4.1 只接收 EVALUATION_INTERNAL。序号从 1 开始。内容摘要基于规范化 UTC 时间、默认字段和规范 JSON，解决同键重放的比较问题。

|状态|谁决定，何时决定|
|---|---|
|OPEN|数据库随新 Adapter 尝试创建，允许有效凭证写入。|
|COMPLETE|来源显式声明完成，所需事件类型齐备，每个父键可解析。连续模式覆盖 1 到最后序号，稀疏模式校验事件数和最高序号。|
|PARTIAL|已有事件，但来源声明不完整、序号有缺口、父键缺失，或窗口先于正常结束声明而关闭。|
|MISSING|封口时没有事件。|

终态保持不变。Worker 正常返回、放弃、失败或恢复遗留尝试时，数据库在 Attempt 的事务中关闭其 OPEN Trace。活跃 Worker 的续租检查凭证到期。Worker 全部离线时，已到期租约立即失去写权限，状态在 Worker 恢复清理时收敛。有效窗口内重复 finalize 返回第一次封口结果。

Case 详情保留每次尝试的 trace_id、trace_status、trace_event_count。Case 级 trace_status 优先选正式预测的来源尝试，尚无预测时选择最后一次有轨迹的尝试。历史记录或尚未领取的样本可显示 NOT_COLLECTED。

## 4.2 版本能力与查询

迁移 0009_trace_capabilities 在不可变 project_versions 行上新增 trace_capabilities JSONB。它参与新版本摘要，随 Task/Run 的 project_version 快照记录。已有版本及历史快照不回填。Trace 读取时关联自己的 Run 获取声明，已有同名项目新版本不会替换它。为同轨父节点展开建立 (trace_id,parent_event_id,sequence) 索引，为时间查询建立 (trace_id,started_at) 索引。显式声明稀疏序号时按实际事件数与最高序号核对。

## 4.3 来源与归档

0010_trace_archives 新增以 trace_id 为主键的在线归档标记。评分沿用不可变的 evaluation_scores.evidence 数组，事件引用绑定正式预测的来源 Attempt。当前 Worker 最低迁移版本为 0011_trace_allocation。升级和降级只替换分配函数，保留历史轨迹、评分及证据。旧 disabled Run 的空轨迹按原 ID 仍可审计，在运行报告与样本列表归为未采集。标记、读取状态、历史引用与 API 契约见 [评分证据](evidence-model.md)。
