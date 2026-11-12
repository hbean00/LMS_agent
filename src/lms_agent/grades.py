"""성적 위치 어림: 비공개로 받은 내 점수 + 공지된 평균·표준편차 → 반에서의 위치, 예상 학점,
남은 시험에 필요한 점수, 먼저 챙길 과목.

전부 추정이다. 정규분포, 평가 항목 사이 상관(ρ), 학점 비율을 가정하므로 결과마다 notes에 그 가정을 적는다.
엑셀(xlsx)에서는 본인 행과 반 전체 집계만 돌려준다. 다른 학생의 행·학번은 결과에 넣지 않는다.
"""
from __future__ import annotations

import io
import math
import posixpath
import re
import statistics
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from statistics import NormalDist

GRADES = ["A+", "A0", "B+", "B0", "C+", "C0", "D+", "D0", "F"]
# 과목 인원 대비 % (과목마다 사용자가 바꿀 수 있다). D+/D0는 거의 안 주므로 0%.
DEFAULT_DISTRIBUTION = {"A+": 15, "A0": 15, "B+": 15, "B0": 15, "C+": 15, "C0": 20, "D+": 0, "D0": 0, "F": 5}

_PHI = NormalDist()
RHO_RANGE = (0.3, 0.9)
DISCLAIMER = "공개된 평균·표준편차로 어림한 추정치 — 실제 순위·학점과 다를 수 있음"
NORMAL_NOTE = "정규분포 근사: 점수가 종 모양으로 퍼져 있다고 가정 — 분포가 치우치면 오차가 커짐"


@dataclass
class Component:
    """과목의 평가 항목 하나(예: 중간고사 30%). 점수·평균·표준편차는 같은 만점 기준."""
    name: str
    weight: float                    # 과목 전체 중 % (예: 30)
    score: float | None = None
    max_score: float | None = None   # None이면 100점 만점
    mean: float | None = None
    sd: float | None = None
    fixed: bool = False              # 모두 거의 같은 점수(출석 만점 등) → 순위에 영향 없음


# ---------------------------------------------------------------- 기본 함수

def validate_distribution(dist: dict) -> list[str]:
    """학점 비율의 문제를 한국어로 돌려준다. 빈 리스트면 정상."""
    if not isinstance(dist, dict):
        return ["학점 비율은 {학점: 비율%} 모양이어야 합니다"]
    out = []
    missing = [g for g in GRADES if g not in dist]
    if missing:
        out.append(f"빠진 학점: {', '.join(missing)}")
    extra = [str(k) for k in dist if k not in GRADES]
    if extra:
        out.append(f"모르는 학점: {', '.join(extra)}")
    total, numeric = 0.0, True
    for g in GRADES:
        if g not in dist:
            continue
        v = dist[g]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            out.append(f"{g} 비율이 숫자가 아닙니다: {v!r}")
            numeric = False
            continue
        if v < 0:
            out.append(f"{g} 비율이 음수입니다: {v:g}%")
        total += v
    if numeric and abs(total - 100) > 0.01:
        out.append(f"비율 합이 {round(total, 2):g}%입니다(100%여야 함)")
    return out


def top_percent(score: float, mean: float, sd: float) -> float:
    """'상위 x%' = 100·(1−Φ(z)). sd<=0이면 ValueError."""
    if sd is None or sd <= 0:
        raise ValueError("표준편차는 0보다 커야 합니다")
    return 100.0 * (1.0 - _PHI.cdf((score - mean) / sd))


def sd_from_quartiles(q1: float, q3: float) -> float:
    """정규분포에서 사분위 범위(IQR) = 1.349σ."""
    if q3 <= q1:
        raise ValueError("3사분위수가 1사분위수보다 커야 합니다")
    return (q3 - q1) / 1.349


def _bands(dist: dict) -> list[tuple[str, float, float]]:
    """비율 > 0인 학점마다 (학점, 위 경계 상위%, 아래 경계 상위%). 0%인 학점은 경계가 없다."""
    out, cum = [], 0.0
    for g in GRADES:
        share = float(dist.get(g, 0) or 0)
        if share > 0:
            out.append((g, cum, cum + share))
            cum += share
    return out


def letter_for(top: float, dist: dict = DEFAULT_DISTRIBUTION) -> str:
    """상위 top% → 학점(GRADES 순서로 누적한 경계, 경계값은 위 학점). 0%인 학점은 건너뛴다."""
    bands = _bands(dist)
    if not bands:
        raise ValueError("학점 비율이 모두 0입니다")
    for g, _, hi in bands:
        if top <= hi + 1e-9:
            return g
    return bands[-1][0]


def _check(dist: dict, rho: float) -> None:
    problems = validate_distribution(dist)
    if problems:
        raise ValueError("학점 비율 오류: " + "; ".join(problems))
    if not 0 <= rho <= 1:
        raise ValueError("상관 ρ는 0~1 사이여야 합니다")


def _as_component(c) -> Component:
    if isinstance(c, Component):
        return c
    if isinstance(c, dict):
        return Component(**{k: c[k] for k in ("name", "weight", "score", "max_score", "mean", "sd", "fixed") if k in c})
    raise TypeError("평가 항목은 Component나 dict여야 합니다")


def _weight(c: Component) -> float:
    w = c.weight
    if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0:
        raise ValueError(f"{c.name}: 비중은 0 이상의 숫자여야 합니다")
    return float(w)


def _max(c: Component) -> float:
    mx = 100.0 if c.max_score is None else float(c.max_score)
    if mx <= 0:
        raise ValueError(f"{c.name}: 만점은 0보다 커야 합니다")
    return mx


