"""강의계획서·게시판·공지·PDF 글에서 평가 비중(중간고사 30%, 기말고사 40% …)을 규칙으로 찾는다. 계약 10장 C.

규칙 기반(표준 라이브러리)이라 AI 없이 누구 PC에서나 같은 결과가 나온다. 찾은 것은 근거 문장과 함께 돌려주고,
합이 100%가 아니거나 단위가 없는 등 애매한 부분은 notes에 적어 confidence를 낮춘다.

다루는 모양:
- 한 줄 목록: "중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%", "Midterm 30% / Final 40% / …",
  "평가방법: 출석(10) 과제(20) 중간(30) 기말(40)", "Lab reports 40%, Midterm project 30%, Final exam 30%"
- 여러 줄 목록(글머리표·번호), 줄이 나뉜 PDF 글("중간고사\\n30%"), 이름 줄 + 숫자 줄로 된 PDF 표
- HTML 표(<tr><td>중간고사</td><td>30</td></tr>, 가로로 놓인 표 포함), "|" 로 나뉜 글 표
- 점수 배분(출석 10점 … 총 100점), 총점이 100이 아니면 %로 바꿔 notes에 적음
비중이 아닌 %는 버린다: 출석률 80% 이상, 상위 30%, 학점 분포(A+ 15% …, D+ 0%), 감점 10%, 30% 이내 등.
"""
from __future__ import annotations

import html as _html
import re
from bisect import bisect_right
from dataclasses import dataclass, field

from .extract import html_to_text


@dataclass
class WeightsFound:
    components: list          # [{"name": "중간고사", "weight": 30.0, "fixed": False}]
    total: float              # 비중 합(%)
    sentence: str             # 근거 문장(원문 그대로, 줄바꿈은 " / ")
    confidence: str           # "high"(합 100) | "medium" | "low"
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- 이름

# 이름 전체가 이것과 같을 때만 대표 이름으로 바꾼다("Midterm project"는 그대로 둔다).
_ALIASES = [
    ("중간고사", r"중간(?:\s*(?:고사|시험|평가))?(?:\s*\(?\s*midterm(?:\s*exam)?\s*\)?)?|중간\s*고사\s*시험|"
                r"mid[\s\-]*term(?:\s*(?:exams?|examinations?|tests?))?|mid[\s\-]*exams?"),
    ("기말고사", r"기말(?:\s*(?:고사|시험|평가))?(?:\s*\(?\s*final(?:\s*exam)?\s*\)?)?|"
                r"finals?(?:\s*(?:exams?|examinations?|tests?))?|final[\s\-]*term(?:\s*exams?)?"),
    ("과제", r"과제(?:물)?|숙제|homeworks?|hws?|assignments?|problem\s*sets?|psets?"),
    ("출석", r"출석(?:\s*점수)?|출결|attendance"),
    ("수업참여", r"(?:수업\s*)?참여(?:도)?|(?:class\s*)?participation"),
    ("퀴즈", r"퀴즈|쪽지\s*시험|quiz(?:zes)?|quizes"),
    ("발표", r"발표|presentations?"),
    ("프로젝트", r"(?:텀\s*|팀\s*)?프로젝트|(?:term\s*|team\s*)?projects?"),
    ("실습", r"실습|labs?"),
]
_ALIAS_RX = [(canon, re.compile(rf"(?:{rx})", re.I)) for canon, rx in _ALIASES]
_FIXED = re.compile(r"출석|출결|참석|참여|attendance|participation", re.I)
# 이름에 이것이 있으면 평가 항목답다(확신도·이름 다듬기에 씀).
_KNOWN = re.compile(r"중간|기말|과제|숙제|레포트|리포트|보고서|퀴즈|쪽지|출석|출결|참여|발표|프로젝트|실습|실험|토론|토의|"
                    r"시험|고사|에세이|논문|포트폴리오|기타|"
                    r"exam|midterm|mid-term|final|homework|\bhws?\b|assignment|quiz|attendance|participation|"
                    r"project|\blabs?\b|report|presentation|paper|essay|discussion|\btests?\b|problem\s*set|"
                    r"pset|exercise|portfolio|design|term|others?", re.I)
_FAMILY = [re.compile(r"중간|mid[\s\-]*term|mid[\s\-]*exam", re.I), re.compile(r"기말|final", re.I)]

