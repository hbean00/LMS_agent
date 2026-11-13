"""코치 카드: 주간 브리핑·바뀐 마감·회고·종강 리포트(계약 22-4). 계산은 coach.py.

상태를 물을 때마다 저장된 것만으로 다시 계산한다(동기화 뒤에 따로 할 일 없음). 저장은 POST /api/coach/dismiss뿐.
"""
from __future__ import annotations

from .. import coach
from ..api import ApiError, router
from .core import items, semester
from .grades import section as grades_section


@router.state_section("coach")
def coach_state(app, ctx):
    try:
        grades = grades_section(ctx)   # 성적 칸과 같은 계산(ctx에 한 번만)
    except Exception:
        grades = None
    try:
        return coach.state(app.store, sem=semester(ctx), items=items(ctx), grades=grades)
    except Exception:   # 코치 카드는 덤이다 — 계산이 실패해도 화면의 나머지는 그대로
        return coach.empty()


@router.route("POST", r"/api/coach/dismiss")
def dismiss(app, body):
    """{"id": 카드 id} → 그 카드를 닫는다(브리핑·회고는 그 주에, 종강 리포트는 그 학기에 다시 안 보임) → 새 state."""
    card_id = body.get("id")
    if not coach.valid_id(card_id):
        raise ApiError(400, "닫을 카드 id가 올바르지 않아요")
    store = app.store
    try:
        with store.locked():   # 백그라운드 동기화가 그 사이 저장했을 수 있으니 다시 읽고 합쳐 쓴다
            store.reload_if_changed()
            if not coach.dismiss(store, card_id):
                raise ApiError(400, "지금 보이는 카드가 아니에요. 화면을 다시 불러와 주세요")
            store.write()
    except TimeoutError:
        raise ApiError(409, "다른 동기화가 저장 중이에요. 잠시 뒤 다시 눌러 주세요") from None
    return app.state()
