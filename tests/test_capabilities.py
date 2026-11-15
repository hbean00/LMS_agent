"""말로 시키면 해석해서 직접 한다 — 능력 목록·실행·공통 되돌리기(계약 28-1).

커버리지(화면의 모든 POST 경로 = 능력 또는 이유 있는 제외), 능력 정의 모양, plan(값 검사·이름 → id·애매하면 후보·미리 보기·바꾸지 않음),
run(화면과 같은 경로 → 되돌리기 기록)·undo(그대로일 때만, 아니면 409·아무것도 안 바꿈), 거절 등급, 밖에 반영하는 능력, catalog_text 글자 수.
데모 날짜·항목 id는 그날·그 PC 시간대에 따라 바뀌므로 저장소에서 찾고, 시각은 같은 순간인지(parse_iso)로 비교한다."""
import json
import re
from datetime import timedelta

import pytest

from lms_agent import actions, autostart, capabilities as C, core, schedule
from lms_agent.api import ApiError, router
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App          # 불러오면 features.load_all() — 모든 경로가 등록된다
from lms_agent.store import Store, course_mode
from lms_agent.timeutil import now_local, parse_iso
from test_actions import AutoRunner, SchedRunner, fake_release


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
    return App(store, tmp_path, cfg={"canvas_url": mock, "token": DEMO_TOKEN},
               scheduler=schedule.Scheduler(tmp_path, runner=SchedRunner()),
               autostart=autostart.Autostart(tmp_path, runner=AutoRunner(), platform="win32"),
               update_fetch=fake_release, update_how="unknown", relaunch=lambda argv: pytest.fail("다시 켜면 안 됨"),
               opener=lambda path: None, gcal_read_every=0)


@pytest.fixture()
def gapp(tmp_path, store, mock):
    """가짜 구글로 연결(쓰기)된 앱."""
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

    flow = OAuthFlow(DEMO_CLIENT_ID, "http://127.0.0.1:1/", DEMO_CLIENT_SECRET,
                     auth_url=u["auth_url"], token_url=u["token_url"])
    try:
        urllib.request.build_opener(NoRedirect).open(flow.authorization_url(read=False), timeout=5)
    except urllib.error.HTTPError as e:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(e.headers["Location"]).query))
    tokens = flow.exchange(q["code"], q["state"])
    a = App(store, tmp_path, cfg={"canvas_url": mock, "token": DEMO_TOKEN, "google": {"tokens": tokens}},
            google=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET), google_urls=u, gcal_read_every=0)
    yield a
    gmock.shutdown()
    gmock.server_close()


def _shape(s: str) -> str:
    return re.sub(r"\{\w*\}", "{}", s)


def _view(app, iid):
    return next(v for v in app.store.view() if v["id"] == iid)


def _lab4(store) -> list:
    return sorted((v for v in store.view() if v.get("title") == "LAB4 보고서" and str(v.get("course_id")) == "101"),
                  key=lambda v: parse_iso(v["due"]))


def _md(due) -> str:
    d = parse_iso(due)
    return f"{d.month}/{d.day}"


def ok_plan(app, cap_id, **params) -> dict:
    p = C.plan(app, cap_id, params)
    assert p["ok"], p["error"]
    return p


# ---------------------------------------------------------------- 커버리지·정의

def test_every_post_route_has_a_capability_or_is_excluded():
    """화면의 POST 경로가 늘면 능력도 늘어야 한다(성적 경로는 capabilities_grades가 더한 능력까지 센다)."""
    from lms_agent import features
    features.load_all()
    C._ensure_loaded()
    routes = []
    for r in router.routes:
        if r.method == "POST":
            s = re.sub(r"\(\?P<\w+>[^)]*\)", "{}", r.pattern.pattern).replace("\\.", ".").replace("\\-", "-")
            if s not in routes:
                routes.append(s)
    covered = {_shape(c.route[1]) for c in C.REGISTRY.values() if c.route and c.route[0] == "POST"}
    excluded = {_shape(s) for s in C.EXCLUDED_ROUTES}
    missing = [s for s in routes if s not in covered and s not in excluded]
    assert not missing, ("이 POST 경로에 능력이 없어요: " + ", ".join(missing) +
                         " — capabilities.py(성적은 capabilities_grades.py)에 능력을 더하거나 EXCLUDED_ROUTES에 이유와 함께 넣어 주세요")
    assert C.uncovered_routes() == []
    stale = sorted({c.id for c in C.REGISTRY.values() if c.route and _shape(c.route[1]) not in set(routes)})
    assert not stale, "능력의 경로가 router에 없어요: " + ", ".join(stale)
    gone = sorted(s for s in C.EXCLUDED_ROUTES if _shape(s) not in set(routes))
    assert not gone, "EXCLUDED_ROUTES에 없는 경로가 있어요: " + ", ".join(gone)
    for s, why in C.EXCLUDED_ROUTES.items():
        assert isinstance(why, str) and re.search(r"[가-힣]", why), s


