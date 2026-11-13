"""성적 '만약에'(계약 22-3): 질문 알아듣기, 과목·평가 항목 고르기, 그리고 숫자가 성적 화면과 같은 계산
(gradebook.section → grades.estimate / needed_on)에서만 나오는지.

기대값은 같은 입력으로 엔진 함수를 직접 불러 만든다(숫자를 손으로 적지 않음 — 엔진이 바뀌어도 '같은 길'인지만 본다).
시각에 기대지 않는다(UTC CI): 학기 주차는 성적 계산에 쓰이지 않는다.
"""
import copy
import json
import re

import pytest

from lms_agent import core, course_modes, distribution as dm, gradebook, grades as g, whatif
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_STUDENT_ID, DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import now_local

ROB, PE, EE, SEMI, LIN = "301", "302", "303", "304", "305"
CUTOFFS = {"A+": 95, "A0": 90, "B+": 85, "B0": 80, "C+": 75, "C0": 70, "D+": 65, "D0": 60}
SHAPE = {"course_id", "course_short", "kind", "target", "component", "lines", "rows", "basis"}


def _cache(*courses):
    return {"courses": [{"id": cid, "name": name, "course_code": code} for cid, name, code in courses],
            "assignments": [], "announcements": [], "conversations": [], "boards": []}


def make_store(tmp_path, which=(ROB, PE, EE, SEMI, LIN)) -> Store:
    """로봇공학개론(상대평가·점수·평균·표준편차), 테니스(P/NP), 회로이론(절대평가 기준표), 반도체공학(평가 항목 없음),
    선형대수(비중만 있고 점수 없음)."""
    s = Store(tmp_path / "state.json")
    names = {ROB: ("262R (서울-학부)로봇공학개론(Intro to Robotics)-00분반", "ROB101"), PE: ("테니스", "PE201"),
             EE: ("회로이론", "EE210"), SEMI: ("반도체공학", "EE330"), LIN: ("선형대수", "MATH210")}
    s.data["cache"] = _cache(*[(cid, *names[cid]) for cid in which])
    if ROB in which:
        gradebook.save_course(s, ROB, {"components": [
            {"name": "중간고사", "weight": 35, "score": 72, "mean": 60, "sd": 15},
            {"name": "기말고사", "weight": 35},
            {"name": "과제", "weight": 20, "score": 17, "max_score": 20, "mean": 15, "sd": 3},
            {"name": "출석", "weight": 10, "fixed": True}], "class_size": 40})
    if PE in which:
        gradebook.save_course(s, PE, {"components": [
            {"name": "출석", "weight": 30, "score": 28, "max_score": 30},
            {"name": "실기 시험", "weight": 40, "score": 80},
            {"name": "기말 실기", "weight": 30}]})
        s.data["course_prefs"] = {PE: {"mode": "pass_fail"}}
    if EE in which:
        gradebook.save_course(s, EE, {"components": [
            {"name": "중간고사", "weight": 40, "score": 85}, {"name": "기말고사", "weight": 40},
            {"name": "과제", "weight": 20, "score": 18, "max_score": 20}]})
        s.data["grades"][EE]["dist_auto"] = {"kind": "absolute", "cutoffs": dict(CUTOFFS), "source_title": "강의계획서",
                                             "summary": "절대평가 A+ 95 · A0 90 …점 이상", "by": "rules"}
    if LIN in which:
        gradebook.save_course(s, LIN, {"components": [{"name": "중간고사", "weight": 50}, {"name": "기말고사", "weight": 50}]})
    return s


def section_course(store, cid):
    """features/core·features/grades와 같은 길로 만든 state.grades의 과목 하나."""
    cache = store.data.get("cache")
    its = store.view()
    sem = core.semester_info(cache, its)
    core.annotate(its, sem, cache)
    gradebook.link_items(store, its, sem)
    courses = course_modes.annotate_courses(core.course_summaries(cache), store.data)
    sec = gradebook.section(store, courses, items=its, sem=sem)
    return next(c for c in sec["courses"] if str(c["course_id"]) == cid)


def rows_of(w):
    return {r["label"]: r["value"] for r in w["rows"]}


def snapshot(store):
    return json.dumps(store.data, sort_keys=True, ensure_ascii=False, default=str)


