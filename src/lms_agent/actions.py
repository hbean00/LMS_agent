"""에이전트가 하는 클릭(계약 22-9).

화면 단추가 부르는 API를 이름 붙여 묶은 목록(CATALOG)과, 말로 한 명령을 그 목록의 제안으로 바꾸는 규칙(handle),
제안 검사(check), 실행(run)·되돌리기(undo).

- 서버 동작은 **같은 API 경로를 앱 안에서 부른다**(api.router.match → handler(app, body, **경로그룹)) — 검증·저장·
  "changed" 알림(구글 반영)이 화면에서 누른 것과 똑같다. 실행은 늘 사용자가 [실행]을 누를 때만(부르는 쪽이 보장).
- 되돌리기는 실행 전 값을 run()이 돌려준 `undo`에 담아 두었다가 같은 경로로 되돌린다. 되돌릴 수 없는 것은 undoable false.
- 화면 동작(client true: 화면 열기·튜토리얼·캘린더 파일 받기)은 화면이 직접 한다(서버 기록 없음).
- 개인정보·동의가 걸린 것(로그아웃·데이터 지우기·학번·AI 켜기/실행기·구글 연결/해제·토큰)은 목록에 없고 check()가 거절한다.

부르는 쪽(features/agent.py)에게: run()·undo()는 경로 처리기가 스스로 저장·잠금(app.lock, store.locked)을 잡으므로
**app.lock이나 store.locked()를 잡지 않은 채로** 불러야 한다(지금 동기화 동작은 app.do_sync가 app.lock을 잡는다).
결과를 대화에 적는 일만 그 뒤에 잠금 안에서 한다.
"""
from __future__ import annotations

import re

from .api import ApiError, StateContext, router

# ---------------------------------------------------------------- 목록

SETTING_NAMES = {   # POST /api/settings가 받는 켜기/끄기 칸 중 에이전트가 바꿀 수 있는 것
    "include_canvas": "Canvas 과제도 캘린더에 넣기",
    "weekly_summary": "주간 요약 일정",
    "review_reminder": "확인할 후보 알림",
    "notify": "백그라운드 알림(신규 마감·보상)",
    "update_check": "새 버전 자동 확인",
    "auto_update": "켤 때 자동 업데이트",
}
INTERVALS = (1, 3, 6, 12, 24)
MODES = {"normal": "성적 있음", "pass_fail": "P/NP", "hidden": "제외"}
DECISIONS = {"approved": "승인(일정에 넣기)", "rejected": "제외", "pending": "확인 전으로"}
CHOICES = {"auto": "고려대 호이·쿠이(자동)", "original": "오리지널 페이스메이커"}
CARDS = {"briefing": "브리핑 카드", "retro": "회고 카드", "report": "학기 리포트"}
VIEWS = {"dash": "대시보드", "inbox": "확인함", "semester": "학기", "courses": "과목", "grades": "성적",
         "materials": "자료", "settings": "설정"}
PANES = {"account": "계정", "google": "구글 캘린더", "grades": "성적", "tour": "튜토리얼", "update": "업데이트",
         "options": "캘린더에 넣을 것", "bg": "백그라운드 동기화", "autostart": "컴퓨터를 켜면 앱 열기",
         "materials": "첨부 자료", "ai": "AI 도움", "file": "캘린더 파일", "mascot": "캐릭터", "runs": "확인 기록"}
TUTORIALS = {"tour": "튜토리얼 보기", "whatsnew": "달라진 점 보기"}

# 하지 않는 것: id → (이름, 직접 할 곳(설정 칸)). 설정 키도 같은 이유로 막는다
FORBIDDEN = {
    "logout": ("로그아웃", "account"), "wipe_data": ("데이터 지우기", "account"), "student_id": ("학번 넣기·바꾸기", "grades"),
    "ai_enable": ("AI 켜기·끄기", "ai"), "ai_provider": ("AI 실행기 바꾸기", "ai"),
    "google_connect": ("구글 캘린더 연결", "google"), "google_disconnect": ("구글 연결 해제", "google"),
    "token": ("Canvas 토큰·로그인", "account"),
}
FORBIDDEN_SETTINGS = {"ai_cli": "ai_enable", "ai_auto_triage": "ai_enable", "ai_provider": "ai_provider",
                      "ai_custom": "ai_provider", "ai_model": "ai_provider", "student_id": "student_id",
                      "token": "token", "canvas_url": "token"}


def _entry(id_, title, params, route, undoable, client=False):
    return {"id": id_, "title": title, "params": params, "client": client, "route": route, "undoable": undoable}


CATALOG: list[dict] = [
    _entry("setting", "설정 켜기/끄기", {"key": "|".join(SETTING_NAMES), "value": "bool"},
           ("POST", "/api/settings"), True),
    _entry("auto_download", "신규 첨부 자동으로 받기 켜기/끄기", {"value": "bool"}, ("POST", "/api/materials/settings"), True),
    _entry("background", "백그라운드 동기화 켜기/끄기·간격",
           {"enabled": "bool", "interval_hours": "1|3|6|12|24 (선택)"}, ("POST", "/api/background"), True),
    _entry("autostart", "컴퓨터를 켜면 앱 열기 켜기/끄기", {"enabled": "bool", "open_browser": "bool (선택)"},
           ("POST", "/api/autostart"), True),
    _entry("sync", "지금 동기화(전체 다시 확인)", {"full": "bool (선택)"}, ("POST", "/api/sync"), False),
    _entry("google_push", "구글 캘린더에 지금 반영", {}, ("POST", "/api/google/push"), False),
    _entry("course_mode", "과목 종류 바꾸기(성적 있음·P/NP·제외)", {"course_id": "str", "mode": "|".join(MODES)},
           ("POST", "/api/course/{course_id}/mode"), True),
    _entry("item_decision", "항목 승인·제외·확인 전으로", {"item_id": "str", "decision": "|".join(DECISIONS)},
           ("POST", "/api/item/{item_id}"), True),
    _entry("item_submitted", "항목 제출함 표시", {"item_id": "str", "submitted": "bool"}, ("POST", "/api/item/{item_id}"), True),
    _entry("update_check", "새 버전 확인", {}, ("POST", "/api/update/check"), False),
    _entry("update_apply", "지금 업데이트", {}, ("POST", "/api/update/apply"), False),
    _entry("mascot", "캐릭터 바꾸기", {"choice": "|".join(CHOICES)}, ("POST", "/api/mascot/map"), True),
    _entry("materials_folder", "자료(첨부) 폴더 열기", {}, ("POST", "/api/materials/open"), False),
    _entry("app_quit", "앱 끄기", {}, ("POST", "/api/app/quit"), False),
    _entry("coach_dismiss", "코치 카드 닫기", {"kind": "|".join(CARDS) + " (또는 id)"}, ("POST", "/api/coach/dismiss"), False),
    _entry("plan_apply", "공부 계획 캘린더에 넣기",
           {"title": "str", "course_id": "str (선택)", "item_id": "str (선택)", "blocks": "[{start, end, title?}]"},
           ("POST", "/api/plan/apply"), True),
    _entry("plan_remove", "공부 계획 지우기", {"id": "str"}, ("POST", "/api/plan/remove"), False),
    _entry("checklist_toggle", "체크리스트 체크·해제", {"item_id": "str", "id": "str"},
           ("POST", "/api/checklist/{item_id}"), True),
    _entry("open_view", "화면 열기",
           {"view": "|".join(VIEWS), "pane": "|".join(PANES) + " (설정일 때, 선택)",
            "course_id": "str (선택)", "item_id": "str (선택)"}, None, False, client=True),
    _entry("tutorial", "튜토리얼·달라진 점 보기", {"which": "|".join(TUTORIALS)}, None, False, client=True),
    _entry("download_calendar", "캘린더 파일(.ics) 받기", {}, None, False, client=True),
]
BY_ID = {a["id"]: a for a in CATALOG}

_ID = re.compile(r"[\w\-]{1,80}")


class _Reject(ValueError):
    """검사 거절(한국어 이유). ValueError라 경로 처리 중에 새어 나가도 400이 된다."""


