"""AI로 성적 철저히 찾기(계약 29-2, 사용자 요청 2026-11-19: "성적조회 Claude CLI를 통해서 철저하게").
규칙이 못 읽은 성적 파일을 AI(score_read)로 읽되 **다른 학생의 학번·이름·점수 줄은 보내지 않는다** — 가리기가 가장 중요하다.
검사(지어낸 점수·평균·근거 거절), 후보 → 연결 → 추정, '넣지 않음', 같은 글 다시 묻지 않기, 동기화 뒤 한도, 백그라운드 경로,
능력(plan·run), 성적 파일 진단의 AI 칸. 학번·이름·점수는 모두 지어낸 것(가짜), 진짜 AI·네트워크 없음(가짜 실행기만)."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

import json
import re
import subprocess
import types

import pytest

from lms_agent import actions, agent_cli, aiscores, gradebook, gradetrace, mock_ai
from lms_agent import capabilities as C
from lms_agent.api import ApiError
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import iso, now_local

CID = "77"
SID = "2099000002"
ME_NAME = "홍길동"
OTHER_IDS = ["2099000001", "2099KU0501", "2099****07", "2099000005", "2099000006", "2099000007", "2099000008"]
OTHER_NAMES = ["김철수", "이영희", "박*수", "Minsu", "최민", "정수아", "윤서준"]
OTHER_ROWS = [(16, 25, 41), (15, 22, 37), (12, 20, 32), (19, 28, 47), (14, 21, 35), (17, 24, 41), (13, 23, 36)]
MY_ROW = (18, 27, 45)


def table_text(label="Lab5", my_id=SID, others=OTHER_IDS, mine_at=2):
    """한 줄에 한 학생인 점수표(PDF에서 뽑은 글 모양). 내 줄은 mine_at번째."""
    lines = [f"2026-2 신호와 시스템 {label} 채점 결과 (만점 50점)", "No 학번 이름 Prelab(20) Report(30) Total(50)"]
    people = list(zip(others, OTHER_NAMES, OTHER_ROWS))
    people.insert(mine_at, (my_id, ME_NAME, MY_ROW))
    for i, (sid, name, (a, b, c)) in enumerate(people, 1):
        lines.append(f"{i} {sid} {name if name != 'Minsu' else 'Kim Minsu'} {a} {b} {c}")
    lines += ["평균 15.9 23.9 39.7", "※ 이의신청은 조교 메일 ta.lab@example.ac.kr 로"]
    return "\n".join(lines)


def other_row_strings():
    return [f"{a} {b} {c}" for a, b, c in OTHER_ROWS]


def leaks(text: str) -> list[str]:
    """보낸 글에 다른 학생의 학번·이름·점수 줄이 있으면 그것들."""
    bad = [x for x in OTHER_IDS + OTHER_NAMES if x in text]
    bad += [r for r in other_row_strings() if r in text]
    bad += [x for x in (ME_NAME, SID) if x in text]   # 내 이름·학번도 보내지 않는다(자리 표시로)
    return bad


def reply(body: dict) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)


def fake(*replies):
    """가짜 AI CLI: 차례대로 답한다(함수면 보낸 글을 받아 답을 만든다). 보낸 글은 .calls에."""
    calls = []

    def run(args, input, timeout, cwd):
        calls.append(input)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        return subprocess.CompletedProcess(args, 0, reply(r(input) if callable(r) else r), "")
    run.calls = calls
    return run


def my_line(sent: str) -> str:
    return next(ln for ln in sent.splitlines() if ln.startswith(aiscores.MY_ROW))


class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.json")
    s.data["settings"].update(student_id=SID, ai_cli=True)
    s.data["cache"] = {"courses": [{"id": CID, "name": "신호와 시스템"}], "announcements": [], "conversations": [],
                       "assignments": [], "boards": [], "errors": []}
    s.data["grades"] = {CID: {"components": [{"name": "Homework assignments", "weight": 40},
                                             {"name": "Final exam", "weight": 60}], "dist": None, "class_size": None}}
    s.data["pdf_texts"] = {f"{CID}:cf5": {"text": table_text(), "course_id": CID, "filename": "Lab5_score.pdf",
                                          "post_key": "board:announcements-5", "posted_at": "2026-11-12T10:00:00+09:00"}}
    s.save()
    return s


@pytest.fixture()
def no_rules(monkeypatch):
    """규칙이 이 파일들을 못 읽는다고 치기(규칙이 좋아져도 AI 경로 시험이 흔들리지 않게)."""
    monkeypatch.setattr(gradebook, "_score_results", lambda key, rec, sid: [])


def session(store, runner, **kw):
    return agent_cli.AiSession(store, runner=runner, command="claude", enabled=True, **kw)


# ================================================================ 가리기(개인정보)

def test_masking_keeps_only_my_row_and_shape():
    p = aiscores.build_input({"text": table_text(), "filename": "Lab5_score.pdf"}, SID, [ME_NAME],
                             components=["Homework assignments"], course="신호와 시스템")
    assert p["ok"] and p["why"] is None
    sent = p["text"]
    assert leaks(sent) == []
    assert "ta.lab@example.ac.kr" not in sent and "[이메일]" in sent
    mine = my_line(sent)
    assert "[내 학번]" in mine and "18 27 45" in mine and "[이름]" in mine
    shape = [ln for ln in sent.splitlines() if ln.startswith(aiscores.OTHER_ROW)]
    assert len(shape) == aiscores.SAMPLE_ROWS and all(not re.search(r"\d", ln) for ln in shape)
    assert all("[학번]" in ln and "<수>" in ln for ln in shape)
    assert "omitted" in sent and "2026-2 신호와 시스템 Lab5 채점 결과 (만점 50점)" in sent   # 제목은 그대로
    assert "No 학번 이름 Prelab(20) Report(30) Total(50)" in sent and "평균 15.9 23.9 39.7" in sent
    # 앱이 계산한 열 통계(모든 줄로) — 내 값·내 등수
    total = next(c for c in p["stats"] if c["name"] == "Total")
    assert total["n"] == 8 and total["mine"] == 45 and total["rank"] == 2 and total["max_score"] == 50
    assert "내 값/my value 45" in sent and "내 등수/my rank 2/8" in sent
    assert set(p["values"]) >= {18.0, 27.0, 45.0} and 15.9 in p["stated"]


def test_masking_letter_ids_masked_ids_and_names_next_to_ids():
    s = aiscores.mask_text("2025KU0501 김철수 / 2021****00 박*수 / 2099-12-3456 이영희", SID)
    assert "2025KU0501" not in s and "2021****00" not in s and "2099-12-3456" not in s
    assert s.count("[학번]") == 3 and "김철수" not in s and "박*수" not in s and "이영희" not in s
    assert aiscores.mask_text("제출일 2026-11-18, 점수 18", SID) == "제출일 2026-11-18, 점수 18"   # 날짜·점수는 그대로
    assert aiscores.mask_text(f"{SID}_{ME_NAME}_score.pdf", SID, mine=True) == "[내 학번]_[이름]_score.pdf"
    # 학번이 있는 줄: 이름·영문 이름은 모두, 상태 낱말(결시)은 그대로
    row = aiscores.mask_text("3 2099000003 Kim Minsu 결시 김OO 18", SID, row=True)
    assert row == "3 [학번] [이름] [이름] 결시 [이름] 18"
    for tok in ("2099000003", "20990000031", "2099KU0501", "2099****07", "2099-00-0003", "B712345"):
        assert aiscores.id_like(tok), tok
    for tok in ("Lab0", "KECE302", "2026-11-18", "11-18", "50", "Prelab", "87.5"):
        assert not aiscores.id_like(tok), tok
    assert aiscores.mask_text("문의 010-1234-5678", SID) == "문의 [전화]"   # 전화번호는 학번 줄로 보지 않고 [전화]
    assert aiscores._info(["문의", "010-1234-5678"], SID)["ids"] == 0


def test_my_id_with_separators_and_masked_unique_row():
    text = table_text(my_id="2099-00-0002")
    p = aiscores.build_input({"text": text, "filename": "Lab5.pdf"}, SID)
    assert p["ok"] and "18 27 45" in my_line(p["text"]) and "2099-00-0002" not in p["text"]
    text = table_text(my_id="2099 000002")   # 띄어 쓴 내 학번
    p = aiscores.build_input({"text": text, "filename": "Lab5.pdf"}, SID)
    assert p["ok"] and "18 27 45" in my_line(p["text"])
    # 가린 학번이 내 학번과 맞는 줄이 하나뿐 → 내 줄
    others = [x for x in OTHER_IDS if "*" not in x]
    p = aiscores.build_input({"text": table_text(my_id="2099****02", others=others), "filename": "x.pdf"}, SID)
    assert p["ok"] and "[내 학번]" in my_line(p["text"]) and "2099****02" not in p["text"]


def test_ambiguous_masked_rows_are_not_sent_unless_a_name_picks_one():
    lines = ["Quiz 3 결과", "학번 이름 점수(10)", "2099****02 김*수 7", "2099****02 홍*동 9", "2099000011 이영희 5",
             "2099000012 최민 6"]
    rec = {"text": "\n".join(lines), "filename": "quiz3_score.pdf"}
    p = aiscores.build_input(rec, SID)
    assert not p["ok"] and p["why"] == "ambiguous" and p["text"] == "" and "여러 개" in p["reason"]
    p = aiscores.build_input(rec, SID, [ME_NAME])   # 사용자 이름(가린 이름 홍*동)으로 하나를 고름
    assert p["ok"] and "9" in my_line(p["text"]) and "김*수" not in p["text"] and "홍*동" not in p["text"]
    assert "이름으로 내 줄을 골랐어요" in p["text"]
    p = aiscores.build_input({"text": "\n".join(lines[:2] + lines[4:]), "filename": "q.pdf"}, SID)
    assert not p["ok"] and p["why"] == "no_row"


def test_stream_layout_rebuilds_rows_and_sends_only_mine():
    cells = ["Lab2 Score", "No", "학번", "이름", "Prelab(20)", "Report(30)", "Total(50)"]
    people = [("2099000001", "김철수", 16, 25, 41), (SID, ME_NAME, 18, 27, 45), ("2099000003", "이영희", 15, 22, 37),
              ("2099000004", "최민", 14, 21, 35)]
    for i, (sid, name, a, b, c) in enumerate(people, 1):
        cells += [str(i), sid, name, str(a), str(b), str(c)]
    cells += ["평균 15.75 23.75 39.5"]
    p = aiscores.build_input({"text": "\n".join(cells), "filename": "Lab2.pdf"}, SID)
    assert p["ok"]
    sent = p["text"]
    assert "18 27 45" in my_line(sent) and "[내 학번]" in my_line(sent)
    assert all(x not in sent for x in ("2099000001", "2099000003", "김철수", "이영희", "최민", ME_NAME, SID))
    body = sent.split(aiscores.TEXT_HEAD, 1)[1].split(aiscores.STATS_HEAD, 1)[0]
    assert all(v not in body.split() for v in ("41", "37", "35", "16", "25"))   # 다른 학생의 점수는 줄로 안 감
    assert "평균 15.75 23.75 39.5" in sent
    tot = next(c for c in p["stats"] if c["mine"] == 45)
    assert tot["n"] == 4 and tot["rank"] == 1


def test_sheet_rows_masked_by_cell():
    rows = [{"0": "Quiz2 결과"}, {"0": "학번", "1": "이름", "2": "Quiz 2 (10)", "3": "비고"}]
    data = [(2099000001.0, "김철수", 7, ""), (float(SID), ME_NAME, 9, "재제출"), ("2099KU0501", "이영희", 6, ""),
            (2099000004.0, "Park Jisoo", 8, ""), (2099000005.0, "최민", 5, "")]
    for sid, name, q, note in data:
        rows.append({"0": sid, "1": name, "2": q, **({"3": note} if note else {})})
    rows.append({"0": "평균", "2": 7})
    rec = {"kind": "sheet", "sheets": [{"name": "Quiz2", "rows": rows}], "text": "", "filename": "Quiz2_성적.xlsx"}
    p = aiscores.build_input(rec, SID)
    assert p["ok"]
    sent = p["text"]
    assert all(x not in sent for x in ("2099000001", "2099KU0501", "김철수", "이영희", "Park", "Jisoo", "최민", SID, ME_NAME))
    assert my_line(sent) == aiscores.MY_ROW + "[내 학번] | [이름] | 9 | 재제출"
    assert "학번 | 이름 | Quiz 2 (10) | 비고" in sent and "평균 |  | 7" in sent
    q = next(c for c in p["stats"] if c["mine"] == 9)
    assert q["n"] == 5 and q["rank"] == 1 and q["max_score"] == 10


def test_names_without_ids_and_short_id_tables_are_not_sent_raw():
    """학번 모양이 아닌(5자리) 번호로 된 표의 다른 학생 줄이 '제목'처럼 새지 않는다 — 내 줄을 못 찾으면 아무것도 안 보냄."""
    text = "\n".join(["Lab1 점수", "번호 이름 점수", "12345 김철수 18 25 43", "12346 이영희 17 20 37", "12347 최민 15 22 37"])
    p = aiscores.build_input({"text": text, "filename": "lab1_score.pdf"}, SID)
    assert not p["ok"] and p["text"] == ""
    # 내 줄이 있는 표 뒤에 학번 없는 학생 줄('수석' 줄 등)이 있어도 숫자가 많은 줄은 보내지 않고, 이름은 가린다
    text = table_text() + "\n12345 김민준 18 25 43\n수석: 윤도현 (50점)"
    p = aiscores.build_input({"text": text, "filename": "Lab5_score.pdf"}, SID)
    assert "김민준" not in p["text"] and "18 25 43" not in p["text"] and "윤도현" not in p["text"]


def test_end_to_end_stdin_has_no_other_students(store, no_rules):
    """실제로 AI CLI에 들어가는 표준 입력 전체(지시·스키마·글)에 다른 학생 정보가 없다."""
    run = fake(mock_ai._score_read)
    out = aiscores.lookup(store, session(store, run))
    assert out["asked"] == 1 and out["found"] == 1
    assert len(run.calls) == 1 and leaks(run.calls[0]) == []
    assert aiscores.MARK in run.calls[0] and "[내 학번]" in run.calls[0]


# ================================================================ 검사(지어낸 점수·평균·근거·평가 항목)

def _prep():
    return aiscores.build_input({"text": table_text(), "filename": "Lab5_score.pdf"}, SID,
                                components=["Homework assignments", "Final exam"])


def _good(sent, **over):
    item = {"label": "Lab5", "column": "Total", "score": 45, "evidence": my_line(sent), "confidence": "high",
            "component": "Homework assignments"}
    item.update(over)
    return {"scores": [item], "note": "ok"}


def _run(prep, *replies):
    run = fake(*replies)
    res = agent_cli.run_task("score_read", {"prepared": prep, "components": ["Homework assignments", "Final exam"]},
                             runner=run, command="claude")
    return res, run


def test_valid_answer_passes_and_app_fills_class_stats():
    prep = _prep()
    res, run = _run(prep, lambda sent: _good(sent))
    assert res.ok and res.attempts == 1
    r = res.data["scores"][0]
    assert r["score"] == 45 and r["component"] == "Homework assignments" and r["max_score"] == 50
    assert r["n"] == 8 and r["rank"] == 2 and r["mean"] == pytest.approx(sum(t[2] for t in OTHER_ROWS + [MY_ROW]) / 8, abs=0.001) and r["high"] == 47
    assert any("앱이" in n for n in r["notes"])


@pytest.mark.parametrize("over, why", [
    ({"score": 44}, "내 줄"),                                   # 지어낸(계산한) 점수
    ({"score": 41}, "내 줄"),                                   # 다른 학생의 점수(보내지도 않았다)
    ({"mean": 42.0}, "mean"),                                   # 글에도 앱 통계에도 없는 평균
    ({"sd": 1.23}, "sd"),
    ({"evidence": "3 [내 학번] 18 27 45 (최고점)"}, "evidence"),  # 고친 인용
    ({"component": "Midterm"}, "component"),                    # 없는 평가 항목
])
def test_invented_values_are_rejected_then_corrected(over, why):
    prep = _prep()
    res, run = _run(prep, lambda sent: _good(sent, **over), lambda sent: _good(sent))
    assert res.ok and res.attempts == 2
    assert why in run.calls[1] and "검사를 통과하지 못했습니다" in run.calls[1]   # 고쳐 달라고 오류를 인용
    res, _ = _run(prep, lambda sent: _good(sent, **over))   # 두 번 다 틀리면 반영하지 않음
    assert not res.ok and res.data is None


def test_stated_mean_and_app_mean_are_both_accepted():
    prep = _prep()
    res, _ = _run(prep, lambda sent: _good(sent, mean=39.7))          # 글에 적힌 평균(요약 줄)
    assert res.ok and res.data["scores"][0]["mean"] == pytest.approx(39.7)
    res, _ = _run(prep, lambda sent: _good(sent, mean=39.29))  # 앱 통계(39.25) ±0.05
    assert res.ok
    res, _ = _run(prep, lambda sent: _good(sent, component=None, max_score=70))   # 글에 없는 만점은 뺀다(점수는 남김)
    r = res.data["scores"][0]
    assert res.ok and r["component"] is None and r["max_score"] == 50 and any("만점" in n for n in r["notes"])
    res, _ = _run(prep, {"scores": [], "note": "내 점수를 찾지 못했어요"})
    assert res.ok and res.data["scores"] == []


def test_build_refuses_without_own_row():
    rec = {"text": "\n".join(["학번 점수", "2099000011 5", "2099000012 6", "2099000013 7"]), "filename": "x.pdf"}
    res = agent_cli.run_task("score_read", {"rec": rec, "sid": SID}, runner=fake({"scores": []}), command="claude")
    assert not res.ok and res.attempts == 0 and "보낼 글" in res.error


# ================================================================ 후보·연결·추정·넣지 않음

def _course(sec):
    return next(c for c in sec["courses"] if str(c["course_id"]) == CID)


def _section(store):
    from lms_agent.core import course_summaries
    return gradebook.section(store, course_summaries(store.data["cache"]))


def test_ai_candidates_link_and_enter_the_estimate(store, no_rules):
    assert _course(_section(store))["estimate"] is None or not _course(_section(store))["config"]["components"][0]["members"]
    aiscores.lookup(store, session(store, fake(lambda s: _good(s))))
    cands = [c for c in gradebook.candidates(store) if c["source_type"] == "ai_score"]
    assert len(cands) == 1
    c = cands[0]
    assert c["key"] == f"ai:{CID}:cf5#Lab5" and c["decided_by"] == "ai" and c["score"] == 45
    assert "AI가" in c["reason"] and "근거" in c["reason"] and "2099****02" in c["reason"]   # 화면에는 가린 학번
    assert "[내 학번]" not in c["reason"] and "[이름]" not in c["reason"]
    sec = _section(store)
    hw = next(x for x in _course(sec)["config"]["components"] if x["name"] == "Homework assignments")
    assert [m["key"] for m in hw["members"]] == [c["key"]] and hw["derived"]["score_pct"] == pytest.approx(90.0)
    assert _course(sec)["estimate"] is not None
    shown = next(x for x in sec["candidates"] if x["key"] == c["key"])
    assert shown["component"] == "Homework assignments" and shown["link_by"] in ("ai", "rules")


def test_rejecting_removes_it_from_the_estimate(store, no_rules):
    aiscores.lookup(store, session(store, fake(lambda s: _good(s))))
    key = f"ai:{CID}:cf5#Lab5"
    gradebook.set_link(store, CID, {"key": key, "component": None})   # 화면의 '넣지 않음'
    sec = _section(store)
    hw = next(x for x in _course(sec)["config"]["components"] if x["name"] == "Homework assignments")
    assert hw["members"] == []
    shown = next(x for x in sec["candidates"] if x["key"] == key)
    assert shown["component"] is None and shown["link_by"] == "user"
    # 같은 파일을 다시 물어도(글이 바뀜) 라벨이 같으면 key를 이어 써서 '넣지 않음'이 남는다
    store.data["pdf_texts"][f"{CID}:cf5"]["text"] += "\n(재공지)"
    aiscores.lookup(store, session(store, fake(lambda s: _good(s))))
    assert [c["key"] for c in gradebook.candidates(store) if c["source_type"] == "ai_score"] == [key]
    hw = next(x for x in _course(_section(store))["config"]["components"] if x["name"] == "Homework assignments")
    assert hw["members"] == []


def test_rules_reading_the_file_suppresses_ai(store):
    """규칙이 읽는 파일은 대상이 아니고, 예전에 AI가 찾은 결과가 있어도 후보로 두 번 세지 않는다."""
    assert aiscores.targets(store) == []
    rec = store.data["pdf_texts"][f"{CID}:cf5"]
    store.data["ai_scores"] = {f"{CID}:cf5": {"hash": aiscores.source_hash(rec, SID), "at": iso(now_local()),
                                              "results": [{"key": f"ai:{CID}:cf5#Lab5", "label": "Lab5", "score": 45}],
                                              "error": None}}
    assert [c for c in gradebook.candidates(store) if c["source_type"] == "ai_score"] == []
    assert any(c["key"].startswith("pdf:") for c in gradebook.candidates(store))


# ================================================================ 다시 묻지 않기·동기화 뒤 한도

def test_hash_skip_and_retry(store, no_rules):
    run = fake(lambda s: _good(s))
    sess = session(store, run)
    assert aiscores.lookup(store, sess)["asked"] == 1
    assert aiscores.lookup(store, sess)["asked"] == 0 and len(run.calls) == 1   # 같은 글 → 다시 안 물음
    e = store.data["ai_scores"][f"{CID}:cf5"]
    assert set(e) >= {"hash", "results", "at", "model", "error"} and e["error"] is None
    store.data["pdf_texts"][f"{CID}:cf5"]["text"] += "\n※ 정정: 오타 수정"   # 글이 바뀜 → 다시 물음
    assert aiscores.lookup(store, sess)["asked"] == 1 and len(run.calls) == 2
    # 실패는 기억하되 자동으로는 RETRY_HOURS 뒤, 사용자가 누르면(explicit) 바로 다시
    bad = fake({"scores": [{"label": "Lab5", "score": 99, "evidence": "nope"}]})
    store.data["pdf_texts"][f"{CID}:cf5"]["text"] += "\n."
    r = aiscores.lookup(store, session(store, bad))
    assert r["failed"] == 1 and store.data["ai_scores"][f"{CID}:cf5"]["error"]
    assert aiscores.lookup(store, session(store, bad))["asked"] == 0
    assert aiscores.lookup(store, session(store, run), explicit=True)["found"] == 1


def _many(store, n):
    for i in range(n):
        store.data["pdf_texts"][f"{CID}:cf{10 + i}"] = {
            "text": table_text(label=f"Lab{i}"), "course_id": CID, "filename": f"Lab{i}_score.pdf",
            "posted_at": f"2026-11-{10 + i:02d}T10:00:00+09:00"}


def test_after_sync_asks_at_most_three_and_respects_the_daily_limit(store, no_rules):
    _many(store, 4)   # 대상 5개
    run = fake(lambda s: _good(s, label=re.search(r"label from the file name: (\S+)", s).group(1)))
    r = aiscores.after_sync(store, session(store, run, max_calls=20))
    assert r["asked"] == aiscores.AUTO_PER_SYNC == 3 and len(run.calls) == 3
    r = aiscores.after_sync(store, session(store, run, max_calls=20))
    assert r["asked"] == 2 and len(store.data["ai_scores"]) == 5
    assert aiscores.after_sync(store, None)["asked"] == 0                       # AI 꺼짐(세션 없음)
    # 하루 한도를 다 썼으면 묻지 않고 이유를 남긴다(기억하지 않음 — 다음에 다시)
    store.data["pdf_texts"][f"{CID}:cf5"]["text"] += "\n."
    store.data["settings"]["ai_daily_limit"] = 30
    store.data["ai_runs"] = [{"at": iso(now_local()), "task": "x", "ok": True, "attempts": 30}]
    r = aiscores.after_sync(store, session(store, run, max_calls=20))
    assert r["asked"] == 0 and "한도" in r["error"] and len(run.calls) == 5
    assert aiscores.needs(store, aiscores.targets(store)[0]) or any(aiscores.needs(store, t) for t in aiscores.targets(store))


def test_no_student_id_no_targets(store, no_rules):
    store.data["settings"].pop("student_id")
    assert aiscores.targets(store) == [] and "학번" in aiscores.lookup(store, None)["error"]


def test_ambiguous_masked_file_through_real_rules(store):
    """규칙(점수표 해석 그대로)이 가린 학번이 여러 줄과 맞아 내 점수를 못 낸 파일: 이름이 없으면 AI에 보내지 않고 이유만,
    사용자 이름으로 내 줄을 하나로 고르면 AI가 읽은 점수가 후보가 된다."""
    lines = ["Quiz 3 결과", "No 학번 이름 Quiz3(10)", "1 2099****02 김*수 7", "2 2099****02 홍*동 9",
             "3 2099000011 이영희 5", "4 2099000012 최민 6", "5 2099000013 정수아 8", "6 2099000014 윤서준 4"]
    key = f"{CID}:cf30"
    store.data["pdf_texts"] = {key: {"text": "\n".join(lines), "course_id": CID, "filename": "quiz3_score.pdf",
                                     "posted_at": "2026-11-15T10:00:00+09:00"}}
    rec = store.data["pdf_texts"][key]
    assert not aiscores.rules_found(key, rec, SID)
    assert [t["why"] for t in aiscores.targets(store)] == ["ambiguous"]
    run = fake(mock_ai._score_read)
    r = aiscores.lookup(store, session(store, run))
    assert r["asked"] == 0 and r["local"] == 1 and run.calls == []
    assert "여러 개" in store.data["ai_scores"][key]["note"]
    assert aiscores.lookup(store, session(store, run), names=[ME_NAME])["asked"] == 0   # 자동으로는 같은 글을 다시 안 봄
    r = aiscores.lookup(store, session(store, run), names=[ME_NAME], explicit=True)   # 사용자가 누름 → 이름으로 골라 보냄
    assert r["asked"] == 1 and r["found"] == 1 and len(run.calls) == 1
    assert aiscores.after_sync(store, session(store, run))["asked"] == 0   # 찾은 기억은 동기화 뒤(이름 없이)에도 그대로
    assert "김*수" not in run.calls[0] and "홍*동" not in run.calls[0] and "2099****02" not in run.calls[0]
    c = next(c for c in gradebook.candidates(store) if c["source_type"] == "ai_score")
    assert c["score"] == 9


def test_files_without_my_row_are_noted_locally_without_ai(store, no_rules):
    store.data["pdf_texts"][f"{CID}:cf9"] = {"text": table_text(my_id="2099000099"), "course_id": CID,
                                             "filename": "Lab9_score.pdf", "posted_at": "2026-11-01T10:00:00+09:00"}
    assert [t["key"] for t in aiscores.targets(store)] == [f"{CID}:cf5"]   # 내 줄 없는 파일은 대상 아님
    run = fake(lambda s: _good(s))
    r = aiscores.lookup(store, session(store, run), key=f"{CID}:cf9", explicit=True)   # 사용자가 고르면 이유를 남김
    assert r["asked"] == 0 and r["local"] == 1 and run.calls == []
    e = store.data["ai_scores"][f"{CID}:cf9"]
    assert e["results"] == [] and "내 학번 줄이 없어요" in e["note"] and e["local"]


# ================================================================ 백그라운드 경로·능력

@pytest.fixture()
def app(store, tmp_path):
    return App(store, tmp_path, cfg={"user_name": ME_NAME}, ai_runner=mock_ai.runner, ai_command="claude",
               scheduler=_Sched())


def test_background_route_finds_and_merges(app, store, no_rules):
    from lms_agent.features import aiscores as feat
    out = actions._call(app, "/api/grades/ai-lookup", {})
    assert out["job"]["status"] in ("running", "done") and out["job"]["total"] == 1
    assert feat.wait(app)
    st = app.state()
    lk = st["grades"]["ai_lookup"]
    assert lk["enabled"] and not lk["running"] and lk["done"] == 1 and lk["found"] == 1 and lk["pending"] == 0
    assert lk["job"]["status"] == "done" and lk["job"]["found"] == 1 and lk["last_at"]
    cand = next(c for c in st["grades"]["candidates"] if c["source_type"] == "ai_score")
    assert cand["score"] == 45 and cand["component"] == "Homework assignments"
    disk = Store(store.path)
    assert disk.data["ai_scores"][f"{CID}:cf5"]["results"][0]["score"] == 45
    assert disk.data["ai_runs"][-1]["task"] == "score_read" and disk.data["ai_runs"][-1]["ok"]
    # 같은 글 → 다시 눌러도 묻지 않고 바로 끝남(메시지)
    again = actions._call(app, "/api/grades/ai-lookup", {"course_id": CID})
    assert again["job"]["status"] == "done" and again["job"]["total"] == 0 and "이미" in again["job"]["message"]


def test_background_route_errors(app, store, no_rules):
    with pytest.raises(ApiError) as e:
        actions._call(app, "/api/grades/ai-lookup", {"course_id": "999"})
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:
        actions._call(app, "/api/grades/ai-lookup", {"key": "nope"})
    assert e.value.status == 404
    aiscores.set_job(store, {"id": "s-x", "status": "running"})
    with pytest.raises(ApiError) as e:
        actions._call(app, "/api/grades/ai-lookup", {})
    assert e.value.status == 409
    aiscores.set_job(store, None)
    store.data["settings"]["ai_cli"] = False
    with pytest.raises(ApiError) as e:
        actions._call(app, "/api/grades/ai-lookup", {})
    assert e.value.status == 400 and "AI" in str(e.value)
    store.data["settings"]["ai_cli"] = True
    # 성적 저장 경로(/api/grades/<id>)가 이 경로를 가로채지 않는다(과목 'ai-lookup'의 성적 설정이 생기지 않음)
    actions._call(app, "/api/grades/ai-lookup", {})
    assert "ai-lookup" not in (Store(store.path).data.get("grades") or {})


def test_background_merge_keeps_user_changes_made_meanwhile(app, store, no_rules, monkeypatch):
    from lms_agent.features import aiscores as feat
    real = aiscores.lookup

    def slow(work, *a, **kw):   # AI가 도는 사이 사용자가 화면에서 연결을 바꿈(다른 칸)
        res = real(work, *a, **kw)
        gradebook.set_link(store, CID, {"key": "pdf:other", "component": "Final exam"})
        store.save()
        return res
    monkeypatch.setattr(aiscores, "lookup", slow)
    actions._call(app, "/api/grades/ai-lookup", {})
    assert feat.wait(app)
    disk = Store(store.path)
    assert disk.data["grade_links"][CID]["pdf:other"]["component"] == "Final exam"
    assert disk.data["ai_scores"][f"{CID}:cf5"]["results"]


def test_capability_plan_and_run(app, store, no_rules):
    from lms_agent.features import aiscores as feat
    cap = C.REGISTRY["grades.ai_lookup"]
    assert cap.tier == "direct" and not cap.undoable and cap.route == ("POST", "/api/grades/ai-lookup")
    assert C.uncovered_routes() == []
    p = C.plan(app, "grades.ai_lookup", {"course": "신호와 시스템"})
    assert p["ok"] and p["params"]["course"] == CID and "1개" in p["changes"][0]["before"]
    res = C.run(app, p)
    assert res["ok"] and res["undo"] is None and "AI" in res["message"]
    assert feat.wait(app)
    assert any(c["source_type"] == "ai_score" for c in gradebook.candidates(store))
    p = C.plan(app, "grades.ai_lookup", {})   # 다 찾아봤음 → 할 것 없음
    assert not p["ok"] and "없어요" in p["error"]
    store.data["settings"]["ai_cli"] = False
    p = C.plan(app, "grades.ai_lookup", {})
    assert not p["ok"] and "AI가 꺼져" in p["error"]


# ================================================================ 성적 파일 진단의 AI 칸

def _rows(store):
    return gradetrace.state_fields(store, _section(store))


def test_trace_rows_show_ai_status(store, no_rules):
    out = _rows(store)
    row = next(r for r in out["trace"] if r["file"] == "Lab5_score.pdf")
    assert row["ai"]["status"] == "pending" and row["stage"] in ("candidate", "my_row", "score_table")
    assert any("AI로 찾아볼 수 있어요" in n for n in row["notes"])
    assert out["ai_lookup"]["pending"] == 1 and out["ai_lookup"]["enabled"]
    assert all(isinstance(r.get("ai"), dict) and "status" in r["ai"] for r in out["trace"])
    aiscores.lookup(store, session(store, fake(lambda s: _good(s))))
    out = _rows(store)
    row = next(r for r in out["trace"] if r["file"] == "Lab5_score.pdf")
    assert row["ai"]["status"] == "found" and row["found_by"] == "ai" and row["in_estimate"]
    assert row["notes"][0].startswith("AI가 찾음") and row["linked"] == "Homework assignments"
    assert row["ai"]["results"][0]["score"] == 45 and "[내 학번]" not in row["ai"]["results"][0]["evidence"]
    assert out["ai_lookup"]["found"] == 1 and out["ai_lookup"]["pending"] == 0
    assert SID not in gradetrace.report_text(store)


def test_trace_ai_none_error_and_off(store, no_rules):
    aiscores.lookup(store, session(store, fake({"scores": [], "note": "내 점수가 보이지 않아요"})))
    row = next(r for r in _rows(store)["trace"] if r["file"] == "Lab5_score.pdf")
    assert row["ai"]["status"] == "none" and any(n.startswith("AI도 내 점수를 찾지 못했어요") for n in row["notes"])
    store.data["pdf_texts"][f"{CID}:cf5"]["text"] += "\n."
    aiscores.lookup(store, session(store, fake({"scores": [{"label": "x", "score": 1234, "evidence": "zzzz"}]})))
    row = next(r for r in _rows(store)["trace"] if r["file"] == "Lab5_score.pdf")
    assert row["ai"]["status"] == "error" and any(n.startswith("AI로 찾지 못했어요") for n in row["notes"])
    store.data["pdf_texts"][f"{CID}:cf5"]["text"] += "\n.."
    store.data["settings"]["ai_cli"] = False
    out = _rows(store)
    row = next(r for r in out["trace"] if r["file"] == "Lab5_score.pdf")
    assert row["ai"]["status"] is None and not any("AI로 찾아볼" in n for n in row["notes"])
    assert out["ai_lookup"]["enabled"] is False and out["ai_lookup"]["pending"] == 0


def test_imported_file_is_an_upload_row_and_an_ai_target(store, no_rules):
    """성적 화면에서 직접 가져온 파일(계약 29-1, pdf_texts "import:<과목>:<해시12>", source·board_id "upload")."""
    key = f"import:{CID}:0123456789ab"
    store.data["pdf_texts"] = {key: {"text": table_text(label="Lab7"), "course_id": CID, "filename": "lab7.pdf",
                                     "board_id": "upload", "source": "upload", "import_key": key,
                                     "imported_at": "2026-11-19T09:00:00+09:00", "posted_at": "2026-11-19T09:00:00+09:00"}}
    out = _rows(store)
    row = next(r for r in out["trace"] if r["id"].startswith("f:import:"))
    assert row["source"] == "upload" and gradetrace.SOURCE_KO["upload"] and row["posted_at"]
    assert row["ai"]["status"] == "pending"   # 이름에 점수 낱말이 없어도 가져온 성적 파일은 대상
    # 화면(S1)이 읽는 모양
    lk = out["ai_lookup"]
    assert {"running", "job", "done", "found", "pending", "errors", "student_id_set"} <= set(lk)
    assert set(row["ai"]) >= {"status", "results", "note"}
    aiscores.lookup(store, session(store, fake(mock_ai._score_read)))
    out = _rows(store)
    row = next(r for r in out["trace"] if r["id"].startswith("f:import:"))
    assert row["source"] == "upload" and row["ai"]["status"] == "found" and row["found_by"] == "ai"
    c = next(c for c in gradebook.candidates(store) if c["source_type"] == "ai_score")
    assert c["key"] == f"ai:{key}#lab7" and c["score"] == 45   # 라벨은 파일 이름(lab7.pdf)에서


def test_mock_ai_reads_my_row_deterministically():
    prep = _prep()
    res, _ = _run(prep, mock_ai._score_read)
    assert res.ok and res.data["scores"][0]["score"] == 45 and res.data["scores"][0]["label"] == "Lab5"
    out = mock_ai.runner(["claude"], "x\n" + aiscores.MARK + "\n=== 입력 글 시작 / INPUT START ===\n없음", 1, ".")
    assert json.loads(json.loads(out.stdout)["result"])["scores"] == []
