"""Windows 알림(토스트): 백그라운드 동기화가 신규 마감 후보를 찾았거나 보상을 받았을 때 앱을 열지 않아도 알 수 있게.

- 추가 설치 없이 PowerShell로 Windows 알림 API(ToastNotificationManager)를 부른다. 창은 뜨지 않는다.
- 잠금 화면에도 보일 수 있으므로 성적 같은 민감한 내용은 넣지 않는다(호출하는 쪽 책임, 여기서는 길이만 자른다).
- Windows가 아니거나 실패하면 조용히 False(알림은 부가 기능이다).
"""
from __future__ import annotations

import base64
import subprocess
import sys
from xml.sax.saxutils import escape

# PowerShell의 기본 앱 ID(별도 등록 없이 알림을 띄울 수 있다). 알림에는 "Windows PowerShell"로 표시될 수 있다.
APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"


def _script(title: str, body: str) -> str:
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{escape(title[:80])}</text><text>{escape(body[:200])}</text>"
           "</binding></visual></toast>")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(@'\n{xml}\n'@);"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($t);"
    )


def _run(args: list[str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, text=True, creationflags=flags, timeout=20)


def toast(title: str, body: str, runner=_run) -> bool:
    if sys.platform != "win32":
        return False
    encoded = base64.b64encode(_script(title, body).encode("utf-16-le")).decode("ascii")
    try:
        r = runner(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                    "-EncodedCommand", encoded])
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def new_deadlines_message(report: dict, view: list[dict]) -> tuple[str, str] | None:
    """동기화 결과에서 '신규 마감 후보' 알림 문구. 없으면 None. 성적·점수는 넣지 않는다."""
    new = set(report.get("new_items") or [])
    fresh = [v for v in view if v["id"] in new and v["status"] == "review"]
    if not fresh:
        return None
    names = ", ".join(f"{v.get('course_short') or v.get('course_name')} {v['title']}" for v in fresh[:2])
    more = f" 외 {len(fresh) - 2}개" if len(fresh) > 2 else ""
    return f"LMS Agent · 신규 마감 후보 {len(fresh)}개", f"{names}{more} — 확인함에서 확인하세요"


# 앱을 안 여는 사람에게도 보상이 닿게(계약 11-1): 보상만, 성적 반응(graded·score)과 출발 인사(start)는 알리지 않는다
REWARD_TOAST = ("km", "week_clear", "streak", "inbox_zero")


def reward_message(pending: list[dict], told) -> tuple[str, str, str] | None:
    """오늘 화면에 띄울 것(celebrations.pending) 중 아직 알리지 않은 보상 하나 → (제목, 본문, 보상 id). 없으면 None.
    pending은 이미 우선순위 순이다. 잠금 화면에도 보일 수 있으므로 점수·학점은 넣지 않는다(보상 이름만)."""
    for e in pending:
        if e.get("kind") not in REWARD_TOAST or not e.get("id") or e["id"] in told:
            continue
        kind = e["kind"]
        if kind == "km":
            body = "학기 마라톤 완주! 정말 수고했어요." if e.get("finish") else "결승선까지 같이 달려요."
        elif kind == "week_clear":
            body = f"이번 주 마감 {e['count']}개를 다 끝냈어요." if e.get("count") else "이번 주 마감을 다 끝냈어요."
        elif kind == "streak":
            body = "꾸준히 달리는 게 제일 빨라요."
        else:
            body = "확인할 마감 후보를 다 정리했어요."
        return f"LMS Agent · {e.get('title') or '보상'}", body + " 앱을 열면 메달을 걸어 드려요.", e["id"]
    return None
