"""과목 종류: 성적 있음 / P/NP / 제외(계약 15장)와 과목별 읽은 글 수(read_counts), 공지 첨부의 화면 이름.

데모: 104 Capstone Design 강의계획서에 P/NP 문장 → pass_fail 제안, 105 '[학생] … 연구실 안전 교육'(학기 표시·공지·게시판 없음) → hidden 제안.
실제 이름 기준: "[학생] [한국어] 2026 인권과 성평등 교육 - 03분반" → hidden 제안, 테니스(글에 P/NP 없음) → 제안 없음.
"""
import json
import urllib.error
import urllib.request

import pytest

from lms_agent import agent_cli, celebrate, core, course_modes, gradebook, gsync, notify
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_STUDENT_ID, DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store, course_mode, hidden_courses


class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


@pytest.fixture()
def demo(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    app = App(store, tmp_path, cfg={}, scheduler=_Sched())
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, store, base
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


def courses_of(st):
    return {c["id"]: c for c in st["courses"]}


# ---------------------------------------------------------------- 제안

def _cache(name, code=None, syllabus=None, announcements=(), boards=(), pdf=None):
    return {"courses": [{"id": "1", "name": name, "course_code": code or name, "syllabus_body": syllabus}],
            "announcements": [{"id": i, "context_code": "course_1", "title": t, "message": m}
                              for i, (t, m) in enumerate(announcements)],
            "boards": list(boards), "conversations": [], "pdf_texts": pdf or {}}


def test_hidden_suggestion_real_name():
    name = "[학생] [한국어] 2026 인권과 성평등 교육 - 03분반"
    s = course_modes.suggestions(_cache(name))["1"]
    assert s == {"mode": "hidden", "evidence": name,
                 "reason": "정규 수업이 아닌 것 같아요(과목 코드에 학기 표시가 없고 공지·게시판이 없음)"}
    # 공지·게시판·강의계획서가 있으면 정규 수업일 수 있다 → 제안 없음
    assert "1" not in course_modes.suggestions(_cache(name, announcements=[("안내", "<p>x</p>")]))
    assert "1" not in course_modes.suggestions(_cache(name, syllabus="<p>강의계획</p>"))
    assert "1" not in course_modes.suggestions(_cache(name, boards=[{"course_id": "1", "board_title": "자료",
                                                                      "posts": [{"title": "a", "attachments": []}]}]))
    # 학기 표시가 있는 과목·대괄호 머리도 '교육'도 없는 과목은 제안하지 않는다
    assert "1" not in course_modes.suggestions(_cache("262R (서울-학부)[학생] 안전 교육-00분반"))
    assert "1" not in course_modes.suggestions(_cache("반도체공학"))
    # 과목 코드 모양을 비교할 다른 과목이 없으면 교육 낱말 하나로는 제안하지 않는다(가족 8: 대괄호 머리도 있어야)
    assert "1" not in course_modes.suggestions(_cache("Lab safety training"))
    assert course_modes.suggestions(_cache("[Students] Lab safety training"))["1"]["mode"] == "hidden"


def test_pass_fail_suggestion_needs_grading_words():
    tennis = "262R (서울-학부)테니스(TENNIS)-05분반"
    assert "1" not in course_modes.suggestions(_cache(tennis, syllabus="<p>매주 실기 수업. 출석 20%, 실기 80%</p>"))
    s = course_modes.suggestions(_cache(tennis, syllabus="<p>성적 평가: P/NP</p>"))["1"]
    assert s["mode"] == "pass_fail" and s["evidence"] == "성적 평가: P/NP" and "강의계획서" in s["reason"]
    for line in ("This course is graded Pass/Fail.", "평가 방법: 패스/논패스", "학점: 합격/불합격", "Grading: S/U basis"):
        assert course_modes.suggestions(_cache("X", pdf={"1:1": {"course_id": "1", "filename": "a.pdf",
                                                                 "text": line}}))["1"]["mode"] == "pass_fail", line
    # 실습 결과의 PASS/FAIL처럼 평가 낱말이 없는 줄은 근거가 아니다
    assert "1" not in course_modes.suggestions(_cache("ASIC", pdf={"1:1": {"course_id": "1", "filename": "Lab3.pdf",
                                                                           "text": "The testbench prints PASS/FAIL"}}))


def test_demo_courses_modes_suggestions_and_read_counts(demo):
    app, store, _ = demo
    cs = courses_of(app.state())
    assert all(c["mode"] == "normal" for c in cs.values())
    assert cs["104"]["mode_suggest"]["mode"] == "pass_fail" and "P/NP" in cs["104"]["mode_suggest"]["evidence"]
    assert cs["105"]["mode_suggest"]["mode"] == "hidden" and cs["105"]["short_name"] == "연구실 안전 교육"
    assert cs["101"]["mode_suggest"] is None and cs["102"]["mode_suggest"] is None
    assert cs["101"]["read_counts"] == {"announcements": 4, "board_posts": 2, "messages": 1, "pdfs": 0, "attachments": 4}
    assert cs["105"]["read_counts"] == {"announcements": 0, "board_posts": 0, "messages": 0, "pdfs": 0, "attachments": 0}
    # 사용자가 [맞아요]로 정한다 — 한 번 정하면 다시 제안하지 않는다(normal로 정해도)
    code, st = call(app, "POST", "/api/course/104/mode", {"mode": "normal"})
    assert code == 200 and courses_of(st)["104"]["mode_suggest"] is None and courses_of(st)["104"]["mode"] == "normal"
    assert store.data["course_prefs"]["104"]["mode"] == "normal" and store.data["course_prefs"]["104"]["at"]
    assert call(app, "POST", "/api/course/104/mode", {"mode": "skip"})[0] == 400
    assert call(app, "POST", "/api/course/999/mode", {"mode": "hidden"})[0] == 400


# ---------------------------------------------------------------- 제외(hidden)

def test_hidden_course_is_left_out_everywhere(demo, tmp_path):
    app, store, base = demo
    before = app.state()
    asic_items = [i for i in before["items"] if i["course_id"] == "101"]
    assert asic_items and any(c["course_id"] == "101" for c in before["grades"]["courses"])
    assert any(p["kind"] == "graded" and p["course_short"] == "ASIC설계(영강)" for p in before["celebrations"]["pending"])
    desired_before = gsync.desired_counts(store)
    code, st = call(app, "POST", "/api/course/101/mode", {"mode": "hidden"})
    assert code == 200 and hidden_courses(store.data) == {"101"}
    assert courses_of(st)["101"]["mode"] == "hidden"            # 과목 목록에는 남는다('제외한 과목'에 [다시 보기])
    assert not [i for i in st["items"] if i["course_id"] == "101"]
    assert not [c for c in st["grades"]["courses"] if c["course_id"] == "101"]
    assert not [c for c in st["grades"]["candidates"] if c["course_id"] == "101"]
    assert not [p for p in st["grades"]["priorities"] if p["course"] == "ASIC설계(영강)"]
    assert not [r for r in st["exams"]["upcoming"] + st["exams"]["not_announced"] if r["course_id"] == "101"]
    assert not [p for p in st["celebrations"]["pending"] if p.get("course_short") == "ASIC설계(영강)"]
    # 캘린더(.ics·구글): 그 과목 일정이 빠진다 → 다음 반영 때 구글에서 지워진다
    items, summaries = core.calendar_payload(store)
    assert not [i for i in items if i["course_id"] == "101"]
    assert not any("ASIC" in s["description"] for s in summaries)
    ids = {e["lms_id"] for e in gsync.desired_events(store)}
    assert not any(i.startswith("canvas-101-") for i in ids)
    assert gsync.desired_counts(store)["deadlines"] < desired_before["deadlines"]
    assert "canvas-101-" not in core.calendar_text(store)
    # 확인 알림·AI 정리·백그라운드 알림 대상에서도
    assert not any("ASIC" in line for r in core.review_reminder(store) for line in r["description"].splitlines())
    todo, view = agent_cli.review_items(store)
    assert not [v for v in todo + view if v["course_id"] == "101"]
    report = {"new_items": [i["id"] for i in asic_items]}
    assert notify.new_deadlines_message(report, store.view()) is None
    # 항목·사용자 결정은 그대로 — 다시 보이면 바로 돌아온다
    assert [it for it in store.data["items"] if it["course_id"] == "101"]
    code, st = call(app, "POST", "/api/course/101/mode", {"mode": "normal"})
    assert code == 200 and len([i for i in st["items"] if i["course_id"] == "101"]) == len(asic_items)
    assert any(c["course_id"] == "101" for c in st["grades"]["courses"])


def test_hidden_course_downloads_and_news(tmp_path):
    """제외한 과목은 첨부를 새로 받지 않고(받은 파일은 둠), 소식 카드(자료)에서도 빠진다."""
    mock, base = start_mock_canvas()
    try:
        store = Store(tmp_path / "state.json")
        store.data["course_prefs"] = {"101": {"mode": "hidden", "at": "2026-11-13T00:00:00+09:00"}}
        core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl")
        assert store.data.get("downloads", {}) == {}            # 데모 첨부는 모두 101(ASIC)
        store.data["course_prefs"] = {}
        core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl")
        got = store.data["downloads"]
        assert len(got) == 4 and all(m["course_id"] == "101" for m in got.values())
        app = App(store, tmp_path, cfg={}, scheduler=_Sched())
        mats = app.state()["materials"]["items"]
        ann = [m for m in mats if m["filename"] in ("Lab0_score.pdf", "Lab1_score.pdf")]
        assert len(ann) == 2 and all(m["board_title"] == "공지 첨부 파일" and m["board_folder"] == "공지" for m in ann)
        assert all(("/공지/" in m.replace("\\", "/")) for m in [got[k]["path"] for k in got if "cf" in k])  # 폴더 이름은 그대로
        board = next(m for m in mats if m["filename"] == "Lab6.pdf")
        assert board["board_title"] == "과제" and not board["course_hidden"]
        store.data["course_prefs"] = {"101": {"mode": "hidden", "at": "2026-11-13T00:00:00+09:00"}}
        assert all(m["course_hidden"] for m in app.state()["materials"]["items"])
        new, changes, mats_news, weights, _ = celebrate._news_parts(store, [], core.now_local(), {"items": {}})
        assert mats_news == [] and weights == []
    finally:
        mock.shutdown()


# ---------------------------------------------------------------- P/NP(pass_fail)

def test_pass_fail_no_letter_estimate_and_no_band(demo):
    app, store, _ = demo
    gradebook.save_course(store, "102", {"components": [
        {"name": "중간고사", "weight": 30, "score": 78, "mean": 65.3, "sd": 12.1},
        {"name": "기말고사", "weight": 70}]})
    st = app.state()
    dsp = next(c for c in st["grades"]["courses"] if c["course_id"] == "102")
    assert dsp["estimate"] and not dsp["pass_fail"]
    assert any(p["course"] == "디지털신호처리" for p in st["grades"]["priorities"])
    code, st = call(app, "POST", "/api/course/102/mode", {"mode": "pass_fail"})
    assert code == 200
    dsp = next(c for c in st["grades"]["courses"] if c["course_id"] == "102")
    assert dsp["pass_fail"] and dsp["mode"] == "pass_fail" and dsp["pf_note"] == "P/NP 과목 — 학점 추정 안 함"
    assert dsp["estimate"] is None and dsp["needed"] == [] and dsp["error"] is None
    assert dsp["config"]["components"][0]["score"] == 78         # 점수는 보여 준다
    assert not any(p["course"] == "디지털신호처리" for p in st["grades"]["priorities"])
    mid = next(p for p in st["celebrations"]["pending"] if p["kind"] == "score" and p["course_short"] == "디지털신호처리")
    assert (mid["band"], mid["letter"], mid["top"], mid["letter_basis"]) == (None, None, None, None)
    # 일정은 그대로
    assert [i for i in st["items"] if i["course_id"] == "102"]
    assert course_mode(store.data, "102") == "pass_fail"


def test_demo_student_id_constant_is_fake():
    assert DEMO_STUDENT_ID.startswith("2026") and len(DEMO_STUDENT_ID) == 10


# ================================================================ 일반화(docs/CORNER_CASES.md 가족 8)
# '정규 수업이 아닌 과목' 신호를 학교마다 다른 과목 코드 모양에도: 내 다른 과목들이 함께 쓰는 코드 모양을 배워서,
# 그 모양이 없고 + (대괄호 대상 머리 또는 교육·training으로 끝나는 이름) + 공지·게시판 없음일 때만 제안한다.
# 어느 과목에도 코드 모양이 없으면(다른 학교) 대괄호 머리와 교육 낱말이 둘 다 있을 때만.

def _many(*courses, announcements=()):
    return {"courses": [{"id": str(i), "name": n, "course_code": c or n, "syllabus_body": None}
                        for i, (n, c) in enumerate(courses, 1)],
            "announcements": [{"id": i, "context_code": f"course_{cid}", "title": "t", "message": "<p>x</p>"}
                              for i, cid in enumerate(announcements)],
            "boards": [], "conversations": [], "pdf_texts": {}}


@pytest.mark.parametrize("courses,hidden", [
    # 고려대 실제 모양: 학기 코드 '262R (서울-학부)'를 다른 과목이 함께 쓴다
    ((("262R (서울-학부)로봇공학개론(GENERAL ROBOTICS)-00분반", None), ("262R (서울-학부)테니스(TENNIS)-05분반", None),
      ("[학생] [한국어] 2026 인권과 성평등 교육 - 03분반", None)), {"3"}),
    # 미국식: 과목 코드가 course_code에만 있다
    ((("Intro to Programming", "CS 101"), ("Calculus II", "MATH 2210"),
      ("Title IX Training 2026", "Title IX Training 2026")), {"3"}),
    # 이름 앞에 코드: 'CS 101: …'
    ((("CS 101: Intro to Programming", None), ("MATH 2210: Calculus II", None),
      ("Academic Integrity Module", None)), {"3"}),
    # 코드가 어디에도 없는 학교: 대괄호 머리 + 오리엔테이션 → 제안
    ((("Calculus", None), ("Physics", None), ("[All Students] New Student Orientation", None)), {"3"}),
    # 코드가 어디에도 없고 교육 낱말만 → 제안하지 않음
    ((("Calculus", None), ("Physics", None), ("Lab Safety Training", None)), set()),
    # 이름 중간에 교육·안전이 있는 정규 과목
    ((("262R (서울-학부)교육학개론-00분반", None), ("262R (서울-학부)안전공학-01분반", None),
      ("262R (서울-학부)테니스-05분반", None)), set()),
    # 코드 모양이 다르지만 교육 낱말도 대괄호 머리도 없음
    ((("262R (서울-학부)로봇공학개론-00분반", None), ("262R (서울-학부)테니스-05분반", None), ("Robotics Club", None)), set()),
    # 대괄호 머리가 붙은 정규 과목(코드 모양이 같다)
    ((("[영강] 262R (서울-학부)반도체공학-00분반", None), ("262R (서울-학부)테니스-05분반", None),
      ("262R (서울-학부)로봇공학개론-00분반", None)), set()),
    # 과목이 하나뿐이고 코드도 없음: 대괄호 머리 + 교육
    ((("[필수] 연구실 안전교육 (2026)", None),), {"1"}),
])
def test_hidden_suggestion_general(courses, hidden):
    got = {cid for cid, s in course_modes.suggestions(_many(*courses)).items() if s["mode"] == "hidden"}
    assert got == hidden, courses


def test_hidden_suggestion_needs_no_posts_even_for_outlier():
    cache = _many(("Intro to Programming", "CS 101"), ("Calculus II", "MATH 2210"),
                  ("Title IX Training 2026", "Title IX Training 2026"), announcements=["3"])
    assert "3" not in course_modes.suggestions(cache)
