# Phase 0 negative gates 実行ログ

実行: `make prove-negative-gates` (helper: `tests/proofs/collect_negative_gates.sh`)。期待 exit code は 0 = guard が defect を検出。収集時刻: 2026-11-11T00:38:12Z UTC

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
[offline-proof] {"case": "runtime-network-none", "network_reachable": false, "network_detail": "172.17.0.1:8098 unreachable (OSError)", "provider": "ollama", "model": null, "provider_settings": {"enable_skills": true, "session_store_enabled": false, "turn_timeout_seconds": 180}, "runtime_started": true, "health_ready": true, "health_detail": "", "list_models_count": 0, "session_id": "8cb6bae3-1ca0-43e1-8bcd-b86de936d8a1", "snapshot_tool_names": ["ofc_home_route_probe"], "tool_registered": true, "turn_aborted": true, "turn_tool_calls": [], "turn_text": "", "turn_events": 1, "handler_calls": [], "session_closed": true, "runtime_stopped": true}
evidence written: /tmp/evidence/offline-runtime-proof.json
evidence: docs/evidence/offline-runtime-proof.json

exit code: 0 (OK)
```
