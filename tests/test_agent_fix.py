"""지우기·고치기 제안(계약 26-3, agent_fix): 검사·반영·되돌리기·지운 평가 항목을 자동 채우기가 다시 만들지 않음·규칙 갈래.
가짜 데이터만 쓴다(과목·제목·점수 모두 지어낸 것). 날짜는 오늘 기준, id는 데이터에서 찾는다."""
import copy
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from lms_agent import agent, agent_fix, core, gradebook, gsync
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso
from lms_agent.weights import WeightsFound

TZ = local_tz()
T = now_local().date()


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def md_ko(days):
    d = T + timedelta(days=days)
    return f"{d.month}월 {d.day}일"


def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


def asg(aid, cid, name, due):
    return {"id": aid, "course_id": cid, "name": name, "due_at": due, "submission_types": ["online_upload"],
            "html_url": f"https://lms.example.test/courses/{cid}/assignments/{aid}"}


def make_cache():
    return {
        "fetched_at": z(0),
        "courses": [
            {"id": "301", "name": "262R (서울-학부)로봇학입문(가짜)-00분반", "course_code": "RB301",
             "syllabus_body": "<p>평가: Report 40%, Project 2 40%, Assignment 20%</p>"},
            {"id": "302", "name": "회로실험", "course_code": "EE302",
             "syllabus_body": "<p>평가 방법은 첫 시간에 안내합니다. 실습과 과제로 성적을 매깁니다.</p>"},
            {"id": "303", "name": "교양세미나", "course_code": "GE303", "syllabus_body": None},
        ],
        "assignments": [asg(11, "301", "Report", z(5, 23, 59)), asg(12, "301", "Project 2", z(12, 23, 59)),
                        asg(21, "302", "LAB 3", z(4, 23, 59)), asg(31, "303", "감상문", z(6, 23, 59))],
        "announcements": [
            ann(1, "301", z(-2), "첨부 안내", f"<p>Assignment due {md_ko(5)} 23:59.</p>"),
            ann(2, "302", z(-1), "LAB 4 안내", f"<p>LAB 4 보고서는 {md_ko(9)} 18:00까지 조교 메일로 제출하세요.</p>"),
            ann(3, "301", z(-3), "중간 점수", "<p>Report 점수: 35/40 (평균 30.5, 표준편차 4.0)</p>"),
        ],
        "conversations": [], "boards": [], "pdf_texts": {}, "errors": [],
    }


def make_store(path, hidden=True):
    s = Store(path)
    s.data["cache"] = make_cache()
    if hidden:
        s.data["course_prefs"] = {"303": {"mode": "hidden"}}
    core.rebuild(s)
    core._auto_weights(s, False)      # 강의계획서의 평가 비중을 자동으로(로봇학입문: Report·Project 2·과제)
    s.save()
    return s


def item(s, title, origin=None, include_hidden=False):
    return next(v for v in s.view(include_hidden=include_hidden)
                if v["title"] == title and (origin is None or v["origin"] == origin))


def names(s, cid):
    return [(c["name"], c["weight"]) for c in s.data["grades"][cid]["components"]]


def chk(s, p, by="ai"):
    return agent_fix.check(agent.Ctx(s), p, by=by)


def thread_with(s, *raw, by="ai"):
    ctx = agent.Ctx(s)
    th = agent.new_thread(s, "지워줘", None)
    th.update(status="done", by=by, proposals=[agent.check_proposal(ctx, p, by=by) for p in raw])
    assert all(p["rejected_reason"] is None for p in th["proposals"]), [p["rejected_reason"] for p in th["proposals"]]
    return th


def md(due):
    return agent._md(parse_iso(due))


SHORT = "로봇학입문(가짜)"


# ---------------------------------------------------------------- 검사

def test_fixture_has_the_stray_assignment_component(tmp_path):
    s = make_store(tmp_path / "s.json")
    assert names(s, "301") == [("Report", 40.0), ("Project 2", 40.0), ("과제", 20.0)]   # 'Assignment' = '과제'
    assert all(c["auto"] for c in s.data["grades"]["301"]["components"])
    assert item(s, "첨부 안내")["origin"] == "found" and item(s, "LAB 4")["status"] == "review"


