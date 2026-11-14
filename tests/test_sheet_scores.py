"""엑셀(xlsx)·CSV 점수표도 성적 후보로(계약 25-2, 베타테스터 제보 2026-11-14: "'디지털통신' 과목은 엑셀로 파일 첨부하였는데
성적 반영이 안 된 것 같다"). 첨부 받기(materials)가 표 파일을 sheets로 읽어 pdf_texts에 표 기록을 두고, 받아 둔 표 파일은
다음 동기화에서 디스크에서 다시 읽고, scorefile.parse_sheet_results가 PDF와 같은 규칙으로 내 점수·반 집계를 만든다.
학번·이름·점수는 모두 지어낸 것(가짜). 네트워크 없음(가짜 Canvas는 127.0.0.1)."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

import json
import os
import statistics
import struct
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from lms_agent import core, gradebook, materials, scorefile, sheets
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import (DEMO_STUDENT_ID, DEMO_TOKEN, SHEET_FILE, score_table_xlsx, start_mock_canvas,
                                   tiny_xlsx)
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import parse_iso

SID = "2099000002"


def rows_of(*rows):
    """[값…] 행 → sheets.read 모양({열 번호: 값}, 빈 칸 없음)."""
    return [{i: v for i, v in enumerate(r) if v is not None} for r in rows]


def sheet(name, *rows):
    return {"name": name, "rows": rows_of(*rows)}


def results(shs, sid=SID, filename="성적.xlsx"):
    return scorefile.parse_sheet_results(shs, sid, filename)


# ---------------------------------------------------------------- 표 → 점수(scorefile.parse_sheet_results)

QUIZ = [["2026-2 디지털통신 Quiz 2 성적"], ["담당: 가짜 교수", "조교: 가짜 조교"],      # 제목 줄(글자 칸 2개도) 위에
        ["번호", "학번", "이름", "문항1 (4)", "문항2 (6)", "합계 (10)"],
        [1, 2099000001, "김*민", 3, 5, 8], [2, 2099000002, "이*수", 4, 5, 9], [3, 2099000003, "박*진", 2, 6, 8],
        [4, 2099000004, "최*아", "결시", None, None], [5, 2099000005, "정*우", 1, 3, 4],
        ["평균", None, None, 2.5, 4.75, 7.25], ["표준편차", None, None, 1.29, 1.26, 2.22]]


def test_xlsx_numeric_ids_title_rows_parts_with_total():
    data = tiny_xlsx([("Sheet1", QUIZ)])
    assert sheets.kind_of(data, "Quiz2_성적.xlsx") == "xlsx"
    shs = sheets.read(data, "Quiz2_성적.xlsx")
    assert isinstance(shs[0]["rows"][3][1], float)                     # 숫자 칸에 저장된 학번(2099000001.0)
    (r,) = scorefile.parse_sheet_results(shs, SID, "Quiz2_성적.xlsx")
    assert (r.label, r.column, r.key) == ("Quiz 2", "합계", None)         # 제목 줄의 평가 이름, 합계 열이 대표
    assert (r.score, r.max_score, r.n, r.rank, r.high, r.low) == (9, 10, 4, 1, 9, 4)
    assert r.mean == pytest.approx(statistics.fmean([8, 9, 8, 4]), abs=1e-3)
    assert r.sd == pytest.approx(statistics.stdev([8, 9, 8, 4]), abs=1e-3)
    cols = {c["name"]: c for c in r.columns}
    assert list(cols) == ["문항1", "문항2", "합계"]                       # 번호·학번·이름은 통계에 없음
    assert (cols["문항1"]["max_score"], cols["문항2"]["max_score"], cols["문항1"]["n"]) == (4, 6, 4)   # 결시 뺌
    assert any("요약 행" in n for n in r.notes) and any("결시" in n for n in r.notes)
    assert any("표본표준편차" in n for n in r.notes)
    dumped = json.dumps(asdict(r), ensure_ascii=False)
    for other in ("2099000001", "2099000003", "김*민", "이*수", "박*진", SID):
        assert other not in dumped                                       # 다른 학생·내 학번은 결과에 없음


def test_csv_separate_assessments_one_result_per_column():
    text = ("순번,학번,이름,Quiz 1 (10),Midterm (100)\n1,2099000001,김*민,0,45\n2,2099000002,이*수,7.5,51\n"
            "3,2099000003,박*진,9,80\n4,2099000004,최*아,10,72\n5,2099000005,정*우,6,-\n")
    data = text.encode("utf-8-sig")
    assert sheets.kind_of(data, "성적.csv") == "csv"
    rs = results(sheets.read(data, "성적.csv"), filename="성적.csv")
    assert [(r.label, r.key, r.score, r.max_score, r.n) for r in rs] == [("Quiz 1", "Quiz 1", 7.5, 10, 5),
                                                                          ("Midterm", "Midterm", 51, 100, 4)]
    assert rs[0].rank == 3 and rs[1].rank == 3 and any("열마다 따로" in n for n in rs[0].notes)   # '-' 칸은 뺌
    # CP949(EUC-KR)로 저장한 CSV도 같은 결과
    rs2 = results(sheets.read(text.encode("cp949"), "성적.csv"), filename="성적.csv")
    assert [(r.label, r.score) for r in rs2] == [("Quiz 1", 7.5), ("Midterm", 51)]


def test_masked_ids_unique_or_not_chosen():
    shs = [sheet("성적", ["학번", "이름", "점수"], ["2099****01", "김*민", 80], ["2099****02", "이*수", 60],
                 ["2099****03", "박*진", 70])]
    (r,) = results(shs)
    assert (r.score, r.n, r.rank) == (60, 3, 3) and any("가린 학번" in n for n in r.notes)
    same = [sheet("성적", ["학번", "점수"], ["2099****02", 80], ["2099****02", 60], ["2099****12", 70])]
    assert results(same) == []                                            # 여러 행과 맞으면 본인을 고르지 않는다
    assert results(shs, sid="2099999999") == []                           # 내 행이 없으면 결과 없음


def test_multiple_sheets_are_separate_assessments():
    rows = lambda base: [["학번", "이름", "점수"]] + [[2099000000 + i, f"학생{i}", base + i] for i in range(1, 5)]
    data = tiny_xlsx([("Quiz1", rows(60)), ("Quiz2", rows(70)), ("안내", [["이 파일은 퀴즈 성적입니다"]])])
    rs = results(sheets.read(data, "퀴즈_성적.xlsx"), filename="퀴즈_성적.xlsx")
    assert [(r.label, r.key, r.score, r.n) for r in rs] == [("Quiz1", "Quiz1", 62, 4), ("Quiz2", "Quiz2", 72, 4)]
    assert all(any("점수표가 2개" in n for n in r.notes) for r in rs)      # 점수표가 아닌 '안내' 시트는 세지 않음
    # 시트마다 열마다 다른 평가면 key에 시트 이름이 붙는다
    sep = lambda: [["학번", "Quiz 1 (10)", "Midterm (100)"]] + [[2099000000 + i, i, 50 + i] for i in range(1, 4)]
    rs = results([sheet("1반", *sep()), sheet("2반", *sep())])
    assert [r.key for r in rs] == ["1반 Quiz 1", "1반 Midterm", "2반 Quiz 1", "2반 Midterm"]


def test_stacked_tables_in_one_sheet_are_not_mixed():
    """한 시트에 표가 위아래로 둘('Lab0 Scores' 표 + 'Lab1 Scores' 표, 머리글이 같아도) → 표마다. 되풀이된 머리글(인쇄용)은 같은 표."""
    block = lambda base: [["학번", "Prelab (20)", "Report (30)", "Total (50)"]] + [
        [2099000000 + i, base + i, 20 + i, base + 20 + 2 * i] for i in range(1, 4)] + [["평균", None, None, None]]
    shs = [sheet("성적", ["Lab0 Scores"], *block(10), ["Lab1 Scores"], *block(15))]
    rs = results(shs, filename="Lab_score.xlsx")
    assert [(r.label, r.key, r.score, r.n) for r in rs] == [("Lab0", "Lab0", 34, 3), ("Lab1", "Lab1", 39, 3)]
    head = ["학번", "점수"]
    shs = [sheet("성적", head, [2099000001, 80], [2099000002, 70], head, [2099000003, 60], [2099000004, 90])]
    (r,) = results(shs, filename="quiz_score.xlsx")
    assert (r.score, r.n, r.rank) == (70, 4, 3)


@pytest.mark.parametrize("head,maxes", [
    (["학번", "Prelab (20)", "Report (30)", "Total (50)"], [20, 30, 50]),
    (["학번", "Prelab/20", "Report/30", "Total/50"], [20, 30, 50]),
    (["학번", "예비보고서 20점", "결과보고서 30점", "총점 50점"], [20, 30, 50]),
    (["학번", "Prelab (max 20)", "Report (20점 만점)", "Total [50]"], [20, 20, 50]),
])
def test_max_score_from_header(head, maxes):
    shs = [sheet("S", head, [2099000001, 18, 25, 43], [2099000002, 15, 20, 35], [2099000003, 20, 30, 50])]
    (r,) = results(shs, filename="Lab2_score.xlsx")
    assert [c["max_score"] for c in r.columns] == maxes
    assert (r.label, r.score, r.max_score, r.n) == ("Lab2", 35, 50, 3)


def test_max_row_and_rank_percent_columns_excluded():
    shs = [sheet("S", ["학번", "Prelab", "Report", "Total", "등수", "백분율"], ["만점", 20, 30, 50],
                 [2099000001, 18, 25, 43, 2, "86%"], [2099000002, 15, 20, 35, 3, "70%"], [2099000003, 20, 30, 50, 1, "100%"])]
    (r,) = results(shs, filename="Lab2_score.xlsx")
    assert [(c["name"], c["max_score"]) for c in r.columns] == [("Prelab", 20), ("Report", 30), ("Total", 50)]
    assert (r.column, r.score, r.rank, r.n) == ("Total", 35, 3, 3)          # 만점 행은 학생이 아니다
    assert any("등수" in n for n in r.notes)


def test_title_with_global_max_and_no_header_names():
    shs = [sheet("S", ["중간고사 성적 (만점 50점)"], [2099000001, 40], [2099000002, 30], [2099000003, 45])]
    (r,) = results(shs, filename="score.xlsx")
    assert (r.column, r.score, r.max_score, r.n) == ("열 B", 30, 50, 3) and any("머리글" in n for n in r.notes)


def test_json_round_trip_keys_are_strings():
    """state.json을 거치면 열 번호가 글자("0")가 된다 — 같은 결과."""
    shs = sheets.read(tiny_xlsx([("Sheet1", QUIZ)]), "Quiz2_성적.xlsx")
    back = json.loads(json.dumps(shs))
    assert [asdict(r) for r in results(back)] == [asdict(r) for r in results(shs)]


# ---------------------------------------------------------------- 점수표가 아닌 표는 조용히

SCHEDULE = [sheet("일정", ["주차", "날짜", "내용"], *[[i, 45900 + 7 * i, f"{i}주차 강의"] for i in range(1, 9)])]
ROSTER = [sheet("명단", ["학번", "이름", "학과", "학년"],
                *[[2099000000 + i, f"학생{i}", "전자공학부", 1 + i % 4] for i in range(1, 9)])]
ATTEND_OX = [sheet("출석", ["학번", "이름", "1주", "2주", "3주"],
                   *[[2099000000 + i, f"학생{i}", "O", "O" if i % 2 else "X", "O"] for i in range(1, 9)])]
ATTEND_01 = [sheet("출석", ["학번", "이름", "1주", "2주", "3주"],
                   *[[2099000000 + i, f"학생{i}", 1, i % 2, 1] for i in range(1, 9)])]
SCORES = [sheet("S", ["학번", "이름", "점수"], *[[2099000000 + i, f"학생{i}", 50 + 5 * i] for i in range(1, 9)])]


@pytest.mark.parametrize("shs,fn", [(SCHEDULE, "강의일정.xlsx"), (ROSTER, "수강생명단.xlsx"), (ATTEND_OX, "출석부.xlsx"),
                                    (ATTEND_01, "출석부.xlsx"), (SCHEDULE, "성적처리일정.xlsx")])
def test_ordinary_spreadsheets_are_not_score_files(shs, fn):
    assert not scorefile.is_score_sheet(shs, fn)
    assert scorefile.is_score_sheet(SCORES, "list.xlsx")                     # 모양만으로도(학번 + 점수 8행)
    two = [sheet("S", ["학번", "점수"], [2099000001, 1], [2099000002, 2])]
    assert scorefile.is_score_sheet(two, "quiz_score.xlsx") and not scorefile.is_score_sheet(two, "list.xlsx")
    assert results(ROSTER, filename="수강생명단.xlsx") == []                  # 학년은 점수가 아니다


def _store(tmp_path, recs: dict, sid=SID):
    s = Store(tmp_path / "state.json")
    s.data["cache"] = {"courses": [{"id": "77", "name": "디지털통신"}], "assignments": [], "announcements": [],
                       "conversations": []}
    s.data["pdf_texts"] = recs
    if sid:
        s.data["settings"]["student_id"] = sid
    return s


def _rec(shs, filename):
    return {"text": sheets.to_text(shs), "sheets": json.loads(json.dumps(shs)), "kind": "sheet", "sheet_kind": "xlsx",
            "sheet_version": materials.SHEET_VERSION, "course_id": "77", "filename": filename,
            "post_key": "board:announcements-5", "board_id": "announcements", "post": 5, "canvas_file_id": 5001,
            "posted_at": "2026-11-10T10:00:00+09:00"}


def test_gradebook_candidates_and_waiting_counts(tmp_path):
    recs = {"77:cf1": _rec([sheet("Sheet1", *QUIZ)], "Quiz2_성적.xlsx"),
            "77:cf2": _rec(ROSTER, "수강생명단.xlsx"), "77:cf3": _rec(ATTEND_01, "출석부.xlsx"),
            "77:cf4": _rec(SCHEDULE, "강의일정.xlsx")}
    s = _store(tmp_path, recs)
    cands = [c for c in gradebook.candidates(s) if c["source_type"] == "pdf"]
    assert [(c["key"], c["file_kind"], c["label"], c["score"], c["max_score"], c["source_title"]) for c in cands] == [
        ("pdf:77:cf1", "sheet", "Quiz 2", 9, 10, "Quiz2_성적.xlsx")]                # 명단·출석부·일정은 후보 없음
    assert parse_iso(cands[0]["posted_at"]) == parse_iso("2026-11-10T10:00:00+09:00")
    assert "2099000001" not in json.dumps(cands, ensure_ascii=False)
    assert gradebook.score_files(s) == (0, [])
    s.data["settings"]["student_id"] = "2099999999"                            # 내 줄 없음 → 학번 확인 안내(점수표만)
    assert gradebook.score_files(s) == (0, ["Quiz2_성적.xlsx"])
    s.data["settings"].pop("student_id")
    assert gradebook.score_files(s) == (1, [])                                  # 학번을 기다리는 점수표 1개
    # 점수표 글(반 전체 학번·점수)은 AI 비중·학점 찾기 글로 고르지 않는다
    srcs = [{"type": "pdf", "title": "첨부 'Quiz2_성적.xlsx'", "text": recs["77:cf1"]["text"] + "\n성적 반영 비율 30%"},
            {"type": "announcement", "title": "안내", "text": "성적은 중간 30% 기말 40%"}]
    assert gradebook._ai_target(srcs)["type"] == "announcement"


# ---------------------------------------------------------------- 받기(materials.fetch)·다시 읽기

def att(i, filename, data):
    return {"id": i, "filename": filename, "filesize": len(data), "canvas_file_id": 7000 + i,
            "url": f"https://lms.example/files/{7000 + i}/download?verifier=SECRET"}


def board(atts, course_id="77"):
    return {"course_id": course_id, "tool_id": None, "board_id": "announcements", "board_title": "공지", "source": "canvas",
            "posts": [{"id": 5, "title": "Quiz 2 결과", "content": "", "created_at": "2026-11-10T01:00:00Z",
                       "updated_at": "2026-11-10T01:00:00Z", "attachments": atts}]}


def fake_get(contents):
    calls = []

    def get(a):
        calls.append(a["id"])
        return contents[a["id"]]
    get.calls = calls
    return get


XLSX = tiny_xlsx([("Sheet1", QUIZ)])


def test_fetch_creates_sheet_records(tmp_path):
    bs = [board([att(1, "Quiz2_성적.xlsx", XLSX), att(2, "broken.xlsx", b"PK\x03\x04broken"),
                 att(3, "template.docx", b"PK-zip-not-a-sheet"), att(4, "list.csv", "학번,점수\n1,2\n".encode())])]
    jobs = materials.plan(bs, {}, {"77": "디지털통신"}, tmp_path)
    now = datetime(2026, 11, 14, 9, 0).astimezone()
    updates, texts, errors = materials.fetch(jobs, fake_get({1: XLSX, 2: b"PK\x03\x04broken", 3: b"PK-zip-not-a-sheet",
                                                             4: "학번,점수\n1,2\n".encode()}), now=now)
    assert errors == [] and sorted(updates) == ["77:1", "77:2", "77:3", "77:4"]
    m = updates["77:1"]
    assert (m["sheet_ok"], m["sheet_kind"], m["sheet_version"], m["pdf_ok"]) == (True, "xlsx", materials.SHEET_VERSION, None)
    assert parse_iso(m["sheet_read_at"]) == parse_iso(m["downloaded_at"])
    t = texts["77:1"]
    assert (t["kind"], t["sheet_kind"], t["filename"], t["course_id"], t["board_id"], t["post"], t["canvas_file_id"]) == \
        ("sheet", "xlsx", "Quiz2_성적.xlsx", "77", "announcements", 5, 7001)
    assert t["post_key"] == "board:announcements-5" and parse_iso(t["posted_at"]) == parse_iso("2026-11-10T01:00:00Z")
    assert t["text"].startswith("[Sheet1]") and "합계 (10)" in t["text"]
    assert t["sheets"][0]["name"] == "Sheet1" and all(isinstance(k, str) for r in t["sheets"][0]["rows"] for k in r)
    bad = updates["77:2"]
    assert bad["sheet_ok"] is False and bad["sheet_notes"] and "77:2" not in texts   # 못 읽은 이유만(내용 없음)
    assert "sheet_ok" not in updates["77:3"]                                    # 표 파일이 아니면 그대로
    assert updates["77:4"]["sheet_ok"] is True and texts["77:4"]["sheet_kind"] == "csv"
    assert sorted(texts) == ["77:1", "77:4"]
    # 받아 둔 파일·기록이 최신이면 다음 계획에 다시 읽기가 없다
    data = {"downloads": updates, "pdf_texts": texts}
    assert materials.plan(bs, data["downloads"], {"77": "디지털통신"}, tmp_path, texts=data["pdf_texts"]) == []


# ---- 옛 엑셀(.xls, BIFF8): 작은 OLE2 복합 문서를 코드로 만든다(LABEL·NUMBER 칸만, 'Workbook' 스트림 4096바이트 이상)

def _biff(rt, body=b""):
    return struct.pack("<HH", rt, len(body)) + body


def _bof(dt):
    return _biff(0x0809, struct.pack("<HHHHII", 0x0600, dt, 0x0DBB, 0x07CC, 0x41, 0x06))


def _xl_str(s):
    wide = any(ord(ch) > 255 for ch in s)
    return struct.pack("<HB", len(s), int(wide)) + (s.encode("utf-16-le") if wide else s.encode("latin-1"))


def tiny_xls(name, rows) -> bytes:
    cells = b""
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if isinstance(v, str):
                cells += _biff(0x0204, struct.pack("<HHH", r, c, 15) + _xl_str(v))           # LABEL
            elif v is not None:
                cells += _biff(0x0203, struct.pack("<HHHd", r, c, 15, float(v)))              # NUMBER
    sheet_body = _bof(0x0010) + cells + _biff(0x000A)

    def globals_(off):
        sheet_name = struct.pack("<BB", len(name), 1) + name.encode("utf-16-le")
        return (_bof(0x0005) + _biff(0x0042, struct.pack("<H", 1200))
                + _biff(0x0085, struct.pack("<IBB", off, 0, 0) + sheet_name) + _biff(0x000A))
    wb = globals_(len(globals_(0))) + sheet_body
    wb += b"\0" * max(0, 4096 - len(wb))                       # 엑셀처럼 4096바이트 이상(작은 스트림 표를 쓰지 않게)
    ss, ENDC, FREE, FATSECT, NONE = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
    n = -(-len(wb) // ss)
    secs = [wb[k * ss:(k + 1) * ss].ljust(ss, b"\0") for k in range(n)]
    fat = [k + 1 if k < n - 1 else ENDC for k in range(n)]

    def dirent(nm, typ, start, size, child=NONE):
        raw = nm.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw), typ, 1) + struct.pack("<III", NONE, NONE, child)
                + b"\0" * 36 + struct.pack("<IQ", start, size))
    secs.append((dirent("Root Entry", 5, ENDC, 0, child=1) + dirent("Workbook", 2, 0, len(wb))).ljust(ss, b"\0"))
    dir_at = len(secs) - 1
    fat += [ENDC, FATSECT]
    fat_at = len(secs)
    fat += [FREE] * (ss // 4 - len(fat))
    secs.append(struct.pack(f"<{ss // 4}I", *fat))
    hdr = bytearray(512)
    hdr[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<5H", hdr, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<8I", hdr, 0x2C, 1, dir_at, 0, 4096, ENDC, 0, ENDC, 0)
    struct.pack_into("<109I", hdr, 0x4C, fat_at, *([FREE] * 108))
    return bytes(hdr) + b"".join(secs)


def test_xls_end_to_end_and_html_disguised_xls(tmp_path):
    xls = tiny_xls("성적", QUIZ)
    assert sheets.kind_of(xls, "Quiz2_성적.xls") == "xls"
    html = b"<html><body><table><tr><td>\xed\x95\x99\xeb\xb2\x88</td></tr></table></body></html>"
    bs = [board([att(1, "Quiz2_성적.xls", xls), att(2, "portal.xls", html)])]
    jobs = materials.plan(bs, {}, {"77": "디지털통신"}, tmp_path)
    updates, texts, errors = materials.fetch(jobs, fake_get({1: xls, 2: html}))
    assert errors == [] and updates["77:1"]["sheet_kind"] == "xls" and texts["77:1"]["sheet_kind"] == "xls"
    s = _store(tmp_path, json.loads(json.dumps(texts)))                        # state.json을 거친 모양으로
    (c,) = [c for c in gradebook.candidates(s) if c["source_type"] == "pdf"]
    assert (c["key"], c["file_kind"], c["label"], c["score"], c["max_score"], c["n"]) == ("pdf:77:1", "sheet", "Quiz 2", 9, 10, 4)
    # 엑셀 이름이지만 웹 페이지(HTML) 표 → 읽지 않고 그 이유(sheets의 한국어 문구)를 sheet_notes에
    bad = updates["77:2"]
    assert bad["sheet_ok"] is False and "77:2" not in texts
    assert bad["sheet_notes"] == [sheets.E_HTML]


def _old_entry(path: Path, key="77:1", filename="Quiz2_성적.xlsx", posted="2026-11-10T10:00:00+09:00"):
    """v0.7.2까지 받아 둔 기록 모양(표 칸 없음)."""
    return {"path": str(path), "filename": filename, "size": path.stat().st_size, "course_id": "77",
            "course_short": "디지털통신", "board_title": "공지", "post_title": "Quiz 2 결과",
            "post_key": "board:announcements-5", "board_id": "announcements", "post": 5, "canvas_file_id": 7001,
            "posted_at": posted, "downloaded_at": "2026-11-11T09:00:00+09:00", "pdf_ok": None, "pdf_notes": []}


def _put(folder: Path, name: str, data: bytes) -> Path:
    p = folder / "디지털통신" / "공지" / "Quiz 2 결과" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_backfill_reads_already_downloaded_xlsx_without_downloading(tmp_path):
    folder = tmp_path / "dl"
    p = _put(folder, "Quiz2_성적.xlsx", XLSX)
    entry = _old_entry(p)
    data = {"cache": {"courses": [{"id": "77", "name": "디지털통신"}],
                      "boards": [board([att(1, "Quiz2_성적.xlsx", XLSX)])]},
            "downloads": {"77:1": entry}, "pdf_texts": {}, "settings": {}}
    get = fake_get({})
    report = materials.run(data, folder, get)
    assert get.calls == []                                                      # 다시 받지 않는다
    assert report == {"planned": 0, "downloaded": 0, "pdf_texts": 1, "errors": [], "reread": 1}
    m = data["downloads"]["77:1"]
    assert m["sheet_ok"] is True and m["downloaded_at"] == entry["downloaded_at"] and m["path"] == str(p)
    rec = data["pdf_texts"]["77:1"]
    assert rec["kind"] == "sheet" and rec["filename"] == "Quiz2_성적.xlsx" and rec["post_key"] == "board:announcements-5"
    s = _store(tmp_path, data["pdf_texts"])
    (c,) = [c for c in gradebook.candidates(s) if c["source_type"] == "pdf"]
    assert (c["key"], c["score"], c["file_kind"]) == ("pdf:77:1", 9, "sheet")
    # 두 번째는 할 일이 없다
    assert materials.run(data, folder, get) == {"planned": 0, "downloaded": 0, "pdf_texts": 0, "errors": []}
    # 읽기 판이 오르면 다시 읽는다
    data["downloads"]["77:1"]["sheet_version"] = 0
    assert materials.run(data, folder, get)["reread"] == 1
    # 표 기록이 지워졌으면(읽었다는 기록만 남음) 다시 읽는다
    data["pdf_texts"].clear()
    assert materials.run(data, folder, get)["reread"] == 1 and "77:1" in data["pdf_texts"]


def test_backfill_respects_folder_size_and_count_limits(tmp_path, monkeypatch):
    folder, outside = tmp_path / "dl", tmp_path / "elsewhere"
    inside_file = _put(folder, "Quiz2_성적.xlsx", XLSX)
    out_file = _put(outside, "Quiz3_성적.xlsx", XLSX)
    gone = folder / "없음.xlsx"
    downloads = {"77:1": _old_entry(inside_file), "77:2": _old_entry(out_file, "77:2", "Quiz3_성적.xlsx"),
                 "77:3": {**_old_entry(inside_file, "77:3"), "path": str(gone)},
                 "77:4": {**_old_entry(inside_file, "77:4", "Lab3.pdf"), "path": str(inside_file)}}
    jobs = materials.reread_jobs(downloads, folder)
    assert [j["key"] for j in jobs] == ["77:1"]                               # 폴더 밖·없는 파일·PDF는 아님
    monkeypatch.setattr(materials, "MAX_BYTES", len(XLSX) - 1)
    assert materials.reread_jobs(downloads, folder) == []                     # 크기 한도
    monkeypatch.setattr(materials, "MAX_BYTES", 50_000_000)
    # 폴더 안 파일이 밖을 가리키는 링크면 읽지 않는다(만들 수 있을 때만)
    link = folder / "링크.xlsx"
    try:
        os.symlink(out_file, link)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        assert materials.reread_jobs({"77:9": {**_old_entry(inside_file, "77:9"), "path": str(link)}}, folder) == []
    # 한 번에 SHEET_REREAD_MAX개 — 최근 글의 파일부터
    many = {}
    for i in range(materials.SHEET_REREAD_MAX + 5):
        f = _put(folder, f"Quiz{i}_성적.xlsx", XLSX)
        many[f"77:{100 + i}"] = _old_entry(f, f"77:{100 + i}", f.name, posted=f"2026-10-{1 + i:02d}T10:00:00+09:00")
    data = {"cache": {"courses": [], "boards": []}, "downloads": many, "pdf_texts": {}, "settings": {}}
    r1 = materials.run(data, folder, fake_get({}))
    assert r1["reread"] == materials.SHEET_REREAD_MAX
    assert "77:124" in data["pdf_texts"] and "77:100" not in data["pdf_texts"]   # 가장 최근 것부터
    assert materials.run(data, folder, fake_get({}))["reread"] == 5
    assert "reread" not in materials.run(data, folder, fake_get({}))


def test_rename_only_spreadsheet_is_not_retried(tmp_path):
    """이름만 .xlsx인 파일(내용이 표가 아님)은 한 번 보고 '읽지 않음'으로 남긴다 — 동기화마다 다시 읽지 않게."""
    folder = tmp_path / "dl"
    p = _put(folder, "notes.xlsx", b"%%just text, not a workbook%%")
    data = {"cache": {"courses": [], "boards": []}, "downloads": {"77:1": _old_entry(p, filename="notes.xlsx")},
            "pdf_texts": {}, "settings": {}}
    assert materials.run(data, folder, fake_get({}))["reread"] == 1
    m = data["downloads"]["77:1"]
    assert m["sheet_ok"] is False and m["sheet_notes"] and data["pdf_texts"] == {}
    assert "reread" not in materials.run(data, folder, fake_get({}))


# ---------------------------------------------------------------- 동기화·화면(가짜 Canvas 데모)

class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


KEY = f"102:cf{SHEET_FILE}"


def test_demo_sync_shows_excel_candidate_and_backfills_old_state(tmp_path):
    """데모(lms_agent demo와 같은 길): '디지털신호처리' 공지의 Quiz2_성적.xlsx → 성적 후보(데모 학번).
    v0.7.2처럼 받아만 두고 표 기록이 없는 상태에서도 다음 동기화가 디스크에서 읽어 후보가 생긴다(다시 받지 않음)."""
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), s, **core.sync_options(s, demo_home=tmp_path))   # 학번 없이 먼저
        m = s.data["downloads"][KEY]
        assert m["filename"] == "Quiz2_성적.xlsx" and m["sheet_ok"] is True and Path(m["path"]).read_bytes() == score_table_xlsx()
        rec = s.data["pdf_texts"][KEY]
        assert rec["kind"] == "sheet" and (rec["board_id"], rec["post"]) == ("announcements", 9010)
        app = App(s, tmp_path, cfg={}, scheduler=_Sched())
        assert app.state()["grades"]["score_files_waiting"] == 3                 # PDF 2 + 엑셀 1
        s.data["settings"]["student_id"] = DEMO_STUDENT_ID
        st = app.state()
        (c,) = [c for c in st["grades"]["candidates"] if c["key"] == f"pdf:{KEY}"]
        assert (c["course_id"], c["course_short"], c["source_type"], c["file_kind"]) == ("102", "디지털신호처리", "pdf", "sheet")
        assert (c["label"], c["score"], c["max_score"], c["n"], c["rank"]) == ("Quiz 2", 9, 10, 7, 2)   # 제목 줄의 이름
        assert c["source_title"] == "Quiz2_성적.xlsx" and "2026000001" not in json.dumps(c, ensure_ascii=False)
        assert st["grades"]["score_files_waiting"] == 0 and st["grades"]["score_files_unmatched"] == []
        # 점수표(반 전체 학번)는 에이전트 검색 글에서 빠진다
        from lms_agent import agent
        assert not [x for x in agent.sources(s) if x["key"] == f"pdf:{KEY}"]

        # v0.7.2 상태 흉내: 받은 기록에서 표 칸을 지우고 표 기록도 없앤다 → 다음 동기화가 디스크에서 읽는다
        for k in ("sheet_ok", "sheet_notes", "sheet_kind", "sheet_version", "sheet_reader", "sheet_read_at"):
            s.data["downloads"][KEY].pop(k, None)
        s.data["pdf_texts"].pop(KEY)
        s.save()
        mock.requests.clear()
        core.sync(Canvas(base, DEMO_TOKEN), s, **core.sync_options(s, demo_home=tmp_path))
        assert not [p for p in mock.requests if p.startswith(("/storage/", "/files/"))]   # 다시 받지 않았다
        assert s.data["pdf_texts"][KEY]["kind"] == "sheet" and s.data["downloads"][KEY]["sheet_ok"] is True
        disk = Store(tmp_path / "state.json")                                     # 저장된 파일에서(열 번호가 글자)
        assert [(c["label"], c["score"]) for c in gradebook.candidates(disk) if c["key"] == f"pdf:{KEY}"] == [("Quiz 2", 9)]
    finally:
        mock.shutdown()
