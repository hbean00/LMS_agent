"""Canvas LMS REST API 클라이언트. 표준 라이브러리(urllib)만 쓴다.

반환값은 Canvas 원본 dict 그대로다(날짜 문자열도 해석하지 않는다).
토큰은 요청 헤더에만 쓰고 오류 메시지·예외에는 절대 넣지 않는다.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import net

USER_AGENT = "lms-agent/0.1"
JSON_PREFIX = "while(1);"
MAX_PAGES = 200
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HTTP_MESSAGES = {
    401: "토큰이 올바르지 않거나 만료됐습니다 (401)",
    403: "권한 없음 (403)",
    404: "찾을 수 없음 (404)",
}


class CanvasError(Exception):
    """Canvas 호출 실패. 메시지는 한국어이고 토큰을 담지 않는다. status는 HTTP 코드(없으면 None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def parse_json(raw: bytes | str):
    """응답 본문을 JSON으로 읽는다. 앞에 붙은 `while(1);`은 떼고, 빈 본문은 None."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    text = text.lstrip("﻿ \t\r\n")
    if text.startswith(JSON_PREFIX):
        text = text[len(JSON_PREFIX):]
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise CanvasError("Canvas 응답을 읽지 못했습니다 (JSON 형식이 아님)") from None


def next_link(header: str | None) -> str | None:
    """Link 헤더에서 rel="next" URL을 꺼낸다. 없으면 None."""
    for m in re.finditer(r"<([^>]*)>([^<]*)", header or ""):
        rel = re.search(r'rel\s*=\s*"?([^";,]*)"?', m.group(2))
        if rel and "next" in rel.group(1).lower().split():
            return m.group(1).strip()
    return None


def origin(url: str) -> tuple | None:
    """(scheme, host, port). 같은 서버인지 비교할 때 쓴다. 해석할 수 없으면 None."""
    try:
        p = urllib.parse.urlsplit(url)
        scheme = p.scheme.lower()
        return scheme, (p.hostname or "").lower(), p.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None


def _as_list(data) -> list[dict]:
    """목록 응답을 dict 목록으로. Canvas가 객체로 감싸 주거나 객체 하나만 줘도 견딘다."""
    if isinstance(data, dict):
        if data.get("errors"):
            raise CanvasError("Canvas가 오류를 돌려줬습니다")
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else ([data] if "id" in data else [])
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _day(d) -> str:
    """start_date/end_date용 YYYY-MM-DD."""
    if isinstance(d, str):
        return d[:10]
    return (d.date() if isinstance(d, datetime) else d).isoformat()


def _seg(value) -> str:
    return urllib.parse.quote(str(value), safe="")


class _KeepAuthOnSameOrigin(urllib.request.HTTPRedirectHandler):
    """리다이렉트 때 Authorization 헤더는 같은 서버로 갈 때만 다시 붙인다(토큰 유출 방지)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        auth = req.unredirected_hdrs.get("Authorization")
        if new is not None and auth and origin(new.full_url) == origin(req.full_url):
            new.add_unredirected_header("Authorization", auth)
        return new


class Canvas:
    def __init__(self, base_url: str, token: str, timeout: float = 20):
        self.base_url = (base_url or "").strip().rstrip("/")
        self._origin = origin(self.base_url)
        if not self._origin or self._origin[0] not in _DEFAULT_PORTS or not self._origin[1]:
            raise CanvasError("Canvas 주소가 올바르지 않습니다 (예: https://canvas.example.ac.kr)")
        token = (token or "").strip()
        if not token:
            raise CanvasError("토큰이 비어 있습니다")
        if any(not 33 <= ord(c) <= 126 for c in token):
            raise CanvasError("토큰 형식이 올바르지 않습니다 (공백이나 허용되지 않는 문자가 있음)")
        self._token = token
        self.timeout = timeout
        self.request_count = 0  # 실행 기록에 "요청 몇 번으로 끝냈는지" 남기려고 센다
        self._opener = urllib.request.build_opener(net.https_handler(), _KeepAuthOnSameOrigin)

    def __repr__(self) -> str:
        return f"Canvas({self.base_url!r})"

    # ---- HTTP ----

    def _url(self, path: str, params: list | None = None) -> str:
        url = self.base_url + path
        return url + "?" + urllib.parse.urlencode(params) if params else url

    def _get(self, url: str):
        """GET 한 번 → (JSON, 응답 헤더)."""
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        req.add_unredirected_header("Authorization", "Bearer " + self._token)
        timeout_msg = f"Canvas 응답이 {self.timeout:g}초 안에 오지 않았습니다 (시간 초과)"
        self.request_count += 1
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return parse_json(resp.read()), resp.headers
        except urllib.error.HTTPError as e:
            e.close()
            code = e.code
            msg = _HTTP_MESSAGES.get(code) or (
                f"Canvas 서버 오류 ({code})" if code >= 500 else f"Canvas 요청 실패 ({code})")
            raise CanvasError(msg, status=code) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise CanvasError(timeout_msg) from None
            if net.is_cert_error(e.reason):   # 백신·네트워크가 HTTPS를 가로채는 경우: 고칠 방법을 알려 준다
                raise CanvasError(net.cert_help(self._origin[1], e.reason, "Canvas", self._origin[2] or 443)) from None
            raise CanvasError(
                f"Canvas에 연결하지 못했습니다. 주소와 인터넷 연결을 확인하세요 ({e.reason})") from None
        except (socket.timeout, TimeoutError):
            raise CanvasError(timeout_msg) from None
        except (OSError, http.client.HTTPException):
            raise CanvasError("Canvas와 통신하다 연결이 끊겼습니다. 잠시 뒤 다시 시도하세요") from None

    def _get_one(self, path: str, params: list | None = None) -> dict:
        data, _ = self._get(self._url(path, params))
        if not isinstance(data, dict):
            raise CanvasError("Canvas 응답 형식이 예상과 다릅니다")
        return data

    def _pages(self, path: str, params: list):
        """페이지마다 dict 목록을 낸다. next URL이 base_url과 같은 서버일 때만 따라간다."""
        url, seen = self._url(path, params), set()
        while url and url not in seen and len(seen) < MAX_PAGES:
            seen.add(url)
            data, headers = self._get(url)
            yield _as_list(data)
            nxt = next_link(headers.get("Link"))
            url = urllib.parse.urljoin(url, nxt) if nxt else None
            if url and origin(url) != self._origin:
                url = None

    def _get_all(self, path: str, params: list) -> list[dict]:
        return [item for page in self._pages(path, params) for item in page]

    # ---- API ----

    def whoami(self) -> dict:
        return self._get_one("/api/v1/users/self")

    def courses(self) -> list[dict]:
        params = [("enrollment_state", "active"), ("include[]", "syllabus_body"), ("include[]", "term"),
                  ("include[]", "total_scores"), ("per_page", 100)]
        # 접근 제한 과목(access_restricted_by_date) 등 name 없는 항목은 뺀다
        return [c for c in self._get_all("/api/v1/courses", params) if c.get("name")]

    def assignments(self, course_id) -> list[dict]:
        params = [("include[]", "submission"), ("per_page", 100)]
        return self._get_all(f"/api/v1/courses/{_seg(course_id)}/assignments", params)

    def announcements(self, course_ids: list, start: datetime, end: datetime) -> list[dict]:
        codes = [c if c.startswith("course_") else "course_" + c for c in map(str, course_ids)]
        codes = list(dict.fromkeys(codes))
        out: list[dict] = []
        for i in range(0, len(codes), 10):
            params = [("context_codes[]", c) for c in codes[i:i + 10]]
            params += [("start_date", _day(start)), ("end_date", _day(end)), ("per_page", 100)]
            out += self._get_all("/api/v1/announcements", params)
        return out

    def conversation_list(self, since: datetime, limit: int = 30) -> list[dict]:
        """메시지함 목록(본문 없음). last_message_at >= since 인 대화만, 최대 limit개."""
        if limit <= 0:
            return []
        since = since.astimezone(timezone.utc)  # naive면 로컬 시간으로 본다
        picked: list[dict] = []
        for page in self._pages("/api/v1/conversations", [("scope", "inbox"), ("per_page", 50)]):
            times = [_parse_time(c.get("last_message_at")) for c in page]
            picked += [c for c, t in zip(page, times) if t and t >= since]
            # 목록은 최근 메시지 순이다. since 이전 것이 나왔으면 다음 페이지는 더 오래된 것뿐이다.
            if len(picked) >= limit or any(t and t < since for t in times):
                break
        return [c for c in picked[:limit] if c.get("id") is not None]

    def conversation_detail(self, conv: dict) -> dict | None:
        """대화 하나의 본문. 지워졌거나 볼 수 없으면 None."""
        try:
            # auto_mark_as_read=false 필수: 본문을 읽어도 읽음 처리하지 않는다
            detail = self._get_one(f"/api/v1/conversations/{_seg(conv['id'])}", [("auto_mark_as_read", "false")])
        except CanvasError as e:
            if e.status in (403, 404):
                return None
            raise
        return {**conv, **detail}

    def conversations(self, since: datetime, limit: int = 30) -> list[dict]:
        out = []
        for conv in self.conversation_list(since, limit):
            d = self.conversation_detail(conv)
            if d is not None:
                out.append(d)
        return out
