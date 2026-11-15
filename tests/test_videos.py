"""강의 영상 시청 마감(계약 26-6, 베타테스터 "그 영상 듣는 거 마감 기한은 안 뜨는지 — 에이직 영상도 6일인가 남았는데").

LearningX(Xinics) 주차학습의 영상(출석 인정 기간이 있는 것)을 `cache["videos"]`로 모으고 항목(origin "video")으로 만든다.
응답 모양은 다른 학교 LearningX를 실제 계정으로 읽은 공개 기록의 필드 이름만 쓴다(고려대 실제 응답은 출시 뒤 확인 — 가정).
가짜 Canvas(mock_canvas)와 가짜 구글(mock_google)만 쓴다. 과목·영상·로그인 id는 모두 지어낸 것이고, 날짜는 오늘 기준(CI는 UTC)."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

import json
from datetime import datetime, time, timedelta

import pytest

from lms_agent import core, gradebook, gsync
from lms_agent.canvas import Canvas
from lms_agent.ics import build_ics, event_summary, validate_ics
from lms_agent.learningx import LearningX, canvas_module_videos, video_records
from lms_agent.mock_canvas import DEMO_TOKEN, VIDEO_TOOL_ID, start_mock_canvas
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()
BASE = "https://lms.example.test"


def at(days, h=23, mi=59, s=59):
    return datetime.combine(T + timedelta(days=days), time(h, mi, s), tzinfo=TZ)


def z(days, h=23, mi=59, s=59):
    return iso(at(days, h, mi, s))


def lecture(item_id, title="1주차 강의", due=None, **kw):
    """다른 학교 LearningX `/modules?include_detail=true`의 module_item 모양."""
    cd = {"item_id": item_id, "title": title, "use_attendance": True, "unlock_at": z(-3, 0, 0, 0),
          "due_at": due if due is not None else z(5), "late_at": z(7), "lock_at": z(7),
          "item_content_data": {"content_type": "everlec", "duration": 1500}}
    cd.update(kw.pop("cd", {}))
    it = {"module_item_id": item_id + 1000, "title": title, "content_type": "attendance_item", "completed": False,
          "attendance_status": "none", "published": True, "content_data": cd}
    it.update(kw)
    return it


def modules(*items):
    return [{"module_id": 1, "title": "1주차", "module_items": list(items)}]


@pytest.fixture()
def env(tmp_path):
    mock, base = start_mock_canvas()
    yield mock, base, Store(tmp_path / "state.json")
    mock.shutdown()


def sync(base, store, **kw):
    return core.sync(Canvas(base, DEMO_TOKEN), store, **kw)


def videos_in(store):
    return [v for v in store.view() if v["origin"] == "video"]


# ---------------------------------------------------------------- 응답 읽기

def test_modules_shape_full_record():
    (v,) = video_records(modules(lecture(501, "3주차: 파이프라인")), "777", BASE, tool_id=7)
    assert v == {"course_id": "777", "id": "501", "title": "3주차: 파이프라인", "unlock_at": z(-3, 0, 0, 0),
                 "due_at": z(5), "late_at": z(7), "lock_at": z(7), "completed": False, "attendance": "none",
                 "duration": 1500, "url": f"{BASE}/courses/777/modules/items/1501"}
    assert parse_iso(v["due_at"]) == at(5)


def test_allcomponents_flat_shape_same_id():
    """옛 LearningX의 allcomponents_db(평평한 목록): component_id = /modules의 content_data.item_id → 같은 영상 id."""
    raw = [{"component_id": 501, "title": "3주차: 파이프라인", "type": "commons", "due_at": "2026-12-01T14:59:59Z",
            "unlock_at": "2026-11-24T15:00:00Z", "late_at": None, "lock_at": "2026-12-01T14:59:59Z",
            "completed": "true", "use_attendance": True, "attendance_status": "attendance",
            "commons_content": {"content_type": "mp4", "duration": "612.4",
                                "view_url": f"{BASE}/learningx/commons/view/501"},
            "view_info": {"view_url": "https://commons.example.net/em/abc"}},
           {"component_id": 900, "title": "Project 1", "type": "assignment", "due_at": "2026-12-01T14:59:59Z",
            "use_attendance": False}]
    (v,) = video_records(raw, 777, BASE)
    assert (v["id"], v["completed"], v["duration"], v["attendance"]) == ("501", True, 612, "attendance")
    assert parse_iso(v["due_at"]) == parse_iso("2026-12-01T23:59:59+09:00")
    assert v["url"] == f"{BASE}/learningx/commons/view/501"       # 다른 서버(뷰어) 주소는 버리고 Canvas 서버 주소만


@pytest.mark.parametrize("item,why", [
    (lecture(1, cd={"item_content_data": {"content_type": "pdf"}}), "문서"),
    (lecture(2, cd={"item_content_data": {"content_type": "video_conference"}}), "실시간 수업"),
    (lecture(3, cd={"use_attendance": False}), "출석 안 씀"),
    (lecture(4, published=False), "숨긴 항목"),
    (lecture(5, content_type="assignment"), "Canvas 과제(이미 모음)"),
    ({"title": "id 없음", "content_data": {"use_attendance": True, "due_at": z(3)}}, "id 없음"),
    ("not a dict", "모양이 다름"),
])
def test_non_videos_are_skipped(item, why):
    assert video_records(modules(item), "1", BASE) == [], why


def test_missing_fields_are_tolerated():
    """칸이 거의 없어도 읽는다: 제목·기간·길이·주소 없음, 완료는 출석 상태로, 영상 종류만 있고 use_attendance 없음."""
    raw = [{"id": 77, "content_data": {"due_at": "2026-12-01 23:59:59", "item_content_data": {"content_type": "movie"}},
            "attendance_status": "attendance"},
           {"module_item_id": 78, "content_data": {"use_attendance": "1", "due_at": 12345, "duration": True}}]
    a, b = video_records(raw, "9", "")
    assert (a["id"], a["title"], a["completed"], a["unlock_at"], a["duration"], a["url"]) == ("77", "강의 영상", True,
                                                                                               None, None, None)
    assert parse_iso(a["due_at"]) == datetime(2026, 12, 1, 23, 59, 59, tzinfo=TZ)   # 시간대 없는 값은 PC 시간대
    assert (b["id"], b["due_at"], b["completed"], b["duration"]) == ("78", None, False, None)  # 숫자 시각은 읽지 않음


def test_wrapped_and_duplicate_items():
    raw = [{"module_id": 1, "items": [lecture(10), lecture(10), lecture(11)]}, lecture(12)]
    assert [v["id"] for v in video_records(raw, "1", BASE)] == ["10", "11", "12"]


def test_canvas_modules_fallback_parsing():
    mods = [{"id": 1, "items": [
        {"id": 31, "type": "ExternalTool", "title": "4주차 영상",
         "external_url": f"{BASE}/learningx/lti/lecture_attendance/items/view/501", "html_url": f"{BASE}/courses/1/modules/items/31",
         "content_details": {"due_at": "2026-12-01T14:59:59Z", "unlock_at": "2026-11-24T15:00:00Z"},
         "completion_requirement": {"type": "must_view", "completed": True}},
        {"id": 32, "type": "Assignment", "title": "과제", "content_details": {"due_at": "2026-12-01T14:59:59Z"}},
        {"id": 33, "type": "ExternalTool", "title": "Zoom", "external_url": "https://zoom.example.com/x",
         "content_details": {"due_at": "2026-12-01T14:59:59Z"}},
        {"id": 34, "type": "ExternalTool", "title": "마감 없음", "external_url": f"{BASE}/learningx/lti/x",
         "content_details": {}}]}]
    (v,) = canvas_module_videos(mods, "1", BASE)
    assert (v["id"], v["title"], v["completed"], v["url"]) == ("501", "4주차 영상", True, f"{BASE}/courses/1/modules/items/31")


@pytest.mark.parametrize("tools,want", [
    ([{"id": 1, "name": "게시판", "url": f"{BASE}/learningx/lti/learningx_board/boards"},
      {"id": 2, "name": "출결현황", "url": f"{BASE}/learningx/lti/attendance"},
      {"id": 3, "name": "주차학습", "url": f"{BASE}/learningx/lti/lecture_attendance"}], 3),
    ([{"id": 4, "name": "(구) 강의콘텐츠", "url": f"{BASE}/learningx/lti/old", "course_navigation": {"text": "(구) 강의콘텐츠"}},
      {"id": 5, "name": "LX", "url": f"{BASE}/learningx/lti/new", "course_navigation": {"text": "강의콘텐츠"}}], 5),
    ([{"id": 6, "name": "Learn Status", "url": f"{BASE}/learningx/lti/attendance"}], 6),
    ([{"id": 7, "name": "주차학습", "url": "https://other.example.com/lti"},                 # LearningX가 아닌 도구
      {"id": 8, "name": "게시판", "url": f"{BASE}/learningx/lti/learningx_board/boards"}], None),
    ([], None), (None, None),
])
def test_video_tool_choice(tools, want):
    t = LearningX.video_tool(None, "1", tools if tools is not None else [])
    assert (t["id"] if t else None) == want


# ---------------------------------------------------------------- 항목

def _cache(*videos):
    return {"fetched_at": z(0), "courses": [{"id": "301", "name": "영상 과목", "syllabus_body": None}],
            "assignments": [], "announcements": [], "conversations": [], "errors": [], "boards": [],
            "videos": [dict(v, course_id=v.get("course_id", "301")) for v in videos]}


def _v(vid, due, **kw):
    d = {"id": vid, "title": f"영상 {vid}", "unlock_at": z(-2, 0, 0, 0), "due_at": due, "late_at": None, "lock_at": None,
         "completed": False, "attendance": None, "duration": 900, "url": f"{BASE}/courses/301/modules/items/{vid}"}
    d.update(kw)
    return d


def test_items_shape_completed_and_no_due(tmp_path):
    cache = _cache(_v("1", z(3), completed=True, late_at=z(5)), _v("2", z(9)), _v("3", None), _v("4", z(-400)))
    items, report, _ = core.build_items(cache, [])
    vids = {it["id"]: it for it in items if it["origin"] == "video"}
    assert sorted(vids) == ["video-301-1", "video-301-2"]       # 마감 없음·너무 오래된 영상은 항목 없음
    one = vids["video-301-1"]
    assert (one["kind"], one["method"], one["default_decision"], one["canvas_submitted"]) == ("과제", "lms", "approved", True)
    assert one["video"]["duration"] == 900 and parse_iso(one["video"]["late_at"]) == at(5)
    assert one["notes"][0].startswith("출석 인정 기간: ") and any(n.startswith("지각 인정") for n in one["notes"])
    assert report["videos"] == 2
    s = Store(tmp_path / "s.json")
    s.data["cache"] = cache
    core.rebuild(s)
    by = {v["id"]: v for v in s.view()}
    assert (by["video-301-1"]["status"], by["video-301-1"]["submitted"], by["video-301-1"]["submitted_source"]) == (
        "scheduled", True, "canvas")                              # 시청 완료 = Canvas 과제의 제출 확인과 같은 길
    assert by["video-301-2"]["status"] == "scheduled" and by["video-301-2"]["submitted"] is False
    # 화면 상태(store.view)에 영상 칸이 그대로 온다(시청 기간·지각 기한 표시용) — 영상이 아니면 None
    v1 = by["video-301-1"]["video"]
    assert set(v1) == {"unlock_at", "due_at", "late_at", "lock_at", "duration", "attendance"}
    assert parse_iso(v1["unlock_at"]) == at(-2, 0, 0, 0) and parse_iso(v1["late_at"]) == at(5) and v1["duration"] == 900
    assert parse_iso(by["video-301-2"]["video"]["due_at"]) == at(9) and by["video-301-2"]["video"]["late_at"] is None
    # 사용자가 '안 봤음'으로 바꾸면 Canvas 과제처럼 사용자 쪽이 이긴다
    s.update_user("video-301-1", {"submitted": False}, by="user")
    v = next(x for x in s.view() if x["id"] == "video-301-1")
    assert (v["submitted"], v["submitted_source"]) == (False, "user")
    s.data["course_prefs"] = {"301": {"mode": "hidden"}}
    assert not [v for v in s.view() if v["origin"] == "video"]   # 제외한 과목은 보이지 않는다(계약 15장)


def test_week_summary_and_calendar_titles(tmp_path):
    s = Store(tmp_path / "s.json")
    s.data["cache"] = _cache(_v("1", z(1), completed=True), _v("2", z(1, 20, 0, 0)))
    core.rebuild(s)
    items, summaries = core.calendar_payload(s)
    titles = {event_summary(it) for it in items}
    assert titles == {"✔ [영상] [영상 과목] 영상 1", "[영상] [영상 과목] 영상 2"}   # 시청 완료 표시 ✔는 맨 앞
    week = [x for x in summaries if x["uid"].startswith("week-")]
    assert week and "영상 2" in week[0]["summary"] and "마감" not in week[0]["summary"]
    assert "[영상] 영상 1 (시청 완료)" in week[0]["description"]
    text = build_ics(items, summaries=summaries)
    assert validate_ics(text) == []
    flat = text.replace("\r\n ", "")
    assert "SUMMARY:[영상] [영상 과목] 영상 2" in flat and "UID:video-301-2@lms-agent" in flat
    assert "CATEGORIES:강의 영상" in flat and "강의 영상 시청(출석)" in flat


# ---------------------------------------------------------------- 가짜 Canvas로 수집

def test_demo_collects_three_videos(env, tmp_path):
    mock, base, store = env
    report = sync(base, store)
    cache = store.data["cache"]
    assert cache["errors"] == [] and report["videos"] == 3
    assert cache["lx_video_tools"]["101"] == VIDEO_TOOL_ID and cache["lx_video_tools"]["102"] == VIDEO_TOOL_ID
    assert cache["lx_video_tools"]["103"] is None
    by = {v["id"]: v for v in videos_in(store)}
    assert len(by) == 3
    done = [v for v in by.values() if v["submitted"]]
    assert len(done) == 1 and done[0]["course_id"] == "101"
    asic6 = [v for v in by.values() if v["course_id"] == "101" and not v["submitted"]]
    assert [parse_iso(v["due"]).date() for v in asic6] == [T + timedelta(days=6)]      # 'ASIC 영상 6일 남음'
    dsp = [v for v in by.values() if v["course_id"] == "102"]
    assert [parse_iso(v["due"]).date() for v in dsp] == [T + timedelta(days=14)]
    assert all(v["status"] == "scheduled" and v["url"].startswith(base) for v in by.values())
    # 영상이 아닌 것(자료 PDF·실시간 수업)은 기록에도 없다
    assert len(cache["videos"]) == 3
    # LearningX를 안 쓰는 과목에는 영상 요청이 없다
    assert not [p for p in mock.requests if "/learningx/api/v1/courses/103" in p]
    assert not [p for p in mock.requests if "allcomponents_db" in p or "/profile" in p]


def test_ids_stable_across_syncs_and_google_diff(env, tmp_path):
    """두 번 동기화해도 영상 id가 같다 → 구글 반영 두 번째는 쓰기 0번(일정 id가 이어진다)."""
    from lms_agent.gcal import GoogleCalendar, OAuthFlow
    from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls
    import urllib.error
    import urllib.parse
    import urllib.request

    mock, base, store = env
    sync(base, store)
    first = sorted(v["id"] for v in videos_in(store))
    sync(base, store)
    assert sorted(v["id"] for v in videos_in(store)) == first and len(first) == 3

    desired = gsync.desired_events(store)
    vids = [e for e in desired if e["lms_id"].startswith("video-")]
    assert len(vids) == 3 and all(e["summary"].lstrip("✔ ").startswith("[영상] ") for e in vids)

    gsrv, gbase = start_mock_google()
    try:
        u = urls(gbase)
        redirect = "http://127.0.0.1:1/"

        class NoFollow(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        flow = OAuthFlow(DEMO_CLIENT_ID, redirect, DEMO_CLIENT_SECRET, auth_url=u["auth_url"], token_url=u["token_url"])
        try:
            urllib.request.build_opener(NoFollow).open(flow.authorization_url(), timeout=5)
            raise AssertionError("리다이렉트가 없음")
        except urllib.error.HTTPError as e:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(e.headers["Location"]).query))
        cal = GoogleCalendar(flow.exchange(q["code"], q["state"]), DEMO_CLIENT_ID, DEMO_CLIENT_SECRET,
                             api_base=u["api_base"], token_url=u["token_url"], backoff=(0, 0))
        cid = cal.ensure_calendar(None)
        r1 = cal.sync(cid, desired)
        assert r1["errors"] == [] and all(e["lms_id"] in r1["events"] for e in vids)
        sync(base, store)
        r2 = cal.sync(cid, gsync.desired_events(store))
        assert (r2["created"], r2["updated"], r2["deleted"]) == (0, 0, 0)
        assert all(r2["events"][e["lms_id"]]["event_id"] == r1["events"][e["lms_id"]]["event_id"] for e in vids)
    finally:
        gsrv.shutdown()
        gsrv.server_close()


def test_video_failure_keeps_last_and_sync_continues(env):
    mock, base, store = env
    sync(base, store)
    before = sorted(v["id"] for v in videos_in(store))
    mock.lx_video_fail = 500
    report = sync(base, store)
    cache = store.data["cache"]
    errs = [e for e in cache["errors"] if "강의 영상" in e]
    assert len(errs) == 1 and "ASIC" in errs[0] and "디지털신호처리" in errs[0]   # 과목이 여럿이어도 한 줄
    assert DEMO_TOKEN not in errs[0] and not any(t in errs[0] for t in mock.lx_tokens)
    assert sorted(v["id"] for v in videos_in(store)) == before                  # 지난번 영상 그대로
    assert report["candidates"] > 0 and any(b.get("board_id") == 9101 for b in cache["boards"])   # 다른 수집은 계속


def test_first_failure_uses_canvas_modules_fallback(env):
    mock, base, store = env
    mock.lx_video_fail = 500
    mock.data["canvas_modules"]["102"] = [{"id": 1, "name": "13주차", "items": [
        {"id": 41, "type": "ExternalTool", "title": "13주차: FIR 필터 설계",
         "external_url": f"{base}/learningx/lti/lecture_attendance/items/view/62001",
         "html_url": f"{base}/courses/102/modules/items/41",
         "content_details": {"due_at": z(14)}, "completion_requirement": {"type": "must_view", "completed": False}}]}]
    sync(base, store)
    assert [v["id"] for v in videos_in(store)] == ["video-102-62001"]          # 나중에 LearningX가 되살아나도 같은 id
    assert len([e for e in store.data["cache"]["errors"] if "강의 영상" in e]) == 1
    assert any(p.startswith("/api/v1/courses/102/modules?") and "content_details" in p for p in mock.requests)


def test_old_learningx_allcomponents_uses_profile_login_only_in_memory(env):
    mock, base, store = env
    mock.data["user"]["login_id"] = "fake-login-7731"
    mock.lx_video_api = "allcomponents"
    sync(base, store)
    assert sorted(v["id"] for v in videos_in(store)) == ["video-101-61001", "video-101-61003", "video-102-62001"]
    assert store.data["cache"]["errors"] == []
    assert [p for p in mock.requests if p == "/api/v1/users/self/profile"] == ["/api/v1/users/self/profile"]  # 한 번만
    assert "fake-login-7731" not in json.dumps(store.data, ensure_ascii=False)   # 저장하지 않는다
    assert "fake-login-7731" not in store.path.read_text(encoding="utf-8")


def test_no_video_api_is_quiet(env):
    mock, base, store = env
    mock.lx_video_api = "none"
    sync(base, store)
    assert videos_in(store) == [] and store.data["cache"]["errors"] == []


def test_board_token_tried_when_no_video_tool(env, monkeypatch):
    """주차학습 도구를 못 찾고 게시판 도구만 있으면 게시판 출입증으로 한 번 — 거절(403)이면 조용히 넘어간다."""
    mock, base, store = env
    monkeypatch.setattr(LearningX, "video_tool", staticmethod(lambda canvas, cid, tools=None: None))
    sync(base, store)
    assert sorted(v["id"] for v in videos_in(store)) == ["video-101-61001", "video-101-61003"]   # 102는 게시판도 없음
    mock.lx_video_own_token = True
    sync(base, store, full=True)
    assert videos_in(store) == [] and not [e for e in store.data["cache"]["errors"] if "강의 영상" in e]


def test_app_state_with_videos(env, tmp_path):
    """화면 상태 전체(학기·주간 요약·코치·체크리스트·계획·성적 연결)가 영상 항목과 함께 만들어진다."""
    mock, base, store = env
    sync(base, store)
    st = App(store, tmp_path, cfg={"canvas_url": base}).state()
    vids = [i for i in st["items"] if i["origin"] == "video"]
    assert len(vids) == 3 and all(i.get("week") for i in vids)
    sem = core.semester_info(store.data["cache"], store.view())
    items = core.annotate(store.view(), sem, store.data["cache"])
    gradebook.link_items(store, items, sem)
    assert all(i["component"] is None for i in items if i["origin"] == "video")   # 영상은 평가 항목이 아니다(데모 제목)
    summaries = core.weekly_summaries(items, sem)
    assert any("영상" in s["summary"] for s in summaries)
    text = core.calendar_text(store)
    flat = text.replace("\r\n ", "")
    assert validate_ics(text) == []
    assert flat.count("SUMMARY:[영상] [") == 2 and flat.count("SUMMARY:✔ [영상] [") == 1
