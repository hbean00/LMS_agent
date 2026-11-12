"""gcal.py를 가짜 구글(mock_google)에 붙여 OAuth·토큰 갱신·캘린더 동기화를 확인한다."""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from lms_agent import gcal
from lms_agent.gcal import GoogleCalendar, GoogleError, OAuthFlow
from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls

KST = timezone(timedelta(hours=9))
REDIRECT = "http://127.0.0.1:8765/oauth/google/callback"
SECRETS: set = {DEMO_CLIENT_SECRET}
ERRORS: list = []


@pytest.fixture
def mock():
    server, base = start_mock_google()
    yield server, urls(base)
    server.shutdown()
    server.server_close()


class _NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def browser(url: str) -> dict:
    """브라우저 대신 인증 주소를 열고 302 Location의 쿼리를 돌려준다."""
    try:
        urllib.request.build_opener(_NoFollow).open(url, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 302
        loc = e.headers["Location"]
        assert loc.startswith(REDIRECT)
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
    raise AssertionError("리다이렉트가 없음")


def connect(u: dict) -> dict:
    flow = OAuthFlow(DEMO_CLIENT_ID, REDIRECT, DEMO_CLIENT_SECRET, auth_url=u["auth_url"], token_url=u["token_url"])
    q = browser(flow.authorization_url())
    SECRETS.update([q["code"], flow.code_verifier])
    tokens = flow.exchange(q["code"], q["state"])
    SECRETS.update([tokens["access_token"], tokens["refresh_token"]])
    return tokens


def client(u: dict, tokens: dict, saved: list | None = None) -> GoogleCalendar:
    return GoogleCalendar(tokens, DEMO_CLIENT_ID, DEMO_CLIENT_SECRET,
                          on_tokens=(saved.append if saved is not None else None),
                          api_base=u["api_base"], token_url=u["token_url"], backoff=(0, 0))


def expect_error(fn, *a, **k) -> GoogleError:
    with pytest.raises(GoogleError) as ei:
        fn(*a, **k)
    ERRORS.append(ei.value)
    return ei.value


def item(n: int, **kw) -> dict:
    due = datetime(2026, 11, 20 + n, 23, 59, tzinfo=KST)
    start, end = gcal.deadline_window(due)
    d = {"lms_id": f"id{n}", "summary": f"[과목] 과제 {n}", "description": f"설명 {n}", "start": start, "end": end,
         "url": f"https://canvas.example.ac.kr/courses/1/assignments/{n}", "reminders": [1440, 180]}
    d.update(kw)
    return d


def writes(srv, since: int = 0) -> list:
    return [(m, p) for m, p in srv.requests[since:] if m in ("POST", "PATCH", "DELETE") and "/calendar/v3/" in p]


# ---- OAuth ----

def test_authorization_url_params(mock):
    _, u = mock
    flow = OAuthFlow(DEMO_CLIENT_ID, REDIRECT, auth_url=u["auth_url"])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(flow.authorization_url()).query))
    assert q["response_type"] == "code" and q["scope"] == gcal.SCOPE and q["client_id"] == DEMO_CLIENT_ID
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256" and q["state"] == flow.state
    assert q["code_challenge"] != flow.code_verifier and 43 <= len(flow.code_verifier) <= 128


def test_full_oauth_flow(mock):
    srv, u = mock
    tokens = connect(u)
    assert set(tokens) == {"access_token", "refresh_token", "expires_at", "scope"}
    assert tokens["expires_at"] > time.time() + 3000 and gcal.SCOPE in tokens["scope"].split()
    assert srv.challenges  # 인증 화면이 challenge를 받았다
    assert all(a is None for (m, p), a in zip(srv.requests, srv.auth_log) if p.startswith("/token"))


def test_wrong_state_rejected(mock):
    srv, u = mock
    flow = OAuthFlow(DEMO_CLIENT_ID, REDIRECT, DEMO_CLIENT_SECRET, auth_url=u["auth_url"], token_url=u["token_url"])
    q = browser(flow.authorization_url())
    SECRETS.add(q["code"])
    expect_error(flow.exchange, q["code"], q["state"] + "x")
    assert not any(p.startswith("/token") for _, p in srv.requests)  # 토큰 요청도 안 보냈다
    assert flow.exchange(q["code"], q["state"])["refresh_token"]  # 진짜 콜백은 여전히 된다


def test_pkce_verifier_checked(mock):
    _, u = mock
    flow = OAuthFlow(DEMO_CLIENT_ID, REDIRECT, DEMO_CLIENT_SECRET, auth_url=u["auth_url"], token_url=u["token_url"])
    q = browser(flow.authorization_url())
    SECRETS.update([q["code"], flow.code_verifier])
    flow.code_verifier = "x" * 64  # 가로챈 코드만으로는 못 바꾼다
    e = expect_error(flow.exchange, q["code"], q["state"])
    assert e.status == 400 and "다시 연결" in str(e)


