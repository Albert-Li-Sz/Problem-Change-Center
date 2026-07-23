import { describe, expect, it } from "vitest";

import { LogChunk } from "../api";
import { logChunkAction } from "./useJobLifecycle";


function chunk(
  offset: number,
  nextOffset: number,
  reset = false
): LogChunk {
  return { text: "data", offset, next_offset: nextOffset, reset };
}

describe("logChunkAction", () => {
  it("appends contiguous chunks and ignores replayed chunks", () => {
    expect(logChunkAction(10, chunk(10, 20))).toBe("append");
    expect(logChunkAction(20, chunk(10, 20))).toBe("ignore");
  });

  it("replaces reset streams and resyncs overlapping chunks", () => {
    expect(logChunkAction(20, chunk(0, 8, true))).toBe("replace");
    expect(logChunkAction(20, chunk(10, 30))).toBe("resync");
  });
});
