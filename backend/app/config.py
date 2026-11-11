"""アプリケーション設定.

仕様書 §7.3 に従い、LLM プロバイダーを環境変数で切り替える。
認証情報は設定ファイルに書かず、環境変数のみで受け取る (§11.2)。
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

# Phase 0 実測結果: BYOK プロバイダー指定時は GitHub 認証が不要。
# したがって github_token は none に初期化し、明示設定時のみ使う。
GITHUB_MCP_SERVER_NAME = "github-mcp-server"

# 1.0.29 では session.idle がターン完了後にしか来ないため、ターン終了まで
# 待ってよい上限を明示する (README §7.2 の 180 秒を採用)。
DEFAULT_TURN_TIMEOUT_SECONDS = 180


class ProviderKind(StrEnum):
    """対応する LLM プロバイダー種別 (§7.1)."""

    COPILOT = "copilot"
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


#: Copilot SDK ProviderConfig が受け付ける API 形式 (§16.5)
BYOK_PROVIDER_TYPES: dict[ProviderKind, str] = {
    ProviderKind.OLLAMA: "openai",
    ProviderKind.OPENAI: "openai",
    ProviderKind.ANTHROPIC: "anthropic",
}


class Settings(BaseModel, frozen=True):
    """実行時設定。`.env` または環境変数から構築する (§7.3)。"""

    provider: ProviderKind = ProviderKind.OLLAMA
    model: str | None = None

    # --- BYOK プロバイダー接続先 ---
    ollama_base_url: str = "http://host.docker.internal:11434/v1"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com"
    api_key: str | None = None

    # --- アプリケーションデータ配置 ---
    data_dir: Path = Path("data")
    skills_dir: Path = Path("skills")
    runtime_dir: Path = Path("data/copilot-runtime")

    # --- Copilot SDK 実行設定 ---
    log_level: str = "info"
    session_idle_timeout_seconds: int = 900
    turn_timeout_seconds: int = DEFAULT_TURN_TIMEOUT_SECONDS
    disabled_mcp_servers: tuple[str, ...] = (GITHUB_MCP_SERVER_NAME,)
    enable_skills: bool = True
    disabled_skills: tuple[str, ...] = ()
    session_store_enabled: bool = False

    # --- HTTP サーバー ---
    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("session_idle_timeout_seconds", "turn_timeout_seconds")
    @classmethod
    def _valid_timeout(cls, value: int) -> int:
        """1.0.29 で実際にターン完了まで待ち受ける秒数のため下限を設ける。"""
        if not 10 <= value <= 3600:
            raise ValueError("タイムアウトは 10〜3600 秒の範囲で設定する")
        return value

    @field_validator("port")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("PORT は 1〜65535 の範囲で設定する")
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"error", "warning", "info", "debug"}
        if value not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return value

    @property
    def rules_dir(self) -> Path:
        """ルールの正本を置くディレクトリ (§4.1)。"""
        return self.data_dir / "rules"

    @property
    def database_path(self) -> Path:
        """検索用インデックス・承認状態を保持する SQLite (§4.1)。"""
        return self.data_dir / "household.db"

    @property
    def byok(self) -> bool:
        """Copilot クラウドではなく BYOK で model を解決するか。"""
        return self.provider is not ProviderKind.COPILOT

    @property
    def byok_provider_type(self) -> str | None:
        """SDK の ProviderConfig.type。BYOK でなければ None。"""
        return BYOK_PROVIDER_TYPES.get(self.provider)

    @property
    def byok_base_url(self) -> str | None:
        """SDK の ProviderConfig.base_url。BYOK でなければ None。"""
        if self.provider is ProviderKind.OLLAMA:
            return self.ollama_base_url
        if self.provider is ProviderKind.OPENAI:
            return self.openai_base_url
        if self.provider is ProviderKind.ANTHROPIC:
            return self.anthropic_base_url
        return None


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    """整数環境変数を安全に読む (不正値は設定エラーとして報告する)。"""
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} は整数で設定してください (受け取った値: {raw!r})"
        ) from exc


def _env_list(name: str) -> tuple[str, ...]:
    """カンマ区切りの環境変数を名前のタプルへ変換する。"""
    raw = _env_opt(name)
    if raw is None:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_opt(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"


def load_settings(repo_root: Path | None = None) -> Settings:
    """環境変数と `.env` から設定を構築する。

    `.env` が無い場合でも例外にしない (Phase 0 では Ollama 既定で動く)。
    """
    if repo_root is not None:
        load_dotenv(repo_root / ".env")

    root = repo_root if repo_root is not None else Path.cwd()
    provider = ProviderKind(_env("AI_PROVIDER", "ollama"))
    truthy = {"1", "true", "on", "yes"}

    return Settings(
        provider=provider,
        model=_env_opt("AI_MODEL"),
        ollama_base_url=_env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL),
        openai_base_url=_env("OPENAI_BASE_URL", DEFAULT_OPENAI_URL),
        anthropic_base_url=_env("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_URL),
        api_key=_env_opt("AI_API_KEY"),
        data_dir=Path(_env("DATA_DIR", str(root / "data"))),
        skills_dir=Path(_env("SKILLS_DIR", str(root / "skills"))),
        runtime_dir=Path(
            _env("COPILOT_RUNTIME_DIR", str(root / "data" / "copilot-runtime"))
        ),
        log_level=_env("LOG_LEVEL", "info"),
        session_idle_timeout_seconds=_env_int("SESSION_IDLE_TIMEOUT_SECONDS", 900),
        # ターン完了まで待ってよい上限秒数 (§7.2) と skills 方針
        turn_timeout_seconds=_env_int(
            "AGENT_TURN_TIMEOUT_SECONDS", DEFAULT_TURN_TIMEOUT_SECONDS
        ),
        enable_skills=_env("SKILLS_ENABLED", "on").lower() in truthy,
        disabled_skills=_env_list("SKILLS_DISABLED"),
        session_store_enabled=_env("COPILOT_SESSION_STORE", "off").lower() in truthy,
        host=_env("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8000),
    )
