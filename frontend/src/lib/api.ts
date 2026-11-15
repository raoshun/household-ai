/**
 * Backend REST client (契約 §4 / README §10.1)。
 *
 * - base path は `/api`。Next `rewrites` で `BACKEND_ORIGIN` (既定 http://127.0.0.1:8000) へ
 *   reverse proxy する。CopilotKit runtime server は使わないため `/api/copilotkit` 相当の
 *   Next API route は持たない (README §12.4 / 契約 §9 実装記録)。
 * - problem details envelope `{error_type, code, message, recoverable, details}` を
 *   400/404/409/422/429/502/503 で typed error へ変換する (`ApiError` 以下)。
 * - wire 表記: 契約 §4 の例は snake_case、backend の DTO は camelCase を返すため reader は
 *   両 spelling を受理し、内部 DTO は camelCase に正規化する (types.ts 前文コメント参照)。
 */

import type {
  AgentCapabilitiesDto,
  ApprovalId,
  ApprovalRecord,
  ChatSessionCreateDto,
  ChatSessionDto,
  ChatSessionForkDto,
  ConcurrencyLimitDetails,
  ContextUsageDto,
  CostAmount,
  FramesPageDto,
  HealthDto,
  JudgmentDto,
  ProblemDetail,
  RuleConflictDto,
  RuleDto,
  SessionEventRowDto,
  SessionSnapshotDto,
  ThreadId,
  TokenUsage,
  TurnId,
  TurnInterruptDto,
  TurnAcceptedDto,
  TurnSummaryDto,
} from "./types";
import { parseJsonLoose } from "./sse";
import {
  normalizeApprovalRecord,
  normalizeContextUsage,
  normalizeCostAmount,
  normalizeJudgmentDto,
  normalizeRuleDto,
  normalizeTokenUsage,
} from "./monitor";

export const UNKNOWN_PROBLEM_CODE = "unknown_error.unknown";

/** backend problem details を HTTP status 付きで投げる基底 error。 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetail;

  constructor(status: number, problem: ProblemDetail) {
    super(problem.message || `${status} ${problem.code}`);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
    this.errorType = problem.errorType;
    this.code = problem.code;
    this.recoverable = problem.recoverable;
    this.details = problem.details;
  }

  readonly errorType: string;
  readonly code: string;
  readonly recoverable: boolean;
  readonly details: Record<string, unknown> | null;
}

export class ValidationError extends ApiError {
  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "ValidationError";
  }
}
export class NotFoundException extends ApiError {
  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "NotFoundException";
  }
}
export class ConflictException extends ApiError {
  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "ConflictException";
  }
}
export class UnprocessableEntityException extends ApiError {
  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "UnprocessableEntityException";
  }
}
/** 429 (`concurrency_limit`): `retry_after_seconds` / `running_turn_ids` を保持する。 */
export class ConcurrencyLimitException extends ApiError {
  readonly retryAfterSeconds: number | null;
  readonly runningTurnIds: TurnId[];

  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "ConcurrencyLimitException";
    const details = detailRecord(problem.details) as ConcurrencyLimitDetails & Record<string, unknown>;
    const retry = details.retryAfterSeconds ?? details.retry_after_seconds;
    const turns = details.runningTurnIds ?? details.running_turn_ids;
    this.retryAfterSeconds = typeof retry === "number" ? retry : null;
    this.runningTurnIds = Array.isArray(turns)
      ? turns.filter((v): v is TurnId => typeof v === "string")
      : [];
  }
}
export class AgentErrorException extends ApiError {
  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "AgentErrorException";
  }
}
export class PermissionStaleException extends ApiError {
  constructor(status: number, problem: ProblemDetail) {
    super(status, problem);
    this.name = "PermissionStaleException";
  }
}

function detailRecord(details: Record<string, unknown> | null): Record<string, unknown> {
  return typeof details === "object" && details !== null ? details : {};
}

