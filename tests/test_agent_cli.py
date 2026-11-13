"""AI 작업 틀(계약 10·12장): 가짜 실행기로만 검사한다(실제 CLI를 띄우지 않음).
실행(stdin으로만 보냄·옵션·재시도·시간 초과·이상한 답), 가리기, 글자 예산, 검사(합 100·근거 원문·날짜),
호출 한도, 실행 기록(내용 없음), 확인함 정리(triage), 평가 묶음, 평가 비중 자동 채우기의 AI 경로."""
import json
import subprocess

import pytest

from lms_agent import agent_cli, gradebook
from lms_agent.store import Store
from lms_agent.timeutil import iso, now_local, parse_iso


def claude_json(model_text, is_error=False):
    return json.dumps({"type": "result", "subtype": "error_during_execution" if is_error else "success",
                       "is_error": is_error, "result": model_text, "session_id": "s",
                       "modelUsage": {"claude-test-model": {"inputTokens": 1}}})


class FakeCLI:
    """replies: 모델 글(str)·dict(JSON으로)·예외·CompletedProcess·함수(args, input) → 그중 하나. 마지막 것은 반복."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, input, timeout, cwd):
        self.calls.append({"args": list(args), "input": input, "timeout": timeout, "cwd": cwd})
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(r) and not isinstance(r, type):
            r = r(args, input)
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, subprocess.CompletedProcess):
            return r
        text = json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r
        return subprocess.CompletedProcess(args, 0, claude_json(text), "")


CMD = r"C:\Users\someone\AppData\Roaming\npm\claude.cmd"

POLICY_HTML = ("<p>Dear All,</p><p>I have attached in the updated grading policy, which includes the midterm quiz (5%) "
               "and the final quiz (5%).</p><ul><li>Homework assignments: 30% (w/ short reports)</li>"
               "<li>Midterm project: 25% (w/ midterm report)</li><li>Midterm quiz: 5%</li>"
               "<li>Final project: 25% (w/ final report)</li><li>Final quiz: 5%</li><li>Attendance: 10%</li></ul>"
               "<p>Best Regards, Prof. Example</p>")
GOOD_WEIGHTS = {"components": [{"name": "Homework assignments", "weight": 30}, {"name": "Midterm project", "weight": 25},
                               {"name": "Midterm quiz", "weight": 5}, {"name": "Final project", "weight": 25},
                               {"name": "Final quiz", "weight": 5}, {"name": "Attendance", "weight": 10}],
                "evidence": "Homework assignments: 30% (w/ short reports)"}


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.json")
    s.data["settings"]["ai_cli"] = True
    return s


# ---------------------------------------------------------------- 찾기·실행

def test_detect_override_and_missing(tmp_path, monkeypatch):
    exe = tmp_path / "my-ai.exe"
    exe.write_text("")
    monkeypatch.setenv(agent_cli.ENV_VAR, str(exe))
    assert agent_cli.detect() == str(exe) and agent_cli.command_name(str(exe)) == "my-ai"
    monkeypatch.setenv(agent_cli.ENV_VAR, str(tmp_path / "nope.exe"))
    assert agent_cli.detect() is None
    monkeypatch.delenv(agent_cli.ENV_VAR)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    assert agent_cli.detect() is None
    assert agent_cli.command_name(CMD) == "claude"


def test_missing_cli_is_a_result_not_an_exception(store, monkeypatch):
    monkeypatch.setattr(agent_cli, "detect", lambda: None)
    res = agent_cli.run_task("grading_weights", {"text": "중간 30%"}, runner=FakeCLI({}), store=store)
    assert not res.ok and "찾지 못했습니다" in res.error and res.attempts == 0
    with pytest.raises(agent_cli.AgentCliError, match="찾지 못했습니다"):
        agent_cli.ask_json("p", "t", "{}", runner=FakeCLI({}))


def test_invocation_stdin_only_flags_and_parsing(store):
    fake = FakeCLI("결과입니다:\n```json\n" + json.dumps(GOOD_WEIGHTS) + "\n```")
    res = agent_cli.run_task("grading_weights", {"text": POLICY_HTML, "is_html": True, "title": "Updated Grading Policy"},
                             runner=fake, command=CMD, store=store)
    assert res.ok and res.attempts == 1 and res.cli == "claude" and res.model == "claude-test-model"
    call = fake.calls[0]
    args = call["args"]
    assert args[0] == CMD and args[1] == "-p" and args[3:5] == ["--output-format", "json"]
    assert "--tools" in args and args[args.index("--tools") + 1] == "" and "--no-session-persistence" in args
    joined = " ".join(args)
    assert "Homework" not in joined and "Updated Grading Policy" not in joined   # 글은 명령줄에 없다
    assert not any(ch in args[2] for ch in '"%^&|<>!\n')                         # .cmd에서도 안전한 지시
    assert "Homework assignments: 30%" in call["input"] and "INPUT START" in call["input"]
    assert "Prof. Example" not in call["input"]                                  # 서명 이름 가림
    assert call["timeout"] == 90 and call["cwd"]
    w = res.data
    assert {c["name"]: c["weight"] for c in w.components}["Midterm quiz"] == 5 and w.total == 100
    assert w.confidence == "medium" and any("AI CLI" in n for n in w.notes)
    assert w.sentence == "Homework assignments: 30% (w/ short reports)"


def test_old_cli_without_new_flags_retried_without_them(store):
    def reply(args, inp):
        if "--tools" in args:
            return subprocess.CompletedProcess(args, 1, "", "error: unknown option '--tools'")
        return subprocess.CompletedProcess(args, 0, claude_json(json.dumps(GOOD_WEIGHTS)), "")
    fake = FakeCLI(reply)
    res = agent_cli.run_task("grading_weights", {"text": POLICY_HTML, "is_html": True}, runner=fake, command=CMD)
    assert res.ok and res.attempts == 1 and len(fake.calls) == 2 and "--tools" not in fake.calls[1]["args"]


def test_timeout_nonzero_and_cli_error_are_results(store):
    res = agent_cli.run_task("grading_weights", {"text": "x"}, command=CMD, store=store,
                             runner=FakeCLI(subprocess.TimeoutExpired("claude", 90)))
    assert not res.ok and "90초" in res.error and res.attempts == 1
    res = agent_cli.run_task("grading_weights", {"text": "x"}, command=CMD,
                             runner=FakeCLI(subprocess.CompletedProcess([], 2, "", f"boom at {CMD}")))
    assert not res.ok and "종료 코드 2" in res.error and "someone" not in res.error   # 전체 경로(사용자 이름) 없음
    res = agent_cli.run_task("grading_weights", {"text": "x"}, command=CMD,
                             runner=lambda a, input, timeout, cwd: subprocess.CompletedProcess(
                                 a, 0, claude_json("Credit balance is too low", is_error=True), ""))
    assert not res.ok and "오류" in res.error and res.attempts == 1   # CLI 자체 오류는 다시 묻지 않는다


def test_bad_json_retried_with_quoted_error_then_ok():
    fake = FakeCLI("죄송합니다, 표를 찾을 수 없어요", GOOD_WEIGHTS)
    res = agent_cli.run_task("grading_weights", {"text": POLICY_HTML, "is_html": True}, runner=fake, command=CMD)
    assert res.ok and res.attempts == 2
    assert "JSON" in fake.calls[1]["input"] and "검사를 통과하지 못했습니다" in fake.calls[1]["input"]


@pytest.mark.parametrize("bad,quote", [
    ({"components": [{"name": "중간", "weight": 30}, {"name": "기말", "weight": 30}], "evidence": "Midterm quiz: 5%"}, "60"),
    ({"components": GOOD_WEIGHTS["components"], "evidence": "The midterm is worth a lot"}, "verbatim"),
    ({"components": [{"name": "중간", "weight": "30%"}]}, "number"),
    ({"components": [{"name": "중간", "weight": 130}]}, "range"),
])
def test_invalid_weights_rejected_after_retry(bad, quote, store):
    fake = FakeCLI(bad)
    res = agent_cli.run_task("grading_weights", {"text": POLICY_HTML, "is_html": True}, runner=fake, command=CMD,
                             store=store)
    assert not res.ok and res.attempts == 2 and res.data is None
    assert quote in fake.calls[1]["input"]                                # 두 번째 질문이 오류를 인용
    with pytest.raises(agent_cli.AgentCliError):
        agent_cli.weights_via_cli(POLICY_HTML, True, runner=FakeCLI(bad), command=CMD)


def test_no_weights_answer_is_none():
    w = agent_cli.weights_via_cli("휴강 안내입니다", runner=FakeCLI({"components": [], "evidence": ""}), command=CMD)
    assert w is None


def test_redaction_before_sending():
    text = ("문의: asic.ta@example.ac.kr, 010-1234-5678, 학번 2026123433 / 2026####33, "
            "자료 https://canvas.example/files/1?verifier=abcdefabcdefabcdefabcdefabcdefabcdef "
            "토큰 " + "Z" * 40 + "\n성적 평가: 중간고사 30%, 기말고사 70%\n김철수 드림")
    fake = FakeCLI({"components": [{"name": "중간고사", "weight": 30}, {"name": "기말고사", "weight": 70}],
                    "evidence": "성적 평가: 중간고사 30%, 기말고사 70%"})
    res = agent_cli.run_task("grading_weights", {"text": text, "names": ["홍길동"]}, runner=fake, command=CMD)
    sent = fake.calls[0]["input"]
    for secret in ("asic.ta@", "010-1234-5678", "2026123433", "2026####33", "verifier", "Z" * 40, "김철수"):
        assert secret not in sent, secret
    assert "[이메일]" in sent and "[학번]" in sent and "[전화]" in sent
    assert res.ok and any("가림" in n for n in res.notes)


def test_text_cap_focuses_on_grading_section():
    long = "강의 소개 " * 3000 + "\n평가 방법: 중간고사 40%, 기말고사 60%\n" + "부록 " * 3000
    fake = FakeCLI({"components": [{"name": "중간고사", "weight": 40}, {"name": "기말고사", "weight": 60}],
                    "evidence": "평가 방법: 중간고사 40%, 기말고사 60%"})
    res = agent_cli.run_task("grading_weights", {"text": long}, runner=fake, command=CMD)
    assert res.ok and res.input_chars <= agent_cli.MAX_TEXT + 200
    assert "평가 방법: 중간고사 40%" in fake.calls[0]["input"]
    assert any("12,000자" in n for n in res.notes)


# ---------------------------------------------------------------- 마감 찾기

def test_deadline_evidence_must_be_verbatim_and_date_must_parse():
    inputs = {"title": "과제: Lab3 Assignment", "posted_at": "2026-11-10T09:00:00+09:00", "is_html": True,
              "text": "<p>Lab3 Assignment is uploaded. Submit to the Canvas box (due : 23:59 on 11/18).</p>"}
    good = {"title": "Lab3", "due": "2026-11-18T23:59:00+09:00", "evidence": "(due : 23:59 on 11/18)"}
    halluc = {"title": "Lab4", "due": "2026-11-25T23:59:00+09:00", "evidence": "Lab4 is due on 11/25"}
    junk = {"title": "Lab5", "due": "next friday", "evidence": "Lab3 Assignment is uploaded."}
    fake = FakeCLI({"deadlines": [good, halluc, junk]}, {"deadlines": [good, halluc]})
    res = agent_cli.run_task("deadline_from_text", inputs, runner=fake, command=CMD)
    assert res.ok and res.attempts == 2
    assert [d["title"] for d in res.data] == ["Lab3"] and res.data[0]["by"] == "ai"
    assert parse_iso(res.data[0]["due"]) == parse_iso("2026-11-18T23:59:00+09:00")   # 같은 순간(CI는 UTC)
    assert "verbatim" in fake.calls[1]["input"]
    res = agent_cli.run_task("deadline_from_text", inputs, runner=FakeCLI({"deadlines": [halluc]}), command=CMD)
    assert not res.ok and res.data is None


# ---------------------------------------------------------------- 한도·기록

def test_session_caps_and_disabled(store):
    off = agent_cli.AiSession(store, runner=FakeCLI("nope"), command=CMD, enabled=False)
    assert "꺼져" in off.run("grading_weights", {"text": "x"}).error
    fake = FakeCLI("not json")
    s = agent_cli.AiSession(store, runner=fake, command=CMD, max_calls=5)
    attempts = [s.run("grading_weights", {"text": "중간 30"}).attempts for _ in range(4)]
    assert attempts == [2, 2, 1, 0] and len(fake.calls) == 5
    assert "이번 동기화" in s.run("grading_weights", {"text": "y"}).error
    store.data["ai_runs"] = [{"at": iso(now_local()), "task": "x", "ok": True, "attempts": 30}]
    s2 = agent_cli.AiSession(store, runner=fake, command=CMD)
    assert "오늘" in s2.run("grading_weights", {"text": "y"}).error and len(fake.calls) == 5


def test_run_log_has_no_content_and_keeps_50(store):
    marker = "비밀표시문자열XYZ"
    for _ in range(55):
        agent_cli.run_task("grading_weights", {"text": f"{marker} 중간고사 30%"}, command=CMD, store=store,
                           runner=FakeCLI({"components": [], "evidence": ""}))
    runs = store.data["ai_runs"]
    assert len(runs) == 50
    assert set(runs[-1]) == {"at", "task", "ok", "attempts", "duration_ms", "input_chars", "cli", "model", "error"}
    dumped = json.dumps(runs, ensure_ascii=False)
    assert marker not in dumped and "중간고사" not in dumped and "someone" not in dumped
    assert agent_cli.calls_today(store) == 50 and len(agent_cli.recent_runs(store)) == 5


# ---------------------------------------------------------------- 확인함 정리(triage)

def _item(iid, title, due, evidence, posted, linked=None, typ="announcement", course=("101", "ASIC설계")):
    return {"id": iid, "origin": "found", "course_id": course[0], "course_name": course[1], "title": title, "due": due,
            "kind": "과제", "method": "email", "confidence": "high", "linked_to": linked, "duplicate_of": None,
            "conflict": None, "notes": [], "evidence": [{"text": evidence, "source_type": typ, "source_title": "t",
                                                         "url": None, "posted_at": posted}]}


@pytest.fixture()
def tstore(store):
    store.data["items"] = [
        _item("f-a", "LAB4 보고서", "2026-11-18T23:59:00+09:00", "LAB4 보고서 마감: 11월 18일(수) 23:59 (asic.ta@example.ac.kr)",
              "2026-11-09T10:00:00+09:00", linked="canvas-101-1012"),
        _item("f-b", "LAB4 보고서", "2026-11-20T23:59:00+09:00", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.",
              "2026-11-12T20:00:00+09:00", linked="canvas-101-1012", typ="inbox"),
        _item("f-h", "휴강", "2026-11-15T23:59:00+09:00", "11월 15일 휴강입니다. 보강은 추후 공지합니다.",
              "2026-11-09T08:00:00+09:00", course=("102", "디지털신호처리")),
        _item("f-u", "중간 프로젝트 보고서", "2026-11-20T23:59:00+09:00", "다음 주 금요일까지 보내 주세요.",
              "2026-11-12T13:00:00+09:00", course=("102", "디지털신호처리")),
    ]
    store.update_user("f-u", {"decision": "rejected"})          # 사용자가 이미 결정
    return store


def D(iid, decision, evidence, reason="근거 원문에 마감이 적혀 있음", **kw):
    return {"id": iid, "decision": decision, "evidence": evidence, "reason": reason, **kw}


def test_triage_applies_verified_decisions(tstore):
    fake = FakeCLI({"decisions": [
        D("f-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.", "연장 메시지가 가장 최근"),
        D("f-a", "rejected", "LAB4 보고서 마감: 11월 18일(수) 23:59", "연장 전 마감"),
        D("f-h", "keep", "11월 15일 휴강입니다.", "휴강은 마감이 아님 — 확인 필요")]})
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert out["ok"] and out["applied"] == 2
    sent = fake.calls[0]["input"]
    assert "f-u" not in sent and "asic.ta@" not in sent                   # 사용자 결정 항목·이메일은 보내지 않음
    assert "f-a" in sent and "f-b" in sent and "최대 하나만" in sent
    view = {v["id"]: v for v in tstore.view()}
    assert view["f-b"]["decision"] == "approved" and view["f-b"]["decided_by"] == "ai"
    assert "연장" in view["f-b"]["ai_reason"] and "근거" in view["f-b"]["ai_reason"]
    assert view["f-a"]["decision"] == "rejected" and view["f-h"]["decision"] == "pending"
    assert view["f-u"]["decided_by"] == "user"
    assert any(s["id"] == "f-h" and "보류" in s["why"] for s in out["skipped"])
    assert tstore.data["ai_triage"]["applied"] == 2 and tstore.data["ai_runs"][-1]["task"] == "triage_review"


def test_triage_rejects_hallucinated_evidence_and_unknown_or_user_items(tstore):
    bad = {"decisions": [D("f-h", "rejected", "11월 15일까지 과제를 제출하세요"),
                         D("f-u", "approved", "다음 주 금요일까지 보내 주세요."),
                         D("f-zzz", "approved", "아무 말")]}
    fake = FakeCLI(bad)
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert out["ok"] and out["applied"] == 0 and out["attempts"] == 2
    assert "evidence" in fake.calls[1]["input"] and "f-zzz" in fake.calls[1]["input"]   # 재질문이 오류를 인용
    whys = {s["id"]: s["why"] for s in out["skipped"]}
    assert "근거 원문" in whys["f-h"] and "없는 id" in whys["f-u"] and "없는 id" in whys["f-zzz"]
    view = {v["id"]: v for v in tstore.view()}
    assert view["f-h"]["decision"] == "pending" and view["f-u"]["decision"] == "rejected"


def test_triage_due_change_must_match_evidence_date(tstore):
    fake = FakeCLI({"decisions": [
        D("f-a", "approved", "LAB4 보고서 마감: 11월 18일(수) 23:59", due="2026-11-25T23:59:00+09:00"),
        D("f-h", "rejected", "11월 15일 휴강입니다.")]})
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    view = {v["id"]: v for v in tstore.view()}
    assert out["applied"] == 2 and view["f-a"]["decision"] == "approved"
    assert view["f-a"]["due"] == "2026-11-18T23:59:00+09:00"            # 근거와 맞지 않는 마감 변경은 버림
    assert any(s["id"] == "f-a" and "±1일" in s["why"] for s in out["skipped"])
    # ±1일 안(자정 → 다음 날 00:00)은 받아들인다
    tstore.update_user("f-a", {"decision": "pending"}, by="ai")
    fake = FakeCLI({"decisions": [D("f-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.",
                                    due="2026-11-21T00:00:00+09:00")]})
    agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert parse_iso({v["id"]: v for v in tstore.view()}["f-b"]["due"]) == parse_iso("2026-11-21T00:00:00+09:00")


def test_triage_conflict_double_approve_rejected(tstore):
    both = {"decisions": [D("f-a", "approved", "LAB4 보고서 마감: 11월 18일(수) 23:59"),
                          D("f-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.")]}
    fake = FakeCLI(both)
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert out["applied"] == 0 and out["attempts"] == 2 and "하나만" in fake.calls[1]["input"]
    assert all(v["decision"] == "pending" for v in tstore.view() if v["id"] in ("f-a", "f-b"))
    # 이미 승인된 같은 과제 후보가 있으면 다른 후보 승인은 거부
    tstore.update_user("f-a", {"decision": "approved"}, by="ai")
    fake = FakeCLI({"decisions": [D("f-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.")]})
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert out["applied"] == 0 and {v["id"]: v for v in tstore.view()}["f-b"]["decision"] == "pending"


def test_triage_skips_items_user_decided_meanwhile_and_respects_switch(tstore):
    def reply(args, inp):
        tstore.update_user("f-h", {"decision": "approved"})          # AI가 생각하는 동안 사용자가 결정
        return {"decisions": [D("f-h", "rejected", "11월 15일 휴강입니다.")]}
    out = agent_cli.auto_triage(tstore, runner=FakeCLI(reply), command=CMD)
    assert out["applied"] == 0 and "사용자가 결정" in out["skipped"][0]["why"]
    assert {v["id"]: v for v in tstore.view()}["f-h"]["decided_by"] == "user"
    tstore.data["settings"]["ai_cli"] = False
    fake = FakeCLI({"decisions": []})
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert not out["ok"] and "꺼져" in out["error"] and not fake.calls


# ---------------------------------------------------------------- 이유: 원문 인용·상대 날짜 계산(계약 13-3)

REL_EV = "중간 프로젝트 보고서는 이메일(dsp.prof@example.ac.kr)로 다음 주 금요일까지 보내 주세요."


@pytest.fixture()
def rstore(tstore):
    tstore.data["items"].append(_item("f-r", "중간 프로젝트 보고서", "2026-11-20T23:59:00+09:00", REL_EV,
                                      "2026-11-12T13:00:00+09:00", course=("102", "디지털신호처리")))
    return tstore


def test_ai_reason_quotes_original_unredacted_sentence(tstore):
    fake = FakeCLI(lambda args, inp: {"decisions": [
        D("f-a", "approved", "LAB4 보고서 마감: 11월 18일(수) 23:59 ([이메일])", "[이메일]로 내라는 공지의 마감")]})
    out = agent_cli.auto_triage(tstore, runner=fake, command=CMD)
    assert out["applied"] == 1 and "asic.ta@" not in fake.calls[0]["input"]           # 보낼 때는 가린다
    reason = {v["id"]: v for v in tstore.view()}["f-a"]["ai_reason"]
    assert "asic.ta@example.ac.kr" in reason and "[이메일]" not in reason and "[" not in reason
    assert reason.startswith("이메일로 내라는 공지의 마감") and "근거" in reason


def test_original_quote_helper_edges():
    ev = [{"text": "제출: asic.ta@example.ac.kr 로 PDF 첨부, 문의 010-1234-5678"}]
    assert agent_cli.original_quote("[이메일] 로 PDF 첨부", ev) == "asic.ta@example.ac.kr 로 PDF 첨부"
    assert agent_cli.original_quote("문의 [전화]", ev) == "문의 010-1234-5678"
    assert agent_cli.original_quote("PDF 첨부", ev) == "PDF 첨부"
    assert agent_cli.original_quote("어디에도 없는 [이메일] 문장", ev) == "어디에도 없는 이메일 문장"   # 자리 표시 괄호는 안 남김


def test_relative_date_reason_shows_computation(rstore):
    fake = FakeCLI({"decisions": [D("f-r", "approved", "다음 주 금요일까지 보내 주세요.",
                                    "상대 날짜라 공지일 기준으로 계산했어요")]})
    out = agent_cli.auto_triage(rstore, runner=fake, command=CMD)
    assert out["applied"] == 1
    reason = {v["id"]: v for v in rstore.view()}["f-r"]["ai_reason"]
    assert "‘다음 주 금요일’ → 공지일 11/12(목) 기준 11/20(금)" in reason
    assert "근거: \"다음 주 금요일까지 보내 주세요.\"" in reason and "[이메일]" not in reason


def test_relative_date_claimed_explicit_is_rejected(rstore):
    claim = {"decisions": [D("f-r", "approved", "다음 주 금요일까지 보내 주세요.", "근거 글에 마감 날짜가 분명히 적혀 있어요")]}
    fake = FakeCLI(claim)
    out = agent_cli.auto_triage(rstore, runner=fake, command=CMD)
    assert out["applied"] == 0 and out["attempts"] == 2 and "상대 날짜" in fake.calls[1]["input"]
    why = next(s["why"] for s in out["skipped"] if s["id"] == "f-r")
    assert "상대 날짜" in why and "다음 주 금요일" in why
    assert {v["id"]: v for v in rstore.view()}["f-r"]["decision"] == "pending"
    # 적힌 날짜(11월 18일)가 있는 근거라면 같은 말도 괜찮다
    ok = FakeCLI({"decisions": [D("f-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.",
                                  "연장 날짜가 분명히 적혀 있어요")]})
    assert agent_cli.auto_triage(rstore, runner=ok, command=CMD)["applied"] == 1


def test_claims_explicit_ignores_negation():
    assert agent_cli.claims_explicit("근거 글에 마감 날짜가 분명히 적혀 있어요")
    assert agent_cli.claims_explicit("마감이 명시되어 있음") and agent_cli.claims_explicit("explicitly stated")
    assert not agent_cli.claims_explicit("날짜가 분명하지 않아 공지일 기준으로 계산했어요")
    assert not agent_cli.claims_explicit("날짜가 적혀 있지 않아 계산")
    assert not agent_cli.claims_explicit("the date is not stated, computed from the post date")


def test_triage_counts_pending_left_and_user_reverted(rstore):
    rstore.update_user("f-h", {"decision": "approved"})
    rstore.update_user("f-h", {"decision": "pending"})            # 사용자가 되돌림 → AI는 건드리지 않음
    fake = FakeCLI(lambda args, inp: {"decisions": [D("f-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.")]})
    out = agent_cli.auto_triage(rstore, runner=fake, command=CMD)
    assert "f-h" not in fake.calls[0]["input"]
    assert out["applied"] == 1 and out["user_reverted"] == 1
    review = [v for v in rstore.view() if v["status"] == "review"]
    assert out["pending_left"] == len(review) == 2                # f-h(되돌림)와 f-r. f-a는 f-b로 대체됨
    assert {v["id"]: v for v in rstore.view()}["f-a"]["status"] == "replaced"
    assert rstore.data["ai_triage"]["pending_left"] == 2 and rstore.data["ai_triage"]["user_reverted"] == 1


# ---------------------------------------------------------------- 평가 묶음

def eval_oracle(args, inp):
    """평가 묶음용 '잘하는' 가짜 AI: 입력을 보고 맞는 답을 준다."""
    if "Homework assignments" in inp:
        return GOOD_WEIGHTS
    if "출석은 10, 과제는 20" in inp:
        return {"components": [{"name": "출석", "weight": 10}, {"name": "과제", "weight": 20},
                               {"name": "중간", "weight": 30}, {"name": "기말", "weight": 40}],
                "evidence": "출석은 10, 과제는 20, 중간은 30, 기말은 40의 비율로 반영한다."}
    if "휴강합니다" in inp:
        return {"components": [], "evidence": ""}
    if "due : 23:59 on 11/18" in inp:
        return {"deadlines": [{"title": "Lab3", "due": "2026-11-18T23:59:00+09:00", "evidence": "(due : 23:59 on 11/18)"}]}
    if "LAB4 보고서는 조교 이메일" in inp:
        assert "asic.ta@" not in inp
        return {"deadlines": [{"title": "LAB4 보고서", "due": "2026-11-18T23:59:00+09:00",
                               "evidence": "마감: 11월 18일(수) 자정"}]}
    if "f-lab4-a" in inp:
        return {"decisions": [D("f-lab4-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다."),
                              D("f-lab4-a", "rejected", "LAB4 보고서 마감: 11월 18일(수) 23:59"),
                              D("f-holiday", "rejected", "11월 15일 휴강입니다.")]}
    if "상위 35% 정도까지 A" in inp:   # grade_distribution(계약 16장)
        return {"kind": "relative", "groups": [{"grades": ["A+", "A0"], "pct": 35},
                                               {"grades": ["B+", "B0"], "pct": 40}],
                "evidence": "수강생의 상위 35% 정도까지 A를 주고, 그다음 40% 정도에 B를 주며"}
    if "P/NP (Pass/Non-pass)" in inp:
        return {"kind": "none"}
    return "모르겠습니다"


def test_eval_set_passes_with_good_agent_and_catches_bad_one():
    rep = agent_cli.run_evals(runner=FakeCLI(eval_oracle), command=CMD)
    assert rep["total"] == len(agent_cli.EVAL_CASES) >= 6
    assert rep["passed"] == rep["total"], [c for c in rep["cases"] if not c["passed"]]
    assert rep["cli"] == "claude" and "input" not in json.dumps(rep)

    def liar(args, inp):  # 근거를 지어내고 같은 과제를 둘 다 승인하는 AI
        if "f-lab4-a" in inp:
            return {"decisions": [D("f-lab4-a", "approved", "LAB4 보고서 마감: 11월 18일(수) 23:59"),
                                  D("f-lab4-b", "approved", "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다."),
                                  D("f-holiday", "approved", "11월 15일까지 제출")]}
        if "due : 23:59" in inp or "LAB4 보고서는" in inp:
            return {"deadlines": [{"title": "X", "due": "2026-12-01T23:59:00+09:00", "evidence": "due on December 1"}]}
        return {"components": [{"name": "시험", "weight": 50}], "evidence": "시험 50%"}
    bad = agent_cli.run_evals(runner=FakeCLI(liar), command=CMD)
    assert bad["passed"] <= 1
    tri = next(c for c in bad["cases"] if c["task"] == "triage_review")
    assert tri["passed"]   # 하네스가 잘못된 결정을 모두 걸러 냈으므로 '반영된 것'은 기대를 어기지 않는다


# ---------------------------------------------------------------- 평가 비중 자동 채우기: AI 경로

def src(text, typ="syllabus", title="강의계획서", posted="2026-09-01T00:00:00+09:00", cid="103", html=False):
    return {"course_id": cid, "course_name": "반도체공학", "type": typ, "id": cid, "title": title,
            "text": text, "is_html": html, "posted_at": posted}


PROSE = "성적 평가는 출석과 과제, 시험을 종합한다. 출석은 10, 과제는 20, 중간은 30, 기말은 40의 비율로 반영한다."


def test_auto_fill_uses_ai_only_when_enabled_and_rules_fail(store, monkeypatch):
    monkeypatch.setattr(agent_cli, "detect", lambda: CMD)
    fake = FakeCLI(eval_oracle)
    # 꺼져 있으면 부르지 않는다
    log = gradebook.auto_fill(store, [src(PROSE)], ai_enabled=False, runner=fake)
    assert not fake.calls and "103" not in store.data["grades"] and any("못 찾은" in x for x in log)
    # 규칙으로 찾으면(high) AI를 부르지 않는다
    rules = src("평가: 중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%", cid="104")
    gradebook.auto_fill(store, [rules], ai_enabled=True, runner=fake)
    assert not fake.calls and store.data["grades"]["104"]["auto_by"] == "rules"
    # 규칙이 못 찾고 켜져 있으면 AI
    log = gradebook.auto_fill(store, [src(PROSE)], ai_enabled=True, runner=fake)
    cfg = store.data["grades"]["103"]
    assert len(fake.calls) == 1 and cfg["auto_by"] == "ai" and any("AI CLI" in x for x in log)
    assert {c["name"]: c["weight"] for c in cfg["components"]} == {"출석": 10, "과제": 20, "중간고사": 30, "기말고사": 40}
    assert all(c["auto"] for c in cfg["components"]) and cfg["auto_source"].startswith("강의계획서: ")
    assert next(c for c in cfg["components"] if c["name"] == "출석")["fixed"]
    # 같은 글이면 다시 부르지 않는다(결과 기억)
    store.data["grades"].pop("103")
    gradebook.auto_fill(store, [src(PROSE)], ai_enabled=True, runner=fake)
    assert len(fake.calls) == 1 and store.data["grades"]["103"]["auto_by"] == "ai"
    sec = gradebook.section(store, [{"id": "103", "short_name": "반도체공학"}])
    assert sec["courses"][0]["auto"] and sec["courses"][0]["auto_by"] == "ai"


def test_auto_fill_ai_failure_logged_not_raised(store, monkeypatch):
    monkeypatch.setattr(agent_cli, "detect", lambda: CMD)
    log = gradebook.auto_fill(store, [src(PROSE)], ai_enabled=True,
                              runner=FakeCLI(subprocess.TimeoutExpired("claude", 90)))
    assert any("AI CLI로 평가 비중을 찾지 못했습니다" in x and "90초" in x for x in log)
    assert "103" not in store.data["grades"] and store.data["ai_runs"][-1]["ok"] is False


# ---------------------------------------------------------------- API (features/ai.py, features/grades.py)

def test_ai_and_grade_settings_routes(tmp_path):
    from lms_agent.api import ApiError, router
    from lms_agent.features import ai as ai_feature
    from lms_agent.server import App

    s = Store(tmp_path / "state.json")
    s.data["items"] = [_item("f-h", "휴강", "2026-11-15T23:59:00+09:00", "11월 15일 휴강입니다.", "2026-11-09T08:00:00+09:00")]
    fake = FakeCLI({"decisions": [D("f-h", "rejected", "11월 15일 휴강입니다.", "휴강 안내라 마감이 아님")]})
    app = App(s, tmp_path, cfg={}, ai_runner=fake, ai_command=CMD)
    app.state = lambda: {"stub": True}   # 다른 기능의 상태 칸과 떼어 이 기능만 본다

    def call(path, body):
        for r, groups in router.match("POST", path):
            out = r.handler(app, body, **groups)
            if out is not None:
                return out

    st = ai_feature.ai_state(app, None)
    assert st["available"] and st["command"] == "claude" and not st["enabled"] and st["calls_today"] == 0
    assert st["limits"] == {"per_sync": 5, "per_day": 30, "max_chars": 12000} and st["last_triage"] is None
    with pytest.raises(ApiError) as e:
        call("/api/ai/triage", {})
    assert e.value.status == 400 and not fake.calls
    call("/api/ai/settings", {"ai_cli": True, "ai_auto_triage": True})
    assert s.data["settings"]["ai_cli"] is True and s.data["settings"]["ai_auto_triage"] is True
    out = call("/api/ai/triage", {})
    assert out["triage"]["applied"] == 1 and len(fake.calls) == 1
    st = ai_feature.ai_state(app, None)
    assert st["enabled"] and st["auto_triage"] and st["last_triage"]["applied"] == 1 and st["calls_today"] == 1
    assert st["recent_runs"][0]["task"] == "triage_review" and "input" not in st["recent_runs"][0]
    assert {v["id"]: v for v in s.view()}["f-h"]["decided_by"] == "ai"
    # /api/grades/settings는 /api/grades/<id>보다 먼저 받는다
    call("/api/grades/settings", {"student_id": "2026123433"})
    assert s.data["settings"]["student_id"] == "2026123433" and "settings" not in (s.data.get("grades") or {})
    with pytest.raises(ValueError):
        call("/api/grades/settings", {"student_id": "x"})
    call("/api/grades/settings", {"student_id": None})
    assert "student_id" not in s.data["settings"]


# ---------------------------------------------------------------- 평가 항목 연결·개수(계약 14-2·14-4)

MAP_INPUT = {"components": ["Homework assignments", "Midterm quiz", "Final quiz"],
             "things": [{"key": "txt:announcement:abc", "label": "Quiz 1", "title": "Quiz 1 결과 (ta@example.ac.kr)",
                         "text": "Quiz 1: 8/10 — 학번 2026123433 문의는 조교에게"}]}


def test_map_score_item_validates_names_and_quotes(store):
    fake = FakeCLI({"links": [{"key": "txt:announcement:abc", "component": "midterm QUIZ", "evidence": "Quiz 1 결과"}]})
    res = agent_cli.run_task("map_score_item", MAP_INPUT, runner=fake, command=CMD, store=store)
    assert res.ok and res.data["links"] == [{"key": "txt:announcement:abc", "component": "Midterm quiz",
                                             "evidence": "Quiz 1 결과"}]
    sent = fake.calls[0]["input"]
    assert "ta@example.ac.kr" not in sent and "2026123433" not in sent and "[이메일]" in sent
    assert "Homework assignments | Midterm quiz | Final quiz" in sent
    # 목록에 없는 이름·지어낸 인용 → 고쳐 묻고, 그래도면 버린다
    bad = FakeCLI({"links": [{"key": "txt:announcement:abc", "component": "Quizzes", "evidence": "Quiz 1 결과"}]})
    res = agent_cli.run_task("map_score_item", MAP_INPUT, runner=bad, command=CMD)
    assert res.ok and res.data["links"] == [] and res.attempts == 2 and "목록" in bad.calls[1]["input"]
    bad = FakeCLI({"links": [{"key": "txt:announcement:abc", "component": "Final quiz", "evidence": "기말 퀴즈입니다"}]})
    res = agent_cli.run_task("map_score_item", MAP_INPUT, runner=bad, command=CMD)
    assert res.ok and res.data["links"] == []
    unknown = FakeCLI({"links": [{"key": "nope", "component": None}]})
    res = agent_cli.run_task("map_score_item", MAP_INPUT, runner=unknown, command=CMD)
    assert res.ok and res.data["links"] == [] and res.data["skipped"]
    none = FakeCLI({"links": [{"key": "txt:announcement:abc", "component": None}]})
    assert agent_cli.run_task("map_score_item", MAP_INPUT, runner=none, command=CMD).data["links"][0]["component"] is None


def test_count_items_needs_quote_and_at_least_observed(store):
    lines = ["Next labs: P&R → STA → SRAM", "Lab3 Assignment"]
    ok = FakeCLI({"count": 7, "evidence": "Next labs: P&R → STA → SRAM"})
    res = agent_cli.run_task("count_items", {"component": "Homework assignments", "family": "lab", "lines": lines,
                                             "low": 4}, runner=ok, command=CMD)
    assert res.ok and res.data == {"count": 7, "evidence": "Next labs: P&R → STA → SRAM"}
    assert "최소 4개" in ok.calls[0]["input"]
    low = FakeCLI({"count": 3, "evidence": "Lab3 Assignment"})
    res = agent_cli.run_task("count_items", {"component": "X", "family": "lab", "lines": lines, "low": 4},
                             runner=low, command=CMD)
    assert not res.ok and res.attempts == 2
    fake_quote = FakeCLI({"count": 9, "evidence": "There are 9 labs"})
    assert not agent_cli.run_task("count_items", {"component": "X", "family": "lab", "lines": lines, "low": 4},
                                  runner=fake_quote, command=CMD).ok
    too_many = FakeCLI({"count": 31, "evidence": "Lab3 Assignment"})
    assert not agent_cli.run_task("count_items", {"component": "X", "family": "lab", "lines": lines},
                                  runner=too_many, command=CMD).ok
    unknown = FakeCLI({"count": None})
    assert agent_cli.run_task("count_items", {"component": "X", "family": "lab", "lines": lines},
                              runner=unknown, command=CMD).data == {"count": None, "evidence": None}
    assert {"map_score_item", "count_items"} <= set(agent_cli.TASKS)


def test_demo_fake_ai_answers_new_tasks():
    from lms_agent import mock_ai
    res = agent_cli.run_task("map_score_item", MAP_INPUT, runner=mock_ai.runner, command="claude")
    assert res.ok and res.attempts == 1 and res.data["links"][0]["component"] is None
    res = agent_cli.run_task("count_items", {"component": "X", "family": "lab", "lines": ["Lab 1"]},
                             runner=mock_ai.runner, command="claude")
    assert res.ok and res.attempts == 1 and res.data["count"] is None
