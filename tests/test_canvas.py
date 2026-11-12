"""canvas.py 클라이언트를 가짜 Canvas(mock_canvas)와 작은 대본 서버에 붙여 확인한다."""
from __future__ import annotations

import socket
import threading
import time as _time
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lms_agent.canvas import Canvas, CanvasError, next_link, parse_json
from lms_agent.mock_canvas import DEMO_TOKEN, build_demo_data, start_mock_canvas

TODAY = date(2026, 11, 12)  # 목요일
KST = timezone(timedelta(hours=9))


def at(n: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime.combine(TODAY + timedelta(days=n), time(hh, mm), tzinfo=KST)


@pytest.fixture
def mock():
    server, base = start_mock_canvas(today=TODAY, tz=KST)
    yield server, Canvas(base, DEMO_TOKEN)
    server.shutdown()
    server.server_close()


# ---- 응답을 마음대로 정하는 작은 서버 (다른 호스트·오류·리다이렉트 확인용) ----

class _Scripted(BaseHTTPRequestHandler):
    def do_GET(self):
        srv = self.server
        srv.hits.append(self.path)
        srv.auth.append(self.headers.get("Authorization"))
        status, body, headers = srv.routes.get(self.path.split("?")[0], (404, b"{}", {}))
        _time.sleep(srv.delay)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def scripted(delay: float = 0):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Scripted)
    srv.routes, srv.hits, srv.auth, srv.delay = {}, [], [], delay
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


# ---- 가짜 Canvas ----

def test_demo_data_dates_are_relative_and_utc():
    data = build_demo_data(TODAY, KST, "http://x")
    a = {x["id"]: x for xs in data["assignments"].values() for x in xs}
    assert a[1013]["due_at"] == "2026-11-22T14:59:00Z"  # T+10 23:59 KST
    assert a[1021]["due_at"] == "2026-11-16T09:00:00Z"  # T+4 18:00 KST
    assert a[1012]["due_at"] is None and a[1012]["html_url"] == "http://x/courses/101/assignments/1012"
    ann = {x["id"]: x for x in data["announcements"]}
    assert "11월 18일(수) 23:59" in ann[9001]["message"]
    assert "Nov 30, 11:59 PM" in ann[9005]["message"]
    assert ann[9001]["posted_at"] == "2026-11-09T01:00:00Z"  # T-3 10:00 KST


def test_whoami(mock):
    _, cv = mock
    assert cv.whoami()["id"] == 1
    assert DEMO_TOKEN not in repr(cv)


def test_courses_include_syllabus(mock):
    _, cv = mock
    courses = {c["id"]: c for c in cv.courses()}
    assert sorted(courses) == [101, 102, 103, 104, 105]   # 105: 정규 수업이 아닌 교육(계약 15장 '제외' 제안)
    assert "ASIC설계" in courses[101]["name"] and courses[101]["course_code"] == "EE401"
    assert "기말고사" in courses[103]["syllabus_body"]
    assert courses[101]["enrollments"][0]["computed_current_score"] == 92.5
    assert courses[101]["term"]["start_at"]


def test_assignments_follow_pagination(mock):
    server, cv = mock
    items = cv.assignments(101)
    assert [a["id"] for a in items] == [1011, 1012, 1013]
    assert items[0]["submission"]["workflow_state"] == "graded" and items[0]["submission"]["score"] == 9.0
    assert items[1]["due_at"] is None and items[1]["submission_types"] == ["none"]
    pages = [p for p in server.requests if p.startswith("/api/v1/courses/101/assignments")]
    assert len(pages) == 2 and "page=2" in pages[1]
    assert cv.assignments(103) == []


def test_announcements_filter_by_course_and_date(mock):
    _, cv = mock
    every = cv.announcements([101, 102, 103, 104], at(-7), at(0))
    assert sorted(a["id"] for a in every) == [9001, 9002, 9003, 9004, 9005]
    assert [a["id"] for a in cv.announcements([101], at(-7), at(0))] == [9001]
    recent = cv.announcements(["101", "course_102", 104], at(-2), at(0))
    assert sorted(a["id"] for a in recent) == [9002, 9003, 9004]


def test_announcements_split_context_codes_by_ten(mock):
    server, cv = mock
    got = cv.announcements(list(range(1, 11)) + [101, 102], at(-7), at(0))
    assert sorted(a["id"] for a in got) == [9001, 9002, 9003, 9004]
    assert cv.announcements([], at(-7), at(0)) == []
    assert len([p for p in server.requests if p.startswith("/api/v1/announcements")]) == 2


def test_conversations_since_limit_and_not_marked_read(mock):
    server, cv = mock
    got = cv.conversations(at(-3))
    assert [c["id"] for c in got] == [7001, 7002]
    assert got[0]["context_code"] == "course_101" and got[0]["context_name"] == "ASIC설계"
    assert got[0]["messages"][0]["body"].startswith("LAB4 보고서 마감이")
    details = [p for p in server.requests if p.startswith("/api/v1/conversations/")]
    assert len(details) == 2 and all("auto_mark_as_read=false" in p for p in details)
    assert all(c["workflow_state"] == "unread" for c in server.data["conversations"])
    assert [c["id"] for c in cv.conversations(at(-1))] == [7001]
    assert len(cv.conversations(at(-30), limit=1)) == 1