def test_check_item_remove(tmp_path):
    s = make_store(tmp_path / "s.json")
    it = item(s, "첨부 안내")
    q = chk(s, {"type": "item_remove", "item_id": it["id"], "reason": "첨부 파일 문장을 잘못 읽은 것", "auto": True})
    assert q["rejected_reason"] is None and q["course_id"] == "301" and q["course_short"] == SHORT
    assert q["summary"] == f"[{SHORT}] '첨부 안내' {md(it['due'])} — 일정에서 빼기(제외)"
    assert (q["item_id"], q["item_title"], q["item_origin"], q["auto"], q["by"]) == (it["id"], "첨부 안내", "found", True, "ai")
    for k in ("evidence", "source_key", "source_type", "source_title", "posted_at", "url", "date_basis", "result",
              "applied_at", "undone_at"):
        assert q[k] is None
    assert q["applied"] is False and isinstance(q["notes"], list)
    assert chk(s, {"type": "item_remove", "item_id": it["id"], "reason": "x", "auto": "true"})["auto"] is False
    canvas = item(s, "Report", "canvas")
    q = chk(s, {"type": "item_remove", "item_id": canvas["id"], "reason": "x"})
    assert q["rejected_reason"] is None and any("Canvas의 과제는 그대로" in n for n in q["notes"])
    # 실패: 없는 id, 이유 없음, 다른 과목, 제외한 과목, 이미 제외함
    assert "항목 목록에 없는" in chk(s, {"type": "item_remove", "item_id": "f-nope", "reason": "x"})["rejected_reason"]
    assert "이유" in chk(s, {"type": "item_remove", "item_id": it["id"], "reason": "  "})["rejected_reason"]
    assert "다른 과목" in chk(s, {"type": "item_remove", "item_id": it["id"], "course_id": "302",
                                "reason": "x"})["rejected_reason"]
    hid = item(s, "감상문", include_hidden=True)
    assert "제외한 과목" in chk(s, {"type": "item_remove", "item_id": hid["id"], "reason": "x"})["rejected_reason"]
    s.update_user(it["id"], {"decision": "rejected"})
    assert "이미 제외한" in chk(s, {"type": "item_remove", "item_id": it["id"], "reason": "x"})["rejected_reason"]


def test_check_item_merge(tmp_path):
    s = make_store(tmp_path / "s.json")
    drop, keep = item(s, "첨부 안내"), item(s, "Report", "canvas")
    q = chk(s, {"type": "item_merge", "keep_id": keep["id"], "drop_id": drop["id"], "reason": "같은 날 같은 과제"})
    assert q["rejected_reason"] is None and q["course_id"] == "301"
    assert q["summary"] == (f"[{SHORT}] 같은 과제로 합치기 — '첨부 안내' {md(drop['due'])} 빼고 "
                            f"'Report' {md(keep['due'])}만 남기기")
    assert (q["keep_title"], q["drop_title"]) == ("Report", "첨부 안내")
    assert "서로 다른" in chk(s, {"type": "item_merge", "keep_id": keep["id"], "drop_id": keep["id"],
                                "reason": "x"})["rejected_reason"]
    lab = item(s, "LAB 4")
    assert "과목이 달라요" in chk(s, {"type": "item_merge", "keep_id": keep["id"], "drop_id": lab["id"],
                                  "reason": "x"})["rejected_reason"]
    assert "drop_id" in chk(s, {"type": "item_merge", "keep_id": keep["id"], "drop_id": "f-nope",
                                "reason": "x"})["rejected_reason"]


