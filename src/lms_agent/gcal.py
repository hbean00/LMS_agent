"""Google 캘린더 API 클라이언트. 표준 라이브러리(urllib)만 쓴다.

승인된 마감을 앱이 만든 'LMS 마감' 캘린더에 올린다(scope는 calendar.app.created라 다른 캘린더는 못 건드린다).
토큰·인증 코드·클라이언트 비밀값은 요청 헤더·본문에만 쓰고 오류 메시지·예외·URL에는 절대 넣지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .canvas import origin

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/calendar/v3"
CALENDAR_NAME = "LMS 마감"

USER_AGENT = "lms-agent/0.1"
HASH_VERSION = 1
REFRESH_MARGIN = 60  # 만료 60초 전부터 미리 갱신
DEFAULT_BACKOFF = (0.5, 2.0)  # 429/5xx 재시도 대기(초). 길이 = 재시도 횟수
DISCONNECTED = "구글 연결이 끊겼습니다. 설정에서 다시 연결하세요"
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
_SAFE_REASON = re.compile(r"^[A-Za-z_]{1,40}$")


class GoogleError(Exception):
    """구글 호출 실패. 메시지는 한국어이고 토큰·코드·비밀값을 담지 않는다. status는 HTTP 코드(없으면 None)."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트는 따라가지 않는다(토큰이 다른 곳으로 새지 않게). 3xx는 HTTPError가 된다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    from . import net   # 운영체제·사용자 루트 인증서까지 믿는 공통 HTTPS 설정(계약 17장)
    return urllib.request.build_opener(net.https_handler(), _NoRedirect)


