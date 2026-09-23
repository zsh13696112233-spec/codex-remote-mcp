import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests",
  testMatch: "*.spec.ts",
  workers: 1,
  outputDir: "../target/sop-browser-results",
  use: {
    baseURL: "http://127.0.0.1:18091",
    channel: "chrome",
    viewport: { width: 1600, height: 1400 },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "node tests/preview-server.mjs",
    url: "http://127.0.0.1:18091",
    reuseExistingServer: false,
  },
});
