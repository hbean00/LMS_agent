"""첨부 파일의 'Assignment' 중복(계약 26-5, 사용자 제보 2026-11-14: "로봇공학개론은 Report·Project 2만 있어야 하는데
'Assignment' 칸이 더 생겼다").

원인: v0.7.2부터 과제 설명·공지에 링크된 PDF도 마감을 찾는데, PDF 출처에는 글 제목(source_title)도 과제 연결(own)도 없어서
파일 속 'Assignment due …' 문장이 제목 'Assignment'인 따로 된 항목이 됐다.
고침: 첨부 PDF의 후보는 그 파일이 붙은 글(attached_to)을 출처처럼 다룬다 — 과제에 붙은 파일이면 그 과제가 own(같은 날 → 건너뜀,
마감 없는 과제 → 연결, 다른 날 → 같은 과제의 다른 마감), 공지·게시판에 붙은 파일이면 그 글 제목을 source_title로.
번호 없는 일반 낱말(Assignment·과제·HW·Homework·Report …)이면 그 글/과제 제목으로 같은 과제인지 본다.
과목·파일·문장은 모두 지어낸 것이고, 날짜는 오늘 기준 상대 날짜다(CI는 UTC)."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

from datetime import datetime, time, timedelta

import pytest

from lms_agent import core
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, PRELAB_FILE, start_mock_canvas, tiny_pdf
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

HOST = "lms.example.test"
TZ = local_tz()
T = now_local().date()
CID = "501"


def at(days, h=23, mi=59):
    return datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ)


def z(days, h=23, mi=59):
    return iso(at(days, h, mi))


def md(days) -> str:
    d = T + timedelta(days=days)
    return f"{d.month}/{d.day}"


def asg(aid, name, due_days, fid=None):
    desc = f'<p>안내 자료: <a href="/courses/{CID}/files/{fid}">{name}.pdf</a></p>' if fid else ""
    return {"id": aid, "course_id": CID, "name": name, "description": desc,
            "due_at": z(due_days) if due_days is not None else None,
            "submission_types": ["online_upload"], "created_at": z(-20, 9), "updated_at": z(-20, 9),
            "html_url": f"https://{HOST}/courses/{CID}/assignments/{aid}",
            "submission": {"workflow_state": "unsubmitted", "submitted_at": None}}


def att(fid, filename):
    return {"id": f"cf{fid}", "filename": filename, "filesize": 100, "canvas_file_id": fid,
            "url": f"/files/{fid}/download"}


def post(pid, title, atts, url=None):
    return {"id": pid, "title": title, "content": "", "created_at": z(-20, 9), "updated_at": z(-20, 9),
            "post_url": url, "attachments": atts}


def board(board_id, title, posts):
    return {"course_id": CID, "tool_id": None, "board_id": board_id, "board_title": title, "source": "canvas",
            "posts": posts}


def pdf(fid, filename, text, board_id, post_id):
    return {"text": text, "course_id": CID, "filename": filename, "post_key": f"board:{board_id}-{post_id}",
            "board_id": board_id, "post": post_id, "canvas_file_id": fid, "posted_at": z(-20, 9)}


def generic_text(word: str, days: int) -> str:
    return f"Project guide\n{word} due: {md(days)} 23:59\nSubmit the report as a single PDF file."


def course_cache(assignments, announcements=(), boards=(), pdf_texts=None) -> dict:
    return {"fetched_at": z(0, 10), "courses": [{"id": CID, "name": "로봇공학개론", "course_code": "ME501",
                                                 "syllabus_body": None}],
            "assignments": list(assignments), "announcements": list(announcements), "conversations": [],
            "errors": [], "boards": list(boards), "pdf_texts": dict(pdf_texts or {})}


def robotics(word="Assignment", p2_pdf_days=6) -> dict:
    """사용자의 과목 모양: Canvas 과제 Project1·Project2·Report(각자 설명에 안내 PDF 링크) + 공지의 기말고사.
    PDF마다 번호 없는 'Assignment due …' 문장(Project2만 p2_pdf_days, 나머지는 Canvas 마감과 같은 날)."""
    assignments = [asg(5011, "Project1", 3, 7011), asg(5012, "Project2", 6, 7012), asg(5013, "Report", 10, 7013)]
    final = T + timedelta(days=20)
    announcements = [{"id": 1, "title": "기말고사 안내", "posted_at": z(-3, 10), "context_code": f"course_{CID}",
                      "html_url": f"https://{HOST}/courses/{CID}/discussion_topics/1",
                      "message": f"<p>기말고사: {final.month}월 {final.day}일 15:00</p>"}]
    boards = [board("assignments", "과제", [
        post(5011, "Project1", [att(7011, "project1.pdf")], f"https://{HOST}/courses/{CID}/assignments/5011"),
        post(5012, "Project2", [att(7012, "project2-1.pdf")], f"https://{HOST}/courses/{CID}/assignments/5012"),
        post(5013, "Report", [att(7013, "report-guide.pdf")], f"https://{HOST}/courses/{CID}/assignments/5013")])]
    texts = {f"{CID}:cf7011": pdf(7011, "project1.pdf", generic_text(word, 3), "assignments", 5011),
             f"{CID}:cf7012": pdf(7012, "project2-1.pdf", generic_text(word, p2_pdf_days), "assignments", 5012),
             f"{CID}:cf7013": pdf(7013, "report-guide.pdf", generic_text(word, 10), "assignments", 5013)}
    return course_cache(assignments, announcements, boards, texts)


def build(cache):
    items, _, _ = core.build_items(cache, [])
    return items


def view(tmp_path, cache):
    s = Store(tmp_path / "state.json")
    s.data["cache"] = cache
    core.rebuild(s)
    return s.view()


def found(items):
    return [it for it in items if it["origin"] == "found"]


# ---------------------------------------------------------------- 과제에 붙은 PDF

@pytest.mark.parametrize("word", ["Assignment", "HW", "Homework", "Report", "과제", "숙제", "보고서"])
def test_assignment_pdf_same_day_is_not_a_new_item(tmp_path, word):
    """사용자 제보 그대로: 화면에는 Canvas 과제 셋 + 기말고사만, 제목이 'Assignment' 같은 일반 낱말인 칸은 없다."""
    rows = view(tmp_path, robotics(word))
    assert sorted(v["title"] for v in rows if v["status"] != "duplicate") == ["Project1", "Project2", "Report", "기말고사"]
    assert not [v for v in found(rows) if v["title"] == word]
    assert not [v for v in found(rows) if any(e["source_type"] == "pdf" for e in v["evidence"])]  # 과제 설명과 같게 건너뜀


def test_assignment_pdf_different_day_is_same_task_conflict(tmp_path):
    """같은 과제의 PDF가 Canvas와 다른 날을 적었다 → 'Project2'의 다른 마감(따로 된 'Assignment' 아님)."""
    items = build(robotics(p2_pdf_days=8))
    (f,) = [it for it in found(items) if any(e["source_type"] == "pdf" for e in it["evidence"])]
    assert f["title"] == "Project2" and parse_iso(f["due"]) == at(8)
    assert f["duplicate_of"] is None and f["linked_to"] is None
    assert f["conflict"] and "Project2" in f["conflict"]
    assert any("Project2" in n and md(6) in n for n in f["notes"])     # 화면(store.view)은 notes로 보인다
    rows = view(tmp_path, robotics(p2_pdf_days=8))
    v = next(v for v in rows if v["id"] == f["id"])
    assert v["status"] == "review" and any("Project2" in n for n in v["notes"])
    assert not [v for v in rows if v["title"] == "Assignment"]


def test_assignment_pdf_without_canvas_due_links_to_it():
    """마감 없는 Canvas 과제에 붙은 PDF의 일반 문장 → 그 과제에 연결(과제 설명과 같게)."""
    cache = robotics()
    cache["assignments"][2]["due_at"] = None                     # Report: Canvas 마감 없음
    items = build(cache)
    (f,) = [it for it in found(items) if it["linked_to"] == f"canvas-{CID}-5013"]
    assert f["title"] == "Report" and parse_iso(f["due"]) == at(10)
    assert f"canvas-{CID}-5013" not in {it["id"] for it in items}  # '마감 없음' 칸은 후보와 연결돼 빠진다


def test_specific_hint_in_assignment_pdf_stays_its_own_item():
    """과제에 붙은 PDF라도 번호가 있는 다른 과제('Lab 3 report')는 그 이름 그대로 — 붙은 과제로 바꾸지 않는다."""
    cache = robotics()
    cache["pdf_texts"][f"{CID}:cf7012"]["text"] = f"Schedule\nLab 3 report due: {md(8)} 23:59"
    items = build(cache)
    (f,) = [it for it in found(items) if parse_iso(it["due"]) == at(8)]
    assert f["title"] != "Project2" and "3" in f["title"]
    assert not any("Project2" in n for n in f["notes"]) and f["conflict"] is None


# ---------------------------------------------------------------- 공지·게시판에 붙은 PDF

def _announced(days):
    """Canvas 과제 Project2(6일 뒤) + 공지 'Project 2 안내'(본문은 파일 링크뿐)에 붙은 PDF의 'Assignment due …'."""
    cache = course_cache(
        [asg(5012, "Project2", 6)],
        [{"id": 2, "title": "Project 2 안내", "posted_at": z(-2, 10), "context_code": f"course_{CID}",
          "html_url": f"https://{HOST}/courses/{CID}/discussion_topics/2",
          "message": f'<p><a href="/courses/{CID}/files/7021">project2-guide.pdf</a></p>'}],
        [board("announcements", "공지", [post(2, "Project 2 안내", [att(7021, "project2-guide.pdf")],
                                              f"https://{HOST}/courses/{CID}/discussion_topics/2")])],
        {f"{CID}:cf7021": pdf(7021, "project2-guide.pdf", generic_text("Assignment", days), "announcements", 2)})
    return cache


def test_announcement_pdf_same_day_is_duplicate(tmp_path):
    items = build(_announced(6))
    (f,) = found(items)
    assert f["duplicate_of"] == f"canvas-{CID}-5012" and f["title"] != "Assignment"
    rows = view(tmp_path, _announced(6))
    assert next(v for v in rows if v["id"] == f["id"])["status"] == "duplicate"
    assert [v["title"] for v in rows if v["status"] != "duplicate"] == ["Project2"]


def test_announcement_pdf_different_day_conflicts_with_canvas():
    items = build(_announced(9))
    (f,) = found(items)
    assert f["duplicate_of"] is None and f["title"] == "Project2" and parse_iso(f["due"]) == at(9)
    assert f["conflict"] and any("Project2" in n and md(6) in n for n in f["notes"])


def test_pdf_on_announcement_and_assignment_belongs_to_assignment():
    """같은 파일이 공지와 과제 설명 둘 다에 링크(공지 쪽으로 한 번만 받음) → 과제가 own → 같은 날이면 건너뜀."""
    cache = _announced(6)
    cache["boards"].append(board("assignments", "과제", [post(5012, "Project2", [att(7021, "project2-guide.pdf")])]))
    assert found(build(cache)) == []


def test_generic_announcement_without_task_title_keeps_hint():
    """글 제목도 과제를 가리키지 않으면(예: '자료 안내') 알 수 있는 게 없다 — 지금처럼 후보로 남는다(지어내지 않음)."""
    cache = _announced(9)
    cache["announcements"][0]["title"] = "자료 안내"
    cache["boards"][0]["posts"][0]["title"] = "자료 안내"
    (f,) = found(build(cache))
    assert f["duplicate_of"] is None and f["linked_to"] is None and "Project2" not in f["title"]


# ---------------------------------------------------------------- 실제 동기화 경로(가짜 Canvas → 받기 → PDF 글자)

@pytest.mark.parametrize("days,expect", [(10, "skip"), (12, "conflict")])
def test_demo_assignment_pdf_full_sync(tmp_path, days, expect):
    """데모 'LAB5 예비보고서'(10일 뒤 마감) 설명에 링크된 PDF에 번호 없는 'Assignment due' 문장."""
    mock, base = start_mock_canvas()
    try:
        mock.data["files"][PRELAB_FILE] = tiny_pdf(["Lab 5 Prelab Report Guide", f"Assignment due: {md(days)} 23:59",
                                                    "Submit the prelab report as a single PDF file on Canvas."])
        s = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), s, **core.sync_options(s, demo_home=tmp_path))
    finally:
        mock.shutdown()
    assert s.data["pdf_texts"][f"101:cf{PRELAB_FILE}"]["board_id"] == "assignments"
    rows = s.view()
    assert not [v for v in rows if v["title"] == "Assignment"]
    mine = [v for v in rows if v["origin"] == "found"
            and any(e["source_type"] == "pdf" and "Lab5_prelab" in (e.get("source_title") or "") for e in v["evidence"])]
    if expect == "skip":
        assert mine == []
    else:
        (v,) = mine
        assert v["title"] == "LAB5 예비보고서" and parse_iso(v["due"]) == at(days) and v["status"] == "review"
        assert any("LAB5 예비보고서" in n for n in v["notes"])
