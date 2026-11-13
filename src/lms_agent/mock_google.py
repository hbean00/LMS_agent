"""데모·테스트용 가짜 Google OAuth + Calendar 서버.

gcal.py가 쓰는 엔드포인트만 흉내 낸다. 앱은 f"{base}/o/oauth2/v2/auth", f"{base}/token",
f"{base}/revoke", f"{base}/calendar/v3"을 쓴다. calendar.app.created처럼 API로 만든 캘린더만 쓸 수 있다.

'다른 일정 보기'(계약 23장): 사용자 캘린더(내 캘린더·동아리·공휴일·가족·학과 행사)와 데모 날짜(오늘 기준)에 맞춘 일정이 있고,
읽기만 된다. 캘린더 목록(GET /users/me/calendarList)은 calendarlist.readonly, 사용자 캘린더의 일정 목록은
events.readonly 권한이 토큰에 있어야 한다(없으면 403 insufficientPermissions). include_granted_scopes=true면 이미 준 권한을 합친다.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import threading
import time
import urllib.parse
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO_CLIENT_ID = "demo-client.apps.googleusercontent.com"
DEMO_CLIENT_SECRET = "demo-secret"
DEMO_PRIMARY = "demo.student@example.com"

_AUTH = "https://www.googleapis.com/auth/calendar"
_LIST_OK = {_AUTH + ".calendarlist.readonly", _AUTH + ".calendarlist", _AUTH + ".readonly", _AUTH}
_EVENTS_OK = {_AUTH + ".events.readonly", _AUTH + ".events", _AUTH + ".readonly", _AUTH}


def build_demo_calendars(today: date | None = None, tz: tzinfo | None = None, base_url: str = "") -> tuple[dict, dict]:
    """데모 사용자 캘린더와 일정(오늘 기준 상대 날짜, tz 로컬 시각) → (calendars, events{cal_id: {event_id: ev}}).

    - 내 캘린더(owner): 어제 회의, 스터디(설명·참석자도 있음 — 앱은 저장하지 않아야 함), 매주 반복 알바(4번),
      제목 없는 일정, 한가함(transparent) 종일 일정, 취소된 일정
    - 동아리(reader): 회의, 비공개(제목이 안 옴 → '바쁨'), 이틀짜리 종일 MT
    - 대한민국의 휴일(reader): 종일·한가함
    - 가족(freeBusyReader, 색 없음 — 화면의 고정 팔레트로): 바쁨만
    색(계약 23-5)은 서로 또렷이 다르게: 내 캘린더 분홍 #f691b2, 동아리 초록 #16a765, 휴일 노랑 #fbe983, 학과 행사 보라 #cd74e6.
    - 학과 행사(reader): 구글에서 표시 꺼 둠(selected 없음) → 기본 선택에서 빠짐
    """
    today = today or date.today()
    tz = tz or datetime.now().astimezone().tzinfo

    def at(n: int, hh: int, mm: int = 0) -> dict:
        return {"dateTime": datetime.combine(today + timedelta(days=n), dtime(hh, mm), tzinfo=tz).isoformat()}

    def day(n: int) -> dict:
        return {"date": (today + timedelta(days=n)).isoformat()}

    cals = {
        DEMO_PRIMARY: {"summary": DEMO_PRIMARY, "summaryOverride": "내 캘린더", "primary": True,
                       "backgroundColor": "#f691b2", "accessRole": "owner", "selected": True},
        "club.demo@group.calendar.google.com": {"summary": "동아리", "backgroundColor": "#16a765",
                                                "accessRole": "reader", "selected": True},
        "ko.south_korea#holiday@group.v.calendar.google.com": {"summary": "대한민국의 휴일",
                                                               "backgroundColor": "#fbe983",
                                                               "accessRole": "reader", "selected": True},
        "family.demo@group.calendar.google.com": {"summary": "가족", "accessRole": "freeBusyReader",
                                                  "selected": True},
        "dept.demo@group.calendar.google.com": {"summary": "학과 행사", "backgroundColor": "#cd74e6",
                                                "accessRole": "reader"},
    }
    for cid, c in cals.items():
        c.update(kind="calendar#calendarListEntry", id=cid)
    evs = {
        DEMO_PRIMARY: [
            {"id": "demomeet01", "summary": "팀 프로젝트 회의", "start": at(-1, 10), "end": at(-1, 11),
             "location": "공학관 401호"},
            {"id": "demostudy01", "summary": "스터디 모임", "start": at(1, 19), "end": at(1, 21),
             "location": "중앙도서관", "description": "비밀 메모(앱이 저장하면 안 됨)",
             "attendees": [{"email": "friend@example.com"}]},
            {"id": "demojob01", "summary": "알바", "start": at(2, 18), "end": at(2, 22),
             "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"]},
            {"id": "demonotitle01", "start": at(4, 12), "end": at(4, 13)},
            {"id": "demobday01", "summary": "엄마 생신", "start": day(10), "end": day(11), "transparency": "transparent"},
            {"id": "democancel01", "summary": "취소된 약속", "start": at(8, 10), "end": at(8, 11), "status": "cancelled"},
        ],
        "club.demo@group.calendar.google.com": [
            {"id": "democlub01", "summary": "동아리 정기 회의", "start": at(3, 19), "end": at(3, 21),
             "location": "학생회관 301호"},
            {"id": "democlub02", "start": at(5, 18), "end": at(5, 19), "visibility": "private"},
            {"id": "democlub03", "summary": "동아리 MT", "start": day(12), "end": day(14)},
        ],
        "ko.south_korea#holiday@group.v.calendar.google.com": [
            {"id": "demohol01", "summary": "임시 공휴일(데모)", "start": day(5), "end": day(6),
             "transparency": "transparent"},
        ],
        "family.demo@group.calendar.google.com": [
            {"id": "demofam01", "start": at(6, 20), "end": at(6, 21, 30)},
        ],
        "dept.demo@group.calendar.google.com": [
            {"id": "demodept01", "summary": "학과 설명회", "start": at(7, 16), "end": at(7, 18),
             "location": "공학관 대강당"},
        ],
    }
    events = {}
    for cid, items in evs.items():
        events[cid] = {}
        for e in items:
            e.setdefault("status", "confirmed")
            e.update(kind="calendar#event", htmlLink=f"{base_url}/calendar/event?eid={e['id']}",
                     updated="2026-01-01T00:00:00.000Z")
            events[cid][e["id"]] = e
    return cals, events


def urls(base: str) -> dict:
    """gcal 함수에 넘길 주소 묶음."""
    return {"auth_url": f"{base}/o/oauth2/v2/auth", "token_url": f"{base}/token",
            "revoke_url": f"{base}/revoke", "api_base": f"{base}/calendar/v3"}


def _rfc3339(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_dt(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _merge(dst: dict, patch: dict) -> None:
    for k, v in patch.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = json.loads(json.dumps(v))


def _api_err(code: int, message: str, reason: str) -> dict:
    return {"error": {"code": code, "message": message, "errors": [{"reason": reason, "message": message}]}}


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockGoogle/1.0"

    def log_message(self, format, *args):
        pass

    def _send(self, status: int, obj=None, headers: dict | None = None, raw: bytes | None = None,
              ctype: str = "application/json; charset=utf-8") -> None:
        body = raw if raw is not None else (b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode())
        self.send_response(status)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self, method: str) -> None:
        srv = self.server
        srv.requests.append((method, self.path))
        srv.auth_log.append(self.headers.get("Authorization"))
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/")
        query = urllib.parse.parse_qs(url.query)
        raw = self._body() if method in ("POST", "PATCH", "PUT") else b""
        with srv.lock:
            for f in srv.faults:
                if f.get("count", 1) > 0 and f.get("method", method) == method \
                        and re.search(f.get("path", ""), path):
                    f["count"] = f.get("count", 1) - 1
                    return self._send(f["status"], f.get("body") or _api_err(f["status"], "injected", "backendError"))
            if method == "GET" and path == "/o/oauth2/v2/auth":
                return self._auth(query)
            if method == "POST" and path == "/token":
                return self._token(dict(urllib.parse.parse_qsl(raw.decode())))
            if method == "POST" and path == "/revoke":
                return self._revoke(dict(urllib.parse.parse_qsl(raw.decode())) or {k: v[0] for k, v in query.items()})
            if method == "GET" and path == "/calendar/event":
                return self._event_page(query)
            if path.startswith("/calendar/v3/"):
                auth = self.headers.get("Authorization") or ""
                tok = srv.data["access_tokens"].get(auth[7:]) if auth.startswith("Bearer ") else None
                if not tok or tok["expires_at"] <= time.time():
                    return self._send(401, _api_err(401, "Request had invalid authentication credentials.",
                                                    "authError"), {"WWW-Authenticate": 'Bearer realm="mock"'})
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    return self._send(400, _api_err(400, "Parse Error", "parseError"))
                scopes = set(str(tok.get("scope") or "").split())
                return self._calendar(method, path[len("/calendar/v3"):], query, body, scopes)
            self._send(404, _api_err(404, "Not Found", "notFound"))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    # ---- OAuth ----

    def _auth(self, q: dict) -> None:
        srv = self.server
        get = lambda k: (q.get(k) or [None])[0]  # noqa: E731
        redirect = get("redirect_uri") or ""
        r = urllib.parse.urlsplit(redirect)
        if get("client_id") not in srv.data["clients"] or r.scheme != "http" \
                or r.hostname not in ("127.0.0.1", "localhost", "::1"):
            return self._send(400, {"error": "invalid_request"})
        if get("response_type") != "code" or get("code_challenge_method") != "S256" or not get("code_challenge"):
            return self._send(400, {"error": "invalid_request"})
        sep = "&" if r.query else "?"
        if srv.auth_error:
            qs = urllib.parse.urlencode({"error": srv.auth_error, "state": get("state") or ""})
            return self._send(302, headers={"Location": redirect + sep + qs})
        code = "4/mock-" + secrets.token_urlsafe(24)
        scope = srv.grant_scope if srv.grant_scope is not None else (get("scope") or "")
        granted = srv.data["granted"].setdefault(get("client_id"), [])
        if get("include_granted_scopes") == "true":   # 증분 동의: 이미 준 권한을 합친다(구글처럼)
            scope = " ".join(dict.fromkeys(granted + scope.split()))
        srv.data["granted"][get("client_id")] = scope.split()
        srv.auth_queries.append({k: v[0] for k, v in q.items()})
        srv.data["codes"][code] = {"client_id": get("client_id"), "redirect_uri": redirect, "scope": scope,
                                   "challenge": get("code_challenge"), "expires_at": time.time() + 600}
        srv.challenges.append(get("code_challenge"))
        qs = urllib.parse.urlencode({"code": code, "state": get("state") or "", "scope": scope})
        self._send(302, headers={"Location": redirect + sep + qs})

    def _issue(self, client_id: str, refresh: str, scope: str) -> dict:
        srv = self.server
        access = "ya29.mock-" + secrets.token_urlsafe(24)
        srv.data["access_tokens"][access] = {"client_id": client_id, "refresh": refresh, "scope": scope,
                                             "expires_at": time.time() + srv.token_ttl}
        return {"access_token": access, "expires_in": srv.token_ttl, "scope": scope, "token_type": "Bearer"}

    def _token(self, f: dict) -> None:
        srv = self.server
        client = srv.data["clients"].get(f.get("client_id"))
        if client is None or (client.get("secret") is not None and f.get("client_secret") != client["secret"]):
            return self._send(401, {"error": "invalid_client", "error_description": "Unauthorized"})
        bad = lambda: self._send(400, {"error": "invalid_grant", "error_description": "Bad Request"})  # noqa: E731
        if f.get("grant_type") == "authorization_code":
            c = srv.data["codes"].pop(f.get("code") or "", None)
            if not c or c["expires_at"] < time.time() or c["client_id"] != f.get("client_id") \
                    or c["redirect_uri"] != f.get("redirect_uri"):
                return bad()
            verifier = (f.get("code_verifier") or "").encode()
            calc = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
            if not verifier or calc != c["challenge"]:
                return bad()
            refresh = "1//mock-" + secrets.token_urlsafe(24)
            srv.data["refresh_tokens"][refresh] = {"client_id": c["client_id"], "scope": c["scope"]}
            return self._send(200, {**self._issue(c["client_id"], refresh, c["scope"]), "refresh_token": refresh})
        if f.get("grant_type") == "refresh_token":
            rt = srv.data["refresh_tokens"].get(f.get("refresh_token") or "")
            if not rt or rt["client_id"] != f.get("client_id"):
                return bad()
            return self._send(200, self._issue(rt["client_id"], f["refresh_token"], rt["scope"]))
        self._send(400, {"error": "unsupported_grant_type"})

    def _revoke(self, f: dict) -> None:
        data = self.server.data
        token = f.get("token") or ""
        if token in data["refresh_tokens"]:
            data["granted"].pop(data["refresh_tokens"][token]["client_id"], None)   # 연결 해제 = 준 권한도 모두 거둠
            del data["refresh_tokens"][token]
            for a in [a for a, v in data["access_tokens"].items() if v["refresh"] == token]:
                del data["access_tokens"][a]
        elif token in data["access_tokens"]:
            del data["access_tokens"][token]
        else:
            return self._send(400, {"error": "invalid_token"})
        self._send(200, {})

    # ---- Calendar ----

    def _calendar(self, method: str, path: str, q: dict, body: dict, scopes: set | None = None) -> None:
        srv, data = self.server, self.server.data
        segs = [urllib.parse.unquote(s) for s in path.strip("/").split("/")]
        nf = lambda: self._send(404, _api_err(404, "Not Found", "notFound"))  # noqa: E731
        scopes = scopes or set()
        no_scope = lambda: self._send(403, _api_err(  # noqa: E731
            403, "Request had insufficient authentication scopes.", "insufficientPermissions"),
            {"WWW-Authenticate": 'Bearer realm="mock", error="insufficient_scope"'})
        if segs == ["users", "me", "calendarList"]:
            if method != "GET":
                return nf()
            if not scopes & _LIST_OK:
                return no_scope()
            return self._calendar_list(q)
        if len(segs) >= 2 and segs[0] == "calendars" and segs[1] in data["user_calendars"]:
            # 사용자 캘린더: 일정 목록 읽기만(이 앱은 쓰지 않는다). 캘린더 자체 정보·쓰기는 앱 권한 밖
            if segs[2:] != ["events"]:
                return nf()
            if method != "GET":
                return self._send(403, _api_err(403, "Forbidden", "forbidden"))
            if not scopes & _EVENTS_OK:
                return no_scope()
            return self._list_user(segs[1], q)
        if segs == ["calendars"] and method == "POST":
            cid = secrets.token_hex(12) + "@group.calendar.google.com"
            cal = {"kind": "calendar#calendar", "id": cid, "etag": f'"{secrets.token_hex(4)}"',
                   "summary": body.get("summary") or "", "timeZone": body.get("timeZone") or "UTC"}
            data["calendars"][cid] = cal
            data["events"][cid] = {}
            return self._send(200, cal)
        if len(segs) < 2 or segs[0] != "calendars" or segs[1] not in data["calendars"]:
            return nf()
        cid = segs[1]
        events = data["events"][cid]
        if len(segs) == 2:
            if method == "GET":
                return self._send(200, data["calendars"][cid])
            if method == "DELETE":
                del data["calendars"][cid], data["events"][cid]
                return self._send(204)
            return nf()
        if segs[2] != "events":
            return nf()
        if len(segs) == 3:
            if method == "GET":
                return self._list(events, q)
            if method == "POST":
                err = self._invalid(body)
                if err:
                    return self._send(400, err)
                eid = "".join(secrets.choice("abcdefghijklmnopqrstuv0123456789") for _ in range(26))
                ev = {"kind": "calendar#event", "id": eid, "status": "confirmed",
                      "htmlLink": f"{srv.base_url}/calendar/event?eid={eid}", "created": self._now()}
                _merge(ev, body)
                ev["updated"] = ev["created"]
                ev["etag"] = f'"{secrets.token_hex(8)}"'
                events[eid] = ev
                return self._send(200, ev)
            return nf()
        ev = events.get(segs[3])
        if ev is None or len(segs) > 4:
            return nf()
        if method == "GET":
            return self._send(200, ev)
        if ev["status"] == "cancelled":
            return self._send(410, _api_err(410, "Resource has been deleted", "deleted"))
        if method == "DELETE":
            ev["status"] = "cancelled"
            ev["updated"] = self._now()
            return self._send(204)
        if method == "PATCH":
            merged = json.loads(json.dumps(ev))
            _merge(merged, body)
            err = self._invalid(merged)
            if err:
                return self._send(400, err)
            merged.update(updated=self._now(), etag=f'"{secrets.token_hex(8)}"')
            events[segs[3]] = merged
            return self._send(200, merged)
        nf()

    def _now(self) -> str:
        srv = self.server
        srv.clock = max(time.time(), srv.clock + 0.001)  # updated가 항상 커지게
        return _rfc3339(srv.clock)

    @staticmethod
    def _invalid(ev: dict) -> dict | None:
        start = _parse_dt((ev.get("start") or {}).get("dateTime"))
        end = _parse_dt((ev.get("end") or {}).get("dateTime"))
        if start is None or end is None:
            return _api_err(400, "Missing time", "required")
        if end < start:
            return _api_err(400, "The specified time range is empty.", "timeRangeEmpty")
        if len(((ev.get("reminders") or {}).get("overrides")) or []) > 5:
            return _api_err(400, "Too many reminders", "invalid")
        return None

    def _list(self, events: dict, q: dict) -> None:
        srv = self.server
        items = list(events.values())
        if (q.get("showDeleted") or ["false"])[0] != "true":
            items = [e for e in items if e["status"] != "cancelled"]
        for cond in q.get("privateExtendedProperty") or []:
            k, _, v = cond.partition("=")
            items = [e for e in items if ((e.get("extendedProperties") or {}).get("private") or {}).get(k) == v]
        try:
            size = min(int((q.get("maxResults") or [250])[0]), srv.page_size)
            offset = int((q.get("pageToken") or ["0"])[0].lstrip("p") or 0)
        except ValueError:
            return self._send(400, _api_err(400, "Invalid value", "invalid"))
        page = {"kind": "calendar#events", "items": items[offset:offset + size]}
        if offset + size < len(items):
            page["nextPageToken"] = f"p{offset + size}"
        self._send(200, page)

    def _page(self, items: list, q: dict, kind: str) -> None:
        try:
            size = min(int((q.get("maxResults") or [250])[0]), self.server.page_size)
            offset = int((q.get("pageToken") or ["0"])[0].lstrip("p") or 0)
        except ValueError:
            return self._send(400, _api_err(400, "Invalid value", "invalid"))
        page = {"kind": kind, "items": items[offset:offset + size]}
        if offset + size < len(items):
            page["nextPageToken"] = f"p{offset + size}"
        self._send(200, page)

    def _calendar_list(self, q: dict) -> None:
        """사용자 캘린더 + 이 앱이 만든 캘린더(구글도 목록에 넣는다 — 앱이 스스로 빼야 함)."""
        data = self.server.data
        items = [json.loads(json.dumps(c)) for c in data["user_calendars"].values()]
        for cid, c in data["calendars"].items():
            items.append({"kind": "calendar#calendarListEntry", "id": cid, "summary": c.get("summary") or "",
                          "backgroundColor": "#ac725e", "accessRole": "owner", "selected": True})
        if (q.get("showHidden") or ["false"])[0] != "true":
            items = [c for c in items if not c.get("hidden")]
        self._page(items, q, "calendar#calendarList")

    def _instances(self, ev: dict) -> list[dict]:
        """반복 일정(RRULE:FREQ=WEEKLY;COUNT=n 만) → 하나하나의 일정(singleEvents=true)."""
        rule = next((r for r in ev.get("recurrence") or [] if str(r).startswith("RRULE:")), None)
        m = re.search(r"FREQ=WEEKLY;COUNT=(\d+)", str(rule or ""))
        if not m:
            return [ev]
        out = []
        for k in range(int(m.group(1))):
            inst = {key: v for key, v in json.loads(json.dumps(ev)).items() if key != "recurrence"}
            for side in ("start", "end"):
                s = _parse_dt(ev[side].get("dateTime"))
                inst[side] = {"dateTime": (s + timedelta(days=7 * k)).isoformat()}
            stamp = _parse_dt(inst["start"]["dateTime"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            inst.update(id=f"{ev['id']}_{stamp}", recurringEventId=ev["id"], originalStartTime=inst["start"])
            out.append(inst)
        return out

    def _edge(self, side: dict) -> datetime | None:
        """일정의 시작·끝 → 비교용 시각(종일은 그 날 0시, 서버 시간대)."""
        if isinstance(side, dict) and side.get("date"):
            try:
                return datetime.combine(date.fromisoformat(side["date"]), dtime(0), tzinfo=self.server.tz)
            except ValueError:
                return None
        return _parse_dt((side or {}).get("dateTime"))

    def _list_user(self, cid: str, q: dict) -> None:
        srv = self.server
        single = (q.get("singleEvents") or ["false"])[0] == "true"
        order = (q.get("orderBy") or [None])[0]
        if order == "startTime" and not single:
            return self._send(400, _api_err(400, "The requested ordering is not available for the particular query.",
                                            "badRequest"))
        items = []
        for ev in srv.data["user_events"].get(cid, {}).values():
            items += self._instances(ev) if single else [json.loads(json.dumps(ev))]
        if (q.get("showDeleted") or ["false"])[0] != "true":
            items = [e for e in items if e.get("status") != "cancelled"]
        lo = _parse_dt((q.get("timeMin") or [None])[0]) if q.get("timeMin") else None
        hi = _parse_dt((q.get("timeMax") or [None])[0]) if q.get("timeMax") else None
        if (q.get("timeMin") and lo is None) or (q.get("timeMax") and hi is None):
            return self._send(400, _api_err(400, "Bad Request", "badRequest"))
        keep = []
        for e in items:
            s, t = self._edge(e.get("start")), self._edge(e.get("end"))
            if s is None or t is None or (lo and t <= lo) or (hi and s >= hi):
                continue
            keep.append((s, e))
        if order == "startTime":
            keep.sort(key=lambda x: x[0])
        if srv.data["user_calendars"][cid].get("accessRole") == "freeBusyReader":   # 바쁨만 볼 수 있는 캘린더: 시각만 준다
            keep = [(s, {k: v for k, v in e.items() if k in ("kind", "id", "status", "start", "end")}) for s, e in keep]
        self._page([e for _, e in keep], q, "calendar#events")

    def _event_page(self, q: dict) -> None:
        """htmlLink로 열 수 있는 데모용 일정 화면(인증 없음, 로컬 데모 전용)."""
        eid = (q.get("eid") or [""])[0]
        every = list(self.server.data["events"].values()) + list(self.server.data["user_events"].values())
        ev = next((evs[k] for k in (eid, eid.split("_")[0]) for evs in every if k in evs), None)
        if ev is None:
            return self._send(404, raw="<p>일정 없음</p>".encode(), ctype="text/html; charset=utf-8")
        page = (f"<!doctype html><meta charset=utf-8><title>{html.escape(ev.get('summary') or '')}</title>"
                f"<h1>{html.escape(ev.get('summary') or '')}</h1>"
                f"<p>{html.escape(str((ev.get('end') or {}).get('dateTime')))} · {html.escape(ev['status'])}</p>"
                f"<pre>{html.escape(ev.get('description') or '')}</pre><p>가짜 구글 캘린더(데모)</p>")
        self._send(200, raw=page.encode("utf-8"), ctype="text/html; charset=utf-8")


def start_mock_google(port: int = 0, today: date | None = None,
                      tz: tzinfo | None = None) -> tuple[ThreadingHTTPServer, str]:
    """가짜 구글을 127.0.0.1에 백그라운드(daemon) 스레드로 띄우고 (server, base_url)을 돌려준다.

    server.requests: (method, 경로+쿼리) 목록. server.auth_log: 같은 순서의 Authorization 헤더.
    server.auth_queries: 동의 화면이 받은 쿼리(scope·include_granted_scopes 확인용).
    server.data: clients/codes/access_tokens/refresh_tokens/calendars/events(앱이 만든 것)
    + user_calendars/user_events(읽기만 되는 사용자 캘린더, build_demo_calendars — today·tz 기준) + granted(클라이언트별 준 권한).
    조절: token_ttl(초), page_size, faults([{method, path(정규식), status, count}]),
    auth_error(예: "access_denied"), grant_scope(허용할 scope 문자열; None이면 요청한 그대로).
    끌 때는 server.shutdown(); server.server_close().
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    server.tz = tz or datetime.now().astimezone().tzinfo
    user_cals, user_events = build_demo_calendars(today, server.tz, server.base_url)
    server.data = {"clients": {DEMO_CLIENT_ID: {"secret": DEMO_CLIENT_SECRET}}, "codes": {},
                   "access_tokens": {}, "refresh_tokens": {}, "calendars": {}, "events": {},
                   "user_calendars": user_cals, "user_events": user_events, "granted": {}}
    server.requests, server.auth_log, server.challenges, server.faults = [], [], [], []
    server.auth_queries = []
    server.token_ttl, server.page_size = 3600, 250
    server.auth_error, server.grant_scope = None, None
    server.clock = 0.0
    server.lock = threading.Lock()
    threading.Thread(target=server.serve_forever, args=(0.1,), name="mock-google", daemon=True).start()
    return server, server.base_url
