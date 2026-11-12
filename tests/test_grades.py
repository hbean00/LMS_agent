"""성적 위치 어림 벤치마크: 쉬움 / 중간 / 어려움.

수식 값은 손으로 계산한 식을 그대로 적어 비교한다(모듈 내부 함수를 다시 쓰지 않는다).
기본 학점 비율: A+15 A0 15 B+15 B0 15 C+15 C0 20 D+0 D0 0 F5 → 누적 15/30/45/60/75/95/95/95/100.
"""
import io
import math
import sys
import zipfile
from pathlib import Path
from statistics import NormalDist, fmean, stdev

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lms_agent.grades import (  # noqa: E402
    DEFAULT_DISTRIBUTION, GRADES, Component, estimate, find_scores, letter_for, needed_on, priorities,
    sd_from_quartiles, top_percent, validate_distribution, xlsx_stats,
)

PHI = NormalDist()


def sigma(pairs, rho):  # 손 계산: Σw²σ² + 2ρΣ_{i<j} w_iw_jσ_iσ_j
    var = sum((w * s) ** 2 for w, s in pairs)
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            var += 2 * rho * pairs[i][0] * pairs[i][1] * pairs[j][0] * pairs[j][1]
    return math.sqrt(var)


# ---------------------------------------------------------------- 쉬움

def test_easy_top_percent_z1():
    assert top_percent(80, 70, 10) == pytest.approx(15.8655, abs=1e-3)
    assert top_percent(70, 70, 10) == pytest.approx(50.0)
    with pytest.raises(ValueError):
        top_percent(80, 70, 0)


def test_easy_sd_from_quartiles():
    assert sd_from_quartiles(58, 74) == pytest.approx(16 / 1.349)
    with pytest.raises(ValueError):
        sd_from_quartiles(74, 58)


@pytest.mark.parametrize("top,grade", [
    (0, "A+"), (15, "A+"), (15.01, "A0"), (30, "A0"), (30.01, "B+"), (45, "B+"), (60.5, "C+"),
    (75.01, "C0"), (95, "C0"), (95.01, "F"), (100, "F"),
])
def test_easy_letter_for_boundaries(top, grade):
    assert letter_for(top) == grade


def test_easy_letter_for_never_returns_zero_share_grade():
    assert {letter_for(t / 10) for t in range(0, 1001)} == {"A+", "A0", "B+", "B0", "C+", "C0", "F"}


def test_easy_validate_distribution():
    assert validate_distribution(DEFAULT_DISTRIBUTION) == []
    assert sum(DEFAULT_DISTRIBUTION.values()) == 100 and list(DEFAULT_DISTRIBUTION) == GRADES
    bad = dict(DEFAULT_DISTRIBUTION)
    del bad["F"]
    bad["A+"] = -5
    problems = validate_distribution(bad)
    assert any("빠진 학점" in p and "F" in p for p in problems)
    assert any("음수" in p for p in problems)
    assert any("합" in p for p in problems)
    assert validate_distribution({**DEFAULT_DISTRIBUTION, "A+": 15.005}) == []  # ±0.01 허용


def test_easy_single_component_estimate():
    r = estimate([Component("중간고사", 30, score=80, mean=70, sd=10)], class_size=40)
    assert r["top"] == pytest.approx(15.8655, abs=1e-3)
    assert r["grade"] == "A0"
    assert r["range"][0] == pytest.approx(r["range"][1])  # 항목 하나면 ρ 영향 없음
    assert r["rank"] == round(0.158655 * 40)
    assert r["completed_weight"] == 30
    assert any("정규분포" in n for n in r["notes"])
    assert any("반영된 비중 30%만으로 계산" in n for n in r["notes"])


# ---------------------------------------------------------------- 중간

