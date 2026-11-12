"""성적: 과목별 평가 항목 설정 저장 · 평가 비중 자동 채우기 · 점수 문장/점수표 후보 · 예상 학점 · 필요 점수 · 우선순위.
점수·마감을 평가 항목에 연결하고(LAB0 → Homework assignments), 세부 점수(members)·개수(count)·일정(schedule)을
상태를 만들 때마다 계산한다(계약 14장, 규칙은 links.py). 제외(hidden)·P/NP(pass_fail) 과목은 계약 15장대로 다룬다.

계산은 grades.py(정규분포 근사, 항목 사이 상관 가정)가 하고, 여기서는 저장된 설정과 Canvas 원본을
화면용 묶음(state["grades"])으로 만든다. 점수 문장은 후보로만 보여 주고, 사용자가 골라 항목에 넣는다.
평가 비중은 사용자가 손대지 않은 과목에 한해 강의계획서·공지에서 찾아 자동으로 넣는다(auto_fill, "auto": true).
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
import statistics
from datetime import datetime, timedelta

from . import agent_cli
from . import core as _core  # core.sync가 auto_fill을 부르므로 순환 import에도 안전하게 모듈로
from . import grades as g
from . import links as lk
from . import scorefile
from .extract import html_to_text
from .store import course_mode, group_key, hidden_courses
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


def candidate_key(cid, source_type, source_title, label, score) -> str:
    """글 속 점수 문장의 안정적인 키(계약 14-1): "txt:<source_type>:<sha1(course_id|source_title|label|score)[:12]>"."""
    raw = "|".join("" if x is None else str(x) for x in (cid, source_title, label, score))
    return f"txt:{source_type}:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def candidates(store, include_hidden: bool = False) -> list[dict]:
    """공지·메시지·직접 입력에서 찾은 점수 문장. 사용자가 골라 평가 항목에 넣는다.
    모든 후보에 안정적인 key(계약 14-1). 제외한(hidden) 과목의 후보는 빼고 돌려준다(include_hidden이면 모두)."""
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
                        "mean": f.mean, "sd": f.sd, "high": f.high, "low": f.low, "n": None, "rank": None,
                        "sentence": f.sentence, "notes": f.notes,
                        "key": candidate_key(cid, typ, title, f.label, f.score)})
    out += score_file_candidates(store, names)
    if not include_hidden:
        hidden = hidden_courses(store.data)
        out = [c for c in out if str(c.get("course_id")) not in hidden]
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
                    "high": r.high, "low": r.low, "n": r.n, "rank": r.rank,
                    "sentence": r.sentence, "notes": list(r.notes),
                    "key": f"pdf:{key}"})
    return out


def score_files(store) -> tuple[int, list[str]]:
    """(학번이 없을 때 받아 둔 성적 PDF 수, 학번은 있는데 내 줄을 못 찾은 성적 PDF 파일 이름) — 계약 14-7.
    제외한 과목의 파일은 세지 않는다. 여기서는 파일 이름이 점수표(score·점수·성적·grade)인 것만 센다 — 글 모양만으로는
    명령어 표·주소 표도 점수표처럼 보여 '학번을 확인해 주세요'가 잘못 뜰 수 있다(실제: Midterm_project.pdf)."""
    sid = (store.data.get("settings") or {}).get("student_id")
    hidden = hidden_courses(store.data)
    waiting, unmatched = 0, []
    for key, rec in sorted((store.data.get("pdf_texts") or {}).items()):
        if not isinstance(rec, dict) or str(rec.get("course_id")) in hidden:
            continue
        fn = rec.get("filename") or ""
        if not scorefile.is_score_file(fn, ""):
            continue
        if not sid:
            waiting += 1
        elif _score_result(key, rec, sid) is None:
            unmatched.append(fn or key)
    return waiting, unmatched


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
    hidden = hidden_courses(store.data)   # 제외한 과목은 AI에 묻지 않는다(계약 15장)
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
        elif ai_enabled and cid not in hidden:
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


def weight_totals(comps: list) -> tuple[float, float]:
    """(항목 비중 합, 100 − 합 — 0 이상). 계약 13-2: 합이 100이 안 되면 나머지는 '평가 항목이 없는' 비중이다."""
    total = 0.0
    for x in comps or []:
        w = x.get("weight") if isinstance(x, dict) else None
        if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0:
            total += float(w)
    total = round(total, 2)
    return total, round(max(0.0, 100.0 - total), 2)


# ================================================================ 14장: 점수·마감 → 평가 항목, 세부 점수(members), 개수(count)

_OLD_TS = 946684800.0   # 2000-01-01 — 시각을 모르는 글
AI_LINK_MAX = 20        # 과목마다 한 번에 AI에 묻는 연결 수
_REP = {"lab": "Lab", "hw": "HW", "quiz": "Quiz", "report": "Report", "project": "Project"}
_BASIS_KO = {"stated": "명시", "hint": "단서", "user": "직접 입력", "observed": "관찰"}


def _ts(v) -> float:
    d = parse_iso(v) if isinstance(v, str) else None
    try:
        return d.timestamp() if d else _OLD_TS
    except (OverflowError, OSError, ValueError):
        return _OLD_TS


def _lcomps(cfg: dict | None, components: list | None = None) -> list:
    cfg = cfg or {}
    return lk.components_of(cfg.get("components") if components is None else components, [cfg.get("auto_source")])


def link_candidates(store, course_id, components, cands) -> dict:
    """점수 후보 → 평가 항목(계약 14-2). 사용자(grade_links) → AI 기억(grade_links_ai) → 규칙 순.
    다른 과목·과목 모르는 후보는 넣지 않는다. → {key: {"component": 이름|None, "by": "rules"|"ai"|"user", "reason", "evidence"}}"""
    cid = str(course_id)
    comps = _lcomps((store.data.get("grades") or {}).get(cid), components or [])
    names = {lk.norm(c.name): c.name for c in comps}
    user = (store.data.get("grade_links") or {}).get(cid) or {}
    ai = (store.data.get("grade_links_ai") or {}).get(cid) or {}
    out = {}
    for c in cands or []:
        key = c.get("key")
        if not key or c.get("course_id") is None or str(c.get("course_id")) != cid:
            continue
        u = user.get(key)
        if isinstance(u, dict) and "component" in u:
            if u["component"] is None:
                out[key] = {"component": None, "by": "user", "reason": "이 과목 성적에 넣지 않음(직접 정함)", "evidence": None}
                continue
            if lk.norm(u["component"]) in names:
                out[key] = {"component": names[lk.norm(u["component"])], "by": "user", "reason": "직접 연결함",
                            "evidence": None}
                continue
        a = ai.get(key)
        if isinstance(a, dict) and a.get("component") and lk.norm(a["component"]) in names:
            out[key] = {"component": names[lk.norm(a["component"])], "by": "ai",
                        "reason": a.get("reason") or "AI가 연결함", "evidence": a.get("evidence")}
            continue
        m = lk.match(c.get("label") or c.get("source_title"), comps, other_text=c.get("source_title") or "")
        reason = m.reason
        if m.component is None and isinstance(a, dict) and not a.get("error") and a.get("component") is None:
            reason += " · AI도 정하지 못했어요"
        out[key] = {"component": m.component, "by": "rules", "reason": reason, "evidence": m.evidence}
    return out


def _label_key(label) -> str:
    return re.sub(r"\s+", "", str(label or "")).lower()


def _label_order(m: dict):
    lab = str(m.get("label") or "")
    n = re.search(r"\d+", lab)
    return (re.sub(r"\d+", "", lab).strip().lower(), int(n.group()) if n else -1, lab)


def members_of(cands, links) -> dict:
    """연결된 점수 후보 → 항목 이름별 세부 점수(계약 14-3). 같은 라벨이 여러 번(다시 공지)이면 가장 최근 것, 라벨 번호 순."""
    latest: dict = {}
    for c in cands or []:
        ln = links.get(c.get("key")) if c.get("key") else None
        if not ln or not ln.get("component") or c.get("score") is None:
            continue
        k = (ln["component"], _label_key(c.get("label") or c.get("source_title")))
        prev = latest.get(k)
        if prev is None or _ts(c.get("posted_at")) > _ts(prev[0].get("posted_at")):
            latest[k] = (c, ln)
    out: dict = {}
    for (comp, _lab), (c, ln) in latest.items():
        out.setdefault(comp, []).append({
            "key": c["key"], "label": c.get("label") or c.get("source_title"), "score": c.get("score"),
            "max_score": c.get("max_score"), "mean": c.get("mean"), "sd": c.get("sd"), "n": c.get("n"),
            "rank": c.get("rank"), "source_type": c.get("source_type"), "source_title": c.get("source_title"),
            "posted_at": c.get("posted_at"), "url": c.get("url"), "link_by": ln["by"], "link_reason": ln["reason"]})
    for v in out.values():
        v.sort(key=_label_order)
    return out


def _pct(v, mx):
    return None if v is None else float(v) / float(mx or 100) * 100.0


def derived_of(members: list, per_item: bool) -> dict | None:
    """세부 점수 → 항목 점수(만점 대비 %). 기본은 세부 점수마다 같은 비중(계약 14-3)."""
    got = [m for m in members or [] if m.get("score") is not None]
    if not got:
        return None

    def avg(xs):
        return round(statistics.fmean(xs), 2) if xs else None
    return {"score_pct": avg([_pct(m["score"], m.get("max_score")) for m in got]),
            "mean_pct": avg([_pct(m["mean"], m.get("max_score")) for m in got if m.get("mean") is not None]),
            "sd_pct": avg([_pct(m["sd"], m.get("max_score")) for m in got if m.get("sd") is not None]),
            "received": len(got), "basis": "per_item" if per_item else "average"}


_TEXT_MEMO: dict = {}


def _course_texts(store) -> dict:
    """과목별 글 [(제목, 글자, 게시 시각)] — 최신 글 먼저. 개수 규칙·AI가 읽는다. 같은 저장 상태에서는 한 번만 만든다."""
    cache = store.data.get("cache")
    # 같은 cache 객체(참조를 쥐고 있어 그 id가 다른 객체에 다시 쓰이지 않는다)이고 크기가 같을 때만 기억한 값을 쓴다
    sig = (store.data.get("last_sync"), len(store.data.get("pdf_texts") or {}), len(store.data.get("manual") or []),
           len((cache or {}).get("announcements") or []), len((cache or {}).get("boards") or []),
           len((cache or {}).get("pdf_texts") or {}), len((cache or {}).get("courses") or []))
    if _TEXT_MEMO.get("obj") is not cache or _TEXT_MEMO.get("sig") != sig:
        out: dict = {}
        if cache:
            for s in _core._sources(cache, store.data.get("manual", []), now_local()):
                if s.get("course_id") is None:
                    continue
                text = html_to_text(s.get("text")) if s.get("is_html") else (s.get("text") or "")
                out.setdefault(str(s["course_id"]), []).append((s.get("title") or "", text, _when(s)))
        for v in out.values():
            v.sort(key=lambda t: -t[2])
        _TEXT_MEMO.clear()
        _TEXT_MEMO.update(obj=cache, sig=sig, value=out, scan={})
    return _TEXT_MEMO["value"]


def _observations(store, cid: str, cands, items) -> dict:
    """과목 글에서 본 번호(계약 14-4 관찰): 점수 라벨, 마감 제목, 게시판 글 제목, 받은 자료 파일 이름 → {계열: [번호…]}."""
    cache = store.data.get("cache") or {}
    names = [c.get("label") for c in cands or [] if str(c.get("course_id")) == cid]
    names += [it.get("title") for it in items or [] if str(it.get("course_id")) == cid]
    for b in cache.get("boards") or []:
        if str(b.get("course_id")) != cid:
            continue
        for p in b.get("posts") or []:
            names.append(p.get("title"))
            names += [a.get("filename") for a in p.get("attachments") or []]
    names += [m.get("filename") for m in (store.data.get("downloads") or {}).values()
              if isinstance(m, dict) and str(m.get("course_id")) == cid]
    names += [t.get("filename") for t in (store.data.get("pdf_texts") or {}).values()
              if isinstance(t, dict) and str(t.get("course_id")) == cid]
    out = {}
    for f in lk.COUNTABLE:
        nums = sorted({n for t in names if t for n in lk.numbered(t, f)})
        if nums:
            out[f] = nums
    return out


def _scan(store, cid: str, fam: str, zero: bool, texts: list) -> tuple:
    """(명시 (n, 줄, 제목) | None, 단서 (n, 줄, 제목) | None) — 최신 글부터. 같은 저장 상태에서는 기억한다."""
    _course_texts(store)
    memo = _TEXT_MEMO.setdefault("scan", {})
    k = (cid, fam, zero, len(texts))
    if k not in memo:
        st = hi = None
        for title, text, _ in texts:
            if st is None:
                r = lk.stated(text, fam)
                if r:
                    st = (r[0], r[1], title)
            if hi is None:
                r = lk.hint(text, fam, zero, lk.numbered(title, fam))
                if r:
                    hi = (r[0], r[1], title)
            if st and hi:
                break
        memo[k] = (st, hi)
    return memo[k]


def _family_target(fam: str, comps: list) -> str | None:
    return lk.match(_REP[fam], comps).component


def _count_for(store, cid: str, comp, comps: list, obs: dict, texts: list) -> dict | None:
    """항목 하나의 개수(계약 14-4): 사용자 > 명시 > 단서(규칙) > 단서(AI) > 관찰."""
    key = lk.norm(comp.name)
    user = ((store.data.get("grade_counts") or {}).get(cid) or {}).get(key)
    ai = ((store.data.get("grade_counts_ai") or {}).get(cid) or {}).get(key)
    targeted = [f for f in obs if _family_target(f, comps) == comp.name]   # 본 번호가 이 항목으로 연결되는 계열
    fams = list(targeted)
    if not comp.name_f & set(lk.TERM) and not re.search(r"\d", comp.name):   # 'Project1'은 하나짜리 — 세지 않는다
        fams += [f for f in lk.COUNTABLE if f in comp.name_f and f not in fams]
    seen = sorted({n for f in targeted for n in obs.get(f, [])})
    low = lk.observed_low(seen)
    base = {"low": low, "seen": seen, "family": fams[0] if fams else None}
    if isinstance(user, dict) and isinstance(user.get("n"), int):
        return {"n": user["n"], "high": user["n"], "basis": "user", "evidence": None, "source_title": None,
                "by": "user", **base}
    if not fams:
        return None
    for f in fams:
        st, _ = _scan(store, cid, f, 0 in obs.get(f, []), texts)
        if st:
            return {"n": st[0], "high": st[0], "basis": "stated", "evidence": st[1], "source_title": st[2],
                    "by": "rules", **base, "family": f}
    for f in fams:
        _, hi = _scan(store, cid, f, 0 in obs.get(f, []), texts)
        if hi:
            return {"n": hi[0], "high": hi[0], "basis": "hint", "evidence": hi[1], "source_title": hi[2],
                    "by": "rules", **base, "family": f}
    if isinstance(ai, dict) and isinstance(ai.get("n"), int) and (low is None or ai["n"] >= low):
        return {"n": ai["n"], "high": ai["n"], "basis": "hint", "evidence": ai.get("evidence"),
                "source_title": ai.get("source_title"), "by": "ai", **base}
    if low is not None:
        return {"n": None, "high": None, "basis": "observed", "evidence": None, "source_title": None,
                "by": "rules", **base}
    return None


def _known_n(comp: dict) -> int | None:
    cnt = comp.get("count") or {}
    n = cnt.get("n") if cnt.get("basis") != "observed" else None
    return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 1 else None


def _received(comp: dict) -> list:
    return [m for m in comp.get("members") or [] if m.get("score") is not None]


def expand_components(components: list) -> list:
    """grades.estimate에 넣을 목록(계약 14-5). members 없는 항목은 그대로.
    개수를 알면 받은 세부 점수마다 W/n, 남은 것 하나 W·(n−r)/n(점수 없음 → 남은 평가).
    개수를 모르면(관찰만) 받은 것의 평균 비율로 W 전체를 하나로."""
    out = []
    for c in components or []:
        base = {k: c.get(k) for k in ("name", "weight", "score", "max_score", "mean", "sd", "fixed")}
        base["fixed"] = bool(base.get("fixed"))
        got = _received(c)
        if not got or base["fixed"]:
            out.append(base)
            continue
        w = float(c.get("weight") or 0)
        n = _known_n(c)
        if n:
            n = max(n, len(got))
            for m in got:
                out.append({"name": f"{c['name']} · {m['label']}", "weight": w / n, "score": m["score"],
                            "max_score": m.get("max_score"), "mean": m.get("mean"), "sd": m.get("sd"),
                            "fixed": False, "parent": c["name"]})
            left = n - len(got)
            if left > 0:
                out.append({"name": f"{c['name']} 남은 {left}개", "weight": w * left / n, "score": None,
                            "max_score": None, "mean": None, "sd": None, "fixed": False, "parent": c["name"],
                            "count_left": left})
        else:
            d = derived_of(got, False)
            out.append({"name": c["name"], "weight": w, "score": d["score_pct"], "max_score": 100.0,
                        "mean": d["mean_pct"], "sd": d["sd_pct"], "fixed": False, "parent": c["name"]})
    return out


def remaining_of(components: list) -> list:
    """courses[].remaining(계약 14-5): [{"component", "weight", "count_left", "range": [lo, hi] | null}].
    개수를 모르는 세부 점수 항목(관찰만)은 weight·count_left가 null이고 range = [관찰 최소 개수로 본 남은 비중, 전체 비중]."""
    out = []
    for c in components or []:
        w = float(c.get("weight") or 0)
        if c.get("fixed") or w <= 0:
            continue
        got = _received(c)
        cnt = c.get("count") or {}
        n = _known_n(c)
        if not got:
            if c.get("score") is None:
                out.append({"component": c["name"], "weight": round(w, 2), "count_left": n, "range": None})
            continue
        if n:
            n = max(n, len(got))
            left = n - len(got)
            if left > 0:
                out.append({"component": c["name"], "weight": round(w * left / n, 2), "count_left": left, "range": None})
            continue
        low = cnt.get("low")
        lo = round(w * max(0, low - len(got)) / low, 2) if isinstance(low, int) and low > 0 else 0.0
        out.append({"component": c["name"], "weight": None, "count_left": None, "range": [lo, round(w, 2)]})
    return out


def _count_notes(components: list) -> list[str]:
    notes = []
    for c in components or []:
        got = _received(c)
        if not got:
            continue
        cnt = c.get("count") or {}
        n = _known_n(c)
        w = float(c.get("weight") or 0)
        if n:
            n = max(n, len(got))
            left = n - len(got)
            src = f" — {cnt.get('source_title')}: \"{cnt['evidence'][:80]}\"" if cnt.get("evidence") else ""
            notes.append(f"{c['name']}: {_BASIS_KO.get(cnt.get('basis'), '')} {n}개 중 {len(got)}개 받음, 하나에 {w / n:.1f}%"
                         + (f" · 남은 {left}개 = {w * left / n:.1f}%" if left else "") + src)
        else:
            notes.append(f"{c['name']}: 남은 개수를 몰라 받은 것 평균으로 어림(받은 {len(got)}개)")
    return notes


def _item_early(it: dict, sem: dict | None) -> bool | None:
    total = (sem or {}).get("weeks_total")
    wk = it.get("week") if isinstance(it.get("week"), int) else (_core.week_of(sem, it.get("due")) if sem else None)
    if not total or not wk:
        return None
    return wk <= total / 2


def link_items(store, items: list, sem: dict | None = None) -> list:
    """마감·시험 Item → 평가 항목(계약 14-9): it["component"], it["component_by"], it["component_reason"].
    사용자(item_links[item_id], 없으면 같은 그룹 키로 남긴 것) → AI 기억(grade_links_ai[cid]["item:<id>"]) → 규칙."""
    saved = store.data.get("grades") or {}
    user = store.data.get("item_links") or {}
    by_group = {}
    for v in sorted((v for v in user.values() if isinstance(v, dict)), key=lambda v: str(v.get("at") or "")):
        if v.get("group"):
            by_group[v["group"]] = v
    ai_all = store.data.get("grade_links_ai") or {}
    memo: dict = {}
    for it in items or []:
        it["component"], it["component_by"], it["component_reason"] = None, None, None
        cid = str(it.get("course_id")) if it.get("course_id") is not None else None
        cfg = saved.get(cid) if cid else None
        if not cfg or not cfg.get("components"):
            continue
        if cid not in memo:
            comps = _lcomps(cfg)
            memo[cid] = (comps, {lk.norm(c.name): c.name for c in comps})
        comps, names = memo[cid]
        u = user.get(it["id"])
        if not isinstance(u, dict):
            gk = group_key(it)
            u = by_group.get(gk) if gk else None
        if isinstance(u, dict) and "component" in u:
            if u["component"] is None:
                it["component_by"], it["component_reason"] = "user", "평가 항목에 연결하지 않음(직접 정함)"
                continue
            if lk.norm(u["component"]) in names:
                it["component"], it["component_by"] = names[lk.norm(u["component"])], "user"
                it["component_reason"] = "직접 연결함"
                continue
        a = (ai_all.get(cid) or {}).get("item:" + it["id"])
        if isinstance(a, dict) and a.get("component") and lk.norm(a["component"]) in names:
            it["component"], it["component_by"] = names[lk.norm(a["component"])], "ai"
            it["component_reason"] = a.get("reason") or "AI가 연결함"
            continue
        ev = " ".join(str(e.get("text") or "") for e in it.get("evidence") or [] if isinstance(e, dict))
        m = lk.match(it.get("title"), comps, term_text=ev, kind=it.get("kind"), early=_item_early(it, sem))
        if m.component:
            it["component"], it["component_by"] = m.component, "rules"
        it["component_reason"] = m.reason
    return items


SCHEDULE_STATUSES = ("review", "scheduled")


def _schedule(items: list, cid: str, name: str, now) -> list:
    out = []
    for it in items or []:
        if str(it.get("course_id")) != cid or it.get("component") != name or it.get("status") not in SCHEDULE_STATUSES:
            continue
        d = parse_iso(it.get("due"))
        if d is None:
            continue
        out.append({"item_id": it["id"], "title": it.get("title"), "due": it.get("due"), "kind": it.get("kind"),
                    "status": it.get("status"), "past": d < now})
    out.sort(key=lambda x: _ts(x["due"]))
    return out


def section(store, courses: list[dict], items: list | None = None, sem: dict | None = None,
            cands: list | None = None) -> dict:
    """state["grades"]: 과목별 설정(+세부 점수·개수·일정)·추정·필요 점수·남은 평가, 우선순위, 점수 후보(+연결).
    제외(hidden)한 과목은 빼고, P/NP(pass_fail) 과목은 학점·상위 % 추정과 우선순위에서 뺀다(계약 15장).
    config는 저장된 설정의 복사본에 계산한 칸(members·derived·count·schedule·date_status)을 붙인 것 — 저장하지 않는다.
    items에 component가 없으면(link_items 전) 여기서 붙인다."""
    saved = store.data.get("grades") or {}
    cands = candidates(store) if cands is None else cands
    if items is not None and items and "component" not in items[0]:
        link_items(store, items, sem)
    hidden = hidden_courses(store.data)
    texts = _course_texts(store)
    now = now_local()
    out_courses, prio_in, all_links = [], [], {}
    for c in courses:
        cid = str(c["id"])
        if cid in hidden:
            continue
        mode = course_mode(store.data, cid)
        cfg = copy.deepcopy(saved.get(cid) or {"components": [], "dist": None, "class_size": None})
        comps = cfg.setdefault("components", [])
        dist = cfg.get("dist") or g.DEFAULT_DISTRIBUTION
        mine = [x for x in cands if str(x.get("course_id")) == cid]
        links = link_candidates(store, cid, comps, mine) if comps else {}
        all_links.update(links)
        lcomps = _lcomps(cfg)
        by_name = {x.name: x for x in lcomps}
        members = members_of(mine, links)
        obs = _observations(store, cid, mine, items) if comps else {}
        for x in comps:
            name = str(x.get("name") or "").strip()
            x["members"] = members.get(name, [])
            x["count"] = _count_for(store, cid, by_name[name], lcomps, obs, texts.get(cid, [])) if name in by_name else None
            x["derived"] = derived_of(x["members"], bool(_known_n(x))) if x["members"] else None
            x["schedule"] = _schedule(items, cid, name, now)
            x["date_status"] = ("announced" if any(not s["past"] for s in x["schedule"])
                                else "past" if x["schedule"] else "not_announced")
        expanded = expand_components(comps)
        w_total, w_missing = weight_totals(comps)
        est, err, needed = None, None, []
        if comps and mode != "pass_fail":
            try:
                est = g.estimate(expanded, dist, class_size=cfg.get("class_size"))
            except ValueError as e:
                err = str(e)
        if est:
            est["notes"] = est["notes"] + _count_notes(comps)
            missing = [x for x in expanded if x["score"] is None and not x["fixed"] and x["weight"] > 0]
            targets = [t for t in (_better(est["grade"], dist), est["grade"]) if t]
            for m in missing:
                extra = {"parent": m["parent"]} if m.get("parent") else {}
                for t in targets:
                    try:
                        needed.append({"component": m["name"], **g.needed_on(expanded, m["name"], t, dist),
                                       "missing_weight": w_missing, **extra})
                    except ValueError as e:
                        needed.append({"component": m["name"], "target": t, "error": str(e),
                                       "missing_weight": w_missing, **extra})
            prio_in.append({"course": c["short_name"], "estimate": est,
                            "remaining_weight": sum(x["weight"] for x in missing), "dist": dist,
                            "missing_weight": w_missing})
        auto = bool(comps) and all(x.get("auto") for x in comps)
        changed = None
        if auto and cfg.get("auto_previous"):
            changed = {"at": cfg.get("auto_updated_at"), "source": cfg.get("auto_source"),
                       **_diff(cfg["auto_previous"], comps)}
        out_courses.append({"course_id": c["id"], "short_name": c["short_name"],
                            "canvas_score": c.get("current_score"), "config": cfg,
                            "using_default_dist": cfg.get("dist") is None,
                            "estimate": est, "error": err, "needed": needed,
                            "weight_total": w_total, "weight_missing": w_missing,
                            "missing_note": g.missing_weight_note(w_missing) if comps else None,
                            "remaining": remaining_of(comps), "links": links,
                            "mode": mode, "pass_fail": mode == "pass_fail",
                            "pf_note": "P/NP 과목 — 학점 추정 안 함" if mode == "pass_fail" else None,
                            "auto": auto, "auto_source": cfg.get("auto_source") if auto else None,
                            "auto_by": cfg.get("auto_by") if auto else None, "auto_changed": changed})
    try:
        prio = g.priorities(prio_in) if prio_in else []
    except ValueError:
        prio = []
    sid = (store.data.get("settings") or {}).get("student_id")
    waiting, unmatched = score_files(store)
    out_cands = []
    for x in cands:
        ln = all_links.get(x.get("key")) or {}
        out_cands.append({**x, "component": ln.get("component"), "link_by": ln.get("by"),
                          "link_reason": ln.get("reason") or ("평가 항목이 아직 없어요" if x.get("course_id") else None),
                          "link_evidence": ln.get("evidence")})
    return {"grades": g.GRADES, "default_dist": g.DEFAULT_DISTRIBUTION, "courses": out_courses,
            "priorities": prio, "candidates": out_cands,
            "student_id_set": bool(sid), "student_id_masked": mask_student_id(sid),
            "score_files_waiting": waiting, "score_files_unmatched": unmatched}


def exams_of(grades_section: dict, items: list | None = None) -> dict:
    """state.exams(계약 14-9): 시험 계열 평가 항목의 다가오는 일정과 날짜 미공지 항목. 지난 시험은 upcoming에서 뺀다.
    items를 주면 평가 항목에 연결되지 않은 다가오는 시험 항목(kind "시험", 제외·대체·겹침 아님 — 예: 평가 항목이 없는
    과목의 기말고사)도 upcoming에 component·weight null로 넣는다(연결된 것과 겹치지 않게). 제외한 과목은 items에 이미 없다."""
    up, na = [], []
    for c in grades_section.get("courses") or []:
        for x in (c.get("config") or {}).get("components") or []:
            if not lk.is_exam_component(str(x.get("name") or "")):
                continue
            row = {"course_id": c["course_id"], "course_short": c.get("short_name"), "component": x["name"],
                   "weight": x.get("weight")}
            if x.get("date_status") == "not_announced":
                na.append(row)
            for s in x.get("schedule") or []:
                if not s["past"]:
                    up.append({**row, "title": s["title"], "due": s["due"], "item_id": s["item_id"], "status": s["status"]})
    seen = {r["item_id"] for r in up}
    now = now_local()
    for it in items or []:
        d = parse_iso(it.get("due"))
        if (it.get("kind") != "시험" or it.get("status") not in SCHEDULE_STATUSES or it.get("component")
                or it["id"] in seen or d is None or d < now):
            continue
        seen.add(it["id"])
        up.append({"course_id": it.get("course_id"), "course_short": it.get("course_short") or it.get("course_name"),
                   "component": None, "weight": None, "title": it.get("title"), "due": it.get("due"),
                   "item_id": it["id"], "status": it.get("status")})
    up.sort(key=lambda r: _ts(r["due"]))
    return {"upcoming": up, "not_announced": na}


# ---------------------------------------------------------------- 사용자 연결·개수(계약 14-6, 14-9)

def _course_component(store, cid, name) -> str | None:
    if not isinstance(name, str):
        return None
    comps = ((store.data.get("grades") or {}).get(str(cid)) or {}).get("components") or []
    for c in comps:
        if lk.norm(c.get("name")) == lk.norm(name):
            return c["name"]
    return None


def set_link(store, course_id, body: dict) -> None:
    """POST /api/grades/<cid>/link {"key", "component": 이름|null} | {"key", "reset": true}. 잘못되면 ValueError."""
    cid = str(course_id)
    key = body.get("key")
    if not isinstance(key, str) or not key.strip() or len(key) > 300:
        raise ValueError("key가 필요합니다")
    links = store.data.setdefault("grade_links", {})
    if body.get("reset"):
        slot = links.get(cid) or {}
        slot.pop(key, None)
        if not slot:
            links.pop(cid, None)
        return
    if "component" not in body:
        raise ValueError("component(이름 또는 null)가 필요합니다")
    comp = body.get("component")
    if comp is not None:
        name = _course_component(store, cid, comp)
        if name is None:
            raise ValueError(f"이 과목에 '{str(comp)[:40]}' 평가 항목이 없습니다")
        comp = name
    links.setdefault(cid, {})[key] = {"component": comp, "at": iso(now_local())}


def set_count(store, course_id, body: dict) -> None:
    """POST /api/grades/<cid>/count {"component": 이름, "n": 정수|null}(null = 지우기). 잘못되면 ValueError."""
    cid = str(course_id)
    name = _course_component(store, cid, body.get("component"))
    if name is None:
        raise ValueError("이 과목에 그 평가 항목이 없습니다")
    n = body.get("n")
    counts = store.data.setdefault("grade_counts", {})
    if n is None:
        slot = counts.get(cid) or {}
        slot.pop(lk.norm(name), None)
        if not slot:
            counts.pop(cid, None)
        return
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= lk.MAX_COUNT:
        raise ValueError(f"개수는 1~{lk.MAX_COUNT} 사이의 정수여야 합니다")
    counts.setdefault(cid, {})[lk.norm(name)] = {"n": n, "at": iso(now_local())}


def set_item_component(store, item_id: str, body: dict) -> None:
    """POST /api/item/<id>/component {"component": 이름|null} → store.data["item_links"][item_id](그룹 키도 함께 남겨
    마감이 바뀌어 id가 달라진 같은 과제 후보에도 이어진다). {"reset": true}면 사용자 연결을 지워 규칙·AI가 다시 정한다
    (같은 그룹 키로 남은 옛 기록도 함께). 항목이 없으면 KeyError, 잘못되면 ValueError."""
    it = next((x for x in store.data.get("items") or [] if x.get("id") == item_id), None)
    if it is None:
        raise KeyError(item_id)
    view = next((v for v in store.view(include_hidden=True) if v["id"] == item_id), it)
    if body.get("reset"):
        links = store.data.get("item_links") or {}
        gk = group_key(view)
        for k in [k for k, v in links.items() if k == item_id or (gk and isinstance(v, dict) and v.get("group") == gk)]:
            links.pop(k, None)
        return
    if "component" not in body:
        raise ValueError("component(이름 또는 null)가 필요합니다")
    comp = body.get("component")
    if comp is not None:
        name = _course_component(store, it.get("course_id"), comp)
        if name is None:
            raise ValueError(f"이 과목에 '{str(comp)[:40]}' 평가 항목이 없습니다")
        comp = name
    store.data.setdefault("item_links", {})[item_id] = {"component": comp, "at": iso(now_local()),
                                                         "group": group_key(view)}


# ---------------------------------------------------------------- AI(동기화 뒤 자동 단계, 계약 14-2·14-4)

def _ask_again(entry) -> bool:
    """AI 기억이 없거나, 실패한 지 AI_RETRY_HOURS가 지났으면 다시 묻는다."""
    if not isinstance(entry, dict):
        return True
    if not entry.get("error"):
        return False
    at = parse_iso(entry.get("at"))
    return at is None or now_local() - at >= timedelta(hours=AI_RETRY_HOURS)


def ai_links(store, session) -> dict:
    """규칙이 못 정한 점수 후보·마감 항목의 평가 항목(map_score_item)과, 명시·단서가 없는 항목의 개수(count_items)를
    AI에 묻는다(settings.ai_cli가 켜져 있을 때 core._after_sync가 같은 AiSession으로 부른다). 제외한 과목은 묻지 않는다.
    결과는 grade_links_ai[cid][key]·grade_counts_ai[cid][이름]에 기억해 같은 것은 다시 묻지 않는다(실패는 12시간 뒤)."""
    out = {"asked": 0, "linked": 0, "counted": 0, "error": None}
    if session is None:
        return out
    saved = store.data.get("grades") or {}
    hidden = hidden_courses(store.data)
    cands = candidates(store)
    view = store.view()
    sem = _core.semester_info(store.data.get("cache"), view)
    _core.annotate(view, sem, store.data.get("cache"))
    link_items(store, view, sem)
    texts = _course_texts(store)
    now = iso(now_local())
    for cid, cfg in sorted(saved.items()):
        if cid in hidden or not (cfg or {}).get("components"):
            continue
        comps = cfg["components"]
        names = [str(c.get("name")) for c in comps if c.get("name")]
        mine = [c for c in cands if str(c.get("course_id")) == cid]
        links = link_candidates(store, cid, comps, mine)
        remembered = (store.data.get("grade_links_ai") or {}).get(cid) or {}
        things = []
        for c in mine:
            ln = links.get(c["key"]) or {}
            if c.get("score") is None or ln.get("component") or ln.get("by") == "user":
                continue
            if _ask_again(remembered.get(c["key"])):
                things.append({"key": c["key"], "label": c.get("label") or "", "title": c.get("source_title") or "",
                               "text": c.get("sentence") or ""})
        for it in view:
            if (str(it.get("course_id")) != cid or it.get("component") or it.get("component_by") == "user"
                    or it.get("status") not in SCHEDULE_STATUSES or not lk.families(it.get("title"))):
                continue
            k = "item:" + it["id"]
            if _ask_again(remembered.get(k)):
                ev = " / ".join(str(e.get("text") or "") for e in (it.get("evidence") or [])[:2])
                things.append({"key": k, "label": it.get("title") or "", "title": it.get("kind") or "", "text": ev})
        things = things[:AI_LINK_MAX]
        if things:
            res = session.run("map_score_item", {"components": names, "things": things})
            if res.attempts == 0 and not res.ok:
                out["error"] = res.error   # 한도·꺼짐·CLI 없음: 기억하지 않는다
                return out
            out["asked"] += len(things)
            slot = store.data.setdefault("grade_links_ai", {}).setdefault(cid, {})
            if res.ok:
                got = {d["key"]: d for d in (res.data or {}).get("links") or []}
                for t in things:
                    d = got.get(t["key"]) or {}
                    comp = d.get("component")
                    slot[t["key"]] = {"component": comp, "evidence": d.get("evidence") if comp else None,
                                      "reason": (f"AI가 연결함 — 근거: \"{str(d.get('evidence') or '')[:120]}\""
                                                 if comp else None), "at": now, "error": None}
                    out["linked"] += bool(comp)
            else:
                out["error"] = res.error
                for t in things:
                    slot[t["key"]] = {"component": None, "at": now, "error": res.error}
        lcomps = _lcomps(cfg)
        obs = _observations(store, cid, mine, view)
        for lc in lcomps:
            cnt = _count_for(store, cid, lc, lcomps, obs, texts.get(cid, []))
            if not cnt or not (cnt["basis"] == "observed" or cnt["by"] == "ai") or not cnt.get("family"):
                continue
            lines = lk.count_lines([(t, x) for t, x, _ in texts.get(cid, [])], cnt["family"])
            if not lines:
                continue
            h = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()[:16]
            prev = ((store.data.get("grade_counts_ai") or {}).get(cid) or {}).get(lk.norm(lc.name))
            if isinstance(prev, dict) and prev.get("hash") == h and not _ask_again(prev):
                continue
            res = session.run("count_items", {"component": lc.name, "family": cnt["family"], "lines": lines,
                                              "low": cnt.get("low")})
            if res.attempts == 0 and not res.ok:
                out["error"] = res.error
                return out
            data = res.data or {}
            store.data.setdefault("grade_counts_ai", {}).setdefault(cid, {})[lk.norm(lc.name)] = {
                "n": data.get("count") if res.ok else None, "evidence": data.get("evidence") if res.ok else None,
                "source_title": None, "hash": h, "at": now, "error": None if res.ok else res.error}
            out["counted"] += bool(res.ok and data.get("count"))
            if not res.ok:
                out["error"] = res.error
    return out
