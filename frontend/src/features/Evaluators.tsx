import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";

type Call = <T>(url: string, method?: string, body?: unknown) => Promise<T>;
type Entry = {
  id: string;
  name: string;
  slug: string;
  kind: string;
  enabled: boolean;
  latest_version: string | null;
};
type Plugin = {
  impl_key: string;
  build: string;
  kind: string;
  example: object;
  requirements: object;
  config_schema: object;
};
type Version = {
  id: string;
  version: string;
  digest: string;
  impl_key: string;
  build: string;
  config: object;
  members: object[];
  prompt_text?: string;
  model_id?: string;
  rubric_version?: string;
  revision_unverified: boolean;
};
type Result = {
  status: string;
  score: number | null;
  passed: boolean | null;
  reason: string;
  details: object;
  evidence: string[];
  evaluator_version: string;
};
const kinds: Record<string, string> = {
  rule: "规则",
  code: "可信代码",
  business: "业务",
  composite: "组合",
  llm_judge: "LLM 裁判",
};
const pretty = (v: unknown) => JSON.stringify(v, null, 2);
function obj(s: string, label: string) {
  try {
    const x = JSON.parse(s);
    if (x !== null && !Array.isArray(x) && typeof x === "object") return x;
  } catch {
    /* show the field label below */
  }
  throw Error(label + "需要 JSON 对象，例如 {}");
}
async function pages<T>(call: Call, path: string) {
  const items: T[] = [];
  let cursor: string | null = null;
  do {
    const data: { items: T[]; next_cursor: string | null } = await call(
      path + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""),
    );
    items.push(...data.items);
    cursor = data.next_cursor;
  } while (cursor);
  return items;
}
function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="dataset-field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
const sample = {
  case_key: "arithmetic_01",
  input: { query: "2 + 2 等于多少？" },
  expected: { answer: "4" },
  extensions: { policy_id: "basic" },
};
const samplePrediction = {
  answer: "4",
  structured_output: { value: 4, policy_id: "basic" },
};

