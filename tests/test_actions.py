"""에이전트가 하는 클릭(계약 22-9): 목록 모양, 말로 한 명령 알아듣기(아닌 것은 None), 검사(금지·모름·이미 그 상태),
실행·되돌리기(화면과 같은 경로를 앱 안에서 — 설정·과목 종류·항목 결정·백그라운드(가짜 스케줄러)·자동 실행(가짜 실행기)…).
다른 담당이 만드는 경로(코치·공부 계획·체크리스트)는 가짜 경로를 앞에 끼워 시험한다."""
import json
import re
import subprocess

import pytest

from lms_agent import actions, autostart, core, schedule
from lms_agent.api import ApiError, Route, router
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App
from lms_agent.store import Store, course_mode
from lms_agent.timeutil import parse_iso

# 데모 항목 id는 마감 시각(그 PC의 시간대로 적힘)에서 나와 CI(UTC)와 한국 시간에서 다르다 → 자리표시를 데모 저장소에서 찾아 바꾼다
LAB4_A, LAB4_B = "<LAB4_A>", "<LAB4_B>"     # 데모의 LAB4 보고서 후보 둘(11/19·11/21, 같은 과제 묶음)
FINAL_103 = "<FINAL_103>"                   # 반도체공학 기말고사


def demo_ids(store) -> dict:
    v = store.view()
    lab4 = sorted((x for x in v if x["id"].startswith("f-") and x.get("title") == "LAB4 보고서"),
                  key=lambda x: parse_iso(x["due"]))
    final = next(x for x in v if x["id"].startswith("f-") and str(x.get("course_id")) == "103"
                 and x.get("kind") == "시험" and "기말" in (x.get("title") or ""))
    assert len(lab4) == 2
    return {LAB4_A: lab4[0]["id"], LAB4_B: lab4[1]["id"], FINAL_103: final["id"]}


def real(x, store, ids=None):
    """자리표시(LAB4_A …)를 이 실행의 실제 항목 id로."""
    ids = ids or demo_ids(store)
    if isinstance(x, dict):
        return {k: real(v, store, ids) for k, v in x.items()}
    if isinstance(x, list):
        return [real(v, store, ids) for v in x]
    return ids.get(x, x) if isinstance(x, str) else x
WEEKLY_LOG = "canvas-104-1041"                          # Capstone Weekly log 9(마감 없음)


class SchedRunner:
    """가짜 schtasks(백그라운드 동기화)."""

    def __init__(self):
        self.xml, self.registered = None, False

    def __call__(self, args):
        if args[1] == "/Create":
            self.xml = open(args[args.index("/XML") + 1], encoding="utf-16").read()
            self.registered = True
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        if args[1] == "/Query":
            return subprocess.CompletedProcess(args, 0 if self.registered else 1, self.xml or "", "")
        if args[1] == "/Delete":
            self.registered = False
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


class AutoRunner(SchedRunner):
    """가짜 schtasks(자동 실행 작업)."""

    def __call__(self, args):
        assert args[args.index("/TN") + 1] == autostart.TASK_NAME
        return super().__call__(args)


def fake_release(url, max_bytes):
    return json.dumps({"tag_name": "v99.0.0", "html_url": "https://github.com/x/y/releases/tag/v99.0.0", "body": "- 고침",
                       "published_at": "2026-11-20T01:00:00Z", "assets": []}).encode()


@pytest.fixture()
def mock():
    srv, base = start_mock_canvas()
    yield base
    srv.shutdown()


@pytest.fixture()
def store(tmp_path, mock):
    st = Store(tmp_path / "state.json")
    core.sync(Canvas(mock, DEMO_TOKEN), st)
    return st


@pytest.fixture()
def app(tmp_path, store, mock, monkeypatch):
    monkeypatch.setattr(schedule, "supported", lambda: True)          # 가짜 스케줄러로 Windows처럼
    a = App(store, tmp_path, cfg={"canvas_url": mock, "token": DEMO_TOKEN},
            scheduler=schedule.Scheduler(tmp_path, runner=SchedRunner()),
            autostart=autostart.Autostart(tmp_path, runner=AutoRunner(), platform="win32"),
            update_fetch=fake_release, update_how="unknown", relaunch=lambda argv: pytest.fail("다시 켜면 안 됨"),
            opener=lambda path: None)
    return a


