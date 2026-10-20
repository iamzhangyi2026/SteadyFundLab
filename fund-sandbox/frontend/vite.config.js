import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 项目根 = frontend/，构建产物落到 frontend/dist，由 FastAPI 在 127.0.0.1:8765 伺服。
// dev 模式跑 vite dev server（默认 5173），/api 代理到后端，便于热更新开发。
export default defineConfig({
  plugins: [react()],
  root: '.',
  base: '/',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 启动优化：全部依赖与页面打成一个巨包时，
    // 首屏必须整包下载解析完才能渲染。拆成按需加载的独立 chunk 后：
    //   ① 首屏只下载「react + 当前页」，echarts / xlsx 等重依赖延后；
    //   ② 各 chunk 文件名带 hash，未变动的部分命中缓存，二次打开更快。
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom'],
          echarts: ['echarts', 'echarts-for-react'],
          xlsx: ['xlsx'],
          motion: ['framer-motion'],
        },
      },
    },
    chunkSizeWarningLimit: 900,
  },
})