def _parse(raw: bytes):
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _send(method: str, url: str, *, body: bytes | None = None, content_type: str | None = None,
          bearer: str | None = None, timeout: float = 20):
    """요청 한 번 → (status, JSON, headers). HTTP 오류도 돌려준다. 네트워크 실패만 GoogleError."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    if bearer:
        req.add_unredirected_header("Authorization", "Bearer " + bearer)
    timeout_msg = f"구글 응답이 {timeout:g}초 안에 오지 않았습니다 (시간 초과)"
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read()), resp.headers
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        finally:
            e.close()
        return e.code, _parse(raw), e.headers
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise GoogleError(timeout_msg) from None
        from . import net
        if net.is_cert_error(e.reason):
            raise GoogleError(net.cert_help(urllib.parse.urlsplit(req.full_url).hostname or "", e.reason, "구글")) from None
        raise GoogleError("구글에 연결하지 못했습니다. 인터넷 연결을 확인하세요") from None
    except (socket.timeout, TimeoutError):
        raise GoogleError(timeout_msg) from None
    except (OSError, http.client.HTTPException):
        raise GoogleError("구글과 통신하다 연결이 끊겼습니다. 잠시 뒤 다시 시도하세요") from None


def _reason(data) -> str | None:
    err = data.get("error") if isinstance(data, dict) else None
    reason = None
    if isinstance(err, dict):
        errs = err.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            reason = errs[0].get("reason")
    elif isinstance(err, str):
        reason = err  # OAuth 오류 코드 (invalid_grant 등)
    return reason if isinstance(reason, str) and _SAFE_REASON.match(reason) else None


def _retryable(status: int, data) -> bool:
    return status == 429 or status >= 500 or (status == 403 and _reason(data) in _RATE_REASONS)


def _send_retry(method, url, backoff, **kw):
    for i in range(len(backoff) + 1):
        status, data, headers = _send(method, url, **kw)
        if not _retryable(status, data) or i == len(backoff):
            return status, data, headers
        time.sleep(backoff[i])
    raise AssertionError("unreachable")


def _api_error(status: int, data) -> GoogleError:
    reason = _reason(data)
    if status == 401:
        return GoogleError(DISCONNECTED, status, reason)
    if status == 429 or (status == 403 and reason in _RATE_REASONS):
        msg = f"구글 요청이 너무 많습니다. 잠시 뒤 다시 시도하세요 ({status})"
    elif status == 403:
        msg = "구글이 권한을 거부했습니다 (403)"
    elif status == 404:
        msg = "구글 캘린더에서 찾을 수 없습니다 (404)"
    elif status == 410:
        msg = "이미 지워진 일정입니다 (410)"
    elif status == 400:
        msg = "구글이 요청을 받아들이지 않았습니다 (400" + (f", {reason})" if reason else ")")
    elif status >= 500:
        msg = f"구글 서버 오류 ({status}). 잠시 뒤 다시 시도하세요"
    elif 300 <= status < 400:
        msg = f"구글 응답이 예상과 다릅니다 ({status})"
    else:
        msg = f"구글 요청 실패 ({status})"
    return GoogleError(msg, status, reason)


def _token_error(status: int, data, refreshing: bool) -> GoogleError:
    reason = _reason(data)
    if reason == "invalid_grant":
        if refreshing:
            return GoogleError(DISCONNECTED, 401, reason)
        return GoogleError("인증 코드가 만료됐거나 이미 쓰였습니다. 다시 연결하세요", status, reason)
    if reason in ("invalid_client", "unauthorized_client"):
        return GoogleError("구글 앱 설정(클라이언트 ID·비밀값)이 올바르지 않습니다", status, reason)
    if status >= 500 or status == 429:
        return _api_error(status, data)
    return GoogleError(f"구글 로그인 처리에 실패했습니다 ({reason or status})", status, reason)


def _form(fields: dict) -> bytes:
    return urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode("ascii")


def _token_dict(data, old: dict | None = None) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise GoogleError("구글 로그인 응답 형식이 예상과 다릅니다")
    try:
        expires_in = float(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    old = old or {}
    return {"access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
            "expires_at": time.time() + expires_in,
            "scope": data.get("scope") or old.get("scope") or ""}


# ---- OAuth (설치형 앱 loopback + PKCE, RFC 8252 / RFC 7636) ----

class OAuthFlow:
    def __init__(self, client_id: str, redirect_uri: str, client_secret: str | None = None,
                 auth_url: str = AUTH_URL, token_url: str = TOKEN_URL, timeout: float = 20):
        if not client_id:
            raise GoogleError("구글 클라이언트 ID가 비어 있습니다")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._client_secret = client_secret
        self.auth_url = auth_url
        self.token_url = token_url
        self.timeout = timeout
        self.state: str | None = None
        self.code_verifier: str | None = None

    def __repr__(self) -> str:
        return f"OAuthFlow({self.redirect_uri!r})"

    def authorization_url(self) -> str:
        """브라우저로 열 주소. 부를 때마다 state와 code_verifier를 새로 만든다."""
        self.state = secrets.token_urlsafe(32)
        self.code_verifier = secrets.token_urlsafe(64)  # 86자, RFC 7636의 43~128자 안
        digest = hashlib.sha256(self.code_verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
                  "scope": SCOPE, "access_type": "offline", "prompt": "consent",
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": self.state}
        return self.auth_url + "?" + urllib.parse.urlencode(params)

    def exchange(self, code: str, state: str) -> dict:
        """콜백으로 받은 code를 토큰으로 바꾼다. state가 다르면 GoogleError."""
        if not self.state or not self.code_verifier:
            raise GoogleError("연결 요청이 만료됐습니다. 다시 연결하세요")
        if not isinstance(state, str) or not secrets.compare_digest(
                state.encode("utf-8"), self.state.encode("utf-8")):
            raise GoogleError("연결 요청이 일치하지 않습니다(state 불일치). 다시 연결하세요", 400)
        if not code or not isinstance(code, str):
            raise GoogleError("구글이 인증 코드를 주지 않았습니다. 다시 연결하세요", 400)
        verifier = self.code_verifier
        self.state = self.code_verifier = None  # 한 번만 쓴다
        status, data, _ = _send("POST", self.token_url, body=_form({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self._client_secret, "code_verifier": verifier,
        }), content_type="application/x-www-form-urlencoded", timeout=self.timeout)
        if not 200 <= status < 300:
            raise _token_error(status, data, refreshing=False)
        tokens = _token_dict(data)
        if not tokens.get("refresh_token"):
            raise GoogleError("구글이 갱신 토큰을 주지 않았습니다. 다시 연결하세요")
        if tokens["scope"] and SCOPE not in tokens["scope"].split():
            raise GoogleError("캘린더 권한이 허용되지 않았습니다. 다시 연결해 권한을 체크하세요")
        return tokens


def revoke(token: str, revoke_url: str = REVOKE_URL) -> None:
    """연결 해제. 실패해도 조용히 넘어간다(best effort). 토큰은 URL이 아니라 본문에 넣는다."""
    if not token:
        return
    try:
        _send("POST", revoke_url, body=_form({"token": token}),
              content_type="application/x-www-form-urlencoded", timeout=10)
    except Exception:
        pass


# ---- 일정 본문 ----

def deadline_window(due: datetime, minutes: int = 0) -> tuple[datetime, datetime]:
    """마감 일정의 (start, end). 기본은 start = end = due (길이 0, .ics와 같다).

    minutes를 주면 due로 끝나는 칸(due − minutes, due)이 된다. 구글 알림은 start 기준이다.
    """
    return due - timedelta(minutes=max(0, minutes)), due


def _aware(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.astimezone()  # naive는 PC 로컬 시간으로 본다


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_body(item: dict) -> tuple[str, dict, str]:
    """desired 항목 → (lms_id, 구글 이벤트 본문, 해시). 잘못된 항목이면 ValueError(한국어)."""
    lms_id = str(item.get("lms_id") or "").strip()
    if not lms_id:
        raise ValueError("항목 ID(lms_id)가 없습니다")
    summary = str(item.get("summary") or "").strip() or "마감"
    description = str(item.get("description") or "")
    start, end = _aware(item.get("start")), _aware(item.get("end"))
    if start is None or end is None:
        raise ValueError("시작·끝 시각이 없거나 올바르지 않습니다")
    if end < start:
        raise ValueError("끝 시각이 시작보다 빠릅니다")
    url = item.get("url") or None
    if url and urllib.parse.urlsplit(str(url)).scheme.lower() not in ("http", "https"):
        url = None  # 구글은 source.url에 http(s)만 받는다
    reminders = sorted({m for m in item.get("reminders") or []
                        if isinstance(m, int) and not isinstance(m, bool) and 0 <= m <= 40320})[:5]
    payload = {"v": HASH_VERSION, "summary": summary, "description": description,
               "start": _utc(start), "end": _utc(end), "url": url, "reminders": reminders}
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    body = {
        "summary": summary, "description": description,
        "start": {"dateTime": start.isoformat(timespec="seconds")},
        "end": {"dateTime": end.isoformat(timespec="seconds")},
        "source": {"title": "LMS Agent", "url": url} if url else None,
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in reminders]},
        "extendedProperties": {"private": {"lmsAgent": "1", "lmsId": lms_id, "lmsHash": digest}},
        "transparency": "transparent",
    }
    return lms_id, body, digest


def _private(ev: dict) -> dict:
    p = (ev.get("extendedProperties") or {}).get("private") if isinstance(ev, dict) else None
    return p if isinstance(p, dict) else {}


def _stamp(value) -> float:
    """RFC3339 updated → epoch. 해석 못 하면 0."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", str(value or ""))
    if not m:
        return 0.0
    tz = m.group(3) or "Z"
    dt = datetime.fromisoformat(m.group(1) + ("+00:00" if tz == "Z" else tz))
    return dt.timestamp() + (float(m.group(2)) if m.group(2) else 0.0)