def test_medium_two_components_hand_computed():
    comps = [Component("중간고사", 40, score=80, mean=70, sd=10),
             Component("기말고사", 60, score=45, mean=35, sd=10, max_score=50),
             Component("출석", 10, score=10, max_score=10, fixed=True),
             Component("과제", 20)]
    r = estimate(comps)
    t, mu = 40 * 0.8 + 60 * 0.9, 40 * 0.7 + 60 * 0.7
    pairs = [(40, 0.1), (60, 0.2)]
    expect = {rho: 100 * (1 - PHI.cdf((t - mu) / sigma(pairs, rho))) for rho in (0.3, 0.6, 0.9)}
    assert r["top"] == pytest.approx(expect[0.6])
    assert r["range"] == pytest.approx(sorted([expect[0.3], expect[0.9]]))
    assert r["range"][0] < r["top"] < r["range"][1]  # ρ가 클수록 σ_T가 커져 덜 극단적
    assert r["grade_range"] == [letter_for(r["range"][0]), letter_for(r["range"][1])]
    assert [c["status"] for c in r["components"]] == ["known", "known", "fixed", "missing"]
    assert r["components"][3]["top"] is None
    assert r["completed_weight"] == 100
    assert any("과제" in n and "제외" in n for n in r["notes"])
    assert any("출석" in n for n in r["notes"])
    assert any("ρ=0.6" in n for n in r["notes"])


def test_medium_estimate_no_known_raises():
    with pytest.raises(ValueError):
        estimate([Component("중간고사", 30, score=80)])


def _needed(comps, target, rho=0.6):
    known = [c for c in comps if c.score is not None]
    k = [c for c in comps if c.score is None][0]
    wsum = sum(c.weight for c in known)
    mk = sum(c.weight * c.mean / 100 for c in known) / wsum
    sk = sum(c.weight * c.sd / 100 for c in known) / wsum
    pairs = [(c.weight, c.sd / 100) for c in known] + [(k.weight, sk)]
    mu = sum(c.weight * c.mean / 100 for c in known) + k.weight * mk
    cut = sum(DEFAULT_DISTRIBUTION[g] for g in GRADES[:GRADES.index(target) + 1])
    need_t = mu + PHI.inv_cdf(1 - cut / 100) * sigma(pairs, rho)
    xk = (need_t - sum(c.weight * c.score / 100 for c in known)) / k.weight
    return xk, 100 * (1 - PHI.cdf((xk - mk) / sk))


def test_medium_needed_on_possible():
    comps = [Component("중간고사", 40, score=80, mean=70, sd=10), Component("기말고사", 60)]
    r = needed_on(comps, "기말고사", "A0")
    xk, need_top = _needed(comps, "A0")
    assert (r["possible"], r["already"]) == (True, False)
    assert r["need_score"] == pytest.approx(xk * 100)
    assert r["need_top"] == pytest.approx(need_top)
    assert any("가정" in n and "기말고사" in n for n in r["notes"])


def test_medium_needed_on_impossible():
    comps = [Component("중간고사", 40, score=40, mean=70, sd=10), Component("기말고사", 60)]
    r = needed_on(comps, "기말고사", "A+")
    assert _needed(comps, "A+")[0] > 1
    assert r["possible"] is False and r["need_score"] is None


def test_medium_needed_on_already():
    comps = [Component("중간고사", 70, score=100, mean=70, sd=10), Component("기말고사", 30)]
    r = needed_on(comps, "기말고사", "C+")
    assert _needed(comps, "C+")[0] <= 0
    assert r["possible"] is True and r["already"] is True and r["need_score"] == 0
    assert needed_on(comps, "기말고사", "F")["already"] is True


def test_medium_priorities_ordering():
    courses = [
        {"course": "중간 위치", "estimate": {"top": 37.5}, "remaining_weight": 40, "dist": DEFAULT_DISTRIBUTION},
        {"course": "끝난 과목", "estimate": {"top": 16.0}, "remaining_weight": 0, "dist": DEFAULT_DISTRIBUTION},
        {"course": "A+ 직전", "estimate": {"top": 16.0}, "remaining_weight": 40, "dist": DEFAULT_DISTRIBUTION},
        {"course": "B+ 아래 경계", "estimate": {"top": 44.5}, "remaining_weight": 30},
    ]
    out = priorities(courses)
    assert [o["course"] for o in out][0] == "A+ 직전"
    assert out[-1]["course"] == "끝난 과목" and out[-1]["reason"] == "남은 평가 없음"
    first = out[0]
    assert first["grade"] == "A0" and "A0/A+ 경계까지 가까움" in first["reason"] and "남은 비중 40%" in first["reason"]
    z = PHI.inv_cdf(0.84)
    expect = 0.4 * (1 / (0.15 + PHI.inv_cdf(0.85) - z) + 0.5 / (0.15 + z - PHI.inv_cdf(0.70)))
    assert first["score"] == pytest.approx(expect)
    low = next(o for o in out if o["course"] == "B+ 아래 경계")
    assert "B+ 아래로 떨어질 위험" in low["reason"]


