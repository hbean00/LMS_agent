"""글(공지, 메시지, 강의계획서, 붙여 넣은 메일)에서 마감 문장을 찾는다.

규칙 기반이다. AI 키 없이 누구 PC에서나 같은 결과가 나와야 하기 때문이다.
판단은 사람에게 맡긴다: 찾은 것마다 근거 문장과, 추정한 부분(연도, 시간, 오전/오후)을 notes로 남긴다.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# 마감 찾기 규칙이 바뀌면 올린다. 저장된 값과 다르면 다음 동기화 때 모든 글을 다시 읽는다.
# -3: 제목이 종류·이름을(결과 제목 제외), 다음 줄 "시간:", 통계 소수는 날짜 아님
# -4: 일반화(docs/CORNER_CASES.md) — 마침표 모양으로 문장 나누기, 이름 덩어리의 머리 낱말이 종류를 정함, 칸 줄(공백·'|'),
#     앞 줄 '시간:', 번호·버전·그림 번호·점수의 점 숫자는 날짜 아님, 앞에 쓴 영어 요일 확인, 점으로 쓴 시각
RULES_VERSION = "2026.11.13-4"

KO_WD = "월화수목금토일"
EN_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]


@dataclass
class Found:
    due: datetime
    sentence: str
    title_hint: str | None
    kind: str            # "과제" | "시험"
    method: str          # "email" | "lms" | "paper" | "unknown"
    email_to: str | None
    confidence: str      # "high" | "medium" | "low"
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 글 다듬기

_BLOCK = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6]|p|div|li|tr|h[1-6])\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", s)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in s.split("\n"))
    return "\n".join(ln for ln in lines if ln)


# ---------------------------------------------------------------- 문장 나누기
# 마침표가 문장 끝인지는 낱말 목록이 아니라 모양으로 정한다(docs/CORNER_CASES.md 가족 1):
#  - 뒤가 소문자로 이어지면 같은 문장("U.S. students", "5 p.m. on Friday")
#  - 뒤가 숫자·'(숫자'면 앞 조각이 줄임말 꼴(짧은 영문 조각·달·요일 이름)일 때 같은 문장("Oct. 22", "No. 3", "Eq. (2.1)")
#  - 앞 조각 안에 마침표가 또 있거나 한 글자면 줄임말·이니셜("e.g.", "U.S.", "J. Smith") — a.m./p.m.은 뒤가 날짜 낱말이 아닐 때 끝
#  - 호칭(Prof. Dr. Mr.)·절대 문장 끝이 아닌 줄임말(vs. cf. approx.)·달/요일 줄임말 + 날짜 낱말은 이어진다
#  - 숫자로 끝난 조각("Nov. 20.", "2026. 11. 20.")은 예전처럼 잇되, 양쪽에 각자 날짜가 있고 뒤가 대문자 영어 낱말이면 끊는다
#    ("Lab 3 is due Nov. 20. Lab 4 is due Nov. 27." → 두 마감)
_WD_FULL = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_HONORIFIC = {"mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "rev", "hon"}
_NEVER_FINAL = {"vs", "cf", "approx", "incl", "viz", "ca", "al", "resp", "esp", "ref", "refs", "fig", "figs", "eq",
                "eqs", "ch", "chap", "sec", "secs", "vol", "pp", "no", "nos", "ex", "tel", "ext", "dept", "univ"}
_CAND = re.compile(r"[.!?。](?=\s+\S)")


def _date_word(w: str) -> bool:
    """달·요일 이름(3글자 이상 앞부분): 'oct', 'sept', 'thurs', 'monday'."""
    w = w.lower()
    return len(w) >= 3 and (any(m.startswith(w) for m in EN_MONTHS) or w == "sept"
                            or any(d.startswith(w) for d in _WD_FULL))


def _has_date(s: str) -> bool:
    return bool(find_dates(s, date(2000, 6, 15)))


def _is_end(left: str, right: str, line_rest: str) -> bool:
    """left는 문장 부호로 끝나는 조각, right는 공백 뒤 글. 여기서 문장을 끊을지."""
    if left[-1] in "!?。":
        return True
    m = re.search(r"(\S*?)\s*\.$", left[-80:])
    tok = (m.group(1) if m else "").lstrip("([{\"'“‘")
    low = tok.lower().rstrip(".")
    nxt = (re.match(r"[A-Za-z]+", right) or [""])[0]
    first = right[:1]
    if re.search(r"\d$", tok):          # "Nov. 20." / "11. 20." / "2026 ."
        return (bool(re.match(r"[A-Z][A-Za-z]", right)) and nxt.upper() not in ("AM", "PM")
                and _has_date(left[-300:]) and _has_date(line_rest[:300]))
    if first.isascii() and first.islower():
        return False
    if first.isdigit() or re.match(r"[(\[]\s*\d", right):
        if _date_word(low) or low in _NEVER_FINAL or (low.isascii() and low.isalpha() and len(low) <= 4):
            return False
        return True
    if "." in tok.rstrip("."):            # e.g. / i.e. / U.S. / a.m. / p.m.
        if low.replace(".", "") in ("am", "pm"):
            return not _date_word(nxt)
        return False
    if len(low) == 1 and low.isalpha() and low.isascii():   # 이니셜 'J. Smith'
        return False
    if low in _HONORIFIC or low in _NEVER_FINAL:
        return False
    if _date_word(low) and _date_word(nxt):                   # 'Mon. Nov. 16', 'Thu. Oct. 22'
        return False
    return True


def split_sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        start = 0
        for m in _CAND.finditer(line):
            right = line[m.end():].lstrip()
            if _is_end(line[start:m.end()], right, right):
                part = line[start:m.end()].strip()
                if part:
                    out.append(part)
                start = m.end()
        rest = line[start:].strip()
        if rest:
            out.append(rest)
    return out


# ---------------------------------------------------------------- 키워드

STRONG = re.compile(r"(?i)마감|제출|기한|데드라인|due|deadline|submit|turn\s+in|보내\s*주|내\s*주세요")
UNTIL = re.compile(r"(?i)까지|until|\bby\b")
TASK = re.compile(r"(?i)과제|숙제|레포트|리포트|보고서|프로젝트|실습|에세이|(?<![a-z])(?:lab|hw|homework|assignment|report|project|essay)(?![a-z])")
EXAM = re.compile(r"(?i)시험|중간고사|기말고사|퀴즈|(?<![a-z])(?:quiz|exam|midterm)(?![a-z])")
# 수업이 없다는 글(휴강·보강·강의실 변경, 'no class') — 제출 낱말이 없으면 마감이 아니다
NEGATIVE = re.compile(r"(?i)휴강|보강|강의실\s*변경|수업\s*없|수업이\s*없|수업(?:은|이)?\s*진행(?:이|하지)?\s*(?:어렵|않)|"
                      r"class(?:es)?\s+(?:is\s+|are\s+)?cancel|(?<![a-z])no\s+(?:class|classes|lecture|lectures)(?![a-z])|"
                      r"class(?:es)?\s+will\s+not\s+(?:be\s+held|meet)")

EMAIL_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_KW = re.compile(r"(?i)이메일|메일|e-?mail")
LMS_KW = re.compile(r"(?i)canvas|캔버스|lms|과제\s*게시판|과제함|e-?class|이클래스")
PAPER_KW = re.compile(r"(?i)출력|프린트|인쇄|하드\s*카피|hard\s*copy|오프라인\s*제출|직접\s*제출|조교실|과사무실|우편함")


def detect_method(text: str) -> tuple[str, str | None]:
    addr = EMAIL_ADDR.search(text)
    if addr or EMAIL_KW.search(text):
        return "email", addr.group(0) if addr else None
    if LMS_KW.search(text):
        return "lms", None
    if PAPER_KW.search(text):
        return "paper", None
    return "unknown", None


# ---------------------------------------------------------------- 이름 덩어리(phrase)
# 'Midterm Project', 'Final Exam Review session', '중간고사 대체 과제'처럼 붙어 있는 낱말 덩어리에서는
# **마지막 낱말(머리)이 종류를 정한다**(docs/CORNER_CASES.md 가족 2). 낱말마다 종류:
#   exam 시험 · task 과제 · event 발표(학기 시점·과제 낱말 뒤일 때만 과제 — '성적 발표'는 알림)
#   meta 시험·과제에 '관한' 것(범위·성적·복습·해설·주간 — 그 자체는 일정이 아님)
#   thru 종류를 바꾸지 않는 말(공지·안내·일정·시간·장소·session) · term 학기 시점(중간·기말 — 뒤 낱말이 정함)
#   weak 'test': 학기 시점·번호가 붙을 때만 시험('Midterm test', 'Test 2' — 'test your code'는 아님)
_LEX = [
    ("exam", "midterm", r"중간\s*고사|중간\s*시험|중간\s*평가"),
    ("exam", "final", r"기말\s*고사|기말\s*시험|기말\s*평가"),
    ("exam", "quiz", r"쪽지\s*시험|퀴즈|quiz(?:zes|es)?"),
    ("exam", "exam", r"시험|고사|exams?|examinations?"),
    ("weak", "exam", r"tests?"),
    ("term", "midterm", r"mid[\s\-]?terms?|중간"),
    ("term", "final", r"finals?|기말"),
    ("task", "hw", r"과제(?:물)?|숙제|homeworks?|hws?|assignments?|problem\s+sets?|psets?"),
    ("task", "report", r"레포트|리포트|보고서|reports?|write-?ups?"),
    ("task", "project", r"프로젝트|projects?"),
    ("task", "lab", r"실습|실험|labs?"),
    ("task", "essay", r"에세이|essays?"),
    ("task", "proposal", r"제안서|proposals?"),
    ("task", "paper", r"논문|papers?"),
    ("event", "presentation", r"발표|시연|presentations?|demos?|demonstrations?|defen[cs]es?"),
    ("meta", None, r"범위|성적|결과|점수|채점|해설|풀이|정답|복습|리뷰|설명회|통계|분포|주간|기간|대비|순서|자료|수업|강의|감독|"
                   r"이의\s*신청|면제|reviews?|scopes?|coverage|range|results?|grades?|grading|scores?|solutions?|answers?|"
                   r"keys?|feedback|surveys?|guides?|statistics|stats|distributions?|weeks?|period|break|prep|preparation|"
                   r"study|regrades?|appeals?|slides?|materials?|order|class(?:es)?|lectures?|proctors?|proctoring|"
                   r"polic(?:y|ies)|rubrics?|templates?|samples?|examples?|questions?|topics?|syllabus|outline|"
                   r"average|mean|median"),
    ("thru", None, r"공지|안내|일정|공고|알림|관련|사항|시간|장소|일시|날짜|일자|세션|notices?|announcements?|"
                   r"info(?:rmation)?|schedules?|details|logistics|reminders?|updates?|sessions?|hours?|time|dates?|"
                   r"days?|rooms?|locations?|venue|instructions?|guidelines?|requirements?|descriptions?|specs?|"
                   r"specifications?"),
]
_LEX_RX = re.compile("(?i)(?<![A-Za-z])(?:" + "|".join(f"(?P<w{i}>{rx})" for i, (_, _, rx) in enumerate(_LEX))
                     + ")(?![A-Za-z])")
# 번호 뒤에 월·일·시 등이 오면 날짜·시간의 숫자이지 과제 번호가 아니다("기말고사 12월 12일").
_NOT_NUM = r"(?!\d)(?!\s*(?:월|일|시|분|주|쪽|명|점|%|[:./~\-]\s*\d))"
_NUM_AFTER = re.compile(r"[ \t]*#?[ \t]*(\d{1,2})" + _NOT_NUM)
_NUM_BEFORE = re.compile(r"(?i)(?<!\d)(\d{1,2})\s*(?:차|번째?|st|nd|rd|th)\s*$")
_LINK_STOP = {"and", "or", "the", "a", "an", "of", "for", "on", "in", "at", "by", "to", "is", "are", "was", "were",
              "will", "be", "been", "with", "from", "after", "before", "this", "that", "these", "those", "next", "last",
              "each", "every", "all", "any", "no", "not", "your", "our", "their", "its", "as", "than", "then", "due",
              "until", "via", "per", "about", "into", "over", "under", "has", "have", "had", "can", "may", "must",
              "또는", "그리고", "이번", "다음", "지난", "모든", "각"}


@dataclass
class _Word:
    start: int
    end: int        # 낱말 끝
    tail: int       # 번호까지 포함한 끝
    cat: str
    fam: str | None
    n: str | None
    raw: str


@dataclass
class _Phrase:
    start: int
    end: int
    cat: str | None          # exam | task | event | meta | None(투명한 말뿐)
    name: str | None         # 'Midterm Project', 'LAB 3', '기말 프로젝트 발표'
    term: str | None         # midterm | final
    fams: frozenset          # 종류 계열(exam·quiz·project·report…) — same_task가 쓴다
    words: list


def _words(text: str) -> list[_Word]:
    out = []
    for m in _LEX_RX.finditer(text):
        i = int(m.lastgroup[1:])
        cat, fam, _ = _LEX[i]
        n, tail = None, m.end()
        if cat != "thru":
            mn = _NUM_AFTER.match(text, m.end())
            if mn:
                n, tail = mn.group(1), mn.end()
            else:
                mp = _NUM_BEFORE.search(text[max(0, m.start() - 8):m.start()])
                n = mp.group(1) if mp else None
        out.append(_Word(m.start(), m.end(), tail, cat, fam, n, m.group(0)))
    return out


def _links(gap: str) -> bool:
    """두 낱말이 한 덩어리인지: 사이가 공백뿐이거나, 공백으로 둘러싸인 모르는 낱말 하나('대체', 'take-home')."""
    if re.fullmatch(r"[ \t]*", gap):
        return True
    m = re.fullmatch(r"[ \t]+([A-Za-z][A-Za-z\-]{2,}|[가-힣]{2,4})[ \t]+", gap)
    if not m:
        return False
    w = m.group(1)
    return w.lower() not in _LINK_STOP and not re.search(r"[은는이가을를의에와과도로]$", w)


def _show(seg: str) -> str:
    out = []
    for tok in seg.split():
        low = tok.lower()
        if low in ("lab", "hw"):
            out.append(low.upper())
        elif re.fullmatch(r"[A-Za-z][A-Za-z\-]*", tok):
            out.append(tok if tok.isupper() and len(tok) > 1 and not _LEX_RX.fullmatch(tok) else tok.title())
        else:
            out.append(tok)
    return " ".join(out)


def _phrase(ws: list[_Word], text: str) -> _Phrase:
    core = [w for w in ws if w.cat != "thru"]
    term = next((w.fam for w in ws if w.cat == "term" or (w.cat == "exam" and w.fam in ("midterm", "final"))), None)
    cat = None
    if core:
        head = core[-1]
        prev = core[-2] if len(core) > 1 else None
        cat = head.cat
        if cat == "event":       # '프로젝트 발표'·'Final presentation'만 발표 일정, '성적 발표'는 알림
            cat = "event" if prev is not None and prev.cat in ("term", "task") else \
                "meta" if prev is not None and prev.cat in ("meta", "exam") else None
        elif cat == "weak":
            cat = "exam" if (term or head.n or (prev is not None and prev.cat == "exam")) else None
        elif cat == "term":      # 'Midterm' 혼자면 시험, 'final'·'finals'(기간)·'중간' 혼자면 알 수 없음
            cat = "exam" if prev is None and head.raw.lower().startswith("mid") else None
    name = None
    if cat in ("exam", "task", "event"):
        lead = next((w for w in ws if w.cat in ("term", "exam", "task", "event", "weak")), core[0])
        k = ws.index(lead)
        stop = next((w for w in ws[k:] if w.n), core[-1])   # 번호가 붙은 낱말에서 이름이 끝난다('LAB 3 보고서' → 'LAB 3')
        name = _show(text[lead.start:stop.end]) + (f" {stop.n}" if stop.n else "")
    fams = set()   # 머리와 같은 종류의 낱말 계열만('중간고사 대체 과제' → {hw}, 'Midterm project report' → {project, report})
    for w in core:
        if cat == "exam" and (w.cat in ("exam", "weak") or (w.cat == "term" and w is core[-1])):
            fams.add("quiz" if w.fam == "quiz" else "exam")
        elif cat in ("task", "event") and w.cat in ("task", "event"):
            fams.add(w.fam)
    return _Phrase(ws[0].start, ws[-1].tail, cat, name, term, frozenset(fams), ws)


def phrases(text: str | None) -> list[_Phrase]:
    """글 속 이름 덩어리들(글 순서)."""
    text = text or ""
    out, cur = [], []
    for w in _words(text):
        if cur and _links(text[cur[-1].tail:w.start]):
            cur.append(w)
            continue
        if cur:
            out.append(_phrase(cur, text))
        cur = [w]
    if cur:
        out.append(_phrase(cur, text))
    return out


def _kind_of(sentence: str, ps: list[_Phrase] | None = None) -> str | None:
    ps = phrases(sentence) if ps is None else ps
    strong = bool(STRONG.search(sentence))
    if NEGATIVE.search(sentence) and not strong:
        return None
    cats = {p.cat for p in ps}
    if "exam" in cats:
        return "시험"
    if "event" in cats:
        return "과제"     # 'Midterm Presentation: Nov. 19' — 발표하는 날
    if strong or (UNTIL.search(sentence) and "task" in cats):
        return "과제"
    return None


def deadline_kind(sentence: str) -> str | None:
    return _kind_of(sentence)


# 점수 통계·학점 낱말 바로 뒤의 점·빗금 숫자('표준편차 12.1', 'score: 9/10')는 날짜가 아니다
_STAT_BEFORE = re.compile(r"(?i)(?:평균|표준\s*편차|편차|중앙값|점수|최고점?|최저점?|만점|평점|학점|백분위|비율|비중|가중치|"
                          r"mean|avg|average|sd|std|stdev|median|scores?|max|min|gpa|grade|rate|percentile|weight)"
                          r"\s*[:：=]?\s*\(?\s*$")
# 번호를 붙여 부르는 것('Fig. 3.4', 'Section 1.2', 'v1.2', 'Problem 2.3', '식 (2.1)', 'at 10.30') 뒤의 점 숫자는 날짜가 아니다
_REF_BEFORE = re.compile(r"(?i)(?:(?<![a-z])(?:fig(?:ure)?s?|eqs?|equations?|tables?|tab|sec(?:tion)?s?|ch(?:ap(?:ter)?)?s?|"
                         r"parts?|pages?|pp?|vol(?:ume)?|ver(?:sion)?|v|rev(?:ision)?|release|steps?|items?|appendix|"
                         r"problems?|prob|exercises?|ex|questions?|q|lemma|theorem|thm|def(?:inition)?|example|at)|§|@|"
                         r"(?<![가-힣])(?:그림|식|표|절|장|쪽|페이지|버전|부록|단계|문제|문항|연습\s*문제|예제|정리))"
                         r"\.?\s*[:#]?\s*[(\[]?\s*$")
# 과제·실습 번호('Lab 2.1')는 바로 뒤에 '까지'·'by' 같은 마감 말이 없을 때만 번호로 본다('과제 11.20까지'는 날짜)
_ITEM_BEFORE = re.compile(r"(?i)(?:(?<![a-z])(?:labs?|hws?|homeworks?|assignments?|quiz(?:zes)?|projects?|tasks?)|"
                          r"(?<![가-힣])(?:과제|실습|퀴즈|프로젝트))\s*#?\s*$")
_DUE_AFTER = re.compile(r"(?i)^\s*(?:까지|마감|전까지|until\b|by\b|due\b)")
# 결과·성적을 알리는 제목
_RESULT_TITLE = re.compile(r"(?i)성적|점수|결과|채점|score|result|grade|graded|feedback")

# '날짜: 12월 15일' / '시간: 10:30am' 같은 칸 줄(제목이 종류를 말해 줄 때 쓴다). 칸 구분은 ':'·'|' 또는
# 표 칸처럼 공백 뒤에 바로 날짜·시각이 오는 경우('날짜 12/15' — HTML 표 <td>날짜</td><td>12/15</td>)
_DATE_START = r"(?=\(?\d|(?:mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s)"
_FIELD_SEP = r"\s*[\])>]?\s*(?:[:：|]|(?<=[\w)\]>])\s+"
_FIELD_DATE = re.compile(r"(?i)^\s*[\[(<]?\s*(?:날짜|일시|일자|시험\s*(?:일|일시|일자|날짜)|일정|date|when|"
                         r"exam\s*date|time\s*(?:&|and)\s*date|date\s*(?:&|and)\s*time)" + _FIELD_SEP + _DATE_START + ")")
_FIELD_TIME = re.compile(r"(?i)^\s*[\[(<]?\s*(?:시간|시각|time|start\s*time|시작\s*시간)" + _FIELD_SEP
                         + r"(?=\d|오전|오후|[ap]\.?m))")


# ---------------------------------------------------------------- 과제 이름

_TITLE_WORDS = (r"과제|숙제|레포트|리포트|보고서|프로젝트|실습|퀴즈|중간고사|기말고사|중간\s*시험|기말\s*시험|시험|"
                r"lab|hw|homework|assignment|report|project|quiz|midterm|final\s+exam|final\s+report|exam")
_TITLE_A = re.compile(r"(?i)(?<![a-z])(?P<w>" + _TITLE_WORDS + r")(?![a-z])(?:\s*#?\s*(?P<n>\d{1,2})" + _NOT_NUM + r")?")
_TITLE_B = re.compile(r"(?i)(?<!\d)(?P<n>\d{1,2})\s*(?:차|번째?)\s*(?P<w>" + _TITLE_WORDS + r")(?![a-z])")

_SYN = {
    "과제": "hw", "숙제": "hw", "hw": "hw", "homework": "hw", "assignment": "hw",
    "레포트": "report", "리포트": "report", "보고서": "report", "report": "report", "final report": "report",
    "프로젝트": "project", "project": "project", "실습": "lab", "lab": "lab",
    "퀴즈": "quiz", "quiz": "quiz", "중간고사": "midterm", "중간시험": "midterm", "midterm": "midterm",
    "기말고사": "final", "기말시험": "final", "final exam": "final", "시험": "exam", "exam": "exam",
}


def title_hint(text: str, prefer: str | None = None) -> str | None:
    """글에서 과제·시험 이름. 덩어리의 머리가 시험·과제·발표인 것 중 첫 번째(prefer="시험"|"과제"면 그 종류 먼저).
    '기말고사 범위'·'Midterm grades'처럼 머리가 범위·성적인 덩어리는 이름이 아니다."""
    ps = [p for p in phrases(text) if p.name]
    if not ps:
        return None
    if prefer:
        want = ("exam",) if prefer == "시험" else ("task", "event")
        for p in ps:
            if p.cat in want:
                return p.name
    return ps[0].name


def title_tokens(text: str | None) -> set[tuple[str, str | None]]:
    """'LAB4 보고서' -> {('lab','4'), ('report', None)}. 같은 과제인지 비교할 때 쓴다.
    학기 시점이 붙은 덩어리('Final Project', '기말 보고서')는 ('final', None)도 넣는다(한 과제를 가리키는 제목)."""
    out: set[tuple[str, str | None]] = set()
    if not text:
        return out
    for rx in (_TITLE_A, _TITLE_B):
        for m in rx.finditer(text):
            key = _SYN.get(re.sub(r"\s+", "" if re.match(r"[가-힣]", m.group("w")) else " ", m.group("w").lower()))
            if key:
                out.add((key, m.group("n")))
    for p in phrases(text):
        if p.term and p.cat in ("exam", "task", "event"):
            out.add((p.term, None))
    return out


def _signatures(text: str | None) -> set:
    return {(p.term, p.fams) for p in phrases(text) if p.term and p.fams and p.cat in ("exam", "task", "event")}


def same_task(a: str | None, b: str | None) -> bool:
    """같은 과제·시험인지: 같은 번호의 같은 종류('LAB4 보고서'↔'LAB 4', '과제 3'↔'Homework 3'), 또는 같은 학기 시점에
    같은 머리 종류('Midterm'↔'중간고사', 'Final Report'↔'기말 보고서'). 'Midterm Project'와 'Midterm Exam'은 다르다."""
    ta, tb = title_tokens(a), title_tokens(b)
    for k, n in ta:
        if n is not None and (k, n) in tb:
            return True
    for t1, f1 in _signatures(a):
        for t2, f2 in _signatures(b):
            if t1 == t2 and f1 & f2:
                return True
    return False


# ---------------------------------------------------------------- 날짜

_WD_AFTER = (r"(?:\s*\(\s*(?P<wd>[월화수목금토일]|[A-Za-z]{3})[A-Za-z]*\.?\s*(?:요일)?\s*\)"
             r"|\s+(?P<wd2>[월화수목금토일])요일)?")
RX_YMD = re.compile(r"(?<!\d)(?P<y>20\d{2})\s*(?:[.\-/]|년)\s*(?P<m>\d{1,2})\s*(?:[.\-/]|월)\s*(?P<d>\d{1,2})(?!\d)\s*(?:일)?\.?" + _WD_AFTER)
RX_MD_KO = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일" + _WD_AFTER)
RX_MD_NUM = re.compile(r"(?<![\d./\-:])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d:])(?!\s*[./]\s*\d)\.?" + _WD_AFTER)
# 'Thu., Oct. 22'·'Monday, Dec. 15'처럼 앞에 쓴 요일도 날짜에 넣는다(요일이 맞는지 확인하고, 요일만 따로 세지 않음)
_EN_WD_BEFORE = r"(?:(?P<wdb>(?:mon|tue|wed|thu|fri|sat|sun)[a-z]{0,6})\.?,?\s+)?"
RX_EN_MD = re.compile(r"(?i)(?<![a-z])" + _EN_WD_BEFORE + r"(?P<mon>[a-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?![\d:])"
                      r"(?:,?\s*(?P<y>20\d{2}))?" + _WD_AFTER)
RX_EN_DM = re.compile(r"(?i)(?<![\d:])(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[a-z]{3,9})(?![a-z])\.?(?:,?\s*(?P<y>20\d{2}))?")

RX_REL = re.compile(r"오늘|금일|내일|명일|모레")
RX_REL_WD = re.compile(r"(?P<wk>다다음\s*주|다음\s*주|차주|담주|이번\s*주|금주)?\s*(?P<wd>[월화수목금토일])요일")
RX_EN_REL = re.compile(r"(?i)(?<![a-z])(?:(?P<rel>today|tonight|tomorrow)|(?:(?P<wk>this|next)\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday))(?![a-z])")

# 숫자 날짜(11/20, 11.20)는 소수·점수와 헷갈리므로, 뒤에 마감다운 말이 올 때만 날짜로 본다.
_NUM_OK_AFTER = re.compile(r"(?i)^\s*(?:까지|마감|자정|정오|오전|오후|저녁|밤|새벽|\d{1,2}\s*[:시]|\d{1,2}\s*[ap]\.?m|[~\-–]|by\b|at\b|until\b|due\b|$|[,)\]])")
_NUM_OK_BEFORE = re.compile(r"(?i)(?:마감|기한|due|deadline|by|until|[~\-–]|(?<![가-힣])(?:일시|날짜|일자)|"
                            r"(?<![a-z])(?:date|when))\s*(?:일|일시|은|는)?\s*[:：|]?\s*$")
_PICK_AFTER = re.compile(r"(?i)^[^가-힣a-z]{0,20}(?:까지|마감|전까지|until)|^\s*(?:\S+\s*){0,3}?(?:까지|마감)")
_PICK_BEFORE = re.compile(r"(?i)(?:마감|기한|due|deadline|by|until|[~\-–])\s*(?:일|일시|은|는|:|：)?\s*$")


@dataclass
class _DateHit:
    start: int
    end: int
    day: date
    relative: bool
    notes: list[str]
    weekday_bad: bool = False


def _wd_index(s: str | None) -> int | None:
    if not s:
        return None
    if s in KO_WD:
        return KO_WD.index(s)
    s = s[:3].lower()
    return EN_WD.index(s) if s in EN_WD else None


def _month_from_word(w: str) -> int | None:
    w = w.lower()
    if len(w) < 3:
        return None
    for i, full in enumerate(EN_MONTHS):
        if full.startswith(w) or (w == "sept" and i == 8):
            return i + 1
    return None


def _infer_year(m: int, d: int, wd: int | None, ref: date) -> tuple[date | None, list[str]]:
    cands = []
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cands.append(date(y, m, d))
        except ValueError:
            pass
    if not cands:
        return None, []

    def cost(c: date) -> int:  # 지난 날짜보다 다가오는 날짜를 고른다
        diff = (c - ref).days
        return diff if diff >= 0 else -diff * 3

    if wd is not None:  # 요일이 맞는 해를 고르되, 반년 넘게 떨어진 해로 건너뛰지는 않는다
        matching = [c for c in cands if c.weekday() == wd and cost(c) <= 180]
        if matching:
            cands = matching

    best = min(cands, key=cost)
    notes = [] if best.year == ref.year else [f"연도 미기재 → {best.year}년으로 추정"]
    return best, notes


def _abs_hit(m: re.Match, mon: int, d: int, y: int | None, ref: date) -> _DateHit | None:
    gd = m.groupdict()
    wdb = gd.get("wdb")
    if wdb and not any(full.startswith(wdb.lower()) for full in _WD_FULL):
        wdb = None   # 'sunny' 같은 낱말은 요일이 아니다
    wd = _wd_index(gd.get("wd") or gd.get("wd2") or wdb)
    notes: list[str] = []
    if y is not None:
        try:
            day = date(y, mon, d)
        except ValueError:
            return None
    else:
        day, notes = _infer_year(mon, d, wd, ref)
        if day is None:
            return None
    bad = wd is not None and day.weekday() != wd
    if bad:
        notes.append(f"요일 불일치: 글에는 {KO_WD[wd]}요일, 실제 {day.month}/{day.day}은 {KO_WD[day.weekday()]}요일 — 확인 필요")
    return _DateHit(m.start(), m.end(), day, False, notes, bad)


def find_dates(s: str, ref: date) -> list[_DateHit]:
    hits: list[_DateHit] = []

    def free(a: int, b: int) -> bool:
        return all(b <= h.start or a >= h.end for h in hits)

    for m in RX_YMD.finditer(s):
        h = _abs_hit(m, int(m["m"]), int(m["d"]), int(m["y"]), ref)
        if h and free(h.start, h.end):
            hits.append(h)
    for m in RX_MD_KO.finditer(s):
        h = _abs_hit(m, int(m["m"]), int(m["d"]), None, ref)
        if h and free(h.start, h.end):
            hits.append(h)
    time_end = -1   # 날짜 바로 뒤의 점 숫자('12/15 10.30~11.30')는 시각이다
    for m in RX_MD_NUM.finditer(s):
        if not free(m.start(), m.end()):
            continue
        before = s[max(0, m.start() - 24):m.start()]
        if not (m["wd"] or m["wd2"]):
            if _STAT_BEFORE.search(before[-16:]):
                continue   # '표준편차 12.1'·'mean: 10.3'·'score: 9/10'은 점수지 날짜가 아니다
            if "." in s[m.start():m.end("d")]:
                if _REF_BEFORE.search(before):
                    continue   # 'Fig. 3.4'·'Section 1.2'·'v1.2'·'식 (2.1)'·'at 10.30'
                if _ITEM_BEFORE.search(before) and not _DUE_AFTER.match(s[m.end():]):
                    continue   # 'Lab 2.1'(번호) — '과제 11.20까지'는 날짜
                prev = max((h.end for h in hits if h.end <= m.start()), default=None)
                if (prev is not None and re.fullmatch(r"[ \t]{0,2}", s[prev:m.start()])) or \
                        (time_end >= 0 and re.fullmatch(r"\s*[~\-–]\s*", s[time_end:m.start()])):
                    time_end = m.end()
                    continue
        if not (m["wd"] or m["wd2"] or _NUM_OK_AFTER.match(s[m.end():])
                or _NUM_OK_BEFORE.search(s[:m.start()])):
            continue
        h = _abs_hit(m, int(m["m"]), int(m["d"]), None, ref)
        if h:
            hits.append(h)
    for rx in (RX_EN_MD, RX_EN_DM):
        for m in rx.finditer(s):
            mon = _month_from_word(m["mon"])
            if mon is None or not free(m.start(), m.end()):
                continue
            h = _abs_hit(m, mon, int(m["d"]), int(m["y"]) if m["y"] else None, ref)
            if h:
                hits.append(h)

    def after_abs(pos: int) -> bool:  # "11월 20일 금요일"처럼 날짜 뒤에 붙은 요일은 따로 세지 않는다
        return any(h.end <= pos <= h.end + 4 for h in hits if not h.relative)

    rel_note = f"게시일({ref.month}/{ref.day}) 기준으로 계산한 날짜"
    rel_hits: list[_DateHit] = []
    for m in RX_REL.finditer(s):
        if free(m.start(), m.end()):
            off = {"오늘": 0, "금일": 0, "내일": 1, "명일": 1, "모레": 2}[m.group(0)]
            rel_hits.append(_DateHit(m.start(), m.end(), ref + timedelta(days=off), True, [rel_note]))
    for m in RX_REL_WD.finditer(s):
        if free(m.start(), m.end()) and not after_abs(m.start()):
            rel_hits.append(_DateHit(m.start(), m.end(), _weekday_date(ref, KO_WD.index(m["wd"]), m["wk"]), True, [rel_note]))
    for m in RX_EN_REL.finditer(s):
        if not free(m.start(), m.end()) or after_abs(m.start()):
            continue
        if m["rel"]:
            day = ref + timedelta(days=1 if m["rel"].lower() == "tomorrow" else 0)
        else:
            wk = {"this": "이번주", "next": "다음주"}.get((m["wk"] or "").lower())
            day = _weekday_date(ref, EN_WD.index(m["wd"][:3].lower()), wk)
        rel_hits.append(_DateHit(m.start(), m.end(), day, True, [rel_note]))
    hits.extend(rel_hits)
    hits.sort(key=lambda h: h.start)
    return hits


def _weekday_date(ref: date, wd: int, wk: str | None) -> date:
    wk = re.sub(r"\s+", "", wk or "")
    monday = ref - timedelta(days=ref.weekday())
    if wk in ("이번주", "금주"):
        return monday + timedelta(days=wd)
    if wk in ("다음주", "차주", "담주"):
        return monday + timedelta(days=7 + wd)
    if wk == "다다음주":
        return monday + timedelta(days=14 + wd)
    ahead = (wd - ref.weekday()) % 7  # "금요일까지" → 게시일 이후 처음 오는 금요일
    return ref + timedelta(days=ahead)


def _choose(s: str, hits: list[_DateHit]) -> _DateHit:
    if len(hits) == 1:
        return hits[0]
    marked = [h for h in hits if _PICK_AFTER.match(s[h.end:]) or _PICK_BEFORE.search(s[max(0, h.start - 15):h.start])]
    pool = marked or hits
    return max(pool, key=lambda h: (not h.relative, h.day))


# ---------------------------------------------------------------- 시간

_AP = r"(?P<ap1>오전|오후|저녁|밤|새벽|아침|낮)"
RX_T_COLON = re.compile(r"(?:" + _AP + r"\s*)?(?<!\d)(?P<h>\d{1,2})\s*:\s*(?P<mi>\d{2})(?!\d)(?:\s*(?P<ap2>[AaPp]\.?\s?[Mm]\.?)(?![a-zA-Z]))?")
RX_T_KO = re.compile(r"(?:" + _AP + r"\s*)?(?<!\d)(?P<h>\d{1,2})\s*시(?!간)(?:\s*(?P<mi>\d{1,2})\s*분|\s*(?P<half>반))?")
RX_T_EN = re.compile(r"(?<![\d:.])(?P<h>\d{1,2})\s*(?P<ap2>[AaPp]\.?\s?[Mm]\.?)(?![a-zA-Z])")
# 점으로 쓴 시각('10.30 am', 'at 11.30') — 오전/오후 표시가 있거나 'at'·'@' 뒤일 때만(그 밖의 '11.30'은 날짜 후보)
RX_T_DOT_AP = re.compile(r"(?<![\d.])(?P<h>\d{1,2})\.(?P<mi>[0-5]\d)(?!\d)\s*(?P<ap2>[AaPp]\.?\s?[Mm]\.?)(?![a-zA-Z])")
RX_T_DOT_AT = re.compile(r"(?i)(?:(?<=\bat\s)|(?<=@)|(?<=@\s))(?P<h>\d{1,2})\.(?P<mi>[0-5]\d)(?!\d)")
# 날짜 바로 뒤('12/15 10.30~11.30') — find_time은 날짜 뒤 글을 받으므로 맨 앞만 본다
RX_T_DOT_LEAD = re.compile(r"^[ \t]{1,2}(?P<h>\d{1,2})\.(?P<mi>[0-5]\d)(?![\d.])")
RX_T_WORD = re.compile(r"(?i)자정|정오|midnight|noon")

MIDNIGHT_NOTE = "'자정'은 그날 밤 23:59로 둠 — 다음 날 0시로 읽힐 수도 있어 확인 필요"


def _resolve_time(m: re.Match, rx) -> tuple[time | None, list[str]]:
    if rx is RX_T_WORD:
        w = m.group(0).lower()
        if w in ("자정", "midnight"):
            return time(23, 59), [MIDNIGHT_NOTE]
        return time(12, 0), []
    gd = m.groupdict()
    h = int(gd["h"])
    mi = int(gd.get("mi") or 0)
    if gd.get("half"):
        mi = 30
    ap = (gd.get("ap1") or gd.get("ap2") or "").lower().replace(".", "").replace(" ", "")
    notes: list[str] = []
    if h > 24 or mi > 59:
        return None, []
    if h == 24:
        return time(23, 59), ["24시 → 23:59로 둠"]
    if ap in ("오후", "저녁", "밤", "pm"):
        if ap == "밤" and h == 12:
            return time(23, 59), [MIDNIGHT_NOTE]
        if h < 12:
            h += 12
    elif ap in ("오전", "새벽", "아침", "am"):
        if h == 12:
            h = 0
    elif ap == "낮":
        if h <= 6:
            h += 12
    elif rx is RX_T_KO and 1 <= h <= 7:
        h += 12
        notes.append(f"오전/오후 미기재 → 오후 {h - 12}시로 둠(확인 필요)")
    elif rx in (RX_T_COLON, RX_T_DOT_AT, RX_T_DOT_LEAD) and 1 <= h <= 7:
        notes.append("오전/오후 미기재 → 적힌 그대로 둠(확인 필요)")
    return time(h, mi), notes


def find_time(s: str) -> tuple[time | None, list[str]]:
    best = None
    for rx in (RX_T_COLON, RX_T_DOT_AP, RX_T_DOT_AT, RX_T_DOT_LEAD, RX_T_KO, RX_T_EN, RX_T_WORD):
        for m in rx.finditer(s):
            if best is None or m.start() < best[0].start():
                best = (m, rx)
            break
    if not best:
        return None, []
    return _resolve_time(*best)


# ---------------------------------------------------------------- 본체

_CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


def find_deadlines(text: str, ref: datetime, *, is_html: bool = False, title: str | None = None) -> list[Found]:
    """text에서 마감을 찾는다. ref는 글이 게시된 시각(로컬 시간대가 붙은 datetime).
    title(글 제목)이 '기말고사 공지'처럼 무엇인지 말해 주면, 본문의 '날짜: 12월 15일' 같은 칸 줄(또는 날짜가 하나뿐인 본문)에
    그 종류·이름을 쓴다 — 실제 공지는 '기말고사'가 제목에만 있고 본문은 날짜·시간·장소 칸뿐인 경우가 많다."""
    plain = html_to_text(text) if is_html else (text or "")
    lines = split_sentences(plain)
    msg_method, msg_email = detect_method(plain)
    ref_day = ref.date()
    found: dict[datetime, Found] = {}
    # 자기 날짜가 있는 줄(표의 다른 행, 다른 마감)은 옆 마감의 제출 방법 근거로 쓰지 않는다.
    dated = [bool(find_dates(ln, ref_day)) for ln in lines]
    sentence_phrases = [phrases(ln) for ln in lines]
    deadline_lines = sum(1 for ln, d, ps in zip(lines, dated, sentence_phrases) if d and _kind_of(ln, ps))
    # 결과를 알리는 제목('중간고사 성적 안내', 'Quiz 1 grade released')·범위/복습 제목('기말고사 범위 안내',
    # 'Final Exam Review' — 머리 낱말이 범위·복습)은 일정이 아니므로 종류를 빌려주지 않는다
    title_kind = _kind_of(title) if title and not _RESULT_TITLE.search(title) else None

    for i, s in enumerate(lines):
        ps = sentence_phrases[i]
        kind = _kind_of(s, ps)
        from_title = False
        # 제목의 종류를 빌리는 줄: 날짜 칸 줄이거나 날짜가 하나뿐인 글, 그리고 그 줄 자체에는 휴강 말도,
        # 다른 종류·범위·성적 낱말도 없을 때('Lab 5 보고서는 12월 1일…'·'12월 10일 복습 세션'은 빌리지 않음)
        if (not kind and title_kind and dated[i] and (_FIELD_DATE.match(s) or sum(dated) == 1)
                and not NEGATIVE.search(s) and not any(p.cat for p in ps)):
            kind, from_title = title_kind, True
        if not kind:
            continue
        hits = find_dates(s, ref_day)
        if not hits:
            continue
        h = _choose(s, hits)
        nxt = min((x.start for x in hits if x.start >= h.end), default=len(s))
        t, tnotes = find_time(s[h.end:nxt])
        if t is None and len(hits) == 1:
            t, tnotes = find_time(s[:h.start])
        if t is None:   # '날짜: 12월 15일' 앞뒤 줄의 '시간: 10:30am - 11:30am'(뒤 줄 먼저)
            for ln in lines[i + 1:i + 3] + lines[max(0, i - 2):i][::-1]:
                if _FIELD_TIME.match(ln) and not find_dates(ln, ref_day):
                    t, tnotes = find_time(ln)
                    if t is not None:
                        tnotes = tnotes + ["시간은 옆 줄의 '시간' 칸에서 읽음"]
                    break
        notes = list(h.notes) + tnotes
        if from_title:
            notes.append(f"제목 '{title.strip()[:40]}'을(를) 보고 {kind}(으)로 봄")
        elif kind == "과제" and not STRONG.search(s) and any(p.cat == "event" for p in ps):
            notes.append("발표하는 날로 봄(제출 마감이 따로 있을 수 있음)")
        has_time = t is not None
        if t is None:
            t = time(23, 59)
            notes.append("시간 미기재 → 23:59로 둠")

        window = " ".join(ln for j, ln in enumerate(lines[max(0, i - 2):i + 3], start=max(0, i - 2))
                          if j == i or not dated[j])
        single = deadline_lines <= 1  # 글 전체를 근거로 삼는 것은 마감이 하나뿐인 글일 때만
        method, email_to = detect_method(s)
        if method == "unknown":
            method, email_to = detect_method(window)
        if method == "unknown" and msg_method != "unknown" and single:
            method, email_to = msg_method, msg_email
        if method == "email" and not email_to:
            addr = EMAIL_ADDR.search(window) or (EMAIL_ADDR.search(plain) if single else None)
            email_to = addr.group(0) if addr else None

        hint = title_hint(s, kind) or title_hint(" ".join(lines[max(0, i - 2):i]), kind)
        if from_title:
            hint = title_hint(title, kind) or hint   # 이 줄의 종류를 제목에서 가져왔으면 이름도 제목에서
        if kind == "과제" and hint and not any(p.cat in ("exam", "task", "event") for p in ps):
            # 이 문장은 '마감: …'뿐이고 이름은 앞 줄에서 왔다 → 이름 덩어리의 종류를 따른다('Midterm Exam' / '마감 …')
            if any(p.cat == "exam" for p in phrases(hint)):
                kind = "시험"

        score = (0 if h.relative else 2) + (1 if has_time else 0) + (1 if STRONG.search(s) else 0) - (2 if h.weekday_bad else 0)
        conf = "high" if score >= 4 else "medium" if score >= 2 else "low"

        due = datetime.combine(h.day, t, tzinfo=ref.tzinfo)
        f = Found(due, s, hint, kind, method, email_to, conf, notes)
        old = found.get(due)
        if old is None or _CONF_ORDER[conf] > _CONF_ORDER[old.confidence]:
            found[due] = f
    return sorted(found.values(), key=lambda f: f.due)
