"""직접 넣은 점수·성적 파일 가져오기(계약 29-1): 검사(한국어 오류), 점수 후보 → 평가 항목(직접 고른 항목이 이김) → 추정,
능력(grades.add_score·update_score·remove_score)의 실행·공통 되돌리기, 가져오기(CSV·xlsx·PDF·zip → 점수 후보), 크기·형식 한도,
경로를 벗어나지 않는 이름, 지우기. 학번·점수·파일은 모두 지어낸 것."""
from __future__ import annotations

import base64
import io
import os
import zipfile
from pathlib import Path

import pytest

from lms_agent import actions, capabilities as caps, gradetrace, manualscores as ms
from lms_agent.api import ApiError
from lms_agent.server import App
from lms_agent.store import Store
from test_grades import _xlsx
from test_pdftext import text_doc

SID = "2021123456"
CID = "101"


class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


def make_app(tmp_path: Path, sid: str | None = SID) -> App:
    s = Store(tmp_path / "state.json")
    if sid:
        s.data["settings"]["student_id"] = sid
    s.data["cache"] = {"fetched_at": "2026-11-10T10:00:00+09:00", "assignments": [], "conversations": [], "boards": [],
                       "errors": [], "announcements": [],
                       "courses": [{"id": "101", "name": "ASIC설계", "course_code": "ASIC101"},
                                   {"id": "102", "name": "디지털신호처리", "course_code": "DSP202"},
                                   {"id": "103", "name": "연구실 안전 교육", "course_code": "SAFE"}]}
    s.data["grades"] = {CID: {"components": [{"name": "Homework assignments", "weight": 30.0},
                                             {"name": "Midterm quiz", "weight": 30.0},
                                             {"name": "Final project", "weight": 40.0}], "dist": None, "class_size": None}}
    s.data["course_prefs"] = {"103": {"mode": "hidden"}}
    s.save()
    return App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": "test-token"}, scheduler=_Sched())


@pytest.fixture()
def app(tmp_path):
    return make_app(tmp_path)


def scores(app, body, cid=CID):
    return actions._call(app, f"/api/grades/{cid}/scores", body)


def err(app, body, cid=CID) -> str:
    with pytest.raises(ValueError) as e:
        scores(app, body, cid)
    return str(e.value)


def course(state, cid=CID):
    return next(c for c in state["grades"]["courses"] if str(c["course_id"]) == cid)


def cand(state, key):
    return next(c for c in state["grades"]["candidates"] if c["key"] == key)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def do_import(app, filename, data: bytes, cid=CID):
    return actions._call(app, f"/api/grades/{cid}/import", {"filename": filename, "data": b64(data)})


def import_err(app, filename, data, cid=CID) -> str:
    with pytest.raises(ValueError) as e:
        actions._call(app, f"/api/grades/{cid}/import", {"filename": filename, "data": data if isinstance(data, str) else b64(data)})
    return str(e.value)


# ---------------------------------------------------------------- 직접 넣은 점수: 검사