def test_capability_definitions_are_well_formed():
    caps = list(C.REGISTRY.values())
    assert len(caps) >= 40
    types = {"str", "int", "float", "bool", "enum", "list", "dict"}
    for c in caps:
        assert "." in c.id and c.area in C.AREAS and c.tier in C.TIERS, c.id
        assert re.search(r"[가-힣]", c.title), c.id
        for name, spec in c.params.items():
            assert spec.get("type") in types, (c.id, name)
            if spec.get("type") == "enum":
                assert spec.get("enum"), (c.id, name)
        if c.tier == "refuse":
            assert c.refuse and re.search(r"[가-힣]", c.refuse) and c.pane in actions.PANES, c.id
            assert c.undoable is False, c.id
        else:
            assert callable(c.build), c.id
        if c.id.startswith(("grades.", "items.set_component")):
            continue
        text = " ".join([c.title, c.refuse or ""] + [str((s or {}).get("desc") or "") for s in c.params.values()])
        assert "새로" not in text, c.id
    tier = {c.id: c.tier for c in caps}
    for cid in ("app.autostart", "app.update_apply", "app.update_restart", "app.quit"):
        assert tier[cid] == "confirm", cid
    for cid in ("ai.enable", "ai.auto_triage", "ai.provider", "ai.daily_limit", "account.login", "account.logout",
                "account.wipe_data", "account.student_id", "calendar.connect", "calendar.disconnect"):
        assert tier[cid] == "refuse", cid
    for cid in ("calendar.push", "app.sync", "settings.weekly_summary", "items.set_due", "courses.set_mode",
                "plan.apply", "checklist.set", "coach.dismiss", "materials.auto_download", "app.background"):
        assert tier[cid] == "direct", cid
    push = C.REGISTRY["calendar.push"]
    assert push.external and not push.undoable and not C.REGISTRY["app.sync"].undoable
    must = {"settings.include_canvas", "settings.weekly_summary", "settings.review_reminder", "notify.enabled",
            "settings.update_check", "settings.auto_update", "settings.tutorial_done", "items.set_title", "items.set_due",
            "items.set_decision", "items.set_submitted", "items.add_text", "courses.set_mode", "materials.set_folder",
            "materials.open", "app.background", "app.autostart", "app.sync", "calendar.push", "calendar.read",
            "calendar.choose", "calendar.refresh", "plan.apply", "plan.remove", "checklist.set", "coach.dismiss",
            "notify.mark_seen", "notify.snooze_grades", "app.mascot", "app.update_check", "app.update_apply",
            "app.update_restart", "app.update_dismiss", "app.quit", "courses.mail_draft"}
    assert must <= set(tier)


# ---------------------------------------------------------------- plan: 이름 → id·값 검사·바꾸지 않음

def test_plan_resolves_course_names_and_lists_candidates_when_ambiguous(app):
    p = C.plan(app, "courses.set_mode", {"course": "EE3", "mode": "pass_fail"})    # EE342·EE361
    assert not p["ok"] and "여럿" in p["error"] and {c["id"] for c in p["candidates"]} == {"102", "103"}
    assert "디지털신호처리" in p["error"] and "반도체공학" in p["error"]
    for name in ("ASIC", "ASIC설계(영강)", "asic설계", "EE401", "101", 101, "ASIC 과목"):
        assert ok_plan(app, "courses.set_mode", course=name, mode="pass_fail")["params"]["course"] == "101", name
    p = C.plan(app, "courses.set_mode", {"course": "없는과목", "mode": "hidden"})
    assert not p["ok"] and "찾지 못했어요" in p["error"] and "반도체공학" in p["error"]
    p = ok_plan(app, "courses.set_mode", course="Capstone", mode="P/NP")             # 한국어 이름으로 고른 enum
    assert p["params"] == {"course": "104", "mode": "pass_fail"}
    assert p["changes"] == [{"label": "Capstone Design 과목 종류", "before": "성적 있음", "after": "P/NP"}]
    assert p["tier"] == "direct" and p["undoable"] and "성적 있음 → P/NP" in p["summary"]
    assert C.plan(app, "courses.set_mode", {"course_id": "반도체", "mode": "hidden"})["params"]["course"] == "103"


