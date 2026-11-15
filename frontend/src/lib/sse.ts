/**
 * SSE (Server-Sent Events) パーサ / 読み取り primitives。
 *
 * README §9.6 / 契約 §2 の monitor frame ストリームと、§3 の AG-UI SSE を
 * 同一の raw event（event / data / id）として受け取るための低層レイヤー。
 * 再接続と replay 自体は `src/lib/monitor.ts` が担当する。
 */

export interface SseFrame {
  /** `event:` 行。無ければ `"message"`（SSE 仕様上のデフォルト）。 */
  event: string;
  /** `data:` 行を `\n` で結合したもの。 */
  data: string;
  /** `id:` 行（無ければ null）。Replay の Last-Event-ID に使う。 */
  id: string | null;
  /** heartbeat などの comment-only block（`retry:` だけの block を含む）。 */
  commentOnly: boolean;
}

export interface SseFrameWithJson<T = unknown> extends SseFrame {
  json: T | null;
  /** data があり JSON として parse できなかった true。 */
  parseError: boolean;
}

export interface SseReadOptions {
  signal?: AbortSignal;
  /** idle な stream を打ち切る上限（テスト用）。0 以下で無効。 */
  maxIdleMs?: number;
}

const DEFAULT_EVENT_NAME = "message";

/** 1 block（`\n\n` 区切り）を SSE frame へ変換する。不正なら null。 */
export function parseSseEventBlock(block: string): SseFrame | null {
  const lines = block.split(/\r?\n/);
  const dataLines: string[] = [];
  let event: string | null = null;
  let id: string | null = null;
  let sawField = false;

  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, "");
    if (line === "") {
      continue;
    }
    if (line.startsWith(":")) {
      continue; // heartbeat comment
    }
    if (line === "retry:" || line.startsWith("retry:")) {
      sawField = true;
      continue;
    }
    const sep = line.indexOf(":");
    const field = sep === -1 ? line : line.slice(0, sep);
    const rawValue = sep === -1 ? "" : line.slice(sep + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    switch (field) {
      case "data":
        sawField = true;
        dataLines.push(value);
        break;
      case "event":
        sawField = true;
        event = value;
        break;
      case "id":
        sawField = true;
        if (value !== "\0") {
          id = value;
        }
        break;
      default:
        break; // retry / 未知 field は無視
    }
  }

  if (!sawField) {
    return null;
  }
  const data = dataLines.join("\n");
  if (data === "" && dataLines.length === 0) {
    // comment / retry only block -> heartbeat として扱う
    return { event: "", data: "", id, commentOnly: true };
  }
  return {
    event: event ?? DEFAULT_EVENT_NAME,
    data,
    id,
    commentOnly: data === "" && event === null,
  };
}

/** data を JSON parse する (`parseError` で失敗を区別)。 */
export function jsonFrames<T = unknown>(
  frames: AsyncIterable<SseFrame>,
): AsyncGenerator<SseFrameWithJson<T>> {
  async function* gen(): AsyncGenerator<SseFrameWithJson<T>> {
    for await (const frame
      of frames) {
      let json: T | null = null;
      let parseError = false;
      if (frame.data !== "") {
        try {
          json = JSON.parse(frame.data) as T;
        } catch {
          parseError = true;
        }
      }
      yield { ...frame, json, parseError };
    }
  }
  return gen();
}

/** `ReadableStream<Uint8Array>` を SSE frame 列へ変換する。 */
export async function* readSseFrames(
  stream: ReadableStream<Uint8Array>,
  options: SseReadOptions = {},
): AsyncGenerator<SseFrame> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  const maxIdleMs = options.maxIdleMs ?? 0;
  const signal = options.signal;

  try {
    for (;;) {
      if (signal?.aborted) {
        return;
      }
      const read = reader.read();
      let timeout: ReturnType<typeof setTimeout> | undefined;
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        if (maxIdleMs > 0) {
          result = await Promise.race([
            read,
            new Promise<never>((_, reject) => {
              timeout = setTimeout(
                () => reject(new Error(`SSE read timeout after ${maxIdleMs}ms`)),
                maxIdleMs,
              );
            }),
          ]);
        } else {
          result = await read;
        }
      } finally {
        if (timeout) {
          clearTimeout(timeout);
        }
      }
      if (result.done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(result.value, { stream: true });
      let boundary = findBoundary(buffer);
      while (boundary !== null) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const parsed = parseSseEventBlock(block);
        if (parsed) {
          yield parsed;
        }
        boundary = findBoundary(buffer);
      }
    }
    const tail = buffer.replace(/^\r?\n/, "");
    const parsed = parseSseEventBlock(tail);
    if (parsed) {
      yield parsed;
    }
  } finally {
    reader.releaseLock();
  }
}

/** fetch → SSE frame 列。SSE は buffer せずに逐次 yield する。 */
export async function* fetchSseFrames(
  url: string,
  init: RequestInit = {},
): AsyncGenerator<SseFrame> {
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: { Accept: "text/event-stream", ...(init.headers ?? {}) },
      cache: "no-store",
    });
  } catch (err) {
    throw new Error(
      `failed to request ${url}: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  if (!res.ok) {
    throw new Error(`SSE request failed: ${res.status} ${res.statusText}`);
  }
  if (!res.body) {
    throw new Error("SSE response has no body");
  }
  yield* readSseFrames(res.body);
}

/** JSON body を読む (@ag-ui / AG-UI の capability envelope は content-type 無視)。 */
export async function parseJsonLoose<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (text === "") {
    throw new Error("empty JSON response");
  }
  let offset = 0;
  while (offset < text.length) {
    const candidate = text.slice(offset);
    try {
      return JSON.parse(candidate) as T;
    } catch (err) {
      const message = err instanceof Error ? err.message : "";
      const match = /at position (\d+)/.exec(message);
      if (!match) {
        throw err;
      }
      const at = Number(match[1]);
      if (!Number.isFinite(at) || at <= offset) {
        throw err;
      }
      offset = at;
    }
  }
  throw new Error("invalid JSON response");
}

/** Chat stream terminal frame 判定 (message_complete / error)。 */
export function isTerminalChatFrame(event: string): boolean {
  return event === "message_complete" || event === "error";
}

/** buffer 内の最初の `\n\n`（または `\r\n\r\n`）を探す。 */
export function findBoundary(
  buffer: string,
): { index: number; length: number } | null {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1 && crlf === -1) {
    return null;
  }
  if (crlf !== -1 && (lf === -1 || crlf < lf)) {
    return { index: crlf, length: 4 };
  }
  return { index: lf, length: 2 };
}

/** delay 可能な exponential backoff（chat reconnect 用。monitor は独自実装）。 */
export class Backoff {
  private attempt = 0;

  constructor(
    private readonly baseDelayMs = 1000,
    private readonly maxDelayMs = 30_000,
    private readonly rng: () => number = () => 0,
  ) {}

  nextDelayMs(): number {
    const exponential = Math.min(
      this.maxDelayMs,
      this.baseDelayMs * Math.pow(2, this.attempt),
    );
    const jitter = exponential * 0.2 * this.rng();
    this.attempt += 1;
    return Math.round(exponential + jitter);
  }

  reset(): void {
    this.attempt = 0;
  }
}
