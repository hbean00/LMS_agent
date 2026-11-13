"""공부 일정 경로(계약 22-5): 미리 보기·캘린더에 넣기·지우기와 state.plans. 짜기·검사는 plan.py.

넣기·지우기는 백그라운드 동기화(다른 프로세스)가 그 사이 저장했을 수 있으니 파일 잠금 안에서 다시 읽고 바꾼 뒤 쓴다.
바뀌면 "changed"를 내보내 구글 캘린더 반영이 따라온다(.ics는 다음에 받을 때).
"""
from __future__ import annotations

from .. import plan
from ..api import ApiError, router


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "요청 본문이 올바르지 않아요")
    return body


def _write(app, fn):
    store = app.store
    try:
        with store.locked():
            store.reload_if_changed()
            out = fn(store)
            store.write()
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    except LookupError as e:
        raise ApiError(404, str(e)) from None
    except TimeoutError as e:
        raise ApiError(409, str(e)) from None
    return out


@router.state_section("plans")
def plans_state(app, ctx):
    return plan.state(app.store)


@router.route("POST", r"/api/plan/preview")
def preview(app, body):
    """{course_id?, item_id?, days?, minutes?, hour?, start?} → {"plan": {...}} (저장 안 함)."""
    b = _body(body)
    try:
        p = plan.make(app.store, course_id=b.get("course_id"), item_id=b.get("item_id"), days=b.get("days"),
                      minutes=b.get("minutes"), hour=b.get("hour"), start=b.get("start"))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return {"plan": p}


@router.route("POST", r"/api/plan/apply")
def apply(app, body):
    """{"title", "course_id"?, "item_id"?, "blocks": [...], "decided_by"?: "user"|"ai", "reason"?} → 검사 → 저장
    → 새 state(+ "plan_id": 저장한 계획 id — 되돌리기는 /api/plan/remove)."""
    b = _body(body)
    rec = _write(app, lambda store: plan.add(store, b))
    app.emit("changed")   # 구글 캘린더 반영
    st = app.state()
    st["plan_id"] = rec["id"]
    return st


@router.route("POST", r"/api/plan/remove")
def remove(app, body):
    """{"id"} → 그 계획의 블록 전부 지움 → 새 state. 모르는 id는 404."""
    b = _body(body)
    _write(app, lambda store: plan.remove(store, b.get("id")))
    app.emit("changed")
    return app.state()
