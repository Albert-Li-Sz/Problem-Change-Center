import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchEventSource } from "@microsoft/fetch-event-source";

import {
  applyRepairs,
  downloadJob,
  inspectZip,
  JobEventHandlers,
  storeAccessKey,
  subscribeToJobEvents
} from "./api";


vi.mock("@microsoft/fetch-event-source", () => ({
  fetchEventSource: vi.fn()
}));

describe("API error handling", () => {
  afterEach(() => {
    sessionStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("formats FastAPI validation arrays as readable field messages", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              {
                type: "string_pattern_mismatch",
                loc: ["body", "options", "hydro", "pid_start"],
                msg: "String should match pattern"
              }
            ]
          }),
          {
            status: 422,
            statusText: "Unprocessable Entity",
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    await expect(
      inspectZip(new File(["zip"], "test.zip", { type: "application/zip" }))
    ).rejects.toThrow("options.hydro.pid_start: String should match pattern");
  });

  it("submits an explicit repair plan and matching supplemental files", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "b".repeat(32),
          status: "queued",
          created_at: "2026-07-23T00:00:00Z",
          started_at: null,
          finished_at: null,
          exit_code: null,
          download_ready: false,
          error: null,
          source_format: "hydro",
          target_format: "icpc",
          report_ready: false,
          report_counts: { warning: 0, loss: 0, fatal: 0 }
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" }
        }
      )
    );
    vi.stubGlobal("fetch", fetchMock);
    const supplement = new File(["answer"], "1.ans");

    await applyRepairs(
      "a".repeat(32),
      [
        {
          suggestion_id: "1".repeat(24),
          upload_name: "1.ans"
        }
      ],
      [supplement]
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/jobs/${"a".repeat(32)}/repairs`);
    expect(init.method).toBe("POST");
    const body = init.body as FormData;
    expect(JSON.parse(String(body.get("plan")))).toEqual({
      selections: [
        {
          suggestion_id: "1".repeat(24),
          upload_name: "1.ans"
        }
      ]
    });
    expect((body.get("files") as File).name).toBe("1.ans");
  });

  it("subscribes to typed job events with the access key header", async () => {
    storeAccessKey("session-only-key");
    vi.mocked(fetchEventSource).mockImplementation(async (_url, options) => {
      await options.onopen?.(
        new Response(null, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" }
        })
      );
      options.onmessage?.({
        data: JSON.stringify({ id: "a".repeat(32), status: "running" }),
        event: "job",
        id: "4:0",
        retry: undefined
      });
      options.onmessage?.({
        data: JSON.stringify({
          text: "runner started\n",
          offset: 0,
          next_offset: 15,
          reset: false
        }),
        event: "logs",
        id: "4:15",
        retry: undefined
      });
      options.onmessage?.({
        data: JSON.stringify({ schema_version: 2, problem_count: 1 }),
        event: "report",
        id: "4:15",
        retry: undefined
      });
    });
    const handlers: JobEventHandlers = {
      onOpen: vi.fn(),
      onJob: vi.fn(),
      onLogs: vi.fn(),
      onReport: vi.fn(),
      onCursor: vi.fn(),
      onError: vi.fn()
    };

    const unsubscribe = subscribeToJobEvents("a".repeat(32), handlers);
    await vi.waitFor(() => expect(handlers.onReport).toHaveBeenCalledOnce());
    unsubscribe?.();

    const [url, options] = vi.mocked(fetchEventSource).mock.calls[0];
    expect(url).toBe(`/api/jobs/${"a".repeat(32)}/events`);
    expect(new Headers(options.headers).get("X-P2H-Access-Key")).toBe(
      "session-only-key"
    );
    expect(options.signal?.aborted).toBe(true);
    expect(handlers.onOpen).toHaveBeenCalledOnce();
    expect(handlers.onJob).toHaveBeenCalledWith(
      expect.objectContaining({ status: "running" })
    );
    expect(handlers.onLogs).toHaveBeenCalledWith({
      text: "runner started\n",
      offset: 0,
      next_offset: 15,
      reset: false
    });
    expect(handlers.onCursor).toHaveBeenCalledWith("4:15");
    expect(handlers.onReport).toHaveBeenCalledWith(
      expect.objectContaining({ problem_count: 1 })
    );
  });

  it("downloads with the access key header and server-provided filename", async () => {
    storeAccessKey("session-download-key");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["zip"]), {
        status: 200,
        headers: {
          "Content-Disposition": 'attachment; filename="converted.zip"'
        }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const createObjectURL = vi.fn(() => "blob:download");
    const revokeObjectURL = vi.fn();
    class DownloadUrl extends URL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    }
    vi.stubGlobal("URL", DownloadUrl);
    let clickedFilename = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      clickedFilename = this.download;
    });

    await downloadJob("a".repeat(32));

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-P2H-Access-Key")).toBe(
      "session-download-key"
    );
    expect(clickedFilename).toBe("converted.zip");
    expect(createObjectURL).toHaveBeenCalledOnce();
  });
});
