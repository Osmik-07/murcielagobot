import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Приложение отдаётся по пути /app/ того же домена, где живёт API —
  // поэтому запросы идут относительными путями и CORS не нужен вовсе.
  base: '/app/',
  build: { outDir: 'dist', sourcemap: false },
  server: {
    // Для локальной разработки: проксируем API на прод-бэкенд, чтобы
    // не поднимать всю инфраструктуру ради вёрстки.
    proxy: { '/api': { target: 'https://murcielagonebot.ru', changeOrigin: true } },
  },
});
