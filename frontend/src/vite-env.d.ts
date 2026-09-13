/// <reference types="vite/client" />

// Vite 环境类型声明。
//
// 用途：让 `import.meta.env.DEV` 等内置常量在 TS 下可用（api/rag.ts 用它判断
// 开发环境是否经 Vite 代理转发大模型请求）。缺少该声明时 vue-tsc 会报
// TS2339: Property 'env' does not exist on type 'ImportMeta'。
