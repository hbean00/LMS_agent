"""성적: 과목별 평가 항목 설정 저장 · 평가 비중 자동 채우기 · 점수 문장/점수표 후보 · 예상 학점 · 필요 점수 · 우선순위.

계산은 grades.py(정규분포 근사, 항목 사이 상관 가정)가 하고, 여기서는 저장된 설정과 Canvas 원본을
화면용 묶음(state["grades"])으로 만든다. 점수 문장은 후보로만 보여 주고, 사용자가 골라 항목에 넣는다.
평가 비중은 사용자가 손대지 않은 과목에 한해 강의계획서·공지에서 찾아 자동으로 넣는다(auto_fill, "auto": true).
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta

from . import agent_cli
from . import core as _core  # core.sync가 auto_fill을 부르므로 순환 import에도 안전하게 모듈로
from . import grades as g
from . import scorefile
from .extract import html_to_text
from .timeutil import iso, now_local, parse_iso
from .weights import WeightsFound, find_weights, normalize_name

MAX_COMPONENTS = 20
STUDENT_ID = re.compile(r"^[A-Za-z0-9]{6,12}$")


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
    """POST /api/grades/<course_id> 본문을 검사해 저장. 잘못되면 ValueError(한국어).
    사용자가 저장한 항목은 모두 사용자 입력이 된다("auto" 표시·자동 근거 기록을 지움) → 이후 auto_fill이 덮어쓰지 않는다."""
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
            out.append({"course_id": cid, "course_short": _core.short_course_name(names.get(cid)) if cid else None,
                        "source_type": typ, "source_title": title, "posted_at": iso(parse_iso(posted)),
                        "url": url, "label": f.label, "score": f.score, "max_score": f.max_score,
                        "mean": f.mean, "sd": f.sd, "high": f.high, "low": f.low,
                        "sentence": f.sentence, "notes": f.notes})
    out += score_file_candidates(store, names)
    out.sort(key=lambda c: c["posted_at"] or "", reverse=True)
    return out[:100]


# ---------------------------------------------------------------- 점수표 PDF(성적 공지 첨부)

_SCORE_CACHE: dict = {}


def mask_student_id(sid: str | None) -> str | None:
    """'2026123433' → '2026****33'. 화면에는 이것만 보인다."""
    if not sid:
        return None
    keep_head = 4 if len(sid) >= 8 else 2
    return sid[:keep_head] + "*" * max(0, len(sid) - keep_head - 2) + sid[-2:]


def set_student_id(store, value) -> None:
    """POST /api/grades/settings: 학번은 이 PC의 state.json에만 둔다(점수표에서 내 행을 찾는 데만 씀). null이면 지움."""
    if value is None or (isinstance(value, str) and not value.strip()):
        store.data["settings"].pop("student_id", None)
        return
    if not isinstance(value, str) or not STUDENT_ID.match(value.strip()):
        raise ValueError("학번은 6~12자의 숫자·영문이어야 합니다")
    store.data["settings"]["student_id"] = value.strip()


def _score_result(key: str, rec: dict, sid: str):
    text = rec.get("text") or ""
    ck = (key, sid, len(text), hash(text), rec.get("filename"))
    if ck not in _SCORE_CACHE:
        if len(_SCORE_CACHE) > 500:
            _SCORE_CACHE.clear()
        fn = rec.get("filename") or ""
        _SCORE_CACHE[ck] = (scorefile.parse_score_text(text, sid, filename=fn)
                            if scorefile.is_score_file(fn, text) else None)
    return _SCORE_CACHE[ck]


def score_file_candidates(store, names: dict | None = None) -> list[dict]:
    """settings.student_id가 있으면, 첨부 PDF 글(pdf_texts) 중 점수표에서 내 점수·반 집계를 후보로.
    키는 다른 후보와 같고 "key": "pdf:<첨부 키>"가 더 있다(같은 파일은 같은 key)."""
    sid = (store.data.get("settings") or {}).get("student_id")
    if not sid:
        return []
    if names is None:
        names = {c["id"]: c["name"] for c in (store.data.get("cache") or {}).get("courses", [])}
    out = []
    for key, rec in sorted((store.data.get("pdf_texts") or {}).items()):
        if not isinstance(rec, dict):
            continue
        r = _score_result(key, rec, sid)
        if r is None:
            continue
        cid = str(rec.get("course_id")) if rec.get("course_id") is not None else None
        out.append({"course_id": cid, "course_short": _core.short_course_name(names.get(cid)) if cid else None,
                    "source_type": "pdf", "source_title": rec.get("filename"),
                    "posted_at": iso(parse_iso(rec.get("posted_at"))), "url": None,
                    "label": r.label, "score": r.score, "max_score": r.max_score, "mean": r.mean, "sd": r.sd,
                    "high": r.high, "low": r.low, "sentence": r.sentence, "notes": list(r.notes),
                    "key": f"pdf:{key}"})
    return out


# ---------------------------------------------------------------- 평가 비중 자동 채우기

_PRIORITY = {"syllabus": 0, "board": 1, "announcement": 1, "pdf": 2, "assignment": 3, "manual": 3, "inbox": 4}
_UPDATE_TITLE = re.compile(r"update|updated|revised|revision|change|변경|수정|정정|업데이트|새\s*평가", re.I)
_COURSE_LEVEL = re.compile(r"중간|기말|mid|final|출석|attendance|participation|참여|exam|시험|고사|quiz|퀴즈|"
                           r"homework|과제|숙제|assignment|project|프로젝트", re.I)
_HAS_GRADING = re.compile(r"평가|성적|배점|비중|반영|grading|grade|weight|%|퍼센트", re.I)
_RANK = {"high": 3, "medium": 2, "low": 1}
AI_RETRY_HOURS = 12


def _when(src: dict) -> float:
    v = src.get("posted_at")
    dt = v if isinstance(v, datetime) else parse_iso(v) if isinstance(v, str) else None
    if dt is None:
        dt = parse_iso(src.get("posted_raw")) if isinstance(src.get("posted_raw"), str) else None
    try:
        return dt.timestamp() if dt else 0.0
    except (OverflowError, OSError, ValueError):
        return 0.0


def _when_iso(src: dict) -> str | None:
    t = _when(src)
    return iso(datetime.fromtimestamp(t).astimezone()) if t else None


def _course_level(src: dict, wf: WeightsFound) -> bool:
    """과목 전체 비중으로 볼 만한지: 강의계획서이거나, 시험·출석·과제 같은 항목이 들어 있음
    (과제 하나의 채점 기준 'Prelab 30%, Report 70%'가 과목 비중을 덮지 않게)."""
    return src.get("type") == "syllabus" or any(_COURSE_LEVEL.search(c["name"]) for c in wf.components)


def _canon(name) -> str:
    """이름 비교용: 대소문자 무시 + 흔한 표현은 대표 이름('Attendance' = '출석')."""
    return normalize_name(str(name)).lower()


def _sig(comps: list[dict]) -> list:
    return sorted((_canon(c["name"]), round(float(c["weight"] or 0), 2)) for c in comps)


def _is_user_edited(cfg: dict | None) -> bool:
    return bool(cfg) and any(not c.get("auto") for c in cfg.get("components") or [])


def _ai_target(srcs: list[dict]) -> dict | None:
    cands = [s for s in srcs if _HAS_GRADING.search(s.get("text") or "")]
    if not cands:
        return None
    syl = [s for s in cands if s.get("type") == "syllabus"]
    return max(syl or cands, key=lambda s: len(s.get("text") or ""))


def _diff(prev: list[dict], comps: list[dict]) -> dict:
    p = {_canon(c["name"]): c for c in prev}
    n = {_canon(c["name"]): c for c in comps}
    return {"added": [{"name": n[k]["name"], "weight": n[k]["weight"]} for k in n if k not in p],
            "removed": [{"name": p[k]["name"], "weight": p[k]["weight"]} for k in p if k not in n],
            "changed": [{"name": n[k]["name"], "from": p[k]["weight"], "to": n[k]["weight"]}
                        for k in n if k in p and round(float(p[k]["weight"]), 2) != round(float(n[k]["weight"]), 2)]}


def _diff_text(d: dict) -> str:
    parts = [f"{x['name']} {x['weight']:g}% 추가" for x in d["added"]]
    parts += [f"{x['name']} {x['from']:g}→{x['to']:g}%" for x in d["changed"]]
    parts += [f"{x['name']} 빠짐" for x in d["removed"]]
    return ", ".join(parts[:6])


def auto_fill(store, sources: list[dict], ai_enabled: bool, runner=None,
              session: agent_cli.AiSession | None = None) -> list[str]:
    """평가 비중을 찾아 사용자가 손대지 않은 과목의 성적 설정에 자동으로 넣는다. 한국어 기록 줄을 돌려준다.

    리더(core.sync)가 항목을 다시 만든 뒤 부른다(저장은 부르는 쪽이):
        gradebook.auto_fill(store, core._sources(cache, store.data["manual"], now),
                            ai_enabled=bool(store.data["settings"].get("ai_cli")), session=같은 AiSession(선택))

    - 대상: 설정이 없거나 항목이 모두 "auto": true인 과목. 사용자가 고친 과목(auto 없는 항목이 하나라도)은 건드리지 않는다.
    - 규칙(weights.find_weights)을 먼저: 확신 high(합 ~100)인 것 중 **가장 최근 글**(posted_at)이 이긴다 — 학기 중
      'Updated Grading Policy'·'평가 방법 변경' 공지가 강의계획서를 대신한다. 같은 시각이면 강의계획서 > 게시판·공지 > 첨부 PDF.
      과제 하나의 채점 기준처럼 과목 전체 비중이 아닌 것은 건너뛴다.
    - high가 없고 ai_enabled면 AI CLI(grading_weights)를 강의계획서(없으면 평가 낱말이 있는 가장 긴 글)에 한 번 —
      같은 글이면 다시 부르지 않는다(결과를 기억, 실패는 AI_RETRY_HOURS 뒤 다시). 그다음 규칙의 medium.
    - 넣을 때 이름이 같은 항목(대소문자 무시)의 점수·만점·평균·표준편차는 그대로 둔다.
      비중이 바뀌면 이전 자동 비중을 auto_previous에 남겨 화면이 "공지로 평가 비중이 바뀌었어요"를 보여 준다.
    """
    store.data.setdefault("grades", {})
    wa = store.data.setdefault("weights_auto", {})
    ai_cache = wa.setdefault("ai", {})
    if ai_enabled and session is None:
        session = agent_cli.AiSession(store, runner=runner, enabled=True)
    by_course: dict[str, list[dict]] = {}
    for s in sources or []:
        if s.get("course_id") is not None and (s.get("text") or "").strip():
            by_course.setdefault(str(s["course_id"]), []).append(s)
    log, missing, ai_blocked = [], [], None
    now = iso(now_local())
    for cid, srcs in sorted(by_course.items()):
        cfg = store.data["grades"].get(cid)
        if _is_user_edited(cfg):
            continue
        short = _core.short_course_name(srcs[0].get("course_name")) or cid
        found = []
        for s in srcs:
            wf = find_weights(s.get("text") or "", bool(s.get("is_html")))
            if wf and _course_level(s, wf):
                found.append((wf, s))
        highs = [x for x in found if x[0].confidence == "high"]
        chosen, by = None, "rules"
        if highs:
            chosen = max(highs, key=lambda x: (_when(x[1]), bool(_UPDATE_TITLE.search(x[1].get("title") or "")),
                                               -_PRIORITY.get(x[1].get("type"), 5)))
        elif ai_enabled:
            target = _ai_target(srcs)
            if target is not None:
                wf, err = _ai_weights(store, ai_cache, cid, target, session)
                if wf is not None:
                    chosen, by = (wf, target), "ai"
                elif err:
                    if err.startswith(("이번 동기화", "오늘", "AI CLI 사용이 꺼져")):
                        ai_blocked = err
                    else:
                        log.append(f"{short}: AI CLI로 평가 비중을 찾지 못했습니다 — {err}")
        if chosen is None:
            meds = [x for x in found if x[0].confidence == "medium" and x[0].total <= 100.5]
            if meds:
                chosen = max(meds, key=lambda x: (-abs(x[0].total - 100), _when(x[1]),
                                                  -_PRIORITY.get(x[1].get("type"), 5)))
        if chosen is None:
            if any(x[0].confidence == "low" for x in found):
                log.append(f"{short}: 평가 비중 후보가 있지만 확신이 낮아 넣지 않았습니다")
            elif not (cfg and cfg.get("components")):
                missing.append(short)
            continue
        line = _apply(store, cid, cfg, chosen[0], chosen[1], by, now, short)
        if line:
            log.append(line)
    if ai_blocked:
        log.append(f"AI CLI를 쓰지 않음: {ai_blocked}")
    if missing:
        log.append("평가 비중을 못 찾은 과목: " + ", ".join(missing[:10]))
    return log


def _ai_weights(store, cache: dict, cid: str, src: dict, session) -> tuple[WeightsFound | None, str | None]:
    text = src.get("text") or ""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    prev = cache.get(cid)
    if prev and prev.get("hash") == h:
        if prev.get("result"):
            r = prev["result"]
            return WeightsFound(r["components"], r["total"], r["sentence"], r["confidence"], list(r["notes"])), None
        at = parse_iso(prev.get("at"))
        if not prev.get("error") or (at and now_local() - at < timedelta(hours=AI_RETRY_HOURS)):
            return None, None  # 같은 글: 비중 없음이었거나 얼마 전에 실패 → 다시 부르지 않는다
    res = session.run("grading_weights", {"text": text, "is_html": bool(src.get("is_html")),
                                          "title": src.get("title")})
    if res.attempts == 0 and not res.ok:
        return None, res.error   # 한도·꺼짐·CLI 없음: 기억하지 않는다
    wf = res.data if res.ok else None
    if wf is not None:
        wf.notes += res.notes
    cache[cid] = {"hash": h, "at": iso(now_local()), "error": None if res.ok else res.error,
                  "result": ({"components": wf.components, "total": wf.total, "sentence": wf.sentence,
                              "confidence": wf.confidence, "notes": wf.notes} if wf else None)}
    return wf, (None if res.ok else res.error)


def _apply(store, cid: str, cfg: dict | None, wf: WeightsFound, src: dict, by: str, now: str, short: str) -> str | None:
    old = (cfg or {}).get("components") or []
    if old and _sig(old) == _sig(wf.components):
        return None  # 같은 비중(다른 글에 다시 적혀 있어도) → 그대로
    title = src.get("title") or src.get("type") or "글"
    source = f"{title}: {wf.sentence}"[:300]
    keep = {_canon(c["name"]): c for c in old}
    comps = []
    for c in wf.components:
        p = keep.get(_canon(c["name"]), {})
        comps.append({"name": c["name"], "weight": float(c["weight"]), "score": p.get("score"),
                      "max_score": p.get("max_score"), "mean": p.get("mean"), "sd": p.get("sd"),
                      "fixed": bool(c.get("fixed")), "source": source, "auto": True})
    store.data["grades"][cid] = {
        "components": comps, "dist": (cfg or {}).get("dist"), "class_size": (cfg or {}).get("class_size"),
        "auto_source": source, "auto_by": by, "auto_confidence": wf.confidence, "auto_notes": list(wf.notes),
        "auto_source_type": src.get("type"), "auto_posted_at": _when_iso(src), "auto_updated_at": now,
        "auto_previous": [{"name": c["name"], "weight": c["weight"]} for c in old] if old else None,
    }
    how = " (AI CLI)" if by == "ai" else ""
    if old:
        return f"{short}: '{title}'에 따라 평가 비중을 바꿨습니다{how} — {_diff_text(_diff(old, comps))}"
    return f"{short}: 평가 비중 {len(comps)}개를 '{title}'에서 찾아 넣었습니다{how}(합 {wf.total:g}%)"


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
        auto = bool(comps) and all(x.get("auto") for x in comps)
        changed = None
        if auto and cfg.get("auto_previous"):
            changed = {"at": cfg.get("auto_updated_at"), "source": cfg.get("auto_source"),
                       **_diff(cfg["auto_previous"], comps)}
        out_courses.append({"course_id": c["id"], "short_name": c["short_name"],
                            "canvas_score": c.get("current_score"), "config": cfg,
                            "using_default_dist": cfg.get("dist") is None,
                            "estimate": est, "error": err, "needed": needed,
                            "auto": auto, "auto_source": cfg.get("auto_source") if auto else None,
                            "auto_by": cfg.get("auto_by") if auto else None, "auto_changed": changed})
    try:
        prio = g.priorities(prio_in) if prio_in else []
    except ValueError:
        prio = []
    sid = (store.data.get("settings") or {}).get("student_id")
    return {"grades": g.GRADES, "default_dist": g.DEFAULT_DISTRIBUTION, "courses": out_courses,
            "priorities": prio, "candidates": candidates(store),
            "student_id_set": bool(sid), "student_id_masked": mask_student_id(sid)}
