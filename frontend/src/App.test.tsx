import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  applyRepairs,
  cancelJob,
  deleteJob,
  getJob,
  getLogs,
  getReport,
  inspectZip,
  startJob
} from "./api";

vi.mock("./api", () => ({
  applyRepairs: vi.fn(),
  cancelJob: vi.fn(),
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
const mockedCancelJob = vi.mocked(cancelJob);
const mockedApplyRepairs = vi.mocked(applyRepairs);
const mockedGetJob = vi.mocked(getJob);
const mockedGetLogs = vi.mocked(getLogs);
const mockedGetReport = vi.mocked(getReport);

afterEach(cleanup);

describe("App archive selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDeleteJob.mockResolvedValue(undefined);
    mockedCancelJob.mockResolvedValue(undefined);
    mockedGetLogs.mockResolvedValue("");
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

  it("shows structured progress and cancels while retaining diagnostics", async () => {
    const user = userEvent.setup();
    const jobId = "e".repeat(32);
    mockedInspectZip.mockResolvedValue({
      job_id: jobId,
      filename: "large.zip",
      size: 1024,
      warnings: [],
      detected_format: "hydro",
      format_candidates: []
    });
    mockedStartJob.mockResolvedValue({
      id: jobId,
      status: "running",
      created_at: "2026-07-23T00:00:00Z",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: null,
      exit_code: null,
      download_ready: false,
      error: null,
      source_format: "hydro",
      target_format: "icpc",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 },
      progress: {
        phase: "write",
        current: 10,
        total: 20,
        unit: "problems",
        problem: "sum",
        detail: "writing problem package",
        started_at: "2026-07-23T00:00:00Z",
        last_activity_at: "2026-07-23T00:00:15Z"
      },
      timeout: null
    });
    mockedGetJob.mockResolvedValue({
      id: jobId,
      status: "cancelled",
      created_at: "2026-07-23T00:00:00Z",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: "2026-07-23T00:00:16Z",
      exit_code: null,
      download_ready: false,
      error: "Cancelled by user",
      source_format: "hydro",
      target_format: "icpc",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 },
      progress: null,
      timeout: null
    });
    render(<App />);

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    await user.upload(
      fileInput!,
      new File(["zip"], "large.zip", { type: "application/zip" })
    );
    await user.click(screen.getByRole("button", { name: "上传并检查" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "输出格式" }),
      "icpc"
    );
    await user.click(screen.getByRole("button", { name: "启动容器转换" }));

    expect(await screen.findByText("写出目标题包")).not.toBeNull();
    expect(screen.getByText("sum · 50%")).not.toBeNull();
    expect(screen.getByText(/阶段耗时 15 秒/)).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "取消并保留诊断" }));
    await waitFor(() => expect(mockedCancelJob).toHaveBeenCalledWith(jobId));
    expect(mockedGetJob).toHaveBeenCalledWith(jobId);
    expect(mockedGetLogs).toHaveBeenCalledWith(jobId);
    expect(await screen.findByText("Cancelled by user")).not.toBeNull();
  });

  it("does not resurrect a cancelled job when reset wins pending diagnostics", async () => {
    const user = userEvent.setup();
    const jobId = "f".repeat(32);
    let resolveCancel: (() => void) | undefined;
    let resolveJob: ((value: Awaited<ReturnType<typeof getJob>>) => void) | undefined;
    let resolveLogs: ((value: string) => void) | undefined;
    mockedInspectZip.mockResolvedValue({
      job_id: jobId,
      filename: "pending.zip",
      size: 3,
      warnings: [],
      detected_format: "hydro",
      format_candidates: []
    });
    mockedStartJob.mockResolvedValue({
      id: jobId,
      status: "running",
      created_at: "2026-07-23T00:00:00Z",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: null,
      exit_code: null,
      download_ready: false,
      error: null,
      source_format: "hydro",
      target_format: "icpc",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 }
    });
    mockedCancelJob.mockReturnValue(
      new Promise((resolve) => {
        resolveCancel = resolve;
      })
    );
    mockedGetJob.mockReturnValue(
      new Promise((resolve) => {
        resolveJob = resolve;
      })
    );
    mockedGetLogs.mockReturnValue(
      new Promise((resolve) => {
        resolveLogs = resolve;
      })
    );
    render(<App />);

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    await user.upload(
      fileInput!,
      new File(["zip"], "pending.zip", { type: "application/zip" })
    );
    await user.click(screen.getByRole("button", { name: "上传并检查" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "输出格式" }),
      "icpc"
    );
    await user.click(screen.getByRole("button", { name: "启动容器转换" }));
    await screen.findByRole("button", { name: "取消并保留诊断" });

    await user.click(screen.getByRole("button", { name: "取消并保留诊断" }));
    await waitFor(() => expect(mockedCancelJob).toHaveBeenCalledWith(jobId));
    await user.click(screen.getByRole("button", { name: "清理任务" }));
    expect(screen.queryByText("pending.zip")).toBeNull();

    resolveCancel?.();
    await waitFor(() => expect(mockedGetJob).toHaveBeenCalledWith(jobId));
    resolveJob?.({
      id: jobId,
      status: "cancelled",
      created_at: "2026-07-23T00:00:00Z",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: "2026-07-23T00:00:01Z",
      exit_code: null,
      download_ready: false,
      error: "Cancelled by user",
      source_format: "hydro",
      target_format: "icpc",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 }
    });
    resolveLogs?.("old diagnostics");

    await waitFor(() => expect(mockedDeleteJob).toHaveBeenCalledWith(jobId));
    expect(screen.queryByText("pending.zip")).toBeNull();
    expect(screen.queryByText("Cancelled by user")).toBeNull();
    expect(screen.queryByText("old diagnostics")).toBeNull();
  });

  it("does not resurrect a repair job when reset wins the pending request", async () => {
    const user = userEvent.setup();
    const jobId = "1".repeat(32);
    let resolveRepair:
      | ((value: Awaited<ReturnType<typeof applyRepairs>>) => void)
      | undefined;
    mockedInspectZip.mockResolvedValue({
      job_id: jobId,
      filename: "repair.zip",
      size: 3,
      warnings: [],
      detected_format: "hydro",
      format_candidates: []
    });
    mockedStartJob.mockResolvedValue({
      id: jobId,
      status: "running",
      created_at: "2026-07-23T00:00:00Z",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: null,
      exit_code: null,
      download_ready: false,
      error: null,
      source_format: "hydro",
      target_format: "icpc",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 }
    });
    mockedGetJob.mockResolvedValue({
      id: jobId,
      status: "failed",
      created_at: "2026-07-23T00:00:00Z",
      started_at: "2026-07-23T00:00:00Z",
      finished_at: "2026-07-23T00:00:01Z",
      exit_code: 1,
      download_ready: false,
      error: "missing output",
      source_format: "hydro",
      target_format: "icpc",
      report_ready: true,
      report_counts: { warning: 0, loss: 0, fatal: 1 }
    });
    mockedGetReport.mockResolvedValue({
      schema_version: 2,
      source_format: "hydro",
      target_format: "icpc",
      problem_count: 1,
      counts: { warning: 0, loss: 0, fatal: 1 },
      issues: [
        {
          severity: "fatal",
          code: "missing-output",
          message: "missing output",
          problem: "P1000",
          field: "cases",
          context: { expected_path: "P1000/1.ans" }
        }
      ],
      artifacts: [],
      repair_ready: true,
      repair_suggestions: [
        {
          id: "a".repeat(24),
          issue_code: "missing-output",
          expected_path: "P1000/1.ans",
          role: "output",
          problem: "P1000",
          candidates: [
            {
              path: "P1000/1.out",
              strategy: "extension-alias",
              confidence: 0.95
            }
          ],
          requires_upload: false
        }
      ]
    });
    mockedApplyRepairs.mockReturnValue(
      new Promise((resolve) => {
        resolveRepair = resolve;
      })
    );
    render(<App />);

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]');
    await user.upload(
      fileInput!,
      new File(["zip"], "repair.zip", { type: "application/zip" })
    );
    await user.click(screen.getByRole("button", { name: "上传并检查" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "输出格式" }),
      "icpc"
    );
    await user.click(screen.getByRole("button", { name: "启动容器转换" }));

    expect(
      await screen.findByText("缺失文件修复助手", {}, { timeout: 3000 })
    ).not.toBeNull();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "选择 P1000/1.ans 的包内候选" }),
      "P1000/1.out"
    );
    await user.click(screen.getByRole("button", { name: "确认修复并重新转换" }));
    await waitFor(() => expect(mockedApplyRepairs).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: "清理任务" }));
    expect(screen.queryByText("repair.zip")).toBeNull();

    resolveRepair?.({
      id: "2".repeat(32),
      status: "queued",
      created_at: "2026-07-23T00:00:02Z",
      started_at: null,
      finished_at: null,
      exit_code: null,
      download_ready: false,
      error: null,
      source_format: "hydro",
      target_format: "icpc",
      report_ready: false,
      report_counts: { warning: 0, loss: 0, fatal: 0 }
    });

    await waitFor(() => expect(mockedDeleteJob).toHaveBeenCalledWith(jobId));
    expect(screen.queryByText("repair.zip")).toBeNull();
    expect(screen.queryByText("等待")).toBeNull();
  });
});
