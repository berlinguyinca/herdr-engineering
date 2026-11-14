import { defineConfig } from "playwright/test";

export default defineConfig({
  testDir: "./tests/playwright",
  testMatch: "scratch-retention.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 120000,
  outputDir: ".superpowers/scratch-playwright",
});
