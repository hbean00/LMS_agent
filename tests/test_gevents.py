"""구글 캘린더의 다른 일정(계약 23장): 증분 동의(읽기 권한은 켤 때만)·체크를 뺀 경우·가져오기와 정리(종일·비공개·한가함·반복)·
캘린더 고르기·끄면 지움·연결 해제·401/403·대시보드 창·15분 타이머·색(23-5).

가짜 구글(mock_google)의 사용자 캘린더와 데모 일정(오늘 기준)을 쓴다. 시각은 문자열이 아니라 같은 순간인지로 비교한다(CI는 UTC).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone

import pytest

from lms_agent import core, gcal, gevents
from lms_agent.canvas import Canvas
from lms_agent.gcal import GoogleCalendar, GoogleError, OAuthFlow
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, DEMO_PRIMARY, start_mock_google, urls
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, parse_iso

TZ = local_tz()
CLUB = "club.demo@group.calendar.google.com"
HOLIDAY = "ko.south_korea#holiday@group.v.calendar.google.com"
FAMILY = "family.demo@group.calendar.google.com"
DEPT = "dept.demo@group.calendar.google.com"
KST = timezone(timedelta(hours=9))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def call(url, method="GET", token=None, body=None, follow=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("X-LMS-Agent", token)
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post(env, path, body=None):
    app, url, _ = env
    code, raw, _ = call(url + path, "POST", app.token, body if body is not None else {})
    return code, json.loads(raw)


def state(env):
    app, url, _ = env
    return json.loads(call(url + "/api/state", token=app.token)[1])


def consent(env, auth_url):
    """브라우저처럼: 동의 화면 → 302로 앱 루트(?state=&code=) → 앱이 /?google=…#settings로 → 그 Location."""
    app, url, _ = env
    code, _, headers = call(auth_url, follow=False)
    assert code == 302
    code, _, headers = call(headers["Location"], follow=False)
    assert code == 302
    return headers["Location"]


def wait(cond, timeout=8, what="조건"):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.03)
    raise AssertionError(f"{what}이(가) 끝나지 않았습니다")


def wait_push(app):
    wait(lambda: not app.google.pushing and (app.store.data.get("gcal") or {}).get("at"), what="구글 반영")


def wait_fetch(app):
    wait(lambda: not app.google.fetching, what="다른 일정 가져오기")


def connect(env):
    """쓰기 연결(처음 연결) → 첫 반영('LMS 마감' 캘린더가 생김)까지."""
    app = env[0]
    _, body = post(env, "/api/google/connect")
    assert consent(env, body["url"]) == "/?google=ok#settings"
    wait_push(app)


def enable_read(env):
    """연결된 상태에서 다른 일정 보기 켜기 → 동의(읽기) → 돌아오면 바로 가져옴."""
    app = env[0]
    code, body = post(env, "/api/google/read", {"enabled": True})
    assert code == 200 and body["need_consent"] is True
    loc = consent(env, body["url"])
    wait_fetch(app)
    return loc


@pytest.fixture()
def env(tmp_path):
    cmock, cbase = start_mock_canvas()
    gmock, gbase = start_mock_google()
    store = Store(tmp_path / "state.json")
    app = App(store, tmp_path, cfg={"canvas_url": cbase, "token": DEMO_TOKEN}, demo=True,
              google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=urls(gbase), gcal_read_every=0)
    core.sync(Canvas(cbase, DEMO_TOKEN), store)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}", gmock
    httpd.shutdown()
    cmock.shutdown()
    gmock.shutdown()
    gmock.server_close()


def at(n, hh=0, mm=0):
    return datetime.combine(date.today() + timedelta(days=n), dtime(hh, mm), tzinfo=TZ)


# ---------------------------------------------------------------- 권한(23-1)

def test_consent_url_scopes_only_when_reading():
    first = OAuthFlow(DEMO_CLIENT_ID, "http://127.0.0.1:1/")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(first.authorization_url()).query))
    assert q["scope"] == gcal.SCOPE and "include_granted_scopes" not in q   # 처음 연결은 예전과 같다(쓰기만)
    read = OAuthFlow(DEMO_CLIENT_ID, "http://127.0.0.1:1/")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(read.authorization_url(read=True)).query))
    assert q["scope"].split() == [gcal.SCOPE, gcal.SCOPE_LIST, gcal.SCOPE_EVENTS]
    assert q["include_granted_scopes"] == "true" and q["prompt"] == "consent" and q["access_type"] == "offline"
    assert read.read is True and first.read is False


