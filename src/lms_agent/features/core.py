"""기본 기능: 화면 페이지, 상태, 동기화, 항목 결정, 붙여넣기, 설정, Canvas 연결, 캘린더 파일. 계약 3~5장."""
from __future__ import annotations

from importlib import resources
from urllib.parse import urlparse

from .. import __version__
from .. import core
from ..api import ApiError, Response, router
from ..canvas import Canvas, CanvasError
from ..config import is_configured, normalize_url, save_config
from ..ics import validate_ics
from ..timeutil import iso, now_local

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; frame-ancestors 'none'")
SETTINGS = ("include_canvas", "weekly_summary")


def items(ctx):
    """항목(사용자 결정 반영) + 주차·짧은 과목명. 여러 칸이 같이 쓴다."""
    def make():
        cache = ctx.app.store.data.get("cache")
        its = ctx.app.store.view()
        core.annotate(its, semester(ctx), cache)
        return its
    return ctx.get("items", make)


def semester(ctx):
    return ctx.get("semester", lambda: core.semester_info(ctx.app.store.data.get("cache"), ctx.app.store.view()))


def courses(ctx):
    return ctx.get("courses", lambda: core.course_summaries(ctx.app.store.data.get("cache")))


# ------------------------------------------------------------ 상태

@router.state_section(None)
def base_state(app, ctx) -> dict:
    d = app.store.data
    return {
        "app_version": __version__, "now": iso(now_local()), "demo": app.demo,
        "configured": is_configured(app.cfg), "canvas_url": app.cfg.get("canvas_url"),
        "last_sync": d.get("last_sync"), "syncing": app.syncing, "sync_error": app.sync_error,
        "report": d.get("report"),
        "settings": {k: bool(d["settings"].get(k, True)) for k in SETTINGS},
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
    allowed = {k: body[k] for k in ("decision", "submitted", "title", "due") if k in body}
    try:
        app.store.update_user(item_id, allowed)
    except KeyError:
        raise ApiError(404, "항목을 찾을 수 없습니다. 동기화 후 다시 시도하세요") from None
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
    for key in SETTINGS:
        if key in body:
            app.store.data["settings"][key] = bool(body[key])
    app.store.save()
    app.emit("changed")
    return app.state()


@router.route("POST", r"/api/setup")
def setup(app, body):
    if app.demo:
        raise ApiError(400, "데모 모드에서는 설정을 바꿀 수 없습니다")
    url, token = normalize_url(str(body.get("canvas_url") or "")), str(body.get("token") or "").strip()
    if not url or not token:
        raise ApiError(400, "Canvas 주소와 토큰을 모두 입력하세요")
    if not urlparse(url).netloc:
        raise ApiError(400, "Canvas 주소가 올바르지 않습니다")
    try:
        Canvas(url, token).whoami()  # 실제로 되는지 먼저 확인
    except CanvasError as e:
        raise ApiError(400, f"연결 확인 실패: {e}") from None
    app.cfg.update(canvas_url=url, token=token)
    save_config(app.cfg, app.home)
    return {"ok": True}
