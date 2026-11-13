"""컴퓨터를 켜면(로그인하면) 앱 열기 — 계약 22-8.

- Windows: 작업 스케줄러 로그온 작업 "LMS Agent App" → 데이터 폴더의 숨김 실행 스크립트(app-start.vbs, wscript).
- macOS: ~/Library/LaunchAgents/kr.lmsagent.app.plist(RunAtLoad) → 데이터 폴더의 app-start.sh.
- Linux: ~/.config/autostart/lms-agent.desktop(XDG 자동 시작, $XDG_CONFIG_HOME이 있으면 그 아래) → app-start.sh.

실행 스크립트는 먼저 http://127.0.0.1:8765/ 에 우리 앱(화면에 `lms-agent-token`이 있음)이 떠 있는지 본다.
떠 있으면 한 번 더 켜지 않고 브라우저만 연다(open_browser일 때). 아니면 `lms-agent serve --port 8765`
(open_browser가 아니면 `--no-browser`)을 지금의 파이썬·exe 경로로 켠다 — 백그라운드 동기화(schedule.py)와 같은 방식.
경로가 바뀌어도 멈추지 않게 앱을 켤 때마다 스크립트를 다시 쓴다(features/autostart.py).

등록·해제는 사용자가 설정에서 켜고 끌 때(또는 사용자가 [실행]을 누른 에이전트 동작)만 일어난다.
테스트는 runner(schtasks 대신)·platform·user_home을 바꿔 실제 작업 스케줄러·홈 폴더를 건드리지 않는다.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .schedule import _src_dir

TASK_NAME = "LMS Agent App"
LABEL = "kr.lmsagent.app"
PORT = 8765
DESKTOP_NAME = "lms-agent.desktop"
VBS_NAME = "app-start.vbs"
SH_NAME = "app-start.sh"
LOG_NAME = "app.log"
MARK = "lms-agent-token"     # 화면 HTML의 <meta name="lms-agent-token"> — 같은 포트에 '우리 앱'이 떠 있는지 가린다
KINDS = {"win32": "windows-task", "darwin": "macos-launchagent", "linux": "linux-autostart"}


def kind_of(platform: str | None = None) -> str | None:
    p = platform or sys.platform
    if p.startswith("linux"):
        p = "linux"
    return KINDS.get(p)


def supported(platform: str | None = None) -> bool:
    return kind_of(platform) is not None


def serve_command(open_browser: bool = True, port: int = PORT, platform: str | None = None) -> list[str]:
    """켤 명령(인자 목록). exe면 exe, 아니면 이 파이썬으로 모듈 실행(Windows는 창 없는 pythonw.exe가 있으면 그것)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "serve", "--port", str(port)]
    else:
        exe = Path(sys.executable)
        if (platform or sys.platform) == "win32":
            pyw = exe.with_name("pythonw.exe")
            exe = pyw if pyw.exists() else exe
        cmd = [str(exe), "-m", "lms_agent", "serve", "--port", str(port)]
    return cmd + ([] if open_browser else ["--no-browser"])


def _url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


# ---------------------------------------------------------------- Windows: 작업 스케줄러 + .vbs

def _vbs(s) -> str:
    return '"' + str(s).replace('"', '""') + '"'


def vbs_text(home: Path, open_browser: bool, port: int = PORT) -> str:
    """창 없이 켜는 VBScript. 먼저 같은 포트의 우리 앱을 확인(WinHttp, 3초 안팎) → 있으면 브라우저만, 없으면 serve."""
    url = _url(port)
    log = home / LOG_NAME
    cmd = serve_command(open_browser, port, "win32")
    line = 'cmd /c "' + " ".join(f'"{a}"' for a in cmd) + f' >> "{log}" 2>&1"'
    lines = [
        "' LMS Agent: 컴퓨터를 켜면 앱 열기(설정에서 끄면 이 파일과 작업이 지워집니다)",
        'Set sh = CreateObject("WScript.Shell")',
        f"url = {_vbs(url)}",
        'st = 0',
        'body = ""',
        'On Error Resume Next',
        'Set http = CreateObject("WinHttp.WinHttpRequest.5.1")',
        'http.SetTimeouts 2000, 2000, 2000, 3000',
        'http.Open "GET", url, False',
        'http.Send',
        # If 조건에서 오류가 나면 Then 쪽으로 넘어가는 VBScript 특성 때문에 조건에는 오류가 날 수 없는 것만 둔다
        'If Err.Number = 0 Then st = http.Status',
        'If Err.Number = 0 Then body = http.ResponseText',
        'ok = (Err.Number = 0)',
        'Err.Clear',
        'On Error GoTo 0',
        f'If ok And st = 200 And InStr(body, {_vbs(MARK)}) > 0 Then',
    ]
    lines.append("  sh.Run url, 1, False" if open_browser else "  ' 브라우저는 열지 않음")
    lines += ["  WScript.Quit 0", "End If"]
    src = _src_dir()
    if src:
        lines.append(f'sh.Environment("Process")("PYTHONPATH") = {_vbs(src)}')
    lines.append(f'sh.Environment("Process")("LMS_AGENT_HOME") = {_vbs(home)}')
    lines.append(f"sh.Run {_vbs(line)}, 0, False")
    return "\n".join(lines) + "\n"     # 쓸 때 \r\n으로(write_launcher)


