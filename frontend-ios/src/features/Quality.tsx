import { useCallback, useEffect, useRef, useState } from "react";

type Call = <T>(
  url: string,
  method?: string,
  body?: unknown,
  headers?: Record<string, string>,
) => Promise<T>;
type Item = {
  id: string;
  name?: string;
  task_id?: string;
  project_version_id?: string;
  environment?: string;
  gate?: string;
  enabled?: boolean;
  agent_run_id?: string;
  availability?: string;
  completeness?: string;
};
type Group = "batches" | "traces" | "inspections" | "shadows" | "experiments";
const groups: Record<Group, string> = {
  batches: "三次回测",
  traces: "生产轨迹",
  inspections: "周期巡检",
  shadows: "影子测试",
  experiments: "A/B 实验",
};
const urls = {
  inspections: "inspection",
  shadows: "shadow",
  experiments: "experiment",
} as const;
type Overview = Record<Group, Item[]>;

type BatchDetails = {
  id: string;
  trials: { trial: number; run_id: string; status: string }[];
  report: {
    gate: string;
    case_count: number;
    passed_cases: number;
    blocked_cases: string[];
  } | null;
  cases: {
    items: {
      id: string;
      case_key: string;
      result: { trial_results: string[]; gate: string };
    }[];
    next_cursor: string | null;
  };
};
type Timeline = {
  trace: {
    trace_id: string;
    agent_run_id: string;
    completeness: string;
    availability: string;
  };
  events: {
    event_key: string;
    sequence: number;
    parent_event_key?: string;
    name: string;
    event_type: string;
    status: string;
  }[];
  next_after: number | null;
};
const verdicts: Record<string, string> = {
  PASSED: "通过",
  FAILED: "失败",
  BLOCKED: "无法判定",
  COMPLETED: "已完成",
  QUEUED: "排队中",
  RUNNING: "执行中",
};
function Result({
  details,
  more,
}: {
  details: unknown;
  more: (suffix: string) => void;
}) {
  if (!details || typeof details !== "object") return null;
  if ("trials" in details && "cases" in details) {
    const b = details as BatchDetails;
    return (
      <section>
        <h3>
          {b.report
            ? "门禁" + (verdicts[b.report.gate] ?? b.report.gate)
            : "等待三次运行结束"}
        </h3>
        {b.report && (
          <p>
            {b.report.case_count} 个样本，{b.report.passed_cases} 个通过，
            {b.report.blocked_cases.length} 个无法判定。
          </p>
        )}
        <ol>
          {b.trials.map((t) => (
            <li key={t.trial}>
              第 {t.trial} 次 · {verdicts[t.status] ?? t.status} · {t.run_id}
            </li>
          ))}
        </ol>
        {b.cases.items.map((c) => (
          <p key={c.id}>
            <strong>{c.case_key}</strong> ·{" "}
            {c.result.trial_results.map((v) => verdicts[v] ?? v).join(" / ")}
          </p>
        ))}
        {b.cases.next_cursor && (
          <button
            onClick={() =>
              more(
                "/regression-batches/" +
                  b.id +
                  "?cursor=" +
                  encodeURIComponent(b.cases.next_cursor!),
              )
            }
          >
            下一页样本
          </button>
        )}
      </section>
    );
  }
  if ("events" in details && "trace" in details) {
    const t = details as Timeline;
    return (
      <section>
        <h3>{t.trace.agent_run_id} 的执行时间线</h3>
        <p>
          {t.trace.completeness} · {t.trace.availability}
        </p>
        <ol>
          {t.events.map((e) => (
            <li key={e.sequence}>
              <strong>
                {e.sequence}. {e.name}
              </strong>{" "}
              · {e.event_type} · {e.status}
              {e.parent_event_key && (
                <small> 上级事件 {e.parent_event_key}</small>
              )}
            </li>
          ))}
        </ol>
        {t.next_after != null && (
          <button
            onClick={() =>
              more(
                "/production-traces/" +
                  t.trace.trace_id +
                  "/timeline?after=" +
                  t.next_after,
              )
            }
          >
            下一页事件
          </button>
        )}
      </section>
    );
  }
  return (
    <details open>
      <summary>记录详情</summary>
      <pre>{JSON.stringify(details, null, 2)}</pre>
    </details>
  );
}

