"""내 PC에서만 열리는 화면 서버(127.0.0.1).

여기는 보안 검사(Host, 세션 값, 본문 크기)와 HTTP만 맡는다. 실제 API는 features/ 의 기능 모듈이
api.router에 등록한다(api.py 맨 위 예시). 계약: docs/CONTRACTS.md.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import core
from .api import ApiError, Response, StateContext, router
from .canvas import Canvas, CanvasError
from .config import is_configured, load_config
from .features import load_all
from .store import Store

MAX_BODY = 200_000

load_all()


class App:
    """앱 하나의 상태. 기능 모듈이 on_init으로 서비스(예: app.google)를 붙인다."""

    def __init__(self, store: Store, home: Path, cfg: dict | None = None, demo: bool = False, **options):
        self.store = store
        self.home = home
        self.cfg = cfg if cfg is not None else load_config(home)
        self.demo = demo
        self.options = options        # 예: google=(id, secret), google_urls={…} (데모·테스트용 가짜 구글)
        self.token = secrets.token_urlsafe(24)
        self.port: int | None = None  # start_server가 채운다
        self.lock = threading.Lock()
        self.syncing = False
        self.sync_error: str | None = None
        self.session_gen = 0          # 로그인·로그아웃마다 1씩: 그 전에 시작한 동기화는 저장하지 않는다
        for hook in router.init_hooks:
            hook(self, options)

    def canvas(self) -> Canvas:
        return Canvas(self.cfg["canvas_url"], self.cfg["token"])

    def state(self) -> dict:
        ctx = StateContext(self)
        out: dict = {}
        for key, fn in router.sections:
            value = fn(self, ctx)
            if key is None:
                out.update(value)
            else:
                out[key] = value
        return out

    def emit(self, event: str, **kw) -> None:
        for fn in router.listeners.get(event, []):
            fn(self, **kw)

    def do_sync(self, full: bool = False) -> dict:
        if not is_configured(self.cfg):
            raise ValueError("먼저 Canvas 주소와 토큰을 설정하세요")
        with self.lock:
            self.syncing = True
            gen = self.session_gen
            try:
                if not is_configured(self.cfg):  # 잠금을 기다리는 사이 로그아웃됐다
                    return {}
                opts = core.sync_options(self.store, demo_home=self.home if self.demo else None)
                report = core.sync(self.canvas(), self.store, full=full,
                                   still_valid=lambda: self.session_gen == gen and is_configured(self.cfg),
                                   ai_runner=getattr(self, "ai_runner", None),
                                   ai_command=getattr(self, "ai_command", None), **opts)
                self.sync_error = None
            except core.SyncCancelled:
                return {}
            except CanvasError as e:
                self.sync_error = str(e)
                raise
            finally:
                self.syncing = False
        self.emit("changed")
        return report

    def sync_in_background(self) -> threading.Thread | None:
        """앱을 켤 때 부른다. 화면은 state.syncing이 true인 동안 상태를 다시 물어 본다."""
        if not is_configured(self.cfg):
            return None

        def run():
            try:
                self.do_sync()
            except (CanvasError, ValueError, OSError):
                pass  # sync_error에 남았다(CanvasError). 화면이 보여 준다.

        self.syncing = True  # 스레드가 잠금을 잡기 전에 화면이 먼저 물어봐도 '동기화 중'으로 보이게
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lms-agent"

        def log_message(self, *args):  # 터미널을 조용히 둔다
            pass

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

        def _error(self, code: int, msg: str) -> None:
            self._send(code, json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

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
            data = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 필요합니다")
            return data

        def _dispatch(self, method: str) -> None:
            if not self._host_ok():
                return self._error(403, "허용되지 않은 호스트입니다")
            parsed = urlparse(self.path)
            body = {k: v[0] for k, v in parse_qs(parsed.query).items()} if method == "GET" else None
            app.store.reload_if_changed()  # 백그라운드 동기화가 저장했으면 따라간다
            try:
                for route, groups in router.match(method, parsed.path):
                    if route.auth and not self._token_ok():
                        return self._error(403, "화면을 새로 고침하세요(세션 값이 맞지 않습니다)")
                    if body is None:
                        body = self._body()
                    result = route.handler(app, body, **groups)
                    if result is None:  # 이 경로가 맡지 않음 → 다음 후보
                        continue
                    if isinstance(result, Response):
                        return self._send(result.status, result.body, result.ctype, result.headers)
                    return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                return self._error(404, "없는 경로입니다")
            except ApiError as e:
                return self._error(e.status, str(e))
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def start_server(app: App, port: int = 8765) -> ThreadingHTTPServer:
    """port가 쓰이고 있으면 다음 번호를 시도한다. port=0이면 아무 빈 포트."""
    last = None
    for p in ([0] if port == 0 else range(port, port + 20)):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            httpd.daemon_threads = True
            app.port = httpd.server_address[1]
            return httpd
        except OSError as e:
            last = e
    raise OSError(f"빈 포트를 찾지 못했습니다: {last}")


def serve_in_thread(httpd: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t