def ok(store, action, **params):
    p = actions.check(store, {"type": "action", "action": action, "params": params})
    assert p["rejected_reason"] is None, p["rejected_reason"]
    return p


def run_and_keep(app, p):
    r = actions.run(app, p)
    assert r["ok"] is True and r["message"]
    return {**p, "applied": True, "result": {"ok": r["ok"], "message": r["message"]}, "_undo": r["undo"]}


# ---------------------------------------------------------------- 목록

def test_catalog_shape_and_text():
    ids = [a["id"] for a in actions.CATALOG]
    assert len(ids) == len(set(ids))
    for a in actions.CATALOG:
        assert set(a) == {"id", "title", "params", "client", "route", "undoable"}
        assert isinstance(a["title"], str) and re.search(r"[가-힣]", a["title"])
        if a["client"]:
            assert a["route"] is None and a["undoable"] is False
        else:
            method, path = a["route"]
            assert method == "POST" and path.startswith("/api/")
    assert not set(ids) & set(actions.FORBIDDEN)
    must = {"setting", "background", "autostart", "sync", "google_push", "course_mode", "item_decision", "update_check",
            "update_apply", "auto_download", "mascot", "coach_dismiss", "plan_apply", "plan_remove", "checklist_toggle",
            "open_view", "tutorial", "download_calendar"}
    assert must <= set(ids)
    assert set(actions.SETTING_NAMES) == {"include_canvas", "weekly_summary", "review_reminder", "notify",
                                          "update_check", "auto_update"}
    text = actions.catalog_text()
    assert len(text) <= 2000 and all(i in text for i in ids) and "로그아웃" in text
    assert "새로" not in text


def test_catalog_routes_exist_for_built_in_features():
    """이미 있는 기능의 경로는 실제로 등록돼 있다(코치·공부 계획·체크리스트는 다른 담당이 만드는 중)."""
    later = {"coach_dismiss", "plan_apply", "plan_remove", "checklist_toggle"}
    for a in actions.CATALOG:
        if a["client"] or a["id"] in later:
            continue
        path = re.sub(r"\{\w+\}", "x1", a["route"][1])
        assert list(router.match("POST", path)), a["id"]


# ---------------------------------------------------------------- 말 알아듣기

