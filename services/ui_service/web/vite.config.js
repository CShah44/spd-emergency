import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// `npm run build` writes the page into ../app/static, where the Python server serves it.
// `npm run dev` serves it with hot reload on :5173 and sends /api to the Python server on :8081.
export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: '../app/static',
    emptyOutDir: true,
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://localhost:8081', changeOrigin: true } },
  },
});
