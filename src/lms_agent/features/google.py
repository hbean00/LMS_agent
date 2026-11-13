"""구글 캘린더 자동 반영: 연결·콜백·반영·해제, 바뀌면 자동 반영(계약 6장) + 다른 일정 보기(계약 23장).

다른 일정 가져오는 때: 앱을 켤 때·동기화 뒤·15분마다(앱이 켜져 있을 때)·켜기/캘린더 고르기/다시 가져오기 요청·읽기 동의에서 돌아올 때.
모두 백그라운드(GoogleLink.fetch_soon, state.google.read.fetching). 15분 타이머는 기능을 쓸 때만 시작하는 데몬 스레드 하나 —
간격은 App(..., gcal_read_every=초)로 바꾸고, 0이면 타이머를 켜지 않는다(테스트). 상태 칸은 저장된 것만 읽는다(네트워크 없음).
"""
from __future__ import annotations

import threading
from urllib.parse import urlencode

from .. import gevents
from ..api import ApiError, Response, router
from ..config import google_client
from ..gcal import GoogleError
from ..gsync import GoogleLink

READ_EVERY = 15 * 60       # 앱이 켜져 있을 때 다른 일정을 다시 가져오는 간격(초)
AFTER_SYNC_GAP = 120       # 동기화 뒤 가져오기: 이보다 최근에 가져왔으면 건너뜀(켤 때 가져오기와 겹치지 않게)


@router.on_init
def attach(app, options):
    # 데모·테스트는 가짜 구글(google=(id, secret), google_urls=…)을 넘긴다
    app.google = GoogleLink(app.cfg, app.home, options.get("google") or google_client(),
                            urls=options.get("google_urls"))
    every = options.get("gcal_read_every", READ_EVERY)
    app.gcal_read_every = float(every) if isinstance(every, (int, float)) and not isinstance(every, bool) else 0.0
    app.gcal_read_stop = threading.Event()
    app.gcal_read_timer = None
    app.gcal_seen_sync = _sync_mark(app.store)
    if _active(app):   # 앱을 켤 때(켜 둔 사람만)
        app.google.fetch_soon(app.store)
        _ensure_timer(app)


def _active(app) -> bool:
    g = app.google
    return bool(g.available and g.read_granted and gevents.enabled(app.store))


def _ensure_timer(app) -> None:
    """15분마다 가져오는 데몬 스레드(하나만). 간격이 0이면 켜지 않는다. 기능이 꺼져 있으면 그 차례는 쉰다."""
    if not app.gcal_read_every or (app.gcal_read_timer is not None and app.gcal_read_timer.is_alive()):
        return

    def loop():
        while not app.gcal_read_stop.wait(app.gcal_read_every):
            try:
                if _active(app):
                    app.google.fetch_soon(app.store, min_gap=app.gcal_read_every / 2)
            except Exception:   # noqa: BLE001 — 타이머가 멈추지 않게(오류는 read.error에)
                pass

    app.gcal_read_timer = threading.Thread(target=loop, name="gcal-read-timer", daemon=True)
    app.gcal_read_timer.start()


@router.on("changed")
def push_after_change(app, **_):
    app.google.push_soon(app.store)


def _sync_mark(store):
    """동기화마다 바뀌는 표시: Canvas 원본(cache)은 동기화 때만 통째로 바뀐다(같은 초에 두 번 동기화해도 구별)."""
    return id(store.data.get("cache")), store.data.get("last_sync")


@router.on("changed")
def fetch_after_sync(app, **_):
    """동기화가 끝난 뒤(원본이 바뀌었을 때만 — 항목 결정·설정 변경으로는 가져오지 않는다)."""
    mark = _sync_mark(app.store)
    if mark == app.gcal_seen_sync:
        return
    app.gcal_seen_sync = mark
    if _active(app):
        app.google.fetch_soon(app.store, min_gap=AFTER_SYNC_GAP)


@router.state_section("google")
def google_state(app, ctx):
    return app.google.status(app.store)


@router.state_section("events")
def events_state(app, ctx):
    """대시보드에 보일 다른 일정(계약 23-2) — 저장된 것만, 꺼져 있으면 []."""
    return gevents.window(app.store)