def _status(c: Component) -> tuple[str, str | None]:
    if c.fixed:
        return "fixed", None
    lack = [n for n, v in (("점수", c.score), ("평균", c.mean), ("표준편차", c.sd)) if v is None]
    if lack:
        return "missing", "·".join(lack) + " 없음"
    if c.sd < 0:
        raise ValueError(f"{c.name}: 표준편차는 음수일 수 없습니다")
    if c.sd == 0:
        return "fixed", "표준편차 0"
    return "known", None


def _sigma(ws: list[tuple[float, float]], rho: float) -> float:
    """σ_T² = Σw²σ̃² + 2ρΣ_{i<j}w_iw_jσ̃_iσ̃_j = (1−ρ)Σa² + ρ(Σa)², a = wσ̃."""
    a = [w * s for w, s in ws]
    var = (1 - rho) * sum(x * x for x in a) + rho * sum(a) ** 2
    return math.sqrt(var) if var > 0 else 0.0


def _dist_note(dist: dict) -> str:
    body = " · ".join(f"{g} {dist[g]:g}" for g in GRADES if dist.get(g))
    return f"학점 비율 가정: {body}%" + (" (기본값 — 강의계획서 비율로 바꿀 수 있음)" if dist == DEFAULT_DISTRIBUTION else "")


# ---------------------------------------------------------------- 위치 추정

def estimate(components: list, dist: dict = DEFAULT_DISTRIBUTION, rho: float = 0.6,
             class_size: int | None = None) -> dict:
    """점수·평균·표준편차가 다 있는 항목만으로 반에서의 위치(상위 %)와 예상 학점을 어림한다."""
    dist = DEFAULT_DISTRIBUTION if dist is None else dist
    _check(dist, rho)
    if class_size is not None and (isinstance(class_size, bool) or int(class_size) < 1):
        raise ValueError("수강 인원은 1 이상이어야 합니다")
    known, fixed, missing, rows, odd = [], [], [], [], []
    for c in map(_as_component, components):
        w = _weight(c)
        st, why = _status(c)
        top = None
        if st == "known":
            mx = _max(c)
            x, m, s = c.score / mx, c.mean / mx, c.sd / mx
            known.append((w, x, m, s))
            top = top_percent(x, m, s)
            if not (0 <= c.score <= mx and 0 <= c.mean <= mx):
                odd.append(c.name)
        elif st == "fixed":
            fixed.append(f"{c.name}({w:g}%" + (f", {why})" if why else ")"))
        else:
            missing.append(f"{c.name}({w:g}%, {why})")
        rows.append({"name": c.name, "weight": w, "top": top, "status": st})
    if not known:
        raise ValueError("점수·평균·표준편차가 모두 있는 평가 항목이 없어 위치를 추정할 수 없습니다")

    total = sum(w * x for w, x, _, _ in known)
    mu = sum(w * m for w, _, m, _ in known)

    def top_at(r: float) -> float:
        sig = _sigma([(w, s) for w, _, _, s in known], r)
        if sig <= 0:
            raise ValueError("반영된 항목의 비중이 모두 0이라 계산할 수 없습니다")
        return top_percent(total, mu, sig)

    top = top_at(rho)
    ends = [top_at(r) for r in RHO_RANGE]
    lo, hi = min(ends + [top]), max(ends + [top])  # 기본 ρ(0.3~0.9 안)에서는 두 끝값 그대로
    done_w = sum(w for w, *_ in known)
    all_w = sum(r["weight"] for r in rows)

    notes = [DISCLAIMER, NORMAL_NOTE]
    if len(known) >= 2:
        notes.append(f"평가 항목끼리의 상관 ρ={rho:g} 가정(실제 값은 모름) — ρ 0.3~0.9면 상위 {lo:.1f}~{hi:.1f}%")
    else:
        notes.append("반영된 항목이 하나라 상관 ρ 가정은 결과에 영향 없음")
    if missing:
        notes.append("점수·평균·표준편차가 다 있지 않아 제외: " + ", ".join(missing))
    if fixed:
        notes.append("모두 비슷하게 받는 항목으로 보고 순위 계산에서 제외: " + ", ".join(fixed))
    notes.append(f"반영된 비중 {done_w:g}%만으로 계산" + (f"(전체 {all_w:g}% 중)" if all_w > done_w else ""))
    if abs(all_w - 100) > 0.01:
        notes.append(f"항목 비중 합이 {all_w:g}%입니다(100%가 아님) — 강의계획서 확인")
    if odd:
        notes.append("점수나 평균이 0~만점 범위를 벗어남(만점 확인 필요): " + ", ".join(odd))
    notes.append(_dist_note(dist))
    rank = None
    if class_size is not None:
        n = int(class_size)
        rank = max(1, min(n, round(top / 100 * n)))
        notes.append(f"석차는 수강 인원 {n}명 × 상위 비율로 어림한 값")
    return {"top": top, "range": [lo, hi], "grade": letter_for(top, dist),
            "grade_range": [letter_for(lo, dist), letter_for(hi, dist)], "rank": rank,
            "completed_weight": done_w, "components": rows, "notes": notes}


def _cutoff(target: str, dist: dict) -> float:
    return sum(float(dist.get(g, 0) or 0) for g in GRADES[:GRADES.index(target) + 1])


