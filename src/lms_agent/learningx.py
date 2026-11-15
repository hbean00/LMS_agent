"""LearningX(Xinics) 게시판: 과제 안내·강의자료가 Canvas 글이 아니라 여기에 올라오는 학교가 많다.

비밀번호 없이 Canvas 토큰만으로 접근한다:
1) Canvas 공식 API `sessionless_launch`로 LearningX '게시판' 도구의 실행 주소를 받는다.
2) 그 주소의 LTI 실행 양식(Canvas가 서명)을 LearningX에 제출하면 `xn_api_token` 쿠키가 온다.
3) 그 값을 Bearer로 LearningX API(`/learningx/api/v1/learningx_board/...`)를 부른다.
`xn_api_token`은 메모리에만 두고 저장·출력하지 않는다. 처음 요청은 Canvas와 같은 서버로만 보낸다.
Canvas 토큰은 Canvas API(canvas.Canvas)에만 쓰고 LearningX API에는 보내지 않는다.
첨부는 LearningX 쿠키가 없는 따로 된 연결로, 인증 헤더 없이 받는다(다른 서버의 저장소로 리디렉션됨).
"""
from __future__ import annotations

import contextlib
import http.client
import http.cookiejar
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from .canvas import Canvas, CanvasError, origin

USER_AGENT = "lms-agent/0.3"
BOARD_TOOL_PATH = "/learningx_board/"
TOKEN_COOKIE = "xn_api_token"
_CHUNK = 64 * 1024
# 강의 영상(계약 26-6)을 읽을 도구의 이름·과목 메뉴 글자. 학교·도구 세대마다 다르다
# ('주차학습', '강의콘텐츠', '(구) 강의콘텐츠', '(Old) Lecture Contents', '출결현황', '출결/학습 현황', 'Learn Status').
_VIDEO_TOOL = re.compile(r"주차\s*학습|강의\s*콘텐츠|학습\s*콘텐츠|모듈|modules?|lecture\s*contents?|weekly\s*learning", re.I)
_ATTEND_TOOL = re.compile(r"출결|출석|학습\s*현황|attendance|learn\s*status", re.I)
_LEGACY_TOOL = re.compile(r"(?:^|\s)\((?:구|old)\)", re.I)
_VIDEO_TOOL_PATHS = ("/modulebuilder", "/lecture_attendance")   # 주차학습(모듈) · 출결현황(강의/출결)
_NOT_VIDEO_TOOL_PATH = re.compile(r"/admin/|/survey|/question_banks|/offline_exam|/notification_message|/mycms|/myspace|"
                                  r"/redirect|/learningx_config|/learningx_setting|/totalgradebook")


class LearningXError(Exception):
    """한국어 메시지. 토큰·쿠키 값은 담지 않는다. status는 HTTP 코드(없으면 None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _TooLarge(LearningXError):
    """크기 제한 초과. 다른 주소로 다시 받아도 같으므로 대체 경로를 쓰지 않는다."""


def _seg(value) -> str:
    return urllib.parse.quote(str(value), safe="")


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"action": a.get("action"), "inputs": {}}
            self.forms.append(self._cur)
        elif tag == "input" and self._cur is not None and a.get("name"):
            self._cur["inputs"][a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":  # 양식 밖 입력칸이 앞 양식에 섞이지 않게
            self._cur = None


class _WebRedirects(urllib.request.HTTPRedirectHandler):
    """http·https로만 따라간다(ftp 등은 거절 → 3xx 오류)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
    from . import net
    op = urllib.request.build_opener(net.https_handler(), urllib.request.HTTPCookieProcessor(jar), _WebRedirects)
    op.addheaders = [("User-Agent", USER_AGENT)]
    return op


def _items(d) -> list[dict]:
    """목록 응답: 맨 목록이거나 {"items": [...]}."""
    if isinstance(d, dict):
        d = d.get("items") or d.get("data") or []
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