def test_read_granted_from_token_scope():
    assert not gcal.read_granted("")
    assert not gcal.read_granted(gcal.SCOPE)
    assert not gcal.read_granted(f"{gcal.SCOPE} {gcal.SCOPE_LIST}")             # 일정 읽기가 빠짐
    assert gcal.read_granted(f"{gcal.SCOPE} {gcal.SCOPE_LIST} {gcal.SCOPE_EVENTS}")
    assert gcal.read_granted("https://www.googleapis.com/auth/calendar.readonly")   # 더 넓은 권한도 됨


def test_mock_refuses_reading_without_read_scope():
    srv, base = start_mock_google()
    try:
        u = urls(base)
        flow = OAuthFlow(DEMO_CLIENT_ID, "http://127.0.0.1:1/", DEMO_CLIENT_SECRET,
                         auth_url=u["auth_url"], token_url=u["token_url"])
        try:
            urllib.request.build_opener(NoRedirect).open(flow.authorization_url(), timeout=5)
        except urllib.error.HTTPError as e:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(e.headers["Location"]).query))
        tokens = flow.exchange(q["code"], q["state"])
        cal = GoogleCalendar(tokens, DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, api_base=u["api_base"],
                             token_url=u["token_url"], backoff=(0, 0))
        with pytest.raises(GoogleError) as ei:
            cal.list_calendars()
        assert ei.value.status == 403 and ei.value.reason == "insufficientPermissions"
        with pytest.raises(GoogleError) as ei:
            cal.list_events(DEMO_PRIMARY, at(0), at(1))
        assert ei.value.status == 403
        assert tokens["access_token"] not in str(ei.value) and tokens["refresh_token"] not in str(ei.value)
    finally:
        srv.shutdown()
        srv.server_close()


def test_enable_read_needs_connection_first(env):
    code, body = post(env, "/api/google/read", {"enabled": True})
    assert code == 400 and "연결" in body["error"]
    code, body = post(env, "/api/google/read", {"enabled": "yes"})
    assert code == 400 and "true 또는 false" in body["error"]
    st = state(env)
    assert st["google"]["read"] == {"enabled": False, "granted": False, "calendars": [], "fetched_at": None,
                                    "count": 0, "error": None, "fetching": False}
    assert st["events"] == []