def test_add_update_delete_validation(app):
    assert "점수 이름" in err(app, {"action": "add", "score": 9})
    assert "내 점수" in err(app, {"action": "add", "label": "Lab 3"})
    assert "숫자" in err(app, {"action": "add", "label": "Lab 3", "score": "아홉"})
    assert "만점" in err(app, {"action": "add", "label": "Lab 3", "score": 11, "max_score": 10})
    assert "100보다" in err(app, {"action": "add", "label": "Lab 3", "score": 120})
    assert "0보다 커야" in err(app, {"action": "add", "label": "Lab 3", "score": 1, "max_score": 0})
    assert "이상" in err(app, {"action": "add", "label": "Lab 3", "score": -1})
    assert "등수" in err(app, {"action": "add", "label": "Lab 3", "score": 9, "max_score": 10, "n": 30, "rank": 31})
    assert "정수" in err(app, {"action": "add", "label": "Lab 3", "score": 9, "n": 30.5})
    assert "최저점" in err(app, {"action": "add", "label": "Lab 3", "score": 9, "max_score": 10, "high": 5, "low": 7})
    assert "평가 항목이 없어요" in err(app, {"action": "add", "label": "Lab 3", "score": 9, "component": "기말고사"})
    assert "60자" in err(app, {"action": "add", "label": "x" * 61, "score": 9})
    assert "action" in err(app, {"action": "replace", "label": "Lab 3", "score": 9})
    assert "과목을 찾을 수 없어요" in err(app, {"action": "add", "label": "Lab 3", "score": 9}, cid="999")
    assert "제외한 과목" in err(app, {"action": "add", "label": "Lab 3", "score": 9}, cid="103")
    assert "평가 항목이 없어요" in err(app, {"action": "add", "label": "퀴즈", "score": 9, "component": "퀴즈"}, cid="102")
    assert app.store.data.get("manual_scores") in (None, {})   # 거절한 것은 아무것도 남기지 않는다

    s = scores(app, {"action": "add", "label": " Lab   3 ", "score": "9", "max_score": "10", "mean": "7.5", "sd": 1.2,
                     "n": 40, "rank": 5})
    (e,) = s["manual_scores"]["courses"][CID]
    assert e["id"].startswith("ms-") and e["label"] == "Lab 3" and e["by"] == "user" and e["at"]
    assert (e["score"], e["max_score"], e["mean"], e["sd"], e["n"], e["rank"]) == (9.0, 10.0, 7.5, 1.2, 40, 5)
    assert s["manual_score_result"] == {"action": "add", "id": e["id"], "text": "Lab 3 9/10"}

    assert "찾을 수 없어요" in err(app, {"action": "update", "id": "ms-nope", "score": 8})
    assert "바꿀 값" in err(app, {"action": "update", "id": e["id"]})
    assert "내 점수" in err(app, {"action": "update", "id": e["id"], "score": None})
    assert "만점" in err(app, {"action": "update", "id": e["id"], "score": 12})   # 지금 만점 10과 비교
    s = scores(app, {"action": "update", "id": e["id"], "score": 10, "mean": None})
    (e2,) = s["manual_scores"]["courses"][CID]
    assert (e2["score"], e2["max_score"], e2["mean"], e2["sd"], e2["n"]) == (10.0, 10.0, None, 1.2, 40)   # 말한 칸만

    s = scores(app, {"action": "delete", "id": e["id"]})
    assert CID not in s["manual_scores"]["courses"] and not app.store.data.get("manual_scores")
    assert "찾을 수 없어요" in err(app, {"action": "delete", "id": e["id"]})


# ---------------------------------------------------------------- 점수 후보 → 평가 항목 → 추정

def test_candidate_links_to_component_and_reaches_estimate(app):
    s = scores(app, {"action": "add", "label": "Lab 3", "score": 9, "max_score": 10, "mean": 7, "sd": 1.5})
    lab = s["manual_scores"]["courses"][CID][0]
    c = cand(s, "manual:" + lab["id"])
    assert (c["source_type"], c["source_title"], c["course_id"]) == ("manual_score", "직접 입력", CID)
    assert (c["component"], c["link_by"]) == ("Homework assignments", "rules")       # 이름 규칙(실습 → 과제)
    assert lab["linked"] == "Homework assignments" and lab["link_by"] == "rules"

    # 평가 항목을 직접 고르면 이름 규칙보다 그것(사용자 연결) — 'Lab 4'는 규칙으로는 과제 쪽이지만 Final project로
    s = scores(app, {"action": "add", "label": "Lab 4", "score": 30, "max_score": 40, "mean": 25, "sd": 5,
                     "component": "final PROJECT"})
    lab4 = next(e for e in s["manual_scores"]["courses"][CID] if e["label"] == "Lab 4")
    assert lab4["component"] == "Final project"
    c4 = cand(s, "manual:" + lab4["id"])
    assert (c4["component"], c4["link_by"]) == ("Final project", "user")
    assert app.store.data["grade_links"][CID]["manual:" + lab4["id"]]["component"] == "Final project"

    g = course(s)
    comps = {x["name"]: x for x in g["config"]["components"]}
    assert [m["key"] for m in comps["Final project"]["members"]] == ["manual:" + lab4["id"]]
    assert [m["key"] for m in comps["Homework assignments"]["members"]] == ["manual:" + lab["id"]]
    assert comps["Final project"]["derived"]["score_pct"] == 75.0
    est = {x["name"]: x["status"] for x in g["estimate"]["components"]}
    assert est["Final project"] == "known" and est["Homework assignments"] == "known" and est["Midterm quiz"] == "missing"

    # 평가 항목을 비우면 사용자 연결을 지우고 이름 규칙으로
    s = scores(app, {"action": "update", "id": lab4["id"], "component": None})
    assert cand(s, "manual:" + lab4["id"])["link_by"] == "rules"
    assert "manual:" + lab4["id"] not in (app.store.data.get("grade_links") or {}).get(CID, {})
    # 지우면 연결도 함께 지워진다
    scores(app, {"action": "update", "id": lab4["id"], "component": "Midterm quiz"})
    scores(app, {"action": "delete", "id": lab4["id"]})
    assert "manual:" + lab4["id"] not in (app.store.data.get("grade_links") or {}).get(CID, {})


