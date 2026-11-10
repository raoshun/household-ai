"""pinned SDK (`github-copilot-sdk==1.0.29`) の API 面を実測する probe (#1).

docs/phase0-capability-notes.md の根拠。API docs (v1.2.0-beta) は pinned version
より新しいため、docs に載る名前が 1.0.29 に実在するかここで確定させる (§18.12)。

実行:
  .venv/bin/python tests/proofs/sdk_api_probe.py      # host
  make probe                                         # image 内の .venv
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from typing import Any

import copilot
import copilot.tools as tools_mod
from copilot import SessionEventType
from copilot.session import CopilotSession

CLIENT_KWARGS = [
    "entra",
    "connection",
    "working_directory",
    "log_level",
    "env",
    "github_token",
    "base_directory",
    "builtin_plugin_directories",
    "extension_launch_provider",
    "installation_confirmation_handler",
    "kerberos_auth",
    "use_logged_in_user",
    "telemetry",
    "session_fs",
    "request_handler",
    "session_idle_timeout_seconds",
    "client_info",
    "on_list_models",
    "on_github_telemetry",
    "mode",
]

SESSION_KWARGS = [
    "available_tools",
    "approval_mode",
    "permission_handler",
    "base_tools",
    "tool_mode",
    "additional_tools",
    "enable_session_store",
    "infinite_sessions",
    "session_idle_timeout_seconds",
    "working_directory",
    "model",
    "provider",
    "settings",
    "on_permission_request",
    "excluded_tools",
    "context_tier",
    "agent",
    "streaming",
    "features",
    "system_message",
    "customInstructions",
    "disabled_mcp_servers",
    "skill_directories",
    "disabled_skills",
    "hooks",
    "session_limits",
    "infinite_sessions",
    "large_output",
]

TOOL_KWARGS = [
    "is_terminal",
    "overrides_built_in_tool",
    "skip_permission",
    "namespace",
    "defer",
    "handler",
    "parameters",
    "metadata",
]

TOOL_NAMES = [
    "Tool",
    "ToolResult",
    "ToolSet",
    "define_tool",
    "ToolHandler",
    "ToolInvocation",
    "ToolNamespace",
    "convert_mcp_call_tool_result",
]

PERMISSION_NAMES = [
    "PermissionHandler",
    "PermissionInvocation",
    "PermissionRequest",
    "PermissionRequestResult",
    "PermissionDecision",
    "ToolApprovalRequest",
]

RPC_TYPES = [
    "SessionApprovalMode",
    "CustomAgentMcpAgentMode",
    "PermissionRequestCustomTool",
    "PermissionApproved",
    "ToolExecutionStart",
    "ToolExecutionComplete",
    "ToolExecutionCompleteResult",
    "SessionSnapshot",
    "SkillInfo",
]

EVENT_KEYWORDS = ["error", "custom", "external", "system", "permission", "skill", "mcp"]


def _params(obj: Any) -> list[str]:
    try:
        return list(inspect.signature(obj).parameters)
    except (TypeError, ValueError):
        return []


def _fields(obj: Any) -> list[str]:
    if obj is None:
        return []
    try:
        return [field.name for field in dataclasses.fields(obj)]
    except TypeError:
        return _params(obj.__init__)


def _present(names: list[str], holder: Any) -> dict[str, bool]:
    return {name: getattr(holder, name, None) is not None for name in names}


def _wire_names(keyword: str) -> list[str]:
    wire = {str(event.value) for event in SessionEventType}
    return sorted(name for name in wire if keyword in name)


def main() -> None:
    client_kwargs = _params(copilot.CopilotClient.__init__)
    tool_kwargs = _params(copilot.Tool.__init__)
    define_tool = getattr(tools_mod, "define_tool", None)
    create_session_kwargs = _params(copilot.CopilotClient.create_session)

    surface: dict[str, Any] = {
        "sdk_version": getattr(copilot, "__version__", "unknown"),
        "client": {
            "init_kwargs": client_kwargs,
            "requested": {name: name in client_kwargs for name in CLIENT_KWARGS},
        },
        "create_session": {
            "owner": "copilot.CopilotClient.create_session",
            "kwargs": create_session_kwargs,
            "requested": {
                name: name in create_session_kwargs for name in SESSION_KWARGS
            },
        },
        "session_api": {
            "public_methods": sorted(
                name for name in dir(CopilotSession) if not name.startswith("_")
            ),
            "send_kwargs": _params(CopilotSession.send),
            "send_and_wait_kwargs": _params(CopilotSession.send_and_wait),
            "on_return": str(inspect.signature(CopilotSession.on).return_annotation),
        },
        "symbols": {
            "copilot": _present(TOOL_NAMES, copilot),
            "copilot_tools": _present(TOOL_NAMES, tools_mod),
            "permission_copilot": _present(PERMISSION_NAMES, copilot),
            "permission_session": _present(PERMISSION_NAMES, copilot.session),
            "rpc": _present(
                RPC_TYPES, __import__("copilot.generated.rpc", fromlist=["x"])
            ),
        },
        "tool_api": {
            "init_kwargs": tool_kwargs,
            "requested": {name: name in tool_kwargs for name in TOOL_KWARGS},
            "handler_annotation": str(getattr(tools_mod, "ToolHandler", None)),
            "invocation_fields": _present(
                [
                    "session_id",
                    "tool_call_id",
                    "tool_name",
                    "arguments",
                    "available_tools",
                    "agent_id",
                    "request_id",
                ],
                getattr(tools_mod, "ToolInvocation", None),
            ),
            "result_fields": _fields(copilot.ToolResult),
            "define_tool_signature": (
                str(inspect.signature(define_tool)) if define_tool else None
            ),
        },
        "events": {
            "event_fields": _fields(copilot.SessionEvent),
            "enum_members": len(list(SessionEventType)),
            "wire_names_by_keyword": {kw: _wire_names(kw) for kw in EVENT_KEYWORDS},
        },
    }
    print(json.dumps(surface, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