def check_shape(r):
    assert set(r) >= {"answer", "whatif"} and isinstance(r["answer"], str) and r["answer"]
    w = r["whatif"]
    assert set(w) == SHAPE
    assert w["kind"] in ("needed", "scenario", "pass_fail", "current", "ask_course")
    assert all(isinstance(x, str) for x in w["lines"] + w["basis"])
    assert all(set(x) == {"label", "value"} and isinstance(x["label"], str) and isinstance(x["value"], str)
               for x in w["rows"])
    assert "새로" not in r["answer"] + " ".join(w["lines"] + w["basis"])
    return w


# ---------------------------------------------------------------- 질문 알아듣기

@pytest.mark.parametrize("msg,intent,target", [
    ("기말에서 몇 점 받아야 A?", "needed", "A0"),
    ("로공개 A 받으려면?", "needed", "A0"),
    ("A+ 받으려면?", "needed", "A+"),
    ("에이쁠 가능해?", "needed", "A+"),
    ("B0 받을 수 있을까?", "needed", "B0"),
    ("기말 몇 점 받아야 해?", "needed", None),
    ("F만 면하려면 기말 몇 점?", "needed", "PASS_LOWEST"),
    ("중간 70점인데 기말 몇 점 받아야 A?", "needed", "A0"),
    ("LAB 하나 0점이면?", "scenario", None),
    ("중간 70점이면 학점은?", "scenario", None),
    ("과제 두 개 안 내면?", "scenario", None),
    ("기말 90점이면 A 가능해?", "scenario", "A0"),
    ("테니스 P 기준 넘었어?", "pass_fail", None),
    ("ASIC 지금 학점 몇이야?", "current", None),
])
def test_parse_intents(msg, intent, target):
    q = whatif.parse(msg)
    assert q is not None and (q["intent"], q["target"]) == (intent, target), (msg, q and q["intent"], q and q["target"])


@pytest.mark.parametrize("msg", [
    "", "기말 범위 어디까지야?", "기말고사 날짜가 안 보여요", "성적이 안 들어와요", "중간고사 점수가 이상해요",
    "LAB4 연장 문의 메일 써줘", "출석 몇 번 빠지면 F야?", "중간고사 평균 몇이야?", "C언어 과제 언제까지야?",
    "컴퓨터 켤 때 자동 실행해줘", "기말 전 2주 공부 계획 세워줘", "ASIC 수업 어디서 해?", "LAB4 0점 받았는데 재제출 되나요?",
    # 놓친 것 신고(22-1 갈래 5, tests/test_agent_chat.py) — 점수·학점 낱말이 있어도 만약에가 가로채지 않는다
    "신호와시스템 성적이 안 들어와요", "열역학 성적이 안 들어오는 문제가 있습니다", "LAB 점수가 성적에 안 들어가요",
    "이 과목 평가 비중이 이상해요", "Signals 기말 날짜가 안 보여요", "LAB 점수가 성적에 반영이 안 돼요",
    "중간고사 점수가 0점으로 나와요 오류인가요?", "기말 0점이면 F인데 점수가 누락됐어요",
])
def test_not_whatif_is_none(msg, tmp_path):
    assert whatif.parse(msg) is None
    assert whatif.handle(make_store(tmp_path, (ROB,)), msg) is None


def test_parse_pairs_scores_with_components_and_counts():
    q = whatif.parse("LAB 하나 0점이면?")
    (a,) = q["assumptions"]
    assert (a["group"]["text"], a["count"], a["spec"]["kind"], a["spec"]["v"]) == ("LAB", 1, "points", 0.0)
    q = whatif.parse("중간 70점, 기말 80/100이면 학점은?")
    assert [(a["group"]["text"], a["spec"]["kind"]) for a in q["assumptions"]] == [("중간", "points"), ("기말", "frac")]
    q = whatif.parse("Lab0 40점이면?")
    assert q["assumptions"][0]["group"]["number"] == 0
    q = whatif.parse("과제 두 개 안 내면?")
    assert (q["assumptions"][0]["count"], q["assumptions"][0]["spec"]["kind"]) == (2, "zero")
    q = whatif.parse("중간 70점인데 기말 몇 점 받아야 A?")   # 가정한 '중간'은 필요 점수 대상에서 빠지고 '기말'만 남는다
    assert [gr["text"] for gr in q["groups"]] == ["기말"] and len(q["assumptions"]) == 1
    assert whatif.parse("상위 30% 안에 들려면 기말 몇 점 받아야 해?")["assumptions"] == []   # '상위 30%'는 점수가 아님