class LearningX:
    """과목 하나의 LearningX 게시판에 붙는다(실행할 때 받은 출입증은 이 객체 안에만)."""

    def __init__(self, canvas: Canvas, course_id, timeout: float = 20):
        self.canvas = canvas
        self.course_id = str(course_id)
        self.timeout = timeout
        self.base = canvas.base_url.rstrip("/")
        self._origin = origin(self.base)
        self._jar = http.cookiejar.CookieJar()
        self._op = _opener(self._jar)
        # 첨부는 출입증 쿠키가 없는 따로 된 연결로 받는다(저장소 서버로 쿠키·헤더가 따라가지 않게)
        self._dl = _opener(http.cookiejar.CookieJar())
        self._token: str | None = None
        self.requests = 0

    def __repr__(self) -> str:
        return f"LearningX(course {self.course_id})"

    @property
    def ready(self) -> bool:
        """출입증이 있는지(값은 내보내지 않는다). 401로 버렸으면 False — 다시 launch해야 한다."""
        return bool(self._token)

    # ------------------------------------------------------------ 찾기·실행

    @staticmethod
    def tools(canvas: Canvas, course_id) -> list[dict] | None:
        """과목에 붙은 외부 도구 목록(Canvas 공식 API). 못 읽으면 None."""
        try:
            return canvas._get_all(f"/api/v1/courses/{_seg(course_id)}/external_tools",
                                   [("include_parents", "true"), ("per_page", 100)])
        except CanvasError:
            return None

    @staticmethod
    def board_tool(canvas: Canvas, course_id, tools: list[dict] | None = None) -> dict | None:
        """과목에 붙은 LearningX 게시판 도구(없으면 None = LearningX를 안 쓰는 학교·과목). tools: 이미 받은 도구 목록."""
        tools = LearningX.tools(canvas, course_id) if tools is None else tools
        boards = [t for t in tools or [] if BOARD_TOOL_PATH in str(t.get("url") or "")]
        boards.sort(key=lambda t: (not t.get("course_navigation"), "total" in str(t.get("url"))))
        return boards[0] if boards else None

    @staticmethod
    def video_tool(canvas: Canvas, course_id, tools: list[dict] | None = None) -> dict | None:
        """강의 영상(주차학습)을 읽을 LearningX 도구(계약 26-6): 이름·과목 메뉴 글자가 '주차학습·강의콘텐츠·모듈'인 것,
        없으면 '출결·학습현황'. 옛 도구('(구) 강의콘텐츠')보다 지금 도구를, 과목 메뉴에 보이는 것을 먼저. 없으면 None."""
        tools = LearningX.tools(canvas, course_id) if tools is None else tools

        def path(t) -> str:
            return urllib.parse.urlsplit(str(t.get("url") or "")).path.lower()

        # 관리자 도구(학생은 실행 거절 401)·설문 도구('설문(주차학습)')·게시판은 고르지 않는다(고려대 실제 목록, 리드 진단 2026-11-15)
        lx = [t for t in tools or [] if isinstance(t, dict) and t.get("id") is not None
              and "/learningx/" in str(t.get("url") or "") and BOARD_TOOL_PATH not in str(t.get("url") or "")
              and not _NOT_VIDEO_TOOL_PATH.search(path(t))]

        def label(t) -> str:
            nav = t.get("course_navigation") if isinstance(t.get("course_navigation"), dict) else {}
            return f"{nav.get('text') or ''} {t.get('name') or ''}"

        for want in _VIDEO_TOOL_PATHS:   # 표준 LearningX 경로가 이름보다 확실하다(이름은 학교·과목마다 다름)
            found = [t for t in lx if want in path(t)]
            if found:
                return found[0]
        for rx in (_VIDEO_TOOL, _ATTEND_TOOL):
            found = [t for t in lx if rx.search(label(t))]
            found.sort(key=lambda t: (bool(_LEGACY_TOOL.search(label(t))), not t.get("course_navigation")))
            if found:
                return found[0]
        return None

    def launch(self, tool_id) -> None:
        """게시판 도구를 실행해 출입증(xn_api_token)을 받는다. 실패하면 LearningXError."""
        self._token = None
        self._jar.clear()
        try:
            d = self.canvas._get_one(f"/api/v1/courses/{_seg(self.course_id)}/external_tools/sessionless_launch",
                                     [("id", str(tool_id))])
        except CanvasError as e:
            raise LearningXError(f"LearningX를 실행하지 못했습니다: {e}", e.status) from None
        url = str(d.get("url") or "")
        if not url:
            raise LearningXError("Canvas가 LearningX 실행 주소를 주지 않았습니다")
        url = urllib.parse.urljoin(self.base + "/", url)
        if origin(url) != self._origin:
            raise LearningXError("LearningX 실행 주소가 Canvas와 다른 서버입니다")
        forms = _Forms()
        forms.feed(self._open(url, fail="LearningX 실행 화면을 열지 못했습니다").decode("utf-8", "replace"))
        forms.close()
        form = next((f for f in forms.forms if f.get("action") and f["inputs"].get("oauth_signature")), None)
        if not form:
            raise LearningXError("LearningX 실행 양식을 찾지 못했습니다")
        action = urllib.parse.urljoin(url, form["action"])
        if origin(action) != self._origin:
            raise LearningXError("LearningX 실행 양식이 Canvas와 다른 서버로 보내게 돼 있어 보내지 않았습니다")
        self._open(action, data=urllib.parse.urlencode(form["inputs"]).encode(),
                   fail="LearningX가 실행을 거절했습니다")
        token = self._cookie(TOKEN_COOKIE, action)
        if not token:
            raise LearningXError("LearningX가 출입증을 주지 않았습니다(학교 설정이 다를 수 있음)")
        if any(not 33 <= ord(c) <= 126 for c in token):  # 헤더에 못 넣는 값(오류 메시지에 값이 찍히지 않게 미리 거름)
            raise LearningXError("LearningX 출입증 형식이 올바르지 않습니다")
        self._token = token

    def _cookie(self, name: str, url: str) -> str | None:
        """url의 서버가 준 쿠키 값(리디렉션 중 다른 서버가 같은 이름으로 준 것은 쓰지 않는다)."""
        _, host = http.cookiejar.eff_request_host(urllib.request.Request(url))
        for c in self._jar:
            dom = (c.domain or "").lstrip(".").lower()
            if c.name == name and c.value and dom and (host == dom or host.endswith("." + dom)):
                return c.value
        return None

    # ------------------------------------------------------------ HTTP

    def _send(self, url: str, data: bytes | None = None, headers: dict | None = None,
              fail: str = "LearningX 요청 실패", opener=None):
        """요청 하나 → 응답 객체(닫는 건 부른 쪽). 처음 주소는 Canvas와 같은 서버만."""
        if origin(url) != self._origin:
            raise LearningXError("Canvas와 다른 서버로는 요청하지 않습니다")
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        for k, v in (headers or {}).items():
            # 리디렉션되면(같은 서버여도) 따라가지 않는 헤더로: 다른 서버에 출입증이 가지 않게
            if k.lower() == "authorization":
                req.add_unredirected_header(k, v)
            else:
                req.add_header(k, v)
        self.requests += 1
        timeout_msg = f"LearningX 응답이 {self.timeout:g}초 안에 오지 않았습니다 (시간 초과)"
        try:
            return (opener or self._op).open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            e.close()
            raise LearningXError(f"{fail} ({e.code})", e.code) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise LearningXError(timeout_msg) from None
            from . import net
            if net.is_cert_error(e.reason):
                host = urllib.parse.urlsplit(getattr(req, "full_url", "")).hostname or ""
                raise LearningXError(net.cert_help(host, e.reason, "LearningX")) from None
            raise LearningXError(f"LearningX에 연결하지 못했습니다 ({e.reason})") from None
        except (socket.timeout, TimeoutError):
            raise LearningXError(timeout_msg) from None
        except (OSError, http.client.HTTPException):
            raise LearningXError("LearningX와 통신하다 연결이 끊겼습니다") from None
        except ValueError:  # 헤더 값 오류 등: 메시지에 헤더 값이 들어 있을 수 있어 버린다
            raise LearningXError("LearningX 요청을 만들지 못했습니다") from None

    def _open(self, url: str, data: bytes | None = None, headers: dict | None = None,
              fail: str = "LearningX 요청 실패") -> bytes:
        with self._send(url, data, headers, fail) as r:
            try:
                return r.read()
            except (OSError, http.client.HTTPException):
                raise LearningXError("LearningX와 통신하다 연결이 끊겼습니다") from None

    def _api(self, path: str):
        if not self._token:
            raise LearningXError("먼저 LearningX를 실행해야 합니다")
        try:
            raw = self._open(f"{self.base}/learningx/api/v1{path}",
                             headers={"Accept": "application/json", "Authorization": "Bearer " + self._token})
        except LearningXError as e:
            if e.status == 401:
                self._token = None  # 만료·거절된 출입증은 버린다 → 다시 launch
                raise LearningXError("LearningX 출입증이 만료됐거나 거절됐습니다 (401). 다시 실행해야 합니다",
                                     401) from None
            raise
        text = raw.decode("utf-8", "replace").lstrip("﻿ \t\r\n")
        if text.startswith("while(1);"):
            text = text[len("while(1);"):]
        try:
            return json.loads(text)
        except ValueError:
            raise LearningXError("LearningX 응답을 읽지 못했습니다") from None

    # ------------------------------------------------------------ 게시판

    def boards(self) -> list[dict]:
        d = self._api(f"/learningx_board/courses/{_seg(self.course_id)}/boards")
        return [{"id": b.get("id"), "title": b.get("title")} for b in _items(d) if b.get("id") is not None]

    def posts(self, board_id, max_pages: int = 5) -> list[dict]:
        """글 목록(본문 포함, 첨부는 attachment_count만). 최대 max_pages쪽."""
        out, seen, page = [], set(), 1
        while page <= max_pages:
            d = self._api(f"/learningx_board/courses/{_seg(self.course_id)}/boards/{_seg(board_id)}/posts"
                          f"?page={page}")
            items = _items(d)
            for p in items:  # 읽는 사이 새 글이 올라와 쪽이 밀려도 같은 글을 두 번 넣지 않는다
                key = p.get("id")
                if key is None or key not in seen:
                    seen.add(key)
                    out.append(p)
            pagination = d.get("pagination") if isinstance(d, dict) else None
            try:
                last = int((pagination or {}).get("last_page") or 1)
            except (TypeError, ValueError):
                last = 1
            if page >= last or not items:
                break
            page += 1
        return out

    def post(self, board_id, post_id) -> dict:
        """글 상세(attachments, post_url 포함). {"item": …}으로 감싸 오든 아니든."""
        d = self._api(f"/learningx_board/courses/{_seg(self.course_id)}/boards/{_seg(board_id)}"
                      f"/posts/{_seg(post_id)}")
        if not isinstance(d, dict):
            return {}
        item = d.get("item")
        return item if isinstance(item, dict) else d

    # ------------------------------------------------------------ 주차학습(강의 영상, 계약 26-6)

    def modules(self) -> list[dict]:
        """주차학습 모듈 목록(모듈마다 module_items). 실제 계정으로 확인된 길(다른 학교의 LearningX):
        `GET /learningx/api/v1/courses/<과목>/modules?include_detail=true` (Bearer xn_api_token). 없는 학교면 LearningXError(404)."""
        d = self._api(f"/courses/{_seg(self.course_id)}/modules?include_detail=true")
        if isinstance(d, dict):
            d = d.get("items") or d.get("modules") or d.get("data") or []
        return [m for m in d if isinstance(m, dict)] if isinstance(d, list) else []

    def allcomponents(self, user_id, user_login) -> list[dict]:
        """옛 LearningX의 학습 현황 목록 `GET /learningx/api/v1/courses/<과목>/allcomponents_db?user_id=&user_login=&role=1`.
        user_login(학생 로그인 id)은 이 요청 주소에만 쓰고 저장·출력하지 않는다(오류 메시지에는 주소가 들어가지 않음)."""
        q = urllib.parse.urlencode([("user_id", str(user_id)), ("user_login", str(user_login)), ("role", "1")])
        return _items(self._api(f"/courses/{_seg(self.course_id)}/allcomponents_db?{q}"))

    def download(self, attachment: dict, dest, max_bytes: int = 50_000_000) -> int:
        """첨부 하나를 dest에 저장. 저장한 바이트 수.
        첨부 주소는 Canvas 파일 주소(/files/<id>/download?verifier=…)라 인증 헤더 없이 받는다(확인값이 주소 안에 있음).
        파일 저장소로 리디렉션되므로 처음 주소만 같은 서버인지 본다. 안 되면 Canvas 파일 API로 다시 시도."""
        dest = Path(dest)
        name = str(attachment.get("filename") or dest.name).lower()
        want_html = name.endswith((".htm", ".html"))
        url = str(attachment.get("url") or "")
        if url and not urllib.parse.urlsplit(url).scheme:
            url = urllib.parse.urljoin(self.base + "/", url)
        fid = attachment.get("canvas_file_id")
        try:
            if not url:
                raise LearningXError("첨부 주소가 없습니다")
            return self._save(url, dest, max_bytes, want_html)
        except _TooLarge:
            raise
        except LearningXError:
            if not fid:
                raise
        try:
            info = self.canvas._get_one(f"/api/v1/files/{_seg(fid)}")
        except CanvasError as e:
            raise LearningXError(f"첨부를 받지 못했습니다: {e}", e.status) from None
        if not info.get("url"):
            raise LearningXError("Canvas가 첨부 주소를 주지 않았습니다(잠긴 파일일 수 있음)")
        return self._save(urllib.parse.urljoin(self.base + "/", str(info["url"])), dest, max_bytes, want_html)

    def _save(self, url: str, dest: Path, max_bytes: int, want_html: bool) -> int:
        """출입증 없는 연결로 받아 조각 파일(.part)에 쓰고 다 받으면 바꿔 넣는다. 크기 제한은 받는 중에도 본다."""
        too_big = f"파일이 너무 큽니다({max_bytes / 1_000_000:g}MB 제한)"
        resp = self._send(url, fail="첨부를 받지 못했습니다", opener=self._dl)
        tmp = dest.with_name(dest.name + ".part")
        try:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype.startswith("text/html") and not want_html:
                raise LearningXError("첨부 대신 웹 페이지가 왔습니다(로그인 화면일 수 있음)")
            length = (resp.headers.get("Content-Length") or "").strip()
            if length.isdigit() and int(length) > max_bytes:
                raise _TooLarge(too_big)
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(tmp, "wb") as f:
                while True:
                    try:
                        chunk = resp.read(_CHUNK)
                    except (OSError, http.client.HTTPException):
                        raise LearningXError("첨부를 받다가 연결이 끊겼습니다") from None
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise _TooLarge(too_big)
                    f.write(chunk)
            os.replace(tmp, dest)
            return size
        except OSError as e:  # 폴더 만들기·쓰기·바꿔 넣기 실패
            raise LearningXError(f"파일을 저장하지 못했습니다 ({e.strerror or type(e).__name__})") from None
        finally:
            resp.close()
            with contextlib.suppress(OSError):
                tmp.unlink()


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_EXT = re.compile(r"[A-Za-z0-9]{1,10}")


