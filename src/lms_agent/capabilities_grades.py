"""성적 능력 — 계약 28-2 (v0.8.3, 담당 H2). capabilities.register로 REGISTRY에 더한다.

비중·평가 항목·점수·학점 비율(상대/절대)·수강 인원·점수 후보 연결·개수·마감↔평가 항목 연결·성적 기본 설정을 말로 바꾸는 능력.
학점 비율은 사용자가 'A 40%·B 40%'처럼 말한 값을 지금 앱의 학점 비율 모양으로 바꾼다(근거 글 필요 없음 — 사용자의 지시가 권한, 계약 28-0).

모든 능력은 build(app, params) → (경로, 본문)에서 지금 성적 설정을 읽어 **성적 화면이 보낼 전체 본문**(web/index.html saveGrades:
components [{name, weight, score, max_score, mean, sd, fixed, source}] · dist {9학점: %} | null · class_size)에 요청한 것만 바꿔 넣는다.
본문은 gradebook.save_course를 저장소 사본에 미리 돌려 보아 경로가 받아 주는 것만 돌려준다(경로가 거절할 것은 여기서 한국어 오류).
preview(app, params) → [{"label", "before", "after"}]는 바뀌는 칸만 짧은 한국어로("학점 비율 A 30% → 40%", "Report 비중 20% → 30%").
이름은 하네스(capabilities.plan)가 id로 바꿔 주지만, 여기서도 과목(짧은 이름·코드·id)·평가 항목(별칭·계열)·항목(제목)·점수 후보(key·이름)를
하나로 정해질 때만 받아 준다 — 여럿이면 후보를, 없으면 지금 있는 이름을 담은 한국어 오류.

학점 비율(상대평가) 바꾸기 규칙(grades.set_dist, kind "relative"):
- 학점 낱말: A+ A0 B+ … D0 F. 맨 글자 A는 A+(또는 A0)가 따로 적혀 있으면 나머지 하나, 아니면 A+·A0 묶음. A-는 A0. 'A학점'·'A grade'도 같음.
- 묶음: 'A+,A0'·'A+/A0'·'A+·A0'(이어진 학점), 범위 'C+~F'·'A~B'(맨 글자 시작은 X+, 끝은 X0), 꼬리 'C 이하'(C+~F),
  누적 'B 이상'(A+부터 B0까지 합 — cumulative: true면 모든 값이 누적), 나머지 '나머지'·'rest'(값이 없으면 100 − 합).
- 말한 묶음은 그 값으로, 묶음 안은 **지금 비율 모양대로** 나눈다(지금 비율 = 사용자 비율 > 강의 글 비율 > 기본 비율, 모두 0이면 기본 모양, 그래도 0이면 고르게).
  말하지 않은 학점은 남은 %(100 − 합)를 지금 비율 모양대로 나눈다. 합이 100을 넘으면 오류, 모든 학점을 말했는데 100이 안 되면 오류.
- 소수 둘째 자리로 반올림, 합이 정확히 100이 되게(남는 0.01은 나눠 받은 학점 중 가장 큰 곳에). 결과는 화면과 같은 {9학점: %}.
절대평가(kind "absolute", 글자별 최저 점수 — 맨 글자 A 90은 A0 90, A+는 위 경계(100 또는 위 학점 기준)와의 가운데)는 검사·미리 보기까지 하지만
지금 성적 화면·경로(/api/grades/<id>)가 사용자 기준표를 저장하지 못해 gradebook.USER_CUTOFFS가 참일 때만 본문에 "cutoffs"를 넣는다(아니면 한국어 오류).
"""
from __future__ import annotations

import copy
import math
import re
import types
import unicodedata

from . import distribution as dm
from . import grades as g
from . import links as lk

GRADES = list(g.GRADES)
_IDX = {x: i for i, x in enumerate(GRADES)}
_F = _IDX["F"]
IDS = ("grades.set_weights", "grades.set_component", "grades.remove_component", "grades.set_score", "grades.set_dist",
       "grades.set_class_size", "grades.link_score", "grades.unlink_score", "grades.set_count", "items.set_component",
       "grades.settings")

_BY_KO = {"rules": "자동", "ai": "AI", "user": "직접"}
_BASIS_KO = {"stated": "명시", "hint": "단서", "user": "직접", "observed": "관찰"}
_SKIP_STATUS = ("rejected", "duplicate")


# ================================================================ 공통 도우미

def _num_text(x) -> str:
    return f"{round(float(x), 2):g}"


def _pct(x) -> str:
    return "—" if x is None else f"{_num_text(x)}%"


def _line(label: str, before, after) -> dict:
    return {"label": label, "before": "—" if before in (None, "") else str(before),
            "after": "—" if after in (None, "") else str(after)}


