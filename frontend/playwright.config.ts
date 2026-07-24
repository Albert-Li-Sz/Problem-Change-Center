import { defineConfig, devices } from "@playwright/test";

import {
  dataRoot,
  fakeDockerPath,
  frontendRoot,
  pythonExecutable,
  repositoryRoot
} from "./e2e/paths";

const backendPort = Number(process.env.P2H_E2E_BACKEND_PORT ?? 8765);
const frontendPort = Number(process.env.P2H_E2E_FRONTEND_PORT ?? 4173);
const externalBackendPort = 11451;
const externalFrontendPort = 11452;
const externalHost = process.env.P2H_E2E_EXTERNAL_HOST ?? "::1";
const externalUrlHost = externalHost.includes(":")
  ? `[${externalHost}]`
  : externalHost;
const externalAccessKeyHash =
  "pbkdf2_sha256$600000$00112233445566778899aabbccddeeff$5bd29e9f2eaf09cc0ee0545ed776a4bfa6b84adea62e97efdb9b61b398d712b0";

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
      name: "internal-chromium",
      testIgnore: "**/external-auth.spec.ts",
      use: {
        ...devices["Desktop Chrome"],
        channel: process.env.CI ? undefined : "chrome"
      }
    },
    {
      name: "external-chromium",
      testMatch: "**/external-auth.spec.ts",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: `http://${externalUrlHost}:${externalFrontendPort}`,
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
    },
    {
      command: `${pythonExecutable} -m uvicorn app.main:app --host ${externalHost} --port ${externalBackendPort}`,
      cwd: `${repositoryRoot}/backend`,
      env: {
        P2H_DEPLOYMENT_MODE: "external",
        P2H_DATA_DIR: `${dataRoot}-external`,
        P2H_DOCKER_BIN: fakeDockerPath,
        P2H_ALLOWED_HOSTS: `${externalHost},localhost`,
        P2H_ALLOWED_ORIGINS: `http://${externalUrlHost}:${externalFrontendPort}`,
        P2H_ACCESS_KEY_HASH: externalAccessKeyHash,
        P2H_JOB_TIMEOUT_SECONDS: "30",
        P2H_JOB_IDLE_TIMEOUT_SECONDS: "15",
        P2H_JOB_STAGE_TIMEOUT_SECONDS: "15",
        P2H_JOB_PROBLEM_TIMEOUT_SECONDS: "15"
      },
      url: `http://${externalUrlHost}:${externalBackendPort}/api/health/live`,
      reuseExistingServer: false,
      timeout: 30_000
    },
    {
      command: `npm run dev -- --host ${externalHost} --port ${externalFrontendPort}`,
      cwd: frontendRoot,
      env: {
        P2H_BACKEND_HOST: externalUrlHost,
        P2H_BACKEND_PORT: String(externalBackendPort)
      },
      url: `http://${externalUrlHost}:${externalFrontendPort}`,
      reuseExistingServer: false,
      timeout: 30_000
    }
  ]
});