/** problem details envelope を読む (content-type を無視して body を読むbackendがあるため lenient)。 */
export async function readProblem(res: Response): Promise<ProblemDetail> {
  const text = await res.text().catch(() => "");
  const parsed = looseJson<Record<string, unknown>>(text) ?? {};
  const rawType = pickString(parsed, "error_type", "errorType", "type", "title");
  const code =
    pickString(parsed, "code", "detail") ??
    (rawType && rawType.includes(".") ? rawType : undefined) ??
    UNKNOWN_PROBLEM_CODE;
  const message =
    pickString(parsed, "message", "detail", "title") ?? `${res.status} ${res.statusText}`.trim();
  const rawRecoverable = pickValue<boolean>(parsed, "recoverable");
  return {
    errorType: normalizeErrorType(rawType),
    code,
    message: message || `${res.status}`,
    recoverable:
      typeof rawRecoverable === "boolean"
        ? rawRecoverable
        : res.status === 429 || res.status === 502 || res.status === 503,
    details: pickValue<Record<string, unknown>>(parsed, "details") ?? null,
  };
}

function looseJson<T>(text: string): T | null {
  if (text === "") {
    return null;
  }
  const candidates = [text, text.slice(text.indexOf("{")), text.slice(text.indexOf("["))];
  for (const candidate of candidates) {
    if (candidate === "") {
      continue;
    }
    try {
      return JSON.parse(candidate) as T;
    } catch {
      continue;
    }
  }
  return null;
}

function normalizeErrorType(raw: unknown): string {
  const text = typeof raw === "string" ? raw : "";
  if (text.startsWith("validation_error")) {
    return "validation_error";
  }
  if (text.startsWith("agent_error")) {
    return "agent_error";
  }
  if (text.startsWith("unknown_error")) {
    return "unknown_error";
  }
  return "unknown_error";
}

/** 契約 §4 の error_type + code 対応表に従って status を typed error へ分類する。 */
export function problemToError(status: number, problem: ProblemDetail): ApiError {
  const code = problem.code ?? "";
  if (status === 400 || status === 401 || status === 403) {
    return new ValidationError(status, problem);
  }
  if (status === 404) {
    return new NotFoundException(status, problem);
  }
  if (status === 409) {
    return new ConflictException(status, problem);
  }
  if (status === 422) {
    return new UnprocessableEntityException(status, problem);
  }
  if (status === 429) {
    return new ConcurrencyLimitException(status, problem);
  }
  if (status === 503 && (problem.errorType === "agent_error" || code.includes("permission_stale"))) {
    return new PermissionStaleException(status, problem);
  }
  if (status >= 500) {
    return new AgentErrorException(status, problem);
  }
  return new ApiError(status, problem);
}

async function toApiError(res: Response): Promise<ApiError> {
  return problemToError(res.status, await readProblem(res));
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

/** JSON endpoint 呼び出し。非 2xx は typed error を throw する。 */
export async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? "GET",
    signal: options.signal,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(options.headers ?? {}),
    },
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw err;
    }
    throw new ApiError(0, {
      errorType: "unknown_error",
      code: "unknown_error.network_error",
      message: err instanceof Error ? err.message : String(err),
      recoverable: true,
      details: { path },
    });
  }
  if (!res.ok) {
    throw await toApiError(res);
  }
  if (res.status === 204 || res.headers.get("content-length") === "0") {
    return undefined as T;
  }
  try {
    return (await parseJsonLoose<T>(res)) as T;
  } catch (err) {
    throw new ApiError(res.status, {
      errorType: "unknown_error",
      code: "unknown_error.invalid_response",
      message: err instanceof Error ? err.message : String(err),
      recoverable: true,
      details: { path },
    });
  }
}

/* ------------------------------------------------------------------ wire readers */

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function alternate(key: string): string {
  return key.includes("_")
    ? key.replace(/_([a-z0-9])/g, (_m, c: string) => c.toUpperCase())
    : key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
}

/** snake_case / camelCase の両 spelling を受理して値を取り出す。 */
export function pickValue<T = unknown>(raw: unknown, ...keys: string[]): T | undefined {
  const obj = asRecord(raw);
  for (const key of keys) {
    const direct = obj[key];
    if (direct !== undefined && direct !== null) {
      return direct as T;
    }
    const alt = obj[alternate(key)];
    if (alt !== undefined && alt !== null) {
      return alt as T;
    }
  }
  return undefined;
}

export function pickString(raw: unknown, ...keys: string[]): string | undefined {
  const value = pickValue<unknown>(raw, ...keys);
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number") {
    return String(value);
  }
  return undefined;
}

