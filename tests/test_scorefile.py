"""성적 공지 첨부 점수표 PDF(글로 뽑은 것)에서 내 점수와 반 집계만: 가린 학번, 같은 행 반복, 요약 행, 여러 열,
내 행 없음, 칸이 줄마다 끊긴 PDF 글, 성적 후보(gradebook.candidates)와 학번 설정."""
import statistics
from dataclasses import asdict

import pytest

from lms_agent import gradebook, scorefile
from lms_agent.store import Store

TABLE = """Lab0 Score Announcement (re-announced)
No 학번 이름 Prelab Report Total(100)
1 2026####11 김*수 8 70 78
2 2026####22 이*희 9 80 89
3 2026####33 박*민 7 65 72
4 2026####44 최*준 10 88 98
5 2026####55 정*아 결시 60 60
평균 8.5 72.6 79.4
표준편차 1.3 11.1 15.0
- 1 -
"""


def test_masked_id_unique_match_multi_columns_and_summary_ignored():
    r = scorefile.parse_score_text(TABLE, "2026123433", filename="Lab0_score-1.pdf")
    assert r is not None and r.label == "Lab0" and r.column == "Total" and r.max_score == 100
    assert (r.score, r.n, r.rank, r.high, r.low) == (72, 5, 4, 98, 60)
    assert r.mean == pytest.approx((78 + 89 + 72 + 98 + 60) / 5, abs=1e-3)
    assert r.sd == pytest.approx(statistics.stdev([78, 89, 72, 98, 60]), abs=1e-3)
    cols = {c["name"]: c for c in r.columns}
    assert set(cols) == {"Prelab", "Report", "Total"}
    assert cols["Prelab"]["n"] == 4 and cols["Prelab"]["mine"] == 7        # 결시는 통계에서 뺌
    assert any("가린 학번" in n for n in r.notes) and any("결시" in n or "빈 칸" in n for n in r.notes)
    assert "내 점수 72/100" in r.sentence and "5명 중 4등" in r.sentence
    dumped = str(asdict(r))
    for other in ("2026####11", "2026####22", "김*수", "이*희", "2026123433"):
        assert other not in dumped                                          # 다른 학생·내 학번은 결과에 없음


def test_exact_id_and_duplicate_rows_counted_once():
    text = ("학번 성명 점수\n2026100001 홍길동 85\n2026100002 김철수 70\n2026100003 이영희 90\n"
            "학번 성명 점수\n2026100002 김철수 70\n2026100003 이영희 90\n2026100004 박민수 55\n")
    r = scorefile.parse_score_text(text, "2026100003", label="중간고사")
    assert r.label == "중간고사" and r.score == 90 and r.n == 4 and r.rank == 1
    assert r.mean == pytest.approx(75.0) and any("한 번만" in n for n in r.notes)


def test_masked_id_matching_several_rows_is_not_chosen():
    text = "학번 점수\n2026****33 80\n2026****33 60\n2026****12 70\n"
    assert scorefile.parse_score_text(text, "2026123433") is None


def test_own_row_missing_returns_none():
    assert scorefile.parse_score_text(TABLE, "2025999999") is None
    assert scorefile.parse_score_text("평균 72.3\n표준편차 10.2", "2026123433") is None
    assert scorefile.parse_score_text("", "2026123433") is None


def test_cells_split_across_lines_rebuilt_by_id():
    text = "No\n학번\n점수\n1\n2026####11\n50\n2\n2026####33\n70\n3\n2026####44\n90\n평균\n70\n"
    r = scorefile.parse_score_text(text, "2026123433", filename="Lab1_score-2.pdf")
    assert r is not None and r.label == "Lab1" and (r.score, r.n, r.mean) == (70, 3, 70)
    assert any("다시 만듦" in n for n in r.notes)


def test_labels_and_score_file_detection():
    assert scorefile.label_from_filename("Lab0_score-1.pdf") == "Lab0"
    assert scorefile.label_from_filename("중간고사_성적.pdf") == "중간고사"
    assert scorefile.label_from_filename("score_midterm.pdf") == "midterm"
    assert scorefile.label_from_filename("Lab_1_score.pdf") == "Lab 1"
    assert scorefile.is_score_file("Lab0_score-1.pdf", "")
    assert scorefile.is_score_file("table.pdf", TABLE)
    assert not scorefile.is_score_file("Lab3.pdf", "Lab3 Assignment\nDesign a 4-bit counter (due : 23:59 on 11/18)")


@pytest.fixture()
def gstore(tmp_path):
    s = Store(tmp_path / "state.json")
    s.data["cache"] = {"courses": [{"id": "101", "name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반"}]}
    s.data["pdf_texts"] = {
        "101:9001": {"text": TABLE, "course_id": "101", "filename": "Lab0_score-1.pdf",
                     "post_key": "announcement:9100", "posted_at": "2026-10-20T10:00:00+09:00"},
        "101:9002": {"text": "Lab3 Assignment\nDesign a counter (due : 23:59 on 11/18)", "course_id": "101",
                     "filename": "Lab3.pdf", "post_key": "board:1-2", "posted_at": "2026-11-10T10:00:00+09:00"},
    }
    return s


