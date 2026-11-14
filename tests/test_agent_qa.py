"""에이전트 묻기 갈래(계약 22-1)·과목 질문 답하기(22-2)·채팅의 액션 제안 실행/되돌리기(22-9).
가짜 데이터만 쓴다(과목·문장·주소 모두 지어낸 것). 다른 모듈의 handle·actions.run/undo는 바꿔 끼운 가짜."""
import json
import subprocess
from datetime import datetime, time, timedelta

import pytest

from lms_agent import actions, agent, agent_cli, core, maildraft, mock_ai, plan, whatif
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


FINAL_DAY = T + timedelta(days=20)
OS_FINAL = f"The final exam is on {MONTHS[FINAL_DAY.month - 1]}. {FINAL_DAY.day} at 9:00 in Room 210."
SCOPE = "기말고사 범위는 5장부터 9장까지입니다."
LATE = "늦게 내면 하루에 10%씩 감점합니다."
TA = "문의는 조교 이메일 ta.circuit@example.ac.kr 로 보내 주세요."


@pytest.fixture(autouse=True)
def _other_handles_quiet(request, monkeypatch):
    """질문·액션 시험이 다른 담당의 handle(명령·메일·계획·만약에 낱말 규칙)에 흔들리지 않게 모두 None으로 둔다.
    갈래 시험은 다시 바꿔 끼우고, 데모 시험(이름에 demo)은 실제 handle 그대로 — 데모 질문이 질문 갈래로 가는지 함께 본다."""
    if "demo" in request.node.name:
        return
    for mod in (actions, maildraft, plan, whatif):
        monkeypatch.setattr(mod, "handle", lambda *a, **k: None)


def make_cache():
    return {
        "fetched_at": z(0),
        "courses": [{"id": "301", "name": "회로이론", "course_code": "EE301", "syllabus_body": None},
                    {"id": "302", "name": "Operating Systems", "course_code": "CS302", "syllabus_body": None}],
        "assignments": [],
        "announcements": [
            ann(1, "301", z(-5), "기말고사 안내", f"<p>{SCOPE}</p><p>시험 장소: 공학관 101호</p>"
                                              "<p>계산기는 지참할 수 있고, 오픈북은 아닙니다.</p>"),
            ann(2, "301", z(-20), "과제 제출 규정", "<p>보고서는 PDF 한 파일로 제출하고 파일명은 학번_이름.pdf로 해 주세요.</p>"
                                                f"<p>{LATE}</p>"),
            ann(3, "302", z(-3), "Final exam", f"<p>{OS_FINAL}</p><p>The final exam covers chapters 4-8.</p>"),
            ann(4, "302", z(-30), "Attendance", "<p>Attendance is checked every class; more than 3 absences result "
                                                "in F.</p>"),
        ],
        "conversations": [
            {"id": 50, "subject": "조교 안내", "context_code": "course_301", "context_name": "회로이론",
             "last_message_at": z(-8), "messages": [{"id": 501, "created_at": z(-8), "body": TA}]},
        ],
        "boards": [],
        "pdf_texts": {},
        "errors": [],
    }


def make_store(path):
    s = Store(path)
    s.data["cache"] = make_cache()
    core.rebuild(s)
    s.save()
    return s


def plain_of(s, key):
    return next(x["plain"] for x in agent.sources(s) if x["key"] == key)


def flat(t):
    return " ".join(str(t).split())


# ---------------------------------------------------------------- 갈래 나누기(22-1)

@pytest.mark.parametrize("message,kind", [
    ("기말 범위 어디까지야?", "question"),
    ("LAB4 제출 형식이 뭐야?", "question"),
    ("지각하면 감점 있어?", "question"),
    ("중간고사 평균이 몇이야?", "question"),
    ("What does the final cover?", "question"),
    ("LAB4 제출 형식이 안 보여요", "question"),        # 주제만 있고 앱이 고칠 것(날짜·점수·비중)은 없음 → 인용으로 답
    ("기말고사 날짜가 안 보여요", "missing"),
    ("성적이 안 들어와요", "missing"),
    ("평가 비중이 안 보여요", "missing"),             # 비중은 assist가 고칠 수 있다
    ("LAB4 마감 언제야?", "missing"),                 # 날짜를 묻는 말은 마감 찾기 흐름
    ("When is the final?", "missing"),
])
def test_classify(message, kind):
    assert agent.classify(message) == kind


