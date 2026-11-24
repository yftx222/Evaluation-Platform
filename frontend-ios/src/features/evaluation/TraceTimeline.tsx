import { useEffect, useRef, useState } from "react";
import type { Call } from "./api";
import { date, pretty } from "./api";

const kinds = [
  "AGENT_STEP",
  "MODEL_CALL",
  "TOOL_CALL",
  "RETRIEVAL",
  "RERANK",
  "GUARDRAIL",
  "FINAL_OUTPUT",
  "CUSTOM",
];
const kindLabel: Record<string, string> = {
  AGENT_STEP: "Agent 步骤",
  MODEL_CALL: "模型调用",
  TOOL_CALL: "工具调用",
  RETRIEVAL: "检索",
  RERANK: "重排",
  GUARDRAIL: "安全检查",
  FINAL_OUTPUT: "最终输出",
  CUSTOM: "自定义",
};
const states: Record<string, string> = {
  OPEN: "采集中",
  COMPLETE: "完整",
  PARTIAL: "部分缺失",
  MISSING: "未收到事件",
  STARTED: "已开始",
  SUCCEEDED: "成功",
  FAILED: "失败",
  CANCELLED: "已取消",
  UNKNOWN: "未知",
};
type Entry = {
  id: string;
  sequence: number;
  name: string;
  event_type: string;
  status: string;
  has_children: boolean;
  parent_event_key: string | null;
  parent_event_id: string | null;
  started_at: string;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
};
type Detail = Entry & {
  input: unknown;
  output: unknown;
  attributes: unknown;
  resource: unknown;
};
type Summary = {
  status: string;
  event_count: number;
  close_reason: string | null;
  source_name: string;
};
type Props = { call: Call; path: string };

function EventDetail({ call, path }: { call: Call; path: string }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    call<Detail>(path)
      .then((v) => {
        if (active) setDetail(v);
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, [call, path]);
  if (error) return <p role="alert">{error}</p>;
  if (!detail) return <p role="status">正在读取事件详情…</p>;
  return (
    <dl>
      {Object.entries({
        事件: "#"+detail.sequence+" "+detail.name,
        属性: detail.attributes,
        输入: detail.input,
        输出: detail.output,
        来源: detail.resource,
      }).map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>
            <pre>{pretty(value)}</pre>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function EventRows({
  call,
  path,
  query,
  tree,
}: { query: string; tree: boolean } & Props) {
  const [rows, setRows] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState("");
  const [expanded, setExpanded] = useState<string[]>([]);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    call<{ items: Entry[]; next_sequence: number | null }>(
      path +
        "/events?summary_only=true&limit=25&after_sequence=" +
        cursor +
        "&" +
        query,
    )
      .then((v) => {
        if (active) {
          setRows((old) => (cursor ? [...old, ...v.items] : v.items));
          setNext(v.next_sequence);
        }
      })
      .catch((e) => {
        if (active) setError(String(e));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [path, call, query, cursor, retry]);
  return (
    <>
      <ol className="trace-events">
        {rows.map((e) => (
          <li
            key={e.id}
            className={e.status === "FAILED" ? "trace-failed" : ""}
          >
            <b>
              #{e.sequence} {kindLabel[e.event_type]} · {e.name}
            </b>{" "}
            · {states[e.status]}
            <br />
            <small>
              {date(e.started_at)} ·{" "}
              {e.duration_ms === null ? "耗时未提供" : e.duration_ms + " ms"}
              {e.input_tokens !== null && " · 输入 Token " + e.input_tokens}
              {e.output_tokens !== null && " · 输出 Token " + e.output_tokens}
              {e.parent_event_key && !e.parent_event_id && " · 父事件尚未收到"}
            </small>{" "}
            <button
              type="button"
              aria-expanded={detail === e.id}
              onClick={() => setDetail(detail === e.id ? "" : e.id)}
            >
              事件详情
            </button>
            {tree && e.has_children && (
              <button
                type="button"
                aria-expanded={expanded.includes(e.id)}
                onClick={() =>
                  setExpanded((old) =>
                    old.includes(e.id)
                      ? old.filter((x) => x !== e.id)
                      : [...old, e.id],
                  )
                }
              >
                子事件
              </button>
            )}
            {detail === e.id && (
              <EventDetail path={path + "/events/" + e.id} call={call} />
            )}
            {tree && expanded.includes(e.id) && (
              <EventRows
                path={path}
                call={call}
                query={"parent_id=" + e.id}
                tree
              />
            )}
          </li>
        ))}
      </ol>
      {loading && <p role="status">正在读取事件…</p>}
      {error && (
        <p role="alert">
          {error}{" "}
          <button type="button" onClick={() => setRetry((n) => n + 1)}>
            重试
          </button>
        </p>
      )}
      {!loading && !error && !rows.length && <p>当前范围没有事件</p>}
      {!loading && !error && next !== null && (
        <button type="button" onClick={() => setCursor(next)}>
          更多事件
        </button>
      )}
    </>
  );
}

export default function TraceTimeline({
  call,
  id,
  eventId,
}: {
  call: Call;
  id: string;
  eventId?: string;
}) {
  const focusTarget = useRef<HTMLElement>(null);
  useEffect(() => {
    if (eventId) focusTarget.current?.focus();
  }, [id, eventId]);
  const path = "/traces/" + id;
  const [header, setHeader] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [tree, setTree] = useState(false);
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setError("");
    call<Summary>(path)
      .then((v) => {
        if (active) setHeader(v);
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, [path, call, revision]);
  return (
    <section aria-label="轨迹时间线">
      <h4>轨迹时间线</h4>
      {eventId && <section key={eventId} aria-label="证据对应事件" tabIndex={-1}
        ref={focusTarget}>
        <h5>证据对应事件</h5>
        <EventDetail call={call} path={path+"/events/"+eventId}/>
      </section>}
      {error && <p role="alert">{error}</p>}
      {header && (
        <p>
          {header.source_name} · {states[header.status]} · {header.event_count}{" "}
          条事件
          {header.close_reason && " · " + header.close_reason}
        </p>
      )}
      <button type="button" onClick={() => setRevision((n) => n + 1)}>
        刷新轨迹
      </button>{" "}
      <button type="button" onClick={() => setTree(!tree)}>
        {tree ? "按时间线查看" : "按父子关系查看"}
      </button>
      {!tree && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const d = new FormData(e.currentTarget);
            const q = new URLSearchParams();
            for (const k of ["event_type", "started_from", "started_to"]) {
              const v = String(d.get(k) || "");
              if (v)
                q.set(k, k === "event_type" ? v : new Date(v).toISOString());
            }
            setQuery(q.toString());
          }}
        >
          <label>
            事件类型
            <select name="event_type">
              <option value="">全部</option>
              {kinds.map((k) => (
                <option value={k} key={k}>
                  {kindLabel[k]}
                </option>
              ))}
            </select>
          </label>
          <label>
            起始时间
            <input type="datetime-local" name="started_from" />
          </label>
          <label>
            截止时间
            <input type="datetime-local" name="started_to" />
          </label>
          <button>筛选事件</button>
        </form>
      )}
      {header && (
        <EventRows
          key={[id, revision, tree, query].join("/")}
          call={call}
          path={path}
          tree={tree}
          query={tree ? "roots=true" : query}
        />
      )}
    </section>
  );
}
