#!/usr/bin/env python3
"""`make proof` の stdout log から tool 登録あり / なし case の実測を集計する.

proof log は model の生テキストや container path を含むため git へコミットしない。
この script は log から数値と event 名だけを抽出し、 docs/evidence/ で共有できる
form の JSON evidence へ変換する。 id 系 token と secret pattern は redact する。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

TURN_RE = re.compile(
    r"\[proof\] turn(\d+) aborted=(True|False) tool_calls=(\[.*?\]) events=(\d+)"
)
TURN_TEXT_RE = re.compile(r"\[proof\] turn(?P<turn>\d+) text=(?P<text>.*)$")
KV_COUNT_RE = re.compile(r"^(?P<name>[a-zA-Z0-9_.]+):(?P<count>\d+)$")
SUMMARY_KEYS = (
    "raw_event_count",
    "denied_like_events",
    "handler_calls",
    "tool_outcomes",
    "permission_events",
    "sdk_types",
    "wire_types_mapped",
    "wire_types_unmapped_count",
)
HEAD_RE = re.compile(
    r"\[proof\] \[(?P<tag>[a-z]+)\] policy=(?P<policy>\S+) tool_case=(?P<case>\S+)"
    r" registered_custom_tools=(?P<registered>\d+)"
)
PROVIDER_RE = re.compile(
    r"\[proof\] \[[a-z]+\] provider=(?P<provider>\S+) model=(?P<model>\S+)"
    r" base_url=(?P<base_url>\S+) skills=(?P<skills>\S+)"
)
FINAL_RE = re.compile(
    r"\[proof\] \[[a-z]+\] tool_case=(?P<case>\S+)"
    r" registered_custom_tools=(?P<registered>\d+)"
    r" requested_tools=(?P<requested>\[.*?\]) executed_tools=(?P<executed>\[.*?\])"
    r" handler_calls=(?P<handler>\d+) custom_completed=(?P<completed>\[.*?\])"
    r" custom_rejected=(?P<rejected>\[.*?\]) builtins_executed=(?P<builtins>\[.*?\])"
)
RESULT_RE = re.compile(r"\[proof\] RESULT \[(?P<tag>[a-z]+)\] ok=(?P<ok>True|False)")
SKILL_RE = re.compile(r"SkillInfo\((?P<fields>.*?)\),?")
SKILL_NAME_RE = re.compile(r"name='(?P<name>[^']+)'")
SKILL_SOURCE_RE = re.compile(r"source='?(?:SkillSource\.)?(?P<source>[A-Za-z_.]+)'?")
TOOL_NAMES_RE = re.compile(r"tool_names=(?P<tools>\[.*?\])")
DISABLED_RE = re.compile(r"\[proof\] Disabled tools: (?P<tools>.*)$")
BUILTIN_SHELL_RE = re.compile(r"\[proof\] built-in_shell_calls=(?P<calls>\[.*?\])")
PERMISSION_REQUEST_RE = re.compile(
    r"(?P<kind>PermissionRequest[A-Za-z]+)\((?P<fields>.*)$"
)
PERMISSION_RESULT_RE = re.compile(r"Permission(?P<kind>[A-Za-z]+)\((?P<fields>[^)]*)\)")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CALL_ID_RE = re.compile(r"\bcall_(?![A-Za-z]*id\b)[A-Za-z0-9]{4,}")
SECRET_RE = re.compile(r"(ghp_|github_pat_|xox[baprs]-|eyJ[A-Za-z0-9_.-]{20,})")

GATE_NAME = "live-proof-two-cases"
SCHEMA_VERSION = 1


def py_text(text: str) -> str:
    """harness が `repr()` で印字した turn text を python literal へ復元する。"""
    try:
        return str(ast.literal_eval(text.strip()))
    except (SyntaxError, ValueError):
        return text


def py_list(text: str) -> list[Any]:
    """Python repr 形式の list ('a', [...]) を安全に評価する。"""
    value = ast.literal_eval(text.strip())
    return list(value) if isinstance(value, (list, tuple)) else [value]


def counts(text: str) -> dict[str, int]:
    """`name:count, name:count` 形式の集計行を dict へ変換する。"""
    out: dict[str, int] = {}
    for item in text.split(", "):
        match = KV_COUNT_RE.match(item.strip())
        if match:
            out[match.group("name")] = int(match.group("count"))
    return out


def redact(value: str) -> str:
    """id 系 token と secret pattern を決め打ちで置換する。"""
    text = UUID_RE.sub("<id>", value)
    text = CALL_ID_RE.sub("<call_id>", text)
    text = SECRET_RE.sub("<redacted>", text)
    return text


def derive_wire_type_stats(turns: list[dict[str, Any]]) -> None:
    """cumulative の wire type 集計から turn 単位の増分を算出して追記する。"""
    previous_mapped: dict[str, int] = {}
    previous_unmapped_events = 0
    for turn in turns:
        mapped = turn.get("wire_types_mapped_cum", {})
        unmapped = turn.get("wire_types_unmapped_cum", {})
        mapped_count = turn.get("wire_types_unmapped_count_cum", 0)
        new_mapped = {
            name: count - previous_mapped.get(name, 0)
            for name, count in mapped.items()
            if count - previous_mapped.get(name, 0)
        }
        turn["wire_types_mapped_names_cum"] = len(mapped)
        turn["wire_types_mapped_events_cum"] = sum(mapped.values())
        turn["wire_types_mapped_new"] = new_mapped
        turn["wire_types_mapped_new_names"] = len(new_mapped)
        turn["wire_types_mapped_new_events"] = sum(new_mapped.values())
        turn["wire_types_unmapped_names_cum"] = len(unmapped)
        turn["wire_types_unmapped_events_cum"] = mapped_count
        turn["wire_types_unmapped_events_new"] = mapped_count - previous_unmapped_events
        previous_mapped = mapped
        previous_unmapped_events = mapped_count


def derive_turn_counts(turns: list[dict[str, Any]]) -> None:
    """cumulative 集計を行ごとの差分と gate 用の countへ変換する。"""
    prev_raw_events = 0
    prev_turn_events = 0
    prev_mapped_events = 0
    prev_unmapped_events = 0
    for turn in turns:
        raw_cum = int(turn.get("raw_event_count_cum", 0))
        turn["raw_event_count_new"] = raw_cum - prev_raw_events
        prev_raw_events = raw_cum

        turn_events_cum = prev_turn_events + int(turn.get("turn_events", 0))
        turn["turn_events_cum"] = turn_events_cum
        turn["turn_events_new"] = turn_events_cum - prev_turn_events
        prev_turn_events = turn_events_cum

        mapped: dict[str, int] = turn.get("wire_types_mapped_cum", {})
        mapped_events = sum(mapped.values())
        turn["wire_types_mapped_events_cum"] = mapped_events
        turn["wire_types_mapped_events_new"] = mapped_events - prev_mapped_events
        prev_mapped_events = mapped_events

        unmapped_map: dict[str, int] = turn.get("wire_types_unmapped_cum", {})
        unmapped_events = sum(unmapped_map.values()) or int(
            turn.get("wire_types_unmapped_count_cum", 0)
        )
        turn["wire_types_unmapped_events_cum"] = unmapped_events
        turn["wire_types_unmapped_events_new"] = unmapped_events - prev_unmapped_events
        prev_unmapped_events = unmapped_events

        sdk: dict[str, int] = turn.get("sdk_event_types_cum", {})
        turn["sdk_type_names_cum"] = len(sdk)
        turn["sdk_unknown_events_cum"] = sum(
            count for name, count in sdk.items() if "UNKNOWN" in name
        )
        tool_calls_cum = sum(
            count
            for name, count in mapped.items()
            if name.startswith("assistant.tool_call")
        )
        turn["tool_call_events_cum"] = tool_calls_cum
        turn["tool_execution_starts_cum"] = mapped.get("tool.execution_start", 0)
        turn["tool_execution_completions_cum"] = mapped.get(
            "tool.execution_complete", 0
        )
        turn["external_tool_events_cum"] = sum(
            count for name, count in mapped.items() if name.startswith("external_tool")
        )
        requested = mapped.get("permission.requested", 0)
        completed = mapped.get("permission.completed", 0)
        turn["permission_requested_events_cum"] = requested
        turn["permission_completed_events_cum"] = completed
        turn["permission_pair_count_cum"] = min(requested, completed)
        turn["events_accounted_cum"] = mapped_events + unmapped_events
        turn["events_balanced_cum"] = turn["events_accounted_cum"] == raw_cum


def derive_case_counts(case: dict[str, Any]) -> None:
    """case 最後の cumulative 値から gate が使える集計を作る。"""
    turns = case["turns"]
    last = turns[-1] if turns else {}
    mapped: dict[str, int] = last.get("wire_types_mapped_cum", {})
    unmapped: dict[str, int] = last.get("wire_types_unmapped_cum", {})
    final = case.get("final_counts", {})
    outcomes: list[list[Any]] = last.get("tool_outcomes_cum", [])
    case["derived"] = {
        "turn_count": len(turns),
        "aborted_turn_count": sum(1 for turn in turns if turn.get("aborted")),
        "turns_with_text": sum(1 for turn in turns if turn.get("text_present")),
        "tool_call_events_total": last.get("tool_call_events_cum", 0),
        "requested_tool_names": sorted(
            {name for turn in turns for name in turn.get("tool_calls", [])}
        ),
        "handler_call_count_total": int(last.get("handler_call_count_cum", 0)),
        "tool_execution_starts_total": last.get("tool_execution_starts_cum", 0),
        "tool_execution_completions_total": last.get(
            "tool_execution_completions_cum", 0
        ),
        "tool_outcomes": [
            {"tool_name": item[0], "success": item[-1]} for item in outcomes
        ],
        "tool_outcome_successes": sum(1 for item in outcomes if item[-1] is True),
        "tool_outcome_failures": sum(1 for item in outcomes if item[-1] is False),
        "external_tool_events_total": last.get("external_tool_events_cum", 0),
        "permission_requested_events_total": last.get(
            "permission_requested_events_cum", 0
        ),
        "permission_completed_events_total": last.get(
            "permission_completed_events_cum", 0
        ),
        "permission_pair_count_total": last.get("permission_pair_count_cum", 0),
        "denied_like_events": int(last.get("denied_like_events_cum", 0)),
        "built_in_shell_call_requests_total": sum(
            len(calls) for calls in case.get("built_in_shell_call_requests", [])
        ),
        "wire_type_names_mapped_total": len(mapped),
        "wire_type_names_unmapped_total": len(unmapped),
        "wire_types_union": sorted(set(mapped) | set(unmapped)),
        "wire_types_union_total": len(set(mapped) | set(unmapped)),
        "raw_event_count_total": last.get("raw_event_count_cum", 0),
        "events_balanced_total": last.get("events_balanced_cum", False),
        "sdk_type_names_total": last.get("sdk_type_names_cum", 0),
        "sdk_unknown_events_total": last.get("sdk_unknown_events_cum", 0),
        "builtins_executed": final.get("builtins_executed", []),
        "disabled_tool_names_total": sorted(case.get("disabled_tools", [])),
        "disabled_tool_names_count": len(case.get("disabled_tools", [])),
        "shell_tools_disabled": sorted(
            name
            for name in case.get("disabled_tools", [])
            if name in {"bash", "write_agent", "read_bash", "stop_bash", "list_bash"}
        ),
    }


def parse_proof_log(path: Path) -> dict[str, Any]:  # noqa: C901  # log dispatch 集約
    """1 本の proof log を解析して case 単位の計測値を返す。"""
    head: dict[str, Any] = {}
    turns: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    ok: bool | None = None
    skills: list[dict[str, str]] = []
    session_tools: list[str] | None = None
    disabled: list[str] = []
    built_in_shell_calls: list[list[str]] = []
    permission_requests: list[dict[str, str]] = []
    permission_results: list[dict[str, str]] = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("[proof]"):
            continue

        match = HEAD_RE.search(line)
        if match:
            head = {
                "tag": match.group("tag"),
                "tool_policy_label": match.group("policy"),
                "tool_case": match.group("case"),
                "registered_custom_tools": int(match.group("registered")),
            }
            continue
        match = PROVIDER_RE.search(line)
        if match:
            head.setdefault("provider", match.group("provider"))
            head.setdefault("model", match.group("model"))
            head.setdefault("base_url", match.group("base_url"))
            head.setdefault("enable_skills", match.group("skills") == "True")
            continue
        match = TURN_RE.search(line)
        if match:
            turns.append(
                {
                    "turn": int(match.group(1)),
                    "aborted": match.group(2) == "True",
                    "tool_calls": [
                        c.strip().strip("'\"")
                        for c in match.group(3).strip("[]").split(",")
                        if c.strip()
                    ],
                    "turn_events": int(match.group(4)),
                }
            )
            continue
        match = TURN_TEXT_RE.search(line)
        if match and turns:
            turn = next(
                (t for t in turns if t["turn"] == int(match.group("turn"))), turns[-1]
            )
            turn["text"] = redact(py_text(match.group("text")))
            turn["text_present"] = True
            continue
        match = RESULT_RE.search(line)
        if match:
            ok = match.group("ok") == "True"
            continue
        match = FINAL_RE.search(line)
        if match:
            final = {
                "requested_tools": py_list(match.group("requested")),
                "executed_tools": py_list(match.group("executed")),
                "handler_calls": int(match.group("handler")),
                "custom_completed": py_list(match.group("completed")),
                "custom_rejected": py_list(match.group("rejected")),
                "builtins_executed": py_list(match.group("builtins")),
            }
            continue
        if "SkillInfo(" in line:
            for fields in SKILL_RE.findall(line):
                name = SKILL_NAME_RE.search(fields)
                if not name or name.group("name") in {item["name"] for item in skills}:
                    continue
                source = SKILL_SOURCE_RE.search(fields)
                skills.append(
                    {
                        "name": name.group("name"),
                        "source": source.group("source") if source else "unknown",
                        "enabled": "enabled=True" in fields,
                    }
                )
        match = TOOL_NAMES_RE.search(line)
        if match and session_tools is None:
            session_tools = py_list(match.group("tools"))
            continue
        match = DISABLED_RE.search(line)
        if match:
            names = [n.strip() for n in match.group("tools").split(",") if n.strip()]
            if names and (not disabled or len(names) > len(disabled)):
                disabled = names
            continue
        match = BUILTIN_SHELL_RE.search(line)
        if match:
            calls = py_list(match.group("calls"))
            if calls not in built_in_shell_calls:
                built_in_shell_calls.append(calls)
            continue
        if line.startswith("[proof] ev SessionEventType.PERMISSION_REQUESTED"):
            found = PERMISSION_REQUEST_RE.search(line)
            if found:
                record = {
                    "kind": found.group("kind"),
                    "fields": redact(found.group("fields")),
                }
                for existing in permission_requests:
                    if (
                        existing["kind"] == record["kind"]
                        and existing["fields"] == record["fields"]
                    ):
                        existing["event_line_count"] += 1
                        break
                else:
                    record["event_line_count"] = 1
                    permission_requests.append(record)
            continue
        if line.startswith("[proof] ev SessionEventType.PERMISSION_COMPLETED"):
            found = PERMISSION_RESULT_RE.search(line)
            if found:
                record = {
                    "kind": f"Permission{found.group('kind')}",
                    "fields": redact(found.group("fields")),
                }
                for existing in permission_results:
                    if (
                        existing["kind"] == record["kind"]
                        and existing["fields"] == record["fields"]
                    ):
                        existing["event_line_count"] += 1
                        break
                else:
                    record["event_line_count"] = 1
                    permission_results.append(record)
            continue

        body = line[len("[proof] ") :]
        key, _, value = body.partition("=")
        if key not in SUMMARY_KEYS or not turns:
            continue
        turn = turns[-1]
        if key in {"raw_event_count", "denied_like_events"}:
            suffix = (
                "raw_event_count" if key == "raw_event_count" else "denied_like_events"
            )
            turn[f"{suffix}_cum"] = int(value)
        elif key == "handler_calls":
            turn["handler_calls_cum"] = py_list(value)
            turn["handler_call_count_cum"] = len(turn["handler_calls_cum"])
        elif key == "tool_outcomes":
            outcomes = [list(item) for item in py_list(value)]
            turn["tool_outcomes_cum"] = outcomes
            turn["tool_outcome_successes_cum"] = sum(
                1 for item in outcomes if item[-1] is True
            )
            turn["tool_outcome_failures_cum"] = sum(
                1 for item in outcomes if item[-1] is False
            )
        elif key == "permission_events":
            turn["permission_wire_types_cum"] = [
                v.strip() for v in value.split(",") if v.strip()
            ]
        elif key == "wire_types_unmapped_count":
            count_text, _, names_text = value.partition(" names=")
            turn["wire_types_unmapped_count_cum"] = int(count_text)
            turn["wire_types_unmapped_cum"] = counts(names_text)
        elif key == "sdk_types":
            turn["sdk_event_types_cum"] = counts(value)
        elif key == "wire_types_mapped":
            turn["wire_types_mapped_cum"] = counts(value)

    derive_wire_type_stats(turns)
    derive_turn_counts(turns)

    unmapped_union: Counter[str] = Counter()
    for turn in turns:
        unmapped_union.update(turn.get("wire_types_unmapped_cum", {}))

    record = {
        "log": path.name,
        **head,
        "result_ok": ok,
        "skills": skills,
        "session_tool_names": session_tools or [],
        "disabled_tools": sorted(disabled),
        "disabled_tools_count": len(disabled),
        "built_in_shell_call_requests": built_in_shell_calls,
        "permission_requests": permission_requests,
        "permission_results": permission_results,
        "turns": turns,
        "final_counts": final,
        "unmapped_wire_types_union": dict(sorted(unmapped_union.items())),
        "unmapped_wire_type_names_union": len(unmapped_union),
    }
    derive_case_counts(record)
    return record


def evaluate_gate(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """live proof の registration boundary を gate として検証する。"""
    checks: list[dict[str, Any]] = []

    def check(case: dict[str, Any], name: str, ok: bool, detail: str = "") -> None:
        checks.append(
            {
                "case": case.get("tag", case["log"]),
                "tool_case": case.get("tool_case", ""),
                "check": name,
                "ok": bool(ok),
                "detail": detail,
            }
        )

    for case in cases:
        derived = case["derived"]
        tool_case = case.get("tool_case")
        check(
            case,
            "result_ok",
            case.get("result_ok") is True,
            repr(case.get("result_ok")),
        )
        check(
            case,
            "events_balanced",
            derived["events_balanced_total"],
            f"raw={derived['raw_event_count_total']} mapped+unmapped で一致",
        )
        check(
            case,
            "model_requested_tool",
            derived["requested_tool_names"] != [],
            ", ".join(derived["requested_tool_names"]),
        )
        check(
            case,
            "builtin_tools_not_executed",
            derived["builtins_executed"] == []
            and derived["built_in_shell_call_requests_total"] == 0,
            f"builtins_executed={derived['builtins_executed']}",
        )
        check(
            case,
            "shell_tools_disabled",
            len(derived["shell_tools_disabled"]) >= 3,
            ", ".join(derived["shell_tools_disabled"]),
        )

        if tool_case == "custom":
            check(
                case,
                "custom_tool_registered",
                case.get("registered_custom_tools") == 1,
                repr(case.get("registered_custom_tools")),
            )
            check(
                case,
                "handler_executed",
                derived["handler_call_count_total"] >= 1,
                f"handler_call_count_total={derived['handler_call_count_total']}",
            )
            check(
                case,
                "custom_tool_outcome_success",
                derived["tool_outcome_successes"] >= 1,
                json.dumps(derived["tool_outcomes"], ensure_ascii=False),
            )
            check(
                case,
                "execution_start_and_complete",
                derived["tool_execution_starts_total"] >= 1
                and derived["tool_execution_completions_total"] >= 1,
                f"start={derived['tool_execution_starts_total']} "
                f"complete={derived['tool_execution_completions_total']}",
            )
            check(
                case,
                "permission_pair_observed",
                derived["permission_pair_count_total"] >= 1,
                f"pair={derived['permission_pair_count_total']}",
            )
        elif tool_case == "none":
            check(
                case,
                "custom_tool_not_registered",
                case.get("registered_custom_tools") == 0,
                repr(case.get("registered_custom_tools")),
            )
            check(
                case,
                "handler_not_executed",
                derived["handler_call_count_total"] == 0,
                f"handler_call_count_total={derived['handler_call_count_total']}",
            )
            check(
                case,
                "custom_tool_outcome_failure",
                derived["tool_outcome_failures"] >= 1,
                json.dumps(derived["tool_outcomes"], ensure_ascii=False),
            )
            check(
                case,
                "permission_pair_absent",
                derived["permission_pair_count_total"] == 0,
                f"pair={derived['permission_pair_count_total']}",
            )
        else:
            check(case, "known_tool_case", False, f"tool_case={tool_case!r}")

        check(
            case,
            "tool_registration_count_matches_session_tools",
            int(case.get("registered_custom_tools") or 0)
            == len(case.get("session_tool_names", [])),
            f"registered_custom_tools={case.get('registered_custom_tools')!r} "
            f"session_tools={case.get('session_tool_names')!r}",
        )

    check(
        {
            "log": "<two-cases>",
            "tag": "registered+unregistered",
            "tool_case": "custom+none",
        },
        "tool_cases_distinct",
        len({str(case.get("tool_case")) for case in cases}) == 2
        and len({str(case.get("tag")) for case in cases}) == 2,
        "tool_case / tag が 2 case (custom, none) で区別されている",
    )
    return checks


def emit_gate_checks(
    gate_checks: list[dict[str, Any]], failures: list[dict[str, Any]]
) -> None:
    """gate检查结果を stderr へ PASS / FAIL 行として出力する."""
    for item in gate_checks:
        status = "PASS" if item["ok"] else "FAIL"
        print(
            f"[gate] {status} {item['case']}/{item['tool_case']} {item['check']}"
            + (f" :: {item['detail']}" if not item["ok"] else ""),
            file=sys.stderr,
        )
    if failures:
        print(f"[gate] FAIL {len(failures)} 件", file=sys.stderr)
    else:
        print(f"[gate] {GATE_NAME} PASS ({len(gate_checks)} checks)", file=sys.stderr)


def recheck_evidence(path: Path) -> int:
    """保存済み evidence JSON の registration boundary gate を再検証する."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    gate_checks = evaluate_gate(cases)
    failures = [item for item in gate_checks if not item["ok"]]
    emit_gate_checks(gate_checks, failures)
    print(
        json.dumps(
            {
                "gate": GATE_NAME,
                "gate_result": "PASS" if not failures else "FAIL",
                "checks": len(gate_checks),
                "failures": len(failures),
                "evidence": str(path),
                "tool_cases": sorted(str(case.get("tool_case")) for case in cases),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="proof log から 2 case 実測を JSON へ集計する"
    )
    parser.add_argument("logs", nargs="*", type=Path, help="解析する proof log")
    parser.add_argument(
        "--recheck-evidence",
        type=Path,
        metavar="EVIDENCE_JSON",
        help="保存済み evidence JSON の gate だけを再検証する (log 不要)",
    )
    parser.add_argument("--output", type=Path, help="出力 JSON path")
    parser.add_argument(
        "--redact-base-url", action="store_true", help="base_url を <redacted> 化する"
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="registration boundary を検証し失敗で exit 1",
    )
    args = parser.parse_args(argv)

    if args.recheck_evidence:
        return recheck_evidence(args.recheck_evidence)

    cases = [parse_proof_log(path) for path in args.logs]
    gate_checks = evaluate_gate(cases)
    gate_failures = [item for item in gate_checks if not item["ok"]]
    if args.redact_base_url:
        for case in cases:
            case["base_url"] = "<redacted>"
    payload = {
        "gate": GATE_NAME,
        "schema_version": SCHEMA_VERSION,
        "note": "proof log (git 対象外) からの実測値。live proof 再実行で再生成する。",
        "cases": cases,
        "gate_checks": gate_checks,
        "gate_result": "PASS" if not gate_failures else "FAIL",
        "gate_failure_count": len(gate_failures),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)

    if args.gate:
        for item in gate_checks:
            status = "PASS" if item["ok"] else "FAIL"
            print(
                f"[gate] {status} {item['case']}/{item['tool_case']} {item['check']}"
                + (f" :: {item['detail']}" if not item["ok"] else ""),
                file=sys.stderr,
            )
        if gate_failures:
            print(f"[gate] FAIL {len(gate_failures)} 件", file=sys.stderr)
            return 1
        print(
            f"[gate] live-proof-two-cases PASS ({len(gate_checks)} checks)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
