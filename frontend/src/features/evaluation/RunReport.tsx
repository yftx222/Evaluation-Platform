import { useEffect, useState } from "react";
import type { Call } from "./api";
import { labels, pretty } from "./api";

type Ratio = { value: number | null; numerator: number; denominator: number };
type Group = {
  label: string | null;
  cases: number;
  mean_score: number | null;
  pass_rate: Ratio;
  score_coverage: Ratio;
};
type Groups = { items: Group[]; next_offset: number | null };
type Version = {
  id: string;
  name: string;
  version: string;
  mean_score: number | null;
  ok: number;
  errors: number;
  skipped: number;
  pass_rate: Ratio;
  score_coverage: Ratio;
  members: { child_version_id: string; weight: number }[];
};
type Summary = {
  id: string;
  total_cases: number;
  metrics: Record<string, Ratio>;
  evaluators: Version[];
  succeeded_cases: number;
  failed_cases: number;
  timed_out_cases: number;
  cancelled_cases: number;
  unknown_delivery_cases: number;
  duration_ms: number | null;
  latency_ms_p50: number | null;
  latency_ms_p95: number | null;
  latency_ms_count: number;
  complete: number;
  partial: number;
  missing: number;
  not_collected: number;
  trace_complete_rate: Ratio;
  tokens_p50: number | null;
  tokens_p95: number | null;
  tokens_count: number;
  total_tokens: number | null;
  input_known: number | null;
  output_known: number | null;
  judge_known: number | null;
  input_count: number;
  output_count: number;
  judge_count: number;
};
type Report = {
  summary: Summary;
  conditions: Record<string, unknown>;
  limits: { max_case_ms: number | null; max_case_tokens: number | null };
  process: {
    attempts: number;
    retries: number;
    delivery_unknown: number;
    quota_waits: number;
    timings: {
      stage: string;
      availability: string;
      ms_count: number;
      ms_p50: number | null;
      ms_p95: number | null;
    }[];
  };
  usage: {
    estimated_cost: number | null;
    currency: string | null;
    truncated: boolean;
    models: {
      role: string;
      version: string | null;
      provider: string | null;
      model: string | null;
      revision: string | null;
      products: number;
      estimate: number | null;
    }[];
  };
};
type Bad = {
  execution_id: string;
  case_key: string;
  status: string;
  case_ms: number | null;
  total_tokens: number | null;
  min_score: number | null;
  reasons: string[];
};
type Comparison = {
  reports: Report[];
  groups: Groups[];
  comparisons: {
    run_id: string;
    differences: string[];
    comparable: boolean;
    deltas: Record<string, number | null> | null;
    changes: {
      matched: number;
      new_failure: number;
      fixed: number;
      regressed: number;
    } | null;
    group_deltas:
      | {
          label: string | null;
          cases: number;
          mean_score: number | null;
          pass_rate: number | null;
        }[]
      | null;
  }[];
};
const titles: Record<string, string> = {
  case_success_rate: "样本成功率",
  score_execution_success_rate: "评分执行成功率",
  score_coverage: "评分覆盖率",
  pass_rate: "评分通过率",
  trace_complete_rate: "完整轨迹占比",
  latency_ms_p50: "P50 耗时（ms）",
  latency_ms_p95: "P95 耗时（ms）",
  total_tokens: "总 Token",
  complete: "完整轨迹",
  partial: "部分轨迹",
  missing: "缺失轨迹",
  project_id: "项目",
  project_version: "项目版本",
  deployment: "部署",
  adapter_build: "Adapter 构建",
  adapter_fingerprint: "Adapter 指纹",
  dataset_digest: "数据集摘要",
  dataset_version: "数据集版本",
  evaluator_dag: "评分依赖摘要",
  judges: "裁判模型、Prompt 和量表版本",
  target_model: "配置的目标模型",
  observed_models: "实际观测模型",
  execution_policy: "执行策略",
  trace_capabilities: "轨迹能力",
  judge_revision_unverified: "裁判版本未核实",
  target_revision_unverified: "目标模型版本未核实",
  model_summary_truncated: "模型种类超出展示上限",
};
const signals: Record<string, string> = {
  any: "全部问题线索",
  all: "所有样本",
  case_failed: "执行失败",
  timed_out: "超时",
  score_error: "评分器错误",
  low_score: "低于分数阈值",
  child_failed: "组合子评分错误或未通过",
  trace_partial: "轨迹部分缺失",
  trace_missing: "轨迹缺失",
  token_limit: "超过 Token 上限",
  latency_limit: "超过耗时上限",
  new_failure: "新增失败",
  fixed: "已修复",
  regressed: "评分退化",
};
const values = (v: number | null | undefined) =>
  v == null
    ? "不可获取"
    : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 3 });
