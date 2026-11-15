"""잘못 들어간 것 지우기·고치기 — 계약 26-3 (v0.8.0, 담당 B).

사용자: "잘못 기입된 일정·성적 제거를 요청했는데 다른 소리부터 한다", "Assignment가 중복 등록된 것 같아 없애 줘를 요청해도 제거해 주지 않는다".
에이전트(AI 도구 propose_* 또는 규칙 handle)가 내는 제안 네 종류와 그 검사·반영·되돌리기:

- item_remove {item_id, reason}: 마감·시험 항목을 일정에서 뺀다(제외). 에이전트가 넣은 항목(agent_items 기록)은 기록을 지운다.
- item_merge {keep_id, drop_id, reason}: 같은 과제로 보이는 두 항목 → drop을 keep과 같은 것으로(drop은 제외, 이유에 keep 제목).
- component_remove {course_id, component, merge_into?, reason}: 평가 항목 지우기(비중은 merge_into에 더하거나 그대로 뺌).
  다음 동기화의 평가 비중 자동 채우기가 같은 항목을 다시 만들지 않아야 한다(성적 설정의 "removed" 기록 → gradebook.drop_removed).
- score_clear {course_id, component, which?, key?, reason}: 잘못 들어간 점수·연결 떼기. which "score" = 그 항목의 점수·만점·평균·
  표준편차를 비움, "link" = 점수 후보 key의 연결을 '연결 안 함'(사용자 연결 grade_links)으로. which가 없으면 key가 있을 때 link.

모든 제안의 args에 auto(bool, 계약 26-4)가 올 수 있다 — 검사 결과에 그대로(auto는 True일 때만 True).

모양(이름 고정 — agent.py가 부른다):
- TYPES: 위 네 종류.
- check(ctx, p, by, why) -> dict: agent.check_proposal과 같은 화면용 모양(type, by, course_id, course_short, reason, summary,
  rejected_reason, applied, applied_at, undone_at, result, notes, auto, 근거 칸은 None, …대상 칸). 근거 글(evidence)은 필요 없다 —
  대상이 지금 있는지(제외한 과목 빼고), 제목·날짜·이름·비중을 summary에 그대로 보여 주는지를 검사한다.
- APPLY: {type: (apply(store, ctx, p, thread) -> (undo 기록, result), undo(store, undo 기록))} — agent._APPLY와 같은 모양.
  그 사이 사용자가 직접 바꿨으면 undo는 agent.AgentConflict.
- handle(store, message, course_id) -> dict | None: AI가 꺼져 있을 때의 규칙 갈래(계약 22-1 모양). '지워줘·없애줘·빼줘·합쳐줘'처럼
  지우기·합치기를 시킨 말이 아니거나, 메시지에 항목 제목·평가 항목 이름이 없으면 None. 제안 카드만(바로 반영하지 않음).

agent.py와 서로 가져오므로 agent·core·gradebook은 함수 안에서 가져온다(모듈을 다 읽기 전에 부르지 않게).
"""
from __future__ import annotations

import copy
import re

from . import links as lk
from .extract import same_task, title_tokens
from .timeutil import iso, now_local, parse_iso

TYPES = ("item_remove", "item_merge", "component_remove", "score_clear")
ITEM_TYPES = ("item_remove", "item_merge")   # 반영·되돌리기가 마감·일정을 바꾼다 → changed(구글 캘린더, 계약 26-4)
SCORE_FIELDS = ("score", "max_score", "mean", "sd")
WHICH = ("score", "link")
MAX_PROPOSALS = 4


