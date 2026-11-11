"""live proof summary と registration boundary gate のUnitテスト。

fixtures は pinned runtime の proof log と同じ形式を持つ合成データであり、
実測ログの値・モデル名・内部 URL は含まない。
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proofs"))

from summarize_live_proof_logs import GATE_NAME, evaluate_gate, main

TOOL = "household_list_housework"
PERM_EVENTS = (
    "SessionEventType.PERMISSION_REQUESTED, SessionEventType.PERMISSION_COMPLETED"
)
GATE_CHECK_COUNT = 22
TOOL_CASE_REGISTERED = "custom"
TOOL_CASE_NONE = "none"
# gate が registered case で要求する effect 証拠の check 名
REGISTERED_EFFECT_CHECKS = (
    "custom_tool_registered",
    "handler_executed",
    "custom_tool_outcome_success",
    "execution_start_and_complete",
    "permission_pair_observed",
    "tool_registration_count_matches_session_tools",
)
# gate が unregistered case で要求する absent 証拠の check 名
UNREGISTERED_ABSENCE_CHECKS = (
    "custom_tool_not_registered",
    "handler_not_executed",
    "custom_tool_outcome_failure",
    "permission_pair_absent",
    "tool_registration_count_matches_session_tools",
)

REG_MAPPED1 = {
    "assistant.message": 10,
    "assistant.tool_call_request": 2,
    "tool.execution_start": 2,
    "tool.execution_complete": 2,
    "external_tool_call": 2,
    "permission.requested": 2,
    "permission.completed": 2,
    "session.status": 6,
    "user.send_message": 2,
}
REG_UNMAPPED1 = {
    "session.thinking_text_delta": 6,
    "agent.custom_tool_call_request": 4,
    "session_stats": 2,
}
REG_SDK1 = {
    "SessionEventType.ASSISTANT_MESSAGE": 10,
    "SessionEventType.ASSISTANT_TOOL_CALL_REQUEST": 2,
    "SessionEventType.TOOL_EXECUTION_START": 2,
    "SessionEventType.TOOL_EXECUTION_COMPLETE": 2,
    "SessionEventType.EXTERNAL_TOOL_CALL": 2,
    "SessionEventType.PERMISSION_REQUESTED": 2,
    "SessionEventType.PERMISSION_COMPLETED": 2,
    "SessionEventType.SESSION_STATUS": 6,
    "SessionEventType.USER_SEND_MESSAGE": 2,
    "SessionEventType.UNKNOWN_EVENT": 12,
}
REG_MAPPED2 = dict(REG_MAPPED1, **{"assistant.message": 14, "session.status": 8})
REG_UNMAPPED2 = dict(REG_UNMAPPED1, **{"session_stats": 4})
REG_SDK2 = {
    "SessionEventType.ASSISTANT_MESSAGE": 14,
    "SessionEventType.ASSISTANT_TOOL_CALL_REQUEST": 2,
    "SessionEventType.TOOL_EXECUTION_START": 3,
    "SessionEventType.TOOL_EXECUTION_COMPLETE": 3,
    "SessionEventType.EXTERNAL_TOOL_CALL": 2,
    "SessionEventType.PERMISSION_REQUESTED": 3,
    "SessionEventType.PERMISSION_COMPLETED": 3,
    "SessionEventType.SESSION_STATUS": 8,
    "SessionEventType.USER_SEND_MESSAGE": 2,
    "SessionEventType.UNKNOWN_EVENT": 14,
}
UNREG_MAPPED1 = {
    "assistant.message": 10,
    "assistant.tool_call_request": 2,
    "session.status": 6,
    "user.send_message": 2,
    "session.error": 2,
    "model.turn_failed": 2,
}
UNREG_UNMAPPED1 = {
    "session.thinking_text_delta": 3,
    "agent.custom_tool_call_request": 2,
    "session_idle": 1,
}
UNREG_SDK1 = {
    "SessionEventType.ASSISTANT_MESSAGE": 10,
    "SessionEventType.ASSISTANT_TOOL_CALL_REQUEST": 2,
    "SessionEventType.SESSION_STATUS": 6,
    "SessionEventType.USER_SEND_MESSAGE": 2,
    "SessionEventType.SESSION_ERROR": 2,
    "SessionEventType.MODEL_TURN_FAILED": 2,
    "SessionEventType.UNKNOWN_EVENT": 6,
}
UNREG_MAPPED2 = dict(
    UNREG_MAPPED1,
    **{
        "assistant.message": 14,
        "session.status": 10,
        "session.error": 4,
        "model.turn_failed": 4,
    },
)
UNREG_SDK2 = {
    "SessionEventType.ASSISTANT_MESSAGE": 14,
    "SessionEventType.ASSISTANT_TOOL_CALL_REQUEST": 2,
    "SessionEventType.SESSION_STATUS": 10,
    "SessionEventType.USER_SEND_MESSAGE": 2,
    "SessionEventType.SESSION_ERROR": 4,
    "SessionEventType.MODEL_TURN_FAILED": 4,
    "SessionEventType.UNKNOWN_EVENT": 6,
}


def _pairs(values: dict[str, int]) -> str:
    return ", ".join(f"{name}:{count}" for name, count in values.items())


def _head(tag: str, policy: str, tool_case: str, registered: int) -> str:
    return (
        f"[proof] [{tag}] policy={policy} tool_case={tool_case} "
        f"registered_custom_tools={registered}"
    )


def _provider(tag: str) -> str:
    return (
        f"[proof] [{tag}] provider=openai/2 model=test-model "
        "base_url=http://127.0.0.1:9/v1 skills=True"
    )


def _turn(
    turn: int,
    aborted: bool,
    tool_calls: list[str],
    events: int,
    text: str,
) -> list[str]:
    return [
        (
            f"[proof] turn{turn} aborted={aborted} "
            f"tool_calls={tool_calls!r} events={events}"
        ),
        f"[proof] turn{turn} text={text!r}",
    ]


def _cum(
    mapped: dict[str, int],
    unmapped: dict[str, int],
    sdk: dict[str, int],
    handlers: list[dict[str, Any]],
    outcomes: list[tuple[str, bool]],
    permission_events: str,
    denied: int = 0,
) -> list[str]:
    raw = sum(mapped.values()) + sum(unmapped.values())
    return [
        f"[proof] raw_event_count={raw}",
        f"[proof] sdk_types={_pairs(sdk)}",
        f"[proof] wire_types_mapped={_pairs(mapped)}",
        (
            f"[proof] wire_types_unmapped_count={sum(unmapped.values())} "
            f"names={_pairs(unmapped)}"
        ),
        f"[proof] permission_events={permission_events}",
        f"[proof] denied_like_events={denied}",
        f"[proof] handler_calls={handlers!r}",
        f"[proof] tool_outcomes={outcomes!r}",
    ]


def _final(
    tag: str,
    tool_case: str,
    registered: int,
    requested: list[str],
    handler_calls: int,
    completed: list[tuple[str, bool]],
    rejected: list[tuple[str, str]],
) -> str:
    return (
        f"[proof] [{tag}] tool_case={tool_case} "
        f"registered_custom_tools={registered} "
        f"requested_tools={requested!r} executed_tools={requested!r} "
        f"handler_calls={handler_calls} custom_completed={completed!r} "
        f"custom_rejected={rejected!r} builtins_executed=[]"
    )


DISABLED_TOOLS = (
    "bash",
    "list_bash",
    "read_bash",
    "stop_bash",
    "list_bash",
    "write_agent",
)
DISABLED_LINE = f"[proof] Disabled tools: {', '.join(DISABLED_TOOLS)}"
SESSION_TOOLS_UNREGISTERED = "[proof] tool_names=[]"

SESSION_TOOLS_REGISTERED = f"[proof] tool_names={[TOOL]!r}"
REGISTERED_LINES = [
    _head("registered", "registered", "custom", 1),
    _provider("registered"),
    SESSION_TOOLS_REGISTERED,
    "[proof] skills SkillInfo(name='household-advisor', source='Repo')",
    DISABLED_LINE,
    "[proof] built-in_shell_calls=[]",
    (
        "[proof] ev SessionEventType.PERMISSION_REQUESTED "
        "PermissionRequestPreferToolUse(tool_name='household_list_housework')"
    ),
    (
        "[proof] ev SessionEventType.PERMISSION_COMPLETED "
        "PermissionApproved(managed_approval_handled=True)"
    ),
    *_turn(1, True, [TOOL], 40, "合成応答 1"),
    *_cum(
        REG_MAPPED1,
        REG_UNMAPPED1,
        REG_SDK1,
        [{"tool": TOOL, "arguments": {}}],
        [(TOOL, True)],
        PERM_EVENTS,
    ),
    *_turn(2, False, [], 14, "合成応答 2"),
    *_cum(
        REG_MAPPED2,
        REG_UNMAPPED2,
        REG_SDK2,
        [{"tool": TOOL, "arguments": {}}, {"tool": TOOL, "arguments": {}}],
        [(TOOL, True), (TOOL, True)],
        PERM_EVENTS,
    ),
    _final("registered", "custom", 1, [TOOL], 2, [(TOOL, True), (TOOL, True)], []),
    "[proof] RESULT [registered] ok=True",
]

UNREGISTERED_LINES = [
    _head("unregistered", "unregistered", "none", 0),
    _provider("unregistered"),
    SESSION_TOOLS_UNREGISTERED,
    "[proof] skills SkillInfo(name='household-advisor', source='Repo')",
    DISABLED_LINE,
    "[proof] built-in_shell_calls=[]",
    (
        "[proof] ev SessionEventType.PERMISSION_DENIED "
        "PermissionDenied(message='Unknown tool')"
    ),
    *_turn(1, False, [TOOL], 30, "合成応答 1"),
    *_cum(
        UNREG_MAPPED1,
        UNREG_UNMAPPED1,
        UNREG_SDK1,
        [],
        [(TOOL, False)],
        "SessionEventType.PERMISSION_DENIED",
        1,
    ),
    *_turn(2, False, [], 12, "合成応答 2"),
    *_cum(
        UNREG_MAPPED2,
        UNREG_UNMAPPED1,
        UNREG_SDK2,
        [],
        [(TOOL, False), (TOOL, False)],
        "SessionEventType.PERMISSION_DENIED",
        2,
    ),
    _final("unregistered", "none", 0, [TOOL], 0, [], [(TOOL, "Unknown tool")]),
    "[proof] RESULT [unregistered] ok=True",
]


def _write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _evidence(
    tmp_path: Path, lines_by_name: dict[str, list[str]] | None = None
) -> Path:
    logs = lines_by_name or {
        "registered.log": REGISTERED_LINES,
        "unregistered.log": UNREGISTERED_LINES,
    }
    args = [str(_write(tmp_path, name, lines)) for name, lines in logs.items()]
    out = tmp_path / "evidence.json"
    rc = main([*args, "--gate", "--output", str(out)])
    assert rc == 0, "fixtures は gate を通過する前提"
    return out


def _failures(checks: list[dict[str, Any]]) -> list[str]:
    """FAIL になった gate check のラベル (case/tool_case check) を列挙する."""
    return [
        f"{item['case']}/{item['tool_case']} {item['check']}"
        for item in checks
        if not item["ok"]
    ]


DISTINCT_CHECK = "registered+unregistered/custom+none tool_cases_distinct"


@pytest.fixture
def evidence(tmp_path: Path) -> dict[str, Any]:
    return json.loads(_evidence(tmp_path).read_text(encoding="utf-8"))


@pytest.fixture
def cases(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return evidence["cases"]


def _log_paths(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _write(tmp_path, "registered.log", REGISTERED_LINES),
        _write(tmp_path, "unregistered.log", UNREGISTERED_LINES),
    )


def _tamper(
    cases: list[dict[str, Any]],
    case_idx: int,
    keys: tuple[str, ...],
    values: tuple[Any, ...],
    level: str = "derived",
) -> list[dict[str, Any]]:
    """gate 対象 case を 1 箇所だけ変化させた copy を返す (生 log は触らない)."""
    tampered = copy.deepcopy(cases)
    target = tampered[case_idx][level] if level == "derived" else tampered[case_idx]
    for key, value in zip(keys, values, strict=True):
        target[key] = value
    return tampered


def _labels(cases: list[dict[str, Any]], case_tag: str) -> set[str]:
    return {
        str(item["check"]) for item in evaluate_gate(cases) if item["case"] == case_tag
    }


def test_registered_fixture_obey_registration_policy(
    cases: list[dict[str, Any]],
) -> None:
    registered = cases[0]
    assert registered["tag"] == "registered"
    assert registered["tool_case"] == TOOL_CASE_REGISTERED
    assert registered["tool_policy_label"] == "registered"
    assert registered["registered_custom_tools"] == 1
    assert registered["session_tool_names"] == [TOOL]
    assert registered["result_ok"] is True
    assert registered["derived"]["turn_count"] == 2
    assert len(registered["turns"]) == 2


def test_unregistered_fixture_obey_registration_policy(
    cases: list[dict[str, Any]],
) -> None:
    unregistered = cases[1]
    assert unregistered["tag"] == "unregistered"
    assert unregistered["tool_case"] == TOOL_CASE_NONE
    assert unregistered["tool_policy_label"] == "unregistered"
    assert unregistered["registered_custom_tools"] == 0
    assert unregistered["session_tool_names"] == []
    assert unregistered["result_ok"] is True
    assert unregistered["derived"]["turn_count"] == 2
    assert len(unregistered["turns"]) == 2


def test_registered_case_records_privileged_effects(
    cases: list[dict[str, Any]],
) -> None:
    derived = cases[0]["derived"]
    assert derived["handler_call_count_total"] == 2
    assert derived["tool_execution_starts_total"] == 2
    assert derived["tool_execution_completions_total"] == 2
    assert derived["tool_outcome_successes"] == 2
    assert derived["tool_outcome_failures"] == 0
    assert derived["permission_pair_count_total"] == 2
    assert derived["permission_requested_events_total"] == 2
    assert derived["permission_completed_events_total"] == 2
    assert derived["requested_tool_names"] == [TOOL]
    assert derived["builtins_executed"] == []
    assert derived["built_in_shell_call_requests_total"] == 0


def test_unregistered_case_records_no_privileged_effects(
    cases: list[dict[str, Any]],
) -> None:
    derived = cases[1]["derived"]
    assert derived["handler_call_count_total"] == 0
    assert derived["tool_execution_starts_total"] == 0
    assert derived["tool_execution_completions_total"] == 0
    assert derived["tool_outcome_successes"] == 0
    assert derived["tool_outcome_failures"] == 2
    assert derived["permission_pair_count_total"] == 0
    assert derived["external_tool_events_total"] == 0
    assert derived["requested_tool_names"] == [TOOL]
    assert derived["builtins_executed"] == []
    assert derived["built_in_shell_call_requests_total"] == 0


def test_both_cases_keep_shell_tools_disabled(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        disabled = set(case["disabled_tools"])
        derived = case["derived"]
        assert {
            "bash",
            "list_bash",
            "read_bash",
            "stop_bash",
            "write_agent",
        } <= disabled
        assert derived["shell_tools_disabled"]
        assert derived["disabled_tool_names_count"] == 6


def test_both_cases_tally_wire_type_names(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        derived = case["derived"]
        assert derived["events_balanced_total"] is True
        assert derived["raw_event_count_total"] > 0
        assert derived["wire_type_names_mapped_total"] > 0
        assert derived["wire_type_names_unmapped_total"] > 0
        assert derived["sdk_type_names_total"] > 0
        assert derived["sdk_unknown_events_total"] > 0
        assert derived["turns_with_text"] == 2


def test_registered_wire_union_keeps_execution_and_permission_events(
    cases: list[dict[str, Any]],
) -> None:
    union = set(cases[0]["derived"]["wire_types_union"])
    assert {"tool.execution_start", "tool.execution_complete"} <= union
    assert {"permission.requested", "permission.completed"} <= union


def test_unregistered_wire_union_has_no_execution_or_permission_events(
    cases: list[dict[str, Any]],
) -> None:
    case = cases[1]
    derived = case["derived"]
    union = set(derived["wire_types_union"])
    unmapped = set(case["unmapped_wire_types_union"])
    assert "tool.execution_start" not in union
    assert "permission.requested" not in union
    assert "model.turn_failed" in union
    assert not {"tool.execution_start", "permission.requested"} & unmapped


def test_gate_passes_on_the_two_fixture_cases(evidence: dict[str, Any]) -> None:
    assert evidence["gate"] == GATE_NAME
    assert evidence["gate_result"] == "PASS"
    assert len(evidence["gate_checks"]) == GATE_CHECK_COUNT
    assert evidence["gate_failure_count"] == 0
    tool_cases = Counter(item["tool_case"] for item in evidence["gate_checks"])
    assert dict(tool_cases) == {
        TOOL_CASE_REGISTERED: 11,
        TOOL_CASE_NONE: 10,
        "custom+none": 1,
    }
    assert {item["case"] for item in evidence["gate_checks"]} == {
        "registered",
        "unregistered",
        "registered+unregistered",
    }


def test_gate_requires_effect_checks_for_registered_case(
    cases: list[dict[str, Any]],
) -> None:
    assert set(REGISTERED_EFFECT_CHECKS) <= _labels(cases, "registered")


def test_gate_requires_absence_checks_for_unregistered_case(
    cases: list[dict[str, Any]],
) -> None:
    assert set(UNREGISTERED_ABSENCE_CHECKS) <= _labels(cases, "unregistered")


# (ケース添字, derived キー組, 注入値, FAIL になる check 名)
REGISTERED_TAMPER: list[tuple[int, tuple[str, ...], tuple[Any, ...], str, str]] = [
    (0, ("handler_call_count_total",), (0,), "handler_executed", "derived"),
    (
        0,
        ("tool_execution_starts_total", "tool_execution_completions_total"),
        (0, 0),
        "execution_start_and_complete",
        "derived",
    ),
    (
        0,
        ("tool_outcome_successes", "tool_outcome_failures"),
        (0, 1),
        "custom_tool_outcome_success",
        "derived",
    ),
    (0, ("permission_pair_count_total",), (0,), "permission_pair_observed", "derived"),
    (
        0,
        ("session_tool_names",),
        ([],),
        "tool_registration_count_matches_session_tools",
        "case",
    ),
    (0, ("shell_tools_disabled",), ([],), "shell_tools_disabled", "derived"),
]

UNREGISTERED_TAMPER: list[tuple[int, tuple[str, ...], tuple[Any, ...], str, str]] = [
    (1, ("handler_call_count_total",), (1,), "handler_not_executed", "derived"),
    (
        1,
        ("tool_outcome_successes", "tool_outcome_failures"),
        (1, 0),
        "custom_tool_outcome_failure",
        "derived",
    ),
    (1, ("permission_pair_count_total",), (1,), "permission_pair_absent", "derived"),
]


@pytest.mark.parametrize(
    ("case_idx", "keys", "values", "expected", "level"), REGISTERED_TAMPER
)
def test_registered_case_effect_evidence_is_required(
    cases: list[dict[str, Any]],
    case_idx: int,
    keys: tuple[str, ...],
    values: tuple[Any, ...],
    expected: str,
    level: str,
) -> None:
    tampered = _tamper(cases, case_idx, keys, values, level=level)
    failures = _failures(evaluate_gate(tampered))
    assert failures == [f"registered/{TOOL_CASE_REGISTERED} {expected}"], failures


@pytest.mark.parametrize(
    ("case_idx", "keys", "values", "expected", "level"), UNREGISTERED_TAMPER
)
def test_unregistered_case_absence_evidence_is_required(
    cases: list[dict[str, Any]],
    case_idx: int,
    keys: tuple[str, ...],
    values: tuple[Any, ...],
    expected: str,
    level: str,
) -> None:
    tampered = _tamper(cases, case_idx, keys, values, level=level)
    failures = _failures(evaluate_gate(tampered))
    assert failures == [f"unregistered/{TOOL_CASE_NONE} {expected}"], failures


def test_registered_custom_tool_flag_cannot_be_flipped(
    cases: list[dict[str, Any]],
) -> None:
    tampered = _tamper(cases, 0, ("registered_custom_tools",), (0,), level="case")
    failures = set(_failures(evaluate_gate(tampered)))
    assert "registered/custom custom_tool_registered" in failures
    assert "registered/custom tool_registration_count_matches_session_tools" in failures


def test_unregistered_custom_tool_flag_cannot_be_flipped(
    cases: list[dict[str, Any]],
) -> None:
    tampered = _tamper(cases, 1, ("registered_custom_tools",), (1,), level="case")
    failures = set(_failures(evaluate_gate(tampered)))
    assert "unregistered/none custom_tool_not_registered" in failures


def test_tool_case_collision_breaks_two_case_gate(cases: list[dict[str, Any]]) -> None:
    tampered = _tamper(cases, 1, ("tool_case",), (TOOL_CASE_REGISTERED,), level="case")
    failures = set(_failures(evaluate_gate(tampered)))
    # tool_case が policy ではなく claim を選ぶ: none 側が registered 相当で落ちる
    assert failures == {
        DISTINCT_CHECK,
        "unregistered/custom custom_tool_registered",
        "unregistered/custom handler_executed",
        "unregistered/custom custom_tool_outcome_success",
        "unregistered/custom execution_start_and_complete",
        "unregistered/custom permission_pair_observed",
    }


def test_registered_case_cannot_claim_the_other_tool_case(
    cases: list[dict[str, Any]],
) -> None:
    tampered = _tamper(cases, 0, ("tool_case",), (TOOL_CASE_NONE,), level="case")
    labels = set(_failures(evaluate_gate(tampered)))
    assert labels == {
        DISTINCT_CHECK,
        "registered/none custom_tool_not_registered",
        "registered/none handler_not_executed",
        "registered/none custom_tool_outcome_failure",
        "registered/none permission_pair_absent",
    }


def test_single_case_evidence_fails_the_two_case_gate(tmp_path: Path) -> None:
    out = tmp_path / "single.json"
    registered_log = _write(tmp_path, "registered.log", REGISTERED_LINES)
    rc = main([str(registered_log), "--gate", "--output", str(out)])
    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate_result"] == "FAIL"
    assert payload["gate_failure_count"] >= 1
    assert [item["check"] for item in payload["gate_checks"] if not item["ok"]] == [
        "tool_cases_distinct"
    ]


def test_evidence_round_trip_keeps_turn_and_tool_counts(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"
    first, second = _log_paths(tmp_path)
    assert main([str(first), str(second), "--gate", "--output", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [Path(case["log"]).name for case in payload["cases"]] == [
        first.name,
        second.name,
    ]
    assert [case["tag"] for case in payload["cases"]] == ["registered", "unregistered"]
    assert [len(case["turns"]) for case in payload["cases"]] == [2, 2]
    assert [case["result_ok"] for case in payload["cases"]] == [True, True]
    assert payload["gate"] == GATE_NAME
    assert payload["schema_version"] == 1
    assert payload["cases"][0]["derived"]["tool_execution_completions_total"] == 2
    assert payload["cases"][1]["derived"]["tool_outcome_failures"] == 2


def test_recheck_evidence_accepts_written_evidence(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"
    assert (
        main([*[str(p) for p in _log_paths(tmp_path)], "--gate", "--output", str(out)])
        == 0
    )
    assert main(["--recheck-evidence", str(out)]) == 0


def test_recheck_evidence_rejects_tampered_evidence(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"
    assert (
        main([*[str(p) for p in _log_paths(tmp_path)], "--gate", "--output", str(out)])
        == 0
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["cases"][1]["derived"]["handler_call_count_total"] = 3
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert main(["--recheck-evidence", str(out)]) == 1


def test_base_url_redaction_removes_endpoint(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"
    argv = [
        *[str(p) for p in _log_paths(tmp_path)],
        "--gate",
        "--redact-base-url",
        "--output",
        str(out),
    ]
    assert main(argv) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {case["base_url"] for case in payload["cases"]} == {"<redacted>"}
