"""마감 찾기 벤치마크: 쉬움 / 중간 / 어려움.

기준일 REF = 2026-11-12(목) 10:00 +09:00. 못 맞히는 문장이 생기면 여기에 추가하고 규칙을 고친다.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lms_agent.extract import find_deadlines, html_to_text, same_task, title_hint  # noqa: E402

KST = timezone(timedelta(hours=9))
REF = datetime(2026, 11, 12, 10, 0, tzinfo=KST)


def one(text, ref=REF, **kw):
    found = find_deadlines(text, ref, **kw)
    assert len(found) == 1, f"1개를 기대했지만 {len(found)}개: {found}"
    return found[0]


def at(y, mo, d, h=23, mi=59):
    return datetime(y, mo, d, h, mi, tzinfo=KST)


# ---------------------------------------------------------------- 쉬움

def test_easy_korean_with_weekday_and_time():
    f = one("LAB4 보고서 마감: 11월 18일(수) 23:59")
    assert f.due == at(2026, 11, 18)
    assert f.title_hint == "LAB 4"
    assert f.confidence == "high"
    assert f.notes == []


def test_easy_full_date_and_email_address():
    f = one("과제 3은 2026.11.20 18:00까지 이메일(ta@x.ac.kr)로 제출")
    assert f.due == at(2026, 11, 20, 18, 0)
    assert (f.method, f.email_to) == ("email", "ta@x.ac.kr")
    assert f.title_hint == "과제 3"


def test_easy_english():
    f = one("Please email your final report to cap@x.edu by Nov 30, 11:59 PM.")
    assert f.due == at(2026, 11, 30)
    assert f.method == "email" and f.email_to == "cap@x.edu"


def test_easy_korean_full_with_pm_minutes():
    f = one("LAB5 예비보고서 2026년 11월 22일 오후 11시 59분까지 업로드")
    assert f.due == at(2026, 11, 22)


# ---------------------------------------------------------------- 중간

def test_medium_relative_next_week_friday():
    f = one("중간 프로젝트 보고서는 이메일로 다음 주 금요일까지 보내 주세요.")
    assert f.due == at(2026, 11, 20)
    assert f.method == "email"
    assert any("게시일" in n for n in f.notes)
    assert f.confidence in ("medium", "low")


def test_medium_midnight_is_flagged():
    f = one("마감이 11월 20일 자정으로 연장되었습니다. 메일로 제출해 주세요.")
    assert f.due == at(2026, 11, 20)
    assert any("자정" in n for n in f.notes)
    assert f.method == "email"


def test_medium_numeric_date_lms():
    f = one("Homework 5는 Canvas 과제함으로 제출하세요. 마감은 11/16 18:00입니다.")
    assert f.due == at(2026, 11, 16, 18, 0)
    assert f.method == "lms"


def test_medium_dotted_date_with_trailing_period():
    f = one("보고서 제출: 11. 25.(수) 오후 6시")
    assert f.due == at(2026, 11, 25, 18, 0)


def test_medium_exam_kind():
    f = one("기말고사: 12월 17일 15:00")
    assert f.kind == "시험"
    assert f.due == at(2026, 12, 17, 15, 0)


def test_medium_year_rollover():
    f = one("과제는 1월 5일까지 제출")
    assert f.due == at(2027, 1, 5)
    assert any("2027" in n for n in f.notes)


def test_medium_missing_time_defaults_and_notes():
    f = one("과제 2 마감: 11월 27일")
    assert f.due == at(2026, 11, 27)
    assert any("시간 미기재" in n for n in f.notes)


def test_medium_hour_without_ampm_assumed_pm():
    f = one("과제는 11월 19일 6시까지 제출")
    assert f.due == at(2026, 11, 19, 18, 0)
    assert any("오전/오후" in n for n in f.notes)


# ---------------------------------------------------------------- 어려움

def test_hard_cancelled_class_is_not_deadline():
    assert find_deadlines("11월 14일 휴강입니다. 보강은 추후 공지합니다.", REF) == []


def test_hard_score_is_not_date():
    assert find_deadlines("제출한 과제의 평균은 8.5점입니다.", REF) == []


def test_hard_range_picks_end():
    f = one("과제 2 제출 기간: 11/13 ~ 11/20")
    assert f.due == at(2026, 11, 20)


def test_hard_weekday_restated_not_double_counted():
    f = one("11월 20일 금요일까지 제출")
    assert f.due == at(2026, 11, 20)
    assert f.notes == ["시간 미기재 → 23:59로 둠"]


def test_hard_weekday_mismatch_flagged_not_jumped_year():
    f = one("마감 11월 19일(금) 23:59")
    assert f.due == at(2026, 11, 19)
    assert any("요일 불일치" in n for n in f.notes)
    assert f.confidence != "high"


def test_hard_multiline_title_and_address_from_context():
    f = one("과제 4 안내\n마감: 2026-11-27\n제출처: prof@x.ac.kr")
    assert f.due == at(2026, 11, 27)
    assert f.title_hint == "과제 4"
    assert (f.method, f.email_to) == ("email", "prof@x.ac.kr")


def test_hard_tomorrow_night():
    f = one("오늘 수업 자료 올렸습니다. 과제는 내일 밤 11시까지 메일로 보내세요")
    assert f.due == at(2026, 11, 13, 23, 0)
    assert f.method == "email"


def test_hard_english_relative_with_am():
    f = one("Submit HW 6 by next Monday 9am")
    assert f.due == at(2026, 11, 16, 9, 0)


def test_hard_time_without_date_is_ignored():
    assert find_deadlines("6시까지 제출해 주세요", REF) == []


def test_hard_html_table_syllabus():
    html = ("<table><tr><td>1주차</td><td>9월 13일 오리엔테이션</td></tr>"
            "<tr><td>과제 2</td><td>11월 24일까지 이메일 제출</td></tr>"
            "<tr><td>기말고사</td><td>12월 12일 15:00</td></tr></table>")
    found = find_deadlines(html, REF, is_html=True)
    assert [(f.due, f.kind, f.title_hint) for f in found] == [
        (at(2026, 11, 24), "과제", "과제 2"),
        (at(2026, 12, 12, 15, 0), "시험", "기말고사"),
    ]


def test_hard_table_row_method_does_not_leak_to_other_rows():
    """강의계획서 표: 과제 2 줄의 '이메일 제출'이 옆 줄(기말고사)에 옮겨 붙으면 안 된다."""
    html = ("<table><tr><td>과제 2</td><td>11월 24일까지 이메일 제출</td></tr>"
            "<tr><td>기말고사</td><td>12월 12일 15:00</td></tr></table>")
    hw, final = find_deadlines(html, REF, is_html=True)
    assert hw.method == "email"
    assert final.method == "unknown" and final.email_to is None


def test_hard_context_line_without_date_still_gives_method():
    """날짜 없는 옆 줄('제출: 주소')은 그 마감의 설명이므로 제출 방법을 가져온다."""
    f = one("LAB4 보고서 마감: 11월 18일(수) 23:59\n제출: ta@x.ac.kr 로 PDF 첨부")
    assert (f.method, f.email_to) == ("email", "ta@x.ac.kr")


@pytest.mark.parametrize("html,due,hint", [
    # 실제 고려대 ASIC LearningX 과제 게시판 글 형식(사용자 확인, 2026-11)
    ("<p>Lab3 Assignment</p><p>Submit as a PDF(report) via email : ta@korea.ac.kr</p><p>(due : 23:59 on 11/18)</p>",
     (2026, 11, 18, 23, 59), "LAB 3"),
    ("<p>Midterm Project</p><p>Submit as a PDF via email : ta@korea.ac.kr (with your verilog zip file)</p>"
     "<p>(due : 23:59 on 11/5)</p>", (2026, 11, 5, 23, 59), None),
    ("<p>Lab1 Assignment</p><p>Submit as a PDF via email : ta@korea.ac.kr</p><p>(due : 13:30 on 10/13)</p>",
     (2026, 10, 13, 13, 30), "LAB 1"),
])
def test_real_learningx_assignment_posts(html, due, hint):
    (f,) = find_deadlines(html, datetime(2026, 10, 1, 9, 0, tzinfo=KST) if due[1] == 10 else REF, is_html=True)
    assert f.due == at(*due)
    assert (f.method, f.email_to, f.confidence) == ("email", "ta@korea.ac.kr", "high")
    if hint:
        assert f.title_hint == hint


def test_score_announcement_is_not_a_deadline():
    html = ("<p><b>Scores for Lab0 and Lab1 have been re-announced due to an error in the score file.</b></p>"
            "<p>For any score inquiries or feedback requests, please email ta@korea.ac.kr.</p>")
    assert find_deadlines(html, REF, is_html=True) == []


def test_hard_paren_weekday_numeric():
    f = one("12/24(목) 까지 프로젝트 제출")
    assert f.due == at(2026, 12, 24)


def test_hard_message_level_email_method():
    text = "기말 리포트 안내\n분량은 5쪽입니다.\n마감은 12월 3일 18:00입니다.\n\n제출은 조교 메일 ta2@x.ac.kr 로 해 주세요."
    f = one(text)
    assert (f.method, f.email_to) == ("email", "ta2@x.ac.kr")


# ---------------------------------------------------------------- 보조 함수

@pytest.mark.parametrize("a,b,expected", [
    ("LAB4 보고서", "LAB 4", True),
    ("Homework 5", "HW5", True),
    ("과제 3", "Homework 3", True),
    ("LAB4", "LAB5", False),
    ("기말고사", "Final exam", True),
    ("Weekly log 9", "과제 9", False),
])
def test_same_task(a, b, expected):
    assert same_task(a, b) is expected


def test_title_hint_number_before_word():
    assert title_hint("3차 과제는 메일로") == "과제 3"


def test_html_to_text_keeps_lines():
    assert html_to_text("<p>a&amp;b</p><p>c<br>d</p>") == "a&b\nc\nd"


# ---- 실제 고려대 ASIC 글(2026-11-13 사용자 제보: "퀴즈 같은 시험 일정이 안 보인다")

def test_abbreviated_month_does_not_split_sentence():
    """'Oct. 22'의 마침표에서 문장을 끊으면 날짜가 사라진다(실제 공지 'Announcement about Midterm Week')."""
    text = ("Dear all,\nAs I have mentioned in the last lecture, we will have a short quiz session "
            "(+ explanation on midterm project) in the regular class hour on Oct. 22, 2026 .\n"
            "There is no class on Oct. 20, 2026 so that you can fully work on your programming assignment "
            "that will be given today.\nRegards,")
    found = find_deadlines(text, datetime(2026, 10, 15, 14, 0, tzinfo=KST))
    assert [(f.due.date().isoformat(), f.kind) for f in found] == [("2026-10-22", "시험")]   # 휴강 줄은 마감 아님


@pytest.mark.parametrize("s", ["Sept. 30", "Nov. 5", "Dec. 1", "Jan. 3"])
def test_other_month_abbreviations_keep_date(s):
    found = find_deadlines(f"The final quiz will be held on {s} in class.", REF)
    assert len(found) == 1 and found[0].kind == "시험"


def test_split_keeps_eg_and_ie():
    from lms_agent.extract import split_sentences
    assert split_sentences("Submit the report, e.g. Lab3 results. Then rest.") == [
        "Submit the report, e.g. Lab3 results.", "Then rest."]


def test_midterm_project_is_a_task_not_an_exam():
    """게시판 'Midterm Project' 글의 '(due : 23:59 on 11/5)'는 프로젝트 보고서 마감이지 시험이 아니다."""
    assert title_hint("Midterm Project") == "Midterm Project"
    f = one("Midterm Project\n(due : 23:59 on 11/5)", ref=datetime(2026, 10, 20, 10, tzinfo=KST))
    assert f.kind == "과제" and f.due == at(2026, 11, 5)
    assert title_hint("Midterm quiz") == "Midterm Quiz"
    assert one("Midterm quiz on Nov. 26 in class.").kind == "시험"


# ---- 실제 고려대 로봇공학개론 공지(2026-11-13 사용자 제보: "기말고사 일정 나왔는데 반영 안 됨")
FINAL_NOTICE = ("날짜: 12월 15일\n시간: 10:30am - 11:30am\n장소: 강의실\n허용물품: 필기구, 학생증\n"
                "불가: 계산기를 포함한 모든 전자기기 (적발시 F)")


def test_title_gives_kind_to_field_style_body():
    """'기말고사'는 제목에만 있고 본문은 '날짜: …' / '시간: …' 칸으로 나뉜 공지."""
    found = find_deadlines(FINAL_NOTICE, datetime(2026, 11, 4, 11, 25, tzinfo=KST), title="기말고사 공지")
    assert len(found) == 1
    f = found[0]
    assert f.due == at(2026, 12, 15, 10, 30) and f.kind == "시험" and f.title_hint == "기말고사"
    assert any("제목" in n for n in f.notes)


def test_title_without_kind_does_not_make_a_deadline():
    found = find_deadlines(FINAL_NOTICE, datetime(2026, 11, 4, 11, 25, tzinfo=KST), title="강의실 안내")
    assert found == []


def test_title_kind_needs_field_line_or_single_date():
    """제목이 '기말고사 안내'여도 본문에 날짜가 여럿이고 칸 모양이 아니면 아무 날짜나 시험으로 보지 않는다."""
    body = "11월 20일에 보강이 있습니다.\n12월 1일까지 질문을 받습니다."
    assert find_deadlines(body, REF, title="기말고사 안내") == []
    # 날짜가 하나뿐이면 제목을 따른다
    one = find_deadlines("12월 16일 오후 2시, 공학관 101호", REF, title="Final Exam")
    assert [(f.due, f.kind) for f in one] == [(at(2026, 12, 16, 14, 0), "시험")]


def test_result_titles_do_not_give_a_kind():
    """'중간고사 성적 안내'처럼 결과를 알리는 제목은 시험 일정이 아니다(실제 데모 메시지에서 거짓 시험이 생김)."""
    body = "본인 점수 78점 (평균 65.3, 표준편차 12.1)입니다."
    for title in ("중간고사 성적 안내", "Midterm exam scores", "기말고사 결과 공지", "Quiz 1 grade released"):
        assert find_deadlines(body, REF, title=title) == [], title


@pytest.mark.parametrize("s", ["표준편차 12.1", "평균 11.5, 표준편차 3.2", "sd 12.1", "mean: 10.3", "12.1점", "3.5%",
                               "점수 9.5 / 10"])
def test_decimal_statistics_are_not_dates(s):
    from lms_agent.extract import find_dates
    assert find_dates(s, REF.date()) == [], s


@pytest.mark.parametrize("s", ["제출: 12.1(화)", "마감 12.1 23:59", "due 12/1"])
def test_dotted_dates_still_parse(s):
    from lms_agent.extract import find_dates
    assert len(find_dates(s, REF.date())) == 1, s


# ================================================================ 일반화(docs/CORNER_CASES.md)
# 위의 실제 사례를 고친 규칙이 '같은 모양'의 다른 글에도 맞는지. 다른 학교·다른 과목의 글을 흉내 낸 문장들이다.
from lms_agent.extract import find_dates, split_sentences  # noqa: E402

OCT1 = datetime(2026, 10, 1, 9, 0, tzinfo=KST)


def dues(found):
    return [(f.due, f.kind, f.title_hint) for f in found]


# ---- 가족 1: 마침표가 문장 끝이 아닌 경우(줄임말·이니셜·날짜 속 마침표)

@pytest.mark.parametrize("text,n", [
    ("Office hours with Prof. Kim on Mon. Nov. 16 at 3 p.m. in Room 101.", 1),
    ("Submit Lab 3 by 5 p.m. Late reports get no credit.", 2),          # 'p.m.' 뒤 대문자 낱말 = 새 문장
    ("Bring a pencil, an eraser, etc. The exam starts at 9.", 2),
    ("Read Ch. 5 and Sec. 2.3 (see Fig. 3.4 and Eq. (2.1)) before the quiz.", 1),
    ("The U.S. holiday moves the quiz, i.e. it will be on Nov. 27.", 1),
    ("Write approx. 5 pages vs. the usual 3.", 1),
    ("Lab 3 is due Nov. 20. Lab 4 is due Nov. 27.", 2),                 # 숫자로 끝난 문장 + 대문자 낱말
    ("보고서는 2026. 11. 20.(금)까지 제출. 늦으면 감점합니다.", 2),
    ("Dr. Lee will proctor the exam. Bring your ID.", 2),
    ("The quiz is on Thu. Oct. 22. See you then.", 1),                 # 숫자 뒤는 양쪽에 날짜가 있을 때만 끊는다
    ("Quiz on Thu. Oct. 22. Final exam on Tue. Dec. 15.", 2),
    ("Submit by 5 P.M. Friday.", 1),                                    # 'P.M.' 뒤가 요일 → 이어진다
    ("J. Smith and Mr. Park will grade the report.", 1),
])
def test_split_sentences_general(text, n):
    assert len(split_sentences(text)) == n, split_sentences(text)


@pytest.mark.parametrize("text,ref,due,kind", [
    ("Report due 22 Oct. 2026 at noon.", OCT1, (2026, 10, 22, 12, 0), "과제"),
    ("Quiz on Thu., Oct. 22 in class.", OCT1, (2026, 10, 22, 23, 59), "시험"),
    ("레포트 제출 기한: 10. 22.(목) 23:59", OCT1, (2026, 10, 22, 23, 59), "과제"),
    ("기말 보고서 제출: 2026. 12. 18. 오후 6시", REF, (2026, 12, 18, 18, 0), "과제"),
    ("Homework 4 is due Tues. Nov. 17 by 11:59 p.m. Late submissions lose 10%.", REF, (2026, 11, 17, 23, 59), "과제"),
    ("The midterm is on Wed., Nov. 25 (see Ch. 3 and Fig. 2.1).", REF, (2026, 11, 25, 23, 59), "시험"),
    ("Prof. Kim: the final exam will be held on Dec. 15 at 10:30 a.m. in Room 101.", REF, (2026, 12, 15, 10, 30), "시험"),
    ("When: Tuesday, Dec. 15, 10:30 AM — final exam", REF, (2026, 12, 15, 10, 30), "시험"),
])
def test_abbreviations_and_date_formats_keep_the_date(text, ref, due, kind):
    f = one(text, ref=ref)
    assert (f.due, f.kind) == (at(*due), kind)
    assert not any("요일 불일치" in n for n in f.notes)


def test_two_deadlines_in_two_english_sentences_on_one_line():
    found = find_deadlines("Lab 3 is due Nov. 20. Lab 4 is due Nov. 27.", REF)
    assert dues(found) == [(at(2026, 11, 20), "과제", "LAB 3"), (at(2026, 11, 27), "과제", "LAB 4")]
    # 뒤 문장에 날짜가 없으면 잇는다: 제출 낱말이 뒤 문장에만 있어도 마감을 잃지 않는다
    f = one("Lab 3: Nov. 20. Submit via email to ta@x.edu.")
    assert (f.due, f.kind, f.method) == (at(2026, 11, 20), "과제", "email")


def test_weekday_written_before_english_date_is_checked():
    f = one("Quiz on Fri., Oct. 22 in class.", ref=OCT1)     # 2026-10-22는 목요일
    assert f.due == at(2026, 10, 22) and any("요일 불일치" in n for n in f.notes)


# ---- 가족 2: 학기 시점 낱말(중간·기말) + 종류 낱말 — 마지막 낱말이 종류를 정한다

@pytest.mark.parametrize("text,kind,hint", [
    ("Final Report due Dec. 18 at 6 pm", "과제", "Final Report"),
    ("Midterm Presentation: Nov. 19 in class", "과제", "Midterm Presentation"),
    ("기말 프로젝트 발표: 12월 10일 수업 시간", "과제", "기말 프로젝트 발표"),
    ("중간 보고서 제출 마감은 11월 20일입니다", "과제", "중간 보고서"),
    ("중간고사 대체 과제는 11월 27일까지 제출하세요", "과제", "중간고사 대체 과제"),
    ("Quiz 3 will be on Nov. 19", "시험", "Quiz 3"),
    ("There will be a pop quiz on Nov. 19 in class.", "시험", "Quiz"),
    ("The midterm exam is on Nov. 25.", "시험", "Midterm Exam"),
    ("Final project report is due Dec. 18.", "과제", "Final Project Report"),
    ("기말고사는 12월 15일 10시입니다.", "시험", "기말고사"),
    ("Midterm take-home exam due Nov. 20 at noon", "시험", "Midterm Take-Home Exam"),
])
def test_term_word_plus_kind_word(text, kind, hint):
    f = one(text)
    assert (f.kind, f.title_hint) == (kind, hint)


@pytest.mark.parametrize("text", [
    "Final Exam Review session on Dec. 10 at 3 pm in Room 101.",       # 복습 세션(시험 아님)
    "Midterm grades will be posted by Nov. 20.",                        # 성적 게시
    "중간고사 성적 확인 기간: 11월 20일까지",
    "기말고사 범위 설명회는 12월 1일 오후 3시입니다.",
    "Midterm week: no class on Oct. 20.",
    "The presentation slides from the Nov. 3 lecture are uploaded.",
    "중간고사 성적 발표: 11월 20일",                                     # '발표' = 성적 알림
    "Final exam solutions will be uploaded on Dec. 20.",
])
def test_exam_word_as_modifier_is_not_an_exam(text):
    assert find_deadlines(text, REF) == [], text


def test_exam_after_scope_phrase_still_found():
    f = one("기말고사 범위는 1~7장이고, 기말고사는 12월 15일입니다.")
    assert (f.due, f.kind, f.title_hint) == (at(2026, 12, 15), "시험", "기말고사")


@pytest.mark.parametrize("a,b,expected", [
    ("Midterm Project", "Midterm Exam", False),     # 같은 '중간'이어도 하나는 프로젝트, 하나는 시험
    ("Midterm Project", "Midterm project report", True),
    ("Final Report", "기말 보고서", True),
    ("Final Project", "Final Exam", False),
    ("Midterm", "중간고사", True),
    ("중간고사 대체 과제", "중간고사", False),       # 머리가 '과제'
])
def test_same_task_term_needs_same_head(a, b, expected):
    assert same_task(a, b) is expected


# ---- 가족 3: 종류는 제목에만, 본문은 칸(날짜·시간·장소)

@pytest.mark.parametrize("title,body,html,due", [
    ("기말고사 공지", "일시: 12/15(화) 10:30~11:30\n장소: 공학관 101호", False, (2026, 12, 15, 10, 30)),
    ("Final Exam", "Date: Dec 15 / Time: 10:30-11:30 / Room: 101", False, (2026, 12, 15, 10, 30)),
    ("Final Exam Information", "When: Tuesday, Dec. 15, 10:30 AM\nWhere: Room 101", False, (2026, 12, 15, 10, 30)),
    ("중간고사 안내", "날짜: 11월 25일(수)\n시간: 10시 30분 ~ 11시 30분", False, (2026, 11, 25, 10, 30)),
    ("Midterm", "시간: 오후 2시부터\n날짜: 11월 25일", False, (2026, 11, 25, 14, 0)),       # 시간 칸이 먼저
    ("기말고사 공지", "<table><tr><td>날짜</td><td>12/15</td></tr><tr><td>시간</td><td>10:30</td></tr></table>",
     True, (2026, 12, 15, 10, 30)),
    ("기말고사 공지", "날짜 | 12월 15일\n시간 | 10:30am\n장소 | 강의실", False, (2026, 12, 15, 10, 30)),
    ("Quiz 2 notice", "Date: Nov. 19 (Thu)\nTime: 9:00 a.m.", False, (2026, 11, 19, 9, 0)),
    ("시험 안내", "시험 일시 : 2026년 12월 15일(화) 오전 10시 30분", False, (2026, 12, 15, 10, 30)),
])
def test_title_kind_with_field_style_body_variants(title, body, html, due):
    found = find_deadlines(body, REF, is_html=html, title=title)
    assert [(f.due, f.kind) for f in found] == [(at(*due), "시험")], found


@pytest.mark.parametrize("title,body", [
    ("기말고사 범위 안내", "12월 10일 복습 세션에서 범위를 설명합니다."),
    ("기말고사 범위 안내", "범위: 1~7장\n질문은 12월 10일까지 받습니다."),
    ("중간고사 성적 공지", "날짜: 11월 20일\n성적 이의 신청은 조교에게."),
    ("Midterm exam results", "Date: Nov. 20\nAverage: 72.5"),
    ("기말고사 공지", "12월 8일 수업은 휴강입니다."),
    ("Final Exam Review", "Date: Dec. 10\nTime: 3 pm"),
    ("Announcement about Midterm Week", "There is no class on Oct. 20, 2026."),
    ("기말고사 공지", "Lab 5 보고서는 12월 1일에 나갑니다."),     # 그 줄에 다른 종류(실습 보고서)가 있다
])
def test_title_kind_does_not_create_bogus_exams(title, body):
    assert find_deadlines(body, REF, title=title) == [], (title, body)


# ---- 가족 4: 점 숫자·빗금 숫자가 날짜가 아닌 경우(통계·버전·절 번호·점수·학점)

@pytest.mark.parametrize("s", [
    "평균 11.5점", "avg 9.25", "median: 8.5", "상위 12.5%", "v1.2", "version 2.1", "1.2절", "Fig. 3.4", "Eq. (2.1)",
    "점수 9.5/10", "GPA 3.8/4.5", "Section 3.2", "Table 2.1", "그림 3.4", "식 (2.1)", "Problem 2.3", "score: 9/10",
    "Lab 2.1", "Exercise 4.12", "표 1.3", "문제 3.2",
])
def test_numbers_that_are_not_dates(s):
    assert find_dates(s, REF.date()) == [], s


@pytest.mark.parametrize("text,due", [
    ("Submit Problem 2.3 by Friday", (2026, 11, 13)),
    ("Read Section 1.2 before the quiz on 11/19.", (2026, 11, 19)),
    ("Submit v1.2 of the report by 11/20", (2026, 11, 20)),
    ("Quiz 2 on Nov. 19 at 10.30 am", (2026, 11, 19, 10, 30)),           # 점으로 쓴 시각
    ("과제는 11.30까지 제출", (2026, 11, 30)),
])
def test_identifiers_do_not_steal_the_date(text, due):
    f = one(text)
    assert f.due == at(*due)


def test_dotted_time_range_after_a_date_is_time_not_dates():
    f = one("시험 일시: 12/15 10.30~11.30")
    assert (f.due, f.kind) == (at(2026, 12, 15, 10, 30), "시험")     # 10.30·11.30을 10/30·11/30으로 읽지 않는다
    assert one("제출 기간: 11.13 ~ 11.20").due == at(2026, 11, 20)  # 날짜 범위는 그대로


def test_quiz_score_fraction_is_not_a_deadline():
    assert find_deadlines("Quiz 1 score: 9/10", REF) == []
    assert find_deadlines("Your midterm score is 85.5 (avg 72.25, median 74.5).", REF) == []
