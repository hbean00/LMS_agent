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
from .ics import validate_ics
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
    app = App(_store(home), home)
    app.sync_in_background()  # 켤 때마다 바뀐 곳만 확인한다
    _run_server(app, args.port, not args.no_browser)


def cmd_demo(args) -> None:
    from .mock_canvas import DEMO_TOKEN, start_mock_canvas
    from .server import App

    from .mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls

    mock, base = start_mock_canvas()
    gmock, gbase = start_mock_google()  # 데모의 '구글 캘린더 연결'은 가짜 구글로 간다
    home = Path(tempfile.mkdtemp(prefix="lms-agent-demo-"))
    cfg = {"canvas_url": base, "token": DEMO_TOKEN}
    from . import mock_ai  # 데모의 [AI로 정리]는 가짜 AI(실제 서비스로 글을 보내지 않음)
    from .mock_canvas import DEMO_STUDENT_ID
    store = _store(home)
    # 데모만: 가짜 점수표의 내 줄(가짜 학번)을 찾게 학번을 미리 넣는다 → 성적 화면에 LAB 세부 점수가 보인다(계약 14장).
    # 설정에서 지우면 '학번을 넣으면 내 점수를 찾아 드려요' 안내를 볼 수 있다.
    store.data["settings"].setdefault("student_id", DEMO_STUDENT_ID)
    app = App(store, home, cfg=cfg, demo=True,
              google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=urls(gbase),
              ai_runner=mock_ai.runner, ai_command=mock_ai.DEMO_COMMAND)
    app.do_sync()
    _out("데모 모드: 가짜 Canvas·가짜 구글로 실행합니다. 실제 계정에는 접속하지 않습니다.")
    try:
        _run_server(app, args.port, not args.no_browser)
    finally:
        mock.shutdown()
        gmock.shutdown()


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
    by = "background" if args.background else "cli"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        # 백그라운드로 도는 동안 앱에서 로그아웃하면 저장하지 않는다(설정 파일의 토큰이 바뀌었는지 본다)
        r = core.sync(Canvas(cfg["canvas_url"], cfg["token"]), store, by=by,
                      still_valid=lambda: load_config(home).get("token") == cfg["token"],
                      **core.sync_options(store))
    except core.SyncCancelled as e:
        _out(f"[{stamp}] {e}")
        return
    except (CanvasError, TimeoutError) as e:
        _out(f"[{stamp}] 동기화 실패: {e}")
        sys.exit(1)
    _out(f"[{stamp}] 확인 {r['checked']}곳 · 건너뜀 {r['skipped']}곳 → 찾은 마감 {r['candidates']}개"
         f"(검토 필요 {r['needs_review']}), 신규 {len(r['new_items'])}개")
    for e in r["errors"]:
        _out(f"  경고: {e}")
    if args.background and store.data["settings"].get("notify", True):  # 앱을 안 열어도 알 수 있게
        from .notify import new_deadlines_message, toast
        view = store.view()
        core.annotate(view, core.semester_info(store.data.get("cache"), view), store.data.get("cache"))
        msg = new_deadlines_message(r, view)
        if msg and toast(*msg):
            _out(f"  알림: {msg[0]}")
    if args.push:  # 구글 캘린더에 반영(연결돼 있을 때만)
        from .config import google_client
        from .gcal import GoogleError
        from .gsync import GoogleLink

        link = GoogleLink(cfg, home, google_client())
        try:
            g = link.push(store)
        except GoogleError as e:
            _out(f"  구글 반영 실패: {e}")
            sys.exit(1)
        _out("  구글: 연결 안 됨(건너뜀)" if g is None else
             f"  구글: 새로 {g['created']} · 수정 {g['updated']} · 삭제 {g['deleted']} · 그대로 {g['unchanged']}")


def cmd_ai_eval(args) -> None:
    """AI CLI 하네스 평가 묶음(evals)을 실제 CLI로 돌린다. 데모 문장만 보낸다(내 강의 글은 보내지 않음)."""
    from . import agent_cli

    if not agent_cli.detect():
        _out("AI CLI(claude)를 찾지 못했습니다. 설치했다면 LMS_AGENT_AI_CLI에 전체 경로를 넣어 보세요.")
        sys.exit(2)
    r = agent_cli.run_evals()
    for c in r["cases"]:
        mark = "통과" if c["passed"] else "실패"
        why = "" if c["passed"] else f" — {c.get('problem') or c.get('error')}"
        _out(f"  [{mark}] {c['name']} ({c['task']}, {c['attempts']}회, {c['duration_ms']}ms){why}")
    _out(f"{r['cli']}: {r['passed']}/{r['total']} 통과")
    sys.exit(0 if r["passed"] == r["total"] else 1)


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
    text = core.calendar_text(store, record=True)
    problems = validate_ics(text)
    if problems:
        _out("캘린더 파일 검사 실패: " + "; ".join(problems))
        sys.exit(1)
    out = Path(args.output)
    out.write_bytes(text.encode("utf-8"))
    store.save()
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
    sp = sub.add_parser("sync", help="Canvas에서 바뀐 곳만 다시 읽기")
    sp.add_argument("--push", action="store_true", help="끝나면 구글 캘린더에 반영")
    sp.add_argument("--background", action="store_true", help="작업 스케줄러가 실행할 때(실행 기록에 표시)")
    sp.set_defaults(fn=cmd_sync)
    sub.add_parser("list", help="찾은 마감 보기").set_defaults(fn=cmd_list)
    sp = sub.add_parser("google-test", help="구글 캘린더 연결 시험(데모 일정으로)")
    sp.add_argument("--mine", action="store_true", help="데모 대신 내 승인된 일정으로 시험")
    sp.set_defaults(fn=lambda a: sys.exit(__import__("lms_agent.gtest", fromlist=["run"]).run(mine=a.mine, out=_out)))
    sub.add_parser("ai-eval", help="AI CLI 하네스 평가(데모 문장으로 실제 CLI 시험)").set_defaults(fn=cmd_ai_eval)
    sp = sub.add_parser("export", help="캘린더 파일(.ics) 저장")
    sp.add_argument("-o", "--output", default="lms-deadlines.ics")
    sp.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        args = p.parse_args(["serve"] + (argv or sys.argv[1:]))
    args.fn(args)


if __name__ == "__main__":
    main()
