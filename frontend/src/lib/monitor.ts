/**
 * 実行監視 SSE client (README §9.4 / §9.6 / §10.3、契約 §1・§2・§6)。
 *
 * 契約 §1: monitor frame の必須 key は `{event_id, seq, event_type, created_at, turn_id,
 * thread_id, session_id, payload}`。`seq` は thread 単位で単調増加、`payload` は常に nested。
 * backend hub の旧 `to_frame()` は payload keys を frame 直下へ merge するため (README §9.4 merge
 * 対策注释) 旧 flat frame も受容する (`asMonitorFrame`)。
 *
 * replay / reconnect:
 * - `GET /api/agui/sessions/{thread_id}/events?after_seq=&limit=` だけを使う (README §9.6:
 *   frontend 側に二重 replay window を持たせない)。
 * -再接続時は `Last-Event-ID: <seq>` を送り、backend がこれを嫌う (400 / 422) 場合は
 *   `after_seq=<seq>` へ fallback する。
 * - heartbeat 途絶 (README §10.3 heartbeat) を watchdog で検知して再購読する。
 */

import type {
  ApprovalRecord,
  ApprovalId,
  ContextUsageDto,
  CostAmount,
  EventId,
  FrameSeq,
  JudgmentDto,
  MessageId,
  PermissionSource,
  PermissionStatus,
  RuleDto,
  SessionId,
  ThreadId,
  TokenUsage,
  ToolCallId,
  TurnId,
  UsageWarningPayload,
} from "./types";
import { parseSseEventBlock, findBoundary } from "./sse";

/** README §9.6 実行監視 buffer 既定値 (backend `max_frames_per_monitor_buffer_frames`)。 */
export const DEFAULT_MONITOR_BUFFER_FRAMES = 500;
/** README §10.1 replay window 既定値 (backend `monitor_replay_limit`)。 */
export const DEFAULT_MONITOR_REPLAY_LIMIT = 500;
/** README §10.3 heartbeat 周期既定値 (backend `monitor_heartbeat_seconds`)。 */
export const DEFAULT_MONITOR_HEARTBEAT_SECONDS = 10;

const BASE_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;
const JITTER_RATIO = 0.2;

/**
 * 契約 §2 / backend `MonitorEventType` と同一の単一語彙 (session_events.py)。
 * `run_started` は禁止語 (契約 §2)。
 */
export const MONITOR_EVENT_TYPES = [
  "agent_run_started",
  "run_queued",
  "run_cancelled",
  "run_finished",
  "message_complete",
  "assistant_text_delta",
  "thinking_delta",
  "tool_call_started",
  "tool_call_args",
  "tool_call_complete",
  "permission_requested",
  "permission_resolved",
  "approval_requested",
  "approval_resolved",
  "pending_approvals_updated",
  "error",
  "session_created",
  "session_closed",
  "session_forked",
  "session_reaped",
  "session_idle",
  "session_event_recorded",
  "judgment_created",
  "rule_changed",
  "heartbeat",
  "context_usage",
  "usage_warning",
] as const;

export type MonitorEventType = (typeof MONITOR_EVENT_TYPES)[number];

/** `MonitorEventType` の外側にある AG-UI CUSTOM frame 名 (契約 §2: `interrupt`)。 */
export const INTERRUPT_FRAME_EVENT = "interrupt";

/** 契約 §5: `tool_call_complete.status` としてありうる値 (`"denied"` は grant 欠如)。 */
export type ToolCallCompletionStatus = "complete" | "completed" | "error" | "denied" | "timeout";
export type MonitorStopReason = string | null;
export type MonitorErrorType = "session_closed" | "session_error" | string;

interface MonitorFrameBase {
  event_id: EventId;
  seq: FrameSeq;
  event_type: MonitorEventType;
  created_at: string;
  turn_id: TurnId | null;
  thread_id: ThreadId | null;
  session_id: SessionId | null;
}

export interface RunStartedFrame extends MonitorFrameBase {
  event_type: "agent_run_started";
  payload: { input_preview: string | null };
}

export interface RunQueuedFrame extends MonitorFrameBase {
  event_type: "run_queued";
  payload: { queued_at: string | null; position: number | null };
}

export interface RunCancelledFrame extends MonitorFrameBase {
  event_type: "run_cancelled";
  payload: { reason: string | null };
}

export interface RunFinishedFrame extends MonitorFrameBase {
  event_type: "run_finished";
  payload: {
    text_preview: string | null;
    stop_reason: MonitorStopReason;
    usage: TokenUsage | null;
    total_cost: CostAmount | null;
    tool_call_ids: ToolCallId[];
  };
}

export interface MessageCompleteFrame extends MonitorFrameBase {
  event_type: "message_complete";
  payload: {
    message_id: MessageId | null;
    parent_message_id: MessageId | null;
    text_preview: string | null;
  };
}

export interface AssistantTextDeltaFrame extends MonitorFrameBase {
  event_type: "assistant_text_delta";
  payload: { delta: string; parent_message_id: MessageId | null };
}

export interface ThinkingDeltaFrame extends MonitorFrameBase {
  event_type: "thinking_delta";
  payload: { delta: string };
}

export interface ToolCallStartedFrame extends MonitorFrameBase {
  event_type: "tool_call_started";
  payload: {
    tool_name: string;
    tool_call_id: ToolCallId | null;
    arguments_preview: string | null;
    parent_message_id: MessageId | null;
    permission_source?: PermissionSource | null;
  };
}

export interface ToolCallArgsFrame extends MonitorFrameBase {
  event_type: "tool_call_args";
  payload: { tool_call_id: ToolCallId | null; args_delta: string };
}

export interface ToolCallCompleteFrame extends MonitorFrameBase {
  event_type: "tool_call_complete";
  payload: {
    tool_call_id: ToolCallId | null;
    tool_name: string | null;
    status: ToolCallCompletionStatus | null;
    result_preview: string | null;
    permission_source?: PermissionSource | null;
  };
}

export interface PermissionRequestedFrame extends MonitorFrameBase {
  event_type: "permission_requested";
  payload: {
    approval_id?: ApprovalId | null;
    approval_ids: ApprovalId[];
    tool_call_id: ToolCallId | null;
    tool_name: string | null;
  };
}

export interface PermissionResolvedFrame extends MonitorFrameBase {
  event_type: "permission_resolved";
  payload: {
    approval_id?: ApprovalId | null;
    tool_call_id: ToolCallId | null;
    tool_name: string | null;
    permission_source: PermissionSource | null;
    state?: PermissionStatus | null;
  };
}

export interface ApprovalRequestedFrame extends MonitorFrameBase {
  event_type: "approval_requested";
  payload: { approval: ApprovalRecord };
}

export interface ApprovalResolvedFrame extends MonitorFrameBase {
  event_type: "approval_resolved";
  payload: { approval: ApprovalRecord; action?: string | null };
}