def test_plan_resolves_items_by_title_number_and_date(app):
    a, b = _lab4(app.store)
    p = C.plan(app, "items.set_decision", {"course": "ASIC", "item": "LAB4 보고서", "decision": "approved"})
    assert not p["ok"] and {c["id"] for c in p["candidates"]} == {a["id"], b["id"]} and "여럿" in p["error"]
    assert ok_plan(app, "items.set_decision", item=f"LAB4 {_md(b['due'])}", decision="approved")["params"]["item"] == b["id"]
    assert ok_plan(app, "items.set_decision", item=a["id"], decision="approved")["params"]["item"] == a["id"]
    lab5 = next(v for v in app.store.view() if (v.get("title") or "").startswith("LAB5"))
    assert ok_plan(app, "items.set_decision", course="ASIC", item="LAB5", decision="rejected")["params"]["item"] == lab5["id"]
    hw = [v for v in app.store.view() if v.get("title") == "Homework 5"]
    alive = [v for v in hw if v["status"] in C.ACTIVE]
    if len(hw) > 1 and len(alive) == 1:          # 같은 제목의 중복 후보는 살아 있는 쪽으로
        assert ok_plan(app, "items.set_decision", item="Homework 5", decision="rejected")["params"]["item"] == alive[0]["id"]
    p = C.plan(app, "items.set_decision", {"course": "반도체", "item": lab5["id"], "decision": "rejected"})
    assert not p["ok"] and "과목의 것이 아니에요" in p["error"]
    p = C.plan(app, "items.set_decision", {"course": "반도체", "item": "LAB 99", "decision": "rejected"})
    assert not p["ok"] and "찾지 못했어요" in p["error"]


def test_plan_checks_values(app):
    def err(cap_id, **params):
        p = C.plan(app, cap_id, params)
        assert not p["ok"] and p["error"], (cap_id, params)
        return p["error"]
    assert "true 또는 false" in err("notify.enabled", value="yes")
    assert "1·3·6·12·24" in err("app.background", enabled=True, interval_hours=5)
    assert "15~240" in err("plan.apply", course="반도체", minutes=5)
    assert "받지 않는 값" in err("settings.weekly_summary", value=False, color="red")
    assert "값이 필요해요" in err("settings.weekly_summary")
    assert "값이 필요해요" in err("items.set_due", item="LAB5")
    assert "YYYY-MM-DDTHH:MM" in err("items.set_due", item="LAB5", due="다음 주 금요일")
    assert "200자" in err("items.set_title", item="LAB5", title="가" * 201)
    assert "모르는 능력" in err("settings.no_such_thing")
    assert "이미 그 상태" in err("settings.weekly_summary", value=True)
    assert "연결돼 있지 않아요" in err("calendar.push")
    p = ok_plan(app, "app.background", enabled=True, interval_hours="6")          # 글자로 준 숫자도 enum으로
    assert p["params"] == {"enabled": True, "interval_hours": 6}
    assert p["changes"][0]["after"] == "켜짐(6시간마다)"
    p = ok_plan(app, "items.set_due", item="LAB5", due="2031-03-02")                # 날짜만 → 23:59(로컬)
    assert p["params"]["due"] == "2031-03-02T23:59" and p["changes"][0]["after"] == "2031/3/2 23:59"


def test_plan_has_no_side_effects(app):
    app.state()                                   # 처음 계산이 쓰는 기록 칸(축하·체크리스트 칸)을 먼저 채워 둔다
    app.store.save()
    before = json.dumps(app.store.data, sort_keys=True, default=str)
    mtime = app.store.path.stat().st_mtime_ns
    a, _ = _lab4(app.store)
    hw = next(v for v in app.store.view() if v.get("title") == "Homework 5" and v["status"] in C.ACTIVE)
    for cap_id, params in (("settings.weekly_summary", {"value": False}), ("courses.set_mode", {"course": "ASIC", "mode": "hidden"}),
                           ("items.set_due", {"item": a["id"], "due": "2031-01-02T10:00"}),
                           ("items.add_text", {"text": "기말 보고서는 12월 20일 23:59까지", "course": "반도체"}),
                           ("plan.apply", {"course": "반도체"}), ("coach.dismiss", {"card": "briefing"}),
                           ("checklist.set", {"item": hw["id"], "entry": "LMS", "done": True}),
                           ("notify.mark_seen", {}), ("app.background", {"enabled": True}),
                           ("courses.mail_draft", {"course": "디지털", "purpose": "question"}),
                           ("ai.enable", {}), ("materials.set_folder", {"folder": None})):
        C.plan(app, cap_id, params)
    assert json.dumps(app.store.data, sort_keys=True, default=str) == before
    assert app.store.path.stat().st_mtime_ns == mtime
    assert not app.scheduler.run.registered


