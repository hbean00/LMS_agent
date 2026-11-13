"""state.json을 지금 버전에 맞추기(계약 19-4). Store가 파일을 읽을 때마다 부른다.

- state.json에는 마지막으로 저장한 앱 버전(`app_version`)이 있다(0.6.0 전에는 없음 = 아주 옛 파일).
- MIGRATIONS의 단계 중 `app_version`보다 새 버전의 것만 차례로 돌린다. 단계는 여러 번 돌아도 같은 결과여야 한다
  (저장하기 전에 다시 읽으면 또 돈다).
- 더 새 버전이 저장한 파일(예: 업데이트했다가 옛 exe로 돌아감)은 아무것도 고치지 않는다. 모르는 칸은 그대로 두고
  (Store는 읽은 칸을 모두 지닌 채 저장한다), `app_version`도 낮추지 않는다.

새 단계 추가: 아래처럼 함수 하나에 `@migration("<그 변화가 들어간 버전>")`.
"""
from __future__ import annotations

from typing import Callable

from . import __version__
from .updater import parse_version

MIGRATIONS: list[tuple[str, Callable[[dict], None]]] = []


def migration(version: str):
    def deco(fn):
        MIGRATIONS.append((version, fn))
        MIGRATIONS.sort(key=lambda m: parse_version(m[0]))
        return fn
    return deco


@migration("0.6.0")
def _container_shapes(data: dict) -> None:
    """0.6.0 전 파일: 최상위 칸의 모양(목록·사전)이 틀리면(손으로 고쳤거나 반쯤 깨진 파일) 앱이 켜지지 않았다.
    틀린 값은 버리지 않고 `_repaired`에 옮겨 두고 빈 값으로 연다. 업데이트 확인 칸("update")을 만든다."""
    shapes = {"items": list, "user": dict, "manual": list, "settings": dict, "runs": list,
              "course_prefs": dict, "update": dict}
    for key, typ in shapes.items():
        if key in data and data[key] is not None and not isinstance(data[key], typ):
            data.setdefault("_repaired", {})[key] = data[key]
            data[key] = typ()
    data.setdefault("update", {})


def stored_version(data: dict) -> str | None:
    v = data.get("app_version")
    return v if isinstance(v, str) and parse_version(v) else None


def migrate(data: dict, current: str = __version__) -> list[str]:
    """data(읽은 state.json)를 그 자리에서 고친다. 돌린 단계의 버전 목록을 돌려준다."""
    stored = parse_version(stored_version(data)) or (0, 0, 0, 1, "")
    cur = parse_version(current)
    if cur is not None and stored > cur:     # 더 새 버전이 저장한 파일 — 손대지 않는다
        return []
    ran = []
    for version, fn in MIGRATIONS:
        if stored < parse_version(version):
            fn(data)
            ran.append(version)
    if cur is not None:
        data["app_version"] = current
    return ran
