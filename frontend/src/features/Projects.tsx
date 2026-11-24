import { useEffect, useState, useMemo } from "react";
import Evaluations from "./Evaluations";
import Datasets from "./Datasets";
import Evaluators from "./Evaluators";
import type { FormEvent, ReactNode } from "react";
import { Button } from "antd";

type Row = {
  id: string;
  name?: string;
  slug?: string;
  kind?: string;
  enabled?: boolean;
  max_parallel?: number;
  target_max_parallel?: number;
  rpm?: number;
  issuer?: string;
  subject?: string;
  role?: string;
  label?: string;
  deployment_ref?: string;
  build?: string;
  trace_capabilities?: Record<string,unknown> | null;
  environment?: Record<string,unknown> | null;
  prefix?: string;
  revoked_at?: string;
  expires_at?: string;
  scopes?: string[];
};
type Page<T> = { items: T[]; next_cursor: string | null };
type Call = <T>(
  url: string,
  method?: string,
  body?: unknown,
  headers?: Record<string, string>,
) => Promise<T>;
function client(token: string): Call {
  return async <T,>(
    url: string,
    method = "GET",
    body?: unknown,
    headers?: Record<string, string>,
  ) => {
    const res = await fetch("/api/v1" + url, {
      method,
      headers: {
        ...headers,
        Authorization: "Bearer " + token,
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 204) return undefined as T;
    const payload = await res.json();
    if (
      res.status === 504 &&
      url.endsWith("/test") &&
      payload.data?.status === "ERROR"
    )
      return payload.data as T;
    if (!res.ok)
      throw Object.assign(new Error(
        payload.error?.message +
          " [" +
          (payload.error?.code ?? res.status) +
          "]",
      ), { status: res.status });
    return payload.data as T;
  };
}
async function all(call: Call, url: string) {
  const rows: Row[] = [];
  let cursor: string | null = null;
  do {
    const page: Page<Row> = await call(
      url + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""),
    );
    rows.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return rows;
}
const val = (data: FormData, key: string) => String(data.get(key) ?? "").trim();
function Input({
  name,
  label,
  initial = "",
  type = "text",
  optional = false,
}: {
  name: string;
  label: string;
  initial?: string | number;
  type?: string;
  optional?: boolean;
}) {
  return (
    <label>
      {label}
      <input
        name={name}
        defaultValue={initial}
        type={type}
        required={!optional}
        min={type === "number" ? 1 : undefined}
      />
    </label>
  );
}
function Editor({
  title,
  submit,
  children,
  save = "保存",
  disabled = false,
}: {
  title: string;
  submit: (data: FormData) => Promise<void>;
  children: ReactNode;
  save?: string;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  async function send(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (disabled || busy) return;
    const data = new FormData(e.currentTarget);
    setBusy(true);
    setMessage("");
    try {
      await submit(data);
      setMessage("已完成");
    } catch (err) {
      setMessage(String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={send}>
      <h3>{title}</h3>
      <div className="fields">{children}</div>
      <Button htmlType="submit" loading={busy} disabled={disabled}>
        {save}
      </Button>
      <output>{message}</output>
    </form>
  );
}
function Limits({ project }: { project?: Row }) {
  return (
    <>
      <Input name="name" label="项目名称" initial={project?.name} />
      <Input
        type="number"
        name="max_parallel"
        label="Case 并发"
        initial={project?.max_parallel ?? 2}
      />
      <Input
        type="number"
        name="target_max_parallel"
        label="目标并发"
        initial={project?.target_max_parallel ?? 2}
      />
      <Input
        type="number"
        name="rpm"
        label="每分钟请求"
        initial={project?.rpm ?? 60}
      />
    </>
  );
}
function limits(d: FormData) {
  return {
    name: val(d, "name"),
    max_parallel: Number(d.get("max_parallel")),
    target_max_parallel: Number(d.get("target_max_parallel")),
    rpm: Number(d.get("rpm")),
  };
}
function roles(initial = "R") {
  return (
    <label>
      项目角色
      <select name="role" defaultValue={initial}>
        <option>R</option>
        <option>W</option>
        <option>A</option>
      </select>
    </label>
  );
}
type AdapterPlugin = {
  kind: string;
  build: string;
  example: Record<string, unknown>;
  config_schema: object;
};
function jsonObject(
  data: FormData,
  key: string,
  label: string,
): Record<string, unknown> {
  try {
    const obj = JSON.parse(val(data, key));
    if (obj && typeof obj === "object" && !Array.isArray(obj)) return obj;
  } catch {
    /* report which field needs attention */
  }
  throw new Error(label + "需要 JSON 对象，例如 {}");
}

function Details({
  id,
  call,
  refresh,
}: {
  id: string;
  call: Call;
  refresh: () => Promise<void>;
}) {
  const path = "/projects/" + id;
  const [project, setProject] = useState<Row>(),
    [versions, setVersions] = useState<Row[]>([]),
    [members, setMembers] = useState<Row[]>([]),
    [keys, setKeys] = useState<Row[]>([]),
    [plugins, setPlugins] = useState<AdapterPlugin[]>([]),
    [pluginKey, setPluginKey] = useState(""),
    [registryError, setRegistryError] = useState(""),
    [registryAttempt, setRegistryAttempt] = useState(0),
    [admin, setAdmin] = useState(false),
    [secret, setSecret] = useState(""),
    [message, setMessage] = useState(""),
    [tab, setTab] = useState("配置");
  const plugin = plugins.find((p) => p.kind + ":" + p.build === pluginKey);
  async function load() {
    const [p, perm, v] = await Promise.all([
      call<Row>(path),
      call<{ role: string }>(path + "/permissions"),
      all(call, path + "/versions"),
    ]);
    setProject(p);
    setVersions(v);
    setAdmin(perm.role === "A");
    if (perm.role === "A") {
      const [m, k] = await Promise.all([
        all(call, path + "/members"),
        all(call, path + "/api-keys"),
      ]);
      setMembers(m);
      setKeys(k);
    }
  }
  useEffect(() => {
    load().catch((e) => setMessage(String(e)));
  }, [id]);
  useEffect(() => {
    if (!admin) return;
    let current = true;
    setRegistryError("");
    setPlugins([]);
    call<AdapterPlugin[]>("/adapters/implementations")
      .then((items) => {
        if (!current) return;
        if (!items.length) throw new Error("没有已部署的接入协议");
        setPlugins(items);
        setPluginKey(items[0].kind + ":" + items[0].build);
      })
      .catch((error) => {
        if (current) setRegistryError("接入协议加载失败：" + String(error));
      });
    return () => {
      current = false;
    };
  }, [admin, id, registryAttempt]);
  async function act(fn: () => Promise<unknown>) {
    try {
      await fn();
      await load();
      await refresh();
    } catch (e) {
      setMessage(String(e));
    }
  }
  if (!project) return <p role="status">{message || "正在读取项目…"}</p>;
  return (
    <section>
      <h2>{project.name}</h2>
      <p>
        {project.slug} · {project.kind} · {project.enabled ? "启用" : "停用"}
      </p>
      <nav>
        {(admin ? ["配置", "版本", "成员", "API Key"] : ["配置", "版本"]).map(
          (t) => (
            <Button
              type={tab === t ? "primary" : "default"}
              key={t}
              onClick={() => {
                setTab(t);
                setSecret("");
                setMessage("");
              }}
            >
              {t}
            </Button>
          ),
        )}
      </nav>
      <p role="status">{message}</p>
      {tab === "配置" &&
        (admin ? (
          <Editor
            title="项目设置"
            submit={async (d) => {
              await call(path, "PATCH", {
                ...limits(d),
                enabled: d.has("enabled"),
              });
              await load();
              await refresh();
            }}
          >
            <Limits project={project} />
            <label>
              <input
                type="checkbox"
                name="enabled"
                defaultChecked={project.enabled}
              />
              启用项目
            </label>
          </Editor>
        ) : (
          <p>
            Case 并发 {project.max_parallel}，目标并发{" "}
            {project.target_max_parallel}，每分钟 {project.rpm} 次。
          </p>
        ))}
      {tab === "版本" && (
        <>
          {admin && (
            <Editor
              title="发布接入版本"
              save="发布版本"
              disabled={!plugin}
              submit={async (d) => {
                const ref = val(d, "secret_ref"),
                  ver = val(d, "secret_version");
                if (!plugin) throw new Error("请选择已部署的接入协议");
                if (Boolean(ref) !== Boolean(ver))
                  throw new Error("密钥引用和密钥版本需要一起填写");
                const options = jsonObject(d, "options", "协议选项");
                if (plugin.kind === "openai_compatible")
                  options.model = val(d, "model");
                const capabilities = val(d,"trace_capabilities").trim();
                const created = await call<Row>(path + "/versions", "POST", {
                  label: val(d, "label"),
                  deployment_ref: val(d, "deployment_ref"),
                  ...(capabilities ? {trace_capabilities: jsonObject(d,"trace_capabilities","采集能力")} : {}),
                  adapter: {
                    kind: plugin.kind,
                    build: plugin.build,
                    options,
                    endpoint: val(d, "endpoint"),
                    probe_endpoint: val(d, "probe_endpoint"),
                    ...(plugin.kind === "http"
                      ? {
                          method: val(d, "method"),
                          request_map: jsonObject(d, "mapping", "请求映射"),
                          answer_path: val(d, "answer_path") || null,
                          structured_path: val(d, "structured_path") || null,
                        }
                      : {}),
                    headers: jsonObject(d, "headers", "请求头"),
                    secret_ref: ref || null,
                    secret_version: ver || null,
                  },
                });
                const latest = await all(call, path + "/versions");
                setVersions(latest);
                if (!latest.some((v) => v.id === created.id)) {
                  throw new Error(
                    "服务器已接受发布，但列表尚未显示新版本，请刷新后核对",
                  );
                }
              }}
            >
              {!plugin &&
                (registryError ? (
                  <div role="alert">
                    {registryError}
                    <button
                      type="button"
                      onClick={() => setRegistryAttempt((n) => n + 1)}
                    >
                      重试加载协议
                    </button>
                  </div>
                ) : (
                  <p role="status">正在加载接入协议…</p>
                ))}
              <label>
                接入协议与构建
                <select
                  disabled={!plugins.length}
                  value={pluginKey}
                  onChange={(e) => setPluginKey(e.target.value)}
                >
                  {plugins.map((p) => (
                    <option
                      key={p.kind + ":" + p.build}
                      value={p.kind + ":" + p.build}
                    >
                      {p.kind} · {p.build}
                    </option>
                  ))}
                </select>
              </label>
              <Input name="label" label="版本标签" />
              <Input name="deployment_ref" label="部署标识" />
              <Input name="endpoint" label="业务地址" type="url" />
              <Input name="probe_endpoint" label="只读探测地址" type="url" />
              {plugin?.kind === "http" && (
                <>
                  <label>
                    方法
                    <select name="method">
                      <option>POST</option>
                      <option>GET</option>
                    </select>
                  </label>
                  <Input
                    name="mapping"
                    label="请求映射 JSON"
                    initial={'{"query":"input.query"}'}
                  />
                  <Input
                    name="answer_path"
                    label="答案字段"
                    initial="answer"
                    optional
                  />
                  <Input name="structured_path" label="结构化字段" optional />
                </>
              )}
              {plugin?.kind === "openai_compatible" && (
                <label>
                  模型标识
                  <input
                    required
                    name="model"
                    placeholder="填写目标服务提供的模型名"
                  />
                </label>
              )}
              {plugin && (
                <label key={pluginKey}>
                  协议选项（JSON 对象）
                  <textarea
                    name="options"
                    required
                    defaultValue={JSON.stringify(
                      Object.fromEntries(
                        Object.entries(plugin.example).filter(
                          ([key]) =>
                            plugin.kind !== "openai_compatible" ||
                            key !== "model",
                        ),
                      ),
                    )}
                  />
                  <small>
                    {plugin.kind === "dataagent"
                      ? "role 填专用账号绑定的 Doris 只读角色，fixture_version 填本次使用的测试数据版本。"
                      : <>默认填写 <code>{"{}"}</code> 即可使用标准字段。自定义字段见下方说明。</>}
                  </small>
                </label>
              )}
              {plugin && (
                <details>
                  <summary>协议字段说明</summary>
                  <pre>{JSON.stringify(plugin.config_schema, null, 2)}</pre>
                </details>
              )}
              {plugin?.kind === "dataagent" && plugin.build === "1" ? <p>DataAgent D1 关闭轨迹采集。</p> : <details>
                <summary>执行轨迹采集方式</summary>
                <label>采集能力（可选 JSON）
                  <textarea name="trace_capabilities" placeholder={'{"mode":"embedded","event_types":["MODEL_CALL","FINAL_OUTPUT"],"required_events":["FINAL_OUTPUT"]}'} />
                </label>
                <small>留空使用协议的回调模式。内嵌事件用 embedded，关闭用 disabled。能力随版本固定。</small>
              </details>}
              <Input name="headers" label="请求头 JSON" initial="{}" />
              <Input name="secret_ref" label="密钥引用" optional={plugin?.kind !== "dataagent"} />
              <Input name="secret_version" label="密钥版本" optional={plugin?.kind !== "dataagent"} />
            </Editor>
          )}
          <h3>已发布版本</h3>
          {versions.map((v) => (
            <div className="row" key={v.id}>
              <div>
                {v.label} · {v.deployment_ref} · {v.kind} / {v.build}
                {v.environment && <details><summary>发布时的评测环境</summary>
                  <pre>{JSON.stringify(v.environment,null,2)}</pre></details>}
                {v.trace_capabilities && <details><summary>已发布的采集能力</summary>
                  <pre>{JSON.stringify(v.trace_capabilities,null,2)}</pre></details>}
              </div>
              {admin && (
                <Button
                  onClick={() =>
                    act(async () => {
                      const r = await call<{
                        ok: boolean;
                        code: string;
                        latency_ms: number;
                      }>(path + "/test-connection", "POST", {
                        version_id: v.id,
                      });
                      setMessage(
                        r.ok
                          ? "连接成功 · " + r.latency_ms + " ms"
                          : "连接失败 · " + r.code,
                      );
                    })
                  }
                >
                  测试连接
                </Button>
              )}
            </div>
          ))}
        </>
      )}
      {tab === "成员" && admin && (
        <>
          <Editor
            title="添加项目成员"
            submit={async (d) => {
              await call(path + "/members", "POST", Object.fromEntries(d));
              await load();
            }}
          >
            <Input name="issuer" label="身份源" initial="development" />
            <Input name="subject" label="成员标识" />
            {roles()}
          </Editor>
          {members.map((m) => (
            <div key={m.id} className="row">
              <span>
                {m.issuer} / {m.subject}
              </span>
              <select
                aria-label={"角色 " + m.subject}
                value={m.role}
                onChange={(e) =>
                  act(() =>
                    call(path + "/members/" + m.id, "PATCH", {
                      role: e.target.value,
                    }),
                  )
                }
              >
                {["R", "W", "A"].map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
              <Button
                onClick={() =>
                  act(() => call(path + "/members/" + m.id, "DELETE"))
                }
              >
                移除
              </Button>
            </div>
          ))}
        </>
      )}
      {tab === "API Key" && admin && (
        <>
          <Editor
            title="创建项目 API Key"
            submit={async (d) => {
              const r = await call<{ secret: string }>(
                path + "/api-keys",
                "POST",
                {
                  name: val(d, "name"),
                  scopes: d.getAll("scopes"),
                  expires_at: new Date(val(d, "expires")).toISOString(),
                },
              );
              setSecret(r.secret);
              await load();
            }}
          >
            <Input name="name" label="凭证名称" />
            <Input name="expires" type="datetime-local" label="到期时间" />
            {["project:read", "results:read"].map((s) => (
              <label key={s}>
                <input name="scopes" value={s} type="checkbox" />
                {s}
              </label>
            ))}
          </Editor>
          {secret && (
            <p>
              本次凭证仅显示一次 <code>{secret}</code>{" "}
              <Button onClick={() => setSecret("")}>隐藏</Button>
            </p>
          )}
          {keys.map((k) => (
            <div key={k.id} className="row">
              <span>
                {k.name} · {k.prefix} ·{" "}
                {k.revoked_at
                  ? "已吊销"
                  : new Date(k.expires_at!).toLocaleString()}{" "}
                · {k.scopes?.join(", ")}
              </span>
              {!k.revoked_at && (
                <>
                  <Editor
                    title="轮换有效期"
                    save="轮换"
                    submit={async (d) => {
                      const r = await call<{ secret: string }>(
                        path + "/api-keys/" + k.id + "/rotate",
                        "POST",
                        {
                          expires_at: new Date(val(d, "expires")).toISOString(),
                        },
                      );
                      setSecret(r.secret);
                      await load();
                    }}
                  >
                    <Input
                      name="expires"
                      label="新到期时间"
                      type="datetime-local"
                    />
                  </Editor>
                  <Button
                    danger
                    onClick={() =>
                      act(async () => {
                        await call(path + "/api-keys/" + k.id, "DELETE");
                        setSecret("");
                      })
                    }
                  >
                    吊销
                  </Button>
                </>
              )}
            </div>
          ))}
        </>
      )}
    </section>
  );
}
export default function Projects() {
  const [token, setToken] = useState(""),
    [org, setOrg] = useState(false),
    [catalog, setCatalog] = useState("projects"),
    [canEdit, setCanEdit] = useState(false),
    [projects, setProjects] = useState<Row[]>([]),
    [selected, setSelected] = useState(""),
    [error, setError] = useState("");
  const call = useMemo(() => client(token), [token]);
  async function refresh() {
    setProjects(await all(call, "/projects"));
  }
  useEffect(() => {
    if (token) refresh().catch((e) => setError(String(e)));
  }, [token]);
  return (
    <main>
      <header>
        <h1>AI 评测中台</h1>
        {token && (
          <nav>
            <Button onClick={() => setCatalog("projects")}>项目管理</Button>
            <Button onClick={() => setCatalog("datasets")}>数据集管理</Button>
            <Button onClick={() => setCatalog("evaluators")}>评分器管理</Button>
            <Button onClick={() => setCatalog("evaluations")}>批量评测</Button>
          </nav>
        )}
        {token && (
          <Button
            onClick={() => {
              setToken("");
              setSelected("");
              setProjects([]);
            }}
          >
            退出
          </Button>
        )}
      </header>
      {!token ? (
        <Editor
          title="登录项目管理"
          save="登录"
          submit={async (d) => {
            const t = val(d, "token");
            const u = await client(t)<{ org_roles: string[] }>("/auth/me");
            setOrg(u.org_roles.includes("Org"));
            setCanEdit(u.org_roles.includes("Cat"));
            setCatalog("projects");
            setToken(t);
          }}
        >
          <Input name="token" label="组织访问令牌" type="password" />
        </Editor>
      ) : catalog === "evaluations" ? (
        <Evaluations token={token} projects={projects} call={call} />
      ) : catalog === "evaluators" ? (
        <Evaluators call={call} canEdit={canEdit} />
      ) : catalog === "datasets" ? (
        <Datasets token={token} canEdit={canEdit} />
      ) : (
        <div className="layout">
          <aside>
            <h2>项目管理</h2>
            {org && <Button onClick={() => setSelected("")}>新建项目</Button>}
            {projects.map((p) => (
              <button
                className="project"
                key={p.id}
                onClick={() => setSelected(p.id)}
              >
                {p.name}
              </button>
            ))}
            <output>{error}</output>
          </aside>
          {selected ? (
            <Details
              key={selected}
              id={selected}
              call={call}
              refresh={refresh}
            />
          ) : org ? (
            <Editor
              title="创建项目"
              save="创建项目"
              submit={async (d) => {
                const p = await call<Row>("/projects", "POST", {
                  ...limits(d),
                  slug: val(d, "slug"),
                  kind: val(d, "kind"),
                });
                await refresh();
                setSelected(p.id);
              }}
            >
              <Input name="slug" label="项目标识" />
              <label>
                类型
                <select name="kind">
                  {["agent", "rag", "dataagent", "llm", "custom"].map((k) => (
                    <option key={k}>{k}</option>
                  ))}
                </select>
              </label>
              <Limits />
            </Editor>
          ) : (
            <section>请选择有权访问的项目。</section>
          )}
        </div>
      )}
    </main>
  );
}