def test_incremental_consent_then_fetch_and_normalize(env):
    app, url, gmock = env
    connect(env)
    q = gmock.auth_queries[-1]
    assert q["scope"] == gcal.SCOPE and "include_granted_scopes" not in q   # 처음 연결은 쓰기만
    assert not app.google.read_granted
    assert state(env)["google"]["read"]["enabled"] is False

    loc = enable_read(env)
    assert loc == "/?google=ok&read=ok#settings"
    q = gmock.auth_queries[-1]
    assert q["scope"].split() == [gcal.SCOPE, gcal.SCOPE_LIST, gcal.SCOPE_EVENTS] and q["include_granted_scopes"] == "true"
    assert app.google.connected and app.google.read_granted

    st = state(env)
    read = st["google"]["read"]
    assert read["enabled"] and read["granted"] and read["error"] is None and read["fetching"] is False
    assert parse_iso(read["fetched_at"]) is not None
    cals = {c["id"]: c for c in read["calendars"]}
    lms = app.cfg["google"]["calendar_id"]
    assert lms not in cals and gcal.CALENDAR_NAME not in {c["name"] for c in cals.values()}   # 앱의 캘린더는 읽지 않는다
    assert set(cals) == {DEMO_PRIMARY, CLUB, HOLIDAY, FAMILY, DEPT}
    assert cals[DEMO_PRIMARY] == {"id": DEMO_PRIMARY, "name": "내 캘린더", "primary": True, "selected": True,
                                  "color": "#f691b2", "access": "owner"}
    assert cals[FAMILY]["color"] is None and cals[FAMILY]["access"] == "freeBusyReader"   # 색 없음 → 화면이 팔레트에서
    colors = [c["color"] for c in cals.values() if c["color"]]
    assert len(colors) == len(set(colors)) == 4                 # 데모 캘린더 색은 서로 다르다(23-5)
    assert cals[DEPT]["selected"] is False                    # 구글에서 표시를 꺼 둔 캘린더는 기본 선택에서 빠진다
    assert all(c["selected"] for k, c in cals.items() if k != DEPT)

    ev = st["events"]
    assert read["count"] == len(ev) == 12
    starts = [parse_iso(e["start"]) for e in ev]
    assert starts == sorted(starts)
    for e in ev:
        assert set(e) == {"id", "calendar_id", "calendar_name", "color", "title", "start", "end", "all_day",
                          "location", "url", "busy"}
        assert e["id"].startswith(e["calendar_id"] + ":") and e["color"] == cals[e["calendar_id"]]["color"]
    by = {e["title"]: e for e in ev}
    study = by["스터디 모임"]
    assert parse_iso(study["start"]) == at(1, 19) and parse_iso(study["end"]) == at(1, 21)
    assert study["location"] == "중앙도서관" and study["busy"] is True and study["all_day"] is False
    assert study["calendar_name"] == "내 캘린더" and study["url"].startswith(gmock.base_url)
    jobs = [e for e in ev if e["title"] == "알바"]               # 매주 반복(4번) → 창(+21일) 안의 3번
    assert [parse_iso(e["start"]) for e in jobs] == [at(2, 18), at(9, 18), at(16, 18)]
    assert len({e["id"] for e in jobs}) == 3
    bday = by["엄마 생신"]                                       # 종일·한가함
    assert bday["all_day"] is True and bday["busy"] is False
    assert parse_iso(bday["start"]) == at(10) and parse_iso(bday["end"]) == at(11)
    mt = by["동아리 MT"]
    assert mt["all_day"] and parse_iso(mt["start"]) == at(12) and parse_iso(mt["end"]) == at(14)
    assert by["(제목 없음)"]["calendar_id"] == DEMO_PRIMARY
    busy_only = [e for e in ev if e["title"] == "바쁨"]          # 비공개로 공유된 일정·바쁨만 보이는 캘린더
    assert {e["calendar_id"] for e in busy_only} == {CLUB, FAMILY}
    assert all(e["location"] is None and e["url"] is None and e["busy"] for e in busy_only)
    assert by["임시 공휴일(데모)"]["busy"] is False
    assert "취소된 약속" not in by and "학과 설명회" not in by

    # 설명·참석자·토큰은 어디에도 저장·응답되지 않는다
    raw_state = (app.home / "state.json").read_text("utf-8")
    raw_api = call(url + "/api/state", token=app.token)[1].decode()
    tokens = app.cfg["google"]["tokens"]
    for text in (raw_state, raw_api):
        assert "비밀 메모" not in text and "friend@example.com" not in text
        assert tokens["access_token"] not in text and tokens["refresh_token"] not in text


def test_unchecked_read_scope_keeps_write_link(env):
    app, url, gmock = env
    connect(env)
    gmock.grant_scope = gcal.SCOPE                               # 동의 화면에서 읽기 체크를 뺌
    code, body = post(env, "/api/google/read", {"enabled": True})
    assert consent(env, body["url"]) == "/?google=ok&read=denied#settings"
    assert app.google.connected and not app.google.read_granted
    read = state(env)["google"]["read"]
    assert read["enabled"] is False and read["granted"] is False and "허락하지 않았어요" in read["error"]
    assert not any("calendarList" in p for _, p in gmock.requests)   # 가져오지 않았다
    code, st = post(env, "/api/google/push")                     # 쓰기(반영)는 그대로
    assert code == 200 and st["google"]["connected"] and st["google"]["error"] is None
    # 다시 켜면 다시 동의를 묻는다(이번엔 허락)
    gmock.grant_scope = None
    assert enable_read(env) == "/?google=ok&read=ok#settings"
    read = state(env)["google"]["read"]
    assert read["enabled"] and read["granted"] and read["error"] is None and read["count"] == 12


def test_cancelled_read_consent(env):
    app, url, gmock = env
    connect(env)
    gmock.auth_error = "access_denied"
    _, body = post(env, "/api/google/read", {"enabled": True})
    loc = consent(env, body["url"])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
    assert q["google"] == "error" and q["read"] == "cancelled" and "그대로" in q["msg"]
    assert app.google.connected and not app.google.read_granted       # 쓰기 연결은 그대로


