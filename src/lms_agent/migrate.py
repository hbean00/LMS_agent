"""state.json을 지금 버전에 맞추기(계약 19-4). Store가 파일을 읽을 때마다 부른다.

- state.json에는 마지막으로 저장한 앱 버전(`app_version`)이 있다(0.6.0 전에는 없음 = 아주 옛 파일).
- MIGRATIONS의 단계 중 `app_version`보다 새 버전의 것만 차례로 돌린다. 단계는 여러 번 돌아도 같은 결과여야 한다
  (저장하기 전에 다시 읽으면 또 돈다).
- 더 새 버전이 저장한 파일(예: 업데이트했다가 옛 exe로 돌아감)은 아무것도 고치지 않는다. 모르는 칸은 그대로 두고
  (Store는 읽은 칸을 모두 지닌 채 저장한다), `app_version`도 낮추지 않는다.

새 단계 추가: 아래처럼 함수 하나에 `@migration("<그 변화가 들어간 버전>")`.

버전이 바뀔 때마다(더 옛 버전이 저장한 파일을 처음 읽을 때 — 단계와 달리 어느 버전이든) ON_VERSION_CHANGE의 함수를 부른다:
fn(data, 옛 버전 | None, 지금 버전). 지금은 받아 둔 파일 모두 다시 읽기 표시(`reprocess`, 계약 29-3) — 읽는 규칙이 바뀐 새 버전이
전에 받아 둔 PDF·표·문서도 다시 읽어 성적·마감에 반영하게.
"""
from __future__ import annotations

from typing import Callable

from . import __version__
from .timeutil import iso, now_local
from .updater import parse_version

MIGRATIONS: list[tuple[str, Callable[[dict], None]]] = []
ON_VERSION_CHANGE: list[Callable[[dict, str | None, str], None]] = []


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


def _mark_reprocess(data: dict, old: str | None, current: str) -> None:
    """받아 둔 파일(downloads)이나 가져온 성적 파일(score_imports)이 있으면 store.data["reprocess"] = {pending, total(None — 첫 동기화가 셈),
    done, at, from, to, by}. 다음 동기화들이 읽을 수 있는 받아 둔 파일을 모두(동기화당 materials.REPROCESS_MAX개) 다시 읽고
    (처음 한 번은 가져온 파일도 — core.reprocess_progress) 다 읽으면 지운다.
    같은 버전으로 이미 켜져 있으면 그대로(여러 번 돌아도 같은 결과)."""
    downloads, imports = data.get("downloads"), data.get("score_imports")   # 받아 둔 첨부, 성적 화면에서 가져온 파일(29-1)
    if not (isinstance(downloads, dict) and downloads) and not (isinstance(imports, dict) and imports):
        return
    rp = data.get("reprocess")
    if isinstance(rp, dict) and rp.get("pending") and rp.get("to") == current:
        return
    data["reprocess"] = {"pending": True, "total": None, "done": 0, "at": iso(now_local()), "from": old, "to": current,
                         "by": "update"}


ON_VERSION_CHANGE.append(_mark_reprocess)


def migrate(data: dict, current: str = __version__) -> list[str]:
    """data(읽은 state.json)를 그 자리에서 고친다. 돌린 단계의 버전 목록을 돌려준다."""
    old = stored_version(data)
    stored = parse_version(old) or (0, 0, 0, 1, "")
    cur = parse_version(current)
    if cur is not None and stored > cur:     # 더 새 버전이 저장한 파일 — 손대지 않는다
        return []
    ran = []
    for version, fn in MIGRATIONS:
        if stored < parse_version(version):
            fn(data)
            ran.append(version)
    if cur is not None and stored < cur:
        for hook in ON_VERSION_CHANGE:
            hook(data, old, current)
    if cur is not None:
        data["app_version"] = current
    return ran