def _patch_handles(monkeypatch, results, calls):
    def fake(kind):
        def handle(*args):
            calls.append((kind, args))
            r = results.get(kind)
            if isinstance(r, Exception):
                raise r
            return r
        return handle
    monkeypatch.setattr(actions, "handle", fake("action"))
    monkeypatch.setattr(maildraft, "handle", fake("mail"))
    monkeypatch.setattr(plan, "handle", fake("plan"))
    monkeypatch.setattr(whatif, "handle", fake("whatif"))


def test_routing_order_first_non_none_wins(tmp_path, monkeypatch):
    s = make_store(tmp_path / "s.json")
    canned = {"action": {"answer": "명령", "proposals": []}, "mail": {"answer": "메일", "draft": {"subject": "연장 문의"}},
              "plan": {"answer": "계획", "plan": {"blocks": []}}, "whatif": {"answer": "만약", "whatif": {"kind": "needed"}}}
    for kind in ("action", "mail", "plan", "whatif"):
        calls = []
        _patch_handles(monkeypatch, {k: v for k, v in canned.items() if list(canned).index(k) >= list(canned).index(kind)},
                       calls)
        r = agent.answer(s, "아무 말", course_id="301", names=["김가짜"])
        assert r["kind"] == kind and r["answer"] == canned[kind]["answer"] and r["status"] == "done"
        assert [c[0] for c in calls] == list(canned)[:list(canned).index(kind) + 1]   # 앞 갈래부터 차례로, 찾으면 멈춤
        assert r["by"] == "rules" and r["proposals"] == [] and r["citations"] == [] and r["finished_at"]
    # 각 handle이 받는 인자(계약 22-1의 모양)
    args = dict((k, a) for k, a in calls)
    assert args["action"] == (s, "아무 말", "301")
    assert args["mail"] == (s, "아무 말", "301", ["김가짜"])
    assert args["plan"][:3] == (s, "아무 말", "301") and isinstance(args["plan"][3], datetime)
    assert isinstance(args["whatif"][3], datetime)
    # 갈래별 칸은 그대로 합쳐진다
    _patch_handles(monkeypatch, {"mail": canned["mail"]}, [])
    assert agent.answer(s, "x")["draft"] == {"subject": "연장 문의"}
    _patch_handles(monkeypatch, {"whatif": canned["whatif"]}, [])
    assert agent.answer(s, "x")["whatif"] == {"kind": "needed"}


def test_routing_falls_through_on_error_and_keeps_thread_frame(tmp_path, monkeypatch):
    s = make_store(tmp_path / "s.json")
    calls = []
    _patch_handles(monkeypatch, {"action": RuntimeError("고장 token=abc"), "mail": "모양이 다름",
                                 "plan": {"answer": "계획", "plan": {"blocks": []}, "id": "t-가짜", "status": "running",
                                          "kind": "whatif", "notes": ["블록 3개"]}}, calls)
    r = agent.answer(s, "기말 전 2주 공부 계획 세워줘")
    assert r["kind"] == "plan" and r["status"] == "done" and "id" not in r     # 대화의 틀은 handle이 바꾸지 못한다
    assert any("'action' 갈래에서 오류" in n and "RuntimeError" in n for n in r["notes"])
    assert all("token=abc" not in n for n in r["notes"])                          # 예외 내용은 남기지 않는다
    assert any("'mail' 갈래의 답 모양" in n for n in r["notes"]) and "블록 3개" in r["notes"]
    # 모두 None → 묻기(놓친 것·질문)
    _patch_handles(monkeypatch, {}, [])
    assert agent.answer(s, "회로이론 기말고사 날짜가 안 보여요")["kind"] == "missing"
    assert agent.answer(s, "회로이론 기말 범위 어디까지야?")["kind"] == "question"


def test_old_threads_without_kind_are_missing(tmp_path):
    s = make_store(tmp_path / "s.json")
    old = agent.new_thread(s, "옛 질문", None)
    old.update(status="done", by="rules", answer="옛 답")
    del old["kind"], old["citations"]
    agent.new_thread(s, "답하는 중", None)
    pub = agent.public_threads(s, active={agent.threads(s)[-1]["id"]})
    assert pub[0]["kind"] == "missing" and pub[0]["citations"] == []
    assert pub[1]["kind"] is None and pub[1]["status"] == "running"


# ---------------------------------------------------------------- 규칙으로 답하기(22-2)

