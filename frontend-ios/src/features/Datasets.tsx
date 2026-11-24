import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

type Entry = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  archived: boolean;
  version_count: number;
  sample_count: number;
  updated_at: string;
};
type Version = {
  id: string;
  version: number;
  state: "DRAFT" | "PUBLISHED";
  base_version_id: string | null;
  sample_count: number;
  published_at: string | null;
  digest: string | null;
  changes?: { added: number; modified: number; removed: number };
};
type Detail = Entry & { draft: Version | null; versions: Version[] };
type Sample = {
  id: string;
  case_key: string;
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
  category: string | null;
  difficulty: string | null;
  tags: string[];
  split: string;
  extensions: Record<string, unknown>;
};
type Page<T> = { items: T[]; next_cursor: string | null };
type RequestFn = (
  path: string,
  method?: string,
  body?: object,
) => Promise<Response>;
async function data<T>(r: Promise<Response>): Promise<T> {
  return (await (await r).json()).data;
}
function form(e: FormEvent<HTMLFormElement>) {
  e.preventDefault();
  return new FormData(e.currentTarget);
}
function field(f: FormData, k: string) {
  return String(f.get(k) ?? "");
}
const formats = ["json", "jsonl", "csv", "xlsx"];
function FormatOptions() {
  return formats.map((f) => <option key={f}>{f}</option>);
}

