import { ConfigProvider } from "antd";
import { createRoot } from "react-dom/client";
import Projects from "./features/Projects";
import "./style.css";

createRoot(document.getElementById("root")!).render(
  <ConfigProvider button={{ autoInsertSpace: false }}>
    <Projects />
  </ConfigProvider>,
);
