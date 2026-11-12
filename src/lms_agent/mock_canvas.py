"""데모·테스트용 가짜 Canvas 서버.

docs/CONTRACTS.md 2절의 시나리오를 today 기준 상대 날짜로 만들어 canvas.py가 쓰는 엔드포인트를 흉내 낸다.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO_TOKEN = "demo-token"
ASSIGNMENT_PAGE_SIZE = 2
USER_ID = 1
# strftime("%b")는 윈도 한국어 로캘에서 "11월"이 나오므로 직접 둔다
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = "월화수목금토일"


def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo


def build_demo_data(today: date | None = None, tz: tzinfo | None = None, base_url: str = "") -> dict:
    """데모 시나리오. 시각은 tz 기준 로컬 시각을 UTC 'Z' 문자열로 바꿔 넣는다.

    base_url을 주면 html_url이 절대 주소가 된다(없으면 /courses/... 경로).
    """
    today = today or date.today()
    tz = tz or _local_tz()

    def day(n: int) -> date:
        return today + timedelta(days=n)

    def at(n: int, hh: int = 0, mm: int = 0) -> str:
        local = datetime.combine(day(n), time(hh, mm), tzinfo=tz)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def md(n: int) -> str:
        return f"{day(n).month}월 {day(n).day}일"

    def assignment(course, aid, name, due, types, state, created, description="", submitted_at=None, score=None):
        return {
            "id": aid, "course_id": course, "name": name, "description": description,
            "due_at": due, "lock_at": None, "unlock_at": None, "points_possible": 10.0,
            "submission_types": types, "published": True,
            "created_at": at(created, 9), "updated_at": at(created + 1, 9),
            "html_url": f"{base_url}/courses/{course}/assignments/{aid}",
            "submission": {"assignment_id": aid, "user_id": USER_ID, "score": score,
                           "workflow_state": state, "submitted_at": submitted_at},
        }

    def announcement(course, aid, posted, title, message):
        return {
            "id": aid, "title": title, "message": message, "posted_at": posted,
            "delayed_post_at": None, "context_code": f"course_{course}", "read_state": "unread",
            "html_url": f"{base_url}/courses/{course}/discussion_topics/{aid}",
        }

    def conversation(cid, subject, code, name, sent, body, author):
        return {
            "id": cid, "subject": subject, "workflow_state": "unread",
            "last_message": body, "last_message_at": sent, "last_authored_message_at": None,
            "message_count": 1, "context_code": code, "context_name": name,
            "participants": [{"id": USER_ID, "name": "데모 학생"}, author],
            "messages": [{"id": cid * 10 + 1, "author_id": author["id"], "created_at": sent,
                          "body": body, "generated": False, "attachments": [],
                          "media_comment": None, "forwarded_messages": []}],
        }

    lab4_due, syl, cap = day(6), (-60, 12, 30), day(18)
    syllabus = (
        f"<table><tr><td>1주차</td><td>{md(syl[0])} 오리엔테이션</td></tr>"
        f"<tr><td>과제 2</td><td>{md(syl[1])}까지 이메일 제출</td></tr>"
        f"<tr><td>기말고사</td><td>{md(syl[2])} 15:00</td></tr></table>"
    )
    # 101은 고려대식 긴 과목명(short_name 줄이기 확인용). 103은 학교가 점수를 숨긴 경우(None).
    courses = [
        {"id": 101, "name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반", "course_code": "EE401",
         "syllabus_body": None},
        {"id": 102, "name": "디지털신호처리", "course_code": "EE342", "syllabus_body": None},
        {"id": 103, "name": "반도체공학", "course_code": "EE361", "syllabus_body": syllabus},
        {"id": 104, "name": "Capstone Design", "course_code": "CAP400", "syllabus_body": None},
    ]
    scores = {101: 92.5, 102: 81.0, 103: None, 104: 88.0}
    term = {"id": 1, "name": "데모 학기", "start_at": at(-70, 0), "end_at": at(41, 23, 59)}
    for c in courses:
        c.update(start_at=at(-70, 9), end_at=at(41, 18), workflow_state="available", enrollment_term_id=1,
                 term=term,
                 enrollments=[{"type": "student", "role": "StudentEnrollment", "enrollment_state": "active",
                               "computed_current_score": scores[c["id"]]}])

    lab4_desc = ('<p>LAB4 보고서는 조교 이메일(<a href="mailto:asic.ta@example.ac.kr">asic.ta@example.ac.kr</a>)'
                 '로 제출합니다.</p><p>마감은 공지사항을 확인하세요.</p>')
    assignments = {
        "101": [
            assignment(101, 1011, "LAB3 보고서", at(-5, 23, 59), ["online_upload"], "graded", -19,
                       submitted_at=at(-5, 21, 47), score=9.0),
            assignment(101, 1012, "LAB4 보고서", None, ["none"], "unsubmitted", -12, description=lab4_desc),
            assignment(101, 1013, "LAB5 예비보고서", at(10, 23, 59), ["online_upload"], "unsubmitted", -2),
        ],
        "102": [assignment(102, 1021, "Homework 5", at(4, 18), ["online_upload"], "unsubmitted", -9)],
        "103": [],
        "104": [assignment(104, 1041, "Weekly log 9", None, ["none"], "unsubmitted", -7)],
    }
    announcements = [
        announcement(101, 9001, at(-3, 10), "LAB4 보고서 제출 안내",
                     f"<p>LAB4 보고서 마감: {md(6)}({WEEKDAYS[lab4_due.weekday()]}) 23:59</p>"
                     "<p>제출: asic.ta@example.ac.kr 로 PDF 첨부</p>"),
        announcement(102, 9002, at(-2, 9), "HW5 안내",
                     f"<p>Homework 5는 Canvas 과제함으로 제출하세요. 마감은 {day(4).month}/{day(4).day} 18:00입니다.</p>"),
        announcement(102, 9003, at(-1, 13), "중간 프로젝트 보고서",
                     "<p>중간 프로젝트 보고서는 이메일(dsp.prof@example.ac.kr)로 다음 주 금요일까지 보내 주세요.</p>"),
        announcement(102, 9004, at(-1, 8), "휴강 안내", f"<p>{md(2)} 휴강입니다. 보강은 추후 공지합니다.</p>"),
        announcement(104, 9005, at(-4, 11), "Final report",
                     f"<p>Please email your final report to capstone@example.ac.kr by "
                     f"{MONTHS[cap.month - 1]} {cap.day}, 11:59 PM.</p>"),
    ]
    conversations = [
        conversation(7001, "LAB4 보고서 마감 연장", "course_101", "ASIC설계", at(-1, 20),
                     f"LAB4 보고서 마감이 {md(8)} 자정으로 연장되었습니다. 메일로 제출해 주세요.",
                     {"id": 201, "name": "ASIC설계 조교"}),
        conversation(7002, "장학금 서류 제출", "user_1", None, at(-2, 14), "확인했습니다.",
                     {"id": 202, "name": "학과 사무실"}),
        # 개별 성적 안내(성적 기능 데모용): 점수·평균·표준편차가 한 문장에
        conversation(7003, "중간고사 성적 안내", "course_102", "디지털신호처리", at(-6, 18),
                     "중간고사 성적 안내드립니다. 본인 점수 78점 (평균 65.3, 표준편차 12.1)입니다.",
                     {"id": 203, "name": "디지털신호처리 조교"}),
    ]
    return {
        "user": {"id": USER_ID, "name": "데모 학생", "short_name": "데모 학생",
                 "sortable_name": "학생, 데모", "login_id": "demo"},
        "courses": courses,
        "assignments": assignments,
        "announcements": announcements,
        "conversations": conversations,
    }


def _query_day(values: list | None, default: date) -> date:
    try:
        return date.fromisoformat(values[0][:10]) if values else default
    except ValueError:
        return default


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockCanvas/1.0"

    def log_message(self, format, *args):  # 요청 로그를 찍지 않는다
        pass

    def _send(self, status: int, obj, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self._send(404, {"errors": [{"message": "The specified resource does not exist."}]})

    def do_GET(self) -> None:
        srv = self.server
        srv.requests.append(self.path)
        if self.headers.get("Authorization") != f"Bearer {DEMO_TOKEN}":
            return self._send(401, {"errors": [{"message": "Invalid access token."}]},
                              {"WWW-Authenticate": 'Bearer realm="canvas-lms"'})
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path.rstrip("/"), urllib.parse.parse_qs(url.query)
        data = srv.data
        if path == "/api/v1/users/self":
            return self._send(200, data["user"])
        if path == "/api/v1/courses":
            keep = "syllabus_body" in query.get("include[]", [])
            return self._send(200, [c if keep else {k: v for k, v in c.items() if k != "syllabus_body"}
                                    for c in data["courses"]])
        m = re.fullmatch(r"/api/v1/courses/([^/]+)/assignments", path)
        if m:
            return self._assignments(path, m.group(1), url.query)
        if path == "/api/v1/announcements":
            return self._send(200, self._announcements(query))
        if path == "/api/v1/conversations":
            return self._send(200, [{k: v for k, v in c.items() if k != "messages"}
                                    for c in sorted(data["conversations"],
                                                    key=lambda c: c["last_message_at"], reverse=True)])
        m = re.fullmatch(r"/api/v1/conversations/([^/]+)", path)
        if m:
            conv = next((c for c in data["conversations"] if str(c["id"]) == m.group(1)), None)
            if conv is None:
                return self._not_found()
            if query.get("auto_mark_as_read", ["true"])[0].lower() not in ("false", "0"):
                conv["workflow_state"] = "read"  # 진짜 Canvas처럼 읽음 처리된다
            return self._send(200, conv)
        self._not_found()

    def _assignments(self, path: str, course_id: str, raw_query: str) -> None:
        items = self.server.data["assignments"].get(course_id)
        if items is None:
            return self._not_found()
        pairs = urllib.parse.parse_qsl(raw_query)
        params = [(k, v) for k, v in pairs if k != "page"]
        try:
            page = max(1, int(dict(pairs).get("page", 1)))
        except ValueError:
            page = 1
        if "submission" not in [v for k, v in pairs if k == "include[]"]:
            items = [{k: v for k, v in a.items() if k != "submission"} for a in items]
        size = ASSIGNMENT_PAGE_SIZE
        last = max(1, -(-len(items) // size))

        def link(n: int, rel: str) -> str:
            qs = urllib.parse.urlencode(params + [("page", n)])
            return f'<{self.server.base_url}{path}?{qs}>; rel="{rel}"'

        links = [link(page, "current")]
        if page < last:
            links.append(link(page + 1, "next"))
        if page > 1:
            links.append(link(page - 1, "prev"))
        links += [link(1, "first"), link(last, "last")]
        self._send(200, items[(page - 1) * size:page * size], {"Link": ",".join(links)})

    def _announcements(self, query: dict) -> list:
        srv = self.server
        codes = set(query.get("context_codes[]", []))
        start = _query_day(query.get("start_date"), srv.today - timedelta(days=14))
        end = _query_day(query.get("end_date"), start + timedelta(days=28))
        found = [a for a in srv.data["announcements"] if a["context_code"] in codes
                 and start <= _utc(a["posted_at"]).astimezone(srv.tz).date() <= end]
        return sorted(found, key=lambda a: a["posted_at"], reverse=True)


def start_mock_canvas(port: int = 0, today: date | None = None,
                      tz: tzinfo | None = None) -> tuple[ThreadingHTTPServer, str]:
    """가짜 Canvas를 127.0.0.1에 백그라운드(daemon) 스레드로 띄우고 (server, base_url)을 돌려준다.

    server.requests에 받은 요청의 경로+쿼리가 쌓인다. 끌 때는 server.shutdown(); server.server_close().
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    server.today = today or date.today()
    server.tz = tz or _local_tz()
    server.data = build_demo_data(server.today, server.tz, server.base_url)
    server.requests = []
    threading.Thread(target=server.serve_forever, args=(0.1,), name="mock-canvas", daemon=True).start()
    return server, server.base_url
