import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 本地开发：先 ssh -L 8791:127.0.0.1:8791 <服务器>，再 FUXI_ADMIN_TOKEN=... pnpm dev（口令只从环境变量读）
const token = process.env.FUXI_ADMIN_TOKEN ?? "";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", assetsDir: "assets", sourcemap: false },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8791",
        changeOrigin: false,
        headers: token ? { "X-Fuxi-Admin": token } : {},
      },
    },
  },
});
