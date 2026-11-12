"""데모·테스트용 가짜 Google OAuth + Calendar 서버.

gcal.py가 쓰는 엔드포인트만 흉내 낸다. 앱은 f"{base}/o/oauth2/v2/auth", f"{base}/token",
f"{base}/revoke", f"{base}/calendar/v3"을 쓴다. calendar.app.created처럼 API로 만든 캘린더만 다룰 수 있다.
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO_CLIENT_ID = "demo-client.apps.googleusercontent.com"
DEMO_CLIENT_SECRET = "demo-secret"


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
                return self._calendar(method, path[len("/calendar/v3"):], query, body)
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
        srv.data["codes"][code] = {"client_id": get("client_id"), "redirect_uri": redirect, "scope": scope,
                                   "challenge": get("code_challenge"), "expires_at": time.time() + 600}
        srv.challenges.append(get("code_challenge"))
        qs = urllib.parse.urlencode({"code": code, "state": get("state") or "", "scope": scope})
        self._send(302, headers={"Location": redirect + sep + qs})

    def _issue(self, client_id: str, refresh: str, scope: str) -> dict:
        srv = self.server
        access = "ya29.mock-" + secrets.token_urlsafe(24)
        srv.data["access_tokens"][access] = {"client_id": client_id, "refresh": refresh,
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
            del data["refresh_tokens"][token]
            for a in [a for a, v in data["access_tokens"].items() if v["refresh"] == token]:
                del data["access_tokens"][a]
        elif token in data["access_tokens"]:
            del data["access_tokens"][token]
        else:
            return self._send(400, {"error": "invalid_token"})
        self._send(200, {})

    # ---- Calendar ----

    def _calendar(self, method: str, path: str, q: dict, body: dict) -> None:
        srv, data = self.server, self.server.data
        segs = [urllib.parse.unquote(s) for s in path.strip("/").split("/")]
        nf = lambda: self._send(404, _api_err(404, "Not Found", "notFound"))  # noqa: E731
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

    def _event_page(self, q: dict) -> None:
        """htmlLink로 열 수 있는 데모용 일정 화면(인증 없음, 로컬 데모 전용)."""
        eid = (q.get("eid") or [""])[0]
        ev = next((evs[eid] for evs in self.server.data["events"].values() if eid in evs), None)
        if ev is None:
            return self._send(404, raw="<p>일정 없음</p>".encode(), ctype="text/html; charset=utf-8")
        page = (f"<!doctype html><meta charset=utf-8><title>{html.escape(ev.get('summary') or '')}</title>"
                f"<h1>{html.escape(ev.get('summary') or '')}</h1>"
                f"<p>{html.escape(str((ev.get('end') or {}).get('dateTime')))} · {html.escape(ev['status'])}</p>"
                f"<pre>{html.escape(ev.get('description') or '')}</pre><p>가짜 구글 캘린더(데모)</p>")
        self._send(200, raw=page.encode("utf-8"), ctype="text/html; charset=utf-8")


def start_mock_google(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """가짜 구글을 127.0.0.1에 백그라운드(daemon) 스레드로 띄우고 (server, base_url)을 돌려준다.

    server.requests: (method, 경로+쿼리) 목록. server.auth_log: 같은 순서의 Authorization 헤더.
    server.data: clients/codes/access_tokens/refresh_tokens/calendars/events.
    조절: token_ttl(초), page_size, faults([{method, path(정규식), status, count}]),
    auth_error(예: "access_denied"), grant_scope(허용할 scope 문자열; None이면 요청한 그대로).
    끌 때는 server.shutdown(); server.server_close().
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    server.data = {"clients": {DEMO_CLIENT_ID: {"secret": DEMO_CLIENT_SECRET}}, "codes": {},
                   "access_tokens": {}, "refresh_tokens": {}, "calendars": {}, "events": {}}
    server.requests, server.auth_log, server.challenges, server.faults = [], [], [], []
    server.token_ttl, server.page_size = 3600, 250
    server.auth_error, server.grant_scope = None, None
    server.clock = 0.0
    server.lock = threading.Lock()
    threading.Thread(target=server.serve_forever, args=(0.1,), name="mock-google", daemon=True).start()
    return server, server.base_url