# ---------------------------------------------------------------- 지금 상태 읽기(저장된 것만)

def _settings_now(store) -> dict:
    from .features.core import _settings_view
    return _settings_view(store.data.get("settings") or {})


def _view(store) -> list[dict]:
    return store.view()


def _courses(store) -> dict:
    return {str(c.get("id")): c for c in (store.data.get("cache") or {}).get("courses") or [] if c.get("id") is not None}


def _short(c: dict | None, cid=None) -> str:
    from .core import short_course_name
    return short_course_name((c or {}).get("name")) or str(cid or "과목")


def _course_mode(store, cid) -> str:
    from .store import course_mode
    return course_mode(store.data, cid)


def _plans(store) -> list[dict]:
    raw = store.data.get("plans") or []
    if isinstance(raw, dict):
        raw = [{**v, "id": v.get("id") or k} for k, v in raw.items() if isinstance(v, dict)]
    return [p for p in raw if isinstance(p, dict) and isinstance(p.get("id"), str)]


def _mascot_choice(store) -> str:
    c = (store.data.get("mascot") or {}).get("choice")
    return c if c in CHOICES else "auto"


def _md(due) -> str:
    from .timeutil import parse_iso
    d = parse_iso(due) if due else None
    return f"{d.month}/{d.day} {d.hour:02d}:{d.minute:02d}" if d else "마감 없음"


def _item_label(v: dict) -> str:
    return f"{v.get('title')}({_short({'name': v.get('course_name')})} · {_md(v.get('due'))})"


# ---------------------------------------------------------------- 검사

def _bool(params, key, required=True):
    if key not in params or params[key] is None:
        if required:
            raise _Reject(f"{key}(true 또는 false)가 필요해요")
        return None
    v = params[key]
    if not isinstance(v, bool):
        raise _Reject(f"{key}는 true 또는 false여야 해요")
    return v


def _id(params, key, required=True):
    v = params.get(key)
    if v is None or v == "":
        if required:
            raise _Reject(f"{key}가 필요해요")
        return None
    v = str(v) if isinstance(v, int) and not isinstance(v, bool) else v
    if not isinstance(v, str) or not _ID.fullmatch(v):
        raise _Reject(f"{key} 값이 올바르지 않아요")
    return v


def _visible_item(store, item_id) -> dict:
    v = next((x for x in _view(store) if x["id"] == item_id), None)
    if v is None:
        raise _Reject("그 항목을 찾을 수 없어요(동기화 뒤 다시 물어봐 주세요)")
    return v


def _v_setting(store, p):
    key = p.get("key")
    if key in FORBIDDEN_SETTINGS:
        raise _Reject(_forbidden_reason(FORBIDDEN_SETTINGS[key]))
    if key not in SETTING_NAMES:
        raise _Reject("에이전트가 바꿀 수 있는 설정이 아니에요: " + str(key)[:40])
    value = _bool(p, "value")
    if _settings_now(store).get(key) == value:
        raise _Reject(f"{SETTING_NAMES[key]}: 이미 {'켜져' if value else '꺼져'} 있어요")
    return {"key": key, "value": value}


def _v_auto_download(store, p):
    value = _bool(p, "value")
    if bool((store.data.get("settings") or {}).get("auto_download", True)) == value:
        raise _Reject(f"신규 첨부 자동으로 받기: 이미 {'켜져' if value else '꺼져'} 있어요")
    return {"value": value}


def _v_background(store, p):
    out = {"enabled": _bool(p, "enabled")}
    h = p.get("interval_hours")
    if h is not None:
        if isinstance(h, bool) or not isinstance(h, int) or h not in INTERVALS:
            raise _Reject("간격은 " + "·".join(map(str, INTERVALS)) + "시간 중 하나여야 해요")
        if out["enabled"]:
            out["interval_hours"] = h
    return out


def _v_autostart(store, p):
    out = {"enabled": _bool(p, "enabled")}
    ob = _bool(p, "open_browser", required=False)
    if ob is not None:
        out["open_browser"] = ob
    return out


def _v_sync(store, p):
    full = _bool(p, "full", required=False)
    return {"full": True} if full else {}


def _v_none(store, p):
    return {}


def _v_course_mode(store, p):
    cid = _id(p, "course_id")
    mode = p.get("mode")
    if mode not in MODES:
        raise _Reject("mode는 normal(성적 있음)·pass_fail(P/NP)·hidden(제외) 중 하나여야 해요")
    courses = _courses(store)
    if cid not in courses:
        raise _Reject("그 과목을 찾을 수 없어요(동기화 뒤 다시 물어봐 주세요)")
    if _course_mode(store, cid) == mode:
        raise _Reject(f"{_short(courses[cid], cid)}: 이미 '{MODES[mode]}'이에요")
    return {"course_id": cid, "mode": mode}


def _v_item_decision(store, p):
    iid = _id(p, "item_id")
    d = p.get("decision")
    if d not in DECISIONS:
        raise _Reject("decision은 approved(승인)·rejected(제외)·pending(확인 전) 중 하나여야 해요")
    v = _visible_item(store, iid)
    if v["decision"] == d:
        raise _Reject(f"{_item_label(v)}: 이미 '{DECISIONS[d]}' 상태예요")
    return {"item_id": iid, "decision": d}


def _v_item_submitted(store, p):
    iid = _id(p, "item_id")
    s = _bool(p, "submitted")
    v = _visible_item(store, iid)
    if bool(v.get("submitted")) == s:
        raise _Reject(f"{_item_label(v)}: 이미 '{'제출함' if s else '제출 안 함'}'으로 표시돼 있어요")
    return {"item_id": iid, "submitted": s}


def _v_mascot(store, p):
    c = p.get("choice")
    if c not in CHOICES:
        raise _Reject("choice는 auto(고려대 호이·쿠이) 또는 original(오리지널)이어야 해요")
    if _mascot_choice(store) == c:
        raise _Reject(f"캐릭터: 이미 '{CHOICES[c]}'이에요")
    return {"choice": c}


def _v_coach(store, p):
    cid = _id(p, "id", required=False)
    kind = p.get("kind")
    if cid:
        return {"id": cid}
    if kind not in CARDS:
        raise _Reject("닫을 카드(kind: briefing·retro·report 또는 id)가 필요해요")
    return {"kind": kind}


def _v_plan_apply(store, p):
    title = p.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise _Reject("계획 제목(100자 이하)이 필요해요")
    blocks = p.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 30:
        raise _Reject("공부 블록(1~30개)이 필요해요")
    clean = []
    for b in blocks:
        if not isinstance(b, dict) or not isinstance(b.get("start"), str) or not isinstance(b.get("end"), str):
            raise _Reject("블록마다 start·end(시각 문자열)가 있어야 해요")
        clean.append({k: b[k] for k in ("start", "end", "title", "course_id", "item_id") if k in b})
    out = {"title": title.strip(), "blocks": clean}
    for k in ("course_id", "item_id"):
        v = _id(p, k, required=False)
        if v:
            out[k] = v
    return out


def _v_plan_remove(store, p):
    pid = _id(p, "id")
    plans = _plans(store)
    if plans and pid not in {x["id"] for x in plans}:
        raise _Reject("그 공부 계획을 찾을 수 없어요")
    return {"id": pid}


def _v_checklist(store, p):
    iid = _id(p, "item_id")
    _visible_item(store, iid)
    cid = p.get("id")
    if not isinstance(cid, str) or not cid or len(cid) > 80:
        raise _Reject("체크리스트 칸 id가 필요해요")
    return {"item_id": iid, "id": cid}


def _v_open_view(store, p):
    view = p.get("view")
    if view not in VIEWS:
        raise _Reject("view는 " + "·".join(VIEWS) + " 중 하나여야 해요")
    out = {"view": view}
    pane = p.get("pane")
    if pane is not None:
        if view != "settings" or pane not in PANES:
            raise _Reject("pane은 설정 화면의 칸(" + "·".join(PANES) + ") 중 하나여야 해요")
        out["pane"] = pane
    cid = _id(p, "course_id", required=False)
    if cid:
        if cid not in _courses(store):
            raise _Reject("그 과목을 찾을 수 없어요")
        out["course_id"] = cid
    iid = _id(p, "item_id", required=False)
    if iid:
        _visible_item(store, iid)
        out["item_id"] = iid
    return out