# ---------------------------------------------------------------- 점수 찾기

def one(text):
    found = find_scores(text)
    assert len(found) == 1, f"1개를 기대했지만 {len(found)}개: {found}"
    return found[0]


def test_scores_korean_paren():
    f = one("중간고사 성적: 78점 (평균 65.3, 표준편차 12.1)")
    assert (f.label, f.score, f.mean, f.sd) == ("중간고사", 78, 65.3, 12.1)


def test_scores_fraction_with_keyword():
    f = one("본인 점수 78 / 100, 평균 65.3점, 표준편차 12.1")
    assert (f.score, f.max_score, f.mean, f.sd) == (78, 100, 65.3, 12.1)
    assert f.label is None and any("어느 평가" in n for n in f.notes)


def test_scores_class_stats_only():
    f = one("평균: 65.3 / 표준편차: 12.1 / 최고점 98")
    assert (f.score, f.mean, f.sd, f.high) == (None, 65.3, 12.1, 98)
    assert any("본인 점수 없음" in n for n in f.notes)


def test_scores_english():
    f = one("Midterm: 78/100 (mean 65.3, SD 12.1)")
    assert (f.label, f.score, f.max_score, f.mean, f.sd) == ("Midterm", 78, 100, 65.3, 12.1)


def test_scores_max_first():
    f = one("만점 50점 중 41점, 평균 32.5점, 표준편차 6.1")
    assert (f.score, f.max_score, f.mean, f.sd) == (41, 50, 32.5, 6.1)


def test_scores_quartiles_fill_sd():
    f = one("중간고사 중앙값 66, 1사분위 58, 3사분위 74")
    assert (f.median, f.q1, f.q3) == (66, 58, 74)
    assert f.sd == pytest.approx(16 / 1.349, abs=0.01)
    assert any("사분위" in n for n in f.notes)


def test_scores_multiline_group():
    f = one("[중간고사 성적 안내]\n본인 점수: 78점\n평균 65.3점, 표준편차 12.1점")
    assert (f.label, f.score, f.mean, f.sd) == ("중간고사", 78, 65.3, 12.1)


def test_scores_two_quizzes_on_adjacent_lines():
    found = find_scores("퀴즈 1: 8/10 (평균 6.5, 표준편차 1.2)\n퀴즈 2: 9/10 (평균 7.0, 표준편차 1.0)")
    assert [(f.label, f.score, f.max_score) for f in found] == [("퀴즈 1", 8, 10), ("퀴즈 2", 9, 10)]


def test_scores_student_id_is_not_score():
    f = one("학번 2021123456 학생 점수 78점, 평균 70")
    assert (f.score, f.mean) == (78, 70)


@pytest.mark.parametrize("text", [
    "중간고사는 11월 3일 오후 3시에 진행합니다.",
    "과제 마감은 11월 20일 23:59입니다.",
    "학번 2021123456 학생은 연구실로 오세요.",
    "문의: 조교 010-1234-5678",
    "성적 반영 비율: 중간고사 30%, 기말고사 40%, 과제 30%",
    "점수 이의신청은 11월 20일까지 가능합니다.",
    "2026.11.03 중간고사 성적 공개 예정",
    "LAB 3 제출: 11/14(금) 18:00",
])
def test_scores_negatives(text):
    assert find_scores(text) == []


# ---------------------------------------------------------------- 엑셀