# ---------------------------------------------------------------- run·undo

def test_run_undo_setting_and_message(app):
    r = C.run(app, ok_plan(app, "settings.weekly_summary", value=False))
    assert app.store.data["settings"]["weekly_summary"] is False and app.state()["settings"]["weekly_summary"] is False
    assert "주간 요약" in r["message"] and r["ok"] is True
    rec = r["undo"]
    assert rec["cap"] == "settings.weekly_summary" and set(rec["keys"]) == {"settings/weekly_summary"}
    assert rec["keys"]["settings/weekly_summary"]["after"] is False and parse_iso(rec["at"]) is not None
    json.dumps(rec)                                                     # 대화 기록(state.json)에 그대로 남길 수 있다
    assert not C.plan(app, "settings.weekly_summary", {"value": False})["ok"]   # 이미 꺼짐
    assert C.undo(app, json.loads(json.dumps(rec)))["ok"]
    assert app.state()["settings"]["weekly_summary"] is True


def test_run_undo_item_due(app):
    a, _ = _lab4(app.store)
    want = parse_iso(a["due"]) + timedelta(days=2, hours=1)
    r = C.run(app, ok_plan(app, "items.set_due", course="ASIC", item=a["id"], due=want.strftime("%Y-%m-%dT%H:%M")))
    v = _view(app, a["id"])
    assert parse_iso(v["due"]) == want and v["edited"] and v["decided_by"] == "user"
    assert list(r["undo"]["keys"]) == [f"user/{a['id']}"]
    C.undo(app, r["undo"])
    v = _view(app, a["id"])
    assert parse_iso(v["due"]) == parse_iso(a["due"]) and not v["edited"]
    assert a["id"] not in app.store.data["user"]


def test_run_undo_course_mode(app):
    r = C.run(app, ok_plan(app, "courses.set_mode", course="Capstone", mode="pass_fail"))
    assert course_mode(app.store.data, "104") == "pass_fail"
    assert next(c for c in app.state()["courses"] if str(c["id"]) == "104")["mode"] == "pass_fail"
    assert set(r["undo"]["keys"]) == {"course_prefs/104"}
    C.undo(app, r["undo"])
    assert course_mode(app.store.data, "104") == "normal"


def _blocks(days: int, n: int = 2) -> list:
    day = now_local().replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return [{"start": (day + timedelta(days=k)).isoformat(), "end": (day + timedelta(days=k, minutes=50)).isoformat()}
            for k in range(n)]


def test_run_undo_plan_apply_and_remove_keep_other_plans(app):
    r1 = C.run(app, ok_plan(app, "plan.apply", title="기말 대비 1", course="반도체", blocks=_blocks(3)))
    r2 = C.run(app, ok_plan(app, "plan.apply", title="기말 대비 2", blocks=_blocks(9)))
    plans = {p["title"]: p for p in app.state()["plans"]}
    assert set(plans) == {"기말 대비 1", "기말 대비 2"} and plans["기말 대비 1"]["decided_by"] == "user"
    assert plans["기말 대비 1"]["course_id"] == "103" and "기말 대비 1" in r1["message"]
    assert list(r1["undo"]["keys"]) == [f"plans/#{plans['기말 대비 1']['id']}"]
    C.undo(app, r1["undo"])                                             # 나중에 넣은 계획은 그대로
    assert [p["title"] for p in app.state()["plans"]] == ["기말 대비 2"]
    rm = C.run(app, ok_plan(app, "plan.remove", plan="대비 2"))
    assert app.state()["plans"] == []
    C.undo(app, rm["undo"])
    assert [p["title"] for p in app.state()["plans"]] == ["기말 대비 2"]
    with pytest.raises(ApiError) as e:                                  # 지운 것을 되살렸으니 넣기 기록은 지금 그대로 — 되돌림
        C.undo(app, rm["undo"])
    assert e.value.status == 409
    C.undo(app, r2["undo"])
    assert app.state()["plans"] == []


