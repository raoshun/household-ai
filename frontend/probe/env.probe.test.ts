import { describe, expect, it } from "vitest";

describe("env", () => {
  it("globals", () => {
    const g = globalThis as Record<string, unknown>;
    const names = ["fetch", "Response", "ReadableStream", "TextDecoder", "EventSource", "TextEncoder", "AbortController", "Event"];
    const report = names.map((n) => `${n}=${typeof g[n]}`).join(" ");
    console.log("GLOBALS:", report);
    expect(typeof g.window).toBe("object");
  });
});
