import { defineConfig } from "@playwright/test";
const port = process.env.FRONTEND_TEST_PORT || "3002";
export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  timeout: 30000,
  use: {
    baseURL: `http://localhost:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `npm run build && npm run preview -- --port ${port}`,
    url: `http://localhost:${port}`,
    reuseExistingServer: false,
  },
});
