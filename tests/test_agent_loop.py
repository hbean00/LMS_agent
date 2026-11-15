"""에이전트 v2 — 도구를 쓰는 AI 루프(계약 26-1·26-2·26-4): 바로 답, 읽기 도구 → 인용 답(그대로가 아닌 인용은 다시 묻기),
3차례 한도, 막히거나 실패하면 규칙 갈래, 바로 반영 조건(시키는 말·대상 하나), 바로 반영과 되돌리기(HTTP), 진행 표시·단계,
구글 개인 일정은 AI에게 보내지 않음, 하루 한도 설정, 가짜 AI(데모)의 흐름.
가짜 데이터만 쓴다(과목·문장·이름·토큰 모두 지어낸 것). 시각은 같은 순간인지로 비교한다(CI는 UTC). 데모 날짜·id는 데이터에서 읽는다."""
import json
import re
import subprocess
import threading
from datetime import datetime, time, timedelta

import pytest

from lms_agent import agent, agent_cli, agent_loop, core, mock_ai
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SECRET = "canvas-test-token-loop-0123456789abcdefABCDEF"


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def due_at(days, h=23, mi=59):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def md_ko(days):
    d = T + timedelta(days=days)
    return f"{d.month}월 {d.day}일"


def md_en(days):
    d = T + timedelta(days=days)
    return f"{MONTHS[d.month - 1]}. {d.day}"


def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


FINAL = f"The final will be held on {md_en(20)} at 10:30 in the regular class hour."
SCOPE = "기말고사 범위는 5장부터 9장까지입니다."
LAB2_EXT = f"LAB2는 하루 늦춰 {md_ko(6)} 23:59까지 받습니다."


def make_cache():
    return {
        "fetched_at": z(0),
        "courses": [{"id": "401", "name": "디지털논리", "course_code": "EE401", "syllabus_body": None},
                    {"id": "402", "name": "자료구조", "course_code": "CS402", "syllabus_body": None}],
        "assignments": [],
        "announcements": [
            ann(1, "401", z(-6), "Week 12 notice", f"<p>{FINAL}</p><p>{SCOPE}</p>"),
            ann(2, "401", z(-5), "LAB2 안내", f"<p>LAB2 보고서 마감: {md_ko(5)} 23:59</p>"),
            ann(5, "401", z(-1), "LAB2 연장", f"<p>{LAB2_EXT}</p>"),
            ann(3, "402", z(-4), "Quiz 1 안내", f"<p>Quiz 1 마감: {md_ko(4)} 23:59</p>"),
            ann(4, "401", z(-3), "Quiz 1 안내", f"<p>Quiz 1 마감: {md_ko(7)} 23:59</p>"),
        ],
        "conversations": [{"id": 50, "subject": "조교 안내", "context_code": "course_401", "context_name": "디지털논리",
                           "last_message_at": z(-8), "messages": [{"id": 501, "created_at": z(-8),
                                                                   "body": "문의는 조교 ta.logic@example.ac.kr 로 보내 주세요."}]}],
        "boards": [], "pdf_texts": {}, "errors": [],
    }


def make_store(path):
    s = Store(path)
    s.data["cache"] = make_cache()
    core.rebuild(s)
    s.save()
    return s


def items(s, title, course="401"):
    return sorted([v for v in s.view() if v["title"] == title and v["course_id"] == course], key=lambda v: v["due"])


