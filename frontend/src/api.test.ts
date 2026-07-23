import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyRepairs,
  inspectZip,
  JobEventHandlers,
  subscribeToJobEvents
} from "./api";

describe("API error handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
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

  it("subscribes to typed job events and closes the EventSource", () => {
    const listeners = new Map<string, (event: Event) => void>();
    const close = vi.fn();
    const source = {
      onopen: null as ((event: Event) => void) | null,
      onerror: null as ((event: Event) => void) | null,
      addEventListener: vi.fn(
        (name: string, listener: (event: Event) => void) => {
          listeners.set(name, listener);
        }
      ),
      close
    };
    const eventSourceConstructor = vi.fn();
    function EventSourceMock(_url: string) {
      eventSourceConstructor(_url);
      return source;
    }
    vi.stubGlobal("EventSource", EventSourceMock);
    const handlers: JobEventHandlers = {
      onOpen: vi.fn(),
      onJob: vi.fn(),
      onLogs: vi.fn(),
      onReport: vi.fn(),
      onCursor: vi.fn(),
      onError: vi.fn()
    };

    const unsubscribe = subscribeToJobEvents("a".repeat(32), handlers);
    source.onopen?.(new Event("open"));
    listeners.get("job")?.(
      new MessageEvent("job", {
        data: JSON.stringify({ id: "a".repeat(32), status: "running" })
      })
    );
    listeners.get("logs")?.(
      new MessageEvent("logs", {
        data: JSON.stringify({
          text: "runner started\n",
          offset: 0,
          next_offset: 15,
          reset: false
        }),
        lastEventId: "4:15"
      })
    );
    listeners.get("report")?.(
      new MessageEvent("report", {
        data: JSON.stringify({ schema_version: 2, problem_count: 1 })
      })
    );
    unsubscribe?.();

    expect(eventSourceConstructor).toHaveBeenCalledWith(
      `/api/jobs/${"a".repeat(32)}/events`
    );
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
    expect(close).toHaveBeenCalledOnce();
  });
});