def test_calendar_selection(env):
    app = env[0]
    connect(env)
    enable_read(env)
    code, st = post(env, "/api/google/read/calendars", {"selected": [DEMO_PRIMARY]})
    assert code == 200
    assert {e["calendar_id"] for e in st["events"]} == {DEMO_PRIMARY}   # 빠진 캘린더는 바로 사라진다
    wait_fetch(app)
    st = state(env)
    assert {c["id"] for c in st["google"]["read"]["calendars"] if c["selected"]} == {DEMO_PRIMARY}
    assert {e["calendar_id"] for e in st["events"]} == {DEMO_PRIMARY} and len(st["events"]) == 7
    # 구글에서 꺼 둔 캘린더도 고를 수 있다
    post(env, "/api/google/read/calendars", {"selected": [DEPT, CLUB]})
    wait_fetch(app)
    st = state(env)
    assert {e["calendar_id"] for e in st["events"]} == {DEPT, CLUB}
    assert any(e["title"] == "학과 설명회" and e["color"] == "#cd74e6" for e in st["events"])
    assert app.store.data["gcal_events"]["selected"] == [CLUB, DEPT]       # 캘린더 목록 순서로
    # 잘못된 것
    for bad in ({"selected": ["nope@example.com"]}, {"selected": "primary"}, {"selected": [1]}, {}):
        code, body = post(env, "/api/google/read/calendars", bad)
        assert code == 400 and body["error"]
    # 빈 목록 = 아무것도 안 보임
    code, st = post(env, "/api/google/read/calendars", {"selected": []})
    assert code == 200 and st["events"] == []


def test_refresh_route_and_disable_wipes(env):
    app, url, gmock = env
    code, body = post(env, "/api/google/read/refresh")
    assert code == 400 and "켜 주세요" in body["error"]
    connect(env)
    enable_read(env)
    n = sum(1 for _, p in gmock.requests if "calendarList" in p)
    code, st = post(env, "/api/google/read/refresh")
    assert code == 200
    wait_fetch(app)
    assert sum(1 for _, p in gmock.requests if "calendarList" in p) == n + 1
    # 끄면 바로 지운다
    code, st = post(env, "/api/google/read", {"enabled": False})
    assert code == 200 and st["events"] == []
    assert st["google"]["read"]["enabled"] is False and st["google"]["read"]["calendars"] == []
    box = app.store.data.get("gcal_events") or {}
    assert not box.get("events") and not box.get("calendars")
    assert "스터디 모임" not in (app.home / "state.json").read_text("utf-8")
    # 권한은 남아 있으니 다시 켜면 동의 없이 바로 가져온다
    code, st = post(env, "/api/google/read", {"enabled": True})
    assert code == 200 and "need_consent" not in st and st["google"]["read"]["enabled"]
    wait_fetch(app)
    assert state(env)["google"]["read"]["count"] == 12


def test_disconnect_wipes_everything(env):
    app = env[0]
    connect(env)
    enable_read(env)
    post(env, "/api/google/read/calendars", {"selected": [CLUB]})
    wait_fetch(app)
    code, st = post(env, "/api/google/disconnect")
    assert code == 200 and not st["google"]["connected"] and st["events"] == []
    assert st["google"]["read"]["enabled"] is False and st["google"]["read"]["granted"] is False
    assert "gcal_events" not in app.store.data and "gcal_read" not in app.store.data["settings"]
    text = (app.home / "state.json").read_text("utf-8")
    assert "동아리" not in text and CLUB not in text


def test_403_marks_not_granted_and_keeps_events(env):
    app, url, gmock = env
    connect(env)
    enable_read(env)
    gmock.faults.append({"method": "GET", "path": r"/calendarList$", "status": 403, "count": 1,
                         "body": {"error": {"code": 403, "message": "x",
                                            "errors": [{"reason": "insufficientPermissions"}]}}})
    post(env, "/api/google/read/refresh")
    wait_fetch(app)
    read = state(env)["google"]["read"]
    assert read["granted"] is False and "허락하지 않았어요" in read["error"]
    assert read["count"] == 12                                   # 지난 일정은 그대로
    assert app.google.connected and app.cfg["google"]["read_denied"] is True
    code, body = post(env, "/api/google/read/refresh")
    assert code == 400 and "권한" in body["error"]
    code, body = post(env, "/api/google/read", {"enabled": True})   # 다시 켜면 다시 동의
    assert code == 200 and body["need_consent"]
    assert consent(env, body["url"]) == "/?google=ok&read=ok#settings"
    wait_fetch(app)
    read = state(env)["google"]["read"]
    assert read["granted"] and read["error"] is None and "read_denied" not in app.cfg["google"]


