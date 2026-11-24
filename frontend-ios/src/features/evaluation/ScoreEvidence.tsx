import { useEffect, useState } from "react";
import type { Call } from "./api";

type Reference = {
  evidence_id: string;
  source_type: string | null;
  json_pointer: string | null;
  excerpt: string | null;
  status: string;
  reason: string | null;
  trace_id: string | null;
  event_id: string | null;
  metadata: { relation?: string };
};
type Group = {
  score_id: string;
  items: Reference[];
  children: {
    score_id: string | null;
    evaluator_version_id: string;
    weight: number;
  }[];
};
const types: Record<string, string> = {
  TRACE_EVENT: "轨迹事件",
  PREDICTION: "正式预测",
  EXPECTED: "预期结果",
  CASE_INPUT: "样本输入",
  EXTERNAL_REFERENCE: "已登记的外部引用",
};
const reasons: Record<string, string> = {
  SOURCE_ARCHIVED: "来源已归档",
  INVALID_POINTER: "字段路径不存在",
  SOURCE_MISSING: "来源缺失或不属于本次执行",
  INVALID_REFERENCE: "引用格式无效",
  UNTRUSTED_SOURCE: "来源未登记",
  SENSITIVE_CONTENT: "内容已脱敏",
};

export default function ScoreEvidence({
  call,
  scoreId,
  onEvent,
  names,
}: {
  call: Call;
  scoreId: string;
  onEvent: (trace: string, event: string) => void;
  names: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const [group, setGroup] = useState<Group | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    let live = true;
    setGroup(null);
    setError("");
    call<Group>("/evaluations/scores/" + scoreId + "/evidence")
      .then((data) => {
        if (live) setGroup(data);
      })
      .catch((e) => {
        if (live)
          setError(
            e.status === 401
              ? "登录凭证已失效，请重新登录"
              : [403, 404].includes(e.status)
                ? "当前无权查看或评分不存在"
                : String(e),
          );
      });
    return () => {
      live = false;
    };
  }, [call, scoreId, open]);
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        查看评分证据
      </button>
      {open && (
        <section aria-label="评分证据">
          {error && <p role="alert">{error}</p>}
          {!group && !error && <p>正在读取证据…</p>}
          {group && !group.items.length && !group.children.length && (
            <p>此评分没有引用证据。</p>
          )}
          {group && (
            <ul>
              {group.items.map((ref) => (
                <li key={ref.evidence_id}>
                  <strong>{types[ref.source_type ?? ""] ?? "证据"}</strong>{" "}
                  {ref.metadata.relation === "prediction_provenance" &&
                    "· 预测来源 "}
                  {ref.json_pointer && <code>{ref.json_pointer}</code>}{" "}
                  {ref.status === "AVAILABLE" ? (
                    <span>{ref.excerpt}</span>
                  ) : (
                    <span>{reasons[ref.reason ?? ""] ?? "证据不可用"}</span>
                  )}{" "}
                  {ref.trace_id &&
                    ref.event_id &&
                    ref.status === "AVAILABLE" && (
                      <button
                        type="button"
                        onClick={() => onEvent(ref.trace_id!, ref.event_id!)}
                      >
                        定位轨迹事件
                      </button>
                    )}
                </li>
              ))}
              {group.children.map((child) => (
                <li key={child.evaluator_version_id}>
                  子评分器{" "}
                  {names[child.evaluator_version_id] ??
                    child.evaluator_version_id}{" "}
                  · 权重 {child.weight}{" "}
                  {child.score_id ? (
                    <a href={"#score-" + child.score_id}>查看子评分</a>
                  ) : (
                    "子评分缺失"
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