def needed_on(components: list, remaining_name: str, target: str, dist: dict = DEFAULT_DISTRIBUTION,
              rho: float = 0.6) -> dict:
    """남은 항목 remaining_name에서 target 학점 이상이 되려면 몇 점(상위 몇 %)이 필요한지 어림한다."""
    dist = DEFAULT_DISTRIBUTION if dist is None else dist
    _check(dist, rho)
    if target not in GRADES:
        raise ValueError(f"모르는 학점: {target}")
    comps = [_as_component(c) for c in components]
    k = next((c for c in comps if c.name.strip() == str(remaining_name).strip()), None)
    if k is None:
        raise ValueError(f"'{remaining_name}' 평가 항목이 없습니다")
    if k.fixed:
        raise ValueError(f"{k.name}은(는) 모두 비슷하게 받는 항목이라 필요 점수를 계산할 수 없습니다")
    wk = _weight(k)
    if wk <= 0:
        raise ValueError(f"{k.name}의 비중이 0입니다")
    mxk = _max(k)
    known, ignored = [], []
    for c in comps:
        if c is k:
            continue
        w = _weight(c)
        st, why = _status(c)
        if st == "known":
            mx = _max(c)
            known.append((w, c.score / mx, c.mean / mx, c.sd / mx))
        else:
            ignored.append(f"{c.name}({w:g}%, {'고정 항목' if st == 'fixed' else why})")

    notes = [DISCLAIMER, NORMAL_NOTE, f"평가 항목끼리의 상관 ρ={rho:g} 가정(실제 값은 모름)"]
    if k.mean is not None and k.sd is not None and k.sd > 0:
        mk, sk = k.mean / mxk, k.sd / mxk
        notes.append(f"{k.name}의 평균·표준편차는 입력한 값({k.mean:g}, {k.sd:g}) 사용")
    else:
        wsum = sum(w for w, *_ in known)
        if not known or wsum <= 0:
            raise ValueError("이미 본 항목의 평균·표준편차가 없어 남은 항목을 가정할 수 없습니다")
        mk = sum(w * m for w, _, m, _ in known) / wsum
        sk = sum(w * s for w, _, _, s in known) / wsum
        notes.append(f"{k.name}의 평균·표준편차는 아직 모르므로 이미 본 항목들의 비중 가중 평균과 같다고 가정"
                     f"(평균 만점의 {mk * 100:.1f}%, 표준편차 만점의 {sk * 100:.1f}%)")
    if k.score is not None:
        notes.append(f"{k.name}에 입력된 점수는 무시하고 남은 항목으로 계산")
    if ignored:
        notes.append("계산에서 뺀 항목: " + ", ".join(ignored))
    cutoff = _cutoff(target, dist)
    notes.append(f"목표 {target} 이상 = 상위 {cutoff:g}% 안 (" + _dist_note(dist) + ")")
    res = {"target": target, "possible": True, "already": False, "need_top": None, "need_score": None, "notes": notes}
    if cutoff >= 100 - 1e-9:
        res.update(already=True, need_top=100.0, need_score=0.0)
        notes.append(f"{target}은(는) 가장 낮은 구간이라 점수와 상관없이 해당")
        return res
    if cutoff <= 1e-9:
        res["possible"] = False
        notes.append(f"{target}까지의 학점 비율이 0%라 도달할 수 없음")
        return res

    items = known + [(wk, None, mk, sk)]
    mu = sum(w * m for w, _, m, _ in items)
    sig = _sigma([(w, s) for w, _, _, s in items], rho)
    done = sum(w * x for w, x, _, _ in known)
    need_total = mu + _PHI.inv_cdf(1 - cutoff / 100) * sig
    xk = (need_total - done) / wk
    if xk > 1:
        res["possible"] = False
        notes.append(f"{k.name} 만점을 받아도 {target}에 못 미칠 가능성이 큼(계산상 만점의 {xk * 100:.0f}% 필요)")
        return res
    need_top = top_percent(xk, mk, sk)
    already = need_top >= 100 or xk <= 0
    res.update(already=already, need_top=need_top, need_score=max(xk, 0.0) * mxk)
    if already:
        notes.append(f"{k.name}을(를) 0점 받아도 계산상 {target} 이상 — 결시·과락 규정은 따로 확인")
    else:
        notes.append(f"{k.name}에서 약 {res['need_score']:.1f}/{mxk:g}점(상위 {need_top:.1f}%) 이상이면 {target} 이상(추정)")
    return res


def _z(top: float) -> float:
    t = min(max(top, 1e-9), 100 - 1e-9)
    return _PHI.inv_cdf(1 - t / 100)


def priorities(courses: list[dict]) -> list[dict]:
    """학점 경계에 가깝고 남은 비중이 큰 과목부터. 남은 평가가 없는 과목은 맨 뒤."""
    out = []
    for i, c in enumerate(courses):
        dist = c.get("dist") or DEFAULT_DISTRIBUTION
        est = c.get("estimate") or {}
        rw = float(c.get("remaining_weight") or 0)
        if est.get("top") is None:
            out.append((False, 0.0, i, {"course": c.get("course"), "score": 0.0, "grade": None,
                                        "reason": "점수·평균·표준편차가 부족해 추정 불가"}))
            continue
        top = float(est["top"])
        grade = letter_for(top, dist)
        if rw <= 0:
            out.append((False, 0.0, i, {"course": c.get("course"), "score": 0.0, "grade": grade, "reason": "남은 평가 없음"}))
            continue
        bands = _bands(dist)
        j = next(n for n, b in enumerate(bands) if b[0] == grade)
        _, up_b, down_b = bands[j]
        z = _z(top)
        gap_up = max(0.0, _z(up_b) - z) if j > 0 else math.inf
        gap_down = max(0.0, z - _z(down_b)) if j < len(bands) - 1 else math.inf
        up = 0.0 if math.isinf(gap_up) else 1 / (0.15 + gap_up)
        down = 0.0 if math.isinf(gap_down) else 0.5 / (0.15 + gap_down)
        score = rw / 100 * (up + down)
        if up == 0 and down == 0:
            why = "학점 경계 없음"
        elif up >= down:
            better = bands[j - 1][0]
            why = f"{grade}/{better} 경계까지 {'가까움' if gap_up < 0.5 else '거리 있음'}(z {gap_up:.2f})"
        elif gap_down < 0.5:
            why = f"{grade} 아래로 떨어질 위험(z {gap_down:.2f})"
        else:
            why = f"{grade} 유지(아래 경계까지 z {gap_down:.2f})"
        out.append((True, score, i, {"course": c.get("course"), "score": score, "grade": grade,
                                     "reason": f"{why} · 남은 비중 {rw:g}%"}))
    out.sort(key=lambda t: (not t[0], -t[1], t[2]))
    return [t[3] for t in out]