def test_server_error_keeps_events(env):
    app, url, gmock = env
    connect(env)
    enable_read(env)
    app.google.urls = {**app.google.urls}
    gmock.faults.append({"method": "GET", "path": r"/events$", "status": 500, "count": 10})
    post(env, "/api/google/read/refresh")
    wait_fetch(app)
    read = state(env)["google"]["read"]
    assert read["granted"] and "서버 오류" in read["error"] and read["count"] == 12


def test_401_drops_connection_and_events(env):
    app, url, gmock = env
    connect(env)
    enable_read(env)
    gmock.data["refresh_tokens"].clear()
    gmock.data["access_tokens"].clear()                          # 구글 쪽에서 연결이 끊김
    post(env, "/api/google/read/refresh")
    wait_fetch(app)
    st = state(env)
    assert not st["google"]["connected"] and st["events"] == []
    assert st["google"]["read"]["granted"] is False and "연결" in st["google"]["read"]["error"]
    assert not (app.store.data.get("gcal_events") or {}).get("events")


def test_fetch_on_start_and_after_sync(tmp_path):
    gmock, gbase = start_mock_google()
    cmock, cbase = start_mock_canvas()
    try:
        u = urls(gbase)
        flow = OAuthFlow(DEMO_CLIENT_ID, "http://127.0.0.1:1/", DEMO_CLIENT_SECRET,
                         auth_url=u["auth_url"], token_url=u["token_url"])
        try:
            urllib.request.build_opener(NoRedirect).open(flow.authorization_url(read=True), timeout=5)
        except urllib.error.HTTPError as e:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(e.headers["Location"]).query))
        tokens = flow.exchange(q["code"], q["state"])
        store = Store(tmp_path / "state.json")
        core.sync(Canvas(cbase, DEMO_TOKEN), store)
        store.data["settings"]["gcal_read"] = True
        store.save()
        cfg = {"canvas_url": cbase, "token": DEMO_TOKEN, "google": {"tokens": tokens}}
        app = App(store, tmp_path, cfg=cfg, demo=True, google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET),
                  google_urls=u, gcal_read_every=0)
        assert app.google.fetching                               # 켤 때 바로
        wait_fetch(app)
        assert len(gevents.window(store)) == 12 and app.gcal_read_timer is None   # 간격 0 → 타이머 없음
        lists = lambda: sum(1 for _, p in gmock.requests if "calendarList" in p)  # noqa: E731
        n = lists()
        app.emit("changed")                                      # 동기화가 아닌 변경 → 가져오지 않음
        assert not app.google.fetching and lists() == n
        app.google.fetch_started = None                          # 켤 때 가져온 지 2분이 지난 것처럼
        app.do_sync()                                            # 동기화 뒤 → 가져옴
        wait_fetch(app)
        assert lists() == n + 1
    finally:
        gmock.shutdown()
        gmock.server_close()
        cmock.shutdown()


def test_timer_fetches_periodically_and_stops(tmp_path):
    gmock, gbase = start_mock_google()
    cmock, cbase = start_mock_canvas()
    try:
        store = Store(tmp_path / "state.json")
        core.sync(Canvas(cbase, DEMO_TOKEN), store)
        app = App(store, tmp_path, cfg={"canvas_url": cbase, "token": DEMO_TOKEN}, demo=True,
                  google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=urls(gbase), gcal_read_every=0.1)
        assert app.gcal_read_timer is None                       # 기능을 안 쓰면 타이머도 없다
        httpd = start_server(app, 0)
        serve_in_thread(httpd)
        env = (app, f"http://127.0.0.1:{httpd.server_address[1]}", gmock)
        connect(env)
        enable_read(env)
        assert app.gcal_read_timer is not None and app.gcal_read_timer.is_alive()
        lists = lambda: sum(1 for _, p in gmock.requests if "calendarList" in p)  # noqa: E731
        n = lists()
        wait(lambda: lists() >= n + 2, what="15분(여기선 0.1초)마다 가져오기")
        app.gcal_read_stop.set()
        app.gcal_read_timer.join(2)
        assert not app.gcal_read_timer.is_alive()
        httpd.shutdown()
    finally:
        gmock.shutdown()
        gmock.server_close()
        cmock.shutdown()


