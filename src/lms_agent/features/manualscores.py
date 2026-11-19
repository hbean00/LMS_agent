"""직접 넣은 점수·성적 파일 가져오기(계약 29-1, 담당 S1): 경로·상태 칸. 일은 manualscores.py가 한다.

- POST /api/grades/<과목>/scores {"action": "add"|"update"|"delete", …} — 능력 grades.add_score·update_score·remove_score(바로 하고 되돌리기)
- POST /api/grades/<과목>/import {filename, data(base64)} — 20MB까지. 본문이 커서 server.py가 경로의 max_body를 읽어야 한다
  (처리기 함수의 max_body 속성 — 없으면 서버 기본 한도). 사용자가 파일을 고르는 일이라 능력이 아니다(EXCLUDED_ROUTES).
- POST /api/grades/<과목>/import/delete {key} — 가져온 파일 지우기(디스크 파일도 지워 되돌릴 수 없음 — 능력 아님).
- GET /api/state의 "manual_scores" 칸(manualscores.state).
"""
from __future__ import annotations

from .. import capabilities, manualscores
from ..api import router
from .grades import section as grades_section


def _write(app, fn):
    """잠금 안에서 다른 프로세스(백그라운드 동기화)의 저장을 따라간 뒤 바꾸고 쓴다 → fn의 결과."""
    store = app.store
    with store.locked():
        store.reload_if_changed()
        out = fn(store)
        store.write()
    return out


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)/scores")
def scores(app, body, course_id):
    """직접 넣은 점수 더하기·고치기·지우기(계약 29-1). 잘못되면 ValueError → 400(한국어)."""
    row = _write(app, lambda store: manualscores.apply(store, course_id, body))
    out = app.state()
    out["manual_score_result"] = {"action": body.get("action"), "id": row.get("id"), "text": manualscores.entry_text(row)}
    return out


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)/import")
def import_file(app, body, course_id):
    """성적 파일 가져오기. 검사·디스크 저장·읽기(PDF 글자 뽑기 등 — 큰 파일은 몇 초)는 잠금 밖에서, 저장소에 넣기만 잠금 안에서.
    응답 = 상태 + "import_result"(가져온 기록 + 점수 후보·상태 한 줄)."""
    prepared = manualscores.prepare_import(app.store, app.home, course_id, body)
    rec = _write(app, lambda store: manualscores.commit_import(store, prepared))
    out = app.state()
    mine = next((r for r in ((out.get("manual_scores") or {}).get("imports") or {}).get(str(course_id)) or []
                 if r.get("key") == rec["key"]), rec)
    out["import_result"] = mine
    return out


import_file.max_body = manualscores.IMPORT_BODY_MAX   # server.py가 이 경로의 본문 한도로 쓴다(계약 29-1, 리드 통합)


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)/import/delete")
def delete_import(app, body, course_id):
    _write(app, lambda store: manualscores.delete_import(store, app.home, course_id, body))
    return app.state()


@router.state_section("manual_scores")
def manual_scores_state(app, ctx):
    """직접 넣은 점수·가져온 파일(연결·찾은 점수는 성적 묶음에서 읽는다). 실패해도 다른 화면은 그대로."""
    try:
        return manualscores.state(app.store, grades_section(ctx))
    except Exception:  # noqa: BLE001
        return {"courses": {}, "imports": {}, "limits": {"max_bytes": manualscores.IMPORT_MAX_BYTES,
                                                          "exts": list(manualscores.IMPORT_EXTS)},
                "error": "직접 넣은 점수·가져온 파일을 보여 주지 못했어요"}


capabilities.EXCLUDED_ROUTES["/api/grades/{course_id}/import"] = (
    "성적 파일 가져오기 — 사용자가 이 PC의 파일을 직접 골라 올리는 일이라 에이전트가 하지 않아요")
capabilities.EXCLUDED_ROUTES["/api/grades/{course_id}/import/delete"] = (
    "가져온 성적 파일 지우기 — 디스크의 파일까지 지워 되돌릴 수 없고, 가져오기처럼 사용자가 화면에서 고르는 일이에요")