def _v_tutorial(store, p):
    w = p.get("which") or "tour"
    if w not in TUTORIALS:
        raise _Reject("which는 tour(튜토리얼) 또는 whatsnew(달라진 점)여야 해요")
    return {"which": w}


_VALIDATE = {
    "setting": _v_setting, "auto_download": _v_auto_download, "background": _v_background, "autostart": _v_autostart,
    "sync": _v_sync, "google_push": _v_none, "course_mode": _v_course_mode, "item_decision": _v_item_decision,
    "item_submitted": _v_item_submitted, "update_check": _v_none, "update_apply": _v_none, "mascot": _v_mascot,
    "materials_folder": _v_none, "app_quit": _v_none, "coach_dismiss": _v_coach, "plan_apply": _v_plan_apply, "plan_remove": _v_plan_remove,
    "checklist_toggle": _v_checklist, "open_view": _v_open_view, "tutorial": _v_tutorial, "download_calendar": _v_none,
}


def _forbidden_reason(fid: str) -> str:
    name, pane = FORBIDDEN.get(fid, ("그 일", "account"))
    return (f"‘{name}’ — 개인정보·동의가 걸린 일이라 에이전트가 하지 않아요. "
            f"설정 → {PANES.get(pane, '설정')}에서 직접 해 주세요.")


def title_of(store, aid: str, params: dict) -> str:
    """제안 단추에 보일 제목(한국어). 검사를 통과한 params 기준."""
    p = params or {}
    on = "켜기"
    off = "끄기"
    if aid == "setting":
        return f"{SETTING_NAMES.get(p.get('key'), '설정')} {on if p.get('value') else off}"
    if aid == "auto_download":
        return f"신규 첨부 자동으로 받기 {on if p.get('value') else off}"
    if aid == "background":
        if not p.get("enabled"):
            return "백그라운드 동기화 끄기"
        return "백그라운드 동기화 켜기" + (f"({p['interval_hours']}시간마다)" if p.get("interval_hours") else "")
    if aid == "autostart":
        if not p.get("enabled"):
            return "컴퓨터를 켜면 앱 열기 끄기"
        return "컴퓨터를 켜면 앱 열기 켜기" + (" (브라우저는 열지 않음)" if p.get("open_browser") is False else "")
    if aid == "sync":
        return "전체 다시 확인" if p.get("full") else "지금 동기화"
    if aid == "course_mode":
        c = _courses(store).get(str(p.get("course_id")))
        return f"{_short(c, p.get('course_id'))} 과목 종류 → {MODES.get(p.get('mode'), '?')}"
    if aid in ("item_decision", "item_submitted", "checklist_toggle") or (aid == "open_view" and p.get("item_id")):
        v = next((x for x in _view(store) if x["id"] == p.get("item_id")), None)
        label = _item_label(v) if v else "항목"
        if aid == "item_decision":
            return f"{label} {DECISIONS.get(p.get('decision'), '?')}"
        if aid == "item_submitted":
            return f"{label} {'제출함' if p.get('submitted') else '제출 안 함'}으로 표시"
        if aid == "checklist_toggle":
            return f"{label} 체크리스트 칸 체크·해제"
        return f"{label} 보기"
    if aid == "mascot":
        return f"캐릭터 → {CHOICES.get(p.get('choice'), '?')}"
    if aid == "coach_dismiss":
        return f"{CARDS.get(p.get('kind'), '코치 카드')} 닫기"
    if aid == "plan_apply":
        return f"공부 계획 '{p.get('title')}' 캘린더에 넣기({len(p.get('blocks') or [])}칸)"
    if aid == "plan_remove":
        plan = next((x for x in _plans(store) if x["id"] == p.get("id")), None)
        return f"공부 계획 '{(plan or {}).get('title') or p.get('id')}' 지우기"
    if aid == "open_view":
        if p.get("pane"):
            return f"설정 → {PANES[p['pane']]} 열기"
        if p.get("course_id"):
            c = _courses(store).get(str(p["course_id"]))
            return f"{_short(c, p['course_id'])} {VIEWS[p['view']] if p['view'] != 'courses' else '과목'} 화면 열기"
        return f"{VIEWS.get(p.get('view'), '')} 화면 열기"
    if aid == "tutorial":
        return TUTORIALS.get(p.get("which"), "튜토리얼 보기")
    a = BY_ID.get(aid)
    return a["title"] if a else str(aid)


def check(store, p: dict) -> dict:
    """제안 검사: id·params를 목록과 지금 상태(저장된 것)에 맞춰 본다. 결과는 p에 칸을 더한 사본 —
    type "action", 정리한 params, 제목(목록 기준으로 다시 씀), client, undoable, rejected_reason(통과면 None).
    모르는 것·금지 목록·이미 그 상태인 것은 rejected_reason(한국어)."""
    out = dict(p) if isinstance(p, dict) else {}
    out["type"] = "action"
    aid = out.get("action")
    params = out.get("params")
    if params is None:
        params = {}
    out["rejected_reason"] = None
    if isinstance(out.get("reason"), str):
        out["reason"] = out["reason"][:300]
    a = BY_ID.get(aid) if isinstance(aid, str) else None
    out["client"] = bool(a and a["client"])
    out["undoable"] = bool(a and a["undoable"])
    if not isinstance(aid, str) or not aid:
        out.update(title="알 수 없는 동작", rejected_reason="어떤 동작인지(action)가 없어요")
        return out
    if aid in FORBIDDEN:
        out.update(title=FORBIDDEN[aid][0], rejected_reason=_forbidden_reason(aid))
        return out
    if a is None:
        out.update(title=str(aid)[:40], rejected_reason=f"에이전트가 할 수 있는 동작이 아니에요({str(aid)[:40]})")
        return out
    if not isinstance(params, dict):
        out.update(title=a["title"], rejected_reason="params는 객체여야 해요")
        return out
    try:
        clean = _VALIDATE[aid](store, params)
    except _Reject as e:
        out.update(title=a["title"], rejected_reason=str(e))
        return out
    out["params"] = clean
    out["title"] = title_of(store, aid, clean)
    return out


def catalog_text() -> str:
    """AI에게 보낼 목록 설명(2,000자 이내)."""
    lines = ['할 수 있는 동작: proposals에 {"type":"action","action":id,"params":{…},"reason":…}. 사용자가 [실행]을 눌러야 실행됨.']
    for a in CATALOG:
        shape = ", ".join(f"{k}: {v}" for k, v in a["params"].items())
        lines.append(f"- {a['id']}{' (화면)' if a['client'] else ''} {{{shape}}} {a['title']}")
    lines.append("setting key: " + ", ".join(f"{k}={v}" for k, v in SETTING_NAMES.items()))
    lines.append("하지 않음(제안 금지): " + ", ".join(v[0] for v in FORBIDDEN.values()) + " → open_view로 그 설정 칸만 열기.")
    return "\n".join(lines)


# ---------------------------------------------------------------- 명령 알아듣기(규칙)

_REQUEST = re.compile(r"줘|주세요|주실|줄래|줄\s*수|주라|주면|해\s*봐|하자|해라|부탁|할래|해\s*줄|please", re.I)
_BARE = re.compile(r"(?:켜|꺼|끄기|켜기|열어|열기|바꿔|바꾸기|닫아|닫기|지워|지우기|승인|제외|동기화|반영|업데이트|확인|"
                   r"보여|받기|다운로드|넣어|빼|해제|등록|표시|로그아웃|초기화|지\s*마|(?<![a-z])on|(?<![a-z])off)"
                   r"\s*(?:해|하기|요|해요|세요)?\s*[.!~]*$", re.I)
_QUESTION = re.compile(r"\?|어디|언제|몇\s*(?:점|시|개|일|번)|왜|어떻게|무엇|뭐(?:야|예요|지|가|를)?|얼마|인가요|나요|까요|"
                       r"을까|ㄹ까|있어$|있나|되나|맞아|어때", re.I)