# ---------------------------------------------------------------- 글에서 점수 찾기

@dataclass
class ScoreFound:
    label: str | None
    score: float | None = None
    max_score: float | None = None
    mean: float | None = None
    sd: float | None = None
    median: float | None = None
    q1: float | None = None
    q3: float | None = None
    high: float | None = None
    low: float | None = None
    sentence: str = ""
    notes: list[str] = field(default_factory=list)


_FIELD_KO = {"score": "점수", "max_score": "만점", "mean": "평균", "sd": "표준편차", "median": "중앙값",
             "q1": "1사분위수", "q3": "3사분위수", "high": "최고점", "low": "최저점"}

# 숫자 뒤에 날짜·시간·인원·비율 단위가 오면 점수가 아니다. 5자리 이상(학번·전화)은 숫자로 보지 않는다.
_BAD_AFTER = (r"(?:%|퍼센트|월|일|시(?!험)|분(?!포)|초|년|회|명|등|위|번|차|주|학년|학기|교시|호|층|쪽|개|"
              r"percent|(?:st|nd|rd|th)(?![a-z]))")


def _num(name: str, after_slash: bool = False) -> str:
    lb = r"(?<![\d.])(?<!\d:)(?<!\d-)" + ("" if after_slash else r"(?<!\d/)")
    return (lb + rf"(?P<{name}>\d{{1,4}}(?:\.\d+)?)(?!\d)(?!\.\d)(?!\s*{_BAD_AFTER})"
            r"(?!\s*:\s*\d)(?!\s*-\s*\d)(?!\.\s+\d{1,2}\s*\.)")


_UNIT = r"(?:\s*(?:점|(?:pts?|points?)(?![a-z])\.?))?"
_SEP = (r"(?:\s*\([^()\d]{0,15}\))?\s*(?:점수|score)?\s*"
        r"(?:[:：=]|은|는|이|가|으로|에서|(?<![a-z])(?:of|was|is|were|are)(?![a-z]))?\s*(?:약|대략|about|approx\.?|around)?\s*")
_KW = {
    "mean": r"평균\s*(?:점수|값|점)?|(?<![a-z])(?:class\s+)?(?:mean|average|avg\.?)(?![a-z])|(?<![a-z])m(?=\s*=)",
    "sd": r"표준\s*편차|표편|(?<![a-z])(?:s\.d\.?|sd|std\.?\s*dev\.?|stdev|std\.?|standard\s+deviation)(?![a-z])|σ",
    "median": r"중앙\s*값|중간\s*값|중위수|(?<![a-z])median(?![a-z])",
    "q1": r"(?<!\d)(?:제\s*)?1\s*사분위\s*(?:수|값|점)?|(?<![a-z])(?:q1|first\s+quartile|25th\s+percentile)(?![a-z])",
    "q3": r"(?<!\d)(?:제\s*)?3\s*사분위\s*(?:수|값|점)?|(?<![a-z])(?:q3|third\s+quartile|75th\s+percentile)(?![a-z])",
    "high": r"최고\s*(?:점수|득점|점)?|최댓값|최대값|(?<![a-z])(?:max(?:imum)?|highest(?:\s+score)?|high)(?![a-z])",
    "low": r"최저\s*(?:점수|득점|점)?|최솟값|최소값|(?<![a-z])(?:min(?:imum)?|lowest(?:\s+score)?|low)(?![a-z])",
    "score": (r"(?:본인|내|나의|귀하의|학생|취득|획득|받은|원)\s*(?:점수|성적)|본인|점수|성적|득점|"
              r"(?<![a-z])(?:your\s+score|score|you\s+(?:got|scored|received|earned)|scored|earned)(?![a-z])"),
}
_ORDER = ["mean", "sd", "median", "q1", "q3", "high", "low", "score"]
_FIELD_RX = {f: re.compile(rf"(?:{_KW[f]}){_SEP}{_num('v')}{_UNIT}", re.I) for f in _ORDER}
_ANYKW = "|".join(f"(?:{_KW[f]})" for f in _ORDER)
_JOIN = r"\s*(?:과|와|및|,|/|and|&|\()\s*"
_PAIR = re.compile(rf"(?P<a>{_ANYKW}){_JOIN}(?P<b>{_ANYKW})\s*\)?\s*(?:은|는|이|가|[:：=]|were|are|is)?\s*"
                   rf"(?:각각|respectively)?\s*{_num('v1')}{_UNIT}{_JOIN}{_num('v2', True)}{_UNIT}\s*\)?", re.I)
_PM = re.compile(rf"{_num('v1')}{_UNIT}\s*(?:±|\+/-|\+-)\s*{_num('v2')}{_UNIT}", re.I)
_TAIL = re.compile(rf"\s*/\s*{_num('m', True)}{_UNIT}", re.I)
_MAX_A = re.compile(rf"{_num('v')}\s*점\s*만점", re.I)
_MAX_B = re.compile(rf"(?:만점|배점|full\s+marks?|out\s+of)\s*(?:은|는|이|[:：=])?\s*{_num('v')}{_UNIT}", re.I)
_AFTER_MAX = re.compile(rf"\s*(?:중(?:에서|에)?|에서|에|기준(?:으로)?)\s*,?\s*(?:본인\s*|내\s*)?(?:점수\s*)?(?:는|은|:)?\s*"
                        rf"{_num('v')}{_UNIT}", re.I)
