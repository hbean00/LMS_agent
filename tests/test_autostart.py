"""컴퓨터를 켜면 앱 열기(계약 22-8): OS별 등록(가짜 실행기·임시 홈), 실행 스크립트, 상태 칸·요청, 데모.
실제 작업 스케줄러·LaunchAgents·~/.config/autostart는 건드리지 않는다(runner·platform·user_home을 바꿔 씀)."""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lms_agent import autostart
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store


class FakeRunner:
    """schtasks 대신. /Create의 XML을 읽어 두고 /Query·/Delete에 맞게 답한다."""

    def __init__(self, create_rc=0):
        self.calls, self.xml, self.registered, self.create_rc = [], None, False, create_rc

    def __call__(self, args):
        self.calls.append(list(args))
        assert args[0] == "schtasks" and args[args.index("/TN") + 1] == autostart.TASK_NAME
        if args[1] == "/Create":
            self.xml = open(args[args.index("/XML") + 1], encoding="utf-16").read()
            if self.create_rc:
                return subprocess.CompletedProcess(args, self.create_rc, "", "ERROR: Access is denied.")
            self.registered = True
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        if args[1] == "/Query":
            return subprocess.CompletedProcess(args, 0 if self.registered else 1, "", "" if self.registered else "ERROR")
        if args[1] == "/Delete":
            self.registered = False
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        raise AssertionError(args)


def no_runner(args):
    raise AssertionError(f"이 OS에서는 schtasks를 부르면 안 됩니다: {args}")


# ---------------------------------------------------------------- Windows

def test_windows_task_register_query_delete(tmp_path):
    run = FakeRunner()
    a = autostart.Autostart(tmp_path / "home", runner=run, platform="win32")
    assert a.kind == "windows-task"
    assert a.status() == {"enabled": False} and run.calls == []     # 스크립트가 없으면 스케줄러를 묻지도 않는다
    a.enable(open_browser=True)
    assert run.calls[-1][:4] == ["schtasks", "/Create", "/TN", "LMS Agent App"]
    xml = run.xml
    assert "<LogonTrigger>" in xml and "<TimeTrigger>" not in xml               # 로그인할 때만
    assert "wscript.exe" in xml and "app-start.vbs" in xml                       # 창 없이
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml                # 켜 둔 앱을 시간 제한으로 끄지 않게
    assert "<Priority>5</Priority>" in xml
    vbs = a.launcher().read_text(encoding="utf-16")
    assert a.launcher().read_bytes()[:2] == b"\xff\xfe"                           # wscript가 읽는 UTF-16(BOM)
    assert a.launcher().read_bytes().count(b"\r\x00\n\x00") == len(vbs.splitlines())  # 줄 끝은 CRLF 하나씩
    assert '""serve"" ""--port"" ""8765""' in vbs and "--no-browser" not in vbs   # VBScript 문자열 안이라 " → ""
    assert "WinHttp.WinHttpRequest.5.1" in vbs and '"lms-agent-token"' in vbs   # 이미 떠 있으면 브라우저만
    assert "sh.Run url, 1, False" in vbs and "WScript.Quit 0" in vbs
    assert "LMS_AGENT_HOME" in vbs and "app.log" in vbs
    assert vbs.rstrip().endswith(", 0, False")                                     # 숨김, 기다리지 않음
    assert all(line.count('"') % 2 == 0 for line in vbs.splitlines() if not line.startswith("'"))
    assert a.status() == {"enabled": True}

    a.enable(open_browser=False)                                                   # 브라우저 없이로 바꾸기
    vbs = a.launcher().read_text(encoding="utf-16")
    assert "--no-browser" in vbs and "sh.Run url, 1" not in vbs
    assert "--no-browser" in a.command_text(False) and "--no-browser" not in a.command_text(True)

    a.disable()
    assert run.calls[-1][:2] == ["schtasks", "/Delete"]
    assert not a.launcher().exists() and a.status() == {"enabled": False}


