"""성적: 과목별 평가 항목 설정 저장과 추정 결과, 점수표용 학번 설정, 점수·개수 연결, 시험 일정. 계약 7·10·14장."""
from __future__ import annotations

from .. import gradebook
from ..api import router
from .core import courses, items, semester


def section(ctx):
    """state.grades — 시험 일정(state.exams)도 같은 계산을 쓰므로 한 번만 만든다."""
    app = ctx.app
    return ctx.get("grades", lambda: gradebook.section(app.store, courses(ctx), items=items(ctx), sem=semester(ctx)))


@router.state_section("grades")
def grades_state(app, ctx):
    return section(ctx)


@router.state_section("exams")
def exams_state(app, ctx):
    """{"upcoming": [...], "not_announced": [...]} — 시험 계열 평가 항목의 일정(계약 14-9) + 평가 항목에 연결되지 않은
    다가오는 시험 항목(component·weight null)."""
    return gradebook.exams_of(section(ctx), items(ctx))


@router.route("POST", r"/api/grades/settings", priority=1)  # 아래 /api/grades/<id>보다 먼저
def grade_settings(app, body):
    """{"student_id": str|null} — 점수표 PDF에서 내 행을 찾는 데만 쓰는 학번(이 PC에만 저장, 화면에는 가린 값만)."""
    if "student_id" in body:
        gradebook.set_student_id(app.store, body.get("student_id"))  # 잘못되면 ValueError → 400
    app.store.save()
    return app.state()


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)/link")
def link(app, body, course_id):
    """점수 후보 → 평가 항목 직접 연결 {"key", "component": 이름|null} / 되돌리기 {"key", "reset": true} — 계약 14-6.
    save_course가 아니므로 평가 비중의 auto 표시는 그대로다."""
    gradebook.set_link(app.store, course_id, body)
    app.store.save()
    return app.state()


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)/count")
def count(app, body, course_id):
    """세부 점수 개수 직접 입력 {"component": 이름, "n": 정수|null}(null = 지우기) — 계약 14-4."""
    gradebook.set_count(app.store, course_id, body)
    app.store.save()
    return app.state()


@router.route("POST", r"/api/grades/(?P<course_id>[\w\-]+)")
def save(app, body, course_id):
    gradebook.save_course(app.store, course_id, body)  # 잘못되면 ValueError → 400. 저장한 항목은 사용자 입력("auto" 없음)
    app.store.save()
    return app.state()
