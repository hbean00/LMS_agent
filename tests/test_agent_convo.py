"""이어지는 대화(계약 24-1)와 일반 대화(24-2): 대화 만들기·잇기·하나만 지우기, state.agent.conversations, 옛 차례, 과목·주제
이어받기, AI 입력의 '이전 대화'(가림·1,500자·근거 아님), 작업 chat(가짜 실행기)·규칙 답, 학업 질문은 그대로, 위험한 말.
가짜 데이터만 쓴다(과목·문장·주소·이름 모두 지어낸 것). 시각은 같은 순간인지로 비교한다(CI는 UTC)."""
import json
import re
import subprocess
from datetime import datetime, time, timedelta

import pytest

from lms_agent import actions, agent, agent_cli, core, maildraft, mock_ai, plan, whatif
from lms_agent.api import ApiError
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

SCOPE = "기말고사 범위는 5장부터 9장까지입니다."
MID_SCOPE = "중간고사 범위는 1장부터 4장까지입니다."
ROOM = "시험 장소: 공학관 101호"
TA = "문의는 조교 이메일 ta.circuit@example.ac.kr 로 보내 주세요."
OS_ATT = "Attendance is checked every class; more than 3 absences result in F."
SECRET = "canvas-test-token-0123456789abcdefABCDEF"


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


def make_cache():
    fin = T + timedelta(days=20)
    return {
        "fetched_at": z(0),
        "courses": [{"id": "301", "name": "회로이론", "course_code": "EE301", "syllabus_body": None},
                    {"id": "302", "name": "Operating Systems", "course_code": "CS302", "syllabus_body": None}],
        "assignments": [],
        "announcements": [
            ann(1, "301", z(-5), "기말고사 안내", f"<p>{SCOPE}</p><p>{ROOM}</p>"),
            ann(2, "301", z(-40), "중간고사 안내", f"<p>{MID_SCOPE}</p>"),
            ann(3, "302", z(-30), "Attendance", f"<p>{OS_ATT}</p>"),
            ann(4, "302", z(-3), "Final exam", f"<p>The final exam is on {MONTHS[fin.month - 1]}. {fin.day} at 9:00 "
                                               "in Room 210.</p><p>The final exam covers chapters 4-8.</p>"),
        ],
        "conversations": [
            {"id": 50, "subject": "조교 안내", "context_code": "course_301", "context_name": "회로이론",
             "last_message_at": z(-8), "messages": [{"id": 501, "created_at": z(-8), "body": TA}]},
            {"id": 51, "subject": "중간고사 성적", "context_code": "course_301", "context_name": "회로이론",
             "last_message_at": z(-6), "messages": [{"id": 511, "created_at": z(-6),
                                                     "body": "중간고사 본인 점수 71점 (평균 60.2, 표준편차 9.5)입니다."}]},
        ],
        "boards": [],
        "pdf_texts": {},
        "errors": [],
    }


def make_store(path, cache=None):
    s = Store(path)
    s.data["cache"] = cache or make_cache()
    core.rebuild(s)
    s.save()
    return s


def make_app(tmp_path, ai=False, runner=None):
    from lms_agent.server import App
    s = make_store(tmp_path / "state.json")
    s.data["settings"]["ai_cli"] = ai
    s.save()
    return App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": SECRET, "user_name": "김가짜"},
               demo=True, ai_runner=runner or mock_ai.runner, ai_command="claude")


@pytest.fixture(autouse=True)
def _other_handles_quiet(request, monkeypatch):
    """명령·메일·계획·만약에 handle의 낱말 규칙에 흔들리지 않게 None으로(데모 시험·handle 시험은 그대로)."""
    if "demo" in request.node.name or "handles" in request.node.name:
        return
    for mod in (actions, maildraft, plan, whatif):
        monkeypatch.setattr(mod, "handle", lambda *a, **k: None)


