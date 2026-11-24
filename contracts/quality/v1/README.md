# Agent 质量工程 · 中台与 AIOps 的 v1 接口契约

此目录保存阶段 A 冻结的线格式，样例中的 UUID 和时间是测试数据。中台维护生产 Trace、草稿提案和批次明细的格式，AIOps 维护事件收件格式。两端的实际 HTTP 验收由中台 `backend/tests/test_quality_peer_http.py` 执行，可核对收件回执、草稿发布和逐样本回测结果。

**本轮对齐项：草稿发布后保留 ID。** 共同样例 `exchanges.json` 中，`submit_proposal` 与 `proposal_replay` 的 `draft_id` 应为 `17b011b7-a102-4c93-94e9-000000000005`，与 `batch-completed.json` 的 `dataset_version_id` 一致。中台现有实现把同一行从 DRAFT 改为 PUBLISHED，这条样本的 ID 随之保留。若后续从已发布版本派生新草稿，系统会复制样本并分配新 ID，须按新版本内的 case_key 重新取样本 ID。早期样例里的尾号 015 不适用于本例。

生产事件的 sequence 上限与 PostgreSQL bigint 及原 TraceEventInput 一致，为 `9223372036854775807`。生产 Trace Schema 作为跨语言文件分发时须保留原始整数字面值。中台 CI 将 traceEvent 定义与现有模型导出的定义直接比较。

## 文件与入口

| 文件 | 消费方式 |
| --- | --- |
| [event.schema.json](event.schema.json) | 根 Schema 校验质量事件；命名定义 receipt、error 校验收件回执和错误。与 AIOps 使用同一份事件定义。 |
| [production-trace.schema.json](production-trace.schema.json) | 按接口选择 writeBatch、writeReceipt、finalize、finalizeReceipt、tracePage、traceResponse 或 eventsPage 定义。traceEvent 沿用中台既有事件结构。 |
| [evaluation.schema.json](evaluation.schema.json) | proposal、proposalResponse、caseResultsResponse 分别校验提案、回执及逐样本结果。 |
| [feedback.json](feedback.json)、[batch-completed.json](batch-completed.json)、[exchanges.json](exchanges.json)、[production-trace.json](production-trace.json) | 依次覆盖反馈、三 Run 批次、双方 HTTP 请求/回执、两页可还原的业务轨迹。命名定义通过 JSON Pointer `#/$defs/<名称>` 选择。 |

## 调用与授权矩阵

表中权限描述表示实际授予的操作范围。部署使用的账号、密钥引用、AIOps Source 名和环境映射由配置提供。演示映射使用 AIOps Source 项目 `ecommerce-customer-service`、环境 `test`，对应 feedback.json 中的中台 UUID 和 slug。配置可显式映射不同名的 Source；禁止按展示名称模糊猜测。

| 谁调用谁 | 方法与路径 | 身份、输入与返回 |
| --- | --- | --- |
| 中台 → AIOps | POST /api/v1/quality-events；GET /api/v1/quality-events/{event_id} | 专用事件投递及本来源对账权限。按身份核对 source_system、项目 UUID/slug/环境。Inbox 持久化后首次与重放均为 202，查询 200，data 含同一 event_id/status/received_at。 |
| 业务 → 中台 | POST /api/v1/production-trace-write/events:batch；POST /api/v1/production-trace-write/finalize | 生产 Trace 写凭证限定项目和环境，服务端派生 project_id，核对环境；成功返回本批接收/重复计数或封口回执。Attempt Token、普通登录令牌与生产写凭证分别验证。 |
| AIOps → 中台 | GET /api/v1/production-traces；GET /api/v1/production-traces/{trace_id}；GET /api/v1/production-traces/{trace_id}/events | 指定项目和环境的生产证据读权限。列表以项目、环境、时间或运行/会话等条件定位；事件支持按类型、Tool/Skill 名称和状态筛选。返回 data 与 request_id。受控载荷取用另核权限，查询留审计。 |
| AIOps → 中台 | POST /api/v1/datasets/{dataset_id}/case-proposals；GET /api/v1/datasets/{dataset_id}/case-proposals/{proposal_id} | 审核提案写/查权限限到调用方、项目、环境与 Dataset。POST 的 Idempotency-Key 使用 proposal_id，路径与正文的 dataset_id 必须一致；首次 201、原样重放 200；GET 200 可返回 published_dataset_version_id。发布由评测负责人走既有审核流程。 |
| AIOps → 中台 | GET /api/v1/regression-batches/{id}/case-results；GET /api/v1/regression-batches/{id}/report | 项目级批次结果读权限，并核对允许的环境。使用完成事件中的受控相对引用。明细含批次 ID、数据集版本、items、next_cursor；报告给出整批门禁。CI/人工启动回测的权限独立，具体触发请求留在 C 阶段。 |

