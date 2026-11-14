"""과제 설명·강의계획서에 링크된 Canvas 파일(계약 24-3, 사용자 제보 2026-11-14: "project 2 … 첨부파일은 못 보는 문제").

공지 본문만 보던 것을 과제 설명(assignments[].description)·강의계획서(syllabus_body)까지 넓혔다:
과목마다 가상 게시판(source "canvas") "announcements"·"assignments"·"syllabus" → materials가 받고 → pdf_texts → pdf_source·
에이전트 검색·체크리스트. 가짜 Canvas(mock_canvas)만 쓴다(네트워크 없음). 과목·파일·문장은 모두 지어낸 것."""
from datetime import datetime, time, timedelta

import pytest

from lms_agent import agent, checklist, core, materials
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, PRELAB_FILE, SCORE_FILES, start_mock_canvas, tiny_pdf
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

HOST = "lms.example.test"
TZ = local_tz()
T = now_local().date()


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


class _Bare:
    """sync_options가 읽는 설정만 있는 가짜 store(자동 받기는 끔 — 받을 폴더는 테스트가 정한다)."""
    data = {"settings": {"auto_download": False}}


def pdf_reader():
    """앱이 쓰는 PDF 글자 뽑기(pdftext, 표준 라이브러리)."""
    return core.sync_options(_Bare())["pdf_reader"]


def files_api(mock) -> list[str]:
    return [p for p in mock.requests if p.startswith("/api/v1/files/")]


def virtual(cache, board_id, course="101") -> dict | None:
    return next((b for b in cache["boards"] if b.get("source") == "canvas" and b["board_id"] == board_id
                 and b["course_id"] == course), None)


def add_file(mock, fid, name, lines):
    """lines: PDF 글 줄(목록) 또는 파일 바이트 그대로."""
    mock.data["files"][fid] = lines if isinstance(lines, bytes) else tiny_pdf(lines)
    mock.data["file_names"][fid] = name


def assignment(mock, aid, course="101"):
    return next(a for a in mock.data["assignments"][course] if a["id"] == aid)


@pytest.fixture()
def env(tmp_path):
    mock, base = start_mock_canvas()
    yield mock, base, Store(tmp_path / "state.json")
    mock.shutdown()


# ---------------------------------------------------------------- 링크 모양

@pytest.mark.parametrize("html,ids", [
    (f'<a href="https://{HOST}/courses/101/files/8201?wrap=1">a.pdf</a>', [8201]),                 # 절대
    ('<a href="/courses/101/files/8201/download?download_frd=1">a.pdf</a>', [8201]),                # 상대 + download
    ('<a href="/files/77/download?verifier=abc&amp;wrap=1">x</a>', [77]),
    ('<img src="/api/v1/files/55">', [55]),
    (f'<a class="instructure_file_link" href="https://{HOST}/courses/101/files/31?wrap=1" '
     f'data-api-endpoint="https://{HOST}/api/v1/courses/101/files/31">b</a>', [31]),               # 같은 파일 두 번 → 한 번
    ('<a href="/courses/101/files?preview=66">c</a> <a href="/courses/101/files/?sort=name&amp;preview=67">d</a>', [66, 67]),
    ('<a href="/groups/5/files/88/preview">g</a> <a href="/users/3/files/99">u</a>', [88, 99]),
    (f'<a href="//{HOST}/courses/1/files/12">p</a>', [12]),                                         # 프로토콜 생략
    ("자료: https://alias.example.edu/courses/101/files/41/download", [41]),                       # 다른 호스트지만 Canvas 모양
    ("<p>본문 https://lms.example.test/courses/101/files/42</p>", [42]),                           # 링크가 아닌 글 속 주소
    ('<a href="https://cdn.example.com/files/123">x</a>', []),                                      # 다른 사이트의 /files/<n>
    (f'<a href="https://{HOST}/courses/101/pages/files/7">x</a>', []),                             # 경로 가운데의 files
    ('<a href="/courses/101/files/folder/week1">폴더</a>', []),                                     # 폴더(파일 id 없음)
    ('<a href="courses/101/files/5">x</a>', []),                                                    # 앞에 /가 없는 상대 주소
    ('<a href="/courses/101/files/9/download">1</a><a href="/courses/101/files/8/download">2</a>'
     '<a href="/courses/101/files/9">1 again</a>', [9, 8]),                                         # 나온 순서, 겹침 없음
    ("", []), (None, []),
])
def test_file_link_shapes(html, ids):
    assert core.file_links(html, HOST) == ids