export function pickStringOr(raw: unknown, fallback: string, ...keys: string[]): string {
  return pickString(raw, ...keys) ?? fallback;
}

export function pickNumber(raw: unknown, ...keys: string[]): number | undefined {
  const value = pickValue<unknown>(raw, ...keys);
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return undefined;
}

export function pickBoolean(raw: unknown, ...keys: string[]): boolean | undefined {
  const value = pickValue<unknown>(raw, ...keys);
  if (typeof value === "boolean") {
    return value;
  }
  if (value === "true") {
    return true;
  }
  if (value === "false") {
    return false;
  }
  return undefined;
}

export function pickStringArray(raw: unknown, ...keys: string[]): string[] {
  const value = pickValue<unknown>(raw, ...keys);
  if (Array.isArray(value)) {
    return value.filter((v): v is string => typeof v === "string");
  }
  return [];
}

export function pickArray(raw: unknown, ...keys: string[]): unknown[] {
  const value = pickValue<unknown>(raw, ...keys);
  return Array.isArray(value) ? value : [];
}

export function nullableString(raw: unknown, ...keys: string[]): string | null {
  return pickString(raw, ...keys) ?? null;
}

/* ------------------------------------------------------------------ DTO mappers */

export function toChatSessionDto(raw: unknown): ChatSessionDto {
  const rec = asRecord(raw);
  const nestedThread = asRecord(pickValue(rec, "thread"));
  const session = asRecord(pickValue(rec, "session"));
  const merged: Record<string, unknown> = { ...session, ...nestedThread, ...rec };
  const pendingNested = pickNumber(asRecord(pickValue(merged, "approvals")), "pending_count", "pending") ?? 0;
  return {
    threadId: pickStringOr(merged, "", "thread_id", "threadId", "id"),
    sessionId: nullableString(merged, "session_id", "sessionId"),
    title: pickString(merged, "title") ?? "",
    createdAt: nullableString(merged, "created_at", "createdAt"),
    closedAt:
      nullableString(merged, "closed_at", "closedAt") ??
      nullableString(nestedThread, "closed_at", "closedAt"),
    model: nullableString(merged, "model"),
    turnCount: pickNumber(merged, "turn_count", "turnCount") ?? 0,
    pendingApprovalCount:
      pickNumber(merged, "pending_approval_count", "pendingApprovalCount", "pending_approvals") ??
      pendingNested,
  };
}

export function toChatSessionCreateDto(raw: unknown): ChatSessionCreateDto {
  const base = toChatSessionDto(raw);
  return {
    threadId: base.threadId,
    sessionId: base.sessionId,
    title: base.title,
    createdAt: base.createdAt,
  };
}

export function toChatSessionForkDto(raw: unknown, fallbackParent: ThreadId): ChatSessionForkDto {
  const rec = asRecord(raw);
  const nested = pickValue(rec, "thread", "session");
  const thread = toChatSessionDto(nested ?? rec);
  return {
    forkedFromThreadId:
      pickString(rec, "forked_from_thread_id", "forkedFromThreadId", "parent_thread_id", "parentThreadId") ??
      fallbackParent,
    thread,
  };
}

export function toTurnSummaryDto(raw: unknown): TurnSummaryDto {
  const rec = asRecord(raw);
  return {
    turnId: pickStringOr(rec, "", "turn_id", "turnId"),
    threadId: nullableString(rec, "thread_id", "threadId"),
    messageId: nullableString(rec, "message_id", "messageId"),
    parentMessageId: nullableString(rec, "parent_message_id", "parentMessageId"),
    text: nullableString(rec, "text", "content"),
    stopReason: nullableString(rec, "stop_reason", "stopReason", "stopped_reason", "stoppedReason"),
    status: nullableString(rec, "status"),
    usage: normalizeTokenUsage(pickValue(rec, "usage")),
    totalCost: normalizeCostAmount(pickValue(rec, "total_cost", "totalCost", "cost")),
    createdAt: nullableString(rec, "created_at", "createdAt", "started_at", "startedAt"),
    toolCallIds: pickStringArray(rec, "tool_call_ids", "toolCallIds"),
  };
}

