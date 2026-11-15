"""성적 파일 진단(계약 27-3, 사용자 요청 2026-11-15: "PDF를 다운 못 받는 것인지, 받았는데 성적을 산출 못하는 것인지 원인을 확정").
파일·출처마다 찾음 → 받음 → 읽음 → 점수표 → 내 줄 → 후보 → 연결 → 추정 중 처음 멈춘 곳과 한국어 한 줄.
학번·점수·이름은 모두 지어낸 것(가짜). 네트워크 없음."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

import json

import pytest

from lms_agent import gradebook, gradetrace
from lms_agent.config import STATE_FILE, data_dir
from lms_agent.store import Store

CID, HID = "77", "88"
SID = "2099000002"
OTHERS = ["2099000001", "2099000003", "2099000004", "2099000005", "2099000006"]
VERIFIER = "SECRETVERIFIER123"
_VALS = [(18, 25), (17, 27), (15, 20), (19, 28), (12, 18), (16, 24)]


def table(label, ids):
    """한 줄에 한 학생인 점수표 글(PDF에서 뽑은 글 모양): Prelab(20)·Report(30)·Total(50)."""
    lines = [f"{label} Score", "No Student ID Prelab(20) Report(30) Total(50)"]
    for i, sid in enumerate(ids, 1):
        a, b = _VALS[(i - 1) % len(_VALS)]
        lines.append(f"{i} {sid} {a} {b} {a + b}")
    return "\n".join(lines)


def att(n, name, size=1000):
    return {"id": f"cf{n}", "filename": name, "filesize": size, "canvas_file_id": n,
            "url": f"https://lms.example.test/files/{n}/download?verifier={VERIFIER}"}


def post(pid, title, *atts):
    return {"id": pid, "title": title, "created_at": f"2026-11-{10 + pid % 5:02d}T10:00:00+09:00",
            "post_url": f"https://lms.example.test/courses/{CID}/discussion_topics/{pid}?module_item_id=3",
            "content": "", "attachments": list(atts)}


def board(cid, *posts):
    return {"course_id": cid, "tool_id": None, "board_id": "announcements", "board_title": "공지", "source": "canvas",
            "posts": list(posts)}


@pytest.fixture()
def store(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    s = Store(tmp_path / "state.json")
    s.data["settings"]["student_id"] = SID
    s.data["cache"] = {
        "courses": [{"id": CID, "name": "신호와 시스템"}, {"id": HID, "name": "숨긴 과목"}],
        "announcements": [{"id": 1, "title": "Lab9 Score", "context_code": f"course_{CID}", "message": "",
                           "posted_at": "2026-11-12T10:00:00+09:00"}],
        "conversations": [],
        "assignments": [{"id": 501, "course_id": CID, "name": "Final exam", "points_possible": 100,
                         "html_url": f"https://lms.example.test/courses/{CID}/assignments/501",
                         "submission": {"score": 80, "graded_at": "2026-11-14T10:00:00+09:00"},
                         "score_statistics": {"mean": 70, "lower_q": 60, "upper_q": 80, "count": 30}}],
        "boards": [
            board(CID,
                  post(1, "Lab0 Score Announcement", att(1, "Lab0_score.pdf"), att(2, "Lab1_score.pdf")),
                  post(2, "Midterm 성적 공지", att(3, "midterm_scan.pdf"), att(4, "성적표.numbers"),
                       att(13, "score_table.hwp"), att(5, "big_score.pdf", size=60_000_000), att(6, "Quiz_score.pdf")),
                  post(3, "Lecture notes", att(7, "week3.pdf")),
                  post(4, "Grading result", att(8, "attendance.pdf")),
                  post(5, "퀴즈 안내", att(9, "Quiz1.xlsx")),
                  post(6, "Lab2, Lab3 점수", att(10, "Lab2_score.pdf"), att(11, "Lab3_score.pdf")),
                  post(7, "Lab4 묶음", att(12, "scores.zip")),
                  post(8, "개인 파일", att(14, f"{SID}_score.pdf"), att(15, f"{OTHERS[0]}_score.pdf"))),
            board(HID, post(9, "HW score", att(21, "hw_score.pdf"))),
        ],
        "errors": ["공지 'Lab9 Score'의 첨부를 확인하지 못했습니다: 권한 없음 (403)"],
    }
    s.data["course_prefs"] = {HID: {"mode": "hidden", "at": "2026-11-01T00:00:00+09:00"}}
    downloads, texts = {}, {}

    def got(cid, n, name, text=None, **extra):
        p = files / f"{n}_{name}"
        p.write_bytes(b"x")
        key = f"{cid}:cf{n}"
        downloads[key] = {"path": str(p), "filename": name, "size": 1, "course_id": cid, "post_key": f"board:announcements-{n}",
                          "board_id": "announcements", "canvas_file_id": n, "posted_at": "2026-11-12T10:00:00+09:00",
                          "downloaded_at": "2026-11-12T11:00:00+09:00", "pdf_ok": None, "pdf_notes": [], **extra}
        if text is not None:
            texts[key] = {"text": text, "filename": name, "course_id": cid, "board_id": "announcements",
                          "posted_at": "2026-11-12T10:00:00+09:00"}
        return key

    got(CID, 1, "Lab0_score.pdf", table("Lab0", [OTHERS[0], SID, OTHERS[1], OTHERS[2], OTHERS[3]]), pdf_ok=True)
    got(CID, 2, "Lab1_score.pdf", table("Lab1", OTHERS), pdf_ok=True)
    got(CID, 3, "midterm_scan.pdf", pdf_ok=False, pdf_notes=["PDF에서 글자를 찾지 못했습니다(스캔본일 수 있습니다)"])
    got(CID, 4, "성적표.numbers")
    got(CID, 13, "score_table.hwp", read_ok=False, read_notes=["암호가 걸린 파일이라 읽지 못했어요"])
    got(CID, 7, "week3.pdf", "Week 3 slides\nSignals and systems", pdf_ok=True)
    got(CID, 8, "attendance.pdf", "출석 안내\n이번 주 출석은 수요일에 확인합니다.", pdf_ok=True)
    rows = [{"0": "학번", "1": "이름", "2": "Quiz 1 (10)"}] + [
        {"0": float(x), "1": f"학생{i}", "2": v} for i, (x, v) in
        enumerate(zip([OTHERS[0], SID, OTHERS[1], OTHERS[2], OTHERS[3], OTHERS[4]], [8, 9, 7, 6, 10, 5]))]
    sheet_text = "[Sheet1]\n" + "\n".join("\t".join(str(v) for v in r.values()) for r in rows)
    k9 = got(CID, 9, "Quiz1.xlsx", sheet_text, sheet_ok=True)
    texts[k9].update(kind="sheet", sheets=[{"name": "Sheet1", "rows": rows}], sheet_version=1, sheet_reader=1)
    masked = ["2099****01", "2099****02", "2099****03", "2099****02", "2099****05"]   # 내 학번과 맞는 가린 줄이 둘
    got(CID, 10, "Lab2_score.pdf", "\n".join(f"{i} {m} {10 + i} {20 + i} {30 + 2 * i}" for i, m in enumerate(masked, 1)),
        pdf_ok=True)
    lab3 = [f"{i} {x} {10 + i} {20 + i} {30 + 2 * i}" for i, x in enumerate(OTHERS, 1)] + [f"6 {SID} 11"]   # 내 줄만 칸 수가 다름
    got(CID, 11, "Lab3_score.pdf", "\n".join(lab3), pdf_ok=True)
    # zip(계약 27-2 모양): 안 파일마다 '<첨부 key>/<안 이름>' 기록 + parent_key·member·zip_name, 표는 kind "sheet"
    got(CID, 12, "scores.zip", read_ok=True, read_kind="zip", zip_members=[
        {"name": "Lab4_score.pdf", "kind": "pdf", "ok": True, "notes": [], "key": f"{CID}:cf12/Lab4_score.pdf"},
        {"name": "Quiz3.xlsx", "kind": "sheet", "ok": True, "notes": [], "key": f"{CID}:cf12/Quiz3.xlsx"},
        {"name": "Lab5_score.hwp", "kind": "hwp", "ok": False, "notes": ["암호가 걸린 문서라 읽지 못했어요"], "key": None},
        {"name": "photo.png", "kind": None, "ok": False, "notes": [], "key": None}])
    inner = {"course_id": CID, "zip_name": "scores.zip", "parent_key": f"{CID}:cf12",
             "posted_at": "2026-11-12T10:00:00+09:00"}
    texts[f"{CID}:cf12/Lab4_score.pdf"] = {"text": table("Lab4", [SID] + OTHERS[:4]), "filename": "Lab4_score.pdf",
                                           "member": "Lab4_score.pdf", "kind": "zip:Lab4_score.pdf", **inner}
    texts[f"{CID}:cf12/Quiz3.xlsx"] = {"text": sheet_text.replace("Quiz 1", "Quiz 3"), "filename": "Quiz3.xlsx",
                                       "member": "Quiz3.xlsx", "kind": "sheet", "sheet_version": 1, "sheet_reader": 1,
                                       "sheets": [{"name": "Sheet1", "rows": [dict(r) for r in rows]}], **inner}
    got(HID, 21, "hw_score.pdf", table("HW1", [SID] + OTHERS[:4]), pdf_ok=True)
    s.data["downloads"], s.data["pdf_texts"] = downloads, texts
    s.data["materials_errors"] = ["'Quiz_score.pdf': 내려받지 못했습니다"]
    gradebook.save_course(s, CID, {"components": [{"name": "Homework", "weight": 30}, {"name": "Midterm", "weight": 30},
                                                  {"name": "Final", "weight": 40}]})
    return s


def one(rows, file, label=None):
    got = [r for r in rows if r["file"] == file and (label is None or (r["candidate"] or {}).get("label") == label)]
    assert len(got) == 1, [(r["file"], r["stage"]) for r in rows]
    return got[0]


def states(r):
    return {s["id"]: s["state"] for s in r["stages"]}


def test_not_downloaded_size_and_error(store):
    rows = gradetrace.trace(store)
    big = one(rows, "big_score.pdf")
    assert (big["found"], big["downloaded"], big["read"], big["stage"]) == (True, "size", None, "downloaded")
    assert big["stop"] == "파일 크기 한도(50MB)를 넘어 받지 않았어요"
    assert states(big)["found"] == "done" and states(big)["downloaded"] == "stop" and states(big)["read"] == "todo"
    err = one(rows, "Quiz_score.pdf")
    assert (err["downloaded"], err["stage"]) == ("error", "downloaded") and "내려받지 못했어요" in err["stop"]
    pend = one(rows, "2099****02_score.pdf")   # 파일 이름의 내 학번은 가린다
    assert (pend["downloaded"], pend["stop"]) == ("pending", "아직 받지 않았어요 — 다음 동기화 때 받아요")


def test_auto_download_off(store):
    store.data["settings"]["auto_download"] = False
    r = one(gradetrace.trace(store), "Quiz_score.pdf")
    assert (r["downloaded"], r["stage"]) == ("off", "downloaded")
    assert r["stop"].startswith("첨부 자동 저장이 꺼져 있어 받지 않았어요")
    assert one(gradetrace.trace(store), "Lab0_score.pdf")["downloaded"] is True   # 이미 받은 것은 그대로


def test_unreadable_scanned_unsupported_encrypted(store):
    rows = gradetrace.trace(store)
    scan = one(rows, "midterm_scan.pdf")
    assert (scan["downloaded"], scan["read"], scan["stage"]) == (True, "scanned", "read")
    assert "스캔본" in scan["stop"] and scan["notes"] == ["PDF에서 글자를 찾지 못했습니다(스캔본일 수 있습니다)"]
    uns = one(rows, "성적표.numbers")
    assert (uns["read"], uns["stop"]) == ("unsupported", "이 형식(.numbers)은 아직 읽지 못해요")
    enc = one(rows, "score_table.hwp")
    assert (enc["read"], enc["stop"]) == ("encrypted", "암호가 걸려 있어 읽지 못했어요")
    assert states(enc)["score_table"] == "todo"


def test_not_a_score_table(store):
    rows = gradetrace.trace(store)
    r = one(rows, "attendance.pdf")   # 파일 이름은 평범하지만 글 제목이 'Grading result'라 진단에 들어간다
    assert (r["read"], r["score_table"], r["my_row"], r["stage"]) == ("ok", False, None, "score_table")
    assert r["stop"].startswith("점수표가 아니라고 봤어요")
    assert not [x for x in rows if x["file"] == "week3.pdf"]                 # 성적과 관계없어 보이는 첨부는 기본에서 뺀다
    every = gradetrace.trace(store, show_all=True)
    assert one(every, "week3.pdf")["stage"] == "score_table"                 # --all이면 보인다


def test_my_row_missing_masked_many_and_no_result(store):
    rows = gradetrace.trace(store)
    lab1 = one(rows, "Lab1_score.pdf")
    assert (lab1["score_table"], lab1["my_row"], lab1["stage"]) == (True, False, "my_row")
    assert lab1["stop"] == "내 학번 줄이 없어요(다른 분반 파일일 수 있어요)"
    lab2 = one(rows, "Lab2_score.pdf")
    assert (lab2["my_row"], lab2["stop"]) == (False, "가린 학번과 맞는 줄이 여러 개라 내 줄을 고르지 않았어요")
    lab3 = one(rows, "Lab3_score.pdf")
    assert (lab3["my_row"], lab3["stage"], lab3["candidate"]) == (True, "candidate", None)
    assert lab3["stop"].startswith("내 줄은 있지만 점수를 계산하지 못했어요")


def test_no_student_id(store):
    store.data["settings"].pop("student_id")
    rows = gradetrace.trace(store)
    r = one(rows, "Lab0_score.pdf")
    assert (r["score_table"], r["my_row"], r["stage"]) == (True, None, "my_row")
    assert r["stop"] == "학번을 넣지 않아 내 줄을 찾지 않았어요"
    assert states(r)["my_row"] == "stop" and states(r)["candidate"] == "todo"


def test_candidate_but_not_linked(store):
    r = one(gradetrace.trace(store), "Quiz1.xlsx")   # 표 파일(이름에 점수 낱말이 없어도 모양으로 점수표)
    assert (r["kind"], r["score_table"], r["my_row"]) == ("sheet", True, True)
    assert r["candidate"]["score"] == 9 and r["candidate"]["n"] == 6
    assert (r["linked"], r["in_estimate"], r["stage"]) == (None, False, "linked")
    assert r["stop"] == "평가 항목에 연결되지 않았어요 — 성적 화면에서 고르세요"
    # 사용자가 '이 과목 성적에 넣지 않음'으로 정하면 그 이유
    gradebook.set_link(store, CID, {"key": r["candidate"]["key"], "component": None})
    r = one(gradetrace.trace(store), "Quiz1.xlsx")
    assert (r["stage"], r["link_by"], r["stop"]) == ("linked", "user", "성적에 넣지 않기로 했어요(직접 정함)")
    # 직접 고르면 그 항목으로 추정까지
    gradebook.set_link(store, CID, {"key": r["candidate"]["key"], "component": "Midterm"})
    r = one(gradetrace.trace(store), "Quiz1.xlsx")
    assert (r["linked"], r["link_by"], r["in_estimate"], r["stage"], r["stop"]) == ("Midterm", "user", True, None, None)


def test_linked_and_in_estimate(store):
    rows = gradetrace.trace(store)
    r = one(rows, "Lab0_score.pdf")
    assert (r["found"], r["downloaded"], r["read"], r["score_table"], r["my_row"]) == (True, True, "ok", True, True)
    assert r["candidate"]["label"] == "Lab0" and r["candidate"]["score"] == 44 and r["candidate"]["max_score"] == 50
    assert (r["linked"], r["link_by"], r["in_estimate"], r["stage"], r["stop"]) == ("Homework", "rules", True, None, None)
    assert set(states(r).values()) == {"done"}
    assert r["source"] == "announcement" and r["post_title"] == "Lab0 Score Announcement"
    assert r["material_key"] == f"{CID}:cf1" and r["material_exists"] is True
    assert r["url"] == f"https://lms.example.test/courses/{CID}/discussion_topics/1"   # 쿼리는 뗀다
    # 그 항목을 '고정'으로 두면 세부 점수를 쓰지 않는다
    cfg = store.data["grades"][CID]
    for c in cfg["components"]:
        c["fixed"] = c["name"] == "Homework"
    r = one(gradetrace.trace(store), "Lab0_score.pdf")
    assert (r["linked"], r["in_estimate"], r["stage"]) == ("Homework", False, "estimate") and "고정" in r["stop"]


def test_zip_members_are_their_own_rows(store):
    rows = gradetrace.trace(store)
    r = one(rows, "scores.zip › Lab4_score.pdf")
    assert (r["kind"], r["read"], r["candidate"]["label"], r["linked"], r["in_estimate"]) == ("pdf", "ok", "Lab4",
                                                                                            "Homework", True)
    assert r["material_key"] == f"{CID}:cf12" and r["source"] == "announcement"
    q = one(rows, "scores.zip › Quiz3.xlsx")                                  # zip 속 표(kind "sheet")
    assert (q["kind"], q["score_table"], q["my_row"], q["stage"]) == ("sheet", True, True, "linked")
    bad = one(rows, "scores.zip › Lab5_score.hwp")                            # 못 읽은 안 파일도 한 줄
    assert (bad["downloaded"], bad["read"], bad["stage"]) == (True, "encrypted", "read")
    assert not [x for x in rows if x["file"] == "scores.zip"]                 # 읽은 zip 자체는 따로 줄이 없다
    assert one(gradetrace.trace(store, show_all=True), "scores.zip › photo.png")["read"] == "image"


def test_canvas_grade_row(store):
    r = one(gradetrace.trace(store), "Final exam")
    assert (r["kind"], r["source"], r["found"]) == ("canvas_grade", "canvas_grade", True)
    assert {k: v for k, v in states(r).items() if v == "na"} == {"downloaded": "na", "read": "na", "score_table": "na",
                                                                  "my_row": "na"}
    assert r["candidate"]["key"] == "canvas:501" and r["candidate"]["score"] == 80 and r["candidate"]["mean"] == 70
    assert (r["linked"], r["in_estimate"], r["stage"]) == ("Final", True, None)
    assert r["url"] == f"https://lms.example.test/courses/{CID}/assignments/501"
    # 채점하지 않는 과제·면제는 후보가 없는 게 맞다 — 진단에 '실패'로 나오지 않는다
    store.data["cache"]["assignments"] += [
        {"id": 503, "course_id": CID, "name": "출석 체크", "grading_type": "not_graded", "submission": {"score": 1}},
        {"id": 504, "course_id": CID, "name": "면제 과제", "submission": {"score": 0, "excused": True}}]
    names = {x["file"] for x in gradetrace.trace(store, show_all=True)}
    assert "출석 체크" not in names and "면제 과제" not in names


def test_file_merged_into_canvas_grade_follows_it(store):
    """같은 과목·같은 이름·같은 점수가 성적부와 점수표에 함께 있으면 성적부 하나로(계약 27-1) — 파일 줄은 그 후보를 따라간다."""
    store.data["cache"]["assignments"].append({"id": 502, "course_id": CID, "name": "Lab0", "points_possible": 50,
                                               "submission": {"score": 44}})
    rows = gradetrace.trace(store)
    f = one(rows, "Lab0_score.pdf")
    assert (f["linked"], f["in_estimate"], f["stage"]) == ("Homework", True, None)
    assert any("Canvas 성적부 'Lab0'와 같은 점수라 하나로 셌어요" in n for n in f["notes"])
    assert one(rows, "Lab0")["in_estimate"] is True


def test_not_found_row_from_sync_errors(store):
    (r,) = [x for x in gradetrace.trace(store) if not x["found"]]
    assert (r["source"], r["post_title"], r["course_id"], r["stage"]) == ("announcement", "Lab9 Score", CID, "found")
    assert "Canvas가 이 글의 첨부 파일 정보를 주지 않았어요" in r["stop"] and r["notes"]
    assert states(r)["found"] == "stop" and states(r)["downloaded"] == "todo"


def test_hidden_course_skipped_by_default_listed_with_all(store):
    rows = gradetrace.trace(store)
    assert not [r for r in rows if r["course_id"] == HID]
    every = gradetrace.trace(store, show_all=True)
    r = one(every, "hw_score.pdf")
    assert r["course_hidden"] is True and r["notes"][0].startswith("제외한 과목이에요")
    assert (r["stage"], r["stop"], r["in_estimate"]) == ("linked", "제외한 과목이라 성적에 넣지 않아요", False)
    st = gradetrace.state_fields(store, gradebook.section(store, [{"id": CID, "name": "신호와 시스템",
                                                                   "short_name": "신호와 시스템"}]))
    assert st["trace_summary"]["hidden_skipped"] == 1 and st["trace_summary"]["total"] == len(st["trace"])


def test_rows_have_no_student_id_or_attachment_url(store):
    every = gradetrace.trace(store, show_all=True)
    dumped = json.dumps(every, ensure_ascii=False)
    assert SID not in dumped and VERIFIER not in dumped and "verifier" not in dumped
    for o in OTHERS[1:]:
        assert o not in dumped                       # 다른 학생 학번(점수표 안)은 줄에 없다
    s = gradetrace.summary(gradetrace.trace(store))
    assert s["in_estimate"] >= 3 and s["stopped"]["downloaded"] >= 3 and list(s["stopped"]) == [
        k for k, _ in gradetrace.STAGES if k in s["stopped"]]


def test_cli_grades_check_safe_to_paste(store, capsys):
    from lms_agent import cli
    home = data_dir()   # conftest가 임시 폴더로(LMS_AGENT_HOME)
    store.path = home / STATE_FILE
    store.save()
    cli.main(["grades-check"])
    out = capsys.readouterr().out
    assert "성적 파일 진단" in out and "Lab0_score.pdf" in out and "멈춤: 내 학번 줄이 없어요" in out
    assert "찾음 O  받음 X  읽음 -" in out                               # 받지 못한 파일의 단계 표시
    assert "제외한 과목의 1개는 뺐어요" in out and "hw_score.pdf" not in out
    for secret in [SID, VERIFIER, "verifier", "http", *OTHERS]:
        assert secret not in out, secret
    assert "44" not in out                                               # 내 점수 숫자는 --scores일 때만
    cli.main(["grades-check", "--all", "--scores"])
    out = capsys.readouterr().out
    assert "hw_score.pdf" in out and "week3.pdf" in out and "44/50" in out
    for secret in [SID, VERIFIER, "http", *OTHERS]:
        assert secret not in out, secret


def test_cli_without_state(capsys):
    from lms_agent import cli
    with pytest.raises(SystemExit):
        cli.main(["grades-check"])
    assert "먼저 'lms-agent sync'" in capsys.readouterr().out


def test_state_has_trace_and_material_read_fields(store, tmp_path):
    from lms_agent.server import App

    class _Sched:
        def status(self):
            return {"enabled": False, "interval_hours": None}

        def command_text(self):
            return ""

    app = App(store, tmp_path, cfg={}, scheduler=_Sched())
    st = app.state()
    rows = st["grades"]["trace"]
    assert rows and st["grades"]["trace_summary"]["total"] == len(rows)
    assert {"candidates", "courses"} <= set(st["grades"])                      # 성적 묶음은 그대로
    assert "trace" not in st["exams"]
    enc = next(m for m in st["materials"]["items"] if m["filename"] == "score_table.hwp")
    assert (enc["read_ok"], enc["read_notes"]) == (False, ["암호가 걸린 파일이라 읽지 못했어요"])
    scan = next(m for m in st["materials"]["items"] if m["filename"] == "midterm_scan.pdf")
    assert scan["pdf_ok"] is False and "read_ok" not in scan          # PDF·표는 pdf_ok·sheet_ok가 말한다