_MISSING = re.compile(r"안\s*(?:보|들어|나와|돼|되|떠|떴|맞)|빠졌|없어요|없네|놓친|못\s*찾|이상해|틀려|문제가", re.I)
_OTHER_BRANCH = re.compile(r"메일|이메일|e-?mail|초안|몇\s*점|받으려면|받아야|만약|라면|이면\s*[?]?$|0점", re.I)
_PLAN = re.compile(r"공부|계획|스케줄")
_REMOVE = re.compile(r"지워|지우|삭제|없애|취소|빼")

_OFF = re.compile(r"꺼|끄|끈|해제|중지|중단|멈춰|멈추|그만|없애|빼|안\s*(?:하|되|넣|보내|열|받|쓰|띄)|지\s*마|말아|"
                  r"disable|turn\s*off|(?<![a-z])off(?![a-z])", re.I)
_B_OFF = re.compile(r"브라우저\S*\s*(?:창\S*\s*)?(?:안\s*(?:열|띄|켜)|열지\s*(?:말|않)|띄우지\s*(?:말|않)|없이|빼|말고|필요\s*없)\S*")
_B_ON = re.compile(r"브라우저\S*\s*(?:창\S*\s*)?(?:같이\s*|함께\s*)?(?:열|띄|켜)\S*")

_F_LOGOUT = re.compile(r"로그아웃|log\s*out|sign\s*out", re.I)
_F_WIPE = re.compile(r"(?:데이터|기록)\s*(?:를|을)?\s*(?:전부|모두|다|싹)?\s*(?:지워|지우|삭제|초기화|리셋)|"
                     r"모든\s*(?:데이터|기록)|초기화|리셋|(?<![a-z])reset", re.I)
_F_SID = re.compile(r"학번")
_F_AI = re.compile(r"(?<![a-z])ai(?![a-z])|에이아이|인공지능|클로드|claude|codex|코덱스|gemini|제미나이|ollama|copilot|opencode",
                   re.I)
_F_AI_VERB = re.compile(r"켜|꺼|끄|바꿔|바꾸|변경|교체|사용|쓰게|쓰자|연결|설정해|enable|disable", re.I)
_F_AI_PROVIDER = re.compile(r"실행기|provider|바꿔|바꾸|변경|교체|다른|codex|코덱스|gemini|제미나이|ollama|copilot|opencode|클로드|claude",
                            re.I)
_SCREEN = re.compile(r"설정|화면|탭|페이지|창")
_F_TOKEN = re.compile(r"토큰|token|비밀\s*번호|password|다시\s*로그인|재로그인|canvas\s*(?:주소|연결)", re.I)
_F_GOOGLE = re.compile(r"구글|google", re.I)
_F_G_OFF = re.compile(r"연결\s*(?:을\s*)?(?:해제|끊)|해제|끊어|끊기|로그아웃|disconnect", re.I)
_F_G_ON = re.compile(r"연결|로그인|계정|connect", re.I)

_QUIT = re.compile(r"(?:앱|프로그램|lms\s*agent|에이전트\s*앱|서버)\s*(?:을|를)?\s*(?:좀\s*)?(?:꺼|끄기|끄자|종료|닫아|닫기)|"
                   r"(?:quit|exit)\s*(?:the\s*)?app", re.I)
_AUTO_UPDATE = re.compile(r"자동\s*(?:으로\s*)?업데이트|켤\s*때\s*(?:자동\s*)?업데이트|auto\s*update", re.I)
_AUTOSTART = re.compile(r"자동\s*(?:실행|시작|켜기|켜짐|열기)|시작\s*프로그램|부팅|(?:컴퓨터|pc|노트북)\s*(?:를|가|을)?\s*(?:켜|켤|킬|키면|부팅)|"
                        r"로그인\s*(?:하면|할\s*때|시)|로그온|(?:윈도우|windows|맥|mac)\s*(?:를|가|이)?\s*(?:시작|켜|켤|부팅)|"
                        r"켤\s*때|켜면\s*(?:바로\s*)?(?:앱|lms|실행|열)|"
                        r"(?:앱|프로그램|lms\s*agent)\s*(?:을|를)?\s*자동으로|autostart|start\s*up|startup", re.I)
_UPDATE_SETTING = re.compile(r"(?:새\s*버전|업데이트)\s*(?:자동\s*)?(?:확인|알림)", re.I)
_EXPLICIT_TOGGLE = re.compile(r"켜|꺼|끄|끈|그만|안\s*(?:하|받)|하지\s*마|말아|해제", re.I)
_REVIEW = re.compile(r"후보\s*알림|확인\s*알림|검토\s*알림|review\s*reminder", re.I)
_WEEKLY = re.compile(r"주간\s*요약|요약\s*일정|weekly\s*summary", re.I)
_CANVAS = re.compile(r"(?:canvas|캔버스)\s*(?:의\s*)?(?:과제|마감|일정)", re.I)
_NOTIFY = re.compile(r"알림|토스트|notification", re.I)
_MAT_FOLDER = re.compile(r"(?:자료|첨부|다운로드|받은\s*파일)\s*(?:저장\s*)?폴더", re.I)
_AUTO_DL = re.compile(r"첨부|자동\s*(?:다운|저장|받)", re.I)
_BACKGROUND = re.compile(r"백그라운드|자동\s*동기화|주기적|정기적|\d+\s*시간\s*(?:마다|간격|에\s*한\s*번)", re.I)
_HOURS = re.compile(r"(\d+)\s*시간")
_GPUSH = re.compile(r"(?:구글|google)\s*(?:캘린더|calendar)?\s*(?:에|으로|로)?\s*(?:지금\s*)?(?:다시\s*)?"
                    r"(?:반영|동기화|올려|보내|푸시|push|업데이트|맞춰)|캘린더\s*(?:에|로)?\s*(?:지금\s*)?(?:반영|올려)", re.I)