/** `session_events` 行 / `frames_tail` 行 (README §9.6)。 */
export function toSessionEventRowDto(raw: unknown): SessionEventRowDto {
  const rec = asRecord(raw);
  const payload = pickValue(rec, "payload");
  const summary =
    typeof payload === "object" && payload !== null
      ? (payload as Record<string, unknown>)
      : rec;
  return {
    eventId: pickStringOr(rec, "", "event_id", "eventId", "id"),
    seq: pickNumber(rec, "seq", "sequence") ?? 0,
    turnId: nullableString(rec, "turn_id", "turnId"),
    threadId: nullableString(rec, "thread_id", "threadId"),
    sessionId: nullableString(rec, "session_id", "sessionId"),
    parentMessageId: nullableString(rec, "parent_message_id", "parentMessageId", "message_id", "messageId"),
    eventType: pickStringOr(rec, "", "event_type", "eventType", "type"),
    toolCallId: nullableString(rec, "tool_call_id", "toolCallId"),
    eventKind: nullableString(rec, "event_kind", "eventKind", "kind"),
    payloadSummary: summarizePayload(summary),
    createdAt: nullableString(rec, "created_at", "createdAt", "recorded_at", "recordedAt"),
  };
}

function summarizePayload(payload: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined) {
      continue;
    }
    if (typeof value === "object") {
      out[key] = Array.isArray(value) ? `[${value.length}]` : "{...}";
    } else {
      out[key] = value;
    }
  }
  return out;
}

export function toContextUsageDto(raw: unknown): ContextUsageDto {
  return normalizeContextUsage(raw) ?? {
    sessionId: null,
    turnCount: 0,
    costAvailable: false,
    usageUnavailableReason: "no_usage_reported",
    totalCost: null,
    totalUsage: null,
    totalCosts: [],
    turns: [],
  };
}

export function toRuleDto(raw: unknown): RuleDto {
  return normalizeRuleDto(asRecord(raw)) ?? { ruleId: "", title: null, text: "", status: null, source: null, version: null, createdAt: null, updatedAt: null, approvedBy: null };
}

export function toJudgmentDto(raw: unknown): JudgmentDto {
  return normalizeJudgmentDto(asRecord(raw)) ?? {
    judgmentId: "",
    inputText: "",
    category: null,
    method: null,
    status: null,
    rationale: null,
    createdAt: null,
    ruleVersionId: null,
    sourceTurnId: null,
    sourceThreadId: null,
  };
}

export function toApprovalRecord(raw: unknown): ApprovalRecord {
  return normalizeApprovalRecord(raw);
}

export function toTokenUsage(raw: unknown): TokenUsage | null {
  return normalizeTokenUsage(raw);
}

export function toCostAmount(raw: unknown): CostAmount | null {
  return normalizeCostAmount(raw);
}

export function toHealthDto(raw: unknown): HealthDto {
  const rec = asRecord(raw);
  const checksRaw = pickValue<Record<string, unknown>>(rec, "checks", "components") ?? {};
  const checks: Record<string, string> = {};
  for (const [key, value] of Object.entries(checksRaw)) {
    checks[key] = typeof value === "string" ? value : value === true ? "ok" : "unknown";
  }
  return {
    status: pickString(rec, "status", "state") ?? "unknown",
    detail: pickString(rec, "detail", "message", "reason") ?? null,
    checks,
    raw: rec,
  };
}

/* ------------------------------------------------------------------ paths */

