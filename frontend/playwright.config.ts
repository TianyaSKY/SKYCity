import {defineConfig} from '@playwright/test';

/**
 * e2e smoke tests assume the stack is already running:
 *   - frontend vite dev server on http://localhost:5173 (`npm run dev` in frontend/)
 *   - backend API on http://localhost:8000 (uvicorn / python main.py in backend/)
 *
 * For Docker, set E2E_BASE_URL and E2E_API_URL to the same single-port URL.
 * Playwright does NOT manage a webServer: the global setup pings both and
 * fails fast with a clear message when either is down.
 */
export default defineConfig({
    testDir: './e2e',
    timeout: 60_000,
    fullyParallel: false,
    globalSetup: './e2e/global-setup.ts',
    use: {
        baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173',
        trace: 'retain-on-failure',
    },
    projects: [
        {
            name: 'chromium',
            use: {browserName: 'chromium'},
        },
    ],
});
