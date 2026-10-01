import { defineConfig, devices } from "@playwright/test";

const port = 31847;

export default defineConfig({
  testDir: "./tests",
  use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    command: `${process.platform === "win32" ? "npm.cmd" : "npm"} run dev -- --hostname 127.0.0.1 --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
