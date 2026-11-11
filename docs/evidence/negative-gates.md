# Phase 0 negative gates 実行ログ

実行: `make prove-negative-gates` (helper: `tests/proofs/collect_negative_gates.sh`)。期待 exit code は 0 = guard が defect を検出。収集時刻: 2026-11-11T12:09:18Z UTC

## make prove-manifest-version-tamper

```shell
$ make prove-manifest-version-tamper

negative gate OK: verify_pinned_manifest.py は exit 1 で reject した
    assert sdk_version == manifest["sdk_version"], (
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: SDK 版 mismatch: 実測 1.0.29 != manifest 9.9.9

exit code: 0 (OK)
```

## make prove-manifest-digest-tamper

```shell
$ make prove-manifest-digest-tamper

negative gate OK: fetch_runtime_mirror.py は exit 1 で reject した
    assert manifest_digest == expected, (
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: manifest digest mismatch for github-copilot-1.0.107-linux-x64.tgz: 0000000000000000000000000000000000000000000000000000000000000000 != a1e3acdb98106350bb0c360e2e18d9fbf549cea0b321be50af51394b3efaf057

exit code: 0 (OK)
```

## make prove-offline-negative

```shell
$ make prove-offline-negative

evidence: docs/evidence/offline-force-download-failure.json

exit code: 0 (OK)
```

## make prove-offline-runtime

```shell
$ make prove-offline-runtime

JsonRpcClient.request JSON-RPC request finished
Traceback (most recent call last):
  File "/app/backend/.venv/lib/python3.12/site-packages/copilot/_jsonrpc.py", line 265, in request
    result = await future
             ^^^^^^^^^^^^
copilot._jsonrpc.JsonRpcError: JSON-RPC Error -32603: Request models.list failed with message: Not authenticated. Please authenticate first.
models.list は BYOK では失敗するため settings.model を返す: JSON-RPC Error -32603: Request models.list failed with message: Not authenticated. Please authenticate first.
JsonRpcClient.request JSON-RPC request finished
Traceback (most recent call last):
  File "/app/backend/.venv/lib/python3.12/site-packages/copilot/_jsonrpc.py", line 265, in request
    result = await future
             ^^^^^^^^^^^^
copilot._jsonrpc.JsonRpcError: JSON-RPC Error -32603: Failed to get response from the AI model; retried 5 times (total retry wait time: 23.00 seconds) Last error: Failed native model HTTP request: error sending request for url (http://host.docker.internal:11434/v1/chat/completions): client error (Connect): dns error: error resolving DNS: failed to lookup address information: Temporary failure in name resolution [ENOTFOUND]
[offline-proof] {"case": "runtime-network-none", "network_reachable": false, "network_detail": "172.17.0.1:8098 unreachable (OSError)", "provider": "ollama", "model": null, "provider_settings": {"enable_skills": true, "session_store_enabled": false, "turn_timeout_seconds": 180}, "runtime_started": true, "health_ready": true, "health_detail": "", "list_models_count": 0, "session_id": "d67c9230-174a-4d74-a59d-695fa1a765be", "snapshot_tool_names": ["ofc_home_route_probe"], "tool_registered": true, "turn_aborted": true, "turn_tool_calls": [], "turn_text": "", "turn_events": 1, "handler_calls": [], "session_closed": true, "runtime_stopped": true}
evidence written: /tmp/evidence/offline-runtime-proof.json
evidence: docs/evidence/offline-runtime-proof.json

exit code: 0 (OK)
```

## make prove-compose-claims-tamper

```shell
$ make prove-compose-claims-tamper

[compose-claims-tamper] literal-credential
[compose-claims] tamper=literal-credential guard_detected=True claim=credentials_not_baked
  reject_reason: ComposeClaimsError: credentials_not_baked 不合格: compose literals=["      AI_API_KEY: sk-FAKELITERALVALUE123", "      AI_API_KEY: sk-FAKELITERALVALUE123"] expanded={"gate": {"AI_API_KEY": {"present": false, "value_class": "placeholder"}, "COPILOT_GITHUB_TOKEN": {"present": false, "value_class": "placeholder"}, "GH_TOKEN": {"present": false, "value_class": "placeholder"}, "GITHUB_TOKEN": {"present": false, "value_class": "placeholder"}}, "proof": {"AI_API_KEY": {"present": true, "value_class": "placeholder"}, "COPILOT_GITHUB_TOKEN": {"present": true, "value_class": "placeholder"}, "GH_TOKEN": {"present": false, "value_class": "placeholder"}, "GITHUB_TOKEN": {"present": false, "value_class": "placeholder"}}, "runtime": {"AI_API_KEY": {"present": true, "value_class": "literal_secret"}, "COPILOT_GITHUB_TOKEN": {"present": true, "value_class": "placeholder"}, "GH_TOKEN": {"present": false, "value_class": "placeholder"}, "GITHUB_TOKEN": {"present": false, "value_class": "placeholder"}}}
[compose-claims-tamper] bind-data-dir
[compose-claims] tamper=bind-data-dir guard_detected=True claim=data_volume_mount
  reject_reason: ComposeClaimsError: data_volume_mount 不合格: named_volumes=["household-data"] runtime=["./data:/app/data:rw"]
[compose-claims-tamper] unprofiled-gate
[compose-claims] tamper=unprofiled-gate guard_detected=True claim=phase0_compose_service_set
  reject_reason: ComposeClaimsError: phase0_compose_service_set 不合格: services=["gate", "proof", "runtime"] default_up=["gate", "runtime"] profiles={"gate": [], "proof": ["checks"], "runtime": []}
[compose-claims-tamper] baked-secret-in-image
[compose-claims] tamper=baked-secret-in-image guard_detected=True claim=secrets_and_data_excluded_from_image
  reject_reason: ComposeClaimsError: secrets_and_data_excluded_from_image 不合格: excluded=["*.py[oc]", "*.zcode.*", ".env", ".env.*", ".git", ".github", ".pytest_cache/", ".ruff_cache/", ".spike/", ".venv*/", ".venv/", "__pycache__/", "copilot-runtime/", "data/", "frontmatter.json", "installed-plugins/", "logs/", "session-state/"] copy_sources=["./", "./README.md", "./app", ".env", "/app/.env", "/app/baked-data", "/app/docs/pinned-runtime-versions.json", "/app/skills", "/app/tests", "backend/README.md", "backend/app", "backend/pyproject.toml", "backend/uv.lock", "data", "docs/pinned-runtime-versions.json", "skills", "tests"]
negative gate OK: defect 検出。evidence: docs/evidence/compose-claims-tamper-*.json

exit code: 0 (OK)
```
