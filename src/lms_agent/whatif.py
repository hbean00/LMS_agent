"""성적 '만약에'(계약 22-3) — 채팅 질문을 규칙으로 알아듣고 성적 화면과 같은 계산으로만 답한다.

- 알아듣는 질문: "기말에서 몇 점 받아야 A?"·"A 받으려면?"(needed), "LAB 하나 0점이면?"·"중간 70점이면 학점은?"(scenario),
  "P 기준 넘었어?"(P/NP 과목 → pass_fail), "지금 학점 몇이야?"(current). 과목을 못 정하면 ask_course.
- 숫자는 성적 화면(features/grades)과 같은 길 — store.view() → core.semester_info·annotate → gradebook.link_items →
  course_modes.annotate_courses → gradebook.section — 의 과목 설정(평가 항목·세부 점수·개수)과
  grades.estimate / estimate_absolute / needed_on 결과만 쓴다. 가정 점수는 expand_components 결과의 복사본에만 넣고
  저장하지 않는다. AI·네트워크를 쓰지 않는다(성적은 이 PC 밖으로 나가지 않음).
- "A"는 A0 이상으로 본다(A+는 A+). 그렇게 본 것과 기본값·추정·가정은 basis에 적는다.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime

from . import distribution as dm
from . import grades as g
from . import links as lk

# ---------------------------------------------------------------- 질문 낱말

# 놓친 것 신고("안 보여요", "성적이 안 들어와요")는 다음 갈래(missing)가 맡는다
_MISSING = re.compile(r"안\s*(?:보여|보이|들어와|들어오|들어옴|들어가|나와|나왔|나옴|떠|뜨|잡혀|잡히|올라와|올라오|반영)|"
                      r"반영\s*(?:이|가|은|는)?\s*안|빠졌|빠진\s*것|누락|없어요|놓친|못\s*찾|이상해|이상하|틀렸|틀린\s*것|"
                      r"잘못\s*(?:나|들어|됐|되)|문제가\s*있|오류|버그")
_PARTICLE = (r"(?=$|[\s?!.,~)\]]|(?:을|를|이|가|은|는|도|만|로|으로|이상|정도|받|맞|나|뜨|떠|면|야|이야|에|까지|라|랑|"
             r"학점|목표|가능|되|돼|안|쯤))")
_LETTER = re.compile(r"(?<![A-Za-z0-9+])(?P<l>[A-D])[ \t]?(?P<m>\+|0|제로)?" + _PARTICLE)
_LETTER_KO = re.compile(r"(?P<l>에이|비|씨|디)\s?(?P<m>쁠|플|뿔|제로)")
_KO_L = {"에이": "A", "비": "B", "씨": "C", "디": "D"}
_F_AVOID = re.compile(r"(?<![A-Za-z])F\s*(?:만|를|은|는|학점)?\s*(?:안\s*(?:받|맞|나|뜨)|면하|면할|피하|피할)|과락\s*(?:안|면|피)|"
                      r"낙제\s*(?:만)?\s*(?:안|면|피)")
_PF = re.compile(r"P\s*/\s*N\s*P|(?<![A-Za-z])PNP(?![A-Za-z])|(?<![A-Za-z])N?P(?![A-Za-z/+0-9])"
                 r"(?=\s*(?:기준|를|을|가|이|는|은|받|넘|이야|야|돼|될|되|가능|나와|나올|뜨|떠|맞|[?!.]|$))|패스|논\s*패스")
_PF_EN = re.compile(r"(?<![a-z])(?:non-?)?pass(?:ed|ing)?(?![a-z])|pass\s*/\s*fail", re.I)
_NEED_VERB = re.compile(r"받아야|받으려면|받으면\s*(?:돼|되)|받을\s*(?:수|까|려면)|맞아야|맞으려면|맞을\s*(?:수|까)|나와야|나오려면|"
                        r"나올\s*(?:수|까|려면)|넘으려면|넘어야|넘기려면|되려면|될\s*(?:수|까|려면)|되나|돼\s*\?|가능|이면\s*(?:돼|되)|"
                        r"이상|필요|해야|노려|목표|어떻게\s*해|(?<![a-z])(?:need|needed|required|possible)(?![a-z])", re.I)
_HOW_MANY = re.compile(r"몇\s*(?:점|%|퍼센트|퍼|프로)|점수\s*(?:가|는|를)?\s*(?:얼마|몇)|how\s+(?:much|many)|what\s+score", re.I)
_CURRENT = re.compile(r"(?:지금|현재|예상|추정|내)\s*(?:학점|성적|등급)|학점\s*(?:이|은|는)?\s*(?:몇|어때|어떻|뭐|어떤|예상|나와|나올|"
                      r"어케|얼마)|몇\s*학점|성적\s*(?:이|은|는)?\s*(?:어때|어떻|어케|괜찮|몇)|등수|석차|상위\s*몇|"
                      r"(?:반에서|내)\s*위치|위치\s*(?:가|는)?\s*(?:어디|어때)|my\s+grade|grade\s+estimate", re.I)
_COND = re.compile(r"(?<!려)면(?![가-힣])|만약|가정|(?<![a-z])if(?![a-z])|친다면|치고", re.I)
_PREMISE = re.compile(r"인데|는데|은데|이고|이니까|니까|라서|받았|맞았|나왔|이었|였")
_NUM = r"\d{1,3}(?:\.\d+)?"
_SCORE = re.compile(
    rf"(?<![A-Za-z\d.])(?P<mx1>{_NUM})\s*점\s*만점\s*(?:에서|에|중|기준)?\s*(?P<v1>{_NUM})\s*점"
    rf"|(?<![A-Za-z\d.])(?P<v2>{_NUM})\s*/\s*(?P<mx2>{_NUM})(?:\s*점)?"
    rf"|(?<![A-Za-z\d.])(?P<v3>{_NUM})\s*(?:%|퍼센트|퍼|프로)"
    rf"|(?<![A-Za-z\d.])(?P<v4>{_NUM})\s*점(?!\s*만점)"
    r"|(?P<full>만점)"
    r"|(?P<zero>빵\s*점|영\s*점|망치면|망하면|망쳐|안\s*내면|못\s*내면|미제출|제출\s*(?:안|못)\s*하면|빼먹으면|결시|"
    r"안\s*보면|못\s*보면|안\s*치면|못\s*치면)")
_NOT_SCORE_BEFORE = re.compile(r"(?:상위|하위|평균|표준\s*편차|표편|비중|비율|반영)\s*(?:이|가|은|는)?\s*$")
_COUNT = re.compile(r"(?P<n>\d{1,2})\s*(?:개|번|회)|(?P<w>하나|한\s*개|한\s*번|둘|두\s*개|두\s*번|셋|세\s*개|세\s*번|넷|네\s*개|"
                    r"네\s*번|다섯\s*개)|(?P<all>(?<![가-힣])다(?=\s|$|[?!.,])|전부|모두|싹\s*다|남은\s*(?:거|것)\s*(?:다|전부|모두)?)")
_COUNT_W = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4, "다섯": 5}
_FAM_RX = [(f, re.compile(rx, re.I)) for f, rx in lk.FAMILY_RX.items()]
_NUM_AFTER = re.compile(r"[ \t]*#?[ \t]*(\d{1,2})(?![\d.])(?!\s*(?:점|%|/|개|번|회|퍼|프로))")
_OUTCOME = re.compile(r"에서 약 [\d.]+/|만점을 받아도|0점 받아도")
_ABBR_STOP = {"하나", "지금", "현재", "만약", "가정", "기준", "학점", "성적", "점수", "목표", "정도", "이상", "얼마", "어때",
              "몇이야", "받으려면", "받아야", "넘었어", "패스", "예상", "추정", "가능", "가능해", "필요", "전부", "모두"}


def _n(x) -> str:
    """화면 숫자: 소수 첫째 자리까지('82.69' → '82.7', '100.0' → '100')."""
    return f"{round(float(x), 1):g}"


def _j(word: str, pair: tuple[str, str]) -> str:
    """받침에 맞는 조사: _j('ASIC설계(영강)', ('은', '는')) → '은'. 한글·숫자가 아니면 '은(는)'."""
    s = re.sub(r"[\s)\]}\"'.]+$", "", str(word or ""))
    if re.fullmatch(r"[A-DF][+0]?", s):   # 학점: 에이·에이플러스·에이제로 … 모두 받침 없음
        return pair[1]
    ch = s[-1:]
    if "가" <= ch <= "힣":
        return pair[0] if (ord(ch) - 0xAC00) % 28 else pair[1]
    if ch.isdigit():
        return pair[0] if ch in "013678" else pair[1]
    return f"{pair[0]}({pair[1]})"


def _dedupe(xs) -> list[str]:
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


# ---------------------------------------------------------------- 질문 읽기

def _target(msg: str) -> tuple[str | None, str | None]:
    """목표 학점과 그렇게 본 이유(basis). 'A' → A0, 'A+' → A+, 'F만 면하려면' → "PASS_LOWEST"(과목의 가장 낮은 통과 학점 —
    학점 비율·기준표를 본 뒤 _resolve_target이 정함)."""
    if _F_AVOID.search(msg):
        return "PASS_LOWEST", None
    m = _LETTER.search(msg)
    if m:
        l, mod = m.group("l"), m.group("m")
        if mod == "+":
            return l + "+", None
        if mod:
            return l + "0", None
        return l + "0", f"'{l}'는 {l}0 이상으로 봤어요({l}+를 원하면 '{l}+'라고 적어 주세요)"
    m = _LETTER_KO.search(msg)
    if m:
        l = _KO_L[m.group("l")]
        return (l + "+" if m.group("m") in ("쁠", "플", "뿔") else l + "0"), None
    return None, None


def _groups(msg: str) -> list[dict]:
    """평가 낱말 덩어리: '중간고사'·'기말 프로젝트'·'Final quiz'·'LAB4' → {start, end, fams, number, text}."""
    spans = sorted((m.start(), m.end(), f) for f, rx in _FAM_RX for m in rx.finditer(msg))
    out: list[dict] = []
    for a, b, f in spans:
        if out and a <= out[-1]["end"] + 2 and re.fullmatch(r"[\s·_\-]*", msg[out[-1]["end"]:a] if a > out[-1]["end"] else ""):
            out[-1]["end"] = max(out[-1]["end"], b)
            out[-1]["fams"].add(f)
        else:
            out.append({"start": a, "end": b, "fams": {f}})
    for gr in out:
        m = _NUM_AFTER.match(msg, gr["end"])
        gr["number"] = int(m.group(1)) if m else None
        if m:
            gr["end"] = m.end()
        gr["text"] = msg[gr["start"]:gr["end"]].strip()
    return out


def _scores(msg: str) -> list[dict]:
    out = []
    for m in _SCORE.finditer(msg):
        if _NOT_SCORE_BEFORE.search(msg[max(0, m.start() - 8):m.start()]):
            continue
        if m.group("v1") is not None:
            spec = {"kind": "frac", "v": float(m.group("v1")), "mx": float(m.group("mx1"))}
        elif m.group("v2") is not None:
            spec = {"kind": "frac", "v": float(m.group("v2")), "mx": float(m.group("mx2"))}
        elif m.group("v3") is not None:
            spec = {"kind": "pct", "v": float(m.group("v3"))}
        elif m.group("v4") is not None:
            spec = {"kind": "points", "v": float(m.group("v4"))}
        elif m.group("full"):
            spec = {"kind": "full"}
        else:
            spec = {"kind": "zero", "word": re.sub(r"\s+", " ", m.group("zero"))}
        out.append({**spec, "start": m.start(), "end": m.end(), "raw": m.group(0)})
    return out


def _count(gap: str):
    """평가 낱말과 점수 사이의 개수: '하나' → 1, '두 개' → 2, '전부' → 'all', 없으면 None."""
    m = _COUNT.search(gap or "")
    if not m:
        return None
    if m.group("n"):
        return int(m.group("n")) or None
    if m.group("w"):
        return _COUNT_W.get(re.split(r"\s", m.group("w"))[0].rstrip("개번"), 1)
    return "all"


def _pair(msg: str, groups: list[dict], scores: list[dict]) -> list[dict]:
    """점수마다 바로 앞(없으면 뒤)의 평가 낱말을 붙인다 → [{group|None, count, spec}]."""
    out = []
    for s in scores:
        prev = [gr for gr in groups if gr["end"] <= s["start"]]
        grp = prev[-1] if prev else next((gr for gr in groups if gr["start"] >= s["end"]), None)
        gap = msg[grp["end"]:s["start"]] if grp is not None and grp["end"] <= s["start"] else ""
        out.append({"group": grp, "count": _count(gap), "spec": s})
    return out


def parse(message: str) -> dict | None:
    """질문 → {"intent": "needed"|"scenario"|"pass_fail"|"current", "target", "target_note", "groups", "assumptions", "pf"}.
    성적 '만약에' 질문이 아니면 None."""
    msg = re.sub(r"\s+", " ", str(message or "")).strip()
    if not msg or _MISSING.search(msg):
        return None
    target, tnote = _target(msg)
    groups = _groups(msg)
    scores = _scores(msg)
    how_many, need_verb = bool(_HOW_MANY.search(msg)), bool(_NEED_VERB.search(msg))
    cond = bool(_COND.search(msg))
    premise = bool(_PREMISE.search(msg)) and how_many
    assumptions = _pair(msg, groups, scores) if scores and (cond or premise) else []
    pf = bool(_PF.search(msg) or _PF_EN.search(msg))
    if how_many and (target or need_verb):
        intent = "needed"
    elif assumptions:
        intent = "scenario"
    elif target and need_verb:
        intent = "needed"
    elif pf:
        intent = "pass_fail"
    elif _CURRENT.search(msg):
        intent = "current"
    else:
        return None
    used = {id(a["group"]) for a in assumptions if a["group"] is not None}
    return {"intent": intent, "target": target, "target_note": tnote, "message": msg,
            "groups": [gr for gr in groups if id(gr) not in used], "assumptions": assumptions, "pf": pf}


# ---------------------------------------------------------------- 성적 화면과 같은 계산 재료

def _context(store, now=None) -> tuple[dict, list[dict]]:
    """features/core·features/grades와 같은 길로 state.grades를 만든다(저장하지 않음). → (grades 묶음, cache 과목들)."""
    from . import core, course_modes, gradebook
    cache = store.data.get("cache")
    its = store.view()
    today = now.date() if isinstance(now, datetime) else now if isinstance(now, date) else None
    sem = core.semester_info(cache, its, today=today) if today else core.semester_info(cache, its)
    core.annotate(its, sem, cache)
    gradebook.link_items(store, its, sem)
    courses = course_modes.annotate_courses(core.course_summaries(cache), store.data)
    return gradebook.section(store, courses, items=its, sem=sem), list((cache or {}).get("courses") or [])


def _has_data(c: dict) -> bool:
    return bool((c.get("config") or {}).get("components"))


def _subseq(t: str, s: str) -> bool:
    it = iter(s)
    return all(ch in it for ch in t)


def _abbrev(msg: str, pool: list[dict]) -> list[str]:
    """줄임말 과목 이름: '로공개' → 로봇공학개론, '디신처' → 디지털신호처리(첫 글자가 같고 글자 순서대로 들어 있을 때)."""
    from .core import short_course_name
    toks = []
    for t in re.findall(r"[가-힣]{2,6}", msg):
        t = re.sub(r"(?:에서|으로|이랑|하고|은|는|이|가|을|를|에|의|도|만|랑|로)$", "", t) if len(t) > 2 else t
        if (len(t) >= 2 and t not in _ABBR_STOP and not lk.families(t) and not _NEED_VERB.search(t)
                and not _CURRENT.search(t) and not _COND.search(t + " ")):
            toks.append(t)
    out = []
    for c in pool:
        name = re.sub(r"[^가-힣]", "", short_course_name(c.get("name")))
        if any(len(name) > len(t) and name[0] == t[0] and t not in name and _subseq(t, name) for t in toks):
            out.append(str(c["id"]))
    return out


def _pick(q: dict, course_id, sec_courses: list[dict], cache_courses: list[dict]):
    """(과목, None) | (None, [되물을 과목…]). 질문에 나온 과목 > 대화의 course_id > (P/NP 질문이면 P/NP 과목 하나) >
    성적이 있는 과목 하나."""
    from .agent import mentioned_courses
    by_id = {str(c["course_id"]): c for c in sec_courses}
    pool = [c for c in cache_courses if str(c.get("id")) in by_id]
    ids = [i for i in mentioned_courses(q["message"], pool) if i in by_id]
    if not ids:
        ids = [i for i in _abbrev(q["message"], pool) if i in by_id]
    ids = list(dict.fromkeys(ids))
    if len(ids) == 1:
        return by_id[ids[0]], None
    if len(ids) > 1:
        return None, [by_id[i] for i in ids]
    if course_id is not None and str(course_id) in by_id:
        return by_id[str(course_id)], None
    if q["pf"]:
        pf = [c for c in sec_courses if c.get("mode") == "pass_fail"]
        if len(pf) == 1:
            return pf[0], None
    have = [c for c in sec_courses if _has_data(c)]
    if not q["pf"]:   # 학점을 묻는 질문이면 P/NP 과목은 후보에서 뒤로(성적 있는 일반 과목이 있을 때)
        normal = [c for c in have if c.get("mode") != "pass_fail"]
        have = normal or have
    if len(have) == 1:
        return have[0], None
    return None, have


def _eff(c: dict) -> dict:
    return dm.effective(c.get("config") or {})   # 사용자 비율 > 강의계획서 > 기본값 — section과 같은 함수


def _dist_basis(c: dict) -> list[str]:
    ds = c.get("dist_source") or {}
    by = c.get("letter_basis")
    if by == "user":
        return ["학점 비율은 직접 넣은 값이에요"]
    if by == "syllabus":
        where = ds.get("source_title") or "강의 글"
        if ds.get("kind") == "absolute":
            return [f"학점 기준은 '{where}'에서 찾은 절대평가 기준이에요({ds.get('summary') or ''})"]
        tail = " — 범위는 가운데 값으로 계산" if ds.get("ranges") else ""
        return [f"학점 비율은 '{where}'에서 찾은 값이에요({ds.get('summary') or ''}){tail}"]
    body = " · ".join(f"{k} {v:g}" for k, v in g.DEFAULT_DISTRIBUTION.items() if v)
    return [f"학점 비율을 몰라 기본값({body}%)으로 어림했어요 — 강의계획서 비율을 넣으면 더 정확해요"]


def _count_basis(c: dict) -> list[str]:
    from . import gradebook
    fn = getattr(gradebook, "_count_notes", None)
    return fn((c.get("config") or {}).get("components") or []) if fn else []


def _est_value(est: dict) -> str:
    if est.get("basis") == "absolute":
        return f"{est['grade']} · 예상 총점 {_n(est['expected_total'])}점"
    return f"{est['grade']} · 상위 {_n(est['top'])}%"


def _est_range(est: dict) -> str | None:
    if est.get("basis") == "absolute":
        lo, hi = est.get("total_range") or (None, None)
        if lo is None or hi - lo < 0.05:
            return None
        best, worst = est["grade_range"]
        return f"남은 평가 0점~만점이면 총점 {_n(lo)}~{_n(hi)}점({worst}~{best})"
    lo, hi = est.get("range") or (None, None)
    if lo is None:
        return None
    a, b = est.get("grade_range") or (est["grade"], est["grade"])
    grades = a if a == b else f"{a}~{b}"
    return f"평가 항목끼리의 상관 가정(ρ 0.3~0.9)에 따라 상위 {_n(lo)}~{_n(hi)}%({grades})"


def _label(e: dict) -> str:
    return f"{e['name']} ({_n(e['weight'])}%)"


def _need_value(e: dict, r: dict, target: str, top: bool = True) -> str:
    """needed_on 결과 한 줄: '약 69.8/100점 이상 (상위 55.8%)' · 개수로 나눈 남은 몫이면 '평균 만점의 91.8% 이상'."""
    if not r.get("possible"):
        return f"만점을 받아도 {target}{_j(target, ('은', '는'))} 어려워요"
    if r.get("already"):
        return f"0점이어도 {target} 이상"
    if e.get("count_left") and e.get("max_score") is None:
        v = f"평균 만점의 {_n(r['need_score'])}% 이상"
    else:
        v = f"약 {_n(r['need_score'])}/{_n(e.get('max_score') or 100)}점 이상"
    if top and r.get("need_top") is not None:
        v += f" (상위 {_n(r['need_top'])}%)"
    return v


def _result(c: dict | None, kind: str, answer: str, target=None, component=None, lines=(), rows=(), basis=()) -> dict:
    return {"answer": answer,
            "whatif": {"course_id": str(c["course_id"]) if c else None, "course_short": c.get("short_name") if c else None,
                       "kind": kind, "target": target, "component": component,
                       "lines": _dedupe(lines), "rows": list(rows), "basis": _dedupe(basis)[:14]}}


def _cannot(c: dict, reason: str, q: dict, extra_lines=(), basis=(), mode: str = "data") -> dict:
    """계산할 수 없을 때: kind current + 이유(계약 22-3). mode: "data"(점수·비중이 모자람) | "ask"(어느 항목인지·점수
    모양을 되묻기) | "info"(계산해도 달라지지 않는 이유만)."""
    from . import gradebook
    short = c["short_name"]
    rows = [{"label": r["component"], "value": (f"남은 비중 {_n(r['weight'])}%" if r["weight"] is not None
                                                else f"남은 비중 {_n(r['range'][0])}~{_n(r['range'][1])}%")}
            for r in gradebook.remaining_of((c.get("config") or {}).get("components") or [])]
    reason = reason.rstrip(". ")
    if mode == "ask":
        answer = f"{short}: {reason}." + ("" if reason.endswith("주세요") else " 다시 물어봐 주세요.")
    elif mode == "info":
        answer = f"{short}: {reason}."
    else:
        answer = (f"{short}{_j(short, ('은', '는'))} 아직 계산할 수 없어요: {reason}. "
                  "성적 화면에서 점수·평균·표준편차나 비중을 넣으면 다시 물어봐 주세요.")
    return _result(c, "current", answer, target=q.get("target") if q.get("target") in g.GRADES else None,
                   lines=list(extra_lines) + [("물어볼 것: " if mode == "ask" else "계산할 수 없는 이유: ") + reason],
                   rows=rows,
                   basis=list(basis) + _dist_basis(c))


# ---------------------------------------------------------------- 평가 항목 고르기·가정 점수 넣기

def _lcomps(c: dict) -> list:
    cfg = c.get("config") or {}
    return lk.components_of(cfg.get("components") or [], [cfg.get("auto_source")])   # gradebook과 같은 근거 문장


def _names_for(c: dict, grp: dict) -> list[str]:
    m = lk.match(grp["text"], _lcomps(c))
    return [m.component] if m.component else list(m.ambiguous)


def _missing(entries: list[dict]) -> list[dict]:
    # section이 필요 점수를 구하는 남은 항목과 같은 조건
    return [e for e in entries if e["score"] is None and not e["fixed"] and e["weight"] > 0]


def _owner(e: dict) -> str:
    return e.get("parent") or e["name"]


def _known_frac(entries: list[dict]) -> tuple[float, float] | None:
    """needed_on과 같은 가정: 평균·표준편차가 없는 항목은 이미 본 항목들의 비중 가중 평균(만점 대비 비율)."""
    known = [e for e in entries if not e["fixed"] and e["score"] is not None and e.get("mean") is not None
             and e.get("sd") is not None and e["sd"] > 0 and e["weight"] > 0]
    wsum = sum(e["weight"] for e in known)
    if not known or wsum <= 0:
        return None
    mx = lambda e: float(e.get("max_score") or 100)  # noqa: E731
    return (sum(e["weight"] * e["mean"] / mx(e) for e in known) / wsum,
            sum(e["weight"] * e["sd"] / mx(e) for e in known) / wsum)


def _score_in(spec: dict, mx: float, name: str) -> tuple[float | None, str | None, str | None]:
    """(점수(만점 mx 기준), basis 메모, 오류)."""
    k = spec["kind"]
    if k == "full":
        return mx, None, None
    if k == "zero":
        return 0.0, f"'{spec['word']}'{_j(spec['word'], ('은', '는'))} 0점으로 봤어요", None
    if k == "pct":
        if spec["v"] > 100:
            return None, None, f"{_n(spec['v'])}%는 점수로 넣을 수 없어요"
        return spec["v"] / 100 * mx, None, None
    if k == "frac":
        if spec["mx"] <= 0 or spec["v"] > spec["mx"]:
            return None, None, f"'{spec['raw']}'{_j(spec['raw'], ('은', '는'))} 점수로 읽을 수 없어요"
        return spec["v"] / spec["mx"] * mx, None, None
    if spec["v"] > mx + 1e-9:
        ex = _n(round(mx * 0.8))
        return None, None, (f"{name} 만점이 {_n(mx)}점이라 {_n(spec['v'])}점은 넣을 수 없어요 — "
                            f"'{ex}/{_n(mx)}'{_j(ex, ('이나', '나'))} '80%'처럼 적어 주세요")
    return spec["v"], None, None


def _apply(c: dict, expanded: list[dict], assumptions: list[dict]) -> dict:
    """가정 점수를 expand_components 결과의 복사본에 넣는다(저장 안 함).
    → {"hyp", "lines", "phrases", "basis", "names", "error", "mode"} — error가 있으면 계산하지 않는다(mode는 _cannot)."""
    from . import gradebook
    cfg = c.get("config") or {}
    comps = cfg.get("components") or []
    cutoffs = _eff(c)["cutoffs"]
    out = {"hyp": copy.deepcopy(expanded), "lines": [], "phrases": [], "names": [], "error": None, "mode": "data",
           "basis": ["가정한 점수는 질문에 적힌 값이에요(저장하지 않음)"]}
    hyp, basis = out["hyp"], out["basis"]
    frac = _known_frac(expanded)

    def fail(msg: str, mode: str = "data") -> dict:
        out.update(error=msg, mode=mode)
        return out

    for a in assumptions:
        grp = a["group"]
        if grp is not None:
            cand = _names_for(c, grp)
            if not cand:
                have = ", ".join(x["name"] for x in comps)
                return fail(f"'{grp['text']}'에 맞는 평가 항목을 못 찾았어요(항목: {have})", "ask")
            left = [n for n in cand if any(_owner(e) == n for e in _missing(hyp))]
            if len(cand) > 1 and len(left) == 1:
                cand = left
            if len(cand) > 1:
                return fail(f"'{grp['text']}'{_j(grp['text'], ('이', '가'))} {', '.join(cand)} 중 어느 것인지 알려 주세요",
                            "ask")
            name = cand[0]
        else:
            left = list(dict.fromkeys(_owner(e) for e in _missing(hyp)))
            if len(left) != 1:
                return fail("어느 평가 항목의 점수인지 알려 주세요(예: '기말 80점이면?')", "ask")
            name = left[0]
        comp = next((x for x in comps if x.get("name") == name), {})
        idx = [i for i, e in enumerate(hyp) if _owner(e) == name]
        rem = [i for i in idx if hyp[i]["score"] is None and not hyp[i]["fixed"]]
        members = [i for i in idx if hyp[i]["score"] is not None and hyp[i]["name"] != name]
        whole = [i for i in idx if hyp[i]["name"] == name]
        count = a["count"]
        target_i, split = None, None
        if grp is not None and grp.get("number") is not None and members:   # 'Lab0 40점이면' → 받은 Lab0 점수를 바꿈
            fams = [f for f in grp["fams"] if f in lk.COUNTABLE] or list(lk.COUNTABLE)
            hit = [i for i in members
                   if any(grp["number"] in lk.numbered(hyp[i]["name"].split(" · ", 1)[-1], f) for f in fams)]
            if hit:
                target_i = hit[0]
        if target_i is None and (comp.get("fixed") or (whole and hyp[whole[0]]["fixed"])):
            if not cutoffs:
                return fail(f"{name}{_j(name, ('은', '는'))} 모두 비슷하게 받는 항목이라 반에서의 위치 계산에 넣지 않아요"
                            "(가정해도 추정이 바뀌지 않아요)", "info")
            target_i = whole[0] if whole else None
        if target_i is None and rem:
            r = hyp[rem[0]]
            n_left = r.get("count_left")
            if n_left is None and count is not None:
                n_known = gradebook._known_n(comp) if hasattr(gradebook, "_known_n") else None
                n_left = n_known if n_known and n_known >= 1 else None
                if n_left is None and count != "all":
                    return fail(f"{name}{_j(name, ('이', '가'))} 몇 개인지 몰라 하나의 비중을 알 수 없어요 — "
                                "성적 화면에서 개수를 넣어 주세요")
            if n_left:
                k = n_left if count in (None, "all") else int(count)
                if k > n_left:
                    basis.append(f"남은 {name}{_j(name, ('은', '는'))} {n_left}개라 {n_left}개로 계산했어요")
                    k = n_left
                if count is None:
                    basis.append(f"개수를 말하지 않아 남은 {name} {n_left}개 모두 그 점수로 봤어요")
                split = (rem[0], k, n_left)
            else:
                target_i = rem[0]
        if target_i is None and split is None:
            if whole and not members:
                target_i = whole[0]
                if hyp[target_i].get("parent") and count not in (None, "all"):
                    return fail(f"{name}{_j(name, ('이', '가'))} 몇 개인지 몰라 하나의 비중을 알 수 없어요 — "
                                "성적 화면에서 개수를 넣어 주세요")
                if count not in (None, "all"):
                    basis.append(f"{name}{_j(name, ('은', '는'))} 점수가 하나로 들어 있어 {count}개가 아니라 "
                                 f"{name} 전체를 그 점수로 봤어요")
            else:
                return fail(f"{name}에는 남은 점수가 없어요(받은 {len(members)}개가 모두 들어 있어요) — "
                            "'Lab0 40점이면'처럼 번호로 적으면 그 점수를 바꿔 계산해요", "ask")
        if split is not None:
            ri, k, n_left = split
            r = hyp[ri]
            maxes = {float(hyp[i].get("max_score") or 100) for i in members}
            mx = maxes.pop() if len(maxes) == 1 else float(r.get("max_score") or 100)
            score, note, err = _score_in(a["spec"], mx, name)
            if err:
                return fail(err, "ask")
            w_one = r["weight"] / n_left
            e = {"name": f"{name} 가정 {k}개", "weight": w_one * k, "score": score, "max_score": mx,
                 "mean": None, "sd": None, "fixed": False, "parent": name}
            if k < n_left:
                r.update(name=f"{name} 남은 {n_left - k}개", weight=w_one * (n_left - k), count_left=n_left - k)
                hyp.insert(ri, e)
            else:
                hyp[ri] = e
            out["lines"].append(f"가정: {name} {k}개 {_n(score)}/{_n(mx)}점 (비중 {_n(e['weight'])}%)")
            out["phrases"].append(f"{name} {k}개를 {_n(score)}/{_n(mx)}점")
        else:
            e = hyp[target_i]
            mx = float(e.get("max_score") or 100)
            score, note, err = _score_in(a["spec"], mx, e["name"])
            if err:
                return fail(err, "ask")
            if e["score"] is not None:
                basis.append(f"{e['name']}에 들어 있는 {_n(e['score'])}점 대신 {_n(score)}점으로 계산했어요")
            e.update(score=score, max_score=mx, fixed=False)
            out["lines"].append(f"가정: {e['name']} {_n(score)}/{_n(mx)}점 (비중 {_n(e['weight'])}%)")
            out["phrases"].append(f"{e['name']}{_j(e['name'], ('을', '를'))} {_n(score)}/{_n(mx)}점")
        if note:
            basis.append(note)
        if not cutoffs and (e.get("mean") is None or e.get("sd") is None):
            if frac is None:
                return fail("평균·표준편차가 있는 점수가 아직 없어 가정 점수로 반에서의 위치를 어림할 수 없어요")
            e["mean"], e["sd"] = frac[0] * e["max_score"], frac[1] * e["max_score"]
            basis.append(f"{e['name']}의 평균·표준편차는 아직 몰라 이미 본 항목들의 비중 가중 평균(평균 만점의 "
                         f"{frac[0] * 100:.1f}%, 표준편차 만점의 {frac[1] * 100:.1f}%)과 같다고 가정했어요 — 필요 점수 계산과 같은 가정")
        out["names"].append(name)
    return out


# ---------------------------------------------------------------- 갈래별 답

def _resolve_target(q: dict, c: dict, base: dict | None) -> tuple[str | None, list[str]]:
    from . import gradebook
    eff = _eff(c)
    dist, cutoffs = eff["dist"], eff["cutoffs"]
    t = q["target"]
    if t == "PASS_LOWEST":
        usable = [x for x in g.GRADES if x in cutoffs] if cutoffs else [x for x in g.GRADES[:-1] if dist.get(x, 0) > 0]
        if not usable:
            return None, []
        return usable[-1], [f"'F만 면하기'는 이 과목의 가장 낮은 통과 학점({usable[-1]}) 이상으로 봤어요"]
    if t:
        return t, [q["target_note"]] if q["target_note"] else []
    if not base:
        return None, []
    better = gradebook._better(base["grade"], dist, cutoffs)
    if better:
        return better, [f"목표 학점을 말하지 않아 지금 추정({base['grade']})보다 한 단계 위인 {better}를 목표로 계산했어요"]
    return base["grade"], [f"목표 학점을 말하지 않아 지금 추정({base['grade']}) 유지를 목표로 계산했어요"]


def _needed(c: dict, q: dict) -> dict:
    from . import gradebook
    eff = _eff(c)
    dist, cutoffs = eff["dist"], eff["cutoffs"]
    cfg = c.get("config") or {}
    expanded = gradebook.expand_components(cfg.get("components") or [])
    lines, basis, used, phrases = [], [], [], []
    hyp = expanded
    if q["assumptions"]:
        a = _apply(c, expanded, q["assumptions"])
        if a["error"]:
            return _cannot(c, a["error"], q, mode=a["mode"])
        hyp, used, phrases = a["hyp"], a["names"], a["phrases"]
        lines += a["lines"]
        basis += a["basis"]
    est = c.get("estimate")
    base = est
    if q["assumptions"]:
        try:
            base = g.estimate(hyp, dist, class_size=cfg.get("class_size"), cutoffs=cutoffs)
        except ValueError:
            base = None
    target, tbasis = _resolve_target(q, c, base)
    if target is None:
        return _cannot(c, c.get("error") or "지금 추정 학점이 없어 목표를 정할 수 없어요", q)
    basis = tbasis + basis
    if est:
        lines.insert(0, f"지금 추정: {_est_value(est)}")
    if q["assumptions"] and base:
        lines.append(f"가정하면: {_est_value(base)}")
    lines.append(f"목표: {target} 이상")
    missing = _missing(hyp)
    wanted, extra = [], []
    for gr in q["groups"]:
        wanted += [n for n in _names_for(c, gr) if n not in wanted and n not in used]
    sel = [e for e in missing if _owner(e) in wanted] if wanted else missing
    if wanted and not sel:
        extra.append(f"{', '.join(wanted)}에는 이미 점수가 있어 남은 항목으로 계산했어요")
        sel = missing
    if not sel:
        if q["assumptions"]:
            return _scenario(c, q)
        return _cannot(c, c.get("missing_note") or "남은 평가 항목이 없어요", q, extra_lines=lines)
    rows, results, errs = [], [], []
    for e in sel:
        try:
            r = g.needed_on(hyp, e["name"], target, dist, cutoffs=cutoffs)   # 성적 화면의 필요 점수와 같은 호출
        except ValueError as ex:
            errs.append(str(ex))
            rows.append({"label": _label(e), "value": f"계산 못 함: {ex}"})
            continue
        results.append((e, r))
        rows.append({"label": _label(e), "value": _need_value(e, r, target)})
    if not results:
        return _cannot(c, errs[0], q, extra_lines=lines, basis=tbasis)
    for i, (_e, r) in enumerate(results):
        basis += [n for n in r["notes"] if not _OUTCOME.search(n) and (i == 0 or not n.startswith("계산에서 뺀 항목"))]
    if len(results) > 1:
        basis.append("항목마다 따로 계산했어요 — 한 항목을 계산할 때 점수가 없는 다른 항목은 빼요(성적 화면의 필요 점수와 같은 방식)")
    basis += _count_basis(c) + _dist_basis(c)
    if c.get("missing_note"):
        lines.append(c["missing_note"])
    lines += extra
    short = c["short_name"]
    owners = list(dict.fromkeys(_owner(e) for e in sel))
    comp = wanted[0] if len(wanted) == 1 else (owners[0] if len(owners) == 1 else None)
    head = f"{short}에서 {target} 이상을 받으려면"
    if len(results) == 1:
        e, r = results[0]
        if not r["possible"]:
            say = f"{short}에서 {target}{_j(target, ('은', '는'))} {e['name']} 만점을 받아도 어려워 보여요."
        elif r["already"]:
            say = (f"{e['name']}{_j(e['name'], ('을', '를'))} 0점 받아도 계산상 {target} 이상이에요. "
                   "결시·과락 규정은 따로 확인해 주세요.")
        else:
            say = f"{head} {e['name']}에서 {_need_value(e, r, target)}이 필요해 보여요."
    else:
        ok = [(e, r) for e, r in results if r["possible"] and not r["already"]]
        if ok:
            parts = ", ".join(f"{e['name']} {_need_value(e, r, target, top=False)}" for e, r in ok[:3])
            say = f"{head} 남은 항목마다 이 정도가 필요해 보여요: {parts}" + (" 등." if len(ok) > 3 else ".")
        elif all(r["already"] for _e, r in results):
            say = f"남은 항목을 0점 받아도 계산상 {target} 이상이에요. 결시·과락 규정은 따로 확인해 주세요."
        else:
            say = f"{short}에서 {target}{_j(target, ('은', '는'))} 남은 항목 만점을 받아도 어려워 보여요."
    pre = (", ".join(phrases) + "으로 가정했어요. ") if phrases else ""
    answer = pre + say + " 지금까지 받은 점수와 학점 비율로 어림한 추정이라 실제와 다를 수 있어요."
    return _result(c, "needed", answer, target=target, component=comp, lines=lines, rows=rows, basis=basis)


LOW_WEIGHT = 30   # 점수가 반영된 비중이 이보다 작으면 추정이 크게 흔들린다고 알린다


def _scenario(c: dict, q: dict) -> dict:
    from . import gradebook
    eff = _eff(c)
    dist, cutoffs = eff["dist"], eff["cutoffs"]
    cfg = c.get("config") or {}
    expanded = gradebook.expand_components(cfg.get("components") or [])
    a = _apply(c, expanded, q["assumptions"])
    if a["error"]:
        return _cannot(c, a["error"], q, mode=a["mode"])
    try:
        est_h = g.estimate(a["hyp"], dist, class_size=cfg.get("class_size"), cutoffs=cutoffs)   # 성적 화면과 같은 호출
    except ValueError as ex:
        return _cannot(c, str(ex), q, extra_lines=a["lines"], basis=a["basis"])
    est = c.get("estimate")
    target, tbasis = (None, [])
    if q["target"]:
        target, tbasis = _resolve_target(q, c, est_h)
    lines = list(a["lines"])
    rows = [{"label": "지금", "value": _est_value(est) if est else "추정 없음(" + (c.get("error") or "점수 부족") + ")"},
            {"label": "가정하면", "value": _est_value(est_h)}]
    rows += [{"label": "가정한 점수", "value": x.replace("가정: ", "")} for x in a["lines"]]
    if est:
        lines.append(f"지금 추정: {_est_value(est)}")
    lines.append(f"가정하면: {_est_value(est_h)}")
    rng = _est_range(est_h)
    if rng:
        lines.append(f"가정하면 {rng}")
    lines.append(f"점수가 반영된 비중 {_n(est_h['completed_weight'])}%")
    short = c["short_name"]
    absolute = est_h.get("basis") == "absolute"
    say = [f"{short}에서 {', '.join(a['phrases'])}으로 가정하면 예상 학점은 {est_h['grade']}"
           + (f"(예상 총점 {_n(est_h['expected_total'])}점)예요." if absolute else f"(상위 {_n(est_h['top'])}%)예요.")]
    if est:
        # 상위 %는 작을수록, 예상 총점은 클수록 좋다
        d = (est_h["expected_total"] - est["expected_total"]) if absolute else (est["top"] - est_h["top"])
        how = "올라가요" if d > 0.05 else "내려가요" if d < -0.05 else "거의 같아요"
        say.append(f"지금 추정({_est_value(est).replace(' · ', ', ')})보다 {how}.")
    else:
        say.append("지금은 점수가 부족해 추정이 없었어요.")
    if target:
        ok = g.GRADES.index(est_h["grade"]) <= g.GRADES.index(target)
        lines.append(f"목표 {target}: " + ("가정하면 이상이에요" if ok else "가정하면 못 미쳐요"))
        say.append(f"목표 {target}에는 " + ("닿는 것으로 보여요." if ok else "못 미치는 것으로 보여요."))
    if not absolute and est_h["completed_weight"] < LOW_WEIGHT:
        say.append(f"아직 점수가 반영된 비중이 {_n(est_h['completed_weight'])}%뿐이라 추정이 크게 흔들려요.")
    else:
        say.append("가정한 점수와 학점 비율로 어림한 추정이에요.")
    basis = tbasis + a["basis"] + list(est_h["notes"]) + _count_basis(c) + _dist_basis(c)
    comp = a["names"][0] if len(set(a["names"])) == 1 else None
    return _result(c, "scenario", " ".join(say), target=target, component=comp, lines=lines, rows=rows, basis=basis)


def _current(c: dict, q: dict) -> dict:
    from . import gradebook
    est = c.get("estimate")
    pf_line = ["이 과목은 P/NP 과목으로 정해져 있지 않아요(성적 화면에서 과목 종류를 바꿀 수 있어요)"] if q["pf"] else []
    if not est:
        return _cannot(c, c.get("error") or "점수가 아직 없어요", q, extra_lines=pf_line)
    eff = _eff(c)
    better = gradebook._better(est["grade"], eff["dist"], eff["cutoffs"])
    t = better or est["grade"]
    expanded = {e["name"]: e for e in gradebook.expand_components((c.get("config") or {}).get("components") or [])}
    rows = [{"label": "예상 학점", "value": _est_value(est)}]
    ok = []
    for nd in c.get("needed") or []:   # 성적 화면이 이미 구한 필요 점수(목표 = 한 단계 위, 없으면 지금 학점)
        if nd.get("target") != t:
            continue
        e = expanded.get(nd["component"]) or {"name": nd["component"], "weight": 0, "max_score": None}
        if nd.get("error"):
            rows.append({"label": f"{_label(e)} → {t}", "value": f"계산 못 함: {nd['error']}"})
            continue
        rows.append({"label": f"{_label(e)} → {t}", "value": _need_value(e, nd, t)})
        if nd.get("possible") and not nd.get("already"):
            ok.append((e, nd))
    lines = pf_line + [f"지금 추정: {_est_value(est)}"]
    rng = _est_range(est)
    if rng:
        lines.append(rng)
    lines.append(f"점수가 반영된 비중 {_n(est['completed_weight'])}%")
    if c.get("missing_note"):
        lines.append(c["missing_note"])
    short = c["short_name"]
    say = f"{short} 지금 추정 학점은 {est['grade']}" + (f"(예상 총점 {_n(est['expected_total'])}점)예요."
                                                     if est.get("basis") == "absolute" else f"(상위 {_n(est['top'])}%)예요.")
    if ok:
        e, nd = ok[0]
        goal = f"{t}까지 가려면" if better else f"{t}{_j(t, ('을', '를'))} 지키려면"
        say += f" {goal} 예를 들어 {e['name']}에서 {_need_value(e, nd, t)}이 필요해 보여요."
    say += " 지금까지 받은 점수로 어림한 추정이라 실제와 다를 수 있어요."
    # section이 추정 메모에 학점 비율 출처(직접·강의계획서)를 이미 붙인다 — 기본값일 때만 덧붙임
    basis = list(est.get("notes") or []) + (_dist_basis(c) if c.get("letter_basis") == "default" else [])
    return _result(c, "current", say, target=None, lines=lines, rows=rows, basis=basis)


def _pass_fail(c: dict, q: dict) -> dict:
    from . import gradebook
    short = c["short_name"]
    comps = (c.get("config") or {}).get("components") or []
    basis = ["과목 종류가 P/NP라 학점·상위 % 추정을 하지 않아요",
             "P 기준(총점·출석 기준 등)은 강의계획서·공지에서 확인해 주세요"]
    if not comps:
        answer = (f"{short}{_j(short, ('은', '는'))} P/NP 과목이에요. 아직 평가 항목이 없어 받은 점수를 정리할 수 없어요 — "
                  "성적 화면에서 항목을 넣으면 정리해 드릴게요.")
        return _result(c, "pass_fail", answer, lines=["P/NP 과목 — 학점 추정 안 함"], basis=basis)
    expanded = gradebook.expand_components(comps)
    got = [e for e in expanded if e["score"] is not None]
    fixed = [e for e in expanded if e["fixed"] and e["score"] is None]
    rem = gradebook.remaining_of(comps)
    rem_w = sum(r["weight"] for r in rem if r["weight"] is not None)
    unknown = [r for r in rem if r["weight"] is None]
    done_w = sum(e["weight"] for e in got)
    pts = sum(e["weight"] * float(e["score"]) / float(e.get("max_score") or 100) for e in got)
    rows = [{"label": _label(e), "value": f"{_n(e['score'])}/{_n(e.get('max_score') or 100)}점"} for e in got]
    rows += [{"label": _label(e), "value": "모두 비슷하게 받는 항목(점수 없음)"} for e in fixed]
    rem_text = f"{_n(rem_w)}%" + "".join(f" + {r['component']} {_n(r['range'][0])}~{_n(r['range'][1])}%" for r in unknown)
    rows.append({"label": "남은 비중", "value": rem_text})
    lines = ["P/NP 과목 — 학점·P 여부는 추정하지 않아요",
             f"점수가 들어간 비중 {_n(done_w)}%: 비중대로 더하면 100점 중 {_n(pts)}점"]
    if c.get("missing_note"):
        lines.append(c["missing_note"])
    basis.append("쌓인 점수 = 받은 점수 ÷ 만점 × 비중의 합 — 남은 항목·모두 비슷하게 받는 항목은 넣지 않았어요")
    basis += _count_basis(c)
    head = f"{short}{_j(short, ('은', '는'))} P/NP 과목이라 학점이나 P 여부는 추정하지 않아요."
    if got:
        answer = (f"{head} 지금까지 점수가 들어간 비중은 {_n(done_w)}%(비중대로 더하면 100점 중 {_n(pts)}점)이고, "
                  f"남은 비중은 {rem_text}예요. P 기준은 강의계획서에서 확인해 주세요.")
    else:
        answer = f"{head} 아직 받은 점수가 없고 남은 비중은 {rem_text}예요. P 기준은 강의계획서에서 확인해 주세요."
    return _result(c, "pass_fail", answer, lines=lines, rows=rows, basis=basis)


def _ask_course(choices: list[dict]) -> dict:
    rows = []
    for c in choices:
        est = c.get("estimate")
        if c.get("mode") == "pass_fail":
            v = "P/NP"
        elif est:
            v = f"추정 {est['grade']}"
        elif _has_data(c):
            v = f"평가 항목 {len(c['config']['components'])}개"
        else:
            v = "평가 항목 없음"
        rows.append({"label": c["short_name"], "value": v})
    names = ", ".join(c["short_name"] for c in choices)
    answer = f"어느 과목 이야기인지 알려 주세요. 성적이 들어 있는 과목: {names}. 과목 이름을 넣어 다시 물어봐 주세요."
    return _result(None, "ask_course", answer, rows=rows)


def handle(store, message: str, course_id=None, now=None) -> dict | None:
    """성적 '만약에' 질문이 아니면 None. 결과 {"answer", "whatif": {course_id, course_short, kind, target, component,
    lines, rows, basis}}(계약 22-3). 저장된 상태는 바꾸지 않는다."""
    q = parse(message)
    if q is None:
        return None
    sec, cache_courses = _context(store, now)
    course, choices = _pick(q, course_id, sec.get("courses") or [], cache_courses)
    if course is None:
        if choices:
            return _ask_course(choices)
        return _result(None, "current", "아직 평가 항목(비중)이 들어간 과목이 없어 계산할 수 없어요. 성적 화면에서 과목별 "
                                        "평가 비중을 넣으면 '만약에'를 계산해 드릴게요.",
                       lines=["성적이 들어 있는 과목 없음"])
    if course.get("mode") == "pass_fail":
        return _pass_fail(course, q)
    if not _has_data(course):
        short = course["short_name"]
        return _result(course, "current",
                       f"{short}에는 아직 평가 항목(비중)이 없어 계산할 수 없어요. 성적 화면에서 강의계획서의 평가 비중을 "
                       "넣어 주세요.", lines=["평가 항목 없음"], basis=_dist_basis(course))
    if q["intent"] == "needed":
        return _needed(course, q)
    if q["intent"] == "scenario":
        return _scenario(course, q)
    return _current(course, q)
