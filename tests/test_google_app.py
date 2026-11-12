"""앱 서버의 구글 연결: 연결 버튼 → 구글 동의(가짜) → 루트로 돌아옴 → 자동 반영 → 승인하면 다시 반영 → 근거 남김."""
import json
import time
import urllib.error
import urllib.request

import pytest

from lms_agent import core
from lms_agent.canvas import Canvas
from lms_agent.gsync import desired_events
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store


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


def wait_push(app, n=0, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if not app.google.pushing and (app.store.data.get("gcal") or {}).get("at") and n <= 0:
            return
        time.sleep(0.05)
        n -= 1
    raise AssertionError("구글 반영이 끝나지 않았습니다")


@pytest.fixture()
def env(tmp_path):
    cmock, cbase = start_mock_canvas()
    gmock, gbase = start_mock_google()
    store = Store(tmp_path / "state.json")
    app = App(store, tmp_path, cfg={"canvas_url": cbase, "token": DEMO_TOKEN}, demo=True,
              google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=urls(gbase))
    core.sync(Canvas(cbase, DEMO_TOKEN), store)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}", gmock
    httpd.shutdown()
    cmock.shutdown()
    gmock.shutdown()


def test_connect_push_and_evidence(env):
    app, url, gmock = env
    st = json.loads(call(url + "/api/state", token=app.token)[1])
    assert st["google"]["available"] and not st["google"]["connected"]

    code, body, _ = call(url + "/api/google/connect", "POST", app.token, {})
    auth = json.loads(body)["url"]
    # 브라우저처럼: 구글 동의 화면 → 302로 앱 루트(?state=&code=) → 앱이 다시 #settings로
    code, _, headers = call(auth, follow=False)
    assert code == 302 and headers["Location"].startswith(url + "/?")
    code, _, headers = call(headers["Location"], follow=False)
    assert code == 302 and headers["Location"] == "/?google=ok#settings"

    wait_push(app)
    st = json.loads(call(url + "/api/state", token=app.token)[1])
    want = desired_events(app.store)
    assert sum(1 for e in want if e["lms_id"].startswith("week-")) >= 1  # 주간 요약도 함께 간다
    assert st["google"]["connected"] and st["google"]["last_result"]["created"] == len(want)
    # 구글에 있어야 할 수 = 마감 n + 주간 요약 m (화면이 '마감 n개 + 주간 요약 m개'로 보여 주고 결과 합과 맞춘다, 계약 13-5)
    summaries = sum(1 for e in want if e["lms_id"].startswith(("week-", "review-")))
    assert st["google"]["desired"] == {"deadlines": len(want) - summaries, "summaries": summaries}
    assert sum(st["google"]["desired"].values()) == sum(st["google"]["last_result"].values())
    lab3 = next(i for i in st["items"] if i["title"] == "LAB3 보고서")
    assert lab3["gcal_event_id"] and lab3["gcal_synced_at"] and lab3["gcal_link"]

    # 승인하면 자동으로 구글에 하나 더 생긴다
    target = next(i for i in st["items"] if i["status"] == "review" and "Capstone" in i["course_name"])
    call(url + f"/api/item/{target['id']}", "POST", app.token, {"decision": "approved"})
    time.sleep(0.1)
    wait_push(app)
    st = json.loads(call(url + "/api/state", token=app.token)[1])
    assert st["google"]["last_result"]["created"] >= 1
    assert next(i for i in st["items"] if i["id"] == target["id"])["gcal_event_id"]

    # 토큰은 화면 응답에 절대 나오지 않는다
    raw = call(url + "/api/state", token=app.token)[1].decode()
    tokens = app.cfg["google"]["tokens"]
    assert tokens["access_token"] not in raw and tokens["refresh_token"] not in raw

    # 연결 해제
    st = json.loads(call(url + "/api/google/disconnect", "POST", app.token, {})[1])
    assert not st["google"]["connected"]


def test_denied_consent_comes_back_with_message(env):
    app, url, gmock = env
    gmock.auth_error = "access_denied"
    auth = json.loads(call(url + "/api/google/connect", "POST", app.token, {})[1])["url"]
    _, _, headers = call(auth, follow=False)
    code, _, headers = call(headers["Location"], follow=False)
    assert code == 302 and "google=error" in headers["Location"]
    assert not app.google.connected


def test_callback_without_pending_flow_is_rejected(env):
    app, url, _ = env
    code, _, headers = call(url + "/?state=x&code=y", follow=False)
    assert code == 302 and "google=error" in headers["Location"]
