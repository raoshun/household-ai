/**
 * Phase 0 UI 契約 (§1〜§6) の wire / DTO 型。
 *
 * 表記規則の取り扱い:
 * - 契約 §4 の例は snake_case (`{thread_id, pending_approval_count, ...}`) で書かれており、
 *   指示側は「backend の camelCase DTO に一致させる」要求になっている。両立できないため
 *   内部 DTO は camelCase、wire reader は snake_case / camelCase 双方を受容する
 *   (`src/lib/api.ts` の `pickString` / `pickNumber`)。実装記録も契約 §9 参照。
 * - monitor frame・payload (README §9.4 / §10.3) は snake_case 固定 (契約 §1・§2)。
 * - AG-UI 由来の id (`toolCallId` / `parentMessageId`) は `src/lib/agui` で §1 の join key に正規化する。
 */

export type ThreadId = string;
export type SessionId = string;
export type TurnId = string;
export type MessageId = string;
export type ToolCallId = string;
export type ApprovalId = string;
export type EventId = string;

/** 契約 §1: thread 単位で単調増加。 */
export type FrameSeq = number;

/** JSON Problem 相当 (契約 §4 error mapping / README §10.1)。 */
export interface ProblemDetail {
  errorType: string;
  code: string;
  message: string;
  recoverable: boolean;
  details: Record<string, unknown> | null;
}

export interface ConcurrencyLimitDetails {
  retryAfterSeconds: number | null;
  runningTurnIds: string[];
}

/** `GET /api/chat/sessions` の 1 行 (契約 §4)。 */
export interface ChatSessionDto {
  threadId: ThreadId;
  sessionId: SessionId | null;
  title: string;
  createdAt: string | null;
  closedAt: string | null;
  model: string | null;
  turnCount: number;
  pendingApprovalCount: number;
}

/** `POST /api/chat/sessions` → 201。 */
export interface ChatSessionCreateDto {
  threadId: ThreadId;
  sessionId: SessionId | null;
  title: string;
  createdAt: string | null;
}

/** `POST /api/chat/sessions/{thread_id}/fork` → 201。 */
export interface ChatSessionForkDto {
  forkedFromThreadId: ThreadId;
  thread: ChatSessionDto;
}

/** `POST /api/chat/sessions/{thread_id}/interrupt` → 200。 */
export interface TurnInterruptDto {
  turnId: TurnId | null;
  cancelled: boolean;
}

/** `POST /api/chat/sessions/{thread_id}/turns` → 202。 */
export interface TurnAcceptedDto {
  turnId: TurnId;
  started: boolean;
}

/** `POST /api/agui/sessions/{thread_id}/events` → 202。 */
export interface QueueAcceptedDto {
  queued: { turnId: TurnId; seq: number }[];
}

/** `GET /api/chat/sessions/{thread_id}/frames`。 */
export interface FramesPageDto {
  frames: unknown[];
  replayTruncated: boolean;
}

/**
 * 承認レコード (backend `PermissionRecord` そのもの: snake_case)。
 * `approval_id === Interrupt.id` だが、backend の broker は `prompt_id` を id に使うため
 * `approvalId` 解決は §1 の join 手順 (approvalIdAliases) で吸収する。
 */
export interface ApprovalRecord {
  approval_id: ApprovalId | null;
  prompt_id: ApprovalId | null;
  session_id: SessionId | null;
  tool_name: string;
  arguments: Record<string, unknown>;
  tool_call_id: ToolCallId | null;
  turn_id: TurnId | null;
  message_id: MessageId | null;
  parent_message_id: MessageId | null;
  status: PermissionStatus;
  decided_by: string | null;
  reason: string | null;
  permission_source: PermissionSource | null;
  feedback: string | null;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
  expires_at: string | null;
  timeout_seconds: number | null;
  sequence: number | null;
}

export type PermissionStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "expired"
  | "cancelled";

/** 契約 §5: `PermissionOutcome.source` / `metadata.permission_source`。 */
export type PermissionSource =
  | "pre-execution-allow-list"
  | "agent-reflect"
  | "rule-precedence"
  | "approved_interactively"
  | "auto_executed"
  | "policy-timeout"
  | "rejected_interactively";

/** 契約 §5: `PermissionRecord.decided_by`。 */
export type DecidedBy = "family" | "policy" | "agent" | "system" | "policy-timeout" | "session-closed";

/** `GET /api/history/judgments` / `POST /api/judgments`。 */
export interface JudgmentDto {
  judgmentId: string;
  inputText: string;
  category: string | null;
  method: string | null;
  status: string | null;
  rationale: string | null;
  createdAt: string | null;
  ruleVersionId: string | null;
  sourceTurnId: TurnId | null;
  sourceThreadId: ThreadId | null;
}

