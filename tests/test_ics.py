import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lms_agent.ics import _fold, build_ics, validate_ics  # noqa: E402

KST = timezone(timedelta(hours=9))


def item(**kw):
    base = {"id": "f-abc", "course_name": "아주 긴 과목 이름이 들어가는 반도체 소자 공학 특론", "title": "LAB4 보고서",
            "due": "2026-11-20T23:59:00+09:00", "method": "email", "email_to": "ta@x.ac.kr", "kind": "과제",
            "submitted": False, "notes": ["'자정'은 그날 밤 23:59로 둠; 확인, 필요"],
            "evidence": [{"text": "마감: 11월 20일 자정, 메일로", "source_type": "announcement",
                          "source_title": "LAB4 안내", "url": "https://c.example/a/1", "posted_at": None}],
            "url": None}
    base.update(kw)
    return base


def test_valid_and_utc():
    text = build_ics([item(), item(id="canvas-1-2", method="lms", submitted=True)],
                     now=datetime(2026, 11, 12, tzinfo=KST))
    assert validate_ics(text) == []
    assert "DTSTART:20261120T145900Z" in text
    unfolded = text.replace("\r\n ", "")
    assert "SUMMARY:✔ [" in unfolded and "(메일 제출)" in unfolded
    assert "\\;" in unfolded and "\\," in unfolded  # 세미콜론·쉼표 이스케이프
    assert "URL:https://c.example/a/1" in unfolded


def test_fold_never_splits_utf8():
    line = "DESCRIPTION:" + "가나다라마바사" * 30
    parts = _fold(line)
    assert all(len(p.encode("utf-8")) <= 75 for p in parts)
    assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == line


def test_items_without_due_skipped():
    text = build_ics([item(due=None)])
    assert "BEGIN:VEVENT" not in text and validate_ics(text) == []


def test_validator_catches_problems():
    assert validate_ics("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert validate_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
