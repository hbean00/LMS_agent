"""백그라운드 동기화: Windows 작업 스케줄러에 '로그인할 때 + N시간마다' 실행 작업을 등록한다.

앱을 열지 않아도 구글 캘린더가 최신으로 유지되게 하는 것이 목적이다(계약 8장).
- 실행 명령: `lms-agent sync --push --background` (바뀐 곳만 읽기 → 구글 반영).
- 콘솔 창이 깜빡이지 않도록 데이터 폴더의 숨김 실행 스크립트(.vbs)를 wscript로 돌린다.
- 등록·조회·삭제는 schtasks.exe. 테스트는 runner를 바꿔 실제 PC 설정을 건드리지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "LMS Agent Sync"
INTERVALS = (1, 3, 6, 12, 24)


def supported() -> bool:
    return sys.platform == "win32"


def sync_command() -> list[str]:
    """백그라운드에서 실행할 명령(인자 목록). exe면 exe, 아니면 이 파이썬으로 모듈 실행."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "sync", "--push", "--background"]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), "-m", "lms_agent", "sync", "--push", "--background"]


def _src_dir() -> str | None:
    """설치 없이 소스 폴더에서 돌릴 때 PYTHONPATH로 넘길 경로(설치된 경우 None)."""
    if getattr(sys, "frozen", False):
        return None
    here = Path(__file__).resolve().parent.parent  # .../src
    return str(here) if (here / "lms_agent" / "__init__.py").exists() and "site-packages" not in str(here) else None


def write_launcher(home: Path) -> Path:
    """창 없이 실행하는 .vbs를 만든다. 출력은 background.log에 덧붙인다."""
    home.mkdir(parents=True, exist_ok=True)
    log = home / "background.log"
    # 명령줄: cmd /c ""exe" -m lms_agent sync … >> "log" 2>&1"  →  VBScript 문자열로 감싼다(" → "")
    line = 'cmd /c "' + " ".join(f'"{a}"' for a in sync_command()) + f' >> "{log}" 2>&1"'

    def vbs(s: str) -> str:
        return '"' + str(s).replace('"', '""') + '"'

    lines = ['Set sh = CreateObject("WScript.Shell")']
    src = _src_dir()
    if src:
        lines.append(f'sh.Environment("Process")("PYTHONPATH") = {vbs(src)}')
    lines.append(f'sh.Environment("Process")("LMS_AGENT_HOME") = {vbs(home)}')
    lines.append(f"sh.Run {vbs(line)}, 0, False")
    path = home / "background-sync.vbs"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16")  # wscript는 UTF-16(BOM)을 읽는다
    return path


def task_xml(launcher: Path, interval_hours: int, now: datetime | None = None) -> str:
    start = (now or datetime.now()).replace(microsecond=0, second=0).isoformat()
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    action = f'"{launcher}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>LMS Agent: Canvas 마감을 확인하고 구글 캘린더에 반영합니다.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT2M</Delay></LogonTrigger>
    <TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
      <Repetition><Interval>PT{interval_hours}H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>wscript.exe</Command><Arguments>{escape(action)}</Arguments></Exec></Actions>
</Task>
"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, text=True, creationflags=flags, timeout=30)


class Scheduler:
    """schtasks 래퍼. runner를 바꾸면(테스트) 실제 작업 스케줄러를 건드리지 않는다."""

    def __init__(self, home: Path, runner=_run):
        self.home = Path(home)
        self.run = runner

    def status(self) -> dict:
        if not supported():
            return {"enabled": False, "interval_hours": None}
        r = self.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
        if r.returncode != 0:
            return {"enabled": False, "interval_hours": None}
        hours = None
        out = r.stdout or ""
        i = out.find("<Interval>PT")
        if i >= 0:
            try:
                hours = int(out[i + len("<Interval>PT"):out.index("H</Interval>", i)])
            except ValueError:
                hours = None
        return {"enabled": True, "interval_hours": hours}

    def enable(self, interval_hours: int) -> None:
        if interval_hours not in INTERVALS:
            raise ValueError(f"간격은 {', '.join(map(str, INTERVALS))}시간 중 하나여야 합니다")
        if not supported():
            raise ValueError("백그라운드 동기화는 Windows에서만 지원합니다")
        launcher = write_launcher(self.home)
        xml = task_xml(launcher, interval_hours)
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        os.close(fd)
        try:
            Path(tmp).write_text(xml, encoding="utf-16")
            r = self.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp, "/F"])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if r.returncode != 0:
            raise ValueError("작업 스케줄러에 등록하지 못했습니다: " + ((r.stderr or r.stdout or "").strip()[:200]))

    def disable(self) -> None:
        if not supported():
            return
        r = self.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if r.returncode != 0 and self.status()["enabled"]:
            raise ValueError("작업 스케줄러에서 지우지 못했습니다: " + ((r.stderr or r.stdout or "").strip()[:200]))

    def command_text(self) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in sync_command())
