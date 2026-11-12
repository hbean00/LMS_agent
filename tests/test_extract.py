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
