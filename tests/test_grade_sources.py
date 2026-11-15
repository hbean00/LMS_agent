"""성적 파일의 출처(계약 27-1): 공지·토론 글의 첨부 칸 파일과 Canvas 성적부 점수.

- 첨부 칸: 실제 ASIC 'Updated Grading Policy'의 PDF는 본문 링크 없이 공지의 `attachments` 칸에만 있었다 → 가상 게시판
  "announcements"에 본문 링크 파일과 함께 넣어 materials가 받고, 카테고리·attached_to·점수표/마감 읽기가 그대로 쓴다.
- Canvas 성적부: `assignments[].submission.score` → 점수 후보 source_type "canvas_grade"(반 통계는 score_statistics,
  축하 카드와 같은 계산). 평가 항목 연결·추정·제외 과목·글/파일 후보와의 겹침 규칙.
가짜 Canvas(mock_canvas)와 지어낸 데이터만 쓴다(네트워크 없음). 날짜는 오늘 기준 상대값, 시각은 같은 순간인지로 비교한다."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

from datetime import datetime, time, timedelta

import pytest

from lms_agent import celebrate, core, gradebook
from lms_agent.canvas import Canvas
from lms_agent.features.materials import category_of
from lms_agent.mock_canvas import (DEMO_STUDENT_ID, DEMO_TOKEN, POLICY_FILE, SCORE_FILES, score_table_pdf,
                                   start_mock_canvas, tiny_pdf)
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


class _Bare:
    data = {"settings": {"auto_download": False}}


def pdf_reader():
    return core.sync_options(_Bare())["pdf_reader"]


def files_api(mock) -> list[str]:
    return [p for p in mock.requests if p.startswith("/api/v1/files/")]


def virtual(cache, board_id, course="101") -> dict | None:
    return next((b for b in cache["boards"] if b.get("source") == "canvas" and b["board_id"] == board_id
                 and b["course_id"] == course), None)


def post_of(cache, board_id, pid, course="101") -> dict | None:
    b = virtual(cache, board_id, course)
    return next((p for p in (b or {}).get("posts") or [] if str(p["id"]) == str(pid)), None)


def ann(mock, aid) -> dict:
    return next(a for a in mock.data["announcements"] if a["id"] == aid)


def field_file(base, fid, name, data) -> dict:
    """Canvas 공지 첨부 칸의 파일 모양(display_name·filename·size·url·content-type)."""
    return {"id": fid, "display_name": name, "filename": name.replace(" ", "+"), "content-type": "application/pdf",
            "url": f"{base}/files/{fid}/download?download_frd=1&verifier=v{fid}", "size": len(data),
            "locked": False, "hidden": False}


@pytest.fixture()
def env(tmp_path):
    mock, base = start_mock_canvas()
    yield mock, base, Store(tmp_path / "state.json")
    mock.shutdown()


# ================================================================ 첨부 칸

def test_demo_attachment_field_file_is_planned_downloaded_and_read(env, tmp_path):
    """데모 'Updated Grading Policy': PDF가 첨부 칸에만 → 받고, 글자를 뽑고, 공지 분류·attached_to가 그 공지."""
    mock, base, store = env
    src = ann(mock, 9006)
    assert str(POLICY_FILE) not in src["message"] and [a["id"] for a in src["attachments"]] == [POLICY_FILE]
    r = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    cache = store.data["cache"]
    assert cache["errors"] == []
    p = post_of(cache, "announcements", 9006)
    assert p is not None and p["title"] == "Updated Grading Policy" and p["content"] == ""
    (a,) = p["attachments"]
    assert (a["id"], a["filename"], a["canvas_file_id"]) == (f"cf{POLICY_FILE}", "Update Grading Policy.pdf", POLICY_FILE)
    assert a["filesize"] == len(mock.data["files"][POLICY_FILE]) and "verifier=" in a["url"]
    assert f"/api/v1/files/{POLICY_FILE}" not in files_api(mock)        # 이름·크기가 글에 있어 묻지 않는다
    assert parse_iso(p["created_at"]) == parse_iso(src["posted_at"])

    key = f"101:cf{POLICY_FILE}"
    m = store.data["downloads"][key]
    assert (m["board_id"], m["post"], m["canvas_file_id"], m["post_key"]) == \
        ("announcements", 9006, POLICY_FILE, "board:announcements-9006")
    assert m["path"].replace("\\", "/").endswith("/Updated Grading Policy/Update Grading Policy.pdf")
    assert category_of(m["post_key"], m["board_title"]) == "announcement"
    t = store.data["pdf_texts"][key]
    assert "Regrade requests" in t["text"] and (t["board_id"], t["post"]) == ("announcements", 9006)
    rec = next(x for x in core.pdf_source(cache, [], now_local()) if x["id"] == key)
    assert [(x["board_id"], x["post"]) for x in rec["attached_to"]] == [("announcements", "9006")]
    assert r["downloaded"] >= 1

    # 다음 동기화: 받은 파일은 다시 받지 않고, 파일 API도 묻지 않는다
    mock.requests.clear()
    r = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    assert r["downloaded"] == 0 and files_api(mock) == []


def test_attachment_field_dedupes_with_body_links(env, tmp_path):
    """본문 링크와 첨부 칸이 같은 파일이면 한 번. 다른 공지 본문에 링크된 같은 파일도 파일 API 없이 첨부 칸 정보를 쓴다."""
    mock, base, store = env
    pol = ann(mock, 9006)
    pol["message"] += f'<p><a href="/courses/101/files/{POLICY_FILE}/download?wrap=1">policy</a></p>'
    scores = ann(mock, 9007)
    scores["message"] += f'<p>정책: <a href="{base}/courses/101/files/{POLICY_FILE}?wrap=1">pdf</a></p>'
    # 점수 공지의 첨부 칸에 본문에도 링크된 점수표 하나 + 신규 파일 하나
    mock.data["files"][8601] = tiny_pdf(["Notes"])
    mock.data["file_names"][8601] = "notes.pdf"
    scores["attachments"] = [field_file(base, SCORE_FILES[0], "Lab0_score.pdf", mock.data["files"][SCORE_FILES[0]]),
                             field_file(base, 8601, "notes.pdf", mock.data["files"][8601]),
                             {"id": None, "display_name": "broken"}]        # id 없는 칸은 버린다
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    cache = store.data["cache"]
    assert [a["id"] for a in post_of(cache, "announcements", 9006)["attachments"]] == [f"cf{POLICY_FILE}"]
    assert [a["id"] for a in post_of(cache, "announcements", 9007)["attachments"]] == [
        f"cf{SCORE_FILES[0]}", f"cf{SCORE_FILES[1]}", f"cf{POLICY_FILE}", "cf8601"]   # 본문 링크 먼저, 다음 첨부 칸
    asked = files_api(mock)
    assert f"/api/v1/files/{POLICY_FILE}" not in asked and "/api/v1/files/8601" not in asked
    assert sorted(set(asked)) == sorted(asked)                               # 한 실행에 같은 파일은 한 번만 묻는다
    got = store.data["downloads"]
    assert f"101:cf{POLICY_FILE}" in got and "101:cf8601" in got
    assert got[f"101:cf{POLICY_FILE}"]["post"] == 9007    # 두 공지에 → 한 번(공지 순서상 먼저 온 최신 공지)
    rec = next(x for x in core.pdf_source(cache, [], now_local()) if x["id"] == f"101:cf{POLICY_FILE}")
    assert {(x["board_id"], x["post"]) for x in rec["attached_to"]} == {("announcements", "9006"), ("announcements", "9007")}


def test_attachment_field_score_table_becomes_candidate(env, tmp_path):
    """첨부 칸의 점수표도 지금의 점수표 읽기가 그대로 읽는다(학번 줄 → pdf 후보)."""
    mock, base, store = env
    data = score_table_pdf("Lab2", 2)
    mock.data["files"][8602] = data
    mock.data["file_names"][8602] = "Lab2_score.pdf"
    ann(mock, 9007)["attachments"] = [field_file(base, 8602, "Lab2_score.pdf", data)]
    store.data["settings"]["student_id"] = DEMO_STUDENT_ID
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    cands = {c["key"]: c for c in gradebook.candidates(store)}
    c = cands["pdf:101:cf8602"]
    assert (c["source_type"], c["source_title"], c["label"], c["course_id"]) == ("pdf", "Lab2_score.pdf", "Lab2", "101")
    assert c["score"] is not None and c["n"] == 8


def test_graded_discussion_topic_files_go_to_its_assignment(env, tmp_path):
    """채점 토론(과제의 discussion_topic): 토론 글의 첨부 칸·본문 링크 → 그 과제 글(attached_to = 과제)."""
    mock, base, store = env
    for fid, name in ((8603, "discussion_guide.pdf"), (8604, "rubric.pdf")):
        mock.data["files"][fid] = tiny_pdf([name])
        mock.data["file_names"][fid] = name
    a = next(x for x in mock.data["assignments"]["102"] if x["id"] == 1021)
    a["discussion_topic"] = {"id": 7701, "message": '<p><a href="/courses/102/files/8604/download">rubric</a></p>',
                             "attachments": [field_file(base, 8603, "discussion_guide.pdf", mock.data["files"][8603])]}
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    p = post_of(store.data["cache"], "assignments", 1021, "102")
    assert [x["id"] for x in p["attachments"]] == ["cf8604", "cf8603"]
    assert files_api(mock).count("/api/v1/files/8604") == 1 and "/api/v1/files/8603" not in files_api(mock)
    m = store.data["downloads"]["102:cf8603"]
    assert (m["board_id"], m["post"]) == ("assignments", 1021)


def test_hidden_course_attachment_field_not_downloaded(env, tmp_path):
    mock, base, store = env
    store.data["course_prefs"] = {"101": {"mode": "hidden", "at": z(-1)}}
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=tmp_path / "dl", pdf_reader=pdf_reader())
    assert f"101:cf{POLICY_FILE}" not in store.data.get("downloads", {})
    assert post_of(store.data["cache"], "announcements", 9006) is not None     # 목록은 남는다(다시 보이면 받음)


# ================================================================ Canvas 성적부

def _store(tmp_path, assignments, courses=None) -> Store:
    s = Store(tmp_path / "state.json")
    s.data["cache"] = {"courses": courses or [{"id": "201", "name": "확률과 통계"}, {"id": "202", "name": "회로이론"}],
                       "assignments": assignments, "announcements": [], "conversations": [], "boards": []}
    return s


def graded(aid, name, score, cid="201", points=10.0, stats=None, **sub):
    a = {"id": aid, "course_id": cid, "name": name, "points_possible": points, "due_at": z(-3, 23, 59),
         "html_url": f"https://lms.example.test/courses/{cid}/assignments/{aid}",
         "submission": {"score": score, "submitted_at": z(-4, 20), "graded_at": z(-2, 9), "workflow_state": "graded",
                        **sub}}
    if stats is not None:
        a["score_statistics"] = stats
    return a


STATS = {"min": 2.0, "max": 10.0, "mean": 6.5, "median": 7.0, "lower_q": 5.0, "upper_q": 8.0}


def canvas_of(store) -> dict:
    return {c["key"]: c for c in gradebook.candidates(store) if c["source_type"] == "canvas_grade"}


def test_canvas_grade_candidate_with_statistics(tmp_path):
    s = _store(tmp_path, [graded(5001, "Quiz  1", 8.0, stats=STATS)])
    (c,) = canvas_of(s).values()
    mean, sd = celebrate._stats({"score_statistics": STATS})
    assert (c["key"], c["label"], c["score"], c["max_score"]) == ("canvas:5001", "Quiz 1", 8.0, 10.0)
    assert (c["mean"], c["sd"]) == (mean, sd) == (6.5, round((8.0 - 5.0) / 1.349, 2))   # 축하 카드와 같은 계산
    assert (c["high"], c["low"], c["n"], c["rank"]) == (10.0, 2.0, None, None)
    assert (c["course_id"], c["course_short"], c["source_title"], c["file_kind"]) == ("201", "확률과 통계", "Canvas 성적부", None)
    assert c["url"] == "https://lms.example.test/courses/201/assignments/5001" and c["assignment_id"] == "5001"
    assert parse_iso(c["posted_at"]) == parse_iso(z(-2, 9))                   # graded_at
    assert c["sentence"] == "Canvas 성적부: Quiz 1 8/10 (반 평균 6.5)"
    assert c["notes"] == [] and c["merged"] == []


def test_canvas_grade_candidate_without_statistics_and_edge_cases(tmp_path):
    s = _store(tmp_path, [
        graded(5002, "Homework 2", 7.5, graded_at=None),                     # 반 통계 없음, graded_at 없음 → submitted_at
        graded(5003, "Bonus", 3.0, points=0),                                 # 배점 0 → 만점 모름
        graded(5004, "Quiz 2", 9.0, stats={"mean": 7.0}),                     # 평균만(사분위 없음)
        graded(5005, "Excused", None, excused=True),                          # 점수 없음·면제
        graded(5006, "Excused 2", 5.0, excused=True),
        dict(graded(5007, "Survey", 1.0), grading_type="not_graded"),
        graded(5008, "Not yet", None),
        {"id": 5009, "course_id": "201", "name": "No submission", "points_possible": 10},
    ])
    cs = canvas_of(s)
    assert sorted(cs) == ["canvas:5002", "canvas:5003", "canvas:5004"]
    hw = cs["canvas:5002"]
    assert (hw["mean"], hw["sd"], hw["high"], hw["low"]) == (None, None, None, None)
    assert parse_iso(hw["posted_at"]) == parse_iso(z(-4, 20))
    assert hw["sentence"] == "Canvas 성적부: Homework 2 7.5/10" and any("반 통계" in n for n in hw["notes"])
    bonus = cs["canvas:5003"]
    assert bonus["max_score"] is None and bonus["sentence"] == "Canvas 성적부: Bonus 3점"
    assert any("만점" in n for n in bonus["notes"])
    q2 = cs["canvas:5004"]
    assert (q2["mean"], q2["sd"]) == (7.0, None) and any("표준편차" in n for n in q2["notes"])


def test_canvas_assignments_request_score_statistics(env):
    """Canvas.assignments가 include[]=score_statistics를 보내고, 가짜 Canvas는 요청할 때만 통계를 준다."""
    mock, base, _ = env
    cv = Canvas(base, DEMO_TOKEN)
    (q,) = cv.assignments(105)
    assert q["score_statistics"]["mean"] == 72.4 and q["submission"]["score"] == 80.0
    path = next(p for p in mock.requests if p.startswith("/api/v1/courses/105/assignments"))
    assert "include%5B%5D=score_statistics" in path and "include%5B%5D=submission" in path
    plain = cv._get_all("/api/v1/courses/105/assignments", [("include[]", "submission")])
    assert "score_statistics" not in plain[0]


def test_canvas_grade_links_to_component_and_enters_estimate(tmp_path):
    s = _store(tmp_path, [graded(5001, "Quiz 1", 8.0, stats=STATS), graded(5010, "Quiz 2", 9.0, stats=STATS)])
    s.data["grades"] = {"201": {"components": [
        {"name": "Quizzes", "weight": 30, "score": None, "max_score": None, "mean": None, "sd": None, "fixed": False},
        {"name": "Final exam", "weight": 70, "score": None, "max_score": None, "mean": None, "sd": None, "fixed": False},
    ], "dist": None, "class_size": None}}
    cands = gradebook.candidates(s)
    links = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], cands)
    assert links["canvas:5001"]["component"] == "Quizzes" and links["canvas:5001"]["by"] == "rules"

    sec = gradebook.section(s, [{"id": "201", "short_name": "확률과 통계", "current_score": None}], items=[])
    (course,) = sec["courses"]
    quizzes = next(x for x in course["config"]["components"] if x["name"] == "Quizzes")
    assert [(m["key"], m["source_type"], m["score"]) for m in quizzes["members"]] == [
        ("canvas:5001", "canvas_grade", 8.0), ("canvas:5010", "canvas_grade", 9.0)]
    assert quizzes["derived"]["received"] == 2 and quizzes["derived"]["score_pct"] == 85.0
    est = course["estimate"]
    assert est is not None and next(r for r in est["components"] if r["name"] == "Quizzes")["status"] == "known"
    shown = {x["key"]: x for x in sec["candidates"]}
    assert shown["canvas:5001"]["component"] == "Quizzes" and shown["canvas:5001"]["link_by"] == "rules"

    # 사용자가 '이 과목 성적에 넣지 않음' → 세부 점수에서 빠진다(다른 후보와 같은 규칙)
    gradebook.set_link(s, "201", {"key": "canvas:5010", "component": None})
    sec = gradebook.section(s, [{"id": "201", "short_name": "확률과 통계", "current_score": None}], items=[])
    quizzes = next(x for x in sec["courses"][0]["config"]["components"] if x["name"] == "Quizzes")
    assert [m["key"] for m in quizzes["members"]] == ["canvas:5001"]
    # 직접 다른 항목으로
    gradebook.set_link(s, "201", {"key": "canvas:5001", "component": "Final exam"})
    links = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], gradebook.candidates(s))
    assert links["canvas:5001"] == {"component": "Final exam", "by": "user", "reason": "직접 연결함", "evidence": None}


def test_canvas_grade_dedupes_with_text_and_file_candidates(tmp_path):
    """같은 과목·같은 이름(띄어쓰기·대소문자 무시)·같은 점수가 성적부와 글·점수표 파일에 → 성적부 하나(합친 것은 merged·notes)."""
    s = _store(tmp_path, [graded(5001, "Quiz 1", 8.0), graded(5011, "Midterm", 78.0, points=100.0, stats=STATS)])
    s.data["settings"]["student_id"] = "2026000002"
    s.data["pdf_texts"] = {"201:cf1": {"text": "\n".join(["Quiz1 Score", "No Student ID Score(10)"]
                                                           + [f"{i} {2026000000 + i} {8 if i == 2 else 5 + i % 4}"
                                                              for i in range(1, 9)]),
                                       "course_id": "201", "filename": "quiz1_score.pdf", "post_key": "board:announcements-1",
                                       "posted_at": z(-1)}}
    s.data["cache"]["conversations"] = [
        {"id": 1, "subject": "중간 성적", "context_code": "course_201",
         "messages": [{"body": "Midterm 78점 (평균 60.1, 표준편차 9.5)입니다.", "created_at": z(-1)}]},
        {"id": 2, "subject": "다른 과목", "context_code": "course_202",
         "messages": [{"body": "Midterm 78점 (평균 60.1, 표준편차 9.5)입니다.", "created_at": z(-1)}]}]
    raw = {c["key"]: c for c in gradebook.score_file_candidates(s)}
    assert raw["pdf:201:cf1"]["label"] == "Quiz1" and raw["pdf:201:cf1"]["score"] == 8.0   # 파일 쪽 후보가 실제로 있다

    cands = gradebook.candidates(s)
    keys = [c["key"] for c in cands]
    assert "pdf:201:cf1" not in keys                                         # 'Quiz1' = 'Quiz 1', 8 = 8 → 성적부 하나
    q = next(c for c in cands if c["key"] == "canvas:5001")
    assert [m["key"] for m in q["merged"]] == ["pdf:201:cf1"]
    assert q["mean"] is not None and q["n"] == 8 and q["rank"] is not None   # 성적부에 없는 반 통계는 파일에서
    assert any("quiz1_score.pdf" in n and "하나로" in n for n in q["notes"])
    assert not any("반 통계를 주지" in n for n in q["notes"])

    mid = next(c for c in cands if c["key"] == "canvas:5011")
    assert [m["source_type"] for m in mid["merged"]] == ["inbox"]
    assert (mid["mean"], mid["sd"]) == celebrate._stats({"score_statistics": STATS})   # 성적부 통계가 이긴다
    others = [c for c in cands if c["source_type"] == "inbox"]
    assert [c["course_id"] for c in others] == ["202"]                       # 다른 과목의 같은 문장은 그대로


def test_dedupe_keeps_different_scores_and_carries_user_link(tmp_path):
    s = _store(tmp_path, [graded(5001, "Quiz 1", 8.0)])
    s.data["manual"] = [{"id": "m1", "course_name": "확률과 통계", "text": "Quiz 1 7점 (평균 6.0, 표준편차 1.0)",
                         "posted_at": z(-1)},
                        {"id": "m2", "course_name": "확률과 통계", "text": "Quiz 1: 8/20점", "posted_at": z(-1)}]
    cands = gradebook.candidates(s)
    manual = [c for c in cands if c["source_type"] == "manual"]
    assert sorted((c["score"], c["max_score"]) for c in manual) == [(7.0, None), (8.0, 20.0)]   # 점수·만점이 다르면 둘 다
    assert next(c for c in cands if c["key"] == "canvas:5001")["merged"] == []

    # 같은 점수의 글 후보를 사용자가 연결해 두었으면, 성적부 후보가 그 연결을 이어받는다
    s.data["manual"] = [{"id": "m3", "course_name": "확률과 통계", "text": "Quiz 1 8점 (평균 6.0, 표준편차 1.0)",
                         "posted_at": z(-1)}]
    text_key = gradebook.candidate_key("201", "manual", "직접 입력", "Quiz 1", 8.0)
    s.data["grades"] = {"201": {"components": [
        {"name": "Quizzes", "weight": 30, "score": None, "max_score": None, "mean": None, "sd": None, "fixed": False},
        {"name": "Participation", "weight": 70, "score": None, "max_score": None, "mean": None, "sd": None,
         "fixed": False}], "dist": None, "class_size": None}}
    gradebook.set_link(s, "201", {"key": text_key, "component": "Participation"})
    cands = gradebook.candidates(s)
    assert text_key not in [c["key"] for c in cands]
    q = next(c for c in cands if c["key"] == "canvas:5001")
    assert q["merged"][0]["key"] == text_key and (q["mean"], q["sd"]) == (6.0, 1.0)
    links = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], cands)
    assert links["canvas:5001"]["component"] == "Participation" and links["canvas:5001"]["by"] == "user"


def test_hidden_course_canvas_grades_excluded(tmp_path):
    s = _store(tmp_path, [graded(5001, "Quiz 1", 8.0, stats=STATS), graded(6001, "Lab 1", 9.0, cid="202")])
    s.data["course_prefs"] = {"202": {"mode": "hidden", "at": z(-1)}}
    assert sorted(canvas_of(s)) == ["canvas:5001"]
    every = {c["key"] for c in gradebook.candidates(s, include_hidden=True)}
    assert {"canvas:5001", "canvas:6001"} <= every


def test_candidates_cap_keeps_many_canvas_grades(tmp_path):
    s = _store(tmp_path, [graded(7000 + i, f"Quiz {i}", 5.0) for i in range(150)])
    assert len(canvas_of(s)) == 150                                          # 100개에서 잘리지 않는다


# ================================================================ 데모 + 축하 카드

def test_demo_canvas_grades(env):
    """데모: 105 '연구실 안전 퀴즈' 80/100 + 반 통계(평가 항목 없음 → 연결 없음), 101 LAB3 보고서 9/10(통계 없음 →
    Homework assignments에 규칙으로 연결). 평균·표준편차는 축하 카드(graded)와 같다."""
    mock, base, store = env
    core.sync(Canvas(base, DEMO_TOKEN), store)
    cs = canvas_of(store)
    assert {"canvas:1051", "canvas:1011"} <= set(cs)
    quiz = cs["canvas:1051"]
    src = next(a for a in mock.data["assignments"]["105"] if a["id"] == 1051)
    assert (quiz["label"], quiz["score"], quiz["max_score"], quiz["course_short"]) == ("연구실 안전 퀴즈", 80.0, 100.0,
                                                                                      "연구실 안전 교육")
    assert (quiz["mean"], quiz["sd"]) == celebrate._stats(src) and quiz["mean"] == 72.4
    assert (quiz["high"], quiz["low"]) == (100.0, 40.0)
    assert parse_iso(quiz["posted_at"]) == parse_iso(src["submission"]["graded_at"])
    events = {e["key"]: e["entry"] for e in celebrate._graded_events(store, {})}
    card = events["graded:105:1051"]
    assert (card["score"], card["mean"], card["sd"]) == (quiz["score"], quiz["mean"], quiz["sd"])

    sec = gradebook.section(store, [{"id": c["id"], "short_name": core.short_course_name(c["name"]),
                                     "current_score": None} for c in store.data["cache"]["courses"]], items=[])
    shown = {x["key"]: x for x in sec["candidates"]}
    assert shown["canvas:1051"]["component"] is None and shown["canvas:1051"]["link_reason"] == "평가 항목이 아직 없어요"
    assert shown["canvas:1011"]["component"] == "Homework assignments"
    hw = next(x for c in sec["courses"] if c["course_id"] == 101 or str(c["course_id"]) == "101"
              for x in c["config"]["components"] if x["name"] == "Homework assignments")
    assert "canvas:1011" in [m["key"] for m in hw["members"]]

    # 제외하면 그 과목 성적부 점수도 빠진다
    store.data["course_prefs"] = {"105": {"mode": "hidden", "at": z(0)}}
    assert "canvas:1051" not in canvas_of(store)
