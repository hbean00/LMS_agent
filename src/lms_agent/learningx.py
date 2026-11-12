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
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _WebRedirects)
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

    # ------------------------------------------------------------ 찾기·실행

    @staticmethod
    def board_tool(canvas: Canvas, course_id) -> dict | None:
        """과목에 붙은 LearningX 게시판 도구(없으면 None = LearningX를 안 쓰는 학교·과목)."""
        try:
            tools = canvas._get_all(f"/api/v1/courses/{_seg(course_id)}/external_tools",
                                    [("include_parents", "true"), ("per_page", 100)])
        except CanvasError:
            return None
        boards = [t for t in tools if BOARD_TOOL_PATH in str(t.get("url") or "")]
        boards.sort(key=lambda t: (not t.get("course_navigation"), "total" in str(t.get("url"))))
        return boards[0] if boards else None

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