def reply(body) -> str:
    return json.dumps({"type": "result", "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)


def script(*bodies, seen=None, gate=None):
    """가짜 실행기: n번째 호출은 bodies[n](input → dict 또는 dict), 다 쓰면 마지막 것. gate = {호출 번호: Event}(그 호출 전에 기다림)."""
    seen = [] if seen is None else seen

    def run(args, input, timeout, cwd):
        seen.append(input)
        if gate and len(seen) in gate:
            gate[len(seen)]["started"].set()
            gate[len(seen)]["go"].wait(10)
        b = bodies[min(len(seen), len(bodies)) - 1]
        return subprocess.CompletedProcess(args, 0, reply(b(input) if callable(b) else b), "")
    run.seen = seen
    return run


def key_for(inp, quote):
    """보낸 도구 결과에서 quote가 든 글의 source_key."""
    tools = inp.split(agent_cli.AGENT_TOOLS_MARK, 1)[-1]
    for block in tools.split("\n### source_key: ")[1:]:
        key, _, body = block.partition("\n")
        if " ".join(quote.split()) in " ".join(body.split()):
            return key.strip()
    return "announcement:999"


def ask(s, message, run, **kw):
    return agent.answer(s, message, ai_enabled=True, runner=run, command="claude", **kw)


def done(say, calls=(), citations=()):
    return {"say": say, "calls": list(calls), "done": True, "citations": list(citations)}


def call(tool, **args):
    return {"tool": tool, "args": args}


# ---------------------------------------------------------------- 바로 답(도구 없이) · 첫 입력

def test_direct_answer_without_tools_and_what_the_ai_gets(tmp_path):
    s = make_store(tmp_path / "s.json")
    from lms_agent import gradebook
    gradebook.save_course(s, "401", {"components": [{"name": "Lab reports", "weight": 40}, {"name": "Final exam", "weight": 60}]})
    run = script(done("안녕하세요! 오늘도 같이 달려 봐요."))
    hist = [{"id": "t-0", "message": "디지털논리 기말 범위 어디까지야?", "answer": f"“{SCOPE}”", "kind": "question",
             "course_id": "401"}]
    r = ask(s, "안녕! 나 김가짜야", run, names=["김가짜"], history=hist)
    assert (r["kind"], r["by"], r["attempts"], r["error"]) == ("chat", "ai", 1, None)
    assert r["answer"].startswith("안녕하세요") and r["proposals"] == [] and r["citations"] == [] and r["steps"] == []
    sent = run.seen[0]
    assert agent_cli.AGENT_MARK in sent and agent_cli.ASSIST_MARK not in sent and agent_cli.ANSWER_MARK not in sent
    assert "search_texts" in sent and "propose_item_remove" in sent and agent_cli.ACTIONS_MARK in sent
    assert agent_cli.HISTORY_MARK in sent and "김가짜" not in sent                  # 이전 대화·가림
    for v in s.view():                                                            # 모든 항목 id·평가 항목 이름·비중
        assert v["id"] in sent
    assert "Lab reports 40%" in sent and "Final exam 60%" in sent
    small = agent_loop.state_summary(agent.Ctx(s), budget=300)                    # 예산이 작으면 자세한 줄을 줄이고 id만
    assert all(v["id"] in small for v in s.view()) and "Lab reports 40%" in small and len(small) < len(
        agent_loop.state_summary(agent.Ctx(s)))
    assert len(sent) < agent_cli.MAX_TEXT + 9000                                  # 지시·도구 목록·스키마 + 12,000자 예산
    assert s.data["ai_runs"][-1]["task"] == "agent" and "안녕" not in json.dumps(s.data["ai_runs"], ensure_ascii=False)


# ---------------------------------------------------------------- 읽기 도구 → 인용 답, 그대로가 아닌 인용

def test_read_tool_then_cited_answer(tmp_path):
    s = make_store(tmp_path / "s.json")
    cite = lambda q: (lambda inp: done("기말 범위는 5장부터 9장까지예요.", citations=[{"source_key": key_for(inp, SCOPE), "quote": q}]))
    run = script({"say": "", "calls": [call("search_texts", query="기말 범위", course_id="401")], "done": False},
                 cite(SCOPE))
    r = ask(s, "디지털논리 기말 범위 어디까지야?", run)
    assert (r["kind"], r["by"], r["attempts"], r["error"]) == ("question", "ai", 2, None)
    assert [(x["round"], x["tool"], x["ok"], x["label"]) for x in r["steps"]] == [(1, "search_texts", True, "글 찾기")]
    assert re.fullmatch(r"\d+개", r["steps"][0]["note"])
    c = r["citations"][0]
    assert c["quote"] == SCOPE and c["title"] == "Week 12 notice" and c["course_short"] == "디지털논리"
    assert parse_iso(c["posted_at"]) == parse_iso(z(-6)) and r["results"] and r["course_context"] == "401"
    assert agent_cli.AGENT_TOOLS_MARK in run.seen[1] and SCOPE in run.seen[1]
    # 이번 도구 결과에 그대로 없는 인용 → 오류를 인용해 다시 묻고, 고치면 통과
    run = script({"say": "", "calls": [call("search_texts", query="기말 범위")], "done": False},
                 cite("기말 범위: 5~9장"), cite(SCOPE))
    r = ask(s, "디지털논리 기말 범위 어디까지야?", run)
    assert r["by"] == "ai" and r["attempts"] == 3 and "그대로 없어요" in run.seen[2] and r["citations"][0]["quote"] == SCOPE
    # 끝까지 그러면 실패 → 규칙 갈래(v0.7.3)로 답하고 이유를 남긴다
    run = script({"say": "", "calls": [call("search_texts", query="기말 범위")], "done": False}, cite("기말 범위: 5~9장"))
    r = ask(s, "디지털논리 기말 범위 어디까지야?", run)
    assert r["by"] == "rules" and r["kind"] == "question" and SCOPE in r["citations"][0]["quote"]
    assert any("AI 에이전트가 끝까지 답하지 못해" in n for n in r["notes"]) and r["steps"][0]["tool"] == "search_texts"
    assert len(run.seen) <= agent.PER_ASK


def test_grade_numbers_only_from_whatif_and_no_invented_dates(tmp_path):
    s = make_store(tmp_path / "s.json")
    day = T + timedelta(days=40)
    run = script(done("기말에서 85점 받으면 A0예요."), done(f"기말은 {day.month}월 {day.day}일이에요."),
                 done("성적 화면에서 '만약에'를 같이 계산해 볼까요?"))
    r = ask(s, "디지털논리 학점 어떻게 될까", run)
    assert r["by"] == "rules" and "whatif" in run.seen[1] and len(run.seen) >= 2     # 두 번 다 틀림 → 규칙
    run = script(done("기말에서 85점 받으면 A0예요."), done("whatif로 계산해야 정확해요."))
    r = ask(s, "디지털논리 학점 어떻게 될까", run)
    assert r["by"] == "ai" and r["attempts"] == 2 and r["answer"] == "whatif로 계산해야 정확해요."
    run = script(done(f"기말은 {day.month}월 {day.day}일이에요."), done("기말 날짜는 공지를 찾아볼게요."))
    r = ask(s, "안녕 기말 언제 봐?", run)
    assert r["by"] == "ai" and "도구 결과·지금 상태에 없어요" in run.seen[1]


# ---------------------------------------------------------------- 3차례 한도

def test_three_round_cap(tmp_path):
    s = make_store(tmp_path / "s.json")
    run = script({"say": "목록을 더 볼게요.", "calls": [call("list_items", course_id="401")], "done": False})
    r = ask(s, "디지털논리 마감 정리해 줄래", run)
    assert len(run.seen) == 3 and r["by"] == "ai" and r["answer"] == "목록을 더 볼게요."
    assert {x["round"] for x in r["steps"]} == {1, 2, 3} and all(x["tool"] == "list_items" for x in r["steps"])
    assert "마지막 차례" in run.seen[2] and any("3차례" in n for n in r["notes"])
    # 마지막 차례에도 답(say)이 없으면 한 번 다시 묻고(4번째), 그래도 없으면 규칙으로
    run = script({"say": "", "calls": [call("list_items")], "done": False})
    r = ask(s, "디지털논리 마감 정리해 줄래", run)
    assert len(run.seen) == agent.PER_ASK == 4 and r["by"] == "rules"
    assert all(agent_cli.AGENT_MARK in x for x in run.seen)                       # 남은 호출이 없어 규칙 갈래는 AI 안 부름


# ---------------------------------------------------------------- 막히거나 실행기 문제 → 규칙

def test_fallback_to_rules_when_blocked_or_cli_fails(tmp_path):
    s = make_store(tmp_path / "s.json")
    s.data["ai_runs"] = [{"at": iso(now_local()), "task": "triage_review", "ok": True, "attempts": 30}]
    s.data["settings"]["ai_daily_limit"] = 30
    run = script(done("x"))
    r = ask(s, "디지털논리 기말 범위 어디까지야?", run)
    assert run.seen == [] and r["by"] == "rules" and "한도" in r["error"] and r["kind"] == "question"
    s.data["ai_runs"] = []
    seen = []

    def broken(args, input, timeout, cwd):
        seen.append(input)
        raise subprocess.TimeoutExpired(args, timeout)
    r = ask(s, "디지털논리 기말 범위 어디까지야?", broken)
    assert len(seen) == 1 and r["by"] == "rules" and "답하지 않았습니다" in r["error"]   # 같은 실행기를 다시 띄우지 않는다
    assert any("규칙으로 답했어요" in n for n in r["notes"]) and SCOPE in r["citations"][0]["quote"]
    # AI가 꺼져 있으면 루프 없이 지금 갈래 그대로
    r = agent.answer(s, "디지털논리 기말 범위 어디까지야?")
    assert r["by"] == "rules" and r["steps"] == [] and not any("에이전트" in n for n in r["notes"])


# ---------------------------------------------------------------- 바로 반영 조건(26-4)

def test_imperative_regex_with_particles_and_questions():
    for msg in ("LAB2를 지워줘", "기말고사 일정은 삭제해 주세요", "Assignment도 없애줘요", "중복된 거 합쳐줘", "마감을 금요일로 고쳐 줄래?",
                "그 일정 좀 빼줘", "LAB3 마감 추가해줘", "Report 점수 연결 끊어 줘", "LAB2 삭제", "기말고사를 일정에 넣어줘",
                "이거 등록해 주세요", "평가 항목 정리하자", "remove the duplicate LAB2", "please delete it"):
        assert agent_loop.is_imperative(msg), msg
    for msg in ("LAB2 지워야 할까?", "이거 삭제해도 돼?", "왜 지웠어?", "삭제하면 어떻게 돼?", "LAB 빼고 다 보여줘", "기말고사 언제야?",
                "should I remove it?", "고쳐야 하나요?", "지울 수 있어?", "LAB2 마감이 안 보여요", "안녕"):
        assert not agent_loop.is_imperative(msg), msg
    assert agent_loop.claims_done("LAB2를 지웠어요.") and agent_loop.claims_done("두 항목을 합쳤습니다")
    assert not agent_loop.claims_done("LAB2를 빼는 제안을 만들었어요 — [반영]을 누르면 돼요")
    assert not agent_loop.claims_done("정말 지웠어요?")


def _fix_lab2(s, auto=True, say="LAB 2 마감을 연장 공지대로 고쳤어요 — [되돌리기]로 되돌릴 수 있어요."):
    first = items(s, "LAB 2")[0]

    def round2(inp):
        return done(say, [call("propose_fix_due", item_id=first["id"], due=due_at(6), evidence=LAB2_EXT,
                               source_key=key_for(inp, LAB2_EXT), reason="연장 공지", auto=auto)])
    return first, script({"say": "", "calls": [call("search_texts", query="LAB2 연장")], "done": False}, round2)


def test_auto_guard_imperative_vs_question_and_ambiguous_targets(tmp_path):
    s = make_store(tmp_path / "s.json")
    first, run = _fix_lab2(s)
    r = ask(s, "LAB2 마감을 연장 공지대로 고쳐줘", run)
    p = r["proposals"][0]
    assert (r["kind"], p["type"], p["auto"], p["auto_applied"], p["rejected_reason"]) == ("missing", "fix_due", True, False, None)
    assert p["item_id"] == first["id"]                         # 같은 과제의 다른 후보(묶음)는 같은 대상으로 본다
    # 묻는 말 → 카드만, '고쳤어요'라고 하면 검사 실패(다시 묻기)
    first, run = _fix_lab2(s, say="LAB 2 마감을 연장 공지대로 고치는 제안이에요 — [반영]을 누르면 돼요.")
    r = ask(s, "LAB2 마감 고쳐야 할까?", run)
    p = r["proposals"][0]
    assert not p["auto"] and any("분명히 시킨 말" in n for n in p["notes"]) and len(run.seen) == 2
    first, run = _fix_lab2(s)
    r = ask(s, "LAB2 마감 고쳐야 할까?", run)
    assert "했다고 하면 안 돼요" in run.seen[2] and len(run.seen) >= 3
    # 같은 이름의 항목이 여러 과목에 → 대상이 분명하지 않음 → 카드만
    q1 = items(s, "Quiz 1", "402")[0]
    run = script({"say": "", "calls": [call("list_items", query="Quiz 1")], "done": False},
                 done("Quiz 1을 빼는 제안이에요.", [call("propose_item_remove", item_id=q1["id"], reason="학생이 지워 달라고 함",
                                                     auto=True)]))
    r = ask(s, "Quiz 1 지워줘", run)
    p = r["proposals"][0]
    assert p["type"] == "item_remove" and p["rejected_reason"] is None and not p["auto"]
    assert any("여럿" in n for n in p["notes"])
    # 과목을 말하면 하나로 정해진다
    run = script({"say": "", "calls": [call("list_items", course_id="402")], "done": False},
                 done("Quiz 1을 빼는 제안이에요.", [call("propose_item_remove", item_id=q1["id"], reason="학생이 지워 달라고 함",
                                                     auto=True)]))
    assert ask(s, "자료구조 Quiz 1 지워줘", run)["proposals"][0]["auto"]
    # 바꾸는 제안이 둘이면(대상이 여럿) 바로 반영하지 않는다
    lab = items(s, "LAB 2")
    run = script(done("둘 다 빼는 제안이에요.", [call("propose_item_remove", item_id=lab[0]["id"], reason="x", auto=True),
                                             call("propose_item_remove", item_id=q1["id"], reason="y", auto=True)]))
    r = ask(s, "LAB2랑 Quiz 1 지워줘", run)
    assert [p["auto"] for p in r["proposals"]] == [False, False]
    # 앱 동작은 auto를 내도 바로 하지 않는다
    run = script(done("동기화 카드를 준비했어요.", [call("propose_action", action="sync", params={}, reason="새 공지", auto=True)]))
    r = ask(s, "지금 동기화해줘", run)
    assert r["kind"] == "action" and r["proposals"][0]["type"] == "action" and not r["proposals"][0]["auto"]


def test_rejected_proposal_in_middle_round_is_fed_back(tmp_path):
    s = make_store(tmp_path / "s.json")
    run = script({"say": "", "calls": [call("propose_item_remove", item_id="f-nope", reason="x")], "done": False},
                 lambda inp: done("그 항목은 없어서 다시 찾아볼게요." if "거절 / rejected" in inp else "?"))
    r = ask(s, "없는 항목 지워줘", run)
    assert r["by"] == "ai" and r["proposals"] == [] and r["steps"][0]["ok"] is False and "거절" in r["steps"][0]["note"]


# ---------------------------------------------------------------- 바로 반영 + 되돌리기(HTTP 앱)

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


def _http(url, path, token, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-LMS-Agent": token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_auto_apply_and_undo_over_http(tmp_path):
    from lms_agent.features import agent as fa
    from lms_agent.server import serve_in_thread, start_server

    def round2(inp):
        return done("디지털논리 기말고사 일정을 추가했어요 — 바꾸고 싶으면 [되돌리기]를 눌러 주세요.",
                    [call("propose_add_deadline", course_id="401", title="Final exam", kind="시험", due=due_at(20, 10, 30),
                          evidence=FINAL, source_key=key_for(inp, FINAL), reason="'final'만 있어 규칙이 놓친 시험", auto=True)])
    run = script({"say": "", "calls": [call("search_texts", query="final", course_id="401")], "done": False}, round2)
    app = make_app(tmp_path, run)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, out = _http(url, "/api/agent/ask", app.token, {"message": "디지털논리 기말고사 일정 추가해줘"})
        assert code == 200 and fa.wait(app, out["job"])
        st = app.state()
        th = st["agent"]["threads"][-1]
        p = th["proposals"][0]
        assert (th["by"], th["kind"], th["progress"]) == ("ai", "missing", None)
        assert (p["type"], p["auto"], p["auto_applied"], p["applied"], p["can_apply"]) == ("add_deadline", True, True, True, False)
        it = next(v for v in st["items"] if v["id"] == p["result"]["item_id"])
        assert (it["status"], it["decided_by"], it["found_by"]) == ("scheduled", "ai", "ai")
        assert parse_iso(it["due"]) == parse_iso(due_at(20, 10, 30)) and "근거:" in it["ai_reason"]
        assert app.events.count("changed") == 1                                    # 구글 캘린더 반영
        assert SECRET not in json.dumps(st, ensure_ascii=False) and '"_auto"' not in json.dumps(st, ensure_ascii=False)
        code, state = _http(url, "/api/agent/undo", app.token, {"thread": th["id"], "proposal": 0})
        assert code == 200 and not any(v["id"] == it["id"] for v in state["items"])
        assert not state["agent"]["threads"][-1]["proposals"][0]["applied"] and app.events.count("changed") == 2
    finally:
        httpd.shutdown()


def test_item_remove_card_apply_emits_changed_and_auto_waits_for_sync_lock(tmp_path):
    from lms_agent.features import agent as fa
    first = {}

    def round2(inp):
        return done("LAB 2 하나를 빼는 제안이에요 — [반영]을 누르면 돼요.",
                    [call("propose_item_remove", item_id=first["id"], reason="잘못 들어간 마감", auto=True)])
    run = script({"say": "", "calls": [call("list_items", course_id="401")], "done": False}, round2)
    app = make_app(tmp_path, run)
    first.update(items(app.store, "LAB 2")[0])
    job = fa.ask(app, {"message": "LAB2 잘못 들어간 거 지워야 할까?"})["job"]      # 묻는 말 → 카드만
    assert fa.wait(app, job)
    p = app.state()["agent"]["threads"][-1]["proposals"][0]
    assert (p["type"], p["auto"], p["auto_applied"], p["can_apply"]) == ("item_remove", False, False, True)
    assert app.events == []
    state = fa.apply_proposal(app, {"thread": job, "proposal": 0})               # [반영] → changed(item_remove도)
    assert app.events == ["changed"]
    assert next(v for v in state["items"] if v["id"] == first["id"])["status"] == "rejected"
    fa.undo_proposal(app, {"thread": job, "proposal": 0})
    assert app.events == ["changed", "changed"]
    # 동기화가 잠금을 오래 쥐고 있으면 바로 반영하지 않고 카드로 남긴다(같은 잠금 — features/agent._change와 같음)
    app.store.reload_if_changed()
    run.seen.clear()
    agent.AUTO_LOCK_SECONDS, old = 0.2, agent.AUTO_LOCK_SECONDS
    try:
        with app.lock:
            th = agent.new_thread(app.store, "LAB2 지워줘", None)
            app.store.save()
            agent.run_job(app.store, th["id"], "LAB2 지워줘", ai_enabled=True, runner=run, command="claude",
                          lock=app.lock, emit=lambda: app.emit("changed"))
    finally:
        agent.AUTO_LOCK_SECONDS = old
    app.store.reload_if_changed()
    t = agent.find_thread(app.store, th["id"])
    assert t["proposals"][0]["auto"] and not t["proposals"][0].get("applied")
    assert any("동기화하는 중" in n for n in t["notes"]) and app.events == ["changed", "changed"]


# ---------------------------------------------------------------- 진행 표시·단계

def test_progress_and_steps_are_written_between_rounds(tmp_path):
    from lms_agent.features import agent as fa
    gate = {2: {"started": threading.Event(), "go": threading.Event()}}
    run = script({"say": "", "calls": [call("list_items", course_id="401"), call("get_settings")], "done": False},
                 done("디지털논리 마감은 목록에 있는 그대로예요."), gate=gate)
    app = make_app(tmp_path, run)
    job = fa.ask(app, {"message": "디지털논리 마감 목록 보여줘"})["job"]
    try:
        assert gate[2]["started"].wait(10)
        app.store.reload_if_changed()
        th = next(t for t in app.state()["agent"]["threads"] if t["id"] == job)
        assert th["status"] == "running" and th["progress"] == agent_loop.ASK_AGAIN
        assert [(x["tool"], x["label"], x["ok"]) for x in th["steps"]] == [("list_items", "마감 목록 보기", True),
                                                                           ("get_settings", "설정 보기", True)]
        assert th["steps"][0]["note"] == f"{len([v for v in app.store.view() if v['course_id'] == '401'])}개"
    finally:
        gate[2]["go"].set()
    assert fa.wait(app, job)
    st = app.state()["agent"]
    th = next(t for t in st["threads"] if t["id"] == job)
    assert th["status"] == "done" and th["progress"] is None and len(th["steps"]) == 2 and th["kind"] == "question"
    assert st["limits"]["per_ask"] == agent.PER_ASK == 4 and st["limits"]["max_rounds"] == 3


# ---------------------------------------------------------------- 보내지 않는 것

def test_personal_google_events_and_secrets_never_sent(tmp_path):
    s = make_store(tmp_path / "s.json")
    s.data["gcal_events"] = {"fetched_at": z(0), "events": [{"id": "g1", "summary": "비밀 약속 장소 망원동", "start": z(1),
                                                            "end": z(1, 11), "location": "비밀 장소", "calendar": "개인"}]}
    s.data["settings"]["student_id"] = "2099123456"
    first = s.view()[0]
    run = script({"say": "", "calls": [call("list_items"), call("get_settings"), call("search_texts", query="약속"),
                                       call("get_item", item_id=first["id"])], "done": False},
                 {"say": "", "calls": [call("get_grades", course_id="401"), call("list_materials")], "done": False},
                 done("목록과 설정을 봤어요."))
    r = ask(s, "내 일정 다 보여줘 망원동 약속도", run, names=["김가짜"])
    assert r["by"] == "ai" and len(run.seen) == 3
    for sent in run.seen:
        for private in ("비밀 약속", "비밀 장소", "2099123456", "ta.logic@example.ac.kr", SECRET):
            assert private not in sent, private
    assert "[이메일]" in run.seen[1] or "ta.logic" not in run.seen[1]


# ---------------------------------------------------------------- 하루 한도 설정

def test_daily_limit_setting_controls_the_loop(tmp_path):
    from lms_agent.features import agent as fa
    s = make_store(tmp_path / "s.json")
    s.data["ai_runs"] = [{"at": iso(now_local()), "task": "triage_review", "ok": True, "attempts": 500}]
    run = script(done("안녕하세요!"))
    r = ask(s, "안녕", run)                                   # 기본 100회 → 막힘
    assert run.seen == [] and r["by"] == "rules" and "한도" in r["error"]
    s.data["settings"]["ai_daily_limit"] = 0                 # 제한 없음
    r = ask(s, "안녕", run)
    assert len(run.seen) == 1 and r["by"] == "ai"
    s.data["settings"]["ai_daily_limit"] = 300
    s.data["ai_runs"] = [{"at": iso(now_local()), "task": "x", "ok": True, "attempts": 299}]
    run = script({"say": "", "calls": [call("list_items")], "done": False}, done("다 봤어요."))
    r = ask(s, "디지털논리 마감 목록", run)                     # 하루에 한 번만 남음 → 1차례 뒤 막힘 → 규칙
    assert len(run.seen) == 1 and r["by"] == "rules" and agent_cli.calls_today(s) == 300
    app = make_app(tmp_path / "app", script(done("x")))
    assert app.state()["agent"]["limits"]["per_day"] == 100
    app.store.data["settings"]["ai_daily_limit"] = 0
    assert app.state()["agent"]["limits"]["per_day"] is None
    del fa


# ---------------------------------------------------------------- 가짜 AI(데모): 루프 흐름

def test_mock_ai_agent_flows(tmp_path):
    from lms_agent import gradebook
    from lms_agent.features import agent as fa
    app = make_app(tmp_path, mock_ai.runner)
    gradebook.save_course(app.store, "401", {"components": [{"name": "Report", "weight": 50}, {"name": "Assignment", "weight": 10},
                                                            {"name": "Final exam", "weight": 40}]})
    app.store.save()

    def ask_app(msg):
        job = fa.ask(app, {"message": msg})["job"]
        assert fa.wait(app, job)
        return next(t for t in app.state()["agent"]["threads"] if t["id"] == job)
    th = ask_app("안녕하세요")
    assert (th["kind"], th["by"], th["steps"]) == ("chat", "ai", []) and th["answer"].startswith("안녕하세요")
    th = ask_app("Assignment가 중복 등록된 것 같아 없애줘")                    # list_items → 평가 항목 지우기(바로)
    assert [x["tool"] for x in th["steps"]] == ["list_items", "propose_component_remove"]
    p = th["proposals"][0]
    assert (th["kind"], p["type"], p["component"], p["auto"], p["auto_applied"]) == ("fix", "component_remove", "Assignment",
                                                                                     True, True)
    assert "Assignment" not in [c["name"] for c in app.store.data["grades"]["401"]["components"]]
    th = ask_app("디지털논리 기말 범위 어디까지야?")                          # search_texts → 그대로 인용
    assert (th["kind"], th["by"]) == ("question", "ai") and th["citations"][0]["quote"] == SCOPE
    lab = items(app.store, "LAB 2")
    th = ask_app("LAB2 지워줘")                                               # list_items → 항목 빼기(바로) → changed
    p = th["proposals"][0]
    assert (p["type"], p["auto_applied"]) == ("item_remove", True) and p["item_id"] in {v["id"] for v in lab}
    assert "changed" in app.events


def test_list_items_in_carried_course_also_shows_named_items_elsewhere(tmp_path):
    """앞 대화의 과목으로 좁혀 목록을 봐도, 학생 말에 나온 제목이 다른 과목에 있으면 함께 보인다(리드 — 데모에서 'LAB 6 일정 지워줘'가
    이어받은 과목에서만 찾다가 '못 찾음'이 됐던 것)."""
    s = make_store(tmp_path / "s.json")
    lab = next(v for v in s.view() if v["course_id"] == "401" and v["title"].replace(" ", "") == "LAB2" and v["status"] != "rejected")
    loop = agent_loop.Loop(s, f"{lab['title']} 일정 지워줘", None, "402", [], [], None, now_local(), None, None)
    text, note = loop.t_list_items({"course_id": "402"})
    assert lab["id"] in text and "다른 과목" in note and "다른 과목" in text
    text2, _ = loop.t_list_items({})                       # 과목을 안 좁히면 덧붙이는 줄이 없다
    assert lab["id"] in text2 and "다른 과목 항목" not in text2
    text3, _ = agent_loop.Loop(s, "Quiz 1 언제야?", None, "402", [], [], None, now_local(), None, None) \
        .t_list_items({"course_id": "402"})
    assert all(v["id"] in text3 for v in s.view() if v["title"] == "Quiz 1" and v["status"] != "rejected")
