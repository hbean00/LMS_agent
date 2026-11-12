"""내 PC에서만 열리는 화면 서버(127.0.0.1). 계약: docs/CONTRACTS.md 3장."""
from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, core
from .canvas import Canvas, CanvasError
from .config import is_configured, load_config, normalize_url, save_config
from .ics import build_ics, validate_ics
from .store import Store
from .timeutil import iso, now_local

MAX_BODY = 200_000


class App:
    def __init__(self, store: Store, home: Path, cfg: dict | None = None, demo: bool = False):
        self.store = store
        self.home = home
        self.cfg = cfg if cfg is not None else load_config(home)
        self.demo = demo
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.syncing = False

    def canvas(self) -> Canvas:
        return Canvas(self.cfg["canvas_url"], self.cfg["token"])

    def state(self) -> dict:
        d = self.store.data
        courses = [{"id": c["id"], "name": c["name"]} for c in (d.get("cache") or {}).get("courses", [])]
        return {
            "app_version": __version__, "now": iso(now_local()), "demo": self.demo,
            "configured": is_configured(self.cfg), "canvas_url": self.cfg.get("canvas_url"),
            "last_sync": d.get("last_sync"), "syncing": self.syncing, "report": d.get("report"),
            "settings": {"include_canvas": bool(d["settings"].get("include_canvas", True))},
            "courses": courses, "items": self.store.view(),
        }

    def do_sync(self) -> dict:
        if not is_configured(self.cfg):
            raise ValueError("먼저 Canvas 주소와 토큰을 설정하세요")
        with self.lock:
            self.syncing = True
            try:
                return core.sync(self.canvas(), self.store)
            finally:
                self.syncing = False

    def setup(self, url: str, token: str) -> None:
        url, token = normalize_url(url), (token or "").strip()
        if not url or not token:
            raise ValueError("Canvas 주소와 토큰을 모두 입력하세요")
        if not urlparse(url).netloc:
            raise ValueError("Canvas 주소가 올바르지 않습니다")
        Canvas(url, token).whoami()  # 실제로 되는지 먼저 확인
        self.cfg = {**self.cfg, "canvas_url": url, "token": token}
        save_config(self.cfg, self.home)


def _page(token: str) -> bytes:
    html = resources.files("lms_agent").joinpath("web/index.html").read_text("utf-8")
    return html.replace("__LMS_AGENT_TOKEN__", token).encode("utf-8")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lms-agent"

        def log_message(self, *args):  # 터미널을 조용히 둔다
            pass

        # -------------------------------------------------------- 응답

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, code: int, msg: str) -> None:
            self._json(code, {"error": msg})

        # -------------------------------------------------------- 보안 검사

        def _host_ok(self) -> bool:
            # DNS 리바인딩 막기: 다른 이름으로 들어온 요청은 받지 않는다
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _token_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-LMS-Agent") or "", app.token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("요청이 너무 큽니다")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 필요합니다")
            return data

        # -------------------------------------------------------- 라우팅

        def do_GET(self):
            if not self._host_ok():
                return self._error(403, "허용되지 않은 호스트입니다")
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, _page(app.token), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                              "frame-ancestors 'none'"})
            if path == "/calendar.ics":
                text = build_ics(app.store.calendar_items())
                problems = validate_ics(text)
                if problems:
                    return self._error(500, "캘린더 파일 검사 실패: " + "; ".join(problems[:3]))
                return self._send(200, text.encode("utf-8"), "text/calendar; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="lms-deadlines.ics"'})
            if path == "/api/state":
                if not self._token_ok():
                    return self._error(403, "화면을 새로 고침하세요(세션 값이 맞지 않습니다)")
                return self._json(200, app.state())
            return self._error(404, "없는 경로입니다")

        def do_POST(self):
            if not self._host_ok():
                return self._error(403, "허용되지 않은 호스트입니다")
            if not self._token_ok():
                return self._error(403, "화면을 새로 고침하세요(세션 값이 맞지 않습니다)")
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/sync":
                    try:
                        app.do_sync()
                    except CanvasError as e:
                        return self._error(502, str(e))
                    return self._json(200, app.state())
                m = re.fullmatch(r"/api/item/([\w\-]+)", path)
                if m:
                    allowed = {k: body[k] for k in ("decision", "submitted", "title", "due") if k in body}
                    try:
                        app.store.update_user(m.group(1), allowed)
                    except KeyError:
                        return self._error(404, "항목을 찾을 수 없습니다. 동기화 후 다시 시도하세요")
                    app.store.save()
                    return self._json(200, app.state())
                if path == "/api/manual":
                    app.store.add_manual(str(body.get("course_name") or ""), str(body.get("text") or ""),
                                         body.get("posted_at"))
                    core.rebuild(app.store)
                    app.store.save()
                    return self._json(200, app.state())
                if path == "/api/settings":
                    if "include_canvas" in body:
                        app.store.data["settings"]["include_canvas"] = bool(body["include_canvas"])
                    app.store.save()
                    return self._json(200, app.state())
                if path == "/api/setup":
                    if app.demo:
                        return self._error(400, "데모 모드에서는 설정을 바꿀 수 없습니다")
                    try:
                        app.setup(str(body.get("canvas_url") or ""), str(body.get("token") or ""))
                    except CanvasError as e:
                        return self._error(400, f"연결 확인 실패: {e}")
                    return self._json(200, {"ok": True})
                return self._error(404, "없는 경로입니다")
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))

    return Handler


def start_server(app: App, port: int = 8765) -> ThreadingHTTPServer:
    """port가 쓰이고 있으면 다음 번호를 시도한다. port=0이면 아무 빈 포트."""
    last = None
    for p in ([0] if port == 0 else range(port, port + 20)):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            httpd.daemon_threads = True
            return httpd
        except OSError as e:
            last = e
    raise OSError(f"빈 포트를 찾지 못했습니다: {last}")


def serve_in_thread(httpd: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t
