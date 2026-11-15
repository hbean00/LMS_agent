"""하루 AI 사용 한도(계약 26-0, 설정 → AI): settings.ai_daily_limit 30·100·300·0(제한 없음), 기본 100.
POST /api/settings의 확인(그 밖의 값은 400, 아무것도 저장하지 않음), state에 보이는 값(settings·ai·agent의 limits.per_day —
제한 없음은 None), 한도가 AI 호출(AiSession)에 걸리는지, 한도만 바꾸면 'changed'(구글 반영)를 부르지 않는지.
실제 CLI·네트워크는 쓰지 않는다(가짜 실행기 이름만)."""
import pytest

from lms_agent import agent_cli
from lms_agent.api import ApiError, StateContext, router
from lms_agent.features import agent as agent_feature
from lms_agent.features import ai as ai_feature
from lms_agent.features import core as core_feature
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import iso, now_local


@pytest.fixture()
def app(tmp_path):
    s = Store(tmp_path / "state.json")
    a = App(s, tmp_path, cfg={}, ai_command="claude")

    def state():   # 이 기능이 보는 칸만(다른 기능의 상태 계산과 떼어 둔다)
        ctx = StateContext(a)
        return {"settings": core_feature.base_state(a, ctx)["settings"], "ai": ai_feature.ai_state(a, ctx),
                "agent": agent_feature.agent_state(a, ctx)}
    a.state = state
    a.events = []
    a.emit = lambda event, **kw: a.events.append(event)
    return a


def post(app, path, body):
    for r, groups in router.match("POST", path):
        out = r.handler(app, dict(body), **groups)
        if out is not None:
            return out
    raise AssertionError("경로 없음: " + path)


def runs(store, n):
    today = iso(now_local())
    store.data["ai_runs"] = [{"at": today, "task": "assist", "ok": True, "attempts": 1, "duration_ms": 10,
                              "input_chars": 0, "cli": "claude", "model": None, "error": None} for _ in range(n)]


def test_default_is_100_everywhere(app):
    st = app.state()
    assert st["settings"]["ai_daily_limit"] == 100 == agent_cli.MAX_CALLS_PER_DAY
    assert st["ai"]["limits"]["per_day"] == 100 and st["agent"]["limits"]["per_day"] == 100
    d = st["ai"]["daily_limit"]
    assert d == {"value": 100, "choices": [30, 100, 300, 0], "default": 100, "left": 100, "reached": False}
    assert "ai_daily_limit" not in app.store.data["settings"]   # 기본값은 저장하지 않는다


@pytest.mark.parametrize("n, per_day", [(30, 30), (100, 100), (300, 300), (0, None)])
def test_choices_are_saved_and_shown(app, n, per_day):
    st = post(app, "/api/settings", {"ai_daily_limit": n})
    assert app.store.data["settings"]["ai_daily_limit"] == n
    assert st["settings"]["ai_daily_limit"] == n
    assert st["ai"]["limits"]["per_day"] == per_day and st["agent"]["limits"]["per_day"] == per_day
    assert st["ai"]["daily_limit"]["value"] == n
    assert st["ai"]["daily_limit"]["left"] == per_day   # 오늘 0번 썼음 → 남은 수 = 한도(제한 없음이면 None)
    assert Store(app.store.path).data["settings"]["ai_daily_limit"] == n   # 파일에 남는다


@pytest.mark.parametrize("bad", [50, 1, -1, 1000, "100", 100.0, True, False, None, [100], {"n": 100}])
def test_other_values_are_400_and_nothing_changes(app, bad):
    post(app, "/api/settings", {"ai_daily_limit": 300})
    with pytest.raises(ApiError) as e:
        post(app, "/api/settings", {"ai_daily_limit": bad, "include_canvas": False})
    assert e.value.status == 400 and "하루 AI 사용 한도" in str(e.value) and "제한 없음" in str(e.value)
    st = app.store.data["settings"]
    assert st["ai_daily_limit"] == 300 and st.get("include_canvas", True) is True   # 함께 보낸 다른 칸도 그대로


def test_broken_saved_value_reads_as_default(app):
    for junk in (55, "300", True, None, 2.5):
        app.store.data["settings"]["ai_daily_limit"] = junk
        st = app.state()
        assert st["settings"]["ai_daily_limit"] == 100 and st["ai"]["limits"]["per_day"] == 100


def test_limit_only_does_not_emit_changed(app):
    post(app, "/api/settings", {"ai_daily_limit": 30})
    assert app.events == []                                   # 일정은 그대로 → 구글 반영 안 함
    post(app, "/api/settings", {"ai_daily_limit": 100, "weekly_summary": False})
    assert app.events == ["changed"]                          # 다른 설정이 같이 바뀌면 지금처럼


def test_session_uses_the_setting(app):
    s = app.store
    s.data["settings"]["ai_cli"] = True
    runs(s, 30)
    post(app, "/api/settings", {"ai_daily_limit": 30})
    why = agent_cli.AiSession(s, runner=lambda *a, **k: None).blocked()
    assert why and "30회" in why and "설정" in why
    st = app.state()
    assert st["ai"]["calls_today"] == 30 and st["ai"]["daily_limit"]["left"] == 0 and st["ai"]["daily_limit"]["reached"]
    post(app, "/api/settings", {"ai_daily_limit": 100})
    assert agent_cli.AiSession(s, runner=lambda *a, **k: None).blocked() is None
    runs(s, 500)
    post(app, "/api/settings", {"ai_daily_limit": 0})          # 제한 없음: 오늘 500번 써도 막지 않는다
    assert agent_cli.AiSession(s, runner=lambda *a, **k: None).blocked() is None
    st = app.state()
    assert st["ai"]["limits"]["per_day"] is None and st["ai"]["daily_limit"]["left"] is None
    assert not st["ai"]["daily_limit"]["reached"]


def test_page_has_the_selector_and_renders_unlimited():
    from importlib import resources
    html = resources.files("lms_agent").joinpath("web/index.html").read_text("utf-8")
    assert "'ai-limit'" in html and "하루 AI 사용 한도" in html and "ai_daily_limit" in html
    assert "AI_LIMITS = [30, 100, 300, 0]" in html and "제한 없음" in html
