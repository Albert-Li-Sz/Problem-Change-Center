import { defineConfig, devices } from "@playwright/test";

import {
  dataRoot,
  fakeDockerPath,
  frontendRoot,
  pythonExecutable,
  repositoryRoot
} from "./e2e/paths";

const backendPort = 8765;
const frontendPort = 4173;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: process.env.CI ? "retain-on-failure" : "off"
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        channel: process.env.CI ? undefined : "chrome"
      }
    }
  ],
  webServer: [
    {
      command: `${pythonExecutable} -m uvicorn app.main:app --host 127.0.0.1 --port ${backendPort}`,
      cwd: `${repositoryRoot}/backend`,
      env: {
        P2H_DATA_DIR: dataRoot,
        P2H_DOCKER_BIN: fakeDockerPath,
        P2H_JOB_TIMEOUT_SECONDS: "30",
        P2H_JOB_IDLE_TIMEOUT_SECONDS: "15",
        P2H_JOB_STAGE_TIMEOUT_SECONDS: "15",
        P2H_JOB_PROBLEM_TIMEOUT_SECONDS: "15"
      },
      url: `http://127.0.0.1:${backendPort}/api/health`,
      reuseExistingServer: false,
      timeout: 30_000
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort}`,
      cwd: frontendRoot,
      env: {
        P2H_BACKEND_HOST: "127.0.0.1",
        P2H_BACKEND_PORT: String(backendPort)
      },
      url: `http://127.0.0.1:${frontendPort}`,
      reuseExistingServer: false,
      timeout: 30_000
    }
  ]
});
