"""시간대 처리. 앱 안의 시간은 모두 PC 로컬 시간대가 붙은 datetime이다."""
from __future__ import annotations

from datetime import datetime, timezone


def local_tz():
    return datetime.now().astimezone().tzinfo


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def parse_iso(s: str | None) -> datetime | None:
    """Canvas의 '2026-11-20T14:59:59Z'와 앱의 '+09:00' 문자열을 모두 받아 로컬 시간대로 바꾼다."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz())


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_local_input(s: str) -> datetime:
    """화면의 datetime-local 값('2026-11-20T23:59')을 로컬 시간대 datetime으로."""
    dt = datetime.strptime(s.strip()[:16], "%Y-%m-%dT%H:%M")
    return dt.replace(tzinfo=local_tz())


def to_utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