_GOT = re.compile(rf"{_num('v')}\s*점\s*(?:을|를)?\s*(?:받|획득|취득|맞)", re.I)
_FRAC = re.compile(rf"{_num('v')}(?P<u1>\s*점)?\s*/\s*{_num('m', True)}(?P<u2>\s*점)?", re.I)

_LW_ONE = (r"중간\s*고사|기말\s*고사|중간\s*(?:시험|평가)|기말\s*(?:시험|평가)|midterm(?:\s+exam)?|"
           r"final(?:\s+exam)?(?!\s+(?:grade|score|total|mark|result))|시험|exam")
_LW_NUM = r"쪽지\s*시험|퀴즈|과제|숙제|레포트|리포트|보고서|프로젝트|실습|quiz|homework|hw|assignment|project|report|lab"
_LNUM = r"(?!\d)(?!\s*(?:월|일|시|분|%|점|회|주|명|번|차|/\s*\d|\.\d|:\d|-\s*\d))"
_LABEL_P = (rf"(?<![a-z])(?:(?P<n2>\d{{1,2}})\s*(?:차|번째|회차?)\s*(?P<w2>{_LW_NUM}|{_LW_ONE})"
            rf"|(?P<w1>{_LW_ONE})|(?P<w>{_LW_NUM})(?:\s*#?\s*(?P<n>\d{{1,2}}){_LNUM})?)(?![a-z])")
_LABEL = re.compile(_LABEL_P, re.I)
_LABEL_SCORE = re.compile(rf"(?:{_LABEL_P})(?:\s*\([^()]{{0,20}}\))?\s*(?P<sep>[:：=]|은|는|에서는?)?\s*"
                          rf"{_num('v')}(?P<unit>{_UNIT})(?:\s*/\s*{_num('m', True)}{_UNIT})?", re.I)

_BLANKS = [
    re.compile(r"(?<!\d)(?:\d{4}|\d{2})\s*(?P<s>[.\-/])\s*\d{1,2}\s*(?P=s)\s*\d{1,2}(?!\d)\.?"),        # 2026.11.03
    re.compile(r"(?<!\d)\d{4}\s*년(?:\s*\d{1,2}\s*월)?(?:\s*\d{1,2}\s*일)?"),
    re.compile(r"(?i)(?<![\d.])\d{1,2}\s*[./]\s*\d{1,2}\s*\.?\s*\(\s*(?:[월화수목금토일]|(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)\.?\s*\)"),
    re.compile(r"(?<![\d.])\d{1,2}\.\s+\d{1,2}\.(?!\d)"),                                                 # 11. 3.
    re.compile(r"(?<![\d.])(?:[01]?\d|2[0-4]):[0-5]\d(?!\d)(?!\s*(?:/|점))"),                             # 23:59
    re.compile(r"(?<!\d)\d{2,3}\s*-\s*\d{3,4}\s*-\s*\d{4}(?!\d)"),                                       # 전화
    re.compile(r"(?i)학번\s*[:：]?\s*\d+|student\s*(?:id|no\.?|number)\s*[:#：]?\s*\d+|(?<![a-z])id\s*[:#：]\s*\d+"),
    re.compile(r"^\s*\d{1,2}[.)]\s"),                                                                    # 목록 번호
]
_ALLOC = re.compile(r"배점|반영|비율|비중|평가\s*(?:방법|기준|요소)|구성|산출|grading|weight", re.I)
_WHEN = re.compile(r"\d{1,2}:\d{2}|[월화수목금토일]요일|\(\s*[월화수목금토일]\s*\)|까지|마감|실시|시행|예정|진행|일정|장소|강의실|"
                   r"고사장|오전|오후|(?<![a-z])(?:am|pm|at|on|due)(?![a-z])", re.I)
_SENT = re.compile(r"(?<=[가-힣)\]][.!?])\s+|(?<=[a-z]{2}[.!?])\s+(?=[A-Z가-힣\[(])")
_TAG = re.compile(r"<[a-zA-Z/][^>]*>")


def _mask(s: str, a: int, b: int) -> str:
    return s[:a] + " " * (b - a) + s[b:]


def _blank(text: str) -> str:
    s = text
    for rx in _BLANKS:
        for m in list(rx.finditer(s)):
            s = _mask(s, m.start(), m.end())
    return s


def _field_of(word: str) -> str | None:
    w = word.strip()
    for f in _ORDER:
        if re.fullmatch(_KW[f], w, re.I):
            return f
    return None


def _label_of(m: re.Match) -> tuple[str, tuple[str, str | None]]:
    w = m.group("w1") or m.group("w") or m.group("w2")
    n = m.group("n") or m.group("n2")
    n = str(int(n)) if n else None
    low = re.sub(r"\s+", "", w.lower())
    ko = bool(re.match(r"[가-힣]", w))
    table = [("midterm", ("중간",), "중간고사", "Midterm"), ("final", ("기말", "final"), "기말고사", "Final"),
             ("quiz", ("퀴즈", "쪽지", "quiz"), "퀴즈", "Quiz"), ("hw", ("과제", "숙제", "homework", "hw"), "과제", "HW"),
             ("hw", ("assignment",), "과제", "Assignment"), ("report", ("레포트", "리포트", "보고서", "report"), low, "Report"),
             ("project", ("프로젝트", "project"), "프로젝트", "Project"), ("lab", ("실습", "lab"), "실습", "LAB"),
             ("exam", ("시험", "exam"), "시험", "Exam")]
    kind, disp = "exam", w
    for k, prefixes, ko_name, en_name in table:
        if low.startswith(prefixes) or (k == "midterm" and low.startswith("midterm")):
            kind, disp = k, (ko_name if ko else en_name)
            break
    return (f"{disp} {n}" if n else disp), (kind, n)


