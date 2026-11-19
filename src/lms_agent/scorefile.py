"""성적 공지 첨부 PDF(학생별 점수표)에서 **내 점수와 반 전체 집계만** 뽑는다.

예: 'Lab0, Lab1 Score Announcement' 공지의 Lab0_score-1.pdf — 한 줄에 한 학생, 학번은 '2026####33'처럼 가려진 경우가 많다.
PDF에서 뽑은 글(materials의 pdf_texts)은 줄이 대략 표의 행을 따르고 칸은 공백으로 나뉜다. 줄이 칸마다 끊긴 글도
학번 자리를 기준으로 다시 행을 만든다.

개인정보: 결과에는 열마다 평균·표본표준편차·인원·최고·최저와 **내 값·내 등수만** 들어간다.
다른 학생의 행·학번·이름은 돌려주지도 저장하지도 않는다(가린 학번이 여러 행과 맞으면 본인을 고르지 않는다).

평가가 하나인지 여럿인지(docs/CORNER_CASES.md 가족 5): 한 PDF에 표가 여럿이면 표마다, 한 표에 서로 다른 평가가 열마다 있으면
('Quiz 1 (10) | Midterm (100)', 합계 열 없음) 열마다 결과를 낸다 — parse_score_results. parse_score_text는 예전처럼 결과 하나.

표 파일(엑셀 xlsx·xls, CSV — sheets.read가 읽은 칸, 계약 25-2)은 parse_sheet_results: 시트 하나 = 표 하나로 열마다 집계한 뒤
PDF와 같은 뒷부분(_col_stats → _table_result, _split_results)을 함께 쓴다. 점수표인지는 is_score_sheet.

코너 케이스(계약 29-3 ①~⑩ — 시험 tests/test_score_corners.py):
① '평균값 중간값 표준편차'·'Mean Median SD' 머리글 줄 + 값 줄(또는 'Mean: 6.9, SD: 1.7' 한 줄)은 적힌 평균·표준편차를 쓴다(계산 값은
  열의 mean_computed·sd_computed, 메모). 점수 범위 밖이면 쓰지 않는다. ② 재검·v2·수정본: revision_of(이름)·label_from_filename이 표시를 뗀다
  (같은 평가의 옛 파일은 gradebook.members_of가 뺀다). ③ 개인 성적표(표 없이 내 학번 하나 — 'Total 87/100'·'총점: 87'): personal=True,
  반 통계는 적힌 것만. ④ 칸 속 '87/100'·'87 / 100'(분모가 같은 열이면 만점)·'87%'(점수가 %뿐일 때만 — 점수 옆 백분위 %는 뺌).
⑤ 가린 학번이 여러 줄과 맞으면 ambiguous = {"kind": "masked_multi", "matches": n, "reason"} 결과(score None, 반 집계만).
⑥ 공지·게시판 본문(HTML 표)은 body_score_results. ⑦ 결시·미응시·'-'는 통계에서 빼고(0점은 셈), 내 칸이면 absent = 그 표시(score None).
⑧ 학점 글자 열(A+·B0, 머리글이 학점·등급이면 A·B도)은 점수가 아니라 letter, 평점(4.5) 열도 점수가 아님. ⑨ 학번 구분자('2099-12-3456',
  내 학번 모양이면 '2099 12 3456'). ⑩ 학번보다 앞의 점수 열(머리글 '점수 학번'), 순번은 머리글·차례로 가른다.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_MASK = set("*#●○■□xX＊")
_NUM = re.compile(r"^\(?([-+]?\d{1,3}(?:\.\d+)?)\)?(?:점|pts?)?$")
_PCT = re.compile(r"^\d+(?:\.\d+)?\s*[%％]$")
# 점수 칸의 날짜·시각(건너뜀). "11.18"처럼 점으로 쓴 짧은 날짜는 넣지 않는다 — 실제 성적표의 22.5 같은 소수 점수와 구별 못 함
_DATE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}\.?$|^\d{1,2}[/-]\d{1,2}$|^\d{1,2}:\d{2}(?::\d{2})?$")
_AVG_LINE = re.compile(r"^\s*(?:평균|average|mean|avg)\b", re.I)
_PLACE = re.compile(r"^(?:-|–|—|−|/|결시|결석|미응시|응시\s*안\s*함|미제출|미채점|불참|면제|absent|abs\.?|n/?a|na|무|"
                    r"excused|incomplete|inc\.?)$", re.I)
_SUMMARY = re.compile(r"^(?:평균|표준\s*편차|합계|총계|최고(?:점)?|최저(?:점)?|최댓값|최솟값|중앙값|인원|응시자|"
                      r"average|mean|avg|sd|std|stdev|stddev|total\s*students|max|min|median|count)\b[:：]?", re.I)
_ID_HEAD = re.compile(r"^(?:학번|수험\s*번호|student|id|no\.?|번호|순번|연번|#|이름|성명|name|성|명|학과|전공|"
                      r"department|major|학년|분반|section)$", re.I)
_TOTAL_HEAD = re.compile(r"total|합계|총점|sum|final|최종|환산|score|점수", re.I)
_MAX_INLINE = re.compile(r"^(?P<name>.*?)(?:[(\[]\s*(?P<a>\d{1,3})\s*(?:점)?\s*[)\]]|/\s*(?P<b>\d{1,3}))$")
_MAX_TOKEN = re.compile(r"[(\[/]\s*(\d{1,3})\s*(?:점)?\s*[)\]]?")
# 만점 행('만점 20 30 50', 'Full marks 20 30 50') — 학생 행도 머리글도 아니다. 'Max'만 있는 행은 반 최고점일 수 있어 쓰지 않는다
_MAX_ROW = re.compile(r"^\s*(?:만점|배점|full\s*marks?|possible(?:\s*points?)?|out\s+of|max(?:imum)?\s*(?:points?|score))\b"
                      r"[:：]?", re.I)
# 점수가 아닌 숫자 칸: 등수(값이 클수록 나쁨)·백분율(%) — 열에서 뺀다
_RANK_HEAD = re.compile(r"^(?:등수|순위|석차|rank(?:ing)?|place)$", re.I)
_PCT_HEAD = re.compile(r"^(?:%|％|\(%\)|percent(?:age)?|pct|백분율|퍼센트|백분위)$", re.I)
# 표 제목의 평가 이름('Lab0 Scores', 'HW 2', '중간고사') — 한 PDF에 표가 여럿일 때 표를 가른다
_TABLE_LABEL = re.compile(r"(?i)(?<![a-z])(?:lab|hw|homework|assignment|quiz|project|exam|test|report)[ \t_]*#?[ \t]*\d{1,2}(?!\d)|"
                          r"(?<![a-z])(?:midterm|final)(?:[ \t]+(?:exam|project|report))?(?![a-z])|중간고사|기말고사|"
                          r"(?:과제|실습|퀴즈|프로젝트)[ \t]*\d{1,2}(?!\d)")


def _norm_header(line: str) -> str:
    """머리글의 만점 표시를 한 모양으로: '(max 20)'·'(20 pts)'·'(만점 20)'·'[20]'·'/ 20' → '(20)'·'[20]'·'/20'."""
    s = re.sub(r"(?i)[(\[]\s*(?:만점|max(?:imum)?|out\s+of|full)\s*[:：]?\s*(\d{1,3})\s*(?:점|pts?|points?|marks?)?\s*[)\]]",
               r"(\1)", line)
    s = re.sub(r"(?i)[(\[]\s*(\d{1,3})\s*(?:점\s*만점|pts?|points?|marks?)\s*[)\]]", r"(\1)", s)
    s = re.sub(r"([(\[/])\s+(\d)", r"\1\2", s)
    s = re.sub(r"(\d)\s+([)\]])", r"\1\2", s)
    return re.sub(r"\s*[|│¦]\s*", " ", s)     # '순번 | 학번 | Quiz 1 (10)'의 칸 구분선
_MAX_GLOBAL = re.compile(r"만점\s*[:：]?\s*(\d{1,3})|(\d{1,3})\s*점\s*만점|out\s+of\s+(\d{1,3})|full\s+marks?\s*[:：]?\s*(\d{1,3})",
                         re.I)
_SCORE_NAME = re.compile(r"score|점수|성적|grade|result|결과|채점", re.I)


# ---------------------------------------------------------------- 칸 값(코너 케이스 ④⑦⑧, 계약 29-3)

class _Absent(str):
    """칸의 결시·미응시·'-' 표시 — 값이 아니다(통계에서 뺀다). 내 칸이면 결과의 absent."""


class _Letter(str):
    """칸의 학점 글자(A+·B0) — 점수가 아니다(열 위치만 지킨다). 내 칸이면 결과의 letter."""


class _Frac(float):
    """칸의 '87/100'(값 87, of = 100)·'87%'(of = 100). 같은 열의 분모가 하나면 그 열의 만점."""
    of = None

    def __new__(cls, v, of=None):
        x = float.__new__(cls, v)
        x.of = of
        return x


def _is_val(v) -> bool:
    return v is not None and not isinstance(v, str)


@dataclass(frozen=True)
class _Opt:
    """한 글(표)의 칸 읽기 선택: fracs = 날짜처럼 보여도 점수로 읽을 분모('9/10'의 10 — 여러 줄이 같은 분모·다른 분자),
    pct = 점수 칸이 '87%'뿐(만점 100), letters = 머리글에 학점·등급이 있어 'A'·'B'도 학점 글자로."""
    fracs: frozenset = frozenset()
    pct: bool = False
    letters: bool = False


_PLAIN = _Opt()
_NUM_ONLY = re.compile(r"^\d{1,4}(?:\.\d+)?$")
_FRAC_TOK = re.compile(r"^\(?(\d{1,4}(?:\.\d+)?)/(\d{1,4}(?:\.\d+)?)\)?(?:점)?$")
_LETTER_STRONG = re.compile(r"^(?:[A-D][+0\-−]|NP)$")
_LETTER_WEAK = re.compile(r"^(?:[A-DF]|P|S|U)$")
_LETTER_HEAD = re.compile(r"(?i)(?:^|\s)(?:학점|등급|평어|letter(?:\s*grade)?|grade)(?=\s|$|[(\[])")
_GPA_HEAD = re.compile(r"^(?:평점|평균\s*평점|gpa|grade\s*points?|학점\s*평점)$", re.I)
_ROWNO = re.compile(r"^(?:no\.?|번호|순번|연번|#)$", re.I)
_ID_WORD_TOK = re.compile(r"^(?:학번|수험\s*번호|student|id)$", re.I)


def _merge_fracs(tokens: list[str], id_at: int = -1) -> tuple[list[str], int]:
    """'87 / 100'·'87 /100'·'87/ 100' → '87/100'(학번 칸은 숫자 4자리를 넘어 합쳐지지 않는다). → (칸들, 학번 칸의 새 위치)."""
    out, new_at, i = [], -1, 0
    while i < len(tokens):
        t = tokens[i]
        if i == id_at:
            new_at = len(out)
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if _NUM_ONLY.match(t) and nxt == "/" and i + 2 < len(tokens) and _NUM_ONLY.match(tokens[i + 2]) and id_at not in (i + 1, i + 2):
            out.append(f"{t}/{tokens[i + 2]}")
            i += 3
            continue
        if _NUM_ONLY.match(t) and re.fullmatch(r"/\d{1,4}(?:\.\d+)?", nxt) and id_at != i + 1:
            out.append(t + nxt)
            i += 2
            continue
        if re.fullmatch(r"\d{1,4}(?:\.\d+)?/", t) and _NUM_ONLY.match(nxt) and id_at != i + 1:
            out.append(t + nxt)
            i += 2
            continue
        out.append(t)
        i += 1
    return out, new_at


def _date_like(num: float, den: float) -> bool:
    return num.is_integer() and den.is_integer() and 1 <= num <= 12 and 1 <= den <= 31


def _scan_opts(lines: list[str]) -> _Opt:
    """학번 줄들의 칸을 훑어 _Opt를 정한다(글 하나에 한 번)."""
    dens: Counter = Counter()
    nums: dict = {}
    id_lines = pct_rows = num_rows = 0
    letters = False
    for line in lines[:3000]:
        toks = line.split()
        ids = [j for j, t in enumerate(toks) if _is_id(t)]
        if not ids:
            if not letters and _LETTER_HEAD.search(_norm_header(line)):
                letters = True
            continue
        if len(ids) != 1:
            continue
        toks, at = _merge_fracs(toks, ids[0])
        id_lines += 1
        has_num = has_pct = False
        for j, t in enumerate(toks):
            if j == at:
                continue
            m = _FRAC_TOK.match(t)
            if m:
                dens[m.group(2)] += 1
                nums.setdefault(m.group(2), set()).add(m.group(1))
                has_num = True
            elif _PCT.match(t):
                has_pct = True
            elif j > at and _NUM.match(t):
                has_num = True
        pct_rows += has_pct and not has_num
        num_rows += has_num
    fracs = frozenset(float(d) for d, c in dens.items() if c >= max(3, 0.5 * id_lines) and len(nums[d]) >= 2)
    pct = id_lines >= 2 and num_rows == 0 and pct_rows >= 0.8 * id_lines
    return _Opt(fracs, pct, letters)


_SKIP = object()


def _cell_value(t: str, opt: _Opt):
    """칸 하나 → 숫자(float·_Frac) | _Absent | _Letter | _SKIP(날짜·백분위·모르는 글자)."""
    m = _FRAC_TOK.match(t)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        if den > 0 and num <= den * 1.5 and (not _date_like(num, den) or den in opt.fracs):
            return _Frac(num, den)
        return _SKIP
    if _PCT.match(t):
        return _Frac(float(re.match(r"\d+(?:\.\d+)?", t).group(0)), 100.0) if opt.pct else _SKIP
    if _DATE.match(t):
        return _SKIP
    m = _NUM.match(t)
    if m:
        return float(m.group(1))
    if _PLACE.match(t):
        return _Absent(t)
    if _LETTER_STRONG.match(t) or (opt.letters and _LETTER_WEAK.match(t)):
        return _Letter(t.replace("−", "-"))
    return _SKIP


@dataclass
class ScoreFileResult:
    label: str | None                # 'Lab0' (파일 이름에서) 또는 넘겨준 이름
    column: str                      # 대표 열(합계·점수 열, 없으면 마지막 숫자 열)
    score: float | None              # 내 값(대표 열). 결시(absent)·애매함(ambiguous)이면 None
    max_score: float | None
    mean: float | None               # 개인 성적표(personal)에 평균이 안 적혀 있으면 None
    sd: float | None
    n: int | None                    # 개인 성적표는 None(반 인원을 모름)
    rank: int | None                 # 대표 열에서 내 등수(1 = 가장 높음)
    high: float | None
    low: float | None
    columns: list = field(default_factory=list)   # [{"name","mine","mean","sd","n","rank","high","low","max_score"(, "absent",
    #                                                 "mean_computed","sd_computed","median")}]
    sentence: str = ""               # 한국어 요약(다른 학생 정보 없음)
    notes: list = field(default_factory=list)
    key: str | None = None           # 한 파일에서 결과가 여럿일 때 결과마다 다른 이름(표 이름·열 이름), 하나뿐이면 None
    # 코너 케이스(계약 29-3) — 성적 후보(gradebook)에 같은 이름의 칸으로 넘어간다(값이 있을 때만). 화면·AI(S2)가 쓴다.
    ambiguous: dict | None = None    # ⑤ {"kind": "masked_multi", "matches": 맞은 줄 수, "reason": 한국어} — 내 줄을 못 정함(score None)
    letter: str | None = None        # ⑧ 내 학점 글자('A+'·'B0' — 점수가 아님)
    absent: str | None = None        # ⑦ 내 칸이 결시·미응시·'-' 등이면 그 표시(0점이 아님 — score None)
    personal: bool = False           # ③ 개인 성적표(표 없이 내 학번 하나) — 반 통계는 글에 적힌 것만


# ---------------------------------------------------------------- 판별·이름

def _is_id(tok: str) -> bool:
    """학번처럼 보이는 칸: 6~12자, 숫자(또는 가린 자리)가 대부분, 숫자 2개 이상."""
    if not 6 <= len(tok) <= 12 or not re.fullmatch(r"[A-Za-z0-9*#●○■□＊]+", tok):
        return False
    digits = sum(ch.isdigit() for ch in tok)
    masked = sum(ch in _MASK for ch in tok)
    if digits < 2 or digits + masked < len(tok) - (2 if digits >= 6 else 1):
        return False
    letters = len(tok) - digits - masked
    return tok.isdigit() or masked > 0 or letters <= 1 or (letters == 2 and digits >= 6)


def label_from_filename(filename: str | None) -> str | None:
    """'Lab0_score-1.pdf' → 'Lab0', '중간고사_성적.pdf' → '중간고사', 'score_midterm.pdf' → 'midterm'."""
    if not filename:
        return None
    parts = [p for p in re.split(r"[_\-\s.()\[\]]+", Path(filename).stem) if p]
    idx = next((i for i, p in enumerate(parts) if _SCORE_NAME.fullmatch(p) or re.fullmatch(r"(?i)scores|grades|results", p)), None)
    if idx is None:
        keep = parts
    elif idx > 0:
        keep = parts[:idx]
    else:
        keep = [p for p in parts[1:] if not re.fullmatch(r"\d{1,2}|v\d+", p)]
    # 다시 낸 표시(v2·재검·수정본 — ②)와 학번 같은 조각('2099123456_Midterm.pdf' — 개인 성적표)은 평가 이름이 아니다
    keep = [p for p in keep if not re.fullmatch(r"(?i)announcement|공지|re|재공지|copy|사본|final_?ver", p)
            and not _REV_PART.fullmatch(p) and not _is_id(p)]
    return " ".join(keep).strip() or None


def is_score_file(filename: str | None, text: str | None) -> bool:
    """점수표 같아 보이는지: 파일 이름에 score·점수·성적·grade가 있거나, 학번 + 숫자(분수·%)인 줄이 여럿,
    또는 개인 성적표 모양('총점: 87' + 파일 이름의 학번 조각이나 글의 '학번: …' — ③)."""
    if filename and _SCORE_NAME.search(Path(filename).stem):
        return True
    text = normalize_ids(text or "")
    rows = 0
    for line in text.splitlines()[:2000]:
        toks = line.split()
        if any(_is_id(t) for t in toks) and any(_NUM.match(t) or (_FRAC_TOK.match(t) and not _DATE.match(t)) or _PCT.match(t)
                                                for t in toks if not _is_id(t)):
            rows += 1
            if rows >= 5:
                return True
    return _personal_shape(filename, text)


def _masked_match(tok: str, sid: str) -> bool:
    if len(tok) != len(sid) or not any(ch in _MASK for ch in tok):
        return False
    visible = [(a, b) for a, b in zip(tok, sid) if a not in _MASK]
    return len(visible) >= len(sid) / 2 and all(a.lower() == b.lower() for a, b in visible)


# ---------------------------------------------------------------- 행 만들기

def _values(tokens: list[str], id_at: int, opt: _Opt = _PLAIN, pre: str = "drop") -> list:
    """학번 칸을 뺀 칸들 → 값 목록(숫자·_Frac, 결시 _Absent, 학점 글자 _Letter — 열 위치를 지킨다).
    pre = 학번 앞 칸: "drop"(작은 정수 = 순번, 예전), "keep"(학번보다 앞의 점수 열 — 머리글 '점수 학번', ⑩), "rowno"(첫 정수는 순번, 나머지는 값)."""
    tokens, id_at = _merge_fracs(tokens, id_at)
    vals = []
    first_pre = True
    for j, t in enumerate(tokens):
        if j == id_at:
            continue
        if j < id_at and re.fullmatch(r"\d{1,4}", t):
            if pre == "drop" or (pre == "rowno" and first_pre):
                first_pre = False
                continue                   # 학번 앞의 작은 정수 = 순번
        if j < id_at:
            first_pre = False
        v = _cell_value(t, opt)
        if v is not _SKIP:
            vals.append(v)
    return vals


def _head_above(lines: list[str], first: int) -> list[str] | None:
    """첫 학번 줄 바로 위(4줄 안)의 머리글 칸(글자가 있고 학번이 없는 줄, 요약·만점 줄은 건너뜀)."""
    for line in reversed(lines[max(0, first - 4):first]):
        if _SUMMARY.match(line.strip()) or _MAX_ROW.match(line):
            continue
        toks = _norm_header(line).split()
        if toks and any(re.search(r"[A-Za-z가-힣]", t) for t in toks) and not any(_is_id(t) for t in toks):
            return toks
    return None


def _pre_rule(lines: list[str], raw: list) -> str:
    """학번 앞 칸을 어떻게 읽을지(⑩): 머리글에서 학번 낱말 앞에 점수 이름이 있으면 그 칸은 값("keep" — 순번 낱말이나
    1, 2, 3 … 차례면 첫 정수는 순번 "rowno"), 아니면 예전처럼 작은 정수는 순번("drop")."""
    head = _head_above(lines, raw[0][0]) or []
    at = next((k for k, t in enumerate(head) if _ID_WORD_TOK.match(t)), None)
    if at is None:
        return "drop"
    before = head[:at]
    if not any(re.search(r"[A-Za-z가-힣]", t) and not _ID_HEAD.match(t) and not _ROWNO.match(t) for t in before):
        return "drop"
    firsts = [int(toks[0]) if at_ > 0 and re.fullmatch(r"\d{1,4}", toks[0]) else None for _, toks, at_ in raw]
    seq = sum(1 for a, b in zip(firsts, firsts[1:]) if a is not None and b is not None and b == a + 1)
    if any(_ROWNO.match(t) for t in before) or (len(firsts) >= 3 and seq >= 0.8 * (len(firsts) - 1)):
        return "rowno"
    return "keep"


def _line_rows(lines: list[str], opt: _Opt = _PLAIN) -> tuple[list[tuple[str, list]], int | None]:
    raw = []
    for i, line in enumerate(lines):
        toks = line.split()
        if not toks or _SUMMARY.match(line.strip()):
            continue
        ids = [j for j, t in enumerate(toks) if _is_id(t)]
        if len(ids) != 1:
            continue
        raw.append((i, toks, ids[0]))
    if not raw:
        return [], None
    pre = _pre_rule(lines, raw)
    rows, first = [], None
    for i, toks, at in raw:
        vals = _values(toks, at, opt, pre)
        if not vals or not any(_is_val(v) or isinstance(v, _Absent) for v in vals):
            continue
        rows.append((toks[at], vals))
        if first is None:
            first = i
    return rows, first


def _stream_rows(text: str, opt: _Opt = _PLAIN) -> list[tuple[str, list]]:
    """칸마다 줄이 끊긴 글: 학번 칸을 기준으로 다시 행을 만든다. 학번 바로 앞의 1,2,3… 은 순번으로 뺀다."""
    toks = text.split()
    ids = [i for i, t in enumerate(toks) if _is_id(t)]
    if len(ids) < 3:
        return []
    before = [toks[i - 1] if i > 0 else "" for i in ids]
    nums = [int(b) if re.fullmatch(r"\d{1,4}", b) else None for b in before]
    seq = sum(1 for a, b in zip(nums, nums[1:]) if a is not None and b is not None and b == a + 1)
    has_rowno = seq >= 0.8 * (len(ids) - 1)
    rows = []
    for k, i in enumerate(ids):
        end = ids[k + 1] - (1 if has_rowno else 0) if k + 1 < len(ids) else len(toks)
        seg = toks[i + 1:end]
        cut = next((j for j, t in enumerate(seg) if _SUMMARY.match(t)), None)
        if cut is not None:
            seg = seg[:cut]
        vals = _values([toks[i]] + seg, 0, opt)
        if vals:
            rows.append((toks[i], vals))
    return rows


def _header(lines: list[str], first: int | None) -> list[str]:
    if first is None:
        return []
    for line in reversed(lines[max(0, first - 4):first]):
        if _SUMMARY.match(line.strip()) or _MAX_ROW.match(line):
            continue   # '만점 20 30 50' 행은 머리글이 아니다
        toks = _norm_header(line).split()
        if sum(bool(re.search(r"[A-Za-z가-힣]", t)) for t in toks) >= 1 and not any(_is_id(t) for t in toks):
            return toks
    return []


def _looks_header(line: str) -> bool:
    toks = _norm_header(line).split()
    return any(_ID_HEAD.match(t) or _TOTAL_HEAD.fullmatch(t) or _MAX_TOKEN.fullmatch(t)
               or (_MAX_INLINE.match(t) and (_MAX_INLINE.match(t).group("a") or _MAX_INLINE.match(t).group("b")))
               for t in toks)


def _label_key(s: str | None) -> str:
    return re.sub(r"[\s_#\-]+", "", str(s or "")).lower()


def _split_tables(lines: list[str], opt: _Opt = _PLAIN) -> list[tuple[int, int, str | None]]:
    """한 글 안의 점수표들 [(시작 줄, 끝 줄, 표 이름)]. 행 묶음 사이의 머리 줄에 다른 평가 이름('Lab1 Scores')이 있거나
    다른 열 머리글이 나오면 새 표. 같은 머리글이 되풀이되는 것(페이지 머리·'Page 1 of 2')은 같은 표다."""
    kinds = []
    for line in lines:
        s = line.strip()
        toks = s.split()
        ids = [j for j, t in enumerate(toks) if _is_id(t)]
        if not toks:
            kinds.append(None)
        elif _SUMMARY.match(s) or _MAX_ROW.match(s):
            kinds.append("sum")
        elif len(ids) == 1 and any(_is_val(v) or isinstance(v, _Absent) for v in _values(toks, ids[0], opt)):
            kinds.append("row")
        elif not ids and re.search(r"[A-Za-z가-힣]", s):
            kinds.append("head")
        else:
            kinds.append(None)
    tables: list[list] = []      # [start, end, label, colhead]
    group: list[int] = []
    for i, k in enumerate(kinds):
        if k == "head":
            group.append(i)
            continue
        if k != "row":
            continue
        heads = [g for g in group if _looks_header(lines[g])]
        colhead = re.sub(r"\s+", " ", _norm_header(lines[heads[-1]])).strip().lower() if heads else None
        labs = [m.group(0) for g in group if g not in heads[-1:] for m in [_TABLE_LABEL.search(lines[g])] if m]
        lab = re.sub(r"\s+", " ", labs[-1]).strip() if labs else None
        if not tables:
            tables.append([0, None, lab, colhead])
        elif group:
            cur = tables[-1]
            if (lab and _label_key(lab) != _label_key(cur[2])) or (colhead and cur[3] and colhead != cur[3]):
                cur[1] = group[0]
                tables.append([group[0], None, lab, colhead])
            elif cur[3] is None:
                cur[3] = colhead
        group = []
    if tables:
        tables[-1][1] = len(lines)
    return [(a, b, lab) for a, b, lab, _ in tables]


def _numbered_name(header: list[str], i: int) -> bool:
    """header[i]가 바로 뒤 만점 표시 앞의 작은 정수 — 'Quiz 1 (10)', 'Lab 2 [20]', 'HW 3 /10'의 번호(이름의 일부)."""
    return bool(re.fullmatch(r"\d{1,2}", header[i]) and i + 1 < len(header) and _MAX_TOKEN.fullmatch(header[i + 1])
                and i > 0 and re.search(r"[A-Za-z가-힣]", header[i - 1]))


def _segments(header: list[str]) -> tuple[list[str], list, int]:
    """"Required Items (10) Functional Description (25)"·"필수 요소(10) 동작 설명(25)"처럼 만점 표시가 이름 끝을 닫는 머리글:
    표시 앞의 낱말들을 한 이름으로 묶는다. (이름들, 만점들, 만점 표시로 닫힌 이름 수)."""
    names, maxes, pending, closed = [], [], [], 0
    for i, t in enumerate(header):
        if _ID_HEAD.match(t) or _PLACE.match(t):
            continue
        m = _MAX_TOKEN.fullmatch(t)
        mm = None if m else _MAX_INLINE.match(t)
        if m and pending:
            names.append(" ".join(pending))
            maxes.append(float(m.group(1)))
        elif mm and mm.group("name") and (mm.group("a") or mm.group("b")):
            names.append(" ".join(pending + [mm.group("name").strip()]))
            maxes.append(float(mm.group("a") or mm.group("b")))
        elif _NUM.match(t):
            if pending and _numbered_name(header, i):
                pending.append(t)   # 'Quiz 1 (10)'의 1은 이름의 번호
            continue
        else:
            pending.append(t)
            continue
        pending, closed = [], closed + 1
    for t in pending:  # 끝에 만점 표시 없이 남은 낱말은 하나씩
        names.append(t)
        maxes.append(None)
    return names, maxes, closed


def _columns(header: list[str], k: int) -> tuple[list[str], list]:
    seg_names, seg_maxes, closed = _segments(header)
    if closed >= 2 and len(seg_names) == k:  # 만점 표시가 이름마다 있고 칸 수가 맞을 때만 묶는다
        return seg_names, seg_maxes
    names, maxes = [], []
    for i, t in enumerate(header):
        if _ID_HEAD.match(t) or _PLACE.match(t):
            continue
        m = _MAX_TOKEN.fullmatch(t)
        if m and names:                      # "Total (100)" → 앞 이름의 만점
            maxes[-1] = float(m.group(1))
            continue
        if _NUM.match(t):
            if names and _numbered_name(header, i):
                names[-1] = f"{names[-1]} {t}"   # 'HW 3 /10'
            continue
        mm = _MAX_INLINE.match(t)
        if mm and mm.group("name") and (mm.group("a") or mm.group("b")):
            names.append(mm.group("name").strip())
            maxes.append(float(mm.group("a") or mm.group("b")))
        else:
            names.append(t)
            maxes.append(None)
    if len(names) >= k:
        return names[-k:], maxes[-k:]
    return [f"열 {i + 1}" for i in range(k)], [None] * k


def _official_means(lines: list[str], k: int, rx=_AVG_LINE) -> list | None:
    """점수표 끝의 '평균 (Average) 9.5 20.0 13.0 42.5' 줄(rx=_MAX_ROW면 '만점 20 30 50' 줄) → 열마다 값.
    칸 수가 맞지 않으면 None."""
    for line in reversed(lines):
        if not rx.match(line):
            continue
        nums = [float(m.group(1)) for t in line.split() for m in [_NUM.match(t)] if m and not t.startswith("(")]
        if len(nums) == k:
            return nums
    return None


# ---------------------------------------------------------------- 본체

def parse_score_text(text: str, student_id: str, label: str | None = None,
                     filename: str | None = None) -> ScoreFileResult | None:
    """점수표 글에서 내 행을 찾아 열마다 반 평균·표본표준편차·인원·내 값·내 등수를 만든다.
    내 행(정확한 학번, 또는 보이는 자리가 맞는 가린 학번이 한 행뿐)을 못 찾거나 통계를 못 내면 None.
    한 글에 점수표가 여럿('Lab0 Scores' 표 + 'Lab1 Scores' 표)이면 섞지 않고 label(또는 파일 이름)과 이름이 맞는 표,
    없으면 내 행이 있는 첫 표만 읽고 notes에 알린다. 평가마다 결과(표마다·서로 다른 평가 열마다)는 parse_score_results."""
    sid = _clean_sid(student_id)
    if not text or not sid:
        return None
    text = normalize_ids(text, sid)
    lines = text.splitlines()
    opt = _scan_opts(lines)
    tables = _split_tables(lines, opt)
    if len(tables) <= 1:
        return _parse_table(lines, text, sid, label, filename, opt)
    want = label or label_from_filename(filename)
    results = []
    for a, b, lab in tables:
        r = _parse_table(lines[a:b], "\n".join(lines[a:b]), sid, lab or want, filename, opt)
        if r:
            results.append((lab, r))
    if not results:
        return None
    pick = None
    if want:
        from .extract import same_task
        pick = next((r for lab, r in results if lab and (_label_key(lab) == _label_key(want) or same_task(lab, want))),
                    None)
    pick = pick or results[0][1]
    others = [lab for lab, r in results if r is not pick and lab]
    pick.notes.append(f"PDF에 점수표가 {len(tables)}개 — '{pick.label or '첫'}' 표만 읽음"
                      + (f"(다른 표: {', '.join(others)})" if others else ""))
    return pick


def parse_score_results(text: str, student_id: str, label: str | None = None,
                        filename: str | None = None, ambiguous: bool = False) -> list[ScoreFileResult]:
    """평가마다 결과 하나씩(내 행이 있는 것만, 글 순서).
    - 표가 여럿('Lab0 Scores' 표 + 'Lab1 Scores' 표)이면 표마다.
    - 한 표 안의 열이 **한 평가의 세부**(합계 열이 있거나, 다른 열 만점의 합이 한 열의 만점 — 'Prelab(20) Report(30) Total(50)')면
      그 표의 결과 하나(대표 = 합계 열, 이름 = 표 제목·label·파일 이름) — parse_score_text와 같다.
    - 열마다 **서로 다른 평가**(열 이름마다 평가 낱말 quiz·midterm·중간·과제·lab… 이 있고 합계 열이 없음 — 'Quiz 1 (10) | Midterm (100)')면
      열마다 결과(label = column = 열 이름, 번호 포함). 내 칸이 빈 평가는 뺀다(notes에 알림).
    - 가린 학번이 여러 줄과 맞으면 ambiguous 결과 하나(score None, 반 집계만 — ⑤). 표가 없고 내 학번만 있는 개인 성적표면 personal 결과(③).
    결과가 둘 이상이면 key가 결과마다 다르다(표 이름 또는 열 이름) — 성적 후보 key 'pdf:<파일 key>#<key>'용. 하나뿐이면 key None."""
    sid = _clean_sid(student_id)
    if not text or not sid:
        return []
    text = normalize_ids(text, sid)
    lines = text.splitlines()
    opt = _scan_opts(lines)
    tables = _split_tables(lines, opt) or [(0, len(lines), None)]
    want = label or label_from_filename(filename)
    parsed = []
    for i, (a, b, lab) in enumerate(tables):
        seg = lines[a:b]
        r = _parse_table(seg, "\n".join(seg), sid, lab or want, filename, opt, ambiguous_ok=ambiguous)
        if r is not None:
            parsed.append((r, lab or r.label or f"표 {i + 1}", lab if len(tables) > 1 else None))
    out = _split_results(parsed, len(tables), f"PDF에 점수표가 {len(tables)}개 — 표마다 따로 읽음")
    if not out:
        mine = _personal_result(text, sid, label, filename)
        if mine is not None:
            out = [mine]
    return out


def _split_results(parsed: list, n_tables: int, multi_note: str) -> list[ScoreFileResult]:
    """표마다 읽은 결과 → 평가마다 결과(PDF·표 파일 공통). parsed = [(결과, 세부 표일 때 key, 열마다일 때 key 앞머리)].
    열마다 다른 평가인 표는 열마다 결과(_per_column), 한 평가의 세부면 그 표 하나. 결과가 하나면 key None, 겹치면 ' (2)'.
    내 줄을 못 정한 표(ambiguous)는 열로 나누지 않고 하나로 둔다."""
    out: list[ScoreFileResult] = []
    for r, key, prefix in parsed:
        if n_tables > 1:
            r.notes.append(multi_note)
        if r.ambiguous is None and _table_kind(r.columns) == "separate":
            out.extend(_per_column(r, prefix))
        else:
            r.key = key
            out.append(r)
    if len(out) == 1:
        out[0].key = None
    else:
        seen: Counter = Counter()
        for r in out:
            seen[r.key] += 1
            if seen[r.key] > 1:
                r.key = f"{r.key} ({seen[r.key]})"
    return out


# 합계 열 이름(세부 점수의 합) — 'final'·'score'처럼 평가 이름이기도 한 말은 넣지 않는다
_TOTAL_STRICT = re.compile(r"(?i)(?<![a-z])(?:total|sum|subtotal|overall)(?![a-z])|합계|총점|총계|소계|총합|최종\s*점수|환산\s*점수")


def _table_kind(cols: list[dict]) -> str:
    """'parts'(한 평가의 세부 점수) | 'separate'(열마다 다른 평가). 애매하면 parts(예전 동작)."""
    if len(cols) < 2 or any(_TOTAL_STRICT.search(c["name"]) for c in cols):
        return "parts"
    from .links import families
    fams = [families(c["name"]) for c in cols]
    mx = [c["max_score"] for c in cols]
    if all(m is not None for m in mx):
        for j in range(len(cols)):
            others = [m for i, m in enumerate(mx) if i != j]
            union = set().union(*[f for i, f in enumerate(fams) if i != j])
            # 'Grade (50)' = 10 + 25 + 15, 'HW (20)' = HW 1 + HW 2 → 세부. 'Midterm (20)' = Quiz 1 + Quiz 2는 우연(다른 평가 낱말)
            if len(others) >= 2 and abs(sum(others) - mx[j]) < 1e-6 and fams[j] <= union:
                return "parts"
    return "separate" if all(fams) else "parts"


def _sentence(label: str | None, prim: dict, multi: bool, ambiguous: dict | None = None) -> str:
    mx = f"/{prim['max_score']:g}" if prim["max_score"] else ""
    if prim["mine"] is not None:
        mine = f"내 점수 {prim['mine']:g}{mx}"
    elif ambiguous:
        mine = f"내 줄을 정하지 못함(가린 학번이 {ambiguous['matches']}줄과 맞음)"
    elif prim.get("absent"):
        mine = f"내 점수 없음('{prim['absent']}' — 0점 아님)"
    else:
        mine = "내 점수 없음(빈 칸)"
    parts = [f"{label or '점수표'}" + (f" '{prim['name']}' 열" if multi else ""), mine]
    if prim["mean"] is not None:
        parts.append(f"평균 {prim['mean']:.1f}")
    if prim["sd"] is not None:
        parts.append(f"표준편차 {prim['sd']:.1f}")
    if prim["n"]:
        parts.append(f"{prim['n']}명" + (f" 중 {prim['rank']}등" if prim["rank"] else ""))
    return " · ".join(parts)


def _per_column(r: ScoreFileResult, table_label: str | None) -> list[ScoreFileResult]:
    names = [c["name"] for c in r.columns]
    base = [n for n in r.notes if not n.startswith("다른 열")]
    base.append(f"한 표에 평가가 {len(names)}개({', '.join(names)}) — 열마다 따로 읽음")
    empty = [c["name"] + (f"('{c['absent']}' — 0점 아님)" if c.get("absent") else "") for c in r.columns if c["mine"] is None]
    if empty:
        base.append(f"내 칸이 비어 있어 뺀 평가: {', '.join(empty)}")
    out = []
    for c in r.columns:
        if c["mine"] is None:
            continue
        out.append(ScoreFileResult(label=c["name"], column=c["name"], score=c["mine"], max_score=c["max_score"],
                                   mean=c["mean"], sd=c["sd"], n=c["n"], rank=c["rank"], high=c["high"], low=c["low"],
                                   columns=[dict(c)], sentence=_sentence(c["name"], c, False), notes=list(base),
                                   key=f"{table_label} {c['name']}" if table_label else c["name"]))
    return out


def _parse_table(lines: list[str], text: str, sid: str, label: str | None,
                 filename: str | None, opt: _Opt = _PLAIN, ambiguous_ok: bool = False) -> ScoreFileResult | None:
    rows, first = _line_rows(lines, opt)
    notes = ["표준편차는 표본표준편차(n−1로 나눔)", "다른 학생의 행·학번은 결과에 넣지 않음(평균 같은 집계만)"]
    if len(rows) < 3:
        stream = _stream_rows(text, opt)
        if len(stream) > len(rows):
            rows, first = stream, None
            notes.append("PDF 글의 칸이 줄마다 끊겨 있어 학번 기준으로 행을 다시 만듦 — 확인 필요")
    if not rows:
        return None
    # 같은 행이 두 번(페이지 머리·다시 공지 등) → 하나로
    uniq, seen = [], set()
    for tok, vals in rows:
        key = (tok.lower(), tuple(vals))
        if key not in seen:
            seen.add(key)
            uniq.append((tok, vals))
    if len(uniq) < len(rows):
        notes.append(f"똑같은 행 {len(rows) - len(uniq)}개는 한 번만 셈")
    k = Counter(len(v) for _, v in uniq).most_common(1)[0][0]
    good = [(t, v) for t, v in uniq if len(v) == k]
    if len(good) < len(uniq):
        notes.append(f"칸 수가 다른 행 {len(uniq) - len(good)}개는 통계에서 뺌")
    mine = [v for t, v in good if t.lower() == sid.lower()]
    ambiguous = None
    if not mine:
        if any(t.lower() == sid.lower() for t, _ in uniq):
            return None  # 내 행의 칸 수가 달라 열을 맞출 수 없다
        masked = [v for t, v in good if _masked_match(t, sid)]
        if len(masked) > 1 and ambiguous_ok:   # 조용히 버리지 않고 '애매함'으로(⑤) — 맞은 줄의 값은 넣지 않는다
            ambiguous = {"kind": "masked_multi", "matches": len(masked),
                         "reason": f"가린 학번이 {len(masked)}줄과 맞아 어느 줄이 내 것인지 정하지 못했어요"
                                   "(보이는 자리가 같은 학생이 있어요 — 직접 넣거나 AI로 찾기)"}
            mine = [[None] * k]
        elif len(masked) != 1:
            return None   # 없거나 여러 행과 맞음 → 본인을 고르지 않는다
        else:
            mine = masked
            notes.append("가린 학번과 보이는 자리가 맞는 행 1개를 본인으로 봄 — 확인 필요")
    elif len(mine) > 1:
        notes.append("학번이 같은 행이 여러 개 — 첫 번째 행 사용")
    my = mine[0]
    header = _header(lines, first)
    if not opt.pct and any(_PCT.match(t) for ln in lines for t in ln.split()):   # 백분율 칸은 값에서 빠졌으니 이름도 뺀다
        header = [t for t in header if not _PCT_HEAD.match(t)]
    names, maxes = _columns(header, k)
    max_row = _official_means(lines, k, _MAX_ROW)   # '만점 20 30 50' 행
    if max_row:
        maxes = [m if m is not None else x for m, x in zip(maxes, max_row)]
    gm = None
    for m in _MAX_GLOBAL.finditer(text[:3000]):
        gm = float(next(g for g in m.groups() if g))
        break
    cols, col_at, letter = [], [], None   # col_at[i] = cols[i]가 행의 몇 번째 칸인지(빈 칸·등수 칸을 건너뛰어도 공식 평균과 맞춘다)
    for j in range(k):
        values = [v[j] for _, v in good if _is_val(v[j])]
        if isinstance(my[j], _Letter) and letter is None:
            letter = str(my[j])   # 학점 글자 열(⑧) — 점수가 아니다
        if not values or _RANK_HEAD.match(names[j]) or _GPA_HEAD.match(names[j]):
            continue   # 등수 칸은 점수가 아니다(클수록 나쁨), 평점(4.5)도 점수가 아니다
        mx = maxes[j]
        if mx is None and all(isinstance(v, _Frac) for v in values):   # '8/10'만 있는 열 → 분모가 만점(④)
            dens = {v.of for v in values if v.of}
            mx = dens.pop() if len(dens) == 1 else None
        cols.append(_col_stats(names[j], values, my[j], mx))
        col_at.append(j)
    if not cols or max(c["n"] for c in cols) < 2:
        return None
    official = _official_means(lines, k)
    if official:
        off = [j for j, c in zip(col_at, cols) if official[j] is not None and abs(official[j] - c["mean"]) > 0.05]
        for j, c in zip(col_at, cols):
            if official[j] is not None:
                c["mean_computed"], c["mean"] = c["mean"], official[j]
        notes.append("평균은 PDF에 적힌 값(표준편차·등수는 읽은 행으로 계산)")
        if off:
            notes.append("PDF에 적힌 평균과 계산한 평균이 다름 — 일부 행을 못 읽었을 수 있음(표준편차·등수 확인 필요)")
    if letter:
        notes.append(f"학점 글자 열은 점수가 아니라 따로 적음(내 학점 {letter})")
    r = _table_result(cols, notes, len(good), label, filename, gm, stated=_stated_stats(lines), ambiguous=ambiguous)
    r.letter = letter
    return r


def _col_stats(name: str, values: list, me, max_score) -> dict:
    """열 하나의 집계(PDF·표 파일 공통): 내 값·평균·표본표준편차·인원·내 등수(1 = 가장 높음)·최고·최저·만점.
    내 칸이 결시·미응시 표시(_Absent)면 mine None + "absent"(0점이 아님 — ⑦)."""
    vals = [float(v) for v in values]
    mine = float(me) if _is_val(me) else None
    out = {"name": name, "mine": mine, "mean": round(statistics.fmean(vals), 3),
           "sd": round(statistics.stdev(vals), 3) if len(vals) >= 2 else None, "n": len(vals),
           "rank": (1 + sum(x > mine for x in vals)) if mine is not None else None,
           "high": max(vals), "low": min(vals), "max_score": max_score}
    if isinstance(me, _Absent):
        out["absent"] = str(me)
    return out


def _table_result(cols: list[dict], notes: list[str], n_rows: int, label: str | None, filename: str | None,
                  gm: float | None, stated: dict | None = None, ambiguous: dict | None = None) -> ScoreFileResult:
    """열 집계 → 결과 하나(PDF·표 파일 공통): 대표 열 = 합계·점수 낱말이 있는 마지막 열(없으면 마지막 열),
    대표 열에 만점이 없으면 글 전체의 '만점 N점'(gm). stated = 글에 적힌 평균·표준편차(①, _stated_stats)."""
    if any(c["n"] < n_rows for c in cols):
        notes.append("빈 칸·결시는 통계에서 뺌")
    prim = next((c for c in reversed(cols) if _TOTAL_HEAD.search(c["name"])), cols[-1])
    if prim["max_score"] is None and gm is not None:
        prim["max_score"] = gm
    label = label or label_from_filename(filename)
    if stated:
        _apply_stated(prim, cols, stated, notes)
    others = [c for c in cols if c is not prim and c["mine"] is not None]
    if others:
        notes.append("다른 열: " + ", ".join(f"{c['name']} 내 {c['mine']:g} / 평균 {c['mean']:.1f}" for c in others[:5]))
    absent = prim.get("absent") if prim["mine"] is None and not ambiguous else None
    if absent:
        notes.append(f"내 칸이 '{absent}'(으)로 적혀 있음 — 0점으로 보지 않음(점수 없음)")
    if ambiguous:
        notes.append(ambiguous["reason"])
    return ScoreFileResult(label=label, column=prim["name"], score=prim["mine"], max_score=prim["max_score"],
                           mean=prim["mean"], sd=prim["sd"], n=prim["n"], rank=None if ambiguous else prim["rank"],
                           high=prim["high"], low=prim["low"], columns=cols,
                           sentence=_sentence(label, prim, len(cols) > 1, ambiguous), notes=notes,
                           ambiguous=ambiguous, absent=absent)


# ---------------------------------------------------------------- ① 글에 적힌 평균·표준편차

_STAT_FIELDS = (
    ("mean", r"평균\s*(?:값|점수|점)?|(?<![a-z])(?:mean|average|avg\.?)(?![a-z])"),
    ("median", r"중간\s*값|중앙\s*값|중위수|(?<![a-z])median(?![a-z])"),
    ("sd", r"표준\s*편차|(?<![a-z])(?:s\.d\.?|sd|std\.?\s*dev\.?|stdev|std\.?|standard\s+deviation)(?![a-z])|σ"),
    ("high", r"최고\s*(?:점수|점)?|최댓값|최대값|(?<![a-z])(?:max(?:imum)?|highest)(?![a-z])"),
    ("low", r"최저\s*(?:점수|점)?|최솟값|최소값|(?<![a-z])(?:min(?:imum)?|lowest)(?![a-z])"),
    ("n", r"응시\s*(?:자|인원)(?:\s*수)?|수강\s*(?:인원|생\s*수)|학생\s*수|인원|(?<![a-z])(?:count|students)(?![a-z])"),
)
_STAT_RX = re.compile("|".join(f"(?P<{f}>{rx})" for f, rx in _STAT_FIELDS), re.I)
_STAT_NUM = r"[-+]?\d{1,4}(?:\.\d+)?"
_STAT_PAIR = re.compile(rf"(?:{'|'.join(rx for _, rx in _STAT_FIELDS)})\s*(?:\([^()\d]{{0,20}}\))?\s*[:：=]?\s*"
                        rf"(?P<v>{_STAT_NUM})(?![\d.])(?:\s*(?:점|명|pts?))?", re.I)
# 통계 줄에 함께 있어도 되는 말(그 밖의 낱말이 있으면 — 'Midterm mean 70' — 어느 평가 것인지 몰라 쓰지 않는다)
_STAT_FILL = re.compile(r"(?i)[\s,;:：=·|/()\[\]~-]+|반|전체|학급|과목|분포|통계|(?<![a-z])(?:class|overall|statistics?|stats?)(?![a-z])")
_STAT_VALUE_LINE = re.compile(rf"^(?:{_STAT_NUM}(?:\s*(?:점|명|pts?))?\s*)+$")


def _stat_field(m: re.Match) -> str | None:
    return next((f for f, _ in _STAT_FIELDS if m.group(f)), None)


def _only_fill(s: str) -> bool:
    return not _STAT_FILL.sub("", s).strip()


def _stated_stats(lines: list[str]) -> dict | None:
    """글에 적힌 반 통계: '평균값 중간값 표준편차' 머리글 줄 다음 줄의 '63.4 69 21.5', 또는 한 줄의 'Mean: 6.9, SD: 1.7'·
    '반 평균: 72.4 표준편차: 10.3'(통계 낱말 + 수 말고 다른 말이 없는 줄만). → {"mean", "median", "sd", "high", "low", "n"} 중 찾은 것 | None."""
    found: dict = {}
    for i, raw in enumerate(lines[:3000]):
        line = raw.strip()
        if not line or any(_is_id(t) for t in line.split()):
            continue
        heads = list(_STAT_RX.finditer(line))
        if not heads:
            continue
        if len(heads) >= 2 and _only_fill(_STAT_RX.sub(" ", line)) and not re.search(r"\d", line):
            fields: list = []
            for m in heads:
                f = _stat_field(m)
                if f and (not fields or fields[-1] != f):   # '평균 (Mean)'은 한 칸
                    fields.append(f)
            for nxt in lines[i + 1:i + 3]:
                s = nxt.strip()
                if not s:
                    continue
                if _STAT_VALUE_LINE.match(s):
                    vals = [float(x) for x in re.findall(_STAT_NUM, s)]
                    if len(vals) == len(fields):
                        for f, v in zip(fields, vals):
                            found.setdefault(f, v)
                break
            continue
        pairs = list(_STAT_PAIR.finditer(line))
        if pairs and _only_fill(_STAT_PAIR.sub(" ", line)):
            for m in pairs:
                f = _stat_field(_STAT_RX.match(m.group(0)))
                if f:
                    found.setdefault(f, float(m.group("v")))
    if found.get("sd") is not None and found["sd"] < 0:
        found.pop("sd")
    return found or None


def _apply_stated(prim: dict, cols: list[dict], stated: dict, notes: list[str]) -> None:
    """적힌 평균·표준편차를 대표 열에(계산한 값은 mean_computed·sd_computed). 열마다 다른 평가인 표이거나 값이 점수 범위 밖이면 쓰지 않는다."""
    m, s = stated.get("mean"), stated.get("sd")
    if m is None and s is None:
        return
    if len(cols) > 1 and _table_kind(cols) == "separate":
        notes.append("글에 적힌 평균·표준편차가 어느 평가 것인지 몰라 쓰지 않음(열마다 읽은 행으로 계산)")
        return
    lo, hi = prim["low"], prim["high"]
    if (m is not None and not lo - 1e-9 <= m <= hi + 1e-9) or (s is not None and not 0 <= s <= max(hi - lo, 0) + 1e-9):
        notes.append("글에 적힌 평균·표준편차가 이 표의 점수 범위와 맞지 않아 쓰지 않음(읽은 행으로 계산)")
        return
    shown, calc = [], []
    if m is not None:
        prim.setdefault("mean_computed", prim["mean"])
        prim["mean"] = m
        shown.append(f"평균 {m:g}")
        calc.append(f"평균 {prim['mean_computed']:.1f}")
    if s is not None:
        prim.setdefault("sd_computed", prim["sd"])
        prim["sd"] = s
        shown.append(f"표준편차 {s:g}")
        if prim["sd_computed"] is not None:
            calc.append(f"표준편차 {prim['sd_computed']:.1f}")
    if stated.get("median") is not None:
        prim["median"] = stated["median"]
    notes.append(f"{'·'.join(shown)}은(는) 글에 적힌 값 — 읽은 {prim['n']}행으로 계산하면 {'·'.join(calc)}")
    if stated.get("n") and int(stated["n"]) != prim["n"]:
        notes.append(f"글에 적힌 인원 {stated['n']:g}명과 읽은 행 {prim['n']}개가 다름 — 일부 행을 못 읽었을 수 있음")


# ---------------------------------------------------------------- ③ 개인 성적표(표 없이 내 학번 하나)

_PERSONAL_SCORE = re.compile(
    r"(?i)(?<![A-Za-z가-힣])(?P<w>total(?:\s*score)?|총\s*점|합\s*계|최종\s*점수|환산\s*점수|your\s+score|my\s+score|"
    r"본인\s*점수|내\s*점수|취득\s*점수|점수|score|득점)"
    r"\s*(?:\([^()\d]{0,15}\))?\s*[:：=]?\s*(?P<v>\d{1,4}(?:\.\d+)?)(?![\d.])\s*(?:점|pts?|points?)?"
    r"(?:\s*(?:/|out\s+of)\s*(?P<m>\d{1,4}(?:\.\d+)?)(?![\d.])(?:\s*점)?|\s*\(\s*(?P<m2>\d{1,4})\s*점?\s*만점\s*\))?")
_STRONG_TOTAL = re.compile(r"(?i)total|총\s*점|합\s*계|최종|환산")
_ID_LABELED = re.compile(r"(?i)(?:학번|수험\s*번호|student\s*(?:id|no\.?|number)|(?<![a-z])id)\s*[:：#]?\s*"
                         r"(?P<id>[A-Za-z0-9*#●○■□＊]{6,12})(?![A-Za-z0-9])")


def _name_parts(filename: str | None) -> list[str]:
    return [p for p in re.split(r"[^A-Za-z0-9*#●○■□＊]+", Path(filename).stem) if p] if filename else []


def _personal_shape(filename: str | None, text: str) -> bool:
    """개인 성적표 모양(학번 모르고): '총점: 87'·'Total 87/100' 같은 점수 줄 + (파일 이름에 학번 같은 조각, 또는 글에 '학번: …')."""
    if not text or not _PERSONAL_SCORE.search(text):
        return False
    return any(_is_id(p) for p in _name_parts(filename)) or any(_is_id(m.group("id")) for m in _ID_LABELED.finditer(text))


def _personal_result(text: str, sid: str, label: str | None, filename: str | None) -> ScoreFileResult | None:
    """개인 성적표(③): 내 학번이 글(칸 하나로)이나 파일 이름 조각에 있고, 같은 모양의 다른 학번이 없고, 점수 줄이 있을 때."""
    low = sid.lower()
    toks = [t.strip(".,:;()[]") for t in text.split()]
    in_text = any(t.lower() == low for t in toks)
    in_name = any(p.lower() == low for p in _name_parts(filename))
    if not (in_text or in_name):
        return None
    if any(_is_id(t) and t.lower() != low and len(t) == len(sid) for t in toks):
        return None   # 다른 학생의 학번이 있다 — 개인 성적표가 아니다
    best = None
    for line in text.splitlines():
        if line.strip().lower().startswith(("평균", "mean", "average", "표준편차")):
            continue
        for m in _PERSONAL_SCORE.finditer(line):
            v = float(m.group("v"))
            mx = m.group("m") or m.group("m2")
            mx = float(mx) if mx else None
            if mx is not None and (v > mx or mx <= 0):
                continue
            rank = (bool(_STRONG_TOTAL.search(m.group("w"))), mx is not None)
            if best is None or rank > best[0]:
                best = (rank, v, mx, re.sub(r"\s+", " ", m.group("w")).strip())
    if best is None:
        return None
    _, v, mx, word = best
    if mx is None:
        for g in _MAX_GLOBAL.finditer(text[:3000]):
            mx = float(next(x for x in g.groups() if x))
            break
    stated = _stated_stats(text.splitlines()) or {}
    mean, sd = stated.get("mean"), stated.get("sd")
    if mx is not None and mean is not None and not 0 <= mean <= mx:
        mean = sd = None
    label = label or _label_of(text) or label_from_filename(filename)
    col = {"name": word, "mine": v, "mean": mean, "sd": sd, "n": None, "rank": None,
           "high": stated.get("high"), "low": stated.get("low"), "max_score": mx}
    notes = ["개인 성적표(표 없이 내 학번만 있는 글)로 읽음 — 반 평균·표준편차는 글에 적힌 값만(없으면 비움)"]
    if mean is None:
        notes.append("반 평균이 적혀 있지 않아 상위 % 추정은 못 함")
    parts = [label or "개인 성적표", f"내 점수 {v:g}" + (f"/{mx:g}" if mx else "")]
    if mean is not None:
        parts.append(f"평균 {mean:.1f}")
    if sd is not None:
        parts.append(f"표준편차 {sd:.1f}")
    parts.append("개인 성적표")
    return ScoreFileResult(label=label, column=word, score=v, max_score=mx, mean=mean, sd=sd, n=None, rank=None,
                           high=stated.get("high"), low=stated.get("low"), columns=[col], sentence=" · ".join(parts),
                           notes=notes, personal=True)


# ---------------------------------------------------------------- ⑥ 공지·게시판 본문의 점수표

def body_score_results(html: str | None, student_id: str, title: str | None = None) -> list[ScoreFileResult]:
    """공지·게시판 본문(HTML 표 — <tr>마다 한 줄이 된다 — 또는 붙여 넣은 글)에 든 점수표를 첨부 점수표와 같은 규칙으로(⑥).
    점수표로 보는 것: 학번 + 값인 줄이 3개 이상이고, 제목이나 머리 줄에 점수 낱말(score·점수·성적·결과·총점…)이 있을 때.
    개인 성적표 모양(내 학번 한 줄)은 글 속 점수 문장(grades.find_scores)이 맡으므로 여기서는 내지 않는다."""
    sid = _clean_sid(student_id)
    if not html or not sid:
        return []
    from .extract import html_to_text
    text = html_to_text(html) if "<" in str(html) else str(html)
    text = normalize_ids(text, sid)
    lines = text.splitlines()
    rows = 0
    named = bool(title and _SCORE_NAME.search(title))
    for line in lines[:3000]:
        toks = line.split()
        if sum(_is_id(t) for t in toks) == 1:
            if any(_NUM.match(t) or _FRAC_TOK.match(t) or _PCT.match(t) or _PLACE.match(t) for t in toks if not _is_id(t)):
                rows += 1
        elif not named and (_SCORE_NAME.search(line) or _TOTAL_HEAD.search(line)):
            named = True
    if rows < 3 or not named:
        return []
    want = _label_of(title or "") or (label_from_filename(f"{title}.txt") if title else None)
    return [r for r in parse_score_results(text, sid, label=want, ambiguous=True) if not r.personal]


def result_extras(r) -> dict:
    """성적 후보에 덧붙일 코너 케이스 칸(값이 있을 때만): ambiguous·letter·absent·personal."""
    return {k: getattr(r, k) for k in ("ambiguous", "letter", "absent", "personal") if getattr(r, k, None)}


# ---------------------------------------------------------------- ②⑨ 재검·v2, 학번 구분자

_REV_NUM = re.compile(r"(?i)(?<![a-z0-9])(?:v|ver\.?|version|rev\.?)[ _-]?(\d{1,2})(?!\d)")
_REV_WORD = re.compile(r"(?i)재\s*검|재\s*채점|재\s*공지|수정|정정|변경|업데이트|최신|"
                       r"(?<![a-z])(?:re-?grade[ds]?|regrading|revised|revision|updated|corrected|correction|fixed|new)(?![a-z])")
_REV_PART = re.compile(r"(?i)v\d{1,2}|ver\.?\d{1,2}|rev\.?\d{0,2}|재검(?:후|결과)?|재채점|재공지|수정(?:본|판)?|정정(?:본|판)?|"
                       r"변경|업데이트|최신|revised|revision|updated|corrected|correction|regraded?|regrading|fixed|new")


def revision_of(name: str | None) -> int:
    """파일 이름·글 제목의 다시 낸 표시(②): 'v3' → 3, '재검·재채점·수정본·정정·revised·updated' → 1, 없으면 0.
    같은 평가의 점수가 여럿이면 게시 시각이 같을 때 이 값이 큰 쪽이 새것(gradebook.members_of)."""
    s = str(name or "")
    nums = [int(m.group(1)) for m in _REV_NUM.finditer(s)]
    return max(nums) if nums else (1 if _REV_WORD.search(s) else 0)


def _clean_sid(student_id) -> str:
    return re.sub(r"[\s\-.]+", "", str(student_id or ""))


_IDCH = "0-9A-Za-z*#●○■□＊"
_GROUPED_ID = re.compile(r"(?<![\w\-./])[0-9*#●○■□＊]{2,6}(?:-[0-9*#●○■□＊]{1,6}){1,3}(?![\w\-/])")


def _join_grouped(m: re.Match) -> str:
    s = m.group(0)
    parts = s.split("-")
    if not 8 <= sum(len(p) for p in parts) <= 12 or s.startswith("0") or re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
        return s   # 날짜(2026-11-18)·전화(010-…)·짧은 번호는 그대로
    return "".join(parts)


def normalize_ids(text: str, student_id: str | None = None) -> str:
    """학번 구분자(⑨): '2099-12-3456' → '2099123456'(날짜·전화 모양은 빼고), 내 학번이 '2099 12 3456'·'2099.12.3456'처럼
    적혀 있으면 같은 모양(자리 수)의 다른 학번도 붙인다."""
    if not text:
        return text or ""
    sid = _clean_sid(student_id) if student_id else ""
    if sid and len(sid) >= 6 and sid.isalnum():
        pat = re.compile(r"(?<![\w.\-])" + r"[\-. ]?".join(map(re.escape, sid)) + r"(?![\w\-])", re.I)
        shapes = set()
        for m in pat.finditer(text):
            s = m.group(0)
            sep = re.search(r"[\-. ]", s)
            if sep:
                shapes.add((sep.group(0), tuple(len(g) for g in re.split(r"[\-. ]", s))))
        for sep, lens in shapes:
            rx = re.compile(r"(?<![\w.\-])" + re.escape(sep).join(f"[{_IDCH}]{{{n}}}" for n in lens) + r"(?![\w\-])")
            text = rx.sub(lambda m: re.sub(r"[\-. ]", "", m.group(0)), text)
    return _GROUPED_ID.sub(_join_grouped, text)


# ---------------------------------------------------------------- 표 파일(엑셀 xlsx·xls, CSV) — 계약 25-2
# sheets.read의 모양: [{"name": 시트 이름, "rows": [{열 번호: 글자 | 숫자}]}]. state.json을 거치면 열 번호가 글자("0")가 된다.
# 시트 하나 = 표 하나. 머리글 → 학번 열 → 숫자 열마다 집계 → 그 뒤는 PDF와 같은 규칙(_table_kind → _per_column / 대표 열).

_CELL_NUM = re.compile(r"^\(?([-+]?\d+(?:\.\d+)?)\)?\s*(?:점|pts?)?$")
# 점수가 아닌 열의 머리글(학번·이름·번호·학과·등수·백분율·비고·날짜·연락처) — 칸 전체(또는 괄호를 뗀 것)가 이 말일 때
_SHEET_SKIP_HEAD = re.compile(
    r"^(?:학번|수험\s*번호|student(?:\s*(?:id|no\.?|number|name))?|id|no\.?|num(?:ber)?|번호|순번|연번|#|"
    r"이름|성명|성|명|name|학과|전공|소속|department|dept\.?|major|학년|분반|section|"
    r"등수|순위|석차|rank(?:ing)?|place|%|％|percent(?:age)?|pct|백분율|퍼센트|백분위|"
    r"비고|remarks?|notes?|memo|메모|e-?mail|이메일|전화(?:번호)?|연락처|phone|날짜|일자|일시|date|time|"
    r"제출\s*(?:일|일시|시각)?|submitted(?:\s*at)?)$", re.I)
_ID_WORD = re.compile(r"학번|수험\s*번호|student\s*(?:id|no|number)|^\s*id\s*$", re.I)
_HEAD_MAX = (re.compile(r"^(?P<name>.*?)\s*[(\[]\s*(?P<m>\d{1,4}(?:\.\d+)?)\s*(?:점)?\s*[)\]]$"),  # 'Quiz 1 (10)'·'[20]'
             re.compile(r"^(?P<name>.*?)\s*/\s*(?P<m>\d{1,4}(?:\.\d+)?)$"),                         # 'Quiz1/10'
             # '과제 10점'·'과제10점'·'중간 100점 만점'('Quiz 2 10점'의 2는 이름, '100점'만 있으면 만점으로 보지 않음)
             re.compile(r"^(?P<name>.*?(?:[^\d\s]|\S\s))\s*(?P<m>\d{1,4}(?:\.\d+)?)\s*점(?:\s*만점)?$"))
SHEET_SHAPE_ROWS = 5          # 파일 이름에 점수 낱말이 없을 때 점수표로 볼 최소 행 수(학번 + 숫자)


def _col_letter(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(v):
    """칸 값 → 글자(앞뒤 공백 없음) | 숫자(float) | None(빈 칸·불리언·NaN·무한대)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    s = str(v).strip()
    return s or None


