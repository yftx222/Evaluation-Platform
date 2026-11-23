# Phase 4.2 · 被测服务如何提供执行轨迹

每次 Adapter 调用有自己的 Trace。项目版本固定采集方式，沿用 Phase 4.1 的短期凭证和项目读权限。服务部署者提供评测 API 的可信地址。

## 选择采集方式

|项目实际能返回什么|配置方式|被测服务需要做什么|
|---|---|---|
|可以在执行时发送事件|callback|从请求头读取 X-Trace-Write 和 X-Trace-Id，使用 SDK 或直接调用批量上报 API。Worker 只向获准的目标版本发送凭证。|
|可以把事件摘要放在答案旁边|embedded|在本次响应中返回事件与结束声明。Worker 保存有效事件后再提交预测，目标无需回调地址或凭证。|
|没有采集能力|disabled|正常提供答案。不分配 Trace，不签发写凭证；样本报告显示 NOT_COLLECTED。|

发布项目版本时可在「执行轨迹采集方式」填写 JSON。已发布版本可展开查看原声明。下面是一个只上报模型调用和最终回答的例子。

```json
{
  "mode": "callback",
  "event_types": ["MODEL_CALL", "FINAL_OUTPUT"],
  "required_events": ["FINAL_OUTPUT"],
  "contiguous": true,
  "parent_links": false,
  "model_usage": true,
  "retrieval_refs": false,
  "tool_details": false
}
```

event_types 和 parent_links 决定接收的类型与关系。required_events 决定封口时哪些类型必须出现，缺少返回 PARTIAL / REQUIRED_EVENT_MISSING。model_usage、retrieval_refs 和 tool_details 记录来源宣称能提供的字段，平台只显示实际观测值。量表可以只要求 FINAL_OUTPUT；没有 TOOL_CALL、RETRIEVAL 的普通问答也可以 COMPLETE。

未填写时，普通 HTTP 与 Agent 使用通用回调契约，允许八种类型及父子关系；OpenAI Compatible 默认允许 MODEL_CALL、FINAL_OUTPUT、CUSTOM，使用平面序号。默认没有必需类型。回调配置还须通过部署 EVAL_TRACE_TARGET_VERSIONS 白名单。项目可在发布时按实际实现收窄或扩展声明。改能力需发布新版本。老版本的空声明沿用 4.1 的回调规则，历史 Run 的快照保持原值。

## Python SDK

在被测服务的 Python 3.11+ 环境中运行 `python -m pip install /path/to/code/backend/trace_sdk`。当前 SDK 版本为 0.2.0，只依赖 httpx。以下示例接在已有异步业务函数中。

```python
from trace_sdk.client import TraceSession

async def handle(request):
    trace = TraceSession.from_headers(PLATFORM_ORIGIN, request.headers)
    if trace is None:
        return {"answer": await answer_question(request)}
    async with trace:
        parent = trace.add("AGENT_STEP", "生成答案", status="STARTED")
        answer = await answer_question(request)
        trace.add("FINAL_OUTPUT", "返回答案", parent_event_key=parent,
                  output={"output_ref": "prediction.answer"})
    return {"answer": answer}
```

SDK 从头部绑定既有轨迹，单次请求一个会话。add 在内存中脱敏、分配 event_key 和 sequence，返回的键可用于父子引用。默认队列 128 条，可配 1–1024；每批默认 15 条，可配 1–15，每条 32 KiB。每次会话只创建一个发送任务。满队列、非法负载、超限或发送失败会影响完整性；窗口关闭前有数据可封为 PARTIAL。发生业务异常或协程取消时也可以使用 `async with trace` 自动发出部分完成声明，业务异常继续传播。

推荐用 `async with trace`。退出时会在会话截止前等到最后一批发送和 finalize 回执，再返回业务响应。异常或取消会请求 PARTIAL 并继续传播业务异常。手动调用 `finish()` 立即返回可等待的 Future。每个等待者共享同一发送任务，取消一个等待者不会中断其他等待者或重复封口。手动集成时在返回前执行 `await trace.finish()`。flush 等待当前队列处理完并报告是否出现过丢失。网络错误、429 和 5xx 的每个请求最多发三次，重试复用完全相同的内容。连接超时 1 秒，单次请求超时 2 秒，整个会话默认 5 秒，最长 300 秒且仍受 Attempt 窗口限制。会话进行中 status 为 None。获封口回执后 finalize_confirmed 为 True，confirmed_status 保存服务端 COMPLETE、PARTIAL 或 MISSING。成功交付时 status 与回执一致。已知丢失或业务取消时本地标记 PARTIAL，即使服务端因未收到事件而回执 MISSING。如果调用方恰在 finalize 已发出后取消，SDK 保留原回执，本地 status 标记 PARTIAL 并传播取消；已提交的服务端封口保持不可变。截止前未获得回执时本地 status 为 PARTIAL，finalize_confirmed 为 False；服务端以实际持久化结果为准。发送任务结束会清除凭证。

## HTTP 与内嵌接入

自行实现客户端时使用 [TraceWrite API](trace-write-api.md)。只发送经批准的业务引用。SDK 和内嵌采集会删除明显的密钥、身份字段，对可识别的敏感字符串替换为 [REDACTED]；原始 HTTP API 继续拒绝不安全事件。

下面的内嵌响应适用于普通 HTTP Adapter，配置 options.extensions_path 为 observations。

```json
{
  "answer": "可查看文档 A",
  "observations": {
    "trace": {
      "events": [{
        "event_key": "end", "sequence": 1, "event_type": "FINAL_OUTPUT",
        "name": "返回答案", "status": "SUCCEEDED",
        "started_at": "2026-11-23T10:00:00Z",
        "output": {"output_ref": "prediction.answer"}
      }],
      "finalize": {"outcome": "COMPLETE", "contiguous": true, "last_sequence": 1}
    }
  }
}
```

OpenAI Compatible 保留原 choices 结构，把同样的 trace 对象放在响应 extensions.trace。Agent 使用 final_answer，可放 extensions.trace，亦可继续使用已有顶层 events 数组；后者的 source_event_key、parent_key、occurred_at 和 metadata 分别转成 event_key、parent_event_key、started_at 和 attributes。已有 Agent 事件未提供状态时记录 UNKNOWN。非对象输入输出放入 value 字段，Token 计数保留在 attributes。SDK 请求、内嵌响应和这三种协议都使用同一事件表。

Worker 从预测中取走内嵌的专用事件字段，避免在预测里重复存储。来源未显式结束时，有效事件只能得到 PARTIAL。解析失败或批次违反声明时，业务答案仍可落库。详情中的 output_ref 和所属 Attempt 的 prediction_attempt_id 可定位正式预测。

## 时间线

从「批量评测 → 运行详情 → 样本 → 第 N 次尝试 → 查看时间线」进入。列表按 sequence 分页，可按事件类型和来源端时间筛选。父子视图按需加载各层；暂时找不到父事件的子项会显示提示。失败事件单独标红。点开事件详情才会请求该事件的输入、输出、属性与来源，检索引用、分数和工具结果按来源提供的内容展示。