def test_foreign_host_without_canvas_host_is_kept():
    """Canvas 주소를 모를 때(host None)는 지금처럼 모든 모양을 센다."""
    assert core.file_links('<a href="https://cdn.example.com/files/123">x</a>') == [123]


# ---------------------------------------------------------------- 수집

def _links(mock, base):
    """과제 설명(절대·상대 두 모양)과 강의계획서(103)에 파일 링크를 넣는다. 공지 9007의 점수표 8101을 과제에도 링크(겹침)."""
    add_file(mock, 8301, "project2-1.pdf", ["Project 2 guide", "Submit the final report as a PDF file by email.",
                                            "Include the source code in a zip file."])
    add_file(mock, 8302, "project2-template.docx", b"PK-zip-not-a-pdf")
    add_file(mock, 8303, "Syllabus_EE361.pdf", ["Course policy", "Late submissions will receive a 20% penalty."])
    a = assignment(mock, 1011)
    a["description"] = (f'<p>Project 2 안내 <a class="instructure_file_link" href="{base}/courses/101/files/8301?wrap=1" '
                        f'data-api-endpoint="{base}/api/v1/courses/101/files/8301">project2-1.pdf</a></p>'
                        '<p>양식: <a href="/courses/101/files/8302/download?download_frd=1">project2-template.docx</a></p>'
                        f'<p>점수표: <a href="/courses/101/files/{SCORE_FILES[0]}/download">Lab0_score.pdf</a></p>')
    c103 = next(c for c in mock.data["courses"] if c["id"] == 103)
    c103["syllabus_body"] += '<p>강의 규정: <a href="/courses/103/files/8303/download?wrap=1">Syllabus_EE361.pdf</a></p>'


def test_assignment_and_syllabus_links_collected(env):
    mock, base, store = env
    _links(mock, base)
    core.sync(Canvas(base, DEMO_TOKEN), store)
    cache = store.data["cache"]
    assert cache["errors"] == []

    ann = virtual(cache, "announcements")
    assert (ann["board_title"], ann["tool_id"], [p["id"] for p in ann["posts"]]) == ("공지", None, [9007])
    assert [a["id"] for a in ann["posts"][0]["attachments"]] == [f"cf{SCORE_FILES[0]}", f"cf{SCORE_FILES[1]}"]  # 지금 그대로

    asg = virtual(cache, "assignments")
    assert asg["board_title"] == "과제"
    posts = {p["id"]: p for p in asg["posts"]}
    assert sorted(posts) == [1011, 1013]                     # 1013 = 데모 LAB5 예비보고서(Lab5_prelab.pdf)
    p = posts[1011]
    src = assignment(mock, 1011)
    assert (p["title"], p["post_url"], p["content"]) == ("LAB3 보고서", src["html_url"], "")
    assert parse_iso(p["created_at"]) == parse_iso(src["created_at"]) and parse_iso(p["updated_at"]) == parse_iso(src["updated_at"])
    assert [(a["id"], a["filename"], a["canvas_file_id"]) for a in p["attachments"]] == [
        ("cf8301", "project2-1.pdf", 8301), ("cf8302", "project2-template.docx", 8302),
        (f"cf{SCORE_FILES[0]}", "Lab0_score.pdf", SCORE_FILES[0])]
    assert all(a["filesize"] and "verifier=" in a["url"] for a in p["attachments"])
    assert [a["filename"] for a in posts[1013]["attachments"]] == ["Lab5_prelab.pdf"]

    syl = virtual(cache, "syllabus", "103")
    assert syl["board_title"] == "강의계획서" and virtual(cache, "syllabus") is None   # 101은 강의계획서가 없음
    (sp,) = syl["posts"]
    assert (sp["id"], sp["title"], sp["post_url"]) == ("103", "강의계획서", f"{base}/courses/103/assignments/syllabus")
    assert [a["filename"] for a in sp["attachments"]] == ["Syllabus_EE361.pdf"]

    # 파일 API는 파일마다 한 번(공지와 과제에 함께 링크된 점수표도 한 번)
    asked = files_api(mock)
    assert sorted(asked) == sorted(set(asked)) and len(asked) == 6
    # 본문은 각 출처가 읽는다 — board 출처로 두 번 읽지 않는다
    keys = {f"{s['type']}:{s['id']}" for s in core._sources(cache, [], now_local())}
    assert "assignment:1011" in keys and not any(k.startswith("board:assignments") or k.startswith("board:syllabus")
                                                 for k in keys)