/** Rules CRUD (README §9.1 / 契約 §4)。 */
export interface RuleDto {
  ruleId: string;
  title: string | null;
  text: string;
  status: string | null;
  source: string | null;
  version: number | null;
  createdAt: string | null;
  updatedAt: string | null;
  approvedBy: string | null;
}

/** `GET /api/rules/conflicts`。 */
export interface RuleConflictDto {
  ruleId: string;
  otherRuleId: string;
  kind: string | null;
  message: string | null;
}

/** §2 `rule_changed` payload (`{rule_id, action, rule}`)。 */
export interface RuleChangedDto {
  ruleId: string;
  action: string;
  rule: RuleDto;
}

export interface TokenUsage {
  inputTokens?: number;
  outputTokens?: number;
  cachedInputTokens?: number;
  reasoningOutputTokens?: number;
  totalTokens?: number;
  /** backend / provider が返す生の key (snake_case 含む) をそのまま保持する。 */
  raw: Record<string, number>;
}

export interface CostAmount {
  value: number;
  currency: string;
  unit?: string | null;
}

/** 契約 §6 の context usage (monitor payload と REST 両方で同じ形)。 */
export interface ContextUsageDto {
  sessionId: SessionId | null;
  turnCount: number;
  costAvailable: boolean;
  usageUnavailableReason: "no_usage_reported" | "no_cost_reported" | null;
  totalCost: CostAmount | null;
  totalUsage: TokenUsage | null;
  totalCosts: ContextUsageCostRow[];
  turns: ContextUsageTurnRow[];
}

export interface ContextUsageCostRow {
  turnId: TurnId | null;
  cost: CostAmount | null;
}

export interface ContextUsageTurnRow {
  turnId: TurnId | null;
  seq: number | null;
  createdAt: string | null;
  stopReason: string | null;
  usage: TokenUsage | null;
  totalCost: CostAmount | null;
}

/** 契約 §6: `usage_warning` payload。 */
export interface UsageWarningPayload {
  threshold: number | null;
  total_cost: CostAmount | null;
  turn_id: TurnId | null;
}

/** `GET /api/health` (liveness) と `GET /api/monitor/health`。 */
export interface HealthDto {
  status: string;
  detail: string | null;
  checks: Record<string, string>;
  raw: Record<string, unknown>;
}

/** `GET /api/agui/capabilities` (`agentCapabilities()`)。 */
export interface AgentCapabilitiesDto {
  tools: { name: string; description: string | null; parameters: Record<string, unknown> }[];
}

/** turn 要約 (snapshot / frames 集計)。`turn_id` は §1 の `{thread_id}:t{n}`。 */
export interface TurnSummaryDto {
  turnId: TurnId;
  threadId: ThreadId | null;
  messageId: MessageId | null;
  parentMessageId: MessageId | null;
  text: string | null;
  stopReason: string | null;
  status: string | null;
  usage: TokenUsage | null;
  totalCost: CostAmount | null;
  createdAt: string | null;
  toolCallIds: ToolCallId[];
}

/** `GET /api/chat/sessions/{thread_id}` snapshot (README §9.6 実行監視の初期状態)。 */
export interface SessionSnapshotDto {
  thread: Record<string, unknown>;
  session: Record<string, unknown>;
  messages: unknown[];
  turns: TurnSummaryDto[];
  framesTail: unknown[];
  approvals: ApprovalRecord[];
  pendingApprovals: ApprovalRecord[];
  contextUsage: ContextUsageDto | null;
  health: HealthDto | null;
}

/** README §9.6: session_events 表の 1 行 (`session_event_recorded` payload)。 */
export interface SessionEventRowDto {
  eventId: EventId;
  seq: number;
  turnId: TurnId | null;
  threadId: ThreadId | null;
  sessionId: SessionId | null;
  parentMessageId: MessageId | null;
  eventType: string;
  toolCallId: ToolCallId | null;
  eventKind: string | null;
  payloadSummary: Record<string, unknown>;
  createdAt: string | null;
}

/** §1 の join key を frontend 側で束ねる key 集合 (README §15 の整合要求)。 */
export interface TranscriptJoinKeys {
  threadId: ThreadId;
  turnId: TurnId;
  messageId: MessageId | null;
  parentMessageId: MessageId | null;
  toolCallId?: ToolCallId;
  approvalId?: ApprovalId;
}

/** README §7 / 契約 §7 の config 表示用スナップショット。 */
export interface ConfigSummaryDto {
  entries: { name: string; envName: string; value: string; unit: string | null }[];
}
