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
    // 0.0.0.0 so the listen shows up in IPv4 /proc/net/tcp. Cursor Cloud
    // auto-forward only detects IPv4 listeners; host '::' (tcp6 only) is
    // invisible and localhost:5173 never opens on your machine.
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // Cursor / cloud previews send a forwarded Host header. Vite 8 blocks
    // unknown hosts with 403 "Blocked request. This host is not allowed."
    allowedHosts: true,
    hmr: {
      host: 'localhost',
      clientPort: 5173,
    },
    proxy: apiProxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    allowedHosts: true,
    proxy: apiProxy,
  },
  optimizeDeps: {
    exclude: ['@ffmpeg/ffmpeg', '@ffmpeg/util'],
  },
  worker: {
    format: 'es',
  },
})
