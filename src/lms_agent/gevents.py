"""구글 캘린더의 다른 일정(계약 23장): 가져오기 · 정리 · 저장 · 대시보드 창.

- 선택(기본 꺼짐): settings.gcal_read. 읽기만 한다 — 다른 캘린더에 쓰거나 지우지 않는다. 앱의 'LMS 마감' 캘린더는 읽지 않는다.
- 저장: store.data["gcal_events"] = {"fetched_at", "events": [정리한 일정…], "error", "calendars": [정리한 캘린더…],
  "selected": [보일 캘린더 id…] | None(= 구글에서 '표시'로 둔 캘린더)}. 이 PC(state.json)에만 둔다.
- 개인정보 최소: 제목·시각·장소·링크·캘린더 이름·색만. 설명·참석자는 저장하지 않는다. AI 입력·알림에는 넣지 않는다
  (agent·notify는 이 칸을 읽지 않는다. 코치·공부 계획은 수·시각만 쓴다).
- 화면 상태(window·read_state·busy)는 저장된 것만으로 계산한다(네트워크 없음). 가져오기(fetch)만 구글에 묻는다.
- 창: 오늘 0시(로컬) −1일 ~ +21일 끝(= 오늘+22일 0시), 시작 시각 순, 최대 300개.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .timeutil import iso, now_local, parse_iso

KEY = "gcal_events"
SETTING = "gcal_read"
DAYS_BEFORE, DAYS_AFTER = 1, 21
MAX_EVENTS = 300
MAX_CALENDARS = 50
MAX_TITLE = 200
MAX_TEXT = 200
UNTITLED = "(제목 없음)"
BUSY_ONLY = "바쁨"
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_RATE = {"rateLimitExceeded", "userRateLimitExceeded"}

READ_NEEDED = ("다른 일정을 읽을 권한이 아직 없어요. 설정 → 구글 캘린더에서 '다른 일정도 대시보드에 보기'를 켜고 "
               "구글 동의 화면에서 '캘린더 목록 보기'와 '일정 보기'를 허락해 주세요")
READ_DENIED = ("구글이 다른 일정 읽기 권한을 허락하지 않았어요(동의 화면에서 체크를 뺐거나 권한을 거뒀어요). "
               "구글 캘린더 반영은 그대로예요. 다시 보려면 설정 → 구글 캘린더에서 '다른 일정도 대시보드에 보기'를 다시 켜 주세요")
NOT_CONNECTED = "먼저 구글 캘린더를 연결해 주세요(설정 → 구글 캘린더)"


# ---------------------------------------------------------------- 저장 칸

def enabled(store) -> bool:
    return (store.data.get("settings") or {}).get(SETTING) is True


def _box(data: dict) -> dict:
    """읽기용(없거나 깨졌으면 빈 dict)."""
    b = data.get(KEY)
    return b if isinstance(b, dict) else {}


def _wbox(data: dict) -> dict:
    """쓰기용(없으면 만든다)."""
    b = data.get(KEY)
    if not isinstance(b, dict):
        b = data[KEY] = {}
    return b


def set_enabled(store, value: bool) -> None:
    """켜기/끄기(잠금 안에서 부르고 저장은 부른 쪽이). 끄면 가져온 일정·캘린더 목록을 바로 지운다(보일 캘린더 선택은 남김)."""
    store.data.setdefault("settings", {})[SETTING] = bool(value)
    if not value:
        sel = _box(store.data).get("selected")
        store.data[KEY] = {"selected": sel} if isinstance(sel, list) else {}


def wipe(store, everything: bool = False) -> None:
    """가져온 일정을 지운다(잠금 안에서). everything이면 선택·설정까지(구글 연결 해제)."""
    if everything:
        store.data.pop(KEY, None)
        if isinstance(store.data.get("settings"), dict):
            store.data["settings"].pop(SETTING, None)
    elif KEY in store.data:
        sel = _box(store.data).get("selected")
        store.data[KEY] = {"selected": sel} if isinstance(sel, list) else {}


def set_error(store, message: str | None) -> None:
    _wbox(store.data)["error"] = message


def calendars(store) -> list[dict]:
    """저장된 캘린더 목록(마지막으로 가져온 것)."""
    return [c for c in _box(store.data).get("calendars") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)]


def default_selection(cals: list[dict]) -> list[str]:
    """기본: 구글에서 '표시'로 둔 캘린더."""
    return [c["id"] for c in cals if c.get("google_selected")]


def selected_ids(store, cals: list[dict] | None = None) -> list[str]:
    cals = calendars(store) if cals is None else cals
    known = [c["id"] for c in cals]
    sel = _box(store.data).get("selected")
    if not isinstance(sel, list):
        return default_selection(cals)
    want = {s for s in sel if isinstance(s, str)}
    return [cid for cid in known if cid in want]


def set_selection(store, ids) -> list[str]:
    """보일 캘린더를 고른다(잠금 안에서). 모르는 id·잘못된 모양이면 ValueError(한국어). 빠진 캘린더의 일정은 바로 지운다."""
    if not isinstance(ids, list) or len(ids) > MAX_CALENDARS or not all(isinstance(i, str) and 0 < len(i) <= 300
                                                                        for i in ids):
        raise ValueError(f"selected는 캘린더 id 목록이어야 해요(최대 {MAX_CALENDARS}개)")
    known = [c["id"] for c in calendars(store)]
    if set(ids) - set(known):
        raise ValueError("모르는 캘린더가 있어요. 화면을 다시 불러온 뒤 골라 주세요")
    chosen = [cid for cid in known if cid in set(ids)]
    box = _wbox(store.data)
    box["selected"] = chosen
    box["events"] = [e for e in box.get("events") or [] if isinstance(e, dict) and e.get("calendar_id") in chosen]
    return chosen


# ---------------------------------------------------------------- 정리

def color(value) -> str | None:
    """구글 backgroundColor → '#rrggbb' 소문자. 엄격히 #과 16진 6자리만, 그 밖은 None(화면이 고정 팔레트에서 고른다)."""
    return value.lower() if isinstance(value, str) and _COLOR.fullmatch(value) else None


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2000:
        return None
    return value if re.match(r"https?://", value, re.I) else None


