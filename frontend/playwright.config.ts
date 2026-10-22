import { defineConfig } from "@playwright/test";

/** Browser tests against a running stack (`docker compose up`), not a dev server: they exercise the
 * real agent, LLM and jammi-server. The Olist connection must exist and be published
 * (backend/scripts/seed_e2e_schema.py). */
export default defineConfig({
  testDir: "./tests",
  // The mocked suite has its own config and server (playwright.mock.config.ts).
  testIgnore: "interpretation.spec.ts",
  timeout: 180_000,
  expect: { timeout: 90_000 },
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000",
    viewport: { width: 1400, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