def test_candidates_from_score_files_need_student_id(gstore):
    assert not [c for c in gradebook.candidates(gstore) if c["source_type"] == "pdf"]
    with pytest.raises(ValueError, match="6~12자"):
        gradebook.set_student_id(gstore, "12-34")
    gradebook.set_student_id(gstore, "2026123433")
    pdf = [c for c in gradebook.candidates(gstore) if c["source_type"] == "pdf"]
    assert len(pdf) == 1
    c = pdf[0]
    assert c["key"] == "pdf:101:9001" and c["source_title"] == "Lab0_score-1.pdf" and c["label"] == "Lab0"
    assert (c["score"], c["max_score"], c["high"], c["low"]) == (72, 100, 98, 60)
    assert c["course_short"] == "ASIC설계(영강)" and c["posted_at"].startswith("2026-10-20")
    assert set(c) >= {"course_id", "course_short", "source_type", "source_title", "posted_at", "url", "label", "score",
                      "max_score", "mean", "sd", "high", "low", "sentence", "notes"}
    assert "2026####11" not in str(c) and "2026123433" not in str(c)
    sec = gradebook.section(gstore, [])
    assert sec["student_id_set"] and sec["student_id_masked"] == "2026****33"
    gradebook.set_student_id(gstore, None)
    assert "student_id" not in gstore.data["settings"] and not gradebook.section(gstore, [])["student_id_set"]


@pytest.mark.parametrize("header", [
    # 실제 고려대 ASIC 성적 PDF의 머리글 모양(값은 가짜): 여러 낱말 이름 + 띄어 쓴 "(만점)"
    "Student ID Required Items (10) Functional Description (25) Code Correctness (15) Total (50)",
    # 같은 PDF의 한국어 머리글: 만점이 마지막 낱말에 붙어 있다
    "학번 필수 요소(10) 동작 설명(25) 코드 정확성(15) 총점(50)",
])
def test_multi_word_column_names_keep_their_max(header):
    rows = ["2026000001 10 25 15 50", "2026000002 8 20 12 40", "2026000003 9 22 15 46"]
    text = "ASIC Design 2026 Lab0 점수 (Lab0 Scores)\n" + header + "\n" + "\n".join(rows)
    r = scorefile.parse_score_text(text, "2026000002", filename="Lab0_score-1.pdf")
    assert r is not None and r.label == "Lab0"
    cols = {c["name"]: c for c in r.columns}
    assert len(cols) == 4
    first, second, third, total = r.columns
    assert (first["mine"], first["max_score"]) == (8.0, 10.0)
    assert (second["mine"], second["max_score"]) == (20.0, 25.0)
    assert (third["mine"], third["max_score"]) == (12.0, 15.0)
    assert (r.score, r.max_score) == (40.0, 50.0) and total["max_score"] == 50.0
    assert " " in first["name"]  # "Required Items" / "필수 요소"처럼 낱말이 붙어 있다


def test_single_marker_header_still_one_name_per_word():
    text = "학번 중간 기말 총점(100)\n2026000001 30 40 70\n2026000002 20 30 50\n2026000003 25 35 60"
    r = scorefile.parse_score_text(text, "2026000002", filename="midterm_score.pdf")
    assert [c["name"] for c in r.columns] == ["중간", "기말", "총점"]
    assert r.max_score == 100.0 and r.score == 50.0


def test_decimal_scores_letter_ids_and_official_average():
    """실제 ASIC 성적 PDF 모양: 22.5 같은 소수 점수(날짜로 오해하면 안 됨), 영문 두 글자가 섞인 학번, 끝의 공식 평균 줄."""
    text = "\n".join([
        "ASIC Design 2026 Lab1 점수 (Lab1 Scores)",
        "Student ID Required Items (10) Functional Description (25) Code Correctness (15) Total (50)",
        "2026000001 10 25 15 50",
        "2026000002 10 22.5 15 47.5",
        "2026000003 8 20 12 40",
        "2026KU0004 10 12.5 10 32.5",
        "평균 (Average) 9.5 20.0 13.0 42.5",
    ])
    r = scorefile.parse_score_text(text, "2026000002", filename="Lab1_score-2.pdf")
    assert r is not None
    assert r.n == 4 and r.score == 47.5 and r.rank == 2          # 소수 행·KU 학번 행 모두 셈
    assert not any("칸 수가 다른" in n for n in r.notes)
    assert r.mean == 42.5 and any("PDF에 적힌 값" in n for n in r.notes)
    assert not any("계산한 평균" in n for n in r.notes)              # 다 읽었으니 공식 평균과 맞다
    r2 = scorefile.parse_score_text(text, "2026KU0004", filename="Lab1_score-2.pdf")
    assert r2 is not None and r2.score == 32.5 and r2.rank == 4


def test_official_average_mismatch_is_reported():
    text = "\n".join(["학번 중간(50) 기말(50) 총점(100)", "2026000001 40 40 80", "2026000002 30 30 60",
                      "2026000003 20 20 40", "평균 35 35 70"])          # 계산 평균 60과 다름(행을 못 읽었다는 신호)
    r = scorefile.parse_score_text(text, "2026000002", filename="midterm_score.pdf")
    assert r.mean == 70.0 and any("계산한 평균" in n for n in r.notes)