def _xlsx(sheet_rows, decoy_rows=None):
    """sheet_rows: 행마다 [값...]. str → 공유 문자열, ('i', s) → 인라인 문자열, 숫자 → 숫자, None → 빈 칸."""
    shared = []

    def sheet_xml(rows):
        out = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, v in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if v is None:
                    continue
                if isinstance(v, tuple):
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v[1]}</t></is></c>')
                elif isinstance(v, str):
                    if v not in shared:
                        shared.append(v)
                    cells.append(f'<c r="{ref}" t="s"><v>{shared.index(v)}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        return ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                f'spreadsheetml/2006/main"><sheetData>{"".join(out)}</sheetData></worksheet>')

    main = sheet_xml(sheet_rows)
    decoy = sheet_xml(decoy_rows or [["x", "y"], [1, 2]])
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' \
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="성적" sheetId="1" r:id="rId7"/>'
                   f'<sheet name="다른" sheetId="2" r:id="rId8"/></sheets></workbook>')
        # 첫 시트가 sheet2.xml을 가리킨다 → rels를 따라가는지 확인
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId7" Type="{rel}/worksheet" Target="worksheets/sheet2.xml"/>'
                   f'<Relationship Id="rId8" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
                   f'<Relationship Id="rId9" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet2.xml", main)
        z.writestr("xl/worksheets/sheet1.xml", decoy)
        sst = "".join(f"<si><t>{s}</t></si>" for s in shared)
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{sst}</sst>')
    return buf.getvalue()


ROWS = [
    ["2026-2 회로이론 중간고사"],                     # 제목 행(글자 칸 1개) → 머리글 아님
    ["학번", "이름", "중간고사", "과제", "비고"],
    [2021123456, "김*수", 78, 9.5, "재시험"],
    ["2021123457", "이*희", 65, None, None],
    [2021123458, ("i", "박*민"), 52, 8, None],
    [2021123459, "최*진", "결시", 7, None],
    ["평균", None, 65, 8.17, None],                   # 요약 행 → 빼야 함
]


def test_xlsx_own_row_and_aggregates():
    r = xlsx_stats(_xlsx(ROWS), "2021123456")
    assert r["found"] is True
    cols = {c["name"]: c for c in r["columns"]}
    assert set(cols) == {"중간고사", "과제"}  # 학번·이름·비고는 통계 대상 아님
    mid = cols["중간고사"]
    assert (mid["mine"], mid["n"]) == (78, 3)
    assert mid["mean"] == pytest.approx(fmean([78, 65, 52]))
    assert mid["sd"] == pytest.approx(stdev([78, 65, 52]))
    assert cols["과제"]["mine"] == 9.5 and cols["과제"]["n"] == 3
    assert cols["과제"]["mean"] == pytest.approx(fmean([9.5, 8, 7]))
    assert any("표본표준편차" in n for n in r["notes"])


def test_xlsx_privacy_no_other_ids_or_names():
    text = repr(xlsx_stats(_xlsx(ROWS), "2021123457"))
    for other in ("2021123456", "2021123458", "2021123459", "김*수", "박*민", "최*진", "재시험"):
        assert other not in text


def test_xlsx_string_id_and_not_found():
    r = xlsx_stats(_xlsx(ROWS), "2021123457")
    assert r["found"] is True
    assert {c["name"]: c["mine"] for c in r["columns"]} == {"중간고사": 65, "과제": None}
    r = xlsx_stats(_xlsx(ROWS), "2099000000")
    assert r["found"] is False and all(c["mine"] is None for c in r["columns"])
    assert r["columns"][0]["n"] == 3


def test_xlsx_masked_ids():
    rows = [["학번", "점수"], ["2021****56", 80], ["2021****57", 60]]
    r = xlsx_stats(_xlsx(rows), "2021123456")
    assert r["found"] is True and r["columns"][0]["mine"] == 80
    assert any("가린 학번" in n for n in r["notes"])


def test_xlsx_rejects_non_zip():
    with pytest.raises(ValueError):
        xlsx_stats(b"not a zip", "1")