def _reply(body) -> str:
    return json.dumps({"type": "result", "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)


def fake_runner(seen, chat=None, answer=None, assist=None):
    """작업별 가짜 답: 각 인자는 (input, 그 작업의 몇 번째 호출) → dict."""
    counts = {}

    def run(args, input, timeout, cwd):
        seen.append(input)
        task = ("chat" if agent_cli.CHAT_MARK in input else "answer" if agent_cli.ANSWER_MARK in input
                else "assist")
        counts[task] = counts.get(task, 0) + 1
        fn = {"chat": chat, "answer": answer, "assist": assist}[task]
        body = fn(input, counts[task]) if fn else {"answer": "모르겠어요.", "citations": [], "proposals": []}
        return subprocess.CompletedProcess(args, 0, _reply(body), "")
    return run


def thread(app, job):
    return next(t for t in app.state()["agent"]["threads"] if t["id"] == job)


def ask(app, fa, message, conversation=None, **kw):
    body = {"message": message, **kw}
    if conversation is not None:
        body["conversation"] = conversation
    r = fa.ask(app, body)
    assert fa.wait(app, r["job"])
    return r, thread(app, r["job"])


HIST = [{"id": "t-prev", "message": "회로이론 기말 범위 어디까지야?", "answer": f"이 글에 이렇게 적혀 있어요: “{SCOPE}”",
         "kind": "question", "course_id": "301", "cited": [("announcement:1", SCOPE)]}]


# ---------------------------------------------------------------- 대화 만들기·잇기·지우기(24-1)

def test_ask_creates_continues_and_clears_one_conversation(tmp_path):
    from lms_agent.features import agent as fa
    app = make_app(tmp_path)
    r1, th1 = ask(app, fa, "회로이론 기말 범위 어디까지야?")
    conv = r1["conversation"]
    assert re.fullmatch(r"c-[0-9a-f]{10}", conv) and th1["conversation"] == conv and set(r1) == {"job", "conversation", "agent"}
    assert th1["carried"] is None and th1["course_context"] == "301"
    r2, th2 = ask(app, fa, "그럼 중간은?", conv)
    assert r2["conversation"] == conv and th2["conversation"] == conv
    assert th2["carried"]["course_id"] == "301" and th2["carried"]["course_short"] == "회로이론"
    assert th2["carried"]["from"] == th1["id"] and "범위" in th2["carried"]["message"]
    assert th2["kind"] == "question" and MID_SCOPE in th2["citations"][0]["quote"]
    # 모르는 id·잘못된 모양 → 신규 대화
    r3, _ = ask(app, fa, "Operating Systems 출석 규정", "c-0000000000")
    r4, _ = ask(app, fa, "Operating Systems final exam room?", 123)
    assert len({conv, r3["conversation"], r4["conversation"]}) == 3
    st = app.state()["agent"]
    convs = st["conversations"]
    assert [c["id"] for c in convs] == [r4["conversation"], r3["conversation"], conv]   # 최근 먼저
    first = convs[-1]
    assert set(first) == {"id", "title", "started_at", "last_at", "turns", "legacy", "running"}
    assert (first["title"], first["turns"], first["legacy"], first["running"]) == ("회로이론 기말 범위 어디까지야?", 2, False, False)
    assert parse_iso(first["started_at"]) == parse_iso(th1["at"]) and parse_iso(first["last_at"]) >= parse_iso(th2["at"])
    assert st["limits"]["history_turns"] == 6 and st["limits"]["max_threads"] == 80 and st["limits"]["per_ask"] == 2
    # 그 대화만 지우기
    state = fa.clear(app, {"conversation": conv})
    assert not [t for t in state["agent"]["threads"] if t["conversation"] == conv]
    assert {c["id"] for c in state["agent"]["conversations"]} == {r3["conversation"], r4["conversation"]}
    with pytest.raises(ApiError) as e:
        fa.clear(app, {"conversation": "c-nope"})
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:
        fa.clear(app, {"conversation": 5})
    assert e.value.status == 400
    state = fa.clear(app, {})                                          # 본문이 없으면 전부
    assert state["agent"]["threads"] == [] and state["agent"]["conversations"] == []
    assert SECRET not in json.dumps(state, ensure_ascii=False)


def test_legacy_threads_trimming_and_conversation_limits(tmp_path):
    s = make_store(tmp_path / "s.json")
    old = agent.new_thread(s, "옛 버전에서 물어본 아주 긴 질문입니다 — 기말고사 날짜와 범위와 장소가 모두 궁금해요", None)
    del old["conversation"], old["carried"], old["course_context"]
    old.update(status="done", by="rules", answer="옛 답", kind="question")
    pub = agent.public_threads(s)
    assert pub[0]["conversation"] == old["id"] and pub[0]["carried"] is None and pub[0]["course_context"] is None
    c = agent.conversations(s)[0]
    assert (c["id"], c["legacy"], c["turns"]) == (old["id"], True, 1)
    assert len(c["title"]) == 40 and c["title"].endswith("…")
    nxt = agent.new_thread(s, "이어서 물어봐요", None, conversation=old["id"])   # 옛 대화(차례 id)도 이어갈 수 있다
    assert nxt["conversation"] == old["id"] and agent.conversations(s)[0]["turns"] == 2
    assert [h["message"] for h in agent.history_for(s, nxt["id"])] == [old["message"]]
    # 최근 MAX_CONVERSATIONS(20)개만, 최근 먼저
    for i in range(25):
        agent.new_thread(s, f"대화 {i}", None)
    convs = agent.conversations(s)
    assert len(convs) == 20 and convs[0]["title"] == "대화 24" and convs[-1]["title"] == "대화 5"
    # 차례는 최근 80개만 — 한 대화가 길어도 묶음은 그대로
    agent._box(s)["threads"] = []
    first = agent.new_thread(s, "q0", None)
    for i in range(1, 85):
        agent.new_thread(s, f"q{i}", None, conversation=first["conversation"])
    assert len(agent.threads(s)) == 80
    only = agent.conversations(s)
    assert len(only) == 1 and only[0]["turns"] == 80 and only[0]["title"] == "q5"


def test_history_for_keeps_last_six_done_turns_of_the_same_conversation(tmp_path):
    s = make_store(tmp_path / "s.json")
    a = agent.new_thread(s, "a0", None)
    a.update(status="done", answer="답0", kind="question", course_context="301")
    for i in range(1, 8):
        t = agent.new_thread(s, f"a{i}", None, conversation=a["conversation"])
        t.update(status="error" if i == 3 else "done", answer=f"답{i}", kind="question")
    agent.new_thread(s, "다른 대화", None).update(status="done", answer="x")
    me = agent.new_thread(s, "지금", None, conversation=a["conversation"])
    hist = agent.history_for(s, me["id"])
    assert [h["message"] for h in hist] == ["a1", "a2", "a4", "a5", "a6", "a7"]   # 오류 차례·다른 대화는 빼고 최근 6개
    assert set(hist[0]) >= {"message", "answer", "kind", "course_id", "effective", "cited"}
    assert agent.history_for(s, a["id"]) == []


# ---------------------------------------------------------------- 과목·주제 이어받기(24-1)

def test_followup_merge_rules():
    courses = make_cache()["courses"]
    cases = [("그럼 중간은?", "회로이론 기말 범위 어디까지야?", ["중간", "범위"], ["기말", "회로이론"]),
             ("그건 언제야?", "LAB4 제출 형식이 뭐야?", ["언제", "LAB4"], ["형식"]),
             ("중간은?", "기말고사 날짜가 안 보여요", ["중간", "날짜가 안 보여요"], ["기말"]),
             ("LAB5도?", "LAB4 연장 문의 메일 써줘", ["LAB5", "연장 문의 메일 써줘"], ["LAB4"]),
             ("또 다른 건?", "회로이론 기말 범위 어디까지야?", ["기말", "범위"], ["또", "다른"])]
    for cur, prev, has, hasnt in cases:
        eff = agent.merge_followup(cur, prev, courses)
        assert agent.is_followup(cur), cur
        assert all(x in eff for x in has) and not any(x in eff for x in hasnt), (cur, eff)
    eff = agent.merge_followup("그럼 B+는?", "기말에서 몇 점 받아야 A?", courses)   # 학점 글자는 이번 것으로 바꾼다
    assert "B+" in eff and "기말" in eff and "받아야" in eff and not re.search(r"(?<![A-Za-z])A(?![A-Za-z+0])", eff)
    assert agent.classify(agent.merge_followup("중간은?", "기말고사 날짜가 안 보여요", courses)) == "missing"
    assert agent.classify(agent.merge_followup("그건 언제야?", "LAB4 제출 형식이 뭐야?", courses)) == "missing"
    for msg in ("회로이론 기말 범위 어디까지야?", "기말고사 날짜가 안 보여요", "Operating Systems final exam covers what chapters?"):
        assert not agent.is_followup(msg)


def test_course_carry_over_topic_merge_and_fallback(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = agent.answer(s, "시험 장소는 어디야?", history=HIST)              # 과목을 말하지 않음 → 앞 차례의 과목
    assert r["carried"] == {"from": "t-prev", "course_id": "301", "course_short": "회로이론", "message": None}
    assert r["kind"] == "question" and "공학관 101호" in r["citations"][0]["quote"] and r["course_context"] == "301"
    assert any("앞 대화의 과목 '회로이론'" in n for n in r["notes"])
    r = agent.answer(s, "그럼 중간은?", history=HIST)                     # 생략 질문 → 앞 질문의 주제(범위)
    assert r["carried"]["message"] and "범위" in r["carried"]["message"]
    assert r["kind"] == "question" and MID_SCOPE in r["citations"][0]["quote"]
    assert all(c["course_id"] == "301" for c in r["citations"])
    r = agent.answer(s, "또 다른 건?", history=HIST)                      # 앞에서 인용한 문단은 빼고
    assert all(SCOPE not in c["quote"] for c in r["citations"])
    # 과목 이름을 말하면·직접 고르면 이어받지 않는다
    r = agent.answer(s, "Operating Systems 출석 규정", history=HIST)
    assert r["carried"] is None and OS_ATT in r["citations"][0]["quote"]
    r = agent.answer(s, "기말 범위", course_id="302", history=HIST)
    assert r["carried"] is None and "chapters 4-8" in r["citations"][0]["quote"]
    # 이어받은 과목에 관련 글이 없으면 모든 과목에서 다시 찾는다(이어받기 취소)
    r = agent.answer(s, "출석 규정 알려줘", history=HIST)
    assert r["carried"] is None and OS_ATT in r["citations"][0]["quote"] and r["course_context"] == "302"
    # 일반 대화 차례는 건너뛰고 그 앞 학업 차례를 이어받는다, 일반 대화는 아무것도 이어받지 않는다
    chatty = HIST + [{"id": "t-chat", "message": "고마워", "answer": "천만에요!", "kind": "chat", "course_id": None}]
    r = agent.answer(s, "그럼 중간은?", history=chatty)
    assert r["carried"]["from"] == "t-prev" and MID_SCOPE in r["citations"][0]["quote"]
    r = agent.answer(s, "고마워요", history=HIST)
    assert r["kind"] == "chat" and r["carried"] is None and r["course_context"] is None


def test_followup_passes_carried_course_and_message_to_handles(tmp_path, monkeypatch):
    s = make_store(tmp_path / "s.json")
    calls = []

    def rec(kind):
        def handle(store, message, course_id=None, *rest):
            calls.append((kind, message, course_id))
            return None
        return handle
    for kind, mod in (("action", actions), ("mail", maildraft), ("plan", plan), ("whatif", whatif)):
        monkeypatch.setattr(mod, "handle", rec(kind))
    hist = [{"id": "t-w", "message": "회로이론 기말에서 몇 점 받아야 A?", "answer": "…", "kind": "whatif",
             "course_id": "301"}]
    agent.answer(s, "그럼 B+는?", history=hist)
    got = {k: (m, c) for k, m, c in calls}
    assert got["whatif"][1] == "301" and got["mail"][1] == "301" and got["plan"][1] == "301"
    assert "B+" in got["whatif"][0] and "기말" in got["whatif"][0] and "받아야" in got["whatif"][0]
    assert got["action"][1] == "301"                                  # 생략 질문이면 명령도 그 과목
    calls.clear()
    agent.answer(s, "기말에서 몇 점 받아야 B0?", history=hist)            # 생략 질문이 아님: 과목만 이어받음
    got = {k: (m, c) for k, m, c in calls}
    assert got["whatif"] == ("기말에서 몇 점 받아야 B0?", "301") and got["action"][1] is None
    calls.clear()
    agent.answer(s, "기말에서 몇 점 받아야 B0?", course_id="302", history=hist)   # 직접 고른 과목이 이긴다
    assert {c for _, _, c in calls} == {"302"}


# ---------------------------------------------------------------- AI 입력의 '이전 대화'(가림·1,500자·근거 아님)

def _history_section(text):
    start = text.index(agent_cli.HISTORY_MARK)
    end = text.find("\n\n", start)
    return text[start:end if end >= 0 else len(text)]


def test_history_in_ai_inputs_is_redacted_capped_and_never_evidence(tmp_path):
    s = make_store(tmp_path / "s.json")
    fake_day = T + timedelta(days=25)
    made_up = f"기말은 {fake_day.month}월 {fake_day.day}일 오전 9시에 봅니다."
    hist = [{"id": f"t-{i}", "message": f"질문 {i}: 김가짜예요, 연락은 me{i}@example.ac.kr", "kind": "question",
             "answer": f"{made_up} 조교 메일 ta.circuit@example.ac.kr. " + "길게 " * 200, "course_id": "301"}
            for i in range(6)]
    seen = []
    assist = lambda inp, n: {"answer": "앞 대화에서 날짜를 봤어요.", "proposals": [
        {"type": "add_deadline", "course_id": "301", "evidence": made_up, "title": "기말고사", "kind": "시험",
         "due": iso(datetime.combine(fake_day, time(9, 0), tzinfo=TZ)), "reason": "앞 대화"}]}
    r = agent.answer(s, "회로이론 기말고사 날짜가 안 보여요", ai_enabled=True, runner=fake_runner(seen, assist=assist),
                     command="claude", names=["김가짜"], history=hist)
    sec = _history_section(seen[0])
    assert len(sec) <= agent_cli.HISTORY_MAX and "질문 5" in sec                  # 최근 차례부터 1,500자 안에
    assert "@example.ac.kr" not in seen[0] and "김가짜" not in seen[0]           # 가린 뒤 보냄
    assert r["kind"] == "missing" and r["attempts"] == 2                          # 이전 답의 문장은 근거가 아니다
    assert "보낸 글에 그대로 없어요" in r["proposals"][0]["rejected_reason"]
    # 작업 answer: 이전 답을 인용하면 다시 묻고, 끝까지 그러면 그 인용은 버린다
    seen.clear()
    ans = lambda inp, n: {"answer": "범위는 5장부터 9장까지예요.", "citations": [{"source_key": "announcement:1",
                                                                       "quote": made_up}]}
    r = agent.answer(s, "회로이론 기말 범위 어디까지야?", ai_enabled=True, runner=fake_runner(seen, answer=ans),
                     command="claude", history=hist)
    assert agent_cli.HISTORY_MARK in seen[0] and all(made_up not in c["quote"] for c in r["citations"])
    # 생략 질문은 친 그대로 + 합친 뜻 한 줄
    seen.clear()
    agent.answer(s, "그럼 중간은?", ai_enabled=True, runner=fake_runner(seen), command="claude", history=HIST)
    head = seen[0].split(agent_cli.ANSWER_MARK, 1)[1].split("\n")
    assert head[1] == "그럼 중간은?" and head[2].startswith(agent_cli.FOLLOW_PREFIX) and "범위" in head[2]
    # 이전 대화가 없으면 그 덩어리도 없다
    seen.clear()
    agent.answer(s, "회로이론 기말 범위 어디까지야?", ai_enabled=True, runner=fake_runner(seen), command="claude")
    assert agent_cli.HISTORY_MARK not in seen[0]


# ---------------------------------------------------------------- 일반 대화 — AI 켜짐(24-2)

def test_chat_with_ai_uses_chat_task_without_proposals_or_private_data(tmp_path):
    s = make_store(tmp_path / "s.json")
    s.data["gcal_events"] = {"events": [{"summary": "비밀 약속 장소", "start": z(1)}]}
    seen = []
    run = fake_runner(seen, chat=lambda inp, n: mock_ai._chat(inp))
    r = agent.answer(s, "안녕!", ai_enabled=True, runner=run, command="claude", names=["김가짜"])
    assert (r["kind"], r["by"], r["intent"], r["attempts"], r["error"]) == ("chat", "ai", "greeting", 1, None)
    assert "안녕하세요" in r["answer"] and r["proposals"] == [] and r["citations"] == [] and r["results"] == []
    sent = seen[0]
    assert agent_cli.CHAT_MARK in sent and agent_cli.ANSWER_MARK not in sent and agent_cli.ASSIST_MARK not in sent
    assert "이번 주 마감 / this week:" in sent and "오늘 / now:" in sent
    for private in ("60.2", "71점", "비밀 약속", SECRET, "ta.circuit"):
        assert private not in sent                                    # 성적 숫자·다른 일정·토큰·주소 없음
    assert s.data["ai_runs"][-1]["task"] == "chat" and "안녕" not in json.dumps(s.data["ai_runs"], ensure_ascii=False)
    # 이전 대화를 받는다
    seen.clear()
    r = agent.answer(s, "오늘 저녁 뭐 먹지?", ai_enabled=True, runner=run, command="claude", history=HIST)
    assert r["kind"] == "chat" and agent_cli.HISTORY_MARK in seen[0] and r["answer"].startswith("앞 이야기에 이어서")


def test_chat_validation_claims_facts_length_and_links(tmp_path):
    s = make_store(tmp_path / "s.json")

    def run(chat):
        seen = []
        return agent.answer(s, "오늘 저녁 뭐 먹지?", ai_enabled=True, runner=fake_runner(seen, chat=chat),
                            command="claude"), seen
    # 앱 동작을 했다고 하면 다시 묻고, 끝까지 그러면 규칙 답
    r, seen = run(lambda inp, n: {"answer": "자동 실행을 켰어요. 이제 편해요."})
    assert r["by"] == "rules" and len(seen) == 2 and "앱 동작을 했다고" in seen[1] and "검사를 통과하지" in r["error"]
    assert "AI에게 묻지 못했어요" in r["answer"]
    # 지어낸 과목 날짜·학점·점수 → 다시 묻기
    day = T + timedelta(days=40)
    r, seen = run(lambda inp, n: {"answer": f"기말은 {day.month}월 {day.day}일이에요. A+ 받을 거예요."} if n == 1 else
                  {"answer": "저녁은 따뜻한 국밥 어때요? 든든하게 먹고 다시 달려요."})
    assert r["by"] == "ai" and r["attempts"] == 2 and "지어내면 안 됩니다" in seen[1] and "국밥" in r["answer"]
    # 묻는 말('잘 보냈어요?')은 했다는 말이 아니다
    r, seen = run(lambda inp, n: {"answer": "주말 잘 보냈어요? 오늘은 가볍게 먹고 푹 쉬어요."})
    assert r["by"] == "ai" and r["attempts"] == 1
    # 8문장·링크: 다시 묻고, 마지막엔 잘라 낸다
    long = " ".join(f"{i}번째 문장이에요." for i in range(1, 12)) + " https://example.com 참고"
    r, seen = run(lambda inp, n: {"answer": long})
    assert r["by"] == "ai" and r["attempts"] == 2 and "8문장" in seen[1]
    assert "https://" not in r["answer"] and "9번째" not in r["answer"] and "8번째" in r["answer"]
    assert len(r["answer"]) <= agent_cli.CHAT_MAX


def test_chat_daily_cap_falls_back_to_rules(tmp_path):
    s = make_store(tmp_path / "s.json")
    s.data["ai_runs"] = [{"at": iso(now_local()), "task": "triage_review", "ok": True, "attempts": 30}]
    seen = []
    r = agent.answer(s, "안녕", ai_enabled=True, runner=fake_runner(seen), command="claude")
    assert seen == [] and (r["kind"], r["by"]) == ("chat", "rules") and "한도" in r["error"]
    assert r["answer"].startswith("안녕하세요")


def test_general_addendum_for_academic_question_without_course_text(tmp_path):
    s = make_store(tmp_path / "s.json")
    seen = []
    nf = lambda inp, n: {"answer": "보낸 글에서 주차 안내는 찾지 못했어요.", "citations": []}
    gen = lambda inp, n: {"answer": "일반적으로 큰 시험장 근처는 주차가 어려워요. 대중교통을 권해요."}
    r = agent.answer(s, "회로이론 기말 시험장 주차 가능해?", ai_enabled=True, runner=fake_runner(seen, answer=nf, chat=gen),
                     command="claude")
    assert (r["kind"], r["by"], r["citations"], r["attempts"]) == ("question", "ai", [], 2)
    assert r["general"] == {"answer": "일반적으로 큰 시험장 근처는 주차가 어려워요. 대중교통을 권해요.", "by": "ai"}
    assert r["answer"].startswith("보낸 글에서 주차 안내는 찾지 못했어요.") and agent.GENERAL_HEAD in r["answer"]
    assert agent_cli.CHAT_MARK in seen[1] and "모드 / mode: general" in seen[1]
    # 한 번 묻기에 AI는 2회까지: 질문 답에 2회를 다 쓰면 덧붙이지 않는다
    seen.clear()
    claim = lambda inp, n: {"answer": "주차는 공학관 뒤편에 할 수 있어요.", "citations": []}
    r = agent.answer(s, "회로이론 기말 시험장 주차 가능해?", ai_enabled=True, runner=fake_runner(seen, answer=claim, chat=gen),
                     command="claude")
    assert len(seen) == 2 and r["by"] == "rules" and "general" not in r and r["kind"] == "question"
    # AI가 꺼져 있으면 지금처럼 '찾지 못했어요'만
    r = agent.answer(s, "Operating Systems 제출 파일명 형식이 뭐야?")
    assert r["kind"] == "question" and "찾지 못했어요" in r["answer"] and "general" not in r


# ---------------------------------------------------------------- 일반 대화 — AI 꺼짐(규칙, 24-2)

def test_chat_rules_mode_replies(tmp_path):
    s = make_store(tmp_path / "s.json")
    before = now_local()
    replies = {m: agent.answer(s, m) for m in ("안녕", "고마워요", "과제가 너무 많아서 힘들어", "오늘 며칠이야?", "지금 몇 시야?",
                                               "뭘 할 수 있어?", "오늘 저녁 뭐 먹지?", "zzqx")}
    after = now_local()
    for m, r in replies.items():
        assert (r["kind"], r["by"], r["proposals"], r["citations"], r["attempts"]) == ("chat", "rules", [], [], 0), m
    assert not s.data.get("ai_runs")
    hi = replies["안녕"]["answer"]
    assert hi.startswith("안녕하세요") and "이번 주" in hi and replies["안녕"]["intent"] == "greeting"
    assert replies["고마워요"]["answer"].startswith("천만에요")
    cheer = replies["과제가 너무 많아서 힘들어"]
    assert cheer["intent"] == "cheer" and "지쳤" in cheer["answer"] and "다시 같이 가요" in cheer["answer"]
    days = {f"{d.month}월 {d.day}일 {'월화수목금토일'[d.weekday()]}요일" for d in (before, after)}
    assert any(d in replies["오늘 며칠이야?"]["answer"] for d in days)
    assert re.search(r"(오전|오후) \d{1,2}시 \d{1,2}분이에요", replies["지금 몇 시야?"]["answer"])
    assert "기말 범위 어디까지야?" in replies["뭘 할 수 있어?"]["answer"] and agent.AI_OFF_CHAT in replies["뭘 할 수 있어?"]["answer"]
    food = replies["오늘 저녁 뭐 먹지?"]["answer"]
    assert agent.AI_OFF_CHAT in food and all(x in food for x in agent.CHAT_EXAMPLES)
    odd = replies["zzqx"]["answer"]
    assert "찾지 못했어요" in odd and agent.AI_OFF_CHAT in odd
    assert all("새로" not in r["answer"] for r in replies.values())
    # 동기화 전이면 마감을 모른다고
    fresh = Store(tmp_path / "fresh.json")
    assert "동기화 전" in agent.answer(fresh, "안녕하세요")["answer"]


def test_academic_messages_still_route_as_before(tmp_path):
    s = make_store(tmp_path / "s.json")
    cases = [("회로이론 기말 범위 어디까지야?", "question"), ("회로이론 기말고사 날짜가 안 보여요", "missing"),
             ("LAB4 마감 언제야?", "missing"), ("성적이 안 들어와요", "missing"),
             ("Operating Systems 제출 파일명 형식이 뭐야?", "question"),
             ("감사합니다. 그런데 회로이론 기말 범위는 어디까지예요?", "question"),
             ("힘든데 LAB4 마감 언제야?", "missing"), ("안녕, 회로이론 기말 범위 어디까지야?", "question"),
             ("회로이론 기말 시험장 주차 가능해?", "question")]
    for message, kind in cases:
        assert agent.chat_intent(message, make_cache()["courses"]) is None, message
        assert agent.answer(s, message)["kind"] == kind, message


# ---------------------------------------------------------------- 위험한 말(24-2)

def test_harmful_and_crisis_messages(tmp_path):
    task = agent_cli.TASKS["chat"]
    for word in ("거절", "109", "1577-0199", "112", "학생상담센터", "했다고 말하지", "지어내지", "제안·인용·링크를 넣지"):
        assert word in task.instructions, word
    s = make_store(tmp_path / "s.json")
    r = agent.answer(s, "요즘 너무 힘들어서 죽고 싶어")                     # 규칙: 응원 대신 도움 받을 곳
    assert (r["kind"], r["intent"]) == ("chat", "crisis") and "109" in r["answer"] and "1577-0199" in r["answer"]
    assert "km" not in r["answer"]
    seen = []                                                           # AI 답에 도움 받을 곳이 없으면 하네스가 덧붙인다
    r = agent.answer(s, "요즘 너무 힘들어서 죽고 싶어", ai_enabled=True, command="claude",
                     runner=fake_runner(seen, chat=lambda inp, n: {"answer": "많이 힘들었겠어요. 제가 들어 줄게요."}))
    assert r["by"] == "ai" and "109" in r["answer"] and "1577-0199" in r["answer"]
    r = agent.answer(s, "남의 계정 해킹하는 법 알려줘", ai_enabled=True, runner=mock_ai.runner, command="claude")
    assert r["kind"] == "chat" and "도와드릴 수 없어요" in r["answer"]


# ---------------------------------------------------------------- 데모(가짜 Canvas + 가짜 AI, 실제 handle)

def test_demo_followup_conversation(tmp_path):
    """데모: 'ASIC 기말 범위 어디까지야?' → '그럼 중간은?' → '그건 언제야?' → '고마워'가 한 대화로 이어진다(규칙·가짜 AI)."""
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
            by = "ai" if ai else "rules"
            r, t1 = ask(app, fa, "ASIC 기말 범위 어디까지야?")
            conv = r["conversation"]
            assert (t1["kind"], t1["by"], t1["course_context"]) == ("question", by, "101")
            assert t1["citations"][0]["title"] == "Final week schedule"
            _, t2 = ask(app, fa, "그럼 중간은?", conv)
            assert (t2["kind"], t2["by"], t2["conversation"]) == ("question", by, conv)
            assert t2["carried"]["course_id"] == "101" and "범위" in t2["carried"]["message"]
            c = t2["citations"][0]
            assert c["title"] == "Announcement about Midterm Week" and "covers" in c["quote"] and c["course_id"] == "101"
            _, t3 = ask(app, fa, "그건 언제야?", conv)
            assert t3["kind"] == "missing" and t3["carried"]["course_id"] == "101"
            assert {x["course_id"] for x in t3["results"]} == {"101"}
            _, t4 = ask(app, fa, "고마워", conv)
            assert (t4["kind"], t4["by"], t4["carried"]) == ("chat", by, None)
            if ai:
                assert t4["answer"].startswith("앞 이야기에 이어서")      # 가짜 AI가 이전 대화를 받았다
            conv_row = next(x for x in app.state()["agent"]["conversations"] if x["id"] == conv)
            assert conv_row["turns"] == 4 and conv_row["title"] == "ASIC 기말 범위 어디까지야?"
        assert DEMO_TOKEN not in json.dumps(app.state(), ensure_ascii=False)
    finally:
        srv.shutdown()