# ---------------------------------------------------------------- 정리(단위)

CAL = {"id": "cal1", "name": "개인", "color": "#123abc", "access": "owner"}


def test_color_normalization():
    assert gevents.color("#9fc6e7") == "#9fc6e7"
    assert gevents.color("#9FC6E7") == "#9fc6e7"                 # 대문자 → 소문자
    for bad in (None, "", "#fff", "9fc6e7", "#9fc6e7 ", "#9fc6e7ff", "#12345g", "red", "rgb(1,2,3)", 0x9fc6e7, ["#9fc6e7"]):
        assert gevents.color(bad) is None, bad
    c = gevents.normalize_calendar({"id": "x", "summary": "S", "backgroundColor": "#ABCDEF"})
    assert c["color"] == "#abcdef"
    assert gevents.normalize_calendar({"id": "x", "summary": "S"})["color"] is None          # 없음
    assert gevents.normalize_calendar({"id": "x", "backgroundColor": "javascript:x"})["color"] is None


def test_normalize_calendar_shape():
    c = gevents.normalize_calendar({"id": "a@b", "summary": "a@b", "summaryOverride": "  내   캘린더 ", "primary": True,
                                    "accessRole": "owner", "selected": True, "backgroundColor": "#000000"})
    assert c == {"id": "a@b", "name": "내 캘린더", "primary": True, "google_selected": True, "color": "#000000",
                 "access": "owner"}
    assert gevents.normalize_calendar({"id": "x", "hidden": True}) is None
    assert gevents.normalize_calendar({"id": "x", "deleted": True}) is None
    assert gevents.normalize_calendar({"summary": "no id"}) is None
    assert gevents.normalize_calendar({"id": "x", "accessRole": "god"})["access"] == "reader"


def test_normalize_event_rules():
    ev = gevents.normalize_event({"id": "e1", "summary": " 회의 ", "start": {"dateTime": "2026-11-17T10:00:00Z"},
                                  "end": {"dateTime": "2026-11-17T11:00:00Z"}, "location": "학생회관",
                                  "htmlLink": "https://www.google.com/calendar/event?eid=e1",
                                  "description": "비밀", "attendees": [{"email": "x@y"}]}, CAL, KST)
    assert ev == {"id": "cal1:e1", "calendar_id": "cal1", "calendar_name": "개인", "color": "#123abc", "title": "회의",
                  "start": "2026-11-17T19:00:00+09:00", "end": "2026-11-17T20:00:00+09:00", "all_day": False,
                  "location": "학생회관", "url": "https://www.google.com/calendar/event?eid=e1", "busy": True}
    # 종일: 그 날 0시(로컬)·다음 날 0시, 끝이 없으면 하루
    d = gevents.normalize_event({"id": "e2", "summary": "휴일", "start": {"date": "2026-11-20"},
                                 "end": {"date": "2026-11-21"}, "transparency": "transparent"}, CAL, KST)
    assert d["all_day"] and d["busy"] is False
    assert parse_iso(d["start"]) == datetime(2026, 11, 20, tzinfo=KST) and parse_iso(d["end"]) == datetime(2026, 11, 21, tzinfo=KST)
    d = gevents.normalize_event({"id": "e3", "summary": "x", "start": {"date": "2026-11-20"}}, CAL, KST)
    assert parse_iso(d["end"]) == datetime(2026, 11, 21, tzinfo=KST)
    # 제목 없음 / 비공개(제목이 안 옴) / 바쁨만 보이는 캘린더
    base = {"start": {"dateTime": "2026-11-17T10:00:00+09:00"}, "end": {"dateTime": "2026-11-17T11:00:00+09:00"}}
    assert gevents.normalize_event({"id": "n", **base}, CAL, KST)["title"] == "(제목 없음)"
    assert gevents.normalize_event({"id": "n", "summary": "   ", **base}, CAL, KST)["title"] == "(제목 없음)"
    p = gevents.normalize_event({"id": "p", "visibility": "private", "location": "집", **base}, CAL, KST)
    assert p["title"] == "바쁨" and p["location"] is None
    fb = gevents.normalize_event({"id": "f", "summary": "새면 안 됨", "location": "어딘가", **base},
                                 {**CAL, "access": "freeBusyReader"}, KST)
    assert fb["title"] == "바쁨" and fb["location"] is None and fb["url"] is None
    # 버리는 것: 취소됨·시각 없음·id 없음, 끝이 시작보다 빠르면 시작으로
    assert gevents.normalize_event({"id": "c", "status": "cancelled", **base}, CAL, KST) is None
    assert gevents.normalize_event({"id": "t", "summary": "x"}, CAL, KST) is None
    assert gevents.normalize_event({"summary": "x", **base}, CAL, KST) is None
    back = gevents.normalize_event({"id": "b", "start": base["end"], "end": base["start"]}, CAL, KST)
    assert back["start"] == back["end"]
    assert gevents.normalize_event({"id": "j", "htmlLink": "javascript:alert(1)", **base}, CAL, KST)["url"] is None
    assert len(gevents.normalize_event({"id": "l", "summary": "가" * 500, **base}, CAL, KST)["title"]) == 200