export interface PendingApprovalsUpdatedFrame extends MonitorFrameBase {
  event_type: "pending_approvals_updated";
  payload: { pending_approval_ids: ApprovalId[] };
}

export interface ErrorFrame extends MonitorFrameBase {
  event_type: "error";
  payload: {
    error_type: MonitorErrorType;
    code: string | null;
    message: string | null;
    source: string | null;
    recoverable: boolean;
  };
}

export interface SessionCreatedFrame extends MonitorFrameBase {
  event_type: "session_created";
  payload: Record<string, unknown>;
}

export interface SessionClosedFrame extends MonitorFrameBase {
  event_type: "session_closed";
  payload: { reason?: string | null; closed_by?: string | null };
}

export interface SessionForkedFrame extends MonitorFrameBase {
  event_type: "session_forked";
  payload: { forked_from_thread_id?: ThreadId | null; title?: string | null };
}

export interface SessionReapedFrame extends MonitorFrameBase {
  event_type: "session_reaped";
  payload: { session_id: SessionId | null; reason: string | null };
}

export interface SessionIdleFrame extends MonitorFrameBase {
  event_type: "session_idle";
  payload: { session_id: SessionId | null; idle_seconds: number | null };
}

export interface SessionEventRecordedFrame extends MonitorFrameBase {
  event_type: "session_event_recorded";
  payload: {
    event_id: EventId | null;
    parent_message_id: MessageId | null;
    session_id: SessionId | null;
    event_type: string | null;
    event_kind?: string | null;
    tool_call_id: ToolCallId | null;
    payload_summary: Record<string, unknown>;
  };
}

export interface JudgmentCreatedFrame extends MonitorFrameBase {
  event_type: "judgment_created";
  payload: { judgment: JudgmentDto | null };
}

export interface RuleChangedFrame extends MonitorFrameBase {
  event_type: "rule_changed";
  payload: { rule_id: string | null; action: string | null; rule: RuleDto | null };
}

export interface HeartbeatFrame extends MonitorFrameBase {
  event_type: "heartbeat";
  payload: {
    status: "active";
    session_id: SessionId | null;
    conversation_id: ThreadId | null;
    active_turn_id: TurnId | null;
    pending_approval_ids: ApprovalId[];
    pending_permission_ids: ApprovalId[];
    timestamp: string | null;
  };
}

export interface ContextUsageFrame extends MonitorFrameBase {
  event_type: "context_usage";
  payload: ContextUsageDto;
}

export interface UsageWarningFrame extends MonitorFrameBase {
  event_type: "usage_warning";
  payload: UsageWarningPayload;
}

/** AG-UI CUSTOM `interrupt` Hint frame (README §9.3 / 契約 §2: AG-UI 側が正式). */
export interface InterruptHintFrame {
  event_id: EventId;
  seq: FrameSeq;
  event_type: typeof INTERRUPT_FRAME_EVENT;
  created_at: string;
  turn_id: TurnId | null;
  thread_id: ThreadId | null;
  session_id: SessionId | null;
  payload: { interrupt: InterruptWirePayload };
}

/** wire 上の interrupt payload。契約 §3 は snake_case (`tool_call_id`, `expires_at`)、AG-UI core は camelCase。 */
export interface InterruptWirePayload {
  id: string | null;
  reason: string;
  message?: string | null;
  tool_call_id?: string | null;
  expires_at?: string | null;
  expiresAt?: string | null;
  response_schema?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
}

export type MonitorFrame =
  | RunStartedFrame
  | RunQueuedFrame
  | RunCancelledFrame
  | RunFinishedFrame
  | MessageCompleteFrame
  | AssistantTextDeltaFrame
  | ThinkingDeltaFrame
  | ToolCallStartedFrame
  | ToolCallArgsFrame
  | ToolCallCompleteFrame
  | PermissionRequestedFrame
  | PermissionResolvedFrame
  | ApprovalRequestedFrame
  | ApprovalResolvedFrame
  | PendingApprovalsUpdatedFrame
  | ErrorFrame
  | SessionCreatedFrame
  | SessionClosedFrame
  | SessionForkedFrame
  | SessionReapedFrame
  | SessionIdleFrame
  | SessionEventRecordedFrame
  | JudgmentCreatedFrame
  | RuleChangedFrame
  | HeartbeatFrame
  | ContextUsageFrame
  | UsageWarningFrame;

export type AnyMonitorFrame = MonitorFrame | InterruptHintFrame;

export type MonitorEventName = MonitorEventType | typeof INTERRUPT_FRAME_EVENT;

/** 契約 §1 / §2 のグループ分け (README §14: run / tool / hitl / lifecycle / monitor 系)。 */
export type MonitorEventGroup = "run" | "tool" | "hitl" | "lifecycle" | "monitor";

const EVENT_GROUPS: Record<MonitorEventName, MonitorEventGroup> = {
  agent_run_started: "run",
  run_queued: "run",
  run_cancelled: "run",
  run_finished: "run",
  message_complete: "run",
  assistant_text_delta: "monitor",
  thinking_delta: "monitor",
  tool_call_started: "tool",
  tool_call_args: "tool",
  tool_call_complete: "tool",
  permission_requested: "hitl",
  permission_resolved: "hitl",
  approval_requested: "hitl",
  approval_resolved: "hitl",
  pending_approvals_updated: "hitl",
  error: "lifecycle",
  session_created: "lifecycle",
  session_closed: "lifecycle",
  session_forked: "lifecycle",
  session_reaped: "lifecycle",
  session_idle: "lifecycle",
  session_event_recorded: "lifecycle",
  judgment_created: "hitl",
  rule_changed: "hitl",
  heartbeat: "monitor",
  context_usage: "monitor",
  usage_warning: "monitor",
  interrupt: "hitl",
};

const APPROVAL_FRAME_EVENTS: readonly MonitorEventName[] = [
  "permission_requested",
  "approval_requested",
  "approval_resolved",
  "pending_approvals_updated",
  "interrupt",
];

const TURN_LIFECYCLE_EVENTS: readonly MonitorEventName[] = [
  "agent_run_started",
  "run_queued",
  "run_cancelled",
  "run_finished",
  "message_complete",
];

export function isMonitorEventName(value: unknown): value is MonitorEventName {
  return (
    typeof value === "string" &&
    (value === INTERRUPT_FRAME_EVENT ||
      (MONITOR_EVENT_TYPES as readonly string[]).includes(value))
  );
}

export function monitorEventGroup(eventType: MonitorEventName): MonitorEventGroup {
  return EVENT_GROUPS[eventType] ?? "monitor";
}

