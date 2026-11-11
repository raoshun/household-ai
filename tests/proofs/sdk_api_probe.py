"""pinned SDK (`github-copilot-sdk==1.0.29`) の API 面を実測する probe (#1).

docs/capability-matrix.md の根拠。API docs (v1.2.0-beta) は pinned version より
docs/capability-matrix.md の根拠。API docs (v1.2.0-beta) は pinned version より
併せてここで確定させる (§18.12)。

実行:
  .venv/bin/python tests/proofs/sdk_api_probe.py      # host
  make probe                                         # image 内の .venv
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

import copilot
import copilot._cli_download as cli_dl
import copilot._cli_version as cli_ver
import copilot.session_events as events_mod
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
    # Phase 0 が使う kwargs
    "available_tools",
    "excluded_tools",
    "tools",
    "model",
    "provider",
    "working_directory",
    "system_message",
    "on_permission_request",
    "skill_directories",
    "disabled_skills",
    "disabled_mcp_servers",
    "enable_session_store",
    "enable_skills",
    "streaming",
    "session_limits",
    "large_output",
    "hooks",
    "agent",
    "context_tier",
    # 状態・sandbox・管理設定の制御点 (docs claim の実測)
    "sandbox_config",
    "managed_settings",
    "enable_managed_settings",
    "excluded_managed_settings",
    "enable_host_git_operations",
    "trust_working_directory",
    "session_id",
    "name",
    "reasoning_effort",
    "skill_provider",
    "included_builtin_skills",
    "custom_agents",
    "memory",
    "mcp_servers",
    "models",
    "providers",
    "feature_flags",
    "config_directory",
    "agent_directories",
    "instruction_directories",
    "plugin_directories",
    "session_store",
    "session_store_dir",
    "session_snapshot",
    "state_snapshot",
    "enable_session_telemetry",
    "skip_embedding_retrieval",
    "embedding_cache_storage",
    "mcp_oauth_token_storage",
    "custom_agents_local_only",
    "enable_config_discovery",
    "enable_on_demand_instruction_discovery",
    "enable_file_hooks",
    "enable_file_change_tracking",
    "skip_custom_instructions",
    "interactive_user",
    # docs site の claim (pinned に無いことを証明して禁止方針へ変換)
    "approval_mode",
    "permission_handler",
    "base_tools",
    "tool_mode",
    "additional_tools",
    "settings",
    "features",
    "customInstructions",
    "session_idle_timeout_seconds",
    "permission_mode",
    "env",
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
    "ToolApprovalRequest",
]

RPC_TYPES = [
    "SessionApprovalMode",
    "CustomAgentMcpAgentMode",
    "PermissionRequestCustomTool",
    "PermissionApproved",
    "PermissionRequestResult",
    "CustomToolsSettings",
    "CustomToolExecutionMode",
    "SessionStoreOption",
    "SessionStoreSnapshot",
    "SessionSnapshot",
    "SessionError",
    "ToolExecutionStart",
    "ToolExecutionComplete",
    "ToolExecutionCompleteResult",
    "SkillInfo",
]

WIRE_KEYWORDS = [
    "assistant",
    "custom",
    "error",
    "external",
    "mcp",
    "model",
    "permission",
    "session",
    "skill",
    "system",
    "tool",
    "turn",
    "user",
]

# API docs / spike notes が claim する名前と env 名が pinned package
# の source に実在するか (存在すれば文件名を返す)。
EVENT_PAYLOAD_NAMES = [
    "AssistantMessageData",
    "AssistantTurnStartData",
    "AssistantTurnEndData",
    "UserMessageData",
    "ToolExecutionStartData",
    "ToolExecutionCompleteData",
    "ExternalToolRequestedData",
    "ExternalToolCompletedData",
    "PermissionRequestedData",
    "PermissionCompletedData",
    "ModelCallStartData",
    "ModelCallFinalResultData",
    "SkillInvokedData",
    "SessionShutdownData",
    "SessionIdleData",
]

# docs claim の session store / state API 名。pinned 1.0.29 での実在を実測する。
STORE_SYMBOLS = [
    "LocalSessionStore",
    "InMemorySessionStore",
    "SessionStore",
    "SessionStoreLimits",
    "ResolvedSessionSnapshot",
    "SessionSnapshot",
    "ResumeSessionResult",
]

CLIENT_STORE_METHODS = [
    "resume_session",
    "list_sessions",
    "delete_session",
    "list_available_builtin_tools",
]

# pinned の download 機構が公開する API 名。
CLI_PATH_API = [
    "download_cli",
    "ensure_runtime_wrapper",
    "ensure_runtime_library",
    "get_cache_dir",
    "get_cached_cli_path",
    "get_or_download_cli",
]

GAP_TOKENS = [
    ".copilot-cli",
    "COPILOT_CACHE_HOME",
    "COPILOT_CLI_DOWNLOAD_BASE_URL",
    "COPILOT_CLI_EXTRACT_DIR",
    "COPILOT_CLI_PATH",
    "COPILOT_GITHUB_TOKEN",
    "COPILOT_HOME",
    "COPILOT_RUNTIME_DIR",
    "COPILOT_SKIP_CLI_DOWNLOAD",
    "CustomToolsSettings",
    "PermissionApproved",
    "PermissionRequestResult",
    "XDG_CACHE_HOME",
    "cli-path",
    "enable_session_store",
    "managed_settings",
    "session_events",
    "session_store",
    "session_snapshot",
    "skill",
]


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


def _env_tokens() -> list[str]:
    """pinned package の source が読む COPILOT_* env 名の実測。"""
    env_re = re.compile(r"COPILOT_[A-Z0-9_]+")
    root = Path(copilot.__file__).parent
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text() if path.is_file() else ""
        names.update(env_re.findall(text))
    return sorted(names)


def _package_grep(tokens: list[str]) -> dict[str, list[str]]:
    """package source 内の token 出現箇所 (docs claim 名の実測)。"""
    root = Path(copilot.__file__).parent
    sources: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            sources.append((path.relative_to(root).as_posix(), path.read_text()))
        except OSError:
            continue
    found: dict[str, list[str]] = {}
    for token in tokens:
        hits = [name for name, text in sources if token in text]
        found[token] = hits[:5]
    return found


def _runtime_paths() -> dict[str, Any]:
    """runtime cache layout を pinned の API から実測 (network アクセス無し)。

    絶対 path は環境依存なので `<cache-root>` へ mask し、相対 layout だけを
    記録する (host / container で byte 単位の突合可能にするため)。
    """
    version = cli_ver.CLI_VERSION
    root = cli_dl.get_cache_dir()
    version_dir = cli_dl.get_cache_dir(version)
    pair_dir = version_dir / "prebuilds" / cli_ver.get_runtime_platform()
    wrapper = cli_ver.get_cli_binary_name()
    wrapper_name = (
        "copilot-runtime.exe" if sys.platform == "win32" else "copilot-runtime"
    )
    cached = cli_dl.get_cached_cli_path()
    return {
        "api": _present(CLI_PATH_API, cli_dl),
        "platform_runtime": cli_ver.get_runtime_platform(),
        "root_dir_name": cli_dl._CACHE_DIR_NAME,
        "cache_root_masked": "<cache-root>/" + cli_dl._CACHE_DIR_NAME + "/cli",
        "cache_root_candidates": [
            "COPILOT_CLI_EXTRACT_DIR",
            "XDG_CACHE_HOME",
            "HOME/.cache",
        ],
        "version_dir_relative": version_dir.relative_to(root).as_posix(),
        "pair_dir_relative": pair_dir.relative_to(root).as_posix(),
        "compat_entrypoint_name": wrapper,
        "runtime_wrapper_name": wrapper_name,
        "runtime_library_name": "runtime.node",
        "hostless_assets_marker": cli_dl._HOSTLESS_ASSETS_MARKER,
        "compat_entrypoint_exists": (pair_dir / wrapper).is_file(),
        "runtime_wrapper_exists": (pair_dir / wrapper_name).is_file(),
        "runtime_library_exists": (pair_dir / "runtime.node").is_file(),
        "hostless_assets_marker_exists": (
            pair_dir / cli_dl._HOSTLESS_ASSETS_MARKER
        ).is_file(),
        "cached_cli_path_present": cached is not None,
        "bundle_complete": cli_dl._runtime_bundle_is_complete(pair_dir, wrapper),
        "env_knobs": {
            "extract_dir": "COPILOT_CLI_EXTRACT_DIR",
            "skip_download": "COPILOT_SKIP_CLI_DOWNLOAD",
            "download_base_url": "COPILOT_CLI_DOWNLOAD_BASE_URL",
            "cli_path": "COPILOT_CLI_PATH",
        },
    }


def _sdk_modules() -> list[str]:
    pkg_dir = Path(copilot.__file__).parent
    modules = [path.stem for path in sorted(pkg_dir.glob("*.py"))]
    modules += [path.name for path in sorted(pkg_dir.iterdir()) if path.is_dir()]
    return modules


def _session_store_api() -> dict[str, Any]:
    """docs claim の session store API が pinned にあるか実測 (§6/§15 対応)。"""
    rpc_module = sys.modules["copilot.generated.rpc"]
    modules = _sdk_modules()
    return {
        "sdk_modules": modules,
        "session_store_module_present": "session_store" in modules,
        "sdk_symbols": _present(STORE_SYMBOLS, copilot),
        "rpc_symbols": _present(STORE_SYMBOLS, rpc_module),
        "client_methods": {
            name: callable(getattr(copilot.CopilotClient, name, None))
            for name in CLIENT_STORE_METHODS
        },
        "session_methods": {
            name: hasattr(CopilotSession, name)
            for name in ("resume", "close", "dispose", "snapshot")
        },
        "create_session_kwargs": {
            name: name in _params(copilot.CopilotClient.create_session)
            for name in ("session_store", "session_store_dir", "session_snapshot")
        },
    }


def _runtime_cli() -> dict[str, Any]:
    """runtime version と download 先の実測 (docs claim と比較)。"""
    version = cli_ver.CLI_VERSION
    platform_key = cli_ver.get_platform_key()
    runtime_platform = cli_ver.get_runtime_platform()
    asset = cli_ver.get_release_asset_name(version, runtime_platform)
    return {
        "cli_version": version,
        "sdk_protocol_version_file": (
            Path(cli_ver.__file__).parent / "_sdk_protocol_version.py"
        ).is_file(),
        "platform_key": list(platform_key),
        "runtime_platform": runtime_platform,
        "runtime_platform_map": {
            f"{sys}-{arch}": name
            for (sys, arch), name in sorted(cli_ver.RUNTIME_PLATFORMS.items())
        },
        "release_asset": asset,
        "checksums_url": cli_ver.get_checksums_url(version),
        "download_url": cli_ver.get_download_url(version, asset),
        "binary_name": cli_ver.get_cli_binary_name(),
        "cache_dir_name": cli_dl._CACHE_DIR_NAME,
        "hostless_marker": cli_dl._HOSTLESS_ASSETS_MARKER,
        "download_functions": sorted(
            name for name in dir(cli_dl) if name.startswith("_fetch")
        ),
        "runtime_request_handler": sorted(
            name for name in dir(copilot) if "RequestHandler" in name
        ),
    }


def main() -> None:
    client_kwargs = _params(copilot.CopilotClient.__init__)
    tool_kwargs = _params(copilot.Tool.__init__)
    define_tool = getattr(tools_mod, "define_tool", None)
    create_session_kwargs = _params(copilot.CopilotClient.create_session)
    rpc_module = sys.modules["copilot.generated.rpc"]

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
            "rpc": _present(RPC_TYPES, rpc_module),
        },
        "rpc_types": _present(RPC_TYPES, rpc_module),
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
                    "session_store",
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
            "wire_name_total": len({str(e.value) for e in SessionEventType}),
            "unknown_member_present": hasattr(SessionEventType, "UNKNOWN"),
            "wire_names_all": sorted(str(event.value) for event in SessionEventType),
            "wire_names_by_keyword": {kw: _wire_names(kw) for kw in WIRE_KEYWORDS},
            "payload_module_members": len(
                [n for n in dir(events_mod) if not n.startswith("_")]
            ),
            "payload_types": _present(EVENT_PAYLOAD_NAMES, events_mod),
        },
        "runtime_cli": _runtime_cli(),
        "runtime_paths": _runtime_paths(),
        "session_store": _session_store_api(),
        "package_env_tokens": _env_tokens(),
        "docs_gap": _package_grep(GAP_TOKENS),
    }
    print(json.dumps(surface, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