def normalize_calendar(raw: dict) -> dict | None:
    """calendarList 항목 → {"id", "name", "primary", "google_selected", "color", "access"}. 지운·숨긴 캘린더는 None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"] or len(raw["id"]) > 300:
        return None
    if raw.get("deleted") or raw.get("hidden"):
        return None
    access = raw.get("accessRole") if raw.get("accessRole") in ("owner", "writer", "reader", "freeBusyReader") else "reader"
    return {"id": raw["id"], "name": _clip(raw.get("summaryOverride") or raw.get("summary") or "(이름 없는 캘린더)", 100),
            "primary": raw.get("primary") is True, "google_selected": raw.get("selected") is True,
            "color": color(raw.get("backgroundColor")), "access": access}


def _edge(side, tz) -> tuple[datetime | None, bool]:
    """일정의 start/end → (로컬 시각, 종일인지). 종일은 그 날 0시(로컬)."""
    if not isinstance(side, dict):
        return None, False
    d = side.get("date")
    if isinstance(d, str):
        try:
            return datetime.combine(date.fromisoformat(d[:10]), time(0), tzinfo=tz), True
        except ValueError:
            return None, True
    v = side.get("dateTime")
    dt = parse_iso(v) if isinstance(v, str) and len(v) <= 40 else None
    return (dt.astimezone(tz) if dt else None), False


def normalize_event(raw: dict, cal: dict, tz) -> dict | None:
    """events.list 항목 → 화면용 일정(계약 23-2). 취소된 것·시각이 없는 것은 None. 설명·참석자는 버린다."""
    if not isinstance(raw, dict) or raw.get("status") == "cancelled" or not isinstance(raw.get("id"), str):
        return None
    start, all_day = _edge(raw.get("start"), tz)
    end, _ = _edge(raw.get("end"), tz)
    if start is None:
        return None
    if end is None or end < start:
        end = start + timedelta(days=1) if all_day else start
    busy_only = cal.get("access") == "freeBusyReader"
    title = _clip(raw.get("summary"), MAX_TITLE)
    if busy_only or (not title and raw.get("visibility") in ("private", "confidential")):
        title, busy_only = BUSY_ONLY, True   # 비공개로 공유된 일정: 제목이 오지 않는다
    elif not title:
        title = UNTITLED
    loc = _clip(raw.get("location"), MAX_TEXT) or None
    return {"id": f"{cal['id']}:{raw['id']}", "calendar_id": cal["id"], "calendar_name": cal.get("name"),
            "color": cal.get("color"), "title": title, "start": iso(start), "end": iso(end), "all_day": all_day,
            "location": None if busy_only else loc, "url": None if busy_only else _url(raw.get("htmlLink")),
            "busy": raw.get("transparency") != "transparent"}


# ---------------------------------------------------------------- 창(저장된 것만)

def window_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or now_local()
    today = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)
    return today - timedelta(days=DAYS_BEFORE), today + timedelta(days=DAYS_AFTER + 1)


def _stored(store) -> list[tuple[datetime, datetime, dict]]:
    if not enabled(store):
        return []
    sel = set(selected_ids(store))
    out = []
    for e in _box(store.data).get("events") or []:
        if not isinstance(e, dict) or e.get("calendar_id") not in sel:
            continue
        s, t = parse_iso(e.get("start")), parse_iso(e.get("end"))
        if s and t:
            out.append((s, t, e))
    out.sort(key=lambda x: (x[0], x[1], str(x[2].get("id"))))
    return out


def window(store, now: datetime | None = None) -> list[dict]:
    """state.events: 꺼져 있으면 []. 창 안(겹치는 것)만, 시작 순, 최대 300."""
    lo, hi = window_range(now)
    keys = ("id", "calendar_id", "calendar_name", "color", "title", "start", "end", "all_day", "location", "url", "busy")
    return [{k: e.get(k) for k in keys} for s, t, e in _stored(store) if t > lo and s < hi][:MAX_EVENTS]


def busy(store) -> list[tuple[datetime, datetime, bool]]:
    """공부 계획·브리핑용: 바쁨(busy)인 저장된 일정의 (시작, 끝, 종일) — 제목은 주지 않는다."""
    return [(s, t, bool(e.get("all_day"))) for s, t, e in _stored(store) if e.get("busy") is not False]


def read_state(link, store, now: datetime | None = None) -> dict:
    """state.google.read(계약 23-2)."""
    connected = bool(link and link.available and link.connected)
    granted = bool(connected and link.read_granted)
    on = enabled(store)
    box = _box(store.data)
    cals = calendars(store) if on else []
    sel = set(selected_ids(store, cals))
    error = box.get("error") if isinstance(box.get("error"), str) else None   # 끄기 전이라도(읽기 체크를 뺀 안내)
    if on and connected and not granted and not error:
        error = READ_NEEDED
    elif on and not connected:
        error = NOT_CONNECTED
    return {"enabled": on, "granted": granted,
            "calendars": [{"id": c["id"], "name": c["name"], "primary": c["primary"], "selected": c["id"] in sel,
                           "color": c["color"], "access": c["access"]} for c in cals],
            "fetched_at": box.get("fetched_at") if on else None, "count": len(window(store, now)),
            "error": error, "fetching": bool(link and getattr(link, "fetching", False))}


# ---------------------------------------------------------------- 가져오기(구글에 묻는다)

def _managed(link) -> set:
    g = link.cfg.get("google") or {}
    return {g["calendar_id"]} if isinstance(g.get("calendar_id"), str) else set()


def fetch(link, store, now: datetime | None = None) -> dict:
    """구글에서 캘린더 목록과 보일 캘린더의 일정을 가져와 저장한다 → {"ok", "count", "error"}.
    실패는 read.error(한국어)에만 남기고 지난 일정은 그대로 둔다. 401/403 권한 오류면 granted false(다시 동의 안내).
    가져오는 사이 꺼지거나 연결이 끊겼으면 저장하지 않는다."""
    from .gcal import CALENDAR_NAME, GoogleError

    if not (enabled(store) and link.available and link.connected):
        return {"ok": False, "count": 0, "error": None}
    if not link.read_granted:
        return {"ok": False, "count": 0, "error": READ_NEEDED}
    now = now or now_local()
    lo, hi = window_range(now)
    tz = now.tzinfo
    managed = _managed(link)
    try:
        cal = link.calendar()
        cals = []
        for raw in cal.list_calendars():
            c = normalize_calendar(raw)
            if c is None or c["id"] in managed or (c["name"] == CALENDAR_NAME and c["access"] == "owner"):
                continue   # 앱의 'LMS 마감' 캘린더는 읽지 않는다(마감이 두 번 보이지 않게)
            cals.append(c)
            if len(cals) >= MAX_CALENDARS:
                break
        prev = _box(store.data)
        sel = prev.get("selected")
        if isinstance(sel, list):
            want = [c["id"] for c in cals if c["id"] in set(sel)]
        else:
            want = default_selection(cals)
        by_id = {c["id"]: c for c in cals}
        events, skipped = [], []
        for cid in want:
            try:
                raws = cal.list_events(cid, lo, hi, limit=MAX_EVENTS)
            except GoogleError as e:
                if e.status in (404, 410) or (e.status == 403 and e.reason not in _RATE | {"insufficientPermissions"}):
                    skipped.append(by_id[cid]["name"])   # 지워졌거나 이 캘린더만 볼 수 없음 → 그 캘린더만 뺀다
                    continue
                raise
            for raw in raws:
                ev = normalize_event(raw, by_id[cid], tz)
                if ev:
                    events.append(ev)
    except GoogleError as e:
        if e.status == 401:
            msg = str(e)
            if "다시 연결" in msg:
                link.drop_tokens(store)   # 연결이 끊김(구글 반영과 같은 처리) — 가져온 일정도 지운다
        elif e.status == 403 and e.reason not in _RATE:
            msg = READ_DENIED
            link.mark_read_denied()
        else:
            msg = str(e)
        _save(link, store, {"error": msg})
        return {"ok": False, "count": 0, "error": msg}
    except OSError:
        msg = "구글과 통신하다 문제가 생겼어요. 잠시 뒤 다시 가져와 주세요"
        _save(link, store, {"error": msg})
        return {"ok": False, "count": 0, "error": msg}
    events.sort(key=lambda e: (parse_iso(e["start"]), parse_iso(e["end"]), e["id"]))
    events = events[:MAX_EVENTS]
    note = ("일부 캘린더는 읽을 수 없어 뺐어요: " + ", ".join(skipped[:5])) if skipped else None
    _save(link, store, {"calendars": cals, "events": events, "fetched_at": iso(now_local()), "error": note})
    return {"ok": True, "count": len(events), "error": note}


def _save(link, store, fields: dict) -> None:
    """잠금 안에서 다시 읽고 합쳐 쓴다(백그라운드 동기화가 그 사이 저장했을 수 있다). 꺼졌거나 연결이 끊겼으면 버린다."""
    try:
        with store.locked():
            store.reload_if_changed()
            if not enabled(store) or not link.connected:
                return
            _wbox(store.data).update(fields)
            store.write()
    except (OSError, TimeoutError):
        pass   # 다음 가져오기에서 다시
