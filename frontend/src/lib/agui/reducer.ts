// AG-UI run stream (§3) と monitor frames (§2) を 1 本の transcript に統合する reducer。
// join key の正は契約 §1:
//   thread_id == conversation_id == ChatSession.id
//   turn_id      = "{thread_id}:t{n}"  (canonical)
//   message_id   = "{turn_id}:message", parent_message_id = message_id
//   tool_call_id = "{turn_id}:tool:{n}"
//   approval_id  = PermissionBroker の approval_id == AG-UI Interrupt.id
// 意味論は backend `app/services/session_events.py` (_EVENT_RANK / dedupe_frames /
// merge_frames / latest_status_by_turn / error_type_for / pending_turn_outcome) の
// 等価実装として揃える (backend mapper との不一致は bug 扱い)。

import type { BaseEvent, Message as AgUiMessage } from "@ag-ui/core";
import { EventType, getRunOutcome } from "@ag-ui/client";
import type { AgentSubscriberParams, RunAgentResult } from "@ag-ui/client";
import type {
  ApprovalRecord as ApprovalRecordDto,
  ContextUsageDto,
  CostAmount as CostAmountDto,
  DecidedBy,
  JudgmentDto,
  PermissionSource,
  RuleChangedDto,
  SessionEventRowDto,
  TokenUsage as TokenUsageDto,
  UsageWarningPayload as UsageWarningDto,
} from "@/lib/types";
import {
  asAnyMonitorFrame,
  asSessionEventRow,
  normalizeApprovalRecord,
  normalizeContextUsage,
  normalizeCostAmount,
  normalizeInterruptPayload,
  normalizeJudgmentDto,
  normalizeRuleDto,
  normalizeTokenUsage,
} from "@/lib/monitor";
import type {
  AnyMonitorFrame,
  HeartbeatFrame,
  InterruptHintFrame,
  InterruptWirePayload,
  SessionEventRecordedFrame,
} from "@/lib/monitor";

export type TurnId = string;
export type MessageId = string;
export type ToolCallId = string;
export type ApprovalId = string;

export type ToolCallStatus =
  | "started"
  | "streaming"
  | "pending_approval"
  | "approved_rejected"
  | "complete"
  | "error";

export type TurnStatus =
  | "unknown"
  | "queued"
  | "running"
  | "streaming"
  | "awaiting_approval"
  | "resuming"
  | "finished"
  | "cancelled"
  | "error";

export const TURN_OUTCOME_FRAME_TYPES: ReadonlySet<string> = new Set([
  "run_finished",
  "run_cancelled",
  "error",
]);

const TERMINAL_FRAME_SET = TURN_OUTCOME_FRAME_TYPES;

/// backend `_EVENT_RANK` の複製 (turn 毎の latest status join 順)。
export const EVENT_RANK: Readonly<Record<string, number>> = {
  agent_run_started: 0,
  run_queued: 0,
  assistant_text_delta: 1,
  thinking_delta: 2,
  permission_requested: 3,
  tool_call_started: 3,
  tool_call_args: 4,
  tool_call_complete: 5,
  permission_resolved: 6,
  approval_requested: 6,
  approval_resolved: 7,
  pending_approvals_updated: 7,
  error: 8,
  run_cancelled: 8,
  message_complete: 9,
  run_finished: 9,
};

export const TOOL_FRAME_TYPES: ReadonlySet<string> = new Set([
  "tool_call_started",
  "tool_call_args",
  "tool_call_complete",
]);

const TURN_TERMINAL_RANK = 8;

export function eventRank(eventType: string): number {
  const rank = EVENT_RANK[eventType];
  return typeof rank === "number" ? rank : -1;
}