def test_bad_token_is_korean_and_hides_token(mock):
    server, _ = mock
    with pytest.raises(CanvasError) as e:
        Canvas(server.base_url, "secret-abc123").whoami()
    assert "토큰" in str(e.value) and e.value.status == 401
    assert "secret-abc123" not in str(e.value) + repr(e.value)


def test_malformed_token_rejected_without_echo():
    with pytest.raises(CanvasError) as e:
        Canvas("https://canvas.example.ac.kr", "abc\r\nX-Evil: 1")
    assert "abc" not in str(e.value)


# ---- 페이지·파싱 ----

@pytest.mark.parametrize("make_next", [
    lambda main, other: f"{other.base}/api/v1/courses?page=2",                      # 포트가 다름
    lambda main, other: main.base.replace("127.0.0.1", "localhost") + "/api/v1/courses?page=2",  # 호스트가 다름
    lambda main, other: main.base.replace("http://", "https://") + "/api/v1/courses?page=2",     # scheme이 다름
])
def test_next_link_to_other_origin_is_not_followed(make_next):
    with scripted() as main, scripted() as other:
        main.routes["/api/v1/courses"] = (200, b'[{"id": 1, "name": "A"}]',
                                          {"Link": f'<{make_next(main, other)}>; rel="next"'})
        other.routes["/api/v1/courses"] = (200, b'[{"id": 2, "name": "B"}]', {})
        assert [c["id"] for c in Canvas(main.base, "tok").courses()] == [1]
        assert len(main.hits) == 1 and other.hits == []


def test_same_origin_next_link_prefix_and_restricted_course():
    with scripted() as s:
        s.routes["/api/v1/courses"] = (200, b'[{"id": 1, "name": "A"}]',
                                       {"Link": f'<{s.base}/page2?x=1>; rel="next"'})
        s.routes["/page2"] = (200, b'while(1);[{"id": 2, "name": "B"}, {"id": 3, "access_restricted_by_date": true}]', {})
        assert [c["id"] for c in Canvas(s.base, "tok").courses()] == [1, 2]
        assert s.auth == ["Bearer tok", "Bearer tok"]


def test_object_response_is_tolerated():
    with scripted() as s:
        s.routes["/api/v1/courses/5/assignments"] = (200, b'{"assignments": [{"id": 9}]}', {})
        s.routes["/api/v1/courses/6/assignments"] = (200, b'{"id": 10, "name": "only"}', {})
        cv = Canvas(s.base, "tok")
        assert [a["id"] for a in cv.assignments(5)] == [9]
        assert [a["id"] for a in cv.assignments(6)] == [10]


def test_parse_json_strips_while1_prefix():
    assert parse_json(b'while(1);[{"id": 1}]') == [{"id": 1}]
    assert parse_json('﻿while(1);{"a": "한글"}') == {"a": "한글"}
    assert parse_json(b"[1, 2]") == [1, 2]
    assert parse_json(b"") is None
    with pytest.raises(CanvasError):
        parse_json(b"<html>login</html>")


def test_next_link_parsing():
    h = ('<https://c.example/api?page=1>; rel="current",<https://c.example/api?page=2>; rel="next",'
         '<https://c.example/api?page=9>; rel="last"')
    assert next_link(h) == "https://c.example/api?page=2"
    assert next_link('<https://c.example/a>; rel="last"') is None
    assert next_link(None) is None


# ---- 오류·리다이렉트 ----

@pytest.mark.parametrize("status, word", [(403, "권한"), (404, "찾을 수 없"), (500, "서버 오류")])
def test_http_errors_are_korean(status, word):
    with scripted() as s:
        s.routes["/api/v1/users/self"] = (status, b'{"errors": []}', {})
        with pytest.raises(CanvasError) as e:
            Canvas(s.base, "tok-123").whoami()
    assert word in str(e.value) and e.value.status == status and "tok-123" not in str(e.value)


def test_connection_failure_and_timeout():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(CanvasError, match="연결"):
        Canvas(f"http://127.0.0.1:{port}", "tok", timeout=5).whoami()
    with scripted(delay=1.5) as s:
        s.routes["/api/v1/users/self"] = (200, b"{}", {})
        with pytest.raises(CanvasError, match="시간 초과"):
            Canvas(s.base, "tok", timeout=0.3).whoami()


def test_redirect_drops_token_for_other_origin():
    with scripted() as main, scripted() as other:
        main.routes["/api/v1/users/self"] = (302, b"", {"Location": f"{other.base}/me"})
        other.routes["/me"] = (200, b'{"id": 1}', {})
        assert Canvas(main.base, "tok").whoami() == {"id": 1}
        assert other.auth == [None]
        main.routes["/api/v1/users/self"] = (302, b"", {"Location": "/me"})
        main.routes["/me"] = (200, b'{"id": 2}', {})
        assert Canvas(main.base, "tok").whoami() == {"id": 2}
        assert main.auth[-2:] == ["Bearer tok", "Bearer tok"]
