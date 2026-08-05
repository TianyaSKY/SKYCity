import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',  // macOS node 默认把 localhost 解析为 ::1 只绑 IPv6,统一 IPv4
    port: 5173,
    strictPort: true,
  },
});