def test_same_label_later_manual_score_replaces_file_score_in_component(app):
    """같은 평가 항목·같은 이름이면 나중에 넣은 점수를 쓴다(members_of) — 직접 고친 점수가 옛 파일 점수를 대신한다."""
    r = do_import(app, "Lab3_성적.csv", csv_bytes(header="학번,이름,Lab 3"))["import_result"]
    app.store.data["pdf_texts"][r["key"]]["posted_at"] = "2026-10-01T10:00:00+09:00"   # 파일이 먼저 올라옴
    app.store.save()

    def members(st):
        return {x["name"]: x for x in course(st)["config"]["components"]}["Homework assignments"]["members"]
    (m,) = members(app.state())
    assert m["key"].startswith("pdf:" + r["key"]) and m["score"] == 9.0
    s = scores(app, {"action": "add", "label": "LAB 3", "score": 10, "max_score": 10})
    lab = s["manual_scores"]["courses"][CID][0]
    (m,) = members(s)
    assert m["key"] == "manual:" + lab["id"] and m["score"] == 10.0


# ---------------------------------------------------------------- 능력·공통 되돌리기

def test_capabilities_add_update_remove_and_undo(app):
    p = caps.plan(app, "grades.add_score", {"course": "ASIC", "label": "Lab 3", "score": 9, "max_score": 10})
    assert p["ok"] and p["tier"] == "direct" and p["undoable"] and p["params"]["course"] == CID
    assert ("직접 넣은 점수", "없음", "Lab 3 9/10 (신규)") in [(c["label"], c["before"], c["after"]) for c in p["changes"]]
    assert any("Homework assignments(자동)" in c["after"] for c in p["changes"])
    r = caps.run(app, p)
    (e,) = ms.entries(app.store, CID)
    assert e["by"] == "agent" and e["score"] == 9.0
    caps.undo(app, r["undo"])
    assert ms.entries(app.store, CID) == []

    # 평가 항목(별칭)까지: 연결 칸도 되돌린다
    p = caps.plan(app, "grades.add_score", {"course": "ASIC", "label": "보너스 퀴즈", "score": 8, "max_score": 10,
                                            "component": "midterm quiz"})
    assert p["ok"], p["error"]
    r = caps.run(app, p)
    (e,) = ms.entries(app.store, CID)
    assert e["component"] == "Midterm quiz"
    assert app.store.data["grade_links"][CID]["manual:" + e["id"]]["component"] == "Midterm quiz"
    assert cand(app.state(), "manual:" + e["id"])["link_by"] == "user"

    # 같은 이름으로 다시 넣으라고 하면 그 점수를 고친다
    p2 = caps.plan(app, "grades.add_score", {"course": "ASIC", "label": "보너스퀴즈", "score": 9, "max_score": 10})
    assert p2["ok"] and "같은 이름이라 고침" in p2["summary"]
    r2 = caps.run(app, p2)
    assert [x["score"] for x in ms.entries(app.store, CID)] == [9.0]
    assert "이미 그 값" in caps.plan(app, "grades.add_score", {"course": "ASIC", "label": "보너스 퀴즈", "score": 9,
                                                              "max_score": 10})["error"]

    p3 = caps.plan(app, "grades.update_score", {"course": "ASIC", "entry": "보너스", "mean": 6.5, "sd": 1})
    assert p3["ok"], p3["error"]
    r3 = caps.run(app, p3)
    assert ms.entries(app.store, CID)[0]["mean"] == 6.5
    caps.undo(app, r3["undo"])
    assert ms.entries(app.store, CID)[0]["mean"] is None
    caps.undo(app, r2["undo"])
    assert ms.entries(app.store, CID)[0]["score"] == 8.0

    p4 = caps.plan(app, "grades.remove_score", {"course": "ASIC", "entry": "보너스 퀴즈"})
    assert p4["ok"] and p4["changes"][0]["after"] == "지움"
    r4 = caps.run(app, p4)
    assert ms.entries(app.store, CID) == [] and "manual:" + e["id"] not in (app.store.data.get("grade_links") or {}).get(CID, {})
    caps.undo(app, r4["undo"])
    assert [x["id"] for x in ms.entries(app.store, CID)] == [e["id"]]
    assert app.store.data["grade_links"][CID]["manual:" + e["id"]]["component"] == "Midterm quiz"
    caps.undo(app, r["undo"])
    assert ms.entries(app.store, CID) == [] and "manual:" + e["id"] not in (app.store.data.get("grade_links") or {}).get(CID, {})

    # 검사 실패는 계획에서 한국어로(아무것도 바꾸지 않음)
    assert "만점" in caps.plan(app, "grades.add_score", {"course": "ASIC", "label": "Lab 9", "score": 12, "max_score": 10})["error"]
    assert "평가 항목이 없어요" in caps.plan(app, "grades.add_score", {"course": "ASIC", "label": "Lab 9", "score": 1,
                                                                    "component": "출석"})["error"]
    assert "직접 넣은 점수가 없어요" in caps.plan(app, "grades.remove_score", {"course": "ASIC", "entry": "Lab 9"})["error"]
    assert ms.entries(app.store, CID) == []