export function monitorEventLabel(eventType: MonitorEventName): string {
  const labels: Record<MonitorEventName, string> = {
    agent_run_started: "実行開始",
    run_queued: "実行待ち",
    run_cancelled: "実行中断",
    run_finished: "実行完了",
    message_complete: "応答確定",
    assistant_text_delta: "応答 (途中)",
    thinking_delta: "思考",
    tool_call_started: "ツールの実行",
    tool_call_args: "ツールの引数",
    tool_call_complete: "ツールの結果",
    permission_requested: "権限のリクエスト",
    permission_resolved: "権限の解決",
    approval_requested: "承認待ち",
    approval_resolved: "承認の解決",
    pending_approvals_updated: "承認リスト更新",
    error: "エラー",
    session_created: "セッション作成",
    session_closed: "セッション終了",
    session_forked: "フォーク",
    session_reaped: "セッション除去",
    session_idle: "アイドル",
    session_event_recorded: "記録",
    judgment_created: "判断",
    rule_changed: "ルール更新",
    heartbeat: "ハートビート",
    context_usage: "コンテキスト使用量",
    usage_warning: "使用量の警告",
    interrupt: "中断",
  };
  return labels[eventType] ?? String(eventType);
}

export function isApprovalFrameEvent(eventType: MonitorEventName): boolean {
  return APPROVAL_FRAME_EVENTS.includes(eventType);
}

export function isTurnLifecycleEvent(eventType: MonitorEventName): boolean {
  return TURN_LIFECYCLE_EVENTS.includes(eventType);
}

/** 契約 §1 dedupe key (`(seq, event_type)` で 1 件)。 */
export function monitorFrameDedupeKey(frame: AnyMonitorFrame): string {
  return `${frame.seq}:${frame.event_type}`;
}

/** 契約 §1 merge 規則: `seq` → `created_at` の stable sort。 */
export function compareMonitorFrames(a: AnyMonitorFrame, b: AnyMonitorFrame): number {
  if (a.seq !== b.seq) {
    return a.seq < b.seq ? -1 : 1;
  }
  const left = a.created_at;
  const right = b.created_at;
  if (left === right) {
    return 0;
  }
  return left < right ? -1 : 1;
}

/** 契約 §1 merge_frames (`seq → created_at`). 同一 `(seq, event_type)` は最初の 1 件を保持する。 */
export function mergeMonitorFrames(
  frames: readonly AnyMonitorFrame[],
  existing: readonly AnyMonitorFrame[] = [],
): AnyMonitorFrame[] {
  const merged: AnyMonitorFrame[] = [...existing];
  const seen = new Set<string>(merged.map(monitorFrameDedupeKey));
  for (const frame of frames) {
    const key = monitorFrameDedupeKey(frame);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(frame);
  }
  return merged.sort(compareMonitorFrames);
}

export interface MonitorFrameParseResult {
  frame: AnyMonitorFrame | null;
  error: string | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readString(record: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string") {
      return value;
    }
  }
  return null;
}

function readNumber(record: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return null;
}

function readStringArray(record: Record<string, unknown>, ...keys: string[]): string[] {
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value)) {
      return value.filter((item): item is string => typeof item === "string");
    }
  }
  return [];
}

/** snake_case / camelCase 双方の token usage dict を正規化する (README §9.4 usage 表記)。 */
export function normalizeTokenUsage(value: unknown): TokenUsage | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  const raw: Record<string, number> = {};
  for (const [key, entry] of Object.entries(record)) {
    const numeric = readNumber({ v: entry });
    if (numeric !== null) {
      raw[key] = numeric;
    }
  }
  const inputTokens =
    readNumber(record, "input_tokens", "inputTokens") ??
    raw["input_tokens"] ??
    raw["inputTokens"] ??
    null;
  const outputTokens =
    readNumber(record, "output_tokens", "outputTokens") ??
    raw["output_tokens"] ??
    raw["outputTokens"] ??
    null;
  const cachedInputTokens =
    readNumber(record, "cached_input_tokens", "cachedInputTokens") ??
    raw["cached_input_tokens"] ??
    null;
  const reasoningOutputTokens =
    readNumber(record, "reasoning_output_tokens", "reasoningOutputTokens") ??
    raw["reasoning_output_tokens"] ??
    null;
  const totalTokens =
    readNumber(record, "total_tokens", "totalTokens") ??
    raw["total_tokens"] ??
    (inputTokens !== null && outputTokens !== null ? inputTokens + outputTokens : null);
  const usage: TokenUsage = { raw };
  if (inputTokens !== null) {
    usage.inputTokens = inputTokens;
  }
  if (outputTokens !== null) {
    usage.outputTokens = outputTokens;
  }
  if (cachedInputTokens !== null) {
    usage.cachedInputTokens = cachedInputTokens;
  }
  if (reasoningOutputTokens !== null) {
    usage.reasoningOutputTokens = reasoningOutputTokens;
  }
  if (totalTokens !== null) {
    usage.totalTokens = totalTokens;
  }
  return usage;
}

export function normalizeCostAmount(value: unknown): CostAmount | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  const numeric = readNumber(record, "value", "total_cost", "cost", "amount");
  if (numeric === null) {
    if (record === null) {
      return null;
    }
    return null;
  }
  const currency = readString(record, "currency", "unit") ?? "JPY";
  const unit = readString(record, "unit", "currency") ?? currency;
  return { value: numeric, currency, unit };
}

function framePayload(raw: Record<string, unknown>): Record<string, unknown> {
  const nested = asRecord(raw["payload"]);
  if (nested) {
    return nested;
  }
  // 旧 hub frame (README §9.4 merge 名残): payload keys が frame 直下へ merge されている。
  return raw;
}

/** 契約 §1 の必須 key を満たす形へ正規化する。不正 `event_type` / seq 欠如は null。 */
export function asMonitorFrame(raw: unknown): MonitorFrame | null {
  const parsed = asAnyMonitorFrame(raw);
  return parsed && parsed.event_type !== INTERRUPT_FRAME_EVENT ? parsed : null;
}