def test_incremental_reuses_known_files(env):
    mock, base, store = env
    _links(mock, base)
    core.sync(Canvas(base, DEMO_TOKEN), store)
    before = [b for b in store.data["cache"]["boards"] if b.get("source") == "canvas"]
    mock.requests.clear()
    r = core.sync(Canvas(base, DEMO_TOKEN), store)
    assert files_api(mock) == [] and r["checked"] == 0               # 아는 파일은 다시 묻지 않는다
    assert [b for b in store.data["cache"]["boards"] if b.get("source") == "canvas"] == before

    # 과제 설명에 링크가 하나 더 → 그 파일만 묻는다
    add_file(mock, 8304, "project2-2.pdf", ["Part 2"])
    a = assignment(mock, 1013)
    a["description"] += '<p><a href="/courses/101/files/8304/download">project2-2.pdf</a></p>'
    mock.requests.clear()
    core.sync(Canvas(base, DEMO_TOKEN), store)
    assert files_api(mock) == ["/api/v1/files/8304"]
    p = next(p for p in virtual(store.data["cache"], "assignments")["posts"] if p["id"] == 1013)
    assert [a["filename"] for a in p["attachments"]] == ["Lab5_prelab.pdf", "project2-2.pdf"]

    # 링크를 지우면 그 글에서 빠진다(파일 API 없이)
    a["description"] = "<p>링크 없음</p>"
    mock.requests.clear()
    core.sync(Canvas(base, DEMO_TOKEN), store)
    assert files_api(mock) == []
    assert 1013 not in [p["id"] for p in virtual(store.data["cache"], "assignments")["posts"]]


def test_learningx_failure_keeps_one_copy_of_virtual_boards(env):
    """LearningX를 못 읽어 지난 게시판을 쓸 때, 지난 가상 게시판까지 더해 두 벌이 되지 않는다."""
    mock, base, store = env
    core.sync(Canvas(base, DEMO_TOKEN), store)
    mock.lx_fail = "api"
    core.sync(Canvas(base, DEMO_TOKEN), store)
    cache = store.data["cache"]
    assert any("LearningX" in e for e in cache["errors"])
    virt = [(b["course_id"], b["board_id"]) for b in cache["boards"] if b.get("source") == "canvas"]
    assert sorted(virt) == [("101", "announcements"), ("101", "assignments")]
    assert {b["board_id"] for b in cache["boards"] if b.get("source") != "canvas"} == {9101, 9102}   # 지난 LearningX 그대로


def test_unknown_file_reports_once_without_token(env):
    mock, base, store = env
    a, b = assignment(mock, 1011), assignment(mock, 1013)
    a["description"] = '<a href="/courses/101/files/9999/download">gone.pdf</a>'
    b["description"] += '<a href="/courses/101/files/9999">gone.pdf</a>'
    core.sync(Canvas(base, DEMO_TOKEN), store)
    errs = [e for e in store.data["cache"]["errors"] if "첨부를 확인하지 못했습니다" in e]
    assert len(errs) == 1 and errs[0].startswith("과제 'LAB3 보고서'의 첨부")
    assert files_api(mock).count("/api/v1/files/9999") == 1
    assert DEMO_TOKEN not in " ".join(store.data["cache"]["errors"])
    p = next(p for p in virtual(store.data["cache"], "assignments")["posts"] if p["id"] == 1013)
    assert [x["filename"] for x in p["attachments"]] == ["Lab5_prelab.pdf"]    # 못 찾은 것만 빠진다


# ---------------------------------------------------------------- 받기·글자·검색

