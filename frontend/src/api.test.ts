import { afterEach, describe, expect, it, vi } from "vitest";

import { applyRepairs, inspectZip } from "./api";

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
});
