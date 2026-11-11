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

`make proof` (= `docker compose --profile checks run --rm proof`) の実行は
`tests/proofs/phase0_live_proof.py`. 実行には model が必要で、LAN の Ollama
(`OLLAMA_BASE_URL=http://host.docker.internal:11434/v1`) を使う。生ログはリダイレクト先
(例 `.local/proof-<policy>.log`) に置き git にコミットしない (session id / provider log を含む)。

env `HOUSEHOLD_TOOL_POLICY` は tool の登録可否を切り替えない: proof script の `tools=` 指定は
`build_declaration()` 1 か所だけで、`HOUSEHOLD_TOOL_POLICY=disabled` を渡して再走した log でも
Custom Tool は登録され turn 1 で実行された (`tool_calls=['household_list_housework']`、
`RESULT [disabled] ok=True`)。この env 変数は log に出す tag の label だけを変える (2026-11-11 に
script と実行 log 両側で確認)。旧 §4 の表がうたっていた「Custom Tool 宣言なし (内蔵 tool のみ)」
case はこの env では再現できない。登録の有無を実際に切り替えるのは別の env
`HOUSEHOLD_PROOF_TOOLS` (`custom` 既定 / `none`) で、`make proof PROOF_TOOLS=none` が
`tools=[]` (Custom Tool 0 件) で proof を走らせる。registered 名 policy と wildcard switch の挙動は
unit test (`tests/unit/test_provider.py` の `test_open_registered_policy_は_wildcardを含まない` /
`test_open_tools無しは_wildcardへfallback` / `test_open_wildcard_switch`) 側で検証する。

env 名が実際に効く場所は proof script ではなく runtime 側である: `provider.py` の
`_policy_wildcard_enabled()` は `HOUSEHOLD_TOOL_POLICY` を読み、値が `wildcard` のときだけ
allow-list を `custom:*` へ戻す (既定は registered 名による deny-by-default)。認められている値は
`registered` (既定) と `wildcard` だけで `registered|disabled|both` という switch は存在しない
(unit test `tests/unit/test_provider.py` の `test_open_tools無しは_wildcardへfallback` と
`test_open_wildcard_switch` がこの 2 経路だけを確かめている)。

### HEAD での実測 (tool 登録あり / なしの 2 case)

HEAD `68ac63a` を `make build` した image (pinned SDK 1.0.29 / CLI 1.0.107) で model
`gemma4:e2b` を使い、`make proof` (tool 登録あり) と `make proof PROOF_TOOLS=none` (登録なし) を
1 回ずつ実行した。生ログは `.local/proof-case-registered2.log` /
`.local/proof-case-unregistered2.log` (git 外)。両 case とも `HOUSEHOLD_TOOL_POLICY=registered` の
まま (policy label は変えず tool 登録の有無だけ切り替えた)。

