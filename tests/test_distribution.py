"""학점 비율·점수 기준 자동 찾기(계약 16장): 특정 문장이 아니라 일반 구조(묶음·범위·누적·이하/이내·꼬리·점수 기준표)를
한·영, 한 줄·한 줄에 하나(PDF)·표(HTML·가로·세로)로 시험한다. 비중·출석 기준·통계·점수 공지·글 속 퍼센트는 버린다.
그리고 gradebook(가장 최근 글·사용자 비율 우선·letter_basis "syllabus")·grades(절대평가)·AI 대체 경로."""
import json
import subprocess

import pytest

from lms_agent import agent_cli, celebrate, core, distribution as dm, gradebook, grades as g
from lms_agent.distribution import find_distribution
from lms_agent.store import Store

GR = g.GRADES


def band(d, *names):
    return round(sum(d[x] for x in names), 2)


# ---------------------------------------------------------------- 실제 예와 그 변형들(상대평가, 9학점을 다 덮음 → high)

ROBOT = ("Grade (Guideline)\nA+,A: 40~50%\nB+,B: 40~60%\nC+~F: 0~10%\n"
         "F: No show at Final exam / Attendance under 66% / Zero score from the total assessment /")


def test_real_robotics_syllabus_shape():
    r = find_distribution(ROBOT)
    assert r.kind == "relative" and r.confidence == "high"
    assert (band(r.dist, "A+", "A0"), band(r.dist, "B+", "B0"), band(r.dist, "C+", "C0", "D+", "D0", "F")) == (45, 50, 5)
    assert r.dist["A+"] == r.dist["A0"] == 22.5 and r.dist["F"] == 1
    assert r.ranges == {"A+·A0": [40, 50], "B+·B0": [40, 60], "C+~F": [0, 10]}
    assert r.sentence == "Grade (Guideline) / A+,A: 40~50% / B+,B: 40~60% / C+~F: 0~10%"
    assert any("가운데 값" in n for n in r.notes) and any("고르게" in n for n in r.notes)
    assert sum(r.dist.values()) == 100 and r.cutoffs is None
    assert [x["label"] for x in r.groups] == ["A+·A0", "B+·B0", "C+~F"]


