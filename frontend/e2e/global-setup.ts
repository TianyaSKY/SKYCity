/**
 * e2e precondition check: fail fast with a clear message when the dev stack
 * (vite dev on :5173, backend on :8000) is not up. The tests intentionally
 * do not manage their own servers.
 */
import type { FullConfig } from '@playwright/test';

const FRONTEND_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173';
const BACKEND_URL = process.env.E2E_API_BASE ?? 'http://localhost:8000';

async function ping(url: string): Promise<boolean> {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return res.ok;
  } catch {
    return false;
  }
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  const [frontUp, backUp] = await Promise.all([
    ping(FRONTEND_URL),
    ping(`${BACKEND_URL}/api/worlds`),
  ]);
  if (!frontUp) {
    throw new Error(
      `e2e 前置检查失败：前端 dev server 未运行于 ${FRONTEND_URL}。请先在 frontend/ 下执行 npm run dev。`,
    );
  }
  if (!backUp) {
    throw new Error(
      `e2e 前置检查失败：后端 API 未运行于 ${BACKEND_URL}。请先启动后端（backend/ 下 uvicorn app.main:app --port 8000）。`,
    );
  }
}