@router.route("GET", r"/", auth=False, priority=10)
def oauth_callback(app, query):
    """구글 동의 뒤 루트로 ?state=&code=(또는 error=)가 붙어 돌아온다. 아니면 다음 경로(페이지)로 넘긴다.
    읽기 동의(다른 일정 보기)였으면 read=ok|denied(체크를 뺌)|cancelled를 더 붙인다 — 쓰기 연결은 그대로라 google=ok."""
    if "state" not in query or ("code" not in query and "error" not in query):
        return None
    purpose = app.google.flow_purpose
    try:
        info = app.google.finish(query)
    except GoogleError as e:
        msg = {"google": "error", "msg": str(e)}
        if purpose == "read":
            msg["read"] = "cancelled"
        return Response(302, b"", headers={"Location": "/?" + urlencode(msg) + "#settings"})
    msg = {"google": "ok"}
    if info.get("purpose") == "read":
        msg["read"] = "ok" if info.get("read_granted") else "denied"
        try:
            _locked(app, lambda: _after_read_consent(app.store, info.get("read_granted")))
        except ApiError:   # 저장 잠금 시간 초과 — 메모리에만 바꿔 두고 다음 저장 때 파일에 남긴다
            _after_read_consent(app.store, info.get("read_granted"))
        if info.get("read_granted"):
            app.google.fetch_soon(app.store)   # 권한을 받으면 바로 가져온다
            _ensure_timer(app)
    else:
        app.google.push_soon(app.store)
        if _active(app):   # 예전에 켜 둔 사람이 다시 연결했고 읽기 권한도 있으면
            app.google.fetch_soon(app.store)
            _ensure_timer(app)
    return Response(302, b"", headers={"Location": "/?" + urlencode(msg) + "#settings"})


def _after_read_consent(store, granted: bool) -> None:
    if granted:
        gevents.set_enabled(store, True)
        gevents.set_error(store, None)
    else:   # 동의 화면에서 읽기 체크를 뺐다 — 켜지 않고 안내만
        gevents.set_error(store, gevents.READ_DENIED)


def _locked(app, fn):
    """잠금 안에서 다시 읽고 바꿔 쓴다(백그라운드 동기화가 그 사이 저장했을 수 있다). 잠금 시간 초과는 409."""
    store = app.store
    try:
        with store.locked():
            store.reload_if_changed()
            out = fn()
            store.write()
            return out
    except TimeoutError:
        raise ApiError(409, "다른 동기화가 저장 중이에요. 잠시 뒤 다시 눌러 주세요") from None


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
    app.google.disconnect(app.store)   # 가져온 다른 일정·'다른 일정 보기' 설정도 지운다(계약 23-1)
    return app.state()


# ------------------------------------------------------------ 다른 일정 보기(계약 23-2)

def _need_link(app) -> None:
    g = app.google
    if not g.available:
        raise ApiError(400, "구글 연결용 클라이언트 ID가 없어요(설정 → 구글 캘린더의 안내를 봐 주세요)")
    if not g.connected:
        raise ApiError(400, gevents.NOT_CONNECTED)


@router.route("POST", r"/api/google/read")
def read_toggle(app, body):
    """{"enabled": bool}. 끄면 저장된 일정을 바로 지운다 → 새 state. 켤 때 권한이 없으면 {"need_consent": true, "url": 동의 화면}
    (아직 켜지 않음 — 동의에서 돌아와 권한을 받으면 켜고 바로 가져온다). 권한이 있으면 켜고 가져오기 시작 → 새 state."""
    on = body.get("enabled")
    if not isinstance(on, bool):
        raise ApiError(400, "enabled는 true 또는 false여야 해요")
    if not on:
        _locked(app, lambda: gevents.set_enabled(app.store, False))
        return app.state()
    _need_link(app)
    if not app.google.read_granted:
        try:
            url = app.google.start(f"http://127.0.0.1:{app.port}/", read=True)
        except GoogleError as e:
            raise ApiError(400, str(e)) from None
        return {"need_consent": True, "url": url}

    def turn_on():
        gevents.set_enabled(app.store, True)
        gevents.set_error(app.store, None)
    _locked(app, turn_on)
    app.google.fetch_soon(app.store)
    _ensure_timer(app)
    return app.state()


def _need_reading(app) -> None:
    if not gevents.enabled(app.store):
        raise ApiError(400, "먼저 '다른 일정도 대시보드에 보기'를 켜 주세요(설정 → 구글 캘린더)")
    _need_link(app)
    if not app.google.read_granted:
        raise ApiError(400, gevents.READ_NEEDED)


@router.route("POST", r"/api/google/read/calendars")
def read_calendars(app, body):
    """{"selected": [캘린더 id…]} — 대시보드에 보일 캘린더. 모르는 id는 400. 빠진 캘린더의 일정은 바로 지우고 다시 가져온다 → 새 state."""
    _need_reading(app)
    sel = body.get("selected")
    try:
        _locked(app, lambda: gevents.set_selection(app.store, sel))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    app.google.fetch_soon(app.store)
    return app.state()


@router.route("POST", r"/api/google/read/refresh")
def read_refresh(app, body):
    """지금 다시 가져오기(백그라운드, read.fetching true) → 새 state."""
    _need_reading(app)
    app.google.fetch_soon(app.store)
    return app.state()
