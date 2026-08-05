/**
 * M9 e2e smoke tests: map render + WS connect, speed button, god weather.
 *
 * Requires the dev stack already running (see global-setup.ts):
 *   - frontend vite dev on http://localhost:5173
 *   - backend API on http://localhost:8000
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.E2E_API_BASE ?? 'http://localhost:8000';

/** A world must exist for the app to connect to; create one when the list is empty. */
async function ensureWorld(request: APIRequestContext): Promise<void> {
  const list = await request.get(`${API_BASE}/api/worlds`);
  if (!list.ok()) {
    throw new Error(`GET ${API_BASE}/api/worlds failed: ${list.status()}`);
  }
  const worlds = (await list.json()) as { world_id: string }[];
  if (worlds.length === 0) {
    const created = await request.post(`${API_BASE}/api/worlds`, {
      data: { name: 'e2e', autonomous: true },
    });
    if (!created.ok()) {
      throw new Error(`POST ${API_BASE}/api/worlds failed: ${created.status()} ${await created.text()}`);
    }
  }
}

/** Wait for the world WS to be connected (clock-bar shows 已连接). */
async function waitConnected(page: Page): Promise<void> {
  await expect(page.locator('.clock-bar .conn')).toContainText('已连接', { timeout: 30_000 });
}

/** Pick a weather option and wait for the matching stream line. */
async function changeWeather(page: Page, optionValue: string, line: string): Promise<void> {
  await page.locator('.weather-select').selectOption(optionValue);
  await expect(page.locator('.event-stream')).toContainText(line, { timeout: 15_000 });
}

test.beforeEach(async ({ request }) => {
  await ensureWorld(request);
});

test('map renders and world connects', async ({ page }) => {
  await page.goto('/');

  // Pixi canvas is present and visible.
  await expect(page.locator('.canvas-host canvas')).toBeVisible({ timeout: 30_000 });

  // WS connects: 连接中 -> 已连接 (allow either to appear first, then require connected).
  await expect(page.locator('.clock-bar .conn')).toContainText(/(连接中|已连接)/, { timeout: 30_000 });
  await expect(page.locator('.clock-bar .conn')).toContainText('已连接', { timeout: 30_000 });

  // World clock renders (clock-bar time + location panel 世界时间).
  await expect(page.locator('.clock-bar .time')).toBeVisible();
  await expect(page.locator('.clock-bar .time')).not.toBeEmpty();
  await expect(page.locator('.location-panel .clock')).toContainText('世界时间');
});

test('speed button posts', async ({ page }) => {
  await page.goto('/');
  await waitConnected(page);

  const speedButtons = page.locator('.speed-btn');
  await expect(speedButtons.first()).toBeEnabled();

  // The world may already be at 10× (persisted across runs); set_speed is a
  // no-op without an event when the speed is unchanged, so first switch to a
  // speed that is NOT currently active — a guaranteed real transition.
  const currentSpeed = await speedButtons.evaluateAll((btns) => {
    const active = btns.find((b) => b.classList.contains('active'));
    return active ? parseInt(active.textContent ?? '1', 10) : 1;
  });
  const firstTarget = currentSpeed === 10 ? 5 : 10;

  const pick = (speed: number) => page.getByRole('button', { name: `${speed}×`, exact: true });
  await pick(firstTarget).click();
  await expect(pick(firstTarget)).toHaveClass(/active/, { timeout: 10_000 });
  await expect(page.locator('.event-stream')).toContainText(`世界速度调整为 ${firstTarget}×`, {
    timeout: 15_000,
  });

  // Now the 10× transition is guaranteed to emit world_speed_changed.
  await pick(10).click();
  await expect(pick(10)).toHaveClass(/active/, { timeout: 10_000 });
  await expect(page.locator('.event-stream')).toContainText('世界速度调整为 10×', {
    timeout: 15_000,
  });
});

test('god weather dropdown', async ({ page }) => {
  await page.goto('/');
  await waitConnected(page);

  const select = page.locator('.weather-select');
  await expect(select).toBeEnabled();

  // If a previous run already left the world at 雨, flip away first so the
  // final 雨 selection is a real change that produces a stream line.
  const current = await select.inputValue();
  if (current === 'rain') {
    await changeWeather(page, 'snow', '天气变为 雪');
  }
  await changeWeather(page, 'rain', '天气变为 雨');
  await expect(select).toHaveValue('rain');
});
