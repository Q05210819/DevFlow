import { defineConfig, devices } from "@playwright/test";

const port = 31847;

export default defineConfig({
  testDir: "./tests",
  // Real-stack specs own the same loopback backend port and isolated DB schemas.
  workers: 1,
  use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    env: { DEVFLOW_API_ORIGIN: "http://127.0.0.1:31848" },
    command: `${process.platform === "win32" ? "npm.cmd" : "npm"} run dev -- --hostname 127.0.0.1 --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
