import {expect, test} from '@playwright/test';

test('loads the world UI and backend health endpoint', async ({page, request}) => {
    const health = await request.get(`${process.env.E2E_API_URL ?? 'http://127.0.0.1:8000'}/health`);
    expect(health.ok()).toBeTruthy();

    await page.goto('/');
    await expect(page).toHaveTitle('AI Tiny World');
    await expect(page.locator('#app')).toBeVisible();
    await expect(page.getByText('API 正常', {exact: true})).toBeVisible();
    await expect(page.locator('canvas')).toBeVisible();
    await expect(page.locator('.conn.connected')).toHaveText('已连接', {timeout: 30_000});
});