# 비중 항목 이름이 될 수 없는 것(학점 분포·기준 비율·통계·합계).
_BAD_NAME = re.compile(
    r"^(?:[A-DF][+0\-]?|[A-DF]\s*학점|P|NP|S|U|pass|fail|합|계|총|합계|총계|총점|전체|만점)$|학점|등급|상위|하위|"
    r"이내|이상|이하|미만|초과|최대|최소|최고|최저|평균|표준\s*편차|중앙값|만점|감점|가산|할인|확률|합격|합계|총점|"
    r"[률율]$|비율$|"
    r"\b(?:top|bottom|percentile|rate|penalty|late|deduct\w*|off|discount|average|mean|median|max|min|"
    r"score|scores|grade|grades|total|sum|least|most|more|less|over|under|chance|probability|curve|"
    r"up\s+to|cutoff)\b", re.I)
_STOP = {"the", "a", "an", "of", "for", "is", "are", "be", "will", "based", "on", "in", "as", "follows",
         "following", "consists", "consist", "include", "includes", "including", "your", "course", "with",
         "각", "각각", "총", "다음", "다음과", "같이", "같다", "이", "그", "본", "이번", "해당", "및"}
_ALLOC_WORD = re.compile(r"성적\w*|평가\w*|배점|반영\w*|비율|비중|구성|산출|방법|기준|요소|항목|"
                         r"grading|grades?|weights?|weighting|evaluation|assessments?|breakdown", re.I)
_TRAIL_STOP = {"is", "are", "worth", "counts", "count", "for", "accounts", "account", "weighted", "at", "will",
               "be", "makes", "make", "up", "of", "each", "comprises", "carries", "equals", "각", "각각"}
_CONNECT = {"및", "and", "&", "그리고", "or", "또는", "/"}
_PARTICLE_STRONG = re.compile(r"(?:은|는|을|를|의|에서|에|으로|로)$")
_PARTICLE_WEAK = re.compile(r"(?:이|가|도)$")
_PARTICLE_AND = re.compile(r"(?:와|과)$")


def _alias(name: str) -> str | None:
    s = re.sub(r"\s+", " ", name.strip())
    for canon, rx in _ALIAS_RX:
        if rx.fullmatch(s):
            return canon
    return None


def _is_label(w: str) -> bool:
    return bool(w) and (_alias(w) is not None or bool(_KNOWN.search(w)))


def normalize_name(name: str) -> str:
    """'Midterm exam' → '중간고사', 'Homework' → '과제'. 이름 전체가 흔한 표현일 때만 바꾸고 나머지는 그대로."""
    return _alias(name) or re.sub(r"\s+", " ", name.strip())[:40]


def is_fixed(name: str) -> bool:
    """출석·참여처럼 모두 비슷하게 받는 항목(순위에 영향 거의 없음)."""
    return bool(_FIXED.search(name or ""))


def display_names(names: list) -> list:
    """목록 전체를 보고 이름을 정한다: 같은 시험 계열(중간·기말)에 흔한 표현이 아닌 이름이 섞여 있으면
    ('Midterm project' + 'Midterm quiz') 모두 원래 이름 그대로, 아니면 흔한 표현만 대표 이름으로('Midterm' → '중간고사')."""
    clean = [re.sub(r"\s+", " ", str(n).strip())[:40] for n in names]
    for fam in _FAMILY:
        members = [n for n in clean if fam.search(n)]
        if len(members) > 1 and any(_alias(n) is None for n in members):
            return clean
    return [normalize_name(n) for n in clean]


def _clean_name(raw: str) -> str | None:
    s = re.split(r"[.!?;。]\s+|[:：]", raw.strip())[-1]
    s = s.strip(" \t-–—.·/|+&'’")
    words = s.split()
    while words and words[-1].lower() in _TRAIL_STOP:
        words.pop()
    if not words:
        return None
    last = words[-1]
    for rx in (_PARTICLE_STRONG, _PARTICLE_WEAK, _PARTICLE_AND):
        m = rx.search(last)
        if m and m.start() >= 1 and _is_label(last[:m.start()]) and _alias(last) is None:
            last = last[:m.start()]
            break
    out = [last]
    pending: list[str] = []
    for w in reversed(words[:-1]):
        if len(out) + len(pending) >= 4:
            break
        lw = w.lower()
        if lw in _CONNECT:
            pending.insert(0, w)
            continue
        if lw in _STOP or _ALLOC_WORD.fullmatch(w):
            break
        if _PARTICLE_STRONG.search(w) and len(w) > 1:
            break
        if _PARTICLE_WEAK.search(w) and len(w) > 1 and not _KNOWN.search(w):
            break
        m = _PARTICLE_AND.search(w)
        if m and len(w) > 1 and not _is_label(w[:m.start()]) and not _KNOWN.search(w):
            break
        out = [w] + pending + out
        pending = []
    name = " ".join(out).strip(" -–—.·/|+&")
    return name or None


