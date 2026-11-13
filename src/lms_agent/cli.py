"""명령어: lms-agent [serve|demo|setup|sync|list|ask|update|export]. 인자 없이 실행하면 화면을 연다."""
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
    from . import updater
    from .features.update import start_background_checks
    from .server import App

    home = data_dir()
    store = _store(home)
    up = updater.Updater(store, home)
    up.startup_cleanup()            # 지난 업데이트가 남긴 옛 실행 파일(.old) 지우기
    if up.auto_update_at_start(out=_out):   # 설정에서 켰을 때만(exe·pip). 적용하면 새 버전으로 다시 켜져 돌아오지 않는다
        return
    app = App(store, home)
    app.sync_in_background()  # 켤 때마다 바뀐 곳만 확인한다
    start_background_checks(app)    # 새 버전 확인은 하루 한 번(설정에서 끌 수 있음, 계약 19장)
    _run_server(app, args.port, not args.no_browser)


def cmd_update(args) -> None:
    """터미널에서 새 버전 확인·업데이트. 화면의 [지금 업데이트]와 같은 확인(체크섬)·백업을 거친다."""
    from . import updater

    home = data_dir()
    up = updater.Updater(_store(home), home)
    c = up.check()
    if c.get("error"):
        _out(f"확인하지 못했어요: {c['error']}")
        sys.exit(1)
    if not up.available():
        _out(f"지금 버전(v{__version__})이 최신이에요.")
        return
    _out(f"새 버전이 있어요: v{up.latest()} (지금 v{__version__}). 바뀐 점: {c.get('notes_url') or updater.RELEASES_PAGE}")
    if args.check:
        return
    plan = up.plan()
    if not plan["can_apply"]:
        _out(f"{plan['reason']}. " + (f"직접 하려면: {plan['command']}" if plan["command"] else "릴리스 페이지에서 받아 바꿔 주세요."))
        sys.exit(1)
    if not args.yes and input("지금 업데이트할까요? 데이터는 먼저 백업해요 [y/N] ").strip().lower() not in ("y", "yes", "ㅇ"):
        _out("업데이트하지 않았어요.")
        return
    r = up.prepare()
    if not r.get("ok"):
        _out(r["message"])
        sys.exit(1)
    if r.get("finish"):
        try:
            r["finish"]()
        except OSError as e:
            _out(f"실행 파일을 바꾸지 못했어요({e}). 앱을 모두 닫고 다시 해 보거나 릴리스 페이지에서 받아 주세요.")
            sys.exit(1)
        _out(f"v{up.latest()} 파일로 바꿨어요. 앱을 다시 켜면 새 버전이 열려요.")
        return
    _out(r["message"])


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


def _print_thread(th: dict, message: str) -> None:
    by = {"ai": "AI", "rules": "규칙 검색"}.get(th.get("by"), "?")
    _out(f"질문: {message}")
    if th.get("status") == "error":
        _out(f"답을 만들지 못했어요: {th.get('error')}")
        return
    _out(f"답({by}): {th.get('answer')}")
    if th.get("results"):
        _out("찾은 글:")
        for r in th["results"][:5]:
            when = (r.get("posted_at") or "")[:10]
            _out(f"  · [{r.get('course_short') or '과목 미상'}] {r.get('title') or ''} ({when}) — "
                 f"{' '.join(str(r.get('snippet') or '').split())[:90]}")
    props = th.get("proposals") or []
    if not props:
        return
    _out("제안:")
    for i, p in enumerate(props, 1):
        state = ("반영함" if p.get("applied") else "설명" if p.get("type") == "note"
                 else "반영 가능" if not p.get("rejected_reason") else f"반영 안 됨: {p['rejected_reason']}")
        _out(f"  {i}. [{state}] {p.get('summary') or p.get('type')}")
        if p.get("evidence"):
            src = f" ({p.get('source_title')})" if p.get("source_title") else ""
            _out(f"     근거: \"{' '.join(str(p['evidence']).split())[:150]}\"{src}")
        if p.get("reason"):
            _out(f"     이유: {p['reason']}")