def task_xml(launcher: Path) -> str:
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    action = f'"{launcher}"'
    # 실행 시간 제한 없음(PT0S): wscript는 바로 끝나지만, 제한이 걸리면 작업 스케줄러가 켜 둔 앱까지 끌 수 있다.
    # Priority 5 = 보통(기본 7은 '낮음'이라 켜 둔 앱이 느려진다)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>LMS Agent: 로그인하면 앱(화면)을 켭니다.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT10S</Delay></LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>wscript.exe</Command><Arguments>{escape(action)}</Arguments></Exec></Actions>
</Task>
"""


# ---------------------------------------------------------------- macOS·Linux: 셸 스크립트

def sh_text(home: Path, open_browser: bool, platform: str, port: int = PORT) -> str:
    q = shlex.quote
    url = _url(port)
    opener = "open" if platform == "darwin" else "xdg-open"
    cmd = serve_command(open_browser, port, platform)
    probe = [
        "running() {",
        "  if command -v curl >/dev/null 2>&1; then",
        f"    curl -s --noproxy '*' -m 3 \"$URL\" 2>/dev/null | grep -q {q(MARK)}",
        "  elif command -v wget >/dev/null 2>&1; then",
        f"    wget -q --no-proxy -T 3 -O - \"$URL\" 2>/dev/null | grep -q {q(MARK)}",
    ]
    if not getattr(sys, "frozen", False):   # curl·wget이 없으면 이 파이썬으로 확인
        py = ("import sys,urllib.request as u;"
              "o=u.build_opener(u.ProxyHandler({}));"
              f"sys.exit(0 if b{MARK!r} in o.open(sys.argv[1],timeout=3).read() else 1)")
        probe += ["  else", f"    {q(cmd[0])} -c {q(py)} \"$URL\" >/dev/null 2>&1"]
    else:
        probe += ["  else", "    return 1"]
    probe += ["  fi", "}"]
    lines = ["#!/bin/sh",
             "# LMS Agent: 컴퓨터를 켜면 앱 열기(설정에서 끄면 이 파일과 자동 시작 항목이 지워집니다)",
             f"URL={q(url)}"] + probe + [
             "if running; then"]
    lines.append(f"  {opener} \"$URL\" >/dev/null 2>&1 &" if open_browser else "  : # 브라우저는 열지 않음")
    lines += ["  exit 0", "fi"]
    src = _src_dir()
    if src:
        lines.append(f"PYTHONPATH={q(src)}; export PYTHONPATH")
    lines.append(f"LMS_AGENT_HOME={q(str(home))}; export LMS_AGENT_HOME")
    lines.append("exec " + " ".join(q(a) for a in cmd) + f" >> {q(str(home / LOG_NAME))} 2>&1")
    return "\n".join(lines) + "\n"


def plist_bytes(launcher: Path, home: Path) -> bytes:
    log = str(home / LOG_NAME)
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/bin/sh", str(launcher)],
                           "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Interactive",
                           "StandardOutPath": log, "StandardErrorPath": log})


_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def _exec_arg(a: str) -> str:
    """데스크톱 항목 Exec의 인자 하나(명세: 예약 문자가 있으면 큰따옴표, 안에서 " ` $ \\ 앞에 \\)."""
    if a and not any(ch in _RESERVED for ch in a):
        return a
    return '"' + "".join("\\" + ch if ch in '"`$\\' else ch for ch in a) + '"'


def desktop_text(launcher: Path) -> str:
    exec_line = " ".join(_exec_arg(a) for a in ["/bin/sh", str(launcher)])
    exec_line = exec_line.replace("\\", "\\\\").replace("%", "%%")   # 문자열 값 규칙(\\)은 인용 규칙보다 먼저 풀린다
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=LMS Agent",
        "Comment=로그인하면 LMS Agent 화면을 켭니다",
        f"Exec={exec_line}",
        "Terminal=false",
        "Hidden=false",
        "X-GNOME-Autostart-enabled=true",
    ]) + "\n"


# ---------------------------------------------------------------- 등록·조회·해제

