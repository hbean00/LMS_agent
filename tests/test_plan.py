"""공부 일정(계약 22-5): 짜기(make)·채팅 문장(handle)·경로(미리 보기·넣기·지우기)·캘린더 파일과 구글 반영.

시각은 문자열이 아니라 같은 순간인지(parse_iso)로 비교한다(CI는 UTC).
"""
import json
import time as _time
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta

import pytest

from lms_agent import core, gsync, plan
from lms_agent.canvas import Canvas
from lms_agent.ics import validate_ics
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso, to_utc_stamp

TZ = local_tz()
T = datetime.now().astimezone().date()
NOW = datetime.combine(T, time(9, 0), tzinfo=TZ)


def at(days, h=23, mi=59):
    return datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ)


@pytest.fixture()
def demo(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    yield store
    mock.shutdown()


def view_one(store, **kw):
    rows = [v for v in store.view() if all(v.get(k) == val for k, val in kw.items())]
    assert len(rows) == 1, rows
    return rows[0]


def synthetic(tmp_path, rows, courses=None):
    """마감·시험 항목을 직접 만든 저장소(모두 승인)."""
    s = Store(tmp_path / "s.json")
    base = {"origin": "found", "course_id": "1", "course_name": "회로이론", "kind": "과제", "linked_to": None,
            "duplicate_of": None, "conflict": None, "notes": [], "default_decision": "pending", "evidence": [],
            "url": None}
    s.data["items"] = [dict(base, **r) for r in rows]
    if courses is not None:
        s.data["cache"] = {"courses": courses, "assignments": [], "announcements": [], "errors": []}
    for r in rows:
        s.update_user(r["id"], {"decision": "approved"})
    return s


def starts(p):
    return [parse_iso(b["start"]) for b in p["blocks"]]


# ---------------------------------------------------------------- make()

def test_make_picks_nearest_exam_on_demo(demo):
    quiz = view_one(demo, kind="시험", course_id="101")
    p = plan.make(demo, now=NOW)
    assert p["item_id"] == quiz["id"] and p["course_id"] == "101" and p["course_short"] == "ASIC설계(영강)"
    assert parse_iso(p["target"]["due"]) == parse_iso(quiz["due"]) == at(5)
    # 오늘(9시라 20시는 아직)~시험 전날, 시험 당일은 비움
    assert starts(p) == [at(d, 20, 0) for d in range(0, 5)]
    for b in p["blocks"]:
        assert parse_iso(b["end"]) - parse_iso(b["start"]) == timedelta(minutes=60)
        assert b["course_id"] == "101" and b["item_id"] == quiz["id"] and len(b["title"]) <= 120
    assert [b["title"] for b in p["blocks"]][-1].endswith("D-1")
    assert any("가장 가까운 시험" in n and "기말고사" in n for n in p["notes"])
    assert any("확인 전" in n for n in p["notes"])                      # 승인 전 시험 날짜라고 알림
    assert any("시험 당일" in n for n in p["notes"])
    assert not demo.data.get("plans")                                    # 미리 보기는 저장하지 않는다


def test_make_course_days_and_cap(demo):
    final = view_one(demo, kind="시험", course_id="103")
    p = plan.make(demo, course_id="103", days=14, minutes=90, hour=21, now=NOW)
    assert p["item_id"] == final["id"] and parse_iso(p["target"]["due"]) == at(30, 15, 0)
    assert starts(p) == [at(d, 21, 0) for d in range(16, 30)]
    assert all(parse_iso(b["end"]) - parse_iso(b["start"]) == timedelta(minutes=90) for b in p["blocks"])
    # 기간을 안 주면 오늘~시험 전날(30일)인데 최대 14블록 → 시험에 가까운 14일
    q = plan.make(demo, course_id=103, now=NOW)
    assert starts(q) == [at(d, 20, 0) for d in range(16, 30)]
    assert any("최대 14개" in n for n in q["notes"])
    # 시작일을 주면 그날부터
    r = plan.make(demo, course_id="103", start=(T + timedelta(days=25)).isoformat(), now=NOW)
    assert starts(r) == [at(d, 20, 0) for d in range(25, 30)]


def test_make_skip_rules(tmp_path):
    s = synthetic(tmp_path, [
        {"id": "f-exam", "title": "중간고사", "kind": "시험", "due": iso(at(5, 10, 0))},
        {"id": "f-a", "title": "LAB1 보고서", "due": iso(at(2))},
        {"id": "f-b", "title": "LAB2 보고서", "due": iso(at(2, 18, 0))},     # T+2: 마감 2개 → 건너뜀
        {"id": "f-c", "title": "LAB3 보고서", "due": iso(at(3))},            # T+3: 하나뿐 → 넣음
    ])
    late = datetime.combine(T, time(21, 0), tzinfo=TZ)                       # 오늘 20시는 지났다
    p = plan.make(s, now=late)
    assert p["item_id"] == "f-exam"
    assert starts(p) == [at(1, 20, 0), at(3, 20, 0), at(4, 20, 0)]
    assert any("오늘은 20:00가 지나" in n for n in p["notes"])
    busy = next(n for n in p["notes"] if n.startswith("마감이 2개 이상"))
    assert "LAB1 보고서" in busy and "LAB2 보고서" in busy
    # 시험 시작 전에 끝나지 않는 블록은 뺀다(T+4 23:00~03:00 > 시험 T+5 01:00)
    s.update_user("f-exam", {"due": (T + timedelta(days=5)).isoformat() + "T01:00"})
    q = plan.make(s, hour=23, minutes=240, now=late)
    assert at(4, 23, 0) not in starts(q) and at(3, 23, 0) in starts(q)
    assert any("끝나지 않는" in n for n in q["notes"])


def test_make_skips_days_taken_by_saved_plan(tmp_path):
    s = synthetic(tmp_path, [{"id": "f-exam", "title": "기말고사", "kind": "시험", "due": iso(at(4, 10, 0))}])
    first = plan.make(s, now=NOW)
    assert len(first["blocks"]) == 4
    rec = plan.add(s, first, now=NOW)
    again = plan.make(s, now=NOW)                                            # 같은 시각은 모두 이미 찼다
    assert again["blocks"] == [] and any("겹치는 날" in n for n in again["notes"])
    other = plan.make(s, hour=8, now=NOW)                                     # 다른 시각이면 넣을 수 있다
    assert len(other["blocks"]) == 3 and starts(other)[0] == at(1, 8, 0)     # 오늘 8시는 이미 지남
    assert rec["id"].startswith("p-")


def test_make_without_exam_and_bad_values(demo):
    p = plan.make(demo, course_id="104", now=NOW)                            # Capstone: 시험 없음
    assert p["blocks"] == [] and p["item_id"] is None and "다가오는 시험 일정이 없어요" in p["notes"][0]
    p = plan.make(demo, item_id="f-nope", now=NOW)
    assert p["blocks"] == [] and "찾지 못했어요" in p["notes"][0]
    past = view_one(demo, title="LAB3 보고서")                               # 지난 항목
    assert "이미 지났어요" in plan.make(demo, item_id=past["id"], now=NOW)["notes"][0]
    # 시험이 아닌 항목도 직접 고르면 그 마감 전으로 짠다
    lab5 = view_one(demo, title="LAB5 예비보고서")
    assert parse_iso(plan.make(demo, item_id=lab5["id"], now=NOW)["blocks"][-1]["start"]) == at(9, 20, 0)
    for kw in ({"minutes": 10}, {"minutes": 241}, {"hour": 24}, {"hour": -1}, {"days": 0}, {"days": 61},
               {"start": "어제"}, {"minutes": True}, {"course_id": "../x"}):
        with pytest.raises(ValueError):
            plan.make(demo, now=NOW, **kw)


def test_make_with_no_upcoming_exam_at_all(tmp_path):
    s = synthetic(tmp_path, [{"id": "f-a", "title": "LAB1 보고서", "due": iso(at(3))}])
    p = plan.make(s, now=NOW)
    assert p["blocks"] == [] and "다가오는 시험" in p["notes"][0]


# ---------------------------------------------------------------- handle()

def test_handle_contract_examples(demo):
    r = plan.handle(demo, "기말 전 2주 공부 계획 세워줘", now=NOW)
    p = r["plan"]
    assert p["course_id"] == "103" and p["target"]["title"] == "기말고사" and p["days"] == 14
    assert starts(p) == [at(d, 20, 0) for d in range(16, 30)]
    assert "기말고사" in r["answer"] and "14개" in r["answer"] and "아직 캘린더에 넣지 않았어요" in r["answer"]
    assert any("기본값" in n for n in p["notes"])

    r = plan.handle(demo, "반도체 시험 대비 스케줄", now=NOW)
    assert r["plan"]["course_id"] == "103" and len(r["plan"]["blocks"]) == 14

    r = plan.handle(demo, "ASIC 퀴즈 대비 공부 계획, 저녁 9시에 매일 2시간", now=NOW)
    p = r["plan"]
    assert p["course_id"] == "101" and p["target"]["title"] == "Quiz" and (p["hour"], p["minutes"]) == (21, 120)
    assert starts(p)[0] == at(0, 21, 0) and parse_iso(p["blocks"][0]["end"]) == at(0, 23, 0)
    assert not any("기본값" in n for n in p["notes"])

    # 채팅 창에서 고른 과목(course_id)을 쓴다
    r = plan.handle(demo, "공부 일정 짜줘", course_id="103", now=NOW)
    assert r["plan"]["course_id"] == "103"
    # 중간(midterm) → Midterm quiz에 연결된 ASIC Quiz
    r = plan.handle(demo, "중간고사 공부 계획 세워줘", now=NOW)
    assert r["plan"]["course_id"] == "101" and r["plan"]["target"]["title"] == "Quiz"
    assert not demo.data.get("plans")                                        # 미리 보기만


def test_handle_course_abbreviation_and_missing(tmp_path):
    courses = [{"id": 7, "name": "262R (서울-학부)로봇공학개론(GENERAL ROBOTICS)-00분반", "course_code": "ROB101"},
               {"id": 8, "name": "반도체공학", "course_code": "EE361"}]
    s = synthetic(tmp_path, [
        {"id": "f-mid", "course_id": "7", "course_name": courses[0]["name"], "title": "중간고사", "kind": "시험",
         "due": iso(at(6, 10, 30))},
        {"id": "f-fin", "course_id": "7", "course_name": courses[0]["name"], "title": "기말고사", "kind": "시험",
         "due": iso(at(40, 10, 30))},
        {"id": "f-semi", "course_id": "8", "course_name": "반도체공학", "title": "기말고사", "kind": "시험",
         "due": iso(at(3, 15, 0))},
    ], courses=courses)
    r = plan.handle(s, "로공개 중간고사 공부 일정 짜줘", now=NOW)           # 줄임말 → 로봇공학개론
    assert r["plan"]["item_id"] == "f-mid" and r["plan"]["course_short"] == "로봇공학개론"
    r = plan.handle(s, "로공개 기말 10일 공부 계획", now=NOW)
    assert r["plan"]["item_id"] == "f-fin" and starts(r["plan"]) == [at(d, 20, 0) for d in range(30, 40)]
    r = plan.handle(s, "반도체 퀴즈 공부 계획 세워줘", now=NOW)              # 그 과목에 퀴즈가 없음 → 짜지 않음
    assert r["plan"]["blocks"] == [] and "퀴즈" in r["answer"] and "찾지 못했어요" in r["answer"]
    assert "다가오는 시험" in r["answer"]
    r = plan.handle(s, "열역학 시험 공부 계획 세워줘", now=NOW)             # 없는 과목 이름 → 숨기지 않고 알림
    assert "'열역학'에 맞는 과목을 찾지 못해" in r["answer"] and r["plan"]["item_id"] == "f-semi"
    # 제외(hidden)한 과목은 고르지 않는다
    s.data["course_prefs"] = {"8": {"mode": "hidden"}}
    r = plan.handle(s, "시험 공부 계획 세워줘", now=NOW)
    assert r["plan"]["item_id"] == "f-mid"


@pytest.mark.parametrize("msg", [
    "기말 범위 어디까지야?", "기말에서 몇 점 받아야 A?", "LAB4 연장 문의 메일 써줘", "컴퓨터 켤 때 자동 실행해줘",
    "기말고사 날짜가 안 보여요", "시험 일정 알려줘", "공부 계획 지워줘", "공부 계획 삭제해줘", "", "동기화해줘",
    "시험 대비 자료 있어?",
])
def test_handle_ignores_other_messages(demo, msg):
    assert plan.handle(demo, msg, now=NOW) is None


@pytest.mark.parametrize("msg", ["기말 전 2주 공부 계획 세워줘", "로공개 중간고사 공부 일정 짜줘", "반도체 시험 대비 스케줄",
                                 "스케줄 짜줘", "study plan for the final", "공부계획 만들어줘", "시험 준비 일정 잡아줘"])
def test_handle_recognizes_plan_messages(demo, msg):
    r = plan.handle(demo, msg, now=NOW)
    assert r is not None and isinstance(r["answer"], str) and r["answer"] and "blocks" in r["plan"]
    # 데모에 없는 '로공개'만 '맞는 과목을 못 찾음' 안내(계획·기간·영어 낱말은 과목 이름으로 보지 않는다)
    assert ("맞는 과목을 찾지 못해" in r["answer"]) == msg.startswith("로공개")


def test_parse_time_duration_days():
    assert plan._parse_hour("저녁 9시")[0] == 21
    assert plan._parse_hour("밤 11시 30분")[0] == 23 and "정시" in plan._parse_hour("밤 11시 30분")[1][0]
    assert plan._parse_hour("오전 7시")[0] == 7 and plan._parse_hour("아침 12시")[0] == 0
    h, notes, _ = plan._parse_hour("9시에")
    assert h == 21 and "저녁 21:00" in notes[0]
    assert plan._parse_hour("21:00부터")[0] == 21 and plan._parse_hour("매일 2시간")[0] is None
    assert plan._parse_hour("밤에")[0] == 22
    assert plan._parse_minutes("매일 1시간 반") == 90 and plan._parse_minutes("두 시간씩") == 120
    assert plan._parse_minutes("45분") == 45 and plan._parse_minutes("1시간 15분") == 75
    assert plan._parse_minutes("공부 계획") is None
    assert plan._parse_days("기말 전 2주") == 14 and plan._parse_days("10일 동안") == 10
    assert plan._parse_days("일주일") == 7 and plan._parse_days("열흘") == 10 and plan._parse_days("두 주") == 14
    assert plan._parse_days("12월 1일까지 3주") == 21 and plan._parse_days("3주차 시험") is None
    assert plan._parse_days("12월 1일 시험") is None and plan._parse_days("D-5부터") == 5


# ---------------------------------------------------------------- 경로

@pytest.fixture()
def server(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    app = App(store, tmp_path, cfg={})
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, store
    httpd.shutdown()
    mock.shutdown()


def call(app, path, body=None, method="POST"):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def future_block(days, h=20, minutes=60, **kw):
    s = datetime.combine(T + timedelta(days=days), time(h, 0), tzinfo=TZ)
    return {"start": iso(s), "end": iso(s + timedelta(minutes=minutes)), **kw}


def test_routes_preview_apply_remove(server):
    app, store = server
    code, st = call(app, "/api/state", method="GET")
    assert code == 200 and st["plans"] == []

    code, out = call(app, "/api/plan/preview", {"course_id": "103", "days": 7, "minutes": 45, "hour": 19})
    assert code == 200 and set(out) == {"plan"}
    p = out["plan"]
    assert p["course_id"] == "103" and len(p["blocks"]) == 7 and not store.data.get("plans")
    assert all(parse_iso(b["start"]).hour == 19 for b in p["blocks"])

    code, st = call(app, "/api/plan/apply", {"title": p["title"], "course_id": p["course_id"], "item_id": p["item_id"],
                                             "blocks": p["blocks"]})
    assert code == 200 and st["plan_id"].startswith("p-")
    (saved,) = st["plans"]
    assert saved["id"] == st["plan_id"] and saved["title"] == p["title"] and saved["decided_by"] == "user"
    assert saved["course_short"] == "반도체공학" and saved["created_at"] and not saved["hidden"]
    assert [parse_iso(b["start"]) for b in saved["blocks"]] == starts(p)
    assert saved["blocks"][0]["uid"] == f"study-{saved['id']}-1@lms-agent"
    assert Store(store.path).data["plans"][0]["id"] == saved["id"]           # 파일에 저장됨

    # 같은 계획을 또 넣으면 겹침
    code, err = call(app, "/api/plan/apply", {"title": p["title"], "blocks": p["blocks"]})
    assert code == 400 and "겹쳐요" in err["error"]

    code, err = call(app, "/api/plan/remove", {"id": "p-nope"})
    assert code == 404
    code, st = call(app, "/api/plan/remove", {"id": saved["id"]})
    assert code == 200 and st["plans"] == [] and Store(store.path).data["plans"] == []


def test_apply_by_ai_keeps_reason(server):
    app, _ = server
    code, st = call(app, "/api/plan/apply", {"title": "기말 대비", "blocks": [future_block(2)], "decided_by": "ai",
                                             "reason": "기말고사 12/13 15:00 전, 마감이 없는 날만"})
    assert code == 200
    (p,) = st["plans"]
    assert p["decided_by"] == "ai" and p["reason"].startswith("기말고사") and p["blocks"][0]["title"] == "기말 대비"


@pytest.mark.parametrize("body, part", [
    ({"title": "x", "blocks": [future_block(-1)]}, "지난"),
    ({"title": "x", "blocks": [{"start": iso(now_local() - timedelta(minutes=5)),
                                "end": iso(now_local() + timedelta(minutes=55))}]}, "지난"),
    ({"title": "x", "blocks": [future_block(d % 40 + 1, h=6 + d // 40) for d in range(31)]}, "최대 30개"),
    ({"title": "x", "blocks": [future_block(1, minutes=10)]}, "15~240분"),
    ({"title": "x", "blocks": [future_block(1, minutes=300)]}, "15~240분"),
    ({"title": "x", "blocks": [future_block(1, minutes=90), future_block(1, h=21)]}, "겹쳐요"),
    ({"title": "가" * 121, "blocks": [future_block(1)]}, "120자"),
    ({"title": "x", "blocks": [future_block(1, title="나" * 121)]}, "120자"),
    ({"title": "", "blocks": [future_block(1)]}, "title"),
    ({"title": "x", "blocks": []}, "blocks"),
    ({"title": "x", "blocks": [{"start": "내일", "end": "모레"}]}, "시각"),
    ({"title": "x", "blocks": [future_block(400)]}, "너무 먼"),
    ({"title": "x", "blocks": [future_block(1)], "decided_by": "robot"}, "decided_by"),
    ({"title": "x", "blocks": ["문자열"]}, "블록"),
])
def test_apply_validation(server, body, part):
    app, store = server
    code, err = call(app, "/api/plan/apply", body)
    assert code == 400 and part in err["error"], err
    assert not store.data.get("plans")


def test_preview_bad_values_are_400(server):
    app, _ = server
    for body in ({"minutes": 5}, {"hour": 30}, {"days": "많이"}, {"start": "언젠가"}):
        code, err = call(app, "/api/plan/preview", body)
        assert code == 400 and err["error"]


# ---------------------------------------------------------------- 캘린더 파일·구글

def unfold(text):
    return text.replace("\r\n ", "").split("\r\n")


def vevents(text):
    out, cur = {}, None
    for ln in unfold(text):
        if ln == "BEGIN:VEVENT":
            cur = []
        elif ln == "END:VEVENT":
            uid = next(x for x in cur if x.startswith("UID:"))[4:]
            out[uid] = cur
            cur = None
        elif cur is not None:
            cur.append(ln)
    return out


def test_ics_contains_study_blocks(demo):
    before = vevents(core.calendar_text(demo))
    p = plan.make(demo, course_id="103", days=3, minutes=90, now=NOW)
    rec = plan.add(demo, p, now=NOW)
    text = core.calendar_text(demo)
    assert validate_ics(text) == []
    evs = vevents(text)
    study = {u: e for u, e in evs.items() if u.startswith("study-")}
    assert sorted(study) == [f"study-{rec['id']}-{n}@lms-agent" for n in (1, 2, 3)]
    for n, b in enumerate(rec["blocks"], 1):
        e = study[f"study-{rec['id']}-{n}@lms-agent"]
        assert f"DTSTART:{to_utc_stamp(parse_iso(b['start']))}" in e
        assert f"DTEND:{to_utc_stamp(parse_iso(b['end']))}" in e                # 길이가 있는 일정(마감과 다름)
        summary = next(x for x in e if x.startswith("SUMMARY:"))
        assert summary.startswith("SUMMARY:[공부] 반도체공학 기말고사 D-")
        assert "TRIGGER:-PT30M" in e and "CATEGORIES:공부" in e
        assert any(x.startswith("DESCRIPTION:") and "기말고사" in x for x in e)
    # 기존 마감·주간 요약 일정은 그대로(UID·알림)
    assert {u: e for u, e in evs.items() if not u.startswith("study-")}.keys() == before.keys()
    for u, e in evs.items():
        if u.startswith("week-"):
            assert "BEGIN:VALARM" not in e and "CATEGORIES:주간 요약" in e
    # 지우면 파일에서도 빠진다
    plan.remove(demo, rec["id"])
    assert not [u for u in vevents(core.calendar_text(demo)) if u.startswith("study-")]


def test_hidden_course_plan_not_exported(demo):
    rec = plan.add(demo, plan.make(demo, course_id="103", days=2, now=NOW), now=NOW)
    assert any(s.get("kind") == "study" for s in core.calendar_payload(demo)[1])
    demo.data["course_prefs"] = {"103": {"mode": "hidden"}}
    assert not any(s.get("kind") == "study" for s in core.calendar_payload(demo)[1])
    (st,) = plan.state(demo)
    assert st["id"] == rec["id"] and st["hidden"] is True


def test_gsync_desired_events_include_and_drop_study(demo):
    base = gsync.desired_counts(demo)
    assert "study" not in base
    rec = plan.add(demo, plan.make(demo, course_id="103", days=4, now=NOW), now=NOW)
    want = gsync.desired_events(demo)
    study = [e for e in want if e["lms_id"].startswith("study-")]
    assert [e["lms_id"] for e in study] == [f"study-{rec['id']}-{n}" for n in range(1, 5)]
    for e, b in zip(study, rec["blocks"]):
        assert e["start"] == parse_iso(b["start"]) and e["end"] == parse_iso(b["end"])
        assert e["reminders"] == [30] and e["summary"].startswith("[공부] ")
    d = gsync.desired_counts(demo)
    assert d == {**base, "study": 4} and sum(d.values()) == len(want)
    # 마감 일정은 그대로 길이 0, 알림 1일·3시간 전
    lab5 = next(e for e in want if e["lms_id"] == "canvas-101-1013")
    assert lab5["start"] == lab5["end"] and lab5["reminders"] == [1440, 180]
    plan.remove(demo, rec["id"])
    assert not [e for e in gsync.desired_events(demo) if e["lms_id"].startswith("study-")]
    assert gsync.desired_counts(demo) == base


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def raw(url, follow=True):
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=10) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def idle(app, timeout=10):
    end = _time.time() + timeout
    while _time.time() < end:
        if not app.google.pushing:
            return
        _time.sleep(0.05)
    raise AssertionError("구글 반영이 끝나지 않았습니다")


def test_google_push_creates_and_deletes_study_events(tmp_path):
    cmock, cbase = start_mock_canvas()
    gmock, gbase = start_mock_google()
    store = Store(tmp_path / "state.json")
    app = App(store, tmp_path, cfg={"canvas_url": cbase, "token": DEMO_TOKEN}, demo=True,
              google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=urls(gbase))
    core.sync(Canvas(cbase, DEMO_TOKEN), store)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        auth = call(app, "/api/google/connect", {})[1]["url"]
        _, h = raw(auth, follow=False)                                       # 가짜 구글 동의 → 앱 루트로
        _, h = raw(h["Location"], follow=False)
        assert h["Location"] == "/?google=ok#settings"
        idle(app)

        p = call(app, "/api/plan/preview", {"course_id": "103", "days": 3})[1]["plan"]
        code, st = call(app, "/api/plan/apply", {"title": p["title"], "course_id": "103", "item_id": p["item_id"],
                                                 "blocks": p["blocks"]})
        assert code == 200
        pid = st["plan_id"]
        idle(app)
        code, st = call(app, "/api/google/push", {})
        assert code == 200, st

        def study_events():
            (cal,) = gmock.data["events"].values()
            return [e for e in cal.values() if e["status"] != "cancelled"
                    and e["extendedProperties"]["private"]["lmsId"].startswith("study-")]

        evs = study_events()
        ids = sorted(e["extendedProperties"]["private"]["lmsId"] for e in evs)
        assert ids == [f"study-{pid}-{n}" for n in (1, 2, 3)]
        for e in evs:
            assert e["summary"].startswith("[공부] ")
            assert e["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]
            assert parse_iso(e["end"]["dateTime"]) - parse_iso(e["start"]["dateTime"]) == timedelta(minutes=60)
        st = call(app, "/api/state", method="GET")[1]
        assert st["google"]["desired"]["study"] == 3
        assert all(b["gcal_link"] for b in st["plans"][0]["blocks"])          # 근거 사슬: 구글에 들어간 링크

        code, _ = call(app, "/api/plan/remove", {"id": pid})
        assert code == 200
        idle(app)
        call(app, "/api/google/push", {})
        assert study_events() == []                                          # 계획을 지우면 구글 일정도 지워진다
        st = call(app, "/api/state", method="GET")[1]
        assert "study" not in st["google"]["desired"]
        assert url
    finally:
        httpd.shutdown()
        cmock.shutdown()
        gmock.shutdown()