def test_downloads_dedupe_pdf_texts_and_search(env, tmp_path):
    mock, base, store = env
    _links(mock, base)
    # LearningX 'Lab6 Assignment'의 첨부(Canvas 파일 8001)를 과제 설명에도 링크 → 이미 받는 파일이라 또 받지 않는다
    assignment(mock, 1013)["description"] += '<a href="/courses/101/files/8001/download">Lab6.pdf</a>'
    dl = tmp_path / "dl"
    r = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    got = store.data["downloads"]
    assert sorted(got) == sorted(["101:18001", "101:18002", "101:cf8101", "101:cf8102",   # 지금까지 받던 것
                                  "101:cf8201", "101:cf8301", "101:cf8302", "103:cf8303"])
    assert r["downloaded"] == 8 and "101:cf8001" not in got
    assert "/공지/" in got["101:cf8101"]["path"].replace("\\", "/")      # 공지·과제 둘 다 → 한 번, 먼저 있던 공지 쪽
    m = got["101:cf8301"]
    assert (m["board_id"], m["post"], m["canvas_file_id"], m["board_title"], m["post_title"]) == \
        ("assignments", 1011, 8301, "과제", "LAB3 보고서")
    assert m["path"].replace("\\", "/").endswith("/과제/LAB3 보고서/project2-1.pdf") and m["post_key"] == "board:assignments-1011"
    assert got["103:cf8303"]["board_id"] == "syllabus" and got["103:cf8303"]["post"] == "103"

    t = store.data["pdf_texts"]["101:cf8301"]
    assert "Include the source code" in t["text"] and (t["board_id"], t["post"]) == ("assignments", 1011)
    assert parse_iso(t["posted_at"]) == parse_iso(assignment(mock, 1011)["created_at"])
    assert "101:cf8302" not in store.data["pdf_texts"]                   # PDF가 아니면 글자 없음

    srcs = {s["key"]: s for s in agent.sources(store)}
    s = srcs["pdf:101:cf8301"]
    assert (s["title"], s["board_id"], s["post"]) == ("첨부 'project2-1.pdf'", "assignments", "1011")
    assert s["attached_to"] == [{"board_id": "assignments", "post": "1011", "title": "LAB3 보고서",
                                 "url": assignment(mock, 1011)["html_url"]}]
    # 공지에서 받은 점수표도 과제에 링크된 곳을 안다(한 번만 받았어도)
    rec = next(x for x in core.pdf_source(store.data["cache"], [], now_local()) if x["id"] == "101:cf8101")
    assert [(x["board_id"], x["post"]) for x in rec["attached_to"]] == [("announcements", "9007"), ("assignments", "1011")]
    lab6 = next(x for x in core.pdf_source(store.data["cache"], [], now_local()) if x["id"] == "101:18001")
    assert ("assignments", "1013") in [(x["board_id"], x["post"]) for x in lab6["attached_to"]]   # 같은 Canvas 파일 id
    hits = agent.search(store, "project 2 source code zip")
    assert any(h["source_key"] == "pdf:101:cf8301" for h in hits)

    # 다음 동기화: 받은 파일은 다시 받지 않는다
    mock.requests.clear()
    r = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    assert r["downloaded"] == 0 and files_api(mock) == []


def test_auto_download_off_and_hidden_course(env, tmp_path):
    mock, base, store = env
    _links(mock, base)
    store.data["settings"]["auto_download"] = False
    assert "downloads_to" not in core.sync_options(store)               # 자동 받기를 끄면 받지 않는다(지금 규칙)
    store.data["settings"]["auto_download"] = True
    store.data["course_prefs"] = {"101": {"mode": "hidden", "at": "2026-11-13T00:00:00+09:00"}}
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    assert sorted(store.data["downloads"]) == ["103:cf8303"]            # 제외한 101은 받지 않는다
    assert virtual(store.data["cache"], "assignments") is not None     # 목록은 남는다(다시 보이면 바로 받음)


# ---------------------------------------------------------------- materials.plan의 겹침

def _att(i, fid=None, size=10):
    return {"id": i, "filename": f"f{i}.pdf", "filesize": size, "canvas_file_id": fid, "url": f"/files/{fid or i}/download"}


def _board(board_id, posts, title="과제", cid="95079", source=None):
    b = {"course_id": cid, "tool_id": None if source else 5, "board_id": board_id, "board_title": title, "posts": posts}
    if source:
        b["source"] = source
    return b


def _post(pid, title, atts):
    return {"id": pid, "title": title, "content": "", "created_at": "2026-11-10T01:00:00Z",
            "updated_at": "2026-11-10T01:00:00Z", "post_url": None, "attachments": atts}


