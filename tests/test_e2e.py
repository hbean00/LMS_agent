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
from lms_agent.core import short_course_name  # noqa: E402
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
    rows = [v for v in store.view() if short_course_name(v["course_name"]) == course and parse_iso(v["due"]) == due]
    assert len(rows) == 1, f"{course} {due}: {len(rows)}개 — {[(v['title'], v['due']) for v in store.view() if v['course_name'] == course]}"
    return rows[0]


def test_report_counts(demo):
    _, _, _, r = demo
    assert (r["courses"], r["assignments"], r["errors"]) == (5, 5, [])   # 105 연구실 안전 교육(과제·공지 없음)
    assert r["no_deadline"] == 1  # Weekly log 9


def test_email_assignment_from_announcement_linked_and_conflicting(demo):
    _, _, store, _ = demo
    a = find(store, "ASIC설계(영강)", at(6))
    b = find(store, "ASIC설계(영강)", at(8))
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
    lab3 = find(store, "ASIC설계(영강)", at(-5))
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
    a = find(store, "ASIC설계(영강)", at(8))
    store.update_user(a["id"], {"decision": "approved", "submitted": True, "title": "LAB4 최종"})
    core.sync(Canvas(base, DEMO_TOKEN), store)
    again = find(store, "ASIC설계(영강)", at(8))
    assert (again["status"], again["submitted"], again["title"], again["edited"]) == ("scheduled", True, "LAB4 최종", True)


def test_manual_paste_without_canvas(tmp_path):
    store = Store(tmp_path / "state.json")
    store.add_manual("회로이론", "기말 리포트는 12월 3일 18:00까지 조교 메일 ta@x.ac.kr 로 보내 주세요.", T.isoformat())
    core.rebuild(store)
    (it,) = store.view()
    assert it["status"] == "review" and it["method"] == "email" and it["course_name"] == "회로이론"


# ---------------------------------------------------------------- 학기 (v0.2)

def test_semester_and_courses(demo):
    _, _, store, _ = demo
    sem = core.semester_info(store.data["cache"], store.view())
    assert sem["source"] == "canvas" and sem["week_now"] == 11 and sem["weeks_total"] in (16, 17)
    assert sem["weeks"][0]["start"] <= (T - timedelta(days=70)).isoformat()
    items = core.annotate(store.view(), sem, store.data["cache"])
    lab5 = next(i for i in items if i["title"] == "LAB5 예비보고서")
    assert lab5["course_short"] == "ASIC설계(영강)" and lab5["week"] in (12, 13)
    courses = {c["id"]: c for c in core.course_summaries(store.data["cache"])}
    assert courses["101"]["short_name"] == "ASIC설계(영강)"
    assert (courses["101"]["current_score"], courses["101"]["graded"], courses["101"]["assignments"]) == (92.5, 1, 3)
    assert courses["103"]["current_score"] is None


@pytest.mark.parametrize("name,short", [
    ("262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반", "ASIC설계(영강)"),
    ("262R (서울-학부)회로이론(1)-01분반", "회로이론(1)"),
    ("디지털신호처리", "디지털신호처리"),
    ("ASIC설계(궁재하)", "ASIC설계(궁재하)"),
    ("Capstone Design", "Capstone Design"),
    ("[학생] [한국어] 2026 인권과 성평등 교육 - 03분반", "인권과 성평등 교육"),
])
def test_short_course_name(name, short):
    assert short_course_name(name) == short


def test_long_canvas_term_snaps_to_16_week_semester():
    """실제 고려대 데이터: 학기(262R)가 8/23~이듬해 2/27로 길고, 1년짜리 필수 교육 과목이 섞여 있다."""
    cache = {"courses": [
        {"id": "1", "name": "[학생] [한국어] 2026 인권과 성평등 교육 - 03분반",
         "term_start": "2026-03-09T00:00:00+09:00", "term_end": "2027-02-21T23:59:00+09:00"},
        *[{"id": str(i), "name": "x", "term_start": "2026-08-23T00:00:00+09:00",
           "term_end": "2027-02-27T23:59:00+09:00"} for i in (2, 3, 4)]]}
    sem = core.semester_info(cache, [], today=datetime(2026, 11, 13).date())
    assert (sem["start"], sem["weeks_total"], sem["week_now"]) == ("2026-09-01", 16, 11)
    assert sem["weeks"][0]["start"] == "2026-08-31" and sem["note"]


def test_default_semester_when_no_dates():
    sem = core.semester_info(None, [], today=datetime(2026, 11, 12).date())
    assert (sem["source"], sem["start"], sem["end"]) == ("default", "2026-09-01", "2026-12-20")
    assert sem["week_now"] == 11


def _deadline_events(text):
    return sum(1 for line in text.split("\r\n") if line.startswith("UID:") and not line.startswith("UID:week-"))


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
    assert _deadline_events(text) == 1 + 3  # 승인 1 + 마감 있는 Canvas 과제 3(LAB3, LAB5, HW5)
    assert "UID:week-" in text and "주차: 마감" in text.replace("\r\n ", "")  # 주간 요약(기본 켜짐)

    code, body, _ = _req(url + "/api/settings", "POST", app.token, {"include_canvas": False})
    assert code == 200
    text = _req(url + "/calendar.ics")[1].decode("utf-8")
    assert _deadline_events(text) == 1
    _req(url + "/api/settings", "POST", app.token, {"weekly_summary": False})
    text = _req(url + "/calendar.ics")[1].decode("utf-8")
    assert "UID:week-" not in text and validate_ics(text) == []

    code, body, _ = _req(url + "/api/setup", "POST", app.token, {"canvas_url": "x", "token": "y"})
    assert code == 400  # 데모에서는 설정 변경 불가


