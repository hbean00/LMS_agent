"""데모·테스트용 가짜 Canvas 서버.

docs/CONTRACTS.md 2절의 시나리오를 today 기준 상대 날짜로 만들어 canvas.py가 쓰는 엔드포인트를 흉내 낸다.
10절의 LearningX 게시판도 실제 고려대 사이트와 같은 순서로 흉내 낸다(learningx.py가 쓰는 길):
  Canvas `external_tools` → `sessionless_launch`(Canvas 토큰) → 실행 화면(토큰 없이, verifier 1회용)
  → LTI 1.1 양식 POST(서명 확인, 쿠키 xn_api_token·laravel_session·XSRF-TOKEN) → LearningX API(Bearer xn_api_token)
  → 첨부 `/files/<id>/download?verifier=`(Authorization이 있으면 403) → 302 → `/storage/<id>`(파일 바이트).
인증 규칙: `/api/v1/...`만 Canvas 토큰. 나머지 길은 각자 규칙(위). 실패 흉내는 server.lx_fail(start_mock_canvas 참고).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO_TOKEN = "demo-token"
ASSIGNMENT_PAGE_SIZE = 2
USER_ID = 1
BOARD_TOOL_ID = 5          # LearningX '게시판' 도구 id (CONTRACTS 10절 예시와 같게)
LX_PAGE_SIZE = 20          # LearningX 글 목록 기본 페이지 크기(server.lx_page_size로 바꿈)
LTI_SECRET = "mock-lti-secret"
LTI_ACTION_PATH = "/learningx/lti/learningx_board/boards"
_LX_API = re.compile(r"/learningx/api/v1/learningx_board/courses/([^/]+)/boards(?:/([^/]+)/posts(?:/([^/]+))?)?")
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
    # 104 강의계획서의 P/NP 문장 → 'P/NP 과목' 제안, 105는 정규 수업이 아닌 교육(학기 표시·공지·게시판 없음) → '제외' 제안(계약 15장).
    courses = [
        {"id": 101, "name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반", "course_code": "EE401",
         "syllabus_body": None},
        {"id": 102, "name": "디지털신호처리", "course_code": "EE342", "syllabus_body": None},
        {"id": 103, "name": "반도체공학", "course_code": "EE361", "syllabus_body": syllabus},
        {"id": 104, "name": "Capstone Design", "course_code": "CAP400",
         "syllabus_body": "<p>Grading: this course is graded on a P/NP (Pass/Non-pass) basis.</p>"
                          "<p>Weekly logs and the final report are required.</p>"},
        {"id": 105, "name": "[학생] [한국어] 2026 연구실 안전 교육 - 01분반",
         "course_code": "[학생] [한국어] 2026 연구실 안전 교육 - 01분반", "syllabus_body": None},
    ]
    scores = {101: 92.5, 102: 81.0, 103: None, 104: 88.0, 105: None}
    term = {"id": 1, "name": "데모 학기", "start_at": at(-70, 0), "end_at": at(41, 23, 59)}
    for c in courses:
        c.update(start_at=at(-70, 9), end_at=at(41, 18), workflow_state="available", enrollment_term_id=1,
                 term=term,
                 enrollments=[{"type": "student", "role": "StudentEnrollment", "enrollment_state": "active",
                               "computed_current_score": scores[c["id"]]}])

    # 뒤의 세 문단은 날짜 없는 제출 요건(계약 22-6 체크리스트 데모: 형식·파일 이름·분량·포함할 것·개인 과제·지각 규정)
    lab4_desc = ('<p>LAB4 보고서는 조교 이메일(<a href="mailto:asic.ta@example.ac.kr">asic.ta@example.ac.kr</a>)'
                 '로 제출합니다.</p><p>마감은 공지사항을 확인하세요.</p>'
                 '<p>보고서는 PDF 한 파일로 만들고, 파일 이름은 Report_Lab4_이름_학번.pdf 형식으로 해 주세요.</p>'
                 '<p>분량은 A4 10쪽 이내이며, 시뮬레이션 파형 스크린샷과 Verilog 코드를 보고서에 포함해야 합니다.</p>'
                 '<p>개인 과제입니다. 늦게 제출하면 감점됩니다.</p>')
    # 과제 설명에 링크된 파일(계약 24-3): 실제 Canvas 편집기가 넣는 instructure_file_link 모양. 요건은 PDF 안에만 있다
    # (날짜 없음) → core._linked_files가 가상 게시판 '과제'로 모아 받고, 체크리스트가 그 PDF에서 요건을 찾는다.
    lab5_desc = ('<p>예비보고서 작성 요령은 첨부 파일을 확인하세요: '
                 f'<a class="instructure_file_link instructure_scribd_file" title="Lab5_prelab.pdf" '
                 f'href="{base_url}/courses/101/files/{PRELAB_FILE}?wrap=1" '
                 f'data-api-endpoint="{base_url}/api/v1/courses/101/files/{PRELAB_FILE}" '
                 'data-api-returntype="File">Lab5_prelab.pdf</a></p>')
    assignments = {
        "101": [
            assignment(101, 1011, "LAB3 보고서", at(-5, 23, 59), ["online_upload"], "graded", -19,
                       submitted_at=at(-5, 21, 47), score=9.0),
            assignment(101, 1012, "LAB4 보고서", None, ["none"], "unsubmitted", -12, description=lab4_desc),
            assignment(101, 1013, "LAB5 예비보고서", at(10, 23, 59), ["online_upload"], "unsubmitted", -2,
                       description=lab5_desc),
        ],
        "102": [assignment(102, 1021, "Homework 5", at(4, 18), ["online_upload"], "unsubmitted", -9)],
        "103": [],
        "104": [assignment(104, 1041, "Weekly log 9", None, ["none"], "unsubmitted", -7)],
        "105": [],
    }
    quiz_day = day(5)
    # 실제 ASIC의 'Updated Grading Policy'·'Announcement about Midterm Week'·'Lab0, Lab1 Score Announcement'와 같은 모양(계약 14장).
    # 점수 공지는 본문에 Canvas 파일 링크만 있다 → core._linked_files가 가상 게시판 '공지'로 모아 받는다.
    policy = ("<p>Dear All,</p><p>I have attached the updated grading policy, which includes the midterm quiz (5%) "
              "and the final quiz (5%).</p><ul><li>Homework assignments: 30% (w/ short reports)</li>"
              "<li>Midterm project: 25% (w/ midterm report)</li><li>Midterm quiz: 5%</li>"
              "<li>Final project: 25% (w/ final report)</li><li>Final quiz: 5%</li><li>Attendance: 10%</li></ul>"
              # 학점 비율(계약 16장): 묶음(A+, A0)·범위(35~45%)·꼬리(C+ ~ F) — 데모 화면이 '강의계획서 비율'을 보여 준다
              "<p>Letter grades (relative, guideline):</p><ul><li>A+, A0: 35~45%</li><li>B+, B0: 40~50%</li>"
              "<li>C+ ~ F: 5~25%</li></ul>")
    midterm_week = ("<p>As I have mentioned in the last lecture, we will have a short quiz session (+ explanation on "
                    f"midterm project) in the regular class hour on {MONTHS[quiz_day.month - 1]}. {quiz_day.day}, "
                    f"{quiz_day.year} .</p><p>The quiz covers the lecture materials so far.</p>")
    # 계약 20장(에이전트에게 묻기) 데모: 'final'만 적힌 시험 일정 — 규칙은 'final' 혼자를 시험으로 보지 않아(기말 프로젝트일 수도
    # 있어서) 놓친다. 대시보드에 'Final quiz 날짜 미공지'로 남고, "기말고사 날짜가 안 보여요"라고 물으면 이 문장을 찾아 제안한다.
    final_day = day(32)
    final_week = ("<p>Dear All,</p><p>The final will be held on "
                  f"{MONTHS[final_day.month - 1]}. {final_day.day} at 10:30 in the regular class hour "
                  "(closed book, 30 minutes).</p><p>Please review the lecture slides from week 9.</p>")
    scores_post = (f'<p>Lab0 and Lab1 scores are uploaded. Please find your row by your student ID.</p>'
                   f'<p><a href="{base_url}/courses/101/files/{SCORE_FILES[0]}/download">Lab0_score.pdf</a> '
                   f'<a href="/courses/101/files/{SCORE_FILES[1]}/download?wrap=1">Lab1_score.pdf</a></p>')
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
        # 아래 넷은 at(-7)~at(0) 밖에 올려 공지 필터 시험(test_canvas)의 기대값을 그대로 둔다
        announcement(101, 9006, at(-36, 14), "Updated Grading Policy", policy),
        announcement(101, 9007, at(-20, 10), "Lab0, Lab1 Score Announcement", scores_post),
        announcement(101, 9008, at(-9, 11), "Announcement about Midterm Week", midterm_week),
        announcement(101, 9009, at(-12, 15), "Final week schedule", final_week),
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
    # LearningX 게시판(실제 고려대 ASIC과 같은 모양): 과제 안내는 여기, 마감은 본문, 자세한 건 첨부 PDF
    lab6 = day(13)
    # 'Next labs: A -> B'(실제 Lab3.pdf는 '→') → 랩 개수 단서: 0번부터 6번 + 2개 = 9개(계약 14-4)
    lab6_pdf = tiny_pdf([f"Lab 6 Assignment", f"Report due: 23:59 on {lab6.month}/{lab6.day}",
                         "Submit as a PDF via email.", "Next labs: P&R (IC Compiler II) -> SRAM"])
    slides = tiny_pdf(["Week 11 lecture slides"])

    def attach(fid, filename, data):
        return {"id": fid + 10000, "filename": filename, "filesize": len(data), "canvas_file_id": fid,
                "url": f"{base_url}/files/{fid}/download?download_frd=1&verifier=v{fid}"}

    learningx = {"101": {
        "boards": [{"id": 9101, "title": "과제"}, {"id": 9102, "title": "강의자료실"}],
        "posts": {
            9101: [{"id": 46001, "title": "Lab6 Assignment", "created_at": at(-2, 21), "updated_at": at(-2, 21),
                    "content": ("<p>Lab6 Assignment</p><p>Submit as a PDF(report) via email : asic.ta@example.ac.kr</p>"
                                f"<p>(due : 23:59 on {lab6.month}/{lab6.day})</p>"),
                    "attachments": [attach(8001, "Lab6.pdf", lab6_pdf)]}],
            9102: [{"id": 46002, "title": "Week 11 강의자료", "created_at": at(-3, 9), "updated_at": at(-3, 9),
                    "content": "<p>11주차 강의자료입니다.</p>", "attachments": [attach(8002, "Week11.pdf", slides)]}],
        }}}
    files = {8001: lab6_pdf, 8002: slides,
             SCORE_FILES[0]: score_table_pdf("Lab0", 0), SCORE_FILES[1]: score_table_pdf("Lab1", 1),
             PRELAB_FILE: prelab_pdf()}
    file_names = {SCORE_FILES[0]: "Lab0_score.pdf", SCORE_FILES[1]: "Lab1_score.pdf", PRELAB_FILE: "Lab5_prelab.pdf"}
    return {
        "user": {"id": USER_ID, "name": "데모 학생", "short_name": "데모 학생",
                 "sortable_name": "학생, 데모", "login_id": "demo"},
        "courses": courses,
        "assignments": assignments,
        "announcements": announcements,
        "conversations": conversations,
        "learningx": learningx,
        "files": files,
        "file_names": file_names,
    }


# 점수표 PDF의 학번은 모두 가짜(2026000001…). 데모 화면은 DEMO_STUDENT_ID로 내 줄을 찾는다(cli demo만 넣는다).
DEMO_STUDENT_ID = "2026000002"
SCORE_FILES = (8101, 8102)
PRELAB_FILE = 8201         # 'LAB5 예비보고서' 설명에 링크된 Lab5_prelab.pdf(계약 24-3)
_SCORE_ROWS = [(18, 25), (17, 27), (15, 20), (19, 28), (12, 18), (16, 24), (20, 29), (14, 22)]


def score_table_pdf(label: str, shift: int = 0) -> bytes:
    """'Lab0, Lab1 Score Announcement'의 첨부 같은 점수표: 한 줄에 한 학생(가짜 학번), Prelab(20)·Report(30)·Total(50)."""
    lines = [f"{label} Score", "No Student ID Prelab(20) Report(30) Total(50)"]
    for i, (a, b) in enumerate(_SCORE_ROWS, 1):
        a, b = max(0, a - shift * (i % 3)), min(30, b + shift * (i % 2))
        lines.append(f"{i} {2026000000 + i} {a} {b} {a + b}")
    return tiny_pdf(lines)


def prelab_pdf() -> bytes:
    """과제 설명에 링크된 안내 PDF: 날짜 없는 제출 요건만(형식·제출처·포함할 것·개인 과제)."""
    return tiny_pdf(["Lab 5 Prelab Report Guide",
                     "Submit the prelab report as a single PDF file on Canvas.",
                     "Include your Verilog code and the simulation waveform screenshots in the report.",
                     "This is an individual assignment."])


def tiny_pdf(lines: list[str]) -> bytes:
    """글자 몇 줄짜리 PDF(Helvetica, 압축 없음). 데모·테스트용 첨부."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({esc(t)}) Tj T*" for t in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
            "/Contents 4 0 R >>",
            f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _query_day(values: list | None, default: date) -> date:
    try:
        return date.fromisoformat(values[0][:10]) if values else default
    except ValueError:
        return default


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _oauth_signature(url: str, params: list) -> str:
    """OAuth 1.0 HMAC-SHA1 서명(LTI 1.1, POST, 토큰 비밀 없음). 실행 화면이 서명하고 POST 받을 때 다시 계산해 맞춘다."""
    def enc(s) -> str:
        return urllib.parse.quote(str(s), safe="-._~")
    pairs = sorted((enc(k), enc(v)) for k, v in params if k != "oauth_signature")
    text = "&".join(["POST", enc(url), enc("&".join(f"{k}={v}" for k, v in pairs))])
    digest = hmac.new((enc(LTI_SECRET) + "&").encode(), text.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _post_summary(post: dict, board_id: int) -> dict:
    """글 목록에 나오는 모양: 본문은 있고 첨부는 개수만(상세에서 받음)."""
    out = {k: v for k, v in post.items() if k != "attachments"}
    out.update(board_id=board_id, attachment_count=len(post.get("attachments") or []))
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockCanvas/1.0"

    def log_message(self, format, *args):  # 요청 로그를 찍지 않는다
        pass

    def _send_bytes(self, status: int, body: bytes, ctype: str, headers=None) -> None:
        """headers: dict 또는 (이름, 값) 목록(Set-Cookie처럼 같은 이름을 여러 번 보낼 때)."""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers.items() if isinstance(headers, dict) else headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, obj, headers=None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", headers)

    def _html(self, status: int, inner: str, headers=None) -> None:
        page = f'<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"></head><body>{inner}</body></html>'
        self._send_bytes(status, page.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _redirect(self, location: str, headers=None) -> None:
        self._send_bytes(302, b"", "text/html; charset=utf-8", [("Location", location)] + list(headers or []))

    def _not_found(self) -> None:
        self._send(404, {"errors": [{"message": "The specified resource does not exist."}]})

    def _record(self, method: str) -> None:
        """server.requests에 경로(+쿼리)를, GET이 아니면 "POST /경로"로 남긴다.
        server.header_log에는 같은 항목과 받은 Authorization·Cookie 헤더(없으면 None)를 한 번에 남긴다."""
        srv = self.server
        entry = self.path if method == "GET" else f"{method} {self.path}"
        srv.requests.append(entry)
        srv.header_log.append((entry, self.headers.get("Authorization"), self.headers.get("Cookie")))

    def _canvas_auth_ok(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {DEMO_TOKEN}":
            return True
        self._send(401, {"errors": [{"message": "Invalid access token."}]},
                   {"WWW-Authenticate": 'Bearer realm="canvas-lms"'})
        return False

    def do_POST(self) -> None:
        self._record("POST")
        try:
            length = min(max(0, int(self.headers.get("Content-Length") or 0)), 1_000_000)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if (path + "/").startswith("/api/v1/"):
            return self._not_found() if self._canvas_auth_ok() else None
        if path == LTI_ACTION_PATH:
            return self._lti_launch(path, body)
        self._not_found()

    def do_GET(self) -> None:
        srv = self.server
        self._record("GET")
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path.rstrip("/"), urllib.parse.parse_qs(url.query)
        if not (path + "/").startswith("/api/v1/"):
            return self._web_get(path, query)  # LearningX·실행 화면·파일: Canvas 토큰 규칙이 아님
        if not self._canvas_auth_ok():
            return
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
        m = re.fullmatch(r"/api/v1/courses/([^/]+)/external_tools", path)
        if m:
            return self._external_tools(m.group(1), query)
        m = re.fullmatch(r"/api/v1/courses/([^/]+)/external_tools/sessionless_launch", path)
        if m:
            return self._sessionless_launch(m.group(1), query)
        m = re.fullmatch(r"/api/v1/files/([0-9]+)", path)
        if m:
            return self._file_info(int(m.group(1)))
        self._not_found()

    # ---------------------------------------------------------------- LearningX (CONTRACTS 10절)

    def _web_get(self, path: str, query: dict) -> None:
        m = re.fullmatch(r"/courses/([^/]+)/external_tools/sessionless_launch", path)
        if m:
            return self._launch_page(m.group(1), query)
        m = _LX_API.fullmatch(path)
        if m:
            return self._learningx_api(*m.groups(), query)
        if re.fullmatch(r"/learningx/courses/[^/]+/boards", path):  # LTI 실행 뒤 리디렉션되는 화면
            return self._html(200, '<div id="app">LearningX 게시판</div>')
        m = re.fullmatch(r"/files/([0-9]+)/download", path)
        if m:
            return self._file_download(int(m.group(1)), query)
        m = re.fullmatch(r"/storage/([0-9]+)", path)
        if m:
            return self._storage(int(m.group(1)))
        if path == "/login/canvas":
            return self._html(200, '<form action="/login/canvas" method="post"><input name="pseudonym_session[unique_id]">'
                                   '<input type="password" name="pseudonym_session[password]"></form>')
        self._not_found()

    def _tools(self, course_id: str, parents: bool) -> list:
        base, out = self.server.base_url, []
        if course_id in self.server.data.get("learningx", {}):
            nav = {"text": "게시판", "enabled": True, "visibility": None}
            out += [
                {"id": 3, "name": "출석", "url": f"{base}/learningx/lti/attendance",
                 "course_navigation": {"text": "출석", "enabled": True}},
                # 'total' 게시판(과목 메뉴에 없음)이 앞에 와도 과목 게시판을 골라야 한다
                {"id": 6, "name": "전체 게시판", "url": f"{base}/learningx/lti/learningx_board/total", "course_navigation": None},
                {"id": BOARD_TOOL_ID, "name": "게시판", "url": f"{base}{LTI_ACTION_PATH}", "course_navigation": nav},
            ]
        if parents:  # 계정 수준 도구(모든 과목에 붙음): 게시판과 무관
            out.append({"id": 12, "name": "Zoom", "url": "https://tools.example.com/lti/launch",
                        "course_navigation": {"text": "Zoom", "enabled": True}})
        for t in out:
            t.setdefault("privacy_level", "public")
            t.setdefault("workflow_state", "public")
        return out

    def _external_tools(self, course_id: str, query: dict) -> None:
        if not any(str(c["id"]) == course_id for c in self.server.data["courses"]):
            return self._not_found()
        parents = (query.get("include_parents") or ["false"])[0].lower() == "true"
        self._send(200, self._tools(course_id, parents))

    def _sessionless_launch(self, course_id: str, query: dict) -> None:
        srv = self.server
        if srv.lx_fail == "sessionless":
            return self._send(500, {"errors": [{"message": "An error occurred."}]})
        tool_id = (query.get("id") or [""])[0]
        if course_id not in srv.data.get("learningx", {}) or tool_id != str(BOARD_TOOL_ID):
            return self._not_found()
        verifier = secrets.token_hex(16)
        srv.lx_verifiers[verifier] = course_id  # 1회용(진짜 Canvas처럼)
        self._send(200, {"id": BOARD_TOOL_ID, "name": "게시판",
                         "url": f"{srv.base_url}/courses/{course_id}/external_tools/sessionless_launch"
                                f"?verifier={verifier}"})

    def _launch_page(self, course_id: str, query: dict) -> None:
        """Canvas가 서명한 LTI 1.1 자동 제출 양식(토큰 없이 verifier로 연다)."""
        srv = self.server
        verifier = (query.get("verifier") or [""])[0]
        if not verifier or srv.lx_verifiers.pop(verifier, None) != course_id:
            return self._html(401, "<p>실행 주소가 만료됐거나 올바르지 않습니다.</p>")
        if srv.lx_fail == "form":
            return self._html(200, "<p>이 도구를 지금 실행할 수 없습니다.</p>")
        course = next(c for c in srv.data["courses"] if str(c["id"]) == course_id)
        action = (f"{srv.base_url}{LTI_ACTION_PATH}" if srv.lx_fail != "foreign"
                  else f"http://lti.example.net{LTI_ACTION_PATH}")
        nonce = secrets.token_hex(16)
        srv.lx_nonces[nonce] = course_id
        params = [
            ("oauth_consumer_key", "learningx"), ("oauth_signature_method", "HMAC-SHA1"),
            ("oauth_timestamp", str(int(datetime.now(timezone.utc).timestamp()))),
            ("oauth_nonce", nonce), ("oauth_version", "1.0"),
            ("context_id", hashlib.sha1(f"course-{course_id}".encode()).hexdigest()),
            ("context_label", course["course_code"]), ("context_title", course["name"]),
            ("custom_canvas_course_id", course_id), ("custom_canvas_user_id", str(USER_ID)),
            ("ext_roles", "urn:lti:instrole:ims/lis/Student,urn:lti:role:ims/lis/Learner"),
            ("launch_presentation_document_target", "iframe"), ("launch_presentation_locale", "ko"),
            ("launch_presentation_return_url",
             f"{srv.base_url}/courses/{course_id}/external_content/success/external_tool_redirect"),
            ("lis_person_contact_email_primary", ""),  # 빈 값도 서명에 들어간다
            ("lis_person_name_full", "데모 학생"),
            ("lti_message_type", "basic-lti-launch-request"), ("lti_version", "LTI-1p0"),
            ("resource_link_id", hashlib.sha1(f"tool-{BOARD_TOOL_ID}-{course_id}".encode()).hexdigest()),
            ("resource_link_title", "게시판"), ("roles", "Learner"),
            ("tool_consumer_instance_name", "데모 대학교"),
            ("user_id", hashlib.sha1(f"user-{USER_ID}".encode()).hexdigest()),
        ]
        params.append(("oauth_signature", _oauth_signature(action, params)))
        esc = html.escape
        fields = "".join(f'<input type="hidden" name="{esc(k)}" id="{esc(k)}" value="{esc(v)}" />'
                         for k, v in params)
        self._html(200,
                   '<form action="/logout" method="post" id="logout_form">'
                   '<input type="hidden" name="authenticity_token" value="mock-csrf" /></form>'
                   f'<form action="{esc(action)}" method="POST" target="tool_content" id="tool_form" '
                   f'data-tool-id="{BOARD_TOOL_ID}">{fields}<button type="submit">게시판 열기</button></form>'
                   '<script>document.getElementById("tool_form").submit();</script>')

    def _lti_launch(self, path: str, body: bytes) -> None:
        """LTI 양식 POST: 서명·1회용 nonce·context_id 확인 → 쿠키 3개(xn_api_token은 과목에 묶임) → 게시판 화면으로."""
        srv = self.server
        params = urllib.parse.parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
        form = dict(params)
        course_id = form.get("custom_canvas_course_id", "")
        sig = form.get("oauth_signature", "")
        good = (srv.lx_fail != "launch" and sig and form.get("context_id")
                and hmac.compare_digest(sig.encode(), _oauth_signature(srv.base_url + path, params).encode())
                and srv.lx_nonces.pop(form.get("oauth_nonce", ""), None) == course_id
                and course_id in srv.data.get("learningx", {}))
        if not good:
            return self._html(401, "<p>LTI 실행 요청을 확인하지 못했습니다.</p>")
        cookies = [("Set-Cookie", f"laravel_session={secrets.token_hex(20)}; Max-Age=7200; Path=/; HttpOnly; SameSite=Lax"),
                   ("Set-Cookie", f"XSRF-TOKEN={secrets.token_hex(20)}; Max-Age=7200; Path=/; SameSite=Lax")]
        if srv.lx_fail != "cookie":
            token = f"xn{course_id}.{secrets.token_urlsafe(24)}"
            srv.lx_tokens[token] = course_id
            cookies.append(("Set-Cookie", f"xn_api_token={token}; Path=/; SameSite=Lax"))
        self._redirect(f"{srv.base_url}/learningx/courses/{course_id}/boards", cookies)

    def _learningx_api(self, course_id: str, board: str | None, post: str | None, query: dict) -> None:
        """Bearer xn_api_token만 받는다(없거나 틀리면 401, 다른 과목 출입증이면 403)."""
        srv = self.server
        auth = self.headers.get("Authorization") or ""
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if srv.lx_fail == "api" or token not in srv.lx_tokens:
            return self._send(401, {"message": "Unauthenticated."})
        if srv.lx_tokens[token] != course_id:
            return self._send(403, {"message": "This action is unauthorized."})
        lx = srv.data.get("learningx", {}).get(course_id)
        if lx is None:
            return self._send(404, {"message": "Not Found"})
        if board is None:
            return self._send(200, [dict(b) for b in lx["boards"]])
        bid = int(board) if re.fullmatch(r"[0-9]+", board) else None
        if not any(b["id"] == bid for b in lx["boards"]):
            return self._send(404, {"message": "Not Found"})
        posts = sorted(lx["posts"].get(bid) or [], key=lambda p: p["created_at"], reverse=True)
        if post is None:
            size = max(1, int(srv.lx_page_size))
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
            except ValueError:
                page = 1
            last = max(1, -(-len(posts) // size))
            return self._send(200, {
                "items": [_post_summary(p, bid) for p in posts[(page - 1) * size:page * size]],
                "pagination": {"current_page": page, "last_page": last, "per_page": size, "total": len(posts)}})
        found = next((p for p in posts if str(p["id"]) == post), None)
        if found is None:
            return self._send(404, {"message": "Not Found"})
        item = _post_summary(found, bid)
        item["attachments"] = [dict(a) for a in found.get("attachments") or []]
        item["post_url"] = f"{srv.base_url}/learningx/courses/{course_id}/boards/{bid}/posts/{found['id']}"
        self._send(200, item if srv.lx_unwrapped else {"item": item})

    def _file_download(self, fid: int, query: dict) -> None:
        """Canvas 파일 주소: 확인값(verifier)이 주소 안에 있어 인증 헤더 없이 받는다. 헤더가 있으면 403(실제 사이트처럼)."""
        srv = self.server
        if self.headers.get("Authorization"):
            return self._send(403, {"errors": [{"message": "user not authorized to perform that action"}]})
        if fid not in srv.data.get("files", {}):
            return self._not_found()
        if (query.get("verifier") or [""])[0] != f"v{fid}":
            return self._redirect(f"{srv.base_url}/login/canvas")  # 진짜 Canvas처럼 로그인 화면으로
        self._redirect(f"{srv.storage_base}/storage/{fid}")

    def _storage(self, fid: int) -> None:
        """파일 저장소(실제로는 다른 서버). 인증 헤더가 같이 오면 거절한다."""
        if self.headers.get("Authorization"):
            return self._send_bytes(400, b"<Error><Code>InvalidArgument</Code>"
                                         b"<Message>Only one auth mechanism allowed</Message></Error>",
                                    "application/xml")
        body = self.server.data.get("files", {}).get(fid)
        if body is None:
            return self._not_found()
        self._send_bytes(200, body, "application/pdf", {"Content-Disposition": f'attachment; filename="{fid}.pdf"'})

    def _file_info(self, fid: int) -> None:
        """Canvas `GET /api/v1/files/:id` (Canvas 토큰) → url은 verifier가 든 다운로드 주소."""
        srv = self.server
        body = srv.data.get("files", {}).get(fid)
        if body is None:
            return self._not_found()
        name = (srv.data.get("file_names") or {}).get(fid) or next(
            (a.get("filename") for lx in srv.data.get("learningx", {}).values()
             for posts in lx["posts"].values() for p in posts for a in p.get("attachments") or []
             if a.get("canvas_file_id") == fid), None) or f"file_{fid}.pdf"
        self._send(200, {"id": fid, "display_name": name, "filename": name, "content-type": "application/pdf",
                         "size": len(body), "locked": False, "hidden": False,
                         "url": f"{srv.base_url}/files/{fid}/download?download_frd=1&verifier=v{fid}"})

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

    server.requests에 받은 요청의 경로+쿼리가 쌓인다(GET은 경로만, 그 밖은 "POST /경로").
    server.header_log: 같은 항목과 받은 (Authorization, Cookie) 헤더 — (entry, auth, cookie) 목록.
    끌 때는 server.shutdown(); server.server_close().

    LearningX 시험용 손잡이(테스트에서 바꿔도 된다):
      lx_fail: None | "sessionless"(실행 API 500) | "form"(실행 화면에 양식 없음) | "foreign"(양식이 다른 서버로)
               | "launch"(LTI POST 401) | "cookie"(POST는 되지만 xn_api_token 없음) | "api"(LearningX API 늘 401)
      lx_page_size: 글 목록 페이지 크기(기본 20). lx_unwrapped: True면 글 상세를 {"item": …}로 감싸지 않는다.
      storage_base: 첨부가 리디렉션되는 저장소 주소(기본 = 이 서버). 다른 서버를 주면 다른 호스트 리디렉션.
      lx_tokens: 발급한 xn_api_token → 과목 id.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    server.today = today or date.today()
    server.tz = tz or _local_tz()
    server.data = build_demo_data(server.today, server.tz, server.base_url)
    server.requests = []
    server.header_log = []
    server.lx_fail = None
    server.lx_page_size = LX_PAGE_SIZE
    server.lx_unwrapped = False
    server.storage_base = server.base_url
    server.lx_tokens, server.lx_verifiers, server.lx_nonces = {}, {}, {}
    threading.Thread(target=server.serve_forever, args=(0.1,), name="mock-canvas", daemon=True).start()
    return server, server.base_url