def test_plan_downloads_each_file_once(tmp_path):
    boards = [_board(83605, [_post(1, "Lab3", [_att(10001, 6001)])]),                                  # LearningX
              _board("announcements", [_post(9, "공지", [_att("cf6002", 6002)])], "공지", source="canvas"),
              _board("assignments", [_post(3011, "Project 2", [_att("cf6001", 6001), _att("cf6002", 6002),
                                                               _att("cf6003", 6003)]),
                                     _post(3012, "Project 3", [_att("cf6003", 6003)])], source="canvas")]
    jobs = materials.plan(boards, {}, {}, tmp_path)
    assert [(j["key"], j["board_id"], j["post"]) for j in jobs] == [
        ("95079:10001", 83605, 1), ("95079:cf6002", "announcements", 9), ("95079:cf6003", "assignments", 3011)]
    updates, _, errors = materials.fetch(jobs, lambda a: b"%PDF-1.4 x")
    assert not errors and len(updates) == 3
    # 처음 것을 받아 두었으면, 같은 Canvas 파일이 다른 key로 처음 나타나도 받지 않는다
    boards.insert(0, _board(83606, [_post(5, "Lab3 다시", [_att(10005, 6003)])]))
    assert materials.plan(boards, updates, {}, tmp_path) == []
    # 다른 과목의 같은 파일 id는 다른 파일로 본다
    other = [_board("assignments", [_post(1, "P", [_att("cf6001", 6001)])], cid="95080", source="canvas")]
    assert [j["key"] for j in materials.plan(boards + other, updates, {}, tmp_path)] == ["95080:cf6001"]


# ---------------------------------------------------------------- 체크리스트

def _cache():
    def asg(aid, cid, name, due, desc):
        return {"id": aid, "course_id": cid, "name": name, "description": desc, "due_at": due,
                "submission_types": ["none"], "created_at": z(-10), "updated_at": z(-10),
                "html_url": f"https://{HOST}/courses/{cid}/assignments/{aid}",
                "submission": {"workflow_state": "unsubmitted", "submitted_at": None}}

    def att(fid, name):
        return {"id": f"cf{fid}", "filename": name, "filesize": 100, "canvas_file_id": fid, "url": f"/files/{fid}/download"}

    d = T + timedelta(days=6)
    return {
        "fetched_at": z(0), "courses": [{"id": "401", "name": "소프트웨어공학", "course_code": "SE401", "syllabus_body": None}],
        "assignments": [asg(4011, "401", "Project2", None, '<p>설명은 <a href="/courses/401/files/7001">project2-1.pdf</a></p>'),
                        asg(4012, "401", "Report", None, '<p><a href="/courses/401/files/7002">report-guide.pdf</a></p>')],
        "announcements": [{"id": 1, "title": "Project2 안내", "posted_at": z(-2), "context_code": "course_401",
                           "html_url": f"https://{HOST}/courses/401/discussion_topics/1",
                           "message": f"<p>Project2 마감: {d.month}월 {d.day}일 23:59</p>"}],
        "conversations": [], "errors": [],
        "boards": [_board("announcements", [], "공지", cid="401", source="canvas"),
                   _board("assignments", [dict(_post(4011, "Project2", [att(7001, "project2-1.pdf")]),
                                               post_url=f"https://{HOST}/courses/401/assignments/4011"),
                                          dict(_post(4012, "Report", [att(7002, "report-guide.pdf")]),
                                               post_url=f"https://{HOST}/courses/401/assignments/4012")],
                          cid="401", source="canvas")],
        "pdf_texts": {
            "401:cf7001": {"text": "Project 2\nThe report must be submitted as a single PDF file.\n"
                                   "Include your source code and screenshots of the test results.\n"
                                   "This is a team project (2 students per team).",
                           "course_id": "401", "filename": "project2-1.pdf", "post_key": "board:assignments-4011",
                           "board_id": "assignments", "post": 4011, "canvas_file_id": 7001, "posted_at": z(-10)},
            # 옛 기록(board_id·post 칸 없음, canvas_file_id 없음): post_key와 cache의 게시판으로 찾는다
            "401:cf7002": {"text": "Report guide\nSubmit the report in hwp format.\nThe report must not exceed 5 pages.",
                           "course_id": "401", "filename": "report-guide.pdf", "post_key": "board:assignments-4012",
                           "posted_at": z(-10)},
        },
    }


