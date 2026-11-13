"""Canvas 로그인(토큰) / 로그아웃 (계약 9장).

로그인: 화면이 Canvas의 토큰 발급 화면(<canvas>/profile/settings)을 새 탭으로 열어 주고, 사용자가 발급한
토큰을 붙여 넣으면 실제로 Canvas에 물어봐(whoami) "OO님으로 연결됨"을 확인한 뒤 저장한다.
토큰은 Windows 암호화로 저장된다(config.save_config). 앱은 학교 포털 비밀번호를 받지 않는다.
로그아웃: 이 PC의 토큰·로그인 상태를 지운다(선택: 마감·결정 기록까지). Canvas 쪽 토큰은 사용자가 그 설정
화면에서 직접 지운다 — 붙여 넣은 토큰의 ID를 앱이 확실히 알 방법이 없어서다(Canvas API 문서 기준).
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..api import ApiError, router
from ..canvas import Canvas, CanvasError
from ..config import is_configured, normalize_url, save_config
from ..store import _default
from .. import vault

DEFAULT_CANVAS = "https://mylms.korea.ac.kr"


@router.state_section("account")
def account_state(app, ctx):
    url = app.cfg.get("canvas_url") or DEFAULT_CANVAS
    return {
        "logged_in": is_configured(app.cfg),
        "user_name": app.cfg.get("user_name"),
        "canvas_url": url,
        "token_page": f"{url}/profile/settings",   # 새 액세스 토큰 발급·삭제하는 Canvas 화면
        "encrypted": vault.available(),             # 토큰을 Windows 암호화로 저장하는지
    }


def _connect(app, url: str, token: str) -> dict:
    url, token = normalize_url(url), (token or "").strip()
    if not url or not token:
        raise ApiError(400, "Canvas 주소와 토큰을 모두 입력하세요")
    if not urlparse(url).netloc:
        raise ApiError(400, "Canvas 주소가 올바르지 않습니다")
    from .. import net
    net.reset()   # certs 폴더에 인증서를 넣고 다시 [연결]하면 바로 읽게
    try:
        me = Canvas(url, token).whoami()  # 실제로 되는지 먼저 확인
    except CanvasError as e:
        raise ApiError(400, f"연결 확인 실패: {e}") from None
    app.session_gen += 1  # 이전 계정으로 돌던 동기화는 저장하지 않는다
    app.cfg.update(canvas_url=url, token=token, user_name=me.get("name") or me.get("short_name"))
    save_config(app.cfg, app.home)
    return me


@router.route("POST", r"/api/account/login")
def login(app, body):
    if app.demo:
        raise ApiError(400, "데모 모드에서는 로그인할 수 없습니다")
    _connect(app, str(body.get("canvas_url") or app.cfg.get("canvas_url") or DEFAULT_CANVAS),
             str(body.get("token") or ""))
    app.sync_in_background()  # 로그인하자마자 첫 동기화
    return app.state()


@router.route("POST", r"/api/account/logout")
def logout(app, body):
    if app.demo:
        raise ApiError(400, "데모 모드에서는 로그아웃할 수 없습니다")
    app.session_gen += 1  # 지금 도는 동기화가 끝나도 저장하지 않게(지운 기록이 되살아나지 않게)
    for k in ("token", "user_name"):
        app.cfg.pop(k, None)
    save_config(app.cfg, app.home)
    if body.get("wipe"):  # 이 PC에 저장된 마감·결정 기록까지 지우기
        with app.store.locked():  # 저장 중인 동기화가 있으면 끝날 때까지 기다렸다가 지운다
            app.store.data = _default()
            app.store.write()
    return app.state()
