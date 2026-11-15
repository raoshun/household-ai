# Phase 0 UI 実装契約 (2026-11-11 改訂)

外部 review (AG-UI / CopilotKit / monitor design) の指摘を受けて、backend bridge + frontend が
守る契約を 1 か所に固定する。README §9.6 / §10.1 / §10.3 / §11 / §12.4 が上位仕様で、この契約は
その **詳細化** である (contradict する場合は README を修正し、本 file へ対応を記録する)。

## 0. review 指摘への回答 (改訂の理由)

| # | 指摘 | 改訂方針 |
|---|---|---|
| 1 | HITL が AG-UI standard interrupt を使わない (Custom event + REST 依存) | `RUN_FINISHED` を `outcome: {type:"interrupt", interrupts:[{id: approval_id, reason:"awaiting_human_approval", tool_call_id, message, expires_at, response_schema, metadata}]}` で閉じる。frontend は CopilotKit `useInterrupt` の `resolve(payload)` で解決し、`POST /api/agui/run` (`RunAgentInput.resume=[{interrupt_id,status,payload}]`) か approval REST のどちらでも解決できる。monitor frame (`approval_requested` / `approval_resolved`) は §9.1 承認待ち list 用の並行経路として維持する |
| 2 | `RunAgentInput.messages` / `context` を ignore している | `context.turns[].turn_id` を suggested turn id として受理し、`messages` は history 補完に使う (backend の session history が正。client 側 history は `POST /api/agui/run` で渡しても session history と重複させない = 新規 human message 1 件だけ実行する)。`context` items は monitor `error_frame(error_type="context_item_unsupported", recoverable=true)` で可視化する |
| 3 | interrupt 語彙が README と食い違う (CUSTOM のみ / README は outcome 型を想定) | 上記 1 で protocol 準拠の outcome を主経路にする。CUSTOM `interrupt` event は diagnostic (monitor frame と同じ payload) として併送出し、README §10.3 の "monitor stream: CUSTOM(event.name="interrupt")" は **monitor stream 限定** と明示する |
| 4 | monitor frame 名が README と違う (`run_started` / `session_idle`) | README §10.3 準拠へ統一: `agent_run_started` (frame), `session_idle`, `pending_approvals_updated`。README に無い独自 type (`tool_denied_without_grant`, `approval_updated`, `run_queued` の payload 違い) は削除するか README へ追記する |
| 5 | turn_id 名前空間衝突 (global seq registry) | canonical turn_id は `ConversationLog.start_turn()` が決める `{conversation_id}:t{n}` (thread scoped で一意)。client が suggested turn_id を送る場合 `^[A-Za-z0-9_-]+:t[0-9]+$` かつ対象 thread scoping を要求し、不一致は HTTP 422 `unknown_error.turn_id_conflict`。`tool_call_id` は `{turn_id}:tool:{n}`、AG-UI `message_id` は `{turn_id}:message` |
| 6 | context usage が turn 単位のみ / usage 取得不能時に UI が識別不能 | `context-usage` HTTP + monitor `context_usage` frame は `{session_id, turn_count, total_cost:{value,currency}, total_usage:{...}, total_costs:[…], turns:[…], cost_available: bool, usage_unavailable_reason: str|null}` を返す。usage 未取得なら `total_cost` は `null` + `cost_available=false` + `usage_unavailable_reason` (README §9.4 の usage 追記要件に対応) |
| 7 | `max_frames_exceeded` が 1 回限り、以降は monitor 購読で continuation できない | AG-UI run stream 上限到達時は `RUN_ERROR(code="max_frames_exceeded", recoverable=true)` で閉じ、client は `GET /api/agui/sessions/{thread_id}/events?after_seq=` か `POST /api/agui/run` (`resume`) で continuation できる。monitor SSE は `MaxFramesExceeded` error frame + `heartbeat` で継続する (replay window は `monitor_replay_limit`) |
| 8 | heartbeat payload が session_id を运ばない / session 断絶で approval が宙に浮く | heartbeat payload は README §10.3 準拠 `{status:"active", session_id, conversation_id, active_turn_id, pending_approval_ids, pending_permission_ids, timestamp}`。session 切断 (runtime `session.error` / close) 時は pending approval を `mark_stale()` + `resolve_many(approved=false, decided_by="session-closed")` し `error_frame(error_type="session_closed"|"session_error")` + `approval_resolved` を送出する |
| 9 | `session_event_recorded` payload が README §11 table と違う | payload は §11 の列準拠: `{event_id, parent_message_id, session_id, event_type, tool_call_id, payload_summary}` (turn_id は frame 直下)。runtime `SessionEvent` を §11 行として mirror する |
| 10 | `session_reaped` payload 不整合 | 共通形 `{conversation_id, session_id, reason:"idle_timeout"|"stale_session", …}` に統一。README §10.3 には `session_reaped{conversation_id, session_id, reason:"idle_timeout"}` として追記する |
| 11 | CORS 未設定 | `Settings.api_cors_origins` を追加し `create_app(cors_origins=settings.api_cors_origins)` で `CORSMiddleware` を有効化する (default は `http://127.0.0.1:3000`,`http://localhost:3000`)。SSE は同源 rewrite 経由でも動くが、compose では frontend→backend が別 origin になるため必須 |
| 12 | config fields / default mismatch (validation min, env 未結線) | §0 の config 表に Env var を明記し、`Field(ge=…)` で min を enforced する (`monitor_replay_limit>=1`, `max_frames_per_thread>=1`, `session_idle_timeout_seconds>=1 or 0=disabled`, `permission_timeout_seconds>=10`) |
| 13 | README §9.6 session_events 表示 / UI 部材が README 側に足りない | README §9.6 へ「session_events 行 (event_id / parent_message_id / event_type / tool_call_id / payload 要約) を表示」を追記し、§9.1 sidebar に `経過監視` pane を明記する (README 改訂は §1 对应表へ記録) |

