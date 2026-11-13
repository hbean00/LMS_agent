"""기본 기능: 화면 페이지, 상태, 동기화, 항목 결정, 붙여넣기, 설정, Canvas 연결, 캘린더 파일. 계약 3~5장."""
from __future__ import annotations

from importlib import resources

from .. import __version__
from .. import core
from ..api import ApiError, Response, router
from ..canvas import CanvasError
from ..config import is_configured
from ..ics import validate_ics
from ..timeutil import iso, now_local

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; frame-ancestors 'none'")
SETTINGS = {"include_canvas": True, "weekly_summary": True, "review_reminder": False,  # 키: 기본값
            "tutorial_done": False, "notify": True,
            "update_check": True, "auto_update": False}                                # 계약 19장
# 정수 설정: 키 → (기본값, 최솟값, 최댓값). tutorial_version = 마지막으로 본(또는 넘긴) 튜토리얼 판(계약 8장)
INT_SETTINGS = {"tutorial_version": (0, 0, 1000)}


def _settings_view(saved: dict) -> dict:
    out = {k: bool(saved.get(k, default)) for k, default in SETTINGS.items()}
    for k, (default, lo, hi) in INT_SETTINGS.items():
        v = saved.get(k, default)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else default
    return out


def items(ctx):
    """항목(사용자 결정 반영) + 주차·짧은 과목명 + 평가 항목 연결(component·component_by, 계약 14-9). 여러 칸이 같이 쓴다.
    제외(hidden)한 과목의 항목은 store.view()가 이미 뺀다(계약 15장)."""
    def make():
        from .. import gradebook
        cache = ctx.app.store.data.get("cache")
        its = ctx.app.store.view()
        sem = semester(ctx)
        core.annotate(its, sem, cache)
        gradebook.link_items(ctx.app.store, its, sem)
        return its
    return ctx.get("items", make)


def semester(ctx):
    return ctx.get("semester", lambda: core.semester_info(ctx.app.store.data.get("cache"), ctx.app.store.view()))


def courses(ctx):
    """과목 목록 + mode·mode_suggest·read_counts(계약 15장). 제외한 과목도 들어 있다(화면의 '제외한 과목'용)."""
    def make():
        from .. import course_modes
        d = ctx.app.store.data
        return course_modes.annotate_courses(core.course_summaries(d.get("cache")), d)
    return ctx.get("courses", make)


# ------------------------------------------------------------ 상태

@router.state_section(None)
def base_state(app, ctx) -> dict:
    d = app.store.data
    return {
        "app_version": __version__, "now": iso(now_local()), "demo": app.demo,
        "configured": is_configured(app.cfg), "canvas_url": app.cfg.get("canvas_url"),
        "last_sync": d.get("last_sync"), "syncing": app.syncing, "sync_error": app.sync_error,
        "report": d.get("report"),
        "settings": _settings_view(d["settings"]),
        "semester": semester(ctx), "courses": courses(ctx), "items": items(ctx),
        "runs": list(reversed((d.get("runs") or [])[-10:])),   # 최근 실행 기록, 최신 먼저
        "last_export": (d.get("last_export") or {}).get("at"),
    }


@router.route("GET", r"/api/state")
def get_state(app, query):
    return app.state()


# ------------------------------------------------------------ 페이지·파일 (세션 값 없이 열림)

@router.route("GET", r"/(?:index\.html)?", auth=False)
def page(app, query):
    html = resources.files("lms_agent").joinpath("web/index.html").read_text("utf-8")
    return Response(200, html.replace("__LMS_AGENT_TOKEN__", app.token).encode("utf-8"),
                    "text/html; charset=utf-8", {"Content-Security-Policy": CSP})