export const apiPaths = {
  health: "/api/health",
  monitorHealth: "/api/monitor/health",
  capabilities: "/api/agui/capabilities",
  sessions: "/api/chat/sessions",
  session: (threadId: ThreadId): string => `/api/chat/sessions/${encodeURIComponent(threadId)}`,
  sessionFork: (threadId: ThreadId): string =>
    `/api/chat/sessions/${encodeURIComponent(threadId)}/fork`,
  sessionInterrupt: (threadId: ThreadId): string =>
    `/api/chat/sessions/${encodeURIComponent(threadId)}/interrupt`,
  sessionTurns: (threadId: ThreadId): string =>
    `/api/chat/sessions/${encodeURIComponent(threadId)}/turns`,
  sessionFrames: (threadId: ThreadId, afterSeq: number): string =>
    `/api/chat/sessions/${encodeURIComponent(threadId)}/frames?after_seq=${afterSeq}`,
  sessionContextUsage: (threadId: ThreadId): string =>
    `/api/chat/sessions/${encodeURIComponent(threadId)}/context-usage`,
  aguiRun: "/api/agui/run",
  aguiRunFollow: (threadId: ThreadId, afterSeq: number): string =>
    `/api/agui/run?thread_id=${encodeURIComponent(threadId)}&after_seq=${afterSeq}`,
  aguiEvents: (threadId: ThreadId, afterSeq: number, limit?: number): string =>
    `/api/agui/sessions/${encodeURIComponent(threadId)}/events?after_seq=${afterSeq}${limit === undefined ? "" : `&limit=${limit}`}`,
  aguiInterrupt: (threadId: ThreadId): string =>
    `/api/agui/sessions/${encodeURIComponent(threadId)}/interrupt`,
  aguiRename: (threadId: ThreadId): string =>
    `/api/agui/sessions/${encodeURIComponent(threadId)}/events/session`,
  aguiFork: (threadId: ThreadId): string =>
    `/api/agui/sessions/${encodeURIComponent(threadId)}/events/fork`,
  rules: "/api/rules",
  rule: (ruleId: string): string => `/api/rules/${encodeURIComponent(ruleId)}`,
  ruleApprove: (ruleId: string): string => `/api/rules/${encodeURIComponent(ruleId)}/approve`,
  ruleReject: (ruleId: string): string => `/api/rules/${encodeURIComponent(ruleId)}/reject`,
  ruleVersions: (ruleId: string): string => `/api/rules/${encodeURIComponent(ruleId)}/versions`,
  ruleConflicts: "/api/rules/conflicts",
  approvals: (status?: string): string =>
    `/api/approvals${status === undefined ? "" : `?status=${encodeURIComponent(status)}`}`,
  approval: (approvalId: ApprovalId): string => `/api/approvals/${encodeURIComponent(approvalId)}`,
  approvalResolve: (approvalId: ApprovalId, action: "approve" | "reject"): string =>
    `/api/approvals/${encodeURIComponent(approvalId)}/${action}`,
  approvalsResolvePending: "/api/approvals/resolve-pending",
  judgments: "/api/history/judgments",
  judgmentCreate: "/api/judgments",
} as const;

/* ------------------------------------------------------------------ endpoints */

export interface SessionsPage {
  sessions: ChatSessionDto[];
  pendingApprovalsByThread: Record<string, number>;
}

/** `GET /api/chat/sessions` → 会話一覧 (pending_approval_count 含む)。 */
export async function fetchSessions(signal?: AbortSignal): Promise<SessionsPage> {
  const data = await requestJson<unknown>(apiPaths.sessions, { signal });
  const rec = asRecord(data);
  const rows = pickArray(rec, "sessions", "items", "threads", "conversations");
  const pendingRaw = pickValue<Record<string, unknown>>(rec, "pending_approvals", "pendingApprovals");
  const pendingApprovalsByThread: Record<string, number> = {};
  if (pendingRaw) {
    for (const [key, value] of Object.entries(pendingRaw)) {
      if (typeof value === "number") {
        pendingApprovalsByThread[key] = value;
      }
    }
  }
  return { sessions: rows.map(toChatSessionDto), pendingApprovalsByThread };
}

/** `POST /api/chat/sessions` → 会話作成 (title 省略可, 契約は 201 を返す)。 */
export async function createSession(
  input: { title?: string; source?: string } = {},
  signal?: AbortSignal,
): Promise<ChatSessionDto> {
  const body: Record<string, unknown> = {};
  if (input.title !== undefined) {
    body.title = input.title;
  }
  if (input.source !== undefined) {
    body.source = input.source;
  }
  return toChatSessionDto(
    await requestJson<unknown>(apiPaths.sessions, { method: "POST", body, signal }),
  );
}

