"""동기화 흐름. 역할을 셋으로 나눴다(JEDEC 에이전트 구조를 규칙 기반 코드로 옮긴 것).

- 수집(collect): Canvas에서 근거가 될 글을 모은다. 일부가 실패해도 나머지는 계속한다.
- 정리(build_items): 글마다 마감을 찾고, 같은 과제끼리 묶고, Canvas 과제와 겹치는지·
  서로 다른 마감이 적혀 있는지 표시하고, 마감 근거를 끝내 못 찾은 과제를 골라낸다.
- 출력(ics.build_ics + validate_ics): 승인된 것만 캘린더 파일로 만들고 검사한다.

사람은 화면에서 근거를 보고 승인·제외만 한다.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta

from .canvas import Canvas, CanvasError
from .extract import RULES_VERSION, Found, detect_method, find_deadlines, html_to_text, same_task, title_hint
from .timeutil import iso, now_local, parse_iso

LMS_TYPES = {"online_upload", "online_text_entry", "online_url", "online_quiz",
             "external_tool", "discussion_topic", "media_recording", "student_annotation"}
CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


# ================================================================ 수집

def collect(canvas: Canvas, now: datetime | None = None, lookback_days: int = 120,
            prev: dict | None = None, full: bool = False) -> dict:
    """정찰: 목록 요청만으로 현재 상태를 받는다. 공지·과제·강의계획서는 목록에 본문이 같이 오므로
    여기서 다 받고(바뀌었는지는 build_items가 해시로 가린다), 메시지는 last_message_at이 달라진
    대화만 본문을 받는다. prev는 지난 실행의 cache."""
    now = now or now_local()
    since = now - timedelta(days=lookback_days)
    errors: list[str] = []
    start_requests = canvas.request_count
    cache = {"fetched_at": iso(now), "courses": [], "assignments": [], "announcements": [],
             "conversations": [], "errors": errors, "probe": {}}

    courses = canvas.courses()  # 과목 목록이 안 되면 아무것도 못 하므로 여기서는 오류를 올려보낸다

    def score(c: dict):
        for e in c.get("enrollments") or []:
            if e.get("type") in ("student", "StudentEnrollment") and e.get("computed_current_score") is not None:
                return e["computed_current_score"]
        return None

    cache["courses"] = [{"id": str(c["id"]), "name": c.get("name") or c.get("course_code") or str(c["id"]),
                         "course_code": c.get("course_code"), "syllabus_body": c.get("syllabus_body"),
                         "start_at": c.get("start_at"), "end_at": c.get("end_at"),
                         "term_start": (c.get("term") or {}).get("start_at"),
                         "term_end": (c.get("term") or {}).get("end_at"),
                         "current_score": score(c)} for c in courses]
    for c in cache["courses"]:
        try:
            for a in canvas.assignments(c["id"]):
                a = dict(a)
                a["course_id"] = c["id"]
                cache["assignments"].append(a)
        except CanvasError as e:  # 못 읽으면 지난번 것을 그대로 둔다(근거가 사라지지 않게)
            cache["assignments"] += [a for a in (prev or {}).get("assignments", []) if a.get("course_id") == c["id"]]
            errors.append(f"'{c['name']}' 과제를 읽지 못해 지난번 내용을 씁니다: {e}")
    try:
        cache["announcements"] = canvas.announcements([c["id"] for c in cache["courses"]], since, now + timedelta(days=1))
    except CanvasError as e:
        cache["announcements"] = list((prev or {}).get("announcements", []))
        errors.append(f"공지를 읽지 못해 지난번 내용을 씁니다: {e}")
    known = {} if full or not prev else {str(c["id"]): c for c in prev.get("conversations", [])}
    fetched, reused = [], 0
    try:
        for conv in canvas.conversation_list(since):
            old = known.get(str(conv["id"]))
            if old and old.get("last_message_at") == conv.get("last_message_at") and old.get("messages") is not None:
                cache["conversations"].append({**old, **conv, "messages": old["messages"]})
                reused += 1
                continue
            detail = canvas.conversation_detail(conv)
            if detail is not None:
                cache["conversations"].append(detail)
                fetched.append(str(conv["id"]))
    except CanvasError as e:
        if not cache["conversations"]:
            cache["conversations"] = list((prev or {}).get("conversations", []))
        errors.append(f"메시지함을 읽지 못했습니다: {e}")
    cache["probe"] ={"requests": canvas.request_count - start_requests,
                      "conversations_fetched": fetched, "conversations_reused": reused}
    return cache


# ================================================================ 정리

# 마감을 찾을 글의 출처. 출처 하나 = 함수 하나: fn(cache, manual, now) → 글 목록.
# 글: course_id, (course_name), type, id, title, text, is_html, posted_raw(해시용 원본 시각), posted_at, url.
# 새 출처(예: LearningX 게시판 알림, Canvas 파일 목록)는 함수를 만들고 @source로 등록한다.
# type:id 는 실행 사이에 같아야 한다(바뀐 글만 다시 읽는 기준).
SOURCES: list = []


def source(fn):
    SOURCES.append(fn)
    return fn


def _text(course_id, typ, sid, title, text, is_html, posted_raw, now, url=None, course_name=None) -> dict:
    return {"course_id": course_id, "course_name": course_name, "type": typ, "id": str(sid), "title": title,
            "text": text or "", "is_html": is_html, "posted_raw": posted_raw,
            "posted_at": parse_iso(posted_raw) or now, "url": url}


@source
def syllabus_source(cache, manual, now):
    return [_text(c["id"], "syllabus", c["id"], "강의계획서", c["syllabus_body"], True, c.get("start_at"), now)
            for c in cache.get("courses", []) if c.get("syllabus_body")]


@source
def assignment_source(cache, manual, now):
    return [_text(a["course_id"], "assignment", a["id"], a.get("name"), a["description"], True,
                  a.get("created_at") or a.get("updated_at"), now, a.get("html_url"))
            for a in cache.get("assignments", []) if a.get("description")]


@source
def announcement_source(cache, manual, now):
    return [_text(str(n.get("context_code", "")).replace("course_", ""), "announcement", n["id"], n.get("title"),
                  n.get("message"), True, n.get("posted_at"), now, n.get("html_url"))
            for n in cache.get("announcements", [])]


@source
def inbox_source(cache, manual, now):
    names = {c["id"] for c in cache.get("courses", [])}
    out = []
    for cv in cache.get("conversations", []):
        code = str(cv.get("context_code") or "")
        cid = code.replace("course_", "") if code.startswith("course_") else None
        for m in (cv.get("messages") or [])[:10]:
            out.append(_text(cid if cid in names else None, "inbox", f"{cv['id']}-{m.get('id')}",
                             cv.get("subject") or "메시지", m.get("body"), False, m.get("created_at"), now,
                             course_name=cv.get("context_name")))
    return out


@source
def manual_source(cache, manual, now):
    names = {c["name"]: c["id"] for c in cache.get("courses", [])}
    return [_text(names.get(m["course_name"]), "manual", m["id"], "직접 입력", m["text"], False,
                  m.get("posted_at"), now, course_name=m["course_name"]) for m in manual]


def _sources(cache: dict | None, manual: list[dict], now: datetime) -> list[dict]:
    """등록된 모든 출처의 글. 과목 이름은 Canvas 과목 목록 기준으로 채운다."""
    cache = cache or {}
    names = {c["id"]: c["name"] for c in cache.get("courses", [])}
    out = [t for fn in SOURCES for t in fn(cache, manual, now)]
    for t in out:
        t["course_name"] = names.get(t["course_id"]) or t["course_name"] or "과목 미상"
    return out


def _hid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _fmt(dt: datetime) -> str:
    return f"{dt.month}/{dt.day} {dt:%H:%M}"


def _canvas_item(a: dict, course_name: str) -> dict:
    types = set(a.get("submission_types") or [])
    if types & LMS_TYPES:
        method, email_to = "lms", None
    elif "on_paper" in types:
        method, email_to = "paper", None
    else:
        method, email_to = detect_method(html_to_text(a.get("description")))
    sub = a.get("submission") or {}
    submitted = bool(types & LMS_TYPES) and (sub.get("workflow_state") in ("submitted", "graded", "pending_review")
                                             or bool(sub.get("submitted_at")))
    due = parse_iso(a.get("due_at"))
    return {"id": f"canvas-{a['course_id']}-{a['id']}", "origin": "canvas", "course_id": a["course_id"],
            "course_name": course_name, "title": a.get("name") or "과제", "due": iso(due),
            "kind": "시험" if "online_quiz" in types else "과제", "method": method, "email_to": email_to,
            "confidence": None, "notes": [], "evidence": [], "url": a.get("html_url"),
            "canvas_submitted": submitted, "duplicate_of": None, "linked_to": None, "conflict": None,
            "default_decision": "approved" if due else "pending", "_name": a.get("name") or ""}


def build_items(cache: dict | None, manual: list[dict], now: datetime | None = None,
                lookback_days: int = 120, extracted: dict | None = None,
                reason_all: str | None = None) -> tuple[list[dict], dict, dict]:
    """extracted: 지난 실행에서 글마다 찾은 결과 {글 key: {hash, rules, found}}.
    내용 해시가 같은 글은 다시 읽지 않고 그 결과를 쓴다(탐색 목표 = 바뀐 글만).
    None이면 모든 글을 읽고, 이유는 reason_all(처음 실행/전체 다시 확인/규칙 업데이트)."""
    now = now or now_local()
    oldest = now - timedelta(days=lookback_days)
    names = {c["id"]: c["name"] for c in (cache or {}).get("courses", [])}
    canvas = [_canvas_item(a, names.get(a["course_id"], "과목 미상")) for a in (cache or {}).get("assignments", [])]
    by_canvas_id = {it["id"]: it for it in canvas}
    sources = _sources(cache, manual, now)

    # 1) 글마다 마감 찾기
    cands: list[dict] = []
    new_extracted: dict = {}
    targets: list[dict] = []
    for s in sources:
        key = f"{s['type']}:{s['id']}"
        h = _hid(s["text"], s.get("posted_raw"), s["course_id"])
        old = (extracted or {}).get(key)
        if old and old.get("hash") == h and old.get("rules") == RULES_VERSION:
            founds = [_found_from(d) for d in old["found"]]
        else:
            founds = find_deadlines(s["text"], s["posted_at"], is_html=s["is_html"])
            if extracted is None:
                reason = reason_all or "처음 실행"
            elif old is None:
                reason = "새 글"
            elif old.get("rules") != RULES_VERSION:
                reason = "규칙 업데이트"
            else:
                reason = "내용 바뀜"
            targets.append({"key": key, "type": s["type"], "title": s["title"], "course_name": s["course_name"],
                            "reason": reason, "found": len(founds)})
        new_extracted[key] = {"hash": h, "rules": RULES_VERSION, "found": [_found_to(f) for f in founds]}
        for f in founds:
            if f.due < oldest:
                continue
            own = by_canvas_id.get(f"canvas-{s['course_id']}-{s['id']}") if s["type"] == "assignment" else None
            if own and own["due"] and parse_iso(own["due"]).date() == f.due.date():
                continue  # 과제 설명에 Canvas 마감을 다시 적은 것
            cands.append({"course_id": s["course_id"], "course_name": s["course_name"], "due": f.due,
                          "hint": f.title_hint, "kind": f.kind, "method": f.method, "email_to": f.email_to,
                          "confidence": f.confidence, "notes": list(f.notes),
                          "own": own["id"] if own else None,
                          "source_title": s["title"] if s["type"] in ("announcement", "inbox") else None,
                          "evidence": [{"text": f.sentence, "source_type": s["type"], "source_title": s["title"],
                                        "url": s["url"], "posted_at": iso(s["posted_at"])}]})

    # 2) 같은 과목·같은 마감 시각은 하나로 묶는다
    merged: dict[tuple, dict] = {}
    for c in cands:
        key = (c["course_id"] or c["course_name"], c["due"])
        m = merged.get(key)
        if m is None:
            merged[key] = c
            continue
        m["evidence"] += c["evidence"]
        m["notes"] = list(dict.fromkeys(m["notes"] + c["notes"]))
        if CONF_ORDER[c["confidence"]] > CONF_ORDER[m["confidence"]]:
            m["confidence"] = c["confidence"]
        for k in ("hint", "email_to", "own", "source_title"):
            m[k] = m[k] or c[k]
        if m["method"] == "unknown":
            m["method"] = c["method"]

    # 3) Canvas 과제와 연결·중복 판단, 제목 정하기
    items: list[dict] = []
    for c in merged.values():
        label = c["hint"] or c["source_title"]
        same_course = [it for it in canvas if it["course_id"] == c["course_id"]]
        linked = by_canvas_id.get(c["own"]) if c["own"] else None
        if linked and linked["due"]:
            linked = None
        if not linked:
            linked = next((it for it in same_course if not it["due"] and label and same_task(label, it["_name"])), None)
        dup = next((it for it in same_course if it["due"] and label and same_task(label, it["_name"])
                    and parse_iso(it["due"]).date() == c["due"].date()), None)
        if linked:
            title = linked["_name"]
        elif c["hint"] and (any(ch.isdigit() for ch in c["hint"]) or c["kind"] == "시험"):
            title = c["hint"]
        else:
            title = c["source_title"] or c["hint"] or "마감"
        notes = c["notes"]
        if linked:
            notes = notes + [f"Canvas 과제 '{linked['_name']}'(마감 없음)의 마감으로 보입니다"]
        if dup:
            notes = notes + [f"Canvas 과제 '{dup['_name']}'와 같은 마감입니다"]
        items.append({"id": "f-" + _hid(c["course_id"] or c["course_name"], iso(c["due"]), title),
                      "origin": "found", "course_id": c["course_id"], "course_name": c["course_name"],
                      "title": title, "due": iso(c["due"]), "kind": c["kind"], "method": c["method"],
                      "email_to": c["email_to"], "confidence": c["confidence"], "notes": notes,
                      "evidence": sorted(c["evidence"], key=lambda e: e["posted_at"] or ""), "url": None,
                      "canvas_submitted": None, "duplicate_of": dup["id"] if dup else None,
                      "linked_to": linked["id"] if linked else None, "conflict": None,
                      "default_decision": "pending", "_label": linked["_name"] if linked else label})

    # 4) 같은 과제에 서로 다른 마감이 적혀 있으면 양쪽에 알린다(변경·연장 공지일 수 있다)
    for a in items:
        others = [b for b in items if b is not a and b["course_id"] == a["course_id"] and b["due"] != a["due"]
                  and not b["duplicate_of"] and ((a["linked_to"] and a["linked_to"] == b["linked_to"])
                                                 or (a["_label"] and b["_label"] and same_task(a["_label"], b["_label"])))]
        if not others or a["duplicate_of"]:
            continue
        mine = max(e["posted_at"] or "" for e in a["evidence"])
        parts = []
        for b in others:
            theirs = max(e["posted_at"] or "" for e in b["evidence"])
            when = "더 최근 글" if theirs > mine else "이전 글"
            src = b["evidence"][-1]
            parts.append(f"{when}('{src['source_title']}')에는 {_fmt(parse_iso(b['due']))}로 적혀 있습니다")
        a["conflict"] = "같은 과제에 다른 마감이 있습니다: " + "; ".join(parts)

    # 5) 마감도 없고 근거도 못 찾은 Canvas 과제만 '마감 없음'으로 남긴다
    linked_ids = {it["linked_to"] for it in items if it["linked_to"]}
    out = [it for it in canvas if it["due"] or it["id"] not in linked_ids] + items
    for it in out:
        it.pop("_name", None)
        it.pop("_label", None)

    found = [it for it in out if it["origin"] == "found"]
    report = {
        "finished_at": iso(now_local()),
        "courses": len((cache or {}).get("courses", [])),
        "assignments": len((cache or {}).get("assignments", [])),
        "announcements": len((cache or {}).get("announcements", [])),
        "messages": sum(len(cv.get("messages") or []) for cv in (cache or {}).get("conversations", [])),
        "syllabi": sum(1 for c in (cache or {}).get("courses", []) if c.get("syllabus_body")),
        "manual": len(manual),
        "candidates": len(found),
        "needs_review": sum(1 for it in found if not it["duplicate_of"]),
        "no_deadline": sum(1 for it in out if it["origin"] == "canvas" and not it["due"]),
        "errors": list((cache or {}).get("errors", [])),
        # 이번 실행에서 실제로 읽은 곳(탐색 목표)과 건너뛴 곳
        "checked": len(targets), "skipped": len(sources) - len(targets), "targets": targets[:50],
        "requests": ((cache or {}).get("probe") or {}).get("requests"),
        "conversations_fetched": len(((cache or {}).get("probe") or {}).get("conversations_fetched", [])),
    }
    return out, report, new_extracted


# ================================================================ 한 번에

def _found_to(f) -> dict:
    return {"due": iso(f.due), "sentence": f.sentence, "title_hint": f.title_hint, "kind": f.kind,
            "method": f.method, "email_to": f.email_to, "confidence": f.confidence, "notes": list(f.notes)}


def _found_from(d: dict) -> Found:
    return Found(parse_iso(d["due"]), d["sentence"], d.get("title_hint"), d.get("kind") or "과제",
                 d.get("method") or "unknown", d.get("email_to"), d.get("confidence") or "low",
                 list(d.get("notes") or []))


MAX_RUNS = 20
RUN_KEYS = ("finished_at", "mode", "reason", "checked", "skipped", "requests", "conversations_fetched",
            "candidates", "errors")


def rebuild(store, mode: str = "incremental", reason: str | None = None) -> dict:
    """저장된 원본으로 항목을 다시 만든다. incremental이면 바뀐 글만 읽는다."""
    extracted = None if mode == "full" else store.data.get("extracted")
    items, report, new_extracted = build_items(
        store.data.get("cache"), store.data.get("manual", []),
        lookback_days=store.data["settings"].get("lookback_days", 120),
        extracted=extracted, reason_all=reason)
    seen = store.data.setdefault("seen", {})
    stamp = iso(now_local())
    new_ids = [it["id"] for it in items if it["id"] not in seen]
    for it in items:
        seen.setdefault(it["id"], stamp)  # 처음 발견한 실행 시각
    report.update(mode=mode, reason=reason, new_items=new_ids)
    store.data.update(items=items, report=report, extracted=new_extracted)
    run = {k: report.get(k) for k in RUN_KEYS}
    run.update(targets=report["targets"][:10], new_items=len(new_ids))
    store.data["runs"] = (store.data.get("runs") or [])[-(MAX_RUNS - 1):] + [run]
    return report


def sync(canvas: Canvas, store, full: bool = False) -> dict:
    """켤 때마다·버튼을 누를 때마다 부른다. 처음 실행이나 규칙이 바뀐 뒤, 또는 full이면 전부 읽는다."""
    prev = store.data.get("cache")
    if prev is None:
        mode, reason = "full", "처음 실행"
    elif store.data.get("rules_version") != RULES_VERSION:
        mode, reason = "full", "규칙 업데이트"
    elif full:
        mode, reason = "full", "전체 다시 확인"
    else:
        mode, reason = "incremental", None
    cache = collect(canvas, lookback_days=store.data["settings"].get("lookback_days", 120),
                    prev=prev, full=(mode == "full"))
    store.data["cache"] = cache
    store.data["last_sync"] = cache["fetched_at"]
    report = rebuild(store, mode=mode, reason=reason)
    store.data["rules_version"] = RULES_VERSION
    store.save()
    return report


# ================================================================ 학기 (v0.2)

KO_WD = "월화수목금토일"
_TERM_PREFIX = re.compile(r"^\s*\d{2,4}[A-Z]?\s*\([^()]*\)\s*")      # "262R (서울-학부)"
_SECTION = re.compile(r"\s*-\s*\d+\s*분반\s*$")                       # "-00분반"
_TRAIL_PAREN = re.compile(r"\((?:[^()]|\([^()]*\))*\)\s*$")          # 끝의 괄호 한 덩어리


def short_course_name(name: str | None) -> str:
    """'262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반' → 'ASIC설계(영강)'."""
    if not name:
        return name or ""
    s = _SECTION.sub("", name)
    if _TERM_PREFIX.match(s):
        s = _TERM_PREFIX.sub("", s)
        m = _TRAIL_PAREN.search(s)
        if (m and m.start() > 0 and not re.search(r"[가-힣]", m.group(0))
                and re.search(r"[A-Za-z]{3}", m.group(0))):  # 끝의 영문 병기 괄호만 뗀다("(1)"은 남김)
            s = s[:m.start()]
    return s.strip() or name


def _default_term(today: date) -> tuple[date, date]:
    if today.month <= 8:  # 1~8월은 1학기 기준
        return date(today.year, 3, 2), date(today.year, 6, 20)
    return date(today.year, 9, 1), date(today.year, 12, 20)


def semester_info(cache: dict | None, items: list[dict], today: date | None = None) -> dict:
    """개강·종강일과 주차. Canvas 과목/학기 날짜 → 항목 날짜 → 통상 학기 순으로 정한다."""
    today = today or now_local().date()
    near = lambda d: d and abs((d - today).days) <= 200  # noqa: E731 — 몇 년 전 '상시 과목' 날짜는 무시
    starts, ends = [], []
    for c in (cache or {}).get("courses", []):
        s = parse_iso(c.get("term_start") or c.get("start_at"))
        e = parse_iso(c.get("term_end") or c.get("end_at"))
        if s and near(s.date()):
            starts.append(s.date())
        if e and near(e.date()):
            ends.append(e.date())
    start, end, source = (min(starts) if starts else None), (max(ends) if ends else None), "canvas"
    if not start or not end:
        dues = sorted(d.date() for d in (parse_iso(it.get("due")) for it in items) if d and near(d.date()))
        if dues:
            start, end, source = start or dues[0], end or dues[-1], "items"
    if not start or not end or end <= start:
        start, end = _default_term(today)
        source = "default"
    monday = start - timedelta(days=start.weekday())
    total = min(26, -(-((end - monday).days + 1) // 7))
    weeks = [{"n": i + 1, "start": (monday + timedelta(days=7 * i)).isoformat(),
              "end": (monday + timedelta(days=7 * i + 6)).isoformat()} for i in range(total)]
    if today < monday:
        now_n = 0
    elif today > monday + timedelta(days=7 * total - 1):
        now_n = total + 1
    else:
        now_n = (today - monday).days // 7 + 1
    return {"start": start.isoformat(), "end": end.isoformat(), "source": source,
            "week_now": now_n, "weeks_total": total, "weeks": weeks}


def week_of(sem: dict, due: str | None) -> int | None:
    d = parse_iso(due)
    if not d or not sem["weeks"]:
        return None
    first = date.fromisoformat(sem["weeks"][0]["start"])
    n = (d.date() - first).days // 7 + 1
    return n if 1 <= n <= sem["weeks_total"] else None


def annotate(items: list[dict], sem: dict, cache: dict | None) -> list[dict]:
    shorts = {c["id"]: short_course_name(c["name"]) for c in (cache or {}).get("courses", [])}
    for it in items:
        it["week"] = week_of(sem, it.get("due"))
        it["course_short"] = shorts.get(it.get("course_id")) or short_course_name(it.get("course_name"))
    return items


def course_summaries(cache: dict | None) -> list[dict]:
    out = []
    assignments = (cache or {}).get("assignments", [])
    for c in (cache or {}).get("courses", []):
        mine = [a for a in assignments if a.get("course_id") == c["id"]]
        graded = sum(1 for a in mine if (a.get("submission") or {}).get("score") is not None
                     or (a.get("submission") or {}).get("workflow_state") == "graded")
        out.append({"id": c["id"], "name": c["name"], "short_name": short_course_name(c["name"]),
                    "current_score": c.get("current_score"), "graded": graded, "assignments": len(mine)})
    return out


def weekly_summaries(items: list[dict], sem: dict) -> list[dict]:
    """휴대폰 캘린더용: 일정이 있는 주마다 월요일 08:00 요약 일정 하나."""
    tz = now_local().tzinfo
    out = []
    for w in sem["weeks"]:
        mine = sorted((it for it in items if it.get("week") == w["n"] and it.get("due")), key=lambda it: it["due"])
        if not mine:
            continue
        exams = sum(1 for it in mine if it.get("kind") == "시험")
        title = f"{w['n']}주차: 마감 {len(mine) - exams}" + (f" · 시험 {exams}" if exams else "")
        lines = []
        for it in mine:
            d = parse_iso(it["due"])
            mark = " (제출함)" if it.get("submitted") else ""
            lines.append(f"{d.month}/{d.day}({KO_WD[d.weekday()]}) {d:%H:%M} [{it.get('course_short') or it.get('course_name')}] "
                         f"{it.get('title')}{mark}")
        start = datetime.combine(date.fromisoformat(w["start"]), time(8, 0), tzinfo=tz)
        out.append({"uid": f"week-{w['start']}@lms-agent", "start": start, "end": start + timedelta(minutes=30),
                    "summary": title, "description": "\n".join(lines) + "\n\nLMS Agent 주간 요약"})
    return out


def calendar_payload(store) -> tuple[list[dict], list[dict]]:
    """내보낼 일정: (승인된 마감 + 설정에 따라 Canvas 과제, 주간 요약). .ics와 구글 반영이 함께 쓴다."""
    cache = store.data.get("cache")
    items = store.calendar_items()
    sem = semester_info(cache, store.view())
    annotate(items, sem, cache)
    summaries = weekly_summaries(items, sem) if store.data["settings"].get("weekly_summary", True) else []
    return items, summaries


def calendar_text(store, record: bool = False) -> str:
    """내보낼 일정을 .ics 문자열로. record면 내보낸 기록(근거 사슬)을 남긴다."""
    from .ics import build_ics

    items, summaries = calendar_payload(store)
    if record:
        store.record_export(items)
    return build_ics(items, summaries=summaries)
