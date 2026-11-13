"""컴퓨터를 켜면 앱 열기: 등록 상태와 켜기·끄기(계약 22-8). 일은 autostart.py가 한다.

- 실제 등록은 사용자가 설정에서 켤 때(또는 사용자가 [실행]을 누른 에이전트 동작, actions.py)만. 데모는 등록하지 않는다(supported false).
- 켠 적이 있으면 앱을 켤 때마다 실행 스크립트를 지금의 파이썬·exe 경로로 다시 쓴다(백그라운드 동기화와 같은 이유).
- 테스트는 App(..., autostart=autostart.Autostart(home, runner=가짜, platform=…, user_home=tmp))로 실제 PC 설정을 건드리지 않는다.
- '브라우저도 열기'(open_browser)는 settings.autostart_open_browser(기본 true)에 둔다.
"""
from __future__ import annotations

import os
import subprocess
import threading

from .. import autostart
from ..api import ApiError, router

OPEN_KEY = "autostart_open_browser"


def open_browser_of(app) -> bool:
    v = (app.store.data.get("settings") or {}).get(OPEN_KEY, True)
    return v if isinstance(v, bool) else True


@router.on_init
def attach(app, options):
    app.quit = options.get("quit")       # 테스트는 가짜(진짜로 끄면 테스트 프로세스가 끝난다)
    fake = options.get("autostart")
    app.autostart = fake or autostart.Autostart(app.home)
    app.autostart_supported = (fake is not None) or (autostart.supported() and not app.demo)
    if app.autostart_supported and fake is None:
        try:
            app.autostart.refresh(open_browser_of(app))
        except OSError:
            pass


@router.state_section("autostart")
def autostart_state(app, ctx):
    ob = open_browser_of(app)
    if not app.autostart_supported:
        return {"supported": False, "enabled": False, "open_browser": ob, "how": None, "command": None, "error": None}
    err = None
    try:
        enabled = bool(app.autostart.status()["enabled"])
    except (OSError, ValueError, subprocess.SubprocessError) as e:   # schtasks가 없거나 응답이 이상할 때
        enabled, err = False, f"자동 실행 등록 상태를 확인하지 못했어요: {e}"
    return {"supported": True, "enabled": enabled, "open_browser": ob, "how": app.autostart.kind,
            "command": app.autostart.command_text(ob), "error": err}


@router.route("POST", r"/api/autostart")
def set_autostart(app, body):
    """{"enabled": bool, "open_browser"?: bool} → 등록/해제 → 새 state. 실패는 400."""
    if not app.autostart_supported:
        raise ApiError(400, "이 PC에서는 '컴퓨터를 켜면 앱 열기'를 쓸 수 없어요(데모에서는 꺼져 있어요)")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "enabled는 true 또는 false여야 해요")
    ob = body.get("open_browser", open_browser_of(app))
    if not isinstance(ob, bool):
        raise ApiError(400, "open_browser는 true 또는 false여야 해요")
    try:
        if enabled:
            app.autostart.enable(ob)
        else:
            app.autostart.disable()
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise ApiError(400, str(e) or "자동 실행을 바꾸지 못했어요") from None
    if (app.store.data.get("settings") or {}).get(OPEN_KEY) != ob:
        app.store.data["settings"][OPEN_KEY] = ob
        app.store.save()
    return app.state()


def _quit_now(app) -> None:
    from .update import _stop_serving
    _stop_serving(app)        # 돌던 동기화가 저장을 마칠 때까지 잠깐 기다리고 포트를 비운다
    os._exit(0)


@router.route("POST", r"/api/app/quit")
def quit_app(app, body):
    """앱 끄기(계약 22-8): 컴퓨터를 켤 때 창 없이 켜진 앱도 끌 수 있게. 응답을 보낸 뒤 잠깐 있다가 서버를 멈추고 프로세스를 끝낸다."""
    fn = app.quit or (lambda: _quit_now(app))
    threading.Timer(0.6, fn).start()
    return {"ok": True, "message": "앱을 껐어요. 다시 열려면 LMS Agent를 실행하세요(컴퓨터를 켜면 앱 열기를 켜 두었다면 다음 로그인 때 저절로 열려요)"}