# ---------------------------------------------------------------- needed

def test_needed_numbers_come_from_needed_on(tmp_path):
    s = make_store(tmp_path)
    before = snapshot(s)
    r = whatif.handle(s, "로봇공학개론 기말에서 몇 점 받아야 A?", now=now_local())
    w = check_shape(r)
    assert (w["kind"], w["course_id"], w["course_short"], w["target"], w["component"]) == (
        "needed", ROB, "로봇공학개론", "A0", "기말고사")
    assert any("'A'는 A0 이상으로 봤어요" in b for b in w["basis"])
    assert any("기본값" in b for b in w["basis"])            # 학점 비율을 몰라 기본값으로 어림했다고 적는다
    c = section_course(s, ROB)
    eff = dm.effective(c["config"])
    ref = g.needed_on(gradebook.expand_components(c["config"]["components"]), "기말고사", "A0", eff["dist"],
                      cutoffs=eff["cutoffs"])
    value = f"약 {whatif._n(ref['need_score'])}/100점 이상 (상위 {whatif._n(ref['need_top'])}%)"
    assert w["rows"] == [{"label": "기말고사 (35%)", "value": value}]
    assert value in r["answer"]
    # 성적 화면이 이미 구한 필요 점수(지금 학점·한 단계 위)에 A0가 있으면 같은 값이다
    same = [n for n in c["needed"] if n["component"] == "기말고사" and n["target"] == "A0"]
    assert all(n["need_score"] == pytest.approx(ref["need_score"]) for n in same)
    assert f"지금 추정: {c['estimate']['grade']} · 상위 {whatif._n(c['estimate']['top'])}%" in w["lines"]
    assert snapshot(s) == before                               # 저장된 상태는 그대로


def test_needed_a_plus_stays_a_plus_and_abbreviation(tmp_path):
    s = make_store(tmp_path)
    w = check_shape(whatif.handle(s, "로공개 A+ 받으려면?"))   # 로봇공학개론의 줄임말
    assert (w["course_id"], w["target"]) == (ROB, "A+")
    assert not any("A0 이상으로 봤어요" in b for b in w["basis"])


def test_needed_premise_score_is_applied_to_a_copy(tmp_path):
    s = make_store(tmp_path)
    before = snapshot(s)
    r = whatif.handle(s, "로봇공학개론 중간 50점인데 기말 몇 점 받아야 A?")
    w = check_shape(r)
    assert (w["kind"], w["component"]) == ("needed", "기말고사")
    c = section_course(s, ROB)
    eff = dm.effective(c["config"])
    hyp = copy.deepcopy(gradebook.expand_components(c["config"]["components"]))
    next(e for e in hyp if e["name"] == "중간고사")["score"] = 50.0
    ref = g.needed_on(hyp, "기말고사", "A0", eff["dist"], cutoffs=eff["cutoffs"])
    assert rows_of(w)["기말고사 (35%)"].startswith(f"약 {whatif._n(ref['need_score'])}/100점 이상")
    assert r["answer"].startswith("중간고사를 50/100점으로 가정했어요.")
    assert snapshot(s) == before


def test_needed_without_target_uses_next_grade(tmp_path):
    s = make_store(tmp_path)
    w = check_shape(whatif.handle(s, "로봇공학개론 기말 몇 점 받아야 해?"))
    c = section_course(s, ROB)
    eff = dm.effective(c["config"])
    better = gradebook._better(c["estimate"]["grade"], eff["dist"], eff["cutoffs"])
    assert w["kind"] == "needed" and w["target"] == (better or c["estimate"]["grade"])
    assert any("목표 학점을 말하지 않아" in b for b in w["basis"])