## 1. join keys (§11 および frontend reducer の正)

- `thread_id == conversation_id == ChatSession.id` (:session / :audit / :tool / :approval join)
- `turn_id = "{thread_id}:t{n}"` (canonical, ConversationLog 生成)
- `message_id = "{turn_id}:message"`, `parent_message_id = message_id`
- `tool_call_id = "{turn_id}:tool:{n}"` — frame, AG-UI `TOOL_CALL_*`, §11 行で共有
- `approval_id` (= `PermissionBroker` の approval_id) — AG-UI `Interrupt.id` にも使う
- frame 必須 keys: `{event_id, seq, event_type, created_at, turn_id, thread_id, session_id, payload}`
  (`payload` は常に nest。`seq` は thread 内で単調 increasing)

## 2. monitor / run frame 語彙 (README §10.3)

`event_type` は `app/services/session_events.py::MonitorEventType` を単一の語彙とし、UI は次を見る:

- run 系: `agent_run_started` `{input_preview}`, `run_queued` `{queued_at, position}`,
  `run_cancelled` `{reason}`, `run_finished` `{text_preview, stop_reason, usage, total_cost, tool_call_ids}`
- message 系: `message_complete` `{message_id, parent_message_id, text_preview}`,
  `assistant_text_delta` `{delta, parent_message_id}`, `thinking_delta` `{delta}`
- tool 系: `tool_call_started` `{tool_name, tool_call_id, arguments_preview, parent_message_id}`,
  `tool_call_args` `{tool_call_id, args_delta}`, `tool_call_complete` `{tool_call_id, tool_name, status, result_preview}`
- HITL: `permission_requested` `{approval_ids, tool_call_id, tool_name}`,
  `approval_requested` `{approval: PermissionRecord}`, `approval_resolved` `{approval: PermissionRecord}`,
  `pending_approvals_updated` `{pending_approval_ids}`
