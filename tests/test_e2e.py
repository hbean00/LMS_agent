"""데모 시나리오(docs/CONTRACTS.md 2장) 전체: 가짜 Canvas → 동기화 → 화면 API → 캘린더 파일."""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lms_agent import core  # noqa: E402
from lms_agent.canvas import Canvas  # noqa: E402
from lms_agent.ics import validate_ics  # noqa: E402
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas  # noqa: E402
from lms_agent.server import App, serve_in_thread, start_server  # noqa: E402
from lms_agent.store import Store  # noqa: E402
from lms_agent.timeutil import local_tz, parse_iso  # noqa: E402

TZ = local_tz()
T = datetime.now().astimezone().date()


def at(days, h=23, mi=59):
    return datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ)


@pytest.fixture()
def demo(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    report = core.sync(Canvas(base, DEMO_TOKEN), store)
    yield mock, base, store, report
    mock.shutdown()


def by(store, **kw):
    return [v for v in store.view() if all(v.get(k) == val for k, val in kw.items())]


def find(store, course, due):
    rows = [v for v in store.view() if v["course_name"] == course and parse_iso(v["due"]) == due]
    assert len(rows) == 1, f"{course} {due}: {len(rows)}개 — {[(v['title'], v['due']) for v in store.view() if v['course_name'] == course]}"
    return rows[0]


def test_report_counts(demo):
    _, _, _, r = demo
    assert (r["courses"], r["assignments"], r["errors"]) == (4, 5, [])
    assert r["no_deadline"] == 1  # Weekly log 9


def test_email_assignment_from_announcement_linked_and_conflicting(demo):
    _, _, store, _ = demo
    a = find(store, "ASIC설계", at(6))
    b = find(store, "ASIC설계", at(8))
    for it in (a, b):
        assert it["status"] == "review" and it["method"] == "email"
        assert it["title"] == "LAB4 보고서"
        assert it["linked_to"] == "canvas-101-1012"
        assert it["conflict"]
    assert a["email_to"] == "asic.ta@example.ac.kr"
    assert "더 최근 글" in a["conflict"] and "이전 글" in b["conflict"]
    assert any("자정" in n for n in b["notes"])
    # 마감 없는 LAB4는 후보와 연결됐으므로 '근거 없음'으로 남지 않는다
    assert not [v for v in store.view() if v["id"] == "canvas-101-1012"]


def test_canvas_items_and_submission(demo):
    _, _, store, _ = demo
    lab3 = find(store, "ASIC설계", at(-5))
    assert (lab3["origin"], lab3["status"], lab3["submitted"], lab3["submitted_source"]) == ("canvas", "scheduled", True, "canvas")
    rows =[v for v in store.view() if v["course_name"] == "디지털신호처리" and parse_iso(v["due"]) == at(4, 18, 0)]
    assert sorted(v["origin"] for v in rows) == ["canvas", "found"]
    dup = next(v for v in rows if v["origin"] == "found")
    assert dup["status"] == "duplicate" and dup["duplicate_of"] == "canvas-102-1021"


def test_relative_and_syllabus_and_english(demo):
    _, _, store, _ = demo
    posted = T - timedelta(days=1)
    next_fri = posted - timedelta(days=posted.weekday()) + timedelta(days=11)
    proj = find(store, "디지털신호처리", datetime.combine(next_fri, time(23, 59), tzinfo=TZ))
    assert proj["method"] == "email" and proj["email_to"] == "dsp.prof@example.ac.kr"
    assert proj["title"] == "중간 프로젝트 보고서"
    hw2 = find(store, "반도체공학", at(12))
    assert hw2["title"] == "과제 2" and hw2["method"] == "email"
    final = find(store, "반도체공학", at(30, 15, 0))
    assert final["kind"] == "시험"
    cap = find(store, "Capstone Design", at(18))
    assert cap["method"] == "email" and cap["email_to"] == "capstone@example.ac.kr"


def test_no_false_positives(demo):
    _, _, store, _ = demo
    dsp = [v for v in store.view() if v["course_name"] == "디지털신호처리"]
    assert not [v for v in dsp if parse_iso(v["due"]) and parse_iso(v["due"]).date() == T + timedelta(days=2)]  # 휴강
    sem = [v for v in store.view() if v["course_name"] == "반도체공학"]
    assert len(sem) == 2  # 오리엔테이션 줄은 마감이 아니다
    log9 = by(store, status="no_deadline")
    assert [v["title"] for v in log9] == ["Weekly log 9"]


def test_inbox_not_marked_read(demo):
    mock, _, _, _ = demo
    details = [p for p in mock.requests if "/conversations/" in p]
    assert details and all("auto_mark_as_read=false" in p for p in details)


def test_user_decisions_survive_resync(demo):
    _, base, store, _ = demo
    a = find(store, "ASIC설계", at(8))
    store.update_user(a["id"], {"decision": "approved", "submitted": True, "title": "LAB4 최종"})
    core.sync(Canvas(base, DEMO_TOKEN), store)
    again = find(store, "ASIC설계", at(8))
    assert (again["status"], again["submitted"], again["title"], again["edited"]) == ("scheduled", True, "LAB4 최종", True)


def test_manual_paste_without_canvas(tmp_path):
    store = Store(tmp_path / "state.json")
    store.add_manual("회로이론", "기말 리포트는 12월 3일 18:00까지 조교 메일 ta@x.ac.kr 로 보내 주세요.", T.isoformat())
    core.rebuild(store)
    (it,) = store.view()
    assert it["status"] == "review" and it["method"] == "email" and it["course_name"] == "회로이론"


# ---------------------------------------------------------------- 서버

def _req(url, method="GET", token=None, body=None, host=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("X-LMS-Agent", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


@pytest.fixture()
def running(demo, tmp_path):
    _, base, store, _ = demo
    app = App(store, tmp_path, cfg={"canvas_url": base, "token": DEMO_TOKEN}, demo=True)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_server_security(running):
    app, url = running
    assert _req(url + "/api/state")[0] == 403
    assert _req(url + "/api/state", token="wrong")[0] == 403
    assert _req(url + "/api/state", token=app.token, host="evil.example")[0] == 403
    assert _req(url + "/api/sync", "POST", body={})[0] == 403


def test_server_flow_to_calendar(running):
    app, url = running
    code, body, _ = _req(url + "/api/state", token=app.token)
    assert code == 200
    state = json.loads(body)
    assert state["demo"] and state["configured"] and state["report"]["candidates"] >= 6
    review = [i for i in state["items"] if i["status"] == "review"]
    target = next(i for i in review if i["course_name"] == "Capstone Design")
    code, body, _ = _req(url + f"/api/item/{target['id']}", "POST", app.token, {"decision": "approved"})
    assert code == 200
    assert next(i for i in json.loads(body)["items"] if i["id"] == target["id"])["status"] == "scheduled"
    assert _req(url + "/api/item/nope", "POST", app.token, {"decision": "approved"})[0] == 404
    assert _req(url + f"/api/item/{target['id']}", "POST", app.token, {"decision": "maybe"})[0] == 400

    code, ics, headers = _req(url + "/calendar.ics")
    text = ics.decode("utf-8")
    assert code == 200 and "attachment" in headers.get("Content-Disposition", "")
    assert validate_ics(text) == []
    assert f"UID:{target['id']}@lms-agent" in text
    assert "canvas-101-1013@lms-agent" in text  # Canvas 과제 포함(기본값)
    assert text.count("BEGIN:VEVENT") == 1 + 3  # 승인 1 + 마감 있는 Canvas 과제 3(LAB3, LAB5, HW5)

    code, body, _ = _req(url + "/api/settings", "POST", app.token, {"include_canvas": False})
    assert code == 200
    text = _req(url + "/calendar.ics")[1].decode("utf-8")
    assert text.count("BEGIN:VEVENT") == 1

    code, body, _ = _req(url + "/api/setup", "POST", app.token, {"canvas_url": "x", "token": "y"})
    assert code == 400  # 데모에서는 설정 변경 불가