@pytest.mark.parametrize("message, action, params", [
    ("컴퓨터 켤 때 자동 실행해줘", "autostart", {"enabled": True}),
    ("컴퓨터 켜면 앱 열어줘, 브라우저는 열지 말고", "autostart", {"enabled": True, "open_browser": False}),
    ("자동 실행 꺼줘", "autostart", {"enabled": False}),
    ("동기화해줘", "sync", {}),
    ("전체 다시 확인해줘", "sync", {"full": True}),
    ("주간 요약 꺼줘", "setting", {"key": "weekly_summary", "value": False}),
    ("Canvas 과제는 캘린더에서 빼줘", "setting", {"key": "include_canvas", "value": False}),
    ("확인할 후보 알림 켜줘", "setting", {"key": "review_reminder", "value": True}),
    ("알림 꺼줘", "setting", {"key": "notify", "value": False}),
    ("업데이트 확인 꺼줘", "setting", {"key": "update_check", "value": False}),
    ("자동 업데이트 켜줘", "setting", {"key": "auto_update", "value": True}),
    ("첨부 자동으로 받지 마", "auto_download", {"value": False}),
    ("6시간마다 자동 동기화해줘", "background", {"enabled": True, "interval_hours": 6}),
    ("백그라운드 동기화 꺼줘", "background", {"enabled": False}),
    ("구글 캘린더에 반영해줘", "google_push", {}),
    ("새 버전 확인해줘", "update_check", {}),
    ("ASIC P/NP로 바꿔줘", "course_mode", {"course_id": "101", "mode": "pass_fail"}),
    ("연구실 안전 교육 제외해줘", "course_mode", {"course_id": "105", "mode": "hidden"}),
    ("기말고사 승인해줘", "item_decision", {"item_id": FINAL_103, "decision": "approved"}),
    ("LAB4 11/21 마감 승인해줘", "item_decision", {"item_id": LAB4_B, "decision": "approved"}),
    ("Weekly log 9 제출함으로 표시해줘", "item_submitted", {"item_id": WEEKLY_LOG, "submitted": True}),
    ("캐릭터 바꿔줘", "mascot", {"choice": "original"}),
    ("브리핑 닫아줘", "coach_dismiss", {"kind": "briefing"}),
    ("자료 폴더 열어줘", "materials_folder", {}),
    ("성적 화면 열어줘", "open_view", {"view": "grades"}),
    ("확인함 열어줘", "open_view", {"view": "inbox"}),
    ("백그라운드 설정 열어줘", "open_view", {"view": "settings", "pane": "bg"}),
    ("ASIC 과목 열어줘", "open_view", {"view": "courses", "course_id": "101"}),
    ("튜토리얼 다시 보여줘", "tutorial", {"which": "tour"}),
    ("달라진 점 보여줘", "tutorial", {"which": "whatsnew"}),
    ("캘린더 파일 받아줘", "download_calendar", {}),
])
def test_handle_recognizes_commands(store, message, action, params):
    r = actions.handle(store, message)
    assert r is not None, message
    p = r["proposals"][0]
    assert (p["type"], p["action"], p["params"]) == ("action", action, real(params, store)), message
    assert p["rejected_reason"] is None and p["title"] and p["title"] in r["answer"]
    assert p["client"] is actions.BY_ID[action]["client"] and p["undoable"] is actions.BY_ID[action]["undoable"]
    assert "[실행]" in r["answer"] and "새로" not in r["answer"]


@pytest.mark.parametrize("message", [
    "기말 범위 어디까지야?", "기말에서 몇 점 받아야 A?", "LAB4 연장 문의 메일 써줘", "기말 전 2주 공부 계획 세워줘",
    "기말고사 날짜가 안 보여요", "성적이 안 들어오는 문제가 있습니다", "LAB 하나 0점이면?", "P/NP 기준 넘었어?",
    "동기화가 안 돼요", "자동 실행 기능 있어?", "ASIC 퀴즈 일정 알려줘", "중간고사 언제야?", "다크 모드 켜줘", "", "   ",
    "Signals 기말 날짜가 안 보여요 (제 메일 hong@korea.ac.kr)", "신호와시스템 성적이 안 들어와요", "LAB 점수가 성적에 안 들어가요",
    "x" * 501,
])
def test_handle_returns_none_for_non_commands(store, message):
    assert actions.handle(store, message) is None


def test_handle_same_title_candidates_and_already_state(store):
    r = actions.handle(store, "LAB4 승인해줘")
    assert [p["params"]["item_id"] for p in r["proposals"]] == real([LAB4_A, LAB4_B], store)     # 날짜를 안 말하면 둘 다 보여 주고 고르게
    assert "골라" in r["answer"] and "11/19" in r["answer"] and "11/21" in r["answer"]
    r = actions.handle(store, "주간 요약 켜줘")                                       # 이미 켜져 있음 → 단추 없이 이유
    assert r["proposals"][0]["rejected_reason"] == r["answer"] and "이미" in r["answer"]


def test_handle_uses_current_course_context(store):
    r = actions.handle(store, "이 과목 P/NP로 바꿔줘", course_id="104")
    assert r["proposals"][0]["params"] == {"course_id": "104", "mode": "pass_fail"}


@pytest.mark.parametrize("message, pane", [
    ("로그아웃해줘", "account"), ("데이터 다 지워줘", "account"), ("학번 넣어줘", "grades"), ("AI 켜줘", "ai"),
    ("codex로 바꿔줘", "ai"), ("구글 연결 해제해줘", "google"), ("구글 캘린더 연결해줘", "google"), ("토큰 바꿔줘", "account"),
])
def test_handle_forbidden_only_opens_the_settings_pane(store, message, pane):
    r = actions.handle(store, message)
    assert [(p["action"], p["params"], p["client"]) for p in r["proposals"]] == [
        ("open_view", {"view": "settings", "pane": pane}, True)]
    assert "에이전트가 하지 않아요" in r["answer"] and "직접" in r["answer"]