_UPDATE = re.compile(r"업데이트|새\s*버전|최신\s*버전|update", re.I)
_UPDATE_CHECK = re.compile(r"확인|있(?:는지|나|어|으면)|나왔|체크", re.I)
_SYNC = re.compile(r"동기화|sync|다시\s*(?:읽|확인)|전체\s*(?:다시\s*)?확인|새로\s*고침|새로고침|갱신", re.I)
_FULL = re.compile(r"전체|처음부터|모두\s*다시|싹\s*다|전부|full", re.I)
_MASCOT = re.compile(r"캐릭터|마스코트|호이|쿠이|페이스메이커", re.I)
_M_ORIGINAL = re.compile(r"오리지널|원래|기본|자체|페이스메이커|안\s*(?:쓰|보)|꺼|끄|빼", re.I)
_M_AUTO = re.compile(r"호이|쿠이|고려대|ku\b|자동", re.I)
_COACH = re.compile(r"브리핑|작전\s*카드|회고|학기\s*리포트|종강\s*리포트", re.I)
_CLOSE = re.compile(r"닫|치워|숨겨|그만|안\s*보|지워|없애|빼", re.I)
_ICS = re.compile(r"(?:캘린더|달력|ics)\s*(?:파일)?\s*(?:을|를)?\s*(?:받|다운|내보내|저장)|\.ics", re.I)
_TOUR = re.compile(r"튜토리얼|사용법|둘러보기|투어|tutorial", re.I)
_WHATSNEW = re.compile(r"달라진\s*점|바뀐\s*점|새\s*기능|what'?s\s*new", re.I)
_CHECKLIST = re.compile(r"체크\s*리스트|checklist", re.I)
_PNP = re.compile(r"p\s*/?\s*np|패논패|패스\s*/?\s*논패스|pass\s*/?\s*(?:fail|no\s*pass)|패스\s*과목|합불", re.I)
_HIDE = re.compile(r"제외|숨겨|숨기|빼|안\s*보이|가려|감춰", re.I)
_NORMAL = re.compile(r"성적\s*(?:있|과목|으로|매기)|일반\s*과목|원래\s*(?:대로)?|되돌려|다시\s*(?:보이|넣|포함)|복원|학점\s*과목|normal", re.I)
_APPROVE = re.compile(r"승인|(?:일정|캘린더)에\s*(?:넣|추가)|확정|추가해", re.I)
_REJECT = re.compile(r"제외|빼|거절|무시|버려|삭제|지워", re.I)
_PENDING = re.compile(r"확인\s*전|보류|대기|확인함으로|미정|승인\s*취소", re.I)
_SUB_OFF = re.compile(r"제출\s*(?:안|못)\s*했|미제출|제출\s*(?:취소|안\s*함)|아직\s*(?:안|못)\s*(?:냈|제출)", re.I)
_SUB_ON = re.compile(r"제출\s*(?:했|함|완료|끝)|냈어|냈음", re.I)
_OPEN = re.compile(r"열어|열기|열자|보여|띄워|가\s*줘|가자|이동|들어가|볼래|보자|켜\s*줘", re.I)
_VIEW_WORDS = [
    ("settings", re.compile(r"설정|환경\s*설정|옵션|setting", re.I)),
    ("inbox", re.compile(r"확인함|인박스|inbox|확인할\s*(?:것|후보)|후보\s*목록", re.I)),
    ("grades", re.compile(r"성적|학점|점수|grade", re.I)),
    ("materials", re.compile(r"자료|첨부\s*(?:목록|화면)|받은\s*파일", re.I)),
    ("semester", re.compile(r"학기|주차|타임라인", re.I)),
    ("courses", re.compile(r"과목", re.I)),
    ("dash", re.compile(r"대시\s*보드|홈|메인|첫\s*화면|dashboard", re.I)),
]
_PANE_WORDS = [
    ("google", _F_GOOGLE), ("bg", re.compile(r"백그라운드", re.I)), ("autostart", _AUTOSTART),
    ("update", re.compile(r"업데이트|버전", re.I)), ("mascot", _MASCOT), ("ai", _F_AI),
    ("account", re.compile(r"계정|로그인|토큰", re.I)), ("materials", re.compile(r"첨부", re.I)),
    ("file", re.compile(r"캘린더\s*파일|ics", re.I)), ("runs", re.compile(r"확인\s*기록|실행\s*기록", re.I)),
    ("tour", _TOUR), ("options", re.compile(r"캘린더에\s*넣을", re.I)),
]
MAX_PROPOSALS = 3


def _norm(s) -> str:
    return re.sub(r"[\s\-_·.,:()\[\]'\"]+", "", str(s or "").lower())


def _is_command(msg: str) -> bool:
    if _REQUEST.search(msg):
        return True
    return bool(_BARE.search(msg)) and not _QUESTION.search(msg)


def _toggle(text: str) -> bool:
    """켜기(True)/끄기(False). 끄는 말이 있으면 끄기."""
    return not _OFF.search(text)


def _browser(text: str) -> tuple[bool | None, str]:
    """'브라우저는 열지 말고' 같은 말 → (open_browser, 그 말을 뺀 글)."""
    m = _B_OFF.search(text)
    if m:
        return False, text[:m.start()] + " " + text[m.end():]
    m = _B_ON.search(text)
    if m:
        return True, text[:m.start()] + " " + text[m.end():]
    return None, text


def _items_in(store, msg: str, course_ids: list[str] | None) -> list[dict]:
    """메시지에 제목이 나온 항목(번호 붙은 제목 낱말이 같거나, 제목이 그대로 들어 있거나, 번호 없는 제목 낱말이 모두 있음)."""
    from .extract import title_tokens
    mt = title_tokens(msg)
    m_num = {t for t in mt if t[1] is not None}
    m_terms = {k for k, n in mt if n is None}
    flat = _norm(msg)
    out = []
    for v in _view(store):
        if course_ids and str(v.get("course_id")) not in course_ids:
            continue
        tt = title_tokens(v.get("title"))
        nums = {t for t in tt if t[1] is not None}
        terms = {k for k, n in tt if n is None}
        t = _norm(v.get("title"))
        if (nums & m_num) or (len(t) >= 3 and t in flat) or (not nums and terms and terms <= m_terms):
            out.append(v)
    return out


_MD = re.compile(r"(\d{1,2})\s*(?:/|월\s*)(\d{1,2})")


def _by_date(hits: list[dict], low: str) -> list[dict]:
    """'LAB4 11/19 마감 승인' — 같은 제목 후보가 여럿이면 말한 날짜(월/일)의 것만(맞는 것이 없으면 그대로)."""
    from .timeutil import parse_iso
    days = {(int(m), int(d)) for m, d in _MD.findall(low)}
    if len(hits) < 2 or not days:
        return hits
    same = []
    for v in hits:
        d = parse_iso(v["due"]) if v.get("due") else None
        if d and (d.month, d.day) in days:
            same.append(v)
    return same or hits


def _mentioned(store, msg: str) -> list[str]:
    from .agent import mentioned_courses
    return mentioned_courses(msg, list(_courses(store).values()))


def _p(action: str, params: dict, reason: str) -> dict:
    return {"type": "action", "action": action, "params": params, "reason": reason}


def _forbidden(low: str) -> str | None:
    if _F_LOGOUT.search(low):
        return "logout"
    if _F_GOOGLE.search(low) and _F_G_OFF.search(low):
        return "google_disconnect"
    if _F_WIPE.search(low):
        return "wipe_data"
    if _F_SID.search(low):
        return "student_id"
    if _F_TOKEN.search(low):
        return "token"
    if _F_AI.search(low) and _F_AI_VERB.search(low) and not _F_GOOGLE.search(low):
        return "ai_provider" if _F_AI_PROVIDER.search(low) else "ai_enable"
    if _F_GOOGLE.search(low) and _F_G_ON.search(low) and not _GPUSH.search(low):
        return "google_connect"
    return None


def _item_view(v: dict) -> dict:
    return {"view": "inbox" if v["status"] == "review" else "dash", "item_id": v["id"]}


def _open_intent(store, msg: str, low: str, course_id, why: str) -> list[dict]:
    """화면 열기(화면이 직접 함): 특정 항목·과목, 설정의 칸, 화면 이름."""
    if not _OPEN.search(low):
        return []
    settings_word = _VIEW_WORDS[0][1].search(low)
    if not settings_word:
        named = _mentioned(store, msg)
        grades = re.search(r"성적|학점|점수", low)
        hits = [] if grades else _items_in(store, msg, named or None)
        if hits:
            return [_p("open_view", _item_view(v), why) for v in hits[:MAX_PROPOSALS]]
        if named:
            view = "grades" if grades else "courses"
            return [_p("open_view", {"view": view, "course_id": cid}, why) for cid in named[:MAX_PROPOSALS]]
    for view, rx in _VIEW_WORDS:
        if rx.search(low):
            params = {"view": view}
            if view == "settings":
                pane = next((name for name, prx in _PANE_WORDS if prx.search(low)), None)
                if pane:
                    params["pane"] = pane
            elif view == "courses" and course_id and re.search(r"이\s*과목|여기", low):
                params["course_id"] = str(course_id)
            return [_p("open_view", params, why)]
    # 화면 이름 없이 기능 이름만 있는 '…화면 열어줘'(예: 백그라운드 동기화 화면) → 설정의 그 칸
    pane = next((name for name, prx in _PANE_WORDS if prx.search(low)), None)
    if pane and _SCREEN.search(low):
        return [_p("open_view", {"view": "settings", "pane": pane}, why)]
    return []