def test_run_undo_manual_text_rebuilds_items(app):
    before = {v["id"] for v in app.store.view()}
    r = C.run(app, ok_plan(app, "items.add_text", course="반도체",
                           text="기말 프로젝트 보고서는 12월 18일 23:59까지 이메일로 제출하세요."))
    new = [v for v in app.store.view() if v["id"] not in before]
    assert new and all(str(v["course_id"]) == "103" for v in new)
    assert [k.split("/")[0] for k in r["undo"]["keys"]] == ["manual"]
    C.undo(app, r["undo"])
    assert app.store.data["manual"] == [] and {v["id"] for v in app.store.view()} == before


def test_run_undo_checklist_coach_mascot_and_folder(app, tmp_path):
    boxes = {iid: b for iid, b in (app.state()["checklists"] or {}).items() if b.get("items")}
    iid, box = next(iter(boxes.items()))
    row = box["items"][0]
    r = C.run(app, ok_plan(app, "checklist.set", item=iid, entry=row["text"][:6], done=True))
    assert next(x for x in app.state()["checklists"][iid]["items"] if x["id"] == row["id"])["done"] is True
    C.undo(app, r["undo"])
    assert next(x for x in app.state()["checklists"][iid]["items"] if x["id"] == row["id"])["done"] is False
    r = C.run(app, ok_plan(app, "checklist.add", item=iid, text="표지에 학번 쓰기"))
    assert any(x["text"] == "표지에 학번 쓰기" for x in app.state()["checklists"][iid]["items"])
    C.undo(app, r["undo"])
    assert not any(x["text"] == "표지에 학번 쓰기" for x in app.state()["checklists"][iid]["items"])

    if (app.state()["coach"] or {}).get("briefing"):                   # 데모 날짜에 따라 이번 주 브리핑이 있을 때
        r = C.run(app, ok_plan(app, "coach.dismiss", card="브리핑 카드"))
        assert app.state()["coach"]["briefing"] is None
        C.undo(app, r["undo"])
        assert app.state()["coach"]["briefing"]

    r = C.run(app, ok_plan(app, "app.mascot", choice="original"))
    assert app.state()["mascot"]["choice"] == "original"
    C.undo(app, r["undo"])
    assert app.state()["mascot"]["choice"] == "auto"

    target = tmp_path / "받은 자료"
    r = C.run(app, ok_plan(app, "materials.set_folder", folder=str(target)))
    assert app.state()["materials"]["folder"] == str(target.resolve())
    C.undo(app, r["undo"])
    assert not app.store.data["settings"].get("download_dir")
    assert "이미" in C.plan(app, "materials.set_folder", {"folder": None})["error"]


def test_undo_conflict_is_409_and_changes_nothing(app):
    a, b = _lab4(app.store)
    C.run(app, ok_plan(app, "items.set_decision", item=a["id"], decision="approved"))
    r = C.run(app, ok_plan(app, "items.set_decision", item=b["id"], decision="approved"))   # 같은 과제의 a는 확인 전으로
    assert set(r["undo"]["keys"]) == {f"user/{a['id']}", f"user/{b['id']}"}
    assert _view(app, a["id"])["decision"] == "pending"
    actions._call(app, f"/api/item/{b['id']}", {"decision": "rejected"})    # 그 뒤 사용자가 화면에서 직접 바꿈
    with pytest.raises(ApiError) as e:
        C.undo(app, r["undo"])
    assert e.value.status == 409
    assert _view(app, a["id"])["decision"] == "pending" and _view(app, b["id"])["decision"] == "rejected"

    r = C.run(app, ok_plan(app, "notify.enabled", value=False))
    app.store.data["settings"]["notify"] = True
    app.store.save()
    with pytest.raises(ApiError) as e:
        C.undo(app, r["undo"])
    assert e.value.status == 409 and app.store.data["settings"]["notify"] is True


def test_run_replans_against_current_state(app):
    p = ok_plan(app, "settings.review_reminder", value=True)
    app.store.data["settings"]["review_reminder"] = True                   # 계획과 실행 사이에 바뀜
    with pytest.raises(ValueError, match="이미"):
        C.run(app, p)
    with pytest.raises(ValueError):
        C.run(app, {"capability": "no.such", "params": {}})