export function asAnyMonitorFrame(raw: unknown): AnyMonitorFrame | null {
  const outer = asRecord(raw);
  if (!outer) {
    return null;
  }
  const eventName = readString(outer, "event_type", "event", "type");
  if (!isMonitorEventName(eventName)) {
    return null;
  }
  const payload = framePayload(outer);
  const seq = readNumber(outer, "seq", "sequence");
  if (seq === null) {
    return null;
  }
  const createdAt =
    readString(outer, "created_at", "createdAt", "timestamp") ??
    readString(payload, "created_at", "timestamp") ??
    "";
  const baseBase = {
    event_id: readString(outer, "event_id", "eventKind", "id") ?? `frame-${seq}-${eventName}`,
    seq,
    event_type: eventName,
    created_at: createdAt,
    turn_id: readString(outer, "turn_id", "turnId") ?? readString(payload, "turn_id") ?? null,
    thread_id:
      readString(outer, "thread_id", "threadId", "conversation_id") ??
      readString(payload, "thread_id") ??
      null,
    session_id:
      readString(outer, "session_id", "sessionId", "cas_session_id", "codex_session_id") ??
      readString(payload, "session_id", "sessionID") ??
      null,
  };
  switch (eventName) {
    case "agent_run_started":
      return { ...baseBase, event_type: eventName, payload: { input_preview: readString(payload, "input_preview", "text_preview") } };
    case "run_queued":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          queued_at: readString(payload, "queued_at", "created_at"),
          position: readNumber(payload, "position", "index"),
        },
      };
    case "run_cancelled":
      return { ...baseBase, event_type: eventName, payload: { reason: readString(payload, "reason", "code") } };
    case "run_finished":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          text_preview: readString(payload, "text_preview", "message_preview"),
          stop_reason: readString(payload, "stop_reason", "stopReason"),
          usage: normalizeTokenUsage(payload["usage"]),
          total_cost: normalizeCostAmount(payload["total_cost"] ?? payload["cost"]),
          tool_call_ids: readStringArray(payload, "tool_call_ids", "toolCallIds"),
        },
      };
    case "message_complete":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          message_id: readString(payload, "message_id", "messageId"),
          parent_message_id: readString(payload, "parent_message_id", "parentMessageId"),
          text_preview: readString(payload, "text_preview"),
        },
      };
    case "assistant_text_delta":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          delta: readString(payload, "delta", "text") ?? "",
          parent_message_id: readString(payload, "parent_message_id", "parentMessageId"),
        },
      };
    case "thinking_delta":
      return { ...baseBase, event_type: eventName, payload: { delta: readString(payload, "delta", "text") ?? "" } };
    case "tool_call_started":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          tool_name: readString(payload, "tool_name", "name") ?? "",
          tool_call_id: readString(payload, "tool_call_id", "toolCallId", "call_id"),
          arguments_preview: readString(payload, "arguments_preview", "args_preview"),
          parent_message_id: readString(payload, "parent_message_id", "parentMessageId"),
          permission_source: readString(payload, "permission_source", "permissionSource") as PermissionSource | null,
        },
      };
    case "tool_call_args":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          tool_call_id: readString(payload, "tool_call_id", "toolCallId", "call_id"),
          args_delta: readString(payload, "args_delta", "arguments_delta", "delta") ?? "",
        },
      };
    case "tool_call_complete":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          tool_call_id: readString(payload, "tool_call_id", "toolCallId", "call_id"),
          tool_name: readString(payload, "tool_name", "name"),
          status: readToolStatus(payload["status"]),
          result_preview: readString(payload, "result_preview", "output_preview"),
          permission_source: readString(payload, "permission_source", "permissionSource") as PermissionSource | null,
        },
      };
    case "permission_requested":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          approval_id: readString(payload, "approval_id", "prompt_id", "id"),
          approval_ids: readStringArray(payload, "approval_ids", "approvalIds", "approval_id"),
          tool_call_id: readString(payload, "tool_call_id", "toolCallId"),
          tool_name: readString(payload, "tool_name", "name"),
        },
      };
    case "permission_resolved":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          approval_id: readString(payload, "approval_id", "prompt_id", "id"),
          tool_call_id: readString(payload, "tool_call_id", "toolCallId"),
          tool_name: readString(payload, "tool_name", "name"),
          permission_source: readString(payload, "permission_source", "permissionSource") as PermissionSource | null,
          state: readString(payload, "state", "status") as PermissionStatus | null,
        },
      };
    case "approval_requested":
    case "approval_resolved": {
      const record = asRecord(payload["approval"]) ?? asRecord(payload["record"]) ?? payload;
      const approval = asApprovalRecord(record, {
        turnId: baseBase.turn_id,
        threadId: baseBase.thread_id,
        sessionId: baseBase.session_id,
      });
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          approval,
          ...(eventName === "approval_resolved"
            ? { action: readString(payload, "action", "decided_by", "resolved_by") }
            : {}),
        },
      };
    }
    case "pending_approvals_updated":
      return {
        ...baseBase,
        event_type: eventName,
        payload: { pending_approval_ids: readStringArray(payload, "pending_approval_ids", "pendingApprovalIds") },
      };
    case "error":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          error_type: readString(payload, "error_type", "errorType", "type") ?? "runtime_error",
          code: readString(payload, "code"),
          message: readString(payload, "message"),
          source: readString(payload, "source"),
          recoverable: payload["recoverable"] === true,
        },
      };
    case "session_created":
      return { ...baseBase, event_type: eventName, payload: { ...payload } };
    case "session_closed":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          reason: readString(payload, "reason"),
          closed_by: readString(payload, "closed_by", "decided_by"),
        },
      };
    case "session_forked":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          forked_from_thread_id: readString(payload, "forked_from_thread_id", "forkedFromThreadId"),
          title: readString(payload, "title"),
        },
      };
    case "session_reaped":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          session_id: readString(payload, "session_id", "sessionId") ?? baseBase.session_id,
          reason: readString(payload, "reason"),
        },
      };
    case "session_idle":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          session_id: readString(payload, "session_id", "sessionId") ?? baseBase.session_id,
          idle_seconds: readNumber(payload, "idle_seconds", "idleSeconds"),
        },
      };
    case "session_event_recorded":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          event_id: readString(payload, "event_id", "eventId") ?? baseBase.event_id,
          parent_message_id: readString(payload, "parent_message_id", "parentMessageId"),
          session_id: readString(payload, "session_id", "sessionId") ?? baseBase.session_id,
          event_type: readString(payload, "event_type", "eventKind"),
          event_kind: readString(payload, "event_kind", "recordType", "kind"),
          tool_call_id: readString(payload, "tool_call_id", "toolCallId"),
          payload_summary:
            asRecord(payload["payload_summary"] ?? payload["approval_summary"] ?? payload["summary"]) ??
            { ...payload },
        },
      };
    case "judgment_created": {
      const judgmentRecord = asRecord(payload["judgment"]);
      return {
        ...baseBase,
        event_type: eventName,
        payload: { judgment: judgmentRecord ? normalizeJudgmentDto(judgmentRecord) : null },
      };
    }
    case "rule_changed": {
      const ruleRecord = asRecord(payload["rule"]);
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          rule_id: readString(payload, "rule_id", "ruleId"),
          action: readString(payload, "action"),
          rule: ruleRecord ? normalizeRuleDto(ruleRecord) : null,
        },
      };
    }
    case "heartbeat":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          status: "active",
          session_id: readString(payload, "session_id", "sessionId", "cas_session_id") ?? baseBase.session_id,
          conversation_id: readString(payload, "conversation_id", "thread_id") ?? baseBase.thread_id,
          active_turn_id: readString(payload, "active_turn_id", "turn_id", "turnId"),
          pending_approval_ids: readStringArray(payload, "pending_approval_ids", "pendingApprovalIds"),
          pending_permission_ids: readStringArray(payload, "pending_permission_ids"),
          timestamp: readString(payload, "timestamp") ?? baseBase.created_at ?? null,
        },
      };
    case "context_usage":
      return { ...baseBase, event_type: eventName, payload: normalizeContextUsage(payload, baseBase.session_id) };
    case "usage_warning":
      return {
        ...baseBase,
        event_type: eventName,
        payload: {
          threshold: readNumber(payload, "threshold"),
          total_cost: normalizeCostAmount(payload["total_cost"]),
          turn_id: readString(payload, "turn_id", "turnId") ?? baseBase.turn_id,
        },
      };
    case INTERRUPT_FRAME_EVENT:
      return {
        ...baseBase,
        event_type: INTERRUPT_FRAME_EVENT,
        payload: { interrupt: normalizeInterruptPayload(payload["interrupt"] ?? payload) },
      };
    default: {
      const exhaustive: never = eventName;
      return exhaustive;
    }
  }
}