function Samples({
  api,
  d,
  v,
  editable,
  refresh,
}: {
  api: RequestFn;
  d: string;
  v: Version;
  editable: boolean;
  refresh: () => void;
}) {
  const path = "/" + d + "/cases";
  const [filter, setFilter] = useState(""),
    [cursor, setCursor] = useState(""),
    [page, setPage] = useState<Page<Sample>>({ items: [], next_cursor: null }),
    [err, setErr] = useState(""),
    [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<Sample | null>(null),
    [editing, setEditing] = useState(false);
  useEffect(() => {
    let active = true;
    data<Page<Sample>>(
      api(
        path +
          "?version_id=" +
          v.id +
          "&limit=20&" +
          filter +
          (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""),
      ),
    )
      .then((p) => {
        if (active) setPage(p);
      })
      .catch((e) => active && setErr(String(e)));
    return () => {
      active = false;
    };
  }, [api, path, v.id, filter, cursor, revision]);
  function changed() {
    setRevision((r) => r + 1);
    setCursor("");
    setEditing(false);
    refresh();
  }
  async function save(e: FormEvent<HTMLFormElement>) {
    const f = form(e);
    try {
      const item = {
        draft_id: v.id,
        case_key: field(f, "case_key"),
        input: JSON.parse(field(f, "input")),
        expected: JSON.parse(field(f, "expected")),
        extensions: JSON.parse(field(f, "extensions")),
        category: field(f, "category") || null,
        difficulty: field(f, "difficulty") || null,
        tags: JSON.parse(field(f, "tags")),
        split: field(f, "split"),
      };
      await api(
        path + (selected ? "/" + selected.id : ""),
        selected ? "PATCH" : "POST",
        item,
      );
      setErr("");
      changed();
    } catch (e) {
      setErr(String(e));
    }
  }
  return (
    <section>
      <h3>样本</h3>
      <form
        onSubmit={(e) => {
          const f = form(e);
          const p = new URLSearchParams();
          f.forEach((s, k) => {
            if (s) p.set(k, String(s));
          });
          setCursor("");
          setFilter(p.toString());
        }}
      >
        <div className="fields">
          {[
            ["q", "搜索问题或标识"],
            ["category", "类别"],
            ["difficulty", "难度"],
            ["tag", "标签"],
            ["split", "划分"],
          ].map(([k, label]) => (
            <label key={k}>
              {label}
              <input name={k} />
            </label>
          ))}
        </div>
        <button>筛选</button>
      </form>
      {editable && (
        <button
          onClick={() => {
            setSelected(null);
            setEditing(true);
          }}
        >
          新增样本
        </button>
      )}
      {page.items.map((c) => (
        <details key={c.id}>
          <summary>
            {c.case_key} · {c.category ?? "未分类"} · {c.split}
          </summary>
          <pre>
            {JSON.stringify(
              {
                input: c.input,
                expected: c.expected,
                extensions: c.extensions,
                tags: c.tags,
                difficulty: c.difficulty,
              },
              null,
              2,
            )}
          </pre>
          {editable && (
            <>
              <button
                onClick={() => {
                  setSelected(c);
                  setEditing(true);
                }}
              >
                编辑样本
              </button>
              <button
                onClick={async () => {
                  if (!confirm("删除这条草稿样本？")) return;
                  try {
                    await api(
                      path + "/" + c.id + "?draft_id=" + v.id,
                      "DELETE",
                    );
                    changed();
                  } catch (e) {
                    setErr(String(e));
                  }
                }}
              >
                删除样本
              </button>
            </>
          )}
        </details>
      ))}
      {cursor && <button onClick={() => setCursor("")}>回到首页</button>}
      {page.next_cursor && (
        <button onClick={() => setCursor(page.next_cursor!)}>下一页</button>
      )}
      {editing && editable && (
        <form key={selected?.id ?? "new"} onSubmit={save}>
          <label>
            样本标识
            <input required name="case_key" defaultValue={selected?.case_key} />
          </label>
          {(["input", "expected", "extensions", "tags"] as const).map((k) => (
            <label key={k}>
              {
                {
                  input: "输入 JSON",
                  expected: "参考答案与 criteria JSON",
                  extensions: "扩展 JSON",
                  tags: "标签 JSON",
                }[k]
              }
              <textarea
                required
                name={k}
                defaultValue={JSON.stringify(
                  selected?.[k] ??
                    {
                      input: { query: "" },
                      expected: { answer: "", criteria: [] },
                      extensions: {},
                      tags: [],
                    }[k],
                  null,
                  2,
                )}
              />
            </label>
          ))}
          <div className="fields">
            <label>
              样本类别
              <input name="category" defaultValue={selected?.category ?? ""} />
            </label>
            <label>
              样本难度
              <input
                name="difficulty"
                defaultValue={selected?.difficulty ?? ""}
              />
            </label>
            <label>
              样本划分
              <select name="split" defaultValue={selected?.split ?? "test"}>
                {["train", "validation", "test"].map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
          </div>
          <button>保存样本</button>
          <button type="button" onClick={() => setEditing(false)}>
            取消编辑
          </button>
        </form>
      )}
      <output>{err}</output>
    </section>
  );
}

function Importer({
  api,
  d,
  v,
  refresh,
}: {
  api: RequestFn;
  d: string;
  v: string;
  refresh: () => void;
}) {
  const [preview, setPreview] = useState<{
      count: number;
      preview: Sample[];
    } | null>(null),
    [err, setErr] = useState(""),
    [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    const payload = form(e);
    payload.set("dataset_id", d);
    payload.set("draft_id", v);
    payload.set("preview", String(!preview));
    setBusy(true);
    setErr("");
    try {
      if (preview) {
        await api("/import", "POST", payload);
        refresh();
        setPreview(null);
        setErr("导入完成");
      } else {
        setPreview(await data(api("/import", "POST", payload)));
      }
    } catch (e) {
      setErr(String(e));
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} onChange={() => setPreview(null)}>
      <h3>导入样本</h3>
      <fieldset disabled={busy}>
        <label>
          上传文件
          <input
            type="file"
            name="file"
            required
            accept=".json,.jsonl,.csv,.xlsx"
          />
        </label>
        <label>
          导入格式
          <select name="format">
            <FormatOptions />
          </select>
        </label>
        <label>
          列映射（源列名到样本字段）
          <textarea name="mapping" defaultValue="{}" />
        </label>
        {preview && (
          <>
            <p>待导入 {preview.count} 条，以下最多展示前五条。</p>
            <pre>{JSON.stringify(preview.preview, null, 2)}</pre>
          </>
        )}
        <button>{preview ? "确认导入" : "预览导入"}</button>
      </fieldset>
      <output>{err}</output>
    </form>
  );
}

function DatasetView({
  api,
  id,
  canEdit,
  updated,
}: {
  api: RequestFn;
  id: string;
  canEdit: boolean;
  updated: () => void;
}) {
  const [info, setInfo] = useState<Detail | null>(null),
    [picked, setPicked] = useState(""),
    [revision, setRevision] = useState(0),
    [review, setReview] = useState<Version | null>(null),
    [err, setErr] = useState("");
  const path = "/" + id;
  useEffect(() => {
    let live = true;
    data<Detail>(api(path))
      .then((x) => {
        if (live) setInfo(x);
      })
      .catch((e) => live && setErr(String(e)));
    return () => {
      live = false;
    };
  }, [api, path, revision]);
  function refresh() {
    setRevision((r) => r + 1);
    setReview(null);
    updated();
  }
  async function change(url: string, method: string, body: object) {
    try {
      await api(url, method, body);
      refresh();
      setErr("");
    } catch (e) {
      setErr(String(e));
      setReview(null);
      setRevision((r) => r + 1);
    }
  }
  if (!info) return <output>{err || "正在读取数据集…"}</output>;
  const v =
    info.versions.find((x) => x.id === picked) ??
    info.draft ??
    info.versions[0];
  const edit = canEdit && !info.archived && v?.state === "DRAFT";
  return (
    <section>
      <h2>{info.name}</h2>
      <p>
        {info.description} · {info.archived ? "已归档" : "使用中"}
      </p>
      {canEdit && (
        <details>
          <summary>目录设置</summary>
          <form
            onSubmit={(e) => {
              const f = form(e);
              void change(path, "PATCH", {
                name: field(f, "name"),
                description: field(f, "description"),
              });
            }}
          >
            <label>
              目录名称
              <input required name="name" defaultValue={info.name} />
            </label>
            <label>
              目录描述
              <textarea
                name="description"
                defaultValue={info.description ?? ""}
              />
            </label>
            <button disabled={info.archived}>保存目录</button>
          </form>
          <button
            onClick={() => change(path, "PATCH", { archived: !info.archived })}
          >
            {info.archived ? "解除归档" : "归档目录"}
          </button>
        </details>
      )}
      <h3>版本历史</h3>
      <label>
        查看版本
        <select
          value={v?.id ?? ""}
          onChange={(e) => {
            setPicked(e.target.value);
            setReview(null);
          }}
        >
          {info.versions.map((x) => (
            <option key={x.id} value={x.id}>
              v{x.version} · {x.state}
            </option>
          ))}
        </select>
      </label>
      {v && (
        <>
          <p>
            样本数 {v.sample_count} · 基线{" "}
            {info.versions.find((x) => x.id === v.base_version_id)?.version ??
              "无"}{" "}
            · 发布时间 {v.published_at ?? "未发布"}
          </p>
          <p>
            内容摘要 <code>{v.digest}</code>
          </p>
          {canEdit && !info.archived && !info.draft && (
            <button
              onClick={async () => {
                try {
                  const draft = await data<Version>(
                    api(path + "/versions", "POST", {
                      action: "draft",
                      base_version_id: v.id,
                    }),
                  );
                  setPicked(draft.id);
                  refresh();
                } catch (e) {
                  setErr(String(e));
                }
              }}
            >
              从此版本创建草稿
            </button>
          )}
          {edit && (
            <>
              <button onClick={() => setReview(v)}>审核草稿</button>
              {review && (
                <div>
                  <p>
                    本次发布 {review.sample_count} 条。相对基线新增{" "}
                    {review.changes?.added}，修改 {review.changes?.modified}
                    ，删除 {review.changes?.removed}。
                  </p>
                  <code>{review.digest}</code>
                  <button
                    onClick={() =>
                      change(path + "/versions", "POST", {
                        action: "publish",
                        draft_id: review.id,
                        reviewed_digest: review.digest,
                      })
                    }
                  >
                    确认发布
                  </button>
                </div>
              )}
              <Importer
                key={v.id}
                api={api}
                d={id}
                v={v.id}
                refresh={refresh}
              />
            </>
          )}
          <form
            onSubmit={async (e) => {
              const f = form(e),
                fmt = field(f, "format");
              try {
                const res = await api(
                  path + "/export?version_id=" + v.id + "&format=" + fmt,
                );
                const url = URL.createObjectURL(await res.blob());
                const a = document.createElement("a");
                a.href = url;
                a.download = info.slug + "-v" + v.version + "." + fmt;
                a.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
              } catch (e) {
                setErr(String(e));
              }
            }}
          >
            <label>
              导出格式
              <select name="format">
                <FormatOptions />
              </select>
            </label>
            <button>导出当前版本</button>
          </form>
          <Samples
            key={v.id + ":" + revision}
            api={api}
            d={id}
            v={v}
            editable={edit}
            refresh={refresh}
          />
        </>
      )}
      <output>{err}</output>
    </section>
  );
}

const creationFields = [
  {
    key: "slug",
    label: "数据集标识",
    example: "customer_service_1",
    hint: "唯一的英文代号，1–64 个字符。以小写英文字母开头，后面可用小写字母、数字、下划线或短横线。",
  },
  {
    key: "name",
    label: "数据集名称",
    example: "客服问答测试集",
    hint: "展示给人看的名字，可用中文，最多 160 个字符。",
  },
  {
    key: "description",
    label: "描述",
    example: "收录售后客服常见问题，用于评测回答质量。",
    hint: "用文字说明数据集的内容或用途，最多 4000 个字符，也可以留空。",
  },
] as const;

type CreationKey = (typeof creationFields)[number]["key"];
type CreationErrors = Partial<Record<CreationKey, string>>;

class DatasetRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function CreateDataset({
  api,
  onCreated,
}: {
  api: RequestFn;
  onCreated: (d: Entry) => void;
}) {
  const [errors, setErrors] = useState<CreationErrors>({});
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    const element = e.currentTarget;
    const f = form(e);
    const slug = field(f, "slug"),
      name = field(f, "name").trim(),
      description = field(f, "description");
    const issues: CreationErrors = {};
    if (!slug) issues.slug = "请填写数据集标识，例如 customer_service_1。";
    else if (!/^[a-z]/.test(slug))
      issues.slug = "请以小写英文字母开头，例如把 1 写成 dataset_1。";
    else if (!/^[a-z][a-z0-9_-]{0,63}$/.test(slug))
      issues.slug =
        "标识最多 64 个字符，只能包含小写英文字母、数字、下划线和短横线。";
    if (!name) issues.name = "请填写数据集名称，例如「客服问答测试集」。";
    else if ([...name].length > 160)
      issues.name = "名称超过 160 个字符，请缩短。";
    if ([...description].length > 4000)
      issues.description = "描述超过 4000 个字符，请缩短或留空。";
    setErrors(issues);
    setFailure("");
    const first = creationFields.find((f) => issues[f.key]);
    if (first) {
      element.querySelector<HTMLElement>('[name="' + first.key + '"]')?.focus();
      return;
    }
    setBusy(true);
    try {
      onCreated(
        await data<Entry>(api("", "POST", { slug, name, description })),
      );
    } catch (err) {
      if (err instanceof DatasetRequestError && err.status === 409) {
        setErrors({ slug: "这个标识已被使用，请换一个，例如在末尾加上 _2。" });
        element.querySelector<HTMLInputElement>('[name="slug"]')?.focus();
      } else
        setFailure(err instanceof Error ? err.message : "创建失败，请重试。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="create-dataset" noValidate onSubmit={submit}>
      <h2>创建数据集</h2>
      {creationFields.map(({ key, label, example, hint }) => {
        const id = "dataset-create-" + key;
        const props = {
          id,
          name: key,
          placeholder: "例如：" + example,
          required: key !== "description",
          "aria-invalid": Boolean(errors[key]),
          "aria-describedby":
            id + "-hint" + (errors[key] ? " " + id + "-error" : ""),
          onChange: () => {
            if (errors[key]) setErrors((old) => ({ ...old, [key]: undefined }));
          },
        };
        return (
          <div className="dataset-field" key={key}>
            <label htmlFor={id}>
              {label}（{props.required ? "必填" : "选填"}）
            </label>
            {key === "description" ? (
              <textarea {...props} rows={3} />
            ) : (
              <input {...props} type="text" />
            )}
            <small id={id + "-hint"}>{hint}</small>
            {errors[key] && (
              <span id={id + "-error"} role="alert">
                {errors[key]}
              </span>
            )}
          </div>
        );
      })}
      <button disabled={busy}>{busy ? "正在创建…" : "创建数据集"}</button>
      {failure && <p role="alert">{failure}</p>}
    </form>
  );
}

export default function Datasets({
  token,
  canEdit,
}: {
  token: string;
  canEdit: boolean;
}) {
  const api = useCallback<RequestFn>(
    async (path, method = "GET", body) => {
      const multipart = body instanceof FormData;
      const res = await fetch("/api/v1/datasets" + path, {
        method,
        headers: {
          Authorization: "Bearer " + token,
          ...(!multipart && body ? { "Content-Type": "application/json" } : {}),
        },
        body: multipart ? body : body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        const payload = await res.json();
        throw new DatasetRequestError(
          payload.error?.message ?? res.statusText,
          res.status,
        );
      }
      return res;
    },
    [token],
  );
  const [id, setId] = useState(""),
    [page, setPage] = useState<Page<Entry>>({ items: [], next_cursor: null }),
    [cursor, setCursor] = useState(""),
    [revision, setRevision] = useState(0),
    [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    data<Page<Entry>>(api("?limit=30&cursor=" + encodeURIComponent(cursor)))
      .then((x) => {
        if (live) setPage(x);
      })
      .catch((e) => live && setErr(String(e)));
    return () => {
      live = false;
    };
  }, [api, cursor, revision]);
  return (
    <div className="layout">
      <aside>
        <h2>数据集列表</h2>
        {canEdit && <button onClick={() => setId("")}>新建数据集</button>}
        {page.items.map((x) => (
          <button className="project" aria-pressed={id === x.id} key={x.id} onClick={() => setId(x.id)}>
            {x.name} · {x.slug}
            <br />
            {x.version_count} 个版本 · {x.sample_count} 条 ·{" "}
            {x.archived ? "已归档" : "使用中"}
            <br />
            {new Date(x.updated_at).toLocaleString()}
          </button>
        ))}
        {cursor && <button onClick={() => setCursor("")}>首页</button>}
        {page.next_cursor && (
          <button onClick={() => setCursor(page.next_cursor!)}>更多目录</button>
        )}
      </aside>
      {id ? (
        <DatasetView
          key={id}
          id={id}
          canEdit={canEdit}
          api={api}
          updated={() => setRevision((n) => n + 1)}
        />
      ) : canEdit ? (
        <CreateDataset
          api={api}
          onCreated={(d) => {
            setId(d.id);
            setCursor("");
            setRevision((n) => n + 1);
            setErr("");
          }}
        />
      ) : (
        <p>选择数据集查看样本与版本。</p>
      )}
      <output>{err}</output>
    </div>
  );
}
