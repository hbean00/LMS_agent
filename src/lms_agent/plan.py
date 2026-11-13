"""공부 일정(계약 22-5): 시험 전 공부 블록을 짜고(make), 채팅 문장을 알아듣고(handle), 넣기·지우기를 검사한다.

- make(): 미리 보기만(저장 안 함). 대상 = 준 항목 → 그 과목의 가장 가까운 시험 → 전체에서 가장 가까운 시험.
  오늘(또는 start)~시험 전날 하루 한 블록, 마감이 2개 이상인 날·이미 넣은 공부 블록과 겹치는 날·시험 당일은 건너뛰고,
  구글 캘린더의 다른 일정(계약 23-3, 켰을 때)이 그 시각에 바쁨(busy)으로 겹치는 날도 건너뛴다(종일 일정은 겹침으로 보지 않음),
  최대 14블록(많으면 시험에 가까운 날부터). 시각은 hour:00, 길이 minutes — 시간대는 now의 것(앱 전체처럼 PC 로컬 = 한국 시간).
- 저장: store.data["plans"] = [{"id": "p-…", "title", "course_id", "item_id", "blocks": [...], "created_at",
  "decided_by": "user"|"ai", "reason"?}] — add()/remove()는 잠금 안에서 부른다(features/plan.py).
- 내보내기: core.study_events가 저장된 블록을 길이가 있는 일정(UID study-<plan>-<n>@lms-agent)으로 낸다.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta

from . import core
from . import links as lk
from .store import hidden_courses
from .timeutil import iso, now_local, parse_iso

MAX_BLOCKS = 14          # make()가 짜는 최대 블록
MAX_APPLY_BLOCKS = 30    # 한 번에 넣을 수 있는 최대 블록
MIN_MINUTES, MAX_MINUTES = 15, 240
MAX_TITLE = 120
MAX_DAYS = 60
MAX_PLANS = 50
MAX_AHEAD_DAYS = 366
BUSY = 2                 # 마감이 이만큼 있는 날은 건너뛴다
USABLE = ("review", "scheduled")
KO_WD = "월화수목금토일"


# ---------------------------------------------------------------- 작은 도구

def _md(d: date) -> str:
    return f"{d.month}/{d.day}({KO_WD[d.weekday()]})"


def _mdhm(dt: datetime) -> str:
    return f"{_md(dt.date())} {dt:%H:%M}"


def _dur(m: int) -> str:
    h, r = divmod(int(m), 60)
    if not h:
        return f"{r}분"
    return f"{h}시간" + (f" {r}분" if r else "")


def _clip(text: str, n: int = MAX_TITLE) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _int(value, label: str, lo: int, hi: int, default):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label}은 숫자여야 해요")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,4}\s*", value):
        value = int(value)
    if not isinstance(value, int):
        raise ValueError(f"{label}은 숫자여야 해요")
    if not lo <= value <= hi:
        raise ValueError(f"{label}은 {lo}~{hi} 사이여야 해요")
    return value


def _start_day(value, tz) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(tz).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            try:
                return date.fromisoformat(s)
            except ValueError:
                pass
        dt = parse_iso(s)
        if dt is not None:
            return dt.astimezone(tz).date()
    raise ValueError("start는 'YYYY-MM-DD' 날짜여야 해요")


def _opt_id(value, label: str, n: int = 300) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label}이 올바르지 않아요")
    value = str(value).strip()
    if len(value) > n or not re.fullmatch(r"[\w\-:.@]+", value):
        raise ValueError(f"{label}이 올바르지 않아요")
    return value


def _course_shorts(store) -> dict:
    return {str(c["id"]): core.short_course_name(c.get("name"))
            for c in (store.data.get("cache") or {}).get("courses") or [] if isinstance(c, dict) and "id" in c}


def _items(store) -> list[dict]:
    """화면과 같은 항목(사용자 결정 반영, 제외한 과목 빠짐) + 주차·짧은 과목명 + 평가 항목 연결."""
    from . import gradebook

    cache = store.data.get("cache")
    its = store.view()
    sem = core.semester_info(cache, its)
    core.annotate(its, sem, cache)
    try:
        gradebook.link_items(store, its, sem)
    except Exception:  # noqa: BLE001 — 평가 항목 연결은 시험 계열 판단을 돕는 덤. 실패해도 kind "시험"만으로 짠다
        pass
    return its


def is_exam(it: dict) -> bool:
    """시험 항목: kind "시험" 또는 시험 계열 평가 항목(Midterm quiz 등)에 연결된 항목(계약 14-9)."""
    if it.get("kind") == "시험":
        return True
    comp = it.get("component")
    return bool(comp) and lk.is_exam_component(str(comp))


def upcoming_exams(items: list[dict], now: datetime) -> list[dict]:
    out = [it for it in items if it.get("status") in USABLE and is_exam(it)
           and parse_iso(it.get("due")) is not None and parse_iso(it["due"]) > now]
    return sorted(out, key=lambda it: parse_iso(it["due"]))


def _exam_label(it: dict) -> str:
    return f"[{it.get('course_short') or it.get('course_name') or '과목'}] {it.get('title') or '시험'}"


def _nearest_note(others: list[dict], tz) -> str:
    rest = ", ".join(f"{_exam_label(it)} {_md(parse_iso(it['due']).astimezone(tz).date())}" for it in others)
    return f"가장 가까운 시험으로 정했어요(다른 시험: {rest})."


def plans(store) -> list[dict]:
    """저장된 계획(모양이 깨진 것은 뺌)."""
    out = []
    for p in store.data.get("plans") or []:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("blocks"), list):
            out.append(p)
    return out


def _slots(store) -> list[tuple[datetime, datetime, str]]:
    """이미 넣은 공부 블록들의 (시작, 끝, 계획 이름)."""
    out = []
    for p in plans(store):
        for b in p["blocks"]:
            s, e = parse_iso((b or {}).get("start")), parse_iso((b or {}).get("end"))
            if s and e:
                out.append((s, e, str(p.get("title") or "공부 계획")))
    return out


def _empty(course_id, notes: list[str], minutes: int, hour: int, days) -> dict:
    return {"title": "공부 계획", "course_id": course_id, "item_id": None, "course_short": None, "target": None,
            "days": days, "minutes": minutes, "hour": hour, "blocks": [], "notes": notes}


# ---------------------------------------------------------------- 짜기

def make(store, course_id=None, item_id=None, days=None, minutes=60, hour=20, start=None, now=None) -> dict:
    """공부 블록 미리 보기(저장 안 함). 잘못된 값이면 ValueError(한국어)."""
    now = now or now_local()
    tz = now.tzinfo
    minutes = _int(minutes, "공부 시간(minutes, 분)", MIN_MINUTES, MAX_MINUTES, 60)
    hour = _int(hour, "시작 시각(hour)", 0, 23, 20)
    days = _int(days, "기간(days, 일)", 1, MAX_DAYS, None)
    first = _start_day(start, tz)
    cid = _opt_id(course_id, "course_id")
    iid = _opt_id(item_id, "item_id")
    items = _items(store)
    shorts = _course_shorts(store)
    notes: list[str] = []

    target = None
    if iid:
        target = next((it for it in items if it["id"] == iid), None)
        if target is None:
            return _empty(cid, ["그 항목을 찾지 못했어요(동기화 뒤 다시 시도하거나, 제외한 과목인지 확인해 주세요)."],
                          minutes, hour, days)
        if target.get("status") not in USABLE:
            return _empty(cid, [f"'{target.get('title')}'은 제외·대체된 항목이라 계획을 짜지 않았어요."], minutes, hour, days)
        if parse_iso(target.get("due")) is None:
            return _empty(cid, [f"'{target.get('title')}'은 날짜가 없어 계획을 짤 수 없어요."], minutes, hour, days)
        if parse_iso(target["due"]) <= now:
            return _empty(cid, [f"'{target.get('title')}'은 이미 지났어요."], minutes, hour, days)
    else:
        exams = upcoming_exams(items, now)
        if cid:
            exams = [it for it in exams if str(it.get("course_id")) == cid]
        if not exams:
            where = f"{shorts.get(cid) or '그 과목'}에" if cid else "지금"
            return _empty(cid, [f"{where} 다가오는 시험 일정이 없어요. "
                                "시험 날짜가 공지되면(확인함에 들어오면) 다시 짜 주세요."], minutes, hour, days)
        target = exams[0]
        if len(exams) > 1:
            notes.append(_nearest_note(exams[1:4], tz))

    due = parse_iso(target["due"]).astimezone(tz)
    tcid = str(target["course_id"]) if target.get("course_id") is not None else cid
    short = target.get("course_short") or shorts.get(tcid or "") or core.short_course_name(target.get("course_name"))
    exam_title = str(target.get("title") or "시험")
    plan = {"title": _clip(f"{short} {exam_title} 공부 계획"), "course_id": tcid, "item_id": target["id"],
            "course_short": short,
            "target": {"title": exam_title, "due": iso(due), "status": target.get("status"), "kind": target.get("kind"),
                       "url": target.get("url")},
            "days": days, "minutes": minutes, "hour": hour, "blocks": [], "notes": notes}
    if target.get("status") == "review":
        notes.append("이 날짜는 아직 확인 전(확인함)이에요. 날짜가 바뀌면 계획을 다시 짜 주세요.")
    notes.append(f"매일 {hour:02d}:00부터 {_dur(minutes)}씩, 시험 당일({_md(due.date())})은 비워 뒀어요.")

    today = now.date()
    lo = today
    if first is not None:
        if first > today:
            lo = first
        elif first < today:
            notes.append("지난 날짜에는 넣을 수 없어 오늘부터 짰어요.")
    if days:
        want_lo = due.date() - timedelta(days=days)
        if want_lo < lo:
            notes.append(f"시험까지 {(due.date() - lo).days}일 남아 {days}일을 다 채우지 못했어요.")
        lo = max(lo, want_lo)
    hi = due.date() - timedelta(days=1)
    if lo > hi:
        notes.append("시험 전날까지 남은 날이 없어 블록을 만들지 못했어요.")
        return plan

    loads: dict = {}
    for it in items:
        if it["id"] == target["id"] or it.get("status") not in USABLE or it.get("submitted"):
            continue
        d = parse_iso(it.get("due"))
        if d is not None:
            loads.setdefault(d.astimezone(tz).date(), []).append(str(it.get("title") or "마감"))
    taken = _slots(store)
    from . import gevents
    others = [(ts, te) for ts, te, all_day in gevents.busy(store) if not all_day]   # 제목은 쓰지 않는다

    chosen: list[tuple[date, datetime, datetime]] = []
    busy, clash, ev_clash, passed, late = [], [], [], False, False
    day = hi
    while day >= lo:
        s = datetime.combine(day, time(hour, 0), tzinfo=tz)
        e = s + timedelta(minutes=minutes)
        if len(chosen) >= MAX_BLOCKS:
            notes.append(f"블록은 최대 {MAX_BLOCKS}개라 시험에 가까운 날부터 넣었어요({_md(lo)}~{_md(day)}는 비움).")
            break
        if s <= now:
            passed = True
        elif e > due:
            late = True
        elif len(loads.get(day, [])) >= BUSY:
            busy.append(f"{_md(day)} " + "·".join(loads[day][:3]))
        elif any(s < te and ts < e for ts, te, _ in taken):
            clash.append(_md(day))
        elif any(s < te and ts < e for ts, te in others):
            ev_clash.append(_md(day))
        else:
            chosen.append((day, s, e))
        day -= timedelta(days=1)
    if passed:
        notes.append(f"오늘은 {hour:02d}:00가 지나 내일부터 넣었어요.")
    if late:
        notes.append("시험 시작 전에 끝나지 않는 블록은 뺐어요.")
    if busy:
        notes.append(f"마감이 {BUSY}개 이상인 날은 건너뛰었어요: " + ", ".join(reversed(busy[-5:]))
                     + (f" 외 {len(busy) - 5}일" if len(busy) > 5 else "") + ".")
    if clash:
        notes.append("이미 넣은 공부 계획과 겹치는 날은 건너뛰었어요: " + ", ".join(reversed(clash[-5:])) + ".")
    if ev_clash:
        notes.append("일정과 겹쳐 뺀 날: " + ", ".join(reversed(ev_clash[-5:]))
                     + (f" 외 {len(ev_clash) - 5}일" if len(ev_clash) > 5 else "") + "(구글 캘린더의 다른 일정).")
    if gevents.enabled(store) and chosen and max(d for d, _, _ in chosen) >= gevents.window_range(now)[1].date():
        notes.append(f"구글 캘린더의 다른 일정은 {gevents.DAYS_AFTER}일 뒤까지만 확인했어요.")

    for day, s, e in sorted(chosen):
        plan["blocks"].append({"start": iso(s), "end": iso(e),
                               "title": _clip(f"{short} {exam_title} D-{(due.date() - day).days}"),
                               "course_id": tcid, "item_id": target["id"]})
    if not plan["blocks"] and not any("만들지 못했어요" in n for n in notes):
        notes.append("넣을 수 있는 날이 없어 블록을 만들지 못했어요. 기간·시각을 바꿔 다시 짜 보세요.")
    return plan


# ---------------------------------------------------------------- 채팅 문장 알아듣기

_PLAN = re.compile(
    r"공부\s*(?:계획|일정|스케[줄쥴]|플랜|시간표)"
    r"|(?:계획|일정|스케[줄쥴]|플랜|시간표)\s*(?:을|를|좀|도|이랑)?\s*(?:세워|세울|짜|짤|잡아|잡을|만들어|만들)"
    r"|(?:대비|준비)\s*(?:공부\s*)?(?:스케[줄쥴]|일정|계획|플랜|시간표)"
    r"|study\s*(?:plan|schedule)", re.I)
_NOT_PLAN = re.compile(r"지워|지우|삭제|취소|없애|빼\s*줘|메일|이메일", re.I)
_WANT_FAMILIES = ("midterm", "final", "quiz")
_FAMILY_LABEL = {"midterm": "중간고사", "final": "기말고사", "quiz": "퀴즈"}
_NUM_WORD = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
_TIME = re.compile(r"(오전|오후|아침|저녁|밤|새벽|낮)?\s*(?:(\d{1,2})\s*시(?!간|험|작|차)(?:\s*(\d{1,2})\s*분|\s*(반))?"
                   r"|(\d{1,2}):(\d{2}))")
_PART_OF_DAY = {"새벽": 6, "아침": 8, "오전": 10, "낮": 13, "오후": 15, "저녁": 20, "밤": 22}
# 과목 이름 찾기에서 뺄 낱말(계획·기간·시각 말)
_STEMS = re.compile(r"(?:공부|계획|일정|스케[줄쥴]|플랜|시간표|대비|준비|매일|하루|날마다|저녁|아침|오전|오후|밤|새벽|낮|"
                    r"전|후|동안|부터|까지|씩|좀|위해|위한|정도|쯤|캘린더|달력|일정표|넣어|"
                    r"일주일|열흘|보름|달|다음|이번|남은|내|제|나|우리|모든|전체|과목|수업|강의|주세요|해서|맞춰|"
                    r"오늘|내일|모레|이번\s*주|다음\s*주|주말|평일|주중|빼고|말고|제외|없이|"
                    r"study|plan|schedule|for|the|my|me|a|an|to|of|in|on|and|please|make|create|before|every|each|"
                    r"days?|weeks?|hours?|minutes?)+"
                    r"(?:에|에서|으로|로|을|를|이|가|은|는|도|만|요|씩|쯤|만큼|부터|까지|마다|엔)*", re.I)
_VERBS = re.compile(r"^(?:세워|세울|짜|짤|잡아|잡을|만들|해줘|해주|부탁|알려|보여|줘)")


def _parse_hour(msg: str) -> tuple[int | None, list[str], str]:
    """(시, 안내, 시각 말을 뺀 문장)."""
    notes: list[str] = []
    m = _TIME.search(msg)
    if not m:
        for word, h in _PART_OF_DAY.items():
            if re.search(word + r"(?:에|마다|\s|$)", msg):
                return h, [f"{word} 시간은 {h:02d}:00로 잡았어요."] if word != "저녁" else [], msg
        return None, notes, msg
    part = m.group(1)
    h = int(m.group(2) or m.group(5))
    odd_minute = bool(m.group(3) or m.group(4) or (m.group(6) and int(m.group(6))))
    rest = msg[: m.start()] + " " + msg[m.end():]
    if h > 24:
        return None, notes, rest
    if part in ("오후", "저녁", "밤"):
        h = h + 12 if h < 12 else (0 if h == 12 and part == "밤" else h)
    elif part in ("오전", "아침", "새벽"):
        h = 0 if h == 12 else h
    elif part == "낮":
        h = h + 12 if h <= 6 else h
    elif 1 <= h <= 11:
        notes.append(f"'{h}시'를 저녁 {h + 12}:00로 봤어요(아침이면 '오전 {h}시'라고 말해 주세요).")
        h += 12
    h %= 24
    if odd_minute:
        notes.append(f"시작은 정시({h:02d}:00)로 맞췄어요.")
    return h, notes, rest


def _parse_minutes(msg: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*시간\s*(?:(\d{1,2})\s*분|(반))?", msg)
    if m:
        return int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 30 if m.group(3) else 0)
    m = re.search(r"(한|두|세|네)\s*시간\s*(반)?", msg)
    if m:
        return _NUM_WORD[m.group(1)] * 60 + (30 if m.group(2) else 0)
    if re.search(r"반\s*시간", msg):
        return 30
    m = re.search(r"(\d{1,3})\s*분", msg)
    if m:
        return int(m.group(1))
    return None


def _parse_days(msg: str) -> int | None:
    if "열흘" in msg:
        return 10
    if "보름" in msg:
        return 15
    if re.search(r"한\s*달", msg):
        return 30
    if re.search(r"일주일|1\s*주일", msg):
        return 7
    m = re.search(r"(\d{1,2})\s*주(?:일|간)?(?!차|말)", msg)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(한|두|세|네)\s*주(?:일|간)?(?!차|말)", msg)
    if m:
        return _NUM_WORD[m.group(1)] * 7
    m = re.search(r"[Dd]\s*-\s*(\d{1,2})", msg)
    if m:
        return int(m.group(1))
    for m in re.finditer(r"(\d{1,2})\s*일(?!정|요일)", msg):
        before = msg[: m.start()].rstrip()
        if before.endswith(("월", "/", ".", "-")):
            continue
        return int(m.group(1))
    return None


def _course_words(message: str) -> list[str]:
    """과목 이름일 수 있는 낱말만(계획·기간·시각·시험 낱말·동사는 뺌)."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9+#\-]*|[가-힣A-Za-z0-9]+", message or ""):
        if (_STEMS.fullmatch(w) or _VERBS.match(w) or re.match(r"^\d", w) or w in _NUM_WORD
                or lk.families(w) or len(w) < 2):
            continue
        out.append(w)
    return out