function readToolStatus(value: unknown): ToolCallCompletionStatus | null {
  if (typeof value !== "string") {
    return null;
  }
  const allowed: ToolCallCompletionStatus[] = ["complete", "completed", "error", "denied", "timeout"];
  return (allowed as string[]).includes(value) ? (value as ToolCallCompletionStatus) : null;
}

export function normalizeInterruptPayload(value: unknown): InterruptWirePayload {
  const record = asRecord(value) ?? {};
  const metadata = asRecord(record["metadata"]);
  const id = readString(record, "id", "approval_id", "interrupt_id", "interruptId", "prompt_id", "approvalId");
  return {
    id,
    reason: readString(record, "reason") ?? "awaiting_human_approval",
    message: readString(record, "message", "title"),
    tool_call_id: readString(record, "tool_call_id", "toolCallId", "call_id"),
    expires_at: readString(record, "expires_at") ?? readString(metadata ?? {}, "expires_at") ?? null,
    expiresAt: readString(record, "expiresAt") ?? readString(metadata ?? {}, "expiresAt") ?? null,
    response_schema: asRecord(record["response_schema"] ?? record["responseSchema"]),
    metadata: metadata ?? null,
  };
}

function applyAliases(
  record: Record<string, unknown>,
  aliases: Record<string, string[]>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...record };
  for (const [key, candidates] of Object.entries(aliases)) {
    if (out[key] !== undefined) {
      continue;
    }
    for (const candidate of candidates) {
      if (record[candidate] !== undefined) {
        out[key] = record[candidate];
        break;
      }
    }
  }
  return out;
}

/** README §12.4 / 契約 §1: `approval_id === Interrupt.id`。backend は `prompt_id` を併記する。 */
export function normalizeApprovalRecord(raw: unknown): ApprovalRecord {
  const record = asRecord(raw) ?? {};
  const aliased = applyAliases(record, {
    approval_id: ["approvalId", "id"],
    prompt_id: ["promptId"],
    session_id: ["sessionId"],
    tool_call_id: ["toolCallId", "call_id", "callId"],
    turn_id: ["turnId"],
    message_id: ["messageId"],
    parent_message_id: ["parentMessageId"],
    decided_by: ["decidedBy", "resolved_by"],
    permission_source: ["permissionSource", "source"],
    created_at: ["createdAt"],
    updated_at: ["updatedAt"],
    resolved_at: ["resolvedAt"],
    expires_at: ["expiresAt"],
    timeout_seconds: ["timeoutSeconds"],
    tool_name: ["toolName", "name"],
  });
  const status = readString(aliased, "status", "state") ?? "pending";
  const argumentsRecord = asRecord(aliased["arguments"]) ?? {};
  return {
    approval_id: readString(aliased, "approval_id"),
    prompt_id: readString(aliased, "prompt_id") ?? readString(aliased, "approval_id"),
    session_id: readString(aliased, "session_id"),
    tool_name: readString(aliased, "tool_name") ?? "",
    arguments: argumentsRecord,
    tool_call_id: readString(aliased, "tool_call_id"),
    turn_id: readString(aliased, "turn_id"),
    message_id: readString(aliased, "message_id"),
    parent_message_id: readString(aliased, "parent_message_id"),
    status: status as PermissionStatus,
    decided_by: readString(aliased, "decided_by"),
    reason: readString(aliased, "reason"),
    permission_source: readString(aliased, "permission_source") as PermissionSource | null,
    feedback: readString(aliased, "feedback"),
    created_at: readString(aliased, "created_at"),
    updated_at: readString(aliased, "updated_at"),
    resolved_at: readString(aliased, "resolved_at"),
    expires_at: readString(aliased, "expires_at"),
    timeout_seconds: readNumber(aliased, "timeout_seconds"),
    sequence: readNumber(aliased, "sequence", "seq"),
  };
}

function asApprovalRecord(
  record: Record<string, unknown>,
  context: { turnId: TurnId | null; threadId: ThreadId | null; sessionId: SessionId | null },
): ApprovalRecord {
  const withDefaults: Record<string, unknown> = {
    turn_id: context.turnId ?? undefined,
    session_id: context.sessionId ?? undefined,
    ...record,
  };
  if (withDefaults["session_id"] === undefined && context.sessionId) {
    withDefaults["session_id"] = context.sessionId;
  }
  void context.threadId;
  return normalizeApprovalRecord(withDefaults);
}

export function normalizeJudgmentDto(raw: Record<string, unknown>): JudgmentDto {
  const aliased = applyAliases(raw, {
    judgment_id: ["judgmentId", "id"],
    input_text: ["inputText"],
    rule_version_id: ["ruleVersionId"],
    source_turn_id: ["sourceTurnId"],
    source_thread_id: ["sourceThreadId"],
    created_at: ["createdAt"],
  });
  return {
    judgmentId: readString(aliased, "judgment_id") ?? readString(aliased, "id") ?? "",
    inputText: readString(aliased, "input_text") ?? "",
    category: readString(aliased, "category"),
    method: readString(aliased, "method"),
    status: readString(aliased, "status"),
    rationale: readString(aliased, "rationale"),
    createdAt: readString(aliased, "created_at"),
    ruleVersionId: readString(aliased, "rule_version_id"),
    sourceTurnId: readString(aliased, "source_turn_id"),
    sourceThreadId: readString(aliased, "source_thread_id"),
  };
}

export function normalizeRuleDto(raw: Record<string, unknown>): RuleDto {
  const aliased = applyAliases(raw, {
    rule_id: ["ruleId", "id"],
    created_at: ["createdAt"],
    updated_at: ["updatedAt"],
    approved_by: ["approvedBy"],
  });
  return {
    ruleId: readString(aliased, "rule_id") ?? readString(aliased, "id") ?? "",
    title: readString(aliased, "title"),
    text: readString(aliased, "text") ?? "",
    status: readString(aliased, "status"),
    source: readString(aliased, "source"),
    version: readNumber(aliased, "version"),
    createdAt: readString(aliased, "created_at"),
    updatedAt: readString(aliased, "updated_at"),
    approvedBy: readString(aliased, "approved_by"),
  };
}