def test_question_rules_mode_quotes_paragraphs_with_citations(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = agent.answer(s, "회로이론 기말 범위 어디까지야?")
    assert (r["kind"], r["by"], r["error"], r["proposals"]) == ("question", "rules", None, [])
    assert "이 글들에 이렇게 적혀 있어요" in r["answer"] and "AI CLI를 켜면" in r["answer"]
    c = r["citations"][0]
    assert set(c) == {"source_key", "course_id", "course_short", "type", "title", "posted_at", "url", "quote"}
    assert (c["source_key"], c["course_id"], c["course_short"], c["type"], c["title"]) == \
        ("announcement:1", "301", "회로이론", "announcement", "기말고사 안내")
    assert SCOPE in c["quote"] and len(c["quote"]) <= 500
    assert flat(c["quote"]) in flat(plain_of(s, c["source_key"]))                  # 그 글의 문장 그대로
    assert parse_iso(c["posted_at"]) == parse_iso(z(-5)) and c["url"].endswith("/discussion_topics/1")
    assert 1 <= len(r["citations"]) <= 3 and all(x["course_id"] == "301" for x in r["citations"])   # 말한 과목 글만
    # 질문 낱말에 맞는 문단: 감점 규정·시험 장소·준비물·영어 질문
    assert LATE in agent.answer(s, "회로이론 과제 늦게 내면 감점 있어?")["citations"][0]["quote"]
    assert "공학관 101호" in agent.answer(s, "회로이론 기말 시험장 어디야?")["citations"][0]["quote"]
    assert "계산기" in agent.answer(s, "기말에 계산기 가져가도 돼?")["citations"][0]["quote"]
    r = agent.answer(s, "What does the Operating Systems final exam cover?")
    assert "chapters 4-8" in r["citations"][0]["quote"]
    assert "absences" in agent.answer(s, "Operating Systems 출석 규정")["citations"][0]["quote"]


def test_question_rules_mode_is_honest_when_nothing_matches(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = agent.answer(s, "Operating Systems 제출 파일명 형식이 뭐야?")     # 그 과목 글에 형식 문장이 없다(다른 과목 글은 인용 안 함)
    assert r["kind"] == "question" and r["citations"] == []
    assert "찾지 못했어요" in r["answer"] and "제출 형식" in r["answer"]
    r = agent.answer(s, "zzqx")                                   # 날짜만 있는 글은 답이 아니다
    assert r["citations"] == [] and "찾지 못했어요" in r["answer"]


# ---------------------------------------------------------------- AI 작업 answer(22-2)

def _key_for(input_text, quote):
    """보낸 글에서 quote가 든 덩어리의 source_key."""
    for block in input_text.split("\n### source_key: ")[1:]:
        key, _, body = block.partition("\n")
        if " ".join(quote.split()) in " ".join(body.split()):
            return key.strip()
    return "announcement:999"


def _reply(body):
    out = json.dumps({"type": "result", "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)
    return out


def fake_runner(bodies, seen):
    """시도마다 bodies[i](input을 받아 답 dict를 만드는 함수)."""
    def run(args, input, timeout, cwd):
        seen.append(input)
        body = bodies[min(len(seen), len(bodies)) - 1](input)
        return subprocess.CompletedProcess(args, 0, _reply(body), "")
    return run


def ask_ai(s, message, bodies, seen=None):
    seen = [] if seen is None else seen
    return agent.answer(s, message, ai_enabled=True, runner=fake_runner(bodies, seen), command="claude"), seen


def test_answer_task_retries_bad_quote_then_accepts(tmp_path):
    s = make_store(tmp_path / "s.json")
    bad = lambda inp: {"answer": "기말 범위는 5장부터 9장까지예요.",
                       "citations": [{"source_key": "announcement:1", "quote": "기말 범위: 5~9장"}]}
    good = lambda inp: {"answer": "기말 범위는 5장부터 9장까지예요.",
                        "citations": [{"source_key": _key_for(inp, SCOPE), "quote": SCOPE}]}
    r, seen = ask_ai(s, "회로이론 기말 범위 어디까지야?", [bad, good])
    assert (r["kind"], r["by"], r["attempts"], r["error"]) == ("question", "ai", 2, None)
    assert "이전 답이 검사를 통과하지 못했습니다" in seen[1] and "그대로 없습니다" in seen[1]
    assert agent_cli.ANSWER_MARK in seen[0] and agent_cli.ASSIST_MARK not in seen[0]
    assert len(seen[0]) < agent_cli.MAX_TEXT + 6000
    c = r["citations"][0]
    assert c["quote"] == SCOPE and c["title"] == "기말고사 안내" and c["course_short"] == "회로이론"
    assert parse_iso(c["posted_at"]) == parse_iso(z(-5)) and r["proposals"] == []
    assert s.data["ai_runs"][-1]["task"] == "answer" and SCOPE not in json.dumps(s.data["ai_runs"], ensure_ascii=False)


def test_answer_task_final_attempt_drops_bad_citation(tmp_path):
    s = make_store(tmp_path / "s.json")
    both = lambda inp: {"answer": "기말 범위는 5장부터 9장까지예요.",
                        "citations": [{"source_key": _key_for(inp, SCOPE), "quote": SCOPE},
                                      {"source_key": "announcement:1", "quote": "범위는 교과서 전체입니다."}]}
    r, seen = ask_ai(s, "회로이론 기말 범위 어디까지야?", [both])
    assert r["by"] == "ai" and r["attempts"] == 2 and len(seen) == 2
    assert [c["quote"] for c in r["citations"]] == [SCOPE]


def test_answer_task_numbers_and_dates_must_be_in_quotes(tmp_path):
    s = make_store(tmp_path / "s.json")
    wrong = lambda inp: {"answer": "기말 범위는 5장부터 10장까지예요.",
                         "citations": [{"source_key": _key_for(inp, SCOPE), "quote": SCOPE}]}
    right = lambda inp: {"answer": "기말 범위는 5장부터 9장까지예요.",
                         "citations": [{"source_key": _key_for(inp, SCOPE), "quote": SCOPE}]}
    r, seen = ask_ai(s, "회로이론 기말 범위 어디까지야?", [wrong, right])
    assert r["by"] == "ai" and r["attempts"] == 2 and "10" in seen[1].split("INPUT START")[0]
    # 끝까지 인용에 없는 숫자 → AI 답을 버리고 규칙으로(인용은 규칙이 고른 문단)
    r, _ = ask_ai(s, "회로이론 기말 범위 어디까지야?", [wrong])
    assert r["by"] == "rules" and "검사를 통과하지 못했습니다" in r["error"] and "AI에게 묻지 못해" in r["answer"]
    assert SCOPE in r["citations"][0]["quote"]
    # 인용은 'Dec. 15', 답은 '12월 15일' — 같은 날짜면 된다. 계산한 날짜(하루 뒤)는 안 된다
    day = FINAL_DAY
    ok = lambda inp: {"answer": f"기말은 {day.month}월 {day.day}일 9:00, Room 210에서 봐요.",
                      "citations": [{"source_key": _key_for(inp, OS_FINAL), "quote": OS_FINAL}]}
    r, _ = ask_ai(s, "Operating Systems final exam room?", [ok])
    assert r["by"] == "ai" and r["attempts"] == 1 and r["citations"][0]["quote"] == OS_FINAL
    nxt = day + timedelta(days=1)
    off = lambda inp: {"answer": f"기말은 {nxt.month}월 {nxt.day}일에 봐요.",
                       "citations": [{"source_key": _key_for(inp, OS_FINAL), "quote": OS_FINAL}]}
    r, _ = ask_ai(s, "Operating Systems final exam room?", [off])
    assert r["by"] == "rules" and "날짜·숫자" in r["error"]


def test_answer_task_zero_citations_only_for_not_found(tmp_path):
    s = make_store(tmp_path / "s.json")
    nf = lambda inp: {"answer": "보낸 글에서 주차 안내는 찾지 못했어요.", "citations": []}
    r, _ = ask_ai(s, "회로이론 기말 시험장 주차 가능해?", [nf])
    assert r["by"] == "ai" and r["citations"] == [] and "찾지 못했어요" in r["answer"]
    claim = lambda inp: {"answer": "주차는 공학관 뒤편에 할 수 있어요.", "citations": []}
    r, seen = ask_ai(s, "회로이론 기말 시험장 주차 가능해?", [claim])
    assert r["by"] == "rules" and len(seen) == 2 and "인용(citations)이 없으면" in seen[1]


def test_answer_task_redacts_and_shows_original_quote(tmp_path):
    s = make_store(tmp_path / "s.json")
    red = "문의는 조교 이메일 [이메일] 로 보내 주세요."
    body = lambda inp: {"answer": "조교 이메일([이메일])로 문의하라고 적혀 있어요.",
                        "citations": [{"source_key": _key_for(inp, red), "quote": red}]}
    r, seen = ask_ai(s, "회로이론 조교 연락처 알려줘", [body])
    assert "ta.circuit@example.ac.kr" not in seen[0]                 # 가린 뒤 보냄
    assert r["by"] == "ai" and r["citations"][0]["quote"] == TA      # 화면에는 이 PC의 원문 인용(자리 표시 없이)
    assert r["citations"][0]["type"] == "inbox" and "[이메일]" not in r["answer"]


def test_answer_without_results_does_not_call_ai(tmp_path):
    """관련 글이 없으면 작업 answer(글 인용)를 부르지 않는다. 계약 24-2부터 학업 말이 없는 'zzqx'는 일반 대화(chat)라 AI가 켜져
    있으면 작업 chat이 한 번 답한다(예전: AI를 아예 부르지 않고 규칙 '찾지 못했어요')."""
    s = make_store(tmp_path / "s.json")
    r, seen = ask_ai(s, "zzqx", [lambda inp: {"answer": "무슨 말인지 잘 모르겠어요. 다시 말해 줄래요?"}])
    assert all(agent_cli.ANSWER_MARK not in x for x in seen) and len(seen) == 1 and agent_cli.CHAT_MARK in seen[0]
    assert (r["kind"], r["by"], r["attempts"], r["error"], r["citations"]) == ("chat", "ai", 1, None, [])
    assert "AI CLI를 켜면" not in r["answer"]


def test_mock_ai_answers_demo_question(tmp_path):
    """데모(가짜 Canvas + 가짜 AI): '기말 범위 어디까지야?' — 규칙·가짜 AI 모두 ASIC 'Final week schedule'의 문장을 인용."""
    from lms_agent.canvas import Canvas
    from lms_agent.features import agent as fa
    from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
    from lms_agent.server import App
    srv, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), s)
        app = App(s, tmp_path, cfg={"canvas_url": base, "token": DEMO_TOKEN}, demo=True,
                  ai_runner=mock_ai.runner, ai_command="claude")
        for ai in (False, True):
            s.data["settings"]["ai_cli"] = ai
            job = fa.ask(app, {"message": "기말 범위 어디까지야?"})["job"]
            assert fa.wait(app, job)
            th = app.state()["agent"]["threads"][-1]
            assert (th["kind"], th["by"], th["proposals"]) == ("question", "ai" if ai else "rules", [])
            c = th["citations"][0]
            assert c["title"] == "Final week schedule" and "lecture slides from week 9" in c["quote"]
            assert c["course_id"] == "101" and c["type"] == "announcement"
            assert DEMO_TOKEN not in json.dumps(app.state(), ensure_ascii=False)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 액션 제안 실행·되돌리기(22-9, 채팅 쪽)

CATALOG = [{"id": "autostart", "title": "컴퓨터를 켜면 앱 열기", "params": {"enabled": "bool"}, "client": False,
            "route": ("POST", "/api/autostart"), "undoable": True},
           {"id": "sync_now", "title": "지금 동기화", "params": {}, "client": False, "route": ("POST", "/api/sync"),
            "undoable": False},
           {"id": "open_view", "title": "화면 열기", "params": {"view": "str"}, "client": True, "route": None,
            "undoable": False}]


def fake_check(store, p):
    entry = next((c for c in CATALOG if c["id"] == p.get("action")), None)
    if entry is None:
        return {**p, "rejected_reason": "모르는 동작이에요"}
    return {**p, "title": p.get("title") or entry["title"], "client": entry["client"], "undoable": entry["undoable"],
            "rejected_reason": None}


def make_app(tmp_path, ai=False, runner=None):
    from lms_agent.server import App
    s = make_store(tmp_path / "state.json")
    s.data["settings"]["ai_cli"] = ai
    s.save()
    return App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": "canvas-test-token-0123456789abcdef"},
               demo=True, ai_runner=runner or mock_ai.runner, ai_command="claude")


@pytest.fixture
def fake_actions(monkeypatch):
    runs = []
    monkeypatch.setattr(actions, "CATALOG", CATALOG)
    monkeypatch.setattr(actions, "check", fake_check)
    monkeypatch.setattr(actions, "catalog_text", lambda: "- autostart {enabled: bool}: 컴퓨터를 켜면 앱 열기\n"
                                                         "- sync_now {}: 지금 동기화")

    def handle(store, message, course_id=None):
        if "자동 실행" not in message:
            return None
        return {"answer": "이렇게 할게요 — [실행]을 눌러 주세요.", "proposals": [
            {"type": "action", "action": "autostart", "params": {"enabled": True}, "reason": "요청하신 자동 실행 켜기"},
            {"type": "action", "action": "open_view", "params": {"view": "settings"}, "reason": "설정 화면도 열기"},
            {"type": "action", "action": "sync_now", "params": {}, "reason": "지금 동기화"},
            {"type": "action", "action": "logout", "params": {}, "reason": "로그아웃"}]}

    def run(app, p):
        lock = app.store.path.with_suffix(".lock")
        runs.append(("run", p["action"], dict(p["params"]), lock.exists(), p.get("_undo")))
        app.store.save()          # 실제 액션은 같은 API 경로를 불러 저장한다 — 잠금 밖에서 불러야 막히지 않는다
        return {"ok": True, "message": "켰어요", "undo": {"enabled": False}}

    def undo(app, p):
        runs.append(("undo", p["action"], dict(p["params"]), app.store.path.with_suffix(".lock").exists(),
                     p.get("_undo")))
        return {"ok": True, "message": "껐어요", "undo": None}

    monkeypatch.setattr(actions, "handle", handle)
    monkeypatch.setattr(actions, "run", run)
    monkeypatch.setattr(actions, "undo", undo)
    return runs


def test_action_proposals_apply_and_undo_through_api(tmp_path, fake_actions):
    from lms_agent.api import ApiError
    from lms_agent.features import agent as fa
    app = make_app(tmp_path)
    job = fa.ask(app, {"message": "컴퓨터 켤 때 자동 실행해줘"})["job"]
    assert fa.wait(app, job)
    th = app.state()["agent"]["threads"][-1]
    assert th["kind"] == "action" and th["by"] == "rules" and th["answer"].startswith("이렇게 할게요")
    auto, view, sync, logout = th["proposals"]
    assert (auto["type"], auto["action"], auto["params"], auto["title"]) == \
        ("action", "autostart", {"enabled": True}, "컴퓨터를 켜면 앱 열기")
    assert auto["can_apply"] and not auto["client"] and auto["undoable"] and auto["rejected_reason"] is None
    assert view["client"] and not view["can_apply"]                         # 화면이 직접 하는 동작
    assert logout["rejected_reason"] == "모르는 동작이에요" and not logout["can_apply"]
    # 실행: actions.run이 잠금 밖에서 불리고, 결과·되돌릴 값이 제안에 남는다(되돌릴 값은 화면에 안 나감)
    state = fa.apply_proposal(app, {"thread": job, "proposal": 0})
    p = state["agent"]["threads"][-1]["proposals"][0]
    assert p["applied"] and p["applied_at"] and p["result"] == {"ok": True, "message": "켰어요"} and not p["can_apply"]
    assert fake_actions == [("run", "autostart", {"enabled": True}, False, None)]
    assert "_undo" not in json.dumps(state, ensure_ascii=False)
    stored = Store(app.store.path).data["agent"]["threads"][-1]["proposals"][0]
    assert stored["_undo"] == {"enabled": False} and stored["applied"]
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 0})
    assert e.value.status == 409                                              # 두 번 실행하지 않는다
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 1})
    assert e.value.status == 400                                              # 화면 동작은 서버에서 실행 안 함
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 3})
    assert e.value.status == 400 and "실행할 수 없는" in str(e.value)
    # 되돌리기: actions.undo가 실행 때 남긴 값을 받는다
    state = fa.undo_proposal(app, {"thread": job, "proposal": 0})
    p = state["agent"]["threads"][-1]["proposals"][0]
    assert not p["applied"] and p["undone_at"] and p["can_apply"] and p["result"]["message"] == "껐어요"
    assert fake_actions[-1] == ("undo", "autostart", {"enabled": True}, False, {"enabled": False})
    assert "_undo" not in Store(app.store.path).data["agent"]["threads"][-1]["proposals"][0]
    with pytest.raises(ApiError) as e:
        fa.undo_proposal(app, {"thread": job, "proposal": 0})
    assert e.value.status == 409
    # 되돌릴 수 없는 동작
    fa.apply_proposal(app, {"thread": job, "proposal": 2})
    with pytest.raises(ApiError) as e:
        fa.undo_proposal(app, {"thread": job, "proposal": 2})
    assert e.value.status == 409 and "되돌릴 수 없는" in str(e.value)
    # 터미널(agent.apply)에서는 동작을 실행하지 않는다
    with pytest.raises(ValueError):
        agent.apply(app.store, job, 0)


