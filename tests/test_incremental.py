"""M1 완료 기준: 매번 전부 읽지 않고, 바뀐 곳만 탐색 목표로 잡아 읽는다.

1. 아무것도 안 바뀐 두 번째 실행 → 본문 요청 0개, 마감 추출 0회, '바뀐 곳 없음'.
2. 새 공지 1 · 새 메시지 1 · 과제 설명 수정 1 → 그 3곳만 읽고, 기존 승인 결정은 유지.
3. 실행마다 대상과 이유가 기록되고, 새 항목에는 근거 사슬(처음 발견 → 승인 → 파일 포함)이 붙는다.
측정은 가짜 Canvas의 요청 기록(server.requests)과 실행 보고서로 한다.
"""
from datetime import datetime, time, timedelta, timezone

import pytest

from lms_agent import core
from lms_agent.canvas import Canvas
from lms_agent.extract import RULES_VERSION
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.store import Store
from lms_agent.timeutil import local_tz

T = datetime.now().astimezone().date()


def utc(days, h=9, m=0):
    local = datetime.combine(T + timedelta(days=days), time(h, m), tzinfo=local_tz())
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def env(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    first = core.sync(Canvas(base, DEMO_TOKEN), store)
    yield mock, base, store, first
    mock.shutdown()


def again(mock, base, store, **kw):
    mock.requests.clear()
    return core.sync(Canvas(base, DEMO_TOKEN), store, **kw)


def test_first_run_reads_everything(env):
    _, _, store, first = env
    assert (first["mode"], first["reason"]) == ("full", "처음 실행")
    assert first["checked"] > 0 and first["skipped"] == 0
    assert all(t["reason"] == "처음 실행" for t in first["targets"])


def test_unchanged_second_run_reads_nothing(env):
    mock, base, store, _ = env
    items_before = [(v["id"], v["due"], v["status"]) for v in store.view()]
    r = again(mock, base, store)
    assert r["mode"] == "incremental"
    assert r["checked"] == 0 and r["targets"] == [] and r["skipped"] > 0
    assert r["conversations_fetched"] == 0
    assert not [p for p in mock.requests if "/conversations/" in p]  # 본문 요청 0개
    assert r["new_items"] == []
    assert [(v["id"], v["due"], v["status"]) for v in store.view()] == items_before


def test_only_changed_sources_are_explored(env):
    mock, base, store, _ = env
    cap = next(v for v in store.view() if "Capstone" in v["course_name"] and v["origin"] == "found")
    store.update_user(cap["id"], {"decision": "approved"})

    d = mock.data
    # 1) 새 공지
    d["announcements"].append({
        "id": 9100, "title": "LAB5 안내", "posted_at": utc(0, 9), "delayed_post_at": None,
        "context_code": "course_101", "read_state": "unread", "html_url": f"{base}/courses/101/discussion_topics/9100",
        "message": f"<p>LAB5 결과보고서는 {(T + timedelta(days=13)).month}월 {(T + timedelta(days=13)).day}일 18:00까지 메일로 제출</p>"})
    # 2) 기존 대화에 새 메시지
    conv = next(c for c in d["conversations"] if c["id"] == 7001)
    conv["last_message_at"] = utc(0, 10)
    conv["messages"].insert(0, {"id": 70012, "author_id": 201, "created_at": utc(0, 10), "generated": False,
                                "attachments": [], "media_comment": None, "forwarded_messages": [],
                                "body": "중간 점검은 다음 주 월요일까지 메일로 보내 주세요."})
    # 3) 과제 설명 수정
    a = next(x for x in d["assignments"]["104"] if x["id"] == 1041)
    a["description"] = "<p>Weekly log 9는 이번 주 금요일까지 이메일로 제출합니다.</p>"
    a["updated_at"] = utc(0, 11)

    r = again(mock, base, store)
    got = sorted((t["key"], t["reason"]) for t in r["targets"])
    assert got == [("announcement:9100", "새 글"), ("assignment:1041", "새 글"), ("inbox:7001-70012", "새 글")]
    assert r["conversations_fetched"] == 1
    assert [p for p in mock.requests if "/conversations/" in p] == [p for p in mock.requests if "/conversations/7001" in p]
    assert len(r["new_items"]) == 3
    # 기존 승인 결정은 그대로
    assert next(v for v in store.view() if v["id"] == cap["id"])["status"] == "scheduled"


def test_edited_announcement_is_detected_by_hash(env):
    mock, base, store, _ = env
    n = next(x for x in mock.data["announcements"] if x["id"] == 9003)
    n["message"] = n["message"].replace("다음 주 금요일", "다음 주 목요일")  # 시각 정보 없이 슬쩍 고침
    r = again(mock, base, store)
    assert [(t["key"], t["reason"]) for t in r["targets"]] == [("announcement:9003", "내용 바뀜")]


def test_rules_update_and_manual_full_rescan(env):
    mock, base, store, _ = env
    store.data["rules_version"] = "old"
    r = again(mock, base, store)
    assert (r["mode"], r["reason"]) == ("full", "규칙 업데이트") and r["skipped"] == 0
    r = again(mock, base, store, full=True)
    assert (r["mode"], r["reason"]) == ("full", "전체 다시 확인")
    assert store.data["rules_version"] == RULES_VERSION


def test_long_absence_catches_everything_since_last_run(env):
    """3주 동안 안 켜도, 비교 기준이 '지난 실행 때 상태'라 그사이 바뀐 것이 한 번에 잡힌다."""
    mock, base, store, _ = env
    for i in range(3):
        mock.data["announcements"].append({
            "id": 9200 + i, "title": f"공지 {i}", "posted_at": utc(-1, 9 + i), "delayed_post_at": None,
            "context_code": "course_102", "read_state": "unread", "html_url": None,
            "message": f"<p>퀴즈 {i + 1}은 {(T + timedelta(days=20 + i)).month}월 {(T + timedelta(days=20 + i)).day}일 15:00</p>"})
    r = again(mock, base, store)
    assert sorted(t["key"] for t in r["targets"]) == ["announcement:9200", "announcement:9201", "announcement:9202"]


def test_evidence_chain_and_run_log(env, tmp_path):
    mock, base, store, _ = env
    it = next(v for v in store.view() if v["origin"] == "found" and v["status"] == "review")
    assert it["first_seen"] and it["decided_at"] is None and it["exported_at"] is None
    store.update_user(it["id"], {"decision": "approved"})
    core.calendar_text(store, record=True)
    v = next(x for x in store.view() if x["id"] == it["id"])
    assert v["decided_at"] and v["exported_at"] and v["export_stale"] is False
    store.update_user(it["id"], {"title": "제목 바꿈"})
    assert next(x for x in store.view() if x["id"] == it["id"])["export_stale"] is True  # 파일을 다시 받아야 함
    again(mock, base, store)
    runs = store.data["runs"]
    assert len(runs) == 2 and runs[0]["reason"] == "처음 실행" and runs[1]["checked"] == 0
    # 저장했다 다시 열어도 증분이 이어진다
    store.save()
    reopened = Store(store.path)
    r = core.sync(Canvas(base, DEMO_TOKEN), reopened)
    assert r["mode"] == "incremental" and r["checked"] == 0
