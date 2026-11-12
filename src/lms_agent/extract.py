"""글(공지, 메시지, 강의계획서, 붙여 넣은 메일)에서 마감 문장을 찾는다.

규칙 기반이다. AI 키 없이 누구 PC에서나 같은 결과가 나와야 하기 때문이다.
판단은 사람에게 맡긴다: 찾은 것마다 근거 문장과, 추정한 부분(연도, 시간, 오전/오후)을 notes로 남긴다.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

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


# 마침표 뒤에서 자르되 "2026. 11. 20." 같은 날짜 안에서는 자르지 않는다.
_SPLIT = re.compile(r"(?<=[!?。])\s+|(?<=[^\d\s.][.])\s+")


def split_sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        for part in _SPLIT.split(line):
            part = part.strip()
            if part:
                out.append(part)
    return out


# ---------------------------------------------------------------- 키워드

STRONG = re.compile(r"(?i)마감|제출|기한|데드라인|due|deadline|submit|turn\s+in|보내\s*주|내\s*주세요")
UNTIL = re.compile(r"(?i)까지|until|\bby\b")
TASK = re.compile(r"(?i)과제|숙제|레포트|리포트|보고서|프로젝트|실습|에세이|(?<![a-z])(?:lab|hw|homework|assignment|report|project|essay)(?![a-z])")
EXAM = re.compile(r"(?i)시험|중간고사|기말고사|퀴즈|(?<![a-z])(?:quiz|exam|midterm)(?![a-z])")
NEGATIVE = re.compile(r"(?i)휴강|보강|강의실\s*변경|수업\s*없|class\s+(?:is\s+)?cancel")

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


def deadline_kind(sentence: str) -> str | None:
    strong = bool(STRONG.search(sentence))
    exam = bool(EXAM.search(sentence))
    if NEGATIVE.search(sentence) and not strong:
        return None
    if exam:
        return "시험"
    if strong or (UNTIL.search(sentence) and TASK.search(sentence)):
        return "과제"
    return None


# ---------------------------------------------------------------- 과제 이름

_TITLE_WORDS = (r"과제|숙제|레포트|리포트|보고서|프로젝트|실습|퀴즈|중간고사|기말고사|중간\s*시험|기말\s*시험|시험|"
                r"lab|hw|homework|assignment|report|project|quiz|midterm|final\s+exam|final\s+report|exam")
# 번호 뒤에 월·일·시 등이 오면 날짜·시간의 숫자이지 과제 번호가 아니다("기말고사 12월 12일").
_NOT_NUM = r"(?!\d)(?!\s*(?:월|일|시|분|주|쪽|명|점|%|[:./~\-]\s*\d))"
_TITLE_A = re.compile(r"(?i)(?<![a-z])(?P<w>" + _TITLE_WORDS + r")(?![a-z])(?:\s*#?\s*(?P<n>\d{1,2})" + _NOT_NUM + r")?")
_TITLE_B = re.compile(r"(?i)(?<!\d)(?P<n>\d{1,2})\s*(?:차|번째?)\s*(?P<w>" + _TITLE_WORDS + r")(?![a-z])")

_SYN = {
    "과제": "hw", "숙제": "hw", "hw": "hw", "homework": "hw", "assignment": "hw",
    "레포트": "report", "리포트": "report", "보고서": "report", "report": "report", "final report": "report",
    "프로젝트": "project", "project": "project", "실습": "lab", "lab": "lab",
    "퀴즈": "quiz", "quiz": "quiz", "중간고사": "midterm", "중간시험": "midterm", "midterm": "midterm",
    "기말고사": "final", "기말시험": "final", "final exam": "final", "시험": "exam", "exam": "exam",
}


def _display_word(w: str) -> str:
    low = re.sub(r"\s+", " ", w.lower())
    if low in ("lab", "hw"):
        return low.upper()
    if re.match(r"[a-z]", low):
        return low.title()
    return re.sub(r"\s+", " ", w)


def title_hint(text: str) -> str | None:
    best = None
    for rx in (_TITLE_A, _TITLE_B):
        m = rx.search(text)
        if m and (best is None or m.start() < best.start()):
            best = m
    if not best:
        return None
    w, n = _display_word(best.group("w")), best.group("n")
    return f"{w} {n}" if n else w


def title_tokens(text: str | None) -> set[tuple[str, str | None]]:
    """'LAB4 보고서' -> {('lab','4'), ('report', None)}. 같은 과제인지 비교할 때 쓴다."""
    out: set[tuple[str, str | None]] = set()
    if not text:
        return out
    for rx in (_TITLE_A, _TITLE_B):
        for m in rx.finditer(text):
            key = _SYN.get(re.sub(r"\s+", "" if re.match(r"[가-힣]", m.group("w")) else " ", m.group("w").lower()))
            if key:
                out.add((key, m.group("n")))
    return out


def same_task(a: str | None, b: str | None) -> bool:
    ta, tb = title_tokens(a), title_tokens(b)
    for k, n in ta:
        if n is not None and (k, n) in tb:
            return True
        if n is None and k in ("midterm", "final") and any(k == k2 for k2, _ in tb):
            return True
    # 번호만 같고 종류 이름이 다른 경우(LAB4 vs 실습 4)는 위에서 lab으로 묶인다.
    return False


# ---------------------------------------------------------------- 날짜

_WD_AFTER = (r"(?:\s*\(\s*(?P<wd>[월화수목금토일]|[A-Za-z]{3})[A-Za-z]*\.?\s*(?:요일)?\s*\)"
             r"|\s+(?P<wd2>[월화수목금토일])요일)?")
RX_YMD = re.compile(r"(?<!\d)(?P<y>20\d{2})\s*(?:[.\-/]|년)\s*(?P<m>\d{1,2})\s*(?:[.\-/]|월)\s*(?P<d>\d{1,2})(?!\d)\s*(?:일)?\.?" + _WD_AFTER)
RX_MD_KO = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일" + _WD_AFTER)
RX_MD_NUM = re.compile(r"(?<![\d./\-:])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d:])(?!\s*[./]\s*\d)\.?" + _WD_AFTER)
RX_EN_MD = re.compile(r"(?i)(?<![a-z])(?P<mon>[a-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?![\d:])(?:,?\s*(?P<y>20\d{2}))?")
RX_EN_DM = re.compile(r"(?i)(?<![\d:])(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[a-z]{3,9})(?![a-z])\.?(?:,?\s*(?P<y>20\d{2}))?")

RX_REL = re.compile(r"오늘|금일|내일|명일|모레")
RX_REL_WD = re.compile(r"(?P<wk>다다음\s*주|다음\s*주|차주|담주|이번\s*주|금주)?\s*(?P<wd>[월화수목금토일])요일")
RX_EN_REL = re.compile(r"(?i)(?<![a-z])(?:(?P<rel>today|tonight|tomorrow)|(?:(?P<wk>this|next)\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday))(?![a-z])")

# 숫자 날짜(11/20, 11.20)는 소수·점수와 헷갈리므로, 뒤에 마감다운 말이 올 때만 날짜로 본다.
_NUM_OK_AFTER = re.compile(r"(?i)^\s*(?:까지|마감|자정|정오|오전|오후|저녁|밤|새벽|\d{1,2}\s*[:시]|\d{1,2}\s*[ap]\.?m|[~\-–]|by\b|at\b|until\b|due\b|$|[,)\]])")
_NUM_OK_BEFORE = re.compile(r"(?i)(?:마감|기한|due|deadline|by|until|[~\-–])\s*(?:일|일시|은|는)?\s*[:：]?\s*$")
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
    wd = _wd_index(gd.get("wd") or gd.get("wd2"))
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
    for m in RX_MD_NUM.finditer(s):
        if not free(m.start(), m.end()):
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
RX_T_EN = re.compile(r"(?<![\d:])(?P<h>\d{1,2})\s*(?P<ap2>[AaPp]\.?\s?[Mm]\.?)(?![a-zA-Z])")
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
    elif rx is RX_T_COLON and 1 <= h <= 7:
        notes.append("오전/오후 미기재 → 적힌 그대로 둠(확인 필요)")
    return time(h, mi), notes


def find_time(s: str) -> tuple[time | None, list[str]]:
    best = None
    for rx in (RX_T_COLON, RX_T_KO, RX_T_EN, RX_T_WORD):
        for m in rx.finditer(s):
            if best is None or m.start() < best[0].start():
                best = (m, rx)
            break
    if not best:
        return None, []
    return _resolve_time(*best)


# ---------------------------------------------------------------- 본체

_CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


def find_deadlines(text: str, ref: datetime, *, is_html: bool = False) -> list[Found]:
    """text에서 마감을 찾는다. ref는 글이 게시된 시각(로컬 시간대가 붙은 datetime)."""
    plain = html_to_text(text) if is_html else (text or "")
    lines = split_sentences(plain)
    msg_method, msg_email = detect_method(plain)
    ref_day = ref.date()
    found: dict[datetime, Found] = {}

    for i, s in enumerate(lines):
        kind = deadline_kind(s)
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
        notes = list(h.notes) + tnotes
        has_time = t is not None
        if t is None:
            t = time(23, 59)
            notes.append("시간 미기재 → 23:59로 둠")

        window = " ".join(lines[max(0, i - 2):i + 3])
        method, email_to = detect_method(s)
        if method == "unknown":
            method, email_to = detect_method(window)
        if method == "unknown" and msg_method != "unknown":
            method, email_to = msg_method, msg_email
        if method == "email" and not email_to:
            addr = EMAIL_ADDR.search(window) or EMAIL_ADDR.search(plain)
            email_to = addr.group(0) if addr else None

        hint = title_hint(s) or title_hint(" ".join(lines[max(0, i - 2):i]))
        if kind == "과제" and hint and EXAM.search(hint):
            kind = "시험"

        score = (0 if h.relative else 2) + (1 if has_time else 0) + (1 if STRONG.search(s) else 0) - (2 if h.weekday_bad else 0)
        conf = "high" if score >= 4 else "medium" if score >= 2 else "low"

        due = datetime.combine(h.day, t, tzinfo=ref.tzinfo)
        f = Found(due, s, hint, kind, method, email_to, conf, notes)
        old = found.get(due)
        if old is None or _CONF_ORDER[conf] > _CONF_ORDER[old.confidence]:
            found[due] = f
    return sorted(found.values(), key=lambda f: f.due)