def _intent(store, msg: str, low: str, course_id) -> tuple[list[dict], str | None, str | None]:
    """(제안 목록, 앞에 둘 말, 뒤에 덧붙일 말). 알아듣지 못하면 ([], None, None)."""
    why = f"요청: “{msg[:80]}”"

    if _SCREEN.search(low):                     # '백그라운드 설정 열어줘' = 켜기가 아니라 그 칸 열기
        opened = _open_intent(store, msg, low, course_id, why)
        if opened:
            return opened, None, None

    fid = _forbidden(low)
    if fid:
        return [_p("open_view", {"view": "settings", "pane": FORBIDDEN[fid][1]}, why)], _forbidden_reason(fid), None

    if _AUTO_UPDATE.search(low):
        return [_p("setting", {"key": "auto_update", "value": _toggle(low)}, why)], None, None

    if _QUIT.search(low) and not _AUTOSTART.search(low):   # '앱 꺼줘'(자동 실행 끄기와 다름)
        return [_p("app_quit", {}, why)], None, "끈 뒤에는 이 화면이 연결되지 않아요. 다시 열려면 LMS Agent를 실행하세요."

    if _AUTOSTART.search(low):
        ob, rest = _browser(low)
        params = {"enabled": _toggle(rest)}
        if ob is not None and params["enabled"]:
            params["open_browser"] = ob
        return [_p("autostart", params, why)], None, None

    if _MAT_FOLDER.search(low) and _OPEN.search(low):
        return [_p("materials_folder", {}, why)], None, None
    if _UPDATE_SETTING.search(low) and _EXPLICIT_TOGGLE.search(low):
        return [_p("setting", {"key": "update_check", "value": _toggle(low)}, why)], None, None
    for key, rx in (("review_reminder", _REVIEW), ("weekly_summary", _WEEKLY), ("include_canvas", _CANVAS)):
        if rx.search(low):
            return [_p("setting", {"key": key, "value": _toggle(low)}, why)], None, None
    if _NOTIFY.search(low) and not _UPDATE.search(low):
        return [_p("setting", {"key": "notify", "value": _toggle(low)}, why)], None, None
    if _AUTO_DL.search(low) and re.search(r"받|다운|저장", low) and not _OPEN.search(low):
        return [_p("auto_download", {"value": _toggle(low)}, why)], None, None

    if _BACKGROUND.search(low):
        params = {"enabled": _toggle(low)}
        m = _HOURS.search(low)
        if m and params["enabled"]:
            params["interval_hours"] = int(m.group(1))
        return [_p("background", params, why)], None, None

    if _GPUSH.search(low):
        return [_p("google_push", {}, why)], None, "구글 캘린더가 연결돼 있어야 해요(설정 → 구글 캘린더)."

    if _UPDATE.search(low) and not _SYNC.search(low):
        if _UPDATE_CHECK.search(low):
            return [_p("update_check", {}, why)], None, None
        from . import __version__
        from .updater import is_newer
        latest = (store.data.get("update") or {}).get("latest")
        if is_newer(latest, __version__):
            return ([_p("update_apply", {}, why)], None,
                    f"v{latest}이(가) 나와 있어요(지금 v{__version__}). 바꾸기 전에 데이터를 백업해요.")
        return [_p("update_check", {}, why)], "새 버전이 있는지 먼저 확인할게요.", None

    if _SYNC.search(low):
        return [_p("sync", {"full": True} if _FULL.search(low) else {}, why)], None, None

    if _MASCOT.search(low):
        if _M_AUTO.search(low) and not re.search(r"오리지널|안\s*(?:쓰|보)|꺼|끄|빼", low):
            choice = "auto"
        elif _M_ORIGINAL.search(low):
            choice = "original"
        else:
            choice = "original" if _mascot_choice(store) == "auto" else "auto"
        return [_p("mascot", {"choice": choice}, why)], None, None

    if _COACH.search(low):
        kind = "retro" if "회고" in low else "report" if "리포트" in low else "briefing"
        if _CLOSE.search(low):
            return [_p("coach_dismiss", {"kind": kind}, why)], None, None
        return [_p("open_view", {"view": "dash"}, why)], None, None

    if _PLAN.search(low) and _REMOVE.search(low):
        plans = _plans(store)
        if not plans:
            return [], "지울 공부 계획이 없어요.", None
        cids = _mentioned(store, msg)
        mine = [x for x in plans if not cids or str(x.get("course_id")) in cids] or plans
        return [_p("plan_remove", {"id": x["id"]}, why) for x in mine[:MAX_PROPOSALS]], None, None

    if _ICS.search(low):
        return [_p("download_calendar", {}, why)], None, None
    if _WHATSNEW.search(low) and _OPEN.search(low):
        return [_p("tutorial", {"which": "whatsnew"}, why)], None, None
    if _TOUR.search(low):
        return [_p("tutorial", {"which": "tour"}, why)], None, None

    # 항목: 제목이 나오면 승인·제외·확인 전·제출 표시(보기는 아래 화면 열기)
    named = _mentioned(store, msg)
    hits = _items_in(store, msg, named or None)
    if not named and course_id:                     # 과목 화면에서 물었으면 그 과목 항목부터
        hits = [v for v in hits if str(v.get("course_id")) == str(course_id)] or hits
    hits = _by_date(hits, low)
    if hits:
        if _CHECKLIST.search(low):
            return ([_p("open_view", _item_view(v), why) for v in hits[:MAX_PROPOSALS]], None,
                    "체크리스트는 항목 카드에서 칸마다 체크할 수 있어요.")
        want = (("submitted", False) if _SUB_OFF.search(low) else ("submitted", True) if _SUB_ON.search(low)
                else ("decision", "pending") if _PENDING.search(low) else ("decision", "approved") if _APPROVE.search(low)
                else ("decision", "rejected") if _REJECT.search(low) else None)
        if want:
            key, val = want
            todo = [v for v in hits if (bool(v.get("submitted")) if key == "submitted" else v["decision"]) != val] or hits
            if key == "decision" and val == "approved":   # 확인 대기 후보부터
                todo.sort(key=lambda v: v["status"] != "review")
            action = "item_submitted" if key == "submitted" else "item_decision"
            return [_p(action, {"item_id": v["id"], key: val}, why) for v in todo[:MAX_PROPOSALS]], None, None

    # 과목 종류
    targets = named or ([str(course_id)] if course_id and re.search(r"이\s*과목|여기", low) else [])
    if targets:
        mode = ("pass_fail" if _PNP.search(low) else "normal" if _NORMAL.search(low)
                else "hidden" if _HIDE.search(low) else None)
        if mode:
            return [_p("course_mode", {"course_id": cid, "mode": mode}, why) for cid in targets[:MAX_PROPOSALS]], None, None

    return _open_intent(store, msg, low, course_id, why), None, None


def handle(store, message: str, course_id=None) -> dict | None:
    """명령이 아니면 None. 결과 {"answer", "proposals": [액션 제안(check를 거친 것)]}(계약 22-9).
    "기말 범위 어디까지야?" 같은 질문, "기말 날짜가 안 보여요" 같은 놓친 것 알림, 메일·계획·만약에는 None(다른 갈래)."""
    msg = (message or "").strip()
    if not msg or len(msg) > 500 or not _is_command(msg):
        return None
    low = msg.lower()
    if _MISSING.search(low) and not _REQUEST.search(low):
        return None
    if _OTHER_BRANCH.search(low):
        return None
    if _PLAN.search(low) and not _REMOVE.search(low):
        return None
    raw, lead, note = _intent(store, msg, low, course_id)
    if not raw and not lead:
        return None
    props = [check(store, p) for p in raw]
    return {"answer": _answer(props, lead, note), "proposals": props}


def _answer(props: list[dict], lead: str | None, note: str | None) -> str:
    ok = [p for p in props if not p["rejected_reason"]]
    parts = [lead if lead.endswith(".") else lead + "."] if lead else []
    if len(ok) == 1:
        p = ok[0]
        tail = ("화면에서 바로 열어요" if p["client"] else
                "나중에 [되돌리기]로 되돌릴 수 있어요" if p["undoable"] else "이 동작은 되돌릴 수 없어요")
        parts.append(f"‘{p['title']}’ — [실행]을 누르면 할게요. {tail}.")
    elif ok:
        parts.append("어느 것인지 골라 [실행]을 눌러 주세요: " + " / ".join(f"‘{p['title']}’" for p in ok))
    elif props:
        parts.append(props[0]["rejected_reason"])
    if note and ok:
        parts.append(note)
    return " ".join(parts)


# ---------------------------------------------------------------- 실행·되돌리기

