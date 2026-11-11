"""pinned runtime が network 不在環境で起動〜Tool 登録までできることを証明する (§15).

make build-offline で build した image (CLI runtime bake 済み) を docker run
--network none で動かし、次を実測する:

1. runtime 起動 (SDK が cache から hostless runtime を解決)
2. Custom Tool 宣言の登録 (apps -> provider -> SDK 変換, §6.2)
3. LLM 不在での turn 結果 (model 到達不可は記録, ハンガーは失敗)

CLI runtime 版の offline reproducibility は fetch_runtime_mirror.py と
verify_pinned_manifest.py が担保する。LLM 推論自体は Phase 2 の BYOK gateway で
担保するため、ここでの合格条件は runtime 起動 + Tool 登録 + turn 即失敗とする。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.agent import AgentRuntime, AgentRuntimeError  # noqa: E402 (sys.path 操作後)
from app.agent.provider import CustomToolDeclaration  # noqa: E402
from app.config import load_settings  # noqa: E402

TOOL_NAME = "ofc_home_route_probe"
PROBE_HOST = os.environ.get("HOUSEHOLD_PROBE_HOST", "172.17.0.1")
PROBE_PORT = int(os.environ.get("HOUSEHOLD_PROBE_PORT", "8098"))
TURN_TIMEOUT_SECONDS = 120


def network_probe() -> tuple[bool, str]:
    """container から mirror host へ到達できるか確認する (netns 検証)."""
    try:
        with socket.create_connection((PROBE_HOST, PROBE_PORT), timeout=3):
            return True, f"{PROBE_HOST}:{PROBE_PORT} reachable"
    except OSError as exc:
        return False, f"{PROBE_HOST}:{PROBE_PORT} unreachable ({type(exc).__name__})"


def build_declaration(calls: list[str]) -> CustomToolDeclaration:
    async def handle(params, *args, **kwargs):
        target = params if isinstance(params, dict) else (args[0] if args else None)
        area = target.get("area") if isinstance(target, dict) else None
        calls.append(str(area))
        return f"OFFLINE_PROBE_EXECUTED area={area}"

    return CustomToolDeclaration(
        name=TOOL_NAME,
        description="offline 検証用 Custom Tool。area を受け取り固定応答を返す。",
        parameters={
            "type": "object",
            "properties": {"area": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=handle,
    )


async def run(case: str) -> dict:
    record: dict = {"case": case}
    reachable, detail = network_probe()
    record["network_reachable"] = reachable
    record["network_detail"] = detail
    if reachable:
        raise AssertionError(
            f"network が到達可能 ({detail})。--network none で実行すること。"
        )

    settings = load_settings(REPO)
    record["provider"] = settings.provider
    record["model"] = settings.model
    record["provider_settings"] = {
        "enable_skills": settings.enable_skills,
        "session_store_enabled": settings.session_store_enabled,
        "turn_timeout_seconds": settings.turn_timeout_seconds,
    }

    calls: list[str] = []
    tool = build_declaration(calls)
    runtime = AgentRuntime(settings)
    try:
        await runtime.start()
        record["runtime_started"] = True

        health = await runtime.health_check()
        record["health_ready"] = health.ready
        record["health_detail"] = (health.detail or "")[:200]

        try:
            models = await asyncio.wait_for(runtime.list_models(), timeout=30)
            record["list_models_count"] = len(list(models))
        except Exception as exc:  # 実測記録が目的
            record["list_models_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

        handle = await runtime.create_session(
            model=settings.model,
            tools=[tool],
            system_message=(
                "家庭内ルートを支援するアシスタント。家用家事の質問には必ず "
                f"{TOOL_NAME} を呼び出すこと。"
            ),
        )
        record["session_id"] = handle.session_id
        snapshot = runtime.snapshot(handle.session_id)
        tool_names = list(getattr(snapshot, "tool_names", []) or [])
        record["snapshot_tool_names"] = tool_names
        record["tool_registered"] = TOOL_NAME in tool_names

        try:
            turn = await asyncio.wait_for(
                runtime.send_message(
                    handle.session_id, "台所の食器はどこに片付けてある？"
                ),
                timeout=TURN_TIMEOUT_SECONDS,
            )
            record["turn_aborted"] = turn.aborted
            record["turn_tool_calls"] = turn.tool_calls
            record["turn_text"] = (turn.text or "")[:200]
            record["turn_events"] = len(turn.events)
        except TimeoutError:
            record["turn_error"] = f"TimeoutError after {TURN_TIMEOUT_SECONDS}s"
        except AgentRuntimeError as exc:
            record["turn_error"] = f"AgentRuntimeError: {str(exc)[:200]}"
        except Exception as exc:  # 実行環境側の失敗を区別して記録
            record["turn_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        record["handler_calls"] = list(calls)

        try:
            await asyncio.wait_for(runtime.close_session(handle.session_id), timeout=15)
            record["session_closed"] = True
        except Exception as exc:  # close 失敗を記録するため
            record["close_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        try:
            await asyncio.wait_for(runtime.stop(), timeout=30)
            record["runtime_stopped"] = True
        except Exception as exc:  # stop 失敗を記録するため
            record["runtime_stop_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

    assert record.get("runtime_started"), "runtime が offline で起動しなかった"
    assert record.get("tool_registered"), (
        f"Custom Tool が登録されなかった: {record.get('snapshot_tool_names')}"
    )
    if record.get("turn_error") is None and record.get("turn_tool_calls"):
        raise AssertionError(f"offline で LLM turn が成功した扱いになった: {record}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="runtime-network-none")
    parser.add_argument(
        "--out",
        default=os.environ.get("HOUSEHOLD_EVIDENCE", "/tmp/offline-runtime-proof.json"),
    )
    args = parser.parse_args()
    try:
        record = asyncio.run(run(args.case))
    except Exception as exc:  # proof は失敗理由を出力して exit 1
        print(f"[offline-proof] FAILED {type(exc).__name__}: {exc}", flush=True)
        return 1
    Path(args.out).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(f"[offline-proof] {json.dumps(record, ensure_ascii=False)}", flush=True)
    print(f"evidence written: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