- error / 監視: `error` `{error_type, code, message, source, recoverable}`, `heartbeat`,
  `session_idle` `{session_id, idle_seconds}`, `session_reaped` `{conversation_id, session_id, reason}`,
  `session_closed` `{conversation_id, session_id, reason}`, `judgment_created` `{judgment}`, `rule_changed` `{rule_id, action, rule}`,
  `context_usage` / `usage_warning` (payload は §6 形), `session_event_recorded` (§11 mirror)
- monitor 側の interrupt 提示: CUSTOM event `interrupt` `{interrupt: {…}}` (AG-UI の interrupt 相当。AG-UI run stream では `RUN_FINISHED.outcome` が正)

`run_started` という name は使わない (README 既存名 `agent_run_started` に合わせる)。
`tool_denied_without_grant` / `approval_updated` は廃止 (§6.3 の grant denial は `error` frame + `tool_call_complete(status="denied")` で表現)。
frontend `MonitorEventType` union も同じ集合に更新する。

## 3. AG-UI run stream 契約

- `POST /api/agui/run` body = `RunAgentInput`。response = AG-UI SSE (`text/event-stream`, `event: …` + `data: event.type`)
- 系列: `RUN_STARTED` → (`TEXT_MESSAGE_START/CONTENT/END`, `TOOL_CALL_START/ARGS/END`, `TOOL_CALL_RESULT`) → 終了の 1 通り:
  1. 正常: `RUN_FINISHED outcome={type:"success", result:{turn_id, message_id, stop_reason, tool_call_ids, usage, total_cost}}`
  2. 中断 (実行中に human 承認待ち): `RUN_FINISHED outcome={type:"interrupt", interrupts:[Interrupt{id=approval_id, reason:"awaiting_human_approval", tool_call_id, message, expires_at, response_schema:{type:"object",properties:{approved:{type:"boolean"},feedback:{type:["string","null"]}}}, metadata:{thread_id, session_id, tool_name, expires_at}}]}`
  3. 失敗: `RUN_ERROR {message, code}` (code: `max_frames_exceeded` / `permission_stale` / `provider_session_error` / `turn_timeout`)
- resume: `RunAgentInput.resume=[{interrupt_id, status:"resolved"|"cancelled", payload:{approved, feedback}}]`
  → service は broker を解決し、残 turn frames を同じ SSE で続けて stream する (interrupt 後に SSE を閉じても frames は thread に蓄積され、`GET …/events?after_seq` で取得可能)
- interrupt 待ちで SSE を閉じた後も backend turn pump は走り続ける (client 切断 ≠ turn cancel)。
  明示的な中断は `POST /api/chat/sessions/{thread_id}/interrupt {reason}`。
- AG-UI `TOOL_CALL_START.parentMessageId` = `message_id`。tool は interrupt 解決後に完走し `TOOL_CALL_RESULT` は interrupt 後の resume stream に現れる。

## 4. HTTP 面 (README §10.1 / §10.2 / §10.3 / §9.4)

base prefix: `/api` (frontend は `/api/...` を `BACKEND_ORIGIN` へ rewrite)。

**Chat / session**
- `POST /api/chat/sessions` `{title?, model?}` → 201 `{thread_id, session_id, title, created_at}`
- `GET /api/chat/sessions` → 200 `{sessions:[{thread_id,title,created_at,closed_at,model,turn_count,pending_approval_count}]}`
- `GET /api/chat/sessions/{thread_id}` → snapshot `{thread, session, messages, turns, frames_tail, approvals, context_usage, pending_approvals, health}`
- `DELETE /api/chat/sessions/{thread_id}` → 200 closed thread (`mark_stale` + cancel active turn)
- `POST /api/chat/sessions/{thread_id}/fork` `{title?}` → 201 `{forked_from_thread_id, thread}`
- `PATCH /api/chat/sessions/{thread_id}` `{title}` → 200 thread
- `POST /api/chat/sessions/{thread_id}/interrupt` `{reason?}` → 200 `{turn_id, cancelled:true}` (404 `unknown_error.no_active_turn`)
- `GET /api/chat/sessions/{thread_id}/frames?after_seq=` → 200 `{frames:[…], replay_truncated:bool}`
- `GET /api/chat/sessions/{thread_id}/context-usage` §6 形 → 200
- `POST /api/chat/sessions/{thread_id}/turns` `{text, turn_id?}` → 202 `{turn_id, started:true}` (409 busy / 422 turn_id_conflict / 429 concurrency)

