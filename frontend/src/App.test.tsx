import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { deleteJob, inspectZip, startJob } from "./api";

vi.mock("./api", () => ({
  deleteJob: vi.fn(),
  downloadUrl: vi.fn((jobId: string) => `/api/jobs/${jobId}/download`),
  getJob: vi.fn(),
  getLogs: vi.fn(),
  getReport: vi.fn(),
  inspectZip: vi.fn(),
  startJob: vi.fn()
}));

const mockedInspectZip = vi.mocked(inspectZip);
const mockedStartJob = vi.mocked(startJob);
const mockedDeleteJob = vi.mocked(deleteJob);

afterEach(cleanup);

describe("App archive selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDeleteJob.mockResolvedValue(undefined);
  });

  it("invalidates the inspected job when a different archive is selected", async () => {
    const user = userEvent.setup();
    mockedInspectZip
      .mockResolvedValueOnce({ job_id: "a".repeat(32), filename: "first.zip", size: 3, warnings: [], detected_format: "polygon", format_candidates: [] })
      .mockResolvedValueOnce({ job_id: "b".repeat(32), filename: "second.zip", size: 3, warnings: [], detected_format: "polygon", format_candidates: [] });
    mockedStartJob.mockResolvedValue({
      id: "b".repeat(32),
      status: "queued",
      created_at: "2026-07-22T00:00:00Z",
      started_at: null,
      finished_at: null,
      exit_code: null,
      download_ready: false,
      error: null,
      source_format: "hydro",
      target_format: "fps",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 }
    });
    render(<App />);

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).not.toBeNull();
    const startButton = screen.getByRole("button", { name: "启动容器转换" });
    const inspectButton = screen.getByRole("button", { name: "上传并检查" });
    const first = new File(["one"], "first.zip", { type: "application/zip" });
    const second = new File(["two"], "second.zip", { type: "application/zip" });

    await user.upload(fileInput!, first);
    await user.click(inspectButton);
    expect((await screen.findAllByText("first.zip")).length).toBeGreaterThan(0);
    expect((startButton as HTMLButtonElement).disabled).toBe(false);

    await user.upload(fileInput!, second);

    expect(screen.queryAllByText("first.zip")).toHaveLength(0);
    expect((startButton as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(mockedDeleteJob).toHaveBeenCalledWith("a".repeat(32)));
    expect(mockedStartJob).not.toHaveBeenCalled();

    await user.click(inspectButton);
    expect((await screen.findAllByText("second.zip")).length).toBeGreaterThan(0);
    expect((startButton as HTMLButtonElement).disabled).toBe(false);
    await user.click(startButton);

    await waitFor(() => expect(mockedStartJob).toHaveBeenCalledTimes(1));
    expect(mockedStartJob.mock.calls[0][0].job_id).toBe("b".repeat(32));
  });

  it("requires an explicit source when automatic detection is ambiguous", async () => {
    const user = userEvent.setup();
    mockedInspectZip.mockResolvedValue({
      job_id: "c".repeat(32),
      filename: "data.zip",
      size: 3,
      warnings: ["不会执行上传包中的脚本"],
      detected_format: null,
      format_candidates: [
        {
          format: "generic",
          confidence: 0.55,
          evidence: ["found paired .in/.out files"]
        }
      ]
    });
    render(<App />);

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).not.toBeNull();
    await user.upload(fileInput!, new File(["zip"], "data.zip", { type: "application/zip" }));
    await user.click(screen.getByRole("button", { name: "上传并检查" }));

    expect(await screen.findByText("自动识别未达到置信阈值，请手动选择输入格式。")).not.toBeNull();
    expect(screen.getByText(/通用测试数据目录 · 55%/)).not.toBeNull();
    expect(screen.getByText("不会执行上传包中的脚本")).not.toBeNull();
    const startButton = screen.getByRole("button", { name: "启动容器转换" });
    expect((startButton as HTMLButtonElement).disabled).toBe(true);

    await user.selectOptions(screen.getByRole("combobox", { name: "输入格式" }), "generic");
    expect(screen.queryByText("自动识别未达到置信阈值，请手动选择输入格式。")).toBeNull();
    expect((startButton as HTMLButtonElement).disabled).toBe(false);
  });

  it("does not resurrect a job when reset wins a pending start request", async () => {
    const user = userEvent.setup();
    let resolveStart: ((value: Awaited<ReturnType<typeof startJob>>) => void) | undefined;
    mockedInspectZip.mockResolvedValue({
      job_id: "d".repeat(32),
      filename: "polygon.zip",
      size: 3,
      warnings: [],
      detected_format: "polygon",
      format_candidates: []
    });
    mockedStartJob.mockReturnValue(
      new Promise((resolve) => {
        resolveStart = resolve;
      })
    );
    render(<App />);

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).not.toBeNull();
    await user.upload(fileInput!, new File(["zip"], "polygon.zip", { type: "application/zip" }));
    await user.click(screen.getByRole("button", { name: "上传并检查" }));
    await screen.findByText("识别为 Polygon / Codeforces");
    await user.click(screen.getByRole("button", { name: "启动容器转换" }));
    await waitFor(() => expect(mockedStartJob).toHaveBeenCalledOnce());

    await user.click(screen.getByRole("button", { name: "清理任务" }));
    await waitFor(() => expect(mockedDeleteJob).toHaveBeenCalledWith("d".repeat(32)));
    expect(screen.queryByText("polygon.zip")).toBeNull();

    resolveStart?.({
      id: "d".repeat(32),
      status: "queued",
      created_at: "2026-07-23T00:00:00Z",
      started_at: null,
      finished_at: null,
      exit_code: null,
      download_ready: false,
      error: null,
      source_format: "polygon",
      target_format: "hydro",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 }
    });
    await waitFor(() => expect(screen.queryByText("等待")).toBeNull());
    expect(screen.queryByText("polygon.zip")).toBeNull();
  });
});
