import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "EVAL_PREVIEW_");
  return {
    server: {
      proxy: { "/api": env.EVAL_PREVIEW_API || "http://127.0.0.1:8000" },
    },
  };
});