def test_windows_register_failure_leaves_nothing(tmp_path):
    a = autostart.Autostart(tmp_path, runner=FakeRunner(create_rc=1), platform="win32")
    with pytest.raises(ValueError, match="작업 스케줄러에 등록하지 못했어요"):
        a.enable(True)
    assert not a.launcher().exists()


def test_windows_launcher_uses_pythonw_and_src_path(tmp_path):
    a = autostart.Autostart(tmp_path, runner=FakeRunner(), platform="win32")
    cmd = a.command(True)
    assert cmd[1:] == ["-m", "lms_agent", "serve", "--port", "8765"]
    if sys.platform == "win32" and Path(sys.executable).with_name("pythonw.exe").exists():
        assert cmd[0].lower().endswith("pythonw.exe")                              # 콘솔 창 없이


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("cscript"), reason="Windows 스크립트 호스트에서만")
def test_windows_vbs_starts_app_only_when_port_is_free(tmp_path, monkeypatch):
    """실제 cscript로 .vbs를 돌린다: 같은 포트에 우리 앱이 있으면 아무것도 켜지 않고, 없으면 명령을 켠다(가짜 명령: 표시 파일 쓰기)."""
    marker = tmp_path / "started.txt"
    code = f"open(r'{marker}','w').write('x')"
    monkeypatch.setattr(autostart, "serve_command", lambda ob=True, port=8765, platform=None: [sys.executable, "-c", code])
    with _fake_app_server() as port:
        a = autostart.Autostart(tmp_path, runner=FakeRunner(), platform="win32", port=port)
        a.write_launcher(open_browser=False)
        subprocess.run(["cscript", "//nologo", "//T:30", str(a.launcher())], check=True, timeout=60)
        time.sleep(1.5)
        assert not marker.exists()                                                # 이미 떠 있음 → 켜지 않음
    a = autostart.Autostart(tmp_path, runner=FakeRunner(), platform="win32", port=_free_port())
    a.write_launcher(open_browser=False)
    subprocess.run(["cscript", "//nologo", "//T:30", str(a.launcher())], check=True, timeout=60)
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists()                                                        # 비어 있음 → 켬


# ---------------------------------------------------------------- macOS·Linux

def test_macos_launch_agent(tmp_path):
    home, user = tmp_path / "data", tmp_path / "user"
    a = autostart.Autostart(home, runner=no_runner, platform="darwin", user_home=user)
    assert a.kind == "macos-launchagent" and a.status() == {"enabled": False}
    a.enable(open_browser=True)
    entry = user / "Library" / "LaunchAgents" / "kr.lmsagent.app.plist"
    assert a.entry() == entry and entry.exists()
    pl = plistlib.loads(entry.read_bytes())
    assert pl["Label"] == "kr.lmsagent.app" and pl["RunAtLoad"] is True and pl["KeepAlive"] is False
    assert pl["ProgramArguments"] == ["/bin/sh", str(a.launcher())]
    sh = a.launcher().read_text(encoding="utf-8")
    assert sh.startswith("#!/bin/sh\n") and "\r" not in sh
    assert "lms-agent-token" in sh and "curl" in sh and 'open "$URL"' in sh
    assert "exec " in sh and "serve --port 8765" in sh and "--no-browser" not in sh
    assert "LMS_AGENT_HOME=" in sh and "app.log" in sh
    if os.name != "nt":
        assert os.access(a.launcher(), os.X_OK)
    assert a.status() == {"enabled": True}
    a.disable()
    assert not entry.exists() and not a.launcher().exists() and a.status() == {"enabled": False}


