"""성적: 과목별 평가 항목 설정 저장 · 점수 문장 후보 · 예상 학점 · 남은 시험 필요 점수 · 공부 우선순위.

계산은 grades.py(정규분포 근사, 항목 사이 상관 가정)가 하고, 여기서는 저장된 설정과 Canvas 원본을
화면용 묶음(state["grades"])으로 만든다. 점수 문장은 후보로만 보여 주고, 사용자가 골라 항목에 넣는다.
"""
from __future__ import annotations

import math

from . import grades as g
from .core import short_course_name
from .extract import html_to_text
from .timeutil import iso, parse_iso

MAX_COMPONENTS = 20


def _num(v, name: str, lo: float | None = None, hi: float | None = None):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}은(는) 숫자여야 합니다") from None
    if math.isnan(x) or math.isinf(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError(f"{name} 값이 범위를 벗어났습니다")
    return x


def save_course(store, course_id: str, body: dict) -> None:
    """POST /api/grades/<course_id> 본문을 검사해 저장. 잘못되면 ValueError(한국어)."""
    comps_in = body.get("components") or []
    if not isinstance(comps_in, list) or len(comps_in) > MAX_COMPONENTS:
        raise ValueError(f"평가 항목은 {MAX_COMPONENTS}개 이하 목록이어야 합니다")
    comps = []
    for c in comps_in:
        if not isinstance(c, dict):
            raise ValueError("평가 항목 형식이 올바르지 않습니다")
        name = str(c.get("name") or "").strip()[:40]
        if not name:
            raise ValueError("평가 항목 이름이 비어 있습니다")
        comps.append({"name": name, "weight": _num(c.get("weight"), f"'{name}' 비중", 0, 100) or 0.0,
                      "score": _num(c.get("score"), f"'{name}' 점수", 0),
                      "max_score": _num(c.get("max_score"), f"'{name}' 만점", 0.0001),
                      "mean": _num(c.get("mean"), f"'{name}' 평균", 0),
                      "sd": _num(c.get("sd"), f"'{name}' 표준편차", 0),
                      "fixed": bool(c.get("fixed")), "source": str(c.get("source") or "")[:300] or None})
    if sum(c["weight"] for c in comps) > 100.5:
        raise ValueError("비중 합이 100%를 넘습니다")
    dist = body.get("dist")
    if dist is not None:
        dist = {k: _num(dist.get(k), f"{k} 비율", 0, 100) or 0.0 for k in g.GRADES}
        problems = g.validate_distribution(dist)
        if problems:
            raise ValueError("; ".join(problems))
    size = body.get("class_size")
    size = int(_num(size, "수강 인원", 1, 100000)) if size not in (None, "") else None
    store.data.setdefault("grades", {})[str(course_id)] = {"components": comps, "dist": dist, "class_size": size}


def candidates(store) -> list[dict]:
    """공지·메시지·직접 입력에서 찾은 점수 문장. 사용자가 골라 평가 항목에 넣는다."""
    cache = store.data.get("cache") or {}
    names = {c["id"]: c["name"] for c in cache.get("courses", [])}
    texts = []
    for n in cache.get("announcements", []):
        cid = str(n.get("context_code", "")).replace("course_", "")
        texts.append((cid, "announcement", n.get("title"), html_to_text(n.get("message")), n.get("posted_at"),
                      n.get("html_url")))
    for cv in cache.get("conversations", []):
        code = str(cv.get("context_code") or "")
        cid = code.replace("course_", "") if code.startswith("course_") else None
        for m in (cv.get("messages") or [])[:10]:
            texts.append((cid, "inbox", cv.get("subject"), m.get("body") or "", m.get("created_at"), None))
    for m in store.data.get("manual", []):
        cid = next((k for k, v in names.items() if v == m["course_name"]), None)
        texts.append((cid, "manual", "직접 입력", m["text"], m.get("posted_at"), None))
    out = []
    for cid, typ, title, text, posted, url in texts:
        for f in g.find_scores(text or ""):
            out.append({"course_id": cid, "course_short": short_course_name(names.get(cid)) if cid else None,
                        "source_type": typ, "source_title": title, "posted_at": iso(parse_iso(posted)),
                        "url": url, "label": f.label, "score": f.score, "max_score": f.max_score,
                        "mean": f.mean, "sd": f.sd, "high": f.high, "low": f.low,
                        "sentence": f.sentence, "notes": f.notes})
    out.sort(key=lambda c: c["posted_at"] or "", reverse=True)
    return out[:100]


def _better(grade: str, dist: dict) -> str | None:
    usable = [x for x in g.GRADES if dist.get(x, 0) > 0]
    i = usable.index(grade) if grade in usable else None
    return usable[i - 1] if i else None


def section(store, courses: list[dict]) -> dict:
    """state["grades"]: 과목별 설정·추정·필요 점수, 우선순위, 점수 후보."""
    saved = store.data.get("grades") or {}
    out_courses, prio_in = [], []
    for c in courses:
        cfg = saved.get(c["id"]) or {"components": [], "dist": None, "class_size": None}
        dist = cfg.get("dist") or g.DEFAULT_DISTRIBUTION
        est, err, needed = None, None, []
        comps = cfg["components"]
        if comps:
            try:
                est = g.estimate(comps, dist, class_size=cfg.get("class_size"))
            except ValueError as e:
                err = str(e)
        if est:
            missing = [x for x in comps if x["score"] is None and not x["fixed"] and x["weight"] > 0]
            targets = [t for t in (_better(est["grade"], dist), est["grade"]) if t]
            for m in missing:
                for t in targets:
                    try:
                        needed.append({"component": m["name"], **g.needed_on(comps, m["name"], t, dist)})
                    except ValueError as e:
                        needed.append({"component": m["name"], "target": t, "error": str(e)})
            prio_in.append({"course": c["short_name"], "estimate": est,
                            "remaining_weight": sum(x["weight"] for x in missing), "dist": dist})
        out_courses.append({"course_id": c["id"], "short_name": c["short_name"],
                            "canvas_score": c.get("current_score"), "config": cfg,
                            "using_default_dist": cfg.get("dist") is None,
                            "estimate": est, "error": err, "needed": needed})
    try:
        prio = g.priorities(prio_in) if prio_in else []
    except ValueError:
        prio = []
    return {"grades": g.GRADES, "default_dist": g.DEFAULT_DISTRIBUTION, "courses": out_courses,
            "priorities": prio, "candidates": candidates(store)}