# ---------------------------------------------------------------- 검사

@pytest.mark.parametrize("p, why", [
    ({"action": "logout"}, "개인정보"),
    ({"action": "google_disconnect", "params": {}}, "개인정보"),
    ({"action": "setting", "params": {"key": "ai_cli", "value": True}}, "개인정보"),
    ({"action": "setting", "params": {"key": "student_id", "value": True}}, "개인정보"),
    ({"action": "setting", "params": {"key": "download_dir", "value": True}}, "바꿀 수 있는 설정이 아니"),
    ({"action": "format_disk", "params": {}}, "할 수 있는 동작이 아니"),
    ({"params": {}}, "action"),
    ({"action": "setting", "params": "weekly_summary"}, "객체"),
    ({"action": "setting", "params": {"key": "weekly_summary", "value": "no"}}, "true 또는 false"),
    ({"action": "setting", "params": {"key": "weekly_summary", "value": True}}, "이미 켜져"),
    ({"action": "background", "params": {"enabled": True, "interval_hours": 5}}, "간격"),
    ({"action": "course_mode", "params": {"course_id": "999", "mode": "hidden"}}, "과목을 찾을 수 없"),
    ({"action": "course_mode", "params": {"course_id": "101", "mode": "secret"}}, "mode"),
    ({"action": "course_mode", "params": {"course_id": "../../etc", "mode": "hidden"}}, "올바르지"),
    ({"action": "item_decision", "params": {"item_id": "nope", "decision": "approved"}}, "항목을 찾을 수 없"),
    ({"action": "item_decision", "params": {"item_id": LAB4_A, "decision": "pending"}}, "이미"),
    ({"action": "open_view", "params": {"view": "desktop"}}, "view"),
    ({"action": "open_view", "params": {"view": "grades", "pane": "ai"}}, "pane"),
    ({"action": "mascot", "params": {"choice": "auto"}}, "이미"),
    ({"action": "plan_apply", "params": {"title": "x", "blocks": []}}, "블록"),
])
def test_check_rejects(store, p, why):
    out = actions.check(store, real(p, store))
    assert out["type"] == "action" and out["rejected_reason"] and why in out["rejected_reason"], out["rejected_reason"]


def test_check_rewrites_title_and_flags_from_catalog(store):
    """AI가 보낸 제목·client·undoable은 믿지 않는다(목록 기준으로 다시 씀). 모르는 params 칸은 버린다."""
    out = actions.check(store, {"action": "sync", "params": {"full": False, "rm": "-rf"}, "title": "구글 연결 해제",
                                "client": True, "undoable": True, "reason": "x" * 1000})
    assert out["rejected_reason"] is None and out["params"] == {}
    assert (out["title"], out["client"], out["undoable"]) == ("지금 동기화", False, False)
    assert len(out["reason"]) == 300


# ---------------------------------------------------------------- 실행·되돌리기

def test_run_undo_setting_and_conflict(app):
    p = run_and_keep(app, ok(app.store, "setting", key="weekly_summary", value=False))
    assert app.store.data["settings"]["weekly_summary"] is False
    assert p["_undo"] == {"key": "weekly_summary", "value": True, "set": False}
    assert actions.undo(app, p)["ok"] and app.store.data["settings"]["weekly_summary"] is True

    p = run_and_keep(app, ok(app.store, "setting", key="weekly_summary", value=False))
    app.store.data["settings"]["weekly_summary"] = True                    # 그 뒤에 사용자가 직접 다시 켬
    with pytest.raises(ApiError) as e:
        actions.undo(app, p)
    assert e.value.status == 409


