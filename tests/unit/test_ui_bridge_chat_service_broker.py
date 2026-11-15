"""ChatService 既定構築の broker 配線 (契約 §4 approvals / §5 解決 API).

chat.py の approval glue は `approvals(session_id=...)` / `get_approval` /
`resolve_permission` を呼ぶため、既定 broker が素の PermissionBroker だと
構築直後の API 呼び出しで TypeError になる。facade + kwargs で構築する。
"""

from __future__ import annotations

from typing import cast

from app.agent.runtime import AgentRuntime
from app.config import Settings
from app.services.chat import ChatService
from app.services.permissions_api import PermissionBrokerFacade

PERMISSION_TIMEOUT_SECONDS = 30


def _settings() -> Settings:
    return Settings(
        model="gemma4:e2b",
        permission_timeout_seconds=PERMISSION_TIMEOUT_SECONDS,
        provider="ollama",
    )


def _unused_runtime() -> AgentRuntime:
    # 検証対象は broker 配線のみ。ChatService.__init__ は runtime を保持するだけで
    # 呼び出さないため、中身のない object で代替できる。
    return cast(AgentRuntime, object())


def test_default_broker_is_facade_using_settings_timeout() -> None:
    service = ChatService(runtime=_unused_runtime(), settings=_settings())

    assert isinstance(service._broker, PermissionBrokerFacade)
    assert service._broker.timeout_seconds == PERMISSION_TIMEOUT_SECONDS


def test_default_broker_supports_approval_listing() -> None:
    service = ChatService(runtime=_unused_runtime(), settings=_settings())

    assert service.approvals() == []
    assert service.pending_approvals() == []


def test_injected_broker_is_preserved() -> None:
    broker = PermissionBrokerFacade(timeout_seconds=7)
    service = ChatService(
        broker=broker,
        runtime=_unused_runtime(),
        settings=_settings(),
    )

    assert service._broker is broker
    assert service._broker.timeout_seconds == 7