def _proof(ev: dict) -> dict:
    return {"event_id": ev.get("id"), "updated": ev.get("updated"), "html_link": ev.get("htmlLink")}


def _seg(value) -> str:
    return urllib.parse.quote(str(value), safe="")


# ---- 캘린더 ----

class GoogleCalendar:
    def __init__(self, tokens: dict, client_id: str, client_secret: str | None = None,
                 on_tokens=None, api_base: str = API_BASE, token_url: str = TOKEN_URL, timeout: float = 20,
                 backoff: tuple = DEFAULT_BACKOFF):
        if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
            raise GoogleError(DISCONNECTED, 401)
        self._tokens = dict(tokens)
        self.client_id = client_id
        self._client_secret = client_secret
        self.on_tokens = on_tokens
        self.api_base = (api_base or "").rstrip("/")
        self._origin = origin(self.api_base)
        self.token_url = token_url
        self.timeout = timeout
        self.backoff = tuple(backoff)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"GoogleCalendar({self.api_base!r})"

    # ---- 토큰 ----

    def _expiring(self) -> bool:
        try:
            exp = float(self._tokens.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0.0
        return not self._tokens.get("access_token") or exp - REFRESH_MARGIN <= time.time()

    def _refresh(self, stale: str | None = None) -> None:
        with self._lock:
            if stale is not None and self._tokens.get("access_token") != stale and not self._expiring():
                return  # 다른 스레드가 이미 새로 받았다
            status, data, _ = _send_retry("POST", self.token_url, self.backoff, body=_form({
                "grant_type": "refresh_token", "refresh_token": self._tokens.get("refresh_token"),
                "client_id": self.client_id, "client_secret": self._client_secret,
            }), content_type="application/x-www-form-urlencoded", timeout=self.timeout)
            if not 200 <= status < 300:
                raise _token_error(status, data, refreshing=True)
            self._tokens = _token_dict(data, self._tokens)
            new = dict(self._tokens)
        if self.on_tokens:
            self.on_tokens(new)

    # ---- HTTP ----

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.api_base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        same = origin(url) == self._origin
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if same and self._expiring():
            self._refresh()
        retried = False
        while True:
            token = self._tokens.get("access_token") if same else None  # Bearer는 api_base 서버에만
            status, data, _ = _send_retry(method, url, self.backoff, body=payload,
                                          content_type="application/json; charset=utf-8" if payload else None,
                                          bearer=token, timeout=self.timeout)
            if status == 401 and same and not retried:
                retried = True
                self._refresh(stale=token)
                continue
            if 200 <= status < 300:
                return data
            raise _api_error(status, data)

    # ---- API ----

    def ensure_calendar(self, calendar_id: str | None, time_zone: str = "Asia/Seoul") -> str:
        """저장해 둔 캘린더가 있으면 그 id, 지워졌거나 없으면 'LMS 마감'을 새로 만들어 id를 돌려준다."""
        if calendar_id:
            try:
                cal = self._call("GET", f"/calendars/{_seg(calendar_id)}")
                return (cal or {}).get("id") or calendar_id
            except GoogleError as e:
                if not (e.status == 404 or (e.status == 403 and e.reason not in _RATE_REASONS)):
                    raise
        cal = self._call("POST", "/calendars", body={"summary": CALENDAR_NAME, "timeZone": time_zone})
        if not isinstance(cal, dict) or not cal.get("id"):
            raise GoogleError("구글이 캘린더를 만들었다는 응답을 주지 않았습니다")
        return cal["id"]

    def _list_managed(self, base: str) -> list[dict]:
        out, token, seen = [], None, set()
        while True:
            params = {"privateExtendedProperty": "lmsAgent=1", "showDeleted": "false",
                      "singleEvents": "true", "maxResults": 250}
            if token:
                params["pageToken"] = token
            data = self._call("GET", base, params)
            if not isinstance(data, dict):
                raise GoogleError("구글 일정 목록 형식이 예상과 다릅니다")
            out += [e for e in data.get("items") or [] if isinstance(e, dict)
                    and e.get("status") != "cancelled" and _private(e).get("lmsAgent") == "1"]
            token = data.get("nextPageToken")
            if not token or token in seen:
                return out
            seen.add(token)

    def sync(self, calendar_id: str, desired: list[dict]) -> dict:
        """캘린더를 desired와 같게 맞춘다(바뀐 것만 쓴다). 항목별 실패는 errors에 모으고 계속한다.

        인증이 끊기면(401) 중간에 멈추고 GoogleError를 낸다. 다음 sync가 이어서 맞춘다.
        """
        res = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "errors": [], "events": {}}
        base = f"/calendars/{_seg(calendar_id)}/events"
        groups: dict[str, list[dict]] = {}
        for ev in self._list_managed(base):
            groups.setdefault(str(_private(ev).get("lmsId") or ""), []).append(ev)
        for evs in groups.values():
            evs.sort(key=lambda e: (_stamp(e.get("updated")), str(e.get("id"))), reverse=True)

        keep: set[str] = set()
        for item in desired:
            lms_id = str((item or {}).get("lms_id") or "").strip() if isinstance(item, dict) else ""
            label = str(item.get("summary") or lms_id or "?") if isinstance(item, dict) else "?"
            try:
                lms_id, body, digest = _event_body(item if isinstance(item, dict) else {})
            except ValueError as e:
                if lms_id:
                    keep.add(lms_id)  # 잘못 들어온 항목의 기존 일정은 지우지 않는다
                res["errors"].append(f"'{label}' 일정을 올리지 못했습니다: {e}")
                continue
            if lms_id in keep:
                res["errors"].append(f"'{label}' 항목이 두 번 들어와 한 번만 올렸습니다")
                continue
            keep.add(lms_id)
            existing = groups.get(lms_id) or []
            try:
                if not existing:
                    ev = self._call("POST", base, body={k: v for k, v in body.items() if v is not None})
                    res["created"] += 1
                elif _private(existing[0]).get("lmsHash") != digest:
                    ev = self._patch_or_insert(base, existing[0], body)
                    res["updated"] += 1
                else:
                    ev = existing[0]
                    res["unchanged"] += 1
            except GoogleError as e:
                if e.status == 401:
                    raise
                res["errors"].append(f"'{label}' 일정을 올리지 못했습니다: {e}")
                continue
            res["events"][lms_id] = _proof(ev if isinstance(ev, dict) else {})

        for lms_id, evs in groups.items():
            for ev in (evs[1:] if lms_id in keep else evs):  # 남은 중복과 더는 없는 항목
                try:
                    self._call("DELETE", f"{base}/{_seg(ev.get('id'))}")
                except GoogleError as e:
                    if e.status == 401:
                        raise
                    if e.status not in (404, 410):
                        res["errors"].append(f"'{ev.get('summary') or lms_id}' 일정을 지우지 못했습니다: {e}")
                        continue
                res["deleted"] += 1
        return res

    def _patch_or_insert(self, base: str, old: dict, body: dict) -> dict:
        try:
            return self._call("PATCH", f"{base}/{_seg(old.get('id'))}", body=body)
        except GoogleError as e:
            if e.status not in (404, 410):
                raise
        return self._call("POST", base, body={k: v for k, v in body.items() if v is not None})