**AG-UI bridge**
- `POST /api/agui/run` (AG-UI SSE)。`GET /api/agui/run?thread_id=&after_seq=` は同一 SSE を GET でも購読できる (interrupt 解決後の continuation 用)
- `GET /api/agui/capabilities` → `{tools:[{name, description, parameters}]}` (`agentCapabilities()`)
- `GET /api/agui/sessions/{thread_id}/events?after_seq=&limit=` → monitor SSE (§10.3: heartbeat + frames + CUSTOM interrupt + session_event_recorded)
- `POST /api/agui/sessions/{thread_id}/events` → 202 `{queued:[{turn_id, seq}]}` (body `{turns:[{text, turn_id?}]}`)
- `POST /api/agui/sessions/{thread_id}/interrupt` / `PATCH …/events/session` `{title}` / `POST …/events/fork`

**Rules / approvals / judgments / health (§10.2 / §10.3 / §9.4)**
- `GET|POST /api/rules`, `GET|PATCH|DELETE /api/rules/{rule_id}`, `POST /api/rules/{rule_id}/approve|reject`,
  `GET /api/rules/{rule_id}/versions`, `GET /api/rules/conflicts`
- `GET /api/approvals?status=`, `GET /api/approvals/{approval_id}`,
  `POST /api/approvals/{approval_id}/approve {feedback?}`, `POST /api/approvals/{approval_id}/reject {feedback?}`,
  `POST /api/approvals/resolve-pending {approved}`
- `GET /api/history/judgments`, `GET /api/monitor/health` (per-session) / `GET /api/health` (liveness)
- `POST /api/judgments` (family 判断 record, 201)

**Error mapping** (README §10.1): JSON Problem 相当 `{error_type, code, message, recoverable, details}`
- 400 `unknown_error.invalid_request` (pydantic / `RuleValidationError` は `validation_error.invalid_text`)
- 404 `unknown_error.thread_not_found` / `.approval_not_found` / `.rule_not_found`
- 409 `unknown_error.thread_closed` / `.turn_in_progress`
- 422 `validation_error.rule_text` (RuleValidationError), `unknown_error.turn_id_conflict`
- 429 `unknown_error.concurrency_limit` `{retry_after_seconds, running_turn_ids}`
- 502 `agent_error.provider_session_error` / `.runtime_start_failed`
- 503 `agent_error.permission_stale`
- CORS preflight を frontend origin に対して許可。

## 5. HITL grant (README §12.4 / §6.3) — 変更しない

- write tools は `execute(needs_approval)` + §6.1 pre-confirm で SDK permission phase を経由し、
  handler は `PermissionBroker` 経由で human decision を待つ (§12.4 human approval)。
- `has_matching_grant` / `consume_grant`で pre-approval (§6.3) は維持。`PermissionOutcome.source` は
  `pre-execution-allow-list` / `agent-reflect` / `rule-precedence` / `approved_interactively` / `auto_executed` /
  `policy-timeout` / `rejected_interactively` を UI に表示 (`metadata.permission_source`)。
- approval 解決は `family` (UI 操作) / `policy` (session closed・timeout・stale) / `agent` (reflect) /
  `system` を区別する (`PermissionRecord.decided_by`)。

## 6. context usage (README §9.4 / §9.6)

`GET …/context-usage` と `context_usage` frame の payload:

