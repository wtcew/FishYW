import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发服务器把 /api 代理到 FastAPI（8000），前端无需处理跨域；
// 构建产物输出到 dist/。
export default defineConfig({
  plugins: [
    vue({
      // 与原型保持一致：代码块依赖模板里的真实换行（.code{white-space:pre}），
      // 而 Vue 模板编译器默认 condense 会把文本节点的换行压成空格，故改为 preserve。
      template: { compilerOptions: { whitespace: "preserve" } },
    }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // 编辑器原子写入会先落 .*.tmpdir/*.tmp 再改名，Vite 的 FSWatcher 监控到
    // 这些瞬时路径会抛 EBUSY 并令 dev server 崩溃 —— 显式忽略之。
    watch: { ignored: ["**/.*.tmpdir/**", "**/*.tmp"] },
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      // 通用大模型代理：/llm-proxy/<厂商域名>/<路径> → https://<厂商域名>/<路径>。
      // 浏览器直连多数厂商 API 会被 CORS 拦截，开发环境经此同源转发；
      // 目标主机从请求路径动态解析，无需为每家厂商单独配置。
      "/llm-proxy": {
        target: "https://api.deepseek.com",
        changeOrigin: true,
        secure: true,
        rewrite: (path: string) => path.replace(/^\/llm-proxy\/[^/]+/, ""),
        router: (req: { url?: string }) => {
          const host = (req.url || "").split("/")[2];
          return host ? "https://" + host : "https://api.deepseek.com";
        },
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});