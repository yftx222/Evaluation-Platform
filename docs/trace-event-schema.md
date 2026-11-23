# TraceEvent 1.0

```json
{
  "events": [{
    "schema_version": "1.0",
    "event_key": "model-1",
    "sequence": 1,
    "parent_event_key": null,
    "event_type": "MODEL_CALL",
    "name": "生成回答",
    "status": "SUCCEEDED",
    "started_at": "2026-11-23T10:00:00Z",
    "ended_at": "2026-11-23T10:00:00.250Z",
    "duration_ms": 250,
    "input": {"prompt_ref": "template-4"},
    "output": {"answer_ref": "prediction"},
    "attributes": {"model": "local-model", "input_tokens": 12},
    "resource": {"service_name": "demo"}
  }]
}
```

必填字段为 event_key、sequence、event_type、name、status、started_at。键为 1–128 位 ASCII 字母、数字或下划线、点、冒号、连字符，名称为 1–160 个字符。sequence 是正整数，最大为 PostgreSQL bigint 的上限。ended_at 须晚于或等于 started_at，时间须带时区。duration_ms 可省略，提供时为 0 至 604800000 的整数。四个 JSON 对象默认均为空。顶层未知字段、重复 JSON 属性和非有限数值均被拒绝。

类型支持 AGENT_STEP、MODEL_CALL、TOOL_CALL、RETRIEVAL、RERANK、GUARDRAIL、FINAL_OUTPUT、CUSTOM。状态支持 STARTED、SUCCEEDED、FAILED、CANCELLED、UNKNOWN。已入库的事件内容不可修改。需要分别记录开始和结束时，使用不同 event_key、sequence，通过父键关联。

来源端可先发送子事件；只按同一 Trace 的 event_key 解析父节点，同名的外部轨迹事件不会被匹配。父事件到达后补齐 parent_event_id；闭环会使整个请求回滚。事件序号与 HTTP 到达顺序独立。收到传输成功响应前重试应复用发送过的键和内容。

每批 1–100 条，原始 HTTP 请求体上限 512 KiB。每条事件规范 JSON 的 UTF-8 大小上限 32 KiB，从事件根开始的嵌套深度上限为 10。限制在落库前检查，读请求体时也限制累计字节数。

## 事件示例

```json
{"schema_version":"1.0","event_type":"RETRIEVAL","name":"检索公开商品资料","status":"SUCCEEDED","event_key":"retrieve-2","sequence":2,"parent_event_key":"plan-1","started_at":"2026-11-23T10:00:00Z","ended_at":"2026-11-23T10:00:00.040Z","duration_ms":40,"input":{"query_ref":"case_input:/query"},"output":{"document_refs":["public-guide"]},"attributes":{"top_k":1},"resource":{"index_version":"catalog-3"}}
```

## 4.2 各类事件的记录口径

|事件|优先记录的实际观测|
|---|---|
|AGENT_STEP|步骤名、Agent 名称、步骤序号和状态。|
|MODEL_CALL|模型、修订、输入/输出 Token、持续时间和错误码。|
|TOOL_CALL|工具名称与版本、脱敏参数、结果摘要与状态。|
|RETRIEVAL|知识库、检索器、top_k、document_refs、scores 与重排顺序。|
|RERANK、GUARDRAIL、CUSTOM|记录动作名称、耗时、状态及项目所需的经批准属性。|
|FINAL_OUTPUT|通过 output_ref 引用当前预测，记录实际输出状态。|

详情使用来源端字段，缺少的 Token 或耗时不填零。新发布版本可声明必需事件及序号策略。稀疏序号的确认方式见 [上报接口](trace-write-api.md)。
