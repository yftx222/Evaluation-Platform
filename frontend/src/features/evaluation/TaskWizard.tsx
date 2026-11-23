import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { all, base, dagLines } from "./api";
import type { Call, Snapshot, Task } from "./api";
type Choice = {
  id: string;
  name: string;
  enabled?: boolean;
  archived?: boolean;
};
type Version = {
  id: string;
  label?: string;
  version?: string | number;
  sample_count?: number;
};
type Fields = {
  name: string;
  project_version_id: string;
  dataset_version_id: string;
  evaluator_version_ids: string[];
  policy: Record<string, number>;
};

export default function TaskWizard({
  project,
  call,
  created,
  close,
}: {
  project: string;
  call: Call;
  created: (t: Task) => void;
  close: () => void;
}) {
  const [projects, setProjects] = useState<Version[]>([]),
    [datasets, setDatasets] = useState<Choice[]>([]),
    [evaluators, setEvaluators] = useState<Choice[]>([]);
  const [ds, setDs] = useState(""),
    [ev, setEv] = useState(""),
    [dv, setDv] = useState<Version[]>([]),
    [versions, setVersions] = useState<Version[]>([]);
  const [chosen, setChosen] = useState<Record<string, string>>({}),
    [preview, setPreview] = useState<Snapshot | null>(null),
    [payload, setPayload] = useState<Fields | null>(null);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    Promise.all([
      all<Version>(call, "/projects/" + project + "/versions"),
      all<Choice>(call, "/datasets"),
      all<Choice>(call, "/evaluators"),
    ])
      .then(([p, d, e]) => {
        if (live) {
          setProjects(p);
          setDatasets(d.filter((x) => !x.archived));
          setEvaluators(e.filter((x) => x.enabled));
        }
      })
      .catch((e) => live && setError(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [call, project]);
  useEffect(() => {
    let live = true;
    setDv([]);
    if (ds)
      all<Version>(call, "/datasets/" + ds + "/versions?state=PUBLISHED")
        .then((v) => live && setDv(v))
        .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [ds, call]);
  useEffect(() => {
    let live = true;
    setVersions([]);
    if (ev)
      all<Version>(call, "/evaluators/" + ev + "/versions")
        .then((v) => live && setVersions(v))
        .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [ev, call]);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      if (preview && payload) {
        created(await call<Task>(base + "/tasks", "POST", payload));
        return;
      }
      const data = new FormData(e.currentTarget);
      const policy: Record<string, number> = {};
      for (const [key, value] of data.entries())
        if (key.startsWith("p.") && value !== "")
          policy[key.slice(2)] = Number(value);
      const body = {
        name: String(data.get("name")).trim(),
        project_version_id: String(data.get("pv")),
        dataset_version_id: String(data.get("dv")),
        evaluator_version_ids: Object.keys(chosen),
        policy,
      };
      if (policy.initial_backoff_ms > policy.max_backoff_ms)
        throw new Error("首次退避不能超过最大退避时间");
      setPreview(await call<Snapshot>(base + "/tasks/preview", "POST", body));
      setPayload(body);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit}>
      <div className="eval-heading">
        <h3>{preview ? "确认冻结配置" : "创建评测任务"}</h3>
        <button type="button" onClick={close}>
          关闭
        </button>
      </div>
      <fieldset hidden={!!preview} disabled={!!preview || busy || loading}>
        <label>
          任务名称
          <input
            name="name"
            required
            maxLength={160}
            placeholder="例如：客服回答质量回归"
          />
        </label>
        <div className="fields">
          <label>
            项目版本
            <select aria-label="项目版本" name="pv" required defaultValue="">
              <option value="">选择已发布版本</option>
              {projects.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            数据集
            <select
              aria-label="数据集"
              value={ds}
              onChange={(e) => {
                setDv([]);
                setDs(e.target.value);
              }}
              required
            >
              <option value="">选择数据集</option>
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            数据集发布版本
            <select aria-label="数据集发布版本" name="dv" key={ds} defaultValue="" required>
              <option value="">选择发布版本</option>
              {dv.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version} · {v.sample_count} 条
                </option>
              ))}
            </select>
          </label>
        </div>
        <label>
          评分器目录
          <select
            aria-label="评分器目录"
            value={ev}
            onChange={(e) => {
              setVersions([]);
              setEv(e.target.value);
            }}
          >
            <option value="">选择评分器，可跨目录多选</option>
            {evaluators.map((x) => (
              <option value={x.id} key={x.id}>
                {x.name}
              </option>
            ))}
          </select>
        </label>
        {versions.map((v) => (
          <label className="eval-check" key={v.id}>
            <input
              type="checkbox"
              checked={v.id in chosen}
              onChange={(e) => {
                const next = { ...chosen };
                if (e.target.checked)
                  next[v.id] =
                    evaluators.find((x) => x.id === ev)?.name + " " + v.version;
                else delete next[v.id];
                setChosen(next);
              }}
            />
            {v.version}
          </label>
        ))}
        <p>已选评分版本：{Object.values(chosen).join("、") || "尚未选择"}</p>
        <div className="fields">
          {(
            [
              ["max_parallel", "同时执行样本数", 1, 1024, 1],
              ["case_timeout_s", "每条样本时限（秒）", 1, 86400, 120],
              ["adapter_max_attempts", "目标最多尝试次数", 1, 10, 1],
              ["evaluator_max_attempts", "评分最多尝试次数", 1, 10, 1],
              ["initial_backoff_ms", "首次退避（毫秒）", 1, 60000, 500],
              ["max_backoff_ms", "最大退避（毫秒）", 1, 3600000, 10000],
            ] as const
          ).map(([key, label, min, max, value]) => (
            <label key={key}>
              {label}
              <input
                name={"p." + key}
                required
                type="number"
                min={min}
                max={max}
                defaultValue={value}
              />
            </label>
          ))}
        </div>
      </fieldset>
      {preview && (
        <>
          <p>
            {payload?.name} · {preview.project_version.label} · 数据集 v
            {preview.dataset_version.version} · {preview.adapter.kind}/
            {preview.adapter.build}
          </p>
          <h4>评分依赖与权重</h4>
          <ul>
            {dagLines(preview).map((line, i) => (
              <li key={preview.evaluator_plan[i].id}>{line}</li>
            ))}
          </ul>
          <details>
            <summary>执行策略</summary>
            <pre>{JSON.stringify(preview.policy, null, 2)}</pre>
          </details>
          <button
            type="button"
            disabled={busy}
            onClick={() => setPreview(null)}
          >
            返回修改
          </button>
        </>
      )}
      <button disabled={loading || busy || !Object.keys(chosen).length}>
        {busy ? "处理中…" : preview ? "确认创建" : "预览评分计划"}
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