# ---------------------------------------------------------------- 짝(이름 + 숫자) 찾기

_WORD = r"[A-Za-z가-힣][\w&'’.+/\-]*"
_NUM = r"(?P<num>\d{1,3}(?:\.\d{1,2})?)(?!\d)"
_UNIT = (r"(?P<unit>%|％|퍼센트|percent(?:age)?(?![a-z])|pct(?![a-z])|점|pts?(?![a-z])\.?|points?(?![a-z])|"
         r"marks?(?![a-z]))")
_PAIR = re.compile(
    rf"(?<![\w&'’.+/\-])(?P<name>{_WORD}(?:[ \t]+(?:{_WORD}|\d{{1,2}}(?![\d.:/%]))){{0,5}}?)"
    rf"(?:[ \t]*[(\[](?P<alias>[^()\[\]%]{{1,40}})[)\]])?"             # 병기: 중간고사(Midterm), 개수: Projects (2)
    rf"[ \t]*(?:[:：=|]|[-–—~](?!\d)|\.{{2,}}|…+)?[ \t]*(?P<open>[(\[])?[ \t]*"
    rf"(?:(?<![\w.])|(?<=[가-힣])(?=\d{{1,3}}(?:\.\d+)?[ \t]*[%％]))"   # '중간고사30%'만 붙여 쓰기 허용
    rf"{_NUM}[ \t]*{_UNIT}?"
    rf"(?(unit)|(?![ \t]*(?:\d|[./:\-]\d)|[A-Za-z가-힣]"                # 단위 없는 수: 날짜·시각·번호가 아니어야,
    rf"|[ \t]*[)\]]?[ \t]*(?:[:：=]|[(\[])?[ \t]*\d{{1,3}}(?:\.\d+)?[ \t]*(?:%|％|퍼센트|점|pts?(?![a-z])|points?)))"
    rf"[ \t]*[)\]]?", re.I)                                             # 뒤에 단위 붙은 수가 오면 앞 수는 번호·개수
_BAD_AFTER = re.compile(
    r"\s*(?:이상|이하|미만|초과|이내|내외|가량|정도|씩|마다|까지|감점|가산|할인|만점|증가|감소|"
    r"[을를]\s*받|받았|받은|의\s*(?:학생|인원|수강생|확률)|"
    r"or\s+(?:more|higher|above|better|less|lower|below)|and\s+(?:above|over|up|higher|below)|"
    r"above\b|below\b|off\b|per\s|penalty|deduct|"
    r"of\s+(?:the\s+)?(?:class|students|lectures|classes|sessions|attendance|the\s+class))", re.I)
_UNIT_KIND = {"%": "pct", "％": "pct", "퍼센트": "pct", "점": "pts"}

_STRONG_KW = re.compile(r"배점|반영\s*(?:비율|비중)?|비율|비중|평가\s*(?:방법|기준|요소|항목|방식|비율|구성)|"
                        r"성적\s*(?:평가|산출|반영|구성|처리|기준)|산출|grading|weight(?:s|ing)?|breakdown|"
                        r"evaluation|assessment|course\s*grade|final\s*grade", re.I)
_WEAK_KW = re.compile(r"평가|성적|구성|grade|policy", re.I)

_SUBPARTS = re.compile(r"(\d\s*(?:%|％))\s*\(([^()]*\d\s*(?:%|％)[^()]*)\)")
_GAP_PAREN = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|（[^（）]*）")
_GAP_STRIP = re.compile(r"[\s,/;|·•ㆍ+\-–—*▶►■□●○◦‣⁃・、.:：=~]+|(?<!\w)(?:\d{1,2}|[a-zA-Z]|[ivx]+|[가-하])[.)](?=\s|$)|"
                        r"[①-⑳]|(?<!\w)(?:및|그리고|and|or|또는|&|와|과|기타\s*:)(?!\w)|"
                        r"(?<!\w)of\s+(?:the\s+|your\s+)?(?:total\s+|final\s+|overall\s+|course\s+)?"
                        r"(?:grade|mark|score)s?(?!\w)|(?:[을를이가은는]\s*)?(?:반영|차지)\w*|"
                        r"(?<!\w)(?:씩|입니다|이며|이고|이다)(?!\w)", re.I)


