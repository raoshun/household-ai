"""時刻生成の共通化 (README 9.6 / 18: タイムスタンプは Asia/Tokyo で表示する)。

tzdata が解決できない環境では固定 +09:00 (JST) へ fallback する。日本は 1989 年
以降サマータイムが無いので、fixed offset でも表示時刻としては同一である。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TOKYO_ZONE_NAME = "Asia/Tokyo"
_TOKYO_FALLBACK = timezone(timedelta(hours=9), name="JST")


def tokyo_tz() -> timezone | ZoneInfo:
    try:
        return ZoneInfo(TOKYO_ZONE_NAME)
    except (KeyError, ValueError):
        return _TOKYO_FALLBACK


def tokyo_now() -> datetime:
    return datetime.now(tokyo_tz())


def tokyo_iso(value: datetime | None = None) -> str:
    """ISO 8601 (Asia/Tokyo offset 付き)。API と UI 表示で同じ形を使う。"""
    moment = tokyo_now() if value is None else value
    if value is not None and value.tzinfo is None:
        moment = value.replace(tzinfo=tokyo_tz())
    return moment.astimezone(tokyo_tz()).isoformat(timespec="milliseconds")


__all__ = ["TOKYO_ZONE_NAME", "tokyo_iso", "tokyo_now", "tokyo_tz"]
