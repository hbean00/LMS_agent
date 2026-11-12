"""마감 충돌 = 하나만 고르는 그룹(계약 13-1, 리뷰 B2·B3)과 구글에 있어야 할 일정 수(13-5, B8).

데모: LAB4 보고서 후보가 둘 — T+6(공지)과 T+8(더 최근 메시지, 연장). 둘 다 마감 없는 Canvas 과제 LAB4에 연결돼 한 그룹이다.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta

import pytest

from lms_agent import agent_cli, core, gsync, notify
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store, group_key
from lms_agent.timeutil import iso, local_tz, parse_iso

TZ = local_tz()
T = datetime.now().astimezone().date()


@pytest.fixture()
def demo(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    app = App(store, tmp_path, cfg={})
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, store
    httpd.shutdown()
    mock.shutdown()


def call(app, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def lab4(items):
    """(T+6 후보, T+8 후보) — 화면 항목 목록에서."""
    rows = [v for v in items if v["origin"] == "found" and v["linked_to"] == "canvas-101-1012"]
    by_day = {parse_iso(v["due"]).date(): v for v in rows}
    assert len(rows) == 2 and set(by_day) == {T + timedelta(days=6), T + timedelta(days=8)}
    return by_day[T + timedelta(days=6)], by_day[T + timedelta(days=8)]


def cal_lab4(store):
    return [v for v in store.calendar_items() if v.get("linked_to") == "canvas-101-1012"]


def test_view_group_fields_on_demo(demo):
    _, store = demo
    a, b = lab4(store.view())
    assert a["group"] == b["group"] == "link:canvas-101-1012"
    assert (a["newest"], b["newest"]) == (False, True)                   # T+8은 더 최근 메시지
    assert a["replaced_by"] is None and b["replaced_by"] is None
    alt = a["alternatives"]
    assert [x["id"] for x in alt] == [b["id"]] and alt[0]["newest"] is True
    assert set(alt[0]) == {"id", "due", "status", "decision", "posted_at", "source_type", "source_title", "newest"}
    assert alt[0]["source_type"] == "inbox" and alt[0]["status"] == "review"
    assert a["conflict"] and "더 최근 글" in a["conflict"] and "이전 글" in b["conflict"]
    # 후보가 하나뿐인 과제는 그룹이 없다
    single = next(v for v in store.view() if v["origin"] == "found" and v["title"] == "중간 프로젝트 보고서")
    assert single["group"] is None and single["alternatives"] == [] and single["conflict"] is None


def test_approve_t6_then_t8_leaves_one_lab4_in_calendar(demo):
    app, store = demo
    a, b = lab4(store.view())
    code, st = call(app, "POST", f"/api/item/{a['id']}", {"decision": "approved"})
    assert code == 200 and st["replaced"] == []
    a1, b1 = lab4(st["items"])
    assert a1["status"] == "scheduled" and b1["status"] == "replaced" and b1["replaced_by"] == a["id"]
    assert a1["conflict"] is None and b1["conflict"] is None                # 승인된 후보가 있으면 충돌 없음
    assert [v["id"] for v in cal_lab4(store)] == [a["id"]]

    code, st = call(app, "POST", f"/api/item/{b['id']}", {"decision": "approved"})
    assert code == 200 and st["replaced"] == [a["id"]]                     # 먼저 승인한 T+6은 pending으로
    a2, b2 = lab4(st["items"])
    assert (a2["decision"], a2["status"], a2["replaced_by"], a2["decided_by"]) == ("pending", "replaced", b["id"], "user")
    assert b2["status"] == "scheduled"
    assert [v["id"] for v in cal_lab4(store)] == [b["id"]]                 # 캘린더에는 LAB4가 하나만
    items, _ = core.calendar_payload(store)
    assert sum(1 for v in items if v.get("linked_to") == "canvas-101-1012") == 1
    assert sum(1 for e in gsync.desired_events(store) if e["lms_id"] in (a["id"], b["id"])) == 1
    # 승인을 되돌리면 둘 다 다시 확인 대기(충돌도 다시)
    code, st = call(app, "POST", f"/api/item/{b['id']}", {"decision": "pending"})
    a3, b3 = lab4(st["items"])
    assert (a3["status"], b3["status"]) == ("review", "review") and a3["conflict"] and b3["conflict"]
    assert st["replaced"] == []


def test_rejecting_t6_clears_conflict_on_t8(demo):
    app, store = demo
    a, b = lab4(store.view())
    assert b["conflict"]
    code, st = call(app, "POST", f"/api/item/{a['id']}", {"decision": "rejected"})
    a1, b1 = lab4(st["items"])
    assert a1["status"] == "rejected" and b1["status"] == "review"
    assert b1["conflict"] is None and a1["conflict"] is None
    assert b1["alternatives"][0]["status"] == "rejected"
    # 저장된 항목의 빌드 때 문장은 남아 있다(view가 기준)
    assert next(it for it in store.data["items"] if it["id"] == b["id"])["conflict"]


def test_legacy_double_approval_keeps_only_latest_in_calendar(demo):
    """예전 버전에서 둘 다 승인된 기록: 나중에 승인한 것만 일정, 다른 것은 replaced."""
    _, store = demo
    a, b = lab4(store.view())
    store.data["user"][a["id"]] = {"decision": "approved", "decided_at": "2026-11-10T09:00:00+09:00"}
    store.data["user"][b["id"]] = {"decision": "approved", "decided_at": "2026-11-12T09:00:00+09:00"}
    a1, b1 = lab4(store.view())
    assert (a1["status"], b1["status"], a1["replaced_by"]) == ("replaced", "scheduled", b["id"])
    assert [v["id"] for v in cal_lab4(store)] == [b["id"]]


def test_replaced_is_not_review_conflict_or_notification(demo):
    app, store = demo
    a, b = lab4(store.view())
    before_review = sum(1 for v in store.view() if v["status"] == "review")
    call(app, "POST", f"/api/item/{b['id']}", {"decision": "approved"})
    view = store.view()
    assert sum(1 for v in view if v["status"] == "review") == before_review - 2
    assert a["id"] not in {v["id"] for v in agent_cli.review_items(store)[0]}   # AI 정리 대상도 아님
    msg = notify.new_deadlines_message({"new_items": [a["id"]]}, view)
    assert msg is None                                                       # 대체된 후보는 '새 후보' 알림이 아님
    store.data["settings"]["review_reminder"] = True
    rem = core.review_reminder(store)
    assert rem and f"{before_review - 2}개" in rem[0]["summary"]
    st = app.state()
    assert not [c for c in st["celebrations"]["news"] if c["kind"] == "changes"
                and any(i["id"] == a["id"] for i in c["items"])]


def test_group_key_rules_shared_with_ai():
    found = {"origin": "found", "course_id": "1", "linked_to": None}
    assert group_key({**found, "linked_to": "canvas-1-2", "title": "아무거나"}) == "link:canvas-1-2"
    assert group_key({**found, "title": "LAB 4 보고서"}) == group_key({**found, "title": "lab4  보고서"})
    assert group_key({**found, "title": "기말고사"}) is not None
    assert group_key({**found, "title": "마감"}) is None                   # 한 과제를 가리키지 않는 제목은 묶지 않음
    assert group_key({**found, "title": "공지"}) is None
    assert group_key({"origin": "canvas", "course_id": "1", "title": "LAB4"}) is None
    assert agent_cli._group_key({**found, "title": "LAB4 보고서"}) == group_key({**found, "title": "LAB4 보고서"})


def test_generic_titles_in_one_course_stay_separate(tmp_path):
    s = Store(tmp_path / "s.json")
    base = {"origin": "found", "course_id": "1", "course_name": "과목", "kind": "과제", "linked_to": None,
            "duplicate_of": None, "conflict": None, "notes": [], "default_decision": "pending"}
    s.data["items"] = [dict(base, id="f-1", title="마감", due=iso(datetime.combine(T, time(23, 59), tzinfo=TZ)),
                            evidence=[]),
                       dict(base, id="f-2", title="마감", due=iso(datetime.combine(T + timedelta(1), time(23, 59), tzinfo=TZ)),
                            evidence=[])]
    s.update_user("f-1", {"decision": "approved"})
    assert s.release_group("f-1") == []
    v = {x["id"]: x for x in s.view()}
    assert v["f-1"]["status"] == "scheduled" and v["f-2"]["status"] == "review" and v["f-2"]["group"] is None


def test_google_desired_counts_match_payload(demo):
    _, store = demo
    want = gsync.desired_events(store)
    d = gsync.desired_counts(store)
    summaries = sum(1 for e in want if e["lms_id"].startswith(("week-", "review-")))
    assert d == {"deadlines": len(want) - summaries, "summaries": summaries} and summaries >= 1
    store.data["settings"]["weekly_summary"] = False
    assert gsync.desired_counts(store)["summaries"] == 0
    store.data["settings"]["review_reminder"] = True                      # 확인 알림도 구글로 가는 요약 일정
    assert gsync.desired_counts(store)["summaries"] == 1
    assert sum(gsync.desired_counts(store).values()) == len(gsync.desired_events(store))