/** 契約 §6 context usage (snake_case payload / REST 双方で同じ形)。 */
export function normalizeContextUsage(raw: unknown, fallbackSessionId: SessionId | null = null): ContextUsageDto {
  const outer = asRecord(raw) ?? {};
  const totalCostRecord = asRecord(outer["total_cost"]) ?? asRecord(outer["totalCost"]);
  const totalCostsRaw = Array.isArray(outer["total_costs"])
    ? outer["total_costs"]
    : Array.isArray(outer["totalCosts"])
      ? outer["totalCosts"]
      : [];
  const turnsRaw = Array.isArray(outer["turns"]) ? outer["turns"] : [];
  const turnRows: ContextUsageDto["turns"] = [];
  for (const entry of turnsRaw) {
    const turn = asRecord(entry) ?? {};
    turnRows.push({
      turnId: readString(turn, "turn_id", "turnId"),
      seq: readNumber(turn, "seq", "sequence"),
      createdAt: readString(turn, "created_at", "createdAt"),
      stopReason: readString(turn, "stop_reason", "stopReason"),
      usage: normalizeTokenUsage(turn["usage"]),
      totalCost: normalizeCostAmount(turn["total_cost"] ?? turn["cost"]),
    });
  }
  const costRows: ContextUsageDto["totalCosts"] = [];
  for (const entry of totalCostsRaw) {
    const row = asRecord(entry) ?? {};
    costRows.push({
      turnId: readString(row, "turn_id", "turnId"),
      cost: normalizeCostAmount(row["cost"] ?? row),
    });
  }
  const unavailable = readString(outer, "usage_unavailable_reason", "usageUnavailableReason");
  const costAvailable =
    typeof outer["cost_available"] === "boolean"
      ? outer["cost_available"]
      : typeof outer["costAvailable"] === "boolean"
        ? outer["costAvailable"]
        : totalCostRecord !== null;
  return {
    sessionId: readString(outer, "session_id", "sessionId") ?? fallbackSessionId,
    turnCount: readNumber(outer, "turn_count", "turnCount") ?? turnRows.length,
    costAvailable,
    usageUnavailableReason:
      unavailable === "no_usage_reported" || unavailable === "no_cost_reported" ? unavailable : null,
    totalCost: normalizeCostAmount(totalCostRecord),
    totalUsage: normalizeTokenUsage(outer["total_usage"] ?? outer["totalUsage"]),
    totalCosts: costRows,
    turns: turnRows,
  };
}

/** README §9.6: `session_event_recorded` payload を表行へ。 */
export function asSessionEventRow(frame: SessionEventRecordedFrame) {
  return {
    eventId: frame.payload.event_id ?? frame.event_id,
    seq: frame.seq,
    turnId: frame.turn_id,
    threadId: frame.thread_id,
    sessionId: frame.payload.session_id ?? frame.session_id,
    parentMessageId: frame.payload.parent_message_id,
    eventType: frame.payload.event_type ?? monitorEventLabel(frame.event_type),
    toolCallId: frame.payload.tool_call_id,
    eventKind: frame.payload.event_kind ?? null,
    payloadSummary: frame.payload.payload_summary,
    createdAt: frame.created_at,
  };
}

/* --------------------------------------------------------------------------
 * bounded buffer (README §9.5: frontend 側に二重 replay window を持たせない)
 * -------------------------------------------------------------------------- */

export interface MonitorFrameBufferStats {
  droppedFrames: number;
  truncated: boolean;
  lastSeq: FrameSeq | null;
  replayLimit: number;
}

/** 契約 §1 dedupe (`(seq, event_type)`) + tail window 保持。 */
export class MonitorFrameBuffer {
  readonly replayLimit: number;
  private frames: AnyMonitorFrame[] = [];
  private seen = new Set<string>();
  private droppedFrames = 0;
  private truncated = false;

  constructor(replayLimit: number = DEFAULT_MONITOR_BUFFER_FRAMES) {
    this.replayLimit = Math.max(1, replayLimit);
  }

  get stats(): MonitorFrameBufferStats {
    return {
      droppedFrames: this.droppedFrames,
      truncated: this.truncated,
      lastSeq: this.frames.length > 0 ? (this.frames[this.frames.length - 1]?.seq ?? null) : null,
      replayLimit: this.replayLimit,
    };
  }

  get lastSeq(): FrameSeq | null {
    return this.stats.lastSeq;
  }

  add(frame: AnyMonitorFrame): boolean {
    return this.addMany([frame]).length > 0;
  }

  addMany(incoming: readonly AnyMonitorFrame[]): AnyMonitorFrame[] {
    const accepted: AnyMonitorFrame[] = [];
    for (const frame of incoming) {
      const key = monitorFrameDedupeKey(frame);
      if (this.seen.has(key)) {
        continue;
      }
      this.seen.add(key);
      accepted.push(frame);
    }
    accepted.sort(compareMonitorFrames);
    this.frames = mergeMonitorFrames(accepted, this.frames);
    if (this.frames.length > this.replayLimit) {
      const excess = this.frames.length - this.replayLimit;
      const dropped = this.frames.slice(0, excess);
      this.frames = this.frames.slice(excess);
      for (const frame of dropped) {
        this.seen.delete(monitorFrameDedupeKey(frame));
      }
      this.droppedFrames += dropped.length;
      this.truncated = true;
    }
    return accepted;
  }

  snapshot(): AnyMonitorFrame[] {
    return [...this.frames];
  }

  latest(limit: number): AnyMonitorFrame[] {
    return this.frames.slice(Math.max(0, this.frames.length - limit));
  }

  framesForTurn(turnId: TurnId): AnyMonitorFrame[] {
    return this.frames.filter((frame) => frame.turn_id === turnId);
  }
}

/* --------------------------------------------------------------------------
 * SSE client (+ Last-Event-ID replay / fallback)
 * -------------------------------------------------------------------------- */

export type MonitorConnectionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "stalled"
  | "closed"
  | "error";

export type MonitorReplayMode = "last-event-id" | "after_seq";

export interface MonitorConnectionInfo {
  status: MonitorConnectionStatus;
  attempt: number;
  mode: MonitorReplayMode | null;
  lastSeq: FrameSeq | null;
  message: string | null;
}

/** 再接続/replay の状態スナップショット (実行監視 pane の接続表示用)。 */
export interface MonitorStreamState {
  status: MonitorConnectionStatus;
  lastSeq: FrameSeq | null;
  mode: MonitorReplayMode | null;
  attempts: number;
  lastError: string | null;
  closed: boolean;
}

