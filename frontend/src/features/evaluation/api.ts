export type Call = <T>(
  url: string,
  method?: string,
  body?: unknown,
  headers?: Record<string, string>,
) => Promise<T>;
export type Page<T> = { items: T[]; next_cursor: string | null };
export const base = "/evaluations";
export async function all<T>(call: Call, url: string): Promise<T[]> {
  const rows: T[] = [];
  let cursor: string | null = null;
  do {
    const join = url.includes("?") ? "&" : "?";
    const result: Page<T> = await call(
      url +
        join +
        "limit=100" +
        (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""),
    );
    rows.push(...result.items);
    cursor = result.next_cursor;
  } while (cursor);
  return rows;
}
export type Task = {
  id: string;
  name: string;
  description: string | null;
  archived_at: string | null;
  config_digest: string;
  snapshot: Snapshot;
};
export type Node = {
  id: string;
  name: string;
  version: string;
  impl_key: string;
  members: { child_version_id: string; weight: number; position: number }[];
};
export type Snapshot = {
  project_version: { label: string; deployment_ref: string };
  dataset_version: { version: number };
  adapter: { kind: string; build: string };
  roots: string[];
  evaluator_plan: Node[];
  policy: Record<string, unknown>;
};
export const labels: Record<string, string> = {
  PENDING: "待初始化",
  QUEUED: "排队中",
  READY: "待执行",
  RUNNING: "运行中",
  RETRY_WAIT: "等待继续",
  PAUSE_REQUESTED: "暂停中",
  PAUSED: "已暂停",
  CANCEL_REQUESTED: "取消中",
  CANCELLED: "已取消",
  COMPLETED: "已完成",
  PARTIAL_FAILED: "部分失败",
  FAILED: "失败",
  TIMED_OUT: "超时",
  UNKNOWN_DELIVERY: "交付待核对",
  SUCCEEDED: "成功",
  OK: "已评分",
  ERROR: "评分错误",
  SKIPPED: "已跳过",
};
export const terminal = (s: string) =>
  ["CANCELLED", "COMPLETED", "PARTIAL_FAILED", "FAILED"].includes(s);
export type Run = {
  id: string;
  status: string;
  total_cases: number;
  pending_cases: number;
  running_cases: number;
  succeeded_cases: number;
  failed_cases: number;
  timed_out_cases: number;
  cancelled_cases: number;
  unknown_delivery_cases: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  snapshot: Snapshot;
  score_counts: Record<string, number>;
  failure_reasons: { code: string; cases: number }[];
};
export const date = (v: string | null) =>
  v ? new Date(v).toLocaleString("zh-CN") : "—";
export const pretty = (value: unknown) => JSON.stringify(value, null, 2);
export function dagLines(s: Snapshot) {
  const nodes = new Map(s.evaluator_plan.map((n) => [n.id, n]));
  return s.evaluator_plan.map((n) => {
    const children = n.members.map((m) => {
      const child = nodes.get(m.child_version_id);
      return (
        (child ? child.name + " " + child.version : m.child_version_id) +
        " × " +
        m.weight
      );
    });
    return (
      n.name +
      " " +
      n.version +
      (children.length ? " ← " + children.join("、") : "") +
      (s.roots.includes(n.id) ? "（选用）" : "")
    );
  });
}
