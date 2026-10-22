import { defineConfig } from "@playwright/test";
import config from "./playwright.config";

/** Offline browser coverage: every API request is fulfilled by the test. */
export default defineConfig(config, {
  testMatch: "interpretation.spec.ts",
  testIgnore: [],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: { baseURL: "http://127.0.0.1:3107" },
  webServer: {
    command: "npm run dev -- --hostname 127.0.0.1 --port 3107",
    url: "http://127.0.0.1:3107",
    reuseExistingServer: false,
    env: { NEXT_PUBLIC_API_BASE_URL: "/api", NEXT_TELEMETRY_DISABLED: "1" },
    timeout: 120_000,
  },
});