def test_needed_absolute_cutoffs(tmp_path):
    s = make_store(tmp_path)
    w = check_shape(whatif.handle(s, "회로이론 기말 몇 점 받아야 A?"))
    c = section_course(s, EE)
    eff = dm.effective(c["config"])
    assert eff["cutoffs"] == CUTOFFS
    ref = g.needed_on(gradebook.expand_components(c["config"]["components"]), "기말고사", "A0", eff["dist"],
                      cutoffs=eff["cutoffs"])
    assert w["rows"] == [{"label": "기말고사 (40%)", "value": f"약 {whatif._n(ref['need_score'])}/100점 이상"}]
    assert any("절대평가" in b for b in w["basis"])


def test_needed_ambiguous_component_gives_row_per_component(tmp_path):
    """'기말'이 Final project·Final quiz 둘 다면 항목마다 한 줄(남은 항목이 여럿이면 항목별)."""
    s = Store(tmp_path / "state.json")
    s.data["cache"] = _cache(("401", "ASIC설계", "EE401"))
    gradebook.save_course(s, "401", {"components": [
        {"name": "Midterm exam", "weight": 40, "score": 70, "mean": 60, "sd": 10},
        {"name": "Final project", "weight": 40}, {"name": "Final quiz", "weight": 20}]})
    w = check_shape(whatif.handle(s, "기말에서 몇 점 받아야 A?"))   # 성적이 있는 과목이 하나라 그 과목
    assert (w["course_id"], w["kind"], w["component"]) == ("401", "needed", None)
    c = section_course(s, "401")
    eff = dm.effective(c["config"])
    exp = gradebook.expand_components(c["config"]["components"])
    for name, label in (("Final project", "Final project (40%)"), ("Final quiz", "Final quiz (20%)")):
        ref = g.needed_on(exp, name, "A0", eff["dist"])
        assert rows_of(w)[label] == f"약 {whatif._n(ref['need_score'])}/100점 이상 (상위 {whatif._n(ref['need_top'])}%)"
    # 가정 점수에서는 되묻는다(어느 쪽인지 모르면 계산하지 않음)
    r = whatif.handle(s, "기말 0점이면?")
    w = check_shape(r)
    assert w["kind"] == "current" and "Final project, Final quiz 중 어느 것인지" in r["answer"]


# ---------------------------------------------------------------- scenario

def test_scenario_reestimates_with_estimate(tmp_path):
    s = make_store(tmp_path)
    before = snapshot(s)
    r = whatif.handle(s, "로봇공학개론 중간 50점이면 학점은?")
    w = check_shape(r)
    assert (w["kind"], w["course_id"], w["component"], w["target"]) == ("scenario", ROB, "중간고사", None)
    c = section_course(s, ROB)
    eff = dm.effective(c["config"])
    hyp = copy.deepcopy(gradebook.expand_components(c["config"]["components"]))
    next(e for e in hyp if e["name"] == "중간고사")["score"] = 50.0
    ref = g.estimate(hyp, eff["dist"], class_size=c["config"]["class_size"], cutoffs=eff["cutoffs"])
    rows = rows_of(w)
    assert rows["가정하면"] == f"{ref['grade']} · 상위 {whatif._n(ref['top'])}%"
    assert rows["지금"] == f"{c['estimate']['grade']} · 상위 {whatif._n(c['estimate']['top'])}%"
    assert rows["가정한 점수"] == "중간고사 50/100점 (비중 35%)"
    assert any("72점 대신 50점" in b for b in w["basis"])
    assert "내려가요" in r["answer"]                            # 72 → 50: 위치가 내려간다
    assert snapshot(s) == before
    # 목표 학점이 있으면 닿는지도 말한다
    w = check_shape(whatif.handle(s, "로봇공학개론 기말 95점이면 A 가능해?"))
    assert w["kind"] == "scenario" and w["target"] == "A0" and any(x.startswith("목표 A0:") for x in w["lines"])


def test_scenario_absolute(tmp_path):
    s = make_store(tmp_path)
    w = check_shape(whatif.handle(s, "회로이론 기말 80점이면?"))
    c = section_course(s, EE)
    eff = dm.effective(c["config"])
    hyp = copy.deepcopy(gradebook.expand_components(c["config"]["components"]))
    next(e for e in hyp if e["name"] == "기말고사")["score"] = 80.0
    ref = g.estimate(hyp, eff["dist"], cutoffs=eff["cutoffs"])
    assert ref["basis"] == "absolute"
    assert rows_of(w)["가정하면"] == f"{ref['grade']} · 예상 총점 {whatif._n(ref['expected_total'])}점"