def test_routes_are_capabilities_or_excluded_with_reason():
    uncovered = caps.uncovered_routes()
    assert not [r for r in uncovered if "/scores" in r or "/import" in r], uncovered
    for shape in ("/api/grades/{course_id}/import", "/api/grades/{course_id}/import/delete"):
        assert "사용자" in caps.EXCLUDED_ROUTES[shape] or "되돌릴 수 없" in caps.EXCLUDED_ROUTES[shape]
    for cap_id in ("grades.add_score", "grades.update_score", "grades.remove_score"):
        c = caps.REGISTRY[cap_id]
        assert c.route == ("POST", "/api/grades/{course_id}/scores") and c.tier == "direct" and c.undoable


# ---------------------------------------------------------------- 성적 파일 가져오기

ROWS = [("2021123401", 7), (SID, 9), ("2021123402", 6), ("2021123403", 8), ("2021123404", 5), ("2021123405", 10)]


def csv_bytes(rows=ROWS, header="학번,이름,Lab 5") -> bytes:
    lines = [header] + [f"{sid},학생{i},{score}" for i, (sid, score) in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def pdf_bytes(rows=ROWS, title=b"Lab 5 Scores") -> bytes:
    ops = [b"BT /F1 11 Tf 72 740 Td (" + title + b") Tj ET", b"BT /F1 11 Tf 72 720 Td (ID Score) Tj ET"]
    y = 700
    for sid, score in rows:
        ops.append(b"BT /F1 11 Tf 72 %d Td (%s %d) Tj ET" % (y, sid.encode(), score))
        y -= 18
    return text_doc([b"\n".join(ops)])


def imports(state, cid=CID):
    return state["manual_scores"]["imports"].get(cid, [])


def test_import_csv_becomes_candidate_and_reaches_estimate(app, tmp_path):
    s = do_import(app, "Lab5_성적.csv", csv_bytes())
    r = s["import_result"]
    assert r["key"].startswith(f"import:{CID}:") and len(r["key"].split(":")[-1]) == 12
    assert r["status"] == "found" and r["ok"] and r["kind"] == "sheet" and "path" not in r
    (c,) = r["candidates"]
    assert (c["score"], c["n"], c["component"]) == (9.0, 6, "Homework assignments")
    rec = app.store.data["pdf_texts"][r["key"]]
    assert (rec["board_id"], rec["source"], rec["course_id"], rec["filename"]) == ("upload", "upload", CID, "Lab5_성적.csv")
    assert rec["posted_at"] and rec["kind"] == "sheet"
    saved = list((tmp_path / "imports" / CID).iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == csv_bytes() and saved[0].name.endswith("-Lab5_성적.csv")
    # 받은 첨부처럼 성적 화면 후보(가져온 파일 key)·평가 항목·추정까지
    gc = cand(s, "pdf:" + r["key"])
    assert gc["source_type"] == "pdf" and gc["component"] == "Homework assignments"
    assert {x["name"]: x["status"] for x in course(s)["estimate"]["components"]}["Homework assignments"] == "known"
    # 진단에도 보이고(학번은 나오지 않음), 같은 파일을 다시 가져오면 하나로
    rows = [x for x in s["grades"]["trace"] if str(x["id"]).startswith("f:" + r["key"])]
    assert rows and rows[0]["in_estimate"] and SID not in str(s["grades"]["trace"])
    s = do_import(app, "Lab5_성적(복사).csv", csv_bytes())
    assert [x["key"] for x in imports(s)] == [r["key"]]


def test_import_xlsx_and_text_pdf(app):
    xlsx = _xlsx([["2026-2 ASIC Lab 6"], ["학번", "이름", "Lab 6", "비고"],
                  [2021123401, "김*수", 18, None], [int(SID), "이*희", 17, None], [2021123403, "박*민", 15, None],
                  [2021123404, "최*진", 12, None], [2021123405, "정*우", 19, None]])
    r = do_import(app, "Lab6_scores.xlsx", xlsx)["import_result"]
    assert r["status"] == "found" and r["candidates"][0]["score"] == 17.0

    r = do_import(app, "Lab5_scores.pdf", pdf_bytes())["import_result"]
    assert r["status"] == "found" and r["kind"] == "pdf", r
    assert r["candidates"][0]["score"] == 9.0
    rec = app.store.data["pdf_texts"][r["key"]]
    assert "kind" not in rec and SID in rec["text"]   # 받은 PDF 기록과 같은 모양(글자만)


def test_import_status_when_not_found(tmp_path):
    app = make_app(tmp_path, sid=None)
    r = do_import(app, "Lab5_성적.csv", csv_bytes())["import_result"]
    assert r["status"] == "no_sid" and "학번" in r["reason"]
    app.store.data["settings"]["student_id"] = "2021999999"
    app.store.save()
    st = app.state()
    assert imports(st)[0]["status"] == "no_row" and "학번" in imports(st)[0]["reason"]
    r = do_import(app, "강의 안내.pdf", text_doc([b"BT /F1 11 Tf 72 740 Td (Welcome to the course) Tj ET"]))["import_result"]
    assert r["status"] == "not_table" and r["candidates"] == []
    # 글자가 없는 PDF(스캔본 같은)도 이유와 함께 남는다
    r = do_import(app, "scan.pdf", text_doc([b""]))["import_result"]
    assert r["status"] == "read_failed" and not r["ok"] and r["reason"]
    assert r["key"] not in app.store.data.get("pdf_texts", {})


def test_import_zip_members_and_delete(app, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Lab5_성적.csv", csv_bytes())
        z.writestr("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40)
    s = do_import(app, "성적 묶음.zip", buf.getvalue())
    r = s["import_result"]
    assert r["kind"] == "zip" and r["ok"] and r["status"] == "found"
    kids = [k for k, v in app.store.data["pdf_texts"].items() if v.get("parent_key") == r["key"]]
    assert kids == [r["key"] + "/Lab5_성적.csv"] and r["records"] == kids
    assert any(m["name"] == "photo.png" and not m["ok"] for m in r["members"])
    assert cand(s, "pdf:" + kids[0])["score"] == 9.0

    csv = do_import(app, "Lab5_성적.csv", csv_bytes(header="학번,이름,Lab 7"))["import_result"]
    s = actions._call(app, f"/api/grades/{CID}/import/delete", {"key": r["key"]})
    assert not [k for k in app.store.data["pdf_texts"] if k.startswith(r["key"])]
    assert [x["key"] for x in imports(s)] == [csv["key"]]
    assert not any(c["key"].startswith("pdf:" + r["key"]) for c in s["grades"]["candidates"])
    assert len(list((tmp_path / "imports" / CID).iterdir())) == 1          # 디스크의 zip도 지움
    s = actions._call(app, f"/api/grades/{CID}/import/delete", {"key": csv["key"]})
    assert not imports(s) and not app.store.data.get("score_imports") and csv["key"] not in app.store.data["pdf_texts"]
    assert list((tmp_path / "imports" / CID).iterdir()) == []
    with pytest.raises(ValueError, match="찾을 수 없어요"):
        actions._call(app, f"/api/grades/{CID}/import/delete", {"key": csv["key"]})
    with pytest.raises(ValueError, match="key"):
        actions._call(app, f"/api/grades/{CID}/import/delete", {"key": "102:cf1"})   # 받은 첨부·다른 과목 것은 못 지움


def test_import_size_and_extension_limits(app, tmp_path, monkeypatch):
    assert "형식(.png)" in import_err(app, "photo.png", b"\x89PNG....")
    assert "형식(.exe)" in import_err(app, "setup.exe", b"MZ")
    assert "확장자 없음" in import_err(app, "README", b"hello")
    assert "형식(.doc)" in import_err(app, "옛 문서.doc", b"x")
    assert "비어" in import_err(app, "a.csv", "")
    assert "빈 파일" in import_err(app, "a.csv", "data:text/csv;base64,")
    assert "base64" in import_err(app, "a.csv", "!!!이건 base64가 아님!!!")
    assert "20MB" in import_err(app, "big.pdf", b"%PDF-1.4\n" + b"0" * ms.IMPORT_MAX_BYTES)
    assert "20MB" in import_err(app, "big.pdf", "A" * (ms.IMPORT_BODY_MAX + 4))   # 풀기 전에 글자 수로
    with pytest.raises(ValueError, match="filename"):
        actions._call(app, f"/api/grades/{CID}/import", {"data": b64(csv_bytes())})
    with pytest.raises(ValueError, match="과목"):
        do_import(app, "a.csv", csv_bytes(), cid="999")
    assert not (tmp_path / "imports").exists() or not any((tmp_path / "imports").rglob("*"))
    # data URL 앞머리(FileReader.readAsDataURL)도 받는다
    s = actions._call(app, f"/api/grades/{CID}/import",
                      {"filename": "Lab5.csv", "data": "data:text/csv;base64," + b64(csv_bytes())})
    assert s["import_result"]["status"] == "found"
    # 과목마다 개수 한도
    monkeypatch.setattr(ms, "MAX_IMPORTS", 1)
    assert "1개까지" in import_err(app, "Lab7.csv", csv_bytes(header="학번,이름,Lab 7"))
    assert len(imports(app.state())) == 1
    # 서버가 경로의 본문 한도로 읽는 값(20MB 파일의 base64 + 여유)
    from lms_agent.features import manualscores as fm
    assert fm.import_file.max_body == ms.IMPORT_BODY_MAX > ms.IMPORT_MAX_BYTES * 4 // 3


@pytest.mark.parametrize("name", ["../../evil.csv", "..\\..\\evil.csv", "/etc/evil.csv", "C:\\Windows\\evil.csv",
                                  "sub/dir/evil.csv", "evil\x00.csv", "CON.csv", "  ..  .csv", 'ev<i>l:"|?*.csv'])
def test_import_names_cannot_leave_the_imports_folder(app, tmp_path, name):
    r = do_import(app, name, csv_bytes())["import_result"]
    root = (tmp_path / "imports").resolve()
    (saved,) = [p for p in root.rglob("*") if p.is_file()]
    assert saved.resolve().parent == root / CID
    fn = r["filename"]
    assert "/" not in fn and "\\" not in fn and ".." not in fn.replace("...", "") and fn.endswith(".csv") and "\x00" not in fn
    assert not any(ch in fn for ch in '<>:"|?*')
    assert not fn.split(".")[0].strip().upper() == "CON"
    assert app.store.data["pdf_texts"][r["key"]]["filename"] == fn


def test_import_course_id_is_checked(app):
    for bad in ("../101", "101/../../x", ""):
        with pytest.raises(ValueError, match="과목"):
            ms.prepare_import(app.store, app.home, bad, {"filename": "a.csv", "data": b64(csv_bytes())})


def test_reread_imports_reads_saved_file_again(app):
    r = do_import(app, "Lab5_성적.csv", csv_bytes())["import_result"]
    app.store.data["pdf_texts"].pop(r["key"])            # 기록이 빠진 경우(옛 판으로 읽음 등)
    assert ms.reread_imports(app.store, app.home) == 1
    assert app.store.data["pdf_texts"][r["key"]]["source"] == "upload"
    assert ms.reread_imports(app.store, app.home, keys={"import:101:nope"}) == 0


def test_imports_show_in_grade_trace_without_student_id(app):
    do_import(app, "Lab5_성적.csv", csv_bytes())
    rows = gradetrace.trace(app.store)
    mine = [r for r in rows if str(r["id"]).startswith("f:import:")]
    assert mine and mine[0]["course_id"] == CID and mine[0]["file"] == "Lab5_성적.csv"
    assert SID not in gradetrace.report_text(app.store, show_scores=True)


def test_state_section_survives_broken_records(app):
    app.store.data["manual_scores"] = {CID: [{"id": "ms-x", "label": "Lab 1", "score": 5}, "garbage", {"no": "id"}],
                                       "zzz": "garbage"}
    app.store.data["score_imports"] = {CID: [{"key": "import:101:aaaaaaaaaaaa", "filename": "x.csv", "ok": False}], "9": None}
    st = app.state()
    assert [e["id"] for e in st["manual_scores"]["courses"][CID]] == ["ms-x"]
    assert st["manual_scores"]["imports"][CID][0]["status"] == "read_failed"
    with pytest.raises((ValueError, ApiError)):
        scores(app, {"action": "delete", "id": "garbage"})
    assert os.path.basename(__file__) == "test_manual_scores.py"


def test_server_allows_large_body_only_for_import_route(tmp_path):
    """서버 본문 한도(리드, 계약 29-1): 성적 파일 가져오기 경로만 max_body(20MB급)까지, 다른 경로는 그대로 200KB."""
    import base64
    import json as _json
    import urllib.error
    import urllib.request
    from lms_agent.server import App, serve_in_thread, start_server
    from lms_agent.store import Store
    s = Store(tmp_path / "state.json")
    s.data["cache"] = {"courses": [{"id": "1", "name": "테스트과목"}], "assignments": [], "announcements": [],
                       "conversations": [], "boards": [], "errors": []}
    s.save()
    app = App(s, tmp_path, cfg={})
    httpd = start_server(app, 0)
    serve_in_thread(httpd)

    def post(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=_json.dumps(body).encode(), method="POST",
                                     headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
        try:
            return 200, _json.loads(urllib.request.urlopen(req, timeout=20).read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read())
    try:
        rows = "학번,점수\n" + "".join(f"20990{i:05d},{i % 100}\n" for i in range(20000))   # 약 300KB CSV
        data = base64.b64encode(rows.encode("utf-8")).decode()
        assert len(data) > 300_000
        code, out = post("/api/grades/1/import", {"filename": "큰점수표.csv", "data": data})
        assert not (code == 400 and "너무 큽니다" in str(out.get("error"))), out
        code, out = post("/api/settings", {"notify": True, "pad": "x" * 300_000})
        assert code == 400 and "너무 큽니다" in out["error"]
    finally:
        httpd.shutdown()
