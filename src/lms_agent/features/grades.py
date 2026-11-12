"""성적: 과목별 평가 항목 설정 저장과 추정 결과, 점수표용 학번 설정. 계약 7·10장."""
from __future__ import annotations

from .. import gradebook
from ..api import router
from .core import courses


@router.state_section("grades")
def grades_state(app, ctx):
    return gradebook.section(app.store, courses(ctx))


@router.route("POST", r"/api/grades/settings", priority=1)  # 아래 /api/grades/<id>보다 먼저
def grade_settings(app, body):
    """{"student_id": str|null} — 점수표 PDF에서 내 행을 찾는 데만 쓰는 학번(이 PC에만 저장, 화면에는 가린 값만)."""
    if "student_id" in body:
        gradebook.set_student_id(app.store, body.get("student_id"))  # 잘못되면 ValueError → 400
    app.store.save()
    return app.state()


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)")
def save(app, body, course_id):
    gradebook.save_course(app.store, course_id, body)  # 잘못되면 ValueError → 400. 저장한 항목은 사용자 입력("auto" 없음)
    app.store.save()
    return app.state()
