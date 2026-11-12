"""동기화 흐름. 역할을 셋으로 나눴다(JEDEC 에이전트 구조를 규칙 기반 코드로 옮긴 것).

- 수집(collect): Canvas에서 근거가 될 글을 모은다. 일부가 실패해도 나머지는 계속한다.
- 정리(build_items): 글마다 마감을 찾고, 같은 과제끼리 묶고, Canvas 과제와 겹치는지·
  서로 다른 마감이 적혀 있는지 표시하고, 마감 근거를 끝내 못 찾은 과제를 골라낸다.
- 출력(ics.build_ics + validate_ics): 승인된 것만 캘린더 파일로 만들고 검사한다.

사람은 화면에서 근거를 보고 승인·제외만 한다.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .canvas import Canvas, CanvasError
from .extract import detect_method, find_deadlines, html_to_text, same_task, title_hint
from .timeutil import iso, now_local, parse_iso

LMS_TYPES = {"online_upload", "online_text_entry", "online_url", "online_quiz",
             "external_tool", "discussion_topic", "media_recording", "student_annotation"}
CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


# ================================================================ 수집

def collect(canvas: Canvas, now: datetime | None = None, lookback_days: int = 120) -> dict:
    now = now or now_local()
    since = now - timedelta(days=lookback_days)
    errors: list[str] = []
    cache = {"fetched_at": iso(now), "courses": [], "assignments": [], "announcements": [],
             "conversations": [], "errors": errors}

    courses = canvas.courses()  # 과목 목록이 안 되면 아무것도 못 하므로 여기서는 오류를 올려보낸다
    cache["courses"] = [{"id": str(c["id"]), "name": c.get("name") or c.get("course_code") or str(c["id"]),
                         "course_code": c.get("course_code"), "syllabus_body": c.get("syllabus_body"),
                         "start_at": c.get("start_at")} for c in courses]
    for c in cache["courses"]:
        try:
            for a in canvas.assignments(c["id"]):
                a = dict(a)
                a["course_id"] = c["id"]
                cache["assignments"].append(a)
        except CanvasError as e:
            errors.append(f"'{c['name']}' 과제를 읽지 못했습니다: {e}")
    try:
        cache["announcements"] = canvas.announcements([c["id"] for c in cache["courses"]], since, now + timedelta(days=1))
    except CanvasError as e:
        errors.append(f"공지를 읽지 못했습니다: {e}")
    try:
        cache["conversations"] = canvas.conversations(since)
    except CanvasError as e:
        errors.append(f"메시지함을 읽지 못했습니다: {e}")
    return cache


# ================================================================ 정리

def _sources(cache: dict, manual: list[dict], now: datetime) -> list[dict]:
    """마감을 찾을 글 목록. 각 글: course_id, course_name, type, id, title, text, is_html, posted_at, url."""
    names = {c["id"]: c["name"] for c in cache.get("courses", [])} if cache else {}
    out: list[dict] = []
    if cache:
        for c in cache["courses"]:
            if c.get("syllabus_body"):
                out.append({"course_id": c["id"], "type": "syllabus", "id": c["id"], "title": "강의계획서",
                            "text": c["syllabus_body"], "is_html": True,
                            "posted_at": parse_iso(c.get("start_at")) or now, "url": None})
        for a in cache["assignments"]:
            if a.get("description"):
                out.append({"course_id": a["course_id"], "type": "assignment", "id": str(a["id"]),
                            "title": a.get("name"), "text": a["description"], "is_html": True,
                            "posted_at": parse_iso(a.get("created_at")) or parse_iso(a.get("updated_at")) or now,
                            "url": a.get("html_url")})
        for n in cache["announcements"]:
            cid = str(n.get("context_code", "")).replace("course_", "")
            out.append({"course_id": cid, "type": "announcement", "id": str(n["id"]), "title": n.get("title"),
                        "text": n.get("message") or "", "is_html": True,
                        "posted_at": parse_iso(n.get("posted_at")) or now, "url": n.get("html_url")})
        for cv in cache["conversations"]:
            code = str(cv.get("context_code") or "")
            cid = code.replace("course_", "") if code.startswith("course_") else None
            for m in (cv.get("messages") or [])[:10]:
                out.append({"course_id": cid if cid in names else None,
                            "course_name": cv.get("context_name"), "type": "inbox",
                            "id": f"{cv['id']}-{m.get('id')}", "title": cv.get("subject") or "메시지",
                            "text": m.get("body") or "", "is_html": False,
                            "posted_at": parse_iso(m.get("created_at")) or now, "url": None})
    for m in manual:
        cid = next((k for k, v in names.items() if v == m["course_name"]), None)
        out.append({"course_id": cid, "course_name": m["course_name"], "type": "manual", "id": m["id"],
                    "title": "직접 입력", "text": m["text"], "is_html": False,
                    "posted_at": parse_iso(m.get("posted_at")) or now, "url": None})
    for s in out:
        s.setdefault("course_name", None)
        s["course_name"] = names.get(s["course_id"]) or s["course_name"] or "과목 미상"
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
                lookback_days: int = 120) -> tuple[list[dict], dict]:
    now = now or now_local()
    oldest = now - timedelta(days=lookback_days)
    names = {c["id"]: c["name"] for c in (cache or {}).get("courses", [])}
    canvas = [_canvas_item(a, names.get(a["course_id"], "과목 미상")) for a in (cache or {}).get("assignments", [])]
    by_canvas_id = {it["id"]: it for it in canvas}
    sources = _sources(cache, manual, now)

    # 1) 글마다 마감 찾기
    cands: list[dict] = []
    for s in sources:
        for f in find_deadlines(s["text"], s["posted_at"], is_html=s["is_html"]):
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
    }
    return out, report


# ================================================================ 한 번에

def rebuild(store) -> dict:
    items, report = build_items(store.data.get("cache"), store.data.get("manual", []),
                                lookback_days=store.data["settings"].get("lookback_days", 120))
    store.data["items"] = items
    store.data["report"] = report
    return report


def sync(canvas: Canvas, store) -> dict:
    cache = collect(canvas, lookback_days=store.data["settings"].get("lookback_days", 120))
    store.data["cache"] = cache
    store.data["last_sync"] = cache["fetched_at"]
    report = rebuild(store)
    store.save()
    return report
