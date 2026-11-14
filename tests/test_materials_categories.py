"""자료 화면 분류(계약 24-4): 가상 게시판은 post_key로, 그 밖은 게시판 이름 낱말로 — 특정 과목 이름에 기대지 않는다."""
import pytest

from lms_agent.features.materials import CATEGORIES, category_of


@pytest.mark.parametrize("post_key, board, want", [
    ("board:announcements-55", "공지", "announcement"),            # 공지 본문에 링크된 파일
    ("board:assignments-1012", "과제", "assignment"),              # 과제 설명에 링크된 파일(24-3)
    ("board:syllabus-101", "강의계획서", "syllabus"),
    ("board:b7-3", "과제 제출", "assignment"),
    ("board:b7-3", "Assignments", "assignment"),
    ("board:b7-3", "Lab 자료", "assignment"),
    ("board:b7-3", "강의자료실", "lecture"),
    ("board:b7-3", "Lecture Slides", "lecture"),
    ("board:b7-3", "수업 자료", "lecture"),
    ("board:b7-3", "공지사항", "announcement"),
    ("board:b7-3", "Notice", "announcement"),
    ("board:b7-3", "Syllabus", "syllabus"),
    ("board:b7-3", "자유게시판", "board"),
    ("board:b7-3", "Q&A", "board"),
    (None, None, "board"),
])
def test_category_of(post_key, board, want):
    assert category_of(post_key, board) == want


def test_categories_list_is_stable():
    assert [c for c, _ in CATEGORIES] == ["assignment", "announcement", "lecture", "syllabus", "board"]
    assert all(label for _, label in CATEGORIES)