def test_run_rechecks_against_current_state(app):
    p = ok(app.store, "setting", key="review_reminder", value=True)
    app.store.data["settings"]["review_reminder"] = True                   # 제안과 실행 사이에 바뀜
    with pytest.raises(ApiError, match="이미 켜져"):
        actions.run(app, p)
    with pytest.raises(ApiError, match="개인정보"):
        actions.run(app, {"type": "action", "action": "setting", "params": {"key": "ai_cli", "value": True}})
    with pytest.raises(ApiError, match="화면에서"):
        actions.run(app, ok(app.store, "open_view", view="grades"))


def test_run_undo_course_mode(app):
    p = run_and_keep(app, ok(app.store, "course_mode", course_id="104", mode="pass_fail"))
    assert course_mode(app.store.data, "104") == "pass_fail"
    assert next(c for c in app.state()["courses"] if str(c["id"]) == "104")["mode"] == "pass_fail"
    actions.undo(app, p)
    assert course_mode(app.store.data, "104") == "normal"


def test_run_undo_item_decision_restores_replaced_candidate(app):
    A, B = real([LAB4_A, LAB4_B], app.store)
    a = run_and_keep(app, ok(app.store, "item_decision", item_id=A, decision="approved"))
    view = {v["id"]: v for v in app.store.view()}
    assert view[A]["status"] == "scheduled"
    b = run_and_keep(app, ok(app.store, "item_decision", item_id=B, decision="approved"))
    assert b["_undo"]["replaced"] == [A] and "다른 후보" in b["result"]["message"]
    view = {v["id"]: v for v in app.store.view()}
    assert (view[A]["decision"], view[B]["status"]) == ("pending", "scheduled")
    actions.undo(app, b)                                                   # 11/21 승인 되돌림 → 11/19가 다시 승인
    view = {v["id"]: v for v in app.store.view()}
    assert (view[A]["status"], view[B]["decision"]) == ("scheduled", "pending")
    actions.undo(app, a)
    assert {v["id"]: v for v in app.store.view()}[A]["decision"] == "pending"


def test_run_undo_item_submitted(app):
    p = run_and_keep(app, ok(app.store, "item_submitted", item_id=WEEKLY_LOG, submitted=True))
    assert next(v for v in app.store.view() if v["id"] == WEEKLY_LOG)["submitted"] is True
    actions.undo(app, p)
    v = next(v for v in app.store.view() if v["id"] == WEEKLY_LOG)
    assert v["submitted"] is False and v["submitted_source"] is None         # 원래 값(null)으로


def test_run_undo_background_with_fake_scheduler(app):
    p = run_and_keep(app, ok(app.store, "background", enabled=True, interval_hours=6))
    assert app.scheduler.run.registered and "<Interval>PT6H</Interval>" in app.scheduler.run.xml
    assert p["_undo"] == {"enabled": False, "interval_hours": 3} and "6시간" in p["result"]["message"]
    again = actions.run(app, ok(app.store, "background", enabled=True, interval_hours=6))   # 이미 그 상태 → 그대로
    assert "이미" in again["message"]
    actions.undo(app, p)
    assert not app.scheduler.run.registered and app.state()["background"]["enabled"] is False


def test_run_undo_autostart_with_fake_runner(app):
    p = run_and_keep(app, ok(app.store, "autostart", enabled=True, open_browser=False))
    st = app.state()["autostart"]
    assert st["enabled"] and st["open_browser"] is False and app.autostart.run.registered
    assert app.autostart.launcher().exists() and "다음 로그인" in p["result"]["message"]
    assert p["_undo"] == {"enabled": False, "open_browser": True}
    actions.undo(app, p)
    st = app.state()["autostart"]
    assert not st["enabled"] and st["open_browser"] is True and not app.autostart.launcher().exists()


def test_run_undo_mascot_and_auto_download(app):
    p = run_and_keep(app, ok(app.store, "mascot", choice="original"))
    assert app.state()["mascot"]["choice"] == "original"
    actions.undo(app, p)
    assert app.state()["mascot"]["choice"] == "auto"
    p = run_and_keep(app, ok(app.store, "auto_download", value=False))
    assert app.state()["materials"]["auto_download"] is False
    actions.undo(app, p)
    assert app.state()["materials"]["auto_download"] is True


