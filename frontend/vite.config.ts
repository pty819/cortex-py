import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0', // 监听所有网卡，允许局域网访问
    port: 5173,
    proxy: {
      // Dev proxy: /v1 → FastAPI backend at :8002
      '/v1': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
    },
  },
})
