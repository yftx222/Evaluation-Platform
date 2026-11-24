import type { CSSProperties } from "react";

const symbols = {
  quality: "M4 5h16v12H4zM8 21h8M8 12l3 2 5-6",
  projects:
    "M3 8V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8Z",
  datasets: "m3 7 9-4 9 4-9 4-9-4Zm0 5 9 4 9-4M3 17l9 4 9-4",
  evaluators: "M4 6h7m4 0h5M4 12h2m4 0h10M4 18h10m4 0h2M11 4v4m-5 2v4m8 2v4",
  evaluations: "M4 18v-4m5 4V9m5 9V5m5 13v-8",
  lock: "M7 10V8a5 5 0 0 1 10 0v2m-12 0h14v10H5V10Zm7 4v2",
};

export function Glyph({ name }: { name: keyof typeof symbols }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={symbols[name]} />
    </svg>
  );
}

export const modules = [
  {
    id: "projects",
    title: "项目管理",
    hint: "管理被测服务、接入版本与访问权限。",
  },
  {
    id: "datasets",
    title: "数据集管理",
    hint: "整理测试样本，审核并发布数据集版本。",
  },
  {
    id: "evaluators",
    title: "评分器管理",
    hint: "配置评分规则，用样本比较不同版本的评分结果。",
  },
  {
    id: "evaluations",
    title: "批量评测",
    hint: "编排评测任务，跟踪进度并查看运行报告。",
  },
  {id:"quality", title:"质量工程", hint:"查看生产轨迹、回测门禁和在线实验。"},
] as const;

export function ModuleNav({
  active,
  select,
}: {
  active: string;
  select: (id: string) => void;
}) {
  const index = modules.findIndex((item) => item.id === active);
  return (
    <nav
      className="module-nav"
      aria-label="主导航"
      style={{ "--index": index } as CSSProperties}
    >
      {modules.map((item) => (
        <button
          type="button"
          key={item.id}
          aria-current={active === item.id ? "page" : undefined}
          onClick={() => select(item.id)}
        >
          <Glyph name={item.id} />
          <span>{item.title}</span>
        </button>
      ))}
    </nav>
  );
}

export function SignalArt() {
  return (
    <div className="signal-art" aria-hidden="true">
      <div className="signal-stack">
        <i className="plate back" />
        <i className="plate middle" />
        <div className="plate face">
          <div className="signal-bars">
            {[22, 36, 29, 52, 41, 68, 54, 81, 70, 96, 84].map((height, i) => (
              <i key={i} style={{ height: height + "%" }} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