def test_sync_update_check_and_not_undoable(app):
    p = run_and_keep(app, ok(app.store, "sync", full=True))
    assert "전체" in p["result"]["message"] and p["_undo"] is None
    with pytest.raises(ApiError, match="되돌릴 수 없는"):
        actions.undo(app, p)
    r = actions.run(app, ok(app.store, "update_check"))
    assert "v99.0.0" in r["message"] and r["undo"] is None
    with pytest.raises(ApiError) as e:                                       # 설치 방식을 모름 → 경로가 400
        actions.run(app, ok(app.store, "update_apply"))
    assert e.value.status == 400


def test_google_push_needs_connection(app):
    with pytest.raises(ApiError, match="연결돼 있지 않아요"):
        actions.run(app, ok(app.store, "google_push"))


def test_undo_without_value(app):
    p = ok(app.store, "setting", key="weekly_summary", value=False)
    with pytest.raises(ApiError, match="되돌릴 값이 없어요"):
        actions.undo(app, p)
    with pytest.raises(ApiError):
        actions.undo(app, {"action": "logout"})


def test_demo_app_refuses_os_actions(tmp_path, store):
    demo = App(store, tmp_path, cfg={}, demo=True)
    for action, params in (("autostart", {"enabled": True}), ("background", {"enabled": True})):
        with pytest.raises(ApiError, match="데모"):
            actions.run(demo, ok(store, action, **params))


# ---------------------------------------------------------------- 다른 담당이 만드는 경로(가짜로 앞에 끼움)

@pytest.fixture()
def fake_routes(monkeypatch):
    calls = []

    def dismiss(app, body):
        calls.append(("dismiss", body))
        return {"ok": True}

    def plan_apply(app, body):
        calls.append(("plan_apply", body))
        plans = app.store.data.setdefault("plans", [])
        plans.append({"id": f"p-{len(plans) + 1}", "title": body["title"], "blocks": body["blocks"]})
        return {"plans": list(plans)}

    def plan_remove(app, body):
        calls.append(("plan_remove", body))
        app.store.data["plans"] = [x for x in app.store.data.get("plans") or [] if x["id"] != body["id"]]
        return {"plans": app.store.data["plans"]}

    def checklist(app, body, item_id):
        calls.append(("checklist", item_id, body))
        return {"ok": True}

    def coach_state(app, ctx):
        return {"briefing": {"id": "b-2026-11-09", "kind": "briefing"}, "retro": None, "report": None, "changes": []}

    fakes = [Route("POST", re.compile(p), fn, True, "fake." + fn.__name__, 99) for p, fn in (
        (r"/api/coach/dismiss", dismiss), (r"/api/plan/apply", plan_apply), (r"/api/plan/remove", plan_remove),
        (r"/api/checklist/(?P<item_id>[\w\-]+)", checklist))]
    monkeypatch.setattr(router, "routes", fakes + router.routes)
    monkeypatch.setattr(router, "sections", [("coach", coach_state)] + router.sections)
    return calls


def test_parallel_routes_plan_checklist_coach(app, fake_routes):
    A = real(LAB4_A, app.store)
    blocks = [{"start": "2030-01-01T20:00:00+09:00", "end": "2030-01-01T21:00:00+09:00"}]
    p = run_and_keep(app, ok(app.store, "plan_apply", title="기말 대비", course_id="103", blocks=blocks))
    assert p["_undo"] == {"plan_id": "p-1"} and fake_routes[-1][0] == "plan_apply"
    assert fake_routes[-1][1]["decided_by"] == "ai" and fake_routes[-1][1]["blocks"] == blocks
    assert actions.handle(app.store, "공부 계획 지워줘")["proposals"][0]["params"] == {"id": "p-1"}
    actions.undo(app, p)
    assert fake_routes[-1] == ("plan_remove", {"id": "p-1"}) and app.store.data["plans"] == []

    p = run_and_keep(app, ok(app.store, "checklist_toggle", item_id=A, id="c1"))
    actions.undo(app, p)
    assert fake_routes[-2:] == [("checklist", A, {"op": "toggle", "id": "c1"})] * 2

    actions.run(app, ok(app.store, "coach_dismiss", kind="briefing"))       # id는 지금 카드에서 찾는다
    assert fake_routes[-1] == ("dismiss", {"id": "b-2026-11-09"})
    with pytest.raises(ApiError, match="되돌릴 수 없는"):
        actions.undo(app, {"action": "coach_dismiss", "_undo": {}})


