import { useCallback, useEffect, useRef, useState } from "react";
import { all, base, date, labels, pretty } from "./evaluation/api";
import type { Call, Run, Task } from "./evaluation/api";
import TaskWizard from "./evaluation/TaskWizard";
import RunDetail from "./evaluation/RunDetail";

function TaskPanel({
  taskId,
  token,
  call,
  write,
  saved,
}: {
  taskId: string;
  token: string;
  call: Call;
  write: boolean;
  saved: () => void;
}) {
  const path = base + "/tasks/" + taskId;
  const [task, setTask] = useState<Task | null>(null),
    [runs, setRuns] = useState<Run[]>([]),
    [current, setCurrent] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const key = useRef("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    Promise.all([call<Task>(path), all<Run>(call, path + "/runs")])
      .then(([t, rs]) => {
        if (live) {
          setTask(t);
          setRuns(rs);
          setCurrent((old) => old || rs[0]?.id || "");
        }
      })
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [call, path, revision]);
  const onStatus = useCallback(
    (id: string, status: string) =>
      setRuns((prior) =>
        prior.map((r) =>
          r.id === id && r.status !== status ? { ...r, status } : r,
        ),
      ),
    [],
  );
  async function act(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    key.current ||= crypto.randomUUID();
    const run = await call<Run>(
      path + "/runs",
      "POST",
      {},
      { "Idempotency-Key": key.current },
    );
    key.current = "";
    setCurrent(run.id);
    setRevision((v) => v + 1);
  }
  return (
    <div>
      {error && <p role="alert">{error}</p>}
      {task && (
        <>
          <section>
            <div className="eval-heading">
              <h3>{task.name}</h3>
              <span>{task.archived_at ? "已归档" : "可运行"}</span>
            </div>
            <p>{task.description}</p>
            <p>
              项目版本 {task.snapshot.project_version.label} · 数据集 v
              {task.snapshot.dataset_version.version} ·{" "}
              {task.snapshot.evaluator_plan.length} 个评分节点
            </p>
            <details>
              <summary>查看冻结配置</summary>
              <pre>{pretty(task.snapshot)}</pre>
              <small>配置摘要 {task.config_digest}</small>
            </details>
            {write && !task.archived_at && (
              <>
                <div className="row">
                  <button disabled={busy} onClick={() => act(start)}>
                    启动运行
                  </button>
                  <button
                    disabled={busy}
                    onClick={() =>
                      act(async () => {
                        await call(path, "DELETE");
                        setRevision((x) => x + 1);
                        saved();
                      })
                    }
                  >
                    归档任务
                  </button>
                </div>
                <details>
                  <summary>编辑名称与描述</summary>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      const d = new FormData(e.currentTarget);
                      void act(async () => {
                        await call(path, "PATCH", {
                          name: d.get("name"),
                          description: d.get("description"),
                        });
                        setTask(await call<Task>(path));
                        saved();
                      });
                    }}
                  >
                    <label>
                      任务名称
                      <input
                        name="name"
                        defaultValue={task.name}
                        required
                        maxLength={160}
                      />
                    </label>
                    <label>
                      描述
                      <textarea
                        name="description"
                        defaultValue={task.description ?? ""}
                        maxLength={4000}
                      />
                    </label>
                    <button disabled={busy}>保存说明</button>
                  </form>
                </details>
              </>
            )}
            <h4>运行记录</h4>
            {!runs.length ? (
              <p>尚未启动运行。</p>
            ) : (
              <label>
                选择运行
                <select
                  value={current}
                  onChange={(e) => setCurrent(e.target.value)}
                >
                  {runs.map((r) => (
                    <option key={r.id} value={r.id}>
                      {date(r.created_at)} · {labels[r.status]} ·{" "}
                      {r.id.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </section>
          {current && (
            <RunDetail
              key={current}
              id={current}
              token={token}
              call={call}
              write={write}
              otherRuns={runs.map(r=>r.id)}
              onStatus={onStatus}
            />
          )}
        </>
      )}
    </div>
  );
}

function ProjectTasks({
  project,
  call,
  token,
}: {
  project: string;
  call: Call;
  token: string;
}) {
  const [items, setItems] = useState<Task[]>([]),
    [id, setId] = useState(""),
    [write, setWrite] = useState(false),
    [wizard, setWizard] = useState(false);
  const [revision, setRevision] = useState(0),
    [error, setError] = useState(""),
    [archive, setArchive] = useState("all");
  useEffect(() => {
    let live = true;
    Promise.all([
      all<Task>(
        call,
        base +
          "/tasks?project_id=" +
          project +
          (archive === "all" ? "" : "&archived=" + archive),
      ),
      call<{ role: string }>("/projects/" + project + "/permissions"),
    ])
      .then(([tasks, p]) => {
        if (live) {
          setItems(tasks);
          setWrite(["W", "A"].includes(p.role));
          setId((old) =>
            tasks.some((t) => t.id === old) ? old : (tasks[0]?.id ?? ""),
          );
        }
      })
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [project, call, revision, archive]);
  return (
    <div className="layout">
      {error && <p role="alert">{error}</p>}
      <aside>
        <h3>评测任务</h3>
        <label>
          任务状态
          <select value={archive} onChange={(e) => setArchive(e.target.value)}>
            <option value="all">全部任务</option>
            <option value="false">可运行</option>
            <option value="true">已归档</option>
          </select>
        </label>
        {write && (
          <button className="eval-create" onClick={() => setWizard(true)}>
            新建评测任务
          </button>
        )}
        {items.map((t) => (
          <button
            className="project"
            aria-pressed={!wizard && t.id === id}
            key={t.id}
            onClick={() => {
              setWizard(false);
              setId(t.id);
            }}
          >
            {t.name}
            {t.archived_at ? "（已归档）" : ""}
          </button>
        ))}
      </aside>
      {wizard ? (
        <TaskWizard
          project={project}
          call={call}
          close={() => setWizard(false)}
          created={(t) => {
            setArchive("all");
            setId(t.id);
            setWizard(false);
            setRevision((v) => v + 1);
          }}
        />
      ) : id ? (
        <TaskPanel
          key={id}
          call={call}
          token={token}
          taskId={id}
          write={write}
          saved={() => setRevision((v) => v + 1)}
        />
      ) : (
        <section>当前项目还没有符合条件的评测任务。</section>
      )}
    </div>
  );
}

export default function Evaluations({
  token,
  projects,
  call,
}: {
  token: string;
  projects: { id: string; name?: string }[];
  call: Call;
}) {
  const [selection, setSelection] = useState("");
  const project = selection || projects[0]?.id || "";
  return (
    <div className="eval">
      <label>
        评测项目
        <select value={project} onChange={(e) => setSelection(e.target.value)}>
          {projects.map((p) => (
            <option value={p.id} key={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      {project ? (
        <ProjectTasks
          key={project}
          project={project}
          token={token}
          call={call}
        />
      ) : (
        <p>暂无可访问的项目。</p>
      )}
    </div>
  );
}