def test_action_failures_are_recorded_and_retryable(tmp_path, fake_actions, monkeypatch):
    from lms_agent.api import ApiError
    from lms_agent.features import agent as fa
    app = make_app(tmp_path)
    job = fa.ask(app, {"message": "자동 실행 켜줘"})["job"]
    assert fa.wait(app, job)
    monkeypatch.setattr(actions, "run", lambda app, p: {"ok": False, "message": "등록하지 못했어요"})
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 0})
    assert e.value.status == 400 and "등록하지 못했어요" in str(e.value)
    p = app.state()["agent"]["threads"][-1]["proposals"][0]
    assert not p["applied"] and p["can_apply"] and p["result"] == {"ok": False, "message": "등록하지 못했어요"}

    def busy(app, p):
        raise ApiError(409, "동기화하는 중이에요")
    monkeypatch.setattr(actions, "run", busy)
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 0})
    assert e.value.status == 409

    def boom(app, p):
        raise RuntimeError("secret-value")
    monkeypatch.setattr(actions, "run", boom)
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 0})
    assert e.value.status == 500 and "secret-value" not in str(e.value)
    # 실행 직전 다시 검사해서 이제 안 되는 동작이면 실행하지 않는다
    monkeypatch.setattr(actions, "check", lambda store, p: {**p, "rejected_reason": "지금은 못 해요"})
    monkeypatch.setattr(actions, "run", lambda app, p: pytest.fail("실행하면 안 됨"))
    with pytest.raises(ApiError) as e:
        fa.apply_proposal(app, {"thread": job, "proposal": 0})
    assert e.value.status == 400 and "다시 확인해 보니" in str(e.value)


