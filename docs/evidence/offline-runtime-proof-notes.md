# Phase 0 §15 再現性と offline proof の記録

実行環境: Fedora 42 (Linux 6.x, x86_64), Docker, ローカル Ollama (`http://host.docker.internal:11434`)。
pinned manifest: `docs/pinned-runtime-versions.json` (sdk 1.0.29 / CLI 1.0.107 / runtime_platform linux-x64)。

## 1. pinned manifest 照合 (build gate)

`make build` / `make build-offline` のビルド内で `tests/proofs/verify_pinned_manifest.py` が走り、
実測値を stdout へ出力する:

```text
[pinned-check] cli_version=1.0.107
```

コンテナ内の実測 (import 不能时):

```text
module=copilot._cli_download
get_runtime_platform() -> linux-x64
get_cli_version()      -> 1.0.107
get_cached_cli_path()  -> /tmp/xdg-cache/github-copilot-sdk/cli/1.0.107/prebuilds/linux-x64/copilot-runtime
```

## 2. mirror を使った offline build

```bash
uv run --project backend python tests/proofs/fetch_runtime_mirror.py --out .local/mirror
# SHA256SumS.txt / SHA256SUMS.txt github-copilot-1.0.107-linux-x64.tgz (96,303,322 bytes)
python -m http.server 8099 --directory .local/mirror --bind 0.0.0.0
make MIRROR_PORT=8099 build-offline
```

build log 実測 (mirror を実際に引いた証跡):

```text
Download URL: http://172.17.0.1:8099/v1.0.107/github-copilot-1.0.107-linux-x64.tgz
Runtime cached at: /tmp/xdg-cache/github-copilot-sdk/cli/1.0.107/prebuilds/linux-x64/copilot-runtime
```

`--build-arg COPILOT_CLI_DOWNLOAD_BASE_URL` を set しないと GitHub origin 由来の URL になることも確認した。

### negative gate

| 条件 | 実測結果 |
| :--- | :--- |
| manifest の `cli_version` を 9.9.9 に改変 | `AssertionError: CLI 版 mismatch: 実測 1.0.29!=manifest 9.9.9` で build 失敗 |
| mirror 以外 (`COPILOT_CLI_DOWNLOAD_BASE_URL=http://127.0.0.1:1`) | `Failed to download checksums from http://127.0.0.1:1/v1.0.107/SHA256SUMS.txt` で失敗 |
| staging script の manifest digest と release SHA256SUMS.txt が不一致 | `AssertionError: manifest digest mismatch` (exit 1) |

## 3. network 遮断下の runtime proof

```bash
make MIRROR_PORT=8099 runtime-mirror   # mirror から runtime を bake
make prove-offline-runtime MIRROR_PORT=8099 (network none proof)   # --network none で proof 実行
```

コンテナ内で `172.17.0.1:8099` への TCP が `Network is unreachable` であることを proof 側でも確認した。
記録は [offline-runtime-proof.json](./offline-runtime-proof.json):

- `runtime_started: true` / `health_ready: true` — bake 済み hostless runtime が network 遮断で起動
- `tool_registered: true`, `snapshot_tool_names: ["ofc_home_route_probe"]` — Custom Tool が登録される
- `turn_aborted: true`, `turn_tool_calls: []` — model 到達不能で turn は失敗し tool 実行まで行かない
- `list_models_count: 0` — BYOK では `models.list` が成功しない (§15.4 の caveat)
- `session_closed: true`, `runtime_stopped: true` — 終了処理が正常

## 4. Custom Tool 実行 (LAN の model を使った live proof)

`make proof` (case 選択は `HOUSEHOLD_TOOL_POLICY=registered|disabled`) の実行は
`tests/proofs/phase0_live_proof.py`. 実行には model が必要で、LAN の Ollama
(`OLLAMA_BASE_URL=http://host.docker.internal:11434/v1`) を使う。生ログはリダイレクト先
(例 `.local/proof-<policy>.log`) に置き git にコミットしない (session id / provider log を含む)。

実測 (pinned SDK 1.0.29 / CLI 1.0.107 / model `gemma4:e2b`):

| case | turn 1 | turn 2 (内蔵 tool 試み) |
| :--- | :--- | :--- |
| `policy=registered` (既定 ToolPolicy, `mcp: "disabled"`) | Custom Tool `household_list_housework` が実行された (`tool.execution_start` / `tool.execution_complete` を各 1 件、`session.store` に tool_call 1 件) | 内蔵 shell / write tool の `tool.execution_start` は 0 件 (`built_in_shell_calls=[]`) |
| `policy=disabled` (`available_tools=ToolSet().add_custom("*")`) | 同じ tool が実行された (tool_call 1 件、shell 系 0 件) | 同上 (shell 系 0 件) |

turn events / raw events: registered case turn1 events=436 raw=908、turn2 events=187 raw=1300。
`wire_types_unmapped_count=` は turn ごとに 16 件 / 24 件で、内訳は `model.*` 系 9 名
(`model.log`, `model.message`, `model.messages_snapshot`, `model.model_call_started`,
`model.model_call_success`, `model.response`, `model.tool_execution`, `model.turn_ended`,
`model.turn_started`) と `session.status` 1 名 (turn 2 で重複カウントされるため件数は turn ごとに 2 倍)。
これらは pinned `SessionEventType` に dot-style enum value が無く、CLI の `SessionEvent.raw_type`
でしか観測できない (runtime は `AgentEvent.raw["sdk_raw_type"]` に保持し `type` は `unknown`)。

permission について: runtime は `permission.skip_permission=None` (既定) で SDK の permission 連鎖
(`permission.requested` → `permission.completed`) を通し、custom tool call に対して
`CustomToolPermissionHandler` が approve する。live proof では custom tool 実行の連鎖のみ観測され、
built-in tool への deny 応答は built-in tool 自体が起動しなかったため**未取得** (unit test で検証)。

## 5. §15.4 版一覧との対応

[capability-matrix.md](../capability-matrix.md) §6 の pinned 表と本記録の pinned 版は一致 (sdk 1.0.29 / CLI 1.0.107 / linux-x64)。