export default function Evaluators({
  call,
  canEdit,
}: {
  call: Call;
  canEdit: boolean;
}) {
  const [entries, setEntries] = useState<Entry[]>([]),
    [plugins, setPlugins] = useState<Plugin[]>([]),
    [selected, setSelected] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  const root = "/evaluators";
  async function refresh() {
    setEntries(await pages<Entry>(call, root));
  }
  useEffect(() => {
    Promise.all([
      refresh(),
      call<Plugin[]>(root + "/implementations").then(setPlugins),
    ]).catch((e) => setMessage(String(e)));
  }, []);
  const current = entries.find((x) => x.id === selected);
  async function create(ev: FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    const data = new FormData(ev.currentTarget);
    setBusy(true);
    setMessage("");
    try {
      const e = await call<Entry>(root, "POST", Object.fromEntries(data));
      await refresh();
      setSelected(e.id);
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="layout">
      <aside>
        <h2>评分器管理</h2>
        {canEdit && (
          <button
            onClick={() => {
              setSelected("");
              setMessage("");
            }}
          >
            新建评分器
          </button>
        )}
        {entries.map((e) => (
          <button
            className="project"
            key={e.id}
            onClick={() => {
              setSelected(e.id);
              setMessage("");
            }}
          >
            {e.name} · {kinds[e.kind]}
            <br />
            <small>
              {e.enabled ? "已启用" : "已停用"} ·{" "}
              {e.latest_version || "尚未发布"}
            </small>
          </button>
        ))}
      </aside>
      <div>
        <output role="alert">{message}</output>
        {current ? (
          <Detail
            key={current.id}
            entry={current}
            entries={entries}
            plugins={plugins.filter((p) => p.kind === current.kind)}
            canEdit={canEdit}
            call={call}
            refresh={refresh}
          />
        ) : canEdit ? (
          <form onSubmit={create}>
            <h2>创建评分器</h2>
            <Field
              label="评分器标识"
              hint="以小写英文字母开头，最多 64 位，可用数字、下划线和短横线。"
            >
              <input
                name="slug"
                required
                pattern="[a-z][a-z0-9_\-]{0,63}"
                placeholder="answer_quality"
              />
            </Field>
            <Field
              label="评分器名称"
              hint="1 至 160 个字，例如「回答完整性」。"
            >
              <input
                name="name"
                required
                maxLength={160}
                pattern=".*\S.*"
                placeholder="回答完整性"
              />
            </Field>
            <Field label="评分器类型">
              <select name="kind">
                {Array.from(new Set(plugins.map((p) => p.kind))).map((k) => (
                  <option key={k} value={k}>
                    {kinds[k]}
                  </option>
                ))}
              </select>
            </Field>
            <button disabled={busy || !plugins.length}>创建评分器</button>
          </form>
        ) : (
          <p>请选择要查看的评分器。</p>
        )}
      </div>
    </div>
  );
}

function Detail({
  entry,
  entries,
  plugins,
  canEdit,
  call,
  refresh,
}: {
  entry: Entry;
  entries: Entry[];
  plugins: Plugin[];
  canEdit: boolean;
  call: Call;
  refresh: () => Promise<void>;
}) {
  const path = "/evaluators/" + entry.id;
  const [versions, setVersions] = useState<Version[]>([]),
    [chosen, setChosen] = useState(""),
    [other, setOther] = useState(""),
    [impl, setImpl] = useState(""),
    [config, setConfig] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [results, setResults] = useState<Result[]>([]);
  const [children, setChildren] = useState<{ id: string; weight: number }[]>(
      [],
    ),
    [available, setAvailable] = useState<{ id: string; name: string }[]>([]);
  const [caseText, setCase] = useState(pretty(sample)),
    [pred, setPred] = useState(pretty(samplePrediction)),
    [materials, setMaterials] = useState(
      pretty({ approved_policy_ids: ["basic"] }),
    );
  const plugin = plugins.find((p) => p.impl_key === impl);
  async function load() {
    const rows = await pages<Version>(call, path + "/versions");
    setVersions(rows);
    setChosen(rows[0]?.id || "");
  }
  useEffect(() => {
    load().catch((e) => setMessage(String(e)));
  }, [path]);
  useEffect(() => {
    if (!impl && plugins.length) {
      setImpl(plugins[0].impl_key);
      setConfig(pretty(plugins[0].example));
    }
  }, [plugins, impl]);
  useEffect(() => {
    let live = true;
    if (entry.kind === "composite") {
      Promise.all(
        entries
          .filter((x) => x.enabled && x.latest_version)
          .map(async (e) =>
            (
              await pages<Version>(call, "/evaluators/" + e.id + "/versions")
            ).map((v) => ({ id: v.id, name: e.name + " / " + v.version })),
          ),
      )
        .then((lists) => {
          if (live) setAvailable(lists.flat());
        })
        .catch((e) => {
          if (live) setMessage(String(e));
        });
    }
    return () => {
      live = false;
    };
  }, [entries, entry.kind]);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  }
  function publish(ev: FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    const d = new FormData(ev.currentTarget);
    void act(async () => {
      if (!plugin) throw Error("请选择已部署的实现");
      const body: Record<string, unknown> = {
        version: d.get("version"),
        impl_key: impl,
        build: plugin.build,
        config: obj(config, "实现配置"),
      };
      if (entry.kind === "llm_judge")
        Object.assign(body, {
          provider: "openai_compatible",
          prompt_text: d.get("prompt"),
          prompt_version: d.get("pv"),
          model_id: d.get("model"),
          model_revision: d.get("revision") || null,
          rubric_version: d.get("rubric"),
        });
      if (entry.kind === "composite") {
        if (!children.length) throw Error("请至少添加一个子版本");
        body.members = children.map((c, i) => ({
          position: i,
          child_version_id: c.id,
          weight: c.weight,
        }));
      }
      await call(path + "/versions", "POST", body);
      await load();
      await refresh();
      setMessage("版本已发布");
    });
  }
  function options() {
    return versions.map((v) => (
      <option key={v.id} value={v.id}>
        {v.version}
      </option>
    ));
  }
  function score(ids: string[]) {
    void act(async () => {
      const input = {
        case: obj(caseText, "Case"),
        prediction: obj(pred, "Prediction"),
        materials: obj(materials, "只读材料"),
      };
      setResults([]);
      for (const id of ids) {
        const r = await call<Result>(path + "/test", "POST", {
          version_id: id,
          ...input,
        });
        setResults((prior) => [...prior, r]);
      }
    });
  }
  const left = versions.find((v) => v.id === chosen),
    right = versions.find((v) => v.id === other);
  return (
    <>
      <section>
        <h2>{entry.name}</h2>
        <p>
          {kinds[entry.kind]} · {entry.slug} ·{" "}
          {entry.enabled ? "已启用" : "已停用"}
        </p>
        {canEdit && (
          <form
            onSubmit={(ev) => {
              ev.preventDefault();
              const d = new FormData(ev.currentTarget);
              void act(async () => {
                await call(path, "PATCH", {
                  name: d.get("name"),
                  enabled: d.get("enabled") === "on",
                });
                await refresh();
              });
            }}
          >
            <Field label="显示名称">
              <input
                name="name"
                required
                maxLength={160}
                defaultValue={entry.name}
              />
            </Field>
            <label>
              <input
                type="checkbox"
                name="enabled"
                defaultChecked={entry.enabled}
              />
              启用评分器
            </label>
            <button disabled={busy}>保存目录设置</button>
          </form>
        )}
        <output aria-live="polite">{message}</output>
      </section>
      {canEdit && entry.enabled && (
        <form onSubmit={publish}>
          <h3>发布评分器版本</h3>
          <Field label="版本号" hint="每次发布使用新编号，例如 v1、v2。">
            <input name="version" required placeholder="v1" maxLength={128} />
          </Field>
          <Field label="可信实现">
            <select
              value={impl}
              onChange={(ev) => {
                const p = plugins.find((x) => x.impl_key === ev.target.value)!;
                setImpl(p.impl_key);
                setConfig(pretty(p.example));
              }}
            >
              {plugins.map((p) => (
                <option key={p.impl_key}>{p.impl_key}</option>
              ))}
            </select>
          </Field>
          {plugin && (
            <>
              <Field
                label="实现配置（JSON 对象）"
                hint="已填入可编辑示例。threshold 为 0 到 1 的数字；字段规则可在下方展开。"
              >
                <textarea
                  required
                  rows={7}
                  value={config}
                  onChange={(e) => setConfig(e.target.value)}
                />
              </Field>
              <details>
                <summary>配置字段和输入要求</summary>
                <pre>
                  {pretty({
                    要求: plugin.requirements,
                    配置: plugin.config_schema,
                  })}
                </pre>
              </details>
            </>
          )}
          {entry.kind === "llm_judge" && (
            <>
              <Field label="模型标识" hint="填部署账户允许调用的模型名。">
                <input required name="model" defaultValue="deepseek-flash" />
              </Field>
              <Field
                label="模型修订号（可选）"
                hint="供应商未提供时留空，结果将标为无法严格比较。"
              >
                <input name="revision" />
              </Field>
              <Field label="系统评分说明">
                <textarea
                  required
                  name="prompt"
                  defaultValue="核对回答与参考答案的事实一致性。正确给 1 分，错误给 0 分。"
                />
              </Field>
              <Field label="提示词版本">
                <input required name="pv" defaultValue="p1" />
              </Field>
              <Field label="量表版本">
                <input required name="rubric" defaultValue="r1" />
              </Field>
            </>
          )}
          {entry.kind === "composite" && (
            <fieldset>
              <legend>子评分器（按添加顺序）</legend>
              <select
                aria-label="添加子版本"
                value=""
                onChange={(ev) => {
                  if (ev.target.value)
                    setChildren([
                      ...children,
                      { id: ev.target.value, weight: 1 },
                    ]);
                }}
              >
                <option value="">请选择已发布版本</option>
                {available
                  .filter((v) => !children.some((c) => c.id === v.id))
                  .map((v) => (
                    <option value={v.id} key={v.id}>
                      {v.name}
                    </option>
                  ))}
              </select>
              {children.map((c, i) => (
                <div key={c.id}>
                  <label>
                    {available.find((x) => x.id === c.id)?.name} 权重
                    <input
                      type="number"
                      required
                      min="0.000001"
                      max="10000"
                      step="any"
                      value={c.weight}
                      onChange={(ev) =>
                        setChildren(
                          children.map((v, j) =>
                            j === i
                              ? { ...v, weight: Number(ev.target.value) }
                              : v,
                          ),
                        )
                      }
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() =>
                      setChildren(children.filter((v) => v.id !== c.id))
                    }
                  >
                    移除
                  </button>
                </div>
              ))}
            </fieldset>
          )}
          <button disabled={busy || !plugin}>发布版本</button>
        </form>
      )}
      {left && (
        <section>
          <h3>版本与试评</h3>
          <Field label="试评版本">
            <select
              value={chosen}
              onChange={(e) => {
                setChosen(e.target.value);
                setResults([]);
              }}
            >
              {options()}
            </select>
          </Field>
          <p>
            实现 {left.impl_key}
            <br />
            构建 <code>{left.build}</code>
            <br />
            版本摘要 <code>{left.digest}</code>
            {left.revision_unverified && <strong> · 模型修订未确认</strong>}
          </p>
          <Field label="对比版本（可选）">
            <select
              value={other}
              onChange={(e) => {
                setOther(e.target.value);
                setResults([]);
              }}
            >
              <option value="">选择另一个版本</option>
              {options()}
            </select>
          </Field>
          {right && (
            <details open>
              <summary>
                版本差异 · {left.version} / {right.version}
              </summary>
              <pre>
                {pretty(
                  Object.fromEntries(
                    Object.keys(left)
                      .filter(
                        (k) =>
                          !["id", "version", "created_at", "digest"].includes(
                            k,
                          ) &&
                          pretty(left[k as keyof Version]) !==
                            pretty(right[k as keyof Version]),
                      )
                      .map((k) => [
                        k,
                        [left[k as keyof Version], right[k as keyof Version]],
                      ]),
                  ),
                )}
              </pre>
            </details>
          )}
          {canEdit && (
            <>
              <Field label="Case（JSON 对象）">
                <textarea
                  rows={7}
                  value={caseText}
                  onChange={(e) => setCase(e.target.value)}
                />
              </Field>
              <Field
                label="Prediction（JSON 对象）"
                hint="填写已经取得的回答，同一输入可重复试评。"
              >
                <textarea
                  rows={5}
                  value={pred}
                  onChange={(e) => setPred(e.target.value)}
                />
              </Field>
              <Field label="只读材料（JSON 对象）">
                <textarea
                  rows={3}
                  value={materials}
                  onChange={(e) => setMaterials(e.target.value)}
                />
              </Field>
              <button disabled={busy} onClick={() => score([chosen])}>
                试评一次
              </button>{" "}
              {right && right.id !== chosen && (
                <button disabled={busy} onClick={() => score([chosen, other])}>
                  用同一预测对比两个版本
                </button>
              )}
              {results.map((r, i) => (
                <article key={i}>
                  <h4>
                    {r.evaluator_version} · {r.status}
                  </h4>
                  <p>
                    得分 {r.score ?? "—"} ·{" "}
                    {r.passed === null
                      ? "无通过结论"
                      : r.passed
                        ? "通过"
                        : "未通过"}
                  </p>
                  <p>{r.reason}</p>
                  <pre>
                    {pretty({ details: r.details, evidence: r.evidence })}
                  </pre>
                </article>
              ))}
            </>
          )}
        </section>
      )}
    </>
  );
}