/** `GET /api/chat/sessions/{thread_id}` → 会話 snapshot (実行監視 pane の初期状態)。 */
export async function fetchSessionSnapshot(
  threadId: ThreadId,
  signal?: AbortSignal,
): Promise<SessionSnapshotDto> {
  const data = await requestJson<unknown>(apiPaths.session(threadId), { signal });
  const rec = asRecord(data);
  const approvals = pickArray(rec, "approvals").map(toApprovalRecord);
  const pendingApprovals = pickArray(rec, "pending_approvals", "pendingApprovals").map(
    toApprovalRecord,
  );
  return {
    thread: rec["thread"] === undefined ? rec : asRecord(rec["thread"]),
    session: asRecord(rec["session"]),
    messages: pickArray(rec, "messages"),
    turns: pickArray(rec, "turns").map(toTurnSummaryDto),
    framesTail: pickArray(rec, "frames_tail", "framesTail").map(toSessionEventRowDto),
    approvals: approvals.length > 0 ? approvals : pendingApprovals,
    pendingApprovals: pendingApprovals.length > 0 ? pendingApprovals : approvals,
    contextUsage: toContextUsageDto(pickValue(rec, "context_usage", "contextUsage")),
    health: rec["health"] ? toHealthDto(rec["health"]) : null,
  };
}

/** `POST /api/chat/sessions/{thread_id}/fork` → fork された新 thread。 */
export async function forkSession(
  threadId: ThreadId,
  input: { title?: string } = {},
  signal?: AbortSignal,
): Promise<ChatSessionForkDto> {
  const body: Record<string, unknown> = {};
  if (input.title !== undefined) {
    body.title = input.title;
  }
  const data = await requestJson<unknown>(apiPaths.sessionFork(threadId), {
    method: "POST",
    body,
    signal,
  });
  return toChatSessionForkDto(data, threadId);
}

/** rename: 契約 §4 の `PATCH /api/agui/sessions/{thread_id}/events/session {title}` (session 情報更新)。 */
export async function renameSession(
  threadId: ThreadId,
  title: string,
  signal?: AbortSignal,
): Promise<ChatSessionDto> {
  const data = await requestJson<unknown>(apiPaths.aguiRename(threadId), {
    method: "PATCH",
    body: { title },
    signal,
  });
  return toChatSessionDto(data);
}

/** `DELETE /api/chat/sessions/{thread_id}` → close/delete (close 済みは 409)。 */
export async function deleteSession(threadId: ThreadId, signal?: AbortSignal): Promise<void> {
  await requestJson<unknown>(apiPaths.session(threadId), { method: "DELETE", signal });
}

/** 会話 close: 契約 §4 の `POST /api/chat/sessions/{thread_id}/interrupt` (進行中 turn を rollback して close する)。 */
export async function closeSession(
  threadId: ThreadId,
  reason = "closed_from_ui",
  signal?: AbortSignal,
): Promise<TurnInterruptDto> {
  const data = await requestJson<unknown>(apiPaths.sessionInterrupt(threadId), {
    method: "POST",
    body: { reason },
    signal,
  });
  const rec = asRecord(data);
  return {
    turnId: pickString(rec, "turn_id", "turnId") ?? null,
    cancelled: pickBoolean(rec, "cancelled") ?? true,
  };
}

/** `GET /api/chat/sessions/{thread_id}/frames?after_seq=` → chat frames replay。 */
export async function fetchFrames(
  threadId: ThreadId,
  afterSeq = 0,
  signal?: AbortSignal,
): Promise<FramesPageDto> {
  const data = await requestJson<unknown>(apiPaths.sessionFrames(threadId, afterSeq), { signal });
  const rec = asRecord(data);
  return {
    frames: pickArray(rec, "frames").map(toSessionEventRowDto),
    replayTruncated: pickBoolean(rec, "replay_truncated", "replayTruncated") ?? false,
  };
}

/** `POST /api/chat/sessions/{thread_id}/turns` → REST 側の turn 開始 (202 `{turn_id, started}`)。 */
export async function postTurn(
  threadId: ThreadId,
  text: string,
  turnId?: TurnId,
  signal?: AbortSignal,
): Promise<TurnAcceptedDto> {
  const body: Record<string, unknown> = { text, content: text };
  if (turnId !== undefined) {
    body.turn_id = turnId;
  }
  const data = await requestJson<unknown>(apiPaths.sessionTurns(threadId), {
    method: "POST",
    body,
    signal,
  });
  const rec = asRecord(data);
  return {
    turnId: pickStringOr(rec, "", "turn_id", "turnId"),
    started: pickBoolean(rec, "started") ?? false,
  };
}