```
{
  "session_id": "…",
  "turn_count": 12,
  "cost_available": true,
  "usage_unavailable_reason": null,
  "total_cost": {"value": 12.34, "currency": "JPY"},
  "total_usage": {…sum of numeric usage fields…},
  "total_costs": [ {turn_id, cost…} ],
  "turns": [ {turn_id, seq, created_at, usage…} ]
}
```

- `usage_unavailable_reason`: `"no_usage_reported"` (1 turn も usage なし) / `"no_cost_reported"` (usage あり cost なし) / null。
- warning 閾値超過なら `usage_warning` frame `{threshold, total_cost, turn_id}` (§9.6)。

## 7. config (Env var 結線 + validation)

| Settings field | Env | default | validation |
|---|---|---:|---|
| `api_cors_origins` | `API_CORS_ORIGINS` (comma list) | `http://localhost:3000,http://127.0.0.1:3000` | list[str] |
| `permission_timeout_seconds` | `PERMISSION_TIMEOUT_SECONDS` | 120 | `10..3600` |
| `monitor_replay_limit` | `MONITOR_REPLAY_LIMIT` | 200 | `>=1` |
| `max_frames_per_thread` | `MAX_FRAMES_PER_THREAD` | 400 | `>=1` |
| `monitor_heartbeat_seconds` | `MONITOR_HEARTBEAT_SECONDS` | 10 | `>=1` |
| `session_idle_timeout_seconds` | `SESSION_IDLE_TIMEOUT_SECONDS` | 900 | `10..3600` (既存) |
| `turn_timeout_seconds` | `AGENT_TURN_TIMEOUT_SECONDS` (既存 name) | 180 | `10..3600` (既存) |
| `max_concurrent_turns` | `MAX_CONCURRENT_TURNS` | 4 | `>=1` |
| `context_token_warning_threshold` | `CONTEXT_TOKEN_WARNING_THRESHOLD` | 0.8 (ratio) | `(0, 1]` |
| `context_warning_cost_jpy` | `CONTEXT_WARNING_COST_JPY` | 20.0 | `>=0` |
| `agui_emit_reasoning` | `AGUI_EMIT_REASONING` (truthy) | on | bool |
| `demo_rule_tools` | `DEMO_RULE_TOOLS` (truthy) | on | bool |

`load_settings()` は上の env name すべてを実際に読むこと (現状 `api_cors_origins` などは
default 固定で env 結線が無い。UI では env 結線が必須)。compose の `api` サービスは
`PERMISSION_TIMEOUT_SECONDS=120`, `CONTEXT_TOKEN_WARNING_THRESHOLD=0.8` を明示する。

## 8. frontend 契約 (README §9.1 / §9.6 / §14)

- `/` : 会話一覧 + 作成 / fork / delete、`/c/[thread_id]`: AG-UI chat (CopilotKit v2 `useAgent` + chat surface)、
  `/c/[thread_id]/monitor`: 実行監視 (timeline + tool calls + approvals + judgments + context usage)、`/settings`。
- 部材 (§9.6): 進行中 turn (run_started / run_queued / heartbeat / run_cancelled)、tool calls (start/args/complete +
  permission_requested + permission_source)、approval buttons (承認/拒否 + feedback)、session_events 行
  (event_id / parent_message_id / event_type / tool_call_id / payload 要約)、判断ログ、rule_changed badge、
  context usage (cost + warning)、error frames。
- `threadId` は backend thread_id と同一 (client 側 uuid を作らない)。
- 再接続: EventSource は 15 s headers timeout 相当で再試行 (backoff, `Last-Event-ID` 送信、`after_seq` fallback)。
- CopilotKit interrupt: `useInterrupt({render})` で `resolve({approved:true|false, feedback})` → resume POST。
- vitest (§14): reducer / reducer-interrupt / status / api client に unit test、`test/e2e-ui-bridge.test.ts` は
  起動済み backend へ component-level fetch (real HTTP) で tool call → interrupt → resume → monitor 系列を確認。
