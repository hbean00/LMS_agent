"""실제 성적표 양식을 지어낸 값으로 옮긴 회귀 시험(사용자 제보 2026-11-18: "이런 성적표도 지금 로직으로 조회되나요 — 테스트 케이스에 반영").

양식 A — '<과목 코드> Final / 채점 결과 및 통계'(사용자가 받은 기말 채점결과·재검 결과 PDF와 같은 모양, PDF에서 뽑은 글 그대로의 줄 배치):
  점수 분포 막대 그래프의 숫자(한 줄에 둘씩 붙기도 함) → 구간 이름 '[0, 10] (10, 20] …' → '평균값 중간값 표준편차' 머리글과 값 한 줄 →
  '학번 점수 학번 점수' 두 칸 표가 두 덩어리(쪽이 바뀌며 머리글 반복) · 소수 점수(61.5) · 0점 · 글자가 섞인 학번(교환학생 '…KU…') ·
  내 줄은 오른쪽 칸. 지금 로직(scorefile·gradebook)을 바꾸지 않고 통과해야 한다 — 학번·이름·점수는 모두 지어낸 것.
  v0.8.4(계약 29-3 ①)부터 평균·표준편차는 PDF에 적힌 값(63.4·21.5)을 쓰고, 읽은 80행으로 계산한 값은 열의 mean_computed·sd_computed에 둔다.
"""
from __future__ import annotations

import statistics
from pathlib import Path

from lms_agent import scorefile
from lms_agent.server import App
from lms_agent.store import Store

SID = "2099123456"
MY_SCORE = 88.0
STATED_MEAN = 63.4
STATED_MEDIAN = 69.0
STATED_SD = 21.5
FILENAME = "2026_TEST_Final_결과_재검후.pdf"

# 80명: 앞 덩어리 21줄 × 2칸 + 뒤 덩어리 19줄 × 2칸. 내 줄은 앞 덩어리 11번째 줄의 오른쪽 칸.
_LEFT_1 = [52, 0, 71, 47, 83, 59, 62, 73, 55, 80, 43, 38, 86, 77, 69, 51, 79, 66, 49, 46, 97]
_RIGHT_1 = [70, 76, 74, 84, 72, 61, 57, 45, 93, 67, None, 71, 77, 80, 82, 87, 94, 63.5, 81, 74, 79]   # None = 내 자리
_LEFT_2 = [54, 65, 75, 33, 25, 70, 64, 90, 78, 85, 48, 72, 83, 81, 92, 58.5, 53, 76, 60]
_RIGHT_2 = [66, 73, 68, 44.5, 0, 41, 62, 59.5, 73, 77, 88, 86, 0, 65, 57, 40, 58, 5, 61]   # 값은 모두 지어낸 것


def _fmt(v) -> str:
    return f"{v:g}"


def layout_a_text() -> tuple[str, list[float]]:
    """양식 A의 PDF 글(줄 배치 그대로)과 모든 점수 목록."""
    ids = (f"20990{n:05d}" for n in range(1, 200))
    lines = ["KECE 999 Final", "채점 결과 및 통계", "점수 분포",
             "20", "18", "18", "16", "16", "14 13", "12", "12", "10 9", "8", "6", "4 4", "4 3", "2 1", "0", "0",
             "[0, 10] (10, 20] (20, 30] (30, 40] (40, 50] (50, 60] (60, 70] (70, 80] (80, 90] (90, 100]",
             "평균값 중간값 표준편차", f"{STATED_MEAN:g} {STATED_MEDIAN:g} {STATED_SD:g}", "", "학번 점수 학번 점수"]
    scores: list[float] = []
    for a, b in zip(_LEFT_1, _RIGHT_1):
        right = (SID, MY_SCORE) if b is None else (next(ids), b)
        lines.append(f"{next(ids)} {_fmt(a)} {right[0]} {_fmt(right[1])}")
        scores += [a, right[1]]
    lines += ["", "학번 점수 학번 점수"]
    for i, (a, b) in enumerate(zip(_LEFT_2, _RIGHT_2)):
        right_id = "2099KU0501" if i == len(_LEFT_2) - 1 else next(ids)   # 글자가 섞인 학번(교환학생)
        lines.append(f"{next(ids)} {_fmt(a)} {right_id} {_fmt(b)}")
        scores += [a, b]
    return "\n".join(lines) + "\n", [float(s) for s in scores]