def _num_cell(v) -> float | None:
    if isinstance(v, float):
        return v
    if isinstance(v, str):
        m = _CELL_NUM.match(v)
        return float(m.group(1)) if m else None
    return None


def _id_cell(v) -> str | None:
    """학번처럼 보이는 칸 → 글자. 숫자로 저장된 학번(2023123456.0)은 정수 글자로."""
    if isinstance(v, float):
        if v > 0 and v.is_integer():
            s = str(int(v))
            return s if 6 <= len(s) <= 12 else None
        return None
    if isinstance(v, str):
        s = re.sub(r"\s+", "", v)
        if "-" in s or "." in s:   # '2099-12-3456'(⑨) — 날짜·전화 모양은 아님
            m = re.fullmatch(r"[0-9*#●○■□＊]{2,6}(?:[-.][0-9*#●○■□＊]{1,6}){1,3}", s)
            if m and not re.fullmatch(r"\d{4}[-.]\d{1,2}[-.]\d{1,2}", s) and not s.startswith("0"):
                s = re.sub(r"[-.]", "", s)
        return s if _is_id(s) else None
    return None


def _frac_cell(v) -> tuple[float, float] | None:
    """글자 칸 '8/10'·'8 / 10' → (8, 10), '87%' → (87, 100) — ④. 아니면 None."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    m = re.fullmatch(r"\(?(\d{1,4}(?:\.\d+)?)\s*/\s*(\d{1,4}(?:\.\d+)?)\)?\s*(?:점)?", s)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return (num, den) if den > 0 and num <= den * 1.5 else None
    m = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*[%％]", s)
    return (float(m.group(1)), 100.0) if m else None


def _letter_cell(v) -> bool:
    return isinstance(v, str) and bool(_LETTER_STRONG.match(v.strip()) or _LETTER_WEAK.match(v.strip()))


def _text(v) -> bool:
    return isinstance(v, str) and _num_cell(v) is None


def _lead_text(row: dict) -> str:
    return next((row[c] for c in sorted(row) if _text(row[c])), "")


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _sheet_rows(sheet: dict) -> list[dict]:
    rows = []
    for r in (sheet or {}).get("rows") or []:
        row = {}
        for k, v in (r.items() if isinstance(r, dict) else []):
            try:
                c = int(k)
            except (TypeError, ValueError):
                continue
            v = _cell(v)
            if v is not None and c >= 0:
                row[c] = v
        rows.append(row)
    return rows


def _head_name(raw: str) -> tuple[str, float | None]:
    """머리글 칸 → (이름, 만점). 'Quiz 1 (10)'·'Quiz1/10'·'과제(10점)'·'Prelab (max 20)'·'중간 100점 만점'."""
    s = re.sub(r"\s+", " ", _norm_header(raw)).strip()
    for rx in _HEAD_MAX:
        m = rx.match(s)
        if m and m.group("name").strip():
            return m.group("name").strip(), float(m.group("m"))
    return s, None


def _skip_head(raw: str) -> bool:
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    bare = re.sub(r"\s*[(\[][^)\]]*[)\]]\s*", " ", s).strip()
    return bool(s) and (bool(_SHEET_SKIP_HEAD.match(s)) or bool(bare and _SHEET_SKIP_HEAD.match(bare))
                        or bool(_ID_WORD.search(s)))


def _head_like(row: dict) -> bool:
    """머리글처럼 보이는 행: 글자 칸 2개 이상이고 요약(평균…)·만점 행이 아님."""
    lead = _lead_text(row)
    return sum(_text(v) for v in row.values()) >= 2 and not (_SUMMARY.match(lead) or _MAX_ROW.match(lead))


def _row_sig(row: dict | None) -> tuple:
    return tuple((c, re.sub(r"\s+", "", _fmt(v)).lower()) for c, v in sorted((row or {}).items()))


def _sheet_tables(sheet: dict) -> list[dict]:
    """시트 하나 → 점수표 모양(학번 열 + 점수 열)인 표들. 보통 하나지만, 한 시트에 표가 위아래로 여럿이면
    (학번 행 사이에 다른 머리글이 나오면) 섞지 않고 나눈다 — 같은 머리글이 되풀이되는 것(인쇄용 머리)은 같은 표.
    학번은 보지 않는다(점수표인지 판단·내 행 찾기가 함께 쓴다)."""
    rows = _sheet_rows(sheet)
    found = {i: {c: s for c, v in r.items() for s in [_id_cell(v)] if s} for i, r in enumerate(rows)}
    found = {i: ids for i, ids in found.items() if ids}
    if len(found) < 2:
        return []
    count = Counter(c for ids in found.values() for c in ids)
    id_cols = {c for c, n in count.items() if n >= max(2, 0.5 * len(found))}
    if not id_cols:
        return []
    data_at = [bool(id_cols & set(found.get(i, {}))) for i in range(len(rows))]
    blocks, cur, floor, label = [], None, 0, None          # cur = [머리글 행 | None, 첫 행, 끝(제외)]
    for i, r in enumerate(rows):
        if data_at[i]:
            if cur is None:
                # 머리글 = 첫 학번 행 바로 위쪽(10줄 안, 앞 표 뒤)의 글자 칸 2개 이상인 행 — 그 위 제목 줄은 건너뜀
                hi = next((j for j in range(i - 1, max(floor, i - 11) - 1, -1) if _head_like(rows[j])), None)
                cur = [hi, hi + 1 if hi is not None else i, None]
                label = _label_of(_joined(rows[floor:hi if hi is not None else i]))
            continue
        lead = _lead_text(r)
        if cur is None or _SUMMARY.match(lead) or _MAX_ROW.match(lead):
            continue
        if _head_like(r):   # 다른 머리글('학번 Prelab …'가 아닌 줄) — 같은 머리글의 반복(인쇄용 머리)은 같은 표
            new = _row_sig(r) != _row_sig(rows[cur[0]] if cur[0] is not None else None)
        else:               # 다른 평가 이름의 제목 줄('Lab1 Scores')
            lab = _label_of(_joined([r]))
            new = lab is not None and _label_key(lab) != _label_key(label)
        if new and any(data_at[j] for j in range(i + 1, min(len(rows), i + 12))):
            cur[2] = floor = i                     # 뒤에 학번 행이 또 → 새 표
            blocks.append(cur)
            cur = None
    if cur is not None:
        cur[2] = len(rows)
        blocks.append(cur)
    name = str((sheet or {}).get("name") or "").strip()
    out, prev_end = [], 0
    for hi, start, end in blocks:
        t = _block_table(rows, hi, start, end, rows[prev_end:hi if hi is not None else start], id_cols, count, name)
        prev_end = end
        if t is not None:
            out.append(t)
    return out


def _joined(rs) -> str:
    return "\n".join("\t".join(_fmt(r[c]) for c in sorted(r)) for r in rs if r)


def _label_of(text: str) -> str | None:
    m = _TABLE_LABEL.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def _block_table(rows: list[dict], hi: int | None, start: int, end: int, title_rows: list[dict], id_cols: set,
                 count: Counter, name: str) -> dict | None:
    header = rows[hi] if hi is not None else {}
    idcol = min(id_cols, key=lambda c: (not _ID_WORD.search(_fmt(header.get(c, ""))), -count[c], c))
    data, ids, summary, max_row = [], [], [], None
    for r in rows[start:end]:
        rid = {c: s for c in id_cols for s in [_id_cell(r.get(c))] if s}
        if rid:
            data.append(r)
            ids.append(rid)
            continue
        lead = _lead_text(r)
        if lead and _MAX_ROW.match(lead):
            max_row = r
        elif lead and _SUMMARY.match(lead):
            summary.append((lead, r))
    if len(data) < 2:
        return None
    cols, skipped, letter_cols = [], [], []
    for c in sorted(set(header) | {c for r in data for c in r}):
        if c in id_cols:
            continue
        raw = _fmt(header[c]) if c in header else ""
        vals, texts, fracs = [], 0, []
        for i, r in enumerate(data):
            v = r.get(c)
            x = _num_cell(v)
            vals.append(x)
            if x is None and v is not None and not (isinstance(v, str) and _PLACE.match(v)):
                f = _frac_cell(v)
                if f is not None:
                    fracs.append((i, f))
                texts += 1
        frac_max = None
        if fracs:   # '8/10'·'87%' 칸(④): 날짜 같은 분수('11/18')는 분모가 하나이고 분자가 여럿일 때만 점수로
            dens = {f[1] for _, f in fracs}
            if ((len(dens) == 1 and len({f[0] for _, f in fracs}) >= 2)
                    or all(not _date_like(*f) for _, f in fracs)):
                for i, f in fracs:
                    vals[i] = f[0]
                texts -= len(fracs)
                frac_max = dens.pop() if len(dens) == 1 else None
        nums = [x for x in vals if x is not None]
        filled = [v for v in (r.get(c) for r in data) if v is not None and not (isinstance(v, str) and _PLACE.match(v))]
        n_letters = sum(_letter_cell(v) for v in filled)
        if filled and not nums and n_letters >= max(2, 0.6 * len(filled)) and (
                _LETTER_HEAD.search(" " + raw) or n_letters >= 0.8 * len(filled)):
            letter_cols.append(c)   # 학점 글자 열(⑧) — 점수가 아니다
            continue
        if not nums or len(nums) < texts:
            continue    # 글자 열(이름·비고 등)
        if _skip_head(raw):
            skipped.append(raw)
            continue
        if c < idcol and len(nums) == len(vals) >= 3 and nums == [float(i) for i in range(1, len(nums) + 1)]:
            continue    # 학번 앞의 1, 2, 3 … = 순번
        if min(nums) >= 10000:
            skipped.append(raw or f"열 {_col_letter(c)}")
            continue    # 날짜·연락처·다른 번호(점수가 아님)
        cname, mx = _head_name(raw) if raw else (f"열 {_col_letter(c)}", None)
        if _GPA_HEAD.match(cname):
            skipped.append(raw)
            continue    # 평점(4.5)은 점수가 아니다(⑧)
        if mx is None and max_row is not None:
            mx = _num_cell(max_row.get(c))
        if mx is None and frac_max is not None and len(fracs) == len(nums):
            mx = frac_max
        cols.append({"col": c, "name": cname, "max": mx, "vals": vals})
    if not cols:
        return None
    # 표 이름: 머리글 위 제목 줄의 평가 이름('Quiz 2 결과') → 없으면 시트 이름('Quiz1')
    return {"name": name, "label": _label_of(_joined(title_rows)) or _label_of(name), "ids": ids,
            "cols": cols, "summary": summary, "skipped": skipped, "title": _joined(title_rows + ([header] if header else [])),
            "header": hi is not None, "rows": data, "letter_cols": letter_cols}


def is_score_sheet(sheets: list | None, filename: str | None = None) -> bool:
    """표 파일이 점수표인지: 학번 열 + 점수 열이 있는 시트가 있고, 파일 이름에 score·점수·성적·grade가 있거나
    (없으면) 학번 + 숫자인 행이 SHEET_SHAPE_ROWS개 이상이고 값이 세 가지 이상인 숫자 열이 있을 때.
    일정표·명단(학번·이름·학년)·출석부(O/X, 1/0)는 점수표로 보지 않는다."""
    named = bool(filename and _SCORE_NAME.search(Path(filename).stem))
    for t in (t for sh in sheets or [] if isinstance(sh, dict) for t in _sheet_tables(sh)):
        if named:
            return True
        if len(t["ids"]) >= SHEET_SHAPE_ROWS and any(
                sum(x is not None for x in col["vals"]) >= SHEET_SHAPE_ROWS
                and len({x for x in col["vals"] if x is not None}) >= 3 for col in t["cols"]):
            return True
    return False


def _sheet_result(t: dict, sid: str, label: str | None, filename: str | None,
                  ambiguous_ok: bool = False) -> ScoreFileResult | None:
    notes = ["표준편차는 표본표준편차(n−1로 나눔)", "다른 학생의 행·학번은 결과에 넣지 않음(평균 같은 집계만)"]
    if t["name"]:
        notes.append(f"표 파일의 '{t['name']}' 시트에서 읽음")
    low = sid.lower()
    hits = [i for i, ids in enumerate(t["ids"]) if any(s.lower() == low for s in ids.values())]
    ambiguous = None
    if not hits:
        masked = [i for i, ids in enumerate(t["ids"]) if any(_masked_match(s, sid) for s in ids.values())]
        if len(masked) > 1 and ambiguous_ok:   # 조용히 버리지 않고 '애매함'(⑤) — 맞은 줄의 값은 넣지 않는다
            ambiguous = {"kind": "masked_multi", "matches": len(masked),
                         "reason": f"가린 학번이 {len(masked)}줄과 맞아 어느 줄이 내 것인지 정하지 못했어요"
                                   "(보이는 자리가 같은 학생이 있어요 — 직접 넣거나 AI로 찾기)"}
        elif len(masked) != 1:
            return None   # 없거나 여러 행과 맞음 → 본인을 고르지 않는다
        else:
            hits = masked
            notes.append("가린 학번과 보이는 자리가 맞는 행 1개를 본인으로 봄 — 확인 필요")
    elif len(hits) > 1:
        notes.append("학번이 같은 행이 여러 개 — 첫 번째 행 사용")
    me = hits[0] if hits else None
    data_rows = t.get("rows") or []
    raw_row = data_rows[me] if me is not None and me < len(data_rows) else {}

    def mine_of(col):
        if me is None:
            return None
        x = col["vals"][me]
        if x is None:
            v = raw_row.get(col["col"])
            if isinstance(v, str) and _PLACE.match(v.strip()):
                return _Absent(v.strip())   # 결시·미응시(⑦) — 0점이 아님
        return x
    used = [col for col in t["cols"] if any(x is not None for x in col["vals"])]
    cols = [_col_stats(col["name"], [x for x in col["vals"] if x is not None], mine_of(col), col["max"])
            for col in used]
    if not cols or max(c["n"] for c in cols) < 2:
        return None
    letter = next((str(raw_row[c]).strip() for c in t.get("letter_cols") or [] if _letter_cell(raw_row.get(c))), None)
    if letter:
        notes.append(f"학점 글자 열은 점수가 아니라 따로 적음(내 학점 {letter})")
    if not t["header"]:
        notes.append("머리글 행(글자 칸 2개 이상)을 못 찾아 열 이름을 열 A, B, …로 둠")
    if t["summary"]:
        notes.append(f"요약 행(평균·합계 등) {len(t['summary'])}개는 통계에서 뺌")
    shown = [s for s in t["skipped"] if not re.fullmatch(r"(?i)번호|순번|연번|no\.?|#|num(?:ber)?|학년|분반", s.strip())]
    if shown:   # 늘 빼는 순번·학년은 알리지 않는다
        notes.append("점수가 아닌 숫자 열(등수·백분율·날짜 등)은 뺌: " + ", ".join(shown[:5]))
    avg = next((r for lead, r in t["summary"] if _AVG_LINE.match(lead)), None)
    if avg is not None:
        off = [c["name"] for col, c in zip(used, cols) for o in [_num_cell(avg.get(col["col"]))]
               if o is not None and abs(o - c["mean"]) > (0.5 if o.is_integer() else 0.05) + 1e-9]
        if off:
            notes.append(f"표에 적힌 평균과 계산한 평균이 다른 열: {', '.join(off[:5])} — "
                         "결시·빈 칸을 0점으로 셌을 수 있음(평균은 읽은 행으로 계산)")
    gm = None
    for m in _MAX_GLOBAL.finditer(t["title"][:3000]):
        gm = float(next(g for g in m.groups() if g))
        break
    r = _table_result(cols, notes, len(t["ids"]), label, filename, gm, ambiguous=ambiguous)
    r.letter = letter
    return r


def parse_sheet_results(sheets: list | None, student_id: str, filename: str | None = None,
                        label: str | None = None, ambiguous: bool = False) -> list[ScoreFileResult]:
    """표 파일(sheets.read 결과)에서 평가마다 결과 — parse_score_results와 같은 모양·규칙(계약 25-2).
    시트 하나 = 표 하나(한 시트에 다른 머리글·다른 평가 제목으로 표가 위아래로 여럿이면 나눔): 머리글(첫 학번 행 바로 위의
    글자 칸 2개 이상인 행 — 그 위 제목 줄은 건너뜀), 학번 열(정확히 같은 값, 숫자로 저장된 학번 2023123456.0도 / 보이는 자리가
    맞는 가린 학번이 한 행뿐), 숫자 열마다 집계(만점 = 머리글 '(10)'·'/10'·'10점' 또는 '만점·배점' 행).
    학번·이름·번호·학과·등수·백분율 열, 요약 행(평균·합계·최고·최저…), 결시·빈 칸은 뺀다.
    결과에는 내 값과 반 집계만 들어간다(다른 학생의 행·학번은 넣지 않음)."""
    sid = _clean_sid(student_id)
    if not sheets or not sid:
        return []
    want = label or label_from_filename(filename)
    per_sheet = [_sheet_tables(sh) for sh in sheets if isinstance(sh, dict)]
    tables = [(t, k, len(ts)) for ts in per_sheet for k, t in enumerate(ts, 1)]
    parsed = []
    for t, k, n_in_sheet in tables:
        r = _sheet_result(t, sid, t["label"] or want, filename, ambiguous_ok=ambiguous)
        if r is not None:   # 이름(여럿일 때 key): 표 이름 → 시트 이름(한 시트에 표가 여럿이면 '시트 표 2')
            name = t["label"] or ((f"{t['name']} 표 {k}" if n_in_sheet > 1 else t["name"]) or None)
            parsed.append((r, name or r.label, name if len(tables) > 1 else None))
    return _split_results(parsed, len(tables), f"표 파일에 점수표가 {len(tables)}개(시트·표마다) — 따로 읽음")
