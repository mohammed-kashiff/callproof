import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    // Dual-stack: with bindv6only=0, host '::' accepts IPv4 and IPv6.
    // localhost often resolves to ::1, so IPv4-only 0.0.0.0 looks refused.
    host: '::',
    port: 5173,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: '::',
    port: 5173,
    strictPort: true,
    proxy: apiProxy,
  },
  optimizeDeps: {
    exclude: ['@ffmpeg/ffmpeg', '@ffmpeg/util'],
  },
  worker: {
    format: 'es',
  },
})
