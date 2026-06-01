import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// During `npm run dev` we forward /api/* to the local Functions host so we
// don't need CORS. In production the static site and the API are co-located
// behind Static Web Apps' linked-backend proxy at the same origin.
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