/** README §9.6 / §10.3 monitor endpoint。frontend は host prefix を持たない (README §9.5)。 */
export function monitorEventsUrl(
  threadId: ThreadId,
  afterSeq: FrameSeq | null,
  limit: number = DEFAULT_MONITOR_REPLAY_LIMIT,
): string {
  const url = new URL(`/api/agui/sessions/${encodeURIComponent(threadId)}/events`, "http://localhost");
  if (afterSeq !== null) {
    url.searchParams.set("after_seq", String(afterSeq));
  }
  url.searchParams.set("limit", String(Math.max(1, limit)));
  return `${url.pathname}${url.search}`;
}

export function chatFramesUrl(threadId: ThreadId, afterSeq: FrameSeq | null): string {
  const url = new URL(`/api/chat/sessions/${encodeURIComponent(threadId)}/frames`, "http://localhost");
  if (afterSeq !== null) {
    url.searchParams.set("after_seq", String(afterSeq));
  }
  return `${url.pathname}${url.search}`;
}

export interface MonitorStreamHandlers {
  onFrames?: (frames: readonly AnyMonitorFrame[]) => void;
  onStatus?: (info: MonitorConnectionInfo) => void;
}

/**
 * 1 本の SSE 購読を read し、frame 不在時は `Last-Event-ID` を (`after_seq=` fallback は
 * backend が `Last-Event-ID` を嫌ったときだけ) 使う reconnect ループ。
 * fetch / timer は注入可能 (README §14 unit test: 偽 EventSource)。
 */
/** 注入可能な fetch (README §14: 偽 fetch/EventSource で net-free に test する)。 */
export type MonitorFetch = (url: string, init?: RequestInit) => Promise<Response>;

export interface MonitorOptions {
  threadId: ThreadId;
  /** 既存 stream への合流点 (snapshot の `frames_tail.seq`)。 */
  initialAfterSeq?: FrameSeq | null;
  buffer?: MonitorFrameBuffer;
  replayLimit?: number;
  heartbeatSeconds?: number;
  onFrames?: (frames: readonly AnyMonitorFrame[], stats: MonitorFrameBufferStats) => void;
  onStatus?: (info: MonitorConnectionInfo) => void;
  onError?: (error: Error) => void;
  /** comment line または `event_type=heartbeat` frame を受けるたびに呼ばれる (README §10.3)。 */
  onHeartbeat?: (count: number) => void;
  fetch?: MonitorFetch;
  delay?: (ms: number) => Promise<void>;
  now?: () => number;
  rng?: () => number;
  requestHeaders?: Record<string, string>;
  maxAttempts?: number;
}

export interface MonitorSubscription {
  close: () => void;
  readonly state: MonitorStreamState;
  readonly buffer: MonitorFrameBuffer;
  /** buffer 内からその turn の frame を取り出す (実行監視 pane 用)。 */
  framesForTurn: (turnId: TurnId) => AnyMonitorFrame[];
  /** watchdog: heartbeat 途絶なら現在購読を打ち切って再接続する。 */
  tick: () => void;
}

class SseReadError extends Error {
  readonly status: number | null;

  constructor(status: number | null, detail: string) {
    super(detail);
    this.name = "SseReadError";
    this.status = status;
  }
}

/* --------------------------------------------------------------------------
 * 1 本の SSE response を raw frame として読む (`id:` / comment 保存)
 * -------------------------------------------------------------------------- */

interface RawSseFrame {
  readonly event: string;
  readonly data: string;
  readonly id: string | null;
  readonly comment: boolean;
}

