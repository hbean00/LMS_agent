"""업데이트 확인·적용 (계약 16장). 일은 updater.py가 하고, 여기는 경로·상태 칸·백그라운드 실행·다시 시작만.

- 확인은 앱을 켤 때(cli serve) 하루 한 번 + 설정의 [지금 확인]. 테스트·데모의 App은 저절로 확인하지 않는다
  (start_background_checks는 cli만 부른다). 테스트는 App(..., update_fetch=가짜, update_how=…, relaunch=가짜)로
  네트워크·실제 다시 시작을 막는다.
- 적용은 항상 사용자가 [지금 업데이트]를 눌러야 한다. exe는 받기·확인 → 교체 → 같은 포트로 다시 켜기까지 한 번에,
  pip·git은 설치한 뒤 [다시 시작]을 한 번 더 누른다(터미널에서 켠 경우가 많아서).
"""
from __future__ import annotations

import os
import threading
import time

from .. import updater
from ..api import ApiError, router


@router.on_init
def attach(app, options):
    app.updater = updater.Updater(app.store, app.home, fetch=options.get("update_fetch"),
                                  how=options.get("update_how"), exe_path=options.get("update_exe"),
                                  root=options.get("update_root"), runner=options.get("update_runner"),
                                  platform=options.get("update_platform"))
    app.relaunch = options.get("relaunch") or updater.relaunch
    app.relaunch_after_exit = options.get("relaunch_after_exit") or updater.relaunch_after_exit
    app.restart_delay = float(options.get("restart_delay", 1.2))


@router.state_section("update")
def update_state(app, ctx):
    return app.updater.status(demo=app.demo)


@router.route("POST", r"/api/update/check")
def check_now(app, body):
    """설정의 [지금 확인]. 확인을 꺼 두었어도 누르면 한 번 묻는다(사용자가 직접 누른 것)."""
    app.updater.check()
    return app.state()


@router.route("POST", r"/api/update/apply")
def apply_now(app, body):
    """[지금 업데이트]. 백그라운드에서 받고 확인한다 → state.update.phase가 "applying"인 동안 화면이 다시 묻는다."""
    if app.demo:
        raise ApiError(400, "데모에서는 업데이트하지 않아요. 실제 앱에서 해 주세요")
    u = app.updater
    if not u.available():
        raise ApiError(400, "지금 쓰는 버전이 최신이에요")
    plan = u.plan()
    if not plan["can_apply"]:
        raise ApiError(400, plan["reason"])
    if not u.start(lambda: _apply_worker(app)):
        raise ApiError(409, "이미 확인하거나 업데이트하는 중이에요. 잠시 뒤 다시 눌러 주세요")
    return app.state()


@router.route("POST", r"/api/update/restart")
def restart_now(app, body):
    """pip·git 설치를 마친 뒤 [다시 시작]. 응답을 보낸 뒤 잠깐 있다가 새 프로세스로 바뀐다."""
    if app.demo:
        raise ApiError(400, "데모에서는 다시 시작하지 않아요")
    u = app.updater
    if not (u.result and u.result.get("ok") and u.result.get("restart")) or u.phase != "idle":
        raise ApiError(400, "다시 시작할 업데이트가 없어요")
    u.phase = "restarting"
    threading.Timer(app.restart_delay, lambda: restart(app)).start()
    return app.state()


@router.route("POST", r"/api/update/dismiss")
def dismiss(app, body):
    """배너 닫기(새 버전 알림 → 그보다 새 버전이 나올 때까지, 방금 업데이트 알림 → 다시 안 보임)."""
    app.updater.dismiss()
    return app.state()


def _apply_worker(app) -> None:
    u = app.updater
    try:
        res = u.prepare()
    except Exception as e:  # 예상 못 한 실패라도 화면이 '받는 중'에 멈춰 있지 않게
        u.finish_phase({"ok": False, "message": f"업데이트하지 못했어요({type(e).__name__})", "restart": False})
        return
    finish = res.get("finish")
    if not (res.get("ok") and finish):          # pip·git(설치까지 끝) 또는 실패
        u.finish_phase(res)
        return
    # exe: 화면이 '다시 켜는 중'을 볼 틈을 준 뒤 교체한다. 교체한 뒤에는 새 모듈을 불러오지 않는다(updater.py 맨 위 주석)
    u.result = {"ok": True, "message": res["message"], "restart": True}
    u.phase = "restarting"
    time.sleep(app.restart_delay)
    try:
        finish()
    except OSError as e:
        if os.name == "nt" and isinstance(e, PermissionError):   # 실행 파일이 잠겨 이름을 못 바꿨다 → 끝난 뒤 바꾸고 다시 켠다
            _stop_serving(app)
            app.relaunch_after_exit(res["new"], u.exe_path, _argv(app))
            return
        try:
            res["new"].unlink()
        except OSError:
            pass
        u.finish_phase({"ok": False, "message": f"실행 파일을 바꾸지 못했어요({e}). 릴리스 페이지에서 받아 바꿔 주세요",
                        "restart": False})
        return
    restart(app)


def _argv(app) -> list[str]:
    return updater.restart_argv(app.updater.how, app.port, app.updater.exe_path)


def _stop_serving(app) -> None:
    """포트를 비운다(새 프로세스가 같은 포트로 켜지게). 돌던 동기화가 저장을 마칠 때까지 잠깐 기다린다."""
    if app.lock.acquire(timeout=30):
        app.lock.release()
    httpd = getattr(app, "httpd", None)
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()


def restart(app) -> None:
    """같은 포트·브라우저 새로 열지 않음으로 새 버전을 켜고 이 프로세스는 끝낸다. 화면은 다시 연결되면 새로 고친다."""
    argv = _argv(app)
    _stop_serving(app)
    try:
        app.relaunch(argv)
    except OSError as e:
        print(f"다시 켜지 못했어요({e}). 앱을 직접 다시 켜 주세요: {' '.join(argv)}", flush=True)
        os._exit(1)


def start_background_checks(app, first_delay: float = 8.0, every: float = 3600.0) -> threading.Thread:
    """cli serve만 부른다: 켜고 조금 뒤 한 번, 그 뒤 한 시간마다 '하루가 지났는지' 보고 GitHub에 묻는다."""
    def loop():
        time.sleep(first_delay)
        while True:
            try:
                if app.updater.due():
                    app.updater.check()
            except Exception:   # 확인 실패가 앱을 멈추게 하지 않는다(오류는 캐시의 error에 남는다)
                pass
            time.sleep(every)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
