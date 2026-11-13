"""성적 공지 첨부 PDF(학생별 점수표)에서 **내 점수와 반 전체 집계만** 뽑는다.

예: 'Lab0, Lab1 Score Announcement' 공지의 Lab0_score-1.pdf — 한 줄에 한 학생, 학번은 '2026####33'처럼 가려진 경우가 많다.
PDF에서 뽑은 글(materials의 pdf_texts)은 줄이 대략 표의 행을 따르고 칸은 공백으로 나뉜다. 줄이 칸마다 끊긴 글도
학번 자리를 기준으로 다시 행을 만든다.

개인정보: 결과에는 열마다 평균·표본표준편차·인원·최고·최저와 **내 값·내 등수만** 들어간다.
다른 학생의 행·학번·이름은 돌려주지도 저장하지도 않는다(가린 학번이 여러 행과 맞으면 본인을 고르지 않는다).

평가가 하나인지 여럿인지(docs/CORNER_CASES.md 가족 5): 한 PDF에 표가 여럿이면 표마다, 한 표에 서로 다른 평가가 열마다 있으면
('Quiz 1 (10) | Midterm (100)', 합계 열 없음) 열마다 결과를 낸다 — parse_score_results. parse_score_text는 예전처럼 결과 하나.
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
_PLACE = re.compile(r"^(?:-|–|—|/|결시|미응시|미제출|불참|absent|abs\.?|n/?a|na|무)$", re.I)
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


@dataclass
class ScoreFileResult:
    label: str | None                # 'Lab0' (파일 이름에서) 또는 넘겨준 이름
    column: str                      # 대표 열(합계·점수 열, 없으면 마지막 숫자 열)
    score: float | None              # 내 값(대표 열)
    max_score: float | None
    mean: float
    sd: float | None
    n: int
    rank: int | None                 # 대표 열에서 내 등수(1 = 가장 높음)
    high: float | None
    low: float | None
    columns: list = field(default_factory=list)   # [{"name","mine","mean","sd","n","rank","high","low","max_score"}]
    sentence: str = ""               # 한국어 요약(다른 학생 정보 없음)
    notes: list = field(default_factory=list)
    key: str | None = None           # 한 파일에서 결과가 여럿일 때 결과마다 다른 이름(표 이름·열 이름), 하나뿐이면 None


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
    keep = [p for p in keep if not re.fullmatch(r"(?i)announcement|공지|re|재공지|copy|사본|final_?ver", p)]
    return " ".join(keep).strip() or None


def is_score_file(filename: str | None, text: str | None) -> bool:
    """점수표 같아 보이는지: 파일 이름에 score·점수·성적·grade가 있거나, 학번 + 숫자인 줄이 여럿."""
    if filename and _SCORE_NAME.search(Path(filename).stem):
        return True
    rows = 0
    for line in (text or "").splitlines()[:2000]:
        toks = line.split()
        if any(_is_id(t) for t in toks) and any(_NUM.match(t) for t in toks if not _is_id(t)):
            rows += 1
            if rows >= 5:
                return True
    return False


def _masked_match(tok: str, sid: str) -> bool:
    if len(tok) != len(sid) or not any(ch in _MASK for ch in tok):
        return False
    visible = [(a, b) for a, b in zip(tok, sid) if a not in _MASK]
    return len(visible) >= len(sid) / 2 and all(a.lower() == b.lower() for a, b in visible)


# ---------------------------------------------------------------- 행 만들기

def _values(tokens: list[str], id_at: int) -> list:
    vals = []
    for j, t in enumerate(tokens):
        if j == id_at:
            continue
        if j < id_at and re.fullmatch(r"\d{1,4}", t):   # 학번 앞의 작은 정수 = 순번
            continue
        if _PCT.match(t) or _DATE.match(t):
            continue
        m = _NUM.match(t)
        if m:
            vals.append(float(m.group(1)))
        elif _PLACE.match(t):
            vals.append(None)
    return vals


def _line_rows(lines: list[str]) -> tuple[list[tuple[str, list]], int | None]:
    rows, first = [], None
    for i, line in enumerate(lines):
        toks = line.split()
        if not toks or _SUMMARY.match(line.strip()):
            continue
        ids = [j for j, t in enumerate(toks) if _is_id(t)]
        if len(ids) != 1:
            continue
        vals = _values(toks, ids[0])
        if not vals:
            continue
        rows.append((toks[ids[0]], vals))
        if first is None:
            first = i
    return rows, first


def _stream_rows(text: str) -> list[tuple[str, list]]:
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
        vals = _values([toks[i]] + seg, 0)
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


def _split_tables(lines: list[str]) -> list[tuple[int, int, str | None]]:
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
        elif len(ids) == 1 and _values(toks, ids[0]):
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
    sid = re.sub(r"\s+", "", str(student_id or ""))
    if not text or not sid:
        return None
    lines = text.splitlines()
    tables = _split_tables(lines)
    if len(tables) <= 1:
        return _parse_table(lines, text, sid, label, filename)
    want = label or label_from_filename(filename)
    results = []
    for a, b, lab in tables:
        r = _parse_table(lines[a:b], "\n".join(lines[a:b]), sid, lab or want, filename)
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
                        filename: str | None = None) -> list[ScoreFileResult]:
    """평가마다 결과 하나씩(내 행이 있는 것만, 글 순서).
    - 표가 여럿('Lab0 Scores' 표 + 'Lab1 Scores' 표)이면 표마다.
    - 한 표 안의 열이 **한 평가의 세부**(합계 열이 있거나, 다른 열 만점의 합이 한 열의 만점 — 'Prelab(20) Report(30) Total(50)')면
      그 표의 결과 하나(대표 = 합계 열, 이름 = 표 제목·label·파일 이름) — parse_score_text와 같다.
    - 열마다 **서로 다른 평가**(열 이름마다 평가 낱말 quiz·midterm·중간·과제·lab… 이 있고 합계 열이 없음 — 'Quiz 1 (10) | Midterm (100)')면
      열마다 결과(label = column = 열 이름, 번호 포함). 내 칸이 빈 평가는 뺀다(notes에 알림).
    결과가 둘 이상이면 key가 결과마다 다르다(표 이름 또는 열 이름) — 성적 후보 key 'pdf:<파일 key>#<key>'용. 하나뿐이면 key None."""
    sid = re.sub(r"\s+", "", str(student_id or ""))
    if not text or not sid:
        return []
    lines = text.splitlines()
    tables = _split_tables(lines) or [(0, len(lines), None)]
    want = label or label_from_filename(filename)
    out: list[ScoreFileResult] = []
    for i, (a, b, lab) in enumerate(tables):
        seg = lines[a:b]
        r = _parse_table(seg, "\n".join(seg), sid, lab or want, filename)
        if r is None:
            continue
        if len(tables) > 1:
            r.notes.append(f"PDF에 점수표가 {len(tables)}개 — 표마다 따로 읽음")
        if _table_kind(r.columns) == "separate":
            out.extend(_per_column(r, lab if len(tables) > 1 else None))
        else:
            r.key = lab or r.label or f"표 {i + 1}"
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


def _sentence(label: str | None, prim: dict, multi: bool) -> str:
    mx = f"/{prim['max_score']:g}" if prim["max_score"] else ""
    parts = [f"{label or '점수표'}" + (f" '{prim['name']}' 열" if multi else ""),
             f"내 점수 {prim['mine']:g}{mx}" if prim["mine"] is not None else "내 점수 없음(빈 칸)",
             f"평균 {prim['mean']:.1f}"]
    if prim["sd"] is not None:
        parts.append(f"표준편차 {prim['sd']:.1f}")
    parts.append(f"{prim['n']}명" + (f" 중 {prim['rank']}등" if prim["rank"] else ""))
    return " · ".join(parts)


def _per_column(r: ScoreFileResult, table_label: str | None) -> list[ScoreFileResult]:
    names = [c["name"] for c in r.columns]
    base = [n for n in r.notes if not n.startswith("다른 열")]
    base.append(f"한 표에 평가가 {len(names)}개({', '.join(names)}) — 열마다 따로 읽음")
    empty = [c["name"] for c in r.columns if c["mine"] is None]
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
                 filename: str | None) -> ScoreFileResult | None:
    rows, first = _line_rows(lines)
    notes = ["표준편차는 표본표준편차(n−1로 나눔)", "다른 학생의 행·학번은 결과에 넣지 않음(평균 같은 집계만)"]
    if len(rows) < 3:
        stream = _stream_rows(text)
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
    if not mine:
        if any(t.lower() == sid.lower() for t, _ in uniq):
            return None  # 내 행의 칸 수가 달라 열을 맞출 수 없다
        masked = [v for t, v in good if _masked_match(t, sid)]
        if len(masked) != 1:
            return None   # 없거나 여러 행과 맞음 → 본인을 고르지 않는다
        mine = masked
        notes.append("가린 학번과 보이는 자리가 맞는 행 1개를 본인으로 봄 — 확인 필요")
    elif len(mine) > 1:
        notes.append("학번이 같은 행이 여러 개 — 첫 번째 행 사용")
    my = mine[0]
    header = _header(lines, first)
    if any(_PCT.match(t) for ln in lines for t in ln.split()):   # 백분율 칸은 값에서 빠졌으니 이름도 뺀다
        header = [t for t in header if not _PCT_HEAD.match(t)]
    names, maxes = _columns(header, k)
    max_row = _official_means(lines, k, _MAX_ROW)   # '만점 20 30 50' 행
    if max_row:
        maxes = [m if m is not None else x for m, x in zip(maxes, max_row)]
    gm = None
    for m in _MAX_GLOBAL.finditer(text[:3000]):
        gm = float(next(g for g in m.groups() if g))
        break
    cols, col_at = [], []     # col_at[i] = cols[i]가 행의 몇 번째 칸인지(빈 칸·등수 칸을 건너뛰어도 공식 평균과 맞춘다)
    for j in range(k):
        values = [v[j] for _, v in good if v[j] is not None]
        if not values or _RANK_HEAD.match(names[j]):
            continue   # 등수 칸은 점수가 아니다(클수록 나쁨)
        me = my[j]
        cols.append({"name": names[j], "mine": me, "mean": round(statistics.fmean(values), 3),
                     "sd": round(statistics.stdev(values), 3) if len(values) >= 2 else None, "n": len(values),
                     "rank": (1 + sum(x > me for x in values)) if me is not None else None,
                     "high": max(values), "low": min(values), "max_score": maxes[j]})
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
    if any(c["n"] < len(good) for c in cols):
        notes.append("빈 칸·결시는 통계에서 뺌")
    prim = next((c for c in reversed(cols) if _TOTAL_HEAD.search(c["name"])), cols[-1])
    if prim["max_score"] is None and gm is not None:
        prim["max_score"] = gm
    label = label or label_from_filename(filename)
    others = [c for c in cols if c is not prim and c["mine"] is not None]
    if others:
        notes.append("다른 열: " + ", ".join(f"{c['name']} 내 {c['mine']:g} / 평균 {c['mean']:.1f}" for c in others[:5]))
    return ScoreFileResult(label=label, column=prim["name"], score=prim["mine"], max_score=prim["max_score"],
                           mean=prim["mean"], sd=prim["sd"], n=prim["n"], rank=prim["rank"],
                           high=prim["high"], low=prim["low"], columns=cols, sentence=_sentence(label, prim, len(cols) > 1),
                           notes=notes)