async function* readMonitorSse(
  response: Response,
  signal: AbortSignal,
): AsyncGenerator<RawSseFrame> {
  if (!response.ok) {
    throw new SseReadError(response.status, `monitor SSE request failed: ${response.status}`);
  }
  if (!response.body) {
    throw new SseReadError(null, "monitor SSE response has no body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const next = await reader.read();
      if (next.done) {
        break;
      }
      buffer += decoder.decode(next.value, { stream: true });
      for (;;) {
        const boundary = findBoundary(buffer);
        if (!boundary) {
          break;
        }
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const parsed = parseSseEventBlock(block);
        if (parsed) {
          yield { event: parsed.event, data: parsed.data, id: parsed.id, comment: parsed.commentOnly };
        }
      }
    }
    if (!signal.aborted) {
      const tail = buffer.trim();
      if (tail !== "") {
        const parsed = parseSseEventBlock(tail);
        if (parsed) {
          yield { event: parsed.event, data: parsed.data, id: parsed.id, comment: parsed.commentOnly };
        }
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // reader is already closed
    }
  }
}

function parseMonitorFrameData(data: string): AnyMonitorFrame[] {
  if (data.trim() === "") {
    return [];
  }
  let decoded: unknown;
  try {
    decoded = JSON.parse(data);
  } catch {
    return [];
  }
  const items = Array.isArray(decoded) ? decoded : [decoded];
  const frames: AnyMonitorFrame[] = [];
  for (const item of items) {
    const frame = asAnyMonitorFrame(item);
    if (frame) {
      frames.push(frame);
    }
  }
  return frames;
}

function classifySseError(error: unknown): {
  message: string;
  definitive: boolean;
  rejectLastEventId: boolean;
  aborted: boolean;
} {
  if (error instanceof SseReadError) {
    const status = error.status;
    return {
      message: error.message,
      definitive: status !== null && (status === 401 || status === 403 || status === 404 || status === 410),
      // backend が `Last-Event-ID` を replay 要求として受け付けない場合 (400 / 422) は
      // `after_seq=` へ切り替える (契約 §4 の query param が正式な replay key)。
      rejectLastEventId: status === 400 || status === 422 || status === 409,
      aborted: false,
    };
  }
  if (error instanceof Error) {
    if (error.name === "AbortError") {
      return { message: "monitor SSE aborted", definitive: false, rejectLastEventId: false, aborted: true };
    }
    return { message: error.message, definitive: false, rejectLastEventId: false, aborted: false };
  }
  return { message: "unknown monitor SSE failure", definitive: false, rejectLastEventId: false, aborted: false };
}

const DEFAULT_MAX_ATTEMPTS = 8;

export function subscribeMonitor(options: MonitorOptions): MonitorSubscription {
  const buffer =
    options.buffer ?? new MonitorFrameBuffer(options.replayLimit ?? DEFAULT_MONITOR_BUFFER_FRAMES);
  const heartbeatSeconds = options.heartbeatSeconds ?? DEFAULT_MONITOR_HEARTBEAT_SECONDS;
  const replayLimit = options.replayLimit ?? DEFAULT_MONITOR_REPLAY_LIMIT;
  const now = options.now ?? ((): number => Date.now());
  const rng = options.rng ?? Math.random;
  const fetchImpl: MonitorFetch = options.fetch ?? ((url, init) => fetch(url, init));
  const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
  const threadId = options.threadId;

  const state: MonitorStreamState = {
    status: "idle",
    lastSeq: options.initialAfterSeq ?? null,
    mode: null,
    attempts: 0,
    lastError: null,
    closed: false,
  };
  let closed = false;
  let lastActivityAt = now();
  let preferAfterSeq = false;
  let heartbeats = 0;
  let attemptController: AbortController | null = null;

  const emitStatus = (status: MonitorConnectionStatus, message: string | null = null): void => {
    state.status = status;
    options.onStatus?.({
      status,
      attempt: state.attempts,
      mode: state.mode,
      lastSeq: state.lastSeq,
      message,
    });
  };

  const noteSeq = (frame: AnyMonitorFrame, sseId: string | null): void => {
    let candidate: number | null = frame.seq;
    if (sseId !== null) {
      const fromId = Number.parseInt(sseId, 10);
      if (Number.isFinite(fromId) && (candidate === null || fromId > candidate)) {
        candidate = fromId;
      }
    }
    if (candidate !== null && (state.lastSeq === null || candidate > state.lastSeq)) {
      state.lastSeq = candidate;
    }
  };

  const consume = (frames: readonly AnyMonitorFrame[], sseId: string | null): number => {
    for (const frame of frames) {
      noteSeq(frame, sseId);
    }
    const accepted = buffer.addMany(frames);
    if (accepted.length > 0) {
      options.onFrames?.(accepted, buffer.stats);
    }
    return accepted.length;
  };

  const sleep = async (ms: number): Promise<void> => {
    if (closed) {
      return;
    }
    if (options.delay) {
      await options.delay(ms);
      return;
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, ms);
      timer.unref?.();
    });
  };

  const run = async (): Promise<void> => {
    while (!closed) {
      const nowMs = now();
      if (nowMs - lastActivityAt > heartbeatSeconds * 1000 * HEARTBEAT_STALL_FACTOR) {
        emitStatus("stalled", "heartbeat 途絶");
      }
      const afterSeq = state.lastSeq;
      const useAfterSeq = preferAfterSeq || afterSeq === null;
      const url = monitorEventsUrl(threadId, useAfterSeq ? afterSeq : null, replayLimit);
      const headers: Record<string, string> = {
        Accept: "text/event-stream",
        ...options.requestHeaders,
      };
      if (!useAfterSeq && afterSeq !== null) {
        headers["Last-Event-ID"] = String(afterSeq);
      }
      state.mode = useAfterSeq ? "after_seq" : "last-event-id";
      emitStatus(state.attempts === 0 ? "connecting" : "reconnecting", state.lastError);

      const controller = new AbortController();
      attemptController = controller;
      let framesRead = 0;
      try {
        const response = await fetchImpl(url, { signal: controller.signal, headers });
        if (!response.ok) {
          throw new SseReadError(response.status, `monitor SSE request failed: ${response.status}`);
        }
        state.status = "open";
        // `event_type=heartbeat` frame (契約 §2) が届くまでの keep-alive 検知用。
        emitStatus("open", null);
        for await (const raw of readMonitorSse(response, controller.signal)) {
          if (closed || controller.signal.aborted) {
            break;
          }
          lastActivityAt = now();
          if (raw.comment) {
            heartbeats += 1;
            options.onHeartbeat?.(heartbeats);
            continue;
          }
          const parsed = parseMonitorFrameData(raw.data);
          if (parsed.length === 0) {
            continue;
          }
          if (parsed.some((frame) => frame.event_type === "heartbeat")) {
            heartbeats += 1;
            options.onHeartbeat?.(heartbeats);
          }
          framesRead += consume(parsed, raw.id);
        }
        if (closed) {
          return;
        }
        if (framesRead > 0) {
          state.attempts = 0;
          state.lastError = null;
          continue; // backend は heartbeat で stream を開いたままにする想定 → 再接続して追随
        }
        // frame 1 つ無いまま stream が閉じた =障害として backoff する (hot loop 回避)。
        state.attempts += 1;
        state.lastError = "monitor stream ended without frames";
        if (state.attempts > maxAttempts) {
          closed = true;
          state.closed = true;
          emitStatus("error", state.lastError);
          options.onError?.(new Error(state.lastError));
          return;
        }
        emitStatus("reconnecting", state.lastError);
        await sleep(monitorBackoffDelay(state.attempts, rng));
      } catch (error) {
        if (closed) {
          return;
        }
        const classified = classifySseError(error);
        state.lastError = classified.message;
        if (classified.aborted) {
          // heartbeat watchdog (tick) 起点の能動切断 → 即再接続 (backoff しない)
          emitStatus("reconnecting", classified.message);
          continue;
        }
        if (classified.rejectLastEventId && !preferAfterSeq) {
          preferAfterSeq = true;
          emitStatus("reconnecting", `Last-Event-ID 拒否 → after_seq fallback: ${classified.message}`);
          continue;
        }
        if (classified.definitive) {
          closed = true;
          state.closed = true;
          emitStatus("error", classified.message);
          options.onError?.(error instanceof Error ? error : new Error(classified.message));
          return;
        }
        state.attempts += 1;
        if (state.attempts > maxAttempts) {
          closed = true;
          state.closed = true;
          emitStatus("error", `${classified.message} (retry limit)`);
          options.onError?.(error instanceof Error ? error : new Error(classified.message));
          return;
        }
        emitStatus("error", classified.message);
        await sleep(monitorBackoffDelay(state.attempts, rng));
      } finally {
        attemptController = null;
      }
    }
    if (!state.closed) {
      state.closed = true;
      emitStatus("closed", null);
    }
  };

  void run();

  return {
    close: () => {
      if (closed) {
        return;
      }
      closed = true;
      state.closed = true;
      attemptController?.abort();
      emitStatus("closed", null);
    },
    get state() {
      return { ...state };
    },
    buffer,
    framesForTurn: (turnId: TurnId) => buffer.framesForTurn(turnId),
    tick: () => {
      if (closed) {
        return;
      }
      const nowMs = now();
      if (nowMs - lastActivityAt > heartbeatSeconds * 1000 * HEARTBEAT_STALL_FACTOR) {
        lastActivityAt = nowMs;
        emitStatus("stalled", "heartbeat 途絶 → 再接続");
        attemptController?.abort();
      }
    },
  };
}

/** README §14 / §9.6: 指数 backoff + jitter (base 1s, cap 30s)。 */
export function monitorBackoffDelay(
  attempt: number,
  rng: () => number = Math.random,
  baseMs: number = BASE_BACKOFF_MS,
  maxMs: number = MAX_BACKOFF_MS,
): number {
  const safeAttempt = Math.max(1, attempt);
  const exponential = Math.min(baseMs * 2 ** (safeAttempt - 1), maxMs);
  const jitter = exponential * JITTER_RATIO * Math.max(0, Math.min(1, rng()));
  return Math.round(exponential + jitter);
}

/** heartbeat 途絶を stall とみなす係数 (heartbeat 周期の N 倍届かないなら切断扱い)。 */
export const HEARTBEAT_STALL_FACTOR = 3;