| 工程 | `PROOF_TOOLS=custom` (tag `registered`) | `PROOF_TOOLS=none` (tag `unregistered`) |
| :--- | :--- | :--- |
| 終了 | exit 0 / `RESULT [registered] ok=True` | exit 0 / `RESULT [unregistered] ok=True` |
| 登録 Custom Tool | 1 件 (`household_list_housework`) | 0 件 |
| 内蔵 tool disabled list | 18 件 (下記) | 同じ 18 件 |
| Skills | `household-advisor` / `rule-management` (custom・enabled、container 内 `/app/skills/*/SKILL.md`) | 同じ 2 件 |
| runtime の tool 名 allow-list | `custom:household_list_housework` + `builtin:skill` | `custom:*` + `builtin:skill` (登録 0 件のため wildcard fallback) |
| turn 1 (家事一覧 tool) | `aborted=True`、`tool_calls=['household_list_housework']`。`TOOL_EXECUTION_START` / `TOOL_EXECUTION_COMPLETE` 各 1 件 (`success=True`、`tool_call_id=call_3ako7axg`)、`EXTERNAL_TOOL_REQUESTED` / `EXTERNAL_TOOL_COMPLETED` 各 1 件、tool result は 3 項目 (`食器洗い: 週 3 回` / `ゴミ出し: 毎週月曜` / `浴室掃除: 週 1 回`) | `aborted=False`、`tool_calls=['household_list_housework']` (model は同じ name を呼んだ) が `TOOL_EXECUTION_COMPLETE success=False`、`error.code=failure`、message `Tool 'household_list_housework' does not exist.`、content `... does not exist. Available tools that can be called are skill.` |
| turn 2 (内蔵 shell 試み) | `aborted=False`、`tool_calls=[]`、`built-in_shell_calls=[]` | `aborted=False`、`tool_calls=[]`、`built-in_shell_calls=[]`。reply text は model が自分で書き出した `uname -a` 風の出力 (実行結果なし) |
| permission | `PERMISSION_REQUESTED` (kind `custom_tool`) → `PERMISSION_COMPLETED` 1 組 (宣言が `approval_mode="ask"`) | `permission_events=` (0 件)。execution 前に permission 要求は立たない |
| handler 呼び出し | `handler_calls=[{tool: household_list_housework, arguments: {}}]` (1 件) | `handler_calls=[]` (0 件 = tool 関数は一度も実行されない) |
| execution 到達 | `executed_tools=['household_list_housework']`、`custom_completed=['household_list_housework']` | `custom_rejected=['household_list_housework']`、`custom_completed=[]` |
| `denied_like_events` | 0 | 0 |
| 生 event 数 | turn 1 = 700 / turn 2 = 150、unmapped 14 件 / 22 件 | turn 1 = 157 / turn 2 = 152、unmapped 16 件 / 24 件 |

**この 2 case で確定した挙動**: Custom Tool を `tools=` へ登録しない session では、model がその name
を呼んでも runtime 側の handler へは届かず (handler 0 件)、SDK が execution を失敗として返す。
Phase 0 の tool policy の実際の境界は「登録された name だけ実行できる」であり、permission の deny
ではなく tool 名の解決失敗として観測される (`denied_like_events=0`)。登録あり側では
`approval_mode="ask"` により permission 連鎖 (request → completed) が 1 組立ち、runtime の handler が
approve して tool が実行された。

内蔵 tool の disabled list 18 件 (両 run で同一): `bash`、`create`、`dynamic_workflows_manage`、
`edit`、`glob`、`grep`、`list_agents`、`list_bash`、`read_agent`、`read_bash`、
`run_dynamic_workflow`、`semantic_code_search`、`sql`、`stop_bash`、`task`、`view`、`web_fetch`、
`write_agent`。`str_replace_editor` / `str_replace_edits` / `skill` はこの list に**含まれない**
(旧 §4 の 20 件という記述は `10-manual-api-surface-audit.md` の CLI `tool_call_names` dump を
実行時の allow-list と取り違えた誤り)。`web_search` と `skill` は list の外だが実行は未観測。

wire 名: mapped は turn ごとに 32 名前後。unmapped は `model.log`、`model.message`、
`model.messages_snapshot`、`model.model_call_started`、`model.model_call_success`、`model.response`、
`model.telemetry`、`model.tool_execution`、`model.turn_ended`、`model.turn_failed`、
`model.turn_started`。これらは pinned `SessionEventType` に dot-style enum value が無く、CLI の
`SessionEvent.raw_type` でしか観測できない (runtime は `AgentEvent.raw["sdk_raw_type"]` に保持し
`type` は `unknown`)。`session.status` はいずれの log にも現れなかった。

registered case の turn 1 は tool 実行が `success=True` で成功した後に `aborted=True` になった
(`SessionEventType.ABORT` 1 件 + `AGENT_INTERRUPTED` 1 件、wire 名 `model.turn_failed` 1 件)。
打ち切りの原因は log から特定できていない (tool call の直後に model 側で失敗しているだけで、
runtime が SDK へ abort を送った経路ではない)。turn の打ち切りと tool 実行成功の切り分けは
Phase 1 で追う。