def _run(args: list[str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, text=True, creationflags=flags, timeout=30)


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:   # Python 3.9: write_text에는 newline이 없다
        f.write(text)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class Autostart:
    """OS별 등록기. runner(schtasks)·platform·user_home을 바꾸면(테스트) 실제 PC 설정을 건드리지 않는다.
    home = 앱 데이터 폴더(실행 스크립트·로그를 둔다)."""

    def __init__(self, home: Path, runner=_run, platform: str | None = None, user_home: Path | None = None,
                 port: int = PORT):
        self.home = Path(home)
        self.run = runner
        self.platform = platform or sys.platform
        if self.platform.startswith("linux"):
            self.platform = "linux"
        self.kind = kind_of(self.platform)
        self._user_home = Path(user_home) if user_home is not None else None
        self.port = port

    # 경로 ----------------------------------------------------------
    def user_home(self) -> Path:
        return self._user_home if self._user_home is not None else Path.home()

    def launcher(self) -> Path:
        return self.home / (VBS_NAME if self.platform == "win32" else SH_NAME)

    def entry(self) -> Path | None:
        """OS에 등록되는 파일(macOS plist·Linux .desktop). Windows는 작업 스케줄러라 None."""
        if self.platform == "darwin":
            return self.user_home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if self.platform == "linux":
            base = None
            xdg = os.environ.get("XDG_CONFIG_HOME")
            if self._user_home is None and xdg and Path(xdg).is_absolute():
                base = Path(xdg)
            return (base or self.user_home() / ".config") / "autostart" / DESKTOP_NAME
        return None

    # 내용 ----------------------------------------------------------
    def write_launcher(self, open_browser: bool) -> Path:
        path = self.launcher()
        if self.platform == "win32":
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-16", newline="\r\n") as f:   # wscript는 UTF-16(BOM), 줄 끝 CRLF
                f.write(vbs_text(self.home, open_browser, self.port))
        else:
            _write(path, sh_text(self.home, open_browser, self.platform, self.port), 0o755)
        return path

    def command(self, open_browser: bool) -> list[str]:
        return serve_command(open_browser, self.port, self.platform)

    def command_text(self, open_browser: bool) -> str:
        cmd = self.command(open_browser)
        if self.platform == "win32":
            return " ".join(f'"{a}"' if " " in a else a for a in cmd)
        return " ".join(shlex.quote(a) for a in cmd)

    # 상태 ----------------------------------------------------------
    def status(self) -> dict:
        """{"enabled": bool}. 이 데이터 폴더의 실행 스크립트가 있고 OS에 등록돼 있을 때만 켜짐
        (스크립트가 없으면 작업 스케줄러를 묻지도 않는다 — 화면을 물을 때마다 프로그램을 띄우지 않게)."""
        if self.kind is None or not self.launcher().exists():
            return {"enabled": False}
        if self.platform == "win32":
            r = self.run(["schtasks", "/Query", "/TN", TASK_NAME])
            return {"enabled": r.returncode == 0}
        return {"enabled": self.entry().exists()}

    def enable(self, open_browser: bool = True) -> None:
        if self.kind is None:
            raise ValueError("이 운영체제에서는 '컴퓨터를 켜면 앱 열기'를 지원하지 않아요")
        had = self.launcher().exists()
        launcher = self.write_launcher(open_browser)
        try:
            if self.platform == "win32":
                self._register_task(launcher)
            elif self.platform == "darwin":
                # launchctl load는 하지 않는다: RunAtLoad라 지금 바로 한 번 더 켜진다. 다음 로그인부터 launchd가 읽는다
                entry = self.entry()
                entry.parent.mkdir(parents=True, exist_ok=True)
                tmp = entry.with_name(entry.name + ".tmp")
                tmp.write_bytes(plist_bytes(launcher, self.home))
                os.replace(tmp, entry)
            else:
                _write(self.entry(), desktop_text(launcher), 0o644)
        except Exception:
            if not had:
                _unlink(launcher)
            raise

    def _register_task(self, launcher: Path) -> None:
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        os.close(fd)
        try:
            Path(tmp).write_text(task_xml(launcher), encoding="utf-16")
            r = self.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp, "/F"])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if r.returncode != 0:
            raise ValueError("작업 스케줄러에 등록하지 못했어요: " + ((r.stderr or r.stdout or "").strip()[:200]))

    def disable(self) -> None:
        if self.kind is None:
            return
        if self.platform == "win32":
            r = self.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
            if r.returncode != 0 and self.run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0:
                raise ValueError("작업 스케줄러에서 지우지 못했어요: " + ((r.stderr or r.stdout or "").strip()[:200]))
        else:
            # launchctl remove는 하지 않는다: 로그인 때 launchd가 켠 앱이 지금 이 요청을 받고 있을 수 있다(끄면 앱이 꺼짐)
            _unlink(self.entry())
        _unlink(self.launcher())

    def refresh(self, open_browser: bool) -> bool:
        """켠 적이 있으면(스크립트가 있으면) 지금의 파이썬·exe 경로로 스크립트를 다시 쓴다. 다시 썼으면 True."""
        if self.kind is None or not self.launcher().exists():
            return False
        self.write_launcher(open_browser)
        return True