/// backend `dedupe_frames` の複製: `(seq, event_type)` keep-first。
export function dedupeFrames(frames: readonly AnyMonitorFrame[]): AnyMonitorFrame[] {
  const seen = new Set<string>();
  const result: AnyMonitorFrame[] = [];
  for (const frame of frames) {
    const key = `${frame.seq}:${frame.event_type}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(frame);
  }
  return result;
}

/// backend `merge_frames` の複製: dedupe 後 `(seq, created_at)` 安定ソート。
export function mergeFrames(...groups: readonly (readonly AnyMonitorFrame[])[]): AnyMonitorFrame[] {
  const flat: AnyMonitorFrame[] = [];
  for (const group of groups) {
    for (const frame of group) {
      flat.push(frame);
    }
  }
  return dedupeFrames(flat).sort(
    (a, b) => a.seq - b.seq || (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0),
  );
}

/// backend `latest_status_by_turn` の複製: turn 毎に「最大 seq + 上位 rank」の 1 frame。
export function latestStatusByTurn(frames: readonly AnyMonitorFrame[]): Map<TurnId, AnyMonitorFrame> {
  const best = new Map<TurnId, AnyMonitorFrame>();
  const bestRank = new Map<TurnId, number>();
  for (const frame of frames) {
    const turnId = frame.turn_id;
    if (!turnId) {
      continue;
    }
    const rank = eventRank(String(frame.event_type));
    const current = best.get(turnId);
    const currentSeq = current ? current.seq : -1;
    const betterSeq = frame.seq > currentSeq;
    const sameSeq = frame.seq === currentSeq && rank > (bestRank.get(turnId) ?? -1);
    if (!current || betterSeq || sameSeq) {
      best.set(turnId, frame);
      bestRank.set(turnId, rank);
    }
  }
  return best;
}

/// backend `error_type_for` の複製。
export function errorTypeFor(frame: {
  event_type?: string;
  error_type?: unknown;
  payload?: Record<string, unknown>;
}): string {
  const raw = typeof frame.error_type === "string" ? frame.error_type : "";
  if (raw) {
    return raw;
  }
  const eventType = String(frame.event_type ?? "");
  if (eventType === "tool_call_complete" && String(frame.payload?.["status"] ?? "") === "denied") {
    return "no_matching_grant";
  }
  if (eventType === "run_cancelled") {
    return "turn_cancelled";
  }
  if (eventType === "run_finished") {
    return "turn_finished";
  }
  return "runtime_error";
}

export interface ToolCallState {
  toolCallId: ToolCallId;
  toolName: string;
  parentMessageId: string | null;
  argsText: string;
  status: ToolCallStatus;
  result?: string | null;
  resultComplete?: boolean;
  permissionSource?: string | null;
  approvalId?: string | null;
}

export interface TurnErrorInfo {
  errorType: string;
  code: string | null;
  message: string;
  source: string | null;
  recoverable: boolean | null;
  frameSeq: number;
}

export interface TurnInterruptState {
  interrupts: InterruptWirePayload[];
  reason: string | null;
  detectedAt: string | null;
  resolvedInterruptIds: ApprovalId[];
}

export interface TurnOutcome {
  stopReason: string | null;
  source: "frame" | "queued" | "absent" | "terminal";
}

export interface TranscriptTurn {
  turnId: TurnId;
  threadId: string | null;
  sessionId: string | null;
  messageId: MessageId | null;
  parentMessageId: MessageId | null;
  text: string;
  textStreamSeq: number;
  textComplete: boolean;
  toolCalls: ToolCallState[];
  status: TurnStatus;
  stopReason: string | null;
  usage: TokenUsageDto | null;
  totalCost: CostAmountDto | null;
  pendingApprovalIds: ApprovalId[];
  pendingPermissionIds: ApprovalId[];
  permissionSource: string | null;
  interrupt: TurnInterruptState | null;
  error: TurnErrorInfo | null;
  lastSeq: number;
  terminalSeq: number | null;
  createdAt: string | null;
  endedAt: string | null;
}

export interface TranscriptState {
  threadId: string;
  sessionId: string | null;
  turns: Map<TurnId, TranscriptTurn>;
  turnOrder: TurnId[];
  /** AG-UI event から作った provisional turn の alias (provisional -> canonical)。 */
  turnAliases: Map<TurnId, TurnId>;
  seenFrameKeys: Set<string>;
  frames: AnyMonitorFrame[];
  lastSeq: number;
  approvals: Map<ApprovalId, ApprovalRecordDto>;
  pendingApprovalIds: ApprovalId[];
  sessionEvents: SessionEventRowDto[];
  ruleChanges: RuleChangedDto[];
  judgments: JudgmentDto[];
  heartbeat: HeartbeatFrame | null;
  contextUsage: ContextUsageDto | null;
  usageWarning: UsageWarningDto | null;
  lastError: TurnErrorInfo | null;
}

export function createTranscriptState(threadId: string, seed?: Partial<TranscriptState>): TranscriptState {
  return {
    threadId,
    sessionId: seed?.sessionId ?? null,
    turns: seed?.turns ?? new Map(),
    turnOrder: seed?.turnOrder ?? [],
    turnAliases: seed?.turnAliases ?? new Map(),
    seenFrameKeys: seed?.seenFrameKeys ?? new Set(),
    frames: seed?.frames ?? [],
    lastSeq: seed?.lastSeq ?? 0,
    approvals: seed?.approvals ?? new Map(),
    pendingApprovalIds: seed?.pendingApprovalIds ?? [],
    sessionEvents: seed?.sessionEvents ?? [],
    ruleChanges: seed?.ruleChanges ?? [],
    judgments: seed?.judgments ?? [],
    heartbeat: seed?.heartbeat ?? null,
    contextUsage: seed?.contextUsage ?? null,
    usageWarning: seed?.usageWarning ?? null,
    lastError: seed?.lastError ?? null,
  };
}

/// §1: tool_call_id = "{turn_id}:tool:{n}" から turn_id を復元する (AG-UI 側 join)。
export function turnIdOfToolCallId(toolCallId: string): string | null {
  const match = /^(.*):tool:[0-9]+$/.exec(toolCallId);
  return match ? match[1] : null;
}

/// §1: message_id = "{turn_id}:message" から turn_id を復元する。
export function turnIdOfMessageId(messageId: string): string | null {
  const match = /^(.*):message$/.exec(messageId);
  return match ? match[1] : null;
}

export function messageIdOfTurn(turnId: string): MessageId {
  return `${turnId}:message`;
}

function canonicalize(state: TranscriptState, turnId: TurnId): TurnId {
  const seen = new Set<TurnId>();
  let current = turnId;
  while (!seen.has(current)) {
    seen.add(current);
    const alias = state.turnAliases.get(current);
    if (!alias) {
      break;
    }
    current = alias;
  }
  return current;
}

function newTurn(state: TranscriptState, turnId: TurnId): TranscriptTurn {
  return {
    turnId,
    threadId: state.threadId,
    sessionId: state.sessionId,
    messageId: turnId.includes(":") ? null : messageIdOfTurn(turnId),
    parentMessageId: null,
    text: "",
    textStreamSeq: 0,
    textComplete: false,
    toolCalls: [],
    status: "unknown",
    stopReason: null,
    usage: null,
    totalCost: null,
    pendingApprovalIds: [],
    pendingPermissionIds: [],
    permissionSource: null,
    interrupt: null,
    error: null,
    lastSeq: 0,
    terminalSeq: null,
    createdAt: null,
    endedAt: null,
  };
}

function ensureTurn(state: TranscriptState, turnId: TurnId): TranscriptTurn {
  const canonical = canonicalize(state, turnId);
  const existing = state.turns.get(canonical);
  if (existing) {
    return existing;
  }
  const turn = newTurn(state, canonical);
  state.turns.set(canonical, turn);
  state.turnOrder.push(canonical);
  return turn;
}

/// provisional (AG-UI 由来) turn を monitor frame の canonical turn_id へ寄せ合わせる。
function promoteTurn(state: TranscriptState, provisionalId: TurnId, canonicalId: TurnId): TranscriptTurn {
  if (provisionalId === canonicalId) {
    return ensureTurn(state, canonicalId);
  }
  const provisional = state.turns.get(provisionalId);
  const canonicalExisting = state.turns.get(canonicalId);
  if (!provisional) {
    return ensureTurn(state, canonicalId);
  }
  if (!canonicalExisting) {
    state.turns.delete(provisionalId);
    provisional.turnId = canonicalId;
    if (!provisional.messageId) {
      provisional.messageId = messageIdOfTurn(canonicalId);
    }
    state.turns.set(canonicalId, provisional);
    const index = state.turnOrder.indexOf(provisionalId);
    if (index >= 0) {
      state.turnOrder[index] = canonicalId;
    } else {
      state.turnOrder.push(canonicalId);
    }
    state.turnAliases.set(provisionalId, canonicalId);
    return provisional;
  }
  mergeTurnInto(canonicalExisting, provisional);
  state.turns.delete(provisionalId);
  const index = state.turnOrder.indexOf(provisionalId);
  if (index >= 0) {
    state.turnOrder.splice(index, 1);
  }
  state.turnAliases.set(provisionalId, canonicalId);
  return canonicalExisting;
}

function mergeTurnInto(target: TranscriptTurn, source: TranscriptTurn): void {
  if (source.text) {
    target.text = source.text.length > target.text.length ? source.text : target.text;
  }
  target.textStreamSeq = Math.max(target.textStreamSeq, source.textStreamSeq);
  target.textComplete = target.textComplete || source.textComplete;
  for (const tool of source.toolCalls) {
    mergeToolCall(target, tool);
  }
  if (source.terminalSeq !== null) {
    target.terminalSeq = target.terminalSeq === null ? source.terminalSeq : Math.min(target.terminalSeq, source.terminalSeq);
  }
  target.lastSeq = Math.max(target.lastSeq, source.lastSeq);
  if (!target.error && source.error) {
    target.error = source.error;
  }
  if (!target.interrupt && source.interrupt) {
    target.interrupt = source.interrupt;
  }
  if (target.status === "unknown" && source.status !== "unknown") {
    target.status = source.status;
  }
  for (const id of source.pendingApprovalIds) {
    if (!target.pendingApprovalIds.includes(id)) {
      target.pendingApprovalIds.push(id);
    }
  }
  if (!target.messageId && source.messageId) {
    target.messageId = source.messageId;
  }
  if (source.sessionId && !target.sessionId) {
    target.sessionId = source.sessionId;
  }
}

function payloadOf(frame: AnyMonitorFrame): Record<string, unknown> {
  return (frame.payload ?? {}) as unknown as Record<string, unknown>;
}

/** frame の envelope を payload 変換へ流し込むための record 化 (snake_case wire  그대로)。 */
function frameAsRecord(frame: AnyMonitorFrame): Record<string, unknown> {
  return frame as unknown as Record<string, unknown>;
}

/** interrupt metadata から tool 名を取り出す (wire 上 metadata.tool_name)。 */
function interruptToolName(interrupt: InterruptWirePayload): string | null {
  const metadata = interrupt.metadata;
  if (isRecord(metadata)) {
    const name = metadata["tool_name"];
    if (typeof name === "string" && name.length > 0) {
      return name;
    }
  }
  return null;
}

/** InterruptWirePayload 同款の一意性判定 (id 優先、無ければ tool_call_id)。 */
function sameInterrupt(a: InterruptWirePayload, b: InterruptWirePayload): boolean {
  if (a.id && b.id) {
    return a.id === b.id;
  }
  if (a.tool_call_id && b.tool_call_id) {
    return a.tool_call_id === b.tool_call_id;
  }
  return false;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

const PERMISSION_SOURCES: readonly PermissionSource[] = [
  "pre-execution-allow-list",
  "agent-reflect",
  "rule-precedence",
  "approved_interactively",
  "auto_executed",
  "policy-timeout",
  "rejected_interactively",
];

const DECIDED_BY_VALUES: readonly DecidedBy[] = [
  "family",
  "policy",
  "agent",
  "system",
  "policy-timeout",
  "session-closed",
];

/** 契約 §5 の列挙値以外 (旧 wire 値など) は null に落とす。 */
function asPermissionSource(value: unknown): PermissionSource | null {
  return PERMISSION_SOURCES.find((candidate) => candidate === value) ?? null;
}

function asDecidedBy(value: unknown): DecidedBy | null {
  return DECIDED_BY_VALUES.find((candidate) => candidate === value) ?? null;
}

/** mergeToolCall 用の patch。statusOverride = monitor 側確定 status を優先適用するフラグ。 */
interface ToolCallPatch {
  toolCallId: ToolCallId;
  toolName?: string | null;
  parentMessageId?: string | null;
  argsText?: string | null;
  status?: ToolCallStatus | null;
  statusOverride?: boolean;
  result?: string | null;
  resultComplete?: boolean;
  permissionSource?: string | null;
  approvalId?: string | null;
}

function mergeToolCall(
  turn: TranscriptTurn,
  patch: ToolCallPatch,
): ToolCallState {
  const existing = turn.toolCalls.find((tool) => tool.toolCallId === patch.toolCallId);
  if (!existing) {
    const created: ToolCallState = {
      toolCallId: patch.toolCallId,
      toolName: patch.toolName ?? "tool",
      parentMessageId: patch.parentMessageId ?? turn.messageId ?? null,
      argsText: patch.argsText ?? "",
      status: patch.status ?? "started",
      result: patch.result ?? null,
      resultComplete: patch.resultComplete === true || (patch.result !== null && patch.result !== undefined),
      permissionSource: patch.permissionSource ?? null,
      approvalId: patch.approvalId ?? null,
    };
    turn.toolCalls.push(created);
    return created;
  }
  if (patch.toolName && patch.toolName !== "tool") {
    existing.toolName = patch.toolName;
  }
  if (patch.argsText !== null && patch.argsText !== undefined) {
    existing.argsText = patch.argsText;
  }
  if (patch.parentMessageId && !existing.parentMessageId) {
    existing.parentMessageId = patch.parentMessageId;
  }
  if (patch.result !== null && patch.result !== undefined) {
    existing.result = patch.result;
    existing.resultComplete = true;
  }
  if (patch.permissionSource) {
    existing.permissionSource = patch.permissionSource;
  }
  if (!existing.approvalId) {
    const patchApprovalId = patch.approvalId;
    if (patchApprovalId) {
      existing.approvalId = patchApprovalId;
    }
  }
  if (patch.status) {
    // monitor 側の確定 status (§2 latest_status_by_turn) は statusOverride で適用する。
    // AG-UI 側 (再生成 / 遅延フレーム) が pending_approval を巻き戻さないためのガード。
    const allowErrorAfterReject =
      existing.status === "approved_rejected" && patch.status === "error";
    const canOverride =
      patch.statusOverride === true ||
      existing.status !== "pending_approval" ||
      patch.status === "pending_approval" ||
      patch.status === "approved_rejected";
    if (canOverride || allowErrorAfterReject) {
      existing.status = patch.status;
    }
  }
  return existing;
}

/** AG-UI run stream 側から tool をマージする (§1 out-of-order 耐性ガード付き)。 */
function mergeAgUiToolCall(
  turn: TranscriptTurn,
  patch: Omit<ToolCallPatch, "statusOverride"> & { statusOverride?: boolean },
): ToolCallState {
  const existing = turn.toolCalls.find((tool) => tool.toolCallId === patch.toolCallId);
  const protectedStatus =
    existing !== undefined &&
    (existing.status === "pending_approval" || existing.status === "approved_rejected") &&
    patch.statusOverride !== true;
  if (protectedStatus) {
    const { status: _status, ...rest } = patch;
    return mergeToolCall(turn, rest);
  }
  return mergeToolCall(turn, patch);
}

function recordApproval(state: TranscriptState, frame: AnyMonitorFrame): ApprovalRecordDto | null {
  const payload = payloadOf(frame);
  const frameRecord = frameAsRecord(frame);
  const nested = isRecord(payload["approval"])
    ? payload["approval"]
    : isRecord(payload["record"])
      ? payload["record"]
      : null;
  const record = normalizeApprovalRecord({ ...frameRecord, ...payload, ...(nested ?? {}) });
  const nestedApprovalId = nested ? str(nested["approval_id"]) ?? str(nested["id"]) : null;
  const approvalId = record.approval_id ?? str(payload["approval_id"]) ?? nestedApprovalId;
  if (!approvalId) {
    return null;
  }
  const existing = state.approvals.get(approvalId);
  const ownTurnId = record.turn_id ?? frame.turn_id;
  const nestedParentId = nested ? str(nested["parent_message_id"]) : null;
  const stalePendingDowngrade =
    existing !== undefined &&
    existing.status !== "pending" &&
    record.status === "pending" &&
    (existing.sequence ?? 0) >= (record.sequence ?? frame.seq);
  if (stalePendingDowngrade) {
    // 解決済み承認へ遅延した pending フレームを適用しない (§1 out-of-order 耐性)。
    return existing;
  }
  const merged: ApprovalRecordDto = {
    ...existing,
    ...record,
    approval_id: approvalId,
    prompt_id: record.prompt_id ?? existing?.prompt_id ?? null,
    session_id: record.session_id ?? existing?.session_id ?? frame.session_id,
    tool_name: record.tool_name || existing?.tool_name || str(payload["tool_name"]) || "tool",
    arguments: isRecord(payload["arguments"])
      ? payload["arguments"]
      : record.arguments ?? existing?.arguments ?? {},
    tool_call_id: record.tool_call_id ?? existing?.tool_call_id ?? str(payload["tool_call_id"]),
    turn_id: record.turn_id ?? existing?.turn_id ?? frame.turn_id,
    message_id: record.message_id ?? existing?.message_id ?? str(payload["message_id"]),
    parent_message_id:
      record.parent_message_id ??
      existing?.parent_message_id ??
      nestedParentId ??
      (ownTurnId ? messageIdOfTurn(ownTurnId) : null),
    status: record.status ?? existing?.status ?? "pending",
    decided_by:
      record.decided_by ??
      existing?.decided_by ??
      asDecidedBy(payload["decided_by"]) ??
      asDecidedBy(payload["resolved_by"]),
    reason: record.reason ?? existing?.reason ?? str(payload["reason"]),
    permission_source:
      record.permission_source ??
      existing?.permission_source ??
      asPermissionSource(payload["permission_source"]),
    feedback: record.feedback ?? existing?.feedback ?? str(payload["feedback"]),
    created_at: record.created_at ?? existing?.created_at ?? frame.created_at,
    updated_at: frame.created_at,
    resolved_at: record.resolved_at ?? existing?.resolved_at ?? null,
    expires_at: record.expires_at ?? existing?.expires_at ?? null,
    timeout_seconds: record.timeout_seconds ?? existing?.timeout_seconds ?? null,
    sequence: record.sequence ?? frame.seq ?? existing?.sequence ?? null,
  };
  state.approvals.set(approvalId, merged);
  const index = state.pendingApprovalIds.indexOf(approvalId);
  if (merged.status === "pending") {
    if (index === -1) {
      state.pendingApprovalIds.push(approvalId);
    }
  } else if (index >= 0) {
    state.pendingApprovalIds.splice(index, 1);
  }
  return merged;
}

function applyTurnFrame(state: TranscriptState, input: AnyMonitorFrame): void {
  const rawTurnId = input.turn_id;
  if (!rawTurnId) {
    return;
  }
  const frame = { ...input, turn_id: rawTurnId } as AnyMonitorFrame;
  const turn = ensureTurn(state, rawTurnId);
  const payload = payloadOf(frame);
  const eventType = String(frame.event_type);
  const rank = eventRank(eventType);
  const terminal = TERMINAL_FRAME_SET.has(eventType);

  // out-of-order 耐性 + 完了 turn の resurrection 禁止:
  // terminal frame より古い seq の frame は status を戻さない (content merge は許容)。
  const afterTerminal = turn.terminalSeq === null || frame.seq >= turn.terminalSeq;
  const isLate = frame.seq < turn.lastSeq;
  const mayChangeStatus = afterTerminal && !(isLate && rank < TURN_TERMINAL_RANK);

  if (frame.seq > turn.lastSeq) {
    turn.lastSeq = frame.seq;
  }
  if (turn.sessionId === null && frame.session_id) {
    turn.sessionId = frame.session_id;
  }
  if (turn.createdAt === null) {
    turn.createdAt = frame.created_at;
  }

  switch (eventType) {
    case "agent_run_started": {
      if (mayChangeStatus && turn.status !== "awaiting_approval" && turn.status !== "resuming") {
        turn.status = "running";
      }
      break;
    }
    case "run_queued": {
      if (mayChangeStatus && turn.status === "unknown") {
        turn.status = "queued";
      }
      break;
    }
    case "assistant_text_delta": {
      const delta = str(payload["delta"]) ?? "";
      const parentMessageId = str(payload["parent_message_id"]);
      if (parentMessageId) {
        turn.parentMessageId = parentMessageId;
        turn.messageId = turn.messageId ?? parentMessageId;
      }
      if (frame.seq >= turn.textStreamSeq) {
        turn.textStreamSeq = frame.seq;
        turn.text += delta;
      }
      if (!terminal && mayChangeStatus && (turn.status === "unknown" || turn.status === "queued" || turn.status === "running")) {
        turn.status = "streaming";
      }
      break;
    }
    case "thinking_delta": {
      if (mayChangeStatus && turn.status === "queued") {
        turn.status = "running";
      }
      break;
    }
    case "message_complete": {
      const messageText = str(payload["text"]);
      const preview = str(payload["text_preview"]);
      const nextText = messageText ?? preview ?? turn.text;
      if (nextText.length >= turn.text.length) {
        turn.text = nextText;
      }
      const messageId = str(payload["message_id"]) ?? turn.messageId;
      if (messageId) {
        turn.messageId = messageId;
      }
      const parentMessageId = str(payload["parent_message_id"]) ?? messageId;
      if (parentMessageId) {
        turn.parentMessageId = parentMessageId;
      }
      if (mayChangeStatus && turn.status !== "awaiting_approval") {
        turn.status = "streaming";
      }
      turn.textComplete = true;
      break;
    }
    case "tool_call_started": {
      const toolCallId = str(payload["tool_call_id"]);
      if (!toolCallId) {
        break;
      }
      mergeToolCall(turn, {
        toolCallId,
        toolName: str(payload["tool_name"]) ?? "tool",
        parentMessageId: str(payload["parent_message_id"]) ?? turn.parentMessageId,
        argsText: str(payload["arguments_preview"]) ?? "",
        status: "started",
        permissionSource: str(payload["permission_source"]),
      });
      if (mayChangeStatus && turn.status !== "awaiting_approval") {
        turn.status = "running";
      }
      break;
    }
    case "tool_call_args": {
      const toolCallId = str(payload["tool_call_id"]) ?? "";
      if (!toolCallId) {
        break;
      }
      const tool = mergeToolCall(turn, { toolCallId, status: "streaming" });
      const delta = str(payload["args_delta"]) ?? "";
      tool.argsText = frame.seq >= turn.lastSeq ? `${tool.argsText}${delta}` : tool.argsText || delta;
      break;
    }
    case "tool_call_complete": {
      const toolCallId = str(payload["tool_call_id"]) ?? "";
      if (!toolCallId) {
        break;
      }
      const completion = String(payload["status"] ?? "complete");
      const status: ToolCallStatus =
        completion === "denied" ? "approved_rejected" : completion === "error" || completion === "timeout" ? "error" : "complete";
      mergeToolCall(turn, {
        toolCallId,
        toolName: str(payload["tool_name"]),
        status,
        statusOverride: true,
        result: str(payload["result_preview"]) ?? tool_result_of(payload),
        permissionSource: str(payload["permission_source"]),
      });
      if (payload["permission_source"] && mayChangeStatus) {
        turn.permissionSource = str(payload["permission_source"]);
      }
      const resolvedTurn = turn.status === "awaiting_approval" || turn.status === "resuming";
      if (mayChangeStatus && status === "error" && !resolvedTurn) {
        turn.status = "error";
        turn.error = {
          errorType: errorTypeFor({ event_type: eventType, error_type: payload["error_type"], payload }),
          code: str(payload["code"]),
          message: str(payload["message"]) ?? `tool ${completion}`,
          source: str(payload["source"]),
          recoverable: typeof payload["recoverable"] === "boolean" ? payload["recoverable"] : null,
          frameSeq: frame.seq,
        };
        registerTurnError(state, turn);
      }
      break;
    }
    case "permission_requested":
    case "approval_requested": {
      const record = recordApproval(state, frame);
      const approvalId = record?.approval_id ?? str(payload["approval_id"]);
      const toolCallId = record?.tool_call_id ?? str(payload["tool_call_id"]);
      const approvalIds = [
        ...new Set([...strArray(payload["approval_ids"]), ...(approvalId ? [approvalId] : [])]),
      ];
      if (approvalIds.length > 0) {
        const target = eventType === "approval_requested" ? turn.pendingApprovalIds : turn.pendingPermissionIds;
        for (const id of approvalIds) {
          if (!target.includes(id)) {
            target.push(id);
          }
        }
      }
      if (toolCallId) {
        mergeToolCall(turn, {
          toolCallId,
          toolName: record?.tool_name ?? str(payload["tool_name"]) ?? "tool",
          status: "pending_approval",
          parentMessageId: record?.parent_message_id ?? turn.messageId ?? null,
          approvalId: approvalId ?? approvalIds[0] ?? null,
        });
      }
      if (mayChangeStatus) {
        turn.status = "awaiting_approval";
      }
      break;
    }
    case "permission_resolved":
    case "approval_resolved": {
      const fallbackId = str(payload["approval_id"]);
      const record = recordApproval(state, frame) ?? (fallbackId ? state.approvals.get(fallbackId) ?? null : null);
      const approvalId = record?.approval_id ?? fallbackId;
      const decision =
        str(payload["decision"]) ?? str(payload["action"]) ?? record?.decided_by ?? null;
      const rejected =
        decision === "rejected" ||
        decision === "expired" ||
        decision === "stale" ||
        record?.status === "rejected" ||
        record?.status === "expired" ||
        record?.status === "cancelled";
      const toolCallId = record?.tool_call_id ?? str(payload["tool_call_id"]);
      const permissionSource = record?.permission_source ?? str(payload["permission_source"]);
      if (toolCallId) {
        mergeToolCall(turn, {
          toolCallId,
          status: rejected ? "approved_rejected" : "streaming",
          statusOverride: true,
          approvalId: approvalId ?? null,
          permissionSource,
        });
      }
      if (approvalId) {
        removeFromList(turn.pendingApprovalIds, approvalId);
        removeFromList(turn.pendingPermissionIds, approvalId);
        removeFromList(state.pendingApprovalIds, approvalId);
        if (turn.interrupt) {
          const ids = turn.interrupt.interrupts
            .map((interrupt) => interrupt.id)
            .filter((id): id is string => id !== null && id !== undefined);
          if (ids.includes(approvalId) && !turn.interrupt.resolvedInterruptIds.includes(approvalId)) {
            turn.interrupt.resolvedInterruptIds.push(approvalId);
          }
        }
      }
      if (permissionSource) {
        turn.permissionSource = permissionSource;
      }
      if (mayChangeStatus && (turn.status === "awaiting_approval" || turn.status === "resuming")) {
        const stillPending =
          turn.pendingApprovalIds.length > 0 || turn.toolCalls.some((tool) => tool.status === "pending_approval");
        if (stillPending) {
          turn.status = "awaiting_approval";
        } else {
          turn.status = rejected ? "cancelled" : "resuming";
          if (rejected) {
            turn.stopReason = turn.stopReason ?? "aborted";
            turn.endedAt = frame.created_at;
            turn.terminalSeq = frame.seq;
          }
        }
      }
      break;
    }
    case "pending_approvals_updated": {
      const ids = strArray(payload["pending_approval_ids"]);
      turn.pendingApprovalIds = ids;
      if (turn.status === "resuming" && ids.length > 0 && mayChangeStatus) {
        turn.status = "awaiting_approval";
      }
      state.pendingApprovalIds = mergeApprovalIds(state.pendingApprovalIds, ids);
      break;
    }
    case "error": {
      applyErrorFrame(state, turn, frame);
      break;
    }
    case "run_cancelled": {
      turn.stopReason = str(payload["stop_reason"]) ?? "aborted";
      turn.status = "cancelled";
      turn.endedAt = frame.created_at;
      turn.terminalSeq = frame.seq;
      turn.error = {
        errorType: errorTypeFor({ event_type: eventType, error_type: payload["error_type"], payload }),
        code: str(payload["code"]),
        message: str(payload["message"]) ?? "turn cancelled",
        source: str(payload["source"]),
        recoverable: false,
        frameSeq: frame.seq,
      };
      registerTurnError(state, turn);
      break;
    }
    case "run_finished": {
      applyRunFinishedFrame(state, turn, frame, payload);
      break;
    }
    default: {
      break;
    }
  }
  if (terminal && turn.terminalSeq === null && eventType !== "run_finished") {
    turn.terminalSeq = frame.seq;
    turn.endedAt = turn.endedAt ?? frame.created_at;
  }
}

function tool_result_of(payload: Record<string, unknown>): string | null {
  for (const key of ["result", "result_json", "output"]) {
    const value = payload[key];
    if (typeof value === "string") {
      return value;
    }
    if (value !== undefined && value !== null) {
      return JSON.stringify(value);
    }
  }
  return null;
}

function removeFromList(list: string[], value: string): void {
  const index = list.indexOf(value);
  if (index >= 0) {
    list.splice(index, 1);
  }
}

function mergeApprovalIds(current: string[], incoming: string[]): string[] {
  const next = [...current];
  for (const id of incoming) {
    if (!next.includes(id)) {
      next.push(id);
    }
  }
  return next;
}

function registerTurnError(state: TranscriptState, turn: TranscriptTurn): void {
  if (turn.error) {
    state.lastError = turn.error;
  }
}

function applyErrorFrame(
  state: TranscriptState,
  turn: TranscriptTurn,
  frame: AnyMonitorFrame,
): void {
  const payload = payloadOf(frame);
  turn.error = {
    errorType: errorTypeFor({ event_type: "error", error_type: payload["error_type"] ?? frameAsRecord(frame)["error_type"], payload }),
    code: str(payload["code"]),
    message: str(payload["message"]) ?? "run error",
    source: str(payload["source"]),
    recoverable: typeof payload["recoverable"] === "boolean" ? payload["recoverable"] : null,
    frameSeq: frame.seq,
  };
  turn.stopReason = turn.stopReason ?? "send_failed";
  turn.terminalSeq = frame.seq;
  turn.endedAt = frame.created_at;
  if (turn.pendingApprovalIds.length > 0 && turn.toolCalls.some((tool) => tool.status === "pending_approval")) {
    // 承認待ちのまま send_failed になった turn は「承認待ち + 送信失敗」を保持する。
    turn.status = "error";
  } else {
    turn.status = "error";
  }
  registerTurnError(state, turn);
}

function applyRunFinishedFrame(
  state: TranscriptState,
  turn: TranscriptTurn,
  frame: AnyMonitorFrame,
  payload: Record<string, unknown>,
): void {
  const rawOutcome = isRecord(payload["outcome"]) ? payload["outcome"] : payload;
  const outcomeType = str(rawOutcome["type"]) ?? "success";
  if (outcomeType === "interrupt") {
    // §3: RUN_FINISHED の interrupt outcome は turn を完了扱いしない (tool は pending のまま)。
    applyInterruptOutcome(
      state,
      turn,
      interruptsOf(rawOutcome["interrupts"] ?? payload["interrupts"]),
      frame.created_at,
    );
    return;
  }
  if (outcomeType === "error") {
    const err: Record<string, unknown> = isRecord(rawOutcome["error"]) ? rawOutcome["error"] : rawOutcome;
    const errRecord: Record<string, unknown> = { ...payload, ...err };
    turn.error = {
      errorType: errorTypeFor({
        event_type: "error",
        error_type: errRecord["error_type"] ?? frameAsRecord(frame)["error_type"],
        payload: errRecord,
      }),
      code: str(err["code"]) ?? str(payload["code"]),
      message: str(err["message"]) ?? str(payload["message"]) ?? "run error",
      source: str(err["source"]),
      recoverable: typeof err["recoverable"] === "boolean" ? err["recoverable"] : null,
      frameSeq: frame.seq,
    };
    turn.status = "error";
    turn.stopReason = turn.stopReason ?? "send_failed";
    turn.terminalSeq = frame.seq;
    turn.endedAt = frame.created_at;
    registerTurnError(state, turn);
    return;
  }
  const result = isRecord(rawOutcome["result"]) ? rawOutcome["result"] : payload;
  const canonicalMessageId =
    str(result["message_id"]) ?? messageIdOfTurn(str(result["turn_id"]) ?? turn.turnId);
  if (canonicalMessageId && !turn.messageId) {
    turn.messageId = canonicalMessageId;
    for (const tool of turn.toolCalls) {
      if (!tool.parentMessageId) {
        tool.parentMessageId = canonicalMessageId;
      }
    }
  }
  turn.stopReason = str(result["stop_reason"]) ?? turn.stopReason ?? "end_turn";
  const usage = result["usage"];
  if (isRecord(usage)) {
    turn.usage = normalizeUsageRecord(usage) ?? turn.usage;
  }
  const totalCost = result["total_cost"];
  if (isRecord(totalCost)) {
    turn.totalCost = normalizeCostRecord(totalCost) ?? turn.totalCost;
  }
  const permissionSource = str(result["permission_source"]);  // §11 tool 完了時の permission_source
  if (permissionSource) {
    turn.permissionSource = permissionSource;
  }
  for (const tool of turn.toolCalls) {
    if (tool.status === "streaming" || tool.status === "started") {
      tool.status = "complete";
    }
  }
  if (turn.pendingApprovalIds.length > 0 && turn.toolCalls.some((tool) => tool.status === "pending_approval")) {
    // 承認待ちを残したまま run_finished になった場合は interrupt 扱いで保持する。
    turn.status = "awaiting_approval";
    return;
  }
  turn.status = "finished";
  turn.textComplete = true;
  turn.terminalSeq = frame.seq;
  turn.endedAt = frame.created_at;
  for (const approvalId of turn.pendingApprovalIds) {
    removeFromList(state.pendingApprovalIds, approvalId);
  }
  turn.pendingApprovalIds = [];
  turn.pendingPermissionIds = [];
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function interruptsOf(value: unknown): InterruptWirePayload[] {
  const raw: unknown[] = Array.isArray(value)
    ? value
    : isRecord(value)
      ? [isRecord(value["interrupt"]) ? value["interrupt"] : value]
      : [];
  const out: InterruptWirePayload[] = [];
  for (const item of raw) {
    const nested = isRecord(item) && isRecord(item["interrupt"]) ? item["interrupt"] : item;
    const interrupt = normalizeInterruptPayload(nested);
    if (!out.some((existing) => sameInterrupt(existing, interrupt))) {
      out.push(interrupt);
    }
  }
  return out;
}

function mergeInterrupts(
  current: readonly InterruptWirePayload[],
  incoming: readonly InterruptWirePayload[],
): InterruptWirePayload[] {
  const out = [...current];
  for (const item of incoming) {
    if (!out.some((existing) => sameInterrupt(existing, item))) {
      out.push(item);
    }
  }
  return out;
}

/** interrupt outcome を turn / state へ適用する (§3: RUN_FINISHED interrupt / CUSTOM interrupt 共通)。 */
function applyInterruptOutcome(
  state: TranscriptState,
  turn: TranscriptTurn,
  incoming: readonly InterruptWirePayload[],
  detectedAt: string,
): void {
  const interrupts = mergeInterrupts(turn.interrupt?.interrupts ?? [], incoming);
  turn.interrupt = {
    interrupts,
    reason: turn.interrupt?.reason ?? incoming[0]?.reason ?? "awaiting_human_approval",
    detectedAt,
    resolvedInterruptIds: turn.interrupt?.resolvedInterruptIds ?? [],
  };
  if (turn.status !== "resuming" && turn.status !== "finished") {
    turn.status = "awaiting_approval";
  }
  turn.stopReason = null;
  for (const interrupt of incoming) {
    const toolCallId = interrupt.tool_call_id;
    if (toolCallId) {
      mergeToolCall(turn, {
        toolCallId,
        toolName: interruptToolName(interrupt),
        status: "pending_approval",
        parentMessageId: turn.messageId ?? null,
        approvalId: interrupt.id,
      });
    }
    const approvalId = interrupt.id;
    if (approvalId) {
      if (!turn.pendingApprovalIds.includes(approvalId)) {
        turn.pendingApprovalIds.push(approvalId);
      }
      if (!state.pendingApprovalIds.includes(approvalId)) {
        state.pendingApprovalIds.push(approvalId);
      }
    }
  }
}

function normalizeUsageRecord(raw: Record<string, unknown>): TokenUsageDto | null {
  return normalizeTokenUsage(raw);
}

function normalizeCostRecord(raw: Record<string, unknown>): CostAmountDto | null {
  return normalizeCostAmount(raw);
}

/// monitor frame を 1 件適用する (§2 単語彙)。dedupe は `(seq, event_type)` keep-first。
export function applyMonitorFrame(state: TranscriptState, input: AnyMonitorFrame): TranscriptState {
  const frame = asAnyMonitorFrame(input);
  if (!frame) {
    return state;
  }
  const key = `${frame.seq}:${frame.event_type}`;
  if (state.seenFrameKeys.has(key)) {
    return state;
  }
  state.seenFrameKeys.add(key);
  state.frames = insertFrameBySeqMerge(state.frames, frame);
  if (frame.seq > state.lastSeq) {
    state.lastSeq = frame.seq;
  }
  if (frame.session_id && !state.sessionId) {
    state.sessionId = frame.session_id;
  }
  const payload = payloadOf(frame);
  if (frame.turn_id) {
    applyTurnFrame(state, frame);
  }
  switch (String(frame.event_type)) {
    case "heartbeat": {
      state.heartbeat = frame as HeartbeatFrame;
      if (state.heartbeat.payload.active_turn_id) {
        const turn = ensureTurn(state, String(state.heartbeat.payload.active_turn_id));
        if (turn.status === "unknown" || turn.status === "queued") {
          turn.status = "running";
        }
        turn.lastSeq = Math.max(turn.lastSeq, frame.seq);
      }
      state.pendingApprovalIds = mergeApprovalIds(
        strArray(state.heartbeat.payload.pending_approval_ids),
        state.pendingApprovalIds,
      );
      break;
    }
    case "session_event_recorded": {
      const row = asSessionEventRow(frame as SessionEventRecordedFrame);
      if (!state.sessionEvents.some((existing) => existing.eventId === row.eventId)) {
        state.sessionEvents.push(row);
      }
      break;
    }
    case "judgment_created": {
      const raw: Record<string, unknown> = isRecord(payload["judgment"]) ? payload["judgment"] : payload;
      const judgment = normalizeJudgmentDto(raw);
      if (!state.judgments.some((item) => item.judgmentId === judgment.judgmentId)) {
        state.judgments = [...state.judgments, judgment];
      }
      break;
    }
    case "rule_changed": {
      const raw: Record<string, unknown> = isRecord(payload["rule"]) ? payload["rule"] : payload;
      const rule = normalizeRuleDto(raw);
      const change: RuleChangedDto = {
        ruleId: str(payload["rule_id"]) ?? rule.ruleId,
        action: str(payload["action"]) ?? "updated",
        rule,
      };
      state.ruleChanges = [...state.ruleChanges, change];
      break;
    }
    case "context_usage": {
      const usage = normalizeContextUsage(payload);
      if (usage) {
        state.contextUsage = usage;
      }
      break;
    }
    case "usage_warning": {
      state.usageWarning = payload as unknown as UsageWarningDto;
      break;
    }
    case "interrupt": {
      const raw: Record<string, unknown> = isRecord(payload["interrupt"])
        ? payload["interrupt"]
        : payload;
      const interrupt = normalizeInterruptPayload({ ...frameAsRecord(frame), ...raw });
      const fromTool = interrupt.tool_call_id ? turnIdOfToolCallId(interrupt.tool_call_id) : null;
      const turnId = fromTool ?? frame.turn_id ?? null;
      if (turnId) {
        const turn = ensureTurn(state, turnId);
        applyInterruptOutcome(state, turn, [interrupt], frame.created_at);
      } else {
        const approvalId = interrupt.id;
        if (approvalId && !state.pendingApprovalIds.includes(approvalId)) {
          state.pendingApprovalIds.push(approvalId);
        }
      }
      break;
    }
    default: {
      break;
    }
  }
  return state;
}

function insertFrameBySeqMerge(frames: AnyMonitorFrame[], frame: AnyMonitorFrame): AnyMonitorFrame[] {
  let index = frames.length;
  for (let i = frames.length - 1; i >= 0; i -= 1) {
    const current = frames[i];
    if (current.seq < frame.seq || (current.seq === frame.seq && current.created_at <= frame.created_at)) {
      index = i + 1;
      break;
    }
    index = i;
  }
  const next = [...frames];
  next.splice(index, 0, frame);
  return next;
}

/// 複数 frame を一括適用 (backend `merge_frames` と同じ join 規則で)。
export function applyMonitorFrames(
  state: TranscriptState,
  frames: readonly (AnyMonitorFrame | null)[],
): TranscriptState {
  for (const frame of mergeFrames(
    frames.filter((frame): frame is AnyMonitorFrame => frame !== null && frame !== undefined),
  )) {
    applyMonitorFrame(state, frame);
  }
  return state;
}

/// backend `pending_turn_outcome` の複製: terminal frame のない turn の stop_reason 推定。
export function pendingTurnOutcome(turn: TranscriptTurn, frames?: readonly AnyMonitorFrame[]): TurnOutcome {
  const statuses = new Set<string>(
    (frames ?? [])
      .filter((frame) => frame.turn_id === turn.turnId)
      .map((frame) => String(frame.event_type)),
  );
  for (const tool of turn.toolCalls) {
    if (tool.status === "pending_approval") {
      statuses.add("permission_requested");
    }
  }
  if (turn.interrupt && turn.status === "awaiting_approval") {
    statuses.add("permission_requested");
  }
  if (turn.status === "error" || statuses.has("error")) {
    return { stopReason: "send_failed", source: "frame" };
  }
  if (turn.status === "cancelled" || statuses.has("run_cancelled")) {
    return { stopReason: "aborted", source: "frame" };
  }
  const toolSeen = statuses.has("tool_call_started") || statuses.has("tool_call_complete");
  if (statuses.has("permission_requested") && !toolSeen) {
    return { stopReason: "permission_required", source: "frame" };
  }
  if (statuses.has("run_queued")) {
    return { stopReason: null, source: "queued" };
  }
  if (statuses.has("agent_run_started") || turn.status !== "unknown") {
    return { stopReason: null, source: "frame" };
  }
  return { stopReason: null, source: "absent" };
}

/// terminal frame がある turn の outcome。
export function turnOutcome(turn: TranscriptTurn, frames?: readonly AnyMonitorFrame[]): TurnOutcome & { source: "terminal" | TurnOutcome["source"] } {
  if (turn.terminalSeq !== null || TURN_OUTCOME_FRAME_TYPES.has(turn.status === "finished" ? "run_finished" : turn.status === "cancelled" ? "run_cancelled" : turn.status === "error" ? "error" : "")) {
    return {
      stopReason: turn.stopReason ?? (turn.status === "cancelled" ? "aborted" : turn.status === "error" ? "send_failed" : "end_turn"),
      source: "terminal",
    };
  }
  return pendingTurnOutcome(turn, frames);
}
