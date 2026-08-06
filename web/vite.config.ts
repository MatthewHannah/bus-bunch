import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// During `npm run dev` we forward /api/* to the local Functions host so we
// don't need CORS. In production the static site calls the Function App
// directly, so the Function App's CORS allowlist handles the browser request.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:7071',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
