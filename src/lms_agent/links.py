"""점수·마감 → 평가 항목 연결과 세부 점수 개수(계약 14장). 규칙만 — 네트워크·저장 없음(같은 입력이면 같은 결과).

- 계열(family): 라벨·제목의 낱말을 lab·hw·report·quiz·midterm·final·project·attendance·exam 으로 본다.
- match(): 라벨의 계열을 평가 항목 이름의 낱말과, 비중 근거 문장의 '(w/ …)' 별칭에 맞춘다(14-2, 14-9).
  하나로 정해지지 않으면 component None + 이유("어느 항목인지 골라 주세요").
- count_*(): 몇 개인지 — 명시(stated) "Lab 0~6"·"총 7개의 실습"·"7 labs"·"과제 7회", 단서(hint) 가장 큰 LabK 뒤의
  "Next labs: A → B → C", 관찰(observed) 과목 글에서 본 번호(14-4).
gradebook이 저장된 상태(store.data)와 이어 붙인다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FAMILY_RX = {
    "lab": r"(?<![a-z])lab(?:s|oratory)?(?![a-z])|실습|실험|(?<![가-힣])랩(?![가-힣])",
    "hw": r"(?<![a-z])(?:hws?|homeworks?|assignments?)(?![a-z])|과제|숙제",
    "report": r"(?<![a-z])reports?(?![a-z])|레포트|리포트|보고서",
    "quiz": r"(?<![a-z])quiz(?:zes)?(?![a-z])|퀴즈|쪽지",
    "midterm": r"(?<![a-z])mid[\s\-]?term|중간",
    "final": r"(?<![a-z])finals?(?![a-z])|기말",
    "project": r"(?<![a-z])projects?(?![a-z])|프로젝트",
    "attendance": r"(?<![a-z])attendance(?![a-z])|출석|출결",
    "exam": r"(?<![a-z])exam(?:s|inations?)?(?![a-z])|시험|고사",
}
_FAM = {k: re.compile(v, re.I) for k, v in FAMILY_RX.items()}
FAM_KO = {"lab": "실습", "hw": "과제", "report": "보고서", "quiz": "퀴즈", "midterm": "중간", "final": "기말",
          "project": "프로젝트", "attendance": "출석", "exam": "시험"}
TERM = ("midterm", "final")
EXAMLIKE = ("quiz", "exam")
SPLIT_WORDS = ("project", "quiz", "exam", "report")   # 후보가 둘 이상일 때 가르는 '다른 낱말'(14-2)
COUNTABLE = ("lab", "hw", "quiz", "report", "project")
_ORDER = ("lab", "midterm", "final", "quiz", "exam", "project", "report", "hw", "attendance")


def families(text) -> set[str]:
    s = str(text or "")
    return {k for k, rx in _FAM.items() if rx.search(s)}


def norm(name) -> str:
    """항목 이름 비교용: 공백을 하나로, 소문자."""
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def is_exam_component(name: str) -> bool:
    """시험 계열 평가 항목(14-9 state.exams): quiz·exam·시험·고사·중간·기말이 있고 project·report가 아닌 것."""
    f = families(name)
    return bool(f & {"quiz", "exam", "midterm", "final"}) and not f & {"project", "report", "hw", "lab"}


# ---------------------------------------------------------------- 평가 항목

@dataclass
class Comp:
    name: str
    name_f: set
    alias: str | None = None          # '(w/ short reports)' 안의 글
    alias_f: set = field(default_factory=set)

    @property
    def all_f(self) -> set:
        return self.name_f | self.alias_f


_ALIAS_AFTER = r"[^()/\n]{0,24}?\(\s*(?:w/|with|incl\.?|including)\s*([^()]{1,60})\)"


def components_of(comps: list[dict], sources: list[str] | None = None) -> list[Comp]:
    """평가 항목 목록 → 이름 계열 + 비중 근거 문장(auto_source·항목 source)의 '(w/ …)' 별칭 계열."""
    texts = [s for s in (sources or []) if s]
    for c in comps or []:
        if isinstance(c, dict) and c.get("source"):
            texts.append(str(c["source"]))
    out = []
    for c in comps or []:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        name = str(c["name"]).strip()
        alias = None
        rx = re.compile(re.escape(name) + _ALIAS_AFTER, re.I)
        for t in texts:
            m = rx.search(t)
            if m:
                alias = re.sub(r"\s+", " ", m.group(1)).strip()
                break
        out.append(Comp(name, families(name), alias, families(alias) if alias else set()))
    return out


# ---------------------------------------------------------------- 연결

@dataclass
class Link:
    component: str | None
    reason: str
    evidence: str | None = None
    ambiguous: list = field(default_factory=list)   # 정하지 못했을 때 남은 후보 이름


def _first(fams: set) -> str | None:
    return next((f for f in _ORDER if f in fams), None)


def _narrow(S: list[Comp], pred, why: str, steps: list[str]) -> list[Comp]:
    T = [c for c in S if pred(c)]
    if T and len(T) < len(S):
        steps.append(why)
        return T
    return S


def match(label, comps: list[Comp], other_text: str = "", term_text: str = "", kind: str | None = None,
          early: bool | None = None) -> Link:
    """라벨 → 평가 항목 하나(계약 14-2·14-9).
    other_text: 출처 제목 등(다른 낱말 project·quiz·exam·report로 가를 때). term_text: 근거 문장(midterm·final 낱말),
    kind: 항목 종류("시험"|"과제"), early: 학기 앞쪽 절반인지(마지막에 중간/기말을 가를 때)."""
    label = str(label or "").strip()
    L = families(label)
    if not comps:
        return Link(None, "평가 항목이 아직 없어요")
    if not L:
        return Link(None, f"'{label or '이름 없음'}'이(가) 어느 평가인지 낱말로 알 수 없어요 — 어느 항목인지 골라 주세요")
    steps: list[str] = []
    evidence = None
    if "lab" in L:
        named = [c for c in comps if "lab" in c.name_f]
        hw = [c for c in comps if "hw" in c.name_f]
        rep = [c for c in comps if "report" in c.alias_f]
        if named:
            S, how = named, "이름에 실습·lab이 있는 항목"
        elif hw:
            S, how = hw, "실습 항목이 없어 과제 항목"
        elif rep:
            S, how = rep, "실습·과제 항목이 없어 보고서 별칭이 있는 항목"
            evidence = f"(w/ {rep[0].alias})" if len(rep) == 1 else None
        else:
            S, how = [], ""
        base = f"'{label}'은(는) 실습 계열 → {how}"
    else:
        S = [c for c in comps if c.name_f & L]
        how = "이름"
        if not S:
            S = [c for c in comps if c.alias_f & L]
            how = "근거 문장의 별칭"
        f0 = _first(L)
        base = f"'{label}'의 {FAM_KO.get(f0, f0)} 계열 → 항목 {how}"
    if not S:
        return Link(None, f"'{label}'에 맞는 평가 항목이 없어요 — 어느 항목인지 골라 주세요")
    # 가르기: 라벨의 다른 계열 → 출처 제목·라벨의 다른 낱말(project·quiz·exam·report)
    for f in [x for x in _ORDER if x in L]:
        S = _narrow(S, lambda c, f=f: f in c.all_f, f"'{FAM_KO[f]}' 낱말", steps)
    for f in [x for x in SPLIT_WORDS if x in families(other_text)]:
        S = _narrow(S, lambda c, f=f: f in c.all_f, f"제목의 '{FAM_KO[f]}' 낱말", steps)
    if len(S) > 1 and not L & set(TERM):
        S = _narrow(S, lambda c: not c.all_f & set(TERM), "중간·기말이 아닌 항목", steps)
    if len(S) > 1 and term_text:
        tf = families(term_text) & set(TERM)
        if len(tf) == 1:
            t = next(iter(tf))
            S = _narrow(S, lambda c, t=t: t in c.all_f, f"근거 문장의 '{FAM_KO[t]}' 낱말", steps)
    if len(S) > 1 and kind in ("시험", "과제"):
        want = kind == "시험"
        S = _narrow(S, lambda c: bool(c.all_f & set(EXAMLIKE)) == want,
                    "시험 일정" if want else "과제 마감", steps)
    if len(S) > 1 and early is not None:
        t = "midterm" if early else "final"
        S = _narrow(S, lambda c, t=t: t in c.all_f,
                    "학기 앞쪽 절반이라 중간" if early else "학기 뒤쪽 절반이라 기말", steps)
    if len(S) != 1:
        names = [c.name for c in S]
        return Link(None, f"'{label}'에 맞는 항목이 둘 이상이에요({', '.join(names[:4])}) — 어느 항목인지 골라 주세요",
                    ambiguous=names)
    c = S[0]
    if evidence is None and c.alias and (L & c.alias_f) and not (L & c.name_f):
        evidence = f"(w/ {c.alias})"
    reason = f"{base} '{c.name}'" + (f" · {', '.join(steps)}로 구분" if steps else "")
    return Link(c.name, reason, evidence)


# ---------------------------------------------------------------- 개수

COUNT_WORDS = {"lab": r"labs?|실습|랩", "hw": r"homeworks?|hws?|assignments?|과제|숙제",
               "quiz": r"quiz(?:zes)?|퀴즈", "report": r"reports?|레포트|리포트|보고서", "project": r"projects?|프로젝트"}
_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
              "eleven": 11, "twelve": 12, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
MAX_COUNT = 30
# '7 labs'처럼 숫자 바로 뒤에 올 때는 여러 개를 뜻하는 꼴만(시간표 줄 "8 Report"·"10 Project1 Presentation"은 주차 번호)
PLURAL_WORDS = {"lab": r"labs|lab[ \t]+(?:assignments|reports|exercises)",
                "hw": r"assignments|homeworks|homework[ \t]+assignments|hws|problem[ \t]+sets",
                "quiz": r"quizzes|quizes", "report": r"reports|lab[ \t]+reports", "project": r"projects"}


def _w(fam: str) -> str:
    return f"(?:{COUNT_WORDS[fam]})"


def numbered(text, fam: str) -> list[int]:
    """'Lab3 Assignment'·'Lab0_score-1.pdf'·'LAB 2' → 번호들(한 줄 안에서만).
    범위는 펼친다: 'Lab 4–6'·'Lab4-Lab6'·'Labs 0 through 6' → 4, 5, 6. 붙임표로 이은 이웃 번호('실습 2-3')는 2번 실습의
    3번일 수도 있어 펼치지 않는다(물결·대시·to/through, 같은 낱말 반복, 2 이상 차이 나는 붙임표만 범위)."""
    W = _w(fam)
    rx = re.compile(rf"(?<![a-z])(?:{W})[ \t_\-]*#?[ \t]*(\d{{1,2}})(?!\d)"
                    rf"(?:[ \t]*(?P<sep>~|～|–|—|-|to|through)[ \t]*(?P<w2>(?:{W})[ \t]*#?[ \t]*)?(?P<b>\d{{1,2}})(?![\d./:]))?",
                    re.I)
    out = []
    for m in rx.finditer(str(text or "")):
        a = int(m.group(1))
        b = int(m.group("b")) if m.group("b") else None
        if b is not None and a < b <= MAX_COUNT and (m.group("sep") != "-" or m.group("w2") or b - a >= 2):
            out.extend(range(a, b + 1))
        else:
            out.append(a)
    return out


_BAD_BEFORE = re.compile(r"(?i)(?:lab|labs|hw|homework|assignment|과제|실습|quiz|퀴즈|report|project|week|주차|no\.|#|"
                         r"next|remaining|남은|다음)\s*$")


def _line_of(text: str, a: int, b: int) -> str:
    s = text.rfind("\n", 0, a) + 1
    e = text.find("\n", b)
    return re.sub(r"\s+", " ", text[s:e if e >= 0 else len(text)]).strip()[:240]


def stated(text: str, fam: str) -> tuple[int, str] | None:
    """명시된 개수: 'Lab 0~6'·'Lab0-Lab6'(=7), '총 7개(의) 실습', '7 labs'·'seven lab assignments', '과제 7회'."""
    s = str(text or "")
    W = _w(fam)
    nw = "|".join(_NUM_WORDS)
    pats = [
        (re.compile(rf"(?<![a-z])(?:{W})[ \t]*#?[ \t]*(\d{{1,2}})[ \t]*(?:~|～|–|—|-|to|through)[ \t]*"
                    rf"(?:(?:{W})[ \t]*#?[ \t]*)?(\d{{1,2}})(?![\d./:])", re.I), "range"),
        (re.compile(rf"총[ \t]*(\d{{1,2}}|{nw})[ \t]*(?:개|번|회)(?:의)?[ \t]*(?:{W})", re.I), "n"),
        (re.compile(rf"(?<![\w.])(\d{{1,2}}|{nw})[ \t]+(?:{PLURAL_WORDS[fam]})(?![a-z가-힣])", re.I), "n"),
        (re.compile(rf"(?<![a-z])(?:{W})[ \t]*(\d{{1,2}})[ \t]*회(?!차)", re.I), "n"),
        (re.compile(rf"(?<![\d.])(\d{{1,2}})[ \t]*회(?:의)?[ \t]*(?:{W})", re.I), "n"),
    ]
    for rx, how in pats:
        for m in rx.finditer(s):
            if how == "range":
                a, b = int(m.group(1)), int(m.group(2))
                if not (0 <= a < b <= MAX_COUNT) or a > 1:
                    continue
                n = b - a + 1
            else:
                g = m.group(1).lower()
                n = int(g) if g.isdigit() else _NUM_WORDS.get(g)
                if _BAD_BEFORE.search(s[max(0, m.start() - 14):m.start()]):
                    continue
                if _PERIOD_AFTER.match(s[m.end():m.end() + 24]):
                    continue   # '2 labs this week'·'과제 2회(매주)'는 이번 주·주마다 개수지 학기 전체가 아니다
            if n and 1 <= n <= MAX_COUNT:
                return n, _line_of(s, m.start(), m.end())
    return _schedule(s, fam)


# 개수 뒤에 이 말이 오면 학기 전체 개수가 아니다
_PERIOD_AFTER = re.compile(r"(?i)^[ \t]*(?:\(?\s*)?(?:this|next|per|each|every|a)[ \t]+(?:week|day|class|session|lecture)|"
                           r"^[ \t]*(?:today|tomorrow|weekly)|^[ \t]*\(?\s*(?:이번\s*주|오늘|내일|매주|주당|한\s*주|매\s*시간)")
# 강의 일정 표의 한 줄: 'Week 3 | Lab 1', '3주차 실습 1', '3 | Lab 1'
_WEEK_ROW = re.compile(r"(?i)^\s*(?:(?:week|wk)\.?\s*(\d{1,2})|(\d{1,2})\s*주차?|(\d{1,2})\s*(?:\||\t|\s{2,}|\.\s))")


def _schedule(text: str, fam: str) -> tuple[int, str] | None:
    """강의 일정 표에서 그 계열 번호가 빈틈없이 이어지면(0 또는 1부터, 3줄 이상, 주차가 늘어남) 그 개수.
    'Week 3 | Lab 1 … Week 9 | Lab 4' → 4. 번호가 비거나(1, 4) 줄이 적으면 세지 않는다."""
    rows = []
    for line in str(text or "").splitlines():
        m = _WEEK_ROW.match(line)
        if not m:
            continue
        nums = numbered(line[m.end():], fam)
        if len(nums) == 1:
            rows.append((int(next(g for g in m.groups() if g)), nums[0], re.sub(r"\s+", " ", line).strip()))
    if len(rows) < 3 or any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        return None
    nums = sorted({r[1] for r in rows})
    if nums[0] not in (0, 1) or nums != list(range(nums[0], nums[-1] + 1)) or len(nums) != len(rows):
        return None
    n = len(nums)
    return (n, f"{rows[0][2]} … {rows[-1][2]}"[:240]) if 1 <= n <= MAX_COUNT else None


# 남은 것의 목록을 여는 말. 영어는 복수('Next labs:')만 — 단수 'Next lab:'은 다음 실습 하나의 안내다.
_NEXT = {"lab": r"(?:next|upcoming|remaining)[ \t]+labs|다음[ \t]*(?:실습|랩)(?:들)?|이후[ \t]*실습|남은[ \t]*(?:실습|랩)",
         "hw": r"(?:next|upcoming|remaining)[ \t]+(?:assignments|homeworks|homework[ \t]+assignments)|다음[ \t]*과제|남은[ \t]*과제",
         "quiz": r"(?:next|upcoming|remaining)[ \t]+quiz(?:zes|es)|다음[ \t]*퀴즈|남은[ \t]*퀴즈",
         "report": r"(?:next|upcoming|remaining)[ \t]+reports|다음[ \t]*보고서|남은[ \t]*보고서",
         "project": r"(?:next|upcoming|remaining)[ \t]+projects|다음[ \t]*프로젝트|남은[ \t]*프로젝트"}
_ARROWS = re.compile(r"\s*(?:→|->|=>|⇒|➜|➔|›|>)\s*")
# 목록 항목이 '이름'이 아니라 할 일·문장이면('bring your laptop', '노트북 지참하세요') 남은 실습 목록이 아니다
_NOT_TITLE = re.compile(r"(?i)^(?:bring|please|use|read|submit|prepare|install|check|make|download|review|finish|"
                        r"complete|remember|be|do|don't|will|you|we|they|it|is|are)\b|"
                        r"(?:다|요|세요|니다|하기|할\s*것|지참|준비|확인|바랍니다)[.!]?$|[;!?]")
_COUNT_ONLY = re.compile(r"^(\d{1,2}|[a-z]+|[가-힣]+)[ \t]*(?:개|번|회)?\.?$", re.I)


def _remaining_patterns(fam: str) -> list:
    """'3 more homework assignments'·'Two more quizzes'·'2 labs left'·'남은 과제 2개'·'실습은 2번 더 남았습니다'."""
    W, P = _w(fam), PLURAL_WORDS[fam]
    nw = "|".join(_NUM_WORDS)
    N = rf"(\d{{1,2}}|{nw})"
    return [
        re.compile(rf"(?<![\w.]){N}[ \t]+(?:more|additional|remaining)[ \t]+(?:{P}|{W})(?![a-z])", re.I),
        re.compile(rf"(?<![\w.]){N}[ \t]+(?:{P})[ \t]+(?:left|remaining|to[ \t]+go)(?![a-z])", re.I),
        re.compile(rf"남은[ \t]*(?:{W})[ \t]*[:：]?[ \t]*{N}[ \t]*(?:개|번|회)", re.I),
        re.compile(rf"(?:{W})(?:은|는|이|가)?[ \t]*{N}[ \t]*(?:개|번|회)[ \t]*(?:더|남)", re.I),
        re.compile(rf"(?<![\d.]){N}[ \t]*(?:개|번|회)(?:의)?[ \t]*(?:{W})(?:이|가)?[ \t]*(?:더|남)", re.I),
    ]


def _num(g: str) -> int | None:
    g = g.lower()
    return int(g) if g.isdigit() else _NUM_WORDS.get(g)


def hint(text: str, fam: str, zero_based: bool, extra_numbers: list[int] | None = None) -> tuple[int, str, int] | None:
    """단서: 같은 글에서 가장 큰 번호 K 뒤에 남은 것이 몇 개인지.
    - 목록 'Next labs: A → B → C'·'다음 실습: A, B' → n = K + 1 + 목록 수(0번부터일 때; 1번부터면 K + 목록 수).
      항목에 번호가 있으면('Upcoming labs: Lab 4, Lab 5') 그 가장 큰 번호로 센다. 항목이 이름이 아니면 세지 않는다.
    - 남은 개수 'Remaining labs: 4'·'3 more homework assignments'·'남은 과제: 2개' → n = K (+1) + 그 수.
    (n, 근거 줄, K). 'the next 3 labs'처럼 마지막이라는 말이 없는 수는 세지 않는다."""
    s = str(text or "")
    extra = list(extra_numbers or [])
    nums = numbered(s, fam) + extra
    if not nums:
        return None
    one = 1 if zero_based else 0
    rx = re.compile(rf"(?i)(?<![a-z])(?:{_NEXT[fam]})[ \t]*[:：][ \t]*([^\n]+)")
    for m in rx.finditer(s):
        body = m.group(1).strip()
        K = max(numbered(s[:m.start()], fam) + extra or nums)
        cm = _COUNT_ONLY.match(body)
        if cm and _num(cm.group(1)) is not None:          # 'Remaining labs: 4'
            n = K + one + _num(cm.group(1))
        else:
            parts = [p.strip() for p in _ARROWS.split(body) if p.strip()]
            arrows = len(parts) >= 2
            if not arrows:
                parts = [p.strip() for p in re.split(r"\s*[,·、]\s*", body) if p.strip()]
            if not 1 <= len(parts) <= 15 or any(_NOT_TITLE.search(p) or len(p) > 60 for p in parts):
                continue
            if not arrows and len(parts) < 2 and re.search(r"[가-힣]", m.group(0)):
                continue   # '다음 실습: STA' — 다음 것 하나일 뿐, 마지막이라는 말이 없다
            listed = [x for p in parts for x in numbered(p, fam)]
            n = max(listed) + one if listed else K + one + len(parts)
        if 1 <= n <= MAX_COUNT:
            return n, _line_of(s, m.start(), m.end()), K
    for rx2 in _remaining_patterns(fam):
        for m in rx2.finditer(s):
            r = _num(m.group(1))
            if not r:
                continue
            K = max(numbered(s[:m.start()], fam) + extra or nums)
            n = K + one + r
            if 1 <= n <= MAX_COUNT:
                return n, _line_of(s, m.start(), m.end()), K
    return None


def observed_low(numbers) -> int | None:
    """본 번호들 → 최소 개수: 0번이 있으면 최대 + 1, 아니면 최대."""
    nums = [n for n in numbers if isinstance(n, int) and 0 <= n <= MAX_COUNT]
    if not nums:
        return None
    return max(nums) + (1 if 0 in nums else 0)


def count_lines(texts: list[tuple[str, str]], fam: str, limit: int = 40) -> list[str]:
    """AI 작업 count_items에 보낼 줄: 그 계열 낱말이 있는 줄(중복 없이)."""
    rx = re.compile(rf"(?i)(?<![a-z])(?:{_w(fam)})")
    out, seen = [], set()
    for _title, t in texts:
        for line in str(t or "").splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if 4 <= len(line) <= 300 and rx.search(line) and line.lower() not in seen:
                seen.add(line.lower())
                out.append(line)
                if len(out) >= limit:
                    return out
    return out