def test_scope_unchecked_rejected(mock):
    srv, u = mock
    srv.grant_scope = "openid"
    flow = OAuthFlow(DEMO_CLIENT_ID, REDIRECT, DEMO_CLIENT_SECRET, auth_url=u["auth_url"], token_url=u["token_url"])
    q = browser(flow.authorization_url())
    SECRETS.add(q["code"])
    assert "권한" in str(expect_error(flow.exchange, q["code"], q["state"]))


def test_revoke_best_effort(mock):
    srv, u = mock
    tokens = connect(u)
    gcal.revoke(tokens["refresh_token"], revoke_url=u["revoke_url"])
    assert not srv.data["refresh_tokens"] and not srv.data["access_tokens"]
    gcal.revoke("nope", revoke_url=u["revoke_url"])  # 실패해도 예외 없음
    gcal.revoke("nope", revoke_url="http://127.0.0.1:1/revoke")


# ---- 토큰 갱신 ----

def test_refresh_when_expiring(mock):
    srv, u = mock
    tokens = connect(u)
    saved: list = []
    cal = client(u, {**tokens, "expires_at": time.time() + 30}, saved)  # 60초 안에 만료 → 미리 갱신
    cal.ensure_calendar(None)
    assert len(saved) == 1 and saved[0]["access_token"] != tokens["access_token"]
    assert saved[0]["refresh_token"] == tokens["refresh_token"]
    SECRETS.add(saved[0]["access_token"])


def test_refresh_on_401_retries_once(mock):
    srv, u = mock
    tokens = connect(u)
    saved: list = []
    cal = client(u, tokens, saved)
    srv.data["access_tokens"].clear()  # 서버 쪽에서 토큰이 무효가 됨
    cid = cal.ensure_calendar(None)
    assert cid in srv.data["calendars"] and len(saved) == 1
    SECRETS.add(saved[0]["access_token"])


def test_invalid_grant_message(mock):
    srv, u = mock
    tokens = connect(u)
    srv.data["refresh_tokens"].clear()
    srv.data["access_tokens"].clear()
    e = expect_error(client(u, tokens).ensure_calendar, None)
    assert e.status == 401 and str(e) == "구글 연결이 끊겼습니다. 설정에서 다시 연결하세요"


def test_bearer_never_in_url(mock):
    srv, u = mock
    tokens = connect(u)
    cal = client(u, tokens)
    cal.sync(cal.ensure_calendar(None), [item(1)])
    for (_, p), a in zip(srv.requests, srv.auth_log):
        assert tokens["access_token"] not in p and tokens["refresh_token"] not in p
        if p.startswith("/calendar/v3/"):
            assert a == "Bearer " + tokens["access_token"]


# ---- 캘린더 ----

def test_ensure_calendar_create_reuse_recreate(mock):
    srv, u = mock
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    assert srv.data["calendars"][cid]["summary"] == gcal.CALENDAR_NAME
    assert srv.data["calendars"][cid]["timeZone"] == "Asia/Seoul"
    assert cal.ensure_calendar(cid) == cid and len(srv.data["calendars"]) == 1
    del srv.data["calendars"][cid]  # 사용자가 캘린더를 지움
    cid2 = cal.ensure_calendar(cid)
    assert cid2 != cid and cid2 in srv.data["calendars"]
    assert cal.ensure_calendar("primary") != "primary"  # 앱이 만들지 않은 캘린더는 못 쓴다(404)


def test_sync_lifecycle(mock):
    srv, u = mock
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    desired = [item(1), item(2), item(3)]
    r = cal.sync(cid, desired)
    assert (r["created"], r["updated"], r["deleted"], r["unchanged"], r["errors"]) == (3, 0, 0, 0, [])
    ev = srv.data["events"][cid][r["events"]["id1"]["event_id"]]
    assert r["events"]["id1"]["updated"] == ev["updated"] and r["events"]["id1"]["html_link"] == ev["htmlLink"]
    assert ev["summary"] == "[과목] 과제 1" and ev["transparency"] == "transparent"
    assert ev["source"] == {"title": "LMS Agent", "url": "https://canvas.example.ac.kr/courses/1/assignments/1"}
    assert ev["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 180},
                                                                  {"method": "popup", "minutes": 1440}]}
    assert ev["extendedProperties"]["private"]["lmsAgent"] == "1"
    assert ev["start"]["dateTime"] == ev["end"]["dateTime"] == "2026-11-21T23:59:00+09:00"

    # 그대로 다시: 쓰기 0번
    n = len(srv.requests)
    r = cal.sync(cid, desired)
    assert (r["created"], r["updated"], r["deleted"], r["unchanged"]) == (0, 0, 0, 3)
    assert writes(srv, n) == [] and r["events"]["id1"]["event_id"] == ev["id"]

    # 같은 시각을 다른 시간대로 줘도 바뀐 게 아니다
    n = len(srv.requests)
    utc = item(1)
    utc["start"] = utc["end"] = utc["start"].astimezone(timezone.utc)
    assert cal.sync(cid, [utc, item(2), item(3)])["unchanged"] == 3 and writes(srv, n) == []

    # 제목 하나 바꾸기 → PATCH 1번
    n = len(srv.requests)
    desired[1] = item(2, summary="[과목] 과제 2 (연장)", url=None)
    r = cal.sync(cid, desired)
    assert (r["created"], r["updated"], r["deleted"], r["unchanged"]) == (0, 1, 0, 2)
    assert [m for m, _ in writes(srv, n)] == ["PATCH"]
    ev2 = srv.data["events"][cid][r["events"]["id2"]["event_id"]]
    assert ev2["summary"] == "[과목] 과제 2 (연장)" and "source" not in ev2
    assert r["events"]["id2"]["updated"] == ev2["updated"]

    # 앱이 만들지 않은 일정(같은 캘린더)
    srv.data["events"][cid]["mine"] = {"id": "mine", "status": "confirmed", "summary": "내 일정",
                                       "updated": "2026-01-01T00:00:00.000Z"}

    # 하나 빼기 → DELETE 1번
    n = len(srv.requests)
    r = cal.sync(cid, desired[:2])
    assert (r["created"], r["updated"], r["deleted"], r["unchanged"]) == (0, 0, 1, 2)
    assert [m for m, _ in writes(srv, n)] == ["DELETE"]
    assert set(r["events"]) == {"id1", "id2"}
    assert srv.data["events"][cid]["mine"]["status"] == "confirmed"  # 손대지 않음

    # 모두 빼도 앱 일정만 지운다
    r = cal.sync(cid, [])
    assert r["deleted"] == 2 and srv.data["events"][cid]["mine"]["status"] == "confirmed"


