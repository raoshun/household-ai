"""Phase 1 認証状態 / Connect gate probe (Issue #4)。

Phase 0 実測の 3 期待を pinned image 内で 1 回の run で検証する:

1. **無凭证起動回帰**: provider (SDK client) が GitHub 凭证なしで起動できる
   (`mode='empty'` / `use_logged_in_user=False` の BYOK 構成)。
2. **token handoff**: GitHub 凭证の確立手順 (Issue #4) = env に token を置いて
   `gh auth login --use-env`。ただし SDK 1.0.29 の `auth.getStatus` は env の
   token だけでは isAuthenticated にならない (pinned image で実測) ため、この
   期待は advisory (note 出力) として検証する。
3. **Connect 状態照会**: `auth.getStatus` RPC が reach でき、凭证なしでは
   unauthenticated を返す (§16.1 へ引き継ぐ health 系 info)。

実行 (Docker、凭证なし = compose の auth-gate service):
  docker compose run --rm auth-gate
実行 (host、pinned image で証跡出力。repo が venv を覆わないよう tests のみ読む):
  docker run --rm -v "$PWD/tests:/app/tests:ro" household-ai-runtime-test:0.1.0 \
    python /app/tests/proofs/phase1_auth_connect_probe.py --gate

凭证を入れて試すときのみ COPILOT_GITHUB_TOKEN を渡す (token は出力しない)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.agent.provider import CopilotAgentProvider  # noqa: E402
from app.config import load_settings  # noqa: E402

TOKEN_ENV_KEYS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def token_present_in_env() -> bool:
    """SDK 1.0.29 が client env から読む凭证の有無 (値は決して取得しない)。"""
    return any(os.environ.get(key, "").strip() for key in TOKEN_ENV_KEYS)


async def run_probe() -> dict[str, Any]:
    # SDK の session/runtime dir は container 内 /tmp が書き込み可 (compose 同様)。
    tmp_root = Path(tempfile.mkdtemp(prefix="household-auth-probe-"))
    settings = load_settings().model_copy(
        update={
            "data_dir": tmp_root,
            "runtime_dir": tmp_root / "copilot-runtime",
        }
    )

    provider = CopilotAgentProvider(settings)
    evidence: dict[str, Any] = {
        "token_env_present": token_present_in_env(),
        "byok_start_ok": False,
        "connect": None,
    }
    try:
        # 期待 1: 凭证設定に依存せず SDK client が起動できること
        await provider.start()
        evidence["byok_start_ok"] = True

        # 期待 3: auth.getStatus RPC が reach できること
        status = await provider.connect_status()
        evidence["connect"] = {
            "reachable": status.reachable,
            "github_authenticated": status.github_authenticated,
            "login": status.login if status.github_authenticated else None,
            "auth_type": status.auth_type if status.github_authenticated else None,
            "detail": status.detail,
        }
    finally:
        await provider.stop()
        shutil.rmtree(tmp_root, ignore_errors=True)
    return evidence


def gate_checks(evidence: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(gate 失敗理由, 参考 notes) を返す。失敗が空なら合格。

    期待 2 (token handoff) は advisory: SDK 1.0.29 の `auth.getStatus` は
    クライアントの Copilot 認証状態を返すだけで、`COPILOT_GITHUB_TOKEN` を
    渡しても isAuthenticated は True にならない (pinned image で実測)。
    Issue #4 の確立手順 (env に token を置いて `gh auth login --use-env`) を
    踏んだ環境でのみ authenticated になるため、不一致は note として記録し、
    gate は期待 1/3 (無凭证起動・RPC reach) と無凭证時の unauthenticated のみ
    必須条件とする。
    """
    failures: list[str] = []
    notes: list[str] = []
    if not evidence["byok_start_ok"]:
        failures.append("期待1: 無凭证で SDK client が起動できなかった")
    connect = evidence["connect"]
    if connect is None or not connect["reachable"]:
        failures.append(f"期待3: auth.getStatus が照会できない: {connect}")
        return failures, notes
    token_present = evidence["token_env_present"]
    authenticated = connect["github_authenticated"]
    if not token_present and authenticated:
        failures.append("期待1/2: 凭证なしなのに authenticated (想定外)")
    if token_present and not authenticated:
        notes.append(
            "期待2 (advisory): token env を渡したが auth.getStatus は "
            "unauthenticated のまま。authenticated にするには env に token を"
            "置いて gh auth login --use-env (Issue #4 の確立手順) が必要"
        )
    return failures, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate",
        action="store_true",
        help="期待との不一致を exit code で弾く (compose/CI 向け)",
    )
    args = parser.parse_args()

    evidence = asyncio.run(run_probe())
    failures: list[str] = []
    notes: list[str] = []
    if args.gate:
        failures, notes = gate_checks(evidence)
    print(
        json.dumps(
            {
                "ok": not failures,
                "failures": failures,
                "notes": notes,
                **evidence,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
