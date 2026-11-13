"""코치 카드(계약 22-4): C 주간 브리핑 · D 바뀐 마감 · E 주간 회고와 종강 리포트 — 학기 마라톤 페이스메이커의 말.

화면이 상태를 물을 때마다 저장된 것(store.data)만으로 계산한다(네트워크 없음, 같은 입력·같은 시각이면 같은 결과).
- 주는 월~일(이 PC 시간대 — 한국 사용자는 KST). 카드의 created_for = 그 카드가 보이는 주의 월요일(종강 리포트는 마지막 주의 일요일).
- 카드 줄마다 근거 항목 id(item_ids)와 말투(tone)를 붙인다. 같은 과제의 후보 묶음(group)은 하나로 센다.
- 바뀐 마감(changes)은 더 새 글의 문장을 그대로 인용할 수 있을 때만 낸다(지어내지 않음).
- 성적은 화면 안에서만: 브리핑의 공부 줄은 학점 글자 없이 말하고, 백그라운드 알림(notify.briefing_message)은 마감·시험 줄만 쓴다.
- 제외(hidden)한 과목은 store.view()와 성적 묶음이 이미 뺀다.
- 구글 캘린더의 다른 일정(계약 23-3, 켰을 때): 브리핑의 '가장 몰린 요일'에 바쁨(busy) 일정 수도 센다 — 수만, 제목은 넣지 않는다.
  백그라운드 알림(toast_card)에는 다른 일정을 넣지 않는다.
기록: store.data["coach"] = {"dismissed": [닫은 카드 id…], "toasted": [브리핑을 알린 주의 월요일…]}(각각 최근 100개).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from . import core
from .extract import same_task
from .timeutil import iso, now_local, parse_iso

KINDS = ("briefing", "retro", "report")
TONES = ("deadline", "exam", "study", "pace", "warn", "cheer")
LIVE = ("review", "scheduled")
EXAM_DAYS = 30        # 브리핑에 넣는 시험: 지금부터 30일 안
STUDY_DAYS = 14       # 공부 줄의 근거: 그 과목의 14일 안 일정
CHANGE_DAYS = 14      # 바뀐 마감: 더 새 글(또는 바뀐 것을 처음 본 때)이 14일 안
MAX_IDS = 50          # 줄 하나의 근거 id 수
MAX_KEEP = 100        # dismissed·toasted 기록 수
MAX_CHANGES = 10
QUOTE_MAX = 500
WD = core.KO_WD
_ID = re.compile(r"(briefing|retro|report):\d{4}-\d{2}-\d{2}")
_OLD = datetime(2000, 1, 1, tzinfo=timezone.utc)
# Canvas 과제 마감과 다른 날짜가 글에 있을 때, 그 문장이 '바뀜'을 말할 때만 바뀐 마감으로 본다(다른 과제를 가리킬 수 있으므로)
_CHANGE_WORDS = re.compile(r"연장|변경|바뀌|바꿉|바꾸|미뤄|미룹|미루|연기|앞당|당겨|조정|정정|수정|"
                           r"extend|postpone|push(?:ed)? back|moved?|chang|updat|revis|resched", re.I)


# ---------------------------------------------------------------- 작은 도움

def _ts(s) -> float:
    d = parse_iso(s) if isinstance(s, str) else s
    return (d or _OLD).timestamp()


def _due(v: dict) -> datetime | None:
    return parse_iso(v.get("due"))


def _latest_ev(v: dict) -> dict:
    ev = [e for e in v.get("evidence") or [] if isinstance(e, dict)]
    return max(ev, key=lambda e: _ts(e.get("posted_at"))) if ev else {}


def _md(d: datetime) -> str:
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def _course(v: dict) -> str:
    return v.get("course_short") or v.get("course_name") or "과목 미상"


def _name(v: dict) -> str:
    return f"{_course(v)} {v.get('title') or '과제'}"


def _is_exam(v: dict) -> bool:
    return v.get("kind") == "시험"   # celebrate의 주 완주와 같은 규칙(시험은 '제출'할 마감이 아님)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _start(d: date, tz) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=tz)


def _week_n(sem: dict, monday: date) -> int | None:
    key = monday.isoformat()
    return next((w["n"] for w in sem.get("weeks") or [] if w.get("start") == key), None)


def _ids(vs) -> list[str]:
    return [v["id"] for v in vs]


def _line(text: str, ids, tone: str) -> dict:
    return {"text": text, "item_ids": list(dict.fromkeys(ids))[:MAX_IDS], "tone": tone}


def _card(kind: str, created_for: str, title: str, lines: list[dict], **extra) -> dict:
    return {"id": f"{kind}:{created_for}", "kind": kind, "title": title, "lines": lines,
            "created_for": created_for, **extra}


def _dday(d: datetime, now: datetime) -> str:
    n = (d.date() - now.date()).days
    return "D-day" if n <= 0 else f"D-{n}"


def _names(vs: list[dict], n: int = 3) -> str:
    out = ", ".join(_name(v) for v in vs[:n])
    return out + (f" 외 {len(vs) - n}개" if len(vs) > n else "")


def empty() -> dict:
    return {"briefing": None, "retro": None, "report": None, "changes": []}


def _read_box(data: dict) -> dict:
    box = data.get("coach")
    return box if isinstance(box, dict) else {}


def _box(data: dict) -> dict:
    box = data.get("coach")
    if not isinstance(box, dict):
        box = data["coach"] = {}
    return box


def _strs(xs) -> list[str]:
    return [x for x in xs or [] if isinstance(x, str)] if isinstance(xs, list) else []


# ---------------------------------------------------------------- 셀 것

def live(items: list[dict]) -> list[dict]:
    """일정·확인 대기 중 마감이 있는 것(마감 순). 같은 과제의 후보 묶음은 하나로: 승인된 것, 없으면 가장 최근 글의 후보.
    가장 최근 글이 Canvas 마감과 같은 후보(duplicate)면 Canvas 과제가 그 마감이므로 묶음은 세지 않고,
    묶음의 다른 후보가 그 과제를 대신하면(예: 연장 공지) 겹친 Canvas 과제는 세지 않는다(한 과제 = 하나)."""
    out, groups, covered = [], {}, set()
    for v in items:
        if v.get("group"):
            groups.setdefault(v["group"], []).append(v)
        elif v.get("status") in LIVE and v.get("due"):
            out.append(v)
    for members in groups.values():
        alive = [v for v in members if v.get("status") in LIVE and v.get("due")]
        if not alive:
            continue
        sched = [v for v in alive if v["status"] == "scheduled"]
        rank = lambda v: (_ts(_latest_ev(v).get("posted_at")), v["id"])  # noqa: E731
        if sched:
            pick = sched[0]
        else:
            top = max((v for v in members if v.get("status") != "rejected"), key=rank)
            if top.get("status") == "duplicate":
                continue
            pick = top if any(top is v for v in alive) else max(alive, key=rank)
        if pick.get("duplicate_of"):   # 승인된 겹친 후보 → Canvas 과제(제출 기록이 있는 쪽)로 센다
            continue
        dups = {v["duplicate_of"] for v in members if v.get("duplicate_of")}
        if dups and not pick.get("submitted") and any(c.get("submitted") for c in items if c["id"] in dups):
            pick = {**pick, "submitted": True}   # Canvas에 낸 기록이 있으면 낸 것으로 센다(항목은 고치지 않는다)
        out.append(pick)
        covered |= dups
    out = [v for v in out if v["id"] not in covered]
    out.sort(key=lambda v: (_ts(v["due"]), v["id"]))
    return out


def _busiest(vs: list[dict], events=()) -> tuple[date, int, dict] | None:
    """마감·시험(과 바쁨 일정 시작 시각들)이 합쳐 2개 이상 몰린 날(같으면 이른 날) → (날, 합, {"deadline", "exam", "event"} 수).
    마감·시험이 하나도 없는 날은 고르지 않는다(브리핑은 마감 줄이다)."""
    by: dict[date, dict] = {}
    for v in vs:
        d = _due(v)
        if d:
            c = by.setdefault(d.date(), {"deadline": 0, "exam": 0, "event": 0})
            c["exam" if _is_exam(v) else "deadline"] += 1
    for start in events:
        if start.date() in by:
            by[start.date()]["event"] += 1
    total = {k: sum(c.values()) for k, c in by.items()}
    if not total:
        return None
    day = min(total, key=lambda k: (-total[k], k))
    return (day, total[day], by[day]) if total[day] >= 2 else None


def _load_text(counts: dict) -> str:
    """' — 마감 2개·일정 3개'(다른 일정이 있을 때만, 수만)."""
    if not counts.get("event"):
        return ""
    parts = [f"{label} {counts[k]}개" for k, label in (("deadline", "마감"), ("exam", "시험"), ("event", "일정"))
             if counts.get(k)]
    return " — " + "·".join(parts)


def _pace_text(cel: dict | None, nudge: bool = False) -> str | None:
    """'지금 27.3km 지점 · '마의 구간'(30km)까지 2.7km · 연속 완주 2주째(3주 메달까지 1주)'."""
    if not cel:
        return None
    parts = []
    km, nxt = cel.get("km"), cel.get("next")
    if isinstance(km, (int, float)) and km > 0:
        parts.append(f"지금 {km:g}km 지점")
    if isinstance(nxt, dict) and nxt.get("km") is not None:
        label = str(nxt.get("label") or "")
        where = ("결승선" if label == "결승" else f"{nxt['km']:g}km" if label.endswith("km") or not label
                 else f"'{label}'({nxt['km']:g}km)")
        parts.append(f"{where}까지 {float(nxt.get('left') or 0):g}km")
    s, sn = int(cel.get("streak") or 0), cel.get("streak_next")
    if s:
        parts.append(f"연속 완주 {s}주째" + (f"({sn}주 메달까지 {sn - s}주)" if sn else ""))
    elif nudge and sn:
        parts.append("이번 주를 다 내면 연속 완주가 시작돼요")
    return " · ".join(parts) or None


_STUDY_LEFT = re.compile(r"남은 비중 [\d.]+%")


def _study_why(reason: str) -> str:
    """gradebook.priorities의 이유를 학점 글자 없이(브리핑은 대시보드 맨 위라 성적을 크게 드러내지 않는다)."""
    left = _STUDY_LEFT.search(reason)
    if "가까움" in reason or "기준까지" in reason:
        why = "한 단계 위 학점이 가까워요"
    elif "위험" in reason or "아래로" in reason:
        why = "지금 학점을 지키려면 챙겨야 해요"
    else:
        why = "남은 평가 비중이 커요"
    return why + (f"({left.group(0)})" if left else "")


def _study_line(grades: dict | None, lv: list[dict], now: datetime) -> dict | None:
    if not grades:
        return None
    prio = [p for p in grades.get("priorities") or []
            if isinstance(p, dict) and p.get("course") and p.get("grade") and float(p.get("score") or 0) > 0][:2]
    if not prio:
        return None
    cid_of = {c.get("short_name"): str(c.get("course_id")) for c in grades.get("courses") or []}
    soon = now + timedelta(days=STUDY_DAYS)
    ids = []
    for p in prio:
        cid = cid_of.get(p["course"])
        ids += [v["id"] for v in lv if cid and str(v.get("course_id")) == cid and now <= _due(v) <= soon]
    text = f"공부는 {prio[0]['course']}부터 — {_study_why(str(prio[0].get('reason') or ''))}"
    if len(prio) > 1:
        text += f", 그다음 {prio[1]['course']}"
    return _line(text, ids, "study")


# ---------------------------------------------------------------- C. 주간 브리핑

def _briefing(sem: dict, lv: list[dict], grades: dict | None, cel: dict | None, now: datetime,
              events=()) -> dict | None:
    monday = _monday(now.date())
    wk = _week_n(sem, monday)
    if not wk:
        return None
    start = _start(monday, now.tzinfo)
    end = start + timedelta(days=7)
    week = [v for v in lv if start <= _due(v) < end]
    # 이번 주의 아직 끝나지 않은 바쁨 일정(시작 시각만 — 제목 없음)
    ev_starts = [s.astimezone(now.tzinfo) for s, t, _ in events if start <= s < end and t >= now]
    dl = [v for v in week if not _is_exam(v)]
    lines = []
    if dl:
        left = [v for v in dl if not v.get("submitted") and _due(v) >= now]
        if not left:
            lines.append(_line(f"이번 주 마감 {len(dl)}개를 모두 끝냈어요. 페이스 좋아요!", _ids(dl), "cheer"))
        else:
            text = (f"이번 주 마감 {len(dl)}개" if len(left) == len(dl)
                    else f"이번 주 마감 {len(dl)}개 중 {len(left)}개 남았어요")
            busy = _busiest(left + [v for v in week if _is_exam(v) and _due(v) >= now], ev_starts)
            if busy:
                text += f", {'오늘' if busy[0] == now.date() else WD[busy[0].weekday()] + '요일'}이 고비예요"
                text += _load_text(busy[2])
            elif len(left) == len(dl):
                text += " — 하나씩 끊어 가요"
            lines.append(_line(text, _ids(dl), "deadline"))
        review = [v for v in dl if v.get("status") == "review"]
        if review:
            lines.append(_line(f"그중 {len(review)}개는 아직 확인 전이에요 — 확인함에서 일정에 넣을지 정해 주세요",
                               _ids(review), "warn"))
    else:
        nxt = [v for v in lv if _due(v) >= end and not _is_exam(v)][:1]
        text = "이번 주는 마감이 없어요"
        text += (f". 다음은 {_name(nxt[0])} {_md(_due(nxt[0]))} — 미리 당겨 두면 편해요" if nxt
                 else ". 숨 고르며 페이스 유지!")
        lines.append(_line(text, _ids(nxt), "cheer"))
    exams = [v for v in lv if _is_exam(v) and now <= _due(v) <= now + timedelta(days=EXAM_DAYS)][:3]
    if exams:
        lines.append(_line("시험 " + " · ".join(f"{_dday(_due(v), now)} {_name(v)} {_md(_due(v))}" for v in exams),
                           _ids(exams), "exam"))
    study = _study_line(grades, lv, now)
    if study:
        lines.append(study)
    pace = _pace_text(cel, nudge=bool(dl))
    if pace:
        lines.append(_line(pace, [], "pace"))
    return _card("briefing", monday.isoformat(), f"{wk}주차 작전", lines, week=wk)


# ---------------------------------------------------------------- E. 주간 회고(월·화)

def _retro(sem: dict, items: list[dict], lv: list[dict], cel: dict | None, now: datetime) -> dict | None:
    if now.weekday() > 1:
        return None
    this_mon = _monday(now.date())
    last_mon = this_mon - timedelta(days=7)
    wk = _week_n(sem, last_mon)
    if not wk:
        return None
    s, e = _start(last_mon, now.tzinfo), _start(this_mon, now.tzinfo)
    inweek = lambda v: v.get("due") and s <= _due(v) < e  # noqa: E731
    sched = [v for v in items if v.get("status") == "scheduled" and not _is_exam(v) and inweek(v)]
    review = [v for v in lv if v.get("status") == "review" and not _is_exam(v) and inweek(v)]
    if not sched and not review:
        return None
    done = [v for v in sched if v.get("submitted")]
    missed = [v for v in sched if not v.get("submitted")]
    lines = []
    if sched and not missed:
        lines.append(_line(f"지난주 마감 {len(sched)}개 모두 제출 — {wk}주차 완주했어요!", _ids(sched), "cheer"))
    elif sched:
        lines.append(_line(f"지난주 마감 {len(sched)}개 중 {len(done)}개 제출했어요", _ids(sched), "deadline"))
        lines.append(_line(f"제출 표시가 없어요: {_names(missed)} — 이메일·종이로 냈다면 체크해 주세요",
                           _ids(missed), "warn"))
    if review:
        lines.append(_line(f"확인 안 한 후보 {len(review)}개가 지난주 마감이었어요 — 확인함에서 정리해 주세요",
                           _ids(review), "warn"))
    pace = _pace_text(cel)
    if pace:
        lines.append(_line(pace, [], "pace"))
    elif missed:
        lines.append(_line("이번 주부터 다시 페이스를 올려 봐요", [], "pace"))
    return _card("retro", this_mon.isoformat(), f"{wk}주차 돌아보기", lines, week=wk,
                 total=len(sched), done=len(done))


# ---------------------------------------------------------------- E. 종강 리포트

def _best_streak(store, sem: dict, items: list[dict], now: datetime) -> int:
    """합류한 뒤 끝난 주들 중 최고 연속 완주(celebrate의 주 완주·연속과 같은 규칙: 마감 없는 주는 건너뛴다)."""
    from . import celebrate
    join = celebrate.join_info(store.data, sem, now)
    by: dict[int, list[dict]] = {}
    for v in items:
        if v.get("status") == "scheduled" and not _is_exam(v) and v.get("due"):
            n = core.week_of(sem, v["due"])
            if n:
                by.setdefault(n, []).append(v)
    run = best = 0
    for w in sem.get("weeks") or []:
        end = date.fromisoformat(w["end"])
        if end >= now.date() or (join and end < join["day"]):
            continue
        its = by.get(w["n"])
        if not its:
            continue
        run = run + 1 if all(v.get("submitted") for v in its) else 0
        best = max(best, run)
    return best


def _course_lines(grades: dict | None, sched: list[dict]) -> list[dict]:
    """과목마다 한 줄: 마감 제출 수 + (화면 안에서만) 추정 학점."""
    rows = []
    if grades and grades.get("courses"):
        courses = [(str(c.get("course_id")), c.get("short_name") or "과목", c) for c in grades["courses"]]
    else:
        seen: dict[str, str] = {}
        for v in sched:
            seen.setdefault(str(v.get("course_id")), _course(v))
        courses = [(cid, name, {}) for cid, name in seen.items()]
    for cid, name, c in courses:
        its = [v for v in sched if str(v.get("course_id")) == cid]
        est = c.get("estimate") or {}
        bits = [f"마감 {len(its)}개 중 {sum(1 for v in its if v.get('submitted'))}개 제출"] if its else []
        if c.get("pass_fail"):
            bits.append("P/NP")
        elif est.get("grade"):
            bits.append(f"추정 학점 {est['grade']}" + (" (기본 학점 비율 기준)" if c.get("letter_basis") == "default" else ""))
        if its or est.get("grade"):
            rows.append(_line(f"{name}: " + " · ".join(bits), _ids(its), "study"))
    return rows


def _report(store, sem: dict, items: list[dict], grades: dict | None, cel: dict | None,
            now: datetime) -> dict | None:
    total = int(sem.get("weeks_total") or 0)
    weeks = sem.get("weeks") or []
    if not total or not weeks or int(sem.get("week_now") or 0) <= total:
        return None
    first = _start(date.fromisoformat(weeks[0]["start"]), now.tzinfo)
    last = _start(date.fromisoformat(weeks[-1]["end"]), now.tzinfo) + timedelta(days=1)
    sched = [v for v in items if v.get("status") == "scheduled" and not _is_exam(v) and v.get("due")
             and first <= _due(v) < last]
    done = [v for v in sched if v.get("submitted")]
    lines = []
    if sched:
        lines.append(_line(f"한 학기 마감 {len(sched)}개 중 {len(done)}개를 냈어요", _ids(sched),
                           "cheer" if len(done) == len(sched) else "deadline"))
    decided = [v for v in items if v.get("origin") == "found" and v.get("decided_at")
               and v.get("decision") in ("approved", "rejected")]
    if decided:
        lines.append(_line(f"공지·메시지에서 찾은 마감 후보 {len(decided)}개를 함께 정리했어요", _ids(decided), "cheer"))
    medals = [m for m in (cel or {}).get("medals") or [] if isinstance(m, dict) and m.get("title")]
    if medals:
        names = ", ".join(m["title"] for m in medals[:4]) + (f" 외 {len(medals) - 4}개" if len(medals) > 4 else "")
        lines.append(_line(f"메달 {len(medals)}개 — {names}", [], "cheer"))
    else:
        lines.append(_line("메달은 없어도 끝까지 같이 달렸어요", [], "cheer"))
    best = _best_streak(store, sem, items, now)
    if best:
        lines.append(_line(f"최고 연속 완주 {best}주", [], "pace"))
    lines += _course_lines(grades, sched)
    return _card("report", weeks[-1]["end"], "한 학기 완주 리포트", lines, weeks_total=total)


# ---------------------------------------------------------------- D. 바뀐 마감

def _tkey(title) -> str:
    return re.sub(r"\s+", "", str(title or "")).lower()


def _quote(ev: dict) -> str | None:
    text = " ".join(str(ev.get("text") or "").split())
    if not text:
        return None
    return text if len(text) <= QUOTE_MAX else text[:QUOTE_MAX] + "…"


def _change(v: dict, old_due: str, ev: dict, basis: str, from_item: str | None, by_id: dict) -> dict | None:
    new_d, old_d = _due(v), parse_iso(old_due)
    quote = _quote(ev)
    if not new_d or not old_d or new_d == old_d or not quote:
        return None
    target = by_id.get(v.get("duplicate_of")) if v.get("status") == "duplicate" else None
    shown = target or v
    return {"item_id": shown["id"], "title": shown.get("title"), "course_short": _course(shown),
            "from": iso(old_d), "to": iso(new_d), "direction": "later" if new_d > old_d else "earlier",
            "source_title": ev.get("source_title"), "source_type": ev.get("source_type"),
            "posted_at": ev.get("posted_at"), "quote": quote, "url": ev.get("url"),
            "basis": basis, "from_item_id": from_item, "status": shown.get("status")}


def changes(store, items: list[dict], now: datetime | None = None) -> list[dict]:
    """최근 14일 안에 마감이 바뀐 항목(최신 글 먼저, 최대 10). 근거 문장(그대로 인용)이 있을 때만.
    basis: "conflict"(같은 과제의 다른 후보 — 충돌·대체됨), "edited"(같은 글이 고쳐져 옛 후보가 사라짐 — 소식 기록 news_seen),
    "canvas"(Canvas 과제 마감이 바뀌었고 같은 날짜를 적은 글이 있음), "canvas_post"(Canvas 마감과 다른 날짜를 '연장·변경'이라 적은 더 새 글)."""
    now = now or now_local()
    cut = now - timedelta(days=CHANGE_DAYS)
    by_id = {v["id"]: v for v in items}
    out: list[dict] = []
    done: set[str] = set()

    def add(entry):
        if entry and entry["item_id"] not in done:
            done.add(entry["item_id"])
            out.append(entry)

    # 1) 같은 과제 후보 묶음: 가장 최근 글의 후보와, 그보다 먼저 쓰인 다른 날짜의 후보
    groups: dict[str, list[dict]] = {}
    for v in items:
        if v.get("group") and v.get("status") != "rejected" and v.get("due"):
            groups.setdefault(v["group"], []).append(v)
    for members in groups.values():
        if len({_ts(v["due"]) for v in members}) < 2:
            continue
        ranked = sorted(members, key=lambda v: (_ts(_latest_ev(v).get("posted_at")), v["id"]), reverse=True)
        new = ranked[0]
        ev = _latest_ev(new)
        at = parse_iso(ev.get("posted_at"))
        if not at or at < cut:
            continue
        older = [b for b in ranked[1:] if _ts(b["due"]) != _ts(new["due"]) and _ts(_latest_ev(b).get("posted_at")) < at.timestamp()]
        if not older:
            continue
        old = older[0]
        if old.get("status") == "scheduled" and new.get("status") == "replaced":
            decided, seen = parse_iso(old.get("decided_at")), parse_iso(new.get("first_seen"))
            if decided and seen and decided >= seen:
                continue   # 신규 글을 본 뒤에 옛 마감을 직접 골랐다
        # 옛 후보가 Canvas 마감과 같은 것(duplicate)이면 옛 마감은 그 Canvas 과제다
        add(_change(new, old["due"], ev, "conflict",
                    old.get("duplicate_of") if old.get("status") == "duplicate" else old["id"], by_id))

    # 2) 같은 글이 고쳐짐(찾은 마감은 날짜가 id에 들어 있어 옛 후보는 사라진다) — 소식 카드가 남긴 기록(celebrate news_seen)과 비교
    recs = (store.data.get("news_seen") or {}).get("items") or {}
    raw_ids = {it.get("id") for it in store.data.get("items") or []}
    vanished = [(rid, r) for rid, r in recs.items() if rid not in raw_ids and isinstance(r, dict) and r.get("due")]
    for v in items:
        if v.get("origin") != "found" or v.get("status") not in LIVE or v["id"] in done or not v.get("due"):
            continue
        seen = parse_iso(v.get("first_seen"))
        if not seen or seen < cut:
            continue
        same = [(rid, r) for rid, r in vanished if _ts(r["due"]) != _ts(v["due"])
                and ((r.get("l") and r["l"] == v.get("linked_to"))
                     or (str(r.get("c")) == str(v.get("course_id")) and r.get("t") == _tkey(v.get("title"))))]
        if same:
            rid, r = max(same, key=lambda x: str(x[1].get("at") or ""))
            add(_change(v, r["due"], _latest_ev(v), "edited", None, by_id))

    # 3) Canvas 과제 마감이 바뀜(소식 기록과 다름) — 같은 새 날짜를 적은 글(겹친 후보)이 있을 때만
    for v in items:
        rec = recs.get(v["id"])
        if v.get("origin") != "canvas" or v["id"] in done or not isinstance(rec, dict) or not rec.get("due") \
                or not v.get("due") or _ts(rec["due"]) == _ts(v["due"]):
            continue
        dups = [f for f in items if f.get("duplicate_of") == v["id"] and _ts(f.get("due")) == _ts(v["due"])]
        evs = [e for e in (_latest_ev(f) for f in dups) if e and _ts(e.get("posted_at")) >= cut.timestamp()]
        if evs:
            add(_change(v, rec["due"], max(evs, key=lambda e: _ts(e.get("posted_at"))), "canvas", None, by_id))

    # 4) Canvas 마감과 다른 날짜를 '연장·변경'이라 적은, Canvas 과제보다 새 글
    updated = {f"canvas-{a.get('course_id')}-{a.get('id')}": parse_iso(a.get("updated_at"))
               for a in (store.data.get("cache") or {}).get("assignments") or []}
    canvas = [c for c in items if c.get("origin") == "canvas" and c.get("due") and c.get("status") in LIVE]
    for f in items:
        if f.get("origin") != "found" or f.get("status") != "review" or f["id"] in done or f.get("group") \
                or f.get("duplicate_of") or not f.get("due"):
            continue
        ev = _latest_ev(f)
        at = parse_iso(ev.get("posted_at"))
        if not at or at < cut or not _CHANGE_WORDS.search(str(ev.get("text") or "")):
            continue
        for c in canvas:
            up = updated.get(c["id"])
            if (str(c.get("course_id")) == str(f.get("course_id")) and _ts(c["due"]) != _ts(f["due"])
                    and up and at > up and same_task(f.get("title"), c.get("title"))):
                add(_change(f, c["due"], ev, "canvas_post", c["id"], by_id))
                break

    out.sort(key=lambda c: _ts(c.get("posted_at")), reverse=True)
    return out[:MAX_CHANGES]


# ---------------------------------------------------------------- 묶음

def _inputs(store, now: datetime, sem, items, grades, celebrations):
    """화면 상태(features)가 넘기지 않은 재료를 같은 방식으로 만든다(백그라운드 알림·테스트용)."""
    data = store.data
    cache = data.get("cache")
    if sem is None:
        sem = core.semester_info(cache, store.view(), today=now.date())
    if items is None:
        from . import gradebook
        items = core.annotate(store.view(), sem, cache)
        try:
            gradebook.link_items(store, items, sem)
        except Exception:   # 평가 항목 연결은 코치에 꼭 필요하지 않다
            pass
    if grades is None:
        try:
            from . import course_modes, gradebook
            grades = gradebook.section(store, course_modes.annotate_courses(core.course_summaries(cache), data),
                                       items=items, sem=sem)
        except Exception:   # 성적 계산이 실패해도 마감·페이스 줄은 낸다
            grades = None
    if celebrations is None:
        try:
            from . import celebrate
            celebrations = celebrate.state(store, sem, items, now)[0]   # 읽기만(저장할 것은 버린다)
        except Exception:
            celebrations = None
    return sem, items, grades, celebrations


def _events(store, events):
    """브리핑에 셀 다른 일정 [(시작, 끝, 종일)] — 주지 않으면 저장된 것(꺼져 있으면 없음)."""
    if events is not None:
        return list(events)
    try:
        from . import gevents
        return gevents.busy(store)
    except Exception:   # 다른 일정은 덤이다
        return []


def cards(store, now: datetime | None = None, sem: dict | None = None, items: list | None = None,
          grades: dict | None = None, celebrations: dict | None = None, events=None) -> dict:
    """닫은 것도 포함한 지금의 카드 {"briefing", "retro", "report"}(없으면 None) + "changes".
    events: 브리핑에 셀 바쁨 일정 [(시작, 끝, 종일)] — None이면 저장된 다른 일정(계약 23-3), []이면 세지 않음."""
    now = now or now_local()
    if store.data.get("cache") is None:   # 첫 동기화 전
        return empty()
    sem, items, grades, cel = _inputs(store, now, sem, items, grades, celebrations)
    lv = live(items)
    return {"briefing": _briefing(sem, lv, grades, cel, now, _events(store, events)),
            "retro": _retro(sem, items, lv, cel, now),
            "report": _report(store, sem, items, grades, cel, now),
            "changes": changes(store, items, now)}


def state(store, now: datetime | None = None, sem: dict | None = None, items: list | None = None,
          grades: dict | None = None, celebrations: dict | None = None, events=None) -> dict:
    """state.coach = {"briefing", "retro", "report": 카드|None, "changes": [...]} — 닫은 카드는 None."""
    out = cards(store, now, sem, items, grades, celebrations, events)
    gone = set(_strs(_read_box(store.data).get("dismissed")))
    for k in KINDS:
        if out[k] and out[k]["id"] in gone:
            out[k] = None
    return out


# ---------------------------------------------------------------- 닫기·알림 기록 (잠금 안에서 부른다)

def valid_id(card_id) -> bool:
    return isinstance(card_id, str) and len(card_id) <= 40 and bool(_ID.fullmatch(card_id))


def dismiss(store, card_id, now: datetime | None = None) -> bool:
    """지금 보이는 카드(또는 이미 닫은 카드)면 닫은 것으로 남기고 True. 모르는 id면 False(저장 안 함)."""
    if not valid_id(card_id):
        return False
    gone = _strs(_read_box(store.data).get("dismissed"))
    if card_id in gone:
        return True
    current = cards(store, now)
    if card_id not in {current[k]["id"] for k in KINDS if current[k]}:
        return False
    _box(store.data)["dismissed"] = (gone + [card_id])[-MAX_KEEP:]
    return True


def toast_card(store, now: datetime | None = None) -> dict | None:
    """백그라운드 동기화가 알릴 이번 주 브리핑(계약 22-4: 주 1회, 그 주 첫 백그라운드 동기화). 알리지 않으면 None:
    이미 알린 주, 카드를 닫은 주, 그리고 이번 주에 앱·CLI로 동기화한 적이 있으면(화면에서 이미 볼 수 있었다 — 앱을 안 여는 사람용 알림)."""
    now = now or now_local()
    card = state(store, now, events=[])["briefing"]   # 알림(잠금 화면)에는 다른 일정을 넣지 않는다(계약 23장)
    if not card or card["created_for"] in _strs(_read_box(store.data).get("toasted")):
        return None
    since = _start(date.fromisoformat(card["created_for"]), now.tzinfo)
    for r in store.data.get("runs") or []:
        if isinstance(r, dict) and r.get("by") != "background" and _ts(r.get("finished_at")) >= since.timestamp():
            return None
    return card


def mark_toasted(data: dict, week: str) -> None:
    box = _box(data)
    told = [w for w in _strs(box.get("toasted")) if w != week]
    box["toasted"] = (told + [week])[-MAX_KEEP:]
