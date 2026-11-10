# Capability matrix: Copilot SDK docs の試行 vs pinned 実装

Status: pinned `github-copilot-sdk==1.0.29` / runtime 1.0.107 で実測 (2026-11-10, Issue #1 Phase 0).
Names marked "docs claim" are from <https://github.com/github/copilot-sdk/tree/main/docs> API docs
(version **v1.2.0-beta**, pinned より新しい). 実在判定の source は `tests/proofs/sdk_api_probe.py`
の出力 (`docs/evidence/sdk_api_probe_pinned_1.0.29.json`) と live proof
(`docs/phase0-verification-report.md`). **実測で無かった名前は Phase 0 では使わない.**

## 0. Reproducibility の固定

| 項目 | 実測値 |
| :--- | :--- |
| host venv / image 内 SDK | `github-copilot-sdk` 1.0.29 (package 名 `copilot`) |
| image python | 3.12.12 (host venv も 3.12) |
| CLI runtime binary | 1.0.107 (lock 外. `download-runtime` で download cache から取得) |
| probe 出力 | host と Docker image で byte 単位一致 (`sdk_api_probe_pinned_1.0.29.json`) |

固定手順: `uv sync --frozen --extra dev --no-install-project` (host) / `uv sync --project /app/backend
--frozen --extra dev --no-install-project` (image). `--frozen` は lockfile の pin 条件を build 中に
書き換えないための必須 flag。host の uv cache/dir は repo 内 home に作らない (Dockerfile build 内でも
`UV_CACHE_DIR` を build 内 path へ向ける)。

## 1. `CopilotClient(...)` (docs §client)

client constructor kwargs として pinned 1.0.29 に実在 (probe で True 確認):
`entra`, `connection`, `working_directory`, `log_level`, `env`, `github_token`, `base_directory`,
`builtin_plugin_directories`, `extension_launch_provider`, `installation_confirmation_handler`,
`kerberos_auth`, `use_logged_in_user`, `telemetry`, `session_fs`, `request_handler`,
`session_idle_timeout_seconds`, `client_info`, `on_list_models`, `on_github_telemetry`, `mode`.

Session idle timeout は **client 側**にしかない (`session_idle_timeout_seconds` 1..3600s。
0 で infinite)。session 単位の timeout 引数は pinned では不存在 → 実装は client 引数 +
turn 完了待ちの `session.idle` 購読 (§13.3) で実装済み。

## 2. `CopilotClient.create_session(...)`

owner は `CopilotClient.create_session` (`CopilotSession` ではない)。pinned 1.0.29 で実在を
確認した Phase 0 利用 kwargs: `model`, `provider`, `working_directory`, `system_message`,
`available_tools` (`list[str] | ToolSet | None`), `excluded_tools`, `tools` (`list[Tool]`),
`on_permission_request`, `enable_session_store`, `infinite_sessions`, `session_limits`,
`large_output`, `hooks`, `skill_directories`, `disabled_skills`, `disabled_mcp_servers`,
`agent`, `context_tier`, `streaming`, `name`, `session_id`, `reasoning_effort`, `tools` 系 config。

docs claim のうち **pinned に存在しない** (使わない):

| docs claim | 1.0.29 実測 | 対応 |
| :--- | :--- | :--- |
| `base_tools: ToolSet` | 無し | `available_tools` + `excluded_tools` (ToolSet)。ToolSet は `add_builtin/add_custom/add_mcp` を持つが SDK 側で custom 追加の ack あり (§5) |
| `approval_mode` (session 引数) | 無し | Custom Tool の `skip_permission` で表現 (`provider.py` の `CustomToolDeclaration.approval_mode` → `skip_permission`) |
| `permission_handler` 引数 | 無し | `on_permission_request` 引数 |
| `tool_mode` / `features` / `settings` / `additional_tools` / `customInstructions` | 無し | 禁止方針は runtime 側 (`ToolPolicy`) で表現し、settings の値に依存しない (Phase 2) |
| `session_idle_timeout_seconds` (session 引数) | 無し | client 引数に固定 |
| `customToolsSettings` / `customToolExecutionMode` | 無し | Custom Tool は `tools=[Tool(...)]` で実行 (§5) |
| `session_store_dir` / `session_store_enabled` | 無し (pinned は `enable_session_store`) | runtime dir を client の `base_directory` へ。Store paths は §13.4 |

## 3. `CopilotSession` API

Public methods (1.0.29): `abort`, `capabilities`, `disconnect`, `get_events`, `log`, `on`,
`open_canvases`, `rpc`, `send`, `send_and_wait`, `send_and_wait_typed`, `set_auto_tier`,
`set_model`, `set_tools`, `ui`, `workspace_path`.

| docs claim | 実測 |
| :--- | :--- |
| `send(prompt)` | 実在。kwargs: `prompt, attachments, source, mode, agent_mode, request_headers, display_prompt, response_schema` |
| `send_and_wait(...)` | 実在 + **`timeout` 引数あり** |
| `on(event, handler)` | 実在。戻り値 `Callable[[], None]` (unsubscribe) → runtime は return 値を保持し close 時に呼ぶ |
| `Session.resume()` | 無し (pinned の resume は `CopilotClient.resume_session(session_id)`) |
| `session.close()` | 無し → `disconnect()` を使い、runtime 側 close で abort + disconnect + reference 削除 |

## 4. Permission policy / decision

Import paths (1.0.29): `copilot.PermissionHandler`, `copilot.PermissionRequest`,
`copilot.PermissionRequestResult`, and `copilot.session` 側に `PermissionDecision`,
`PermissionInvocation`, `PermissionHandler`, `PermissionRequest`, `PermissionRequestResult`。
`copilot.generated.rpc` には `PermissionDecision*` 系はあるが `PermissionApproved` /
`SessionApprovalMode` は **無い** (docs claim / spike 時は newer SDK 名前)。

実測 chain (Custom Tool 実行時): `permission.requested` → `permission.completed` →
`external_tool.requested` → `external_tool.completed` → `tool.execution_complete`, 併発
`permission.carriedForward`, `permission.messageAuthorization`, `session.permissions_changed`。

Phase 0 実装方針 (`provider.py` の `AgentSession._decide_permission`): registered Custom Tool
名に含まれるときだけ `PermissionDecisionApproveOnce()` を返し、それ以外 (built-in・未登録名) は
`PermissionDecisionReject(feedback=...)` で理由付き拒否する。承認時も拒否時と同じ audit log を
残す (Phase 1 の PermissionHandler port が要求する監査証跡)。`approval_mode="auto"` の
declaration は `skip_permission=True` で handler 自体を呼ばせない。

## 5. Custom Tools

- docs の `custom_tool.*` wire 名は pinned に**存在しない**。160件の wire 名に `custom_tool.*` は無く
  custom 系は `session.custom_agents_updated`, `session.custom_notification` のみ。
- 実行 chain は §4 の通り (`permission.*` + `external_tool.*` + `tool.execution_*`).
- `copilot.Tool` init kwargs: `name, description, handler, parameters, overrides_built_in_tool,
  skip_permission, defer, metadata, is_terminal, namespace` (docs の `Tool` 定義と一致).
- `copilot.tools.ToolHandler = Callable[[ToolInvocation], ToolResult | Awaitable[ToolResult]]`
  (docs claim の "ToolResult を返す" は 1.0.29 でも妥当。spike 時に見た `CallToolResult` は
  `copilot.tools.convert_mcp_call_tool_result` の戻り値で使われる型で、Custom Tool の 戻り値型ではない).
- `ToolInvocation` fields: `session_id`, `tool_call_id`, `tool_name` (docs claim の `arguments` は
  実測では別经路 → runtime 側で normalize 済み: `normalize_tool_arguments`).
- `ToolResult` fields: `text_result_for_llm`, `result_type`, `error`, `binary_results_for_llm`,
  `session_log`, `tool_telemetry`, `tool_references`.
- `ToolSet().add_custom(...)` は custom 実行に**必要** (probe: 追加しないと SDK が custom tool を
  resolve できず "Tool use rejected" になる). `ToolSet` の `add_builtin/add_custom/add_mcp` 実在.

## 6. Wire event 型 (session events)

`copilot.SessionEventType` enum: 160 members (probe 実測). 関連 wire 名の抜粋:

| 目的 | wire 名 (1.0.29 実測) |
| :--- | :--- |
| Chat turn 境界 | `session.start`, `session.info`, `user.message`, `assistant.turn_start`, `assistant.turn_end`, `assistant.message`, `assistant.idle`, `session.usage_info` |
| Tool 実行 | `tool.user_requested`, `tool.execution_start`, `tool.execution_complete`, `tool.execution_partial_result`, `tool.execution_progress`, `session.tools_updated`, `assistant.tool_call_delta` |
| Custom Tool | `permission.requested`, `permission.completed`, `external_tool.requested`, `external_tool.completed` |
| Skills | `session.skills_loaded`, `skill.invoked`, `skill.context_delivered`, `skill.invoked_ref` |
| MCP | `session.mcp_servers_loaded`, `session.mcp_server_*`, `mcp.tools.list_changed`, `mcp.oauth_*` |
| system / error / cancel | `system.message`, `system.notification`, `session.error`, `session.shutdown`, `session.shutdown_scheduled`, `session.request_user_input` |

Phase 0 で**未確認** (docs claim / spike 由来): `session.status`, `session.user_cancelled`,
`assistant.server_tool_progress` 系の一部、`custom_tool.*`. runtime 側 normalize (events.py) に
含まれない wire 名は proof log で UNKNOWN として観測済み → `docs/phase0-verification-report.md`.

## 7. Skills

- `create_session` の `enable_skills` (bool) / `skill_directories` / `disabled_skills` / `skill_provider`
  が pinned に実在 (docs claim うち `include_skills` / `disable_builtin_skills` は**無し**).
- `session.skills_loaded` event が pinned wire 名として実在。runtime は session start で購読し
  失敗時に skills_ready=false。
- skills 無効化 (`enable_skills=False`) では `session.skills_loaded` が届かず turn が走る
  (実測 1 例: 5 turn 完走) → runtime 側 fallback 実装済 (`docs/phase1-reproducibility-gate.md`).

## 8. Auth / credential

- pinned CLI runtime は `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` を host env として読む
  (`copilot/client.py` の env 名 list 実測)。client 引数 `github_token` でも inject 可。
  **`COPILOT_GITHUB_TOKEN_FILE` は存在しない** → file 指定はできない (compose では host env を渡す)。
- BYOK (provider env) の chat + Custom Tool 実行に GitHub login 不要と実測
  (host / Docker, token 有無 4 組み green: `docs/phase0-verification-report.md`)。
- `base_directory` (runtime dir) に credential state は永続化されない (token 無しで gate green
  → runtime dir に auth state を要求しない実装) 。
- `list_models` は BYOK 構成 (token 無し) だと runtime 側で "Not authenticated" になる. 実装は
  例外化して settings.model を返す (BYOK model 名は settings 側). Connect 系機能 (GitHub 接続,
  MCP OAuth, `Copilot-MCP`) は credential を要求するため Issue #4 で扱う.

## 9. CLI runtime 取得と保存 location

- subcommands: `download-runtime`, `version`, `-h` のみ. **`cli-path` は存在しない** (docs claim).
- pinned の実装は `copilot._cli_download` と `copilot._cli_version`. docs が前提する 2 段 layout の
  name (`_cli_cache_dir`, `_cli_base_dir`, `_runtime_dir`, `_sdk_version_dir`, `_cli_version_dir`,
  `_download_cli_archive`, `_extract_cli_package`, `_normalize_runtime_version` など) は
  **pinned module に存在しない** (probe 実測: `docs_gap.cli_runtime_download`).
- download 先: `https://github.com/github/copilot-cli/releases/download/v<cli_ver>/` 配下の
  `SHA256SUMS.txt` と `github-copilot-<cli_ver>-<runtime_platform>.tgz` (hostless 版. 1.0.107 の
  linux-x64 asset は 96,303,322 bytes = sha256 `a1e3acdb...af057`. SEA 版 `copilot-<platform>.tar.gz`
  は hostless asset が 404 の場合のみ fallback). `_UNSTABLE_VERSION` の fullmatch は
  `(?:-unstable|-(0|[1-9][0-9]*)\.unstable)\.r[1-9][0-9]*\.g[0-9a-f]{7}` (実測) で、一致時は
  repo `copilot-sdk` / tag `runtime-<ver>` を指す (stable は repo `copilot-cli` / tag `v<ver>`).
  `COPILOT_CLI_DOWNLOAD_BASE_URL` は tag より前の base として尊重される (`get_download_url`).
- checksum 検証: SHA-256 を `SHA256SUMS.txt` と照合し、entry は `<sha>  <file>` と `*<file>`
  (binary-mode prefix) の両方を受け付ける. 一致なし・entry 無し・HTTP 失敗は RuntimeError.
  download 失敗時は download 先を削除、extract 済み cache dir は保持 (再実行で verify 成功まで retry).
- cache dir 決定 (`get_cache_dir(version)`): `COPILOT_CLI_EXTRACT_DIR` が **version dir そのものを
  上書き**し、win32 では %LOCALAPPDATA 配下の github-copilot-sdk/cli/<ver>、linux/darwin では
  `XDG_CACHE_HOME` を優先、未 set のとき `~/.cache`. 実測: `XDG_CACHE_HOME` と `XDG_DATA_HOME` を
  同時に set すると前者が勝った (pinned に `XDG_DATA_HOME` fallback は無い).
- bundle layout (host で `COPILOT_CLI_EXTRACT_DIR=/tmp/mirror-cache` を使い mirror から download して実測):

  ```text
  <cache_root>/github-copilot-sdk/cli/<cli_version>/prebuilds/<runtime_platform>/
    copilot-runtime                # wrapper (executable)
    runtime.node                   # in-process runtime library
    .hostless-runtime-assets-v2    # hostless bundle marker
    builtin/  builtin-skills/  animations/  definitions/  plugins/  ripgrep/  schemas/  tgrep/
  ```

  `_HOSTLESS_EXCLUDED_TOP_LEVEL` に列挙された SEA 専用 entry (app.js、cli-main.js、copilot、
  copilot-sdk、index.js、package.json、preload、sea-loader.js、webview、tree-sitter wasm、
  voice-*、cli-native.node など) は extract 時に除外、`prebuilds/<platform>/` 配下の深い
  layout は 2 段上へ promote、危険 path (absolute / `..` / `:`) を reject する。
- `get_cached_cli_path()` は wrapper / runtime library / marker に加え compat 用 `copilot`
  entry point を要求するため、hostless 版だけの cache では **None を返す** (host 実測)。
  `ensure_runtime_wrapper()` と `ensure_runtime_library()` が不足分を cache へ materialize し、
  client は explicit `COPILOT_CLI_PATH` → env `COPILOT_CLI_PATH` → wrapper 解決の順で runtime を
  選ぶ (`_cli_path_source` は explicit / environment / downloaded)。in-process mode は
  `COPILOT_CLI_PATH` + `ensure_runtime_library()` の pair を使う。
- runtime platform 名 (`get_runtime_platform()`): linux-x86_64→linux-x64、linux-aarch64→linux-arm64、
  darwin-x86_64→darwin-x64、darwin-arm64→darwin-arm64、win32-AMD64→win32-x64、win32-ARM64→win32-arm64。
  musl host では machine 名 (x86_64 / aarch64 / arm64) から linuxmusl-x64 / linuxmusl-arm64 となり、
  release asset 名 `github-copilot-<ver>-linuxmusl-<arch>.tgz` と一致する (SHA256SUMS.txt の 8 asset と照合).
- pinned が尊重する env (`package_env_tokens` 実測): `COPILOT_CLI_PATH`、`COPILOT_CLI_EXTRACT_DIR`、
  `COPILOT_SKIP_CLI_DOWNLOAD` (ON/1/true/yes で download を short-circuit)、
  `COPILOT_CLI_DOWNLOAD_BASE_URL` (release base 先 override = mirror 運用の根拠)、
  `COPILOT_SDK_DEFAULT_CONNECTION`、`COPILOT_HOME`/`COPILOT_CACHE_HOME`/`COPILOT_DISABLE_KEYTAR`
  (base_directory 由-derived)。`~/.copilot-cli` は存在しない (docs claim)。
- offline 運用の実測 recipe (host で検証済み、165 MB の cache が checksum verify 完了):
  release 配布先と同じ layout の mirror を用意し (`<base>/v1.0.107/SHA256SUMS.txt` と
  `<base>/v1.0.107/github-copilot-1.0.107-linux-x64.tgz`)、`COPILOT_CLI_DOWNLOAD_BASE_URL=http://<mirror>` で
  `python -m copilot download-runtime` を実行 → wrapper path が stdout に出る。
  container へ bundle を preseed して download を offline で完了させることも実測 (次節)。

## 10. Session store API (pinned では不在)

docs は `CopilotClient(..., session_store=LocalSessionStore(dir=...), session_snapshot=...)`、
`LocalSessionStore` / `Json.getSessionStore()` 等を claim するが、pinned 1.0.29 では
`copilot` / `copilot.generated.rpc` / `copilot.session_store` (module 無し) / `copilot.json` の
いずれにも `LocalSessionStore`、`InMemorySessionStore`、`SessionSnapshotInput/Result`、
`SessionStore*`、`Snapshot*`、`ListSessions*` 型の definition は **存在しない** (probe 実測、
`session_store.symbols_present` はすべて False)。
pinned に在るのは session 操作 API だけ: `CopilotClient.resume_session` / `list_sessions` /
`delete_session` / `session_snapshot_dir`、`CopilotSession.resume` / `list_snapshots` /
`restore_snapshot` / `get_snapshot_state` / `save_session_entry`。`create_session` の kwargs に
session store 系は無く、session flag `enable_session_store` が存在する。
Phase 1 の conversation log 永続化 (F17/F18) は store abstraction を app 側で持ち、
store API 前提の docs recipe は pinned への移植時に作り直すこと。


## 11. Sandbox / managed settings (Phase 2)

pinned に `sandbox_config`, `managed_settings`, `enable_managed_settings`, `excluded_managed_settings`
, `enable_host_git_operations`, `trust_working_directory` 等の kwargs が`create_session` に実在.
ただし `PermissionHandler.approve_all(...)` は managed settings 有効時に RuntimeError.
禁止方針 (deny 側) は runtime 側 policy で表現し、settings の値に依存さない (Phase 2 design).
