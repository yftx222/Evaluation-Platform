# Phase 4.2 · TraceWrite 与时间线接口

所有路径以 /api/v1 开头。服务端随尝试建轨。被测服务从受信请求头 X-Trace-Id 取得本次轨迹 ID，从 X-Trace-Write 取得仅此次回调可用的短期令牌。上报地址由服务部署配置。HTTP 回调使用 Authorization: Bearer <短期令牌>，请求体为 UTF-8 JSON。项目、Run、Attempt 和 Trace 归属均由服务端查出。

|方法和路径|请求|成功响应 data|权限|
|---|---|---|---|
|POST /trace-write/events:batch|{"events":[事件,...]}|trace_id、accepted、duplicate、rejected|当前持租 ADAPTER Attempt 的 TraceWrite|
|POST /trace-write/finalize|{"outcome":"COMPLETE","contiguous":true,"last_sequence":2}|封口后的 Trace 摘要，含 status、event_count、close_reason、ended_at|同上|
|GET /traces/{trace_id}|路径 UUID|Trace 摘要和 archive_eligible_at，可为空|项目 R/W/A、Org 或本项目 results:read Key|
|GET /traces/{trace_id}/events|after_sequence 默认 0，limit 默认 50，范围 1–100|items 按 sequence 升序，next_sequence 为空表示本次没有下一页|同上|

成功响应均附 request_id。分页可用上一页的 next_sequence 接着读。OPEN 期间较小序号可能迟到，要求完整遍历时从封口后的第一页开始。finalize 的 outcome 可为 COMPLETE 或 PARTIAL，last_sequence 为非负整数。在版本声明 contiguous=false 时，来源可以提交 contiguous=false、event_count 和 last_sequence。实际总数、最高序号、必需类型和父子引用都吻合才判 COMPLETE。使用连续序号的旧契约继续要求从 1 开始无缺段。finalize 先于迟到事件提交时，终态保留并拒绝追加。在凭证仍有效时重复 finalize 幂等；凭证已过期或撤销后统一返回 401。

|HTTP|错误码|操作|
|---|---|---|
|401|INVALID_TRACE_CREDENTIAL|无效凭证、错误身份、过期、取消、阶段或租约变化。终止该尝试的回调。|
|409|EVENT_KEY_CONFLICT、SEQUENCE_CONFLICT、PARENT_CYCLE、TRACE_SEALED|整批回滚。核对键、序号和父子关系；已封口时停止追加。|
|413|EVENT_TOO_LARGE、BATCH_TOO_LARGE|缩小事件或批次。|
|422|INVALID_JSON、INVALID_EVENT、EMPTY_BATCH、SENSITIVE_FIELD、UNSAFE_VALUE、FIELD_DEPTH、INVALID_FINALIZE|修正结构、删除敏感字段或替换为经批准的引用。|
|401/403/404|查询端遵循既有身份和项目权限|使用本项目获准的只读身份。|

批量内容错误返回 accepted=0、duplicate=0、rejected=批大小。无法解析或无法完整读取请求时 rejected=null。error.index 在能定位时给出从零开始的事件位置。认证失败使用通用错误信封，不透露请求指定的执行对象是否存在。以同一 event_key、同一语义内容重放已接受事件，在 OPEN 期间计入 duplicate。写操作在同一数据库事务内执行，冲突不会留下半批数据。

## 时间线读接口

所有读接口沿用项目 R/W/A、Org 或本项目 results:read Key。TraceWrite 凭证不能读取事件。

|方法与路径|参数和结果|
|---|---|
|GET /traces/{id}|摘要附 capabilities（来自本 Run 快照），历史记录可能为 null。|
|GET /traces/{id}/events|可选 event_type、started_from、started_to（带时区，含边界），沿用 after_sequence、limit。roots=true 列出根及未解析父节点的事件，parent_id 指定直接子项；两者互斥。|
|GET /traces/{id}/events?summary_only=true|返回核心字段、has_children 和实际数值型 input_tokens / output_tokens。省略 input、output、attributes、resource。旧调用默认仍可获取整页事件。|
|GET /traces/{id}/events/{event_id}|按原项目授权，校验事件同时属于指定 Trace。返回单事件全部业务字段，其他 Trace 的事件 ID 返回 404。|

写入未声明的类型或不支持的父关系时整批返回 422 UNDECLARED_EVENT。未满足必需类型时封为 PARTIAL，close_reason 为 REQUIRED_EVENT_MISSING。时间区间颠倒或同时指定根和父节点返回 422。详见 [SDK 与接入](trace-sdk-guide.md)。