export default function Quality({
  projects,
  call,
}: {
  projects: Pick<Item, "id" | "name">[];
  call: Call;
}) {
  const [project, setProject] = useState(projects[0]?.id ?? "");
  const [environment, setEnvironment] = useState("test");
  const [section, setSection] = useState<Group>("batches");
  const [tasks, setTasks] = useState<Item[]>([]);
  const [versions, setVersions] = useState<{ id: string; label: string }[]>([]);
  const [taskId, setTask] = useState("");
  const [data, setData] = useState<Overview | null>(null);
  const [details, setDetails] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const triggerKey = useRef("");
  const base = "/projects/" + project;
  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    if (!project || !environment.trim()) return;
    try {
      const [overview, taskPage, versionPage] = await Promise.all([
        call<Overview>(
          base +
            "/quality-overview?environment=" +
            encodeURIComponent(environment),
        ),
        call<{ items: Item[] }>(
          "/evaluations/tasks?project_id=" +
            project +
            "&archived=false&limit=200",
        ),
        call<{ items: { id: string; label: string }[] }>(
          base + "/versions?limit=200",
        ),
      ]);
      if (gen === generation.current) {
        setData(overview);
        setTasks(taskPage.items);
        setVersions(versionPage.items);
        setError("");
      }
    } catch (e) {
      if (gen === generation.current) setError(String(e));
    }
  }, [base, call, environment, project]);
  useEffect(() => {
    setDetails(null);
    setData(null);
    setTasks([]);
    setVersions([]);
    setTask("");
    triggerKey.current = "";
    void refresh();
    return () => {
      generation.current++;
    };
  }, [refresh]);
  async function run(work: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      const result = await work();
      setDetails(result);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section>
      <label>
        项目
        <select
          aria-label="项目"
          disabled={busy}
          value={project}
          onChange={(e) => setProject(e.target.value)}
        >
          <option value="">选择项目</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        环境
        <input
          disabled={busy}
          value={environment}
          onChange={(e) => setEnvironment(e.target.value)}
          placeholder="例如 test"
        />
      </label>
      <nav aria-label="质量工程模块">
        {(Object.keys(groups) as Group[]).map((k) => (
          <button
            disabled={busy}
            aria-pressed={section === k}
            key={k}
            onClick={() => {
              setSection(k);
              setDetails(null);
            }}
          >
            {groups[k]}
          </button>
        ))}
      </nav>
      <button disabled={!project || busy} onClick={() => void refresh()}>
        刷新结果
      </button>
      {error && <p role="alert">{error}</p>}
      {section === "batches" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const task = tasks.find((t) => t.id === taskId);
            if (!task || !task.project_version_id) return;
            triggerKey.current ||= crypto.randomUUID();
            void run(async () => {
              const result = await call(
                base + "/regression-batches",
                "POST",
                {
                  environment,
                  task_id: task.id,
                  project_version_id: task.project_version_id,
                },
                { "Idempotency-Key": triggerKey.current },
              );
              triggerKey.current = "";
              return result;
            });
          }}
        >
          <label>
            已冻结的评测任务
            <select
              required
              disabled={busy}
              value={taskId}
              onChange={(e) => {
                setTask(e.target.value);
                triggerKey.current = "";
              }}
            >
              <option value="">选择任务</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <button disabled={!taskId || !environment || busy} type="submit">
            用同一版本跑三次
          </button>
        </form>
      )}
      {section in urls && (
        <form
          key={section + project + environment}
          onSubmit={(e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            const val = (k: string) => String(f.get(k) ?? "");
            const num = (k: string) => Number(val(k));
            const spec =
              section === "inspections"
                ? {
                    task_id: val("task"),
                    period_s: num("period"),
                    threshold: num("threshold"),
                    starts_at: new Date().toISOString(),
                  }
                : section === "shadows"
                  ? {
                      task_id: val("task"),
                      sample_rate: num("ratio"),
                      max_parallel: num("parallel"),
                      timeout_s: num("timeout"),
                    }
                  : {
                      control_id: val("control"),
                      candidate_id: val("candidate"),
                      ratio: num("ratio"),
                      max_sessions: num("cap"),
                      starts_at: new Date(val("start")).toISOString(),
                      ends_at: new Date(val("end")).toISOString(),
                    };
            void run(() =>
              call(
                base +
                  "/" +
                  {
                    inspections: "inspection-plans",
                    shadows: "shadow-plans",
                    experiments: "experiments",
                  }[section as keyof typeof urls],
                "POST",
                { ...spec, environment },
              ),
            );
          }}
        >
          {section !== "experiments" && (
            <label>
              已核准的只读或隔离任务
              <select name="task" required defaultValue="">
                <option value="">选择任务</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {section === "inspections" && (
            <>
              <label>
                每隔多少秒执行
                <input
                  name="period"
                  required
                  type="number"
                  min="60"
                  max="31536000"
                />
              </label>
              <label>
                最低通过率（0 至 1）
                <input
                  name="threshold"
                  type="number"
                  min="0"
                  max="1"
                  step="any"
                  required
                />
              </label>
            </>
          )}
          {section === "shadows" && (
            <>
              <label>
                采样比例（0 至 1）
                <input
                  name="ratio"
                  type="number"
                  min="0"
                  max="1"
                  step="any"
                  required
                />
              </label>
              <label>
                同时运行上限
                <input
                  name="parallel"
                  type="number"
                  min="1"
                  max="128"
                  required
                />
              </label>
              <label>
                单次超时秒数
                <input
                  name="timeout"
                  type="number"
                  min="1"
                  max="3600"
                  required
                />
              </label>
            </>
          )}
          {section === "experiments" && (
            <>
              {(["control", "candidate"] as const).map((key) => (
                <label key={key}>
                  {key === "control" ? "对照版本" : "候选版本"}
                  <select name={key} defaultValue="" required>
                    <option value="">选择版本</option>
                    {versions.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.label}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
              <label>
                候选比例（大于 0 且小于 1）
                <input
                  name="ratio"
                  type="number"
                  min="0.000001"
                  max="0.999999"
                  step="any"
                  required
                />
              </label>
              <label>
                会话上限
                <input
                  name="cap"
                  type="number"
                  min="1"
                  max="10000000"
                  required
                />
              </label>
              <label>
                开始时间
                <input name="start" type="datetime-local" required />
              </label>
              <label>
                结束时间
                <input name="end" type="datetime-local" required />
              </label>
            </>
          )}
          <button disabled={busy || !project || !environment}>
            创建{groups[section]}
          </button>
        </form>
      )}
      {data &&
        (data[section].length === 0 ? (
          <p>此环境暂无{groups[section]}记录。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>记录</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {data[section].map((item) => (
                <tr key={item.id}>
                  <td>{item.agent_run_id ?? item.id}</td>
                  <td>
                    {item.gate ??
                      item.completeness ??
                      (item.enabled === undefined
                        ? "运行中"
                        : item.enabled
                          ? "启用"
                          : "已停止")}
                  </td>
                  <td>
                    <button
                      disabled={busy}
                      onClick={() =>
                        void run(() =>
                          call(
                            section === "batches"
                              ? base + "/regression-batches/" + item.id
                              : section === "traces"
                                ? base +
                                  "/production-traces/" +
                                  item.id +
                                  "/timeline"
                                : base +
                                  "/online/" +
                                  urls[section] +
                                  "/" +
                                  item.id,
                          ),
                        )
                      }
                    >
                      查看详情
                    </button>
                    {section in urls && (
                      <button
                        disabled={busy}
                        onClick={() =>
                          void run(() =>
                            call(
                              base +
                                "/online/" +
                                urls[section as keyof typeof urls] +
                                "/" +
                                item.id,
                              "PATCH",
                              { enabled: !item.enabled },
                            ),
                          )
                        }
                      >
                        {item.enabled ? "停止" : "启用"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      <Result
        details={details}
        more={(suffix) => void run(() => call(base + suffix))}
      />
    </section>
  );
}