def _call(app, path: str, body: dict):
    """화면이 부르는 것과 같은 경로 처리기를 앱 안에서 부른다(None이면 다음 후보 — 예: /api/settings의 첨부 설정 통과)."""
    for route, groups in router.match("POST", path):
        res = route.handler(app, dict(body), **groups)
        if res is not None:
            return res
    raise ApiError(404, "이 기능은 이 버전의 앱에 아직 없어요")


def _section(app, key: str):
    ctx = StateContext(app)
    for k, fn in router.sections:
        if k == key:
            return fn(app, ctx)
    return None


def _done(message: str, undo=None) -> dict:
    return {"ok": True, "message": message, "undo": undo}


def _onoff(v: bool) -> str:
    return "켰어요" if v else "껐어요"


def _r_setting(app, p):
    prev = _settings_now(app.store)[p["key"]]
    _call(app, "/api/settings", {p["key"]: p["value"]})
    return _done(f"{SETTING_NAMES[p['key']]}: {_onoff(p['value'])}", {"key": p["key"], "value": prev, "set": p["value"]})


def _r_auto_download(app, p):
    prev = bool((app.store.data.get("settings") or {}).get("auto_download", True))
    _call(app, "/api/materials/settings", {"auto_download": p["value"]})
    return _done(f"신규 첨부 자동으로 받기: {_onoff(p['value'])}", {"value": prev, "set": p["value"]})


def _r_background(app, p):
    st = _section(app, "background") or {}
    prev = {"enabled": bool(st.get("enabled")), "interval_hours": st.get("interval_hours")}
    same = prev["enabled"] == p["enabled"] and (not p["enabled"] or p.get("interval_hours") in (None, prev["interval_hours"]))
    if st.get("supported") and same:
        return _done("백그라운드 동기화: 이미 " + (f"켜져 있어요({prev['interval_hours']}시간마다)" if p["enabled"] else "꺼져 있어요"),
                     prev)
    _call(app, "/api/background", p)
    after = _section(app, "background") or {}
    msg = (f"백그라운드 동기화를 켰어요({after.get('interval_hours')}시간마다)" if p["enabled"] else "백그라운드 동기화를 껐어요")
    return _done(msg, prev)


def _r_autostart(app, p):
    st = _section(app, "autostart") or {}
    prev = {"enabled": bool(st.get("enabled")), "open_browser": st.get("open_browser", True)}
    _call(app, "/api/autostart", p)
    if p["enabled"]:
        ob = p.get("open_browser", prev["open_browser"])
        msg = "컴퓨터를 켜면 앱이 열리게 했어요" + ("" if ob else "(브라우저는 열지 않음)") + ". 다음 로그인부터 적용돼요"
    else:
        msg = "컴퓨터를 켜도 앱이 저절로 열리지 않게 했어요"
    return _done(msg, prev)


def _r_sync(app, p):
    res = _call(app, "/api/sync", p)
    rep = (res or {}).get("report") or {}
    head = "전체를 다시 확인했어요" if p.get("full") else "동기화했어요"
    return _done(f"{head}(확인할 후보 {rep.get('needs_review', 0)}개)")


def _r_google_push(app, p):
    g = getattr(app, "google", None)
    if not (g and g.available and g.connected):
        raise ApiError(400, "구글 캘린더가 연결돼 있지 않아요. 설정 → 구글 캘린더에서 먼저 연결해 주세요")
    res = _call(app, "/api/google/push", {})
    r = ((res or {}).get("google") or {}).get("last_result") or {}
    return _done(f"구글 캘린더에 반영했어요(추가 {r.get('created') or 0} · 바뀜 {r.get('updated') or 0} · "
                 f"지움 {r.get('deleted') or 0})")


def _r_course_mode(app, p):
    prev = _course_mode(app.store, p["course_id"])
    _call(app, f"/api/course/{p['course_id']}/mode", {"mode": p["mode"]})
    c = _courses(app.store).get(p["course_id"])
    return _done(f"{_short(c, p['course_id'])}: 과목 종류를 '{MODES[p['mode']]}'(으)로 바꿨어요",
                 {"course_id": p["course_id"], "mode": prev, "set": p["mode"]})


def _r_item_decision(app, p):
    v = _visible_item(app.store, p["item_id"])
    res = _call(app, f"/api/item/{p['item_id']}", {"decision": p["decision"]})
    replaced = [r for r in (res or {}).get("replaced") or [] if isinstance(r, str)]
    msg = f"{_item_label(v)}: {DECISIONS[p['decision']]}" + (f"(같은 과제의 다른 후보 {len(replaced)}개는 확인 전으로)" if replaced else "")
    return _done(msg, {"item_id": p["item_id"], "decision": v["decision"], "set": p["decision"], "replaced": replaced})


def _r_item_submitted(app, p):
    v = _visible_item(app.store, p["item_id"])
    prev = (app.store.data["user"].get(p["item_id"]) or {}).get("submitted")
    _call(app, f"/api/item/{p['item_id']}", {"submitted": p["submitted"]})
    return _done(f"{_item_label(v)}: {'제출함' if p['submitted'] else '제출 안 함'}으로 표시했어요",
                 {"item_id": p["item_id"], "submitted": prev, "set": p["submitted"]})


def _r_update_check(app, p):
    res = _call(app, "/api/update/check", {})
    u = (res or {}).get("update") or {}
    if u.get("available"):
        return _done(f"새 버전 v{u.get('latest')}이(가) 있어요(지금 v{u.get('current')}). '지금 업데이트'라고 하면 바꿔 드려요")
    if u.get("error"):
        return _done(f"확인하지 못했어요: {u['error']}")
    return _done(f"지금 쓰는 v{u.get('current')}이(가) 최신이에요")


def _r_update_apply(app, p):
    _call(app, "/api/update/apply", {})
    return _done("업데이트를 시작했어요. 끝나면 앱이 다시 켜져요(pip·git 설치면 설정 → 업데이트의 [다시 시작])")


def _r_mascot(app, p):
    prev = _mascot_choice(app.store)
    _call(app, "/api/mascot/map", {"choice": p["choice"]})
    return _done(f"캐릭터를 '{CHOICES[p['choice']]}'(으)로 바꿨어요", {"choice": prev, "set": p["choice"]})


def _r_materials_folder(app, p):
    _call(app, "/api/materials/open", {})
    return _done("자료 폴더를 열었어요")


def _r_app_quit(app, p):
    res = _call(app, "/api/app/quit", {}) or {}
    return _done(res.get("message") or "앱을 껐어요")


def _r_coach(app, p):
    cid = p.get("id")
    if not cid:
        card = (_section(app, "coach") or {}).get(p["kind"])
        cid = card.get("id") if isinstance(card, dict) else None
        if not cid:
            raise ApiError(400, f"지금 닫을 {CARDS[p['kind']]}가 없어요")
    _call(app, "/api/coach/dismiss", {"id": cid})
    return _done(f"{CARDS.get(p.get('kind'), '코치 카드')}를 닫았어요")


def _r_plan_apply(app, p):
    before = {x["id"] for x in _plans(app.store)}
    res = _call(app, "/api/plan/apply", p) or {}
    new = res.get("plan_id") if isinstance(res.get("plan_id"), str) else None
    if not new:   # plan_id를 주지 않는 경로면 앞뒤 목록을 비교
        after = [x for x in res.get("plans") or _plans(app.store) if isinstance(x, dict)]
        new = next((x["id"] for x in after if isinstance(x.get("id"), str) and x["id"] not in before), None)
    return _done(f"공부 계획 '{p['title']}'을(를) 넣었어요({len(p['blocks'])}칸)", {"plan_id": new} if new else None)


def _r_plan_remove(app, p):
    _call(app, "/api/plan/remove", {"id": p["id"]})
    return _done("공부 계획을 지웠어요(다음 구글 반영 때 그 일정도 지워져요)")


def _r_checklist(app, p):
    _call(app, f"/api/checklist/{p['item_id']}", {"op": "toggle", "id": p["id"]})
    return _done("체크리스트 칸을 바꿨어요", {"item_id": p["item_id"], "id": p["id"]})


