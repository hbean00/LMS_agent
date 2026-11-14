"""에이전트에게 묻기(채팅, 계약 20장): 검색·검증·반영/되돌리기·agent_items 합치기·AI 꺼짐·작업·CLI·하루 한도.
가짜 데이터만 쓴다(과목·문장·학번 모두 지어낸 것)."""
from datetime import datetime, time, timedelta

import pytest

from lms_agent import agent, core
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()


def z(days, h=10, mi=0):
    """오늘 기준 로컬 시각 → ISO(+오프셋)."""
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def md_en(days):
    d = T + timedelta(days=days)
    return f"{['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.month - 1]}. {d.day}"


def md_ko(days):
    d = T + timedelta(days=days)
    return f"{d.month}월 {d.day}일"


def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


def make_cache():
    return {
        "fetched_at": z(0),
        "courses": [
            {"id": "201", "name": "262R (서울-학부)신호와시스템(영강)(Signals and Systems)-00분반", "course_code": "EE201",
             "syllabus_body": None},
            {"id": "202", "name": "열역학", "course_code": "ME202",
             "syllabus_body": "<p>평가: 중간고사 30%, 기말고사 40%, 과제 30%</p>"},
            {"id": "203", "name": "현대미술의 이해", "course_code": "AR203", "syllabus_body": None},
        ],
        "assignments": [],
        "announcements": [
            ann(1, "201", z(-6), "Week 12 notice",
                f"<p>The final will be held on {md_en(20)} at 10:30 in the regular class hour.</p>"
                "<p>Closed book. Bring a calculator.</p>"),
            ann(2, "201", z(-30), "Homework policy", "<p>Each homework is due one week after it is posted.</p>"),
            ann(3, "202", z(-4), "기말 안내", f"<p>기말은 {md_ko(22)} 오후 2시 공학관 101호에서 봅니다.</p>"),
            ann(4, "203", z(-3), "전시 관람", f"<p>{md_ko(9)} 미술관 관람 후 감상문을 씁니다.</p>"),
        ],
        "conversations": [
            {"id": 50, "subject": "중간고사 성적 안내", "context_code": "course_202", "context_name": "열역학",
             "last_message_at": z(-8), "messages": [{"id": 501, "created_at": z(-8),
                                                     "body": "중간고사 성적 안내드립니다. 본인 점수 71점 (평균 60.2, 표준편차 9.5)입니다."}]},
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


# ---------------------------------------------------------------- 검색(20-2)

def test_search_finds_missed_final_in_english_and_korean(tmp_path):
    s = make_store(tmp_path / "s.json")
    # 규칙은 'The final will be held …'·'기말은 …'(final·기말 혼자)을 시험 마감으로 읽지 못한다(그래서 묻는 것)
    assert not [v for v in s.view() if v["course_id"] in ("201", "202") and v["kind"] == "시험"]
    r = agent.search(s, "기말고사 날짜가 안 보여요")
    top = {x["course_id"] for x in r[:2]}
    assert top == {"201", "202"}
    first = r[0]
    assert first["dates"] and first["source_key"].startswith("announcement:")
    assert set(first) >= {"source_key", "type", "title", "posted_at", "course_id", "snippet", "dates"}
    # 과목 이름을 말하면 그 과목이 먼저
    r = agent.search(s, "신호와시스템 final 언제예요?")
    assert r[0]["course_id"] == "201" and "final" in r[0]["snippet"]
    assert parse_iso(r[0]["posted_at"]) == parse_iso(z(-6))
    assert r[0]["dates"][0]["date"] == (T + timedelta(days=20)).isoformat()
    # course_id를 주면 그 과목 글만
    assert {x["course_id"] for x in agent.search(s, "기말 날짜", course_id="202")} == {"202"}


def test_search_scores_and_keywords(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = agent.search(s, "성적이 안 들어오는 문제가 있습니다")
    assert r and r[0]["type"] == "inbox" and "71점" in r[0]["snippet"]
    r = agent.search(s, "감상문 언제까지?")
    assert r[0]["course_id"] == "203"


def test_search_skips_hidden_courses_and_score_files(tmp_path):
    cache = make_cache()
    cache["pdf_texts"] = {"201:9": {"text": "Quiz 1 Score\nNo Student ID Score\n1 2026000001 8\n2 2026000002 9",
                                    "course_id": "201", "filename": "quiz1_score.pdf", "posted_at": z(-2)}}
    s = make_store(tmp_path / "s.json", cache)
    assert not [x for x in agent.search(s, "quiz 점수") if x["type"] == "pdf"]   # 반 전체 점수표는 넣지 않는다
    s.data["course_prefs"] = {"201": {"mode": "hidden"}}
    assert not [x for x in agent.search(s, "final 날짜") if x["course_id"] == "201"]


def test_tokens_drop_particles_and_chatter():
    assert agent.tokens("기말고사 날짜가 안 보이네요") == ["기말고사", "날짜"]
    assert "LAB4" in agent.tokens("LAB4 마감이 없어요")


# ---------------------------------------------------------------- 검증(20-3)

POLICY = ("<p>Grading: Homework 30%, Midterm exam 30%, Final exam 40%.</p>"
          "<p>Letter grades: A+, A0: 40%, B+, B0: 40%, C+ ~ F: 20%.</p>"
          "<p>Homework is handed out every other week, five sets this term.</p>")


def rich_cache():
    c = make_cache()
    c["announcements"] += [
        ann(5, "201", z(-40), "Grading policy", POLICY),
        ann(6, "201", z(-2), "LAB2 안내", f"<p>LAB2 보고서 마감: {md_ko(5)} 23:59 (조교 메일 ta.signals@example.ac.kr)</p>"),
        ann(7, "202", z(-1), "다음 주 일정", "<p>기말은 다음 주 금요일에 봅니다.</p>"),
    ]
    c["conversations"].append(
        {"id": 60, "subject": "Quiz results", "context_code": "course_201", "context_name": "신호와시스템",
         "last_message_at": z(-3), "messages": [{"id": 601, "created_at": z(-3),
                                                 "body": "Quiz score: 8/10 (mean 6.5, sd 1.2)."}]})
    return c


FINAL = f"The final will be held on {md_en(20)} at 10:30 in the regular class hour."


def due_at(days, h=23, mi=59):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def check(s, p, **kw):
    return agent.check_proposal(agent.Ctx(s), p, **kw)


def test_validation_accepts_grounded_deadline_and_rejects_bad_ones(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    ok = {"type": "add_deadline", "course_id": "201", "source_key": "announcement:1", "evidence": FINAL,
          "title": "Final exam", "kind": "시험", "due": due_at(20, 10, 30), "reason": "'final'만 있어 규칙이 놓침"}
    q = check(s, ok)
    assert q["rejected_reason"] is None and q["source_type"] == "announcement" and q["course_short"] == "신호와시스템(영강)"
    assert parse_iso(q["due"]) == parse_iso(due_at(20, 10, 30)) and "Final exam" in q["summary"]
    # 날짜가 근거와 ±1일 넘게 다름
    assert "±1일" in check(s, {**ok, "due": due_at(23, 10, 30)})["rejected_reason"]
    assert check(s, {**ok, "due": due_at(21, 10, 30)})["rejected_reason"] is None     # ±1일은 통과
    assert "ISO" in check(s, {**ok, "due": "다음 주"})["rejected_reason"]
    # 없는 과목·다른 과목의 근거·제외한 과목
    assert "과목 목록에 없는" in check(s, {**ok, "course_id": "999"})["rejected_reason"]
    assert "다른 과목" in check(s, {**ok, "course_id": "202"})["rejected_reason"]
    # 이 PC의 글에 없는 근거
    assert "글에 없어요" in check(s, {**ok, "evidence": "The final is on Mars."})["rejected_reason"]
    # 이미 있는 마감(규칙이 찾은 LAB2) → 거절
    lab2 = next(v for v in s.view() if v["title"].replace(" ", "") == "LAB2")
    dup = check(s, {**ok, "evidence": f"LAB2 보고서 마감: {md_ko(5)} 23:59", "title": "LAB2 보고서", "kind": "과제",
                    "due": lab2["due"], "source_key": "announcement:6"})
    assert dup["rejected_reason"].startswith("이미 있어요: LAB 2") and "확인함" in dup["rejected_reason"]
    # 상대 날짜인데 '명시'라고 주장 → 거절, 계산했다고 하면 통과(이유에 계산이 남는다)
    fri = T + timedelta(days=-1)
    fri = fri - timedelta(days=fri.weekday()) + timedelta(days=11)
    rel = {"type": "add_deadline", "course_id": "202", "evidence": "기말은 다음 주 금요일에 봅니다.",
           "title": "기말고사", "kind": "시험", "due": iso(datetime.combine(fri, time(23, 59), tzinfo=TZ)),
           "reason": "날짜가 명시돼 있어요"}
    assert "상대 날짜" in check(s, rel)["rejected_reason"]
    good = check(s, {**rel, "reason": "다음 주 금요일을 게시일 기준으로 계산했어요"})
    assert good["rejected_reason"] is None and good["date_basis"]["relative"]
    assert "‘다음 주 금요일’ → 공지일" in agent.reason_text(good)


def test_validation_other_types(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    w = {"type": "set_weights", "course_id": "201", "source_key": "announcement:5", "reason": "평가 비중 글",
         "evidence": "Grading: Homework 30%, Midterm exam 30%, Final exam 40%.",
         "components": [{"name": "Homework", "weight": 30}, {"name": "Midterm exam", "weight": 30},
                        {"name": "Final exam", "weight": 40}]}
    assert check(s, w)["rejected_reason"] is None
    bad = check(s, {**w, "components": w["components"][:2]})
    assert "합이 60%" in bad["rejected_reason"]
    d = {"type": "set_dist", "course_id": "201", "reason": "학점 비율", "evidence": "Letter grades: A+, A0: 40%, B+, B0: 40%, C+ ~ F: 20%.",
         "groups": [{"grades": ["A+", "A0"], "pct": 40}, {"grades": ["B+", "B0"], "pct": 40},
                    {"grades": ["C+", "C0", "D+", "D0", "F"], "pct": 20}]}
    q = check(s, d)
    assert q["rejected_reason"] is None and q["dist"]["dist"]["A+"] == 20
    overlap = check(s, {**d, "groups": [{"grades": ["A+", "A0"], "pct": 60}, {"grades": ["A0", "B+"], "pct": 40}]})
    assert "겹칩니다" in overlap["rejected_reason"]
    assert "학점" in check(s, {**d, "evidence": "Grading: Homework 30%, Midterm exam 30%, Final exam 40%."})["rejected_reason"]
    # 평가 항목이 없는 과목: 점수 연결·개수는 거절, 모르는 key·item_id도
    link = {"type": "link_score", "course_id": "201", "key": "txt:inbox:nope", "component": "Homework",
            "evidence": "Quiz score: 8/10 (mean 6.5, sd 1.2).", "reason": "퀴즈"}
    r = check(s, link)["rejected_reason"]
    assert "없는 key" in r and "없는 평가 항목" in r
    fix = {"type": "fix_due", "course_id": "201", "item_id": "f-nope", "due": due_at(20, 10, 30), "evidence": FINAL,
           "reason": "x"}
    assert "없는 item_id" in check(s, fix)["rejected_reason"]
    assert "모르는 제안" in check(s, {"type": "delete_all", "reason": "x"})["rejected_reason"]
    assert check(s, {"type": "note", "reason": "설명"})["rejected_reason"] is None


def test_assist_harness_redacts_retries_and_keeps_rejections(tmp_path):
    from lms_agent import agent_cli
    s = make_store(tmp_path / "s.json", rich_cache())
    seen = []

    def fake(args, input, timeout, cwd):
        import json
        import subprocess
        seen.append(input)
        lab2 = f"LAB2 보고서 마감: {md_ko(5)} 23:59 (조교 메일 [이메일])"
        bad = {"type": "add_deadline", "course_id": "201", "evidence": "The final is on Mars.", "title": "x",
               "due": due_at(20), "reason": "지어냄"}
        good = {"type": "add_deadline", "course_id": "201", "source_key": "announcement:1", "evidence": FINAL,
                "title": "Final exam", "kind": "시험", "due": due_at(20, 10, 30), "reason": "'final'만 있어 규칙이 놓침"}
        quote = {"type": "note", "evidence": lab2, "reason": "LAB2는 이미 있어요"}
        body = {"answer": "공지에서 기말 날짜를 찾았어요.", "proposals": [bad, good, quote] if len(seen) == 1 else [good, quote]}
        out = json.dumps({"type": "result", "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)
        return subprocess.CompletedProcess(args, 0, out, "")

    r = agent.answer(s, "Signals 기말 날짜가 안 보여요 (제 메일 me@example.ac.kr)", ai_enabled=True, runner=fake,
                     command="claude")
    assert r["by"] == "ai" and r["attempts"] == 2 and len(seen) == 2
    assert "me@example.ac.kr" not in seen[0] and "ta.signals@example.ac.kr" not in seen[0]   # 가린 뒤 보냄
    assert "이전 답이 검사를 통과하지 못했습니다" in seen[1] and "Mars" not in seen[1].split("INPUT START")[1]
    assert len(seen[0]) < agent_cli.MAX_TEXT + 6000   # 지시·스키마 + 12,000자 예산
    good, note = r["proposals"]
    assert good["rejected_reason"] is None and note["type"] == "note"
    assert "ta.signals@example.ac.kr" in note["evidence"]   # 인용은 이 PC의 원문으로(자리 표시 없음)
    assert s.data["ai_runs"][-1]["task"] == "assist" and "Final" not in str(s.data["ai_runs"])   # 기록에 내용 없음


def test_assist_final_attempt_shows_rejected_without_button(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())

    def fake(args, input, timeout, cwd):
        import json
        import subprocess
        body = {"answer": "찾았어요.", "proposals": [{"type": "add_deadline", "course_id": "201", "evidence": FINAL,
                                                     "title": "Final", "kind": "시험", "due": due_at(25), "reason": "x"}]}
        out = json.dumps({"type": "result", "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)
        return subprocess.CompletedProcess(args, 0, out, "")

    r = agent.answer(s, "final 날짜", ai_enabled=True, runner=fake, command="claude")
    assert r["by"] == "ai" and r["attempts"] == 2
    assert "±1일" in r["proposals"][0]["rejected_reason"]


# ---------------------------------------------------------------- 반영·되돌리기(20-4), agent_items(20-4), 놓친 사례(20-5)

def thread_with(s, *raw, by="ai"):
    ctx = agent.Ctx(s)
    th = agent.new_thread(s, "질문", None)
    th.update(status="done", by=by, proposals=[agent.check_proposal(ctx, p, by=by) for p in raw])
    assert all(p["rejected_reason"] is None for p in th["proposals"]), [p["rejected_reason"] for p in th["proposals"]]
    return th


ADD = {"type": "add_deadline", "course_id": "201", "source_key": "announcement:1", "evidence": FINAL,
       "title": "Final exam", "kind": "시험", "due": None, "reason": "'final'만 있어 규칙이 놓친 시험 일정"}


def test_apply_add_deadline_survives_rebuild_and_undo(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    th = thread_with(s, {**ADD, "due": due_at(20, 10, 30)})
    p = agent.apply(s, th["id"], 0)
    assert p["applied"] and p["result"]["item_id"].startswith("a-")
    it = next(v for v in s.view() if v["id"] == p["result"]["item_id"])
    assert (it["origin"], it["found_by"], it["decision"], it["decided_by"], it["status"]) == \
        ("found", "ai", "approved", "ai", "scheduled")
    assert it["kind"] == "시험" and parse_iso(it["due"]) == parse_iso(due_at(20, 10, 30))
    assert "근거:" in it["ai_reason"] and it["evidence"][0]["text"] == FINAL
    assert it["id"] in {v["id"] for v in s.calendar_items()}
    assert s.data["missed_cases"][0]["type"] == "add_deadline" and s.data["missed_cases"][0]["rule_version"]
    with pytest.raises(agent.AgentConflict):
        agent.apply(s, th["id"], 0)                       # 두 번 반영하지 않는다
    s.save()
    again = Store(s.path)
    core.rebuild(again)                                    # 다음 동기화가 항목을 다시 만들어도 남는다
    assert next(v for v in again.view() if v["id"] == it["id"])["status"] == "scheduled"
    agent.undo(again, th["id"], 0)
    assert not [v for v in again.view() if v["id"] == it["id"]]
    assert again.data["agent_items"] == [] and it["id"] not in again.data["user"] and again.data["missed_cases"] == []
    with pytest.raises(agent.AgentConflict):
        agent.undo(again, th["id"], 0)


def test_rule_found_same_deadline_merges_into_agent_item(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    th = thread_with(s, {**ADD, "due": due_at(20, 10, 30)})
    rid = agent.apply(s, th["id"], 0)["result"]["item_id"]
    # 나중에 교수가 같은 시험을 규칙이 읽는 모양으로 다시 공지 → 규칙 항목과 하나로(에이전트 id·결정 유지, 근거는 둘)
    s.data["cache"]["announcements"].append(ann(9, "201", z(-1), "Final exam", f"<p>Final exam: {md_en(20)} 10:30, Room 301</p>"))
    core.rebuild(s)
    finals = [v for v in s.view() if v["course_id"] == "201" and v["kind"] == "시험"]
    assert [v["id"] for v in finals] == [rid]
    assert len(finals[0]["evidence"]) == 2 and "규칙도 같은 마감을 찾음" in finals[0]["notes"]
    assert finals[0]["status"] == "scheduled" and finals[0]["decided_by"] == "ai"


def test_apply_fix_due_and_undo(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    s.data["cache"]["announcements"].append(ann(8, "201", z(-1), "공지", f"<p>LAB2는 하루 늦춰 {md_ko(6)} 23:59까지 받습니다.</p>"))
    core.rebuild(s)
    lab2 = next(v for v in s.view() if v["title"] == "LAB 2" and parse_iso(v["due"]) == parse_iso(due_at(5)))
    th = thread_with(s, {"type": "fix_due", "course_id": "201", "item_id": lab2["id"], "due": due_at(6),
                         "evidence": f"LAB2는 하루 늦춰 {md_ko(6)} 23:59까지 받습니다.", "reason": "연장 공지"})
    agent.apply(s, th["id"], 0)
    v = next(x for x in s.view() if x["id"] == lab2["id"])
    assert parse_iso(v["due"]) == parse_iso(due_at(6)) and v["decided_by"] == "ai" and v["orig_due"] == lab2["due"]
    agent.undo(s, th["id"], 0)
    v = next(x for x in s.view() if x["id"] == lab2["id"])
    assert parse_iso(v["due"]) == parse_iso(due_at(5)) and v["decided_by"] is None
    # 반영 뒤 사용자가 직접 바꿨으면 되돌리지 않는다(사용자 쪽이 이김)
    agent.apply(s, th["id"], 0)
    s.update_user(lab2["id"], {"due": "2030-01-01T09:00"})
    with pytest.raises(agent.AgentConflict):
        agent.undo(s, th["id"], 0)


def test_apply_weights_dist_link_count_and_undo(tmp_path):
    from lms_agent import distribution as dm
    from lms_agent import gradebook
    s = make_store(tmp_path / "s.json", rich_cache())
    w = {"type": "set_weights", "course_id": "201", "source_key": "announcement:5", "reason": "평가 비중",
         "evidence": "Grading: Homework 30%, Midterm exam 30%, Final exam 40%.",
         "components": [{"name": "Homework", "weight": 30}, {"name": "Midterm exam", "weight": 30},
                        {"name": "Final exam", "weight": 40}]}
    d = {"type": "set_dist", "course_id": "201", "reason": "학점 비율",
         "evidence": "Letter grades: A+, A0: 40%, B+, B0: 40%, C+ ~ F: 20%.",
         "groups": [{"grades": ["A+", "A0"], "pct": 40}, {"grades": ["B+", "B0"], "pct": 40},
                    {"grades": ["C+", "C0", "D+", "D0", "F"], "pct": 20}]}
    th = thread_with(s, w, d)
    agent.apply(s, th["id"], 0)
    cfg = s.data["grades"]["201"]
    assert [c["name"] for c in cfg["components"]] == ["Homework", "Midterm exam", "Final exam"]
    assert all(c["auto"] for c in cfg["components"]) and cfg["auto_by"] == "ai" and cfg["auto_chat"]
    agent.apply(s, th["id"], 1)
    eff = dm.effective(s.data["grades"]["201"])
    assert eff["by"] == "syllabus" and eff["auto"]["by"] == "ai" and eff["auto"]["chat"]
    # 이제 평가 항목이 있다 → 점수 연결·개수 제안
    cand = next(c for c in gradebook.candidates(s) if c["course_id"] == "201")
    before = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], [cand])[cand["key"]]
    assert before["by"] != "ai"
    th2 = thread_with(s, {"type": "link_score", "course_id": "201", "key": cand["key"], "component": "Midterm exam",
                          "evidence": "Quiz score: 8/10 (mean 6.5, sd 1.2).", "reason": "학기 앞쪽 퀴즈"},
                      {"type": "set_count", "course_id": "201", "component": "homework", "n": 5,
                       "evidence": "Homework is handed out every other week, five sets this term.",
                       "reason": "다섯 번이라고 적혀 있어요"})
    agent.apply(s, th2["id"], 0)
    ln = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], [cand])[cand["key"]]
    assert (ln["component"], ln["by"]) == ("Midterm exam", "ai")
    agent.apply(s, th2["id"], 1)
    sec = gradebook.section(s, core.course_summaries(s.data["cache"]))
    hw = next(c for c in sec["courses"] if c["course_id"] == "201")["config"]["components"][0]
    assert hw["count"] is None or hw["count"]["by"] in ("ai", "rules")
    assert s.data["grade_counts_ai"]["201"]["homework"]["n"] == 5 and s.data["grade_counts_ai"]["201"]["homework"]["chat"]
    # 되돌리기: 반영 전 값으로(연결·개수는 지우고, 학점 비율·비중은 없던 상태로)
    for tid, i in ((th2["id"], 1), (th2["id"], 0), (th["id"], 1), (th["id"], 0)):
        agent.undo(s, tid, i)
    assert "homework" not in s.data["grade_counts_ai"]["201"] and cand["key"] not in s.data["grade_links_ai"]["201"]
    assert "201" not in s.data["grades"]


def test_user_set_values_are_not_overwritten(tmp_path):
    from lms_agent import gradebook
    s = make_store(tmp_path / "s.json", rich_cache())
    gradebook.save_course(s, "201", {"components": [{"name": "All", "weight": 100}]})   # 사용자가 직접 넣음
    r = check(s, {"type": "set_weights", "course_id": "201", "reason": "x",
                  "evidence": "Grading: Homework 30%, Midterm exam 30%, Final exam 40%.",
                  "components": [{"name": "Homework", "weight": 30}, {"name": "Midterm exam", "weight": 30},
                                 {"name": "Final exam", "weight": 40}]})
    assert "직접 넣은 평가 비중" in r["rejected_reason"]


def test_rules_mode_when_ai_off_and_apply_adds_review_candidate(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    r = agent.answer(s, "신호와시스템 기말고사 날짜가 안 보여요", ai_enabled=False)
    assert r["by"] == "rules" and r["error"] is None and r["searched"] > 0 and r["results"]
    assert "규칙 검색" in r["answer"] and "AI CLI를 켜면" in r["answer"]
    p = next(x for x in r["proposals"] if x["course_id"] == "201")
    assert p["by"] == "rules" and p["rejected_reason"] is None and p["kind"] == "시험"
    assert parse_iso(p["due"]) == parse_iso(due_at(20, 10, 30)) and p["evidence"] == FINAL
    th = agent.new_thread(s, "q", None)
    th.update(r)
    agent.apply(s, th["id"], r["proposals"].index(p))
    it = next(v for v in s.view() if v.get("found_by") == "rules")
    assert (it["status"], it["decision"], it["decided_by"]) == ("review", "pending", None)   # 사용자가 확인함에서 승인
    # 점수 질문: 평가 항목이 없는 과목이라 넣을 곳이 없다는 진단
    r = agent.answer(s, "열역학 성적이 안 들어오는 문제가 있습니다")
    assert "평가 항목(비중)이 없어서" in r["answer"]


# ---------------------------------------------------------------- 작업(백그라운드)·화면 API·CLI·하루 한도

SECRET = "canvas-test-token-abcdefghijklmnopqrstuvwxyz0123"


def make_app(tmp_path, ai=False, runner=None):
    from lms_agent import mock_ai
    from lms_agent.server import App
    s = make_store(tmp_path / "state.json", rich_cache())
    s.data["settings"]["ai_cli"] = ai
    s.save()
    return App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": SECRET, "user_name": "김가짜"},
               demo=True, ai_runner=runner or mock_ai.runner, ai_command="claude")


def test_ask_runs_in_background_and_merges_under_lock(tmp_path):
    import json
    import threading

    from lms_agent import mock_ai
    from lms_agent.api import ApiError
    from lms_agent.features import agent as fa
    gate, started = threading.Event(), threading.Event()

    def slow(args, input, timeout, cwd):
        started.set()
        gate.wait(10)
        return mock_ai.runner(args, input, timeout, cwd)

    app = make_app(tmp_path, ai=True, runner=slow)
    lab2 = next(v for v in app.store.view() if v["title"] == "LAB 2")
    out = fa.ask(app, {"message": "신호와시스템 기말고사 날짜가 안 보여요"})
    assert started.wait(10)                                  # 바로 돌아왔고, AI는 백그라운드에서 도는 중
    st = app.state()["agent"]
    assert st["busy"] and st["threads"][-1]["id"] == out["job"] and st["threads"][-1]["status"] == "running"
    with pytest.raises(ApiError) as e:
        fa.ask(app, {"message": "하나 더"})
    assert e.value.status == 409
    other = Store(app.store.path)                            # 그 사이 다른 프로세스(백그라운드 동기화 등)가 저장
    other.update_user(lab2["id"], {"decision": "rejected"})
    other.save()
    gate.set()
    assert fa.wait(app, out["job"])
    st = app.state()["agent"]
    th = st["threads"][-1]
    assert (th["status"], th["by"], th["error"]) == ("done", "ai", None) and not st["busy"]
    assert th["proposals"][0]["type"] == "add_deadline" and th["proposals"][0]["can_apply"]
    assert app.store.data["user"][lab2["id"]]["decision"] == "rejected"   # 다른 프로세스의 저장이 지워지지 않았다
    assert Store(app.store.path).data["agent"]["threads"][-1]["status"] == "done"
    assert st["calls_today"] == 1 and st["enabled"] and st["limits"]["per_day"] == 30
    dump = json.dumps(app.state(), ensure_ascii=False)
    assert SECRET not in dump and "_undo" not in dump


def _http(url, path, token=None, body=None, method="POST"):
    import json
    import urllib.error
    import urllib.request
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **({"X-LMS-Agent": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), dict(e.headers)


def test_http_routes_ask_apply_undo_clear(tmp_path):
    import json

    from lms_agent.features import agent as fa
    from lms_agent.server import serve_in_thread, start_server
    app = make_app(tmp_path, ai=False)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    url, tok = f"http://127.0.0.1:{httpd.server_address[1]}", app.token
    try:
        assert _http(url, "/api/agent/ask", None, {"message": "x"})[0] == 403          # 세션 값 없이 안 됨
        assert _http(url, "/api/agent/ask", tok, {"message": "  "})[0] == 400
        assert _http(url, "/api/agent/ask", tok, {"message": "가" * 501})[0] == 400
        assert _http(url, "/api/agent/ask", tok, {"message": "x", "course_id": "999"})[0] == 400
        app.store.data["course_prefs"] = {"203": {"mode": "hidden"}}
        app.store.save()
        assert _http(url, "/api/agent/ask", tok, {"message": "x", "course_id": "203"})[0] == 400   # 제외한 과목
        code, body, _ = _http(url, "/api/agent/ask", tok, {"message": "기말고사 날짜가 안 보여요", "course_id": "201"})
        assert code == 200
        job = json.loads(body)["job"]
        assert fa.wait(app, job)
        st = json.loads(_http(url, "/api/state", tok, method="GET")[1])["agent"]
        th = st["threads"][-1]
        assert th["by"] == "rules" and th["course_id"] == "201" and not st["enabled"]
        i = next(k for k, p in enumerate(th["proposals"]) if p["can_apply"])
        assert _http(url, "/api/agent/apply", tok, {"thread": "t-nope", "proposal": 0})[0] == 404
        assert _http(url, "/api/agent/apply", tok, {"thread": job, "proposal": 99})[0] == 400
        assert _http(url, "/api/agent/apply", tok, {"thread": job})[0] == 400
        code, body, _ = _http(url, "/api/agent/apply", tok, {"thread": job, "proposal": i})
        state = json.loads(body)
        assert code == 200 and any(v.get("found_by") == "rules" for v in state["items"])
        p = state["agent"]["threads"][-1]["proposals"][i]
        assert p["applied"] and not p["can_apply"] and state["agent"]["missed_cases"] == 1
        assert _http(url, "/api/agent/apply", tok, {"thread": job, "proposal": i})[0] == 409
        code, body, headers = _http(url, "/api/agent/missed-cases", tok, method="GET")
        assert code == 200 and "attachment" in headers.get("Content-Disposition", "")
        cases = json.loads(body)["missed_cases"]
        assert cases[0]["type"] == "add_deadline" and "_ref" not in cases[0]
        assert _http(url, "/api/agent/missed-cases", None, method="GET")[0] == 403
        code, body, _ = _http(url, "/api/agent/undo", tok, {"thread": job, "proposal": i})
        assert code == 200 and not any(v.get("found_by") for v in json.loads(body)["items"])
        assert _http(url, "/api/agent/undo", tok, {"thread": job, "proposal": i})[0] == 409
        code, body, _ = _http(url, "/api/agent/clear", tok, {})
        assert code == 200 and json.loads(body)["agent"]["threads"] == []
    finally:
        httpd.shutdown()


def test_threads_keep_last_80(tmp_path):
    """계약 24-1: 이어지는 대화를 위해 남기는 차례 수 20 → 80(오래된 것부터 지움)."""
    s = make_store(tmp_path / "s.json")
    for i in range(85):
        agent.new_thread(s, f"q{i}", None)
    ts = agent.threads(s)
    assert agent.MAX_THREADS == 80
    assert len(ts) == 80 and ts[0]["message"] == "q5" and ts[-1]["message"] == "q84"


def test_daily_cap_is_shared_with_other_ai_tasks(tmp_path):
    from lms_agent import agent_cli, mock_ai
    s = make_store(tmp_path / "s.json", rich_cache())
    s.data["ai_runs"] = [{"at": iso(now_local()), "task": "triage_review", "ok": True, "attempts": 29}]
    r = agent.answer(s, "신호와시스템 기말 날짜", ai_enabled=True, runner=mock_ai.runner, command="claude")
    assert r["by"] == "ai" and r["attempts"] == 1                 # 남은 한 번만 썼다
    assert agent_cli.calls_today(s) == 30
    assert "오늘 AI 호출 한도" in agent_cli.AiSession(s, enabled=True).blocked()   # 확인함 정리 등도 막힌다
    r = agent.answer(s, "신호와시스템 기말 날짜", ai_enabled=True, runner=mock_ai.runner, command="claude")
    assert r["by"] == "rules" and "한도" in r["error"] and "규칙 검색으로 답했어요" in r["answer"]
    assert r["proposals"]                                       # AI 없이도 [마감 후보로 넣기]


def test_cli_ask_prints_answer_and_applies(tmp_path, monkeypatch, capsys):
    from lms_agent import cli
    from lms_agent.config import STATE_FILE, data_dir, save_config
    home = data_dir()
    save_config({"canvas_url": "https://lms.example.test", "token": SECRET}, home)
    make_store(home / STATE_FILE, rich_cache())
    cli.main(["ask", "신호와시스템 기말고사 날짜가 안 보여요"])
    out = capsys.readouterr().out
    assert "답(규칙 검색)" in out and "제안:" in out and "1. [반영 가능]" in out and "--apply 번호" in out
    assert SECRET not in out
    cli.main(["ask", "신호와시스템 기말고사 날짜가 안 보여요", "--apply", "1"])
    out = capsys.readouterr().out
    assert "반영했어요" in out and SECRET not in out
    s = Store(home / STATE_FILE)
    assert len(agent.threads(s)) == 1                            # 같은 질문은 다시 묻지 않고 그 제안을 반영
    assert any(v.get("found_by") == "rules" for v in s.view())
    with pytest.raises(SystemExit):
        cli.main(["ask", "   "])


def test_demo_conversation_final_quiz(tmp_path):
    """데모(가짜 Canvas + 가짜 AI): 규칙이 놓친 ASIC 'Final quiz' 날짜를 묻고 반영하면 '날짜 미공지'에서 빠진다."""
    import json

    from lms_agent import mock_ai
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
        before = app.state()
        assert {"course_id": "101", "course_short": "ASIC설계(영강)", "component": "Final quiz", "weight": 5.0} \
            in before["exams"]["not_announced"]
        for ai in (False, True):
            s.data["settings"]["ai_cli"] = ai
            job = fa.ask(app, {"message": "기말고사 날짜가 안 보여요"})["job"]
            assert fa.wait(app, job)
            th = app.state()["agent"]["threads"][-1]
            assert th["by"] == ("ai" if ai else "rules")
            asic = [p for p in th["proposals"] if p["course_id"] == "101" and p["can_apply"]]
            assert len(asic) == 1 and asic[0]["title"] == "Final quiz" and asic[0]["kind"] == "시험"
            assert "The final will be held on" in asic[0]["evidence"] and asic[0]["source_type"] == "announcement"
            assert any(str(p["rejected_reason"]).startswith("이미 있어요: 기말고사") for p in th["proposals"]) or ai
        idx = th["proposals"].index(asic[0])
        state = fa.apply_proposal(app, {"thread": th["id"], "proposal": idx})
        assert not [r for r in state["exams"]["not_announced"] if r["component"] == "Final quiz"]
        up = next(r for r in state["exams"]["upcoming"] if r["component"] == "Final quiz")
        it = next(v for v in state["items"] if v["id"] == up["item_id"])
        assert (it["found_by"], it["decided_by"], it["status"], it["component"]) == ("ai", "ai", "scheduled", "Final quiz")
        assert "a-" in it["id"] and json.dumps(state, ensure_ascii=False).count(DEMO_TOKEN) == 0
        core.sync(Canvas(base, DEMO_TOKEN), s)                   # 다음 동기화에도 남는다
        assert any(v["id"] == it["id"] and v["status"] == "scheduled" for v in s.view())
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 제목은 근거 문장에서, 항목의 되돌리기 연결

LAB4_EXT = f"LAB4 보고서 마감이 {md_ko(8)} 자정으로 연장되었습니다."


def _with_final_quiz_component(s):
    from lms_agent import gradebook
    gradebook.save_course(s, "201", {"components": [{"name": "Homework", "weight": 60}, {"name": "Final quiz", "weight": 40}]})


def test_rules_title_comes_from_the_evidence_not_the_question(tmp_path):
    """UI 리뷰 버그: '기말고사 날짜가 안 보여요'에 'LAB4 보고서 마감 연장' 문장이 'Final quiz'(시험)로 제안됐다."""
    c = rich_cache()
    c["conversations"].append({"id": 70, "subject": "LAB4 보고서 마감 연장", "context_code": "course_201",
                               "context_name": "신호와시스템", "last_message_at": z(-1),
                               "messages": [{"id": 701, "created_at": z(-1), "body": LAB4_EXT + " 메일로 제출해 주세요."}]})
    s = make_store(tmp_path / "s.json", c)
    _with_final_quiz_component(s)
    ctx = agent.Ctx(s)
    assert agent.guess_title(ctx, "201", LAB4_EXT, "LAB4 보고서 마감 연장") == ("LAB 4", "과제", False)
    assert agent.guess_title(ctx, "201", FINAL, "Week 12 notice") == ("Final quiz", "시험", False)
    assert agent.guess_title(ctx, "201", "Room change: we meet on Dec. 3 in Room 2.", "Notice") == ("마감 후보", "과제", True)
    for msg in ("기말고사 날짜가 안 보여요", "마감 날짜가 안 보여요"):
        r = agent.answer(s, msg, course_id="201")
        for p in r["proposals"]:
            if "LAB4" in (p["evidence"] or ""):
                assert (p["title"], p["kind"]) == ("LAB 4", "과제")
            if p["title"] == "Final quiz":
                assert "final" in p["evidence"].lower()
    r = agent.answer(s, "Room 날짜", course_id="201")
    assert all(p["title"] != "Final quiz" for p in r["proposals"] if "final" not in (p["evidence"] or "").lower())


def test_items_carry_agent_ref_for_undo_from_the_item(tmp_path):
    s = make_store(tmp_path / "s.json", rich_cache())
    s.data["cache"]["announcements"].append(ann(8, "201", z(-1), "공지", f"<p>LAB2는 하루 늦춰 {md_ko(6)} 23:59까지 받습니다.</p>"))
    core.rebuild(s)
    lab2 = next(v for v in s.view() if v["title"] == "LAB 2" and parse_iso(v["due"]) == parse_iso(due_at(5)))
    th = thread_with(s, {**ADD, "due": due_at(20, 10, 30)},
                     {"type": "fix_due", "course_id": "201", "item_id": lab2["id"], "due": due_at(6),
                      "evidence": f"LAB2는 하루 늦춰 {md_ko(6)} 23:59까지 받습니다.", "reason": "연장 공지"})
    rid = agent.apply(s, th["id"], 0)["result"]["item_id"]
    agent.apply(s, th["id"], 1)
    view = {v["id"]: v for v in s.view()}
    assert view[rid]["agent_ref"] == {"thread": th["id"], "proposal": 0}
    assert view[lab2["id"]]["agent_ref"] == {"thread": th["id"], "proposal": 1}
    assert all(v["agent_ref"] is None for k, v in view.items() if k not in (rid, lab2["id"]))
    agent.undo(s, th["id"], 1)                                     # 항목 카드의 [되돌리기] → 마감까지 원래대로
    v = next(x for x in s.view() if x["id"] == lab2["id"])
    assert parse_iso(v["due"]) == parse_iso(due_at(5)) and v["agent_ref"] is None
    s.update_user(rid, {"decision": "rejected"})                   # 사용자가 손대도 추가한 항목의 되돌리기는 남는다
    assert next(x for x in s.view() if x["id"] == rid)["agent_ref"] == {"thread": th["id"], "proposal": 0}
    agent._box(s)["threads"] = []                                  # 대화 기록을 지우면 되돌릴 곳이 없다 → null
    assert next(x for x in s.view() if x["id"] == rid)["agent_ref"] is None


def test_mock_ai_proposes_link_score_for_unlinked_quiz(tmp_path):
    """가짜 AI(데모): 규칙이 'Quiz'를 Midterm quiz / Final quiz 중 정하지 못한 점수를, 점수 글의 'midterm' 낱말로 제안한다
    — 하네스 검증·반영·되돌리기를 그대로 지난다."""
    from lms_agent import gradebook, mock_ai
    c = rich_cache()
    c["conversations"].append({"id": 80, "subject": "Quiz results", "context_code": "course_201", "context_name": "x",
                               "last_message_at": z(-1), "messages": [{"id": 801, "created_at": z(-1), "body":
                               "Your quiz score: 7/10 (mean 6.4, std 1.3). This was the short quiz held in the midterm week."}]})
    s = make_store(tmp_path / "s.json", c)
    gradebook.save_course(s, "201", {"components": [{"name": "Homework", "weight": 80}, {"name": "Midterm quiz", "weight": 10},
                                                    {"name": "Final quiz", "weight": 10}]})
    r = agent.answer(s, "신호와시스템 성적이 안 들어와요", ai_enabled=True, runner=mock_ai.runner, command="claude")
    link = next(p for p in r["proposals"] if p["type"] == "link_score" and p["score_label"] == "Quiz"
                and "midterm" in p["evidence"])
    assert link["rejected_reason"] is None and link["component"] == "Midterm quiz"
    th = agent.new_thread(s, "q", None)
    th.update(r)
    agent.apply(s, th["id"], r["proposals"].index(link))
    cand = next(x for x in gradebook.candidates(s) if x["key"] == link["key"])
    ln = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], [cand])[cand["key"]]
    assert (ln["component"], ln["by"]) == ("Midterm quiz", "ai")
    agent.undo(s, th["id"], r["proposals"].index(link))
    ln = gradebook.link_candidates(s, "201", s.data["grades"]["201"]["components"], [cand])[cand["key"]]
    assert ln["by"] == "rules" and ln["component"] is None


def test_undo_add_refuses_after_user_edit(tmp_path):
    """에이전트가 넣은 마감을 사용자가 그 뒤 고쳤으면, 오래된 화면에서 [되돌리기]를 눌러도 지우지 않는다(사용자 쪽이 이김)."""
    s = make_store(tmp_path / "s.json", rich_cache())
    th = thread_with(s, {**ADD, "due": due_at(20, 10, 30)})
    rid = agent.apply(s, th["id"], 0)["result"]["item_id"]
    s.update_user(rid, {"due": due_at(21, 9, 0)[:16]})        # 사용자가 시각을 직접 고침
    with pytest.raises(agent.AgentConflict):
        agent.undo(s, th["id"], 0)
    assert any(v["id"] == rid for v in s.view())               # 그대로 남는다


def test_search_ranks_titled_exam_notice_above_lecture_title_dates(tmp_path):
    """실제 사례(로봇공학개론): '기말고사'는 공지 제목에만 있고 본문은 '날짜: …' 칸. 강의 슬라이드 첫 장의 날짜
    ('강사 이름 2026.11.26')가 더 위에 오면 안 된다 — 질문의 주제가 제목·본문에 있는 글이 먼저."""
    c = make_cache()
    cid = c["courses"][0]["id"]
    c["announcements"].append(ann(31, str(cid), z(-9), "기말고사 공지",
                                  f"<p>날짜: {md_ko(30)}</p><p>시간: 10:30am - 11:30am</p><p>장소: 강의실</p>"))
    s = make_store(tmp_path / "s.json", c)
    lecture_day = (T + timedelta(days=12)).strftime("%Y.%m.%d")
    s.data["pdf_texts"] = {f"{cid}:7001": {"text": f"Lecture 16\nProf. Kim {lecture_day}\nSchool of Engineering",
                                           "filename": "lecture16.pdf", "course_id": str(cid),
                                           "posted_at": z(-1), "ok": True}}
    hits = agent.search(s, "기말고사 날짜가 안 보여요", course_id=str(cid), limit=12)
    titles = [h["title"] for h in hits]
    lecture = next((i for i, h in enumerate(hits) if "lecture16" in str(h["title"])), len(hits))
    assert "기말고사 공지" in titles and titles.index("기말고사 공지") < lecture
    assert titles.index("Week 12 notice") < lecture        # 본문에 final이 있는 공지도 슬라이드 날짜보다 위
