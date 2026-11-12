"""과목 종류: 성적 있음(normal) / P/NP(pass_fail) / 제외(hidden) — 계약 15장. 과목별 읽은 글 수(read_counts).

- 저장: store.data["course_prefs"][cid] = {"mode", "at"}. 읽기는 store.course_mode / store.hidden_courses(모든 모듈이 같은 규칙).
- 제안(근거가 있을 때만, 사용자가 [맞아요]로 정함 — 저절로 바꾸지 않음):
  hidden — 과목 코드·이름에 학기 표시("262R (서울-학부)")가 없고, "[학생]" 같은 대괄호 머리가 있거나 '교육'·'training'으로
  끝나며, 공지·게시판·강의계획서가 하나도 없을 때.
  pass_fail — 강의계획서·공지·게시판·PDF의 한 줄에 "P/NP"·"Pass/Fail"·"Pass/Non-pass"·"패스/논패스"·"합격/불합격"·"S/U"와
  성적·평가 낱말이 함께 있을 때(근거 줄 인용). 시험 결과의 'PASS/FAIL' 같은 말에 끌려가지 않게 평가 낱말을 함께 본다.
사용자가 한 번 정하면(normal 포함) 다시 제안하지 않는다.
"""
from __future__ import annotations

import re

from .core import _SECTION, _TERM_PREFIX
from .extract import html_to_text
from .store import COURSE_MODES, course_mode
from .timeutil import iso, now_local

_BRACKET_HEAD = re.compile(r"^\s*\[[^\]]+\]")
_TRAINING_END = re.compile(r"(?:교육|training|orientation|오리엔테이션)\s*$", re.I)
_PNP = re.compile(r"(?<![A-Za-z])P\s*/\s*N\s*P(?![A-Za-z])|pass\s*/\s*(?:fail|non[\s\-]?pass)(?![a-z])|"
                  r"pass\s*[-/]?\s*no\s*pass|패스\s*/\s*논\s*패스|합격\s*/\s*불합격|(?<![A-Za-z/])S\s*/\s*U(?![A-Za-z/])", re.I)
_GRADING_CTX = re.compile(r"성적|평가|학점|이수|grading|graded|grade|basis|평점", re.I)
_SOURCE_KO = {"syllabus": "강의계획서", "announcement": "공지", "board": "게시판", "pdf": "첨부 PDF"}

HIDDEN_REASON = "정규 수업이 아닌 것 같아요(과목 코드에 학기 표시가 없고 공지·게시판이 없음)"


def set_mode(store, course_id, mode) -> None:
    """POST /api/course/<cid>/mode {"mode"}. 모르는 과목·값이면 ValueError."""
    cid = str(course_id)
    if mode not in COURSE_MODES:
        raise ValueError("mode는 normal, pass_fail, hidden 중 하나여야 합니다")
    courses = {str(c.get("id")) for c in (store.data.get("cache") or {}).get("courses") or []}
    if courses and cid not in courses:
        raise ValueError("그 과목을 찾을 수 없습니다. 동기화 후 다시 시도하세요")
    store.data.setdefault("course_prefs", {})[cid] = {"mode": mode, "at": iso(now_local())}


def read_counts(cache: dict | None) -> dict:
    """과목마다 마지막 동기화에서 읽은 것: {cid: {"announcements", "board_posts", "messages", "pdfs", "attachments"}}.
    board_posts는 LearningX 게시판 글만(공지 첨부를 모은 가상 게시판 '공지'는 공지로 이미 셈), attachments는 공지 첨부도 포함."""
    cache = cache or {}
    out = {str(c["id"]): {"announcements": 0, "board_posts": 0, "messages": 0, "pdfs": 0, "attachments": 0}
           for c in cache.get("courses") or []}
    for n in cache.get("announcements") or []:
        cid = str(n.get("context_code", "")).replace("course_", "")
        if cid in out:
            out[cid]["announcements"] += 1
    for b in cache.get("boards") or []:
        cid = str(b.get("course_id"))
        if cid not in out:
            continue
        for p in b.get("posts") or []:
            if b.get("source") != "canvas":
                out[cid]["board_posts"] += 1
            out[cid]["attachments"] += len(p.get("attachments") or [])
    for cv in cache.get("conversations") or []:
        code = str(cv.get("context_code") or "")
        cid = code.replace("course_", "") if code.startswith("course_") else None
        if cid in out:
            out[cid]["messages"] += len(cv.get("messages") or [])
    for t in (cache.get("pdf_texts") or {}).values():
        cid = str((t or {}).get("course_id"))
        if cid in out and (t or {}).get("text"):
            out[cid]["pdfs"] += 1
    return out