def cmd_ask(args) -> None:
    """에이전트에게 묻기(계약 20장)를 터미널에서. 같은 질문의 답이 이미 있으면 --apply N은 다시 묻지 않고 그 제안을 반영한다."""
    from . import agent, agent_cli

    home = data_dir()
    store = _store(home)
    try:
        msg, cid = agent.check_message(args.message, args.course, store)
    except ValueError as e:
        _out(str(e))
        sys.exit(2)
    names = [n for n in [load_config(home).get("user_name")] if n]
    th = None
    if args.apply:
        th = next((t for t in reversed(agent.threads(store)) if t.get("message") == msg and t.get("course_id") == cid
                   and t.get("status") == "done"), None)
    if th is None:
        with store.locked():
            store.reload_if_changed()
            th = agent.new_thread(store, msg, cid)
            store.write()
        ai_on = bool(store.data["settings"].get("ai_cli"))
        if ai_on:
            _out("AI에게 묻는 중이에요(수십 초 걸릴 수 있어요)…")
        agent.run_job(store, th["id"], msg, cid, ai_enabled=ai_on, command=agent_cli.detect(), names=names)
        th = agent.find_thread(store, th["id"]) or th
    _print_thread(th, msg)
    if not args.apply:
        if any(not p.get("rejected_reason") and p.get("type") != "note" and not p.get("applied")
               for p in th.get("proposals") or []):
            _out(f"반영하려면: lms-agent ask \"{msg}\"" + (f" --course {cid}" if cid else "") + " --apply 번호")
        return
    try:
        with store.locked():
            store.reload_if_changed()
            p = agent.apply(store, th["id"], args.apply - 1, names=names)
            store.write()
    except (LookupError, ValueError, agent.AgentConflict) as e:
        _out(f"반영하지 못했어요: {e}")
        sys.exit(1)
    _out(f"반영했어요: {p.get('summary')}" + (" — 확인함에 후보로 넣었어요(승인해 주세요)" if p.get("by") == "rules"
                                              and p.get("type") == "add_deadline" else ""))


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
    sp = sub.add_parser("ask", help="에이전트에게 묻기(예: \"기말고사 날짜가 안 보여요\")")
    sp.add_argument("message", help="물어볼 내용(500자 이하)")
    sp.add_argument("--course", help="과목 id(이 과목 글만 찾기)")
    sp.add_argument("--apply", type=int, metavar="N", help="N번 제안을 반영(같은 질문의 답이 있으면 다시 묻지 않음)")
    sp.set_defaults(fn=cmd_ask)
    sp = sub.add_parser("google-test", help="구글 캘린더 연결 시험(데모 일정으로)")
    sp.add_argument("--mine", action="store_true", help="데모 대신 내 승인된 일정으로 시험")
    sp.set_defaults(fn=lambda a: sys.exit(__import__("lms_agent.gtest", fromlist=["run"]).run(mine=a.mine, out=_out)))
    sub.add_parser("ai-eval", help="AI CLI 하네스 평가(데모 문장으로 실제 CLI 시험)").set_defaults(fn=cmd_ai_eval)
    sp = sub.add_parser("update", help="새 버전 확인·업데이트(데이터를 먼저 백업)")
    sp.add_argument("--check", action="store_true", help="확인만 하고 바꾸지 않음")
    sp.add_argument("-y", "--yes", action="store_true", help="묻지 않고 업데이트")
    sp.set_defaults(fn=cmd_update)
    sp = sub.add_parser("export", help="캘린더 파일(.ics) 저장")
    sp.add_argument("-o", "--output", default="lms-deadlines.ics")
    sp.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        args = p.parse_args(["serve"] + (argv or sys.argv[1:]))
    args.fn(args)


if __name__ == "__main__":
    main()