def test_check_component_remove(tmp_path):
    s = make_store(tmp_path / "s.json")
    q = chk(s, {"type": "component_remove", "course_id": "301", "component": "과제", "reason": "강의계획서에 없음"})
    assert q["rejected_reason"] is None and q["summary"] == f"[{SHORT}] 평가 항목 '과제'(20%) 지우기"
    assert (q["component"], q["weight"], q["merge_into"]) == ("과제", 20.0, None)
    assert any("100% → 80%" in n for n in q["notes"]) and any("다시 넣지 않아요" in n for n in q["notes"])
    # 흔한 표현이 같은 이름('Assignment' = 저장된 '과제'), 대소문자·공백 무시
    assert chk(s, {"type": "component_remove", "course_id": "301", "component": "Assignment",
                   "reason": "x"})["component"] == "과제"
    q = chk(s, {"type": "component_remove", "course_id": 301, "component": "assignment", "merge_into": "report",
                "reason": "보고서에 포함"})
    assert q["rejected_reason"] is None and q["merge_into"] == "Report" and q["new_weight"] == 60.0
    assert q["summary"] == f"[{SHORT}] 평가 항목 '과제'(20%) 지우고 비중을 'Report'에 더하기(40% → 60%)"
    bad = [({"component": "Quiz"}, "없는 평가 항목"), ({"component": "과제", "merge_into": "과제"}, "같은 항목"),
           ({"component": "과제", "merge_into": "Midterm"}, "merge_into"), ({"course_id": "999"}, "과목 목록에 없는"),
           ({"course_id": "303"}, "과목 목록에 없는"), ({"course_id": None}, "course_id")]
    for patch, want in bad:
        p = {"type": "component_remove", "course_id": "301", "component": "과제", "reason": "x", **patch}
        assert want in (chk(s, p)["rejected_reason"] or ""), (patch, chk(s, p)["rejected_reason"])


def test_check_score_clear(tmp_path):
    s = make_store(tmp_path / "s.json")
    cand = next(c for c in gradebook.candidates(s) if c["course_id"] == "301")
    q = chk(s, {"type": "score_clear", "course_id": "301", "component": "Report", "reason": "x"})
    assert q["which"] == "score" and "지울 점수가 없어요" in q["rejected_reason"] and "link" in q["rejected_reason"]
    q = chk(s, {"type": "score_clear", "course_id": "301", "component": "Report", "key": cand["key"],
                "reason": "다른 과목 점수"})
    assert q["rejected_reason"] is None and q["which"] == "link" and q["key"] == cand["key"]
    assert q["summary"] == f"[{SHORT}] 'Report'(40%)에서 점수 후보 'Report 35/40 · 평균 30.5 · 표준편차 4' 떼기(연결 안 함)"
    assert "점수 후보에 없는" in chk(s, {"type": "score_clear", "course_id": "301", "component": "Report",
                                       "key": "txt:nope", "reason": "x"})["rejected_reason"]
    assert "연결돼 있지 않아요" in chk(s, {"type": "score_clear", "course_id": "301", "component": "Project 2",
                                         "key": cand["key"], "reason": "x"})["rejected_reason"]
    assert "which" in chk(s, {"type": "score_clear", "course_id": "301", "component": "Report", "which": "all",
                              "reason": "x"})["rejected_reason"]
    comps = s.data["grades"]["301"]["components"]
    comps[0].update(score=33, max_score=40, mean=29.5, sd=None)
    q = chk(s, {"type": "score_clear", "course_id": "301", "component": "report", "which": "score", "reason": "x"})
    assert q["rejected_reason"] is None and q["summary"] == f"[{SHORT}] 'Report'(40%)의 점수 지우기 — 33/40 · 평균 29.5"


# ---------------------------------------------------------------- 반영·되돌리기

def test_canvas_item_remove_hides_it_from_schedule_and_calendar_and_undo(tmp_path):
    s = make_store(tmp_path / "s.json")
    rep = item(s, "Report", "canvas")
    assert rep["status"] == "scheduled" and rep["id"] in {v["id"] for v in core.calendar_payload(s)[0]}
    th = thread_with(s, {"type": "item_remove", "item_id": rep["id"], "reason": "이미 끝난 과제"})
    p = agent.apply(s, th["id"], 0)
    assert p["applied"] and p["result"] == {"item_id": rep["id"]}
    v = item(s, "Report", "canvas")
    assert (v["status"], v["decision"], v["decided_by"]) == ("rejected", "rejected", "ai")
    assert "이미 끝난 과제" in v["ai_reason"] and v["agent_ref"] == {"thread": th["id"], "proposal": 0}
    assert rep["id"] not in {x["id"] for x in s.calendar_items()}
    assert rep["id"] not in {x["id"] for x in core.calendar_payload(s)[0]}
    assert rep["id"] not in {e["lms_id"] for e in gsync.desired_events(s)}
    s.save()
    again = Store(s.path)                 # 저장·다시 읽기·다음 동기화(다시 만들기) 뒤에도 그대로
    core.rebuild(again)
    assert item(again, "Report", "canvas")["status"] == "rejected"
    agent.undo(again, th["id"], 0)
    v = item(again, "Report", "canvas")
    assert (v["status"], v["decided_by"]) == ("scheduled", None) and rep["id"] in {x["id"] for x in again.calendar_items()}
    assert rep["id"] not in again.data["user"]