def test_refuse_tier_never_runs(app):
    saved = json.dumps(app.store.data["settings"], sort_keys=True)
    for cap_id, params in (("ai.enable", {}), ("ai.daily_limit", {}), ("ai.provider", {}), ("account.logout", {}),
                           ("account.wipe_data", {}), ("account.student_id", {}), ("calendar.disconnect", {}),
                           ("calendar.connect", {})):
        p = C.plan(app, cap_id, params)
        assert not p["ok"] and p["tier"] == "refuse" and p["pane"] in actions.PANES and "직접" in p["error"], cap_id
        with pytest.raises(ValueError, match="직접"):
            C.run(app, {"capability": cap_id, "params": params})
    assert json.dumps(app.store.data["settings"], sort_keys=True) == saved
    assert "[직접]" in C.catalog_text() and "ai.enable" in C.catalog_text("ai")


def test_external_and_one_way_capabilities_are_not_undoable(gapp, app):
    p = ok_plan(gapp, "calendar.push")
    assert p["external"] and not p["undoable"]
    r = C.run(gapp, p)
    assert r["undo"] is None and "반영했어요" in r["message"]
    assert gapp.state()["google"]["last_push"]
    with pytest.raises(ValueError, match="되돌릴 수 없는"):
        C.undo(gapp, {"cap": "calendar.push", "keys": {}})
    r = C.run(app, ok_plan(app, "app.sync"))
    assert r["undo"] is None and "동기화" in r["message"]
    r = C.run(app, ok_plan(app, "app.update_check"))
    assert r["undo"] is None and "v99.0.0" in r["message"]


def test_side_state_background_undo_and_conflict(app):
    r = C.run(app, ok_plan(app, "app.background", enabled=True, interval_hours=6))
    assert app.scheduler.run.registered and "<Interval>PT6H</Interval>" in app.scheduler.run.xml
    assert r["undo"]["side"]["after"] == {"enabled": True, "interval_hours": 6} and "6시간" in r["message"]
    C.undo(app, r["undo"])
    assert not app.scheduler.run.registered and app.state()["background"]["enabled"] is False
    r = C.run(app, ok_plan(app, "app.background", enabled=True, interval_hours=12))
    actions._call(app, "/api/background", {"enabled": False})              # 그 뒤 화면에서 직접 끔
    with pytest.raises(ApiError) as e:
        C.undo(app, r["undo"])
    assert e.value.status == 409 and not app.scheduler.run.registered


def test_emits_changed_once_per_run_and_on_undo(app, monkeypatch):
    seen = []
    monkeypatch.setitem(router.listeners, "changed", list(router.listeners.get("changed", [])) + [lambda a, **k: seen.append(1)])
    r = C.run(app, ok_plan(app, "settings.include_canvas", value=False))   # 경로가 이미 알림 → 다시 알리지 않음
    assert len(seen) == 1
    C.undo(app, r["undo"])                                                 # 되돌리기는 경로를 거치지 않으므로 직접 알림
    assert len(seen) == 2 and app.store.data["settings"]["include_canvas"] is True


def test_undo_rejects_bad_records(app):
    for rec in (None, {"cap": "no.such"}, {"cap": "settings.weekly_summary", "keys": {"x": {"path": ["cache"], "before": 1, "after": 2}}},
                {"cap": "settings.weekly_summary", "keys": {"x": {"path": "settings"}}}):
        with pytest.raises(ValueError):
            C.undo(app, rec)
    assert C.undo(app, {"cap": "settings.weekly_summary", "keys": {}})["ok"]


# ---------------------------------------------------------------- AI용 목록

def test_catalog_text_overview_and_areas_fit_budget():
    full = C.catalog_text()
    assert len(full) <= 3000 and "새로" not in full
    for area in ("settings", "items", "courses", "calendar", "plan", "app", "account", "ai"):
        assert f"- {area}(" in full, area
    for area in C.AREAS:
        text = C.catalog_text(area)
        assert len(text) <= 3000, area
        mine = [c for c in C.REGISTRY.values() if c.area == area]
        if len("\n".join(C._cap_line(c, detail=True) for c in mine)) < 2700:
            assert all(c.id in text for c in mine), area
    items = C.catalog_text("items")
    assert "items.set_due {item: 항목, course?: 과목, due: YYYY-MM-DDTHH:MM|null}" in items
    assert "[확인 카드]" in C.catalog_text("app") and "(되돌릴 수 없음)" in C.catalog_text("app")
    small = C.catalog_text("items", budget=260)
    assert len(small) <= 260 and "더" in small
    assert len(C.catalog_text(budget=150)) <= 150
    assert "모르는 영역" in C.catalog_text("nope")
    cat = C.catalog("items")
    assert cat and all(set(x) >= {"id", "area", "title", "tier", "params"} for x in cat)
