import { useEffect, useState } from "react";
import type { Call, Run, Page } from "./api";
import { base, labels, date, pretty, terminal } from "./api";

type Attempt = {
  id: string;
  stage: string;
  attempt_no: number;
  status: string;
  delivery_state: string;
  error_code: string | null;
};
type Score = {
  id: string;
  evaluator_version_id: string;
  status: string;
  score: number | null;
  passed: boolean | null;
  reason: string;
  details: { model?: string; usage?: Record<string, number> };
};
type Result = {
  id: string;
  case_key: string;
  status: string;
  input: unknown;
  prediction: unknown;
  adapter_error_code: string | null;
  attempts: Attempt[];
  scores: Score[];
  trace_status: string;
  trace_ids: string[];
};
const columns = [
  ["pending_cases", "待执行"],
  ["running_cases", "运行中"],
  ["succeeded_cases", "成功"],
  ["failed_cases", "失败"],
  ["timed_out_cases", "超时"],
  ["cancelled_cases", "取消"],
  ["unknown_delivery_cases", "待核对"],
] as const;
type EventData = {
  event_id: string;
  status: string;
  total: number;
  pending: number;
  running: number;
  succeeded: number;
  failed: number;
  timed_out: number;
  cancelled: number;
  unknown_delivery: number;
};
async function consume(
  response: Response,
  receive: (kind: string, data: EventData) => void,
) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("进度流不可用");
  const decoder = new TextDecoder();
  let pending = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      pending += decoder.decode(value, { stream: true });
      let end;
      while ((end = pending.indexOf("\n\n")) >= 0) {
        const lines = pending.slice(0, end).split("\n");
        pending = pending.slice(end + 2);
        const kind = lines.find((v) => v.startsWith("event: "))?.slice(7);
        const data = lines.find((v) => v.startsWith("data: "))?.slice(6);
        if (kind && data) receive(kind, JSON.parse(data));
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export default function RunDetail({
  id,
  token,
  call,
  write,
  onStatus,
}: {
  id: string;
  token: string;
  call: Call;
  write: boolean;
  onStatus: (id: string, status: string) => void;
}) {
  const path = base + "/runs/" + id;
  const [run, setRun] = useState<Run | null>(null),
    [link, setLink] = useState("正在连接"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [cursor, setCursor] = useState<string[]>([]),
    [results, setResults] = useState<Page<Result> | null>(null),
    [updated, setUpdated] = useState(0),
    [statusFilter, setFilter] = useState("");
  const [selected, setSelected] = useState("");
  const [statsOpen, setStatsOpen] = useState(false);
  useEffect(() => {
    if (!statsOpen) return;
    let live = true;
    call<Run>(path).then((detail) => {
      if (live) setRun((prior) => prior && ({
        ...prior,
        score_counts: detail.score_counts,
        failure_reasons: detail.failure_reasons,
      }));
    }).catch((e) => live && setError(String(e)));
    return () => { live = false; };
  }, [call, path, statsOpen, updated]);
  useEffect(() => {
    let alive = true;
    const abort = new AbortController();
    let lastId = "";
    async function monitor() {
      while (alive) {
        try {
          const info = await call<Run>(path);
          if (!alive) return;
          setRun(info);
          setError("");
          onStatus(id, info.status);
          setUpdated((x) => x + 1);
          if (terminal(info.status)) {
            setLink("已收敛");
            return;
          }
          const res = await fetch("/api/v1" + path + "/progress", {
            headers: {
              Authorization: "Bearer " + token,
              ...(lastId ? { "Last-Event-ID": lastId } : {}),
            },
            signal: abort.signal,
            cache: "no-store",
          });
          if (!res.ok) {
            if (
              res.status === 401 ||
              res.status === 403 ||
              res.status === 404
            ) {
              setRun(null);
              setResults(null);
              setError("当前身份已无法读取此运行");
              return;
            }
            throw new Error("进度连接暂时不可用");
          }
          if (!alive) return;
          setError("");
          setLink("实时连接");
          await consume(res, (kind, v) => {
            if (!alive) return;
            lastId = v.event_id;
            if (kind === "run.status" || kind === "run.finished")
              onStatus(id, v.status);
            if (kind === "case.completed" || kind === "run.finished")
              setUpdated((x) => x + 1);
            setRun(
              (prior) =>
                prior && {
                  ...prior,
                  status: v.status,
                  total_cases: v.total,
                  pending_cases: v.pending,
                  running_cases: v.running,
                  succeeded_cases: v.succeeded,
                  failed_cases: v.failed,
                  timed_out_cases: v.timed_out,
                  cancelled_cases: v.cancelled,
                  unknown_delivery_cases: v.unknown_delivery,
                },
            );
          });
        } catch (e) {
          if (!alive) return;
          if (e instanceof Error && "status" in e && [401, 403, 404].includes(Number(e.status))) {
            setRun(null);
            setResults(null);
            setLink("连接结束");
            setError("当前身份已无法读取此运行");
            return;
          }
          setError(String(e));
        }
        if (alive) {
          setLink("正在重新同步");
          await new Promise((res) => setTimeout(res, 900));
        }
      }
    }
    void monitor();
    return () => {
      alive = false;
      abort.abort();
    };
  }, [call, id, path, token, onStatus]);
  const last = cursor.at(-1) ?? "";
  useEffect(() => {
    let alive = true;
    const query = new URLSearchParams({ limit: "20" });
    if (last) query.set("cursor", last);
    if (statusFilter) query.set("status", statusFilter);
    call<Page<Result>>(path + "/results?" + query)
      .then((v) => {
        if (alive) setResults(v);
      })
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [call, path, last, statusFilter, updated]);
  async function control(action: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const v = await call<Run>(path + "/" + action, "POST", {});
      setRun(v);
      onStatus(id, v.status);
      setUpdated((x) => x + 1);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const item = results?.items.find((c) => c.id === selected);
  return (
    <section aria-label="运行详情">
      <div className="eval-heading">
        <h3>运行详情</h3>
        <span>{link}</span>
      </div>
      {error && <output role="alert">{error}</output>}
      {run && (
        <>
          <p>
            <strong>{labels[run.status] ?? run.status}</strong> · 共{" "}
            {run.total_cases} 条 · 开始 {date(run.started_at)} · 结束{" "}
            {date(run.finished_at)}
          </p>
          <progress
            aria-label="运行进度"
            value={run.total_cases - run.pending_cases - run.running_cases}
            max={run.total_cases || 1}
          />
          <div className="eval-counts">
            {columns.map(([key, title]) => (
              <span key={key}>
                {title} <b>{run[key]}</b>
              </span>
            ))}
          </div>
          {write && (
            <div className="row">
              {["QUEUED", "RUNNING"].includes(run.status) && (
                <button disabled={busy} onClick={() => control("pause")}>
                  暂停运行
                </button>
              )}
              {run.status === "PAUSED" && (
                <button disabled={busy} onClick={() => control("resume")}>
                  恢复运行
                </button>
              )}
              {!terminal(run.status) && run.status !== "CANCEL_REQUESTED" && (
                <button disabled={busy} onClick={() => control("cancel")}>
                  取消运行
                </button>
              )}
            </div>
          )}
          <details onToggle={(e) => setStatsOpen(e.currentTarget.open)}>
            <summary>运行快照和评分统计</summary>
            <p>
              已评分 {run.score_counts.OK} · 评分错误 {run.score_counts.ERROR} ·
              跳过 {run.score_counts.SKIPPED} · 通过 {run.score_counts.passed} ·
              未通过 {run.score_counts.failed}
            </p>
            <pre>{pretty(run.snapshot)}</pre>
          </details>
          {!!run.failure_reasons.length && (
            <p>
              失败原因：
              {run.failure_reasons
                .map((r) => r.code + " × " + r.cases)
                .join("、")}
            </p>
          )}
          <h4>样本结果</h4>
          <label>
            样本状态
            <select
              value={statusFilter}
              onChange={(e) => {
                setFilter(e.target.value);
                setCursor([]);
                setResults(null);
                setSelected("");
              }}
            >
              <option value="">全部状态</option>
              {[
                "READY",
                "RUNNING",
                "RETRY_WAIT",
                "SUCCEEDED",
                "FAILED",
                "TIMED_OUT",
                "CANCELLED",
                "UNKNOWN_DELIVERY",
              ].map((s) => (
                <option value={s} key={s}>
                  {labels[s]}
                </option>
              ))}
            </select>
          </label>
          <table>
            <thead>
              <tr>
                <th>样本标识</th>
                <th>状态</th>
                <th>尝试次数</th>
              </tr>
            </thead>
            <tbody>
              {results?.items.map((c) => (
                <tr key={c.id}>
                  <td>
                    <button onClick={() => setSelected(c.id)}>
                      {c.case_key}
                    </button>
                  </td>
                  <td>{labels[c.status]}</td>
                  <td>{c.attempts.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {results && !results.items.length && <p>当前条件下没有样本。</p>}
          <div className="row">
            <button
              disabled={!cursor.length}
              onClick={() => {
                setCursor((c) => c.slice(0, -1));
                setResults(null);
                setSelected("");
              }}
            >
              上一页
            </button>
            <span>第 {cursor.length + 1} 页</span>
            <button
              disabled={!results?.next_cursor}
              onClick={() => {
                if (results?.next_cursor)
                  setCursor((c) => [...c, results.next_cursor!]);
                setResults(null);
                setSelected("");
              }}
            >
              下一页
            </button>
          </div>
          {item && (
            <article>
              <h4>样本详情 · {item.case_key}</h4>
              <p>{item.adapter_error_code}</p>
              <b>原始输入（已脱敏）</b>
              <pre>{pretty(item.input)}</pre>
              <b>实际预测</b>
              <pre>
                {item.prediction ? pretty(item.prediction) : "尚无已保存的预测"}
              </pre>
              <b>执行尝试</b>
              <ul>
                {item.attempts.map((a) => (
                  <li key={a.id}>
                    {a.attempt_no} · {a.stage} · {a.status} · {a.delivery_state}{" "}
                    {a.error_code}
                  </li>
                ))}
              </ul>
              <b>评分结果</b>
              <ul>
                {item.scores.map((s) => (
                  <li key={s.id}>
                    {
                      run.snapshot.evaluator_plan.find(
                        (v) => v.id === s.evaluator_version_id,
                      )?.name
                    }{" "}
                    · {labels[s.status]} · {s.score ?? "—"}{" "}
                    {s.passed === null ? "" : s.passed ? "通过" : "未通过"} ·{" "}
                    {s.reason}
                    {s.details.model && (
                      <p>
                        裁判模型 {s.details.model} · Token 用量{" "}
                        {pretty(s.details.usage ?? null)}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
              <p>
                Trace：
                {item.trace_ids.length
                  ? item.trace_ids.join("、")
                  : "尚无已采集轨迹"}
              </p>
            </article>
          )}
        </>
      )}
    </section>
  );
}