_RUN = {
    "setting": _r_setting, "auto_download": _r_auto_download, "background": _r_background, "autostart": _r_autostart,
    "sync": _r_sync, "google_push": _r_google_push, "course_mode": _r_course_mode, "item_decision": _r_item_decision,
    "item_submitted": _r_item_submitted, "update_check": _r_update_check, "update_apply": _r_update_apply,
    "mascot": _r_mascot, "materials_folder": _r_materials_folder, "app_quit": _r_app_quit, "coach_dismiss": _r_coach,
    "plan_apply": _r_plan_apply, "plan_remove": _r_plan_remove, "checklist_toggle": _r_checklist,
}


def run(app, p: dict) -> dict:
    """사용자가 [실행]을 누른 서버 동작을 실행한다 → {"ok", "message", "undo": 되돌릴 값 | None}.
    다시 검사(지금 상태 기준)하고, 실행 전 값을 기억한 뒤, 화면과 같은 경로 처리기를 부른다.
    거절·실패는 ApiError(400/404/409…)나 경로 처리기의 오류(ValueError → 400)로 올린다."""
    app.store.reload_if_changed()
    q = check(app.store, p)
    if q["rejected_reason"]:
        raise ApiError(400, q["rejected_reason"])
    if q["client"]:
        raise ApiError(400, "화면에서 바로 하는 동작이에요(서버에서 실행하지 않아요)")
    params = dict(q["params"])
    if q["action"] == "plan_apply":   # 공부 계획은 누가 넣었는지 남긴다(계약 22-5: 에이전트 제안 → decided_by "ai")
        params.update(decided_by="ai", reason=str(q.get("reason") or "에이전트 제안")[:300])
    return _RUN[q["action"]](app, params)


def _conflict(now, want) -> None:
    if now != want:
        raise ApiError(409, "실행한 뒤에 직접 바꾼 것 같아 되돌리지 않았어요(지금 값이 그대로 남아요)")


def _u_setting(app, u):
    key = u.get("key")
    if key not in SETTING_NAMES or not isinstance(u.get("value"), bool):
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    _conflict(_settings_now(app.store)[key], u.get("set"))
    _call(app, "/api/settings", {key: u["value"]})
    return f"{SETTING_NAMES[key]}: 다시 {_onoff(u['value'])}"


def _u_auto_download(app, u):
    if not isinstance(u.get("value"), bool):
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    _conflict(bool((app.store.data.get("settings") or {}).get("auto_download", True)), u.get("set"))
    _call(app, "/api/materials/settings", {"auto_download": u["value"]})
    return f"신규 첨부 자동으로 받기: 다시 {_onoff(u['value'])}"


def _u_background(app, u):
    body = {"enabled": bool(u.get("enabled"))}
    if body["enabled"] and u.get("interval_hours") in INTERVALS:
        body["interval_hours"] = u["interval_hours"]
    _call(app, "/api/background", body)
    return "백그라운드 동기화를 실행 전으로 되돌렸어요"


def _u_autostart(app, u):
    body = {"enabled": bool(u.get("enabled"))}
    if isinstance(u.get("open_browser"), bool):
        body["open_browser"] = u["open_browser"]
    _call(app, "/api/autostart", body)
    return "컴퓨터를 켜면 앱 열기를 실행 전으로 되돌렸어요"


def _u_course_mode(app, u):
    cid, mode = u.get("course_id"), u.get("mode")
    if not isinstance(cid, str) or not _ID.fullmatch(cid) or mode not in MODES:
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    _conflict(_course_mode(app.store, cid), u.get("set"))
    _call(app, f"/api/course/{cid}/mode", {"mode": mode})
    return f"{_short(_courses(app.store).get(cid), cid)}: 다시 '{MODES[mode]}'"


def _u_item_decision(app, u):
    iid, d = u.get("item_id"), u.get("decision")
    if not isinstance(iid, str) or not _ID.fullmatch(iid) or d not in DECISIONS:
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    try:
        v = _visible_item(app.store, iid)
    except _Reject as e:
        raise ApiError(404, str(e)) from None
    _conflict(v["decision"], u.get("set"))
    _call(app, f"/api/item/{iid}", {"decision": d})
    for rid in u.get("replaced") or []:    # 승인하면서 확인 전으로 돌렸던 같은 과제의 다른 후보를 다시 승인
        if isinstance(rid, str) and _ID.fullmatch(rid):
            try:
                _call(app, f"/api/item/{rid}", {"decision": "approved"})
            except ApiError:
                pass
    return f"{_item_label(v)}: 다시 '{DECISIONS[d]}'"


def _u_item_submitted(app, u):
    iid, s = u.get("item_id"), u.get("submitted")
    if not isinstance(iid, str) or not _ID.fullmatch(iid) or (s is not None and not isinstance(s, bool)):
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    try:
        v = _visible_item(app.store, iid)
    except _Reject as e:
        raise ApiError(404, str(e)) from None
    _conflict((app.store.data["user"].get(iid) or {}).get("submitted"), u.get("set"))
    _call(app, f"/api/item/{iid}", {"submitted": s})     # null = 원래(Canvas) 값으로
    return f"{_item_label(v)}: 제출 표시를 실행 전으로 되돌렸어요"


def _u_mascot(app, u):
    c = u.get("choice")
    if c not in CHOICES:
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    _conflict(_mascot_choice(app.store), u.get("set"))
    _call(app, "/api/mascot/map", {"choice": c})
    return f"캐릭터: 다시 '{CHOICES[c]}'"


def _u_plan_apply(app, u):
    pid = u.get("plan_id")
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        raise ApiError(400, "되돌릴 공부 계획을 찾지 못했어요")
    _call(app, "/api/plan/remove", {"id": pid})
    return "넣었던 공부 계획을 지웠어요"


def _u_checklist(app, u):
    iid, cid = u.get("item_id"), u.get("id")
    if not isinstance(iid, str) or not _ID.fullmatch(iid) or not isinstance(cid, str):
        raise ApiError(400, "되돌릴 값이 올바르지 않아요")
    _call(app, f"/api/checklist/{iid}", {"op": "toggle", "id": cid})
    return "체크리스트 칸을 실행 전으로 되돌렸어요"


_UNDO = {
    "setting": _u_setting, "auto_download": _u_auto_download, "background": _u_background, "autostart": _u_autostart,
    "course_mode": _u_course_mode, "item_decision": _u_item_decision, "item_submitted": _u_item_submitted,
    "mascot": _u_mascot, "plan_apply": _u_plan_apply, "checklist_toggle": _u_checklist,
}


def undo(app, p: dict) -> dict:
    """run()이 돌려준 되돌릴 값(제안의 `_undo`, 없으면 `result.undo`·`undo`)으로 실행 전으로 되돌린다 → {"ok", "message"}.
    되돌릴 수 없는 동작이면 ApiError(400), 실행한 뒤 사용자가 직접 바꿨으면 409(사용자 쪽이 이김)."""
    aid = (p or {}).get("action")
    a = BY_ID.get(aid) if isinstance(aid, str) else None
    if a is None:
        raise ApiError(400, "에이전트가 할 수 있는 동작이 아니에요")
    if not a["undoable"] or aid not in _UNDO:
        raise ApiError(400, f"‘{(p or {}).get('title') or a['title']}’은(는) 되돌릴 수 없는 동작이에요")
    u = p.get("_undo")
    if u is None:
        u = (p.get("result") or {}).get("undo") if isinstance(p.get("result"), dict) else None
    if u is None:
        u = p.get("undo")
    if not isinstance(u, dict):
        raise ApiError(400, "되돌릴 값이 없어요(실행하지 않았거나 이미 되돌렸어요)")
    app.store.reload_if_changed()
    if "set" in u and u.get("set") == u.get(_PREV_KEY.get(aid, "value")):
        return {"ok": True, "message": "실행 전과 같아 되돌릴 것이 없어요"}
    return {"ok": True, "message": _UNDO[aid](app, u)}


_PREV_KEY = {"setting": "value", "auto_download": "value", "course_mode": "mode", "item_decision": "decision",
             "item_submitted": "submitted", "mascot": "choice"}