def test_scenario_input_problems_are_asked_back(tmp_path):
    s = make_store(tmp_path)
    r = whatif.handle(s, "로봇공학개론 과제 30점이면?")         # 과제 만점 20
    assert r["whatif"]["kind"] == "current" and "만점이 20점" in r["answer"]
    r = whatif.handle(s, "로봇공학개론 출석 0점이면?")          # 모두 비슷하게 받는 항목(상대평가)
    assert r["whatif"]["kind"] == "current" and "모두 비슷하게 받는 항목" in r["answer"]


# ---------------------------------------------------------------- 데모(lms_agent demo와 같은 길)

@pytest.fixture()
def demo(tmp_path):
    """데모 + 학번: ASIC Homework assignments에 Lab0·Lab1 점수, 9개 중 2개(계약 14장)."""
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        s.data["settings"]["student_id"] = DEMO_STUDENT_ID
        core.sync(Canvas(base, DEMO_TOKEN), s, **core.sync_options(s, demo_home=tmp_path))
    finally:
        mock.shutdown()
    return s


class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


def test_demo_lab_one_zero_matches_screen_engine(demo, tmp_path):
    before = snapshot(demo)
    r = whatif.handle(demo, "ASIC LAB 하나 0점이면?")
    assert snapshot(demo) == before                            # 가정 점수는 복사본에만(화면 상태를 만들기 전에 확인)
    w = check_shape(r)
    assert (w["kind"], w["course_id"], w["component"]) == ("scenario", "101", "Homework assignments")
    # 화면(App.state)과 같은 성적 묶음에서 같은 가정을 직접 만들어 엔진에 넣는다
    c = next(x for x in App(demo, tmp_path, cfg={}, scheduler=_Sched()).state()["grades"]["courses"]
             if str(x["course_id"]) == "101")
    exp = gradebook.expand_components(c["config"]["components"])
    rem = next(e for e in exp if e.get("count_left"))           # Homework assignments 남은 n개
    n = rem["count_left"]
    known = [e for e in exp if e["score"] is not None and e.get("mean") is not None and e.get("sd")]
    wsum = sum(e["weight"] for e in known)
    mk = sum(e["weight"] * e["mean"] / e["max_score"] for e in known) / wsum
    sk = sum(e["weight"] * e["sd"] / e["max_score"] for e in known) / wsum
    mx = known[0]["max_score"]
    one = {"name": "가정", "weight": rem["weight"] / n, "score": 0.0, "max_score": mx, "mean": mk * mx, "sd": sk * mx,
           "fixed": False}
    hyp = [e for e in exp if e is not rem] + [one, dict(rem, weight=rem["weight"] * (n - 1) / n)]
    eff = dm.effective(c["config"])
    ref = g.estimate(hyp, eff["dist"], class_size=c["config"].get("class_size"), cutoffs=eff["cutoffs"])
    rows = rows_of(w)
    assert rows["지금"] == f"{c['estimate']['grade']} · 상위 {whatif._n(c['estimate']['top'])}%"
    assert rows["가정하면"] == f"{ref['grade']} · 상위 {whatif._n(ref['top'])}%"
    assert rows["가정한 점수"].startswith(f"Homework assignments 1개 0/{whatif._n(mx)}점")
    assert any("비중 가중 평균" in b for b in w["basis"])     # 평균·표준편차를 가정한 것을 적는다


def test_demo_current_uses_screen_needed(demo, tmp_path):
    r = whatif.handle(demo, "ASIC 지금 학점 몇이야?")
    w = check_shape(r)
    assert (w["kind"], w["course_id"]) == ("current", "101")
    c = next(x for x in App(demo, tmp_path, cfg={}, scheduler=_Sched()).state()["grades"]["courses"]
             if str(x["course_id"]) == "101")
    assert w["rows"][0] == {"label": "예상 학점",
                            "value": f"{c['estimate']['grade']} · 상위 {whatif._n(c['estimate']['top'])}%"}
    assert c["estimate"]["grade"] in r["answer"]
    shown = [x["label"] for x in w["rows"][1:]]
    eff = dm.effective(c["config"])
    t = gradebook._better(c["estimate"]["grade"], eff["dist"], eff["cutoffs"]) or c["estimate"]["grade"]
    assert len(shown) == len([n for n in c["needed"] if n["target"] == t]) and all(x.endswith(f"→ {t}") for x in shown)