def _unit_label(text: str):
    best = None
    for m in _LABEL.finditer(_blank(text)):
        lab = _label_of(m)
        if lab[1][0] != "exam":
            return lab
        best = best or lab
    return best


def _segments(sentence: str) -> list[str]:
    """한 문장에 다른 평가 이름이 여럿 있으면(중간고사 …, 기말고사 …) 이름마다 나눈다."""
    cuts, prev = [0], None
    for m in _LABEL.finditer(_blank(sentence)):
        key = _label_of(m)[1]
        if key[0] == "exam":
            continue
        if prev is not None and key != prev:
            cuts.append(m.start())
        prev = key
    cuts.append(len(sentence))
    return [sentence[a:b].strip(" ,;/·") for a, b in zip(cuts, cuts[1:]) if sentence[a:b].strip(" ,;/·")]


def _scan(text: str, whole: str) -> tuple[dict, list[str]]:
    """한 조각에서 숫자를 항목별로 뽑는다. 쓴 숫자는 공백으로 지워 두 번 쓰지 않는다."""
    s = _blank(text)
    vals: dict[str, float] = {}
    notes: list[str] = []

    def put(f: str, v) -> None:
        v = float(v)
        if f in vals:
            if vals[f] != v:
                notes.append(f"{_FIELD_KO[f]} 값이 여러 개({vals[f]:g}, {v:g}) — 앞의 값 사용")
            return
        vals[f] = v

    def tail(end: int, v: float) -> int:  # "78 / 100"의 100 → 만점
        t = _TAIL.match(s, end)
        if t and float(t["m"]) >= v:
            put("max_score", t["m"])
            return t.end()
        return end

    for m in list(_PAIR.finditer(s)):
        fa, fb = _field_of(m["a"]), _field_of(m["b"])
        if fa and fb and fa != fb:
            put(fa, m["v1"])
            put(fb, m["v2"])
            s = _mask(s, m.start(), m.end())
    for m in list(_PM.finditer(s)):
        put("mean", m["v1"])
        put("sd", m["v2"])
        s = _mask(s, m.start(), m.end())
    for rx in (_MAX_A, _MAX_B):
        for m in list(rx.finditer(s)):
            put("max_score", m["v"])
            end = m.end()
            a = _AFTER_MAX.match(s, end)
            if a and "score" not in vals:
                put("score", a["v"])
                end = a.end()
            s = _mask(s, m.start(), end)
    for f in _ORDER:
        for m in list(_FIELD_RX[f].finditer(s)):
            put(f, m["v"])
            s = _mask(s, m.start(), tail(m.end(), float(m["v"])))
    if "score" not in vals:
        m = _GOT.search(s)
        if m:
            put("score", m["v"])
            s = _mask(s, m.start(), m.end())
    if "score" not in vals and not _ALLOC.search(whole):
        m = _LABEL_SCORE.search(s)
        if m:
            v, mx = float(m["v"]), (float(m["m"]) if m["m"] else None)
            context = bool(vals) or bool(m["unit"].strip())
            datey = mx is not None and not m["unit"].strip() and v <= 12 and mx <= 31 and v.is_integer() and mx.is_integer()
            ok = (m["sep"] or context or mx is not None) and (mx is None or v <= mx)
            if datey and (_WHEN.search(whole) or not (m["sep"] or context)):
                ok = False
            if ok:
                put("score", v)
                if mx is not None:
                    put("max_score", mx)
                s = _mask(s, m.start(), m.end())
    if "score" not in vals:
        m = _FRAC.search(s)
        if m and (m["u1"] or m["u2"]) and float(m["v"]) <= float(m["m"]):
            put("score", m["v"])
            put("max_score", m["m"])
    return vals, notes


def _units(text: str):
    """줄 → 문장 → 평가 이름별 조각. 빈 줄은 None(묶음 끊기)."""
    for line in text.split("\n"):
        if not line.strip():
            yield None
            continue
        for sent in _SENT.split(line):
            for seg in _segments(sent.strip()):
                vals, notes = _scan(seg, sent)
                yield {"text": seg, "label": _unit_label(seg), "vals": vals, "notes": notes}


def _finish(g: dict) -> ScoreFound | None:
    v, notes = dict(g["vals"]), list(g["notes"])
    if v.get("sd") is None and v.get("q1") is not None and v.get("q3") is not None:
        if v["q3"] > v["q1"]:
            v["sd"] = round(sd_from_quartiles(v["q1"], v["q3"]), 2)
            notes.append(f"표준편차가 없어 사분위수로 추정: (Q3 {v['q3']:g} − Q1 {v['q1']:g}) / 1.349 ≈ {v['sd']:.2f} (정규분포 가정)")
        else:
            notes.append("3사분위수가 1사분위수보다 크지 않아 표준편차를 추정하지 못함")
    if all(v.get(k) is None for k in ("score", "mean", "sd")):
        return None
    label = g["label"][0] if g["label"] else None
    if label is None:
        notes.append("어느 평가인지 못 찾음 — 직접 골라 주세요")
    if v.get("score") is None:
        notes.append("본인 점수 없음 — 반 전체 통계만 있는 글")
    if v.get("mean") is None:
        notes.append("평균 없음" + (f" — 중앙값 {v['median']:g}을 평균 대신 쓰면 대략적인 추정은 가능" if v.get("median") is not None else ""))
    if v.get("sd") is None:
        notes.append("표준편차 없음 — 상위 % 추정에는 표준편차가 필요")
    mx = v.get("max_score")
    if mx is None:
        notes.append("만점 미기재 → 100점 만점으로 볼지 확인")
    elif any(v.get(k) is not None and v[k] > mx for k in ("score", "mean", "high")):
        notes.append("점수가 만점보다 큼 — 확인 필요")
    if len(g["texts"]) > 1:
        notes.append(f"이어진 {len(g['texts'])}줄(문장)을 하나로 묶음")
    return ScoreFound(label=label, sentence=" ".join(g["texts"]), notes=notes,
                      **{k: v.get(k) for k in _FIELD_KO})