def test_found_item_remove_by_rules_is_a_user_decision_and_undo_conflicts_after_edit(tmp_path):
    s = make_store(tmp_path / "s.json")
    lab = item(s, "LAB 4")
    th = thread_with(s, {"type": "item_remove", "item_id": lab["id"], "reason": "요청"}, by="rules")
    agent.apply(s, th["id"], 0)
    v = item(s, "LAB 4")
    assert (v["status"], v["decided_by"], v["ai_reason"]) == ("rejected", "user", None)
    s.update_user(lab["id"], {"decision": "approved"})     # 사용자가 그 사이 다시 승인 → 되돌리지 않는다
    with pytest.raises(agent.AgentConflict):
        agent.undo(s, th["id"], 0)
    assert item(s, "LAB 4")["status"] == "scheduled"


def test_agent_item_record_is_removed_with_absorbed_rule_candidate_and_undo(tmp_path):
    s = make_store(tmp_path / "s.json")
    lab = item(s, "LAB 4")
    # '에이전트에게 묻기'로 넣은 같은 마감(규칙 후보 LAB 4를 품음)과 따로 된 기말고사
    s.data["agent_items"] = [
        {"id": "a-fake00000001", "course_id": "302", "title": "LAB 4 보고서", "due": lab["due"], "kind": "과제",
         "method": "email", "found_by": "ai", "notes": [], "evidence": [{"text": "LAB 4 보고서 마감", "source_type": "manual"}]},
        {"id": "a-fake00000002", "course_id": "301", "title": "기말고사", "due": z(30, 10, 30), "kind": "시험",
         "found_by": "ai", "notes": [], "evidence": [{"text": "기말고사 안내", "source_type": "manual"}]}]
    core.refresh_items(s)
    assert lab["id"] not in {v["id"] for v in s.view()}          # 규칙 후보가 에이전트 항목에 합쳐짐
    th = thread_with(s, {"type": "item_remove", "item_id": "a-fake00000001", "reason": "잘못 넣음"},
                     {"type": "item_remove", "item_id": "a-fake00000002", "reason": "잘못 넣음"})
    assert th["proposals"][0]["item_origin"] == "agent" and "에이전트가 넣은 항목" in th["proposals"][0]["summary"]
    agent.apply(s, th["id"], 0)
    agent.apply(s, th["id"], 1)
    assert s.data["agent_items"] == []
    ids = {v["id"]: v for v in s.view()}
    assert "a-fake00000001" not in ids and "a-fake00000002" not in ids
    assert ids[lab["id"]]["status"] == "rejected"                 # 다시 나온 규칙 후보도 함께 제외(지운 것이 되살아나지 않게)
    s.save()
    again = Store(s.path)
    core.rebuild(again)
    assert "a-fake00000002" not in {v["id"] for v in again.view()}
    agent.undo(again, th["id"], 1)
    agent.undo(again, th["id"], 0)
    assert [r["id"] for r in again.data["agent_items"]] == ["a-fake00000001", "a-fake00000002"]
    ids = {v["id"] for v in again.view()}
    assert {"a-fake00000001", "a-fake00000002"} <= ids and lab["id"] not in ids
    assert lab["id"] not in again.data["user"]
    # 지운 뒤 같은 마감을 다시 넣었으면 되돌리지 않는다
    agent.apply(again, th["id"], 1)
    again.data["agent_items"].append({"id": "a-fake00000002", "course_id": "301", "title": "기말고사",
                                      "due": z(30, 10, 30), "kind": "시험", "found_by": "ai"})
    with pytest.raises(agent.AgentConflict):
        agent.undo(again, th["id"], 1)


