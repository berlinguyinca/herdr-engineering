import { defineConfig } from "playwright/test";

export default defineConfig({
  testDir: "./tests/playwright",
  testMatch: "communications.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 30000,
  outputDir: ".superpowers/communications-playwright",
  use: {
    baseURL: "http://127.0.0.1:18765",
    headless: true,
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    command: `${process.env.COMMUNICATIONS_TEST_PYTHON || "python3"} tests/playwright/communications-server.py`,
    url: "http://127.0.0.1:18765/api/v1/health",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
