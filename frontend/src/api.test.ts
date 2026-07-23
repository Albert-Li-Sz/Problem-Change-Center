import { afterEach, describe, expect, it, vi } from "vitest";

import { inspectZip } from "./api";

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
});