def find_scores(text: str) -> list[ScoreFound]:
    """글(메시지·공지)에서 점수·평균·표준편차 문장을 찾는다. 2~3줄에 걸친 한 묶음(제목 줄 + 숫자 줄)은 합친다."""
    if not text:
        return []
    if _TAG.search(text):
        text = re.sub(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", "\n", text)
        text = _TAG.sub(" ", text).replace("&nbsp;", " ").replace("&amp;", "&")
    groups, cur = [], None

    def compatible(g: dict, u: dict) -> bool:
        if len(g["texts"]) >= 3:
            return False
        if any(k in g["vals"] and g["vals"][k] != x for k, x in u["vals"].items()):
            return False
        a, b = g["label"], u["label"]
        return a is None or b is None or a[1] == b[1] or "exam" in (a[1][0], b[1][0])

    for u in _units(text):
        if u is None or not u["vals"]:
            if cur:
                groups.append(cur)
            cur = None
            if u is not None and u["label"]:  # 제목 줄: 다음 줄 숫자와 묶일 수 있다
                cur = {"label": u["label"], "vals": {}, "notes": [], "texts": [u["text"]]}
            continue
        if cur and compatible(cur, u):
            cur["vals"].update(u["vals"])
            cur["notes"] += u["notes"]
            cur["texts"].append(u["text"])
            if u["label"] and (cur["label"] is None or cur["label"][1][0] == "exam"):
                cur["label"] = u["label"]
        else:
            if cur:
                groups.append(cur)
            cur = {"label": u["label"], "vals": dict(u["vals"]), "notes": list(u["notes"]), "texts": [u["text"]]}
    if cur:
        groups.append(cur)
    return [f for f in map(_finish, groups) if f]


# ---------------------------------------------------------------- 엑셀

_XLSX_LIMIT = 64 * 1024 * 1024
_SUMMARY = re.compile(r"^\s*(?:평균|표준\s*편차|합계|총계|최고(?:점)?|최저(?:점)?|최댓값|최솟값|중앙값|인원|"
                      r"average|mean|avg|sd|std|stdev|total|sum|max|min|median|count)\s*[:：]?\s*$", re.I)
_ID_HEAD = re.compile(r"학번|수험\s*번호|student\s*(?:id|no|number)|^\s*(?:id|no\.?|번호|순번|연번|#)\s*$|이름|성명|^\s*name\s*$", re.I)
_MASK_CH = set("*●○■□xX")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _zxml(z: zipfile.ZipFile, path: str):
    try:
        info = z.getinfo(path)
    except KeyError:
        return None
    if info.file_size > _XLSX_LIMIT:
        raise ValueError("엑셀 파일 안의 내용이 너무 큽니다")
    try:
        return ET.fromstring(z.read(path))
    except ET.ParseError as e:
        raise ValueError(f"엑셀 파일 구조를 읽지 못했습니다({path})") from e


def _rels(z: zipfile.ZipFile, part: str) -> dict:
    d, base = posixpath.split(part)
    root = _zxml(z, posixpath.join(d, "_rels", base + ".rels"))
    out = {}
    for r in (root if root is not None else []):
        if _local(r.tag) != "Relationship" or r.get("TargetMode") == "External":
            continue
        t = r.get("Target", "")
        out[r.get("Id")] = (r.get("Type", ""), t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(d, t)))
    return out


def _rich(el) -> str:
    parts = []
    for ch in el:
        n = _local(ch.tag)
        if n == "t":
            parts.append(ch.text or "")
        elif n == "r":
            parts.extend(t.text or "" for t in ch if _local(t.tag) == "t")
    return "".join(parts)