def test_item_merge_rejects_drop_with_keep_title_and_undo(tmp_path):
    s = make_store(tmp_path / "s.json")
    drop, keep = item(s, "첨부 안내"), item(s, "Report", "canvas")
    th = thread_with(s, {"type": "item_merge", "keep_id": keep["id"], "drop_id": drop["id"], "reason": "같은 날 같은 과제"})
    p = agent.apply(s, th["id"], 0)
    assert p["result"] == {"item_id": drop["id"], "keep_id": keep["id"]}
    v = item(s, "첨부 안내")
    assert v["status"] == "rejected" and v["ai_reason"].startswith("'Report'와 같은 과제라 하나로 합쳤어요")
    assert item(s, "Report", "canvas")["status"] == "scheduled"
    agent.undo(s, th["id"], 0)
    assert item(s, "첨부 안내")["status"] == "review" and drop["id"] not in s.data["user"]
    agent.apply(s, th["id"], 0)
    s.update_user(drop["id"], {"title": "직접 고친 제목"})
    with pytest.raises(agent.AgentConflict):
        agent.undo(s, th["id"], 0)


def test_component_remove_is_not_readded_by_auto_weights_and_undo(tmp_path):
    s = make_store(tmp_path / "s.json")
    th = thread_with(s, {"type": "component_remove", "course_id": "301", "component": "Assignment", "reason": "중복"})
    p = agent.apply(s, th["id"], 0)
    assert p["result"] == {"course_id": "301", "component": "과제", "merge_into": None}
    assert names(s, "301") == [("Report", 40.0), ("Project 2", 40.0)]
    rec = s.data["grades"]["301"]["removed"]
    assert [(r["name"], r["weight"], r["merge_into"], r["by"]) for r in rec] == [("과제", 20.0, None, "ai")]
    # 다음 동기화: 저장·다시 읽기 → 항목 다시 만들기 → 평가 비중 자동 채우기(강의계획서에는 여전히 Assignment 20%)
    s.save()
    again = Store(s.path)
    core.rebuild(again, mode="full")
    logs = core._auto_weights(again, False)
    assert names(again, "301") == [("Report", 40.0), ("Project 2", 40.0)], logs
    assert not any("로봇학입문" in line for line in logs)                # 바꾼 것 없음
    # 강의계획서의 비중이 바뀌어도(규칙이 확신하는 글) 지운 항목은 빼고 넣는다
    again.data["cache"]["courses"][0]["syllabus_body"] = "<p>평가: Report 30%, Project 2 50%, Assignment 20%</p>"
    core.rebuild(again)
    core._auto_weights(again, False)
    assert names(again, "301") == [("Report", 30.0), ("Project 2", 50.0)]
    assert again.data["grades"]["301"]["removed"][0]["name"] == "과제"
    assert any("다시 넣지 않았어요" in n for n in again.data["grades"]["301"]["auto_notes"])
    with pytest.raises(agent.AgentConflict):                          # 그 사이 성적 설정이 바뀜
        agent.undo(again, th["id"], 0)


def test_component_merge_into_and_undo_restores_exactly(tmp_path):
    s = make_store(tmp_path / "s.json")
    before = copy.deepcopy(s.data["grades"]["301"])
    th = thread_with(s, {"type": "component_remove", "course_id": "301", "component": "과제", "merge_into": "Report",
                         "reason": "보고서에 포함된 항목"}, by="rules")
    agent.apply(s, th["id"], 0)
    assert names(s, "301") == [("Report", 60.0), ("Project 2", 40.0)]
    core._auto_weights(s, False)                                     # 같은 강의계획서 → 합친 비중 그대로
    assert names(s, "301") == [("Report", 60.0), ("Project 2", 40.0)]
    assert s.data["grades"]["301"]["removed"][0]["merge_into"] == "Report"
    agent.undo(s, th["id"], 0)
    assert s.data["grades"]["301"] == before and "removed" not in s.data["grades"]["301"]
    core._auto_weights(s, False)
    assert names(s, "301") == [("Report", 40.0), ("Project 2", 40.0), ("과제", 20.0)]