# (글, html, {묶음: 기대 %}) — 묶음은 학점 이름을 '·'로 이은 것
HIGH = [
    # 묶음 표기: 쉼표·빗금·가운뎃점·물결·'및'·괄호·'학점'·'등급'·"A's"
    ("A+,A: 40~50%\nB+,B: 40~60%\nC+~F: 0~10%", False, {"A+·A0": 45, "B+·B0": 50, "C+·C0·D+·D0·F": 5}),
    ("학점 비율: A+/A0 40~50%, B+/B0 40~60%, C+~F 0~10%", False, {"A+·A0": 45, "B+·B0": 50}),
    ("Grade: A+·A0 35%, B+·B0 45%, C+ ~ F 20%", False, {"A+·A0": 35, "B+·B0": 45, "C+·C0·D+·D0·F": 20}),
    ("성적 분포: A+~A0 30%, B+~B0 40%, C+~F 30%", False, {"A+·A0": 30, "B+·B0": 40}),
    ("A+ 및 A0 학점 30%, B+ 및 B0 학점 40%, C+ 이하 30%", False, {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    ("A (A+, A0): 30%, B (B+, B0): 40%, C 이하: 30%", False, {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    ("A학점 35%, B학점 40%, C학점 이하 25%", False, {"A+·A0": 35, "B+·B0": 40, "C+·C0·D+·D0·F": 25}),
    ("A등급 30%, B등급 40%, C등급 이하 30%", False, {"A+·A0": 30, "B+·B0": 40}),
    ("Grade distribution: A's 30 to 40%, B's 40 to 50%, the rest 20%", False, {"A+·A0": 35, "B+·B0": 45,
                                                                                "C+·C0·D+·D0·F": 20}),
    # 값 표기: 범위 하이픈·%가 두 번·'퍼센트'·각각
    ("Letter grade guideline: A+/A0 30-40%, B+/B0 40-50%, C+ and below 10-30%", False,
     {"A+·A0": 35, "B+·B0": 45, "C+·C0·D+·D0·F": 20}),
    ("학점: A+, A0 30%~40%, B+, B0 40%~50%, C+ 이하 10%~30%", False, {"A+·A0": 35, "B+·B0": 45}),
    ("학점 비율 A 30퍼센트, B 40퍼센트, C 이하 30퍼센트", False, {"A+·A0": 30, "B+·B0": 40}),
    ("A+, A0 각각 15%, B+, B0 각각 20%, C+ ~ F 30%", False, {"A+": 15, "A0": 15, "B+": 20, "B0": 20}),
    # 9학점 한 줄
    ("학점 비율: A+ 15%, A0 15%, B+ 20%, B0 20%, C+ 10%, C0 10%, D+ 0%, D0 0%, F 10%", False,
     {"A+": 15, "A0": 15, "B+": 20, "B0": 20, "C+": 10, "C0": 10, "D+": 0, "D0": 0, "F": 10}),
    # 상한 + 나머지(다 덮음)
    ("A+/A0: 30% 이내, B+/B0: 40% 이내, C+ 이하: 나머지", False, {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    ("Grading curve: A up to 30%, B no more than 40%, C or below: remaining", False,
     {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    ("상대평가: A 최대 30%, B 최대 40%, 나머지는 C+ 이하", False, {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    ("Grade distribution: A ≤ 30%, B ≤ 40%, C or below: the rest", False, {"A+·A0": 30, "B+·B0": 40}),
    # '상위' 위치(맨 위부터 누적)
    ("Grades will be curved: A (top 30%), B (next 40%), C or below (remaining 30%)", False,
     {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    ("학점 기준: A 상위 30% 이내, B 상위 31~70%, C 이하 나머지", False, {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    # 누적 + 꼬리로 다 덮음
    ("학점: A 30%, B 이상 누적 70%, C 이하 30%", False, {"A+·A0": 30, "B+·B0": 40, "C+·C0·D+·D0·F": 30}),
    # PDF: 칸마다 한 줄 / 가로 표 / 이름 줄 + 값 줄
    ("Grade\nA+\n15%\nA0\n15%\nB+\n20%\nB0\n20%\nC+~F\n30%", False, {"A+": 15, "A0": 15, "B+": 20, "B0": 20}),
    ("성적 분포\n등급 A+ A0 B+ B0 C+ C0 D+ D0 F\n비율(%) 10 20 20 20 10 10 0 0 10", False,
     {"A+": 10, "A0": 20, "F": 10, "D+": 0}),
    ("A+,A:\n40~50%\nB+,B:\n40~60%\nC+~F:\n0~10%", False, {"A+·A0": 45, "B+·B0": 50}),
    # HTML 표: 세로(머리글 %) / 가로 / 칸 안 범위
    ("<p>학점 비율</p><table><tr><th>학점</th><th>비율(%)</th></tr><tr><td>A+</td><td>15</td></tr>"
     "<tr><td>A0</td><td>15</td></tr><tr><td>B+</td><td>20</td></tr><tr><td>B0</td><td>20</td></tr>"
     "<tr><td>C+ ~ F</td><td>30</td></tr></table>", True, {"A+": 15, "A0": 15, "B+": 20, "B0": 20}),
    ("<table><tr><td>학점</td><td>A+</td><td>A0</td><td>B+</td><td>B0</td><td>C+</td><td>C0</td><td>D+</td>"
     "<td>D0</td><td>F</td></tr><tr><td>비율</td><td>10</td><td>20</td><td>20</td><td>20</td><td>10</td><td>10</td>"
     "<td>0</td><td>0</td><td>10</td></tr></table>", True, {"A+": 10, "A0": 20, "C0": 10, "F": 10}),
    ("<h3>Grade guideline</h3><table><tr><th>Grade</th><th>Share</th></tr><tr><td>A+, A0</td><td>35~45%</td></tr>"
     "<tr><td>B+, B0</td><td>40~50%</td></tr><tr><td>C+ ~ F</td><td>5~25%</td></tr></table>", True,
     {"A+·A0": 40, "B+·B0": 45, "C+·C0·D+·D0·F": 15}),
    ("<ul><li>A+, A0: 35~45%</li><li>B+, B0: 40~50%</li><li>C+ ~ F: 5~25%</li></ul>", True,
     {"A+·A0": 40, "B+·B0": 45}),
    # 전각 문자 / 붙여 쓰기('A+15%')
    ("학점 비율 Ａ＋,Ａ０：４０～５０％ / Ｂ＋,Ｂ０：４０～６０％ / Ｃ＋～Ｆ：０～１０％", False, {"A+·A0": 45, "B+·B0": 50}),
    ("Grade: A+15%, A0 15%, B+20%, B0 20%, C+~F 30%", False, {"A+": 15, "B+": 20, "C+·C0·D+·D0·F": 30}),
    # 뒤에 붙은 출석 낱말은 괜찮다
    ("Grade guideline\nA+, A0: 40~50% (attendance required)\nB+, B0: 40~60%\nC+ ~ F: 0~10%", False,
     {"A+·A0": 45, "B+·B0": 50}),
]


@pytest.mark.parametrize("text,is_html,want", HIGH)
def test_relative_variants_high(text, is_html, want):
    r = find_distribution(text, is_html)
    assert r is not None, text
    assert r.kind == "relative" and r.confidence == "high", (r.confidence, r.notes)
    for k, v in want.items():
        assert band(r.dist, *k.split("·")) == pytest.approx(v, abs=0.02), (k, r.dist)
    assert sum(r.dist.values()) == pytest.approx(100, abs=1e-6) and not g.validate_distribution(r.dist)
    assert r.sentence


# 일부만 적혀 나머지를 기본 비율 모양으로 채움·범위 안에서 맞춤·% 없음 → medium
MEDIUM = [
    ("성적은 상대평가로 A학점 30% 이내, B학점 이상 70% 이내에서 부여합니다.", False, {"A+·A0": 30, "B+·B0": 40}),
    ("Grades: A 30% or less, A+B ≤ 70%", False, {"A+·A0": 30, "B+·B0": 40}),
    ("Grade distribution: A: 35%, B or above: 75%", False, {"A+·A0": 35, "B+·B0": 40}),
    ("학점: A+ 10%, A0 20%, B+ 이상 누적 60%", False, {"A+": 10, "A0": 20, "B+": 30}),
    ("Letter grades: A 20~30%, B 30~40%, C or below 20~40%", False, {"A+·A0": 27.5, "B+·B0": 37.5}),   # 합 90 → 범위 안에서
    ("학점 비율: A+ 15, A0 15, B+ 20, B0 20, C+ 10, C0 10, D+ 0, D0 0, F 10", False, {"A+": 15, "F": 10}),   # % 없음
]


@pytest.mark.parametrize("text,is_html,want", MEDIUM)
def test_relative_variants_medium(text, is_html, want):
    r = find_distribution(text, is_html)
    assert r is not None and r.kind == "relative" and r.confidence == "medium", (text, r and r.confidence)
    for k, v in want.items():
        assert band(r.dist, *k.split("·")) == pytest.approx(v, abs=0.02), (k, r.dist)
    assert sum(r.dist.values()) == pytest.approx(100, abs=1e-6)
    assert r.notes


def test_cumulative_is_subtracted_and_unstated_tail_uses_default_shape():
    r = find_distribution("성적은 상대평가로 A학점 30% 이내, B학점 이상 70% 이내에서 부여합니다.")
    assert r.ranges == {"A+·A0": [0, 30], "A+~B0": [0, 70]}
    tail = {x: r.dist[x] for x in ("C+", "C0", "D+", "D0", "F")}   # 30%를 기본 비율(15·20·0·0·5) 모양으로
    assert tail == {"C+": 11.25, "C0": 15, "D+": 0, "D0": 0, "F": 3.75}
    assert any("누적" in n for n in r.notes) and any("기본 비율" in n for n in r.notes)
    assert any(x["cumulative"] for x in r.groups)


def test_sum_not_100_adjusted_inside_ranges_or_scaled_low():
    r = find_distribution("Letter grades: A 20~30%, B 30~40%, C or below 20~40%")
    assert any("범위 안에서" in n for n in r.notes)
    assert 20 <= band(r.dist, "A+", "A0") <= 30 and 30 <= band(r.dist, "B+", "B0") <= 40
    r = find_distribution("학점 비율: A 30%, B 40%, C 이하 40%")      # 범위가 없어 맞출 수 없음 → 비례로, low
    assert r.confidence == "low" and any("비례" in n for n in r.notes) and sum(r.dist.values()) == pytest.approx(100)


def test_minus_grades_and_bare_letter_meaning():
    r = find_distribution("Grade distribution: A+ 10%, A 20%, A- 10%, B+ 20%, B 20%, C+ ~ F 20%")
    assert r.dist["A+"] == 10 and r.dist["A0"] == 30 and any("마이너스" in n for n in r.notes)   # A·A-는 A0
    r = find_distribution("학점 비율: A 30%, B 40%, C 이하 30%")       # A+가 따로 없으면 A = A+·A0
    assert r.dist["A+"] == r.dist["A0"] == 15


def test_korean_prose_with_position_and_particles():
    r = find_distribution("성적은 상대평가로 매깁니다. 수강생의 상위 35% 정도까지 A를 주고, 그다음 40% 정도에 B를 주며, "
                          "나머지는 C 이하로 줍니다.")
    assert r.kind == "relative" and band(r.dist, "A+", "A0") == 35 and band(r.dist, "B+", "B0") == 40
    assert band(r.dist, "C+", "C0", "D+", "D0", "F") == 25
    assert find_distribution("학점은 상대평가입니다. 나머지는 C 이하입니다.") is None    # 숫자 있는 묶음이 없음


def test_groups_not_anchored_to_the_top_are_low():
    r = find_distribution("학점 비율: A0 20%, B0 30%")          # A+·B+가 빠져 어디부터인지 모름
    assert r.confidence == "low" and any("위치를 알 수 없는" in n for n in r.notes)


def test_single_group_is_low_and_needs_grade_context():
    r = find_distribution("A학점은 수강인원의 30% 이내로 부여합니다.")
    assert r.kind == "relative" and r.confidence == "low" and band(r.dist, "A+", "A0") == 30
    assert find_distribution("A 10% penalty applies to late submissions.") is None


# ---------------------------------------------------------------- 절대평가(점수 기준표)

ABS = [
    ("절대평가\nA+: 95점 이상\nA0: 90점 이상\nB+: 85점 이상\nB0: 80점 이상\nC+: 75점 이상\nC0: 70점 이상\n"
     "D+: 65점 이상\nD0: 60점 이상\nF: 60점 미만", False, "high",
     {"A+": 95, "A0": 90, "B+": 85, "B0": 80, "C+": 75, "C0": 70, "D+": 65, "D0": 60}),
    ("90 ≤ A0 < 95\n95 ≤ A+\n85 ≤ B+ < 90\n80 ≤ B0 < 85", False, "high", {"A+": 95, "A0": 90, "B+": 85, "B0": 80}),
    ("A+ ≥ 95, A0 ≥ 90, B+ ≥ 85, B0 ≥ 80, C+ ≥ 75", False, "high", {"A+": 95, "A0": 90, "C+": 75}),
    ("95점 이상 A+, 90점 이상 A0, 85점 이상 B+, 80점 이상 B0", False, "high", {"A+": 95, "B0": 80}),
    ("A+ : 95~100점, A0 : 90~94점, B+ : 85~89점, B0 : 80~84점", False, "high", {"A+": 95, "A0": 90, "B0": 80}),
    ("<table><tr><th>학점</th><th>점수</th></tr><tr><td>A+</td><td>95</td><td>100</td></tr>"
     "<tr><td>A0</td><td>90</td><td>94</td></tr><tr><td>B+</td><td>85</td><td>89</td></tr></table>", True, "high",
     {"A+": 95, "A0": 90, "B+": 85}),
    # 묶음(A·B·C)만 적힘 → +/0 경계는 가운데로 가정(medium)
    ("Grading scale: A: 90-100, B: 80-89, C: 70-79, D: 60-69, F: below 60", False, "medium",
     {"A+": 95, "A0": 90, "B+": 85, "B0": 80, "D0": 60}),
    ("A: 90% and above, B: 80% and above, C: 70% and above, D: 60% and above", False, "medium",
     {"A0": 90, "B0": 80, "C0": 70, "D0": 60}),
    ("절대평가: A 90점 이상, B 80점 이상, C 70점 이상", False, "medium", {"A+": 95, "A0": 90, "C0": 70}),
    # 300점 만점 → 100점 기준
    ("총점 300점 만점, 절대평가: A+ 285점 이상, A0 270점 이상, B+ 255점 이상", False, "high",
     {"A+": 95, "A0": 90, "B+": 85}),
]


@pytest.mark.parametrize("text,is_html,conf,want", ABS)
def test_absolute_cutoffs(text, is_html, conf, want):
    r = find_distribution(text, is_html)
    assert r is not None and r.kind == "absolute", text
    assert r.confidence == conf, r.notes
    assert r.dist is None and not g.validate_cutoffs(r.cutoffs)
    for k, v in want.items():
        assert r.cutoffs[k] == pytest.approx(v), (k, r.cutoffs)
    assert "F" not in r.cutoffs


# ---------------------------------------------------------------- 학점 비율이 아닌 것

NOT_DIST = [
    "평가 방법: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%",
    "Assessment: Final exam 50% / Report 20% / Project1 15% / Project2 15%",
    "Liberal attendance - you need to participate in the class over 66% by the end of the semester.",
    "출석률 66% 이상 출석해야 하며, 5회 이상 결석 시 F 학점.",
    "출석률 70% 미만은 F 처리합니다.",
    "F: No show at Final exam / Attendance under 66% / Zero score from the total assessment",
    "Grading: this course is graded on a P/NP (Pass/Non-pass) basis.",
    "P/F 과목: 출석 80% 이상이면 P(Pass), 미만이면 F(Fail)",
    "중간고사 결과: A+ 3명, A0 5명, B+ 8명. 평균 72.5, 표준편차 12.3",
    "평균 65.3, 표준편차 12.1, 최고점 98, Q1 55, Q3 78",
    "About 30% of instructions are loads and 10% are stores.",
    "Type A: 30%, Type B: 70% of the dataset",
    "Class A amplifier efficiency 25%, Class B 78.5%, Class C 90%",
    "We use C++ and C# in 30% of labs.",
    "과제 A 10%, 과제 B 10%, 중간고사 40%, 기말고사 40%",
    "본인 점수 A0, 중간고사 85점(상위 15%)",
    "D%84%B0%EA%B5%AC%EC%A1%B0-CH4-RISC-V 50%",
    "Part A: 40%, Part B: 60%",
    "11/20(금) 23:59까지 A4 용지 2장 제출",
    "A형 30%, B형 70%로 시험지를 나눕니다.",
]


@pytest.mark.parametrize("text", NOT_DIST)
def test_not_a_distribution(text):
    r = find_distribution(text)
    assert r is None or r.confidence == "low", (text, r)
    assert r is None, (text, r.as_dict() if r else None)


def test_weights_and_distribution_in_same_syllabus():
    text = ("평가 방법: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%\n"
            "학점 부여: 상대평가 — A+/A0 30%, B+/B0 40%, C+ 이하 30%\n출석 3/4 미만은 F")
    r = find_distribution(text)
    assert r.confidence == "high" and band(r.dist, "A+", "A0") == 30
    assert "중간고사" not in r.sentence


def test_two_different_tables_lower_confidence():
    r = find_distribution("학점 비율: A 30%, B 40%, C 이하 30%\n\n본문\n설명입니다 그리고\n학점 비율: A 20%, B 50%, C 이하 30%")
    assert r.confidence == "medium" and any("다른 학점 기준" in n for n in r.notes)


def test_long_text_is_fast():
    import time
    text = ("Lecture notes about pipelines. 30% of instructions are loads. Type A and B hazards. " * 400
            + "\n" + ROBOT + "\n" + "More slides 20% faster. " * 400)
    t = time.perf_counter()
    r = find_distribution(text)
    # 평소 ~0.9초. 느린 CI·바쁜 PC에서도 흔들리지 않게 넉넉히 — 정규식 폭주(수십 배 느려짐)만 잡으면 된다
    assert r and r.confidence == "high" and time.perf_counter() - t < 10.0


# ---------------------------------------------------------------- grades: 절대평가 계산

CUT = {"A+": 95, "A0": 90, "B+": 85, "B0": 80, "C+": 75, "C0": 70, "D+": 65, "D0": 60}


def test_letter_for_score_and_validate_cutoffs():
    assert [g.letter_for_score(x, CUT) for x in (100, 95, 94.99, 90, 81, 60, 59.9, 0)] == \
        ["A+", "A+", "A0", "A0", "B0", "D0", "F", "F"]
    assert g.letter_for_score(97, {"A0": 90, "B0": 80}) == "A0"      # 적힌 학점만
    assert g.validate_cutoffs({"A+": 90, "A0": 95}) and g.validate_cutoffs({"Z": 3}) and g.validate_cutoffs({})
    with pytest.raises(ValueError):
        g.letter_for_score(90, {"A0": 90, "B0": 95})


def test_estimate_absolute_uses_expected_total():
    comps = [{"name": "중간고사", "weight": 40, "score": 88, "max_score": 100, "mean": 70, "sd": 10},
             {"name": "과제", "weight": 20, "score": 19, "max_score": 20},
             {"name": "기말고사", "weight": 40}]
    est = g.estimate(comps, cutoffs=CUT)
    # (40·0.88 + 20·0.95) / 60 = 90.33 → A0. 남은 기말 0점이면 54.2(F), 만점이면 94.2(A0)
    assert est["basis"] == "absolute" and est["expected_total"] == pytest.approx(90.33, abs=0.01)
    assert est["grade"] == "A0" and est["grade_range"] == ["A0", "F"]
    assert est["total_range"] == [pytest.approx(54.2), pytest.approx(94.2)]
    assert est["top"] is not None    # 평균·표준편차가 있는 항목으로 위치도 함께
    assert any("절대평가 기준" in n for n in est["notes"]) and any("예상 총점" in n for n in est["notes"])
    # 평균·표준편차가 하나도 없어도 절대평가는 계산된다(순위 추정은 안 됨)
    est = g.estimate([{"name": "중간", "weight": 50, "score": 80}, {"name": "기말", "weight": 50}], cutoffs=CUT)
    assert est["grade"] == "B0" and est["top"] is None
    with pytest.raises(ValueError):
        g.estimate([{"name": "기말", "weight": 100}], cutoffs=CUT)


def test_needed_absolute_and_priorities():
    comps = [{"name": "중간", "weight": 50, "score": 80, "max_score": 100, "mean": 70, "sd": 10},
             {"name": "기말", "weight": 50, "mean": 70, "sd": 10}]
    n = g.needed_on(comps, "기말", "A0", cutoffs=CUT)
    assert n["possible"] and n["need_score"] == pytest.approx(100.0)     # (90·100/100 − 40)/50 = 1.0
    n = g.needed_on(comps, "기말", "B+", cutoffs=CUT)
    assert n["need_score"] == pytest.approx(90.0) and n["need_top"] is not None
    assert not g.needed_on(comps, "기말", "A+", cutoffs=CUT)["possible"]
    assert g.needed_on(comps, "기말", "D0", cutoffs=CUT)["need_score"] == pytest.approx(40.0)
    est = g.estimate(comps, cutoffs=CUT)
    pr = g.priorities([{"course": "X", "estimate": est, "remaining_weight": 50, "cutoffs": CUT,
                        "letter_basis": "syllabus"}])
    assert pr[0]["grade"] == "B0" and "점" in pr[0]["reason"] and pr[0]["letter_basis"] == "syllabus"


# ---------------------------------------------------------------- AI 답 풀기(from_groups)

def test_from_groups_resolves_like_rules_and_rejects_bad_answers():
    r = dm.from_groups("relative", [{"grades": ["A+", "A0"], "low": 40, "high": 50},
                                    {"grades": ["B+", "B0"], "low": 40, "high": 60},
                                    {"grades": ["C+", "C0", "D+", "D0", "F"], "low": 0, "high": 10}])
    assert band(r.dist, "A+", "A0") == 45 and r.ranges["B+·B0"] == [40, 60]
    r = dm.from_groups("relative", [{"grades": ["A+", "A0"], "pct": 30}, {"grades": ["A+", "A0", "B+", "B0"],
                                                                         "pct": 70, "cumulative": True}])
    assert band(r.dist, "B+", "B0") == 40
    for bad in ([{"grades": ["A+", "B+"], "pct": 30}],                                   # 이어지지 않음
                [{"grades": ["A+", "A0"], "pct": 60}, {"grades": ["A0", "B+"], "pct": 40}],   # 겹침
                [{"grades": ["A+", "A0"], "pct": 70}, {"grades": ["B+", "B0"], "pct": 60},
                 {"grades": ["C+", "C0", "D+", "D0", "F"], "pct": 20}],                # 합 150
                [{"grades": ["A+"], "pct": 60, "low": 10, "high": 20}]):               # pct가 범위 밖
        with pytest.raises(ValueError):
            dm.from_groups("relative", bad)
    assert dm.from_groups("absolute", cutoffs={"A+": 95, "A0": 90}).cutoffs == {"A+": 95, "A0": 90}
    with pytest.raises(ValueError):
        dm.from_groups("absolute", cutoffs={"A+": 80, "A0": 90})


def test_effective_prefers_user_then_auto_then_default():
    auto = {"kind": "relative", "dist": find_distribution(ROBOT).dist}
    assert dm.effective({"dist": dict(g.DEFAULT_DISTRIBUTION), "dist_auto": auto})["by"] == "user"
    assert dm.effective({"dist": None, "dist_auto": auto})["by"] == "syllabus"
    assert dm.effective({"dist": None, "dist_auto": {"kind": "relative", "dist": {"A+": 3}}})["by"] == "default"
    e = dm.effective({"dist_auto": {"kind": "absolute", "cutoffs": CUT}})
    assert e["by"] == "syllabus" and e["kind"] == "absolute" and e["cutoffs"]["A0"] == 90
    assert dm.effective(None)["by"] == "default"


# ---------------------------------------------------------------- gradebook: 자동 넣기

def S(text, typ="pdf", title="첨부 'Syllabus.pdf'", posted="2026-09-01T09:00:00+09:00", cid="201", html=False):
    return {"course_id": cid, "course_name": "262R (서울-학부)로봇공학개론(GENERAL ROBOTICS)-00분반", "type": typ,
            "id": cid, "title": title, "text": text, "is_html": html, "posted_at": posted}


@pytest.fixture()
def bstore(tmp_path):
    s = Store(tmp_path / "state.json")
    s.data["grades"] = {}
    return s


def course(store, cid="201"):
    return next(c for c in gradebook.section(store, [{"id": cid, "short_name": "로봇공학개론"}])["courses"]
                if str(c["course_id"]) == cid)


def test_auto_dist_fills_course_and_reports_syllabus_basis(bstore):
    log = gradebook.auto_dist(bstore, [S(ROBOT)], ai_enabled=False)
    a = bstore.data["grades"]["201"]["dist_auto"]
    assert a["kind"] == "relative" and a["by"] == "rules" and a["confidence"] == "high"
    assert a["source_title"] == "Syllabus.pdf" and a["source_type"] == "pdf" and a["posted_at"]
    assert a["ranges"] == {"A+·A0": [40, 50], "B+·B0": [40, 60], "C+~F": [0, 10]}
    assert a["source"].startswith("Syllabus.pdf: Grade (Guideline)")
    assert any("로봇공학개론" in x and "학점 비율" in x for x in log)
    assert gradebook.auto_dist(bstore, [S(ROBOT)], ai_enabled=False) == []        # 같은 결과 → 기록 없음
    gradebook.save_course(bstore, "201", {"components": [
        {"name": "Final exam", "weight": 50, "score": 80, "mean": 60, "sd": 10}, {"name": "Report", "weight": 50}]})
    c = course(bstore)
    assert c["using_default_dist"] is False and c["letter_basis"] == "syllabus"
    assert c["estimate"]["letter_basis"] == "syllabus" and c["dist_kind"] == "relative"
    assert c["dist_used"]["A+"] == 22.5 and c["cutoffs_used"] is None
    ds = c["dist_source"]
    assert ds["by"] == "syllabus" and ds["source_title"] == "Syllabus.pdf" and ds["ranges"]["A+·A0"] == [40, 50]
    assert ds["sentence"].startswith("Grade (Guideline)") and ds["found_by"] == "rules"
    assert any("Syllabus.pdf" in n and "가운데" in n for n in c["estimate"]["notes"])
    # 상위 15.9%: 기본 비율이면 A0, 강의계획서(A+ 22.5%)면 A+
    assert c["estimate"]["grade"] == "A+"


def test_newest_high_wins_and_user_dist_always_wins(bstore):
    old = S(ROBOT, posted="2026-09-01T09:00:00+09:00")
    new = S("<p>Updated grade guideline</p><p>A+/A0 30%, B+/B0 40%, C+ ~ F 30%</p>", typ="announcement",
            title="Updated grading policy", posted="2026-10-20T09:00:00+09:00", html=True)
    gradebook.auto_dist(bstore, [old, new], ai_enabled=False)
    a = bstore.data["grades"]["201"]["dist_auto"]
    assert a["source_title"] == "Updated grading policy" and a["dist"]["A+"] == 15
    gradebook.auto_dist(bstore, [new, old], ai_enabled=False)                       # 순서와 상관없이
    assert bstore.data["grades"]["201"]["dist_auto"]["dist"]["A+"] == 15
    mine = {"A+": 10, "A0": 20, "B+": 20, "B0": 20, "C+": 10, "C0": 10, "D+": 0, "D0": 0, "F": 10}
    gradebook.save_course(bstore, "201", {"components": [{"name": "Final", "weight": 100, "score": 70,
                                                          "mean": 60, "sd": 10}], "dist": mine})
    assert bstore.data["grades"]["201"]["dist_auto"]       # 남겨 두지만
    c = course(bstore)
    assert c["letter_basis"] == "user" and c["dist_source"]["by"] == "user" and c["dist_used"]["A+"] == 10
    gradebook.auto_dist(bstore, [old, new], ai_enabled=False)
    assert course(bstore)["config"]["dist"] == mine            # 자동이 사용자 비율을 덮지 않는다
    gradebook.save_course(bstore, "201", {"components": [{"name": "Final", "weight": 100, "score": 70,
                                                          "mean": 60, "sd": 10}], "dist": None})
    assert course(bstore)["letter_basis"] == "syllabus"        # 사용자 비율을 지우면 강의계획서 비율로 돌아감


def test_medium_used_low_skipped_and_stale_removed(bstore):
    gradebook.auto_dist(bstore, [S("성적은 상대평가로 A학점 30% 이내, B학점 이상 70% 이내에서 부여합니다.")], ai_enabled=False)
    assert bstore.data["grades"]["201"]["dist_auto"]["confidence"] == "medium"
    log = gradebook.auto_dist(bstore, [S("A학점은 수강인원의 30% 이내로 부여합니다.")], ai_enabled=False)
    assert "dist_auto" not in bstore.data["grades"]["201"] and any("근거가 사라져" in x for x in log)
    log = gradebook.auto_dist(bstore, [S("A학점은 수강인원의 30% 이내로 부여합니다.")], ai_enabled=False)
    assert any("확신이 낮아" in x for x in log) and "dist_auto" not in bstore.data["grades"]["201"]


def test_hidden_and_pass_fail_courses_skipped(bstore):
    bstore.data["course_prefs"] = {"201": {"mode": "pass_fail"}, "202": {"mode": "hidden"}}
    gradebook.auto_dist(bstore, [S(ROBOT), S(ROBOT, cid="202")], ai_enabled=False)
    assert "201" not in bstore.data["grades"] and "202" not in bstore.data["grades"]
    bstore.data["grades"]["201"] = {"components": [{"name": "X", "weight": 100, "score": 90, "mean": 70, "sd": 10}],
                                    "dist": None, "class_size": None,
                                    "dist_auto": {"kind": "relative", "dist": find_distribution(ROBOT).dist}}
    c = course(bstore)
    assert c["pass_fail"] and c["estimate"] is None and c["letter_basis"] is None and c["dist_source"] is None


def test_weights_auto_fill_keeps_dist_auto(bstore):
    gradebook.auto_dist(bstore, [S(ROBOT)], ai_enabled=False)
    gradebook.auto_fill(bstore, [S("Assessment: Final exam 50% / Report 20% / Project1 15% / Project2 15%")],
                        ai_enabled=False)
    cfg = bstore.data["grades"]["201"]
    assert cfg["dist_auto"]["kind"] == "relative" and [c["name"] for c in cfg["components"]][0] == "기말고사"


def test_absolute_course_uses_cutoffs_end_to_end(bstore):
    gradebook.auto_dist(bstore, [S("절대평가: A+ 95점 이상, A0 90점 이상, B+ 85점 이상, B0 80점 이상, C+ 75점 이상")],
                        ai_enabled=False)
    gradebook.save_course(bstore, "201", {"components": [{"name": "중간", "weight": 50, "score": 92},
                                                         {"name": "기말", "weight": 50}]})
    c = course(bstore)
    assert c["dist_kind"] == "absolute" and c["cutoffs_used"]["A0"] == 90 and c["dist_used"] is None
    assert c["estimate"]["basis"] == "absolute" and c["estimate"]["grade"] == "A0" and c["letter_basis"] == "syllabus"
    targets = {n["target"]: n for n in c["needed"]}
    assert set(targets) == {"A+", "A0"} and targets["A+"]["need_score"] == pytest.approx(98.0)
    assert any("절대평가" in n for n in c["estimate"]["notes"])


# ---------------------------------------------------------------- 성적 반응: letter_basis "syllabus"

def test_celebrate_judge_syllabus_basis_and_absolute(bstore):
    bstore.data["grades"]["201"] = {"components": [], "dist": None, "class_size": None,
                                    "dist_auto": {"kind": "relative", "dist": find_distribution(ROBOT).dist}}
    j = celebrate._judge(bstore, "201", 85, 70, 10, {})
    assert j["letter_basis"] == "syllabus" and j["letter"] == "A+" and j["basis"] == "score"   # 상위 6.7% < 22.5
    bstore.data["grades"]["201"]["dist_auto"] = {"kind": "absolute", "cutoffs": CUT}
    j = celebrate._judge(bstore, "201", 18, None, None, {}, max_score=20)                      # 90점 → A0
    assert (j["letter"], j["band"], j["letter_basis"]) == ("A0", "A", "syllabus")
    j = celebrate._judge(bstore, "201", 85, 70, 10, {})                                        # 만점을 모름 → 학점 없이 위치만
    assert j["letter"] is None and j["top"] is not None
    bstore.data["grades"]["201"].pop("dist_auto")
    assert celebrate._judge(bstore, "201", 85, 70, 10, {})["letter_basis"] == "default"


# ---------------------------------------------------------------- AI 대체 경로(grade_distribution)

CMD = r"C:\Users\someone\AppData\Roaming\npm\claude.cmd"
PROSE = ("학점은 상대평가입니다. A는 수강생의 삼분의 일, B는 그다음 삼분의 일 정도이고 나머지는 C 이하입니다. "
         "문의: prof@example.ac.kr")   # 숫자 대신 말 — 규칙으로는 못 읽는 꼴
GOOD = {"kind": "relative", "groups": [{"grades": ["A+", "A0"], "pct": 35}, {"grades": ["B+", "B0"], "pct": 35}],
        "evidence": "A는 수강생의 삼분의 일, B는 그다음 삼분의 일 정도이고"}


class FakeCLI:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, args, input, timeout, cwd):
        self.calls.append(input)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, BaseException):
            raise r
        text = json.dumps(r, ensure_ascii=False)
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                          "session_id": "s", "modelUsage": {"m": {}}})
        return subprocess.CompletedProcess(args, 0, out, "")


def test_ai_task_validates_quote_sum_and_redacts():
    assert find_distribution(PROSE) is None             # 규칙으로는 못 읽는 꼴
    fake = FakeCLI(GOOD)
    res = agent_cli.run_task("grade_distribution", {"text": PROSE, "title": "강의계획서"}, runner=fake, command=CMD)
    assert res.ok and res.data.kind == "relative" and band(res.data.dist, "A+", "A0") == 35
    assert res.data.confidence == "medium" and any("AI CLI" in n for n in res.data.notes)
    assert "prof@example" not in fake.calls[0] and "[이메일]" in fake.calls[0]
    # 지어낸 근거 → 고쳐 달라고 다시 묻고, 그래도면 실패
    lie = dict(GOOD, evidence="A학점 35%, B학점 40%")
    res = agent_cli.run_task("grade_distribution", {"text": PROSE}, runner=FakeCLI(lie), command=CMD)
    assert not res.ok and res.attempts == 2 and "그대로" in res.error
    # 합이 맞지 않음 → 실패, 두 번째에 고치면 통과
    bad = dict(GOOD, groups=[{"grades": ["A+", "A0"], "pct": 70}, {"grades": ["B+", "B0"], "pct": 60}])
    fake = FakeCLI(bad, GOOD)
    res = agent_cli.run_task("grade_distribution", {"text": PROSE}, runner=fake, command=CMD)
    assert res.ok and res.attempts == 2 and "검사를 통과하지 못했습니다" in fake.calls[1]
    # 없음
    res = agent_cli.run_task("grade_distribution", {"text": "P/NP 과목입니다."}, runner=FakeCLI({"kind": "none"}),
                             command=CMD)
    assert res.ok and res.data is None


def test_auto_dist_ai_only_when_rules_fail_and_enabled(bstore, monkeypatch):
    monkeypatch.setattr(agent_cli, "detect", lambda: CMD)
    bstore.data["settings"]["ai_cli"] = True
    fake = FakeCLI(GOOD)
    gradebook.auto_dist(bstore, [S(PROSE, typ="syllabus", title="강의계획서")], ai_enabled=False, runner=fake)
    assert not fake.calls and "201" not in bstore.data["grades"]
    gradebook.auto_dist(bstore, [S(ROBOT)], ai_enabled=True, runner=fake)          # 규칙이 찾으면 AI 안 부름
    assert not fake.calls and bstore.data["grades"]["201"]["dist_auto"]["by"] == "rules"
    log = gradebook.auto_dist(bstore, [S(PROSE, typ="syllabus", title="강의계획서", cid="202")], ai_enabled=True,
                              runner=fake)
    a = bstore.data["grades"]["202"]["dist_auto"]
    assert len(fake.calls) == 1 and a["by"] == "ai" and band(a["dist"], "A+", "A0") == 35
    assert any("AI CLI" in x for x in log)
    # 같은 글이면 다시 묻지 않고, AI를 꺼도 기억한 결과는 그대로
    gradebook.auto_dist(bstore, [S(PROSE, typ="syllabus", title="강의계획서", cid="202")], ai_enabled=True, runner=fake)
    gradebook.auto_dist(bstore, [S(PROSE, typ="syllabus", title="강의계획서", cid="202")], ai_enabled=False)
    assert len(fake.calls) == 1 and bstore.data["grades"]["202"]["dist_auto"]["by"] == "ai"
    assert course(bstore, "202")["dist_source"]["found_by"] == "ai"


def test_mock_ai_answers_none_for_distribution(bstore, monkeypatch):
    from lms_agent import mock_ai
    monkeypatch.setattr(agent_cli, "detect", lambda: CMD)
    res = agent_cli.run_task("grade_distribution", {"text": PROSE}, runner=mock_ai.runner, command=CMD)
    assert res.ok and res.data is None and res.attempts == 1


# ---------------------------------------------------------------- 데모(가짜 Canvas) 전체

def test_demo_asic_policy_distribution_through_sync(tmp_path):
    from lms_agent.canvas import Canvas
    from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        rep = core.sync(Canvas(base, DEMO_TOKEN), s)
        assert any("ASIC" in x and "학점 비율" in x for x in rep.get("auto_dist", []))
        a = s.data["grades"]["101"]["dist_auto"]
        assert a["source_title"] == "Updated Grading Policy" and a["source_type"] == "announcement"
        assert a["ranges"] == {"A+·A0": [35, 45], "B+·B0": [40, 50], "C+~F": [5, 25]}
        assert (band(a["dist"], "A+", "A0"), band(a["dist"], "B+", "B0")) == (40, 45)
        assert [c["name"] for c in s.data["grades"]["101"]["components"]][0] == "Homework assignments"
        assert all("dist_auto" not in s.data["grades"].get(cid, {}) for cid in ("102", "103", "104", "105"))
        s2 = Store(tmp_path / "state.json")      # 저장됐는지
        assert s2.data["grades"]["101"]["dist_auto"]["kind"] == "relative"
    finally:
        mock.shutdown()
