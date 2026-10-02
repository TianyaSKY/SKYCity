import {defineConfig} from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
    plugins: [vue()],
    server: {
        host: '127.0.0.1',
        port: 5173,
        strictPort: true,
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
            },
            '/health': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
            },
            '/assets/world_data': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
            },
            '/ws': {
                target: 'ws://127.0.0.1:8000',
                ws: true,
            },
        },
    },
});