def test_removed_component_survives_user_save_until_readded(tmp_path):
    s = make_store(tmp_path / "s.json")
    th = thread_with(s, {"type": "component_remove", "course_id": "301", "component": "과제", "reason": "x"})
    agent.apply(s, th["id"], 0)
    gradebook.save_course(s, "301", {"components": [{"name": "Report", "weight": 50}, {"name": "Project 2", "weight": 50}]})
    assert [r["name"] for r in s.data["grades"]["301"]["removed"]] == ["과제"]
    with pytest.raises(agent.AgentConflict):                          # 사용자가 성적 화면에서 고침 → 되돌리지 않음
        agent.undo(s, th["id"], 0)
    gradebook.save_course(s, "301", {"components": [{"name": "Report", "weight": 50}, {"name": "Assignment", "weight": 50}]})
    assert "removed" not in s.data["grades"]["301"]                    # 직접 다시 넣으면 기록에서 빠진다


def test_ai_weights_do_not_readd_removed_component(tmp_path):
    s = make_store(tmp_path / "s.json")
    calls = []

    def run(task, inputs):
        calls.append(task)
        wf = WeightsFound([{"name": "LAB", "weight": 70.0, "fixed": False},
                           {"name": "Assignment", "weight": 30.0, "fixed": False}], 100.0,
                          "실습과 과제로 성적을 매깁니다.", "high", [])
        return SimpleNamespace(ok=True, attempts=1, data=wf, notes=[], error=None)

    session = SimpleNamespace(run=run)
    srcs = core._sources(s.data["cache"], s.data["manual"], now_local())
    gradebook.auto_fill(s, srcs, True, session=session)
    assert calls == ["grading_weights"] and names(s, "302") == [("LAB", 70.0), ("Assignment", 30.0)]
    assert s.data["grades"]["302"]["auto_by"] == "ai"
    th = thread_with(s, {"type": "component_remove", "course_id": "302", "component": "과제", "reason": "중복"})
    agent.apply(s, th["id"], 0)
    assert names(s, "302") == [("LAB", 70.0)]
    gradebook.auto_fill(s, srcs, True, session=session)               # 같은 글 → AI 기억(같은 답)을 다시 써도
    assert names(s, "302") == [("LAB", 70.0)]
    s.data["weights_auto"]["ai"].clear()                              # AI에 다시 물어도
    gradebook.auto_fill(s, srcs, True, session=session)
    assert len(calls) == 2 and names(s, "302") == [("LAB", 70.0)]


def test_score_clear_score_and_link_apply_undo(tmp_path):
    s = make_store(tmp_path / "s.json")
    comps = s.data["grades"]["301"]["components"]
    comps[1].update(score=18, max_score=20, mean=15.5, sd=2.5)
    th = thread_with(s, {"type": "score_clear", "course_id": "301", "component": "Project 2", "which": "score",
                         "reason": "다른 과목 점수"})
    agent.apply(s, th["id"], 0)
    c = s.data["grades"]["301"]["components"][1]
    assert [c[k] for k in ("score", "max_score", "mean", "sd")] == [None, None, None, None]
    agent.undo(s, th["id"], 0)
    c = s.data["grades"]["301"]["components"][1]
    assert [c[k] for k in ("score", "max_score", "mean", "sd")] == [18, 20, 15.5, 2.5]
    agent.apply(s, th["id"], 0)
    s.data["grades"]["301"]["components"][1]["score"] = 17                # 사용자가 그 사이 점수를 넣음
    with pytest.raises(agent.AgentConflict):
        agent.undo(s, th["id"], 0)
    # 연결 떼기: 규칙이 Report에 연결한 점수 후보 → '연결 안 함'(사용자 연결)
    cand = next(x for x in gradebook.candidates(s) if x["course_id"] == "301")
    th2 = thread_with(s, {"type": "score_clear", "course_id": "301", "component": "Report", "which": "link",
                          "key": cand["key"], "reason": "잘못 연결"})
    agent.apply(s, th2["id"], 0)
    ln = gradebook.link_candidates(s, "301", s.data["grades"]["301"]["components"], [cand])[cand["key"]]
    assert (ln["component"], ln["by"]) == (None, "user")
    sec = gradebook.section(s, core.course_summaries(s.data["cache"]))
    rep = next(c for c in sec["courses"] if c["course_id"] == "301")["config"]["components"][0]
    assert rep["name"] == "Report" and rep["members"] == []
    agent.undo(s, th2["id"], 0)
    ln = gradebook.link_candidates(s, "301", s.data["grades"]["301"]["components"], [cand])[cand["key"]]
    assert (ln["component"], ln["by"]) == ("Report", "rules") and "301" not in (s.data.get("grade_links") or {})
    agent.apply(s, th2["id"], 0)
    gradebook.set_link(s, "301", {"key": cand["key"], "component": "Project 2"})
    with pytest.raises(agent.AgentConflict):
        agent.undo(s, th2["id"], 0)


