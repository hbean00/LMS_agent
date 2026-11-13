"""평가 비중 찾기(계약 10장 C): 한·영 강의계획서 문장, 표, 여러 줄 목록, 비중이 아닌 %는 버리기, 합·확신도, 출석 fixed."""
import pytest

from lms_agent.weights import find_weights, is_fixed, normalize_name


def comps(w):
    return {c["name"]: c["weight"] for c in w.components}


@pytest.mark.parametrize("text,is_html,want", [
    ("중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("<table><tr><th>평가항목</th><th>비율</th></tr><tr><td>중간고사</td><td>30</td></tr>"
     "<tr><td>기말고사</td><td>40</td></tr><tr><td>과제</td><td>20</td></tr><tr><td>출석</td><td>10</td></tr>"
     "<tr><td>합계</td><td>100</td></tr></table>", True,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("<table><tr><td>중간고사</td><td>30%</td></tr><tr><td>기말고사</td><td>40%</td></tr>"
     "<tr><td>과제</td><td>30%</td></tr></table>", True, {"중간고사": 30, "기말고사": 40, "과제": 30}),
    ("Midterm 30% / Final 40% / Homework 20% / Attendance 10%", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("평가방법: 출석(10) 과제(20) 중간(30) 기말(40)", False, {"출석": 10, "과제": 20, "중간고사": 30, "기말고사": 40}),
    ("평가 방법\n- 중간고사: 30%\n- 기말고사: 40%\n- 과제: 20%\n- 출석: 10%", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("Lab reports 40%, Midterm project 30%, Final exam 30%", False,
     {"Lab reports": 40, "Midterm project": 30, "기말고사": 30}),
    ("성적은 중간고사는 30%, 기말고사는 40%를 반영하고 과제 20%, 출석 10%입니다.", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("Grading: Midterm exam 30%, final exam 40%, homework 20%, attendance 10%.", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("Exam 1 20%, Exam 2 20%, Final Exam 30%, Homework 20%, Participation 10%", False,
     {"Exam 1": 20, "Exam 2": 20, "기말고사": 30, "과제": 20, "수업참여": 10}),
    ("중간고사(Midterm) 30%, 기말고사(Final) 40%, 과제(Homework) 20%, 출석(Attendance) 10%", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("Final exam is worth 40% of the grade, the midterm 30%, homework 20% and attendance 10%.", False,
     {"기말고사": 40, "중간고사": 30, "과제": 20, "출석": 10}),
    # PDF에서 뽑은 글: 칸마다 줄이 끊김 / 이름 줄 + 숫자 줄
    ("평가방법\n출석\n10%\n중간고사\n30%\n기말고사\n30%\n과제\n30%", False,
     {"출석": 10, "중간고사": 30, "기말고사": 30, "과제": 30}),
    ("성적 평가\n구분 출석 중간고사 기말고사 과제\n비율(%) 10 30 40 20", False,
     {"출석": 10, "중간고사": 30, "기말고사": 40, "과제": 20}),
    # 괄호 안 세부 비중은 빼고 큰 항목만
    ("중간고사 30%(객관식 20%, 서술형 10%), 기말고사 40%, 과제 20%, 출석 10%", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
])
def test_finds_full_block_high(text, is_html, want):
    w = find_weights(text, is_html)
    assert w is not None, text
    assert comps(w) == {k: float(v) for k, v in want.items()}
    assert w.confidence == "high" and w.total == 100
    assert w.sentence


def test_points_allocation_converted_when_not_100():
    w = find_weights("배점: 출석 5점, 과제 15점, 중간 40점, 기말 40점 (총 100점)")
    assert w.confidence == "high" and comps(w) == {"출석": 5, "과제": 15, "중간고사": 40, "기말고사": 40}
    w = find_weights("배점: 중간고사 100점, 기말고사 100점, 과제 50점, 출석 50점")
    assert w.confidence == "medium" and comps(w) == {"중간고사": 33.33, "기말고사": 33.33, "과제": 16.67, "출석": 16.67}
    assert any("점수 배분" in n for n in w.notes)


@pytest.mark.parametrize("text,is_html", [
    ("출석률 80% 이상이어야 합니다. 상위 30%는 A입니다.", False),
    ("상대평가로 A학점은 상위 30% 이내, B학점은 상위 70% 이내입니다.", False),
    ("A+ 15%, A0 15%, B+ 15%, B0 15%, C+ 15%, C0 20%, D+ 0%, F 5%", False),
    ("<table><tr><th>학점</th><th>비율</th></tr><tr><td>A+</td><td>15%</td></tr><tr><td>A0</td><td>15%</td></tr>"
     "<tr><td>B+</td><td>20%</td></tr><tr><td>B0</td><td>20%</td></tr><tr><td>C+</td><td>30%</td></tr></table>", True),
    ("지각 시 10% 감점, 늦은 제출은 하루에 20% 감점합니다.", False),
    ("Late submissions lose 10% per day; at least 80% attendance is required.", False),
    ("Homework 5는 Canvas 과제함으로 제출하세요. 마감은 11/20 18:00입니다.", False),
    ("중간고사 78점, 평균 65.3점, 표준편차 12.1", False),       # 점수 안내(비중 아님)
    ("기말고사 12월 15일 15:00, 중간고사 10/23", False),         # 날짜
    ("LAB4 보고서 마감: 11월 18일(수) 23:59", False),
])
def test_not_weights(text, is_html):
    assert find_weights(text, is_html) is None


def test_partial_sum_medium_with_note():
    w = find_weights("평가: 중간고사 30%, 기말고사 30%")
    assert w.confidence == "medium" and w.total == 60
    assert any("60%" in n for n in w.notes)


def test_attendance_fixed_and_threshold_ignored_inside_block():
    w = find_weights("중간 30%, 기말 40%, 과제 20%, 출석 10% (출석률 80% 미만은 F)")
    assert comps(w) == {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10} and w.confidence == "high"
    fixed = {c["name"]: c["fixed"] for c in w.components}
    assert fixed == {"중간고사": False, "기말고사": False, "과제": False, "출석": True}
    assert is_fixed("Class participation") and is_fixed("출결") and not is_fixed("Midterm project")


def test_prefers_block_summing_to_100_over_partial_sentence():
    text = ("수업 소개: 이번 학기에는 퀴즈 10%가 새로 생겼습니다.\n"
            "성적 평가: 중간고사 30%, 기말고사 30%, 과제 20%, 퀴즈 10%, 출석 10%")
    w = find_weights(text)
    assert w.confidence == "high" and len(w.components) == 5


def test_duplicate_bilingual_listing_merged():
    w = find_weights("평가: 중간고사 30%, 기말고사 40%, 과제 30% / Midterm 30%, Final 40%, Homework 30%")
    assert comps(w) == {"중간고사": 30, "기말고사": 40, "과제": 30} and w.total == 100
    assert any("하나로" in n for n in w.notes)


def test_updated_grading_policy_announcement():
    """실제 고려대 ASIC 공지 모양: 앞 문장의 '(5%) … (5%)'가 아니라 6개 목록 전체, 이름은 서로 다르게 그대로."""
    html = ("<p>Dear All,</p><p>I have attached in the updated grading policy, which includes the midterm quiz (5%) "
            "and the final quiz (5%). For each midterm week and final week, we will have a short quiz session that "
            "covers the lecture materials.</p><ul><li>Homework assignments: 30% (w/ short reports)</li>"
            "<li>Midterm project: 25% (w/ midterm report)</li><li>Midterm quiz: 5%</li>"
            "<li>Final project: 25% (w/ final report)</li><li>Final quiz: 5%</li><li>Attendance: 10%</li></ul>"
            "<p>Best Regards, Prof. Example</p>")
    w = find_weights(html, is_html=True)
    assert w.confidence == "high" and w.total == 100
    assert comps(w) == {"Homework assignments": 30, "Midterm project": 25, "Midterm quiz": 5,
                        "Final project": 25, "Final quiz": 5, "Attendance": 10}
    assert {c["name"] for c in w.components if c["fixed"]} == {"Attendance"}
    assert "w/" not in "".join(comps(w))
    assert w.sentence.startswith("Homework assignments: 30%")
    # 같은 글을 PDF에서 뽑은 모양(글머리표 줄)도 같은 결과
    pdf = ("Update Grading Policy\n• Homework assignments: 30% (w/ short reports)\n• Midterm project: 25% (w/ midterm report)\n"
           "• Midterm quiz: 5%\n• Final project: 25% (w/ final report)\n• Final quiz: 5%\n• Attendance: 10%\n")
    assert comps(find_weights(pdf)) == comps(w)


def test_normalize_only_exact_aliases():
    assert normalize_name("Midterm Exam") == "중간고사" and normalize_name("기말 시험") == "기말고사"
    assert normalize_name("HW") == "과제" and normalize_name("Assignments") == "과제"
    assert normalize_name("Lab 보고서") == "Lab 보고서" and normalize_name("Midterm project") == "Midterm project"


def test_long_text_is_fast():
    import time
    text = ("이번 학기에는 LAB 1 보고서와 LAB 2 보고서를 11월 20일 23:59까지 제출합니다. " * 500
            + "\n평가: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%")
    t = time.perf_counter()
    w = find_weights(text)
    assert w.confidence == "high" and time.perf_counter() - t < 2


# ---------------------------------------------------------------- 평가 비중 자동 채우기(규칙 경로)

from lms_agent import gradebook  # noqa: E402
from lms_agent.store import Store  # noqa: E402

SYLLABUS = "<p>평가 방법: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%</p>"
UPDATED = ("<p>Dear All, I have attached in the updated grading policy, which includes the midterm quiz (5%) and the "
           "final quiz (5%).</p><ul><li>Homework assignments: 30% (w/ short reports)</li><li>Midterm project: 25% "
           "(w/ midterm report)</li><li>Midterm quiz: 5%</li><li>Final project: 25% (w/ final report)</li>"
           "<li>Final quiz: 5%</li><li>Attendance: 10%</li></ul>")
UPDATED_PDF = ("Update Grading Policy\n• Homework assignments: 30% (w/ short reports)\n• Midterm project: 25%\n"
               "• Midterm quiz: 5%\n• Final project: 25%\n• Final quiz: 5%\n• Attendance: 10%")


def S(text, typ="syllabus", title="강의계획서", posted="2026-09-01T00:00:00+09:00", cid="101", html=True):
    return {"course_id": cid, "course_name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반",
            "type": typ, "id": f"{typ}-{cid}", "title": title, "text": text, "is_html": html, "posted_at": posted}


@pytest.fixture()
def gstore(tmp_path):
    return Store(tmp_path / "state.json")


def names(store, cid="101"):
    return {c["name"]: c["weight"] for c in store.data["grades"][cid]["components"]}


def test_auto_fill_fills_empty_course(gstore):
    log = gradebook.auto_fill(gstore, [S(SYLLABUS)], ai_enabled=False)
    cfg = gstore.data["grades"]["101"]
    assert names(gstore) == {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}
    assert all(c["auto"] for c in cfg["components"]) and cfg["auto_by"] == "rules"
    assert cfg["auto_source"] == "강의계획서: 평가 방법: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%"
    assert cfg["auto_previous"] is None and "ASIC설계(영강)" in log[0] and "4개" in log[0]
    sec = gradebook.section(gstore, [{"id": "101", "short_name": "ASIC설계(영강)"}])["courses"][0]
    assert sec["auto"] and sec["auto_source"].startswith("강의계획서") and sec["auto_changed"] is None
    assert sec["estimate"] is None or sec["error"] is None
    # 다시 돌려도 바뀌는 것 없음
    assert gradebook.auto_fill(gstore, [S(SYLLABUS)], ai_enabled=False) == []


def test_auto_fill_never_overwrites_user_edited_course(gstore):
    gradebook.save_course(gstore, "101", {"components": [{"name": "중간", "weight": 50}, {"name": "기말", "weight": 50}]})
    assert gradebook.auto_fill(gstore, [S(SYLLABUS), S(UPDATED, "announcement", "Updated Grading Policy",
                                                       "2026-11-01T10:00:00+09:00")], ai_enabled=True) == []
    assert names(gstore) == {"중간": 50, "기말": 50}
    sec = gradebook.section(gstore, [{"id": "101", "short_name": "ASIC"}])["courses"][0]
    assert not sec["auto"] and sec["auto_source"] is None


def test_user_save_drops_auto_flag(gstore):
    gradebook.auto_fill(gstore, [S(SYLLABUS)], ai_enabled=False)
    comps = gstore.data["grades"]["101"]["components"]
    gradebook.save_course(gstore, "101", {"components": comps})   # 화면이 그대로 돌려보내도
    cfg = gstore.data["grades"]["101"]
    assert not any(c.get("auto") for c in cfg["components"]) and "auto_source" not in cfg
    gradebook.auto_fill(gstore, [S(UPDATED, "announcement", "Updated", "2026-11-01T10:00:00+09:00")], ai_enabled=False)
    assert names(gstore) == {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}


def test_newer_updated_policy_supersedes_syllabus_and_keeps_scores(gstore):
    gradebook.auto_fill(gstore, [S(SYLLABUS.replace("중간고사 30%", "Midterm project 30%")
                                   .replace("기말고사 40%", "Final project 40%").replace("과제 20%", "Homework assignments 20%")
                                   .replace("출석 10%", "Attendance 10%"))], ai_enabled=False)
    comp = next(c for c in gstore.data["grades"]["101"]["components"] if c["name"] == "Midterm project")
    comp.update(score=78, mean=65.3, sd=12.1)                     # 점수 후보를 넣어 둔 상태(이름이 같으면 유지)
    gstore.data["grades"]["101"]["class_size"] = 40
    srcs = [S(SYLLABUS.replace("중간고사 30%", "Midterm project 30%").replace("기말고사 40%", "Final project 40%")
              .replace("과제 20%", "Homework assignments 20%").replace("출석 10%", "Attendance 10%")),
            S(UPDATED, "announcement", "Updated Grading Policy", "2026-11-01T10:00:00+09:00"),
            S(UPDATED_PDF, "pdf", "첨부 'Update Grading Policy.pdf'", "2026-11-01T10:00:00+09:00", html=False)]
    log = gradebook.auto_fill(gstore, srcs, ai_enabled=False)
    cfg = gstore.data["grades"]["101"]
    assert names(gstore) == {"Homework assignments": 30, "Midterm project": 25, "Midterm quiz": 5,
                             "Final project": 25, "Final quiz": 5, "Attendance": 10}
    assert cfg["auto_source"].startswith("Updated Grading Policy: ") and cfg["auto_source_type"] == "announcement"
    mp = next(c for c in cfg["components"] if c["name"] == "Midterm project")
    assert (mp["score"], mp["mean"], mp["sd"]) == (78, 65.3, 12.1) and cfg["class_size"] == 40
    assert {(p["name"], p["weight"]) for p in cfg["auto_previous"]} >= {("Midterm project", 30.0)}
    assert len(log) == 1 and "바꿨습니다" in log[0] and "Midterm quiz 5% 추가" in log[0]
    ch = gradebook.section(gstore, [{"id": "101", "short_name": "ASIC"}])["courses"][0]["auto_changed"]
    assert {a["name"] for a in ch["added"]} == {"Midterm quiz", "Final quiz"}
    assert {(c["name"], c["from"], c["to"]) for c in ch["changed"]} == {
        ("Homework assignments", 20, 30), ("Midterm project", 30, 25), ("Final project", 40, 25)}
    assert ch["removed"] == [] and ch["at"] and ch["source"].startswith("Updated")
    # 공지와 그 첨부 PDF가 같은 내용이면 다음 동기화에서 흔들리지 않는다
    assert gradebook.auto_fill(gstore, srcs, ai_enabled=False) == []


def test_assignment_rubric_does_not_replace_course_weights(gstore):
    gradebook.auto_fill(gstore, [S(SYLLABUS)], ai_enabled=False)
    rubric = S("<p>Lab3 채점 기준: Prelab 30%, Report 70%</p>", "board", "과제: Lab3", "2026-11-05T09:00:00+09:00")
    assert gradebook.auto_fill(gstore, [S(SYLLABUS), rubric], ai_enabled=False) == []
    assert names(gstore) == {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}


def test_medium_partial_applied_and_low_skipped(gstore):
    gradebook.auto_fill(gstore, [S("<p>평가: 중간고사 30%, 기말고사 30%</p>", cid="201")], ai_enabled=False)
    assert gstore.data["grades"]["201"]["auto_confidence"] == "medium"
    log = gradebook.auto_fill(gstore, [S("<p>평가: 중간고사 10%, 기말고사 20%</p>", cid="202")], ai_enabled=False)
    assert "202" not in gstore.data["grades"] and any("확신이 낮아" in x for x in log)


# ================================================================ 일반화(docs/CORNER_CASES.md 가족 7)
# 'Homework assignments: 30% (w/ short reports)'·'Assessment: Final exam 50% / Report 20% / …'와 같은 모양의 다른 글.

@pytest.mark.parametrize("text,is_html,want", [
    # 점수(배점)로 적은 목록
    ("성적 평가: 과제 30점, 중간고사 30점, 기말고사 40점", False, {"과제": 30, "중간고사": 30, "기말고사": 40}),
    ("평가 비율: 중간고사 (30) 기말고사 (40) 과제 (20) 출석 (10)", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    # 줄마다 흩어진 목록(빈 줄·글머리표)
    ("Grading\n\n- Midterm: 30%\n\n- Final: 40%\n\n- Homework: 20%\n\n- Attendance: 10%", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    # 퍼센트와 점이 섞임(합 100)
    ("Grading: Midterm 30%, Final 40 points, Homework 30", False, {"중간고사": 30, "기말고사": 40, "과제": 30}),
    # 괄호 안 조건(결석 3회 이상 F)은 비중이 아니다
    ("평가: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10% (결석 3회 이상 F)", False,
     {"중간고사": 30, "기말고사": 40, "과제": 20, "출석": 10}),
    ("Grading: Midterm 30%, Final 40%, Labs 20%, Attendance 10% (3+ absences = F)", False,
     {"중간고사": 30, "기말고사": 40, "실습": 20, "출석": 10}),
    # 표(HTML) — 머리글이 영어, 셀에 % 없음
    ("<table><tr><th>Component</th><th>Weight (%)</th></tr><tr><td>Midterm Exam</td><td>35</td></tr>"
     "<tr><td>Final Exam</td><td>35</td></tr><tr><td>Lab Reports</td><td>30</td></tr></table>", True,
     {"중간고사": 35, "기말고사": 35, "Lab Reports": 30}),
    # '|'로 나눈 글 표
    ("평가 항목 | 비율\n중간고사 | 30%\n기말고사 | 30%\n프로젝트 | 40%", False, {"중간고사": 30, "기말고사": 30, "프로젝트": 40}),
    # 이름 줄 + 숫자 줄(쉼표)
    ("Grading breakdown\nMidterm, Final, Homework, Quizzes\n30%, 30%, 25%, 15%", False,
     {"중간고사": 30, "기말고사": 30, "과제": 25, "퀴즈": 15}),
    # 각 항목의 세부 배분은 큰 항목에 넣는다
    ("Grading: Quizzes 20% (4 × 5%), Midterm 30%, Final 50%", False, {"퀴즈": 20, "중간고사": 30, "기말고사": 50}),
])
def test_weight_variants(text, is_html, want):
    w = find_weights(text, is_html)
    assert w is not None, text
    assert comps(w) == {k: float(v) for k, v in want.items()}, (text, comps(w))
    assert w.total == 100


@pytest.mark.parametrize("text", [
    "30% of instructions are loads or stores; miss rate = 2%; hit rate 98%.",      # 강의 자료의 %
    "Late penalty: 10% per day, up to 50%.",
    "출석 80% 이상 참여해야 하며, 결석 3회 이상이면 F입니다.",
    "Lab3 results: 85% passed the testbench, 15% failed.",
    "A+, A: 40~50% / B+, B: 40~60% / C+~F: 0~10%",                                  # 실제 강의계획서의 학점 가이드
    "Your score: Midterm 78, Final 85, Homework 92",                               # 점수 알림
])
def test_weight_variants_not_weights(text):
    assert find_weights(text) is None, text


@pytest.mark.parametrize("text,want", [
    # 항목 번호가 비중으로 읽히면 안 된다('Exam 1 (20%)'의 1)
    ("Exam 1 (20%), Exam 2 (20%), Final (30%), HW (30%)", {"Exam 1": 20, "Exam 2": 20, "기말고사": 30, "과제": 30}),
    # 괄호 안 개수('Projects (2)')는 비중이 아니다
    ("Participation: 5% / Quizzes: 15% / Projects (2): 40% / Final: 40%",
     {"수업참여": 5, "퀴즈": 15, "프로젝트": 40, "기말고사": 40}),
    # 'A and B each 30%' — 둘 다 30%
    ("The midterm and the final each count for 30%, homework 40%.", {"중간고사": 30, "기말고사": 30, "과제": 40}),
    ("성적: 중간고사와 기말고사는 각각 30%, 과제 40%", {"중간고사": 30, "기말고사": 30, "과제": 40}),
])
def test_item_numbers_counts_and_each(text, want):
    w = find_weights(text)
    assert w is not None and comps(w) == {k: float(v) for k, v in want.items()}, (text, w and comps(w))
    assert w.total == 100