def test_plan_apply_through_the_real_route_when_present(app):
    """공부 계획 경로(담당 F)가 들어와 있으면 실제 경로로 넣고 되돌린다(아직 없으면 건너뜀)."""
    if not any(not r.name.startswith("fake.") for r, _ in router.match("POST", "/api/plan/apply")):
        pytest.skip("공부 계획 경로가 아직 없음")
    from datetime import timedelta
    from lms_agent.timeutil import iso, now_local
    day = now_local().replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(days=3)
    blocks = [{"start": iso(day + timedelta(days=n)), "end": iso(day + timedelta(days=n, hours=1))} for n in (0, 1)]
    p = run_and_keep(app, ok(app.store, "plan_apply", title="기말 대비", course_id="103", blocks=blocks))
    pid = p["_undo"]["plan_id"]
    assert any(x["id"] == pid for x in app.state()["plans"])
    actions.undo(app, p)
    assert not any(x["id"] == pid for x in app.state()["plans"])


def test_missing_route_is_a_clear_error(app, monkeypatch):
    monkeypatch.setattr(router, "routes", [r for r in router.routes if r.pattern.pattern != r"/api/coach/dismiss"])
    with pytest.raises(ApiError) as e:
        actions.run(app, ok(app.store, "coach_dismiss", id="b-1"))
    assert e.value.status == 404 and "없어요" in str(e.value)


# ---------------------------------------------------------------- 구글 캘린더의 다른 일정(계약 23-3)

@pytest.fixture()
def gapp(tmp_path, store, mock):
    """가짜 구글로 연결(쓰기만)된 앱. 읽기 권한은 grant_read()로."""
    import urllib.error
    import urllib.parse
    import urllib.request

    from lms_agent.gcal import OAuthFlow
    from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls

    gmock, gbase = start_mock_google()
    u = urls(gbase)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def tokens(read):
        flow = OAuthFlow(DEMO_CLIENT_ID, "http://127.0.0.1:1/", DEMO_CLIENT_SECRET,
                         auth_url=u["auth_url"], token_url=u["token_url"])
        try:
            urllib.request.build_opener(NoRedirect).open(flow.authorization_url(read=read), timeout=5)
        except urllib.error.HTTPError as e:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(e.headers["Location"]).query))
        return flow.exchange(q["code"], q["state"])

    a = App(store, tmp_path, cfg={"canvas_url": mock, "token": DEMO_TOKEN, "google": {"tokens": tokens(False)}},
            google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=u, gcal_read_every=0)
    a.grant_read = lambda: a.cfg["google"].update(tokens=tokens(True))
    yield a
    gmock.shutdown()
    gmock.server_close()


def _wait_fetch(app):
    import time
    end = time.time() + 8
    while app.google.fetching and time.time() < end:
        time.sleep(0.03)
    assert not app.google.fetching


@pytest.mark.parametrize("message, on", [
    ("구글 다른 일정 보여줘", "on"), ("구글캘린더 연동해서 다른 일정도 대시보드에 보이게 해줘", "on"),
    ("다른 일정도 대시보드에 보여줘", "on"), ("구글 다른 일정 꺼줘", "off"), ("구글 일정 안 보이게 해줘", "off"),
    ("구글 다른 일정 보기 해제해줘", "off"), ("구글 일정 새로 고침", "refresh"), ("구글 일정 다시 가져와", "refresh"),
])
def test_handle_gcal_read_commands(store, message, on):
    store.data["settings"]["gcal_read"] = on != "on"                        # '이미 그 상태'가 아니게
    r = actions.handle(store, message)
    assert r is not None, message
    p = r["proposals"][0]
    want = ("gcal_refresh", {}) if on == "refresh" else ("gcal_read", {"enabled": on == "on"})
    assert (p["action"], p["params"]) == want and p["rejected_reason"] is None, message
    assert p["undoable"] is (on != "refresh") and "[실행]" in r["answer"] and "새로" not in r["answer"]
    if on == "on":
        assert "설정 → 구글 캘린더" in r["answer"]                              # 동의는 사용자가 직접


