import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const repository = path.resolve(import.meta.dirname, "..");
const python = process.env.P2H_E2E_PYTHON ?? path.join(repository, "backend/.venv/bin/python");

export default defineConfig({
  testDir: "./public-e2e",
  workers: 1,
  timeout: 90_000,
  reporter: "line",
  use: { ...devices["Desktop Chrome"], baseURL: "http://127.0.0.1:4180", channel: process.env.CI ? undefined : "chrome", trace: "retain-on-failure" },
  webServer: [
    { command: `${python} tests/public_browser_server.py`, cwd: path.join(repository, "backend"), env: { PYTHONPATH: "." }, url: "http://127.0.0.1:8780/api/health/live", timeout: 60_000, reuseExistingServer: false },
    { command: "npm run dev -- --host 127.0.0.1 --port 4180", env: { VITE_PUBLIC_MODE: "1", P2H_BACKEND_PORT: "8780" }, url: "http://127.0.0.1:4180", reuseExistingServer: false }
  ]
});
