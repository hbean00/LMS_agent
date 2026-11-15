"""말로 시키면 해석해서 직접 한다(계약 28-3): 루프의 act·list_capabilities, 바로 하기(direct + 시키는 말 + 바꾸기 하나 → 잠금 밖에서
capabilities.run → auto_applied·[되돌리기]), 확인 카드(confirm → [실행]), 거절(refuse → 이유 + [설정 열기]), plan이 막히면 그 이유를
다음 차례 도구 결과로 AI에게, 단계 이름, 가짜 AI(데모)의 흐름.
능력 목록(capabilities)은 가짜로 바꿔 끼운다(H1·H2의 진짜 능력과 상관없이 에이전트 쪽 모양만 본다). 가짜 데이터만 쓴다.
시각은 같은 순간인지로 비교한다(CI는 UTC)."""
import copy
import json
import subprocess
from datetime import datetime, time, timedelta

import pytest

from lms_agent import agent, agent_cli, agent_loop, capabilities, core, mock_ai
from lms_agent.api import ApiError
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local

TZ = local_tz()
T = now_local().date()
SECRET = "canvas-test-token-act-0123456789abcdefABCDEF"


def z(days, h=10):
    return iso(datetime.combine(T + timedelta(days=days), time(h, 0), tzinfo=TZ))


def make_cache():
    return {"fetched_at": z(0),
            "courses": [{"id": "401", "name": "디지털논리", "course_code": "EE401", "syllabus_body": None},
                        {"id": "403", "name": "데이터구조", "course_code": "CS403", "syllabus_body": None},
                        {"id": "404", "name": "데이터베이스", "course_code": "CS404", "syllabus_body": None}],
            "assignments": [], "announcements": [], "conversations": [], "boards": [], "pdf_texts": {}, "errors": []}


def make_store(path):
    s = Store(path)
    s.data["cache"] = make_cache()
    core.rebuild(s)
    s.save()
    return s


# ---------------------------------------------------------------- 가짜 능력 목록(capabilities 모양 그대로 — 계약 28-1)

