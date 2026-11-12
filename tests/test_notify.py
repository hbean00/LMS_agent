"""백그라운드 알림(Windows 토스트): 문구에 성적이 없고, 명령은 창 없이 인코딩된 스크립트로 실행된다."""
import base64
import subprocess
import sys

import pytest

from lms_agent import notify


def view(status="review", **kw):
    return {"id": kw.get("id", "f-1"), "status": status, "title": kw.get("title", "LAB 6"),
            "course_short": "ASIC설계(영강)", "course_name": "x"}


def test_message_only_for_new_review_items():
    assert notify.new_deadlines_message({"new_items": []}, [view()]) is None
    assert notify.new_deadlines_message({"new_items": ["f-1"]}, [view("scheduled")]) is None
    title, body = notify.new_deadlines_message({"new_items": ["f-1", "f-2", "f-3"]},
                                               [view(id="f-1"), view(id="f-2", title="과제 2"), view(id="f-3")])
    assert title == "LMS Agent · 신규 마감 후보 3개"
    assert "ASIC설계(영강) LAB 6" in body and "외 1개" in body and "확인함" in body


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 알림")
def test_toast_runs_hidden_encoded_powershell():
    calls = []

    def runner(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    assert notify.toast("제목 <&>", "본문 '따옴표'", runner=runner)
    args = calls[0]
    assert args[0] == "powershell" and "-EncodedCommand" in args and "Hidden" in args
    script = base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "제목 &lt;&amp;&gt;" in script and "ToastNotificationManager" in script   # XML 이스케이프


def test_toast_failure_is_quiet():
    def boom(args):
        raise OSError("no powershell")
    assert notify.toast("a", "b", runner=boom) is False
