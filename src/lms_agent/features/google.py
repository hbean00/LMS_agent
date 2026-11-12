"""구글 캘린더 자동 반영: 연결·콜백·반영·해제, 바뀌면 자동 반영. 계약 6장."""
from __future__ import annotations

from urllib.parse import urlencode

from ..api import ApiError, Response, router
from ..config import google_client
from ..gcal import GoogleError
from ..gsync import GoogleLink


@router.on_init
def attach(app, options):
    # 데모·테스트는 가짜 구글(google=(id, secret), google_urls=…)을 넘긴다
    app.google = GoogleLink(app.cfg, app.home, options.get("google") or google_client(),
                            urls=options.get("google_urls"))


@router.on("changed")
def push_after_change(app, **_):
    app.google.push_soon(app.store)


@router.state_section("google")
def google_state(app, ctx):
    return app.google.status(app.store)


@router.route("GET", r"/", auth=False, priority=10)
def oauth_callback(app, query):
    """구글 동의 뒤 루트로 ?state=&code=(또는 error=)가 붙어 돌아온다. 아니면 다음 경로(페이지)로 넘긴다."""
    if "state" not in query or ("code" not in query and "error" not in query):
        return None
    try:
        app.google.finish(query)
        app.google.push_soon(app.store)
        msg = {"google": "ok"}
    except GoogleError as e:
        msg = {"google": "error", "msg": str(e)}
    return Response(302, b"", headers={"Location": "/?" + urlencode(msg) + "#settings"})


@router.route("POST", r"/api/google/connect")
def connect(app, body):
    try:
        return {"url": app.google.start(f"http://127.0.0.1:{app.port}/")}
    except GoogleError as e:
        raise ApiError(400, str(e)) from None


@router.route("POST", r"/api/google/push")
def push(app, body):
    try:
        app.google.push(app.store)
    except GoogleError as e:
        raise ApiError(502, str(e)) from None
    return app.state()


@router.route("POST", r"/api/google/disconnect")
def disconnect(app, body):
    app.google.disconnect()
    return app.state()