def test_apply_rechecks_and_refuses_stale_target(tmp_path):
    s = make_store(tmp_path / "s.json")
    th = thread_with(s, {"type": "component_remove", "course_id": "301", "component": "과제", "reason": "x"})
    gradebook.save_course(s, "301", {"components": [{"name": "Report", "weight": 100}]})   # 그 사이 사용자가 지움
    with pytest.raises(ValueError):
        agent.apply(s, th["id"], 0)
    assert agent.can_apply(th["proposals"][0])       # 화면 단추는 있다(누르면 다시 검사)


# ---------------------------------------------------------------- 규칙 갈래(AI 꺼짐)

def test_wants_fix_words_with_particles():
    for m in ("Assignment가 중복 등록된 것 같아 없애 줘", "과제를 지워줘", "LAB 4는 빼 주세요", "그거 삭제해줘",
              "중복된 건 하나로 합쳐", "Report 점수 연결 끊어줘", "기말고사 일정 제거해 주세요", "과제 좀 지워 버려",
              "remove the Assignment component", "Can you delete LAB 4?", "please merge them", "삭제 부탁해요",
              "LAB 4 지워줄 수 있어?", "중복 정리해줘"):
        assert agent_fix.wants_fix(m), m
    for m in ("과제 지워지면 어떻게 돼?", "삭제하면 어떻게 돼?", "LAB 4 빼고 알려줘", "Report랑 합쳐서 몇 점이야?",
              "평가 비중 합쳐?", "what happens if I delete LAB 4?", "is the duplicate removed?", "공지 정리해줘",
              "지운 항목 어디서 봐?", "안녕", ""):
        assert not agent_fix.wants_fix(m), m