成功回执的 request_id 标记本次 HTTP 请求，重复请求允许返回不同 request_id。认证缺失或无效返回 401；写入超出身份范围返回 403/SCOPE_DENIED；按资源 ID 读取无可见记录返回 404/NOT_FOUND，避免泄露其他项目的记录。正文不合法返回 422。同键异文返回 409；事件冲突码为 EVENT_ID_CONFLICT。429、暂时性 5xx 和超时均按有限策略处理，遵循 Retry-After；契约冲突保留原回执供对账。

## 关联、分页与缺证据

- project_id、trace_id、event_id、case_id、proposal_id、review_id、evaluation_case_id、批次、Task、Run 和各中台版本 ID 使用 UUID。会话、消息、agent_run_id 和分组是业务标识，保持来源字符串。会话可对应多个 Run，一个 Case 可关联多个事件和 Trace。未观察到的版本字段可省略或按 Schema 返回 null。质量信封的 deployment_version 键必须存在，未知时为 null。
- 生产 Trace 写入至少定位 trace_id、agent_run_id、environment、deployment_version 和 started_at。额外的 lineage 可记录代码部署关联、模型与 revision、Prompt、Tool/Skill、知识索引、Embedding、配置摘要和 Fixture。评测样本的 dataset_version_id、evaluator_version_ids 来自评测快照；巡检仅有评测证据时，不造生产运行 ID。
- 事件序号从 1 开始，同一 Trace 内唯一，event_key 重试保持不变，parent_event_key 表示实际调用关系。批次到达先后可与执行顺序不同。服务端在封口时校验数量、顺序和父子关系；缺口或未完成上报不能得到 COMPLETE。相同事件键、不同语义内容报告冲突。
- 查询把 **completeness**（OPEN / COMPLETE / PARTIAL / MISSING）与 **availability**（AVAILABLE / ARCHIVED / EXPIRED / DELETED）分开。可见元数据保留时返回 200 并说明证据是否可取；已知运行零事件可标 MISSING，完全未知的 Trace 用 404。已过期、已删除的事件正文不可用；归档时显示归档状态与可用的取回方式，不用空事件页冒充完整证据。
- 列表和事件均使用服务端不透明 cursor。请求带 limit 和上页 next_cursor，返回 items 和 next_cursor，末页为 null。事件页按 sequence 递增，继续查询保持相同筛选和授权范围；OPEN 的内容可能增长，最终完整性以封口结果为准。
- 提案使用人工确认的 review_id、case_id 与指向证据的引用，错误回答留作证据。相同审核修订的重放复用 proposal_id；人工改判或新修订产生新的 review_id 和 proposal_id。根据 proposal_id 查询处理结果；授权范围和内容冲突不得返回其他提案的数据。
- 完成事件使用 REGRESSION_BATCH_COMPLETED，项目在 project 中，payload 中携带三个独立的 trial/run_id/status、版本清单、blocking_failures、gate、report_ref、case_results_ref。三个 trial 编号分别是 1、2、3，run_id 互异。回调只发送引用与摘要。case-results 的 trial_results 按 1、2、3 排列，各槽为 PASSED、FAILED 或 BLOCKED；无法判定保留 BLOCKED。用 dataset_version_id 与该版 evaluation_case_id/case_key 精确关联，整批 gate 不能覆盖样本判断。

## 幂等与重放

事件全局幂等键为 event_id；提案幂等键为 proposal_id。双方对同一已解析 JSON 正文使用递归键排序、数组保持顺序、UTF-8、无多余空格、保留非 ASCII 字符的序列化，然后取 SHA-256。示例算法与中台 canonical 一致，禁止重复 JSON 对象键或非有限数字；重试复用原始已冻结正文，包括时间戳表达，授权头和 request_id 不参与摘要。feedback.json 的向量为 `630ab2c078a01d3919966ca654972419eb418add01e96f1ad0f2a22e64d0f1a7`。业务变更和 Outbox 同事务；接收方在 Inbox 落库后才确认。案例归并独立于事件去重。

## 验证方式

在中台 backend 目录运行 `uv run --locked --group test pytest -q tests/test_quality_contract_v1.py`。中台测试支持用 QUALITY_PEER_CONTRACT_DIR 指向对端样例；AIOps 自身的测试支持用 AIOPS_QUALITY_CONTRACT_DIR 指向本目录。先校验 Schema，再分别断言两端的回执、分页、血缘和失败语义。以上是线格式验证；真实 HTTP 鉴权、持久化、故障重放和两个数据库的对账在对应实现阶段另行验收。