def _flat(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _josa(word, final: str, plain: str) -> str:
    """받침이 있으면 final('과'·'은'·'을'), 없으면 plain('와'·'는'·'를'). 한글·숫자는 읽는 소리로, 영어는 l·m·n만 받침으로 본다."""
    s = re.sub(r"[\s'\"‘’“”()\[\]{}<>.,·:;!?~\-]+$", "", str(word or ""))
    if not s:
        return plain
    ch = s[-1]
    if "가" <= ch <= "힣":
        return final if (ord(ch) - 0xAC00) % 28 else plain
    if ch.isdigit():
        return final if ch in "013678" else plain
    return final if ch.lower() in "lmn" else plain


def _num(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _pct(w) -> str:
    return "비중 없음" if w is None else f"{_num(w)}%"


def _when(due) -> str:
    from .agent import _md
    d = parse_iso(due) if isinstance(due, str) else None
    return _md(d) if d else "날짜 없음"


def _label(v: dict) -> str:
    return f"'{v.get('title')}' {_when(v.get('due'))}"


def _head(ctx, cid) -> str:
    short = ctx.shorts.get(cid) if cid else None
    return f"[{short}] " if short else ""


def _score_text(vals: dict) -> str:
    parts = []
    if vals.get("score") is not None:
        parts.append(_num(vals["score"]) + (f"/{_num(vals['max_score'])}" if vals.get("max_score") is not None else ""))
    elif vals.get("max_score") is not None:
        parts.append(f"만점 {_num(vals['max_score'])}")
    if vals.get("mean") is not None:
        parts.append(f"평균 {_num(vals['mean'])}")
    if vals.get("sd") is not None:
        parts.append(f"표준편차 {_num(vals['sd'])}")
    return " · ".join(parts) or "점수 없음"


def _agent_record(store, iid) -> dict | None:
    return next((r for r in store.data.get("agent_items") or [] if isinstance(r, dict) and r.get("id") == iid), None)


# ---------------------------------------------------------------- 검사(화면용 모양 — 계약 20-3과 같은 칸)

def check(ctx, p: dict, by: str = "ai", why: str | None = None) -> dict:
    """제안 하나를 지금 상태(ctx = agent.Ctx)로 검사해 화면용 모양으로. 통과하지 못하면 rejected_reason(한국어 / 영어)."""
    from .agent_cli import unplace
    p = p if isinstance(p, dict) else {}
    typ = str(p.get("type") or "")
    errs: list[str] = [why] if why else []
    if typ not in TYPES:
        errs.append(f"모르는 제안 종류 '{typ[:20]}' / unknown proposal type")
    reason = _flat(unplace(str(p.get("reason") or "")))[:300]
    if not reason:
        errs.append("이유(reason)가 없어요 / reason is required")
    cid = p.get("course_id")
    out = {"type": typ, "by": by, "course_id": None if cid in (None, "") else str(cid), "course_short": None,
           "reason": reason, "summary": "", "rejected_reason": None, "applied": False, "applied_at": None,
           "undone_at": None, "result": None, "notes": [], "evidence": None, "source_key": None, "source_type": None,
           "source_title": None, "posted_at": None, "url": None, "date_basis": None, "auto": p.get("auto") is True}
    fn = _CHECKS.get(typ)
    if fn is not None:
        fn(ctx, p, out, errs)
    out["course_short"] = ctx.shorts.get(out["course_id"]) if out["course_id"] else None
    out["rejected_reason"] = "; ".join(dict.fromkeys(e for e in errs if e))[:400] or None
    return out


def _course_ok(ctx, cid, errs) -> bool:
    if cid is None:
        errs.append("과목(course_id)이 없어요 / course_id is required")
        return False
    if cid not in ctx.courses:
        errs.append("과목 목록에 없는 과목이에요(제외한 과목 포함) / unknown or hidden course_id")
        return False
    return True


def _item(ctx, iid: str, errs, field: str = "item_id") -> dict | None:
    if not iid:
        errs.append(f"{field}가 없어요 / {field} is required")
        return None
    it = ctx.by_id.get(iid)
    if it is None:
        raw = next((x for x in ctx.store.data.get("items") or [] if isinstance(x, dict) and x.get("id") == iid), None)
        if raw is not None and str(raw.get("course_id")) in ctx.hidden:
            errs.append(f"제외한 과목의 항목이에요({field}) / {field} belongs to a hidden course")
        else:
            errs.append(f"항목 목록에 없는 {field}예요 / unknown {field}")
    return it


def _item_course(ctx, out: dict, it: dict, errs) -> None:
    icid = str(it["course_id"]) if it.get("course_id") is not None else None
    if out["course_id"] is not None and icid != out["course_id"]:
        errs.append("그 항목은 다른 과목이에요 / item belongs to another course")
        return
    out["course_id"] = icid


def _check_remove(ctx, p, out, errs):
    iid = _flat(p.get("item_id"))
    it = _item(ctx, iid, errs)
    rec = _agent_record(ctx.store, iid) if iid else None
    out.update(item_id=iid, item_title=it["title"] if it else None, item_due=it.get("due") if it else None,
               item_kind=it.get("kind") if it else None, item_status=it.get("status") if it else None,
               item_origin=("agent" if rec else it.get("origin")) if it else None)
    if it is None:
        return
    _item_course(ctx, out, it, errs)
    if it.get("status") == "rejected" and rec is None:
        errs.append("이미 제외한 항목이에요 / item is already rejected")
    head = _head(ctx, out["course_id"])
    if rec is not None:
        out["summary"] = f"{head}{_label(it)} — 에이전트가 넣은 항목이라 지우기"
        out["notes"].append("'에이전트에게 묻기'로 넣은 마감이라 기록을 지워요(되돌리기로 다시 넣을 수 있어요)")
    else:
        out["summary"] = f"{head}{_label(it)} — 일정에서 빼기(제외)"
        if it.get("origin") == "canvas":
            out["notes"].append("Canvas의 과제는 그대로 두고 이 앱의 일정·캘린더에서만 빼요")
        elif it.get("origin") == "video":
            out["notes"].append("LMS의 영상은 그대로 두고 이 앱의 일정·캘린더에서만 빼요")


def _check_merge(ctx, p, out, errs):
    keep_id, drop_id = _flat(p.get("keep_id")), _flat(p.get("drop_id"))
    if keep_id and keep_id == drop_id:
        errs.append("남길 항목과 없앨 항목이 같아요 — 서로 다른 두 항목이어야 해요 / keep_id and drop_id must differ")
        keep = drop = None
        _item(ctx, keep_id, errs, "keep_id")
    else:
        keep = _item(ctx, keep_id, errs, "keep_id")
        drop = _item(ctx, drop_id, errs, "drop_id")
    out.update(keep_id=keep_id, drop_id=drop_id, keep_title=keep["title"] if keep else None,
               drop_title=drop["title"] if drop else None, keep_due=keep.get("due") if keep else None,
               drop_due=drop.get("due") if drop else None)
    if keep is None or drop is None:
        return
    if str(keep.get("course_id")) != str(drop.get("course_id")):
        errs.append("두 항목의 과목이 달라요 — 같은 과목의 항목만 합칠 수 있어요 / items belong to different courses")
    _item_course(ctx, out, keep, errs)
    if drop.get("status") == "rejected":
        errs.append("없앨 항목(drop)은 이미 제외돼 있어요 / drop item is already rejected")
    if keep.get("status") == "rejected":
        errs.append("남길 항목(keep)이 제외돼 있어요 — 먼저 되살려 주세요 / keep item is rejected")
    out["summary"] = (f"{_head(ctx, out['course_id'])}같은 과제로 합치기 — {_label(drop)} 빼고 "
                      f"{_label(keep)}만 남기기")
    out["notes"].append(f"'{drop['title']}'{_josa(drop['title'], '은', '는')} 제외하고 '{keep['title']}' 하나만 남겨요")


def _comp_name(ctx, cid, raw) -> str | None:
    """그 과목 평가 항목의 저장된 이름: 공백·대소문자 무시(links.norm), 아니면 흔한 표현이 같은 하나('Assignment' = '과제')."""
    if cid is None or not isinstance(raw, str) or not raw.strip():
        return None
    name = ctx.component(cid, raw)
    if name:
        return name
    from .gradebook import _canon
    want = _canon(raw)
    hits = [c["name"] for c in ctx.components(cid) if c.get("name") and _canon(c["name"]) == want]
    return hits[0] if len(hits) == 1 else None


def _comp(ctx, cid, name) -> dict | None:
    if not name:
        return None
    return next((c for c in ctx.components(cid) if c.get("name") == name), None)


def _weight(c: dict | None) -> float | None:
    w = (c or {}).get("weight")
    return float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) else None


def _check_component(ctx, p, out, errs):
    cid = out["course_id"]
    ok = _course_ok(ctx, cid, errs)
    raw = p.get("component") if p.get("component") not in (None, "") else p.get("name")
    name = _comp_name(ctx, cid, raw) if ok else None
    if ok and name is None:
        errs.append("그 과목에 없는 평가 항목이에요 / component is not in this course")
    into_raw = p.get("merge_into")
    into = None
    if into_raw not in (None, ""):
        into = _comp_name(ctx, cid, into_raw) if ok else None
        if ok and into is None:
            errs.append("합칠 평가 항목(merge_into)이 그 과목에 없어요 / merge_into is not in this course")
        elif name is not None and into == name:
            errs.append("같은 항목에는 합칠 수 없어요 / merge_into must be a different component")
    comp, tgt = _comp(ctx, cid, name), _comp(ctx, cid, into)
    w, tw = _weight(comp), _weight(tgt)
    new = round((tw or 0.0) + (w or 0.0), 2) if tgt is not None and into != name else None
    out.update(component=name or (_flat(raw)[:40] or None), merge_into=into or (_flat(into_raw)[:40] or None),
               weight=w, merge_weight=tw, new_weight=new)
    if comp is None:
        return
    head = _head(ctx, cid)
    if new is not None:
        out["summary"] = f"{head}평가 항목 '{name}'({_pct(w)}) 지우고 비중을 '{into}'에 더하기({_pct(tw)} → {_pct(new)})"
    else:
        out["summary"] = f"{head}평가 항목 '{name}'({_pct(w)}) 지우기"
        total = round(sum(_weight(c) or 0.0 for c in ctx.components(cid)), 2)
        if w:
            out["notes"].append(f"비중 합이 {_num(total)}% → {_num(round(total - w, 2))}%가 돼요")
    out["notes"].append(f"다음 동기화에서 평가 비중을 자동으로 채울 때도 '{name}'{_josa(name, '은', '는')} 다시 넣지 않아요")


def _check_score(ctx, p, out, errs):
    cid = out["course_id"]
    ok = _course_ok(ctx, cid, errs)
    raw = p.get("component") if p.get("component") not in (None, "") else p.get("name")
    name = _comp_name(ctx, cid, raw) if ok else None
    if ok and name is None:
        errs.append("그 과목에 없는 평가 항목이에요 / component is not in this course")
    key = _flat(p.get("key"))[:300]
    which = p.get("which")
    which = str(which).strip().lower() if which not in (None, "") else ("link" if key else "score")
    if which not in WHICH:
        errs.append("which는 'score'(점수 칸 비우기) 또는 'link'(점수 후보 연결 떼기)여야 해요 / which must be score or link")
    comp = _comp(ctx, cid, name)
    out.update(component=name or (_flat(raw)[:40] or None), which=which, key=key or None, weight=_weight(comp))
    if comp is None or which not in WHICH:
        return
    head = f"{_head(ctx, cid)}'{name}'({_pct(_weight(comp))})"
    links = ctx.links(cid)
    if which == "score":
        vals = {k: comp.get(k) for k in SCORE_FIELDS}
        out["cleared"] = vals
        if all(v is None for v in vals.values()):
            linked = any(ln.get("component") == name for ln in links.values())
            errs.append(f"'{name}'에 지울 점수가 없어요"
                        + (" — 연결된 점수 후보는 which 'link'와 key로 떼어 주세요" if linked else "")
                        + " / no stored score to clear")
            return
        out["summary"] = f"{head}의 점수 지우기 — {_score_text(vals)}"
        return
    if not key:
        errs.append("떼어 낼 점수 후보(key)가 없어요 / key is required for which=link")
        return
    cand = next((c for c in ctx.cands if c.get("key") == key and str(c.get("course_id")) == cid), None)
    if cand is None:
        errs.append("그 과목의 점수 후보에 없는 key예요 / unknown candidate key")
        return
    ln = links.get(key) or {}
    if ln.get("component") != name:
        errs.append(f"그 점수 후보는 '{name}'에 연결돼 있지 않아요 / candidate is not linked to this component")
    label = _flat(cand.get("label") or cand.get("source_title") or "점수")[:60]
    out.update(score_label=label, score=cand.get("score"), max_score=cand.get("max_score"), link_by=ln.get("by"))
    out["summary"] = f"{head}에서 점수 후보 '{label} {_score_text(cand)}' 떼기(연결 안 함)"
    out["notes"].append("점수 후보는 그대로 두고 이 평가 항목에 넣지 않게만 해요(성적 화면에서 다시 연결할 수 있어요)")


_CHECKS = {"item_remove": _check_remove, "item_merge": _check_merge, "component_remove": _check_component,
           "score_clear": _check_score}


# ---------------------------------------------------------------- 반영·되돌리기(agent._APPLY와 같은 모양)

def _conflict(msg: str):
    from .agent import AgentConflict
    return AgentConflict(msg)


def _reject(store, iid: str, p: dict, reason: str) -> dict:
    """그 항목을 제외(사용자 결정 rejected)로 — AI 제안이면 decided_by "ai"·이유, 규칙 제안([반영]을 누름)이면 사용자 결정."""
    before = copy.deepcopy(store.data["user"].get(iid))
    by = "ai" if p.get("by") == "ai" else "user"
    store.update_user(iid, {"decision": "rejected"}, by=by, reason=reason if by == "ai" else None)
    if p.get("_ref"):
        store.data["user"][iid]["agent_ref"] = p["_ref"]   # 항목 카드의 [되돌리기]가 /api/agent/undo를 부르게(계약 20-4)
    return {"item_id": iid, "before": before, "after": copy.deepcopy(store.data["user"].get(iid))}


def _check_after(store, recs: list) -> None:
    for a in recs:
        if store.data["user"].get(a["item_id"]) != a["after"]:
            raise _conflict("그 사이 이 항목을 직접 바꿔서 되돌리지 않았어요")


def _restore(store, a: dict) -> None:
    if a.get("before") is None:
        store.data["user"].pop(a["item_id"], None)
    else:
        store.data["user"][a["item_id"]] = copy.deepcopy(a["before"])


def _apply_remove(store, ctx, p, th):
    from . import core
    iid = p["item_id"]
    reason = p.get("reason") or "에이전트에게 묻기로 제외"
    recs = list(store.data.get("agent_items") or [])
    idx = next((i for i, r in enumerate(recs) if isinstance(r, dict) and r.get("id") == iid), None)
    if idx is None:
        return {"mode": "decision", **_reject(store, iid, p, reason)}, {"item_id": iid}
    from .agent import same_deadline
    rec = recs.pop(idx)
    before_ids = {it.get("id") for it in store.data.get("items") or []}
    store.data["agent_items"] = recs
    core.refresh_items(store)
    # 에이전트 항목이 품고 있던 규칙 후보(같은 마감)가 다시 나오면 함께 제외한다 — 지웠는데 후보로 되살아나지 않게
    a = core.agent_item(rec)
    also = [_reject(store, it["id"], p, reason) for it in store.data["items"]
            if it.get("id") not in before_ids and it.get("origin") == "found" and not it.get("found_by")
            and same_deadline(a, it)]
    return ({"mode": "agent", "record": copy.deepcopy(rec), "index": idx, "also": also},
            {"item_id": iid, "removed": True})


def _undo_remove(store, u):
    from . import core
    if u.get("mode") != "agent":
        _check_after(store, [u])
        _restore(store, u)
        return
    rid = (u.get("record") or {}).get("id")
    recs = list(store.data.get("agent_items") or [])
    if any(isinstance(r, dict) and r.get("id") == rid for r in recs):
        raise _conflict("그 사이 같은 마감을 다시 넣어서 되돌리지 않았어요")
    _check_after(store, u.get("also") or [])
    for a in u.get("also") or []:
        _restore(store, a)
    recs.insert(min(int(u.get("index") or 0), len(recs)), copy.deepcopy(u["record"]))
    store.data["agent_items"] = recs
    core.refresh_items(store)


def _apply_merge(store, ctx, p, th):
    keep = ctx.by_id.get(p["keep_id"]) or {}
    title = keep.get("title") or p.get("keep_title") or "다른 항목"
    reason = f"'{title}'{_josa(title, '과', '와')} 같은 과제라 하나로 합쳤어요"
    if p.get("reason"):
        reason += f" · {p['reason']}"
    u = _reject(store, p["drop_id"], p, reason[:300])
    return {"mode": "decision", **u}, {"item_id": p["drop_id"], "keep_id": p["keep_id"]}


def _cfg(store, cid) -> dict | None:
    cfg = (store.data.get("grades") or {}).get(str(cid))
    return cfg if isinstance(cfg, dict) else None


def _apply_component(store, ctx, p, th):
    from .gradebook import _canon, removed_components
    cid, name, into = p["course_id"], p["component"], p.get("merge_into") or None
    cfg = _cfg(store, cid)
    if cfg is None:
        raise ValueError("이 과목의 성적 설정이 없어요")
    had = "removed" in cfg
    before = {"components": copy.deepcopy(cfg.get("components")), "removed": copy.deepcopy(cfg.get("removed"))}
    comps = [c for c in cfg.get("components") or [] if isinstance(c, dict)]
    target = next((c for c in comps if c.get("name") == name), None)
    if target is None:
        raise ValueError(f"'{name}' 평가 항목이 없어요")
    w = _weight(target) or 0.0
    rest = [c for c in cfg.get("components") or [] if c is not target]
    if into:
        t = next((c for c in rest if isinstance(c, dict) and c.get("name") == into), None)
        if t is None:
            raise ValueError(f"합칠 '{into}' 평가 항목이 없어요")
        t["weight"] = round((_weight(t) or 0.0) + w, 2)
    cfg["components"] = rest
    rec = {"name": name, "weight": w, "merge_into": into, "at": iso(now_local()), "by": p.get("by") or "ai",
           "reason": str(p.get("reason") or "")[:200]}
    cfg["removed"] = [r for r in removed_components(cfg) if _canon(r["name"]) != _canon(name)] + [rec]
    return ({"cid": cid, "had_removed": had, "before": before,
             "after": {"components": copy.deepcopy(cfg["components"]), "removed": copy.deepcopy(cfg["removed"])}},
            {"course_id": cid, "component": name, "merge_into": into})


def _undo_component(store, u):
    cfg = _cfg(store, u["cid"])
    if (cfg is None or cfg.get("components") != u["after"]["components"]
            or cfg.get("removed") != u["after"]["removed"]):
        raise _conflict("그 사이 성적 설정이 바뀌어 되돌리지 않았어요(성적 화면에서 고쳐 주세요)")
    cfg["components"] = copy.deepcopy(u["before"]["components"])
    if u.get("had_removed"):
        cfg["removed"] = copy.deepcopy(u["before"]["removed"])
    else:
        cfg.pop("removed", None)


def _apply_score(store, ctx, p, th):
    cid, name = p["course_id"], p["component"]
    if p.get("which") == "link":
        key = p["key"]
        links = store.data.setdefault("grade_links", {})
        had = cid in links
        slot = links.setdefault(cid, {})
        before = copy.deepcopy(slot.get(key))
        slot[key] = {"component": None, "at": iso(now_local()), "decided_by": "ai" if p.get("by") == "ai" else "user",
                     "reason": f"'{name}'에서 뗌 · {p.get('reason') or '에이전트에게 묻기'}"[:300]}
        return ({"which": "link", "cid": cid, "key": key, "had_slot": had, "before": before,
                 "after": copy.deepcopy(slot[key])}, {"course_id": cid, "component": name, "which": "link", "key": key})
    cfg = _cfg(store, cid)
    comp = next((c for c in (cfg or {}).get("components") or [] if isinstance(c, dict) and c.get("name") == name), None)
    if comp is None:
        raise ValueError(f"'{name}' 평가 항목이 없어요")
    before = {k: comp.get(k) for k in SCORE_FIELDS}
    for k in SCORE_FIELDS:
        comp[k] = None
    return ({"which": "score", "cid": cid, "name": name, "before": before, "after": {k: None for k in SCORE_FIELDS}},
            {"course_id": cid, "component": name, "which": "score"})


def _undo_score(store, u):
    if u.get("which") == "link":
        links = store.data.setdefault("grade_links", {})
        slot = links.get(u["cid"])
        if not isinstance(slot, dict) or slot.get(u["key"]) != u["after"]:
            raise _conflict("그 사이 이 점수 후보의 연결이 바뀌어 되돌리지 않았어요")
        if u.get("before") is None:
            slot.pop(u["key"], None)
            if not slot and not u.get("had_slot"):
                links.pop(u["cid"], None)
        else:
            slot[u["key"]] = copy.deepcopy(u["before"])
        return
    cfg = _cfg(store, u["cid"])
    comp = next((c for c in (cfg or {}).get("components") or [] if isinstance(c, dict) and c.get("name") == u["name"]),
                None)
    if comp is None or {k: comp.get(k) for k in SCORE_FIELDS} != u["after"]:
        raise _conflict("그 사이 이 평가 항목의 점수가 바뀌어 되돌리지 않았어요(성적 화면에서 고쳐 주세요)")
    comp.update(copy.deepcopy(u["before"]))


APPLY: dict = {"item_remove": (_apply_remove, _undo_remove), "item_merge": (_apply_merge, _undo_remove),
               "component_remove": (_apply_component, _undo_component), "score_clear": (_apply_score, _undo_score)}


# ---------------------------------------------------------------- 규칙 갈래(AI가 꺼져 있을 때, 계약 26-3)

# 지우기·합치기를 시킨 말: 낱말 줄기 + 시키는 말끝('지워줘'·'없애 주세요'·'빼 버려'·문장 끝 '합쳐'). '지워지면'·'삭제하면'·'합쳐?'는 아니다.
_STEM = r"(?:지워|없애|빼|떼어|떼|끊어|합쳐|(?:삭제|제거)\s*(?:좀\s*)?해|중복\S*\s*(?:좀\s*)?정리\s*해)"
_KO_IMP = re.compile(
    _STEM + r"\s*(?:줘|주세요|주시|주실|주라|줄래|줄\s*수|주면|봐|버려|라(?![가-힣]))"
    r"|" + _STEM + r"\s*[.!~]*\s*$"
    r"|(?:지우|없애|빼|삭제하|제거하|합치)고\s*싶"
    r"|(?:삭제|제거|합치기|지우기|빼기)\s*(?:좀\s*)?(?:부탁|요망|하자|할래|해\s*줘|해\s*주세요)")
_EN_IMP = re.compile(
    r"^\s*(?:please\s+|pls\s+|plz\s+)?(?:remove|delete|merge|drop|get\s+rid\s+of|unlink)\b"
    r"|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:remove|delete|merge|drop|get\s+rid\s+of|unlink)\b"
    r"|\bplease\s+(?:remove|delete|merge|drop|get\s+rid\s+of|unlink)\b"
    r"|\b(?:want|need|like)\s+(?:you\s+)?to\s+(?:remove|delete|merge|get\s+rid\s+of)\b", re.I)
_REMOVE = re.compile(r"지워|지우|없애|빼|삭제|제거|떼|끊|remove|delete|drop|get\s+rid|unlink", re.I)
_MERGE = re.compile(r"합쳐|합치|하나로|merge|combine", re.I)
_DUP = re.compile(r"중복|겹쳐|겹친|겹치|두\s*번|duplicat", re.I)
_COMP_HINT = re.compile(r"성적|평가\s*항목|평가|비중|배점|칸|component|weight|grading", re.I)
_ITEM_HINT = re.compile(r"일정|마감|캘린더|달력|대시\s*보드|확인함|deadline|due|calendar|schedule", re.I)
_SCORE = re.compile(r"점수|(?<![a-z])(?:scores?|marks?)(?![a-z])", re.I)
_LINK = re.compile(r"연결|(?<![a-z])link", re.I)
# 다른 갈래가 맡는 것(공부 계획·메일·알림·대화·데이터·캐릭터·코치 카드·체크리스트·계정)
_OTHER = re.compile(r"계획(?!서)|플랜|(?<![a-z])plans?(?![a-z])|메일|e-?mail|초안|알림|대화|채팅|데이터|캐릭터|마스코트|브리핑|회고|"
                    r"체크\s*리스트|checklist|토큰|학번|로그\s*아웃", re.I)
_NEG_AFTER = re.compile(r"\s?(?:은|는|이|가|을|를)?\s?(?:말고|빼고|제외하고|외에|외의|빼면|말구)")
_INTO_AFTER = re.compile(r"(?:에다가?|에|으로|로)(?=\s|$)|\s+쪽")
_DROP_AFTER = re.compile(r"(?:을|를)(?=\s|$)|\s+into(?![a-z])")
_TOK = re.compile(r"[a-z][a-z0-9+#\-]*|[가-힣]+")
_AN = re.compile(r"[a-z0-9]")
_HANGUL = re.compile(r"[가-힣]")


def wants_fix(message: str) -> bool:
    """지우기·합치기를 시킨 말인지(한국어 말끝·영어 명령). 계약 26-4의 '시키는 말' 판단에도 쓸 수 있다."""
    msg = _flat(message)
    return bool(msg and (_KO_IMP.search(msg) or _EN_IMP.search(msg)))


def _spaced(s) -> str:
    """비교용: 소문자, 글자·숫자가 아닌 것은 공백 하나로(한글은 그대로)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w가-힣]+", " ", str(s or "").lower().replace("_", " "))).strip()


def _find(hay: str, needle: str) -> int | None:
    """needle이 hay에 낱말로 들어 있는 자리(영어·숫자는 앞뒤가 이어지지 않게 — 'lab'은 'lab3'·'prelab'에 없음, 복수 s는 허용.
    한글은 낱말 처음에서만 — '과제'는 '기말과제'에 없고 '과제를'에는 있음)."""
    if not needle:
        return None
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return None
        j = i + len(needle)
        b = hay[i - 1] if i > 0 else " "
        a = hay[j] if j < len(hay) else " "
        ok = not (_AN.match(needle[0]) and _AN.match(b)) and not (_HANGUL.match(needle[0]) and _HANGUL.match(b))
        if ok and _AN.match(needle[-1]) and _AN.match(a):
            nxt = hay[j + 1] if j + 1 < len(hay) else " "
            ok = needle[-1].isalpha() and a == "s" and not _AN.match(nxt)
        if ok:
            return i
        start = i + 1


def _generic(name) -> bool:
    """번호 없는 한 낱말의 평가·과제 이름('과제'·'Assignment'·'Report'·'기말고사') — 'LAB 4 과제'의 '과제'처럼 꾸밈말일 수 있다."""
    sp = _spaced(name)
    return bool(sp) and " " not in sp and not re.search(r"\d", sp) and bool(lk.families(name) or title_tokens(name))


def _tokens(sp: str):
    """(낱말, 시작, 끝) — 한글은 조사를 뗀다(agent._strip)."""
    from .agent import _strip
    for m in _TOK.finditer(sp):
        t = _strip(m.group())
        yield t, m.start(), m.start() + len(t)


def _match(kind, obj, cid, strength, pos, end, name) -> dict:
    return {"kind": kind, "obj": obj, "cid": cid, "strength": strength, "pos": pos, "end": end,
            "generic": _generic(name), "name": name}


def _match_items(ctx, sp: str, mt: set, scope: set | None) -> list[dict]:
    m_num = {t for t in mt if t[1] is not None}
    m_terms = {k for k, n in mt if n is None}
    out = []
    for v in ctx.items:
        cid = str(v.get("course_id")) if v.get("course_id") is not None else None
        if cid not in ctx.courses or (scope and cid not in scope):
            continue
        title = str(v.get("title") or "")
        tsp = _spaced(title)
        tt = title_tokens(title)
        nums = {t for t in tt if t[1] is not None}
        terms = {k for k, n in tt if n is None}
        pos = _find(sp, tsp) if len(tsp.replace(" ", "")) >= 2 else None
        if pos is not None or (nums & m_num):
            out.append(_match("item", v, cid, 2, pos, None if pos is None else pos + len(tsp), title))
        elif not nums and terms and terms <= m_terms:
            out.append(_match("item", v, cid, 1, None, None, title))
    return out


def _match_comps(ctx, sp: str, msg: str, mt: set, scope: set | None) -> list[dict]:
    from .gradebook import _canon
    m_num = {t for t in mt if t[1] is not None}
    toks = list(_tokens(sp))
    grams = toks + [(f"{a[0]} {b[0]}", a[1], b[2]) for a, b in zip(toks, toks[1:]) if sp[a[2]:b[1]].strip() == ""]
    out = []
    for cid in ctx.courses:
        if scope and cid not in scope:
            continue
        for c in ctx.components(cid):
            name = str(c.get("name") or "")
            if not name:
                continue
            nsp = _spaced(name)
            pos = _find(sp, nsp)
            end = None if pos is None else pos + len(nsp)
            if pos is None:   # 흔한 표현이 같은 낱말('Assignment' = '과제', 'Midterm' = '중간고사')
                want = _canon(name)
                hit = next(((s, e) for t, s, e in grams if _canon(t) == want), None)
                if hit:
                    pos, end = hit
            nums = {t for t in title_tokens(name) if t[1] is not None}
            if pos is not None or (nums and nums <= m_num):
                out.append(_match("comp", c, cid, 2, pos, end, name))
            elif same_task(msg, name):
                out.append(_match("comp", c, cid, 1, None, None, name))
    return out


def _match_cands(ctx, sp: str, mt: set, scope: set | None) -> list[dict]:
    """점수 후보(연결된 것만) — 'Quiz 2 점수 지워줘'처럼 항목 이름보다 좁은 라벨."""
    m_num = {t for t in mt if t[1] is not None}
    out = []
    for cid in ctx.courses:
        if scope and cid not in scope:
            continue
        links = ctx.links(cid)
        for c in ctx.cands:
            ln = links.get(c.get("key")) or {}
            if str(c.get("course_id")) != cid or not ln.get("component"):
                continue
            label = str(c.get("label") or "")
            lsp = _spaced(label)
            pos = _find(sp, lsp) if len(lsp.replace(" ", "")) >= 2 else None
            nums = {t for t in title_tokens(label) if t[1] is not None}
            if pos is not None or (nums and nums <= m_num):
                out.append({**_match("cand", c, cid, 2, pos, None if pos is None else pos + len(lsp), label),
                            "component": ln["component"]})
    return out


def _prune(ms: list[dict], sp: str) -> list[dict]:
    """'X 말고'는 빼고, 다른 이름 안에 든 짧은 이름·번호 붙은 이름 바로 뒤의 꾸밈말('LAB 4 과제'의 '과제')은 뺀다.
    정확히 맞는 것(strength 2)이 있으면 비슷한 것(1)은 버린다."""
    ms = [m for m in ms if m["end"] is None or not _NEG_AFTER.match(sp[m["end"]:])]
    spans = [m for m in ms if m["pos"] is not None]
    keep = []
    for m in ms:
        if m["pos"] is not None and any(o is not m and o["pos"] <= m["pos"] and m["end"] <= o["end"]
                                        and (o["end"] - o["pos"]) > (m["end"] - m["pos"]) for o in spans):
            continue
        if m["generic"] and m["pos"] is not None:
            specific = [o for o in ms if not o["generic"] and o["strength"] == 2]
            before = sp[:m["pos"]]
            if specific and (re.search(r"\d\s?$", before)
                             or any(o["end"] is not None and before.rstrip() == sp[:o["end"]].rstrip() for o in specific)):
                continue
        keep.append(m)
    if any(m["strength"] == 2 for m in keep):
        keep = [m for m in keep if m["strength"] == 2]
    return keep


def _rank(v: dict) -> int:
    """합칠 때 남길 쪽: Canvas 과제 > LMS 영상 > 그 밖, 일정(승인)이면 +1."""
    return {"canvas": 3, "video": 2}.get(v.get("origin"), 0) + (1 if v.get("status") == "scheduled" else 0)


def _direction(sp: str, a: dict, b: dict):
    """'A를 B에 합쳐'·'merge A into B' → (없앨 것, 남길 것). 모르면 None."""
    for m, other in ((a, b), (b, a)):
        if m["end"] is None:
            continue
        rest = sp[m["end"]:]
        if _DROP_AFTER.match(rest):
            return m, other
        if _INTO_AFTER.match(rest):
            return other, m
    return None


def _order(ms: list[dict]) -> list[dict]:
    return sorted(ms, key=lambda m: (m["pos"] is None, m["pos"] or 0))


def _pair(sp: str, a: dict, b: dict, by_rank: bool):
    """(없앨 것, 남길 것): 말의 방향 → (항목이면) 남길 쪽 순위 → 먼저 말한 것을 없앰."""
    d = _direction(sp, a, b)
    if d:
        return d
    first, second = _order([a, b])
    if by_rank and _rank(first["obj"]) > _rank(second["obj"]):
        return second, first
    return first, second


def _partner(ctx, it: dict) -> dict | None:
    """같은 과제로 보이는 다른 항목: Canvas 중복(duplicate_of) → 같은 과목·같은 날의 같은 마감(agent.same_deadline)."""
    from .agent import same_deadline
    dup = ctx.by_id.get(it.get("duplicate_of") or "")
    if dup is not None and dup.get("status") != "rejected":
        return dup
    same = [v for v in ctx.items if v["id"] != it["id"] and str(v.get("course_id")) == str(it.get("course_id"))
            and v.get("status") != "rejected" and same_deadline(it, v)]
    return max(same, key=_rank) if same else None


def _score_raws(ctx, m: dict, msg: str, why: str) -> list[dict]:
    cid, name = m["cid"], m["name"]
    comp = m["obj"]
    linked = [k for k, ln in ctx.links(cid).items() if ln.get("component") == name]
    stored = any(comp.get(k) is not None for k in SCORE_FIELDS)
    if linked and (_LINK.search(msg) or not stored):
        return [{"type": "score_clear", "course_id": cid, "component": name, "which": "link", "key": k, "reason": why}
                for k in linked[:MAX_PROPOSALS]]
    return [{"type": "score_clear", "course_id": cid, "component": name, "which": "score", "reason": why}]


def _raws(ctx, msg: str, sp: str, items: list, comps: list, cands: list, why: str) -> tuple[list[dict], list[str]]:
    raws, notes = [], []
    if cands or (comps and _SCORE.search(msg)):
        for m in cands:
            raws.append({"type": "score_clear", "course_id": m["cid"], "component": m["component"], "which": "link",
                         "key": m["obj"]["key"], "reason": why})
        for m in comps:
            raws += _score_raws(ctx, m, msg, why)
        return raws, notes
    merge, dup, remove = bool(_MERGE.search(msg)), bool(_DUP.search(msg)), bool(_REMOVE.search(msg))
    # 평가 항목: 'A를 B에 합쳐' → A를 지우고 비중을 B에, 그 밖은 지우기('중복'은 비중을 더하지 않는다 — 두 번 센 것이므로)
    by_course: dict[str, list] = {}
    for m in comps:
        by_course.setdefault(m["cid"], []).append(m)
    comp_note = None
    paired: set = set()
    for cid, ms in by_course.items():
        if merge and len(ms) == 2:
            drop, keep = _pair(sp, ms[0], ms[1], by_rank=False)
            raws.append({"type": "component_remove", "course_id": cid, "component": drop["name"],
                         "merge_into": keep["name"], "reason": why})
            paired |= {m["pos"] for m in ms if m["pos"] is not None}
        elif merge and not remove:
            comp_note = "합칠 두 평가 항목 이름을 함께 적어 주세요(예: 'A를 B에 합쳐줘')"
        else:
            raws += [{"type": "component_remove", "course_id": cid, "component": m["name"], "reason": why} for m in ms]
    # 마감 항목: 합치기·중복이면 짝을 찾아 item_merge, 아니면 item_remove. 평가 항목 둘을 합치는 말의 같은 자리 이름은 평가 항목 쪽
    items = [m for m in items if m["pos"] is None or m["pos"] not in paired]
    if not (merge or dup):
        raws += [{"type": "item_remove", "item_id": m["obj"]["id"], "reason": why} for m in items]
        if comp_note and not raws:
            notes.append(comp_note)
        return raws, notes
    by_course = {}
    for m in items:
        by_course.setdefault(m["cid"], []).append(m)
    for cid, ms in by_course.items():
        if len(ms) == 2:
            drop, keep = _pair(sp, ms[0], ms[1], by_rank=True)
            raws.append({"type": "item_merge", "keep_id": keep["obj"]["id"], "drop_id": drop["obj"]["id"], "reason": why})
            continue
        for m in ms:
            it = m["obj"]
            other = _partner(ctx, it)
            if other is not None:
                drop, keep = (other, it) if _rank(other) < _rank(it) else (it, other)
                raws.append({"type": "item_merge", "keep_id": keep["id"], "drop_id": drop["id"], "reason": why})
            elif remove:
                raws.append({"type": "item_remove", "item_id": it["id"], "reason": why})
            else:
                notes.append(f"'{it['title']}'{_josa(it['title'], '과', '와')} 같은 과제로 보이는 다른 항목을 찾지 못했어요 — "
                             "두 항목 제목을 함께 적어 주세요")
    if comp_note and not raws and not notes:
        notes.append(comp_note)
    return raws, notes


def _answer(ok: list[dict], failed: list[dict], notes: list[str]) -> str:
    if len(ok) == 1:
        parts = [f"{ok[0]['summary']} — 맞으면 [반영]을 눌러 주세요. 반영한 뒤에도 [되돌리기]로 되돌릴 수 있어요."]
    elif ok:
        parts = ["맞는 것만 [반영]을 눌러 주세요(되돌릴 수 있어요): " + " / ".join(f"‘{p['summary']}’" for p in ok)]
    elif failed:
        parts = [f"그대로는 반영할 수 없어요: {failed[0]['rejected_reason']}"]
    else:
        parts = []
    return " ".join(parts + notes).strip()


def handle(store, message: str, course_id=None):
    """AI가 꺼져 있을 때 '지워줘·없애줘·빼줘·삭제해줘·합쳐줘·중복' + 메시지에 나온 항목 제목·평가 항목 이름 → 제안 카드(by "rules").
    시키는 말이 아니거나(질문 '과제 지워지면 어떻게 돼?') 다른 갈래의 것(공부 계획·메일·알림 …)이거나 대상이 없으면 None.
    결과 {"answer", "proposals": [check를 거친 제안], "by": "rules"} — 바로 반영하지 않는다."""
    msg = _flat(message)
    if not msg or len(msg) > 500 or not wants_fix(msg) or _OTHER.search(msg):
        return None
    from . import agent
    ctx = agent.Ctx(store)
    if not ctx.courses:
        return None
    sp = _spaced(msg)
    mt = title_tokens(msg)
    named = agent.mentioned_courses(msg, list(ctx.courses.values()))
    scope = {str(c) for c in named} or None
    score = bool(_SCORE.search(msg))
    comps = _match_comps(ctx, sp, msg, mt, scope)
    items = [] if score else _match_items(ctx, sp, mt, scope)
    cands = _match_cands(ctx, sp, mt, scope) if score else []
    ms = _prune(items + comps + cands, sp)
    cid = str(course_id) if course_id not in (None, "") else None
    if scope is None and cid in ctx.courses and any(m["cid"] == cid for m in ms):
        ms = [m for m in ms if m["cid"] == cid]   # 과목 화면에서 물었으면 그 과목의 것만
    comp_hint, item_hint = bool(_COMP_HINT.search(msg)) or score, bool(_ITEM_HINT.search(msg))
    items = [m for m in ms if m["kind"] == "item" and not (comp_hint and not item_hint)]
    comps = [m for m in ms if m["kind"] == "comp" and not (item_hint and not comp_hint)]
    cands = [m for m in ms if m["kind"] == "cand"]
    if cands:   # 라벨이 맞은 점수 후보가 있으면 그 연결만 뗀다(평가 항목 전체가 아니라)
        comps = [m for m in comps if not any(c["component"] == m["name"] and c["cid"] == m["cid"] for c in cands)]
    live = [m for m in items if m["obj"].get("status") != "rejected"]
    items = live or items
    if not (items or comps or cands):
        return None
    why = f"요청: “{msg[:80]}”"
    raws, notes = _raws(ctx, msg, sp, items, comps, cands, why)
    seen, uniq = set(), []
    for r in raws:
        k = tuple(sorted((k, str(v)) for k, v in r.items() if k != "reason"))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    props = [check(ctx, r, by="rules") for r in uniq[:MAX_PROPOSALS]]
    ok = [p for p in props if not p["rejected_reason"]]
    failed = [p for p in props if p["rejected_reason"]]
    if not ok and not failed and not notes:
        return None
    if len(uniq) > MAX_PROPOSALS:
        notes.append(f"맞는 것이 많아 {MAX_PROPOSALS}개만 보여 드렸어요 — 과목 이름이나 날짜를 함께 적으면 좁혀져요")
    if any(m["strength"] == 1 for m in ms):
        notes.append("제목이 똑같은 것은 없어 비슷한 것을 골랐어요 — 맞는지 확인해 주세요")
    return {"answer": _answer(ok, failed, notes), "proposals": ok or failed[:2], "by": "rules"}
