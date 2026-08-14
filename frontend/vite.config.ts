import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Dual-stack so Cursor port-forward and browsers resolving localhost to ::1
    // do not hit ERR_CONNECTION_REFUSED (IPv4-only 0.0.0.0 leaves ::1 closed).
    host: '::',
    port: 5173,
    strictPort: true,
  },
  optimizeDeps: {
    exclude: ['@ffmpeg/ffmpeg', '@ffmpeg/util'],
  },
  worker: {
    format: 'es',
  },
})
