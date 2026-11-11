# Phase 0 negative gates 実行ログ

再生成: `make prove-negative-gates` (個別: `make prove-manifest-version-tamper` / `prove-manifest-digest-tamper` / `prove-offline-negative` / `prove-offline-runtime`)。収集時刻: 2026-11-11T00:12:42Z UTC

## make prove-manifest-version-tamper

```text
negative gate OK: manifest 版改ざんは failure になった
```

## make prove-manifest-digest-tamper

```text
negative gate OK: digest 改ざんは failure になった (staging 照合)
```

## make prove-offline-negative

```text
docker run --rm --network none household-ai-runtime-test:0.1.0 \
  python /app/tests/proofs/offline_runtime_cache_proof.py --mode force-download-failure \
  > docs/evidence/offline-force-download-failure.json
evidence: docs/evidence/offline-force-download-failure.json
```

## make prove-offline-runtime

```text
docker run --rm --network none -v $PWD/.local/evidence:/tmp/evidence household-ai-runtime-test:0.1.0 \
  python /app/tests/proofs/phase0_offline_runtime_proof.py \
  --out /tmp/evidence/offline-runtime-proof.json
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
[offline-proof] {"case": "runtime-network-none", "network_reachable": false, "network_detail": "172.17.0.1:8098 unreachable (OSError)", "provider": "ollama", "model": null, "provider_settings": {"enable_skills": true, "session_store_enabled": false, "turn_timeout_seconds": 180}, "runtime_started": true, "health_ready": true, "health_detail": "", "list_models_count": 0, "session_id": "6a684be8-2ed2-4b6c-9395-4b73d9753a4c", "snapshot_tool_names": ["ofc_home_route_probe"], "tool_registered": true, "turn_aborted": true, "turn_tool_calls": [], "turn_text": "", "turn_events": 1, "handler_calls": [], "session_closed": true, "runtime_stopped": true}
evidence written: /tmp/evidence/offline-runtime-proof.json
evidence: docs/evidence/offline-runtime-proof.json
```
