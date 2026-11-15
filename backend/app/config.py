"""アプリケーション設定.

仕様書 §7.3 に従い、LLM プロバイダーを環境変数で切り替える。
認証情報は設定ファイルに書かず、環境変数のみで受け取る (§11.2)。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Phase 0 実測結果: BYOK プロバイダー指定時は GitHub 認証が不要。
# したがって github_token は none に初期化し、明示設定時のみ使う。
GITHUB_MCP_SERVER_NAME = "github-mcp-server"

# 1.0.29 では session.idle がターン完了後にしか来ないため、ターン終了まで
# 待ってよい上限を明示する (README §7.2 の 180 秒を採用)。
DEFAULT_TURN_TIMEOUT_SECONDS = 180

# README §12.4 の approval timeout (5 分) より短い既定にする: human がその場に
# いる UI 検証では 2 分待てば十分で、待ちすぎは turn 側の timeout を圧迫する。
DEFAULT_PERMISSION_TIMEOUT_SECONDS = 120

# UI bridge (README §9.4 / §10.3 / 契約 §7) の replay・frames・heartbeat 既定値
DEFAULT_MONITOR_REPLAY_LIMIT = 200
DEFAULT_MAX_FRAMES_PER_THREAD = 400
DEFAULT_MONITOR_HEARTBEAT_SECONDS = 10.0
DEFAULT_MAX_CONCURRENT_TURNS = 4
DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD = 0.8
DEFAULT_CONTEXT_WARNING_COST_JPY = 20.0
DEFAULT_API_CORS_ORIGINS = ("http://127.0.0.1:3000", "http://localhost:3000")


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

    # --- HTTP サーバー / 動作確認 UI (§9 / §10 の API surface) ---
    host: str = "127.0.0.1"
    port: int = 8000
    api_cors_origins: list[str] = Field(
        default_factory=lambda: list(DEFAULT_API_CORS_ORIGINS)
    )
    """開発 UI (Next.js) からの CORS 許可 origin (§12.2: 家庭内ネットワーク限定)。"""

    permission_timeout_seconds: int = Field(
        DEFAULT_PERMISSION_TIMEOUT_SECONDS, ge=10, le=3600
    )
    """§10.3 の承認待ちで human 応答を待つ上限。超過は deny (2.5.4)。"""

    demo_rule_tools: bool = True
    """Phase 0 検証用の in-memory 規則 tool を登録するか (Phase 1 で差し替え)。"""

    agui_emit_reasoning: bool = True
    """AG-UI bridge が REASONING_* event を出すか (§2.1 トレース表示)。"""

    max_frames_per_thread: int = Field(DEFAULT_MAX_FRAMES_PER_THREAD, ge=1)
    """monitor frames の thread ごと保持上限 (README §9.2 replay と監視証跡の上限)。"""

    monitor_replay_limit: int = Field(DEFAULT_MONITOR_REPLAY_LIMIT, ge=1)
    """monitor SSE 再接続時に最後に送る replay 件数 (README §9.2)。"""

    monitor_heartbeat_seconds: float = Field(DEFAULT_MONITOR_HEARTBEAT_SECONDS, ge=1)
    """monitor SSE の idle heartbeat 間隔 (README §9.2)。"""

    max_concurrent_turns: int = Field(DEFAULT_MAX_CONCURRENT_TURNS, ge=1)
    """同時実行するターン上限 (UI で複数 thread を同時に動かすため)。"""

    context_token_warning_threshold: float = Field(
        DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD, gt=0.0, le=1.0
    )
    """provider context 使用率の警告閾値 (0〜1。README §9.4 対策の 1 段目)。"""

    context_warning_cost_jpy: float = Field(DEFAULT_CONTEXT_WARNING_COST_JPY, ge=0)
    """BYOK の context 膨張が明示コストに到達したとみなす閾値 (円)。"""

    @field_validator("session_idle_timeout_seconds")
    @classmethod
    def _valid_idle_timeout(cls, value: int) -> int:
        """0 は reaper 無効。設定する場合は 10〜3600 秒 (§7.3 / 契約 §7)。"""
        if value == 0:
            return 0
        return cls._valid_timeout(value)

    @field_validator("turn_timeout_seconds")
    @classmethod
    def _valid_turn_timeout(cls, value: int) -> int:
        """1.0.29 で実際にターン完了まで待ち受ける秒数のため下限を設ける。"""
        return cls._valid_timeout(value)

    @staticmethod
    def _valid_timeout(value: int) -> int:
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


def _env_list(name: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    """カンマ区切りの環境変数を名前のタプルへ変換する (未設定なら default)。"""
    raw = _env_opt(name)
    if raw is None:
        return tuple(default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_origins(name: str, default: Sequence[str]) -> list[str]:
    """CORS origin 読み込み: JSON 配列またはカンマ区切りリストを受け付ける (§7.3)。"""
    raw = _env_opt(name)
    if raw is None:
        return list(default)
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{name} は JSON 配列またはカンマ区切りで設定してください"
            ) from exc
        if not isinstance(parsed, list):
            raise ValueError(f"{name} は文字列の JSON 配列で設定してください")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in stripped.split(",") if item.strip()]


def _env_float(name: str, default: float) -> float:
    """float env 読み込み (不正値は default にフォールバック)."""
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
        api_cors_origins=_env_origins("API_CORS_ORIGINS", DEFAULT_API_CORS_ORIGINS),
        permission_timeout_seconds=_env_int(
            "PERMISSION_TIMEOUT_SECONDS", DEFAULT_PERMISSION_TIMEOUT_SECONDS
        ),
        demo_rule_tools=_env("DEMO_RULE_TOOLS", "on").lower() in truthy,
        agui_emit_reasoning=_env("AGUI_EMIT_REASONING", "on").lower() in truthy,
        max_frames_per_thread=_env_int(
            "MAX_FRAMES_PER_THREAD", DEFAULT_MAX_FRAMES_PER_THREAD
        ),
        monitor_replay_limit=_env_int(
            "MONITOR_REPLAY_LIMIT", DEFAULT_MONITOR_REPLAY_LIMIT
        ),
        monitor_heartbeat_seconds=_env_float(
            "MONITOR_HEARTBEAT_SECONDS", DEFAULT_MONITOR_HEARTBEAT_SECONDS
        ),
        max_concurrent_turns=_env_int(
            "MAX_CONCURRENT_TURNS", DEFAULT_MAX_CONCURRENT_TURNS
        ),
        context_token_warning_threshold=_env_float(
            "CONTEXT_TOKEN_WARNING_THRESHOLD", DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD
        ),
        context_warning_cost_jpy=_env_float(
            "CONTEXT_WARNING_COST_JPY", DEFAULT_CONTEXT_WARNING_COST_JPY
        ),
        host=_env("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8000),
    )
