"""Settings と load_settings の意味論テスト (仕様 §4.3 / §7.3)。

enum を mixin へ移行したことによる振る舞い変化 (文字列として素朴に扱えること) と、
環境変数からの設定構築を固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from app.agent.provider import build_provider_config
from app.config import (
    DEFAULT_ANTHROPIC_URL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_OPENAI_URL,
    GITHUB_MCP_SERVER_NAME,
    ProviderKind,
    Settings,
    load_settings,
)

#: load_settings() が読み取る環境変数 (§7.3)
ENV_NAMES = (
    "AI_PROVIDER",
    "AI_MODEL",
    "AI_API_KEY",
    "OLLAMA_BASE_URL",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "DATA_DIR",
    "SKILLS_DIR",
    "COPILOT_RUNTIME_DIR",
    "LOG_LEVEL",
    "SESSION_IDLE_TIMEOUT_SECONDS",
    "HOST",
    "PORT",
    "AGENT_TURN_TIMEOUT_SECONDS",
    "SKILLS_ENABLED",
    "SKILLS_DISABLED",
)


def _clear_env(monkeypatch: MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_provider_kind_members_are_plain_strings() -> None:
    assert [str(kind) for kind in ProviderKind] == [
        "copilot",
        "ollama",
        "openai",
        "anthropic",
    ]
    assert ProviderKind("ollama") is ProviderKind.OLLAMA
    assert f"{ProviderKind.ANTHROPIC}" == "anthropic"


def test_ollama_defaults_connect_to_local_openai_compatible_api() -> None:
    settings = Settings()
    assert settings.provider is ProviderKind.OLLAMA
    assert settings.byok is True
    assert settings.byok_provider_type == "openai"
    assert type(settings.byok_provider_type) is str
    assert settings.byok_base_url == DEFAULT_OLLAMA_URL


def test_copilot_provider_uses_sdk_default_auth() -> None:
    settings = Settings(provider=ProviderKind.COPILOT)
    assert settings.byok is False
    assert settings.byok_provider_type is None
    assert settings.byok_base_url is None
    assert build_provider_config(settings) is None


def test_byok_provider_config_carries_base_url_and_key() -> None:
    settings = Settings(
        provider=ProviderKind.OPENAI,
        model="gpt-4o-mini",
        api_key="test-key",
        skills_dir=Path("skills"),
    )
    assert build_provider_config(settings) == {
        "type": "openai",
        "base_url": DEFAULT_OPENAI_URL,
        "api_key": "test-key",
    }
    assert Settings(provider=ProviderKind.ANTHROPIC).byok_base_url == (
        DEFAULT_ANTHROPIC_URL
    )


def test_ollama_provider_config_omits_api_key_when_absent() -> None:
    assert build_provider_config(Settings()) == {
        "type": "openai",
        "base_url": DEFAULT_OLLAMA_URL,
    }


def test_data_paths_are_derived_from_data_dir() -> None:
    settings = Settings(data_dir=Path("custom"))
    assert settings.rules_dir == Path("custom/rules")
    assert settings.database_path == Path("custom/household.db")
    assert settings.disabled_mcp_servers == (GITHUB_MCP_SERVER_NAME,)


def test_log_level_is_validated() -> None:
    assert Settings(log_level="debug").log_level == "debug"
    with pytest.raises(ValidationError):
        Settings(log_level="nope")


def test_load_settings_defaults(monkeypatch: MonkeyPatch, tmp_path) -> None:
    _clear_env(monkeypatch)
    settings = load_settings(tmp_path)
    assert settings.provider is ProviderKind.OLLAMA
    assert settings.model is None
    assert settings.api_key is None
    assert settings.data_dir == tmp_path / "data"
    assert settings.skills_dir == tmp_path / "skills"
    assert settings.runtime_dir == tmp_path / "data" / "copilot-runtime"
    assert settings.session_idle_timeout_seconds == 900
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.log_level == "info"
    assert settings.enable_skills is True
    assert settings.session_store_enabled is False


def test_load_settings_treats_empty_env_as_unset(monkeypatch: MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_MODEL", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    settings = load_settings(Path.cwd())
    assert settings.model is None
    assert settings.ollama_base_url == DEFAULT_OLLAMA_URL


def test_load_settings_reads_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("AI_API_KEY", "env-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.invalid:11434/v1")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_SECONDS", "1500")
    settings = load_settings(tmp_path)
    assert settings.provider is ProviderKind.OPENAI
    assert settings.model == "gpt-4o-mini"
    assert settings.api_key == "env-key"
    assert settings.session_idle_timeout_seconds == 1500


def test_load_settings_rejects_unknown_provider(monkeypatch: MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    with pytest.raises(ValueError, match="gemini"):
        load_settings(Path.cwd())


def test_agent_timeout_and_skills_are_configurable_by_env(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENT_TURN_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("SKILLS_ENABLED", "off")
    monkeypatch.setenv("SKILLS_DISABLED", "builtin:brainstorming, app-skill")

    settings = load_settings(tmp_path)

    assert settings.turn_timeout_seconds == 45
    assert settings.enable_skills is False
    assert settings.disabled_skills == ("builtin:brainstorming", "app-skill")


def test_bad_integer_env_becomes_setting_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_SECONDS", "soon")

    with pytest.raises(ValueError, match="SESSION_IDLE_TIMEOUT_SECONDS"):
        load_settings(tmp_path)


def test_timeout_and_port_ranges_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(turn_timeout_seconds=1)
    with pytest.raises(ValidationError):
        Settings(session_idle_timeout_seconds=3601)
    with pytest.raises(ValidationError):
        Settings(port=70_000)