@dataclass
class _Pair:
    name: str
    weight: float
    kind: str        # "pct" | "pts" | "bare"
    start: int
    end: int
    paren: bool
    name_start: int = -1   # 다듬은 이름이 시작하는 자리(근거 문장용)


def _pairs(text: str) -> list:
    """글 순서대로 (이름, 숫자) 짝. 비중이 아닌 것은 None(목록을 끊는 자리)으로 남긴다."""
    out = []
    for m in _PAIR.finditer(text):
        name = _clean_name(m.group("name"))
        after = text[m.end():m.end() + 24]
        unit = (m.group("unit") or "").lower()
        kind = _UNIT_KIND.get(unit) or ("pct" if unit.startswith(("percent", "pct")) else
                                         "pts" if unit else "bare")
        w = float(m.group("num"))
        if not name or _BAD_NAME.search(name) or _BAD_AFTER.match(after) or w > 100:
            out.append(None)
            continue
        if w == 0:  # 0%짜리는 항목으로 넣지 않는다(목록은 끊지 않음)
            continue
        at = m.group("name").rfind(name.split()[0])
        out.append(_Pair(name, w, kind, m.start(), m.end(),
                         bool(m.group("open")) or text[m.end() - 1:m.end()] in ")]",
                         m.start("name") + at if at >= 0 else m.start()))
    return out


def _gap_ok(gap: str) -> bool:
    g = _GAP_PAREN.sub(" ", gap)
    rest = _GAP_STRIP.sub(" ", g).strip()
    if not rest:
        return True
    # 줄이 바뀌는 사이의 짧은 소제목 한 줄(예: "Coursework")은 같은 목록으로 본다.
    lines = [ln.strip() for ln in g.split("\n")]
    inner = [ln for ln in lines[1:-1] if _GAP_STRIP.sub(" ", ln).strip()]
    edge = _GAP_STRIP.sub(" ", lines[0]).strip() or (_GAP_STRIP.sub(" ", lines[-1]).strip() if len(lines) > 1 else "")
    return (len(lines) >= 3 and len(inner) == 1 and not edge and len(inner[0]) <= 25
            and not re.search(r"\d", inner[0]))


def _prose_lines(text: str, pairs: list) -> set:
    """짝을 빼고도 글이 많이 남는 줄(문장 속에 %가 섞인 줄). 다른 줄의 목록과 이어 붙이지 않는다
    ('… includes the midterm quiz (5%) and the final quiz (5%).' 다음 줄부터 시작하는 목록)."""
    starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    spans: dict[int, list] = {}
    for p in pairs:
        if p is not None:
            spans.setdefault(bisect_right(starts, p.start) - 1, []).append(p)
    out = set()
    for li, ps in spans.items():
        a = starts[li]
        b = starts[li + 1] - 1 if li + 1 < len(starts) else len(text)
        line, cut = text[a:b], []
        pos = 0
        for p in sorted(ps, key=lambda x: x.start):
            s = (p.name_start if p.name_start >= 0 else p.start) - a
            cut.append(line[pos:max(pos, s)])
            pos = p.end - a
        cut.append(line[pos:])
        rest = _GAP_STRIP.sub(" ", _GAP_PAREN.sub(" ", "".join(cut))).strip()
        if len(rest) > 30:
            out.add(li)
    return out


def _blocks(text: str) -> list[list[_Pair]]:
    pairs = _pairs(text)
    starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    prose = _prose_lines(text, pairs)

    def line(p):
        return bisect_right(starts, p.start) - 1

    blocks, cur = [], []
    for p in pairs:
        if p is None:
            if cur:
                blocks.append(cur)
            cur = []
            continue
        cross = cur and line(cur[-1]) != line(p) and (line(cur[-1]) in prose or line(p) in prose)
        if cur and (cross or not _gap_ok(text[cur[-1].end:p.start])):
            blocks.append(cur)
            cur = []
        cur.append(p)
    if cur:
        blocks.append(cur)
    return blocks