def _key(s) -> str:
    """이름 비교용: NFKC, 소문자, 공백·문장부호 없앰('LAB 4' = 'lab4')."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(s or "")).lower())


def _flat(s, n: int = 40) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def _fnum(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


_UNIT = re.compile(r"\s*(?:%|퍼센트|percent|점|pts?|points?|명|개|인)?\s*(?:이상|이하|이내|까지|정도|쯤|내외)?\s*$", re.I)


def _number(v, what: str, lo: float | None = None, hi: float | None = None, integer: bool = False):
    """말로 받은 숫자('40%'·'48명'도) → float(integer면 int). 범위를 벗어나면 한국어 ValueError."""
    raw = v
    if isinstance(v, str):
        s = _UNIT.sub("", v.strip().replace(",", ""))
        try:
            v = float(s)
        except ValueError:
            raise ValueError(f"{what}은(는) 숫자여야 해요: '{_flat(raw, 20)}'") from None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what}은(는) 숫자여야 해요")
    x = float(v)
    if integer:
        if abs(x - round(x)) > 1e-9:
            raise ValueError(f"{what}은(는) 정수여야 해요")
        x = int(round(x))
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        if lo is not None and hi is not None:
            raise ValueError(f"{what}은(는) {lo:g}~{hi:g} 사이여야 해요")
        raise ValueError(f"{what}은(는) {lo:g} 이상이어야 해요" if lo is not None else f"{what}은(는) {hi:g} 이하여야 해요")
    return x


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on", "예", "네", "응")
    return bool(v)


def _p(params: dict, *names):
    for n in names:
        if n in params:
            return params[n]
    return None


# ---------------------------------------------------------------- 과목

def _courses(store) -> dict:
    return {str(c["id"]): c for c in (store.data.get("cache") or {}).get("courses") or []
            if isinstance(c, dict) and c.get("id") is not None}


def _short(store, cid) -> str:
    from .core import short_course_name
    c = _courses(store).get(str(cid))
    return short_course_name((c or {}).get("name")) or str(cid)


def resolve_course(store, raw) -> str:
    """과목 id·짧은 이름·이름·과목 코드(대소문자·띄어쓰기 무시, 부분 이름도) → id. 하나로 정해질 때만."""
    from .store import hidden_courses
    courses = _courses(store)
    hidden = hidden_courses(store.data)
    visible = [cid for cid in courses if cid not in hidden]
    shorts = ", ".join(_short(store, cid) for cid in visible) or "없음"
    s = _flat(raw, 200)
    if not s:
        raise ValueError(f"어느 과목인지 알려 주세요. 지금 과목: {shorts}")
    if s in courses:
        return s
    k = _key(s)

    def names(cid):
        c = courses[cid]
        return [_key(x) for x in (_short(store, cid), c.get("name"), c.get("course_code")) if x]

    for pool in (visible, [cid for cid in courses if cid in hidden]):
        exact = [cid for cid in pool if k in names(cid)]
        hits = exact or [cid for cid in pool if k and any(k in n for n in names(cid))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError(f"'{_flat(s)}'에 맞는 과목이 여럿이에요: {', '.join(_short(store, c) for c in hits)}"
                             " — 하나를 골라 주세요")
    raise ValueError(f"'{_flat(s)}' 과목을 찾지 못했어요. 지금 과목: {shorts}")


def _grade_course(store, params) -> str:
    """성적 화면에 있는 과목(제외한 과목이면 오류)."""
    from .store import hidden_courses
    cid = resolve_course(store, _p(params, "course", "course_id"))
    if cid in hidden_courses(store.data):
        raise ValueError(f"'{_short(store, cid)}'은(는) 제외한 과목이라 성적 화면에 없어요 — "
                         "과목 종류를 먼저 '성적 있음'으로 바꿔 주세요")
    return cid


def _pass_fail(store, cid) -> bool:
    from .store import course_mode
    return course_mode(store.data, cid) == "pass_fail"


# ---------------------------------------------------------------- 성적 설정·화면 본문

def _cfg(store, cid) -> dict:
    cfg = (store.data.get("grades") or {}).get(str(cid))
    return copy.deepcopy(cfg) if isinstance(cfg, dict) else {"components": [], "dist": None, "class_size": None}


def _comps(cfg: dict) -> list:
    return [c for c in (cfg or {}).get("components") or [] if isinstance(c, dict) and str(c.get("name") or "").strip()]


def _screen_row(c: dict) -> dict:
    """성적 화면 saveGrades가 한 줄에 보내는 모양(빈 비중은 0, 빈 칸은 null, source 없으면 null)."""
    w = _fnum(c.get("weight"))
    return {"name": str(c.get("name") or "").strip(), "weight": w if w is not None else 0.0,
            "score": _fnum(c.get("score")), "max_score": _fnum(c.get("max_score")), "mean": _fnum(c.get("mean")),
            "sd": _fnum(c.get("sd")), "fixed": bool(c.get("fixed")), "source": c.get("source") or None}


def screen_body(cfg: dict) -> dict:
    """지금 설정 그대로 성적 화면이 [저장]할 때 보내는 본문(직접 넣은 학점 비율이 있으면 그 9학점, 없으면 null)."""
    dist = (cfg or {}).get("dist")
    return {"components": [_screen_row(c) for c in _comps(cfg)],
            "dist": {x: _fnum(dist.get(x)) or 0.0 for x in GRADES} if isinstance(dist, dict) and dist else None,
            "class_size": (cfg or {}).get("class_size")}


def _dry_save(store, cid: str, body: dict) -> dict:
    """경로(gradebook.save_course)를 저장소 사본에 미리 돌린 결과 설정 — 경로가 거절하면 그 한국어 ValueError."""
    from . import gradebook
    prev = (store.data.get("grades") or {}).get(str(cid))
    fake = types.SimpleNamespace(data={"grades": {str(cid): copy.deepcopy(prev)} if isinstance(prev, dict) else {}})
    gradebook.save_course(fake, str(cid), copy.deepcopy(body))
    return fake.data["grades"][str(cid)]


def _save_path(cid) -> str:
    return f"/api/grades/{cid}"


def _total_text(comps: list) -> str:
    from .gradebook import weight_totals
    total, missing = weight_totals(comps)
    return _pct(total) + (f" (나머지 {missing:g}%는 평가 항목이 없어 계산 못 해요)" if missing > 0.01 else "")


def _weight_lines(before: dict, after: dict, merged: dict | None = None) -> list:
    """평가 항목·비중의 전→후(바뀐 줄만) + 비중 합이 바뀌면 그 줄(100이 안 되면 모르는 비중 메모)."""
    from .gradebook import weight_totals
    b = {lk.norm(c["name"]): c for c in _comps(before)}
    a = {lk.norm(c["name"]): c for c in _comps(after)}
    lines = []
    for k, c in a.items():
        w = _fnum(c.get("weight")) or 0.0
        fx = " · 순위 제외" if c.get("fixed") else ""
        if k not in b:
            lines.append(_line(f"{c['name']} 비중", "없음", f"{_pct(w)} (신규{fx})"))
            continue
        bw = _fnum(b[k].get("weight")) or 0.0
        if round(bw, 2) != round(w, 2):
            lines.append(_line(f"{c['name']} 비중", _pct(bw), _pct(w)))
        if bool(b[k].get("fixed")) != bool(c.get("fixed")):
            lines.append(_line(f"{c['name']} 순위 제외", "예" if b[k].get("fixed") else "아니요",
                               "예" if c.get("fixed") else "아니요"))
    for k, c in b.items():
        if k not in a:
            into = (merged or {}).get(k)
            lines.append(_line(f"{c['name']} 비중", _pct(_fnum(c.get("weight")) or 0.0),
                               f"지움({into}에 더함)" if into else "지움"))
    if weight_totals(_comps(before))[0] != weight_totals(_comps(after))[0]:
        lines.append(_line("비중 합", _total_text(_comps(before)), _total_text(_comps(after))))
    return lines


def _auto_line(before: dict) -> list:
    """강의 글에서 자동으로 찾은 비중이었으면: 화면에서 저장한 것처럼 직접 넣은 값이 되어 다음 동기화가 바꾸지 않는다."""
    if any(c.get("auto") for c in _comps(before)):
        return [_line("평가 비중 자동 채우기", "강의 글 따라 자동", "직접 넣은 값으로 고정")]
    return []


def _result(path: str, body: dict, lines: list) -> dict:
    return {"path": path, "body": body, "lines": lines}


# ---------------------------------------------------------------- 평가 항목 이름

def _strict(names: list, raw) -> list:
    """이름이 같거나(공백·대소문자 무시), 흔한 표현이 같거나('Midterm exam' = '중간고사'), 한쪽 이름이 다른 쪽에 들어 있음."""
    from .gradebook import _canon
    n = lk.norm(raw)
    hit = [x for x in names if lk.norm(x) == n]
    if hit:
        return hit[:1]
    c = _canon(raw)
    hit = [x for x in names if _canon(x) == c]
    if hit:
        return hit
    k = _key(raw)
    if k:
        hit = [x for x in names if k in _key(x)]
        if hit:
            return hit
        hit = [x for x in names if _key(x) and _key(x) in k]
        if hit:
            return hit
    return []


def _by_family(names: list, raw) -> list:
    """말의 계열(과제·중간·기말·프로젝트…)이 모두 이름에 있는 항목('과제' → 'Homework assignments')."""
    fam = lk.families(raw)
    return [x for x in names if fam and fam <= lk.families(x)] if fam else []


def find_component(cfg: dict, raw, strict_only: bool = False) -> tuple:
    """(저장된 이름 | None, 여럿일 때 후보들). strict_only가 아니면 계열·별칭·실습→과제 규칙(links.match)까지."""
    from .gradebook import _lcomps
    names = [str(c["name"]).strip() for c in _comps(cfg)]
    if not names or not _flat(raw):
        return None, []
    for step in (_strict, _by_family):
        hit = step(names, raw)
        if len(hit) == 1:
            return hit[0], []
        if hit:
            return None, hit
    if strict_only:
        return None, []
    m = lk.match(_flat(raw), _lcomps(cfg))
    if m.component:
        return m.component, []
    if "둘 이상" in (m.reason or ""):
        amb = [x for x in names if x in m.reason]
        return None, amb or names
    return None, []


def need_component(store, cid: str, cfg: dict, raw, what: str = "평가 항목", loose: bool = True) -> str:
    """있는 평가 항목 하나로 정해지면 그 이름, 아니면 한국어 ValueError(여럿이면 후보, 없으면 지금 이름들).
    loose가 아니면(지우기처럼 되돌리기 전까지 눈에 덜 띄는 변경) 별칭·실습→과제 규칙(links.match)은 쓰지 않는다."""
    short = _short(store, cid)
    names = [str(c["name"]).strip() for c in _comps(cfg)]
    if not names:
        raise ValueError(f"{short}에는 아직 평가 항목이 없어요 — 먼저 평가 비중을 넣어 주세요")
    if not _flat(raw):
        raise ValueError(f"어느 {what}인지 알려 주세요. 지금 평가 항목: {', '.join(names)}")
    name, amb = find_component(cfg, raw, strict_only=not loose)
    if name:
        return name
    if amb:
        raise ValueError(f"'{_flat(raw)}'에 맞는 평가 항목이 여럿이에요: {', '.join(amb)} — 하나를 골라 주세요")
    raise ValueError(f"{short}에 '{_flat(raw)}' 평가 항목이 없어요. 지금 평가 항목: {', '.join(names)}")


# ================================================================ 평가 비중·항목

def _row_for(name: str, weight: float, keep: dict | None, fixed=None) -> dict:
    from .weights import is_fixed
    row = _screen_row(keep) if keep else {"name": name, "weight": 0.0, "score": None, "max_score": None, "mean": None,
                                          "sd": None, "fixed": is_fixed(name), "source": None}
    row["weight"] = float(weight)
    if fixed is not None:
        row["fixed"] = _bool(fixed)
    return row


def _weight_entries(raw) -> list:
    """[{name, weight}] 또는 {이름: 비중} → [(이름, 비중)]."""
    if isinstance(raw, dict):
        raw = [{"name": k, "weight": v} for k, v in raw.items()]
    if not isinstance(raw, list) or not raw:
        raise ValueError("평가 비중은 [{name, weight}] 목록이어야 해요(예: [{\"name\": \"중간고사\", \"weight\": 30}])")
    from .gradebook import MAX_COMPONENTS
    if len(raw) > MAX_COMPONENTS:
        raise ValueError(f"평가 항목은 {MAX_COMPONENTS}개까지예요")
    out = []
    for e in raw:
        if not isinstance(e, dict):
            raise ValueError("평가 비중 한 줄은 {name, weight} 모양이어야 해요")
        name = _flat(e.get("name") if e.get("name") not in (None, "") else e.get("component"))
        if not name:
            raise ValueError("평가 항목 이름이 비어 있어요")
        out.append((name, _number(e.get("weight"), f"'{name}' 비중", 0, 100), e.get("fixed")))
    return out


def _check_total(store, cid, comps: list) -> None:
    total = round(sum(_fnum(c.get("weight")) or 0.0 for c in comps), 2)
    if total > 100.5:
        raise ValueError(f"비중 합이 {total:g}%가 돼요(100%를 넘을 수 없어요) — 다른 평가 항목의 비중도 함께 알려 주세요")


def _set_weights(app, p: dict) -> dict:
    """grades.set_weights: 평가 항목 전체를 이 목록으로. 같은 이름(별칭 포함)의 점수·만점·평균·표준편차·순위 제외·출처는 그대로,
    목록에 없는 항목은 지운다. 새 이름은 신규 항목(출석·참여는 순위 제외)."""
    store = app.store
    cid = _grade_course(store, p)
    before = _cfg(store, cid)
    old = {str(c["name"]).strip(): c for c in _comps(before)}
    rows, used = [], {}
    for name, w, fixed in _weight_entries(_p(p, "weights", "components")):
        hit, amb = find_component(before, name, strict_only=True)
        if amb:
            raise ValueError(f"'{name}'에 맞는 평가 항목이 여럿이에요: {', '.join(amb)} — 정확한 이름으로 알려 주세요")
        key = lk.norm(hit or name)
        if key in used:
            raise ValueError(f"'{name}'이(가) '{used[key]}'와 같은 평가 항목이에요 — 한 번만 적어 주세요")
        used[key] = name
        rows.append(_row_for(hit or name[:40], w, old.get(hit) if hit else None, fixed))
    _check_total(store, cid, rows)
    body = {**screen_body(before), "components": rows}
    after = _dry_save(store, cid, body)
    lines = _weight_lines(before, after)
    if not lines:
        raise ValueError("지금과 같은 평가 비중이에요 — 바꿀 것이 없어요")
    return _result(_save_path(cid), body, lines + _auto_line(before))


def _set_component(app, p: dict) -> dict:
    """grades.set_component: 이름이 있으면 그 비중(과 순위 제외)만 바꾸고, 없으면 신규 항목으로 끝에 더한다."""
    from .gradebook import MAX_COMPONENTS
    store = app.store
    cid = _grade_course(store, p)
    before = _cfg(store, cid)
    raw = _flat(_p(p, "component", "name"))
    if not raw:
        raise ValueError("평가 항목 이름을 알려 주세요")
    w = _number(_p(p, "weight"), f"'{raw}' 비중", 0, 100)
    fixed = p.get("fixed")
    hit, amb = find_component(before, raw, strict_only=True)
    if amb:
        raise ValueError(f"'{raw}'에 맞는 평가 항목이 여럿이에요: {', '.join(amb)} — 하나를 골라 주세요")
    rows = [_screen_row(c) for c in _comps(before)]
    if hit:
        row = next(r for r in rows if r["name"] == hit)
        row["weight"] = float(w)
        if fixed is not None:
            row["fixed"] = _bool(fixed)
    else:
        if len(rows) >= MAX_COMPONENTS:
            raise ValueError(f"평가 항목은 {MAX_COMPONENTS}개까지예요")
        rows.append(_row_for(raw, w, None, fixed))
    _check_total(store, cid, rows)
    body = {**screen_body(before), "components": rows}
    after = _dry_save(store, cid, body)
    lines = _weight_lines(before, after)
    if not lines:
        raise ValueError(f"'{hit or raw}' 비중은 이미 {_pct(w)}예요 — 바꿀 것이 없어요")
    return _result(_save_path(cid), body, lines + _auto_line(before))


def _remove_component(app, p: dict) -> dict:
    """grades.remove_component: 그 항목을 빼고(merge_into가 있으면 비중을 그 항목에 더함 — agent_fix와 같은 반올림).
    화면 저장 경로를 쓰므로 남은 항목은 모두 직접 넣은 값이 되어 다음 동기화의 자동 채우기가 지운 항목을 되살리지 않는다
    (지운 기록 cfg.removed는 경로가 그대로 둔다)."""
    store = app.store
    cid = _grade_course(store, p)
    before = _cfg(store, cid)
    name = need_component(store, cid, before, _p(p, "component", "name"), loose=False)
    into_raw = _p(p, "merge_into", "into")
    into = need_component(store, cid, before, into_raw, "합칠 평가 항목", loose=False) if _flat(into_raw) else None
    if into == name:
        raise ValueError("같은 평가 항목에는 합칠 수 없어요 — 다른 항목을 골라 주세요")
    rows = [_screen_row(c) for c in _comps(before)]
    gone = next(r for r in rows if r["name"] == name)
    rows = [r for r in rows if r is not gone]
    if into:
        t = next(r for r in rows if r["name"] == into)
        t["weight"] = round((t["weight"] or 0.0) + (gone["weight"] or 0.0), 2)
    body = {**screen_body(before), "components": rows}
    after = _dry_save(store, cid, body)
    lines = _weight_lines(before, after, merged={lk.norm(name): into} if into else None)
    if not rows:
        lines.append(_line("평가 항목", f"{len(_comps(before))}개", "없음(다음 동기화에서 강의 글의 비중이 다시 들어올 수 있어요)"))
    return _result(_save_path(cid), body, lines + _auto_line(before))


# ---------------------------------------------------------------- 점수

_SCORE_FIELDS = (("score", "점수"), ("max_score", "만점"), ("mean", "평균"), ("sd", "표준편차"))


def _members(store, cid: str, cfg: dict) -> dict:
    from . import gradebook
    comps = _comps(cfg)
    if not comps:
        return {}
    mine = [c for c in gradebook.candidates(store) if str(c.get("course_id")) == cid]
    return gradebook.members_of(mine, gradebook.link_candidates(store, cid, comps, mine))


def _set_score(app, p: dict) -> dict:
    """grades.set_score: 그 평가 항목의 내 점수(필수)와 만점·평균·표준편차(준 것만 — null·빈 값은 '안 줌'으로 그대로 둠).
    clear면 네 칸을 모두 비운다(agent_fix score_clear와 같은 뜻). 세부 점수(연결된 점수 후보)로 계산하는 항목은 화면처럼 직접 넣지 않는다."""
    store = app.store
    cid = _grade_course(store, p)
    before = _cfg(store, cid)
    name = need_component(store, cid, before, _p(p, "component", "name"))
    clear = _bool(p.get("clear"))
    ms = _members(store, cid, before).get(name) or []
    if ms and not clear:
        labels = ", ".join(_flat(m.get("label"), 20) for m in ms[:4])
        raise ValueError(f"'{name}'은(는) 연결된 세부 점수({labels})로 계산해서 점수를 직접 넣을 수 없어요 — "
                         "그 점수 후보 연결을 먼저 떼어 주세요")
    if not clear and p.get("score") in (None, ""):
        raise ValueError("넣을 점수(score)를 알려 주세요")
    rows = [_screen_row(c) for c in _comps(before)]
    row = next(r for r in rows if r["name"] == name)
    lo = {"score": 0, "max_score": 0.0001, "mean": 0, "sd": 0}
    for f, ko in _SCORE_FIELDS:
        if clear:
            row[f] = None
        elif p.get(f) not in (None, ""):
            row[f] = _number(p[f], f"'{name}' {ko}", lo[f])
    body = {**screen_body(before), "components": rows}
    after = _dry_save(store, cid, body)
    old = next(c for c in _comps(before) if str(c["name"]).strip() == name)
    new = next(c for c in _comps(after) if c["name"] == name)
    lines = []
    for f, ko in _SCORE_FIELDS:
        a, b = _fnum(old.get(f)), new.get(f)
        if a != b:
            lines.append(_line(f"{name} {ko}", "—" if a is None else _num_text(a), "비움" if b is None else _num_text(b)))
    if not lines:
        raise ValueError(f"'{name}' 점수는 이미 그 값이에요 — 바꿀 것이 없어요")
    mx = new.get("max_score") or 100.0
    if (new.get("score") or 0) > mx or (new.get("mean") or 0) > mx:
        lines.append(_line("확인", None, f"만점({_num_text(mx)})보다 높은 값이 있어요 — 만점이 맞는지 확인해 주세요"))
    return _result(_save_path(cid), body, lines + _auto_line(before))


def _set_class_size(app, p: dict) -> dict:
    """grades.set_class_size: 수강 인원(1~100000, null이면 지움 — 석차를 어림할 때만 씀)."""
    store = app.store
    cid = _grade_course(store, p)
    before = _cfg(store, cid)
    raw = _p(p, "size", "class_size", "n")
    size = None if raw in (None, "") else _number(raw, "수강 인원", 1, 100000, integer=True)
    if before.get("class_size") == size:
        raise ValueError("수강 인원이 이미 그 값이에요 — 바꿀 것이 없어요")
    body = {**screen_body(before), "class_size": size}
    _dry_save(store, cid, body)
    old = before.get("class_size")
    lines = [_line("수강 인원", f"{old}명" if old else "—", f"{size}명" if size else "지움")]
    return _result(_save_path(cid), body, lines + _auto_line(before))


# ================================================================ 학점 비율(상대) · 점수 기준(절대)

_DROP = re.compile(r"학점|등급|GRADES?|LETTERS?|['’]S", re.I)
_CUM_SUFFIX = re.compile(r"(?:이상|ORABOVE|ORHIGHER|ORBETTER|ANDABOVE|ANDUP|\+이상)$")
_TAIL_SUFFIX = re.compile(r"(?:이하|ORBELOW|ORLOWER|ANDBELOW)$")
_RANGE_SEP = re.compile(r"~|〜|∼|–|—|TO|THROUGH|부터")
_LIST_SEP = re.compile(r",|/|·|&|및|AND")
_LETTER = re.compile(r"^([A-DF])(\+|0|-)?$")
_REST_KEYS = {"나머지", "REST", "THEREST", "OTHERS", "OTHER", "그외", "기타", "그밖", "나머지전부", "나머지모두"}
_FORMS = "A+, A0, A(=A+·A0), A+·A0, C+~F, C 이하, B 이상, 나머지"


def _norm_letter_key(raw) -> str:
    s = unicodedata.normalize("NFKC", str(raw or "")).upper()
    s = re.sub(r"\s+", "", s)
    s = _DROP.sub("", s)
    return s.replace("까지", "")


def _letter(tok: str, raw) -> tuple:
    """학점 낱말 하나 → (A+는 0 … F는 8, 끝 인덱스, 맨 글자인지)."""
    m = _LETTER.match(tok)
    if not m:
        raise ValueError(f"'{_flat(raw, 20)}'은(는) 학점으로 읽지 못했어요 — {_FORMS} 꼴로 알려 주세요")
    L, s = m.group(1), m.group(2)
    if L == "F":
        if s:
            raise ValueError(f"'{_flat(raw, 20)}'은(는) 학점으로 읽지 못했어요 — F에는 +·0이 없어요")
        return _F, _F, False
    top, bottom = _IDX[L + "+"], _IDX[L + "0"]
    if s == "+":
        return top, top, False
    if s in ("0", "-"):
        return bottom, bottom, False
    return top, bottom, True


def _parse_rel_key(raw, single_given: set) -> dict:
    """사용자의 학점 말 하나 → {"lo", "hi", "cum", "rest", "label"}."""
    label = _flat(raw, 16)
    s = _norm_letter_key(raw)
    if s in _REST_KEYS or label in _REST_KEYS:
        return {"lo": None, "hi": None, "cum": False, "rest": True, "label": "나머지"}
    cum = tail = False
    m = _CUM_SUFFIX.search(s)
    if m and s[:m.start()]:
        cum, s = True, s[:m.start()]
    m = _TAIL_SUFFIX.search(s)
    if m and s[:m.start()]:
        tail, s = True, s[:m.start()]
    if not s:
        raise ValueError(f"'{label}'은(는) 학점으로 읽지 못했어요 — {_FORMS} 꼴로 알려 주세요")
    parts = [x for x in _RANGE_SEP.split(s) if x]
    if len(parts) == 2 and _RANGE_SEP.search(s):
        lo = _letter(parts[0], raw)[0]
        hi = _letter(parts[1], raw)[1]
        if lo > hi:
            raise ValueError(f"'{label}' 범위가 거꾸로예요 — 높은 학점부터 적어 주세요(예: C+~F)")
    elif _LIST_SEP.search(s):
        idx = set()
        for t in [x for x in _LIST_SEP.split(s) if x]:
            a, b, _bare = _letter(t, raw)
            idx.update(range(a, b + 1))
        lo, hi = min(idx), max(idx)
        if sorted(idx) != list(range(lo, hi + 1)):
            raise ValueError(f"'{label}'의 학점이 이어지지 않아요 — 이어진 학점끼리 묶어 주세요")
    else:
        lo, hi, bare = _letter(s, raw)
        if bare and not tail and not cum:
            L = s[0]
            if L + "+" in single_given:
                lo = hi = _IDX[L + "0"]
            elif L + "0" in single_given:
                lo = hi = _IDX[L + "+"]
    if tail:
        hi = _F
    if cum and tail:
        raise ValueError(f"'{label}'에 '이상'과 '이하'가 함께 있어요")
    return {"lo": lo, "hi": hi, "cum": cum, "rest": False, "label": label}


def _shape(idx: list, cur: dict) -> list:
    """나눌 모양: 지금 비율 → (모두 0이면) 기본 비율 → (그래도 0이면) 고르게."""
    w = [max(0.0, _fnum(cur.get(GRADES[i])) or 0.0) for i in idx]
    if sum(w) <= 0:
        w = [float(g.DEFAULT_DISTRIBUTION.get(GRADES[i], 0)) for i in idx]
    if sum(w) <= 0:
        w = [1.0] * len(idx)
    return w


def _split(total: float, idx: list, cur: dict, out: dict) -> None:
    w = _shape(idx, cur)
    for i, wi in zip(idx, w):
        out[GRADES[i]] = total * wi / sum(w)


def _round100(raw: dict, exact: set) -> dict:
    """소수 둘째 자리, 합 정확히 100(남는 몫은 사용자가 그 값을 콕 집어 말하지 않은 학점 중 가장 큰 곳에)."""
    out = {x: round(float(raw.get(x) or 0.0) + 0.0, 2) for x in GRADES}
    diff = round(100.0 - sum(out.values()), 2)
    if abs(diff) >= 0.005:
        pool = [x for x in GRADES if x not in exact and out[x] + diff >= 0] or [x for x in GRADES if out[x] + diff >= 0]
        x = max(pool, key=lambda k: out[k])
        out[x] = round(out[x] + diff, 2)
    return out


def relative_dist(spec, current: dict | None = None, cumulative: bool = False) -> tuple:
    """사용자의 학점 비율 말({"A": 40, "B": 40, "C": 20} · {"A+": 10, "A0": 30} · {"B 이상": 70} · {"C+~F": 10} · {"나머지": 20})
    → (화면과 같은 {9학점: %}(합 100), 미리 보기용 묶음 [{"label", "idx"}], 사용자가 말한 학점 인덱스 집합). 규칙은 모듈 머리말."""
    if not isinstance(spec, dict) or not spec:
        raise ValueError("학점 비율은 {학점: %} 모양이어야 해요(예: {\"A\": 40, \"B\": 40, \"C\": 20})")
    cur = current if isinstance(current, dict) and not g.validate_distribution(current) else g.DEFAULT_DISTRIBUTION
    singles = set()
    for k in spec:
        s = _norm_letter_key(k)
        m = _LETTER.match(s)
        if m and m.group(2) in ("+", "0"):
            singles.add(m.group(1) + m.group(2))
    stmts = []
    for k, v in spec.items():
        st = _parse_rel_key(k, singles)
        if cumulative and not st["rest"]:
            st["cum"] = True
        if st["rest"] and v in (None, ""):
            st["v"] = None
        else:
            st["v"] = _number(v, f"{st['label']} 비율", 0, 100)
        stmts.append(st)
    if sum(1 for s in stmts if s["rest"]) > 1:
        raise ValueError("'나머지'는 한 번만 적어 주세요")
    bands = []   # [(인덱스 집합, 값, 문장)]
    for st in sorted((s for s in stmts if not s["cum"] and not s["rest"]), key=lambda s: s["lo"]):
        idx = set(range(st["lo"], st["hi"] + 1))
        clash = next((b for b in bands if b[0] & idx), None)
        if clash:
            raise ValueError(f"'{clash[2]['label']}'와 '{st['label']}'의 학점이 겹쳐요 — 겹치지 않게 알려 주세요")
        bands.append((idx, st["v"], st))
    for st in sorted((s for s in stmts if s["cum"]), key=lambda s: s["hi"]):
        j = st["hi"]
        for idx, _v, other in bands:
            if min(idx) <= j < max(idx):
                raise ValueError(f"'{other['label']}'와 '{st['label']}'의 학점이 겹쳐요 — 겹치지 않게 알려 주세요")
        covered = set().union(*[b[0] for b in bands if max(b[0]) <= j]) if bands else set()
        above = sum(b[1] for b in bands if max(b[0]) <= j)
        free = [i for i in range(0, j + 1) if i not in covered]
        left = st["v"] - above
        if left < -0.01 or (not free and abs(left) > 0.01):
            raise ValueError(f"'{st['label']}' {_pct(st['v'])}가 그 위 학점들의 합 {_pct(above)}과 맞지 않아요"
                             " — 누적이면 위에서부터 늘어나야 해요")
        if free:
            bands.append((set(free), max(0.0, left), st))
    rest = next((s for s in stmts if s["rest"]), None)
    covered = set().union(*[b[0] for b in bands]) if bands else set()
    if rest is not None:
        free = [i for i in range(len(GRADES)) if i not in covered]
        if not free:
            raise ValueError("'나머지'에 해당하는 학점이 없어요 — 이미 모든 학점을 말했어요")
        given = sum(b[1] for b in bands)
        v = max(0.0, 100.0 - given) if rest["v"] is None else rest["v"]
        bands.append((set(free), v, rest))
        covered |= set(free)
    total = round(sum(b[1] for b in bands), 4)
    if total > 100.01:
        raise ValueError(f"학점 비율 합이 {_num_text(total)}%라 100%를 넘어요"
                         " — 점수 기준(절대평가)이면 kind를 'absolute'로 알려 주세요")
    out: dict = {}
    exact = set()
    for idx, v, st in bands:
        idx = sorted(idx)
        if len(idx) == 1 and not st["cum"] and not st["rest"]:
            exact.add(GRADES[idx[0]])
        _split(v, idx, cur, out)
    free = [i for i in range(len(GRADES)) if i not in covered]
    left = 100.0 - total
    if free:
        _split(max(0.0, left), free, cur, out)
    elif left > 0.01:
        raise ValueError(f"학점 비율 합이 {_num_text(total)}%예요 — 모든 학점을 말했으니 합이 100%가 되게 알려 주세요")
    dist = _round100(out, exact)
    groups = []
    for st in sorted(stmts, key=lambda s: (s["lo"] if s["lo"] is not None else 99)):
        if st["rest"]:
            idx = sorted(next(b[0] for b in bands if b[2] is st))
        elif st["cum"]:
            idx = list(range(0, st["hi"] + 1))
        else:
            idx = list(range(st["lo"], st["hi"] + 1))
        groups.append({"label": st["label"], "idx": idx})
    return dist, groups, covered


def absolute_cutoffs(spec) -> dict:
    """사용자의 점수 기준 말({"A": 90, "B": 80} · {"A+": 95, "A0": 90}) → {학점: 100점 기준 최소 점수}(A+부터 내려감).
    맨 글자는 그 학점대의 최소(A → A0), 같은 대의 + 기준이 없으면 위 경계(A는 100, 아니면 위 학점대의 최소)와의 가운데."""
    if not isinstance(spec, dict) or not spec:
        raise ValueError("점수 기준은 {학점: 최소 점수} 모양이어야 해요(예: {\"A\": 90, \"B\": 80})")
    cut, bare = {}, {}
    for k, v in spec.items():
        s = _norm_letter_key(k)
        s = _CUM_SUFFIX.sub("", s) or s
        m = _LETTER.match(s)
        if not m or m.group(1) == "F":
            raise ValueError(f"'{_flat(k, 16)}'은(는) 점수 기준으로 쓸 학점이 아니에요 — A+~D0만(F는 기준 없음)")
        x = _number(v, f"{_flat(k, 16)} 기준", 0, 100)
        L, sgn = m.group(1), m.group(2)
        if sgn == "+":
            cut[L + "+"] = x
        elif sgn in ("0", "-"):
            cut[L + "0"] = x
        else:
            bare[L] = x
    for L, x in bare.items():
        cut.setdefault(L + "0", x)
    for L in "ABCD":
        if L in bare and L + "+" not in cut:
            up = 100.0 if L == "A" else next((cut[x] for x in reversed(GRADES[:_IDX[L + "+"]]) if x in cut), None)
            if up is not None and up > cut[L + "0"]:
                cut[L + "+"] = round((up + cut[L + "0"]) / 2, 2)
    cut = {x: cut[x] for x in GRADES if x in cut}
    problems = g.validate_cutoffs(cut)
    if problems:
        raise ValueError("점수 기준이 맞지 않아요: " + "; ".join(problems))
    return cut


def _basis_text(eff: dict) -> str:
    a = eff.get("auto") or {}
    if eff["by"] == "user":
        return "직접 넣은 비율"
    if eff["by"] == "syllabus":
        where = a.get("source_title") or "강의 글"
        return f"절대평가 기준({where})" if eff["kind"] == "absolute" else f"강의 글({where})"
    return "기본 비율"


def _runs(idx: list) -> list:
    out, run = [], []
    for i in idx:
        if run and i != run[-1] + 1:
            out.append(run)
            run = []
        run.append(i)
    if run:
        out.append(run)
    return out


def _run_label(run: list) -> str:
    return GRADES[run[0]] if len(run) == 1 else f"{GRADES[run[0]]}~{GRADES[run[-1]]}"


def _dist_lines(before_eff: dict, new_dist: dict | None, after_eff: dict, groups: list, mentioned: set) -> list:
    bd = before_eff["dist"] if before_eff["kind"] == "relative" else None
    ad = after_eff["dist"] if after_eff["kind"] == "relative" else None
    lines = []

    def total(d, idx):
        return None if d is None else sum(_fnum(d.get(GRADES[i])) or 0.0 for i in idx)
    for gr in groups:
        lines.append(_line(f"학점 비율 {gr['label']}", _pct(total(bd, gr["idx"])), _pct(total(ad, gr["idx"]))))
    changed = [i for i in range(len(GRADES)) if i not in mentioned
               and (bd is None or ad is None or round(total(bd, [i]), 2) != round(total(ad, [i]), 2))]
    if bd is not None and ad is not None:
        for run in _runs(changed):
            lines.append(_line(f"학점 비율 {_run_label(run)}", _pct(total(bd, run)), _pct(total(ad, run))))
    if (before_eff["by"], before_eff["kind"]) != (after_eff["by"], after_eff["kind"]):
        lines.append(_line("학점 기준", _basis_text(before_eff), _basis_text(after_eff)))
    return lines


def _set_dist(app, p: dict) -> dict:
    """grades.set_dist: 상대평가 학점 비율(글자별 % 또는 누적) / 절대평가 점수 기준 / reset(직접 넣은 비율 지우기)."""
    from . import gradebook
    store = app.store
    cid = _grade_course(store, p)
    if _pass_fail(store, cid):
        raise ValueError(f"{_short(store, cid)}은(는) P/NP 과목이라 학점 비율을 쓰지 않아요")
    before = _cfg(store, cid)
    before_eff = dm.effective(before)
    body = screen_body(before)
    if _bool(p.get("reset")):
        if not before.get("dist") and not before.get("cutoffs"):
            raise ValueError("직접 넣은 학점 비율이 없어요 — 이미 강의 글·기본 비율을 써요")
        body["dist"] = None
        if "cutoffs" in before:
            body["cutoffs"] = None
        after = _dry_save(store, cid, body)
        lines = _dist_lines(before_eff, None, dm.effective(after), [], set())
        return _result(_save_path(cid), body, lines + _auto_line(before))
    spec = _p(p, "dist", "grades", "ratios")
    kind = str(p.get("kind") or "relative").strip().lower()
    if kind not in ("relative", "absolute"):
        raise ValueError("kind는 'relative'(상대평가 %) 또는 'absolute'(절대평가 최저 점수)여야 해요")
    if kind == "absolute":
        cut = absolute_cutoffs(spec)
        old = before_eff["cutoffs"] or {}
        lines = [_line(f"학점 기준 {x}", f"{_num_text(old[x])}점 이상" if x in old else "—", f"{_num_text(v)}점 이상")
                 for x, v in cut.items() if old.get(x) != v]
        if before_eff["kind"] != "absolute" or before_eff["by"] != "user":
            lines.append(_line("학점 기준", _basis_text(before_eff), "직접 넣은 절대평가 기준"))
        if not getattr(gradebook, "USER_CUTOFFS", False):
            raise ValueError("절대평가 기준(학점별 최저 점수)은 아직 직접 넣을 수 없어요 — 성적 화면도 강의 글에서 찾은 기준만 써요. "
                             "학점 비율(%)이면 바꿀 수 있어요")
        body["dist"], body["cutoffs"] = None, cut
        _dry_save(store, cid, body)
        return _result(_save_path(cid), body, lines + _auto_line(before))
    dist, groups, mentioned = relative_dist(spec, before_eff["dist"] if before_eff["kind"] == "relative" else None,
                                            cumulative=_bool(p.get("cumulative")))
    if isinstance(before.get("dist"), dict) and all(abs((_fnum(before["dist"].get(x)) or 0) - dist[x]) < 0.005
                                                    for x in GRADES):
        raise ValueError("이미 그 학점 비율이에요 — 바꿀 것이 없어요")
    body["dist"] = dist
    if "cutoffs" in before:
        body["cutoffs"] = None
    after = _dry_save(store, cid, body)
    lines = _dist_lines(before_eff, dist, dm.effective(after), groups, mentioned)
    return _result(_save_path(cid), body, lines + _auto_line(before))


# ================================================================ 점수 후보 연결 · 개수

def _cands(store, cid: str | None = None) -> list:
    from . import gradebook
    return [c for c in gradebook.candidates(store) if c.get("key") and (cid is None or str(c.get("course_id")) == cid)]


def _cand_text(c: dict) -> str:
    label = _flat(c.get("label") or c.get("source_title") or "점수", 30)
    if c.get("score") is None:
        return label
    return f"{label} {_num_text(c['score'])}" + (f"/{_num_text(c['max_score'])}" if c.get("max_score") else "점")


def resolve_candidate(store, raw, cid: str | None) -> dict:
    """점수 후보: key 그대로, 아니면 이름(대소문자·띄어쓰기·문장부호 무시, 부분 이름도)이 하나로 정해질 때."""
    from .gradebook import _same_label
    s = _flat(raw, 300)
    if not s:
        raise ValueError("어느 점수 후보인지(key 또는 이름) 알려 주세요")
    pool = _cands(store, cid)
    hit = [c for c in pool if c["key"] == s]
    if not hit:
        k = _same_label(s)
        hit = [c for c in pool if k and _same_label(c.get("label")) == k]
        if not hit:
            hit = [c for c in pool if k and (k in _same_label(c.get("label"))
                                            or k in _same_label(c.get("source_title")))]
    if len(hit) == 1:
        return hit[0]
    if hit:
        opts = "; ".join(f"{_cand_text(c)} · {c.get('source_title') or c.get('source_type')} (key {c['key']})"
                         for c in hit[:5])
        raise ValueError(f"'{_flat(s)}'에 맞는 점수 후보가 여럿이에요: {opts} — key로 골라 주세요")
    where = _short(store, cid) + "의 " if cid else ""
    names = ", ".join(_cand_text(c) for c in pool[:8]) or "없음"
    raise ValueError(f"{where}점수 후보에 '{_flat(s)}'이(가) 없어요. 지금 점수 후보: {names}")


def _link_now(store, cid: str, cfg: dict, cand: dict) -> dict:
    from . import gradebook
    comps = _comps(cfg)
    if not comps:
        return {}
    mine = _cands(store, cid)
    return gradebook.link_candidates(store, cid, comps, mine).get(cand["key"]) or {}


def _link_text(ln: dict) -> str:
    if not ln:
        return "연결 안 됨"
    if ln.get("component") is None:
        return "넣지 않음(직접)" if ln.get("by") == "user" else "연결 안 됨"
    return f"{ln['component']}({_BY_KO.get(ln.get('by'), '자동')})"


def _cand_course(store, p: dict) -> tuple:
    """(과목 id, 점수 후보) — 과목을 안 줬으면 점수 후보에서."""
    from .store import hidden_courses
    raw_c = _p(p, "course", "course_id")
    cid = _grade_course(store, p) if _flat(raw_c) else None
    cand = resolve_candidate(store, _p(p, "score", "key", "candidate", "label"), cid)
    if cid is None:
        cid = str(cand.get("course_id")) if cand.get("course_id") is not None else None
        if cid is None:
            raise ValueError(f"'{_cand_text(cand)}'은(는) 과목을 모르는 점수 후보라 연결할 수 없어요")
        if cid in hidden_courses(store.data):
            raise ValueError(f"'{_short(store, cid)}'은(는) 제외한 과목이에요")
    return cid, cand


def _link_score(app, p: dict) -> dict:
    """grades.link_score: 점수 후보 → 평가 항목(사용자 연결, POST /api/grades/<id>/link)."""
    store = app.store
    cid, cand = _cand_course(store, p)
    cfg = _cfg(store, cid)
    name = need_component(store, cid, cfg, _p(p, "component", "to"))
    now = _link_now(store, cid, cfg, cand)
    if now.get("component") == name and now.get("by") == "user":
        raise ValueError(f"'{_cand_text(cand)}'은(는) 이미 '{name}'에 연결돼 있어요")
    body = {"key": cand["key"], "component": name}
    return _result(f"/api/grades/{cid}/link", body,
                   [_line(f"점수 '{_cand_text(cand)}'", _link_text(now), f"{name}(직접)")])


def _unlink_score(app, p: dict) -> dict:
    """grades.unlink_score: 점수 후보 연결 떼기 — 기본은 '이 과목 성적에 넣지 않음'(component null),
    reset이면 직접 정한 연결을 지워 자동(규칙·AI)으로 돌린다."""
    store = app.store
    cid, cand = _cand_course(store, p)
    cfg = _cfg(store, cid)
    now = _link_now(store, cid, cfg, cand)
    user = ((store.data.get("grade_links") or {}).get(cid) or {}).get(cand["key"])
    if _bool(p.get("reset")):
        if not isinstance(user, dict):
            raise ValueError(f"'{_cand_text(cand)}'은(는) 직접 정한 연결이 없어요 — 이미 자동으로 정해요")
        return _result(f"/api/grades/{cid}/link", {"key": cand["key"], "reset": True},
                       [_line(f"점수 '{_cand_text(cand)}'", _link_text(now), "자동으로 정함")])
    if now.get("component") is None and now.get("by") == "user":
        raise ValueError(f"'{_cand_text(cand)}'은(는) 이미 이 과목 성적에 넣지 않게 돼 있어요")
    return _result(f"/api/grades/{cid}/link", {"key": cand["key"], "component": None},
                   [_line(f"점수 '{_cand_text(cand)}'", _link_text(now), "넣지 않음")])


def _grades_course_view(app, cid: str) -> dict | None:
    """성적 화면이 보는 그 과목(개수·세부 점수가 붙은 설정) — 못 만들면 None(미리 보기에서 '모름')."""
    try:
        from .actions import _section
        sec = _section(app, "grades") or {}
    except Exception:   # 미리 보기 덤 — 화면 묶음을 못 만들어도 바꾸기는 경로가 검사한다
        return None
    return next((c for c in sec.get("courses") or [] if str(c.get("course_id")) == cid), None)


def _count_text(cnt: dict | None) -> str:
    if not isinstance(cnt, dict):
        return "모름"
    if cnt.get("n"):
        return f"{cnt['n']}개({_BASIS_KO.get(cnt.get('basis'), '자동')})"
    if cnt.get("low"):
        return f"{cnt['low']}개 이상(관찰)"
    return "모름"


def _set_count(app, p: dict) -> dict:
    """grades.set_count: 평가 항목의 학기 전체 개수(1~links.MAX_COUNT, null이면 직접 넣은 개수 지우기 — /count)."""
    store = app.store
    cid = _grade_course(store, p)
    cfg = _cfg(store, cid)
    name = need_component(store, cid, cfg, _p(p, "component", "name"))
    raw = _p(p, "n", "count")
    n = None if raw in (None, "") else _number(raw, f"'{name}' 개수", 1, lk.MAX_COUNT, integer=True)
    user = ((store.data.get("grade_counts") or {}).get(cid) or {}).get(lk.norm(name))
    if n is None and not isinstance(user, dict):
        raise ValueError(f"'{name}'에 직접 넣은 개수가 없어요 — 이미 자동으로 세요")
    if n is not None and isinstance(user, dict) and user.get("n") == n:
        raise ValueError(f"'{name}' 개수는 이미 {n}개예요 — 바꿀 것이 없어요")
    view = _grades_course_view(app, cid)
    comp = next((c for c in ((view or {}).get("config") or {}).get("components") or [] if c.get("name") == name), None)
    before = _count_text((comp or {}).get("count"))
    return _result(f"/api/grades/{cid}/count", {"component": name, "n": n},
                   [_line(f"{name} 개수", before, f"{n}개(직접)" if n else "자동으로 정함")])


# ================================================================ 마감·시험 → 평가 항목

def _view_items(store) -> list:
    from . import core, gradebook
    cache = store.data.get("cache")
    view = store.view()
    sem = core.semester_info(cache, view)
    core.annotate(view, sem, cache)
    gradebook.link_items(store, view, sem)
    return view


def _item_text(store, v: dict) -> str:
    from .timeutil import parse_iso
    d = parse_iso(v.get("due")) if v.get("due") else None
    when = f" {d.month}/{d.day}" if d else ""
    return f"'{_flat(v.get('title'), 30)}'({_short(store, v.get('course_id'))}{when})"


def resolve_item(store, raw, cid: str | None = None, view: list | None = None) -> dict:
    """항목 id 그대로, 아니면 제목(띄어쓰기·대소문자 무시, 부분 제목도 — 강의 영상 빼고). 같은 과제 후보 묶음(group_key)이면 하나로 본다."""
    from .store import group_key
    view = _view_items(store) if view is None else view
    s = _flat(raw, 200)
    if not s:
        raise ValueError("어느 마감·시험 항목인지 알려 주세요")
    by_id = {v["id"]: v for v in view}
    if s in by_id:
        return by_id[s]
    pool = [v for v in view if v.get("origin") != "video" and (cid is None or str(v.get("course_id")) == cid)]
    k = _key(s)
    hits = [v for v in pool if _key(v.get("title")) == k] or [v for v in pool if k and k in _key(v.get("title"))]
    hits = [v for v in hits if v.get("status") not in _SKIP_STATUS] or hits
    if len(hits) > 1:
        keys = {group_key(v) for v in hits}
        if len(keys) == 1 and None not in keys:
            hits = hits[:1]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"'{_flat(s)}'에 맞는 항목이 여럿이에요: {', '.join(_item_text(store, v) for v in hits[:5])}"
                         " — 과목이나 날짜를 함께 알려 주세요")
    raise ValueError(f"'{_flat(s)}' 마감·시험 항목을 찾지 못했어요" + (f"({_short(store, cid)})" if cid else ""))


def _item_component(app, p: dict) -> dict:
    """items.set_component: 마감·시험 항목 → 평가 항목(POST /api/item/<id>/component). exclude = 연결하지 않음,
    reset = 직접 정한 연결을 지워 자동(규칙·AI)으로."""
    from .store import hidden_courses
    store = app.store
    cid = _grade_course(store, p) if _flat(_p(p, "course", "course_id")) else None
    view = _view_items(store)
    it = resolve_item(store, _p(p, "item", "item_id", "title"), cid, view)
    if it.get("origin") == "video":
        raise ValueError("강의 영상 시청은 평가 항목에 연결하지 않아요")
    icid = str(it.get("course_id")) if it.get("course_id") is not None else None
    if icid is None or icid in hidden_courses(store.data):
        raise ValueError(f"{_item_text(store, it)}은(는) 성적 화면에 있는 과목의 항목이 아니에요")
    cfg = _cfg(store, icid)
    by = _BY_KO.get(it.get("component_by"), "자동")
    now = f"{it['component']}({by})" if it.get("component") else (
        "연결 안 함(직접)" if it.get("component_by") == "user" else "연결 안 됨")
    label = f"{_item_text(store, it)} 평가 항목"
    path = f"/api/item/{it['id']}/component"
    if _bool(p.get("reset")):
        if it.get("component_by") != "user":
            raise ValueError(f"{_item_text(store, it)}은(는) 직접 정한 연결이 없어요 — 이미 자동으로 정해요")
        return _result(path, {"reset": True}, [_line(label, now, "자동으로 정함")])
    if _bool(p.get("exclude")):
        if it.get("component_by") == "user" and not it.get("component"):
            raise ValueError(f"{_item_text(store, it)}은(는) 이미 평가 항목에 연결하지 않게 돼 있어요")
        return _result(path, {"component": None}, [_line(label, now, "연결 안 함")])
    name = need_component(store, icid, cfg, _p(p, "component", "name"))
    if it.get("component") == name and it.get("component_by") == "user":
        raise ValueError(f"{_item_text(store, it)}은(는) 이미 '{name}'에 연결돼 있어요")
    return _result(path, {"component": name}, [_line(label, now, f"{name}(직접)")])


# ================================================================ 등록

_REFUSE_SID = "학번은 개인정보라 에이전트가 바꾸지 않아요 — 설정 → 성적에서 직접 넣거나 지워 주세요"


def _refused(app, p):
    raise ValueError(_REFUSE_SID)


def _course_param(required: bool = True, desc: str = "과목(짧은 이름·과목 코드·id)") -> dict:
    return {"type": "str", "required": required, "desc": desc, "resolve": "course"}


def _comp_param(desc: str = "평가 항목 이름(별칭 가능 — 예: '과제' = 'Homework assignments')", required: bool = True,
                resolve: str | None = "component") -> dict:
    return {"type": "str", "required": required, "desc": desc, "resolve": resolve}


def _num_param(desc: str, required: bool = False, typ: str = "float", **extra) -> dict:
    """extra: 하네스의 값 검사(capabilities._typed)가 읽는 min·max, nullable(null = 지우기를 일부러 준 값으로)."""
    return {"type": typ, "required": required, "desc": desc, "resolve": None, **extra}


def _bool_param(desc: str) -> dict:
    return {"type": "bool", "required": False, "desc": desc, "resolve": None}


def _wrap(fn):
    def build(app, params):
        r = fn(app, dict(params or {}))
        return r["path"], r["body"]

    def preview(app, params):
        return fn(app, dict(params or {}))["lines"]
    return build, preview


def _specs() -> list:
    return [
        ("grades.set_weights", "grades", "평가 비중 전체 바꾸기", ("POST", "/api/grades/{course_id}"), _set_weights, {
            "course": _course_param(),
            "weights": {"type": "list", "items": "dict", "required": True, "resolve": None, "max": 20,
                        "desc": "[{name, weight(0~100)}] — 평가 항목 전체(목록에 없는 항목은 지움, 같은 이름의 점수는 그대로)"}}),
        ("grades.set_component", "grades", "평가 항목 하나 추가·비중 바꾸기", ("POST", "/api/grades/{course_id}"),
         _set_component, {
             "course": _course_param(),
             "component": _comp_param("평가 항목 이름 — 있으면 그 비중을 바꾸고, 없으면 신규로 추가", resolve=None),
             "weight": _num_param("비중 %(0~100)", required=True, min=0, max=100),
             "fixed": _bool_param("순위 계산에서 빼기(출석처럼 모두 비슷하게 받는 항목)")}),
        ("grades.remove_component", "grades", "평가 항목 지우기", ("POST", "/api/grades/{course_id}"), _remove_component, {
            "course": _course_param(),
            "component": _comp_param("지울 평가 항목"),
            "merge_into": _comp_param("비중을 더할 다른 평가 항목(선택)", required=False)}),
        ("grades.set_score", "grades", "평가 항목 점수 넣기", ("POST", "/api/grades/{course_id}"), _set_score, {
            "course": _course_param(),
            "component": _comp_param(),
            "score": _num_param("내 점수(clear가 아니면 필요)", min=0),
            "max_score": _num_param("만점(선택, 없으면 100)", min=0.0001),
            "mean": _num_param("반 평균(선택)", min=0),
            "sd": _num_param("표준편차(선택)", min=0),
            "clear": _bool_param("점수·만점·평균·표준편차를 모두 비우기")}),
        ("grades.set_dist", "grades", "학점 비율·기준 바꾸기", ("POST", "/api/grades/{course_id}"), _set_dist, {
            "course": _course_param(),
            "dist": {"type": "dict", "required": False, "resolve": None,
                     "desc": "상대평가: {학점: %} — 'A'(=A+·A0)·'A+'·'C+~F'·'B 이상'·'C 이하'·'나머지'(말하지 않은 학점은 남은 %를 지금 모양대로) / "
                             "절대평가: {학점: 최저 점수}"},
            "kind": {"type": "enum", "required": False, "enum": ["relative", "absolute"], "resolve": None,
                     "desc": "relative(상대평가 %, 기본) | absolute(절대평가 최저 점수)"},
            "cumulative": _bool_param("값이 위에서부터 누적 %('B 이상 70%')"),
            "reset": _bool_param("직접 넣은 학점 비율을 지우고 강의 글·기본 비율로")}),
        ("grades.set_class_size", "grades", "수강 인원 바꾸기", ("POST", "/api/grades/{course_id}"), _set_class_size, {
            "course": _course_param(),
            "size": _num_param("수강 인원(1~100000, null이면 지움)", required=True, typ="int", nullable=True, min=1,
                               max=100000)}),
        ("grades.link_score", "grades", "점수 후보를 평가 항목에 연결", ("POST", "/api/grades/{course_id}/link"), _link_score, {
            "course": _course_param(required=False, desc="과목(선택 — 없으면 점수 후보의 과목)"),
            "score": {"type": "str", "required": True, "resolve": None, "desc": "점수 후보 key 또는 이름(예: 'LAB3 보고서')"},
            "component": _comp_param("연결할 평가 항목")}),
        ("grades.unlink_score", "grades", "점수 후보 연결 떼기", ("POST", "/api/grades/{course_id}/link"), _unlink_score, {
            "course": _course_param(required=False, desc="과목(선택 — 없으면 점수 후보의 과목)"),
            "score": {"type": "str", "required": True, "resolve": None, "desc": "점수 후보 key 또는 이름"},
            "reset": _bool_param("'넣지 않음' 대신 직접 정한 연결을 지워 자동으로")}),
        ("grades.set_count", "grades", "평가 항목 개수 바꾸기", ("POST", "/api/grades/{course_id}/count"), _set_count, {
            "course": _course_param(),
            "component": _comp_param(),
            "n": _num_param(f"학기 전체 개수(1~{lk.MAX_COUNT}, null이면 직접 넣은 개수 지우기)", required=True, typ="int",
                            nullable=True, min=1, max=lk.MAX_COUNT)}),
        ("items.set_component", "items", "마감·시험을 평가 항목에 연결", ("POST", "/api/item/{item_id}/component"),
         _item_component, {
             "item": {"type": "str", "required": True, "resolve": "item", "desc": "마감·시험 항목(id 또는 제목)"},
             "component": _comp_param("연결할 평가 항목(exclude·reset이면 없어도 됨)", required=False),
             "course": _course_param(required=False, desc="과목(선택 — 같은 제목이 여러 과목에 있을 때)"),
             "exclude": _bool_param("평가 항목에 연결하지 않음"),
             "reset": _bool_param("직접 정한 연결을 지워 자동으로")}),
    ]


def register_all() -> None:
    """이 모듈의 능력을 capabilities.REGISTRY에 넣는다(여러 번 불러도 같음)."""
    from .capabilities import Capability, register
    for cid, area, title, route, fn, params in _specs():
        build, preview = _wrap(fn)
        register(Capability(id=cid, area=area, title=title, route=route, params=params, tier="direct",
                            build=build, preview=preview, undoable=True, external=False))
    register(Capability(id="grades.settings", area="grades", title="성적 기본 설정(학번)",
                        route=("POST", "/api/grades/settings"),
                        params={"student_id": {"type": "str", "required": False, "resolve": None,
                                               "desc": "점수표에서 내 줄을 찾는 학번(개인정보 — 거절)"}},
                        tier="refuse", build=_refused, preview=lambda app, p: [], undoable=False, external=False,
                        refuse=_REFUSE_SID, pane="grades"))
