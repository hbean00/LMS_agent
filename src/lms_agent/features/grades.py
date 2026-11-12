"""성적: 과목별 평가 항목 설정 저장과 추정 결과. 계약 7장."""
from __future__ import annotations

from .. import gradebook
from ..api import router
from .core import courses


@router.state_section("grades")
def grades_state(app, ctx):
    return gradebook.section(app.store, courses(ctx))


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)")
def save(app, body, course_id):
    gradebook.save_course(app.store, course_id, body)  # 잘못되면 ValueError → 400
    app.store.save()
    return app.state()