def test_linux_xdg_autostart(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored"))   # user_home을 넘기면 실제 설정 폴더를 보지 않는다
    home, user = tmp_path / "my data $x", tmp_path / "user"
    a = autostart.Autostart(home, runner=no_runner, platform="linux", user_home=user)
    assert a.kind == "linux-autostart"
    a.enable(open_browser=False)
    entry = user / ".config" / "autostart" / "lms-agent.desktop"
    assert a.entry() == entry and entry.exists() and not (tmp_path / "ignored").exists()
    text = entry.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]\n") and "Type=Application" in text and "Terminal=false" in text
    exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))
    # 공백·$가 든 경로는 큰따옴표로 감싸고 $ 앞에 \\(문자열 값 규칙으로 한 번 더) — 명세의 예와 같은 모양
    assert exec_line.startswith('Exec=/bin/sh "') and "\\\\$x" in exec_line
    sh = a.launcher().read_text(encoding="utf-8")
    assert "xdg-open" not in sh and "--no-browser" in sh            # 이미 떠 있어도 브라우저를 열지 않음
    a.enable(open_browser=True)
    sh = a.launcher().read_text(encoding="utf-8")
    assert 'xdg-open "$URL"' in sh and "--no-browser" not in sh
    a.disable()
    assert not entry.exists()


def test_linux_respects_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    a = autostart.Autostart(tmp_path, platform="linux")          # user_home 없음 → 환경 변수만 본다(파일은 만들지 않음)
    assert a.entry() == tmp_path / "cfg" / "autostart" / "lms-agent.desktop"


def test_unsupported_platform(tmp_path):
    a = autostart.Autostart(tmp_path, runner=no_runner, platform="sunos5")
    assert a.kind is None and not autostart.supported("sunos5")
    assert a.status() == {"enabled": False}
    with pytest.raises(ValueError, match="지원하지 않아요"):
        a.enable(True)


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"), reason="POSIX 셸에서만")
def test_posix_script_only_opens_browser_when_app_is_up(tmp_path, monkeypatch):
    """실제 sh로 스크립트를 돌린다: 우리 앱이 떠 있으면 0으로 끝(켜지 않음), 없으면 명령을 exec(가짜 명령은 7로 끝남)."""
    monkeypatch.setattr(autostart, "serve_command", lambda ob=True, port=8765, platform=None: ["/bin/sh", "-c", "exit 7"])
    with _fake_app_server() as port:
        a = autostart.Autostart(tmp_path, runner=no_runner, platform="linux", user_home=tmp_path / "u", port=port)
        a.write_launcher(open_browser=False)
        assert subprocess.run(["sh", str(a.launcher())], timeout=30).returncode == 0
    a = autostart.Autostart(tmp_path, runner=no_runner, platform="linux", user_home=tmp_path / "u", port=_free_port())
    a.write_launcher(open_browser=False)
    assert subprocess.run(["sh", str(a.launcher())], timeout=30).returncode == 7


# ---------------------------------------------------------------- 화면 서버(상태 칸·요청)

def _post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture()
def served(tmp_path):
    started = []

    def make(**opts):
        app = App(Store(tmp_path / "state.json"), tmp_path, cfg={}, **opts)
        httpd = start_server(app, 0)
        serve_in_thread(httpd)
        started.append(httpd)
        return app

    yield make
    for h in started:
        h.shutdown()


def test_state_and_api_windows(tmp_path, served):
    run = FakeRunner()
    app = served(autostart=autostart.Autostart(tmp_path, runner=run, platform="win32"))
    st = app.state()["autostart"]
    assert st == {"supported": True, "enabled": False, "open_browser": True, "how": "windows-task",
                  "command": st["command"], "error": None}
    assert "serve" in st["command"] and "8765" in st["command"]
    code, st = _post(app, "/api/autostart", {"enabled": True, "open_browser": False})
    assert code == 200 and st["autostart"]["enabled"] is True and st["autostart"]["open_browser"] is False
    assert "--no-browser" in st["autostart"]["command"] and run.registered
    assert Store(tmp_path / "state.json").data["settings"]["autostart_open_browser"] is False   # 저장됨
    code, err = _post(app, "/api/autostart", {"enabled": "yes"})
    assert code == 400 and "enabled" in err["error"]
    code, err = _post(app, "/api/autostart", {"enabled": True, "open_browser": 1})
    assert code == 400 and "open_browser" in err["error"]
    code, st = _post(app, "/api/autostart", {"enabled": False})
    assert code == 200 and st["autostart"]["enabled"] is False and st["autostart"]["open_browser"] is False
    assert not run.registered


