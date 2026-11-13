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


def test_reward_message_only_rewards_never_grades():
    """앱을 안 여는 사람에게 닿는 보상 알림(계약 11-1): 보상 이름만, 성적 반응·출발 인사는 넣지 않고 한 번만."""
    pending = [
        {"id": "g:1", "kind": "graded", "title": "중간고사", "band": "A", "score": 91},     # 성적 → 잠금 화면에 안 보이게 뺀다
        {"id": "start:t", "kind": "start", "title": "27km 지점에서 합류했어요"},             # 합류 인사 — 보상 아님
        {"id": "week_clear:t:2026-11-09", "kind": "week_clear", "title": "11주차 완주", "count": 4},
        {"id": "km:t:30", "kind": "km", "title": "30km, 가장 힘든 구간이에요", "km": 30.0},
    ]
    title, body, rid = notify.reward_message(pending, set())
    assert title == "LMS Agent · 11주차 완주" and rid == "week_clear:t:2026-11-09"
    assert "4개" in body and "메달" in body and "91" not in body + title and "중간고사" not in body + title
    title, body, rid = notify.reward_message(pending, {"week_clear:t:2026-11-09"})   # 알린 것은 다시 안 알림
    assert rid == "km:t:30" and "결승선" in body
    assert notify.reward_message(pending, {"week_clear:t:2026-11-09", "km:t:30"}) is None
    assert notify.reward_message(pending[:2], set()) is None
    _, body, _ = notify.reward_message([{"id": "km:t:42.195", "kind": "km", "title": "결승선", "finish": True}], set())
    assert "완주" in body