def test_duplicate_cleanup_keeps_newest(mock):
    srv, u = mock
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    r = cal.sync(cid, [item(1)])
    first = r["events"]["id1"]["event_id"]
    # 이전 실행이 응답을 못 받고 끝나 같은 lmsId 일정이 하나 더 생긴 상황
    cal._call("POST", f"/calendars/{urllib.parse.quote(cid, safe='')}/events",
              body={k: v for k, v in gcal._event_body(item(1))[1].items() if v is not None})
    newest = max((e for e in srv.data["events"][cid].values() if e["status"] == "confirmed"),
                 key=lambda e: e["updated"])["id"]
    assert newest != first
    n = len(srv.requests)
    r = cal.sync(cid, [item(1)])
    assert (r["deleted"], r["unchanged"]) == (1, 1) and r["events"]["id1"]["event_id"] == newest
    assert writes(srv, n) == [("DELETE", f"/calendar/v3/calendars/{urllib.parse.quote(cid, safe='')}/events/{first}")]


def test_paging(mock):
    srv, u = mock
    srv.page_size = 2
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    desired = [item(i) for i in range(5)]
    assert cal.sync(cid, desired)["created"] == 5
    n = len(srv.requests)
    r = cal.sync(cid, desired)
    assert r["unchanged"] == 5 and writes(srv, n) == []
    lists = [p for m, p in srv.requests[n:] if m == "GET" and p.split("?")[0].endswith("/events")]
    assert len(lists) == 3 and "pageToken=p2" in lists[1] and "pageToken=p4" in lists[2]
    assert "privateExtendedProperty=lmsAgent%3D1" in lists[0] and "maxResults=250" in lists[0]


def test_per_item_errors_collected(mock):
    srv, u = mock
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    srv.faults.append({"method": "POST", "path": r"/events$", "status": 400, "count": 1})
    bad_time = item(3)
    bad_time["start"] = None
    r = cal.sync(cid, [item(1), item(2), bad_time, item(2)])
    assert r["created"] == 1 and set(r["events"]) == {"id2"}
    assert len(r["errors"]) == 3 and all(isinstance(e, str) for e in r["errors"])
    assert "과제 1" in r["errors"][0] and "400" in r["errors"][0]
    assert "시각" in r["errors"][1] and "두 번" in r["errors"][2]


def test_retry_on_5xx_then_give_up(mock):
    srv, u = mock
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    srv.faults.append({"method": "GET", "path": r"/events$", "status": 503, "count": 2})
    assert cal.sync(cid, [item(1)])["created"] == 1  # 두 번 실패 뒤 성공
    srv.faults.append({"method": "GET", "path": r"/events$", "status": 503, "count": 3})
    e = expect_error(cal.sync, cid, [item(1)])
    assert e.status == 503 and "구글 서버 오류" in str(e)


def test_sync_on_deleted_calendar(mock):
    srv, u = mock
    cal = client(u, connect(u))
    cid = cal.ensure_calendar(None)
    del srv.data["calendars"][cid]
    assert expect_error(cal.sync, cid, [item(1)]).status == 404


def test_zz_no_secret_in_any_error():
    """앞의 테스트에서 모은 모든 GoogleError 메시지에 토큰·코드·비밀값이 없다."""
    if not ERRORS:
        pytest.skip("이 테스트만 따로 돌리면 모은 오류가 없다")
    for e in ERRORS:
        text = str(e) + repr(e.args)
        for s in SECRETS:
            assert s not in text
