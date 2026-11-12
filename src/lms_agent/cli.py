"""명령어: lms-agent [serve|demo|setup|sync|list|export]. 인자 없이 실행하면 화면을 연다."""
from __future__ import annotations

import argparse
import getpass
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from . import __version__, core
from .canvas import Canvas, CanvasError
from .config import STATE_FILE, data_dir, is_configured, load_config, normalize_url, save_config
from .ics import build_ics, validate_ics
from .store import Store
from .timeutil import parse_iso

METHOD = {"email": "이메일", "lms": "LMS", "paper": "종이", "unknown": "확인 필요"}


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _store(home: Path) -> Store:
    return Store(home / STATE_FILE)


def _need_config(cfg: dict) -> None:
    if not is_configured(cfg):
        _out("아직 Canvas 연결이 설정되지 않았습니다. 'lms-agent setup'을 먼저 실행하거나, 'lms-agent'로 화면을 열어 설정하세요.")
        sys.exit(2)


def _run_server(app, port: int, open_browser: bool) -> None:
    from .server import serve_in_thread, start_server

    httpd = start_server(app, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    serve_in_thread(httpd)
    _out(f"LMS Agent {__version__} 화면: {url}")
    _out("이 창을 닫거나 Ctrl+C를 누르면 종료됩니다.")
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _out("종료합니다.")
    finally:
        httpd.shutdown()


def cmd_serve(args) -> None:
    from .server import App

    home = data_dir()
    _run_server(App(_store(home), home), args.port, not args.no_browser)


def cmd_demo(args) -> None:
    from .mock_canvas import DEMO_TOKEN, start_mock_canvas
    from .server import App

    mock, base = start_mock_canvas()
    home = Path(tempfile.mkdtemp(prefix="lms-agent-demo-"))
    cfg = {"canvas_url": base, "token": DEMO_TOKEN}
    app = App(_store(home), home, cfg=cfg, demo=True)
    app.do_sync()
    _out("데모 모드: 가짜 Canvas 데이터로 실행합니다. 실제 계정에는 접속하지 않습니다.")
    try:
        _run_server(app, args.port, not args.no_browser)
    finally:
        mock.shutdown()


def cmd_setup(args) -> None:
    home = data_dir()
    cfg = load_config(home)
    _out("Canvas 연결 설정")
    _out("토큰 발급: Canvas → 계정 → 설정 → '새 액세스 토큰'. 토큰은 이 PC에만 저장됩니다.")
    url = normalize_url(args.url or input(f"Canvas 주소 [{cfg.get('canvas_url') or '예: https://canvas.school.ac.kr'}]: ").strip()
                        or cfg.get("canvas_url", ""))
    token = getpass.getpass("액세스 토큰(입력해도 화면에 보이지 않습니다): ").strip() or cfg.get("token", "")
    if not url or not token:
        _out("주소와 토큰이 모두 필요합니다.")
        sys.exit(2)
    try:
        me = Canvas(url, token).whoami()
    except CanvasError as e:
        _out(f"연결 확인 실패: {e}")
        sys.exit(1)
    path = save_config({**cfg, "canvas_url": url, "token": token}, home)
    _out(f"연결됨: {me.get('name') or '사용자'} — 설정 저장 위치: {path}")


def cmd_sync(args) -> None:
    home = data_dir()
    cfg = load_config(home)
    _need_config(cfg)
    store = _store(home)
    try:
        r = core.sync(Canvas(cfg["canvas_url"], cfg["token"]), store)
    except CanvasError as e:
        _out(f"동기화 실패: {e}")
        sys.exit(1)
    _out(f"과목 {r['courses']} · 과제 {r['assignments']} · 공지 {r['announcements']} · 메시지 {r['messages']}"
         f" → 찾은 마감 {r['candidates']}개(검토 필요 {r['needs_review']}), 마감 근거 없는 과제 {r['no_deadline']}개")
    for e in r["errors"]:
        _out(f"  경고: {e}")


def cmd_list(args) -> None:
    store = _store(data_dir())
    groups = {"review": "검토 필요", "scheduled": "일정", "no_deadline": "마감 근거 없음"}
    for status, label in groups.items():
        rows = [v for v in store.view() if v["status"] == status]
        if not rows:
            continue
        _out(f"[{label}] {len(rows)}개")
        for v in rows:
            due = parse_iso(v["due"])
            when = f"{due:%m/%d %H:%M}" if due else "  마감 없음"
            _out(f"  {when}  {v['course_name']} · {v['title']} ({METHOD.get(v['method'], '')})")
    if not store.data["items"]:
        _out("항목이 없습니다. 먼저 'lms-agent sync'를 실행하세요.")


def cmd_export(args) -> None:
    store = _store(data_dir())
    text = build_ics(store.calendar_items())
    problems = validate_ics(text)
    if problems:
        _out("캘린더 파일 검사 실패: " + "; ".join(problems))
        sys.exit(1)
    out = Path(args.output)
    out.write_bytes(text.encode("utf-8"))
    _out(f"{text.count('BEGIN:VEVENT')}개 일정을 저장했습니다: {out.resolve()}")


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="lms-agent", description="Canvas에서 마감이 안 보이는 과제까지 찾아 캘린더로 모아 줍니다.")
    p.add_argument("--version", action="version", version=f"lms-agent {__version__}")
    sub = p.add_subparsers(dest="cmd")
    for name, fn, help_ in (("serve", cmd_serve, "화면 열기(기본)"), ("demo", cmd_demo, "가짜 데이터로 체험")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--port", type=int, default=8765)
        sp.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("setup", help="Canvas 주소와 토큰 설정")
    sp.add_argument("--url")
    sp.set_defaults(fn=cmd_setup)
    sub.add_parser("sync", help="Canvas에서 다시 읽기").set_defaults(fn=cmd_sync)
    sub.add_parser("list", help="찾은 마감 보기").set_defaults(fn=cmd_list)
    sp = sub.add_parser("export", help="캘린더 파일(.ics) 저장")
    sp.add_argument("-o", "--output", default="lms-deadlines.ics")
    sp.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        args = p.parse_args(["serve"] + (argv or sys.argv[1:]))
    args.fn(args)


if __name__ == "__main__":
    main()
