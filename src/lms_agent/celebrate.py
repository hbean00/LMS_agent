"""성적 공개 반응과 학기 중 보상 — "학기 마라톤을 함께 달리는 페이스메이커"(계약 11장, 11-1장).

화면이 상태를 물을 때마다 저장된 것(store.data)만으로 다시 계산한다(네트워크 없음, 같은 입력이면 같은 결과).
- 성적: Canvas 과제 점수(submission.score)가 새로 생기거나 바뀜(kind "graded"), 글에서 찾은 점수 문장(kind "score").
  평균·표준편차가 있으면 상위 %와 학점 구간(band)을, 없으면 그 과목 성적 추정이 있을 때 그 학점을 쓴다.
  근거가 없으면 band는 None — 등급을 지어내지 않는다.
- 보상: 출발(start), km 이정표(km), 한 주 완주(week_clear), 연속 완주(streak), 확인함 비우기(inbox_zero).
  같은 보상은 학기에 한 번, 화면에 새로 띄우는 보상은 하루 2개까지, 같은 종류가 여럿 밀려 있으면 가장 최근 것 하나만 띄운다.

기록: store.data["celebrated"](본 것: 키 → 값), store.data["medals"](받은 보상), store.data["celebrate_init"](처음 계산한 시각),
store.data["review_peak"](확인함이 가장 많이 쌓였던 수 — 확인함 비우기 판정용).
처음 계산할 때는 14일보다 오래된 점수를 조용히 '본 것'으로 둔다(오래 쓴 계정에서 알림이 쏟아지지 않게).
처음 계산한 시점이 '합류'(joined_at·joined_km, 계약 13-4): 합류 전에 지난 km 이정표·주 완주·연속은 메달이 아니다
(milestones[].before_join으로만 보이고, 예전 버전이 메달함에 넣어 둔 것도 읽을 때 뺀다). 출발(start)은 메달이 아닌 합류 인사.
성적은 이 PC에서만 다룬다: 이 모듈은 아무 데도 보내지 않는다.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone

from . import grades as g
from .core import short_course_name, week_of
from .timeutil import iso, now_local, parse_iso

TOTAL_KM = 42.195
# (km, 짧은 이름) — 30km는 '마의 구간'(보통 기말 준비기)
MILESTONES = [(5.0, "5km"), (10.0, "10km"), (21.1, "하프"), (30.0, "마의 구간"), (35.0, "35km"), (TOTAL_KM, "결승")]
STREAKS = (3, 5, 8)
RECENT_DAYS = 14          # 처음 계산 때 이보다 오래된 점수는 조용히 넘긴다
DAILY_REWARDS = 2         # 하루에 새로 띄우는 보상 수(성적 반응은 세지 않음)
INBOX_PEAK = 3            # 확인함이 이만큼 쌓였다가 0이 되면 '확인함 비우기'
GRADE_KINDS = ("graded", "score")
REWARD_KINDS = ("start", "km", "week_clear", "streak", "inbox_zero")
# 같은 날 여러 보상이 겹치면 이 순서로 띄운다
PRIORITY = {"start": 0, "finish": 1, "streak": 2, "week_clear": 3, "km": 4, "inbox_zero": 5}

_MID = re.compile(r"중간|midterm|mid-term|\bmid\b", re.I)
_FIN = re.compile(r"기말|final", re.I)


# ---------------------------------------------------------------- 작은 계산

def km_of(sem: dict | None) -> float:
    """km = 42.195 × week_now / weeks_total(소수 1자리). 개강 전 0, 마지막 주부터는 결승(42.195)."""
    sem = sem or {}
    total = int(sem.get("weeks_total") or 0)
    wn = int(sem.get("week_now") or 0)
    if total <= 0 or wn <= 0:
        return 0.0
    if wn >= total:
        return TOTAL_KM
    return round(TOTAL_KM * wn / total, 1)


def band_of(letter: str | None) -> str | None:
    """A+·A0 → A, B+·B0 → B, C+·C0 → C, D+·D0 → D, F → F."""
    return letter[0] if letter and letter[0] in "ABCDF" else None


def exam_of(*texts) -> str:
    """라벨·제목에 중간/midterm → midterm, 기말/final → final, 그 밖 → assignment."""
    for t in texts:
        t = str(t or "")
        if _MID.search(t):
            return "midterm"
        if _FIN.search(t):
            return "final"
    return "assignment"


def term_of(sem: dict | None) -> str:
    """학기 이름(보상 키에 쓴다). 개강일이 조금 바뀌어도 같은 학기로 보이게 '연도-1|2'."""
    s = parse_iso((sem or {}).get("start"))
    if not s:
        return "term"
    return f"{s.year}-{1 if s.month <= 7 else 2}"


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fmt(x) -> str:
    v = _num(x)
    return "-" if v is None else f"{v:g}"


def _hash(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _day(s: str | None) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- 성적 반응

def _course_dist(store, cid) -> dict:
    cfg = (store.data.get("grades") or {}).get(str(cid)) if cid is not None else None
    return (cfg or {}).get("dist") or g.DEFAULT_DISTRIBUTION


def _dist_basis(store, cid) -> str:
    """학점 비율의 근거: 사용자가 그 과목 비율을 정했으면 "user", 기본 비율(15/30/45/60/75…)이면 "default"(계약 13-2)."""
    cfg = (store.data.get("grades") or {}).get(str(cid)) if cid is not None else None
    return "user" if (cfg or {}).get("dist") else "default"


def _course_estimate(store, cid, memo: dict):
    """그 과목 성적 설정으로 계산한 추정(없으면 None). 과목마다 한 번만 계산한다."""
    if cid is None:
        return None
    key = str(cid)
    if key not in memo:
        cfg = (store.data.get("grades") or {}).get(key) or {}
        est = None
        if cfg.get("components"):
            try:
                est = g.estimate(cfg["components"], cfg.get("dist") or g.DEFAULT_DISTRIBUTION,
                                 class_size=cfg.get("class_size"))
            except (ValueError, TypeError, KeyError):
                est = None
        memo[key] = est
    return memo[key]


def _judge(store, cid, score, mean, sd, memo) -> dict:
    """점수의 위치: 평균·표준편차가 있으면 이 점수로, 없으면 과목 추정으로, 둘 다 없으면 판정 없음.
    letter_basis: 학점을 사용자가 정한 비율로 매겼으면 "user", 기본 비율이면 "default"(화면은 '기본 비율이라면 …'), 학점이 없으면 None."""
    if score is not None and mean is not None and sd is not None and sd > 0:
        try:
            top = g.top_percent(score, mean, sd)
            letter = g.letter_for(top, _course_dist(store, cid))
            return {"top": round(top, 1), "letter": letter, "band": band_of(letter), "basis": "score",
                    "letter_basis": _dist_basis(store, cid)}
        except ValueError:
            pass
    est = _course_estimate(store, cid, memo)
    if est and est.get("grade"):
        return {"top": round(float(est["top"]), 1) if est.get("top") is not None else None,
                "letter": est["grade"], "band": band_of(est["grade"]), "basis": "course",
                "letter_basis": _dist_basis(store, cid)}
    return {"top": None, "letter": None, "band": None, "basis": None, "letter_basis": None}


def _stats(a: dict):
    """Canvas score_statistics(있을 때만): 평균, 사분위로 어림한 표준편차."""
    st = a.get("score_statistics") or {}
    mean = _num(st.get("mean"))
    sd = None
    q1, q3 = _num(st.get("lower_q")), _num(st.get("upper_q"))
    if q1 is not None and q3 is not None and q3 > q1:
        sd = g.sd_from_quartiles(q1, q3)
    return mean, (round(sd, 2) if sd else None)


def _graded_events(store, memo) -> list[dict]:
    cache = store.data.get("cache") or {}
    names = {str(c.get("id")): c.get("name") for c in cache.get("courses", [])}
    out = []
    for a in cache.get("assignments", []):
        sub = a.get("submission") or {}
        score = _num(sub.get("score"))
        if score is None or sub.get("excused"):
            continue
        if score == 0 and not sub.get("submitted_at"):
            continue  # 안 낸 과제의 0점은 '점수가 나왔어요'로 알리지 않는다
        cid = str(a.get("course_id") or "")
        key = f"graded:{cid}:{a.get('id')}"
        sig = _fmt(score)
        mean, sd = _stats(a)
        at = sub.get("graded_at") or sub.get("submitted_at") or a.get("due_at") or a.get("updated_at")
        out.append({"key": key, "sig": sig, "also": [],
                    "entry": {"id": f"{key}:{sig}", "kind": "graded", "course_id": cid or None,
                              "course_short": short_course_name(names.get(cid)) or None,
                              "title": a.get("name") or "과제", "score": score,
                              "max_score": _num(a.get("points_possible")) or None,
                              "max_assumed": not _num(a.get("points_possible")), "mean": mean, "sd": sd,
                              **_judge(store, cid or None, score, mean, sd, memo),
                              "exam": exam_of(a.get("name")), "source_title": "Canvas 과제", "source_type": "canvas",
                              "url": a.get("html_url"), "at": iso(parse_iso(at))}})
    return out


_CAND_MEMO: dict = {}


def _candidates(store) -> list[dict]:
    """gradebook.candidates는 글 전체에서 점수 문장을 찾으므로 같은 저장 상태에서는 한 번만."""
    from .gradebook import candidates
    cache = store.data.get("cache") or {}
    sig = (id(store.data.get("cache")), store.data.get("last_sync"), len(store.data.get("manual") or []),
           (store.data.get("settings") or {}).get("student_id"),   # 학번을 넣으면 점수표 PDF에서 내 점수가 생긴다
           len(store.data.get("pdf_texts") or {}), len(cache.get("pdf_texts") or {}))
    if _CAND_MEMO.get("sig") != sig:
        try:
            found = candidates(store)
        except Exception:  # noqa: BLE001 — 점수 문장 찾기가 깨져도 화면은 떠야 한다
            found = []
        _CAND_MEMO.update(sig=sig, value=found)
    return _CAND_MEMO["value"]


def _score_events(store, memo) -> list[dict]:
    groups: dict = {}
    for c in _candidates(store):
        score = _num(c.get("score"))
        if score is None:
            continue  # 평균만 적힌 공지 등은 내 점수가 아니다
        mx, mean, sd = _num(c.get("max_score")), _num(c.get("mean")), _num(c.get("sd"))
        cid = c.get("course_id")
        key = "score:" + _hash(cid, c.get("source_type"), c.get("source_title"), c.get("posted_at"),
                               c.get("label"), score, mx, mean, sd)
        same = (cid, (c.get("label") or "").strip(), score, mx, mean, sd)   # 같은 점수가 두 글에 → 하나로
        if same in groups:
            groups[same]["also"].append(key)
            continue
        groups[same] = {"key": key, "sig": "1", "also": [],
                        "entry": {"id": key, "kind": "score", "course_id": cid,
                                  "course_short": c.get("course_short"),
                                  "title": c.get("label") or c.get("source_title") or "점수",
                                  "score": score, "max_score": mx, "max_assumed": mx is None,   # 만점 미기재 → 100점 가정
                                  "mean": mean, "sd": sd,
                                  **_judge(store, cid, score, mean, sd, memo),
                                  "exam": exam_of(c.get("label"), c.get("source_title")),
                                  "source_title": c.get("source_title"), "source_type": c.get("source_type"),
                                  "url": c.get("url"), "at": c.get("posted_at")}}
    return list(groups.values())


# ---------------------------------------------------------------- 보상

def _week_results(sem: dict, items: list[dict], today: date) -> list[dict]:
    """지난 주마다 마감 수·제출 수(시험 제외, 일정에 들어간 것만). 마감이 없는 주도 넣는다."""
    by = {}
    for it in items:
        if it.get("status") != "scheduled" or it.get("kind") == "시험" or not it.get("due"):
            continue
        n = it.get("week") if isinstance(it.get("week"), int) else week_of(sem, it.get("due"))
        if n:
            by.setdefault(n, []).append(it)
    out = []
    for w in sem.get("weeks") or []:
        end = _day(w.get("end"))
        if not end or end >= today:
            continue
        its = by.get(w["n"], [])
        done = sum(1 for it in its if it.get("submitted"))
        out.append({"n": w["n"], "start": w["start"], "end": w["end"], "total": len(its), "done": done,
                    "clear": bool(its) and done == len(its)})
    return out


def _streaks(weeks: list[dict]) -> tuple[int, list[tuple[int, dict]]]:
    """연속 완주: 마감이 없는 주는 건너뛰고(끊지 않음), 하나라도 안 낸 주가 있으면 끊긴다."""
    run, hits = 0, []
    for w in weeks:
        if not w["total"]:
            continue
        if w["clear"]:
            run += 1
            if run in STREAKS:
                hits.append((run, w))
        else:
            run = 0
    return run, hits


def _reward(key: str, kind: str, title: str, at, week, **extra) -> dict:
    return {"key": key, "sig": "1", "also": [],
            "entry": {"id": key, "kind": kind, "title": title, "at": at, "week": week, **extra}}


def _milestone_title(km: float) -> str:
    return {5.0: "5km 통과", 10.0: "10km 통과", 21.1: "반환점 돌았어요", 30.0: "30km, 가장 힘든 구간이에요",
            35.0: "35km, 결승선이 보여요", TOTAL_KM: "한 학기 완주!"}.get(km, f"{km:g}km 통과")


def _week_at(sem: dict, day: date) -> int:
    """그 날의 주차(개강 전 0, 종강 뒤 weeks_total + 1). semester_info의 week_now와 같은 계산."""
    weeks = sem.get("weeks") or []
    first = _day(weeks[0].get("start")) if weeks else None
    if first is None:
        return int(sem.get("week_now") or 0)
    total = int(sem.get("weeks_total") or len(weeks))
    if day < first:
        return 0
    return min(total + 1, (day - first).days // 7 + 1)


def join_info(data: dict, sem: dict, now: datetime) -> dict | None:
    """합류(처음 계산한 시점, celebrate_init): {"at", "day", "week", "km"}. 첫 동기화 전에는 None.
    아직 처음 계산 전이면 지금이 합류 시점이다. km는 지금 학기 날짜로 그 날의 주차를 다시 셈한 값(계약 13-4)."""
    if data.get("cache") is None:
        return None
    at = parse_iso(data.get("celebrate_init")) or now
    wk = _week_at(sem, at.date())
    return {"at": at, "day": at.date(), "week": wk,
            "km": km_of({"week_now": wk, "weeks_total": sem.get("weeks_total")})}


def _before_join(join: dict | None, kind: str, km=None, end=None) -> bool:
    """합류 전에 이미 지난 보상인지: km 이정표는 합류 km 이하, 주 완주·연속은 합류 전에 끝난 주."""
    if join is None:
        return False
    if kind == "km":
        return km is not None and float(km) <= join["km"] + 1e-9
    if kind in ("week_clear", "streak"):
        d = _day(end)
        return d is not None and d < join["day"]
    return False


def _inbox_title(no_deadline_left: int) -> str:
    """확인함 비우기 보상 이름. 마감 없는 과제가 남아 있으면 '놓친 마감 없음'이라 하지 않고 그 수를 넣는다(계약 13-4)."""
    if no_deadline_left:
        return f"확인함 비우기 완료 · 마감 모르는 과제 {no_deadline_left}개 남음"
    return "확인함 비우기 완료"


def _reward_events(store, sem: dict, items: list[dict], now: datetime, term: str) -> tuple[list[dict], int]:
    """보상 후보. 합류 전에 지난 km 이정표·주 완주·연속은 넣지 않는다(메달 아님, milestones[].before_join으로만 보임).
    연속 완주도 합류한 뒤에 끝난 주만 센다. start는 메달이 아닌 합류 인사."""
    out = []
    if store.data.get("cache") is None:   # 첫 동기화 전에는 학기 날짜도 추정이라 보상을 주지 않는다
        return out, 0
    weeks = sem.get("weeks") or []
    total = int(sem.get("weeks_total") or len(weeks) or 0)
    join = join_info(store.data, sem, now)
    runs = store.data.get("runs") or []
    first = next((r.get("finished_at") for r in runs if r.get("finished_at")), None) or store.data.get("last_sync")
    title = (f"출발! {total}주 마라톤, 끝까지 같이 달려요" if join["week"] <= 1
             else f"{join['km']:g}km 지점에서 합류했어요. 결승선까지 같이 달려요")
    out.append(_reward(f"start:{term}", "start", title, first or iso(now), max(join["week"], 0) or None, km=join["km"]))
    km = km_of(sem)
    for m, label in MILESTONES:
        if total and km >= m - 1e-9 and not _before_join(join, "km", km=m):
            reach = min(total, max(1, math.ceil(m * total / TOTAL_KM - 1e-9)))
            wk = weeks[reach - 1] if 0 < reach <= len(weeks) else {}
            out.append(_reward(f"km:{term}:{m:g}", "km", _milestone_title(m), wk.get("start"), reach,
                               km=m, label=label, finish=m == TOTAL_KM))
    results = [w for w in _week_results(sem, items, now.date()) if not _before_join(join, "week_clear", end=w["end"])]
    for w in results:
        if w["clear"]:
            out.append(_reward(f"week_clear:{term}:{w['start']}", "week_clear", f"{w['n']}주차 완주",
                               w["end"], w["n"], count=w["total"]))
    streak, hits = _streaks(results)
    for n, w in hits:
        out.append(_reward(f"streak:{term}:{n}", "streak", f"{n}주 연속 완주", w["end"], w["n"], n=n))
    left = sum(1 for it in items if it.get("status") == "no_deadline")
    for ev in (store.data.get("review_peak") or {}).get("earned") or []:
        out.append(_reward(ev["key"], "inbox_zero", _inbox_title(left), ev.get("at"), ev.get("week"),
                           count=ev.get("count"), no_deadline_left=left))
    return out, streak


def _medal_counts(m: dict, join: dict | None) -> bool:
    """메달함에 보일 것인지: 출발 인사(start)와 합류 전 보상은 메달이 아니다(예전 계정에 남은 것도 읽을 때 뺀다)."""
    kind = m.get("kind")
    if kind == "start":
        return False
    if kind == "km":
        return not _before_join(join, "km", km=m.get("km"))
    if kind in ("week_clear", "streak"):
        return not _before_join(join, kind, end=m.get("at"))
    return True


def _review_update(store, items: list[dict], now: datetime, sem: dict, term: str) -> dict | None:
    """확인함 최고치를 따라가다가, 3개 이상 쌓였던 것을 사용자가 정해서 0이 되면 '확인함 비우기'를 남긴다.
    바뀔 것이 없으면 None(저장하지 않음)."""
    rp = dict(store.data.get("review_peak") or {})
    count, since = int(rp.get("count") or 0), rp.get("since")
    n = sum(1 for it in items if it.get("status") == "review")
    if n:
        if n > count or not since:
            rp.update(count=max(count, n), since=since or iso(now))
            return rp
        return None
    if not count:
        return None
    if count >= INBOX_PEAK:
        start = parse_iso(since)
        decided = [parse_iso((u or {}).get("decided_at")) for u in (store.data.get("user") or {}).values()]
        if start and any(d and d >= start for d in decided):   # 사용자가 직접 정리했을 때만(로그아웃·삭제는 아님)
            wk = week_of(sem, iso(now))
            monday = (now.date() - timedelta(days=now.weekday())).isoformat()
            earned = list(rp.get("earned") or [])
            key = f"inbox_zero:{term}:{monday}"
            if all(e.get("key") != key for e in earned):
                earned.append({"key": key, "at": iso(now), "week": wk, "count": count})
            rp["earned"] = earned[-20:]
    rp.update(count=0, since=None)
    return rp


# ---------------------------------------------------------------- 화면용 묶음

def _seen(celebrated: dict, ev: dict) -> bool:
    return celebrated.get(ev["key"]) == ev["sig"]


def _medal(ev: dict, term: str, seen_at: str, silent: bool = False) -> dict:
    e = ev["entry"]
    m = {k: e.get(k) for k in ("id", "kind", "title", "at", "week")}
    for k in ("km", "label", "n", "count", "finish", "no_deadline_left"):
        if e.get(k) is not None:
            m[k] = e[k]
    m.update(term=term, seen_at=seen_at)
    if silent:
        m["silent"] = True
    return m


def _collapse(events: list[dict]) -> list[dict]:
    """같은 종류가 여럿 밀려 있으면 가장 최근(km는 가장 먼, 연속은 가장 긴) 하나만 띄우고 나머지는 그것과 같이 본 것으로."""
    best: dict = {}
    for ev in events:
        e = ev["entry"]
        kind = "finish" if e.get("finish") else e["kind"]
        rank = (e.get("km") or 0, e.get("n") or 0, str(e.get("at") or ""))
        cur = best.get(kind)
        if cur is None or rank > cur[0]:
            if cur is not None:
                ev["also"] = ev["also"] + [cur[1]["key"]] + cur[1]["also"]
            best[kind] = (rank, ev)
        else:
            cur[1]["also"] = cur[1]["also"] + [ev["key"]] + ev["also"]
    return sorted((v[1] for v in best.values()),
                  key=lambda ev: PRIORITY["finish" if ev["entry"].get("finish") else ev["entry"]["kind"]])


def _all_events(store, sem, items, now, term):
    memo: dict = {}
    grades_ev = _graded_events(store, memo) + _score_events(store, memo)
    rewards_ev, streak = _reward_events(store, sem, items, now, term)
    return grades_ev, rewards_ev, streak


def state(store, sem: dict, items: list[dict], now: datetime | None = None) -> tuple[dict, dict]:
    """state["celebrations"]와, 저장할 것(apply로 store.data에 합친다; 없으면 빈 dict).

    저장할 것이 생기는 경우: 처음 계산(오래된 점수·지난 보상을 조용히 본 것으로), 확인함 최고치가 바뀜."""
    now = now or now_local()
    sem = sem or {}
    term = term_of(sem)
    data = store.data
    updates: dict = {}
    rp = _review_update(store, items, now, sem, term)
    if rp is not None:
        updates["review_peak"] = rp
        data = {**data, "review_peak": rp}
    view = _Shadow(store, data)
    grades_ev, rewards_ev, streak = _all_events(view, sem, items, now, term)
    celebrated = dict(data.get("celebrated") or {})
    medals = list(data.get("medals") or [])

    first_calc = not data.get("celebrate_init") and data.get("cache") is not None
    if first_calc:
        # 처음(= 합류): 14일보다 오래된(또는 시각을 모르는) 점수는 조용히 본 것으로. 합류 전에 지난 이정표·주 완주·연속은
        # 보상 후보에 아예 없다(메달이 아님). 혹시 남은 보상(출발 인사 말고)도 조용히 본 것으로만 — 메달함에 넣지 않는다.
        stamp, cut = iso(now), now - timedelta(days=RECENT_DAYS)
        quiet = {}
        for ev in grades_ev:
            at = parse_iso(ev["entry"].get("at"))
            if at is None or at < cut:
                for k in [ev["key"]] + ev["also"]:
                    quiet[k] = ev["sig"]
        for ev in rewards_ev:
            if ev["entry"]["kind"] == "start" or _seen(celebrated, ev):
                continue
            quiet[ev["key"]] = ev["sig"]
        updates.update(celebrate_init=stamp, celebrated_add=quiet)
        celebrated.update(quiet)

    join = join_info(data, sem, now)
    mine = [m for m in medals if m.get("term") == term and _medal_counts(m, join)]
    mine.sort(key=lambda m: (str(m.get("at") or ""), m.get("id") or ""))
    pend_grades = sorted((ev for ev in grades_ev if not _seen(celebrated, ev)),
                         key=lambda ev: str(ev["entry"].get("at") or ""))
    waiting = _collapse([ev for ev in rewards_ev if not _seen(celebrated, ev)])
    today = now.date().isoformat()
    shown_today = sum(1 for m in mine if not m.get("silent") and str(m.get("seen_at") or "")[:10] == today)
    pend_rewards = waiting[:max(0, DAILY_REWARDS - shown_today)]

    news_cards, news_init = _news(view, items, now, [ev["entry"] for ev in pend_grades])
    if news_init is not None:
        updates["news_init"] = news_init

    km = km_of(sem)
    total = int(sem.get("weeks_total") or 0)
    wn = int(sem.get("week_now") or 0)
    nxt = next(((m, label) for m, label in MILESTONES if km < m - 1e-9), None)
    out = {
        "news": news_cards,
        "pending": [ev["entry"] for ev in pend_grades] + [ev["entry"] for ev in pend_rewards],
        "medals": [{k: v for k, v in m.items() if k not in ("silent", "term")} for m in mine],
        "streak": streak, "streak_next": next((n for n in STREAKS if n > streak), None),
        "km": km, "total_km": TOTAL_KM, "week_now": wn, "weeks_total": total,
        "joined_at": iso(join["at"]) if join else None, "joined_km": join["km"] if join else None,
        "finished": total > 0 and wn >= total, "after_finish": total > 0 and wn > total,
        "milestones": [{"km": m, "label": label, "title": _milestone_title(m), "reached": km >= m - 1e-9,
                        "before_join": _before_join(join, "km", km=m)} for m, label in MILESTONES],
        "next": {"km": nxt[0], "label": nxt[1], "left": round(nxt[0] - km, 1), "title": _milestone_title(nxt[0])}
        if nxt else None,
        "waiting": max(0, len(waiting) - len(pend_rewards)),   # 오늘 몫을 넘어 내일 띄울 보상 수
    }
    return out, updates


class _Shadow:
    """store 대신 넘기는 읽기 전용 껍데기(아직 저장 안 한 review_peak를 반영해 계산하려고)."""

    def __init__(self, store, data):
        self._store = store
        self.data = data


def apply(data: dict, updates: dict) -> None:
    """state()가 돌려준 저장할 것을 store.data에 합친다(같은 것을 두 번 합쳐도 결과가 같다)."""
    if not updates:
        return
    if "review_peak" in updates:
        data["review_peak"] = updates["review_peak"]
    if updates.get("celebrated_add"):
        data.setdefault("celebrated", {}).update(updates["celebrated_add"])
    if updates.get("medals_add"):
        have = {m.get("id") for m in data.get("medals") or []}
        data.setdefault("medals", []).extend(m for m in updates["medals_add"] if m.get("id") not in have)
    if updates.get("celebrate_init") and not data.get("celebrate_init"):
        data["celebrate_init"] = updates["celebrate_init"]
    if updates.get("news_init") and not (data.get("news_seen") or {}).get("init"):
        _merge_news_seen(data, updates["news_init"], init=True)


def mark_seen(store, sem: dict, items: list[dict], ids: list[str], now: datetime | None = None) -> int:
    """POST /api/celebrations/seen: 지금 띄울 수 있는 것 중 ids를 본 것으로(보상은 메달함에 넣는다).
    같은 종류로 묶여 함께 밀려 있던 보상도 같이 넣는다. 모르는 id는 무시. 본 것으로 바꾼 수를 돌려준다."""
    now = now or now_local()
    want = {str(i) for i in ids}
    term = term_of(sem)
    grades_ev, rewards_ev, _ = _all_events(store, sem, items, now, term)
    n_news = _mark_news(store, items, now, want, [ev["entry"] for ev in grades_ev])
    celebrated = store.data.setdefault("celebrated", {})
    medals = store.data.setdefault("medals", [])
    have = {m.get("id") for m in medals}
    by_key = {ev["key"]: ev for ev in rewards_ev}
    stamp = iso(now)
    n = 0
    for ev in grades_ev:
        if ev["entry"]["id"] in want and not _seen(celebrated, ev):
            for k in [ev["key"]] + ev["also"]:
                celebrated[k] = ev["sig"]
            n += 1
    for ev in _collapse([e for e in rewards_ev if not _seen(celebrated, e)]):
        if ev["entry"]["id"] not in want:
            continue
        for i, k in enumerate([ev["key"]] + ev["also"]):
            other = by_key.get(k)
            if other is None or _seen(celebrated, other):
                continue
            celebrated[k] = other["sig"]
            if other["entry"]["kind"] == "start":
                continue   # 출발은 합류 인사 — 메달이 아니다(계약 13-4)
            if other["entry"]["id"] not in have:
                medals.append(_medal(other, term, stamp, silent=i > 0))   # 묶여 온 것은 오늘 몫에 세지 않음
                have.add(other["entry"]["id"])
        n += 1
    if n:
        rp = store.data.get("review_peak") or {}
        if rp.get("earned"):   # 본 확인함 비우기는 기록에서 뺀다(메달함에 남음)
            rp["earned"] = [e for e in rp["earned"] if celebrated.get(e.get("key")) != "1"]
    return n + n_news


# ---------------------------------------------------------------- 동기화 소식(가운데 큰 카드)
# 마지막으로 '봤어요' 한 뒤 새로 생긴 것만: 성적 공개, 과제 추가, 마감 바뀜, 자료 도착, 평가 비중 바뀜.
# 기록 store.data["news_seen"] = {"init", "items": {id: {due, c, t, l, x, at}}, "materials": {key: 1}, "weights": {cid: 서명}},
# 성적 '나중에 볼래요' 횟수 store.data["news_snooze"] = {성적 id: 횟수} — 2번 미루면 큰 카드 대신 작은 표시만.

NEWS_KINDS = ("first", "grades", "deadlines", "changes", "materials", "weights")
SNOOZE_MAX = 2
NEWS_ITEMS_CAP = 3000
_PAST_GRACE = timedelta(hours=12)
NEWS_RECENT = timedelta(hours=24)   # 전부터 쓰던 계정을 조용히 초기화할 때도, 이만큼 최근에 들어온 것은 소식으로 남긴다
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)  # "아주 옛날" (Windows는 1970 근처 astimezone이 안 됨)
_ACTIVE = ("review", "scheduled")


def _tkey(title) -> str:
    return re.sub(r"\s+", "", str(title or "")).lower()


def _raw_items(store) -> dict:
    return {it.get("id"): it for it in store.data.get("items") or []}


def _rec(v: dict, raw: dict, now: datetime) -> dict:
    r = raw.get(v["id"]) or {}
    return {"due": r.get("due", v.get("due")), "c": v.get("course_id"), "t": _tkey(r.get("title") or v.get("title")),
            "l": v.get("linked_to"), "x": bool(v.get("conflict")), "at": iso(now)}


def _weight_sigs(store) -> dict:
    """평가 비중이 자동으로 들어간 과목마다 그 비중의 서명(이름·비중)."""
    out = {}
    for cid, cfg in (store.data.get("grades") or {}).items():
        comps = [c for c in (cfg or {}).get("components") or [] if isinstance(c, dict) and c.get("auto")]
        if comps:
            out[str(cid)] = _hash(*sorted(f"{c.get('name')}:{_fmt(c.get('weight'))}" for c in comps))
    return out


def _all_marks(store, items, now) -> dict:
    raw = _raw_items(store)
    return {"items": {v["id"]: _rec(v, raw, now) for v in items},
            "materials": {k: 1 for k in (store.data.get("downloads") or {})}, "weights": _weight_sigs(store)}


def _merge_news_seen(data: dict, marks: dict, init: bool = False) -> None:
    seen = data.setdefault("news_seen", {})
    if init and not seen.get("init"):
        seen["init"] = marks.get("at") or iso(now_local())
    recs = seen.setdefault("items", {})
    recs.update(marks.get("items") or {})
    for k in marks.get("drop") or []:
        recs.pop(k, None)
    if len(recs) > NEWS_ITEMS_CAP:   # 오래된 기록부터 지운다
        for k in sorted(recs, key=lambda k: str(recs[k].get("at") or ""))[:len(recs) - NEWS_ITEMS_CAP]:
            recs.pop(k, None)
    seen.setdefault("materials", {}).update(marks.get("materials") or {})
    seen.setdefault("weights", {}).update(marks.get("weights") or {})


def _short(store, cid) -> str | None:
    for c in (store.data.get("cache") or {}).get("courses", []):
        if str(c.get("id")) == str(cid):
            return short_course_name(c.get("name"))
    return None


def _card(kind: str, covers: list, **fields) -> dict:
    return {"id": f"news:{kind}:" + _hash(kind, *sorted(str(c) for c in covers)), "kind": kind, **fields}


GRADES_FRESH = timedelta(days=3)   # 이보다 오래된 점수만 남았으면 '새 성적'이 아니라 '앱에서 아직 안 본 성적'


def _grades_card(store, grade_pending: list[dict], now: datetime | None = None, first_run: bool = False) -> dict | None:
    """성적 소식 카드. fresh: 첫 실행이 아니고, 합류한 뒤 3일 안에 나온 점수가 하나라도 있을 때만 True(계약 13-4).
    합류 전에 이미 나와 있던 점수는 '새 성적'이 아니라 '앱에서 아직 안 본 성적'이다."""
    snooze_n = store.data.get("news_snooze") or {}
    gs = [p for p in grade_pending if int(snooze_n.get(p["id"]) or 0) < SNOOZE_MAX]
    if not gs:
        return None
    now = now or now_local()
    since = max(now - GRADES_FRESH, parse_iso(store.data.get("celebrate_init")) or now)
    fresh = (not first_run) and any((parse_iso(p.get("at")) or _EPOCH) >= since for p in gs)
    return _card("grades", [p["id"] for p in gs], count=len(gs), mood="surprise", grade_ids=[p["id"] for p in gs],
                 snoozed=any(snooze_n.get(p["id"]) for p in gs), fresh=fresh,
                 items=[{"course_short": p.get("course_short"), "title": p.get("title"), "exam": p.get("exam"),
                         "kind": p.get("kind")} for p in gs[:3]])


def _same_task(a: dict, b: dict) -> bool:
    if a.get("linked_to") and a.get("linked_to") == b.get("linked_to"):
        return True
    return a.get("course_id") == b.get("course_id") and _tkey(a.get("title")) == _tkey(b.get("title"))


def _news_parts(store, items: list[dict], now: datetime, seen: dict | None = None):
    """(새 마감, 바뀐 마감, 새 자료, 바뀐 평가 비중, 지난 비중 기록) — 지난번 '봤어요' 기록(seen)과 비교."""
    seen = seen if seen is not None else (store.data.get("news_seen") or {})
    recs = seen.get("items") or {}
    raw = _raw_items(store)
    cur = {v["id"]: v for v in items}
    vanished: dict = {}
    for sid, r in recs.items():
        if sid in cur:
            continue
        if r.get("l"):
            vanished.setdefault(("l", r["l"]), []).append((sid, r))
        vanished.setdefault(("t", r.get("c"), r.get("t")), []).append((sid, r))
    new, changes = [], []
    for v in items:
        if v.get("status") not in _ACTIVE:
            continue
        rv = raw.get(v["id"]) or {}
        due_raw = rv.get("due", v.get("due"))
        rec = recs.get(v["id"])
        if rec is not None:   # 같은 항목(Canvas 과제)의 마감이 바뀜
            if v.get("origin") == "canvas" and due_raw and rec.get("due") != due_raw:
                changes.append({"id": v["id"], "old": rec.get("due"), "new": due_raw, "conflict": False, "drop": []})
            continue
        d = parse_iso(due_raw)
        if not d or d < now - _PAST_GRACE:
            continue           # 이미 지난 마감은 소식이 아니다
        keys = ([("l", v["linked_to"])] if v.get("linked_to") else []) + \
               [("t", v.get("course_id"), _tkey(rv.get("title") or v.get("title")))]
        old = None
        for k in keys:
            cands = [x for x in vanished.get(k, []) if x[1].get("due") != due_raw]
            if cands:
                old = max(cands, key=lambda x: str(x[1].get("at") or ""))
                break
        sib = [b for b in items if b["id"] != v["id"] and b["id"] in recs and b.get("status") in _ACTIVE
               and _same_task(v, b)]
        if old is not None:    # 찾은 마감은 날짜가 id에 들어 있어, 마감이 바뀌면 새 항목이 생기고 옛 항목은 사라진다
            changes.append({"id": v["id"], "old": old[1].get("due"), "new": due_raw, "conflict": False,
                            "drop": [old[0]]})
        elif v.get("conflict") and sib:
            changes.append({"id": v["id"], "old": (raw.get(sib[0]["id"]) or {}).get("due", sib[0].get("due")),
                            "new": due_raw, "conflict": True, "drop": []})
        else:
            new.append(v)
    mats_seen = seen.get("materials") or {}
    mats = [(k, m) for k, m in (store.data.get("downloads") or {}).items() if k not in mats_seen]
    w_seen = seen.get("weights") or {}
    weights = [(cid, sig) for cid, sig in _weight_sigs(store).items() if w_seen.get(cid) != sig]
    return new, changes, mats, weights, w_seen


def _news(store, items: list[dict], now: datetime, grade_pending: list[dict]) -> tuple[list[dict], dict | None]:
    """화면 가운데에 하나씩 띄울 소식 카드와, 조용히 '본 것'으로 둘 처음 기록(이 기능 전부터 쓰던 계정)."""
    if store.data.get("cache") is None:
        return [], None
    seen = store.data.get("news_seen") or {}
    if not seen.get("init"):
        if len(store.data.get("runs") or []) > 1:   # 전부터 쓰던 계정: 쌓인 것을 한꺼번에 '새 소식'이라 하지 않는다
            grades = _grades_card(store, grade_pending, now)
            marks = _quiet_marks(store, items, now)   # 단, 최근 24시간 안에 들어온 것(업데이트 직후 동기화 등)은 소식으로
            return _regular_cards(store, items, now, grades, marks), marks
        grades = _grades_card(store, grade_pending, now, first_run=True)
        # 첫 실행 요약: 겹치지 않게 — 일정에 넣은 (다가오는) 마감 + 확인할 후보 = count_deadlines, 그리고 받은 자료
        scheduled = [v for v in items if v.get("status") == "scheduled" and parse_iso(v.get("due"))
                     and parse_iso(v.get("due")) >= now - _PAST_GRACE]
        review = [v for v in items if v.get("status") == "review"]
        mats = len(store.data.get("downloads") or {})
        n_deadlines = len(scheduled) + len(review)
        rows = [{"label": "일정에 넣은 마감", "n": len(scheduled), "go": "dash"},
                {"label": "확인할 후보", "n": len(review), "go": "inbox"},
                {"label": "받은 자료", "n": mats, "go": "materials"}]
        rows = [r for r in rows if r["n"]]
        cards = []
        if rows:   # 처음 동기화: 항목마다가 아니라 한 장으로
            cards.append({"id": "news:first", "kind": "first", "count": n_deadlines + mats, "mood": "start",
                          "count_deadlines": n_deadlines, "count_materials": mats, "items": rows})
        if grades:
            cards.append(grades)
        if not rows:
            return cards, {**_all_marks(store, items, now), "at": iso(now)}
        return cards, None
    return _regular_cards(store, items, now, _grades_card(store, grade_pending, now), seen), None


def _quiet_marks(store, items: list[dict], now: datetime) -> dict:
    """전부터 쓰던 계정의 첫 계산: 지금 있는 것을 '본 것'으로 — 최근 NEWS_RECENT 안에 처음 보인 마감·받은 자료·자동 비중은 빼고."""
    recent = now - NEWS_RECENT
    fresh = lambda stamp: (parse_iso(stamp) or _EPOCH) >= recent  # noqa: E731
    marks = _all_marks(store, items, now)
    marks["items"] = {k: r for k, r in marks["items"].items()
                      if not any(v["id"] == k and fresh(v.get("first_seen")) for v in items)}
    downloads = store.data.get("downloads") or {}
    marks["materials"] = {k: 1 for k in marks["materials"] if not fresh((downloads.get(k) or {}).get("downloaded_at"))}
    cfgs = store.data.get("grades") or {}
    marks["weights"] = {cid: sig for cid, sig in marks["weights"].items()
                        if not fresh((cfgs.get(cid) or {}).get("auto_updated_at"))}
    marks["at"] = iso(now)
    return marks


def _regular_cards(store, items: list[dict], now: datetime, grades: dict | None, seen: dict) -> list[dict]:
    new, changes, mats, weights, w_seen = _news_parts(store, items, now, seen)
    cards = [grades] if grades else []
    cur = {v["id"]: v for v in items}
    if new:
        new.sort(key=lambda v: str(v.get("due") or ""))
        cards.append(_card("deadlines", [v["id"] for v in new], count=len(new),
                           mood="busy" if len(new) >= 3 else "run",
                           review_ids=[v["id"] for v in new if v.get("status") == "review"],
                           items=[{"id": v["id"], "course_short": v.get("course_short"), "title": v.get("title"),
                                   "due": v.get("due"), "status": v.get("status"), "kind": v.get("kind")}
                                  for v in new[:3]]))
    if changes:
        cards.append(_card("changes", [c["id"] for c in changes], count=len(changes), mood="surprise",
                           items=[{"id": c["id"], "course_short": cur[c["id"]].get("course_short"),
                                   "title": cur[c["id"]].get("title"), "old": c["old"], "new": c["new"],
                                   "conflict": c["conflict"], "status": cur[c["id"]].get("status")}
                                  for c in changes[:3]]))
    if mats:
        mats.sort(key=lambda km: str(km[1].get("posted_at") or km[1].get("downloaded_at") or ""), reverse=True)
        cards.append(_card("materials", [k for k, _ in mats], count=len(mats), mood="start",
                           items=[{"key": k, "filename": m.get("filename"), "post_title": m.get("post_title"),
                                   "course_short": m.get("course_short") or _short(store, m.get("course_id"))}
                                  for k, m in mats[:3]]))
    if weights:
        cfgs = store.data.get("grades") or {}
        rows = []
        for cid, _ in weights:
            cfg = cfgs.get(cid) or {}
            comps = [{"name": c.get("name"), "weight": c.get("weight")} for c in cfg.get("components") or []
                     if isinstance(c, dict) and c.get("auto")]
            prev = cfg.get("auto_previous") or None   # 자동 비중이 공지 등으로 바뀌면 gradebook이 이전 비중을 남긴다
            rows.append({"course_id": cid, "course_short": _short(store, cid), "changed": bool(prev) or cid in w_seen,
                         "components": comps, "diff": _weights_diff(prev, comps) if prev else None,
                         "source": cfg.get("auto_source"), "by": cfg.get("auto_by")})
        cards.append(_card("weights", [f"{cid}:{sig}" for cid, sig in weights], count=len(weights), mood="surprise",
                           changed=any(r["changed"] for r in rows), items=rows[:3]))
    return cards


def _weights_diff(prev: list[dict], comps: list[dict]) -> dict:
    p = {_tkey(c.get("name")): c for c in prev or []}
    n = {_tkey(c.get("name")): c for c in comps}
    return {"added": [n[k] for k in n if k not in p], "removed": [p[k] for k in p if k not in n],
            "changed": [{"name": n[k]["name"], "from": p[k].get("weight"), "to": n[k].get("weight")}
                        for k in n if k in p and _fmt(p[k].get("weight")) != _fmt(n[k].get("weight"))]}


def _mark_news(store, items: list[dict], now: datetime, want: set, grade_entries: list[dict]) -> int:
    """'봤어요' 한 소식 카드가 다룬 것을 기록한다. 성적 카드는 성적 반응을 볼 때 따로 기록된다."""
    if not any(i.startswith("news:") for i in want):
        return 0
    seen = store.data.get("news_seen") or {}
    if not seen.get("init"):   # 처음 카드: 지금 있는 것 전부를 본 것으로
        if "news:first" not in want:
            return 0
        _merge_news_seen(store.data, {**_all_marks(store, items, now), "at": iso(now)}, init=True)
        return 1
    raw = _raw_items(store)
    cur = {v["id"]: v for v in items}
    new, changes, mats, weights, _ = _news_parts(store, items, now)
    n = 0
    for card in _news(store, items, now, grade_entries)[0]:
        if card["id"] not in want:
            continue
        n += 1
        if card["kind"] == "deadlines":
            # 새 마감과 함께, 소식이 될 수 없는 항목(지난 마감·제외·겹침)도 지금 모습으로 적어 둔다
            recs = (store.data.get("news_seen") or {}).get("items") or {}
            quiet = [v["id"] for v in items if v["id"] not in recs and v.get("status") not in _ACTIVE]
            _merge_news_seen(store.data, {"items": {i: _rec(cur[i], raw, now) for i in [v["id"] for v in new] + quiet}})
        elif card["kind"] == "changes":
            _merge_news_seen(store.data, {"items": {c["id"]: _rec(cur[c["id"]], raw, now) for c in changes},
                                          "drop": [d for c in changes for d in c["drop"]]})
        elif card["kind"] == "materials":
            _merge_news_seen(store.data, {"materials": {k: 1 for k, _ in mats}})
        elif card["kind"] == "weights":
            _merge_news_seen(store.data, {"weights": dict(weights)})
    return n


def snooze(store, ids: list[str]) -> int:
    """성적 소식 '앗 쫄려요… 나중에 볼래요': 성적 id마다 미룬 횟수를 센다(2번이면 큰 카드로는 다시 안 띄움)."""
    sn = store.data.setdefault("news_snooze", {})
    n = 0
    for i in ids:
        i = str(i)
        if i.startswith(("graded:", "score:")):
            sn[i] = int(sn.get(i) or 0) + 1
            n += 1
    if len(sn) > 500:
        for k in list(sn)[:len(sn) - 500]:
            sn.pop(k, None)
    return n
