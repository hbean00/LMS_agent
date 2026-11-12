"""동기화 뒤 자동 단계(평가 비중·AI 확인함 정리)와 저장 합치기(계약 12장).

- settings.ai_cli + ai_auto_triage가 켜져 있으면 동기화가 끝난 뒤 AI가 확인함을 정리한다(검증된 것만, decided_by "ai").
- AI가 도는 사이 다른 프로세스(앱 화면)에서 사용자가 결정하면, 사용자 결정이 이긴다.
"""
import pytest

from lms_agent import core, mock_ai
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.store import Store


@pytest.fixture()
def canvas():
    srv, base = start_mock_canvas()
    yield Canvas(base, DEMO_TOKEN)
    srv.shutdown()


def _review(store):
    return [v for v in store.view() if v["status"] == "review" and v["decision"] == "pending"]


def test_auto_triage_runs_after_sync_when_enabled(tmp_path, canvas):
    store = Store(tmp_path / "state.json")
    core.sync(canvas, store)                                   # 첫 동기화: AI 꺼짐
    assert not any(v.get("decided_by") == "ai" for v in store.view())
    before = len(_review(store))
    assert before > 0

    store.data["settings"].update(ai_cli=True, ai_auto_triage=True)
    store.save()
    r = core.sync(canvas, store, ai_enabled=True, ai_runner=mock_ai.runner, ai_command="claude")
    assert r["ai_triage"]["ok"] and r["ai_triage"]["applied"] > 0
    disk = Store(tmp_path / "state.json")                     # 파일에 저장됐다
    by_ai = [v for v in disk.view() if v.get("decided_by") == "ai"]
    assert len(by_ai) == r["ai_triage"]["applied"]
    assert all(v["ai_reason"] and "근거" in v["ai_reason"] for v in by_ai)
    assert disk.data["ai_triage"]["applied"] == r["ai_triage"]["applied"]
    assert disk.data["ai_runs"][-1]["task"] == "triage_review"
    # 같은 과제(LAB4) 후보 둘 중 하나만 승인
    lab4 = [v for v in disk.view() if v["origin"] == "found" and "LAB4" in v["title"] and v["decision"] == "approved"]
    assert len(lab4) <= 1
    # 이유는 정직하게(계약 13-3): 자리 표시 없음, 원문 인용, 상대 날짜는 계산을 보여 주고 '분명히 적혀'라고 하지 않는다
    assert all("[이메일]" not in v["ai_reason"] and "[링크]" not in v["ai_reason"] for v in by_ai)
    proj = next(v for v in by_ai if v["title"] == "중간 프로젝트 보고서")
    assert "‘다음 주 금요일’ → 공지일" in proj["ai_reason"] and " 기준 " in proj["ai_reason"]
    assert "분명" not in proj["ai_reason"] and "dsp.prof@example.ac.kr" in proj["ai_reason"]   # 이 PC에서는 원문 그대로
    assert disk.data["ai_triage"]["pending_left"] == sum(1 for v in disk.view() if v["status"] == "review")
    assert disk.data["ai_triage"]["user_reverted"] == 0 and r["ai_triage"]["pending_left"] >= 0
    # 캘린더에는 LAB4가 하나만
    assert sum(1 for v in disk.calendar_items() if v.get("linked_to") == "canvas-101-1012") == 1


def test_auto_triage_off_unless_both_settings(tmp_path, canvas):
    store = Store(tmp_path / "state.json")
    store.data["settings"].update(ai_cli=True, ai_auto_triage=False)
    calls = []

    def spy(*a, **kw):
        calls.append(a)
        return mock_ai.runner(*a, **kw)

    r = core.sync(canvas, store, ai_enabled=True, ai_runner=spy, ai_command="claude")
    assert "ai_triage" not in r
    assert not any(v.get("decided_by") == "ai" for v in store.view())


def test_user_decision_during_ai_wins(tmp_path, canvas):
    """백그라운드 동기화(store A)의 AI가 도는 사이 앱(store B)에서 사용자가 같은 항목을 결정 → 사용자 결정이 남는다."""
    path = tmp_path / "state.json"
    a = Store(path)
    core.sync(canvas, a)
    a.data["settings"].update(ai_cli=True, ai_auto_triage=True)
    a.save()
    targets = [v["id"] for v in _review(a)]
    assert len(targets) >= 2
    user_pick = targets[0]

    def runner(args, input, timeout, cwd):
        if "### id: " in (input or ""):
            b = Store(path)                                    # 다른 프로세스의 앱 화면
            b.update_user(user_pick, {"decision": "rejected"})
            b.save()
        return mock_ai.runner(args, input, timeout, cwd)

    r = core.sync(canvas, a, ai_enabled=True, ai_runner=runner, ai_command="claude")
    disk = {v["id"]: v for v in Store(path).view()}
    assert disk[user_pick]["decision"] == "rejected" and disk[user_pick]["decided_by"] == "user"
    others = [disk[i] for i in targets[1:] if disk[i].get("decided_by") == "ai"]
    assert others, "사용자가 손대지 않은 다른 후보에는 AI 결정이 반영된다"
    assert r["ai_skipped_user_changed"] == 1               # AI가 같은 항목을 결정했지만 사용자 쪽이 이겼다