def _stored(tmp_path, events, on=True, selected=None):
    s = Store(tmp_path / "s.json")
    s.data["settings"]["gcal_read"] = on
    s.data["gcal_events"] = {"calendars": [{"id": "cal1", "name": "개인", "primary": True, "google_selected": True,
                                            "color": None, "access": "owner"},
                                           {"id": "cal2", "name": "숨김", "primary": False, "google_selected": False,
                                            "color": None, "access": "reader"}],
                             "events": events, "fetched_at": iso(at(0, 8))}
    if selected is not None:
        s.data["gcal_events"]["selected"] = selected
    return s


def ev(n, hh, title="일", cal="cal1", hours=1, all_day=False, busy=True):
    s = at(n, 0 if all_day else hh)
    e = s + (timedelta(days=1) if all_day else timedelta(hours=hours))
    return {"id": f"{cal}:{n}-{hh}-{title}", "calendar_id": cal, "calendar_name": "개인", "color": None, "title": title,
            "start": iso(s), "end": iso(e), "all_day": all_day, "location": None, "url": None, "busy": busy}


def test_window_only_stored_in_range_sorted_and_selected(tmp_path):
    now = at(0, 9)
    s = _stored(tmp_path, [ev(5, 10, "나중"), ev(-3, 10, "오래 전"), ev(-1, 9, "어제"), ev(0, 8, "오늘 아침"),
                           ev(21, 23, "끝날"), ev(22, 0, "창 밖"), ev(2, 10, "숨김", cal="cal2")])
    titles = [e["title"] for e in gevents.window(s, now)]
    assert titles == ["어제", "오늘 아침", "나중", "끝날"]                 # 오늘 0시 −1일 ~ +21일 끝, 시작 순
    assert gevents.window_range(now) == (at(-1), at(22))
    s.data["gcal_events"]["selected"] = ["cal2"]                     # 고른 캘린더만
    assert [e["title"] for e in gevents.window(s, now)] == ["숨김"]
    s.data["settings"]["gcal_read"] = False                          # 꺼져 있으면 아무것도
    assert gevents.window(s, now) == [] and gevents.busy(s) == []


def test_window_cap(tmp_path):
    s = _stored(tmp_path, [ev(1 + i // 20, i % 20, f"e{i}") for i in range(400)])
    assert len(gevents.window(s, at(0, 9))) == gevents.MAX_EVENTS == 300


def test_busy_spans_have_no_titles(tmp_path):
    s = _stored(tmp_path, [ev(1, 19, "비밀 약속"), ev(2, 0, "휴일", all_day=True), ev(3, 10, "한가함", busy=False)])
    spans = gevents.busy(s)
    assert spans == [(at(1, 19), at(1, 20), False), (at(2), at(3), True)]
    assert "비밀" not in repr(spans)


def test_events_never_go_to_ai_or_notifications():
    """AI 입력(에이전트 상태 요약·검색 글)·알림 문구를 만드는 모듈은 저장된 다른 일정 칸을 읽지 않는다(계약 23장)."""
    import inspect

    from lms_agent import agent, agent_cli, notify
    for mod in (agent, agent_cli, notify):
        src = inspect.getsource(mod)
        assert "gcal_events" not in src and "gevents" not in src, mod.__name__