def test_api_reports_register_failure(tmp_path, served):
    app = served(autostart=autostart.Autostart(tmp_path, runner=FakeRunner(create_rc=1), platform="win32"))
    code, err = _post(app, "/api/autostart", {"enabled": True})
    assert code == 400 and "작업 스케줄러" in err["error"]
    assert app.state()["autostart"]["enabled"] is False


def test_state_and_api_macos(tmp_path, served):
    a = autostart.Autostart(tmp_path / "data", runner=no_runner, platform="darwin", user_home=tmp_path / "user")
    app = served(autostart=a)
    code, st = _post(app, "/api/autostart", {"enabled": True})
    assert code == 200 and st["autostart"]["how"] == "macos-launchagent" and st["autostart"]["enabled"] is True
    assert (tmp_path / "user" / "Library" / "LaunchAgents" / "kr.lmsagent.app.plist").exists()


def test_demo_does_not_register(tmp_path, served):
    app = served(demo=True)
    assert app.state()["autostart"] == {"supported": False, "enabled": False, "open_browser": True, "how": None,
                                        "command": None, "error": None}
    code, err = _post(app, "/api/autostart", {"enabled": True})
    assert code == 400 and "데모" in err["error"]


def test_launcher_is_refreshed_on_start(tmp_path, monkeypatch):
    """켠 적이 있으면(스크립트가 있으면) 앱을 켤 때 지금의 파이썬·exe 경로로 다시 쓴다. 등록(스케줄러·홈 폴더)은 건드리지 않는다."""
    monkeypatch.setattr(autostart, "supported", lambda platform=None: True)
    launcher = autostart.Autostart(tmp_path).launcher()
    old = r"C:\old\python.exe -m lms_agent serve"
    if launcher.suffix == ".vbs":
        launcher.write_text(old, encoding="utf-16")
    else:
        launcher.write_text(old, encoding="utf-8")
    store = Store(tmp_path / "state.json")
    store.data["settings"]["autostart_open_browser"] = False
    store.save()
    App(Store(tmp_path / "state.json"), tmp_path, cfg={})
    text = launcher.read_text(encoding="utf-16" if launcher.suffix == ".vbs" else "utf-8")
    assert old not in text and "lms_agent" in text and "--no-browser" in text
    assert str(Path(sys.executable).parent) in text


def test_launcher_not_created_when_never_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "supported", lambda platform=None: True)
    App(Store(tmp_path / "state.json"), tmp_path, cfg={})
    assert not (tmp_path / autostart.VBS_NAME).exists() and not (tmp_path / autostart.SH_NAME).exists()


# ---------------------------------------------------------------- 도우미

def _free_port() -> int:
    s = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = s.server_address[1]
    s.server_close()
    return port


class _fake_app_server:
    """GET / 에 우리 앱처럼 <meta name="lms-agent-token">을 주는 작은 서버."""

    def __enter__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b'<html><meta name="lms-agent-token" content="x"></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd.server_address[1]

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_quit_route_and_agent_action_use_the_quit_hook(tmp_path, served):
    """앱 끄기(계약 22-8): 창 없이 켜진 앱도 끌 수 있게. 테스트는 가짜 quit(진짜로 끄면 테스트 프로세스가 끝난다)."""
    from lms_agent import actions
    quit_called = threading.Event()
    app = served(quit=quit_called.set)
    code, res = _post(app, "/api/app/quit", {})
    assert code == 200 and res["ok"] and "껐어요" in res["message"]
    assert quit_called.wait(5)                                   # 응답을 보낸 뒤 잠깐 있다가 끈다
    quit_called.clear()
    r = actions.handle(app.store, "앱 꺼줘")
    (p,) = r["proposals"]
    assert (p["action"], p["client"], p["undoable"]) == ("app_quit", False, False)
    assert actions.handle(app.store, "컴퓨터 켤 때 자동 실행 꺼줘")["proposals"][0]["action"] == "autostart"   # 자동 실행 끄기와 다름
    out = actions.run(app, actions.check(app.store, p))
    assert out["ok"] and quit_called.wait(5)