def test_demo_without_scores_says_why(tmp_path):
    """학번 없이: ASIC은 비중만 있고 점수가 없어 계산할 수 없다 → kind current + 이유. 성적 있는 과목이 ASIC뿐이라 고른다."""
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), s)
    finally:
        mock.shutdown()
    r = whatif.handle(s, "기말에서 몇 점 받아야 A?")
    w = check_shape(r)
    assert (w["kind"], w["course_id"]) == ("current", "101")
    assert "계산할 수 없어요" in r["answer"]


# ---------------------------------------------------------------- P/NP·과목 고르기

def test_pass_fail_reports_scores_and_remaining_only(tmp_path):
    s = make_store(tmp_path)
    r = whatif.handle(s, "테니스 P 기준 넘었어?")
    w = check_shape(r)
    assert (w["kind"], w["course_id"], w["target"]) == ("pass_fail", PE, None)
    assert rows_of(w) == {"출석 (30%)": "28/30점", "실기 시험 (40%)": "80/100점", "남은 비중": "30%"}
    assert any("100점 중 60점" in x for x in w["lines"])      # 28/30×30 + 80/100×40
    text = r["answer"] + " ".join(w["lines"])
    assert "P/NP" in text and not re.search(r"(?<![A-Za-z])[A-D][+0](?![A-Za-z0-9])|상위 \d", text)   # 학점·위치 추정 없음
    # 학점을 물어도 P/NP 과목이면 같은 정리
    assert whatif.handle(s, "테니스 기말 실기 몇 점 받아야 A?")["whatif"]["kind"] == "pass_fail"
    # P 질문인데 과목을 말하지 않으면 P/NP 과목 하나를 고른다
    assert whatif.handle(s, "P 기준 넘었어?")["whatif"]["course_id"] == PE


def test_ask_course_when_several_courses_have_grades(tmp_path):
    s = make_store(tmp_path)
    r = whatif.handle(s, "A 받으려면?")
    w = check_shape(r)
    # P/NP(테니스)·평가 항목 없는 과목(반도체공학)은 학점 질문의 후보에서 뺀다
    assert (w["kind"], w["course_id"], w["course_short"]) == ("ask_course", None, None)
    assert [x["label"] for x in w["rows"]] == ["로봇공학개론", "회로이론", "선형대수"]
    assert "로봇공학개론" in r["answer"] and "회로이론" in r["answer"]
    # 대화의 과목(course_id)이 있으면 그 과목
    assert whatif.handle(s, "A 받으려면?", course_id=EE)["whatif"]["course_id"] == EE
    # 질문에 나온 과목이 course_id보다 먼저
    assert whatif.handle(s, "로봇공학개론 A 받으려면?", course_id=EE)["whatif"]["course_id"] == ROB


def test_single_course_with_grades_is_used(tmp_path):
    s = make_store(tmp_path, (ROB, SEMI))
    w = check_shape(whatif.handle(s, "A 받으려면?"))
    assert (w["kind"], w["course_id"]) == ("needed", ROB)


def test_no_weights_or_no_scores_is_current_with_reason(tmp_path):
    s = make_store(tmp_path)
    r = whatif.handle(s, "반도체공학 A 받으려면?")
    assert (r["whatif"]["kind"], r["whatif"]["course_id"]) == ("current", SEMI) and "평가 항목" in r["answer"]
    r = whatif.handle(s, "선형대수 A 받으려면?")
    w = check_shape(r)
    assert (w["kind"], w["course_id"]) == ("current", LIN) and "계산할 수 없어요" in r["answer"]
    assert {x["label"] for x in w["rows"]} == {"중간고사", "기말고사"}   # 남은 항목·비중은 보여 준다
    s2 = make_store(tmp_path / "empty", (SEMI,))
    r = whatif.handle(s2, "A 받으려면?")
    assert (r["whatif"]["kind"], r["whatif"]["course_id"]) == ("current", None)
