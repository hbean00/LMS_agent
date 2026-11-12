"""결정한 주체(user/ai)와 AI 이유가 근거 사슬에 남는다(계약 12장)."""
import pytest

from lms_agent.store import Store


def make(tmp_path):
    s = Store(tmp_path / "state.json")
    s.data["items"] = [{"id": "f-1", "origin": "found", "title": "LAB 6", "due": "2026-11-25T23:59:00+09:00",
                        "default_decision": "pending"}]
    return s


def test_ai_decision_is_marked_and_user_override_wins(tmp_path):
    s = make(tmp_path)
    s.update_user("f-1", {"decision": "approved"}, by="ai", reason="게시판 본문에 (due : 23:59 on 11/25)")
    v = s.view()[0]
    assert (v["status"], v["decided_by"], v["ai_reason"]) == ("scheduled", "ai", "게시판 본문에 (due : 23:59 on 11/25)")
    s.update_user("f-1", {"decision": "pending"})  # 사용자가 되돌림
    v = s.view()[0]
    assert (v["status"], v["decided_by"], v["ai_reason"]) == ("review", "user", None)


def test_unknown_actor_rejected(tmp_path):
    with pytest.raises(ValueError):
        make(tmp_path).update_user("f-1", {"decision": "approved"}, by="robot")


def test_view_keeps_original_values_after_manual_edit(tmp_path):
    """직접 고친 마감·제목은 원래 값이 view에 남는다(화면: "직접 고침 (원문 11/25)", 계약 13장)."""
    from lms_agent.store import Store
    s = Store(tmp_path / "s.json")
    s.data["items"] = [{"id": "f-1", "origin": "found", "title": "과제 2", "due": "2026-11-25T23:59:00+09:00"}]
    v = s.view()[0]
    assert v["orig_due"] is None and v["orig_title"] is None
    s.update_user("f-1", {"due": "2026-11-24T18:00", "title": "과제 2 (보고서)"})
    v = s.view()[0]
    assert v["due"].startswith("2026-11-24T18:00") and v["orig_due"] == "2026-11-25T23:59:00+09:00"
    assert v["title"] == "과제 2 (보고서)" and v["orig_title"] == "과제 2" and v["edited"]
