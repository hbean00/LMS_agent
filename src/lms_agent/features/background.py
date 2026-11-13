"""백그라운드 동기화 설정: 작업 스케줄러 등록 상태와 켜기·끄기. 계약 8장.

실제 등록은 사용자가 설정 화면에서 켤 때만 일어난다. 데모는 등록하지 않는다(supported=false).
테스트는 App(..., scheduler=가짜)로 실제 작업 스케줄러를 건드리지 않는다.
"""
from __future__ import annotations

from .. import schedule
from ..api import ApiError, router


@router.on_init
def attach(app, options):
    app.scheduler = options.get("scheduler") or schedule.Scheduler(app.home)
    app.background_supported = (options.get("scheduler") is not None) or (schedule.supported() and not app.demo)
    # 백그라운드 동기화를 켠 적이 있으면 실행 스크립트를 지금의 파이썬·exe 경로로 다시 쓴다
    # (파이썬을 다시 설치·옮기거나 exe로 바꾸면 예전 경로가 남아 백그라운드 동기화가 조용히 멈추던 문제)
    if app.background_supported and options.get("scheduler") is None:
        try:
            if (app.home / "background-sync.vbs").exists():
                schedule.write_launcher(app.home)
        except OSError:
            pass


@router.state_section("background")
def background_state(app, ctx):
    runs = [r for r in (app.store.data.get("runs") or []) if r.get("by") == "background"]
    last = runs[-1] if runs else None
    if not app.background_supported:
        return {"supported": False, "enabled": False, "interval_hours": None, "command": None,
                "last_run": last and last.get("finished_at"), "error": None}
    try:
        st = app.scheduler.status()
        err = None
    except (OSError, ValueError) as e:  # schtasks가 없거나 응답이 이상할 때
        st, err = {"enabled": False, "interval_hours": None}, f"작업 스케줄러를 확인하지 못했습니다: {e}"
    return {"supported": True, "enabled": st["enabled"],
            "interval_hours": st["interval_hours"] or app.store.data["settings"].get("background_hours", 3),
            "command": app.scheduler.command_text(),
            "last_run": last and last.get("finished_at"),
            "error": err or (last and "; ".join(last.get("errors") or [])) or None}


@router.route("POST", r"/api/background")
def set_background(app, body):
    if not app.background_supported:
        raise ApiError(400, "백그라운드 동기화는 Windows에서만 지원합니다(데모에서는 꺼져 있습니다)")
    enabled = bool(body.get("enabled"))
    hours = int(body.get("interval_hours") or app.store.data["settings"].get("background_hours", 3))
    if enabled:
        app.scheduler.enable(hours)  # 잘못된 간격이면 ValueError → 400
        app.store.data["settings"]["background_hours"] = hours
        app.store.save()
    else:
        app.scheduler.disable()
    return app.state()