def test_handle_gcal_read_leaves_other_google_commands_alone(store):
    assert actions.handle(store, "구글 캘린더에 반영해줘")["proposals"][0]["action"] == "google_push"
    r = actions.handle(store, "구글 연결 해제해줘")
    assert r["proposals"][0]["params"] == {"view": "settings", "pane": "google"} and "하지 않아요" in r["answer"]
    assert actions.handle(store, "구글 일정 설정 열어줘")["proposals"][0]["params"] == {"view": "settings", "pane": "google"}
    assert actions.handle(store, "구글 일정이 안 보여요") is None              # 불평은 '놓친 것' 갈래로
    assert actions.handle(store, "구글 일정은 어디서 봐?") is None             # 질문
    r = actions.handle(store, "구글 다른 일정 꺼줘")                           # 이미 꺼져 있음
    assert "이미 꺼져" in r["proposals"][0]["rejected_reason"]
    out = actions.check(store, {"action": "gcal_refresh", "params": {}})
    assert "꺼져" in out["rejected_reason"]
    out = actions.check(store, {"action": "gcal_read", "params": {"enabled": "yes"}})
    assert "true 또는 false" in out["rejected_reason"]


def test_run_gcal_read_needs_connection_and_permission(app, gapp):
    with pytest.raises(ApiError, match="연결돼 있지 않아요") as e:              # 연결 안 됨
        actions.run(app, ok(app.store, "gcal_read", enabled=True))
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:                                       # 연결됐지만 읽기 권한 없음 → 거짓 성공 없이 안내
        actions.run(gapp, ok(gapp.store, "gcal_read", enabled=True))
    assert e.value.status == 400 and "설정 → 구글 캘린더에서 권한을 허락해 주세요" in str(e.value)
    assert gapp.store.data["settings"].get("gcal_read") is not True and gapp.google.flow is None   # 동의 화면을 열지 않음
    with pytest.raises(ApiError, match="꺼져 있어요"):                       # 꺼져 있으면 다시 가져오기도 거절
        actions.run(gapp, {"type": "action", "action": "gcal_refresh", "params": {}})
    gapp.store.data["settings"]["gcal_read"] = True                         # 켜져 있어도 권한이 없으면 안내
    with pytest.raises(ApiError, match="권한을 허락해 주세요"):
        actions.run(gapp, ok(gapp.store, "gcal_refresh"))


def test_run_undo_gcal_read_and_refresh(gapp):
    gapp.grant_read()
    p = run_and_keep(gapp, ok(gapp.store, "gcal_read", enabled=True))
    assert gapp.store.data["settings"]["gcal_read"] is True and p["_undo"] == {"enabled": False, "set": True}
    _wait_fetch(gapp)
    assert len(gapp.state()["events"]) == 12
    r = actions.run(gapp, ok(gapp.store, "gcal_refresh"))
    assert r["undo"] is None and "다시 가져오고" in r["message"]
    _wait_fetch(gapp)
    with pytest.raises(ApiError, match="되돌릴 수 없는"):
        actions.undo(gapp, {"action": "gcal_refresh", "_undo": {}})
    actions.undo(gapp, p)                                                    # 되돌리기 = 끄기(가져온 일정도 지움)
    assert gapp.store.data["settings"]["gcal_read"] is False and gapp.state()["events"] == []
    # 끈 것을 되돌리면 다시 켠다 / 실행한 뒤 사용자가 바꿨으면 409
    off = run_and_keep(gapp, ok(gapp.store, "gcal_read", enabled=True))
    _wait_fetch(gapp)
    gapp.store.data["settings"]["gcal_read"] = False
    with pytest.raises(ApiError) as e:
        actions.undo(gapp, off)
    assert e.value.status == 409
