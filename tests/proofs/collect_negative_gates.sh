#!/usr/bin/env bash
# 5 本の negative gate を順に実行し、results を markdown evidence へ保存する。
# 各 gate target は「guard が working tree / manifest / cache を実際に信用していないこと」を
# fail として signal する設計なので、expected exit code は 0 (guard が defect を検出) である。
set -u

tmp_dir=${1:-.local/negative-gates}
log_path=${2:-docs/evidence/negative-gates.md}
gate_targets=(
  prove-manifest-version-tamper
  prove-manifest-digest-tamper
  prove-offline-negative
  prove-offline-runtime
  prove-compose-claims-tamper
)

mkdir -p "$tmp_dir" "$(dirname "$log_path")"

{
  printf '# Phase 0 negative gates 実行ログ\n\n'
  printf '実行: `make prove-negative-gates` (helper: `tests/proofs/collect_negative_gates.sh`)。'
  printf '期待 exit code は 0 = guard が defect を検出。収集時刻: %s UTC\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$log_path"

failures=0
for target in "${gate_targets[@]}"; do
  echo "[negative-gates] running make $target" >&2
  output=$(make -s --no-print-directory "$target" "GATE_TMP=$tmp_dir" 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    failures=$((failures + 1))
    status="FAIL (guard が defect を検出できなかった、または target error)"
  else
    status="OK"
  fi
  {
    printf '\n## make %s\n\n```shell\n$ make %s\n\n' "$target" "$target"
    printf '%s\n' "$output"
    printf '\nexit code: %s (%s)\n```\n' "$rc" "$status"
  } >>"$log_path"
done

if [ "$failures" -ne 0 ]; then
  echo "[negative-gates] NG: $failures / ${#gate_targets[@]} gate が期待に反して fail" >&2
  exit 1
fi

echo "[negative-gates] OK: ${#gate_targets[@]} gate すべて guard defect を検出。evidence: $log_path" >&2