def test_checklist_reads_pdf_attached_to_assignment(tmp_path):
    s = Store(tmp_path / "s.json")
    s.data["cache"] = _cache()
    core.rebuild(s)
    s.save()
    app = App(s, tmp_path, cfg={"canvas_url": f"https://{HOST}"})
    st = app.state()
    found = next(v for v in st["items"] if v["title"] == "Project2" and v["origin"] == "found")
    assert found["linked_to"] == "canvas-401-4011"                       # 공지의 마감 → 마감 없는 Canvas 과제에 연결
    srcs = checklist.all_sources(s)
    keys = [t["key"] for t in checklist.texts_for(found, srcs, s.view())]
    assert keys[:2] == ["assignment:4011", "pdf:401:cf7001"]              # 과제 설명 다음에 그 과제의 PDF
    assert "pdf:401:cf7002" not in keys                                   # 다른 과제의 PDF는 아님
    rows = st["checklists"][found["id"]]["items"]
    got = {(r["kind"], r["text"]): r for r in rows}
    assert {("format", "PDF 파일로 제출"), ("include", "코드 포함"), ("include", "스크린샷 포함")} <= set(got)
    pdf_row = got[("format", "PDF 파일로 제출")]
    assert pdf_row["source_title"] == "첨부 'project2-1.pdf'"
    assert pdf_row["url"] == f"https://{HOST}/courses/401/assignments/4011" and "single PDF" in pdf_row["quote"]
    assert any(k == "team" for k, _ in got)

    report = next(v for v in s.view() if v["id"] == "canvas-401-4012")
    keys = [t["key"] for t in checklist.texts_for(report, srcs, s.view())]
    assert keys[:2] == ["assignment:4012", "pdf:401:cf7002"]              # 옛 기록도 읽힌다
    rules = checklist.rules_for(report, srcs, s.view())
    assert ("format", "한글(HWP) 파일로 제출") in [(r["kind"], r["text"]) for r in rules]


def test_checklist_pdf_downloaded_from_announcement_still_belongs_to_assignment(tmp_path):
    """공지와 과제 설명에 같은 파일 → 한 번만(공지 쪽으로) 받았어도 과제의 체크리스트가 그 PDF를 읽는다."""
    cache = _cache()
    rec = cache["pdf_texts"]["401:cf7001"]
    rec.update(post_key="board:announcements-1", board_id="announcements", post=1)
    cache["boards"][0]["posts"] = [_post(1, "Project2 안내", [{"id": "cf7001", "filename": "project2-1.pdf",
                                                              "filesize": 100, "canvas_file_id": 7001}])]
    s = Store(tmp_path / "s.json")
    s.data["cache"] = cache
    core.rebuild(s)
    found = next(v for v in s.view() if v["title"] == "Project2" and v["origin"] == "found")
    keys = [t["key"] for t in checklist.texts_for(found, checklist.all_sources(s), s.view())]
    assert "pdf:401:cf7001" in keys


def test_hidden_course_pdf_not_in_checklist_sources(tmp_path):
    s = Store(tmp_path / "s.json")
    s.data["cache"] = _cache()
    s.data["course_prefs"] = {"401": {"mode": "hidden"}}
    core.rebuild(s)
    assert not [x for x in checklist.all_sources(s) if x["type"] == "pdf"]


def test_demo_lab5_checklist_from_linked_pdf(tmp_path):
    """데모: 'LAB5 예비보고서' 설명의 Lab5_prelab.pdf(요건은 PDF 안에만) → 받고 → 체크리스트."""
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), s, **core.sync_options(s, demo_home=tmp_path))
        st = App(s, tmp_path, cfg={}).state()
    finally:
        mock.shutdown()
    key = f"101:cf{PRELAB_FILE}"
    assert s.data["downloads"][key]["filename"] == "Lab5_prelab.pdf" and s.data["pdf_texts"][key]["board_id"] == "assignments"
    lab5 = next(v for v in st["items"] if v["id"] == "canvas-101-1013")
    rows = st["checklists"][lab5["id"]]["items"]
    got = {(r["kind"], r["text"]) for r in rows}
    assert {("where", "LMS(Canvas)에 제출"), ("format", "PDF 파일로 제출"), ("include", "코드 포함"),
            ("team", "개인 과제(혼자 작성)")} <= got
    assert all(r["source_title"] == "첨부 'Lab5_prelab.pdf'" and r["url"].endswith("/courses/101/assignments/1013")
               for r in rows)
    mats = {m["filename"]: m for m in st["materials"]["items"]}
    assert (mats["Lab5_prelab.pdf"]["board_title"], mats["Lab5_prelab.pdf"]["post_title"]) == ("과제", "LAB5 예비보고서")
    assert not [v for v in st["items"] if v["course_id"] == "101" and v["origin"] == "found"
                and any(e.get("source_type") == "pdf" and "Lab5_prelab" in (e.get("source_title") or "")
                        for e in v.get("evidence") or [])]                  # 날짜 없는 PDF — 마감 후보를 만들지 않는다
