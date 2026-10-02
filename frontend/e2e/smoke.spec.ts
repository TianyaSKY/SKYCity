import {expect, test} from '@playwright/test';

test('loads the world UI and backend health endpoint', async ({page, request}) => {
    const health = await request.get('http://127.0.0.1:8000/health');
    expect(health.ok()).toBeTruthy();

    await page.goto('/');
    await expect(page).toHaveTitle('AI Tiny World');
    await expect(page.locator('#app')).toBeVisible();
});
