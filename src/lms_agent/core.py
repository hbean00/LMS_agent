"""동기화 흐름. 역할을 셋으로 나눴다(JEDEC 에이전트 구조를 규칙 기반 코드로 옮긴 것).

- 수집(collect): Canvas에서 근거가 될 글을 모은다. 일부가 실패해도 나머지는 계속한다.
- 정리(build_items): 글마다 마감을 찾고, 같은 과제끼리 묶고, Canvas 과제와 겹치는지·
  서로 다른 마감이 적혀 있는지 표시하고, 마감 근거를 끝내 못 찾은 과제를 골라낸다.
- 출력(ics.build_ics + validate_ics): 승인된 것만 캘린더 파일로 만들고 검사한다.

사람은 화면에서 근거를 보고 승인·제외만 한다.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path

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
    cache["boards"], cache["lx_tools"], lx_requests, posts_fetched = _collect_boards(canvas, cache["courses"], prev,
                                                                                  full, errors)
    cache["boards"] += _announcement_files(canvas, cache["announcements"], prev, errors)
    cache["pdf_texts"] = dict((prev or {}).get("pdf_texts") or {})  # sync가 다운로드 뒤 새 글자를 더한다
    cache["probe"] = {"requests": canvas.request_count - start_requests + lx_requests,
                      "conversations_fetched": fetched, "conversations_reused": reused,
                      "board_posts_fetched": posts_fetched}
    return cache


_FILE_LINK = re.compile(r"/(?:api/v1/)?(?:courses/\d+/)?files/(\d+)")


def _announcement_files(canvas: Canvas, announcements: list[dict], prev: dict | None, errors: list[str]) -> list[dict]:
    """Canvas 공지 본문에 링크된 파일(예: 'Lab0_score-1.pdf', 'Update Grading Policy.pdf')을 첨부처럼 모은다.
    다운로드(materials)가 같은 모양으로 처리하도록 과목마다 가상 게시판 '공지'(source="canvas")로 묶는다.
    공지 본문은 announcement 출처가 이미 읽으므로 board 출처에서는 건너뛴다. 새 파일만 파일 API로 이름·크기를 묻는다."""
    known = {a.get("canvas_file_id"): a for b in (prev or {}).get("boards", []) if b.get("source") == "canvas"
             for p in b.get("posts", []) for a in p.get("attachments", [])}
    by_course: dict[str, list[dict]] = {}
    for n in announcements:
        ids = list(dict.fromkeys(int(m.group(1)) for m in _FILE_LINK.finditer(str(n.get("message") or ""))))
        if not ids:
            continue
        attachments = []
        for fid in ids:
            if fid in known:
                attachments.append(known[fid])
                continue
            try:
                info = canvas._get_one(f"/api/v1/files/{fid}")
            except CanvasError as e:
                errors.append(f"공지 '{n.get('title')}'의 첨부를 확인하지 못했습니다: {e}")
                continue
            attachments.append({"id": f"cf{fid}", "filename": info.get("display_name") or info.get("filename"),
                                "filesize": info.get("size"), "canvas_file_id": fid, "url": info.get("url")})
        if attachments:
            cid = str(n.get("context_code", "")).replace("course_", "")
            by_course.setdefault(cid, []).append({
                "id": n["id"], "title": n.get("title") or "공지", "content": "", "created_at": n.get("posted_at"),
                "updated_at": n.get("posted_at"), "post_url": n.get("html_url"), "attachments": attachments})
    return [{"course_id": cid, "tool_id": None, "board_id": "announcements", "board_title": "공지",
             "source": "canvas", "posts": posts} for cid, posts in by_course.items()]


def _collect_boards(canvas: Canvas, courses: list[dict], prev: dict | None, full: bool, errors: list[str]):
    """LearningX 게시판(계약 10장). 글 목록에는 본문이 같이 오므로 매번 받고, 첨부 상세는
    새 글·바뀐 글에만 요청한다. 게시판 도구가 없는 과목은 기억해 두고 '전체 다시 확인' 때만 다시 본다."""
    from .learningx import LearningX, LearningXError

    prev = prev or {}
    prev_boards = {(b["course_id"], b["board_id"]): b for b in prev.get("boards", [])}
    known_tools = {} if full else dict(prev.get("lx_tools") or {})
    out, tools, requests, fetched = [], {}, 0, 0
    for c in courses:
        cid = c["id"]
        if cid in known_tools:
            tool_id = known_tools[cid]
        else:
            tool = LearningX.board_tool(canvas, cid)
            tool_id = tool["id"] if tool else None
        tools[cid] = tool_id
        if tool_id is None:
            continue
        lx = LearningX(canvas, cid)
        try:
            lx.launch(tool_id)
            for b in lx.boards():
                old_posts = {p["id"]: p for p in (prev_boards.get((cid, b["id"])) or {}).get("posts", [])}
                posts = []
                for p in lx.posts(b["id"], max_pages=3):
                    o = old_posts.get(p.get("id"))
                    same = (o and not full and o.get("updated_at") == p.get("updated_at")
                            and o.get("content") == p.get("content"))
                    if same:
                        attachments, post_url = o.get("attachments") or [], o.get("post_url")
                    elif p.get("attachment_count"):
                        d = lx.post(b["id"], p["id"])
                        fetched += 1
                        attachments = [{k: a.get(k) for k in ("id", "filename", "filesize", "canvas_file_id", "url")}
                                       for a in d.get("attachments") or []]
                        post_url = d.get("post_url")
                    else:
                        attachments, post_url = [], p.get("post_url")
                    if post_url and str(post_url).startswith("/"):
                        post_url = canvas.base_url + post_url
                    posts.append({"id": p.get("id"), "title": p.get("title") or "제목 없음",
                                  "content": p.get("content") or "", "created_at": p.get("created_at"),
                                  "updated_at": p.get("updated_at"), "post_url": post_url,
                                  "attachments": attachments})
                out.append({"course_id": cid, "tool_id": tool_id, "board_id": b["id"],
                            "board_title": b.get("title") or "게시판", "posts": posts})
        except LearningXError as e:
            errors.append(f"'{c['name']}' LearningX 게시판을 읽지 못해 지난번 내용을 씁니다: {e}")
            out += [b for b in prev.get("boards", []) if b["course_id"] == cid]
        requests += lx.requests
    return out, tools, requests, fetched


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
def board_source(cache, manual, now):
    """LearningX 게시판 글(과제 안내·강의자료실 등). 제목은 '<게시판>: <글 제목>'."""
    return [_text(b["course_id"], "board", f"{b['board_id']}-{p['id']}", f"{b['board_title']}: {p['title']}",
                  p.get("content"), True, p.get("created_at"), now, p.get("post_url"))
            for b in cache.get("boards", []) if b.get("source") != "canvas" for p in b.get("posts", [])]


@source
def pdf_source(cache, manual, now):
    """내려받은 첨부 PDF에서 뽑은 글자(materials가 채운 pdf_texts)."""
    return [_text(t.get("course_id"), "pdf", key, f"첨부 '{t.get('filename')}'", t.get("text"), False,
                  t.get("posted_at"), now)
            for key, t in (cache.get("pdf_texts") or {}).items() if t.get("text")]


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
            "course_name": course_name, "title": " ".join((a.get("name") or "").split()) or "과제", "due": iso(due),
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
            founds = find_deadlines(s["text"], s["posted_at"], is_html=s["is_html"], title=s.get("title"))
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
                          "source_title": s["title"] if s["type"] in ("announcement", "inbox", "board") else None,
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
        elif c["hint"] and (any(ch.isdigit() for ch in c["hint"]) or c["kind"] == "시험"
                            or " " in c["hint"]):   # 'Midterm Project'처럼 과제 하나를 가리키는 이름
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
            "candidates", "errors", "by")


def rebuild(store, mode: str = "incremental", reason: str | None = None, by: str = "app") -> dict:
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
    report.update(mode=mode, reason=reason, new_items=new_ids, by=by)
    store.data.update(items=items, report=report, extracted=new_extracted)
    run = {k: report.get(k) for k in RUN_KEYS}
    run.update(targets=report["targets"][:10], new_items=len(new_ids))
    store.data["runs"] = (store.data.get("runs") or [])[-(MAX_RUNS - 1):] + [run]
    return report


def sync_options(store, demo_home: Path | None = None) -> dict:
    """앱·CLI·백그라운드가 같은 방식으로 sync에 넘길 선택 사항(다운로드 폴더, PDF 읽기, AI CLI).
    다운로드 폴더 우선순위: 환경 변수 LMS_AGENT_DOWNLOADS > 설정 download_dir > (데모) <home>/downloads > 문서/LMS Agent."""
    settings = store.data["settings"]
    opts: dict = {"ai_enabled": bool(settings.get("ai_cli"))}
    if settings.get("auto_download", True):
        folder = os.environ.get("LMS_AGENT_DOWNLOADS") or settings.get("download_dir")
        if folder:
            opts["downloads_to"] = Path(folder)
        elif demo_home is not None:
            opts["downloads_to"] = Path(demo_home) / "downloads"
        else:
            try:
                from .materials import default_folder
                opts["downloads_to"] = default_folder()
            except ImportError:
                pass
    try:
        from .pdftext import extract_text
        def read_pdf(data: bytes) -> dict:  # materials.fetch가 받는 모양({text, notes, ok})으로
            r = extract_text(data)
            return {"text": r.text, "notes": list(r.notes), "ok": r.ok}
        opts["pdf_reader"] = read_pdf
    except ImportError:
        pass
    return opts


def _attachment_bytes(canvas: Canvas, att: dict, max_bytes: int = 50_000_000) -> bytes:
    """첨부 받기: Canvas 파일 주소(확인값 포함)를 인증 헤더 없이 → 안 되면 Canvas 파일 API의 주소."""
    from .canvas import origin

    def get(url: str) -> bytes:
        if url.startswith("/"):
            url = canvas.base_url + url
        if origin(url) != origin(canvas.base_url):
            raise ValueError("첨부 주소가 Canvas와 다른 서버입니다")
        req = urllib.request.Request(url, headers={"User-Agent": "lms-agent"})  # 인증 헤더 없음
        from . import net
        with net.urlopen(req, timeout=60) as r:
            data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("파일이 너무 큽니다")
        return data

    try:
        return get(str(att.get("url") or ""))
    except (OSError, ValueError, urllib.error.URLError):
        if not att.get("canvas_file_id"):
            raise
    info = canvas._get_one(f"/api/v1/files/{att['canvas_file_id']}")
    return get(str(info.get("url") or ""))


def _download_new(canvas: Canvas, cache: dict, store, folder: Path, pdf_reader) -> tuple[dict, dict, list[str]]:
    """새 첨부만 내려받고 PDF는 글자를 뽑는다(materials 모듈). 잠금 없이(네트워크) 실행한다."""
    try:
        from .materials import fetch, plan
    except ImportError:
        return {}, {}, []
    from .store import hidden_courses
    shorts = {c["id"]: short_course_name(c["name"]) for c in cache.get("courses", [])}
    hidden = hidden_courses(store.data)   # 제외한 과목은 새로 받지 않는다(받은 파일은 둠, 계약 15장)
    jobs = [j for j in plan(cache.get("boards", []), dict(store.data.get("downloads") or {}), shorts, Path(folder))
            if str(j.get("course_id")) not in hidden]
    if not jobs:
        return {}, {}, []
    return fetch(jobs, lambda att: _attachment_bytes(canvas, att), pdf_reader=pdf_reader)


def _auto_weights(store, ai_enabled: bool, session=None) -> list[str]:
    """평가 비중 자동 찾기(gradebook.auto_fill). AI CLI는 오래 걸릴 수 있어 잠금 밖에서 부른다."""
    try:
        from .gradebook import auto_fill
    except ImportError:
        return []
    srcs = _sources(store.data.get("cache"), store.data.get("manual", []), now_local())
    return auto_fill(store, srcs, ai_enabled, session=session) or []


def _auto_dist(store, ai_enabled: bool, session=None) -> list[str]:
    """학점 비율·점수 기준 자동 찾기(gradebook.auto_dist, 계약 16장) — 평가 비중 다음, 같은 AiSession으로."""
    try:
        from .gradebook import auto_dist
    except ImportError:
        return []
    srcs = _sources(store.data.get("cache"), store.data.get("manual", []), now_local())
    return auto_dist(store, srcs, ai_enabled, session=session) or []


# 자동 단계(평가 비중·학점 비율·AI 정리·평가 항목 연결/개수)가 바꾸는 칸: id → 값
_AI_KEYS = ("user", "grades", "weights_auto", "grade_links_ai", "grade_counts_ai", "dist_ai")


def _after_sync(store, ai_enabled: bool, ai_runner=None, ai_command=None, still_valid=None) -> dict:
    """동기화 뒤 자동 단계: 평가 비중 찾기, 학점 비율 찾기, (켜져 있으면) AI 평가 항목 연결·개수, AI 확인함 정리.
    한 번의 AiSession을 나눠 써서 호출 한도가 함께 적용된다.
    AI는 몇십 초 걸릴 수 있어 잠금 밖에서 돌리고, 저장할 때 파일을 다시 읽어 **그 사이 사용자가 손대지 않은 칸만** 덮는다
    (백그라운드 동기화가 앱에서 방금 한 사용자 결정을 지우지 않게)."""
    out: dict = {}
    before = {k: copy.deepcopy(store.data.get(k) or {}) for k in _AI_KEYS}
    runs_before = list(store.data.get("ai_runs") or [])
    session = None
    if ai_enabled:
        try:
            from .agent_cli import AiSession
            session = AiSession(store, runner=ai_runner, command=ai_command)
        except ImportError:
            session = None
    logs = _auto_weights(store, ai_enabled, session)
    if logs:
        out["auto_weights"] = logs
    logs = _auto_dist(store, ai_enabled, session)
    if logs:
        out["auto_dist"] = logs
    if session is not None:   # 규칙이 못 정한 점수·마감의 평가 항목, 명시·단서 없는 개수(계약 14-2·14-4)
        from .gradebook import ai_links
        r = ai_links(store, session)
        if r.get("asked") or r.get("counted") or r.get("error"):
            out["ai_grade_links"] = r
    triage = None
    if session is not None and store.data["settings"].get("ai_auto_triage"):
        from .agent_cli import auto_triage
        triage = auto_triage(store, session=session)
        out["ai_triage"] = {k: triage.get(k) for k in ("ok", "applied", "error", "pending_left")}
    changed = {}
    for k in _AI_KEYS:
        now_k = store.data.get(k) or {}
        diff = {i: copy.deepcopy(v) for i, v in now_k.items() if before[k].get(i) != v}
        diff.update({i: None for i in before[k] if i not in now_k})  # None = 지워짐
        if diff:
            changed[k] = diff
    new_runs = [r for r in store.data.get("ai_runs") or [] if r not in runs_before]
    if not changed and not new_runs and triage is None:
        return out
    with store.locked():
        reloaded = store.reload_if_changed()
        if still_valid is not None and not still_valid():
            if not reloaded:
                store._load()  # 메모리의 자동 변경을 버리고 로그아웃이 남긴 상태로
            return {}
        if reloaded:  # 다른 프로세스가 그 사이 저장했다 → 손대지 않은 칸에만 자동 결과를 다시 얹는다
            skipped = 0
            for k, diff in changed.items():
                disk = store.data.setdefault(k, {})
                for i, v in diff.items():
                    if disk.get(i) != before[k].get(i):
                        skipped += 1  # 사용자가 먼저 바꿨다: 사용자 쪽이 이긴다
                        continue
                    if v is None:
                        disk.pop(i, None)
                    else:
                        disk[i] = v
            if skipped:
                out["ai_skipped_user_changed"] = skipped
            runs = store.data.setdefault("ai_runs", [])
            runs.extend(r for r in new_runs if r not in runs)
            from .agent_cli import RUN_LOG_KEEP
            del runs[:-RUN_LOG_KEEP]
            if triage is not None:
                store.data["ai_triage"] = triage
        store.write()
    return out


class SyncCancelled(Exception):
    """동기화 도중 로그아웃(또는 다른 계정으로 로그인)됐다. 읽은 것은 저장하지 않는다."""


def sync(canvas: Canvas, store, full: bool = False, by: str = "app", downloads_to: Path | None = None,
         pdf_reader=None, ai_enabled: bool = False, still_valid=None, ai_runner=None, ai_command=None) -> dict:
    """켤 때마다·버튼을 누를 때마다·백그라운드(by="background")로 부른다.
    처음 실행이나 규칙이 바뀐 뒤, 또는 full이면 전부 읽는다.
    Canvas 읽기·첨부 다운로드(몇 초~)는 잠금 없이 하고, 저장 직전에 잠금을 잡고 파일을 다시 읽어 그 사이의
    사용자 결정(다른 프로세스가 저장한 것) 위에 합친다. downloads_to가 없으면(테스트 기본) 내려받지 않는다.
    still_valid(): 저장 직전(잠금 안)에 부른다. False면 그 사이 로그아웃된 것 — 지운 기록을 되살리지 않게
    아무것도 저장하지 않고 SyncCancelled를 낸다(내려받은 파일은 문서 폴더에 남는다)."""
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
    downloaded, texts, dl_errors = ({}, {}, [])
    if downloads_to is not None:
        downloaded, texts, dl_errors = _download_new(canvas, cache, store, downloads_to, pdf_reader)
    with store.locked():
        store.reload_if_changed()
        if still_valid is not None and not still_valid():
            raise SyncCancelled("동기화하는 동안 로그아웃되어 저장하지 않았습니다")
        if downloads_to is not None:
            from .materials import apply
            apply(store.data, downloaded, texts, dl_errors)  # 다시 받은 PDF의 옛 글자 정리까지
        cache["pdf_texts"] = dict(store.data.get("pdf_texts") or {})
        cache["errors"] += dl_errors
        store.data["cache"] = cache
        store.data["last_sync"] = cache["fetched_at"]
        report = rebuild(store, mode=mode, reason=reason, by=by)
        report["downloaded"] = len(downloaded)
        store.data["rules_version"] = RULES_VERSION
        store.write()
    if still_valid is not None and not still_valid():
        return report
    report.update(_after_sync(store, ai_enabled, ai_runner, ai_command, still_valid))
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
    s = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+(?:20\d{2}\s+)?", "", s)  # "[학생] [한국어] 2026 " 같은 앞머리
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


SEMESTER_WEEKS = 16
MAX_TERM_DAYS = 20 * 7


def _median(days: list[date]) -> date | None:
    if not days:
        return None
    days = sorted(days)
    return days[(len(days) - 1) // 2]


def _standard_start(d: date) -> date | None:
    """한국 대학 표준 개강일(3/2, 9/1) 중 d에서 3주 안에 있는 날."""
    for c in (date(d.year, 3, 2), date(d.year, 9, 1), date(d.year + 1, 3, 2)):
        if abs((c - d).days) <= 21:
            return c
    return None


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
    # 가운데 값: 1년짜리 필수 교육 과목 같은 예외 하나에 끌려가지 않게
    start, end, source = _median(starts), _median(ends), "canvas"
    note = None
    if start and end and (end - start).days > MAX_TERM_DAYS:
        # 학교 Canvas의 '학기'는 성적 처리·방학까지 포함해 길다(예: 8/23~이듬해 2/27) → 수업 16주로 본다
        std = _standard_start(start)
        start = std or start
        end = start - timedelta(days=start.weekday()) + timedelta(weeks=SEMESTER_WEEKS, days=-1)
        note = f"Canvas 학기 기간이 길어 {start.month}/{start.day} 개강, {SEMESTER_WEEKS}주로 계산했습니다"
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
    return {"start": start.isoformat(), "end": end.isoformat(), "source": source, "note": note,
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


def review_reminder(store, now: datetime | None = None) -> list[dict]:
    """확인 대기 후보가 있으면 오늘 09:00에 '[확인] 마감 후보 N개' 일정 하나(설정 review_reminder가 켜졌을 때).
    매일 그날 것 하나만 원하는 일정으로 내므로, 지난날의 알림은 다음 반영 때 구글에서 지워진다."""
    now = now or now_local()
    pending = [v for v in store.view() if v["status"] == "review"
               and (parse_iso(v["due"]) is None or parse_iso(v["due"]) >= now - timedelta(days=1))]
    if not pending:
        return []
    start = datetime.combine(now.date(), time(9, 0), tzinfo=now.tzinfo)
    lines = [f"[{short_course_name(v['course_name'])}] {v['title']}" for v in pending[:10]]
    if len(pending) > 10:
        lines.append(f"외 {len(pending) - 10}개")
    return [{"uid": f"review-{now.date().isoformat()}@lms-agent", "start": start, "end": start + timedelta(minutes=15),
             "summary": f"[확인] 마감 후보 {len(pending)}개 — LMS Agent에서 확인",
             "description": "공지·메시지에서 찾은 마감입니다. 앱의 확인함에서 일정에 넣을지 정하세요.\n\n"
                            + "\n".join(lines)}]


def calendar_payload(store) -> tuple[list[dict], list[dict]]:
    """내보낼 일정: (승인된 마감 + 설정에 따라 Canvas 과제, 주간 요약·확인 알림). .ics와 구글 반영이 함께 쓴다."""
    cache = store.data.get("cache")
    items = store.calendar_items()
    sem = semester_info(cache, store.view())
    annotate(items, sem, cache)
    summaries = weekly_summaries(items, sem) if store.data["settings"].get("weekly_summary", True) else []
    if store.data["settings"].get("review_reminder", False):
        summaries += review_reminder(store)
    return items, summaries


def calendar_text(store, record: bool = False) -> str:
    """내보낼 일정을 .ics 문자열로. record면 내보낸 기록(근거 사슬)을 남긴다."""
    from .ics import build_ics

    items, summaries = calendar_payload(store)
    if record:
        store.record_export(items)
    return build_ics(items, summaries=summaries)