class FakeCaps:
    """plan·run·undo·catalog_text를 흉내 낸다. run·undo는 경로 처리기처럼 **스스로 파일 잠금**을 잡는다 — 부르는 쪽이 잠금을 쥐고
    있으면 여기서 멈춰 실패하므로, 잠금 밖에서 부르는지 확인된다. app.lock도 비어 있어야 한다(동기화 잠금을 쥔 채 부르지 않음)."""

    def __init__(self):
        C = capabilities.Capability
        self.reg = {c.id: c for c in [
            C("grades.set_dist", "grades", "학점 비율 바꾸기", ("POST", "/api/grades/{course_id}"),
              {"course": {"type": "str", "required": True, "desc": "과목", "resolve": "course"},
               "letters": {"type": "dict", "required": True, "desc": "글자별 %"}}, "direct"),
            C("calendar.google_push", "calendar", "구글 캘린더에 지금 반영", ("POST", "/api/google/push"), {}, "direct",
              undoable=False, external=True),
            C("app.update_apply", "app", "지금 업데이트", ("POST", "/api/update/apply"), {}, "confirm", undoable=False),
            C("ai.toggle", "ai", "AI 켜기·끄기", None, {"value": {"type": "bool", "required": True}}, "refuse",
              refuse="AI 켜기·끄기는 에이전트 자신을 끄는 일이라 하지 않아요 — 설정 → AI 도움에서 직접", pane="ai"),
        ]}
        self.plans, self.runs, self.undos = [], [], []
        self.fail_run = None

    def install(self, mp):
        mp.setattr(capabilities, "REGISTRY", self.reg)
        mp.setattr(capabilities, "plan", self.plan)
        mp.setattr(capabilities, "run", self.run)
        mp.setattr(capabilities, "undo", self.undo)
        mp.setattr(capabilities, "catalog_text", self.catalog_text)
        return self

    def catalog_text(self, area=None, budget=3000):
        rows = [c for c in self.reg.values() if area is None or c.area == area]
        if area is None:
            return "\n".join(f"- {c.id} [{c.tier}] {c.title}" for c in rows)[:budget]
        return "\n".join(f"- {c.id} [{c.tier}] {c.title} params: {', '.join(c.params) or '없음'}" for c in rows)[:budget]

    def plan(self, app, cap_id, params):
        self.plans.append({"cap": cap_id, "app": type(app).__name__, "params": copy.deepcopy(params),
                           "token": getattr(app, "token", None)})
        cap = self.reg.get(cap_id)
        out = {"ok": False, "capability": cap_id, "title": None, "area": None, "tier": None, "params": {}, "summary": "",
               "changes": [], "external": False, "undoable": False, "error": None, "pane": None}
        if cap is None:
            return {**out, "error": f"모르는 능력이에요({cap_id})"}
        out.update(title=cap.title, area=cap.area, tier=cap.tier, external=cap.external, undoable=cap.undoable, pane=cap.pane)
        if cap.tier == "refuse":
            return {**out, "error": cap.refuse}
        if cap_id != "grades.set_dist":
            return {**out, "ok": True, "params": dict(params or {}), "summary": cap.title,
                    "changes": [{"label": cap.title, "before": None, "after": "지금"}]}
        name = str((params or {}).get("course") or "")
        hits = [c for c in app.store.data["cache"]["courses"] if name and (str(c["id"]) == name or name in c["name"])]
        if len(hits) != 1:
            return {**out, "error": ("과목이 여럿이에요: " + ", ".join(c["name"] for c in hits)) if hits else "그런 과목이 없어요",
                    "candidates": [{"id": str(c["id"]), "label": c["name"]} for c in hits]}
        letters = (params or {}).get("letters")
        if not isinstance(letters, dict) or not letters:
            return {**out, "error": "글자별 비율(letters)이 필요해요"}
        cid = str(hits[0]["id"])
        cur = (app.store.data.get("test_dist") or {}).get(cid) or {"A": 30, "B": 40}
        return {**out, "ok": True, "params": {"course_id": cid, "letters": dict(letters)},
                "summary": f"[{hits[0]['name']}] 학점 비율 " + " · ".join(f"{g} {v:g}%" for g, v in letters.items()),
                "changes": [{"label": f"학점 비율 {g}", "before": f"{cur.get(g, 0):g}%", "after": f"{v:g}%"}
                            for g, v in letters.items()]}

    def _free(self, app):
        assert app.lock.acquire(blocking=False), "동기화 잠금(app.lock)을 쥔 채로 불렀어요"
        app.lock.release()

    def run(self, app, planned):
        self._free(app)
        assert planned["ok"] and planned["tier"] != "refuse"
        self.runs.append(planned["capability"])
        if self.fail_run is not None:
            raise self.fail_run
        if planned["capability"] != "grades.set_dist":
            return {"ok": True, "message": f"{planned['title']} 했어요", "undo": None}
        cid, letters = planned["params"]["course_id"], planned["params"]["letters"]
        with app.store.locked():     # 경로 처리기처럼 스스로 잠금 — 부르는 쪽이 잡고 있으면 멈춘다
            app.store.reload_if_changed()
            box = app.store.data.setdefault("test_dist", {})
            before = copy.deepcopy(box.get(cid))
            box[cid] = dict(letters)
            app.store.write()
        app.emit("changed")
        return {"ok": True, "message": "학점 비율을 바꿨어요",
                "undo": {"cap": "grades.set_dist", "cid": cid, "before": before, "after": dict(letters)}}

    def undo(self, app, rec):
        self._free(app)
        self.undos.append(rec["cap"])
        with app.store.locked():
            app.store.reload_if_changed()
            box = app.store.data.setdefault("test_dist", {})
            if box.get(rec["cid"]) != rec["after"]:
                raise ApiError(409, "그 사이 직접 바꿔서 되돌리지 않았어요")
            if rec["before"] is None:
                box.pop(rec["cid"], None)
            else:
                box[rec["cid"]] = rec["before"]
            app.store.write()
        app.emit("changed")
        return {"ok": True, "message": "되돌렸어요"}