/** `POST /api/agui/sessions/{thread_id}/events` → monitor 側に turn を queue (202 `{queued:[...]}`)。 */
export async function queueEvents(
  threadId: ThreadId,
  turns: { text: string; turnId?: TurnId }[],
  signal?: AbortSignal,
): Promise<{ queued: { turnId: TurnId; seq: number }[] }> {
  const data = await requestJson<unknown>(apiPaths.aguiEvents(threadId, 0), {
    method: "POST",
    body: { turns: turns.map((t) => ({ text: t.text, turn_id: t.turnId })) },
    signal,
  });
  return {
    queued: pickArray(asRecord(data), "queued").map((row) => ({
      turnId: pickStringOr(row, "", "turn_id", "turnId"),
      seq: pickNumber(row, "seq") ?? 0,
    })),
  };
}

/** `GET /api/chat/sessions/{thread_id}/context-usage` → 契約 §6 の token/cost 集計。 */
export async function fetchContextUsage(
  threadId: ThreadId,
  signal?: AbortSignal,
): Promise<ContextUsageDto> {
  return toContextUsageDto(
    await requestJson<unknown>(apiPaths.sessionContextUsage(threadId), { signal }),
  );
}

/** `GET /api/approvals?status=pending` → 承認待ち一覧 (README §9.1 承認待ち pane)。 */
export async function fetchApprovals(status = "pending", signal?: AbortSignal): Promise<ApprovalRecord[]> {
  const data = await requestJson<unknown>(apiPaths.approvals(status), { signal });
  const rec = asRecord(data);
  const rows = rec["approvals"] === undefined && Array.isArray(data) ? data : pickArray(rec, "approvals", "items");
  return rows
    .map(toApprovalRecord)
    .filter((record) => record.approval_id !== "" || record.prompt_id !== null);
}

/** `GET /api/approvals/{approval_id}`。 */
export async function fetchApproval(approvalId: ApprovalId, signal?: AbortSignal): Promise<ApprovalRecord> {
  return toApprovalRecord(await requestJson<unknown>(apiPaths.approval(approvalId), { signal }));
}

/**
 * `POST /api/approvals/{approval_id}/approve|reject {feedback?}`。
 * AG-UI resume (`src/lib/agui/interrupt.ts`) が失敗したときの fallback 経路 (README §12.4)。
 */
export async function resolveApproval(
  approvalId: ApprovalId,
  approved: boolean,
  feedback?: string,
  signal?: AbortSignal,
): Promise<ApprovalRecord> {
  const body: Record<string, unknown> = {};
  const text = feedback?.trim();
  if (text !== undefined && text !== "") {
    body.feedback = text;
  }
  const data = await requestJson<unknown>(
    apiPaths.approvalResolve(approvalId, approved ? "approve" : "reject"),
    { method: "POST", body, signal },
  );
  return toApprovalRecord(data);
}

/** approvals を REST で解決する (approved/false なら reject)。 resume 失敗時の fallback。 */
export async function resolveApprovalFallback(
  approvalId: ApprovalId,
  approved: boolean,
  feedback?: string,
  signal?: AbortSignal,
): Promise<ApprovalRecord> {
  return resolveApproval(approvalId, approved, feedback, signal);
}

/** `POST /api/approvals/resolve-pending {approved}` → 残 pending の一括解決 (policy 相当の UI 操作)。 */
export async function resolvePendingApprovals(
  approved: boolean,
  signal?: AbortSignal,
): Promise<ApprovalRecord[]> {
  const data = await requestJson<unknown>(apiPaths.approvalsResolvePending, {
    method: "POST",
    body: { approved },
    signal,
  });
  const rec = asRecord(data);
  return pickArray(rec, "resolved", "approvals", "items").map(toApprovalRecord);
}