def _has_term(c: dict) -> bool:
    return any(_TERM_PREFIX.match(str(x or "")) for x in (c.get("name"), c.get("course_code")))


def _hidden_suggest(c: dict, counts: dict) -> dict | None:
    name = str(c.get("name") or "")
    base = _SECTION.sub("", name).strip()
    looks = bool(_BRACKET_HEAD.match(name) or _TRAINING_END.search(base))
    posts = counts.get("announcements", 0) + counts.get("board_posts", 0) + (1 if c.get("syllabus_body") else 0)
    if _has_term(c) or not looks or posts:
        return None
    return {"mode": "hidden", "reason": HIDDEN_REASON, "evidence": name}


def _texts_of(cache: dict, cid: str):
    for c in cache.get("courses") or []:
        if str(c.get("id")) == cid and c.get("syllabus_body"):
            yield "syllabus", "강의계획서", html_to_text(c["syllabus_body"])
    for n in cache.get("announcements") or []:
        if str(n.get("context_code", "")).replace("course_", "") == cid:
            yield "announcement", n.get("title") or "공지", html_to_text(n.get("message"))
    for b in cache.get("boards") or []:
        if str(b.get("course_id")) == cid and b.get("source") != "canvas":
            for p in b.get("posts") or []:
                yield "board", f"{b.get('board_title')}: {p.get('title')}", html_to_text(p.get("content"))
    for t in (cache.get("pdf_texts") or {}).values():
        if isinstance(t, dict) and str(t.get("course_id")) == cid and t.get("text"):
            yield "pdf", f"첨부 '{t.get('filename')}'", t["text"]


def _pass_fail_suggest(cache: dict, cid: str) -> dict | None:
    for typ, title, text in _texts_of(cache, cid):
        for m in _PNP.finditer(text or ""):
            s = text.rfind("\n", 0, m.start()) + 1
            e = text.find("\n", m.end())
            line = re.sub(r"\s+", " ", text[s:e if e >= 0 else len(text)]).strip()
            if not _GRADING_CTX.search(line):
                continue
            where = _SOURCE_KO.get(typ, "글")
            label = where if typ == "syllabus" else f"{where} '{title}'"
            return {"mode": "pass_fail", "reason": f"{label}에 P/NP(패스/논패스) 평가라고 적혀 있어요",
                    "evidence": line[:200]}
    return None


_MEMO: dict = {}


def suggestions(cache: dict | None, counts: dict | None = None) -> dict:
    """과목마다 제안 {cid: {"mode", "reason", "evidence"}} (근거가 있는 것만). 같은 cache면 한 번만 계산한다."""
    cache = cache or {}
    # 같은 객체(참조를 쥐고 있어 id가 다른 cache에 다시 쓰이지 않는다) + 내용 크기가 같을 때만 기억한 값을 쓴다
    sig = (cache.get("fetched_at"), len(cache.get("courses") or []), len(cache.get("announcements") or []),
           len(cache.get("boards") or []), len(cache.get("pdf_texts") or {}))
    if _MEMO.get("obj") is cache and _MEMO.get("sig") == sig:
        return _MEMO["value"]
    counts = counts if counts is not None else read_counts(cache)
    out = {}
    for c in cache.get("courses") or []:
        cid = str(c["id"])
        s = _hidden_suggest(c, counts.get(cid) or {}) or _pass_fail_suggest(cache, cid)
        if s:
            out[cid] = s
    _MEMO.update(obj=cache, sig=sig, value=out)
    return out


def annotate_courses(courses: list[dict], data: dict) -> list[dict]:
    """state.courses[]에 mode, mode_suggest(사용자가 아직 정하지 않았고 지금과 다를 때만), read_counts를 붙인다."""
    cache = data.get("cache") or {}
    counts = read_counts(cache)
    sugg = suggestions(cache, counts)
    prefs = data.get("course_prefs") or {}
    for c in courses:
        cid = str(c["id"])
        c["mode"] = course_mode(data, cid)
        s = sugg.get(cid)
        c["mode_suggest"] = dict(s) if s and cid not in prefs and s["mode"] != c["mode"] else None
        c["read_counts"] = counts.get(cid) or {"announcements": 0, "board_posts": 0, "messages": 0, "pdfs": 0,
                                               "attachments": 0}
    return courses
