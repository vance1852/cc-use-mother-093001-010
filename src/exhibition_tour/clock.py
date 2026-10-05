"""提供可替换的 UTC 时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """定义调度服务所需的最小时钟接口。"""

    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    """使用系统 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """为测试与离线验收提供可推进的固定时间。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("固定时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._value

    def advance(self, **delta) -> None:
        """推进固定时间，便于测试租约到期与重排。"""

        self._value = self._value + timedelta(**delta)
