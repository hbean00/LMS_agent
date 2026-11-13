"""과제 체크리스트·문의 메일 초안 경로(계약 22-6·22-7). 일은 checklist.py·maildraft.py가 하고, 여기는 경로·상태 칸만.

- state.checklists = {item_id: {"items": [{"id", "kind", "text", "quote", "source_title", "url", "done", "by"}], "updated_at"}}
  화면에 보이는 항목(review·scheduled) 중 칸이 하나라도 있는 것만. 규칙 칸은 저장된 글로 계산(네트워크 없음)하고
  앱 안에 기억해 둔다(상태 파일·항목이 바뀔 때만 다시 계산).
- POST /api/checklist/<item_id> {"op": "toggle", "id", "done"?} | {"op": "add", "text"} | {"op": "remove", "id"} | {"op": "refresh"}
  → 새 state. 사용자 체크·추가·지우기는 store.data["checklists"]에 남아 다시 동기화해도 그대로다.
- POST /api/mail/draft {"course_id", "purpose", "item_id"?, "note"?} → {"draft": {...}} (저장 안 함, 보내지 않음).
"""
from __future__ import annotations

import threading

from .. import checklist, maildraft
from ..api import ApiError, router
from .core import items as core_items

NOT_FOUND = "항목을 찾을 수 없습니다. 동기화 후 다시 시도하세요"


@router.on_init
def attach(app, options):
    app.checklist_cache = {}
    app.checklist_guard = threading.Lock()


def _key(app, items: list[dict]) -> tuple:
    d = app.store.data
    visible = tuple((v["id"], v.get("status"), v.get("title"), len(v.get("evidence") or []), v.get("group"))
                    for v in items if v.get("status") in checklist.VISIBLE)
    return (app.store.mtime, id(d.get("cache")), id(d.get("items")), id(d.get("pdf_texts")),
            len(d.get("manual") or []), visible)


def _rules(app, items: list[dict]) -> dict:
    key = _key(app, items)
    with app.checklist_guard:
        c = app.checklist_cache
        if c.get("key") != key:
            c.clear()
            c["rules"] = checklist.compute(app.store, items)
            c["key"] = key
        return c["rules"]


@router.state_section("checklists")
def checklists_state(app, ctx):
    its = core_items(ctx)
    return checklist.view(app.store, its, _rules(app, its))


@router.route("POST", r"/api/checklist/(?P<item_id>[\w\-]+)")
def checklist_op(app, body, item_id):
    if body.get("op") not in checklist.OPS:
        raise ApiError(400, "op는 toggle, add, remove, refresh 중 하나여야 해요")
    with app.store.locked():
        app.store.reload_if_changed()
        view = app.store.view()
        item = next((v for v in view if v["id"] == item_id), None)
        if item is None:
            raise ApiError(404, NOT_FOUND)
        rules = checklist.rules_for(item, checklist.all_sources(app.store), view)
        try:
            checklist.apply(app.store, item_id, rules, body)   # 잘못된 입력은 ValueError → 400
        except LookupError as e:
            raise ApiError(404, str(e)) from None
        app.store.write()
    if body.get("op") == "refresh":
        with app.checklist_guard:
            app.checklist_cache.clear()
    return app.state()


def _names(app) -> list:
    n = (app.cfg or {}).get("user_name")
    return [n] if isinstance(n, str) and n.strip() else []


@router.route("POST", r"/api/mail/draft")
def mail_draft(app, body):
    cid, iid = body.get("course_id"), body.get("item_id")
    if cid is not None and (isinstance(cid, bool) or not isinstance(cid, (str, int)) or not str(cid).strip()
                            or len(str(cid)) > 40):
        raise ApiError(400, "course_id가 올바르지 않아요")
    if iid is not None and (not isinstance(iid, str) or not iid or len(iid) > 80):
        raise ApiError(400, "item_id가 올바르지 않아요")
    if cid is None and iid is None:
        raise ApiError(400, "course_id(과목) 또는 item_id(항목)가 필요해요")
    try:
        d = maildraft.draft(app.store, str(cid) if cid is not None else None, body.get("purpose"), iid,
                            body.get("note"), names=_names(app))
    except LookupError as e:
        raise ApiError(404, str(e)) from None
    return {"draft": d}
