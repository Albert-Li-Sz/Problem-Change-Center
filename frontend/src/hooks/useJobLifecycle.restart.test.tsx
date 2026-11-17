// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { JobResponse } from "../api";

const subscribe = vi.hoisted(() => vi.fn(() => () => undefined));
vi.mock("../api", () => ({
  subscribeToJobEvents: subscribe,
  getJob: vi.fn(),
  getLogs: vi.fn(),
  getReport: vi.fn()
}));

import { useJobLifecycle } from "./useJobLifecycle";

function job(status: JobResponse["status"]): JobResponse {
  return {
    id: "a".repeat(32),
    status,
    created_at: "",
    started_at: null,
    finished_at: null,
    exit_code: null,
    download_ready: false,
    error: null,
    source_format: null,
    target_format: null,
    report_ready: false,
    report_counts: { warning: 0, loss: 0, fatal: 0 }
  };
}

it("subscribes again when a finished job is restarted under the same id", () => {
  const options = {
    requestEpoch: { current: 0 },
    setJob: vi.fn(),
    setLogs: vi.fn(),
    setReport: vi.fn(),
    setError: vi.fn()
  };
  const { rerender } = renderHook(
    ({ status }: { status: JobResponse["status"] }) =>
      useJobLifecycle({ ...options, job: job(status) }),
    { initialProps: { status: "queued" as JobResponse["status"] } }
  );
  expect(subscribe).toHaveBeenCalledTimes(1);

  rerender({ status: "running" });
  rerender({ status: "failed" });
  expect(subscribe).toHaveBeenCalledTimes(1);

  rerender({ status: "queued" });
  expect(subscribe).toHaveBeenCalledTimes(2);
});