# ---------------------------------------------------------------- 글 다듬기(PDF 줄 나눔·가로 표)

_NUM_TOKEN = re.compile(r"^\(?\s*\d{1,3}(?:\.\d{1,2})?\s*(?:%|％|점|퍼센트)?\s*\)?$")
_LABEL_LINE = re.compile(r"^[A-Za-z가-힣][^\d]{0,29}$")


def _tokens(line: str) -> list[str]:
    """표 한 줄의 칸: 탭·'|'·두 칸 이상 공백, 없으면 쉼표·세미콜론('Midterm, Final, Homework' / '30%, 30%, 25%'),
    그것도 없으면 공백."""
    parts = re.split(r"\t+|\s*\|\s*|\s{2,}", line.strip().strip("|"))
    if len(parts) <= 1 and re.search(r"[,;]", line):
        parts = re.split(r"\s*[,;]\s*", line.strip().strip(",;"))
    if len(parts) <= 1:
        parts = line.split()
    return [p.strip() for p in parts if p.strip()]


_EACH = re.compile(r"(?i)(?:(?<=\s)|^)(?:are\s+|will\s+be\s+)?(?:each|각각|각)\s+(?:worth\s+|counts?\s+(?:for\s+)?|"
                   r"weighted\s+(?:at\s+)?|is\s+)?(?P<num>\d{1,3}(?:\.\d+)?)\s*(?P<unit>%|％|퍼센트|percent|점)")
_EACH_SPLIT = re.compile(r"(?i)\s*(?:,|\band\b|&|및|(?<=[가-힣])(?:와|과)(?=\s))\s*")


def _expand_each(text: str) -> str:
    """'The midterm and the final each count for 30%'·'중간고사와 기말고사는 각각 30%' → 항목마다 30%.
    앞 마디(':'·';'·줄 시작부터)가 평가 항목 이름 둘 이상으로만 이루어졌을 때만 펼친다."""
    out, pos = [], 0
    for m in _EACH.finditer(text):
        start = max(text.rfind(c, 0, m.start()) for c in ":;\n.") + 1
        if m.start() - start > 80:
            continue
        seg = text[start:m.start()]
        names = [re.sub(r"(?i)^(?:the|both)\s+|(?:은|는|이|가)$", "", p.strip()) for p in _EACH_SPLIT.split(seg)]
        names = [n for n in names if n]
        if len(names) < 2 or not all(_is_label(n) for n in names):
            continue
        lead = re.match(r"\s*", seg).group(0)
        out.append(text[pos:start] + lead + ", ".join(f"{n} {m['num']}{m['unit']}" for n in names))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _prepare(text: str) -> str:
    text = _expand_each(text)
    text = _SUBPARTS.sub(r"\1", text)  # "중간고사 30%(객관식 20%, 서술형 10%)" → 세부 비중은 뺀다
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # 이름 줄 + 숫자 줄(PDF 가로 표): "구분 출석 중간 기말 과제" / "비율(%) 10 30 40 20"
        names, nums = _tokens(ln), _tokens(nxt)
        tail = []
        for t in reversed(nums):
            if not _NUM_TOKEN.match(t):
                break
            tail.insert(0, t)
        lead = nums[:len(nums) - len(tail)]
        if (len(tail) >= 2 and len(lead) <= 1 and len(names) >= len(tail)
                and all(re.search(r"[A-Za-z가-힣]", t) and not _NUM_TOKEN.match(t) for t in names[-len(tail):])):
            unit = "%" if re.search(r"%|％|비율|퍼센트", " ".join(lead) + ln) and not any("%" in t for t in tail) else ""
            out.append(", ".join(f"{n} {v}{unit}" for n, v in zip(names[-len(tail):], tail)))
            i += 2
            continue
        # 이름 줄 다음에 숫자만 있는 줄: "중간고사" / "30%"
        if _NUM_TOKEN.match(nxt) and _LABEL_LINE.match(ln) and len(ln.split()) <= 4:
            out.append(f"{ln} {nxt}")
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------- HTML 표

