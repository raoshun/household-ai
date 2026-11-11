"""AgentRuntime 経由の実証: registered-name policy でも Custom Tool が実行され、
内蔵 file/shell tool と MCP が無効なことを 1 回の run で示す (Issue #1 完了条件 3・4)。

実行 (host):
  HOUSEHOLD_TOOL_POLICY=registered AI_PROVIDER=ollama AI_MODEL=gemma4:e2b \
    OLLAMA_BASE_URL=http://localhost:11434/v1 AI_API_KEY=local-dummy \
    .venv/bin/python tests/proofs/phase0_live_proof.py registered

実行 (Docker): docker compose run --rm proof
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.agent.events import wire_type  # noqa: E402
from app.agent.provider import (  # noqa: E402 (sys.path 操作後に import する)
    CopilotAgentSession,
    CustomToolDeclaration,
)
from app.agent.runtime import AgentRuntime  # noqa: E402
from app.config import load_settings  # noqa: E402

MODEL = os.environ.get("AI_MODEL", "gemma4:e2b")
TOOL_NAME = "household_list_housework"
# Custom Tool を登録する case (custom) と登録しない case (none) を切り替える。
# 両 case で内蔵 file/shell tool が実行されないことを実測するために使う。
TOOLS_ENV = "HOUSEHOLD_PROOF_TOOLS"
CASE_TAGS = {"custom": "registered", "none": "unregistered"}
BUILTIN_TOOL_NAMES = {
    "bash",
    "run_bash",
    "read_bash",
    "stop_bash",
    "view",
    "edit",
    "write",
    "create",
    "grep",
    "glob",
    "web_fetch",
    "sql",
}
PROMPT_TOOL = (
    f"{TOOL_NAME} ツールを必ず 1 回呼び出して、家用家事の一覧を取得し、"
    "3 項目を日本語で伝えてください。ツールを使わずに答えないでください。"
)
PROMPT_SHELL = (
    "シェルを実行して uname -a の出力を確認し、その結果をそのまま教えてください。"
)

raw_events: list[Any] = []
handler_calls: list[dict[str, Any]] = []

_ORIGINAL_HANDLE = CopilotAgentSession._handle_sdk_event


def _spy_handle(self: CopilotAgentSession, sdk_event: Any) -> None:
    raw_events.append(sdk_event)
    _ORIGINAL_HANDLE(self, sdk_event)


CopilotAgentSession._handle_sdk_event = _spy_handle  # type: ignore[method-assign]


def build_declaration() -> CustomToolDeclaration:
    async def handler(tool_name: str, arguments: dict[str, Any]) -> str:
        handler_calls.append({"tool": tool_name, "arguments": dict(arguments)})
        print(
            f"[proof] handler 実行 tool_name={tool_name} arguments={arguments}",
            flush=True,
        )
        return "食器洗い: 週 3 回\nゴミ出し: 毎週月曜\n浴室掃除: 週 1 回"

    return CustomToolDeclaration(
        name=TOOL_NAME,
        description=(
            "List registered housework items. Call this before answering about "
            "家用家事."
        ),
        handler=handler,
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "家用家事の名前 (例: 家用家事)",
                }
            },
            "required": ["name"],
        },
        approval_mode="ask",
    )


def tool_case() -> str:
    """Custom Tool を登録する case ('custom') と非登録 case ('none') を返す。"""
    raw = os.environ.get(TOOLS_ENV, "custom").strip().lower()
    return raw if raw in CASE_TAGS else "custom"


def dump_disabled_tools() -> list[str]:
    lines: list[str] = []
    for ev in raw_events:
        data = getattr(ev, "data", None)
        if getattr(data, "info_type", None) == "configuration":
            message = getattr(data, "message", "") or ""
            if "Disabled tools" in message:
                lines.append(message)
    return lines


def dump_tool_events() -> None:
    for ev in raw_events:
        etype = str(getattr(ev, "type", ""))
        if "tool" not in etype.lower() and "permission" not in etype.lower():
            continue
        data = getattr(ev, "data", None)
        if isinstance(data, dict):
            summary = json.dumps(data, ensure_ascii=False, default=str)[:400]
        else:
            summary = ", ".join(
                f"{k}={v!r}"
                for k, v in getattr(data, "__dict__", {}).items()
                if not k.startswith("_") and v is not None
            )[:400]
        print(f"[proof] ev {etype}: {summary}", flush=True)


def tool_outcomes() -> list[tuple[str, bool]]:
    """TOOL_EXECUTION_COMPLETE から (tool 名, success) を拾う。

    request だけの tool 名と実際に execution まで到達した tool 名を区別するため使う。
    未登録 name は execution failure になる (does not exist を 2026-11-11 実測)。
    """
    outcomes: list[tuple[str, bool]] = []
    for ev in raw_events:
        if "tool.execution_complete" not in str(getattr(ev, "type", "")).lower():
            continue
        data = getattr(ev, "data", None)
        name = getattr(data, "tool_name", None) or "<unknown>"
        outcomes.append((str(name), bool(getattr(data, "success", False))))
    return outcomes


def summarize(tag: str) -> None:
    types = collections.Counter(str(getattr(ev, "type", "?")) for ev in raw_events)
    # raw_events に積み上がるのは SDK の SessionEvent オブジェクト。wire 名は
    # events.wire_type() で辿り、runtime の normalize がマップしない wire 名は enum が
    # UNKNOWN (= "unknown") に解決し、素の名前は SessionEvent.raw_type へ残る。
    # 網羅性を実測するため mapped / unmapped を分離する (§15)。
    mapped: collections.Counter[str] = collections.Counter()
    unmapped: collections.Counter[str] = collections.Counter()
    for event in raw_events:
        name = wire_type(getattr(event, "type", None), getattr(event, "raw_type", None))
        if name != "unknown":
            mapped[name] += 1
            continue
        raw_name = getattr(event, "raw_type", None)
        unmapped[raw_name if isinstance(raw_name, str) and raw_name else "<empty>"] += 1
    unknown_count = sum(unmapped.values())
    mapped_text = ", ".join(f"{k}:{v}" for k, v in sorted(mapped.items()))
    unmapped_text = ", ".join(f"{k}:{v}" for k, v in sorted(unmapped.items())) or "none"
    print(f"\n===== proof [{tag}] =====", flush=True)
    print(f"[proof] raw_event_count={len(raw_events)}", flush=True)
    for info in dump_disabled_tools():
        print(f"[proof] {info}", flush=True)
    print(
        f"[proof] sdk_types={', '.join(f'{k}:{v}' for k, v in sorted(types.items()))}",
        flush=True,
    )
    print(f"[proof] wire_types_mapped={mapped_text}", flush=True)
    print(
        f"[proof] wire_types_unmapped_count={unknown_count} names={unmapped_text}",
        flush=True,
    )
    print(
        "[proof] permission_events="
        + ", ".join(
            str(getattr(ev, "type", "?"))
            for ev in raw_events
            if "permission" in str(getattr(ev, "type", "")).lower()
            or "Permission" in type(ev).__name__
        ),
        flush=True,
    )
    denied = [
        ev
        for ev in raw_events
        if "denied" in str(getattr(ev, "data", "")).lower()
        or "denied" in str(getattr(ev, "type", "")).lower()
    ]
    print(f"[proof] denied_like_events={len(denied)}", flush=True)
    for ev in denied[:5]:
        print(f"[proof]   {type(ev).__name__} {ev}", flush=True)
    print(f"[proof] handler_calls={handler_calls}", flush=True)
    print(f"[proof] tool_outcomes={tool_outcomes()}", flush=True)
    dump_tool_events()


async def run_policy(tag: str | None = None) -> int:
    raw_events.clear()
    handler_calls.clear()
    case = tool_case()
    tag = tag or CASE_TAGS[case]
    tools = [build_declaration()] if case == "custom" else []
    registered = len(tools or [])
    settings = load_settings(REPO)
    policy = os.environ.get("HOUSEHOLD_TOOL_POLICY", "registered")
    print(
        f"[proof] [{tag}] policy={policy} tool_case={case}"
        f" registered_custom_tools={registered}",
        flush=True,
    )
    print(
        f"[proof] [{tag}] provider={settings.provider} model={MODEL}"
        f" base_url={settings.byok_base_url} skills={settings.enable_skills}",
        flush=True,
    )
    async with AgentRuntime(settings) as runtime:
        health = await runtime.health_check()
        print(f"[proof] health ready={health.ready} detail={health.detail}", flush=True)
        try:
            models = await runtime.list_models()
            print(f"[proof] list_models={models}", flush=True)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            print(f"[proof] list_models 失敗 {type(exc).__name__}: {exc}", flush=True)

        handle = await runtime.create_session(
            model=MODEL,
            tools=tools,
            system_message=(
                "あなたは家庭内ルートを支援するアシスタントです。家用家事の質問には"
                f"必ず {TOOL_NAME} ツールを呼び出して答えください。"
            ),
        )
        print(f"[proof] session_id={handle.session_id}", flush=True)
        snapshot = runtime.snapshot(handle.session_id)
        print(f"[proof] snapshot={snapshot}", flush=True)

        turn = await runtime.send_message(handle.session_id, PROMPT_TOOL)
        print(
            f"[proof] turn1 aborted={turn.aborted} tool_calls={turn.tool_calls}"
            f" events={len(turn.events)}",
            flush=True,
        )
        print(
            f"[proof] turn1 text={(turn.text or '')[:300]!r}",
            flush=True,
        )
        summarize(f"{tag}/tool-turn")

        turn2 = await runtime.send_message(handle.session_id, PROMPT_SHELL)
        print(
            f"[proof] turn2 aborted={turn2.aborted} tool_calls={turn2.tool_calls}"
            f" events={len(turn2.events)}",
            flush=True,
        )
        print(f"[proof] turn2 text={(turn2.text or '')[:300]!r}", flush=True)
        shell_calls = [
            str(call)
            for call in turn2.tool_calls
            if str(call).lower() in BUILTIN_TOOL_NAMES
        ]
        print(f"[proof] built-in_shell_calls={shell_calls}", flush=True)
        summarize(f"{tag}/shell-attempt")

        await runtime.close_session(handle.session_id)

    outcomes = tool_outcomes()
    requested = [str(call) for call in [*turn.tool_calls, *turn2.tool_calls]]
    # execution まで到達した tool 名 (success 成否を問わず) だけを見る。
    executed = sorted({name for name, _ in outcomes})
    builtins_executed = sorted(n for n in executed if n.lower() in BUILTIN_TOOL_NAMES)
    custom_completed = [n for n, ok_done in outcomes if n == TOOL_NAME and ok_done]
    custom_failed = [n for n, ok_done in outcomes if n == TOOL_NAME and not ok_done]
    custom_executed = bool(custom_completed)
    if case == "custom":
        # 宣言 tool は handler 実行、内蔵 file/bash tool は到達しないこと。
        ok = bool(handler_calls) and bool(custom_completed) and not builtins_executed
    else:
        # 未登録 name は handler 実行も success もないこと。
        ok = (
            not handler_calls
            and not custom_executed
            and bool(custom_failed)
            and not builtins_executed
        )
    print(
        f"[proof] [{tag}] tool_case={case} registered_custom_tools={registered}"
        f" requested_tools={requested} executed_tools={executed}"
        f" handler_calls={len(handler_calls)} custom_completed={custom_completed}"
        f" custom_rejected={custom_failed} builtins_executed={builtins_executed}",
        flush=True,
    )
    print(f"[proof] RESULT [{tag}] ok={ok}", flush=True)
    return 0 if ok else 1


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # tag は case 名から決める (env の HOUSEHOLD_TOOL_POLICY は session の tool 許可
    # 方針の名前であり、proof の tool 登録 case と混同しないため tag には使わない)。
    tag = sys.argv[1] if len(sys.argv) > 1 else None
    return await run_policy(tag)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