def _col(ref: str | None) -> int | None:
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _col_name(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _as_num(v) -> float | None:
    if isinstance(v, float):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*[-+]?\d+(?:\.\d+)?\s*", v.replace(",", "")):
        return float(v.replace(",", ""))
    return None


def _read_sheet(data: bytes) -> tuple[list[dict], str, int]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("xlsx 파일을 읽을 수 없습니다(옛 .xls라면 .xlsx로 다시 저장해 주세요)") from e
    with z:
        wb_path = next((p for t, p in _rels(z, "").values() if t.endswith("/officeDocument")), "xl/workbook.xml")
        wb = _zxml(z, wb_path)
        if wb is None:
            raise ValueError("xlsx 안에 workbook.xml이 없습니다")
        wb_rels = _rels(z, wb_path)
        sheets = [el for el in wb.iter() if _local(el.tag) == "sheet"]
        if not sheets:
            raise ValueError("xlsx 안에 시트가 없습니다")
        rid = next((v for k, v in sheets[0].attrib.items() if k.endswith("}id")), None)
        path = wb_rels.get(rid, ("", ""))[1] or posixpath.join(posixpath.dirname(wb_path), "worksheets/sheet1.xml")
        sst_path = next((p for t, p in wb_rels.values() if t.endswith("/sharedStrings")),
                        posixpath.join(posixpath.dirname(wb_path), "sharedStrings.xml"))
        sst_root = _zxml(z, sst_path)
        sst = [_rich(si) for si in sst_root if _local(si.tag) == "si"] if sst_root is not None else []
        sheet = _zxml(z, path)
        if sheet is None:
            raise ValueError("첫 번째 시트를 찾지 못했습니다")
    rows = []
    for row in (el for el in sheet.iter() if _local(el.tag) == "row"):
        cells, nxt = {}, 0
        for c in row:
            if _local(c.tag) != "c":
                continue
            col = _col(c.get("r"))
            col = nxt if col is None else col
            nxt = col + 1
            t = c.get("t", "n")
            v = next((ch.text for ch in c if _local(ch.tag) == "v"), None)
            val = None
            if t == "s":
                if v is not None and v.strip().isdigit() and int(v) < len(sst):
                    val = sst[int(v)]
            elif t == "inlineStr":
                is_ = next((ch for ch in c if _local(ch.tag) == "is"), None)
                val = _rich(is_) if is_ is not None else None
            elif t in ("str", "d"):
                val = v
            elif t not in ("b", "e") and v not in (None, ""):
                try:
                    val = float(v)
                except ValueError:
                    val = None
                if val is not None and not math.isfinite(val):
                    val = None
            if val is not None and not (isinstance(val, str) and not val.strip()):
                cells[col] = val
        rows.append(cells)
    return rows, sheets[0].get("name") or "Sheet1", len(sheets)


def _masked_match(cell: str, sid: str) -> bool:
    cell = cell.strip()
    if len(cell) != len(sid) or not any(ch in _MASK_CH for ch in cell):
        return False
    visible = [(a, b) for a, b in zip(cell, sid) if a not in _MASK_CH]
    return len(visible) >= len(sid) / 2 and all(a == b for a, b in visible)


def xlsx_stats(data: bytes, student_id: str) -> dict:
    """성적 엑셀(xlsx)에서 숫자 열마다 내 값과 반 평균·표준편차를 뽑는다. 다른 학생의 행·학번은 돌려주지 않는다."""
    rows, sheet_name, n_sheets = _read_sheet(data)
    notes = [f"첫 번째 시트 '{sheet_name}'만 읽음" + (f"(시트 {n_sheets}개)" if n_sheets > 1 else ""),
             "표준편차는 표본표준편차(n−1로 나눔)", "다른 학생의 행·학번은 결과에 넣지 않음(평균 같은 집계만)"]

    def texts(r: dict) -> list[str]:
        return [v for v in r.values() if isinstance(v, str) and _as_num(v) is None]

    hi = next((i for i, r in enumerate(rows) if len(texts(r)) >= 2), None)
    header = rows[hi] if hi is not None else {}
    if hi is None:
        notes.append("머리글 행(글자 칸 2개 이상)을 못 찾아 열 이름을 A, B, …로 둠")
    data_rows, summary = [], 0
    for r in rows[(hi + 1) if hi is not None else 0:]:
        if not r:
            continue
        first = next((r[c] for c in sorted(r) if isinstance(r[c], str)), None)
        if first is not None and _SUMMARY.match(first):
            summary += 1
            continue
        data_rows.append(r)
    if summary:
        notes.append(f"요약 행(평균·합계 등) {summary}개는 계산에서 뺌")

    sid = str(student_id or "").strip()
    sid_num = float(sid) if re.fullmatch(r"\d+", sid) else None
    hits = [(i, c) for i, r in enumerate(data_rows) for c, v in r.items()
            if sid and ((isinstance(v, str) and v.strip() == sid) or (isinstance(v, float) and sid_num is not None and v == sid_num))]
    if not hits and sid:
        masked = [(i, c) for i, r in enumerate(data_rows) for c, v in r.items() if isinstance(v, str) and _masked_match(v, sid)]
        if len({i for i, _ in masked}) == 1:
            hits = masked[:1]
            notes.append("가린 학번과 보이는 자리가 맞는 행 1개를 본인으로 봄 — 확인 필요")
        elif masked:
            notes.append("가린 학번과 맞는 행이 여러 개라 본인 행을 고르지 않음")
    mine_row = data_rows[hits[0][0]] if hits else None
    id_cols = {c for _, c in hits}
    if len({i for i, _ in hits}) > 1:
        notes.append("학번이 맞는 행이 여러 개 — 첫 번째 행 사용")
    notes.append("학번이 맞는 행을 본인 점수로 사용" if mine_row is not None else "학번을 찾지 못함 — 반 전체 통계만 계산")

    cols = sorted(set(header) | {c for r in data_rows for c in r})
    out, skipped = [], []
    for c in cols:
        name = str(header.get(c) or f"열 {_col_name(c)}").strip()
        cells = [r.get(c) for r in data_rows if r.get(c) is not None]
        values = [x for x in map(_as_num, cells) if x is not None]
        if not values or len(values) < 0.5 * len(cells):
            continue
        if c in id_cols or _ID_HEAD.search(name) or (all(v.is_integer() for v in values) and min(values) >= 100000):
            skipped.append(name)
            continue
        mine = _as_num(mine_row.get(c)) if mine_row is not None else None
        out.append({"name": name, "mine": mine, "mean": statistics.fmean(values),
                    "sd": statistics.stdev(values) if len(values) >= 2 else None, "n": len(values),
                    "rank": (1 + sum(v > mine for v in values)) if mine is not None else None})
    if skipped:
        notes.append("학번·번호로 보이는 열은 통계에서 뺌: " + ", ".join(skipped))
    if any(x["n"] < len(data_rows) for x in out):
        notes.append("빈 칸·글자(결시 등)는 통계에서 뺌")
    return {"found": mine_row is not None, "columns": out, "notes": notes}