def test_server_grades_links_exams_and_modes(running):
    """계약 14·15장 칸이 화면 API에 그대로 나온다(데모: ASIC 'Updated Grading Policy'·퀴즈 공지, 105 교육 과목)."""
    app, url = running
    state = json.loads(_req(url + "/api/state", token=app.token)[1])
    asic = next(c for c in state["grades"]["courses"] if c["course_id"] == "101")
    names = [x["name"] for x in asic["config"]["components"]]
    assert names == ["Homework assignments", "Midterm project", "Midterm quiz", "Final project", "Final quiz", "Attendance"]
    hw = asic["config"]["components"][0]
    assert set(hw) >= {"members", "derived", "count", "schedule", "date_status"}
    # 이 시험은 첨부를 받지 않아(Lab6.pdf의 'Next labs' 단서 없음) 관찰만: LAB 0·1(점수 공지 제목)·3~6 → 최소 7개
    assert (hw["count"]["n"], hw["count"]["low"], hw["count"]["basis"]) == (None, 7, "observed")
    assert {"LAB4 보고서", "LAB5 예비보고서"} <= {s["title"] for s in hw["schedule"]}
    assert asic["remaining"][0] == {"component": "Homework assignments", "weight": 30.0, "count_left": None, "range": None}
    # 계약 16장: 'Updated Grading Policy'의 학점 가이드라인(A+, A0: 35~45% …) → 강의계획서 비율이 기본값을 대신한다
    ds = asic["dist_source"]
    assert (ds["by"], ds["kind"], ds["source_title"], ds["found_by"]) == ("syllabus", "relative", "Updated Grading Policy",
                                                                          "rules")
    assert ds["ranges"] == {"A+·A0": [35, 45], "B+·B0": [40, 50], "C+~F": [5, 25]}
    assert asic["letter_basis"] == "syllabus" and asic["using_default_dist"] is False
    assert (asic["dist_used"]["A+"], asic["dist_used"]["B0"], asic["dist_used"]["F"]) == (20, 22.5, 3)
    dsp = next(c for c in state["grades"]["courses"] if c["course_id"] == "102")
    assert dsp["dist_source"]["by"] == "default" and dsp["letter_basis"] == "default"
    assert state["grades"]["score_files_waiting"] == 0 and state["grades"]["score_files_unmatched"] == []
    quiz = next(i for i in state["items"] if i["course_id"] == "101" and i["kind"] == "시험")
    assert (quiz["title"], quiz["component"], quiz["component_by"]) == ("Quiz", "Midterm quiz", "rules")
    assert parse_iso(quiz["due"]) == at(5)
    up = state["exams"]["upcoming"]
    final = next(i for i in state["items"] if i["course_id"] == "103" and i["kind"] == "시험")   # 평가 항목이 없는 과목
    assert [(r["course_short"], r["component"], r["weight"], r["item_id"]) for r in up] == [
        ("ASIC설계(영강)", "Midterm quiz", 5.0, quiz["id"]), ("반도체공학", None, None, final["id"])]
    assert parse_iso(up[1]["due"]) == at(30, 15, 0) and up[1]["title"] == "기말고사" and up[1]["status"] == "review"
    assert {"course_id": "101", "course_short": "ASIC설계(영강)", "component": "Final quiz", "weight": 5.0} \
        in state["exams"]["not_announced"]
    cs = {c["id"]: c for c in state["courses"]}
    assert cs["105"]["mode_suggest"]["mode"] == "hidden" and cs["104"]["mode_suggest"]["mode"] == "pass_fail"
    assert all(set(c["read_counts"]) == {"announcements", "board_posts", "messages", "pdfs", "attachments"}
               for c in cs.values())
    code, body, _ = _req(url + "/api/course/105/mode", "POST", app.token, {"mode": "hidden"})
    assert code == 200 and {c["id"]: c for c in json.loads(body)["courses"]}["105"]["mode"] == "hidden"
    assert _req(url + "/api/course/105/mode", "POST", app.token, {"mode": "x"})[0] == 400
    assert _req(url + "/api/grades/101/count", "POST", app.token, {"component": "Homework assignments", "n": 99})[0] == 400
    code, body, _ = _req(url + f"/api/item/{quiz['id']}/component", "POST", app.token, {"component": "Final quiz"})
    assert code == 200 and json.loads(body)["exams"]["upcoming"][0]["component"] == "Final quiz"
    # 연결을 끊으면(null) 연결 안 된 시험으로 남는다 — 같은 시험이 두 번 나오지 않는다
    code, body, _ = _req(url + f"/api/item/{quiz['id']}/component", "POST", app.token, {"component": None})
    up = json.loads(body)["exams"]["upcoming"]
    assert code == 200 and [r["item_id"] for r in up].count(quiz["id"]) == 1
    assert next(r for r in up if r["item_id"] == quiz["id"])["component"] is None
    # 제외(rejected)한 시험·제외(hidden)한 과목의 시험은 빠진다
    code, body, _ = _req(url + f"/api/item/{final['id']}", "POST", app.token, {"decision": "rejected"})
    assert code == 200 and final["id"] not in [r["item_id"] for r in json.loads(body)["exams"]["upcoming"]]
    _req(url + f"/api/item/{final['id']}", "POST", app.token, {"decision": "pending"})
    code, body, _ = _req(url + "/api/course/103/mode", "POST", app.token, {"mode": "hidden"})
    assert code == 200 and not [r for r in json.loads(body)["exams"]["upcoming"] if r["course_id"] == "103"]