/** `GET /api/rules` (q/tool 絞り込み可)。 */
export async function fetchRules(
  options: { q?: string; tool?: string; includeDisabled?: boolean; signal?: AbortSignal } = {},
): Promise<RuleDto[]> {
  const params = new URLSearchParams();
  if (options.q) {
    params.set("q", options.q);
  }
  if (options.tool) {
    params.set("tool", options.tool);
  }
  if (options.includeDisabled) {
    params.set("include_disabled", "1");
  }
  const qs = params.toString();
  const data = await requestJson<unknown>(`${apiPaths.rules}${qs ? `?${qs}` : ""}`, {
    signal: options.signal,
  });
  const rec = asRecord(data);
  const rows = rec["rules"] === undefined && Array.isArray(data) ? data : pickArray(rec, "rules", "items");
  return rows.map(toRuleDto);
}

/** `POST /api/rules` → 新規 rule 作成 (自然文 text, 契約 §4 route 9)。 */
export async function createRule(
  input: { title?: string; text: string; priority?: number; triggers?: string[]; tools?: string[] },
  signal?: AbortSignal,
): Promise<RuleDto> {
  const body: Record<string, unknown> = { text: input.text };
  if (input.title !== undefined) {
    body.title = input.title;
  }
  if (input.priority !== undefined) {
    body.priority = input.priority;
  }
  if (input.triggers !== undefined) {
    body.triggers = input.triggers;
  }
  if (input.tools !== undefined) {
    body.tools = input.tools;
  }
  return toRuleDto(await requestJson<unknown>(apiPaths.rules, { method: "POST", body, signal }));
}

/** `GET /api/rules/conflicts?text=` → 競合診断 (rules hub)。 */
export async function fetchRuleConflicts(text: string, signal?: AbortSignal): Promise<RuleConflictDto[]> {
  const data = await requestJson<unknown>(
    `${apiPaths.ruleConflicts}?text=${encodeURIComponent(text)}`,
    { signal },
  );
  const rec = asRecord(data);
  const rows =
    rec["conflicts"] === undefined && Array.isArray(data) ? data : pickArray(rec, "conflicts", "items");
  return rows.map((row) => {
    const r = asRecord(row);
    return {
      ruleId: pickStringOr(r, "", "rule_id", "ruleId", "a"),
      otherRuleId: pickStringOr(r, "", "other_rule_id", "otherRuleId", "b"),
      kind: pickString(r, "kind", "verdict") ?? null,
      message: pickString(r, "message", "reason") ?? null,
    };
  });
}

/** `GET /api/history/judgments` (または `GET /api/judgments`) → 判断ログ。 */
export async function fetchJudgments(signal?: AbortSignal): Promise<JudgmentDto[]> {
  let data: unknown;
  try {
    data = await requestJson<unknown>(apiPaths.judgments, { signal });
  } catch (err) {
    if (err instanceof NotFoundException) {
      data = await requestJson<unknown>(apiPaths.judgmentCreate, { signal });
    } else {
      throw err;
    }
  }
  const rec = asRecord(data);
  const rows =
    rec["judgments"] === undefined && Array.isArray(data)
      ? data
      : pickArray(rec, "judgments", "items", "history");
  return rows.map(toJudgmentDto);
}

/** `GET /api/health` → liveness + pinned versions 表示 (settings page)。 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthDto> {
  return toHealthDto(await requestJson<unknown>(apiPaths.health, { signal }));
}

/** `GET /api/monitor/health` → hub の queue / session_events 状態。 */
export async function fetchMonitorHealth(signal?: AbortSignal): Promise<HealthDto> {
  return toHealthDto(await requestJson<unknown>(apiPaths.monitorHealth, { signal }));
}

/** `GET /api/agui/capabilities` → tools 一覧 (デモ tool 名の確認用)。 */
export async function fetchCapabilities(signal?: AbortSignal): Promise<AgentCapabilitiesDto> {
  const data = await requestJson<unknown>(apiPaths.capabilities, { signal });
  const rec = asRecord(data);
  return {
    tools: pickArray(rec, "tools").map((tool) => {
      const t = asRecord(tool);
      return {
        name: pickStringOr(t, "", "name", "tool_name", "toolName"),
        description: pickString(t, "description") ?? null,
        parameters: asRecord(pickValue(t, "parameters", "input_schema", "inputSchema")),
      };
    }),
  };
}

/** settings page 用の pinned versions 表示 (package.json の値を frontend 側で保持する)。 */
export const pinnedVersions = {
  next: "15.5.29",
  react: "19.3.1",
  copilotKit: "1.87.0",
  agUi: "1.0.4",
} as const;