@pytest.fixture
def caps(monkeypatch):
    return FakeCaps().install(monkeypatch)


# ---------------------------------------------------------------- 가짜 AI 실행기·앱

def reply(body) -> str:
    return json.dumps({"type": "result", "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)


def script(*bodies):
    seen = []

    def run(args, input, timeout, cwd):
        seen.append(input)
        b = bodies[min(len(seen), len(bodies)) - 1]
        return subprocess.CompletedProcess(args, 0, reply(b(input) if callable(b) else b), "")
    run.seen = seen
    return run


def done(say, calls=()):
    return {"say": say, "calls": list(calls), "done": True}


def act(cap, auto=None, **params):
    args = {"capability": cap, "params": params, "reason": "학생이 시킨 일"}
    if auto is not None:
        args["auto"] = auto
    return {"tool": "act", "args": args}


def make_app(tmp_path, runner):
    from lms_agent.server import App
    s = make_store(tmp_path / "state.json")
    s.data["settings"]["ai_cli"] = True
    s.save()
    app = App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": SECRET, "user_name": "김가짜"},
              demo=True, ai_runner=runner, ai_command="claude")
    app.events = []
    real = app.emit
    app.emit = lambda e, **k: (app.events.append(e), real(e, **k))
    return app


def ask_app(app, msg):
    from lms_agent.features import agent as fa
    job = fa.ask(app, {"message": msg})["job"]
    assert fa.wait(app, job)
    return next(t for t in app.state()["agent"]["threads"] if t["id"] == job)


def _http(url, path, token, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-LMS-Agent": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


DIST_SAY = "디지털논리 학점 비율을 A 40%로 바꿨어요 — [되돌리기]로 되돌릴 수 있어요."


# ---------------------------------------------------------------- 바로 하기 + 되돌리기(HTTP)

def test_direct_act_runs_outside_locks_then_undo_over_http(tmp_path, caps):
    from lms_agent.features import agent as fa
    from lms_agent.server import serve_in_thread, start_server
    run = script(done(DIST_SAY, [act("grades.set_dist", course="디지털논리", letters={"A": 40})]))
    app = make_app(tmp_path, run)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, out = _http(url, "/api/agent/ask", app.token, {"message": "디지털논리 학점 비율 A 40%로 바꿔줘"})
        assert code == 200 and fa.wait(app, out["job"])
        st = app.state()
        th = st["agent"]["threads"][-1]
        p = th["proposals"][0]
        assert (th["status"], th["kind"], th["by"], th["progress"]) == ("done", "act", "ai", None)
        assert (p["type"], p["capability"], p["tier"], p["decided_by"], p["by"]) == ("act", "grades.set_dist", "direct", "user", "ai")
        assert (p["auto"], p["auto_applied"], p["applied"], p["can_apply"], p["undoable"]) == (True, True, True, False, True)
        assert p["params"] == {"course_id": "401", "letters": {"A": 40}} and p["course_short"] == "디지털논리"
        assert p["changes"] == [{"label": "학점 비율 A", "before": "30%", "after": "40%"}]
        assert p["result"] == {"ok": True, "message": "학점 비율을 바꿨어요"} and p["rejected_reason"] is None
        assert app.store.data["test_dist"] == {"401": {"A": 40}} and caps.runs == ["grades.set_dist"]
        assert app.events.count("changed") == 1                                   # run이 알림(두 번 알리지 않음)
        assert [(s["tool"], s["label"], s["ok"], s["note"]) for s in th["steps"]] == [
            ("act", "직접 하기 · 학점 비율 바꾸기", True, "바로 할 수 있음")]
        dump = json.dumps(st, ensure_ascii=False)
        assert '"_undo"' not in dump and '"_raw_params"' not in dump and '"_auto"' not in dump and SECRET not in dump
        # 루프의 plan은 작업 사본을 읽는 PlanApp(앱의 속성은 그대로), [실행] 때는 진짜 앱으로 다시 plan
        assert [x["app"] for x in caps.plans] == ["PlanApp", "App"] and caps.plans[0]["token"] == app.token
        assert caps.plans[1]["params"] == {"course": "디지털논리", "letters": {"A": 40}}   # AI가 낸 값 그대로 다시 검사
        code, state = _http(url, "/api/agent/undo", app.token, {"thread": th["id"], "proposal": 0})
        assert code == 200 and "401" not in app.store.data["test_dist"]
        q = state["agent"]["threads"][-1]["proposals"][0]
        assert (q["applied"], q["can_apply"], bool(q["undone_at"])) == (False, True, True)
        assert app.events.count("changed") == 2 and caps.undos == ["grades.set_dist"]
        code, err = _http(url, "/api/agent/undo", app.token, {"thread": th["id"], "proposal": 0})
        assert code == 409
        code, _ = _http(url, "/api/agent/apply", app.token, {"thread": th["id"], "proposal": 0})   # 다시 [실행]
        assert code == 200 and app.store.data["test_dist"]["401"] == {"A": 40}
        app.store.data["test_dist"]["401"] = {"A": 35}                            # 그 사이 직접 바꿈 → 되돌리기 409
        app.store.save()
        code, err = _http(url, "/api/agent/undo", app.token, {"thread": th["id"], "proposal": 0})
        assert code == 409 and "직접" in err["error"]
        assert app.store.data["test_dist"]["401"] == {"A": 35}
    finally:
        httpd.shutdown()


def test_questions_and_auto_false_become_cards_then_execute(tmp_path, caps):
    from lms_agent.features import agent as fa
    say = "디지털논리 학점 비율을 A 40%로 바꾸는 카드를 준비했어요 — 맞으면 [실행]을 눌러 주세요."
    run = script(done(say, [act("grades.set_dist", course="디지털논리", letters={"A": 40})]))
    app = make_app(tmp_path, run)
    th = ask_app(app, "디지털논리 학점 비율 A 40%로 바꿔도 될까?")                   # 묻는 말 → 카드만
    p = th["proposals"][0]
    assert (p["auto"], p["auto_applied"], p["applied"], p["can_apply"]) == (False, False, False, True)
    assert "test_dist" not in app.store.data and caps.runs == []
    assert any("[실행]" in n for n in p["notes"])
    state = fa.apply_proposal(app, {"thread": th["id"], "proposal": 0})           # [실행]
    q = next(t for t in state["agent"]["threads"] if t["id"] == th["id"])["proposals"][0]
    assert (q["applied"], q["auto_applied"], q["result"]["ok"]) == (True, False, True)
    assert app.store.data["test_dist"]["401"] == {"A": 40}
    # 애매한 지시를 구체적인 값으로 바꾼 것(auto: false)은 시키는 말이어도 확인 카드
    run2 = script(done(say, [act("grades.set_dist", auto=False, course="데이터베이스", letters={"A": 40, "B": 40})]))
    app.ai_runner = run2
    th = ask_app(app, "데이터베이스 학점 잘 주신대 비율 조정도 해줘")
    p = th["proposals"][0]
    assert (p["tier"], p["auto"], p["applied"], p["can_apply"]) == ("direct", False, False, True)
    assert [c["after"] for c in p["changes"]] == ["40%", "40%"] and "404" not in app.store.data["test_dist"]
    assert any("알아들었어요" in n for n in p["notes"])


def test_confirm_tier_needs_execute_and_is_not_undoable(tmp_path, caps):
    from lms_agent.features import agent as fa
    run = script(done("업데이트는 되돌리기 어려워서 카드로 준비했어요 — [실행]을 누르면 시작해요.", [act("app.update_apply")]))
    app = make_app(tmp_path, run)
    th = ask_app(app, "지금 업데이트 설치해줘")                                    # 시키는 말이어도 confirm은 바로 하지 않음
    p = th["proposals"][0]
    assert (p["tier"], p["auto"], p["applied"], p["can_apply"], p["undoable"]) == ("confirm", False, False, True, False)
    assert caps.runs == [] and any("되돌리기 어려운" in n for n in p["notes"])
    fa.apply_proposal(app, {"thread": th["id"], "proposal": 0})
    assert caps.runs == ["app.update_apply"]
    with pytest.raises(ApiError) as e:
        fa.undo_proposal(app, {"thread": th["id"], "proposal": 0})
    assert e.value.status == 409
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": th["id"], "proposal": 0})                 # 두 번 실행하지 않음
    assert e.value.status == 409 and caps.runs == ["app.update_apply"]


def test_refuse_tier_and_harness_refusal_cards(tmp_path, caps):
    from lms_agent.features import agent as fa
    say = "AI 켜기·끄기는 제가 하지 않아요 — 설정 → AI 도움에서 직접 바꿔 주세요."
    run = script(done(say, [act("ai.toggle", value=False)]))                       # AI가 거절 등급에 act를 냄
    app = make_app(tmp_path, run)
    th = ask_app(app, "AI 꺼줘")
    p = th["proposals"][0]
    assert (th["kind"], p["type"], p["tier"], p["pane"], p["can_apply"], p["rejected_reason"]) == (
        "act", "act", "refuse", "ai", False, None)
    assert "하지 않아요" in p["refuse"] and app.store.data["settings"]["ai_cli"] is True
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": th["id"], "proposal": 0})
    assert e.value.status == 400 and caps.runs == []
    # AI가 act 없이 이유만 말해도 하네스가 [설정 열기] 거절 카드를 붙인다
    for msg, pane in (("AI 꺼줘", "ai"), ("하루 AI 사용 한도 300번으로 늘려줘", "ai"), ("학번 바꿔줘", "grades"),
                      ("다른 AI 실행기로 바꿔줘", "ai"), ("로그아웃해줘", "account")):
        app.ai_runner = script(done("그건 제가 하지 않는 일이에요. 설정에서 직접 바꿔 주세요."))
        th = ask_app(app, msg)
        assert len(th["proposals"]) == 1, msg
        p = th["proposals"][0]
        assert (p["type"], p["tier"], p["pane"], p["can_apply"]) == ("act", "refuse", pane, False), msg
        assert "직접" in p["refuse"] and th["kind"] == "act"
    app.ai_runner = script(done("AI 도움은 지금 켜져 있어요."))                    # 묻는 말이면 카드 없음
    th = ask_app(app, "AI 켜져 있어?")
    assert th["proposals"] == [] and caps.runs == []
    assert agent.refusal_for(None, "기말 범위 알려줘") is None


def test_ambiguous_plan_error_is_fed_back_as_tool_result(tmp_path, caps):
    def round2(inp):
        tools = inp.split(agent_cli.AGENT_TOOLS_MARK, 1)[-1]
        assert "거절 / rejected: 과목이 여럿이에요: 데이터구조, 데이터베이스" in tools
        assert "후보/candidates: 데이터구조 (id 403); 데이터베이스 (id 404)" in tools
        return done("데이터베이스 학점 비율을 A 40%로 바꿨어요.", [act("grades.set_dist", course="데이터베이스", letters={"A": 40})])
    run = script(done("바꿨어요.", [act("grades.set_dist", course="데이터", letters={"A": 40})]), round2)
    app = make_app(tmp_path, run)
    th = ask_app(app, "데이터 과목 학점 비율 A 40%로 바꿔줘")
    assert len(run.seen) == 2 and th["attempts"] == 2                               # 다시 묻기를 쓰지 않고 다음 차례로
    assert [(s["round"], s["tool"], s["label"], s["ok"]) for s in th["steps"]] == [
        (1, "act", "직접 하기", False), (2, "act", "직접 하기 · 학점 비율 바꾸기", True)]
    assert th["steps"][0]["note"].startswith("거절: 과목이 여럿이에요")
    assert [p["params"]["course_id"] for p in th["proposals"]] == ["404"]           # 막힌 제안은 남기지 않음
    assert th["proposals"][0]["auto_applied"] and app.store.data["test_dist"] == {"404": {"A": 40}}


def test_unknown_capability_on_last_round_is_a_rejected_card(tmp_path, caps):
    run = script({"say": "", "calls": [{"tool": "get_settings", "args": {}}], "done": False},
                 {"say": "", "calls": [{"tool": "list_capabilities", "args": {"area": "grades"}}], "done": False},
                 done("그건 할 수 없어요.", [act("grades.nope")]))
    app = make_app(tmp_path, run)
    th = ask_app(app, "디지털논리 성적 뭔가 바꿔줘")
    p = th["proposals"][0]
    assert (p["type"], p["can_apply"], p["applied"]) == ("act", False, False) and "모르는 능력" in p["rejected_reason"]
    assert [s["label"] for s in th["steps"]] == ["설정 보기", "할 수 있는 일 보기", "직접 하기"]


def test_list_capabilities_tool_and_overview_in_first_input(tmp_path, caps):
    s = make_store(tmp_path / "s.json")
    run = script({"say": "", "calls": [{"tool": "list_capabilities", "args": {"area": "grades"}},
                                       {"tool": "list_capabilities", "args": {"area": "nope"}}], "done": False},
                 done("성적에서는 학점 비율을 바꿀 수 있어요."))
    r = agent.answer(s, "성적에서 뭘 바꿀 수 있어?", ai_enabled=True, runner=run, command="claude")
    first, second = run.seen
    assert agent_cli.CAPS_MARK in first and "- calendar.google_push [direct] 구글 캘린더에 지금 반영" in first
    assert "act {capability, params" in first or "act" in first
    assert "- grades.set_dist [direct] 학점 비율 바꾸기 params: course, letters" in second
    assert "모르는 영역이에요" in second
    assert [(x["tool"], x["label"], x["ok"]) for x in r["steps"]] == [
        ("list_capabilities", "할 수 있는 일 보기", True), ("list_capabilities", "할 수 있는 일 보기", False)]
    assert r["steps"][0]["note"] == "1개" and r["kind"] == "question" and r["proposals"] == []
    # 앱이 없어도(테스트·CLI) act 제안은 만들어진다 — plan은 store만 가진 PlanApp을 읽는다
    run = script(done("디지털논리 학점 비율을 바꾸는 카드를 준비했어요.", [act("grades.set_dist", course="디지털논리", letters={"A": 40})]))
    r = agent.answer(s, "디지털논리 학점 비율 A 40%로 할까?", ai_enabled=True, runner=run, command="claude")
    assert r["proposals"][0]["tier"] == "direct" and caps.plans[-1]["app"] == "PlanApp" and caps.plans[-1]["token"] is None


def test_auto_failure_keeps_card_and_says_so(tmp_path, caps):
    from lms_agent.features import agent as fa
    caps.fail_run = ApiError(400, "구글 캘린더가 연결돼 있지 않아요. 설정 → 구글 캘린더에서 먼저 연결해 주세요")
    run = script(done("구글 캘린더에 반영했어요.", [act("calendar.google_push")]))
    app = make_app(tmp_path, run)
    th = ask_app(app, "구글 캘린더에 반영해줘")
    p = th["proposals"][0]
    assert (p["auto"], p["auto_applied"], p["applied"], p["can_apply"]) == (True, False, False, True)
    assert p["result"]["ok"] is False and "연결" in p["result"]["message"]
    assert "바로 하지 못했어요" in th["answer"] and th["status"] == "done"
    assert any("되돌리기" in n for n in p["notes"])                              # 밖에 반영 → 되돌리기 없음 표시
    caps.fail_run = None
    fa.apply_proposal(app, {"thread": th["id"], "proposal": 0})                     # 연결한 뒤 [실행]
    q = next(t for t in app.state()["agent"]["threads"] if t["id"] == th["id"])["proposals"][0]
    assert (q["applied"], q["undoable"], q["result"]["ok"]) == (True, False, True)


def test_is_command_and_auto_targets_for_act():
    for msg in ("구글 캘린더에 반영해줘", "구글 캘린더 일정 반영해줘", "ASIC 학점 비율 A 40%로 바꿔줘", "주간 요약 켜줘", "동기화해줘"):
        assert agent_loop.is_command(msg), msg
    for msg in ("A 40%로 바꿔도 될까?", "구글 캘린더에 반영할 수 있어?", "학점 비율이 뭐야", "반영 됐나요"):
        assert not agent_loop.is_command(msg), msg
    direct = {"type": "act", "tier": "direct", "_auto": True, "rejected_reason": None}
    confirm = {"type": "act", "tier": "confirm", "_auto": False, "rejected_reason": None}
    assert agent_loop.auto_targets(None, "바꿔줘", ("", ""), [dict(direct)])[0] == {0}
    assert agent_loop.auto_targets(None, "바꿔도 돼?", ("", ""), [dict(direct)])[0] == set()
    assert agent_loop.auto_targets(None, "바꿔줘", ("", ""), [dict(direct), dict(direct)])[0] == set()   # 바꾸기 둘
    assert agent_loop.auto_targets(None, "바꿔줘", ("", ""), [dict(direct), dict(confirm)])[0] == set()
    refuse = {"type": "act", "tier": "refuse", "refuse": "x", "rejected_reason": None}
    assert agent_loop.auto_targets(None, "바꿔줘", ("", ""), [dict(direct), refuse])[0] == {0}   # 거절 카드는 바꾸기가 아님


# ---------------------------------------------------------------- 가짜 AI(데모)의 흐름

def test_mock_ai_demo_flows(tmp_path, caps):
    app = make_app(tmp_path, mock_ai.runner)
    th = ask_app(app, "디지털논리 학점 비율 A 40%로 바꿔줘")                         # list_capabilities → act(grades.set_dist) 바로
    assert [(s["tool"], s["label"]) for s in th["steps"]] == [("list_capabilities", "할 수 있는 일 보기"),
                                                              ("act", "직접 하기 · 학점 비율 바꾸기")]
    p = th["proposals"][0]
    assert (th["kind"], p["capability"], p["auto_applied"], p["applied"]) == ("act", "grades.set_dist", True, True)
    assert app.store.data["test_dist"]["401"] == {"A": 40.0}
    th = ask_app(app, "구글 캘린더 일정 반영해줘")                                 # act(구글 반영) 바로, 되돌리기 없음
    p = th["proposals"][0]
    assert (p["capability"], p["auto_applied"], p["undoable"], p["external"]) == ("calendar.google_push", True, False, True)
    assert caps.runs == ["grades.set_dist", "calendar.google_push"]
    th = ask_app(app, "AI 꺼줘")                                                    # 도구 없이 이유 + 하네스의 [설정 열기]
    p = th["proposals"][0]
    assert (th["by"], th["steps"], p["tier"], p["pane"]) == ("ai", [], "refuse", "ai")
    assert "하지 않아요" in th["answer"] and app.store.data["settings"]["ai_cli"] is True
    th = ask_app(app, "컴퓨터 켤 때 자동 실행해줘")                                # 지금까지의 앱 동작 카드는 그대로
    assert th["proposals"][0]["type"] == "action" and th["proposals"][0]["action"] == "autostart"
    assert len(caps.runs) == 2


# ---------------------------------------------------------------- 진짜 능력 목록(H1·H2)과 데모 과목으로 — 화면과 같은 경로·공통 되돌리기

@pytest.fixture()
def demo_app(tmp_path):
    from lms_agent.canvas import Canvas
    from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
    from lms_agent.server import App
    srv, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    store.data["settings"]["ai_cli"] = True
    store.save()
    app = App(store, tmp_path, cfg={"canvas_url": base, "token": DEMO_TOKEN}, demo=True, ai_runner=mock_ai.runner,
              ai_command=mock_ai.DEMO_COMMAND)
    app.events = []
    real = app.emit
    app.emit = lambda e, **k: (app.events.append(e), real(e, **k))
    yield app
    srv.shutdown()


def test_demo_flows_with_the_real_capabilities(demo_app):
    from lms_agent.features import agent as fa
    from lms_agent import distribution as dm
    app = demo_app
    before = copy.deepcopy((app.store.data.get("grades") or {}).get("101"))
    # 데모 ASIC은 강의 글의 A+·A0 40%라 눈에 보이게 바뀌는 값으로(리드 — H2 보고)
    th = ask_app(app, "ASIC 학점 비율 A 50%로 바꿔줘")                              # list_capabilities → grades.set_dist 바로
    assert [s["tool"] for s in th["steps"]] == ["list_capabilities", "act"], th["steps"]
    p = th["proposals"][0]
    assert (p["capability"], p["tier"], p["auto_applied"], p["applied"], p["undoable"]) == (
        "grades.set_dist", "direct", True, True, True), (p.get("rejected_reason"), p.get("notes"))
    assert p["params"]["course"] == "101" and p["params"]["dist"] == {"A": 50.0} and th["status"] == "done"
    assert (p["course_id"], p["course_short"]) == ("101", core.short_course_name(app.store.data["cache"]["courses"][0]["name"]))
    assert {"label": "학점 비율 A", "before": "40%", "after": "50%"} in p["changes"]
    eff = dm.effective(app.store.data["grades"]["101"])
    assert abs(eff["dist"]["A+"] + eff["dist"]["A0"] - 50) < 0.02
    fa.undo_proposal(app, {"thread": th["id"], "proposal": 0})                     # 공통 되돌리기 → 실행 전 설정
    assert (app.store.data.get("grades") or {}).get("101") == before
    th = ask_app(app, "ASIC 절대평가 기준 A+ 95 A0 90 B+ 85 B0 80으로 바꿔줘")        # 직접 넣는 절대평가 기준표(리드 — 28-2)
    p = th["proposals"][0]
    assert (p["capability"], p["auto_applied"], p["params"].get("kind")) == ("grades.set_dist", True, "absolute"), \
        (p.get("rejected_reason"), p.get("params"))
    eff = dm.effective(app.store.data["grades"]["101"])
    assert (eff["by"], eff["kind"], eff["cutoffs"]["A+"], eff["cutoffs"]["B0"]) == ("user", "absolute", 95.0, 80.0)
    gc = next(c for c in app.state()["grades"]["courses"] if str(c["course_id"]) == "101")
    assert (gc["letter_basis"], gc["dist_kind"], gc["cutoffs_used"]["A0"]) == ("user", "absolute", 90.0)   # 화면이 읽는 칸
    fa.undo_proposal(app, {"thread": th["id"], "proposal": 0})
    assert (app.store.data.get("grades") or {}).get("101") == before
    th = ask_app(app, "AI 꺼줘")
    p = th["proposals"][0]
    assert (p["tier"], p["pane"], p["can_apply"]) == ("refuse", "ai", False) and app.store.data["settings"]["ai_cli"]
    th = ask_app(app, "구글 캘린더에 반영해줘")                                     # 구글 연결 안 됨 → 막힌 이유로 답
    assert any(s["tool"] == "act" for s in th["steps"]) and th["status"] == "done"
    assert all(not q.get("applied") for q in th["proposals"])


def test_explicit_numbers_override_ai_caution():
    """학생이 값을 직접 말했으면 AI가 auto false를 내도 바로 할 후보(리드 — 실제 AI가 'A 50%로 바꿔줘'를 카드로 낸 것)."""
    from lms_agent.agent_loop import _explicit_numbers
    assert _explicit_numbers({"course": "ASIC", "dist": {"A": 50}}, "ASIC 학점 비율 A 50%로 바꿔줘")
    assert _explicit_numbers({"course": "1", "component": "Report", "weight": 30}, "Report 비중 30%로 해줘")
    assert not _explicit_numbers({"course": "ASIC", "dist": {"A": 45, "B": 45, "C 이하": 10}}, "ASIC 학점 잘 주신대 비율 조정해줘")
    assert not _explicit_numbers({"course": "ASIC", "dist": {"A": 50, "B": 40}}, "A 50%로 바꿔줘")   # B 40은 AI가 지어냄
    assert not _explicit_numbers({"key": "weekly_summary", "value": False}, "주간 요약 꺼줘")       # 숫자 없음 → AI 판단대로