def safe_name(name: str, limit: int = 80) -> str:
    """파일·폴더 이름으로 쓸 수 있게(Windows 금지 문자·예약 이름(CON, NUL…), 끝의 점·공백, 너무 긴 이름).
    길어서 자를 때는 확장자(.pdf 등)를 남긴다."""
    s = re.sub(r"\s+", " ", str(name or ""))
    s = _BAD.sub("_", s).strip().rstrip(". ")
    if s.split(".")[0].strip().upper() in _RESERVED:
        s = "_" + s
    if len(s) > limit:
        stem, dot, ext = s.rpartition(".")
        if dot and stem and _EXT.fullmatch(ext) and len(ext) + 2 <= limit // 2:
            s = stem[:limit - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            s = s[:limit]
        s = s.rstrip(". ")
    return s or "_"


# ---------------------------------------------------------------- 강의 영상(주차학습) 읽기 — 계약 26-6
# 응답 모양은 공개 자료(다른 학교 LearningX를 실제 계정으로 읽은 오픈소스 기록)에서 확인한 필드만 읽고, 없는 칸은 비워 둔다.
#  - /modules?include_detail=true: 모듈 [{module_id, title, module_items: [{module_item_id, title, content_type
#    ("attendance_item"…), completed, attendance_status("attendance"·"absent"·"late"·"none"), published,
#    content_data: {item_id, title, use_attendance, unlock_at, due_at, late_at, lock_at, duration?,
#    item_content_data: {content_type("everlec"·"movie"·"mp4"·"screenlecture"·"pdf"…), duration(초)}}}]}]
#  - allcomponents_db: 평평한 목록 [{component_id, title, type, unlock_at, due_at, late_at, lock_at, completed, use_attendance,
#    attendance_status, commons_content: {content_type, duration, view_url}, view_info: {view_url}}]
_NOT_VIDEO_KINDS = {"assignment", "quiz", "offline_exam", "exam", "survey", "discussion", "discussion_topic", "page",
                    "wiki_page", "file", "attachment", "external_url", "subheader", "sub_header",
                    "context_module_sub_header", "text", "link"}
_DOC_MEDIA = {"pdf", "file", "doc", "docx", "ppt", "pptx", "hwp", "hwpx", "xls", "xlsx", "csv", "zip", "image", "img",
              "html", "text", "txt", "link", "url", "webpage", "document"}
_LIVE_MEDIA = {"zoom", "video_conference", "videoconference", "webex", "live", "meet", "teams", "bigbluebutton"}
_ATTEND_ONLY = {"smart_attendance", "offline_attendance", "attendance", "electronic_attendance", "qr_attendance",
                "offline", "classroom"}
_VIDEO_MEDIA = {"mp4", "everlec", "movie", "screenlecture", "youtube", "vod", "video", "commons", "kollus", "vimeo",
                "m3u8", "mov", "webm", "wmv", "avi", "flv", "mp3", "audio"}
_YES = {"true", "1", "yes", "y", "complete", "completed", "done"}
_NO = {"false", "0", "no", "n", "incomplete", "not_completed", "none", ""}


def _flag(value) -> bool | None:
    """참·거짓으로 읽을 수 있으면 bool, 아니면 None(모름)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _YES | _NO:
        return value.strip().lower() in _YES
    return None


def _when(value) -> str | None:
    """시각 문자열 → 로컬 시간대 ISO(앱 안의 모양). 읽지 못하면 None."""
    from .timeutil import iso, parse_iso
    return iso(parse_iso(value)) if isinstance(value, str) and value.strip() else None


def _seconds(*values) -> int | None:
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", v):
            v = float(v)
        if isinstance(v, (int, float)) and 0 < v < 100_000:
            return int(round(v))
    return None


def _flatten(raw) -> list[dict]:
    """모듈 목록(module_items·items를 펼침) 또는 평평한 목록 → 항목 목록."""
    out: list[dict] = []
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            continue
        kids = x.get("module_items") if isinstance(x.get("module_items"), list) else x.get("items")
        if isinstance(kids, list) and "due_at" not in x and "content_data" not in x:
            out += [k for k in kids if isinstance(k, dict)]
        else:
            out.append(x)
    return out


def _same_site(url, base: str) -> str | None:
    """Canvas와 같은 서버의 http(s) 주소만(다른 서버 뷰어 주소는 버린다)."""
    if not isinstance(url, str) or not url.strip() or not base:
        return None
    full = urllib.parse.urljoin(base.rstrip("/") + "/", url.strip())
    if urllib.parse.urlsplit(full).scheme not in ("http", "https") or origin(full) != origin(base):
        return None
    return full


def video_records(raw, course_id, base_url: str, tool_id=None) -> list[dict]:
    """LearningX 응답(/modules 또는 allcomponents_db) → 영상 기록 목록(계약 26-6 `cache["videos"]` 한 칸 모양):
    {course_id, id, title, unlock_at, due_at, late_at, lock_at, completed, attendance, duration, url}.
    영상 = 출석에 쓰는 항목(use_attendance)이면서 문서·실시간 수업이 아닌 것, 또는 영상 종류(content_type)이면서 '출석 안 씀'이 아닌 것.
    과제·퀴즈(Canvas 과제로 이미 모음)·숨긴 항목·id 없는 항목은 뺀다. id는 LearningX 항목 id(content_data.item_id =
    allcomponents의 component_id)를 먼저 써서 실행 사이·두 길 사이에 같게 한다. 마감이 없어도 기록은 남긴다(항목은 core가 거른다)."""
    cid = str(course_id)
    base = str(base_url or "").rstrip("/")
    out, seen = [], set()
    for it in _flatten(raw):
        cd = it.get("content_data") if isinstance(it.get("content_data"), dict) else {}
        icd = next((d for d in (cd.get("item_content_data"), it.get("commons_content"), cd.get("commons_content"))
                    if isinstance(d, dict)), {})

        def pick(key, it=it, cd=cd):
            v = cd.get(key)
            return it.get(key) if v is None else v

        kind = str(it.get("type") or it.get("content_type") or cd.get("type") or "").strip().lower()
        # 담긴 것의 종류(고려대 실제 응답, 리드 진단 2026-11-15): content_data.item_content_type — "commons"(Commons에 올린 미디어·문서),
        # "smart_attendance"(대면 수업 출석 체크), "zoom"(실시간 화상 수업) … allcomponents 모양은 type "commons" + commons_content.
        holder = str(cd.get("item_content_type") or ("commons" if kind == "commons" else "")).strip().lower()
        media = str(icd.get("content_type") or "").strip().lower()
        if kind in _NOT_VIDEO_KINDS or holder in _NOT_VIDEO_KINDS or media in _DOC_MEDIA or holder in _DOC_MEDIA:
            continue
        if holder in _ATTEND_ONLY or icd.get("schedule_time") or icd.get("attendance_time_limit"):
            continue   # 수업 시간의 출석 체크(제목이 날짜, 창이 수업 시간) — 영상이 아니다
        if holder in _LIVE_MEDIA or (media in _LIVE_MEDIA and holder != "commons"):
            continue   # 실시간 화상 수업. Commons에 올린 줌 '녹화본'(content_type "zoom")은 영상이다
        if _flag(it.get("published")) is False or _flag(cd.get("published")) is False:
            continue
        attend = _flag(pick("use_attendance"))
        # 영상이라는 단서가 있어야 한다: Commons 미디어·영상 종류·진도(progress_support)·미디어 뷰어(/em/) — 출석만 쓰는 항목은 아니다
        is_media = (holder == "commons" or media in _VIDEO_MEDIA or holder in _VIDEO_MEDIA
                    or icd.get("progress_support") is True or str(icd.get("view_url") or "").startswith("/em/"))
        if not is_media or attend is False:
            continue
        vid = next((v for v in (cd.get("item_id"), it.get("component_id"), it.get("item_id"), it.get("module_item_id"),
                                it.get("id")) if v not in (None, "") and not isinstance(v, (bool, dict, list))), None)
        if vid is None or len(str(vid)) > 64 or str(vid) in seen:
            continue
        seen.add(str(vid))
        status = it.get("attendance_status") if it.get("attendance_status") is not None else cd.get("attendance_status")
        status = str(status).strip().lower()[:30] if isinstance(status, str) and status.strip() else None
        done = _flag(it.get("completed"))
        if done is None:
            done = status == "attendance"
        view = it.get("view_info") if isinstance(it.get("view_info"), dict) else {}
        mid = it.get("module_item_id")
        url = (f"{base}/courses/{_seg(cid)}/modules/items/{_seg(mid)}" if base and mid not in (None, "") else None) \
            or _same_site(view.get("view_url"), base) or _same_site(icd.get("view_url"), base) \
            or (f"{base}/courses/{_seg(cid)}/external_tools/{_seg(tool_id)}" if base and tool_id is not None else None)
        title = " ".join(str(it.get("title") or cd.get("title") or "").split())[:200] or "강의 영상"
        out.append({"course_id": cid, "id": str(vid), "title": title,
                    "unlock_at": _when(pick("unlock_at")), "due_at": _when(pick("due_at")),
                    "late_at": _when(pick("late_at")), "lock_at": _when(pick("lock_at")),
                    "completed": bool(done), "attendance": status,
                    "duration": _seconds(icd.get("duration"), cd.get("duration"), it.get("duration")), "url": url})
    return out


_LX_ITEM_IN_URL = re.compile(r"/learningx/[^?#]*?/(?:items/view|components|attendance_items)/(\d+)")


def canvas_module_videos(modules, course_id, base_url: str) -> list[dict]:
    """대체 길: Canvas 공식 모듈 API(`/api/v1/courses/<과목>/modules?include[]=items&include[]=content_details`)의 항목 중
    LearningX를 띄우는 외부 도구 항목(주소에 /learningx/)이면서 마감(content_details.due_at)이 있는 것 → 영상 기록.
    완료는 완료 조건(completion_requirement.completed). id는 주소 속 LearningX 항목 번호(없으면 'm' + Canvas 모듈 항목 id)."""
    cid = str(course_id)
    out, seen = [], set()
    for m in modules if isinstance(modules, list) else []:
        for it in (m.get("items") if isinstance(m, dict) and isinstance(m.get("items"), list) else []):
            if not isinstance(it, dict) or str(it.get("type") or "") != "ExternalTool":
                continue
            link = str(it.get("external_url") or it.get("url") or "")
            if "/learningx/" not in link or _flag(it.get("published")) is False:
                continue
            det = it.get("content_details") if isinstance(it.get("content_details"), dict) else {}
            m_id = _LX_ITEM_IN_URL.search(link)
            vid = m_id.group(1) if m_id else (f"m{it['id']}" if it.get("id") not in (None, "") else None)
            if vid is None or vid in seen or not _when(det.get("due_at")):
                continue
            seen.add(vid)
            req = it.get("completion_requirement") if isinstance(it.get("completion_requirement"), dict) else {}
            out.append({"course_id": cid, "id": vid,
                        "title": " ".join(str(it.get("title") or "").split())[:200] or "강의 영상",
                        "unlock_at": _when(det.get("unlock_at")), "due_at": _when(det.get("due_at")),
                        "late_at": None, "lock_at": _when(det.get("lock_at")),
                        "completed": bool(_flag(req.get("completed"))), "attendance": None, "duration": None,
                        "url": _same_site(it.get("html_url"), base_url)})
    return out