@router.route("GET", r"/calendar\.ics", auth=False)
def calendar_file(app, query):
    text = core.calendar_text(app.store, record=True)
    problems = validate_ics(text)
    if problems:
        raise ApiError(500, "캘린더 파일 검사 실패: " + "; ".join(problems[:3]))
    app.store.save()  # 내보낸 기록(근거 사슬)
    return Response(200, text.encode("utf-8"), "text/calendar; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="lms-deadlines.ics"'})


# ------------------------------------------------------------ 쓰기

@router.route("POST", r"/api/sync")
def sync(app, body):
    try:
        app.do_sync(full=bool(body.get("full")))
    except CanvasError as e:
        raise ApiError(502, str(e)) from None
    return app.state()


@router.route("POST", r"/api/item/(?P<item_id>[\w\-]+)")
def update_item(app, body, item_id):
    """사용자 결정. 같은 과제 묶음의 한 후보를 승인하면, 이미 승인돼 있던 다른 후보는 pending으로 돌린다
    (한 과제 = 캘린더 일정 하나, 계약 13-1) → 응답 state의 "replaced": [돌린 id…]."""
    allowed = {k: body[k] for k in ("decision", "submitted", "title", "due") if k in body}
    try:
        app.store.update_user(item_id, allowed)
    except KeyError:
        raise ApiError(404, "항목을 찾을 수 없습니다. 동기화 후 다시 시도하세요") from None
    replaced = app.store.release_group(item_id) if allowed.get("decision") == "approved" else []
    app.store.save()
    app.emit("changed")
    out = app.state()
    out["replaced"] = replaced
    return out


@router.route("POST", r"/api/item/(?P<item_id>[\w\-]+)/component")
def item_component(app, body, item_id):
    """마감·시험 항목을 평가 항목에 직접 연결 {"component": 이름|null}(null = 연결하지 않음), 되돌리기 {"reset": true}
    (사용자 연결을 지워 규칙·AI가 다시 정함) — 계약 14-9."""
    from .. import gradebook
    try:
        gradebook.set_item_component(app.store, item_id, body)   # 잘못된 이름은 ValueError → 400
    except KeyError:
        raise ApiError(404, "항목을 찾을 수 없습니다. 동기화 후 다시 시도하세요") from None
    app.store.save()
    return app.state()


@router.route("POST", r"/api/course/(?P<course_id>[\w\-]+)/mode")
def course_mode(app, body, course_id):
    """과목 종류 {"mode": "normal"|"pass_fail"|"hidden"} — 계약 15장. 제외하면 그 과목 일정은 다음 구글 반영 때 지워진다."""
    from .. import course_modes
    course_modes.set_mode(app.store, course_id, body.get("mode"))   # 잘못되면 ValueError → 400
    app.store.save()
    app.emit("changed")
    return app.state()


@router.route("POST", r"/api/manual")
def add_manual(app, body):
    app.store.add_manual(str(body.get("course_name") or ""), str(body.get("text") or ""), body.get("posted_at"))
    core.rebuild(app.store)
    app.store.save()
    app.emit("changed")
    return app.state()


@router.route("POST", r"/api/settings")
def settings(app, body):
    for key, (_, lo, hi) in INT_SETTINGS.items():
        if key in body:
            v = body[key]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ApiError(400, f"{key}는 {lo}~{hi} 사이의 정수여야 해요")
    for key in SETTINGS:
        if key in body:
            app.store.data["settings"][key] = bool(body[key])
    for key in INT_SETTINGS:
        if key in body:
            app.store.data["settings"][key] = body[key]
    app.store.save()
    app.emit("changed")
    return app.state()


@router.route("POST", r"/api/setup")
def setup(app, body):
    """예전 연결 API(계약 3장). 새 화면은 /api/account/login을 쓴다(같은 확인·저장)."""
    if app.demo:
        raise ApiError(400, "데모 모드에서는 설정을 바꿀 수 없습니다")
    from .account import _connect
    _connect(app, str(body.get("canvas_url") or ""), str(body.get("token") or ""))
    return {"ok": True}