_TABLE = re.compile(r"(?is)<table\b.*?</table\s*>")
_ROW = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>")
_CELL = re.compile(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>")
_TOTAL_NAME = re.compile(r"^(?:합계|총계|합|계|총점|total|sum)$", re.I)


def _cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", html_to_text(s)).strip()


def _table_blocks(html: str) -> list[tuple[list[_Pair], str, str]]:
    """HTML 표에서 (짝 목록, 근거 문장, 표 앞뒤 글) 목록."""
    out = []
    for tm in _TABLE.finditer(html):
        rows = [[_cell_text(c) for c in _CELL.findall(r)] for r in _ROW.findall(tm.group(0))]
        before = _cell_text(html[max(0, tm.start() - 400):tm.start()])[-150:]
        head = " ".join(" ".join(r) for r in rows if not any(_NUM_TOKEN.match(c) for c in r))
        context = before + " " + head
        pct_head = bool(re.search(r"%|％|비율|비중|퍼센트|percent|weight", head, re.I))
        cur: list[_Pair] = []
        pos = 0

        def flush():
            if cur:
                out.append((list(cur), " / ".join(f"{p.name} {p.weight:g}" for p in cur), context))
            cur.clear()

        i = 0
        while i < len(rows):
            r = rows[i]
            nums = [j for j, c in enumerate(r) if _NUM_TOKEN.match(c)]
            texts = [j for j, c in enumerate(r) if c and j not in nums and re.search(r"[A-Za-z가-힣]", c)]
            nxt = rows[i + 1] if i + 1 < len(rows) else []
            nxt_nums = [c for c in nxt if _NUM_TOKEN.match(c)]
            # 가로 표: 이름 행 다음에 숫자 행
            if not nums and len(texts) >= 2 and len(nxt_nums) >= 2 and len(texts) >= len(nxt_nums):
                flush()
                for name, v in zip([r[j] for j in texts][-len(nxt_nums):], nxt_nums):
                    p = _row_pair(name, v, pct_head, pos)
                    pos += 1
                    if p is None:
                        cur.clear()
                        break
                    cur.append(p)
                flush()
                i += 2
                continue
            if nums and texts:
                name = r[texts[0]]
                v = next((r[j] for j in nums if re.search(r"%|％", r[j])), r[nums[0]])
                if _TOTAL_NAME.match(name.strip()):
                    i += 1
                    continue
                p = _row_pair(name, v, pct_head, pos)
                pos += 1
                if p is None:
                    flush()
                else:
                    cur.append(p)
            elif cur:
                flush()
            i += 1
        flush()
    return out


def _row_pair(name: str, v: str, pct_head: bool, pos: int) -> _Pair | None:
    clean = _clean_name(re.sub(r"\([^()]*\)", " ", name)) or None
    m = re.search(r"\d{1,3}(?:\.\d{1,2})?", v)
    if not clean or not m or _BAD_NAME.search(clean):
        return None
    w = float(m.group(0))
    if w <= 0 or w > 100:
        return None
    kind = "pct" if re.search(r"%|％|퍼센트", v) else "pts" if "점" in v else ("pct" if pct_head else "bare")
    return _Pair(clean, w, kind, pos, pos + 1, False)


# ---------------------------------------------------------------- 묶음 평가

_RANK = {"high": 3, "medium": 2, "low": 1}


def _merge(pairs: list[_Pair], notes: list[str]) -> list[dict]:
    """같은 이름은 하나로: 같은 비중이 반복되면(한·영 병기) 하나만, 다르면 더한다 — 합이 100에 가까운 쪽."""
    groups: dict[str, list[_Pair]] = {}
    labels: dict[str, str] = {}
    for p, name in zip(pairs, display_names([p.name for p in pairs])):  # Midterm project + Midterm quiz → 그대로
        key = name.lower()
        groups.setdefault(key, []).append(p)
        labels.setdefault(key, name)
    dedupe = {k: v[0].weight for k, v in groups.items()}
    summed = {k: sum(p.weight for p in v) for k, v in groups.items()}
    use = dedupe
    if any(len(v) > 1 for v in groups.values()):
        if abs(sum(dedupe.values()) - 100) > 1 and abs(sum(summed.values()) - 100) <= 1:
            use = summed
            notes.append("같은 이름 항목을 더함: " + ", ".join(labels[k] for k, v in groups.items() if len(v) > 1))
        else:
            notes.append("같은 항목이 두 번 적혀 하나로 셈: " + ", ".join(labels[k] for k, v in groups.items() if len(v) > 1))
    return [{"name": labels[k], "weight": round(w, 2), "fixed": is_fixed(labels[k])} for k, w in use.items()]


def _evaluate(pairs: list[_Pair], sentence: str, context: str, table: bool) -> WeightsFound | None:
    notes: list[str] = []
    kinds = {p.kind for p in pairs}
    strong = bool(_STRONG_KW.search(context))
    weak = strong or bool(_WEAK_KW.search(context))
    known = sum(1 for p in pairs if _is_label(p.name))
    if "pct" in kinds:
        kind = "pct"
    elif kinds == {"pts"}:
        kind = "pts"
    elif "pts" in kinds:
        kind = "pts"
    else:
        kind = "bare"
    comps = _merge(pairs, notes)
    total = round(sum(c["weight"] for c in comps), 2)
    if len(comps) < 2 and not (len(comps) == 1 and total == 100 and kind == "pct" and strong):
        return None
    near = abs(total - 100) <= 1
    if kind == "pts":
        if not (strong or (table and weak)):
            return None  # "중간고사 85점, 기말고사 90점" 같은 점수 안내일 수 있다
        if not near:
            notes.append(f"점수 배분(총 {total:g}점)을 %로 바꿈")
            for c in comps:
                c["weight"] = round(c["weight"] * 100 / total, 2)
            total = 100.0
            conf = "medium"
        else:
            conf = "high"
    elif kind == "bare":
        if not near or not (weak or table or all(p.paren for p in pairs)):
            return None
        conf = "high" if (strong or table) else "medium"
        if conf != "high":
            notes.append("숫자에 % 표시가 없어 비중으로 추정")
    else:
        if near:
            conf = "high" if (weak or table or known >= 2) else "medium"
        elif total > 100:
            if not (weak or known >= 2):
                return None
            conf = "low"
            notes.append(f"비중 합이 {total:g}%로 100%를 넘습니다 — 다른 표가 섞였을 수 있습니다")
        else:
            if not (weak or table or known >= 2):
                return None
            conf = "medium" if total >= 50 else "low"
            notes.append(f"비중 합이 {total:g}%입니다(100%가 아님) — 빠진 항목이 있을 수 있습니다")
    if len(comps) == 1:
        conf = "medium"
        notes.append("항목이 하나뿐입니다")
    return WeightsFound(comps, total, sentence[:240], conf, notes)


def _sentence(text: str, first: _Pair, last: _Pair) -> str:
    begin = first.name_start if first.name_start >= 0 else first.start
    ls = text.rfind("\n", 0, begin) + 1
    start = ls if begin - ls <= 40 else begin
    cut = max((m.end() for m in re.finditer(r"[.!?。]\s+", text[start:begin])), default=0)
    s = text[start + cut:last.end]
    s = re.sub(r"(?m)^[ \t]*(?:[-–—*•·▶►■□●○◦‣⁃・]|\d{1,2}[.)]|[①-⑳])[ \t]*", "", s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", " / ", s)).strip(" /")


def _key(w: WeightsFound) -> tuple:
    return tuple(sorted((c["name"].lower(), c["weight"]) for c in w.components))


def find_weights(text: str, is_html: bool = False) -> WeightsFound | None:
    """글에서 평가 비중 목록 하나를 찾는다. 합이 100%인 목록을 먼저 고르고(high), 없으면 일부만 맞는 것(medium/low)."""
    if not text:
        return None
    found: list[tuple[WeightsFound, int]] = []
    if is_html:
        for pairs, sentence, context in _table_blocks(text):
            w = _evaluate(pairs, sentence, context, table=True)
            if w:
                found.append((w, 0))
    plain = _prepare(html_to_text(text) if is_html else _html.unescape(text).replace("\xa0", " "))
    for pairs in _blocks(plain):
        first, last = pairs[0], pairs[-1]
        context = plain[max(0, first.start - 150):last.end]
        w = _evaluate(pairs, _sentence(plain, first, last), context, table=False)
        if w:
            found.append((w, first.start))
    if not found:
        return None
    found.sort(key=lambda x: (-_RANK[x[0].confidence], abs(x[0].total - 100), -len(x[0].components), x[1]))
    best = found[0][0]
    others = {_key(w) for w, _ in found if w.confidence == "high" and w is not best} - {_key(best)}
    if best.confidence == "high" and others:
        best.confidence = "medium"
        best.notes.append("글에 다른 평가 비중 목록이 더 있습니다 — 맞는 것인지 확인하세요")
    return best
