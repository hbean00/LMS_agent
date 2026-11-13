"""학점 비율(상대평가)·점수 기준표(절대평가)를 강의계획서·공지·게시판·PDF 글에서 규칙으로 찾는다. 계약 16장.

원칙(사용자): 특정 과목·문장에 맞추지 않고 **일반 구조**로 읽는다. 다른 학생의 어떤 과목이 와도 같은 규칙이 돈다.

1) 학점 낱말: A+ A0 A A- B+ … D0 D F, 'A학점'·'A등급'·'A grades'·"A's". 'Part A'·'유형 A'·'A형'·'C++'·'%B0' 같은 것은 학점이 아니다.
   맨 글자 A는 같은 글에 A+가 따로 적혀 있으면 A0, 아니면 A+·A0 묶음(밴드)이다. A-는 A0로 센다.
2) 묶음: 목록(A+,A · A+/A0 · A+·A0 · A+ 및 A0 · A (A+, A0) · 'A+B'), 범위(A+~A0 · C+~F · C+부터 F까지),
   누적(B 이상 · B or above · '누적' · 'A+B ≤ 70%'처럼 맨 위부터), 꼬리(C 이하 · C or below), 나머지(나머지 · 그 외 · the rest).
3) 값: 단일(30%) · 범위(40~50% · 40-50% · 40 to 50% → 가운데 값, ranges에 그대로) · 상한(30% 이내/이하/까지 · 최대 30% ·
   ≤30% · up to 30% · no more than 30% → 상한값) · 하한 · '상위 30%'(맨 위부터 누적 위치) · '각각'(묶음 안 학점마다).
4) 묶음과 값을 식 'C(아래 경계까지 누적 %) − C(위 경계 앞) = 값'으로 바꿔 9학점 비율을 푼다: 누적은 차이로, 적힌 묶음 안은
   고르게, 글에 없는 구간은 기본 비율(grades.DEFAULT_DISTRIBUTION) 모양으로 나눈다. 합이 100이 아니면 범위 안에서 맞추고
   (안 되면 비례로) 메모를 남긴다.
5) 점수 단위(점·points), '90 ≤ A0 < 95', 학점 순서대로 내려가는 기준값(합이 100을 크게 넘음: 90·80·70 …)은 절대평가
   기준표(cutoffs, 100점 기준 최소 점수). 'A: 90-100'처럼 묶음만 적히면 A+/A0 경계는 가운데로 둔다.
6) 버리는 것: 평가 비중(중간 30% — 학점 낱말이 없음), 출석 기준('66% 이상 출석', '5회 이상 결석 시 F'), 사람 수(A+ 3명),
   날짜·시각·분수, 글 속 일반 퍼센트('30% of instructions'), 근거(학점 표기·'학점/grade' 낱말) 없는 맨 글자.

확신도: high = 묶음 2개 이상 + 학점 근거 + 9학점을 글이 다 덮고 합이 100(맞춤·추정 없음) · 절대평가는 기준 3개 이상 + 점수 단위/
'이상'/'절대평가' + 가운데 가정 없음. medium = 일부를 기본 모양으로 채움·범위 안에서 맞춤·% 표시 없음·기준 2개.
low = 묶음 하나·서로 어긋남·비례로 맞춤 — 자동으로 넣지 않는다(gradebook).
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import asdict, dataclass, field, replace

from .grades import DEFAULT_DISTRIBUTION, GRADES, validate_distribution

_IDX = {g: i for i, g in enumerate(GRADES)}
_LAST = len(GRADES) - 1   # F
_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class DistFound:
    kind: str                  # "relative" | "absolute"
    dist: dict | None          # relative: {9학점: %}(합 100) — absolute면 None
    ranges: dict               # {묶음: [lo, hi]} — 글에 적힌 범위·상한·하한 그대로(가운데 값을 쓰기 전)
    cutoffs: dict | None       # absolute: {학점: 100점 기준 최소 점수} — relative면 None
    sentence: str              # 근거(원문 줄을 " / "로 이음)
    confidence: str            # "high" | "medium" | "low"
    notes: list = field(default_factory=list)
    groups: list = field(default_factory=list)   # [{"label", "grades", "pct", "range", "bound", "cumulative", "text"}]

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 글 다듬기

_FW = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}   # 전각 ASCII → 반각(％ ～ ： Ａ …)
_FW.update({0x3000: " ", 0xA0: " ", 0x2009: " ", 0x202F: " ", 0x2002: " ", 0x2003: " ",
            ord("〜"): "~", ord("∼"): "~", ord("–"): "-", ord("—"): "-", ord("−"): "-", ord("‐"): "-",
            ord("‑"): "-", ord("≦"): "≤", ord("≧"): "≥", ord("⩽"): "≤", ord("⩾"): "≥",
            ord("ㆍ"): "·", ord("・"): "·", ord("･"): "·", ord("∙"): "·"})

_TABLE = re.compile(r"(?is)<table\b.*?</table\s*>")
_ROW = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>")
_CELL = re.compile(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>")
_BLOCK = re.compile(r"(?i)<\s*/?\s*(?:br|p|div|li|ul|ol|tr|table|tbody|thead|h[1-6]|section|article|blockquote|dt|dd|"
                    r"pre|hr)\b[^>]*>")
_TAGS = re.compile(r"<[^>]*>")
_NUMCELL = re.compile(r"^\d{1,4}(?:\.\d+)?(?:\s*[~\-]\s*\d{1,4}(?:\.\d+)?)?$")
_PCT_HEAD = re.compile(r"%|비율|비중|퍼센트|percent|ratio|share|분포|distribution", re.I)
_SCORE_HEAD = re.compile(r"점수|score|points?|cut\s*-?\s*off|기준\s*점|구간", re.I)


def _strip(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", s)
    return _html.unescape(_TAGS.sub(" ", _BLOCK.sub("\n", s)))


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", _strip(s)).strip()


def _table_text(tb: str) -> str:
    """HTML 표 → 칸을 ' | '로 이은 줄. 머리글이 %·비율이면 숫자만 있는 칸에 %를, 점수·기준이면 '점'을 붙인다.
    [학점 | 90 | 94]처럼 숫자 칸이 둘이면 범위 하나(90~94)로."""
    rows = [[_cell(c) for c in _CELL.findall(r)] for r in _ROW.findall(tb)]
    rows = [r for r in rows if any(r)]
    head = " ".join(c for r in rows for c in r if not re.search(r"\d", c))
    pct, score = bool(_PCT_HEAD.search(head)), bool(_SCORE_HEAD.search(head))
    unit = "%" if pct and not score else "점" if score and not pct else ""
    out = []
    for r in rows:
        cells = list(r)
        if (len(cells) == 3 and re.fullmatch(r"\d{1,4}(?:\.\d+)?", cells[1])
                and re.fullmatch(r"\d{1,4}(?:\.\d+)?", cells[2]) and not re.search(r"\d", cells[0])):
            cells = [cells[0], f"{cells[1]}~{cells[2]}"]
        if unit:
            cells = [c + unit if _NUMCELL.match(c) else c for c in cells]
        out.append(" | ".join(cells))
    return "\n".join(out)


def _html_text(s: str) -> str:
    parts, pos = [], 0
    for tm in _TABLE.finditer(s):
        parts.append(_strip(s[pos:tm.start()]))
        parts.append("\n" + _table_text(tm.group(0)) + "\n")
        pos = tm.end()
    parts.append(_strip(s[pos:]))
    return "".join(parts)


_GTOKEN = re.compile(r"^[A-DF](?:\+|0|-)?(?:[,/·~&\-][A-DF](?:\+|0|-)?)*$")
_VTOKEN = re.compile(r"^(?:[<>≤≥]=?|~)?\d{1,4}(?:\.\d+)?(?:[~\-]\d{1,4}(?:\.\d+)?)?(?:%|점)?$")
_GROUP_ONLY = re.compile(r"[(\[]?\s*[A-DF](?:\+|0|-)?(?:\s*(?:[,/·~&\-]|및|to)\s*[A-DF](?:\+|0|-)?)*\s*"
                         r"(?:학점|등급|grades?)?\s*(?:이상|이하|\(누적\)|or\s+(?:above|below|higher|lower))?\s*[)\]]?\s*[:=|]?",
                         re.I)
_VALUE_LINE = re.compile(r"\s*(?:[:=|]\s*)?(?:상위|top|최대|최소|max|min|up\s+to|≤|<=|≥|>=|<|>|~)?\s*\d{1,4}(?:\.\d+)?\s*"
                         r"(?:%|점|퍼센트|percent|[~\-]|이상|이하|이내|미만|$)", re.I)


def _cells(line: str) -> list[str]:
    s = re.sub(r"(\d)\s*([~\-])\s*(\d)", r"\1\2\3", line)
    s = re.sub(r"(\d)\s+(%|점)", r"\1\2", s)
    parts = re.split(r"\s*\|\s*|\t+|\s{2,}", s.strip().strip("|"))
    if len(parts) <= 2:
        parts = s.strip().strip("|").split()
    return [p.strip().strip("|").strip() for p in parts if p.strip().strip("|").strip()]


def _transpose(a: str, b: str) -> str | None:
    """가로 표(PDF·HTML 공통): 학점 줄 'A+ A0 B+ …' + 값 줄 '15 15 20 …'(앞에 이름 칸 하나 허용) → 'A+: 15%, A0: 15%, …'."""
    ga, gb = _cells(a), _cells(b)
    la = ga[1:] if ga and not _GTOKEN.match(ga[0]) else ga
    lb = gb[1:] if gb and not _VTOKEN.match(gb[0]) else gb
    if not (len(la) >= 3 and len(la) == len(lb) and all(_GTOKEN.match(x) for x in la)
            and all(_VTOKEN.match(x) for x in lb)):
        return None
    unit = ""
    if not any(x.endswith(("%", "점")) for x in lb):
        head = (ga[0] if la is not ga else "") + " " + (gb[0] if lb is not gb else "")
        unit = "%" if _PCT_HEAD.search(head) else "점" if _SCORE_HEAD.search(head) else ""
    return ", ".join(f"{g}: {v}{unit}" for g, v in zip(la, lb))


def _lines(text: str, is_html: bool) -> list[tuple[str, str]]:
    """(읽을 줄, 근거로 보일 줄). PDF의 칸마다 한 줄('A+,A:' / '40~50%')·가로 표는 한 줄로 잇는다."""
    raw = _html_text(text) if is_html else _html.unescape(text)
    raw = raw.translate(_FW)
    lines = [ln for ln in (re.sub(r"[ \t\r\f\v]+", " ", x).strip() for x in raw.split("\n")) if ln]
    out, i = [], 0
    while i < len(lines):
        a = lines[i]
        b = lines[i + 1] if i + 1 < len(lines) else ""
        if b:
            t = _transpose(a, b)
            if t:
                out.append((t, f"{a} / {b}"))
                i += 2
                continue
            if _GROUP_ONLY.fullmatch(a) and _VALUE_LINE.match(b) and not _atoms(b):
                out.append((f"{a} {b}", f"{a} {b}"))
                i += 2
                continue
        out.append((a, a))
        i += 1
    return out


# ---------------------------------------------------------------- 학점 낱말(atom)

_ATOMRX = re.compile(r"(?<![A-Za-z0-9%#_@\\])(?<![A-Za-z]\.)(?<![A-Za-z0-9]-)(?P<L>[A-DF])"
                     r"(?P<S>\+|0(?![\d.])|-(?![\dA-DF+]))?(?![A-Za-z0-9+#_])")
# 학점 낱말 앞에 와도 되는 영어 낱말('Part A'·'Class B'·'Type C'는 학점이 아니다)
_EN_OK = {"grade", "grades", "letter", "letters", "a", "an", "the", "of", "to", "get", "gets", "got", "receive",
          "receives", "received", "earn", "earns", "earned", "and", "or", "nor", "with", "for", "as", "is", "are", "be",
          "than", "at", "from", "between", "through", "thru", "above", "below", "under", "over", "max", "maximum",
          "min", "minimum", "up", "no", "not", "more", "less", "only", "top", "by", "within", "plus", "either",
          "both", "into", "vs", "versus", "give", "given", "assign", "assigned", "award", "awarded", "will", "may",
          "can", "cumulative", "approx", "about", "around", "rest", "next", "then", "while", "if", "e", "g", "i"}
_KO_BAD = re.compile(r"(?:과제|숙제|문제|퀴즈|시험|고사|팀|조|분반|유형|실험|실습|레포트|리포트|보고서|프로젝트|파트|섹션|"
                     r"그룹|타입|형|반|구역|동|관|플랜|옵션|방식|버전|모델|건물|강의실|호|층|사이즈|용지|사례|케이스)$")
_KO_OK = re.compile(r"(?:학점|등급|이상|이하|이내|미만|초과|까지|부터|에서|및|와|과|은|는|이|가|의|를|을|에|로|으로|만|도|"
                    r"급|그룹|군|누적|비율|인원|학생|구간|범위|각각|각)")


@dataclass
class _Atom:
    letter: str
    suffix: str | None
    start: int
    end: int


# 'A+15'·'D+0'(띄어 쓰지 않음)
_ATOM_TIGHT = re.compile(r"(?<![A-Za-z0-9%#_@\\])(?<![A-Za-z]\.)(?<![A-Za-z0-9]-)(?P<L>[A-DF])(?P<S>\+)(?=\d)")


def _atoms(s: str) -> list[_Atom]:
    out = []
    found = sorted(list(_ATOMRX.finditer(s)) + list(_ATOM_TIGHT.finditer(s)), key=lambda m: m.start())
    for m in found:
        pre, post = s[:m.start()], s[m.end():]
        w = re.search(r"([A-Za-z]+)[ \t]*$", pre)
        if w and w.group(1).lower() not in _EN_OK:
            continue
        k = re.search(r"([가-힣]+)[ \t]*$", pre)
        if k and _KO_BAD.search(k.group(1)):
            continue
        if re.match(r"[가-힣]", post) and not _KO_OK.match(post):
            continue          # A형·A조·C언어
        if re.match(r"['’][A-Za-z]", post) and not re.match(r"['’]s(?![A-Za-z])", post):
            continue          # D'Angelo
        out.append(_Atom(m.group("L"), m.group("S"), m.start(), m.end()))
    return out


# ---------------------------------------------------------------- 값

_NUM = r"\d{1,4}(?:\.\d+)?"
_UNIT = r"(?:%|퍼센트|percent(?:age)?(?![a-z])|pct(?![a-z])|점|pts?(?![a-z])\.?|points?(?![a-z]))"
_PRE_UP = (r"최대|최고|max(?:imum)?\.?(?![a-z])|up\s+to|no\s+more\s+than|not\s+more\s+than|not\s+(?:to\s+)?exceed(?:ing)?|"
           r"at\s+most|within|under|less\s+than|below|≤|<=|=<|<|~")
_PRE_LO = r"최소|최저|min(?:imum)?\.?(?![a-z])|at\s+least|no\s+less\s+than|more\s+than|over|above|≥|>=|=>|>"
_PRE_AP = r"약|대략|about|approx(?:imately)?\.?|around|roughly"
_POST_UP = (r"이내|이하|까지|미만|max(?:imum)?(?![a-z])|or\s+(?:less|fewer|lower|below|under)|at\s+most")
_POST_LO = r"이상|초과|or\s+(?:more|higher|above|greater|better|over)|and\s+(?:above|up|over|higher)|at\s+least|minimum|\+(?![\d+])"
_POST_AP = r"정도|내외|가량|안팎|쯤"
_VALRX = re.compile(
    rf"\s*(?P<pos>상위|top(?![a-z]))?\s*(?:(?P<pup>{_PRE_UP})|(?P<plo>{_PRE_LO})|(?P<pap>{_PRE_AP}))?\s*"
    rf"(?<![\d.A-Za-z])(?P<a>{_NUM})(?!\d)(?P<ua>\s*{_UNIT})?"
    rf"(?:\s*(?P<amid>이상|초과)\s*(?:(?:~|-|to|에서|부터)\s*)?(?P<b1>{_NUM})(?!\d)(?P<ub1>\s*{_UNIT})?\s*(?P<bp1>미만|이하|까지)"
    rf"|\s*(?:~|-|to(?![a-z])|에서|부터)\s*(?P<b2>{_NUM})(?!\d)(?P<ub2>\s*{_UNIT})?(?:\s*(?P<bp2>미만|이하|까지))?)?"
    rf"(?:\s*(?:(?P<qup>{_POST_UP})|(?P<qlo>{_POST_LO})|(?P<qap>{_POST_AP})))?", re.I)
# 숫자 뒤에 오면 비율·점수가 아니다(사람 수·횟수·날짜·학점 수·분수·시각)
_BAD_AFTER = re.compile(r"\s*(?:명|인(?![가-힣])|people|persons?|students?|회|번(?!째)|주(?!의)|일(?!반)|시간|시(?![가-힣])|"
                        r"분(?!포|반)|초(?!과)|개(?!인)|년|월|학년|학기|교시|호|층|쪽|pages?|학점|credits?|[./:]\d|/)", re.I)
_REST_WORD = r"나머지|그\s*외|그\s*밖|기타|the\s+rest|rest|all\s+others|others?|remaining|remainder"
_REST_VAL = re.compile(rf"\s*(?:{_REST_WORD})(?:\s*(?:전부|모두|all))?(?:은|는|이|가|을|를|의|도)?(?![가-힣A-Za-z])(?!\s*\d)",
                       re.I)
_REST_GROUP = re.compile(rf"(?<![가-힣A-Za-z])(?:{_REST_WORD})(?![가-힣A-Za-z])", re.I)
_LE = re.compile(r"\s*(?:≤|<=|=<|<)\s*$")


@dataclass
class _V:
    start: int
    end: int
    lo: float | None = None
    hi: float | None = None
    val: float | None = None
    bound: str = "exact"     # exact | approx | range | upper | lower | rest
    unit: str = "bare"       # pct | score | bare
    pos: bool = False        # '상위 30%' — 맨 위부터의 누적 위치
    ineq: bool = False       # '90 ≤ A0' 꼴
    text: str = ""


def _unit_of(*us) -> str:
    u = next((x.strip().lower() for x in us if x and x.strip()), "")
    if not u:
        return "bare"
    return "pct" if u.startswith(("%", "퍼센트", "percent", "pct")) else "score"


def _value_at(s: str, p: int, limit: int | None = None) -> _V | None:
    limit = len(s) if limit is None else limit
    r = _REST_VAL.match(s, p)
    if r and r.end() <= limit:
        return _V(p, r.end(), bound="rest", text=s[p:r.end()].strip())
    m = _VALRX.match(s, p)
    if not m or m.end() > limit:
        return None
    marked = any(m.group(k) for k in ("ua", "ub1", "ub2", "qup", "qlo", "pup", "plo", "pos"))
    if not marked and _BAD_AFTER.match(s, m.end()):
        return None
    a = float(m.group("a"))
    b = m.group("b1") or m.group("b2")
    v = _V(m.start("pos") if m.group("pos") else m.start(), m.end(), unit=_unit_of(m.group("ua"), m.group("ub1"),
                                                                                    m.group("ub2")),
           pos=bool(m.group("pos")), text=s[m.start():m.end()].strip())
    if b is not None:
        lo, hi = sorted((a, float(b)))
        v.lo, v.hi, v.val, v.bound = lo, hi, (lo + hi) / 2, "range"
    elif m.group("pup") or m.group("qup"):
        v.lo, v.hi, v.val, v.bound = 0.0, a, a, "upper"
    elif m.group("plo") or m.group("qlo"):
        v.lo, v.hi, v.val, v.bound = a, None, a, "lower"
    else:
        v.lo = v.hi = v.val = a
        v.bound = "approx" if (m.group("pap") or m.group("qap")) else "exact"
    if v.unit == "pct" and (v.hi or v.val or 0) > 100:
        return None
    return v


_LEFT_TAIL = re.compile(r"\s*(?:[:=|\-→>]|은|는|이면|면|일\s*때|=>|까지(?:는|의)?|에(?:게|는)?|의)?\s*"
                        r"(?:학점|등급)?\s*[:=]?\s*$")


def _left_value(s: str, lo: int, hi: int) -> _V | None:
    """묶음 바로 앞의 값('95점 이상 A+', '90 ≤ A0', '95~100: A+', '상위 30%: A'). 맨 숫자 하나는 값으로 보지 않는다(목록 번호)."""
    seg = s[lo:hi]
    le = _LE.search(seg)
    body = seg[:le.start()] if le else seg
    for st in range(len(body)):
        ch = body[st]
        if not (ch.isdigit() or ch in "<>≤≥~" or ch.isalpha()):
            continue
        if st > 0 and (body[st - 1].isdigit() or body[st - 1] == "." or (ch.isalpha() and body[st - 1].isalpha())):
            continue
        v = _value_at(body, st)
        if v is None:
            continue
        rest = body[v.end:]
        if le:
            if rest.strip():
                continue
            v.bound, v.ineq, v.lo, v.hi = "lower", True, v.val, None
        elif not _LEFT_TAIL.fullmatch(rest):
            continue
        elif v.bound == "exact" and v.unit == "bare" and not v.pos:
            return None
        v.start, v.end = v.start + lo, v.end + lo
        return v
    return None


# ---------------------------------------------------------------- 묶음(group)과 문장(statement)

_SEP_LIST = re.compile(r"\s*(?:,|/|·|&|\+|및|와|과|and|or|그리고|또는)\s*|\s*(?:학점|등급)?\s*\(\s*|\s*,\s*(?:and|or)\s*",
                       re.I)
_SEP_RANGE = re.compile(r"\s*(?:~|-|to|through|thru|부터|에서|→)\s*", re.I)
_MODRX = re.compile(
    r"\s*(?:(?P<band>학점|등급|grades?(?![a-z])|'s(?![a-z])|급(?![가-힣])|그룹|군(?![가-힣]))"
    r"|(?P<cum>이상(?:의)?|or\s+(?:above|higher|better)|and\s+(?:above|higher|better|up))"
    r"|(?P<tail>이하(?:의)?|or\s+(?:below|lower|worse)|and\s+(?:below|lower|under))"
    r"|(?P<cumw>\(\s*(?:누적|cumulative)\s*\)|누적(?:으로)?|cumulative(?:ly)?)"
    r"|(?P<each>각각|각(?=\s)|each|respectively)"
    r"|(?P<till>까지)"
    r"|(?P<punct>[:=|()\[\]]|-(?!\s*\d)|→)"
    r"|(?P<part>은|는|이(?!상|하|내)|가|의|을|를|에(?:는|게|서)?|로|으로))", re.I)
_FILLER = re.compile(r"[^\d%,;|.!?()\[\]\n<>≤≥=~:/]{1,24}?(?=(?:상위|top|최대|최소|최고|최저|max|min|up\s+to|no\s+more|"
                     r"not\s+more|at\s+most|at\s+least|within|under|less\s+than|more\s+than|약|about|approx|around|"
                     r"≤|<|>|≥|~)?\s*\d)", re.I)
_ATTEND = re.compile(r"출석|결석|출결|참석|지각|attendance|attend|absen|participat", re.I)


@dataclass
class _St:
    atoms: list
    rng: bool
    v: _V
    line: int
    text: str
    cum: bool = False
    tail: bool = False
    each: bool = False
    band: bool = False
    rest_group: bool = False
    i: int = -1
    j: int = -1
    oi: int = -1
    oj: int = -1


def _merge(s: str, atoms: list[_Atom]) -> list[dict]:
    groups, cur, rng = [], [], False
    for a in atoms:
        if cur:
            gap = s[cur[-1].end:a.start]
            if _SEP_RANGE.fullmatch(gap):
                cur.append(a)
                rng = True
                continue
            if _SEP_LIST.fullmatch(gap):
                cur.append(a)
                continue
            groups.append({"atoms": cur, "rng": rng, "start": cur[0].start, "end": cur[-1].end})
        cur, rng = [a], False
    if cur:
        groups.append({"atoms": cur, "rng": rng, "start": cur[0].start, "end": cur[-1].end})
    return groups


def _mods(s: str, p: int, limit: int) -> tuple[int, dict]:
    flags = {"cum": False, "tail": False, "each": False, "band": False}
    for _ in range(10):
        m = _MODRX.match(s, p)
        if not m or m.end() > limit or m.end() == p:
            break
        if m.group("band"):
            flags["band"] = True
        if m.group("cum") or m.group("cumw"):
            flags["cum"] = True
        if m.group("tail"):
            flags["tail"] = True
        if m.group("each"):
            flags["each"] = True
        p = m.end()
    return p, flags


def _right_value(s: str, p: int, limit: int) -> _V | None:
    v = _value_at(s, p, limit)
    if v is None:
        f = _FILLER.match(s, p)
        if f and f.end() < limit and not _ATTEND.search(f.group(0)):
            v = _value_at(s, _mods(s, f.end(), limit)[0], limit)
    if v is not None and v.end > limit:
        return None
    return v


def _line_statements(line: str, li: int) -> list[_St]:
    # 'A+B'(맨 글자 + 맨 글자) = A와 B를 합친 것('A+B ≤ 70%')
    s = re.sub(r"(?<![A-Za-z0-9])([A-DF])\+(?=[A-DF](?![+0A-Za-z0-9]))", r"\1&", line)
    atoms = _atoms(s)
    groups = _merge(s, atoms) if atoms else []
    real = list(groups)
    for m in _REST_GROUP.finditer(s):
        if any(g["start"] <= m.start() < g["end"] for g in real):
            continue
        before = [g for g in real if g["end"] <= m.start()]
        if before and not s[_mods(s, before[-1]["end"], m.start())[0]:m.start()].strip():
            continue   # 'C 이하: 나머지' — 앞 묶음의 값이다
        after = [g for g in real if g["start"] >= m.end()]
        if after and re.fullmatch(r"(?:은|는|이|가|을|를|의|도)?\s*[:=]?\s*", s[m.end():after[0]["start"]]):
            continue   # '나머지는 C+ 이하' — 뒤 묶음의 값이다
        groups.append({"atoms": [], "rng": False, "start": m.start(), "end": m.end(), "rest": True})
    if not groups:
        return []
    groups.sort(key=lambda g: g["start"])
    left_mode = bool(real) and _left_value(s, 0, real[0]["start"]) is not None
    out, prev = [], 0
    for k, g in enumerate(groups):
        nxt = groups[k + 1]["start"] if k + 1 < len(groups) else len(s)
        p, flags = _mods(s, g["end"], nxt)
        v = None
        if g.get("rest"):
            v = _right_value(s, p, nxt)
            if v is None or v.bound == "rest":
                continue
        elif left_mode:
            v = _left_value(s, prev, g["start"])
            if v is not None:   # '90 ≤ A0 < 95'의 오른쪽 '< 95'
                r = re.match(rf"\s*(?:<|≤|<=|미만)\s*(?P<b>{_NUM})", s[p:nxt])
                if r and v.bound == "lower":
                    v.hi, v.bound, v.end = float(r.group("b")), "range", p + r.end()
                    v.val = (v.lo + v.hi) / 2
                    if v.ineq:
                        v.val = v.lo
        else:
            v = _right_value(s, p, nxt)
            if v is None:
                v = _left_value(s, prev, g["start"])
        if v is None:
            prev = g["end"]
            continue
        out.append(_St(atoms=g["atoms"], rng=g["rng"], v=v, line=li, text=s[g["start"]:g["end"]],
                       cum=flags["cum"], tail=flags["tail"], each=flags["each"], band=flags["band"],
                       rest_group=bool(g.get("rest"))))
        prev = max(g["end"], v.end)
    att = _ATTEND.search(s)
    solo = [x for x in out if not x.rest_group]
    if att and len(solo) <= 1 and (not solo or all(a.letter == "F" for a in solo[0].atoms)
                                   or att.start() < solo[0].v.end):
        return []   # '출석률 70% 미만 F' · '5회 이상 결석 시 F' (뒤에 붙은 '(attendance required)'는 괜찮다)
    return out


# ---------------------------------------------------------------- 묶음 → 학점 구간

def _label(i: int, j: int) -> str:
    gs = GRADES[i:j + 1]
    return gs[0] if len(gs) == 1 else "·".join(gs) if len(gs) == 2 else f"{gs[0]}~{gs[-1]}"


def _resolve_sets(sts: list[_St], notes: list[str]) -> list[_St]:
    """맨 글자(A)는 같은 글의 다른 묶음에 A+가 있으면 A0, 아니면 A+·A0. 범위는 위 끝~아래 끝. 누적은 맨 위부터, 꼬리는 F까지."""
    explicit = [{f"{a.letter}{'+' if a.suffix == '+' else '0'}" for a in st.atoms if a.suffix in ("+", "0")}
                for st in sts]
    minus = False
    out = []
    for k, st in enumerate(sts):
        if st.rest_group:
            out.append(st)
            continue
        others = set().union(*(e for m, e in enumerate(explicit) if m != k)) if len(sts) > 1 else set()

        def grades_of(a: _Atom) -> list[str]:
            nonlocal minus
            if a.letter == "F":
                return ["F"]
            if a.suffix == "+":
                return [a.letter + "+"]
            if a.suffix == "0":
                return [a.letter + "0"]
            if a.suffix == "-":
                minus = True
                return [a.letter + "0"]
            band = [a.letter + "+", a.letter + "0"]
            return [x for x in band if x not in others] or band

        if st.rng:
            top = min(_IDX[x] for x in grades_of(st.atoms[0]))
            bot = max(_IDX[x] for x in grades_of(st.atoms[-1]))
            idx = list(range(min(top, bot), max(top, bot) + 1))
        else:
            idx = sorted({_IDX[x] for a in st.atoms for x in grades_of(a)})
            if idx != list(range(idx[0], idx[-1] + 1)):
                notes.append(f"이어지지 않는 학점 묶음({st.text})은 뺌")
                continue
        st.oi, st.oj = idx[0], idx[-1]
        st.i = 0 if st.cum else st.oi
        st.j = _LAST if st.tail else st.oj
        out.append(st)
    if minus:
        notes.append("마이너스 학점(A- 등)은 A0처럼 셈")
    return out


# ---------------------------------------------------------------- 상대평가 풀기

def _nominal(v: _V) -> float | None:
    return None if v.bound == "rest" else v.val


def _fill(grades: list[str], total: float, fixed: dict) -> dict:
    """구간 total을 나눈다: 고정된 학점(글에 따로 적힌 것)은 그대로, 나머지는 기본 비율 모양(다 0이면 고르게)."""
    rest = [x for x in grades if x not in fixed]
    left = total - sum(fixed.values())
    w = [float(DEFAULT_DISTRIBUTION.get(x, 0)) for x in rest]
    if sum(w) <= 0:
        w = [1.0] * len(rest)
    out = dict(fixed)
    for x, wi in zip(rest, w):
        out[x] = left * wi / sum(w)
    return out


def _adjust(cons: list, notes: list[str]) -> str | None:
    """구간이 겹치지 않고 9학점을 다 덮는 묶음들의 합이 100이 아니면: 범위 안에서 폭에 비례해 맞추고, 안 되면 None."""
    total = sum(c["v"] for c in cons)
    d = 100.0 - total
    if abs(d) <= 0.5:
        return "ok"
    slack = [(c["hi"] - c["v"]) if d > 0 else (c["v"] - c["lo"]) for c in cons]
    if sum(slack) + 1e-9 < abs(d):
        return None
    for c, sl in zip(cons, slack):
        c["v"] += d * sl / sum(slack) if sum(slack) > 0 else 0
    notes.append(f"묶음 비율 합이 {total:g}%라 적힌 범위 안에서 100%로 맞춤")
    return "adjusted"


def _round100(dist: dict) -> dict:
    """소수 둘째 자리로 반올림하되 합이 정확히 100이 되게(남는 0.01은 반올림에서 가장 많이 깎인 학점에)."""
    cents = {x: max(0.0, float(dist.get(x, 0.0))) * 100 for x in GRADES}
    base = {x: int(cents[x] + 1e-6) for x in GRADES}
    left = 10000 - sum(base.values())
    order = sorted(GRADES, key=lambda x: -(cents[x] - base[x]))
    for x in (order * 2)[:abs(left)] if left else []:
        base[x] += 1 if left > 0 else -1
    return {x: round(base[x] / 100, 2) for x in GRADES}


def _merge_same(sts: list[_St], notes: list[str]) -> list[_St]:
    """같은 학점 묶음이 여러 번 적힘(한·영 병기, 'A'와 'A-'가 둘 다 A0): 값이 같으면 하나로, 다르면 더한 합이 100에
    더 가까울 때만 더한다(아니면 앞의 것)."""
    plain = [st for st in sts if not st.rest_group and not st.cum and not st.v.pos and st.v.bound != "rest"]
    by: dict = {}
    for st in plain:
        by.setdefault((st.i, st.j), []).append(st)
    dups = {k: v for k, v in by.items() if len(v) > 1}
    if not dups:
        return sts
    first = sum(v[0].v.val for v in by.values())
    summed = sum(sum(x.v.val for x in v) for v in by.values())
    drop = {id(x) for v in dups.values() for x in v[1:]}
    if all(len({x.v.val for x in v}) == 1 for v in dups.values()):
        notes.append("같은 학점 묶음이 두 번 적혀 하나로 셈")
    elif abs(summed - 100) < abs(first - 100):
        for v in dups.values():
            lo = sum(x.v.lo or 0.0 for x in v)
            hi = sum(x.v.hi if x.v.hi is not None else x.v.val for x in v)
            exact = all(x.v.bound in ("exact", "approx") for x in v)
            v[0].v = replace(v[0].v, val=sum(x.v.val for x in v), lo=lo, hi=hi, bound="exact" if exact else "range")
            notes.append(f"같은 학점에 해당하는 묶음({', '.join(x.text for x in v)})의 비율을 더함")
    else:
        notes.append("같은 학점 묶음에 서로 맞지 않는 비율이 적혀 앞의 것을 씀 — 확인 필요")
    return [st for st in sts if id(st) not in drop]


def _relative(sts: list[_St], notes: list[str]) -> tuple[dict, list, dict, bool, bool] | None:
    """→ (9학점 비율, 묶음 목록, ranges, 다 덮었는지, 맞춤·비례가 있었는지)."""
    sts = _merge_same(sts, notes)
    cons, ranges, groups_out = [], {}, []
    for st in sts:
        if st.rest_group:
            continue
        v = st.v
        k = (st.oj - st.oi + 1) if st.cum else (st.j - st.i + 1)
        val, lo, hi = _nominal(v), v.lo, v.hi
        if st.each and val is not None and k > 1:
            val, lo, hi = val * k, (lo or 0) * k, (hi * k if hi is not None else None)
            notes.append(f"'{st.text}' 각각 {v.val:g}% → 묶음 {val:g}%")
        if v.pos and val is not None:
            if v.bound == "range":
                top = v.lo - 1 if (v.lo > 1 and v.lo % 10 == 1) else v.lo
                cons.append({"a": -1, "b": st.oi - 1, "v": top, "st": st, "pos": True, "lo": top, "hi": top})
                cons.append({"a": -1, "b": st.oj, "v": v.hi, "st": st, "pos": True, "lo": v.hi, "hi": v.hi})
            else:
                cons.append({"a": -1, "b": st.oj, "v": val, "st": st, "pos": True, "lo": lo, "hi": hi})
            continue
        hi_eff = hi if hi is not None else 100.0
        cons.append({"a": st.i - 1, "b": st.j, "v": val, "st": st, "pos": False,
                     "lo": lo if lo is not None else 0.0, "hi": hi_eff})
    if not cons:
        return None
    # 묶음끼리 겹치지 않고 다 덮으면(보통의 표) 합을 범위 안에서 맞춘다
    bands = [c for c in cons if not c["pos"] and not c["st"].cum and c["v"] is not None]
    cover = sorted(i for c in bands for i in range(c["a"] + 1, c["b"] + 1))
    adjusted = scaled = False
    if len(bands) == len(cons) and cover == list(range(len(GRADES))):
        r = _adjust(bands, notes)
        adjusted = r == "adjusted"
    # 누적 C(b) − C(a) = v 를 퍼뜨린다
    C = {-1: 0.0}
    conflict = False

    def spread():
        nonlocal conflict
        changed = True
        while changed:
            changed = False
            for c in cons:
                if c["v"] is None:
                    continue
                a, b = c["a"], c["b"]
                if a in C and b not in C:
                    C[b] = C[a] + c["v"]
                    changed = True
                elif b in C and a not in C:
                    C[a] = C[b] - c["v"]
                    changed = True
    spread()
    stated_total = _LAST in C
    # 값 없는 '나머지' 묶음과 '나머지 30%'(학점 없는 묶음)
    covered = {i for c in cons for i in range(c["a"] + 1, c["b"] + 1)}
    uncovered = [i for i in range(len(GRADES)) if i not in covered]
    for st in sts:
        if st.rest_group and uncovered and uncovered == list(range(uncovered[0], uncovered[-1] + 1)):
            cons.append({"a": uncovered[0] - 1, "b": uncovered[-1], "v": _nominal(st.v), "st": st, "pos": False,
                         "lo": st.v.lo or 0.0, "hi": st.v.hi if st.v.hi is not None else 100.0, "rest": True})
            spread()
            stated_total = _LAST in C
            break
    if not stated_total:
        C[_LAST] = 100.0
        spread()
    rests = [c for c in cons if c["v"] is None]
    for c in rests:   # 'C 이하: 나머지' — 나머지를 그 묶음이 다 가진다(아래 학점은 0)
        if c["a"] in C and c["b"] not in C:
            nxt = min(k for k in C if k > c["b"])
            C[c["b"]] = C[nxt]
            if nxt > c["b"]:
                notes.append(f"나머지는 {_label(c['a'] + 1, c['b'])}로 — {_label(c['b'] + 1, nxt)}는 0%로 봄")
        elif c["b"] in C and c["a"] not in C:
            prv = max(k for k in C if k < c["a"])
            C[c["a"]] = C[prv]
    for c in cons:
        if c["v"] is not None and c["a"] in C and c["b"] in C and abs(C[c["b"]] - C[c["a"]] - c["v"]) > 0.5:
            conflict = True
    if conflict:
        notes.append("적힌 비율끼리 맞지 않는 곳이 있어 앞의 값을 씀 — 확인 필요")
    if abs(C[_LAST] - 100.0) > 0.5:
        if C[_LAST] <= 0:
            return None
        f = 100.0 / C[_LAST]
        notes.append(f"비율 합이 {C[_LAST]:g}%라 비례로 100%에 맞춤 — 확인 필요")
        for k in list(C):
            C[k] *= f
        scaled = True
    pts = sorted(C)
    if any(C[b] < C[a] - 0.5 for a, b in zip(pts, pts[1:])):
        return None
    dist: dict = {}
    full = True
    for a, b in zip(pts, pts[1:]):
        span = list(range(a + 1, b + 1))
        grades = [GRADES[i] for i in span]
        total = max(0.0, C[b] - C[a])
        stated = next((c for c in cons if (c["a"] == a and c["b"] == b and not c["pos"])
                       or ((c["st"].cum or c["pos"]) and c["st"].oi - 1 == a and c["st"].oj == b)), None)
        if stated is not None:
            for x in grades:
                dist[x] = total / len(grades)
            continue
        inner = [c for c in cons if c["v"] is not None and a <= c["a"] and c["b"] <= b and not c["pos"]
                 and not (c["a"] in C and c["b"] in C)]
        fixed = {}
        for c in inner:
            gs = GRADES[c["a"] + 1:c["b"] + 1]
            for x in gs:
                fixed[x] = c["v"] / len(gs)
        if inner:
            notes.append(f"위·아래 학점이 빠져 위치를 알 수 없는 묶음({', '.join(c['st'].text for c in inner)}) — 확인 필요")
        if total > 0.01 and len(fixed) < len(grades):
            full = False
            notes.append(f"글에 없는 {_label(span[0], span[-1])} {total:g}%는 기본 비율 모양으로 나눔")
        dist.update(_fill(grades, total, fixed))
    dist = _round100(dist)
    if validate_distribution(dist):
        return None
    for st in sts:
        if st.rest_group:
            continue
        v = st.v
        lab = _label(st.i, st.j)
        si, sj = (st.oi, st.oj) if (st.cum or v.pos) else (st.i, st.j)   # 이 묶음이 가진 학점
        if v.bound in ("range", "upper", "lower") and not v.pos:
            ranges[lab] = [v.lo if v.lo is not None else 0.0, v.hi if v.hi is not None else 100.0]
        elif v.pos and v.bound == "range":
            ranges[f"상위 {_label(st.oi, st.oj)}"] = [v.lo, v.hi]
        own = GRADES[si:sj + 1]
        if v.bound == "range" and not v.pos:
            notes.append(f"범위는 가운데 값: {lab} {v.lo:g}~{v.hi:g}% → {v.val:g}%")
        elif v.bound == "upper":
            notes.append(f"상한을 그대로 씀: {lab} {v.hi:g}% 이내 → {v.hi:g}%")
        elif v.bound == "lower":
            notes.append(f"하한을 그대로 씀: {lab} {v.lo:g}% 이상 → {v.lo:g}%")
        if st.cum and st.oi > 0:
            notes.append(f"누적에서 뺌: {_label(st.oi, st.oj)} = {_label(0, st.oj)} 누적 − 위 학점")
        if len(own) > 1 and not st.each:
            share = round(sum(dist[x] for x in own) / len(own), 2)
            notes.append(f"묶음 안은 고르게: {_label(si, sj)} → {share:g}%씩")
        groups_out.append({"label": _label(si, sj), "grades": own,
                           "pct": round(sum(dist[x] for x in own), 2),
                           "range": ([v.lo if v.lo is not None else 0.0, v.hi if v.hi is not None else 100.0]
                                     if v.bound in ("range", "upper", "lower") else None),
                           "bound": v.bound, "cumulative": bool(st.cum or v.pos), "text": st.text})
    seen, uniq = set(), []
    for n in notes:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    notes[:] = uniq
    return dist, groups_out, ranges, full and not conflict, adjusted or scaled or conflict


# ---------------------------------------------------------------- 절대평가 풀기

_MAXRX = re.compile(r"(\d{2,4})\s*점\s*만점|만점\s*(?:은|:)?\s*(\d{2,4})|out\s+of\s+(\d{2,4})|총점\s*(\d{2,4})\s*점", re.I)


def _absolute(sts: list[_St], notes: list[str], context: str) -> tuple[dict, list, dict, bool] | None:
    """→ (cutoffs {학점: 100점 기준 최소 점수}, 묶음 목록, ranges, 가운데 가정을 했는지)."""
    rows, uppers, ranges, groups_out = [], [], {}, []
    for st in sts:
        if st.rest_group or st.v.bound == "rest":
            continue
        v = st.v
        if v.bound == "upper":
            uppers.append((st.oi, st.oj, v.hi, st))
            continue
        lo = v.lo if v.bound == "range" else v.val
        hi = v.hi if v.bound == "range" else None
        rows.append((st.oi, st.oj, float(lo), hi, st))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    top_val = max([r[3] for r in rows if r[3] is not None] + [r[2] for r in rows])
    scale = 1.0
    if top_val > 100.5:
        m = _MAXRX.search(context)
        mx = float(next(x for x in m.groups() if x)) if m else None
        if not mx or mx < top_val:
            return None
        scale = 100.0 / mx
        notes.append(f"{mx:g}점 만점을 100점 기준으로 바꿈")
    cut: dict = {}
    interp = False
    for oi, oj, lo, hi, st in rows:
        lo = lo * scale
        hi = hi * scale if hi is not None else None
        gs = [x for x in GRADES[oi:oj + 1] if x != "F"]
        if not gs:
            continue
        above = [cut[x] for x in GRADES[:oi] if x in cut]
        up = hi if (hi is not None and not above) else (min(above) if above else (hi if hi is not None else 100.0))
        if len(gs) == 1:
            cut[gs[0]] = round(lo, 2)
        else:
            interp = True
            for m_, x in enumerate(gs):
                cut[x] = round(lo + (up - lo) * (len(gs) - 1 - m_) / len(gs), 2)
            notes.append(f"{'/'.join(gs)} 경계는 {_label(oi, oj)} 구간({lo:g}~{up:g}점)을 고르게 나눠 가정")
        ranges[_label(oi, oj)] = [round(lo, 2), round(hi, 2) if hi is not None else None]
        groups_out.append({"label": _label(oi, oj), "grades": GRADES[oi:oj + 1], "min_score": round(lo, 2),
                           "range": [round(lo, 2), round(hi, 2) if hi is not None else None],
                           "bound": st.v.bound, "cumulative": False, "text": st.text})
    for oi, oj, hi, st in uppers:   # 'F: 60점 미만' → 바로 위 학점의 기준이 비어 있으면 60
        above = GRADES[oi - 1] if oi > 0 else None
        if above and above != "F" and above not in cut and hi is not None:
            cut[above] = round(hi * scale, 2)
    vals = [cut[x] for x in GRADES if x in cut]
    if len(vals) < 1 or any(b >= a for a, b in zip(vals, vals[1:])):
        return None
    return cut, groups_out, ranges, interp


# ---------------------------------------------------------------- 묶음 평가

_GRADE_KW = re.compile(r"학점|등급|성적\s*(?:분포|비율|부여|기준|평가)|grade|grading|letter|상대\s*평가|절대\s*평가|curv", re.I)
_REL_KW = re.compile(r"상대\s*평가|relative|curv|비율|분포|distribution|guideline|가이드|이내|수강\s*(?:인원|생)|"
                     r"of\s+(?:the\s+)?(?:class|students)", re.I)
_ABS_KW = re.compile(r"절대\s*평가|absolute|cut\s*-?\s*off|컷|grading\s+scale|점수\s*(?:기준|구간|범위)|기준\s*점수", re.I)


def _is_absolute(sts: list[_St], ctx: str) -> bool:
    vals = [st for st in sts if not st.rest_group and st.v.bound != "rest"]
    if not vals or any(st.v.pos or st.cum for st in vals):
        return False
    if _REL_KW.search(ctx) and re.search(r"상대\s*평가|relative|curv", ctx, re.I) and not _ABS_KW.search(ctx):
        return False
    score_n = sum(st.v.unit == "score" for st in vals)
    ineq_n = sum(st.v.ineq for st in vals)
    if score_n >= 2 or ineq_n >= 1 or (score_n >= 1 and _ABS_KW.search(ctx)):
        return True
    keyed = sorted((st.oi, st.v.lo if st.v.bound == "range" else st.v.val) for st in vals if st.v.bound != "upper")
    desc = len(keyed) >= 2 and all(a[1] > b[1] for a, b in zip(keyed, keyed[1:]))
    total = sum(x[1] for x in keyed)
    lower_n = sum(st.v.bound == "lower" for st in vals)
    low_min = min((x[1] for x in keyed), default=0)
    return desc and (total > 110 or (lower_n >= 2 and low_min >= 40) or bool(_ABS_KW.search(ctx)))


def _evaluate(block: dict, lines: list[tuple[str, str]]) -> DistFound | None:
    sts = block["sts"]
    first = block["lines"][0]
    head = [lines[k][0] for k in range(max(0, first - 3), first)]
    ctx = " ".join(head + [lines[k][0] for k in block["lines"]])
    notes: list[str] = []
    sts = _resolve_sets(sts, notes)
    real = [st for st in sts if not st.rest_group]
    if not real:
        return None
    distinct = {(st.i, st.j) for st in real}
    suffix = any(a.suffix in ("+", "0") for st in real for a in st.atoms)
    band = any(st.band for st in real)
    kw = bool(_GRADE_KW.search(ctx))
    if len(distinct) == 1 and not (band or kw):
        return None
    evidence = suffix or band or kw
    # 근거 문장: 바로 위의 짧은 제목 줄(Grade (Guideline))도 함께
    idx = list(block["lines"])
    if first > 0 and first - 1 not in idx:
        h = lines[first - 1][1]
        if len(h) <= 60 and (_GRADE_KW.search(h) or _REL_KW.search(h) or _ABS_KW.search(h)) and not re.search(r"\d", h):
            idx.insert(0, first - 1)
    sentence = " / ".join(lines[k][1] for k in idx)[:240]
    if _is_absolute(real, ctx):
        r = _absolute(real, notes, ctx)
        if r is None:
            return None
        cut, groups, ranges, interp = r
        strong = any(st.v.unit == "score" or st.v.ineq or st.v.bound == "lower" for st in real) or bool(_ABS_KW.search(ctx))
        n = len(groups)
        conf = "high" if (n >= 3 and strong and not interp) else "medium" if n >= 2 else "low"
        if not evidence and conf == "high":
            conf = "medium"
        if n < 2:
            notes.append("기준이 하나뿐입니다")
        return DistFound("absolute", None, ranges, cut, sentence, conf, notes, groups)
    if not any(st.v.unit in ("pct", "bare") for st in real) or all(st.v.bound == "rest" for st in real):
        return None
    r = _relative(sts, notes)
    if r is None:
        return None
    dist, groups, ranges, full, fudged = r
    bare = all(st.v.unit == "bare" for st in real)
    if bare:
        notes.append("숫자에 % 표시가 없어 비율로 봄")
    if len(distinct) < 2 or not evidence or any(x in n for n in notes for x in ("비례", "맞지 않는", "위치를 알 수 없는")):
        conf = "low"
    elif full and not fudged and not bare:
        conf = "high"
    else:
        conf = "medium"
    if len(distinct) < 2:
        notes.append("학점 묶음이 하나뿐입니다")
    return DistFound("relative", dist, ranges, None, sentence, conf, notes, groups)


def _blocks(lines: list[tuple[str, str]]) -> list[dict]:
    blocks, cur, gap = [], None, 0
    for li, (line, _) in enumerate(lines):
        sts = _line_statements(line, li) if (re.search(r"[A-DF]", line) or _REST_GROUP.search(line)) else []
        real = [s for s in sts if not s.rest_group]
        if real or (cur is not None and sts):
            if cur is None:
                cur = {"lines": [], "sts": []}
            cur["lines"].append(li)
            cur["sts"] += sts
            gap = 0
            continue
        if cur is not None:
            gap += 1
            if gap > 1 or len(line) > 60 or re.search(r"\d", line):
                blocks.append(cur)
                cur, gap = None, 0
    if cur is not None:
        blocks.append(cur)
    return blocks


def _sig(r: DistFound):
    return (r.kind, tuple(sorted((r.dist or r.cutoffs or {}).items())))


def find_distribution(text: str, is_html: bool = False) -> DistFound | None:
    """글에서 학점 비율(상대평가) 또는 점수 기준표(절대평가) 하나를 찾는다. 없으면 None(P/NP만 있는 글도 None)."""
    if not text or not re.search(r"[A-DFａ-ｚＡ-Ｚ]", text):
        return None
    lines = _lines(text, is_html)
    found = []
    for pos, b in enumerate(_blocks(lines)):
        r = _evaluate(b, lines)
        if r is not None:
            found.append((r, pos))
    if not found:
        return None
    found.sort(key=lambda x: (-_RANK[x[0].confidence], -len(x[0].groups), x[1]))
    best = found[0][0]
    others = {_sig(r) for r, _ in found[1:] if r.confidence == "high"} - {_sig(best)}
    if best.confidence == "high" and others:
        best.confidence = "medium"
        best.notes.append("글에 다른 학점 기준이 더 있습니다 — 맞는 것인지 확인하세요")
    return best


# ---------------------------------------------------------------- AI 결과·저장값 다루기

def from_groups(kind: str, groups: list | None = None, cutoffs: dict | None = None, sentence: str = "",
                confidence: str = "medium", notes: list | None = None) -> DistFound:
    """AI CLI 답(묶음 목록 또는 기준표)을 규칙과 같은 방법으로 풀어 DistFound로. 맞지 않으면 ValueError(한국어 + 영어)."""
    notes = list(notes or [])
    if kind == "absolute":
        cut = {}
        for g_, v in (cutoffs or {}).items():
            if g_ not in _IDX or g_ == "F":
                raise ValueError(f"모르는 학점 {g_} / unknown grade {g_}")
            cut[g_] = round(float(v), 2)
        vals = [cut[x] for x in GRADES if x in cut]
        if len(vals) < 2:
            raise ValueError("점수 기준이 2개 이상이어야 합니다 / need at least two cutoffs")
        if any(b >= a for a, b in zip(vals, vals[1:])):
            raise ValueError("점수 기준은 학점 순서대로 내려가야 합니다 / cutoffs must decrease from A+ to D0")
        return DistFound("absolute", None, {}, cut, sentence[:240], confidence, notes,
                         [{"label": x, "grades": [x], "min_score": cut[x], "range": None, "bound": "lower",
                           "cumulative": False, "text": x} for x in GRADES if x in cut])
    sts = []
    for n, gr in enumerate(groups or []):
        gs = [x for x in gr.get("grades") or [] if x in _IDX]
        if not gs:
            raise ValueError(f"{n}번째 묶음에 학점이 없습니다 / group {n} has no grades")
        idx = sorted(_IDX[x] for x in gs)
        if idx != list(range(idx[0], idx[-1] + 1)):
            raise ValueError(f"{n}번째 묶음의 학점이 이어지지 않습니다 / group {n} grades are not contiguous")
        lo, hi, pct = gr.get("low"), gr.get("high"), gr.get("pct")
        if lo is not None and hi is not None:
            lo, hi = float(lo), float(hi)
            if lo > hi:
                raise ValueError(f"{n}번째 묶음의 low가 high보다 큽니다 / low > high in group {n}")
            if pct is not None and not lo - 0.01 <= float(pct) <= hi + 0.01:
                raise ValueError(f"{n}번째 묶음의 pct가 범위 밖입니다 / pct outside [low, high] in group {n}")
            v = _V(0, 0, lo, hi, (lo + hi) / 2 if pct is None else float(pct), "range", "pct")
        elif hi is not None and pct is None:
            v = _V(0, 0, 0.0, float(hi), float(hi), "upper", "pct")
        elif pct is not None:
            v = _V(0, 0, float(pct), float(pct), float(pct), "exact", "pct")
        else:
            raise ValueError(f"{n}번째 묶음에 비율이 없습니다 / group {n} has no percent")
        st = _St(atoms=[], rng=True, v=v, line=0, text=_label(idx[0], idx[-1]), cum=bool(gr.get("cumulative")))
        st.oi, st.oj = idx[0], idx[-1]
        st.i, st.j = (0 if st.cum else st.oi), st.oj
        sts.append(st)
    if not sts:
        raise ValueError("묶음이 없습니다 / no groups")
    plain = [st for st in sts if not st.cum]
    used = [i for st in plain for i in range(st.oi, st.oj + 1)]
    if len(used) != len(set(used)):
        raise ValueError("묶음끼리 학점이 겹칩니다 / groups overlap")
    r = _relative(sts, notes)
    if r is None:
        raise ValueError("비율을 9학점으로 풀 수 없습니다(합·누적 확인) / could not resolve the percentages")
    dist, groups_out, ranges, _full, fudged = r
    if fudged and any("비례" in x or "맞지 않는" in x for x in notes):
        raise ValueError("비율 합이 100%가 되지 않습니다 / percentages do not add up to 100")
    return DistFound("relative", dist, ranges, None, sentence[:240], confidence, notes, groups_out)


def effective(cfg: dict | None) -> dict:
    """과목 성적 설정에서 실제로 쓸 학점 기준(계약 16장): 사용자 비율(config.dist) > 자동 비율(config.dist_auto) > 기본값.
    → {"by": "user"|"syllabus"|"default", "kind": "relative"|"absolute", "dist": {9학점 %}, "cutoffs": {...}|None,
       "auto": config.dist_auto|None}. 절대평가면 dist는 기본 비율(상위 % 계산에만 쓰임)이고 학점은 cutoffs로 정한다."""
    cfg = cfg or {}
    if cfg.get("dist"):
        return {"by": "user", "kind": "relative", "dist": cfg["dist"], "cutoffs": None, "auto": None}
    a = cfg.get("dist_auto")
    if isinstance(a, dict):
        if a.get("kind") == "absolute" and isinstance(a.get("cutoffs"), dict) and a["cutoffs"]:
            cut = {k: float(v) for k, v in a["cutoffs"].items() if k in _IDX and isinstance(v, (int, float))}
            vals = [cut[x] for x in GRADES if x in cut]
            if vals and all(b < x for x, b in zip(vals, vals[1:])):
                return {"by": "syllabus", "kind": "absolute", "dist": DEFAULT_DISTRIBUTION, "cutoffs": cut, "auto": a}
        if a.get("kind") == "relative" and isinstance(a.get("dist"), dict) and not validate_distribution(a["dist"]):
            return {"by": "syllabus", "kind": "relative", "dist": a["dist"], "cutoffs": None, "auto": a}
    return {"by": "default", "kind": "relative", "dist": DEFAULT_DISTRIBUTION, "cutoffs": None, "auto": None}


def summary(r: dict | DistFound) -> str:
    """'A+·A0 45%(40~50) · B+·B0 50%(40~60) · C+~F 5%(0~10)' / '절대평가 A+ 95 · A0 90 …점 이상'."""
    d = r.as_dict() if isinstance(r, DistFound) else r
    if d.get("kind") == "absolute":
        cut = d.get("cutoffs") or {}
        return "절대평가 " + " · ".join(f"{x} {cut[x]:g}" for x in GRADES if x in cut) + "점 이상"
    parts = []
    for gr in d.get("groups") or []:
        rg = gr.get("range")
        parts.append(f"{gr['label']} {gr['pct']:g}%" + (f"({rg[0]:g}~{rg[1]:g})" if rg and rg[0] is not None
                                                          and rg[1] is not None else ""))
    return " · ".join(parts)