permission: 宣言は `approval_mode="ask"` なので provider は `Tool(skip_permission=False)` を渡す
(`backend/app/agent/provider.py` の Tool 構築)。custom tool call で SDK の permission 連鎖
(`permission.requested` → `permission.completed`) が 1 組観測され、runtime 側の handler が approve した。
built-in tool への deny 応答は built-in tool が 1 件も起動しなかったため**未取得** (turn 2 の shell
試みは tool 実行 0 件のまま model が実行結果らしい文章を出力している)。non-custom tool への deny
応答は unit test (`tests/unit/test_provider.py` の `test_permission_denies_non_custom_tool`) で検証する。

---

## 5. §15.4 版一覧との対応

[capability-matrix.md](../capability-matrix.md) §6 の pinned 表と本記録の pinned 版は一致 (sdk 1.0.29 / CLI 1.0.107 / linux-x64)。

## 6. compose claims gate (展開構成の検査)

`docker-compose.yml` が書いた通りに展開されているかを、実展開値から検査する track。
実行は `make gate-compose-claims` (stage → claims → tamper)。

| target | 何を走らせるか | evidence |
| :--- | :--- | :--- |
| `compose-claims-stage` | host の `docker compose --profile checks --env-file /dev/null config --format json` で展開 JSON を `.local/compose-claims/` へ staging し、検査用の compose / Dockerfile / .dockerignore copy を置く | (staging のみ。git 外) |
| `prove-compose-claims` | pinned image 内で claims 8 件を検査 | `compose-claims.json` |
| `prove-compose-claims-tamper` | 壊した copy に対して同じ検査を 4 case 実行し、guard が defect を拒否することを実証 | `compose-claims-tamper-<mode>.json` (4 件) |

claims 8 件 (id 順): `phase0_compose_service_set` / `single_pinned_image` /
`credentials_not_baked` / `data_volume_mount` / `host_ollama_gateway` /
`gate_uses_working_tree_ro` / `proof_state_in_container_tmp` /
`secrets_and_data_excluded_from_image`。

tamper 4 case (`--tamper <mode>`) と検出 claim:

| tamper mode | 壊す内容 | 検出する claim |
| :--- | :--- | :--- |
| `literal-credential` | compose file へ literal の API key を焼付ける | `credentials_not_baked` |
| `bind-data-dir` | named volume を `./data` bind mount へ差し替える | `data_volume_mount` |
| `unprofiled-gate` | gate の profile を外して default up に入れる | `phase0_compose_service_set` |
| `baked-secret-in-image` | `.env` / data dir を COPY する Dockerfile に差し替える | `secrets_and_data_excluded_from_image` |

evidence の host path について: gate は host repo root を `<repo>` へ mask してから書く
(`redact_host_paths` / `write_evidence`)。mask 後の payload に host root が残っている場合は
書き出さず失敗する。unit test は `tests/unit/test_phase0_compose_claims.py`
(pinned image の `make test` で走る。gate container には `docs/evidence` が mount されないため
evidence file を読む test は置かない)。

残存局限 (Phase 0 で closed にしていないこと):

- 実 `docker compose up` の起動証跡はこの gate を含まない (検査対象は expand 結果)。
- gate 用 image に docker CLI が無いため expand は host 側で行う。container 側で見るのは
  expand 済み JSON と compose / Dockerfile / .dockerignore の copy だけ。
- expand は `--env-file /dev/null` なので、展開値は compose file の既定値 / placeholder
  (`AI_API_KEY: "local-dummy"`, `COPILOT_GITHUB_TOKEN: ""`) であり、host の実 env を反映しない。
- §4 の live proof 同様、built-in tool への deny 応答は実測未取得 (unit test 検証)。