const percent = (r: Ratio) =>
  r.value == null ? "不可获取" : (r.value * 100).toFixed(1) + "%";
function Rate({ v }: { v: Ratio }) {
  return (
    <span>
      {percent(v)}{" "}
      <small>
        （{v.numerator}/{v.denominator}）
      </small>
    </span>
  );
}
function useLoad<T>(call: Call, url: string) {
  const [state, set] = useState<{ url: string; data?: T; error?: string }>({
    url: "",
  });
  useEffect(() => {
    let live = true;
    if (url)
      call<T>(url)
        .then((data) => live && set({ url, data }))
        .catch((e) => live && set({ url, error: String(e) }));
    return () => {
      live = false;
    };
  }, [call, url]);
  return state.url === url ? state : { url };
}
function Pages({
  offset,
  next,
  step,
  change,
}: {
  offset: number;
  next: number | null | undefined;
  step: number;
  change: (n: number) => void;
}) {
  return (
    <div className="row">
      <button
        disabled={!offset}
        onClick={() => change(Math.max(offset - step, 0))}
      >
        前一组
      </button>
      <span>第 {Math.floor(offset / step) + 1} 页</span>
      <button
        disabled={next == null}
        onClick={() => next != null && change(next)}
      >
        后一组
      </button>
    </div>
  );
}
function Percentiles({
  label,
  unit,
  count,
  median,
  high,
}: {
  label: string;
  unit: string;
  count: number;
  median: number | null;
  high: number | null;
}) {
  return (
    <figure className="report-distribution">
      <figcaption>
        {label} · {count} 条有效样本
      </figcaption>
      {(
        [
          ["P50", median],
          ["P95", high],
        ] as const
      ).map(([name, value]) => (
        <div key={name}>
          <span>{name}</span>
          {value != null && high != null ? (
            <>
              <meter
                aria-label={label + name}
                min={0}
                max={Math.max(1, high)}
                value={value}
              />
              <span>
                {values(value)} {unit}
              </span>
            </>
          ) : (
            <span>不可获取</span>
          )}
        </div>
      ))}
    </figure>
  );
}
function Conditions({ report }: { report: Report }) {
  return (
    <details>
      <summary>查看版本和比较条件</summary>
      <dl>
        {Object.entries(report.conditions).map(([k, v]) => (
          <div key={k}>
            <dt>{titles[k] ?? k}</dt>
            <dd>
              <code>{typeof v === "string" ? v : pretty(v)}</code>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
export default function RunReport({
  id,
  token,
  call,
  onCase,
  otherRuns,
}: {
  id: string;
  token: string;
  call: Call;
  onCase: (id: string) => void;
  otherRuns: string[];
}) {
  const path = "/reports/runs/" + id;
  const info = useLoad<Report>(call, path);
  const [dim, setDim] = useState("category"),
    [ver, setVer] = useState(""),
    [pos, setPos] = useState(0);
  const [signal, setSignal] = useState("any"),
    [threshold, setThreshold] = useState("0.5"),
    [page, setPage] = useState(0);
  const [base, setBase] = useState(""),
    [compareIds, setCompare] = useState(""),
    [baseline, setBaseline] = useState("");
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const groupQuery = new URLSearchParams({
    dimension: dim,
    limit: "20",
    offset: String(pos),
  });
  if (ver) groupQuery.set("evaluator_version_id", ver);
  const groups = useLoad<Groups>(call, path + "/metrics?" + groupQuery);
  const query = new URLSearchParams({
    signal,
    threshold: threshold || "0.5",
    limit: "20",
    offset: String(page),
  });
  if (baseline) query.set("baseline_id", baseline);
  const bad = useLoad<{ items: Bad[]; next_offset: number | null }>(
    call,
    path + "/bad-cases?" + query,
  );
  const comparisons = useLoad<Comparison>(
    call,
    compareIds
      ? "/reports/compare?" +
          compareIds +
          "&dimension=" +
          dim +
          "&offset=" +
          pos +
          "&limit=20"
      : "",
  );
  async function download(format: string, filtered: boolean) {
    setBusy(true);
    setError("");
    try {
      const q = new URLSearchParams(filtered ? query : undefined);
      q.set("format", format);
      const response = await fetch("/api/v1" + path + "/export?" + q, {
        headers: { Authorization: "Bearer " + token },
      });
      if (!response.ok)
        throw new Error((await response.json()).error?.message ?? "导出失败");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "report-" + id + "." + format;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const report = info.data;
  if (!report)
    return (
      <p role={info.error ? "alert" : "status"}>
        {info.error ?? "正在生成报告…"}
      </p>
    );
  const s = report.summary;
  return (
    <section className="run-report" aria-label="评测报告">
      <h3>评测报告</h3>
      <p>
        运行编号 <code>{id}</code> · 共 {s.total_cases} 条样本 · 总耗时{" "}
        {values(s.duration_ms)} ms
      </p>
      <p>
        成功 {s.succeeded_cases} · 失败 {s.failed_cases} · 超时{" "}
        {s.timed_out_cases} · 已取消 {s.cancelled_cases} · 交付待核对{" "}
        {s.unknown_delivery_cases}
      </p>
      <div className="report-rates">
        {[
          "case_success_rate",
          "score_execution_success_rate",
          "score_coverage",
          "pass_rate",
        ].map((k) => (
          <div key={k}>
            <strong>{titles[k]}</strong>
            <Rate v={s.metrics[k]} />
            {s.metrics[k].value != null && (
              <meter
                aria-label={titles[k]}
                value={s.metrics[k].value}
                min={0}
                max={1}
              />
            )}
          </div>
        ))}
      </div>
      <small>
        样本成功率按全部样本计算；评分执行成功率按 OK + ERROR
        计算；覆盖率按计划评分数计算；通过率按 OK 评分计算。
      </small>
      <h4>评分器表现</h4>
      <table>
        <thead>
          <tr>
            {[
              "评分器版本",
              "平均分",
              "已评分 / 错误 / 跳过",
              "通过率",
              "覆盖率",
            ].map((v) => (
              <th key={v}>{v}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {s.evaluators.map((v) => (
            <tr key={v.id}>
              <td>
                {v.name} · {v.version}
                {v.members.map((m) => (
                  <small key={m.child_version_id}>
                    {
                      s.evaluators.find((e) => e.id === m.child_version_id)
                        ?.name
                    }{" "}
                    × {m.weight}
                  </small>
                ))}
              </td>
              <td>
                {values(v.mean_score)}{" "}
                {v.mean_score != null && (
                  <meter value={v.mean_score} min={0} max={1} />
                )}
              </td>
              <td>
                {v.ok} / {v.errors} / {v.skipped}
              </td>
              <td>
                <Rate v={v.pass_rate} />
              </td>
              <td>
                <Rate v={v.score_coverage} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h4>样本分组</h4>
      <div className="row">
        <label>
          分组维度
          <select
            value={dim}
            onChange={(e) => {
              setDim(e.target.value);
              setPos(0);
            }}
          >
            <option value="category">类别</option>
            <option value="difficulty">难度</option>
            <option value="tag">标签</option>
          </select>
        </label>
        <label>
          分组评分器
          <select
            value={ver}
            onChange={(e) => {
              setVer(e.target.value);
              setPos(0);
            }}
          >
            <option value="">全部评分器</option>
            {s.evaluators.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name} · {v.version}
              </option>
            ))}
          </select>
        </label>
      </div>
      {groups.error && <p role="alert">{groups.error}</p>}
      <table>
        <thead>
          <tr>
            <th>分组</th>
            <th>样本数</th>
            <th>平均分</th>
            <th>通过率</th>
            <th>覆盖率</th>
          </tr>
        </thead>
        <tbody>
          {groups.data?.items.map((g) => (
            <tr key={JSON.stringify(g.label)}>
              <td>{g.label ?? "未填写"}</td>
              <td>{g.cases}</td>
              <td>{values(g.mean_score)}</td>
              <td>
                <Rate v={g.pass_rate} />
              </td>
              <td>
                <Rate v={g.score_coverage} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pages
        offset={pos}
        next={groups.data?.next_offset}
        step={20}
        change={setPos}
      />
      <h4>轨迹与耗时</h4>
      <p>
        完整 {s.complete} · 部分缺失 {s.partial} · 缺失 {s.missing} · 未采集{" "}
        {s.not_collected} · 完整率 <Rate v={s.trace_complete_rate} />
      </p>
      <p>
        执行尝试 {report.process.attempts} · 重试 {report.process.retries} ·
        配额等待 {report.process.quota_waits} · 交付未知{" "}
        {report.process.delivery_unknown}
      </p>
      <table>
        <thead>
          <tr>
            <th>耗时来源</th>
            <th>观测数</th>
            <th>P50（ms）</th>
            <th>P95（ms）</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>样本开始至结束</td>
            <td>{s.latency_ms_count}</td>
            <td>{values(s.latency_ms_p50)}</td>
            <td>{values(s.latency_ms_p95)}</td>
          </tr>
          {report.process.timings.map((t) => (
            <tr key={t.stage}>
              <td>{t.stage}</td>
              <td>{t.ms_count}</td>
              <td>
                {t.availability === "NOT_APPLICABLE"
                  ? "不适用"
                  : values(t.ms_p50)}
              </td>
              <td>
                {t.availability === "NOT_APPLICABLE"
                  ? "不适用"
                  : values(t.ms_p95)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="report-distributions">
        <Percentiles
          label="样本耗时分布"
          unit="ms"
          count={s.latency_ms_count}
          median={s.latency_ms_p50}
          high={s.latency_ms_p95}
        />
        <Percentiles
          label="样本用量分布"
          unit="Token"
          count={s.tokens_count}
          median={s.tokens_p50}
          high={s.tokens_p95}
        />
      </div>
      <h4>模型用量与成本</h4>
      <p>
        已观测输入 {values(s.input_known)}（{s.input_count} 条） · 输出{" "}
        {values(s.output_known)}（{s.output_count} 条） · Judge{" "}
        {values(s.judge_known)}（{s.judge_count} 条）
      </p>
      <p>
        运行总 Token {values(s.total_tokens)} · 单样本 P50 / P95{" "}
        {values(s.tokens_p50)} / {values(s.tokens_p95)}（{s.tokens_count}{" "}
        条完整用量）
      </p>
      <p>
        正式结果估算成本{" "}
        {report.usage.estimated_cost == null
          ? "不可获取（价格或用量未齐）"
          : report.usage.estimated_cost + " " + report.usage.currency}
      </p>
      {report.usage.models.map((m, i) => (
        <p key={i}>
          {m.role === "target" ? "目标" : "裁判"} · {m.provider ?? "供应商未知"}{" "}
          · {m.model ?? "模型未知"} · {m.revision ?? "修订未知"} · {m.products}{" "}
          条结果
        </p>
      ))}
      {report.usage.truncated && <p>只展示前 100 组模型。</p>}
      <Conditions report={report} />
      <h4>运行对比</h4>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const ids = base
            .trim()
            .split(/[\s,，]+/)
            .filter(Boolean);
          const q = new URLSearchParams();
          for (const v of [...ids, id]) q.append("run_id", v);
          setCompare(q.toString());
          setBaseline("");
          setSignal("any");
          setPage(0);
        }}
      >
        <label>
          基线运行编号
          <input
            required
            list={"runs-" + id}
            value={base}
            placeholder="选择或粘贴编号，多个用逗号分开"
            onChange={(e) => setBase(e.target.value)}
          />
        </label>
        <datalist id={"runs-" + id}>
          {otherRuns
            .filter((v) => v !== id)
            .map((v) => (
              <option key={v} value={v} />
            ))}
        </datalist>
        <button>比较运行</button>
      </form>
      {comparisons.error && <p role="alert">{comparisons.error}</p>}
      {comparisons.data && (
        <div>
          {comparisons.data.reports.map((rp, i) => (
            <details key={rp.summary.id}>
              <summary>
                {i === 0 ? "基线" : "对比运行"} {rp.summary.id}
              </summary>
              <Conditions report={rp} />
              {[
                "case_success_rate",
                "score_execution_success_rate",
                "score_coverage",
                "pass_rate",
              ].map((k) => (
                <p key={k}>
                  {titles[k]} <Rate v={rp.summary.metrics[k]} />
                </p>
              ))}
              <p>
                P95 耗时 {values(rp.summary.latency_ms_p95)} ms · 总 Token{" "}
                {values(rp.summary.total_tokens)} · 完整轨迹{" "}
                {rp.summary.complete}
              </p>
              {comparisons.data?.groups[i].items.map((g) => (
                <p key={JSON.stringify(g.label)}>
                  {g.label ?? "未填写"} · {g.cases} 条 · 通过率{" "}
                  <Rate v={g.pass_rate} />
                </p>
              ))}
            </details>
          ))}
          {comparisons.data.comparisons.map((c) => (
            <div key={c.run_id}>
              <h5>{c.run_id}</h5>
              {!c.comparable ? (
                <p>
                  条件不一致，无法直接比较：
                  {c.differences.map((v) => titles[v] ?? v).join("、")}
                </p>
              ) : (
                <>
                  <p>比较条件一致 · 对齐 {c.changes?.matched} 条</p>
                  <p>
                    {Object.entries(c.deltas ?? {})
                      .map(
                        ([k, v]) =>
                          titles[k] +
                          " " +
                          (k.includes("rate") || k === "score_coverage"
                            ? v == null
                              ? "不可获取"
                              : values(v * 100) + " 个百分点"
                            : values(v)),
                      )
                      .join(" · ")}
                  </p>
                  {c.group_deltas?.map((g) => (
                    <p key={JSON.stringify(g.label)}>
                      {g.label ?? "未填写"} · {g.cases} 条 · 均分变化{" "}
                      {values(g.mean_score)} · 通过率变化{" "}
                      {g.pass_rate == null
                        ? "不可获取"
                        : values(g.pass_rate * 100) + " 个百分点"}
                    </p>
                  ))}
                  {(["new_failure", "fixed", "regressed"] as const).map((k) => (
                    <button
                      key={k}
                      disabled={c.run_id !== id}
                      onClick={() => {
                        setSignal(k);
                        setPage(0);
                        setBaseline(
                          new URLSearchParams(compareIds).get("run_id")!,
                        );
                      }}
                    >
                      {signals[k]} {c.changes?.[k]}
                    </button>
                  ))}
                </>
              )}
            </div>
          ))}
        </div>
      )}
      <h4>问题样本</h4>
      <div className="row">
        <label>
          筛选条件
          <select
            value={signal}
            onChange={(e) => {
              setSignal(e.target.value);
              setPage(0);
            }}
          >
            {Object.entries(signals)
              .filter(
                ([k]) =>
                  baseline ||
                  !["regressed", "fixed", "new_failure"].includes(k),
              )
              .map(([k, title]) => (
                <option key={k} value={k}>
                  {title}
                </option>
              ))}
          </select>
        </label>
        <label>
          低分阈值（0–1）
          <input
            type="number"
            step="0.01"
            min="0"
            max="1"
            value={threshold}
            onChange={(e) => {
              setThreshold(e.target.value);
              setPage(0);
            }}
          />
        </label>
      </div>
      <small>
        项目上限 · Token {report.limits.max_case_tokens ?? "未配置"} · 耗时{" "}
        {report.limits.max_case_ms == null
          ? "未配置"
          : report.limits.max_case_ms + " ms"}
      </small>
      {bad.error && <p role="alert">{bad.error}</p>}
      {bad.data && !bad.data.items.length && <p>没有符合条件的样本。</p>}
      {!!bad.data?.items.length && (
        <table>
          <thead>
            <tr>
              <th>样本 / 评分证据</th>
              <th>状态与线索</th>
              <th>最低分</th>
              <th>耗时（ms）</th>
              <th>Token</th>
            </tr>
          </thead>
          <tbody>
            {bad.data.items.map((b) => (
              <tr key={b.execution_id}>
                <td>
                  <button onClick={() => onCase(b.execution_id)}>
                    {b.case_key}
                  </button>
                </td>
                <td>
                  {[labels[b.status], ...b.reasons.map((k) => signals[k])].join(
                    " · ",
                  )}
                </td>
                <td>{values(b.min_score)}</td>
                <td>{values(b.case_ms)}</td>
                <td>{values(b.total_tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Pages
        offset={page}
        next={bad.data?.next_offset}
        step={20}
        change={setPage}
      />
      <div className="row">
        {["csv", "json"].map((fmt) => (
          <button
            disabled={busy}
            key={fmt}
            onClick={() => download(fmt, false)}
          >
            导出报告 {fmt.toUpperCase()}
          </button>
        ))}
        <button disabled={busy} onClick={() => download("csv", true)}>
          导出筛选样本 CSV
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