def test_action_routes_over_http(tmp_path, fake_actions):
    import urllib.error
    import urllib.request

    from lms_agent.features import agent as fa
    from lms_agent.server import serve_in_thread, start_server
    app = make_app(tmp_path)
    job = fa.ask(app, {"message": "자동 실행 켜줘"})["job"]
    assert fa.wait(app, job)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-LMS-Agent": app.token})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
    try:
        code, state = post("/api/agent/apply", {"thread": job, "proposal": 0})
        assert code == 200 and state["agent"]["threads"][-1]["proposals"][0]["applied"]
        assert post("/api/agent/apply", {"thread": job, "proposal": 1})[0] == 400
        assert post("/api/agent/apply", {"thread": "t-nope", "proposal": 0})[0] == 404
        assert post("/api/agent/apply", {"thread": job, "proposal": 9})[0] == 400
        code, state = post("/api/agent/undo", {"thread": job, "proposal": 0})
        assert code == 200 and not state["agent"]["threads"][-1]["proposals"][0]["applied"]
    finally:
        httpd.shutdown()


def test_assist_can_propose_action_checked_by_actions(tmp_path, fake_actions):
    s = make_store(tmp_path / "s.json")
    seen = []

    def body(inp):
        return {"answer": "동기화하면 새 공지를 다시 읽어요.", "proposals": [
            {"type": "action", "action": "sync_now", "params": {}, "reason": "새 공지를 읽으려면 동기화가 필요해요"},
            {"type": "action", "action": "delete_everything", "params": {}, "reason": "x"}]}
    r = agent.answer(s, "회로이론 기말고사 날짜가 안 보여요", ai_enabled=True, runner=fake_runner([body], seen),
                     command="claude")
    assert r["kind"] == "missing" and r["by"] == "ai" and r["attempts"] == 2   # 모르는 동작 → 다시 묻고, 마지막엔 단추 없이
    assert agent_cli.ACTIONS_MARK in seen[0] and "sync_now {}: 지금 동기화" in seen[0]
    sync, bad = r["proposals"]
    assert sync["type"] == "action" and sync["rejected_reason"] is None and sync["by"] == "ai"
    assert sync["title"] == "지금 동기화" and not sync["client"] and agent.can_apply(sync)
    assert bad["rejected_reason"] == "모르는 동작이에요" and not agent.can_apply(bad)