def test_handle_component_remove_card_from_user_complaint(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = agent_fix.handle(s, "Assignment가 중복 등록된 것 같아 없애 줘")
    assert r["by"] == "rules" and len(r["proposals"]) == 1
    p = r["proposals"][0]
    assert (p["type"], p["course_id"], p["component"], p["by"], p["rejected_reason"], p["applied"]) == \
        ("component_remove", "301", "과제", "rules", None, False)
    assert p["summary"] in r["answer"] and "[반영]" in r["answer"] and "되돌리기" in r["answer"]
    assert names(s, "301")[-1] == ("과제", 20.0)                       # 바로 반영하지 않는다
    for m in ("로봇학입문 성적에서 과제 빼줘", "과제 평가 항목 지워 주세요", "remove the Assignment component please"):
        r = agent_fix.handle(s, m)
        assert [(x["type"], x["component"]) for x in r["proposals"]] == [("component_remove", "과제")], m
    r = agent_fix.handle(s, "과제를 Report에 합쳐줘")
    assert [(x["type"], x["component"], x["merge_into"]) for x in r["proposals"]] == [("component_remove", "과제", "Report")]
    r = agent_fix.handle(s, "Report에 과제를 합쳐줘")
    assert [(x["component"], x["merge_into"]) for x in r["proposals"]] == [("과제", "Report")]


def test_handle_items_scores_and_scope(tmp_path):
    s = make_store(tmp_path / "s.json")
    lab, att, rep = item(s, "LAB 4"), item(s, "첨부 안내"), item(s, "Report", "canvas")
    for m in ("LAB 4 없애줘", "LAB 4는 일정에서 빼 주세요", "LAB 4 과제 없애줘", "remove LAB 4 please"):
        r = agent_fix.handle(s, m)
        assert [(x["type"], x["item_id"]) for x in r["proposals"]] == [("item_remove", lab["id"])], m
    for m in ("첨부 안내 중복 없애줘", "첨부 안내 중복이야 Report랑 합쳐줘", "첨부 안내를 Report에 합쳐줘"):
        r = agent_fix.handle(s, m)
        assert [(x["type"], x["keep_id"], x["drop_id"]) for x in r["proposals"]] == [("item_merge", rep["id"], att["id"])], m
    r = agent_fix.handle(s, "Project 2 말고 과제 없애줘")
    assert [(x["type"], x.get("component")) for x in r["proposals"]] == [("component_remove", "과제")]
    r = agent_fix.handle(s, "Report 점수 지워줘")
    cand = next(c for c in gradebook.candidates(s) if c["course_id"] == "301")
    assert [(x["type"], x["which"], x["key"]) for x in r["proposals"]] == [("score_clear", "link", cand["key"])]
    # 제외한 과목의 항목은 대상이 아니다, 과목 화면(course_id)에서 물으면 그 과목의 것만
    assert agent_fix.handle(s, "감상문 없애줘") is None
    s.data["course_prefs"] = {}
    assert agent_fix.handle(s, "감상문 없애줘")["proposals"][0]["item_title"] == "감상문"
    s.data["cache"]["assignments"].append(asg(22, "302", "Report", z(5, 23, 59)))
    core.rebuild(s)
    both = agent_fix.handle(s, "Report 일정 없애줘")
    assert {x["course_id"] for x in both["proposals"]} == {"301", "302"}
    one = agent_fix.handle(s, "Report 일정 없애줘", course_id="302")
    assert [x["course_id"] for x in one["proposals"]] == ["302"]
    named = agent_fix.handle(s, "회로실험 Report 일정 없애줘")
    assert [x["course_id"] for x in named["proposals"]] == ["302"]


def test_handle_returns_none_quickly_for_non_triggers(tmp_path, monkeypatch):
    s = make_store(tmp_path / "s.json")
    built = []
    real = agent.Ctx

    def spy(store):
        built.append(1)
        return real(store)

    monkeypatch.setattr(agent, "Ctx", spy)
    for m in ("과제 지워지면 어떻게 돼?", "Assignment가 중복 등록된 것 같아", "삭제하면 어떻게 돼?", "LAB 4 언제까지야?",
              "what happens if I delete LAB 4?", "공지 정리해줘", "안녕"):
        assert agent_fix.handle(s, m) is None, m
    assert built == []                                               # 상태를 만들기 전에 끝낸다
    for m in ("공부 계획 지워줘", "LAB 4 알림 꺼줘", "대화 기록 지워줘"):   # 다른 갈래의 것
        assert agent_fix.handle(s, m) is None, m
    for m in ("없는과제XYZ 지워줘", "그거 없애줘"):                      # 대상이 없음
        assert agent_fix.handle(s, m) is None, m


def test_answer_routes_fix_and_apply_undo_through_agent(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = agent.answer(s, "Assignment가 중복 등록된 것 같아 없애 줘", ai_enabled=False)
    assert r["kind"] == "fix" and r["by"] == "rules" and r["status"] == "done"
    th = agent.new_thread(s, "Assignment가 중복 등록된 것 같아 없애 줘", None)
    th.update(r)
    pub = agent.public_threads(s)[-1]["proposals"][0]
    assert pub["can_apply"] and pub["type"] == "component_remove"
    agent.apply(s, th["id"], 0)
    assert names(s, "301") == [("Report", 40.0), ("Project 2", 40.0)]
    agent.undo(s, th["id"], 0)
    assert names(s, "301")[-1] == ("과제", 20.0)


def test_grade_words_route_to_fix_before_item_actions(tmp_path):
    """마감과 평가 항목이 같은 이름(Report)일 때: 성적 낱말이 있으면 평가 항목 지우기, 없으면 지금처럼 마감 제외(액션)가 먼저(리드, 계약 26-3)."""
    s = make_store(tmp_path / "s.json")
    r = agent.answer(s, "Report 평가 항목 지워줘", ai_enabled=False)
    assert r["kind"] == "fix" and [(p["type"], p["component"]) for p in r["proposals"]] == [("component_remove", "Report")]
    assert agent._handles(s, "Report 지워줘", None, [], None)[0][0] == "action"
    assert agent._handles(s, "Report 성적 지워줘", None, [], None)[0][0] == "fix"