def test_layout_a_reads_my_row_with_existing_logic():
    text, scores = layout_a_text()
    assert len(scores) == 80
    assert scorefile.is_score_file(FILENAME, text)
    (r,) = scorefile.parse_score_results(text, SID, scorefile.label_from_filename(FILENAME))
    assert r.score == MY_SCORE and r.max_score is None
    # 계약 29-3 ①: '평균값 중간값 표준편차' 머리글 + 값 줄 → 적힌 평균·표준편차를 쓴다(읽은 80행으로 계산한 값은 열에 남김)
    assert (r.mean, r.sd) == (STATED_MEAN, STATED_SD)
    (col,) = r.columns
    assert abs(col["mean_computed"] - statistics.mean(scores)) < 0.01 and abs(col["sd_computed"] - statistics.stdev(scores)) < 0.01
    assert col["median"] == STATED_MEDIAN and any("적힌 값" in n for n in r.notes)
    assert (r.n, r.high, r.low) == (80, max(scores), min(scores))
    assert r.rank == 1 + sum(1 for s in scores if s > MY_SCORE)
    assert "Final" in r.label
    for note in r.notes:                                         # 다른 학생 정보가 결과에 들어가지 않는다
        assert "20990" not in note and "KU0501" not in note


def test_layout_a_without_my_row_gives_nothing():
    text, _ = layout_a_text()
    assert scorefile.parse_score_results(text.replace(SID, "2099000999"), SID, "Final") in (None, [])


def test_layout_a_links_to_final_component_and_reaches_estimate(tmp_path: Path):
    """받아 둔 첨부(pdf_texts) → 점수 후보 → '기말고사'에 규칙으로 연결 → 성적 추정까지(지금 로직 그대로)."""
    text, _ = layout_a_text()
    s = Store(tmp_path / "state.json")
    s.data["settings"]["student_id"] = SID
    s.data["cache"] = {"fetched_at": "2026-06-10T10:00:00+09:00", "courses": [{"id": "1", "name": "테스트설계", "course_code": "T999"}],
                       "assignments": [], "conversations": [], "boards": [], "errors": [],
                       "announcements": [{"id": 5, "title": "기말고사 재검 결과", "message": "<p>재검 결과를 첨부합니다.</p>",
                                          "posted_at": "2026-06-06T10:00:00+09:00", "context_code": "course_1",
                                          "html_url": "https://lms.example.test/1"}]}
    s.data["pdf_texts"] = {"1:cf1": {"course_id": "1", "filename": FILENAME, "text": text,
                                     "posted_at": "2026-06-06T10:00:00+09:00", "board_id": "announcements", "post": "5"}}
    s.data["grades"] = {"1": {"components": [{"name": "중간고사", "weight": 30.0}, {"name": "기말고사", "weight": 40.0},
                                             {"name": "과제", "weight": 30.0}], "dist": None, "class_size": None}}
    s.save()
    app = App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "token": "test-token"})
    g = app.state()["grades"]
    (cand,) = [c for c in g["candidates"] if c.get("source_title") == FILENAME]
    assert (cand["score"], cand["n"], cand["component"]) == (MY_SCORE, 80, "기말고사")
    assert (cand["mean"], cand["sd"]) == (STATED_MEAN, STATED_SD)   # 적힌 값(①)
    (course,) = g["courses"]
    known = {c["name"]: c["status"] for c in course["estimate"]["components"]}
    assert known["기말고사"] == "known" and known["중간고사"] == "missing"
    assert SID not in str(g.get("trace") or "")                  # 진단 칸에도 학번이 나오지 않는다


def test_regraded_final_with_different_name_replaces_original(tmp_path: Path):
    """원본 '…463 기말고사 채점결과'와 재검 뒤 '…Final 결과_재검후'(이름이 바뀜)는 같은 기말 — 추정에는 최근 것만(리드, 계약 29-3 ②)."""
    from lms_agent import gradebook
    text, _ = layout_a_text()
    regraded = text.replace(f"{SID} 88", f"{SID} 91")
    cands = [{"key": "pdf:a", "label": "2026 TEST 463 기말고사 채점결과", "score": 88.0, "posted_at": "2026-06-06T10:00:00+09:00",
              "source_title": "2026_TEST_463_기말고사_채점결과.pdf"},
             {"key": "pdf:b", "label": "2026 TEST Final", "score": 91.0, "posted_at": "2026-06-09T10:00:00+09:00",
              "source_title": "2026_TEST_Final_결과_재검후.pdf"},
             {"key": "pdf:c", "label": "Lab 3", "score": 9.0, "posted_at": "2026-05-01T10:00:00+09:00", "source_title": "Lab3.pdf"},
             {"key": "pdf:d", "label": "Lab 4", "score": 8.0, "posted_at": "2026-05-08T10:00:00+09:00", "source_title": "Lab4.pdf"}]
    links = {k: {"component": comp, "by": "rules", "reason": "시험"} for k, comp in
             (("pdf:a", "기말고사"), ("pdf:b", "기말고사"), ("pdf:c", "과제"), ("pdf:d", "과제"))}
    m = gradebook.members_of(cands, links)
    assert [x["score"] for x in m["기말고사"]] == [91.0]                 # 재검 뒤 점수 하나만
    assert sorted(x["score"] for x in m["과제"]) == [8.0, 9.0]          # 번호가 다른 LAB은 따로
    assert SID in regraded                                              # (지어낸 양식에 내 줄이 있음)
