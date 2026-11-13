import { expect, it } from "vitest";

it("globals available", () => {
  const g = globalThis as Record<string, unknown>;
  const names = ["fetch", "Response", "ReadableStream", "TextDecoder", "TextEncoder", "AbortController", "EventSource", "Event", "window"];
  process.stderr.write("GLOBALS " + names.map((n) => `${n}=${typeof g[n]}`).join(" ") + "\n");
  expect(g.window).toBeDefined();
});
