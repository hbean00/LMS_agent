"""점수표 코너 케이스 ①~⑩(계약 29-3, 사용자 요청 2026-11-19: "Corner case를 없애기 위해 모든 조치").

각 케이스는 지어낸 학번·점수만 쓴다. PDF·문서에서 뽑은 글(parse_score_results)과 표 파일 칸(parse_sheet_results)이 같은 규칙을 쓰는지,
성적 후보·추정까지 이어지는지 본다. 결과에는 내 값과 반 집계만 — 다른 학생의 학번·점수는 들어가지 않는다."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

import json
import statistics
from pathlib import Path

import pytest

from lms_agent import gradebook, grades, scorefile
from lms_agent.server import App
from lms_agent.store import Store

SID = "2099123456"   # 가짜 학번
OTHERS = [f"20990000{i:02d}" for i in range(1, 30)]


def rows_text(scores, mine, fmt="{id} {v}", ids=None, at=3) -> tuple[list[str], list]:
    """한 줄에 한 학생: 다른 학생 점수(scores) 사이 at번째에 내 줄."""
    ids = list(ids or OTHERS)
    lines, values = [], []
    for i, v in enumerate(scores):
        if i == at:
            lines.append(fmt.format(id=SID, v=mine))
            values.append(mine)
        lines.append(fmt.format(id=ids[i], v=v))
        values.append(v)
    return lines, values


def no_other_ids(obj) -> bool:
    s = json.dumps(obj, ensure_ascii=False, default=lambda o: o.__dict__)
    return not any(o in s for o in OTHERS)


def _app(tmp_path: Path, pdf_texts: dict, components=None, announcements=None, boards=None) -> App:
    s = Store(tmp_path / "state.json")
    s.data["settings"]["student_id"] = SID
    s.data["cache"] = {"fetched_at": "2026-11-18T10:00:00+09:00", "courses": [{"id": "1", "name": "테스트과목", "course_code": "T1"}],
                       "assignments": [], "conversations": [], "boards": boards or [], "errors": [],
                       "announcements": announcements or [], "pdf_texts": dict(pdf_texts)}
    s.data["pdf_texts"] = dict(pdf_texts)
    s.data["grades"] = {"1": {"components": components or [{"name": "실습", "weight": 30.0}, {"name": "중간고사", "weight": 30.0},
                                                          {"name": "기말고사", "weight": 40.0}],
                              "dist": None, "class_size": None}}
    s.save()
    return App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": "test-token"})


def rec(text, filename, posted="2026-11-10T10:00:00+09:00", **extra) -> dict:
    return {"course_id": "1", "filename": filename, "text": text, "posted_at": posted, "board_id": "announcements",
            "post": "5", **extra}


# ================================================================ ① 적힌 평균·표준편차(머리글 줄 + 값 줄)

def test_1_stated_mean_median_sd_header_then_values_line():
    body, values = rows_text([61, 75, 48, 90, 66, 72, 58, 81], 77)
    text = "\n".join(["Midterm 채점 결과", "평균값 중간값 표준편차", "71.2 70 12.5", "", "학번 점수"] + body)
    (r,) = scorefile.parse_score_results(text, SID, "Midterm")
    assert (r.score, r.n) == (77, len(values))
    assert (r.mean, r.sd) == (71.2, 12.5)                                    # 적힌 값
    col = r.columns[0]
    assert abs(col["mean_computed"] - statistics.mean(values)) < 0.01 and abs(col["sd_computed"] - statistics.stdev(values)) < 0.01
    assert col.get("median") == 70
    assert any("적힌" in n and "평균" in n for n in r.notes)


def test_1_stated_stats_english_and_inline_forms():
    body, _ = rows_text([6, 7, 5, 9, 8, 4], 9)
    en = "\n".join(["Quiz 3 Results", "Mean Median SD", "6.9 7 1.7", "Student ID Score"] + body)
    (r,) = scorefile.parse_score_results(en, SID, "Quiz 3")
    assert (r.mean, r.sd) == (6.9, 1.7)
    inline = "\n".join(["Quiz 3 Results", "Student ID Score"] + body + ["Mean: 6.9, SD: 1.7"])
    (r,) = scorefile.parse_score_results(inline, SID, "Quiz 3")
    assert (r.mean, r.sd) == (6.9, 1.7)


def test_1_stated_stats_outside_the_column_are_not_used():
    body, values = rows_text([6, 7, 5, 9, 8, 4], 9)
    text = "\n".join(["평균 표준편차", "71.2 12.5", "학번 점수"] + body)   # 다른 시험의 통계(값 범위 밖)
    (r,) = scorefile.parse_score_results(text, SID, "Quiz")
    assert abs(r.mean - statistics.mean(values)) < 0.01
    assert any("쓰지 않음" in n for n in r.notes)


# ================================================================ ② 재검·v2·수정본

def _table(mine, others=(6, 7, 5, 8, 4), head="학번 점수 (10)"):
    body, _ = rows_text(list(others), mine)
    return "\n".join([head] + body)


def _members(app, comp):
    (course,) = app.state()["grades"]["courses"]
    (c,) = [c for c in course["config"]["components"] if c["name"] == comp]
    return c["members"]


def test_2_regraded_file_replaces_older_one_with_a_different_name(tmp_path):
    app = _app(tmp_path, {
        "1:cf1": rec(_table(6), "Lab1_score.pdf", "2026-11-01T10:00:00+09:00"),
        "1:cf2": rec(_table(9), "Lab1_v2_score.pdf", "2026-11-05T10:00:00+09:00"),
    })
    ms = _members(app, "실습")
    assert [(m["label"], m["score"], m["source_title"]) for m in ms] == [("Lab1", 9, "Lab1_v2_score.pdf")]


def test_2_regraded_file_in_the_same_post_wins_over_the_original(tmp_path):
    same = "2026-11-05T10:00:00+09:00"
    for older, newer in (("Midterm_score.pdf", "Midterm_score_재검.pdf"), ("중간고사_성적.pdf", "중간고사_성적_수정본.pdf"),
                         ("Midterm_score_v2.pdf", "Midterm_score_v3.pdf")):
        app = _app(tmp_path, {"1:cf1": rec(_table(52, (40, 61, 70, 55, 66), "학번 점수"), older, same),
                              "1:cf2": rec(_table(58, (40, 61, 70, 55, 66), "학번 점수"), newer, same)})
        ms = _members(app, "중간고사")
        assert [(m["score"], m["source_title"]) for m in ms] == [(58, newer)], (older, newer)
    assert scorefile.revision_of("Lab1_score.pdf") == 0
    assert scorefile.revision_of("Lab1_v2_score.pdf") == 2 and scorefile.revision_of("Lab1 재채점 결과.pdf") >= 1
    assert scorefile.label_from_filename("Midterm_재검_score.pdf") == "Midterm"
    assert scorefile.label_from_filename("수정본_Lab1_score.pdf") == "Lab1"


# ================================================================ ③ 개인 성적표(표 없이 내 학번 하나)

def test_3_personal_sheet_total_with_max_and_stated_stats():
    text = "\n".join(["2026 중간고사 개인 성적표", f"학번: {SID}", "이름: 홍길동", "문항 1 20/25", "문항 2 30/35",
                      "Total 87/100", "반 평균: 72.4 표준편차: 10.3"])
    assert scorefile.is_score_file("report.pdf", text)
    (r,) = scorefile.parse_score_results(text, SID)
    assert (r.score, r.max_score, r.mean, r.sd, r.n, r.rank) == (87, 100, 72.4, 10.3, None, None)
    assert r.label == "중간고사" and r.personal is True
    assert any("개인 성적표" in n for n in r.notes)


def test_3_personal_sheet_id_in_filename_korean_total():
    (r,) = scorefile.parse_score_results("Midterm Exam\n총점: 87\n", SID, filename=f"{SID}_Midterm.pdf")
    assert (r.score, r.label, r.mean, r.personal) == (87, "Midterm Exam", None, True)   # 글의 평가 이름이 먼저
    (r,) = scorefile.parse_score_results("총점: 87\n", SID, filename=f"{SID}_Midterm.pdf")
    assert (r.score, r.label) == (87, "Midterm")                                       # 없으면 파일 이름(학번 조각은 뺌)
    assert scorefile.parse_score_results("Midterm Exam\n총점: 87\n", SID, filename="2099000001_Midterm.pdf") == []
    assert scorefile.parse_score_results(f"학번 {OTHERS[0]}\nTotal 87/100", SID) == []          # 다른 학생의 성적표


def test_3_personal_sheet_reaches_candidates(tmp_path):
    app = _app(tmp_path, {"1:cf1": rec(f"중간고사 성적 안내\n학번 {SID}\n총점: 81 / 100\n", "notice.pdf")})
    (c,) = [c for c in app.state()["grades"]["candidates"] if c["key"] == "pdf:1:cf1"]
    assert (c["score"], c["max_score"], c["component"]) == (81, 100, "중간고사")


# ================================================================ ④ 칸 속 '87/100'·'87%'

def test_4_fraction_and_percent_cells_in_text():
    body, values = rows_text(["6/10", "7/10", "5/10", "8/10", "4/10"], "9/10")
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 4", "학번 점수"] + body), SID, "Quiz 4")
    assert (r.score, r.max_score, r.n) == (9, 10, 6) and r.rank == 1
    spaced = [ln.replace("/", " / ") for ln in body]
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 4", "학번 점수"] + spaced), SID, "Quiz 4")
    assert (r.score, r.max_score) == (9, 10)
    body, _ = rows_text(["61%", "75%", "48%", "90%"], "87%")
    (r,) = scorefile.parse_score_results("\n".join(["Final", "학번 점수"] + body), SID, "Final")
    assert (r.score, r.max_score, r.n) == (87, 100, 5)


def test_4_dates_are_not_fractions_and_percentile_column_stays_out():
    body, _ = rows_text(["11/18 6", "11/19 7", "11/20 5", "11/21 8"], "11/22 9")
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 5", "학번 제출일 점수"] + body), SID, "Quiz 5")
    assert (r.score, r.n) == (9, 5)
    body, _ = rows_text(["6 40%", "7 60%", "5 20%", "8 80%"], "9 100%")
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 6", "학번 점수 백분위"] + body), SID, "Quiz 6")
    assert (r.score, r.n) == (9, 5)                                         # 점수 열 + 백분위(%) 열 → 백분위는 뺀다


def test_4_fraction_and_percent_cells_in_sheets():
    rows = [{0: "학번", 1: "Quiz 1", 2: "Final"}]
    for i, (a, b) in enumerate([("6/10", "61%"), ("7/10", "75%"), ("5/10", "48%"), ("8/10", "90%")]):
        rows.append({0: OTHERS[i], 1: a, 2: b})
    rows.append({0: SID, 1: "9/10", 2: "87%"})
    out = scorefile.parse_sheet_results([{"name": "성적", "rows": rows}], SID, "성적.xlsx")
    got = {r.label: (r.score, r.max_score, r.n) for r in out}
    assert got == {"Quiz 1": (9, 10, 5), "Final": (87, 100, 5)}


# ================================================================ ⑤ 가린 학번이 여러 행과 맞음 → '애매함'

def test_5_masked_id_matching_several_rows_is_ambiguous_not_silent():
    ids = ["2099****56", "2099****56", "2099****11", "2099****22", "2099****33", "2099****44"]
    lines = ["Lab 2", "학번 점수"] + [f"{i} {v}" for i, v in zip(ids, [7, 9, 5, 6, 8, 4])]
    assert scorefile.parse_score_results("\n".join(lines), SID, "Lab 2") == []    # 옛 호출(기본)은 예전처럼 결과 없음
    (r,) = scorefile.parse_score_results("\n".join(lines), SID, "Lab 2", ambiguous=True)   # 성적 후보(gradebook)가 켬
    assert r.score is None and r.rank is None
    assert r.ambiguous == {"kind": "masked_multi", "matches": 2, "reason": r.ambiguous["reason"]} and r.ambiguous["reason"]
    assert r.n == 6 and abs(r.mean - statistics.mean([7, 9, 5, 6, 8, 4])) < 0.01   # 반 집계는 있다
    assert "7" not in str(r.ambiguous) and "9" not in str(r.ambiguous)            # 맞은 줄의 점수는 넣지 않는다
    # 하나만 맞으면 예전처럼 본인으로
    lines[2] = "2099****99 7"
    (r,) = scorefile.parse_score_results("\n".join(lines), SID, "Lab 2")
    assert r.score == 9 and r.ambiguous is None


def test_5_masked_ambiguous_in_sheets_and_candidate_field(tmp_path):
    rows = [{0: "학번", 1: "점수"}] + [{0: i, 1: v} for i, v in zip(
        ["2099****56", "2099****56", "2099****11", "2099****22"], [7, 9, 5, 6])]
    assert scorefile.parse_sheet_results([{"name": "Lab2", "rows": rows}], SID, "Lab2_성적.xlsx") == []
    (r,) = scorefile.parse_sheet_results([{"name": "Lab2", "rows": rows}], SID, "Lab2_성적.xlsx", ambiguous=True)
    assert r.score is None and r.ambiguous["matches"] == 2
    lines = ["Lab 2", "학번 점수"] + [f"{i} {v}" for i, v in zip(
        ["2099****56", "2099****56", "2099****11", "2099****22", "2099****33"], [7, 9, 5, 6, 8])]
    app = _app(tmp_path, {"1:cf1": rec("\n".join(lines), "Lab2_score.pdf")})
    (c,) = [c for c in app.state()["grades"]["candidates"] if c["key"] == "pdf:1:cf1"]
    assert c["score"] is None and c["ambiguous"]["kind"] == "masked_multi"
    assert _members(app, "실습") == []                                         # 추정에는 넣지 않는다


# ================================================================ ⑥ 공지·게시판 본문(HTML 표)

def _html_table(mine, others=(6, 7, 5, 8, 4), caption="Quiz 2 결과를 알려 드립니다."):
    trs = "".join(f"<tr><td>{i}</td><td>{v}</td></tr>" for i, v in zip(OTHERS, others))
    return (f"<p>{caption}</p><table><tr><th>학번</th><th>점수 (10)</th></tr>{trs}"
            f"<tr><td>{SID}</td><td>{mine}</td></tr></table>")


def test_6_score_table_inside_announcement_body(tmp_path):
    anns = [{"id": 5, "title": "Quiz 2 결과", "message": _html_table(9), "posted_at": "2026-11-10T10:00:00+09:00",
             "context_code": "course_1", "html_url": "https://lms.example.test/1/a/5"}]
    app = _app(tmp_path, {}, announcements=anns)
    cands = [c for c in app.state()["grades"]["candidates"] if c.get("file_kind") == "body"]
    (c,) = cands
    assert (c["key"], c["source_type"], c["score"], c["max_score"], c["n"], c["label"]) == \
        ("body:announcement:5", "announcement", 9, 10, 6, "Quiz 2")
    assert c["url"] == "https://lms.example.test/1/a/5" and no_other_ids(c)
    assert app.state()["grades"]["candidates"] == app.state()["grades"]["candidates"]   # 같은 key(안정)


def test_6_body_table_is_not_duplicated_when_the_same_table_is_attached(tmp_path):
    anns = [{"id": 5, "title": "Quiz 2 결과", "message": _html_table(9), "posted_at": "2026-11-10T10:00:00+09:00",
             "context_code": "course_1", "html_url": "https://lms.example.test/1/a/5"}]
    body, _ = rows_text([6, 7, 5, 8, 4], 9)
    app = _app(tmp_path, {"1:cf1": rec("\n".join(["Quiz 2", "학번 점수 (10)"] + body), "Quiz2_score.pdf")},
               announcements=anns)
    cands = [c for c in app.state()["grades"]["candidates"] if c.get("score") == 9]
    assert [c["key"] for c in cands] == ["pdf:1:cf1"]


def test_6_board_post_body_table(tmp_path):
    boards = [{"course_id": "1", "board_id": 77, "board_title": "성적 게시판", "source": "learningx",
               "posts": [{"id": 3, "title": "Lab 4 점수", "content": _html_table(8, (5, 6, 7, 9), "점수를 확인하세요."),
                          "created_at": "2026-11-11T10:00:00+09:00", "post_url": "https://lx.example.test/77/3"}]}]
    app = _app(tmp_path, {}, boards=boards)
    (c,) = [c for c in app.state()["grades"]["candidates"] if c.get("file_kind") == "body"]
    assert (c["key"], c["source_type"], c["score"], c["label"]) == ("body:board:77-3", "board", 8, "Lab 4")


def test_6_text_scores_never_come_from_other_students_rows():
    text = "Quiz 2 결과\n학번 점수\n" + "\n".join(f"{i} {v}" for i, v in zip(OTHERS, [6, 7, 5])) + "\n평균 6.0"
    assert all(f.score is None for f in grades.find_scores(text))


# ================================================================ ⑦ 결시·미응시·'-'와 0점

def test_7_zero_is_a_score_absent_marks_are_not():
    body, _ = rows_text([7, "결시", 0, "-", "미응시", 5, "0"], 0)
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 7", "학번 점수"] + body), SID, "Quiz 7")
    assert r.score == 0 and r.n == 5 and r.low == 0 and r.absent is None       # 0점 셋(나·0·0) + 7·5
    assert abs(r.mean - statistics.mean([0, 7, 0, 5, 0])) < 0.01
    assert any("결시" in n for n in r.notes)


def test_7_my_absent_mark_is_not_zero(tmp_path):
    body, _ = rows_text([7, 6, 0, 5, 8], "결시")
    text = "\n".join(["Midterm", "학번 점수"] + body)
    (r,) = scorefile.parse_score_results(text, SID, "Midterm")
    assert r.score is None and r.absent == "결시" and r.n == 5
    assert any("0점으로 보지 않음" in n for n in r.notes)
    app = _app(tmp_path, {"1:cf1": rec(text, "Midterm_score.pdf")})
    (c,) = [c for c in app.state()["grades"]["candidates"] if c["key"] == "pdf:1:cf1"]
    assert c["score"] is None and c["absent"] == "결시"
    assert _members(app, "중간고사") == []                                    # 0점으로 추정에 넣지 않는다


def test_7_absent_and_zero_in_sheets():
    rows = [{0: "학번", 1: "점수"}] + [{0: i, 1: v} for i, v in zip(OTHERS, [7, "결시", 0, "-", 5])] + [{0: SID, 1: "미응시"}]
    (r,) = scorefile.parse_sheet_results([{"name": "Quiz", "rows": rows}], SID, "Quiz_성적.xlsx")
    assert r.score is None and r.absent == "미응시" and r.n == 3


# ================================================================ ⑧ 학점 글자 열

def test_8_letter_grade_column_is_letter_not_score():
    body, _ = rows_text(["61 C+ 2.5", "75 B0 3.0", "48 D0 1.0", "90 A+ 4.5"], "87 A0 4.0")
    (r,) = scorefile.parse_score_results("\n".join(["Final", "학번 점수 학점 평점"] + body), SID, "Final")
    assert (r.score, r.letter, r.n) == (87, "A0", 5)
    assert [c["name"] for c in r.columns] == ["점수"]
    body, _ = rows_text(["61 C+", "75 B0", "48 D", "90 A+"], "87 A")
    (r,) = scorefile.parse_score_results("\n".join(["Final", "학번 점수 학점"] + body), SID, "Final")
    assert (r.score, r.letter) == (87, "A")


def test_8_letter_grade_column_in_sheets_and_candidate(tmp_path):
    rows = [{0: "학번", 1: "총점", 2: "학점"}] + [{0: i, 1: v, 2: g} for i, (v, g) in zip(
        OTHERS, [(61, "C+"), (75, "B0"), (48, "D0"), (90, "A+")])] + [{0: SID, 1: 87, 2: "A0"}]
    (r,) = scorefile.parse_sheet_results([{"name": "Final", "rows": rows}], SID, "Final_성적.xlsx")
    assert (r.score, r.letter) == (87, "A0")
    body, _ = rows_text(["61 C+", "75 B0", "48 D0", "90 A+"], "87 A0")
    app = _app(tmp_path, {"1:cf1": rec("\n".join(["Final", "학번 점수 학점"] + body), "Final_score.pdf")})
    (c,) = [c for c in app.state()["grades"]["candidates"] if c["key"] == "pdf:1:cf1"]
    assert (c["score"], c["letter"]) == (87, "A0")


# ================================================================ ⑨ 학번 구분자

def test_9_id_with_hyphens_or_spaces():
    dashed = [f"{i[:4]}-{i[4:6]}-{i[6:]}" for i in OTHERS]
    body, _ = rows_text([6, 7, 5, 8], 9, fmt="{id} {v}", ids=dashed)
    body = [ln.replace(SID, f"{SID[:4]}-{SID[4:6]}-{SID[6:]}") for ln in body]
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 9", "학번 점수"] + body), SID, "Quiz 9")
    assert (r.score, r.n) == (9, 5)
    spaced = [ln.replace("-", " ") for ln in body]
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 9", "학번 점수"] + spaced), SID, "Quiz 9")
    assert (r.score, r.n) == (9, 5)
    # 날짜(2026-11-18)는 학번이 아니다
    body, _ = rows_text(["2026-11-18 6", "2026-11-18 7", "2026-11-19 5", "2026-11-19 8"], "2026-11-19 9")
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 9", "학번 제출일 점수"] + body), SID, "Quiz 9")
    assert (r.score, r.n) == (9, 5)


def test_9_id_with_separators_in_sheets():
    rows = [{0: "학번", 1: "점수"}] + [{0: f"{i[:4]}-{i[4:6]}-{i[6:]}", 1: v} for i, v in zip(OTHERS, [6, 7, 5])] + \
        [{0: f"{SID[:4]}-{SID[4:6]}-{SID[6:]}", 1: 9}]
    (r,) = scorefile.parse_sheet_results([{"name": "Quiz", "rows": rows}], SID, "Quiz_성적.xlsx")
    assert (r.score, r.n) == (9, 4)


# ================================================================ ⑩ 머리글이 둘째 줄·점수 열이 학번보다 앞

def test_10_score_column_before_id_column_in_text():
    body, _ = rows_text([6, 7, 5, 8], 9, fmt="{v} {id}")
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 10", "점수 학번"] + body), SID, "Quiz 10")
    assert (r.score, r.n) == (9, 5)
    numbered = [f"{n} {ln}" for n, ln in enumerate(body, 1)]                  # 순번 점수 학번
    (r,) = scorefile.parse_score_results("\n".join(["Quiz 10", "번호 점수 학번"] + numbered), SID, "Quiz 10")
    assert (r.score, r.n) == (9, 5)


def test_10_header_on_second_row_and_score_before_id_in_sheets():
    rows = [{0: "2026 2학기 Quiz 10 성적"}, {0: "점수", 1: "학번", 2: "이름"}]
    for i, v in enumerate([6, 7, 5, 8]):
        rows.append({0: v, 1: OTHERS[i], 2: "김*수"})
    rows.append({0: 9, 1: SID, 2: "나*빈"})
    (r,) = scorefile.parse_sheet_results([{"name": "Sheet1", "rows": rows}], SID, "quiz10.xlsx")
    assert (r.score, r.n, r.label) == (9, 5, "Quiz 10")
    grouped = [{0: "학번", 1: "시험", 3: "과제"}, {1: "중간 (100)", 2: "기말 (100)", 3: "HW 1 (10)"}]
    for i, (a, b, c) in enumerate([(61, 70, 8), (75, 66, 9), (48, 52, 7), (90, 88, 10)]):
        grouped.append({0: OTHERS[i], 1: a, 2: b, 3: c})
    grouped.append({0: SID, 1: 80, 2: 77, 3: 9})
    out = scorefile.parse_sheet_results([{"name": "성적", "rows": grouped}], SID, "성적.xlsx")
    assert {r.label: (r.score, r.max_score) for r in out} == {"중간": (80, 100), "기말": (77, 100), "HW 1": (9, 10)}


# ================================================================ 공통: 다른 학생 정보가 결과에 없다

@pytest.mark.parametrize("make", [
    lambda: scorefile.parse_score_results("\n".join(["평균값 중간값 표준편차", "6.9 7 1.7", "학번 점수"]
                                                    + rows_text([6, 7, 5, 9, 8, 4], 9)[0]), SID, "Quiz"),
    lambda: scorefile.parse_score_results("\n".join(["학번 점수 학점"] + rows_text(["61 C+", "75 B0", "48 D0", "90 A+"], "87 A0")[0]),
                                          SID, "Final"),
])
def test_results_carry_only_my_values_and_class_aggregates(make):
    out = make()
    assert out and no_other_ids([r.__dict__ for r in out])