def _abbrev(word: str, name: str) -> bool:
    """'로공개' ↔ '로봇공학개론': 첫 글자가 같고 글자가 순서대로 다 들어 있음(한글 세 글자 이상)."""
    word = re.sub(r"(?:은|는|이|가|을|를|의|에|도|만|요)$", "", word)
    if len(word) < 3 or not re.fullmatch(r"[가-힣]+", word) or not name or name[0] != word[0]:
        return False
    pos = 0
    for ch in word:
        pos = name.find(ch, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def _mentioned(store, message: str) -> list[str]:
    courses = [c for c in (store.data.get("cache") or {}).get("courses") or []
               if isinstance(c, dict) and str(c.get("id")) not in hidden_courses(store.data)]
    words = _course_words(message)
    if not words or not courses:
        return []
    text = " ".join(words)
    try:
        from .agent import mentioned_courses   # 채팅과 같은 과목 찾기(이름·괄호 뗀 이름·과목 코드)
        found = mentioned_courses(text, courses)
    except Exception:  # noqa: BLE001 — agent 모듈을 못 불러도 아래 줄임말 찾기로
        found = []
    if found:
        return found
    out = []
    for c in courses:
        short = core.short_course_name(c.get("name"))
        bare = re.sub(r"\([^()]*\)", "", short).strip()
        if any(_abbrev(w, bare) or _abbrev(w, short) for w in words):
            out.append(str(c["id"]))
    return out


def handle(store, message: str, course_id=None, now=None) -> dict | None:
    """해당 없으면 None. 결과 {"answer", "plan": {...}}(계약 22-5) — 미리 보기만, 저장은 사용자가 [캘린더에 넣기]."""
    msg = str(message or "")
    if not _PLAN.search(msg) or _NOT_PLAN.search(msg):
        return None
    now = now or now_local()
    tz = now.tzinfo
    hour, hour_notes, rest = _parse_hour(msg)
    minutes = _parse_minutes(rest)
    days = _parse_days(rest)
    extra = list(hour_notes)
    if minutes is not None and not MIN_MINUTES <= minutes <= MAX_MINUTES:
        fixed = min(MAX_MINUTES, max(MIN_MINUTES, minutes))
        extra.append(f"한 번에 {_dur(minutes)}은 넣을 수 없어 {_dur(fixed)}으로 맞췄어요({MIN_MINUTES}~{MAX_MINUTES}분).")
        minutes = fixed
    if days is not None and not 1 <= days <= MAX_DAYS:
        fixed = min(MAX_DAYS, max(1, days))
        extra.append(f"기간은 최대 {MAX_DAYS}일이라 {fixed}일로 맞췄어요.")
        days = fixed
    if hour is None and minutes is None:
        extra.append("시각·길이는 기본값(20:00, 1시간)이에요. '저녁 9시, 매일 2시간'처럼 말하면 바꿔 짜요.")
    hour = 20 if hour is None else hour
    minutes = 60 if minutes is None else minutes

    cids = _mentioned(store, msg)
    unknown = None
    if not cids and course_id not in (None, ""):
        cids = [str(course_id)]
    elif not cids and _course_words(msg):   # 과목 이름 같은데 맞는 과목이 없음 → 숨기지 않고 알린다
        unknown = f"'{_course_words(msg)[0]}'에 맞는 과목을 찾지 못해 모든 과목에서 골랐어요."
    want = [f for f in _WANT_FAMILIES if f in lk.families(msg)]
    items = _items(store)
    exams = upcoming_exams(items, now)
    scope = [it for it in exams if not cids or str(it.get("course_id")) in cids]
    shorts = _course_shorts(store)
    where = "·".join(shorts.get(c, c) for c in cids) if cids else ""

    matches = [it for it in scope
               if not want or lk.families(f"{it.get('title') or ''} {it.get('component') or ''}") & set(want)]
    target = matches[0] if matches else None
    if target is None:
        label = "·".join(_FAMILY_LABEL[f] for f in want) or "시험"
        head = f"{where}의 다가오는 {label}" if where else f"다가오는 {label}"
        others = [f"{_exam_label(it)} {_mdhm(parse_iso(it['due']).astimezone(tz))}" for it in (scope or exams)[:3]]
        notes = ([unknown] if unknown else []) + [f"{head} 일정을 찾지 못했어요."]
        if others:
            notes.append("다가오는 시험: " + ", ".join(others) + ".")
        else:
            notes.append("시험 날짜가 공지되면(확인함에 들어오면) 다시 짜 주세요.")
        plan = _empty(cids[0] if len(cids) == 1 else None, notes, minutes, hour, days)
        return {"answer": " ".join(notes), "plan": plan}

    plan = make(store, item_id=target["id"], days=days, minutes=minutes, hour=hour, now=now)
    if len(matches) > 1:   # item_id로 짜면 make()는 '가장 가까운 시험' 안내를 붙이지 않는다
        plan["notes"].insert(0, _nearest_note(matches[1:4], tz))
    plan["notes"].extend(extra)
    if unknown:
        plan["notes"].insert(0, unknown)
    if not plan.get("target"):
        return {"answer": " ".join(plan["notes"][:1]) or "공부 계획을 짜지 못했어요.", "plan": plan}

    due = parse_iso(plan["target"]["due"])
    head = f"[{plan['course_short']}] {plan['target']['title']}({_mdhm(due)})"
    blocks = plan["blocks"]
    if not blocks:
        reason = next((n for n in plan["notes"] if "만들지 못했어요" in n), "넣을 수 있는 날이 없어요.")
        return {"answer": f"{head} 공부 계획을 짜지 못했어요. {reason}", "plan": plan}
    first, last = parse_iso(blocks[0]["start"]), parse_iso(blocks[-1]["start"])
    span = _md(first.date()) if first.date() == last.date() else f"{_md(first.date())}~{_md(last.date())}"
    skipped = any(n.startswith(("마감이", "이미 넣은", "일정과 겹쳐")) for n in plan["notes"])
    answer = ((unknown + " " if unknown else "")
              + f"{head} 전까지 공부 블록 {len(blocks)}개를 짰어요: {span}, 매일 {hour:02d}:00부터 {_dur(minutes)}씩."
              + (" 마감이 몰린 날·이미 넣은 공부나 다른 일정과 겹치는 날은 건너뛰었어요." if skipped else "")
              + " 아직 캘린더에 넣지 않았어요 — [캘린더에 넣기]를 누르면 '[공부]' 일정으로 들어가요"
                "(구글 캘린더가 연결돼 있으면 거기에도).")
    return {"answer": answer, "plan": plan}


# ---------------------------------------------------------------- 넣기·지우기(잠금 안에서)

def _dt(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    return parse_iso(value)


def validate(store, body, now=None) -> dict:
    """POST /api/plan/apply 본문 → 저장할 계획(id·created_at 없음). 잘못되면 ValueError(한국어).
    검사: 제목 1~120자, 블록 1~30개, 각 블록은 미래·15~240분·제목 120자 이내, 블록끼리·이미 넣은 공부 블록과 겹침 없음."""
    now = now or now_local()
    if not isinstance(body, dict):
        raise ValueError("요청 본문이 올바르지 않아요")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("계획 이름(title)이 필요해요")
    title = " ".join(title.split())
    if len(title) > MAX_TITLE:
        raise ValueError(f"계획 이름은 {MAX_TITLE}자 이내로 써 주세요")
    cid = _opt_id(body.get("course_id"), "course_id")
    iid = _opt_id(body.get("item_id"), "item_id")
    blocks = body.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("넣을 블록(blocks)이 하나 이상 있어야 해요")
    if len(blocks) > MAX_APPLY_BLOCKS:
        raise ValueError(f"블록은 한 번에 최대 {MAX_APPLY_BLOCKS}개까지 넣을 수 있어요")
    if len(plans(store)) >= MAX_PLANS:
        raise ValueError(f"공부 계획이 너무 많아요(최대 {MAX_PLANS}개). 지난 계획을 지운 뒤 넣어 주세요")
    out = []
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict):
            raise ValueError(f"{i}번째 블록이 올바르지 않아요")
        s, e = _dt(b.get("start")), _dt(b.get("end"))
        if s is None or e is None:
            raise ValueError(f"{i}번째 블록의 시작·끝 시각이 올바르지 않아요")
        if s <= now:
            raise ValueError(f"{i}번째 블록({_mdhm(s)})은 이미 지난 시각이에요. 다시 짜 주세요")
        if s > now + timedelta(days=MAX_AHEAD_DAYS):
            raise ValueError(f"{i}번째 블록이 너무 먼 날이에요(1년 안만)")
        mins = (e - s).total_seconds() / 60
        if not MIN_MINUTES <= mins <= MAX_MINUTES:
            raise ValueError(f"{i}번째 블록 길이는 {MIN_MINUTES}~{MAX_MINUTES}분이어야 해요(지금 {mins:g}분)")
        bt = b.get("title")
        if bt is None or (isinstance(bt, str) and not bt.strip()):
            bt = title
        if not isinstance(bt, str):
            raise ValueError(f"{i}번째 블록의 제목이 올바르지 않아요")
        bt = " ".join(bt.split())
        if len(bt) > MAX_TITLE:
            raise ValueError(f"{i}번째 블록의 제목은 {MAX_TITLE}자 이내로 써 주세요")
        out.append({"start": iso(s), "end": iso(e), "title": bt,
                    "course_id": _opt_id(b.get("course_id", cid), f"{i}번째 블록의 course_id"),
                    "item_id": _opt_id(b.get("item_id", iid), f"{i}번째 블록의 item_id"), "_s": s, "_e": e})
    out.sort(key=lambda b: b["_s"])
    for a, b in zip(out, out[1:]):
        if b["_s"] < a["_e"]:
            raise ValueError(f"블록끼리 시간이 겹쳐요: {_mdhm(a['_s'])}와 {_mdhm(b['_s'])}")
    for ts, te, name in _slots(store):
        for b in out:
            if b["_s"] < te and ts < b["_e"]:
                raise ValueError(f"이미 넣은 공부 계획 '{_clip(name, 40)}'의 {_mdhm(ts)} 블록과 겹쳐요")
    decided_by = body.get("decided_by", "user")
    if decided_by not in ("user", "ai"):
        raise ValueError("decided_by는 'user' 또는 'ai'여야 해요")
    reason = body.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ValueError("reason은 500자 이내 글이어야 해요")
    rec = {"title": title, "course_id": cid, "item_id": iid,
           "blocks": [{k: v for k, v in b.items() if not k.startswith("_")} for b in out],
           "decided_by": decided_by}
    if decided_by == "ai" and reason and reason.strip():
        rec["reason"] = reason.strip()
    return rec


def add(store, body, now=None) -> dict:
    """검사한 뒤 store.data["plans"]에 더한다(저장은 부른 쪽이). 저장한 계획을 돌려준다."""
    now = now or now_local()
    rec = validate(store, body, now=now)
    rec = {"id": "p-" + uuid.uuid4().hex[:10], **rec, "created_at": iso(now)}
    store.data["plans"] = plans(store) + [rec]
    return rec


def remove(store, plan_id) -> dict:
    """그 계획(블록 전부)을 지운다. 없으면 LookupError. 다음 구글 반영에서 일정도 지워진다."""
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise ValueError("지울 계획의 id가 필요해요")
    keep, gone = [], None
    for p in plans(store):
        if p["id"] == plan_id and gone is None:
            gone = p
        else:
            keep.append(p)
    if gone is None:
        raise LookupError("그 공부 계획을 찾을 수 없어요(이미 지웠을 수 있어요)")
    store.data["plans"] = keep
    return gone


def lms_id(plan_id: str, n: int) -> str:
    """n번째(1부터) 블록의 캘린더 id. UID는 여기에 '@lms-agent'."""
    return f"study-{plan_id}-{n}"


def state(store, now=None) -> list[dict]:
    """state.plans: 저장된 계획(첫 블록이 이른 것부터). 블록마다 uid·구글 링크(마지막 반영에 들어 있었으면)."""
    now = now or now_local()
    gcal = (store.data.get("gcal") or {}).get("events") or {}
    hidden = hidden_courses(store.data)
    shorts = _course_shorts(store)
    out = []
    for p in plans(store):
        blocks = []
        for n, b in enumerate(p["blocks"], 1):
            if not isinstance(b, dict):
                continue
            lid = lms_id(p["id"], n)
            blocks.append({"start": b.get("start"), "end": b.get("end"), "title": b.get("title"),
                           "course_id": b.get("course_id"), "item_id": b.get("item_id"), "uid": lid + "@lms-agent",
                           "gcal_link": (gcal.get(lid) or {}).get("html_link")})
        ends = [parse_iso(b["end"]) for b in blocks if parse_iso(b.get("end"))]
        cid = p.get("course_id")
        out.append({"id": p["id"], "title": p.get("title"), "course_id": cid, "item_id": p.get("item_id"),
                    "course_short": shorts.get(str(cid)) if cid is not None else None,
                    "blocks": blocks, "created_at": p.get("created_at"), "decided_by": p.get("decided_by") or "user",
                    "reason": p.get("reason"), "hidden": cid is not None and str(cid) in hidden,
                    "past": bool(ends) and max(ends) <= now})
    out.sort(key=lambda p: (parse_iso((p["blocks"][0] if p["blocks"] else {}).get("start")) or now, p["id"]))
    return out
