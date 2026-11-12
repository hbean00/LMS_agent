"""실제 구글 캘린더 연결 시험: 브라우저 로그인 → 'LMS 마감' 캘린더 → 데모 일정 반영 → 다시 반영(변경 0) 확인.

    lms-agent google-test            # 데모 Canvas 데이터의 일정으로 시험
    lms-agent google-test --mine     # 내 설정의 승인된 일정으로 시험

.env(또는 환경 변수)에 LMS_AGENT_GOOGLE_CLIENT_ID / LMS_AGENT_GOOGLE_CLIENT_SECRET 필요(데스크톱 앱 OAuth 클라이언트).
토큰은 데이터 폴더 config.json의 google 항목에 저장한다. 값은 화면에 출력하지 않는다.
"""
from __future__ import annotations

import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import core
from .config import STATE_FILE, data_dir, google_client, load_config, save_config
from .store import Store
from .gcal import deadline_window
from .timeutil import parse_iso


def _wait_for_code(timeout: float = 300) -> tuple[str, dict]:
    """127.0.0.1의 빈 포트로 리디렉트를 한 번 받는다. (redirect_uri, {code, state, error})"""
    got: dict = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            body = ("<meta charset=utf-8><p style='font-family:sans-serif'>"
                    + ("구글 연결이 끝났습니다. 이 창을 닫고 앱으로 돌아가세요." if "code" in got else "연결이 취소됐습니다.")
                    + "</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    srv = HTTPServer(("127.0.0.1", 0), H)
    uri = f"http://127.0.0.1:{srv.server_address[1]}/"
    t = threading.Thread(target=lambda: [srv.handle_request() for _ in range(3) if not done.is_set()], daemon=True)
    return uri, {"server": srv, "thread": t, "done": done, "got": got, "timeout": timeout}


def _desired(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        due = parse_iso(it.get("due"))
        if not due:
            continue
        from .ics import event_description, event_summary
        start, end = deadline_window(due)  # 길이 0: 알림(1일·3시간 전)이 실제 마감 기준으로 울린다
        out.append({"lms_id": it["id"], "summary": event_summary(it), "description": event_description(it),
                    "start": start, "end": end, "url": it.get("url"), "reminders": [1440, 180]})
    return out


def run(mine: bool = False, out=print, *, urls: dict | None = None, client: tuple | None = None,
        home: Path | None = None, open_browser=webbrowser.open) -> int:
    """urls/client/home/open_browser는 가짜 구글로 시험할 때만 바꾼다."""
    from .gcal import GoogleCalendar, GoogleError, OAuthFlow

    urls = urls or {}
    client_id, secret = client or google_client()
    if not client_id:
        out("LMS_AGENT_GOOGLE_CLIENT_ID가 없습니다. 프로젝트 폴더의 .env에 넣어 주세요.")
        return 2
    if client_id.startswith("AIza"):
        out("넣은 값은 API 키입니다. 일정을 쓰려면 OAuth 클라이언트 ID(데스크톱 앱)가 필요합니다.")
        return 2
    home = home or data_dir()
    cfg = load_config(home)

    if mine:
        store = Store(home / STATE_FILE)
    else:  # 데모 Canvas로 일정 만들기
        from .canvas import Canvas
        from .mock_canvas import DEMO_TOKEN, start_mock_canvas
        mock, base = start_mock_canvas()
        store = Store(Path(tempfile.mkdtemp(prefix="lms-agent-gtest-")) / STATE_FILE)
        core.sync(Canvas(base, DEMO_TOKEN), store)
        for v in store.view():  # 데모 후보는 모두 승인한 것으로
            if v["status"] == "review" and v["due"]:
                store.update_user(v["id"], {"decision": "approved"})
        mock.shutdown()
    items = store.calendar_items()
    sem = core.semester_info(store.data.get("cache"), store.view())
    core.annotate(items, sem, store.data.get("cache"))
    desired = _desired(items)
    out(f"반영할 일정 {len(desired)}개")

    g = cfg.get("google") or {}
    tokens = g.get("tokens")
    if not tokens:
        uri, ctx = _wait_for_code()
        flow = OAuthFlow(client_id, uri, client_secret=secret,
                         **{k: urls[k] for k in ("auth_url", "token_url") if k in urls})
        url = flow.authorization_url()
        ctx["thread"].start()
        out("브라우저에서 구글 로그인과 권한 동의를 해 주세요(이 앱이 만든 캘린더만 다룰 수 있는 권한).")
        open_browser(url)
        if not ctx["done"].wait(ctx["timeout"]):
            out("5분 안에 로그인이 끝나지 않아 멈춥니다.")
            return 1
        ctx["server"].server_close()
        got = ctx["got"]
        if "code" not in got:
            out(f"구글이 연결을 거절했습니다: {got.get('error', '알 수 없음')}")
            return 1
        tokens = flow.exchange(got["code"], got.get("state", ""))
        out("구글 연결됨.")

    def save_tokens(t: dict) -> None:
        cfg["google"] = {**(cfg.get("google") or {}), "tokens": t}
        save_config(cfg, home)

    save_tokens(tokens)
    try:
        cal = GoogleCalendar(tokens, client_id, client_secret=secret, on_tokens=save_tokens,
                             **{k: urls[k] for k in ("api_base", "token_url") if k in urls})
        cal_id = cal.ensure_calendar((cfg.get("google") or {}).get("calendar_id"))
        cfg["google"]["calendar_id"] = cal_id
        save_config(cfg, home)
        r1 = cal.sync(cal_id, desired)
        out(f"1차 반영: 새로 {r1['created']} · 수정 {r1['updated']} · 삭제 {r1['deleted']} · 그대로 {r1['unchanged']}")
        r2 = cal.sync(cal_id, desired)
        out(f"2차 반영(같은 내용): 새로 {r2['created']} · 수정 {r2['updated']} · 삭제 {r2['deleted']} · 그대로 {r2['unchanged']}")
        for e in r1["errors"] + r2["errors"]:
            out(f"  오류: {e}")
        ok = r2["created"] == r2["updated"] == r2["deleted"] == 0 and not r1["errors"]
        out("확인: 구글 캘린더 'LMS 마감'에서 일정을 보세요." if ok else "확인 필요: 위 오류를 보세요.")
        return 0 if ok else 1
    except GoogleError as e:
        out(f"구글 오류: {e}")
        return 1
