import { test as base, expect } from "@playwright/test";

/** Optional UI-only proxy: review a worktree at port 3108 while preserving the Compose
 * browser origin/CORS configuration. API requests go directly to the real backend.
 * With no E2E_FRONTEND_PREVIEW_URL this is ordinary Playwright against the running UI.
 */
export const test = base.extend({
  page: async ({ page, baseURL }, use) => {
    const preview = process.env.E2E_FRONTEND_PREVIEW_URL;
    if (preview && baseURL) {
      const origin = new URL(baseURL).origin;
      await page.route(`${origin}/**`, async (route) => {
        const original = new URL(route.request().url());
        const target = new URL(original.pathname + original.search, preview);
        const response = await route.fetch({ url: target.toString() });
        await route.fulfill({ response });
      });
    }
    await use(page);
  },
});

export { expect };
