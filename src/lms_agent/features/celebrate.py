"""성적 공개 반응과 학기 중 보상(계약 11장, 11-1장). 계산은 celebrate.py.

상태를 물을 때마다 저장된 것만으로 다시 계산한다(동기화 뒤에도 따로 할 일 없음).
저장은 두 경우뿐: 처음 계산(오래된 점수를 조용히 본 것으로)·확인함 최고치 변화, 그리고 POST /api/celebrations/seen.
"""
from __future__ import annotations

from .. import celebrate
from ..api import ApiError, router
from .core import items, semester

MAX_IDS = 200


def _persist(app, updates: dict) -> None:
    """백그라운드 동기화가 그 사이 저장했을 수 있으니 잠금 안에서 다시 읽고 합쳐 쓴다."""
    store = app.store
    try:
        with store.locked():
            store.reload_if_changed()
            celebrate.apply(store.data, updates)
            store.write()
    except (OSError, TimeoutError):
        celebrate.apply(store.data, updates)  # 이번에는 메모리에만. 다음 계산 때 다시 저장을 시도한다


@router.state_section("celebrations")
def celebrations_state(app, ctx):
    out, updates = celebrate.state(app.store, semester(ctx), items(ctx))
    if updates:
        _persist(app, updates)
    return out


@router.route("POST", r"/api/celebrations/seen")
def seen(app, body):
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) > MAX_IDS or not all(isinstance(i, str) and len(i) <= 300 for i in ids):
        raise ApiError(400, "ids는 문자열 목록이어야 합니다")
    store = app.store
    with store.locked():
        store.reload_if_changed()
        sem = core_semester(store)
        n = celebrate.mark_seen(store, sem, core_items(store, sem), ids)
        if n:
            store.write()
    return app.state()


@router.route("POST", r"/api/celebrations/snooze")
def snooze(app, body):
    """성적 소식 '나중에 볼래요' {"ids": [성적 id…]} → 미룬 횟수 +1(두 번이면 큰 카드로는 다시 안 띄움) → 새 state."""
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) > MAX_IDS or not all(isinstance(i, str) and len(i) <= 300 for i in ids):
        raise ApiError(400, "ids는 문자열 목록이어야 합니다")
    store = app.store
    with store.locked():
        store.reload_if_changed()
        if celebrate.snooze(store, ids):
            store.write()
    return app.state()


def core_semester(store):
    from .. import core
    return core.semester_info(store.data.get("cache"), store.view())


def core_items(store, sem):
    from .. import core
    return core.annotate(store.view(), sem, store.data.get("cache"))
