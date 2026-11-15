"""에이전트가 말로 시킨 것을 직접 하는 능력 목록 — 계약 28장 (v0.8.3, 설계 docs/AGENT_V3.md).

화면의 API 경로(POST)마다 '능력(Capability)' 하나 이상: 무엇을 하는지(한국어), 받는 값의 모양, 위험 등급, 화면과 같은 경로로 부를 본문 만들기,
바뀌기 전→후 미리 보기. AI는 `catalog_text()`로 고르고 `act(capability, params)`를 내면 하네스가 `plan()`(검사·이름→id·미리 보기, 바꾸지 않음) →
등급대로 바로 `run()`(실행 전후 상태를 기록해 공통 되돌리기) 또는 확인 카드. 거절 등급은 실행하지 않고 그 설정 칸을 열어 준다.

모양(이름 고정 — 담당 H1이 몸통, H2는 `capabilities_grades.py`에서 register로 성적 능력을 더하고, H3은 에이전트·화면에서 부른다):
- Capability(id, area, title, route, params, tier, build, preview, undoable, external, refuse, pane)
  - id: "영역.동작"(예: "grades.set_dist"), area: AREAS 중 하나, title: 한국어 짧은 이름
  - route: (메서드, 경로 모양) — 예: ("POST", "/api/grades/{course_id}"). 커버리지 시험이 router의 POST 경로와 맞춰 본다(None = 경로 없이 하는 능력)
  - params: {이름: {"type": "str|int|float|bool|enum|list|dict", "required": bool, "desc": 한국어, "resolve": "course|item|component|None",
             "enum": [...]?}}
  - tier: "direct"(바로 하고 되돌리기) | "confirm"(확인 카드) | "refuse"(하지 않음 — 사용자 결정: AI 켜기·끄기·실행기, 하루 AI 한도, 개인정보)
  - build(app, params) -> (path, body): 화면과 같은 경로·본문(지금 상태를 읽어 일부만 바꾼 전체 본문을 만들어도 됨)
  - preview(app, params) -> [{"label", "before", "after"}]: 바뀌기 전→후(한국어, 짧게)
  - undoable: 공통 되돌리기(상태 기록)로 되돌릴 수 있는지 — 동기화·업데이트·앱 끄기처럼 밖이나 되돌릴 수 없는 것은 False
  - external: 이 PC 밖에 영향(구글 캘린더 등)
  - refuse/pane: 거절 등급일 때 이유(한국어)와 열어 줄 설정 칸(open_view의 pane)
- REGISTRY: {id: Capability}, register(cap), EXCLUDED_ROUTES: {경로 모양: 이유}(에이전트 능력이 아닌 경로 — 대화 자체·첫 설정·미리 보기 등)
- catalog(area=None) -> [ {id, area, title, tier, params(짧게)} ], catalog_text(area=None, budget=3000) -> str(AI용)
- plan(app, cap_id, params) -> {"ok", "capability", "title", "area", "tier", "params"(이름을 id로 바꾼 값), "summary"(한국어 한 줄),
                                "changes": [{"label","before","after"}], "external", "undoable", "error"(한국어 | None), "pane"}
- run(app, planned) -> {"ok", "message", "undo": 되돌리기 기록 | None} — 거절 등급·검사 실패는 ValueError(한국어). 경로 처리기의 ApiError는 그대로 올린다.
- undo(app, record) -> {"ok", "message"} — 실행 뒤 그 칸을 직접 바꿨으면 ApiError(409)(사용자 쪽이 이김).

몸통(H1)이 정한 것:
- params 칸에 더 쓸 수 있는 것: "nullable"(None 허용), "default", "min"/"max"(숫자는 값, 글자는 길이, 목록은 개수), "labels"({enum 값: 한국어} —
  한국어로 말해도 맞춤), "format"("datetime" → 'YYYY-MM-DDTHH:MM' 로컬 시각, "date" → 'YYYY-MM-DD'), "items"(목록 칸의 "str"|"dict").
  "required"가 없으면 필수. 모르는 값 이름은 거절("reason"만 무시 — 이유는 부르는 쪽이 따로 남긴다).
- 이름 → id(RESOLVERS[이름](app, 값, 지금까지 검사한 params, cap) → 값): course(id·전체/짧은 이름·과목 코드·하나로 정해지는 부분 이름),
  item(id·과목 안의 제목·번호 토큰 'LAB4'·날짜 'M/D'로 좁힘), component(성적 설정의 평가 항목 이름 — H2가 바꿔 끼워도 됨),
  plan·checklist·calendar·download(공부 계획·체크리스트 칸·구글 캘린더·받은 파일). 여럿이면 Ambiguous(후보를 담은 오류) → plan의 "candidates".
  course 칸을 먼저 풀어 item·component가 그 과목 안에서 찾게 한다.
- plan 결과에 "candidates"(애매할 때 [{"id", "label"}])를 더 담는다. 미리 보기의 전·후가 모두 같으면 "이미 그 상태" 오류(ok False).
- run 결과에 "data"(경로가 돌려준 것 중 보여 줄 것 — 메일 초안 등, 없으면 None)를 더 담는다.
- 되돌리기 기록 {"cap", "title", "keys": {이름: {"path", "before", "after"}}, "side"?: {"before", "after"}, "at"}:
  - 실행 전후 `store.data`를 비교한다. SNAPSHOT_SKIP(원본·기록·캐시 칸)은 보지 않는다.
  - 최상위 칸이 dict면 하위 칸마다(예: settings/weekly_summary, user/<항목 id>, grades/<과목 id>),
    id 붙은 dict 목록이면 id마다(예: plans/#p-…, manual/#…), 그 밖은 칸 통째로. 없던 칸은 {"$absent": true}로 적는다.
  - 상태가 앱 밖에 있는 능력(백그라운드 동기화·자동 실행 = OS 작업, 다른 일정 보기·캘린더 고르기 = 가져온 일정)은 "side"에 그 상태를 적고
    되돌릴 때 같은 경로로 되돌린다(_SIDE).
  - undo: 모든 칸이 지금 == after인지 먼저 다 보고(하나라도 다르면 409, 아무것도 바꾸지 않음), 잠금 안에서 before로 쓴다.
    manual·agent_items를 되돌리면 항목을 다시 만든다(core.refresh_items). 항목·캘린더 입력이 바뀌면 "changed".
- run·undo는 app.lock·store.locked()를 잡지 않은 채로 불러야 한다(경로 처리기가 스스로 잡는다 — actions와 같음).
"""
from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .api import ApiError, StateContext, router

AREAS = ("settings", "items", "courses", "grades", "calendar", "materials", "plan", "checklist", "coach", "app",
         "notify", "account", "ai")
TIERS = ("direct", "confirm", "refuse")
AREA_NAMES = {"settings": "설정", "items": "마감·항목", "courses": "과목", "grades": "성적", "calendar": "구글 캘린더",
              "materials": "첨부 자료", "plan": "공부 계획", "checklist": "체크리스트", "coach": "코치 카드",
              "app": "앱(동기화·백그라운드·자동 실행·업데이트·캐릭터)", "notify": "알림·소식", "account": "계정(직접)",
              "ai": "AI 설정(직접)"}


@dataclass
class Capability:
    id: str
    area: str
    title: str
    route: tuple | None
    params: dict = field(default_factory=dict)
    tier: str = "direct"
    build: Callable[[Any, dict], tuple] | None = None
    preview: Callable[[Any, dict], list] | None = None
    undoable: bool = True
    external: bool = False
    refuse: str | None = None
    pane: str | None = None


REGISTRY: dict = {}
EXCLUDED_ROUTES: dict = {}
RESOLVERS: dict = {}


def register(cap: Capability) -> Capability:
    if cap.area not in AREAS or cap.tier not in TIERS:
        raise ValueError(f"잘못된 능력 정의: {cap.id}")
    if cap.route is not None and (not isinstance(cap.route, tuple) or len(cap.route) != 2):
        raise ValueError(f"잘못된 능력 정의(route): {cap.id}")
    if not isinstance(cap.params, dict):
        raise ValueError(f"잘못된 능력 정의(params): {cap.id}")
    REGISTRY[cap.id] = cap
    return cap


class Ambiguous(ValueError):
    """이름이 여러 대상에 맞음 — 한국어 문구 + 후보 [{"id", "label"}]."""

    def __init__(self, message: str, candidates: list[dict]):
        super().__init__(message)
        self.candidates = candidates


# ================================================================ 경로 모양

def route_shape(pattern: str) -> str:
    """router의 정규식 경로 → 능력의 경로 모양: r"/api/item/(?P<item_id>[\\w\\-]+)" → "/api/item/{item_id}"."""
    s = re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", pattern)
    return s.replace("\\.", ".").replace("\\-", "-")


def _shape_key(shape: str) -> str:
    return re.sub(r"\{\w*\}", "{}", shape)


def post_routes() -> list[str]:
    """router에 등록된 POST 경로 모양(중복 없이, 등록 순서)."""
    out = []
    for r in router.routes:
        if r.method == "POST":
            s = route_shape(r.pattern.pattern)
            if s not in out:
                out.append(s)
    return out


def uncovered_routes() -> list[str]:
    """능력도 EXCLUDED_ROUTES도 아닌 POST 경로(커버리지 시험이 쓴다)."""
    _ensure_loaded()
    have = {_shape_key(c.route[1]) for c in REGISTRY.values() if c.route and c.route[0] == "POST"}
    have |= {_shape_key(s) for s in EXCLUDED_ROUTES}
    return [s for s in post_routes() if _shape_key(s) not in have]


# ================================================================ 목록

def catalog(area: str | None = None) -> list[dict]:
    _ensure_loaded()
    return [{"id": c.id, "area": c.area, "title": c.title, "tier": c.tier, "params": dict(c.params),
             "undoable": c.undoable, "external": c.external}
            for c in REGISTRY.values() if area is None or c.area == area]


_RESOLVE_WORD = {"course": "과목", "item": "항목", "component": "평가 항목", "plan": "공부 계획", "checklist": "칸",
                 "calendar": "캘린더", "download": "받은 파일"}


def _param_text(name: str, spec: dict) -> str:
    t = spec.get("type")
    if t == "enum":
        shape = "|".join(str(e) for e in spec.get("enum") or [])
    elif spec.get("resolve") in _RESOLVE_WORD:
        shape = _RESOLVE_WORD[spec["resolve"]] + ("[]" if t == "list" else "")
    elif spec.get("format") == "datetime":
        shape = "YYYY-MM-DDTHH:MM"
    elif spec.get("format") == "date":
        shape = "YYYY-MM-DD"
    else:
        shape = str(t or "값")
    if spec.get("nullable"):
        shape += "|null"
    return f"{name}{'' if spec.get('required', True) else '?'}: {shape}"


def _cap_line(c: Capability, detail: bool = False) -> str:
    """한 줄(영역 보기면 목록·객체 칸의 모양 설명을 아래에 덧붙임)."""
    params = ", ".join(_param_text(n, s or {}) for n, s in c.params.items())
    more = ""
    if detail and c.tier != "refuse":
        more = "".join(f"\n    · {n}: {str(s.get('desc'))[:200]}" for n, s in c.params.items()
                       if (s or {}).get("type") in ("list", "dict") and s.get("desc"))
    flag = ""
    if c.tier == "refuse":
        flag = " [하지 않음 → 설정 열기]"
    else:
        if c.tier == "confirm":
            flag += " [확인 카드]"
        if not c.undoable:
            flag += " (되돌릴 수 없음)"
        if c.external:
            flag += " (구글 등 밖에 반영)"
    return f"- {c.id} {{{params}}} {c.title}{flag}{more}"


def _fit(lines: list[str], budget: int, more: Callable[[int], str]) -> str:
    """lines를 budget 글자 안에 담는다. 다 못 담으면 앞에서부터 담고 못 담은 줄 수를 more(n)으로 알린다."""
    budget = max(0, int(budget))
    whole = "\n".join(lines)
    if len(whole) <= budget:
        return whole
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        sep = 1 if out else 0
        note = more(len(lines) - i - 1)
        if used + sep + len(line) + 1 + len(note) <= budget:
            out.append(line)
            used += sep + len(line)
            continue
        note = more(len(lines) - i)
        if used + sep + len(note) <= budget:
            out.append(note)
        break
    return "\n".join(out)[:budget]


def catalog_text(area: str | None = None, budget: int = 3000) -> str:
    """AI에게 줄 능력 목록. area가 없으면 영역별 개요(능력 id만), 있으면 그 영역의 자세한 모양(값 이름·종류, 등급)."""
    _ensure_loaded()
    caps = list(REGISTRY.values())
    if area:
        if area not in AREAS:
            return _fit([f"모르는 영역이에요: {str(area)[:30]}. 영역: " + ", ".join(AREAS)], budget, lambda n: "")
        mine = [c for c in caps if c.area == area]
        head = (f"[{area} — {AREA_NAMES.get(area, area)}] act(capability, params). 과목·항목 이름은 그대로 써도 돼요"
                "(하네스가 id로 바꿈, 여럿이면 후보를 알려 줌). ?는 선택.")
        lines = [head] + [_cap_line(c, detail=True) for c in mine]
        if not mine:
            lines.append("(이 영역에는 능력이 없어요)")
        return _fit(lines, budget, lambda n: f"…({n}줄 더 — 필요한 능력 id로 다시 물어보세요)")
    head = ("할 수 있는 일(영역 → 능력 id). 자세한 값 모양은 list_capabilities(영역)으로 보고 act(capability, params)로 해요. "
            "[확인] = 확인 카드, [직접] = 하지 않고 설정 칸을 열어 줌.")
    lines = [head]
    for a in AREAS:
        mine = [c for c in caps if c.area == a]
        if not mine:
            continue
        ids = ", ".join(c.id + (" [확인]" if c.tier == "confirm" else " [직접]" if c.tier == "refuse" else "")
                        for c in mine)
        lines.append(f"- {a}({AREA_NAMES.get(a, a)}) {len(mine)}개: {ids}")
    return _fit(lines, budget, lambda n: f"…({n}개 영역 더)")


# ================================================================ 이름 → id

def _key(s) -> str:
    """비교용: 소문자, 글자·숫자 말고는 모두 뺌(한글은 그대로)."""
    return re.sub(r"[\W_]+", "", str(s if s is not None else "").lower())


def _course_list(store) -> list[dict]:
    return [c for c in (store.data.get("cache") or {}).get("courses") or [] if isinstance(c, dict) and c.get("id") is not None]


def course_short(app, cid) -> str:
    from .core import short_course_name
    c = next((x for x in _course_list(app.store) if str(x.get("id")) == str(cid)), None)
    return short_course_name((c or {}).get("name")) or str(cid or "과목")


def _course_variants(c: dict) -> list[str]:
    from .core import short_course_name
    name = str(c.get("name") or "")
    short = short_course_name(name) or ""
    return [name, short, re.sub(r"\([^()]*\)", "", short).strip(), str(c.get("course_code") or "")]


def course_of(cap: Capability, clean: dict) -> str | None:
    """이 능력의 params에서 과목(resolve "course") 값 — 이미 id로 바뀐 것."""
    for n, s in cap.params.items():
        if (s or {}).get("resolve") == "course" and clean.get(n) is not None:
            return str(clean[n])
    return None


def item_of(cap: Capability, clean: dict) -> str | None:
    for n, s in cap.params.items():
        if (s or {}).get("resolve") == "item" and clean.get(n) is not None:
            return str(clean[n])
    return None


def _as_text(value, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{what}은(는) 이름이나 id(글자)로 주세요")
    s = str(value).strip()
    if not s:
        raise ValueError(f"{what}이(가) 비어 있어요")
    return s


_TAIL = re.compile(r"(?:과목|수업|강의|강좌|course|class)$")


def find_course(app, value) -> str:
    """과목 → id: id, 전체 이름·짧은 이름(괄호 뗀 것 포함)·과목 코드, 하나로 정해지는 부분 이름. 여럿이면 Ambiguous."""
    raw = _as_text(value, "과목")
    cs = _course_list(app.store)
    if not cs:
        raise ValueError("과목 목록이 아직 없어요(먼저 동기화해 주세요)")
    for c in cs:
        if str(c["id"]) == raw:
            return str(c["id"])
    q = _key(raw)
    q2 = _TAIL.sub("", q) or q
    exact, part, inside = [], [], []
    for c in cs:
        vs = {_key(v) for v in _course_variants(c)} - {""}
        if q in vs or q2 in vs:
            exact.append(c)
        elif len(q2) >= 2 and any(q2 in v for v in vs):
            part.append(c)
        elif any(len(v) >= 3 and v in q for v in vs):
            inside.append(c)
    for group in (exact, part, inside):
        if len(group) == 1:
            return str(group[0]["id"])
        if len(group) > 1:
            cands = [{"id": str(c["id"]), "label": course_short(app, c["id"])} for c in group]
            raise Ambiguous(f"‘{raw}’에 맞는 과목이 여럿이에요 — 하나를 골라 주세요: "
                            + " · ".join(f"{x['label']}({x['id']})" for x in cands), cands)
    names = ", ".join(course_short(app, c["id"]) for c in cs)
    raise ValueError(f"‘{raw}’에 맞는 과목을 찾지 못했어요. 과목: {names}"[:300])


def _resolve_course(app, value, clean, cap):
    return find_course(app, value)


_MD = re.compile(r"(\d{1,2})\s*(?:/|월\s*)(\d{1,2})\s*일?")
ACTIVE = ("review", "scheduled", "no_deadline")


def _md(due) -> str:
    """'11/21 23:59'(올해가 아니면 '2027/1/5 23:59')."""
    from .timeutil import now_local, parse_iso
    d = parse_iso(due) if due else None
    if d is None:
        return "마감 없음"
    year = f"{d.year}/" if d.year != now_local().year else ""
    return f"{year}{d.month}/{d.day} {d.hour:02d}:{d.minute:02d}"


def item_label(v: dict) -> str:
    from .core import short_course_name
    return f"{v.get('title')}({short_course_name(v.get('course_name')) or '과목'} · {_md(v.get('due'))})"


def _by_days(group: list[dict], days: set) -> list[dict]:
    from .timeutil import parse_iso
    if not days or len(group) < 2:
        return group
    same = []
    for v in group:
        d = parse_iso(v.get("due")) if v.get("due") else None
        if d and (d.month, d.day) in days:
            same.append(v)
    return same or group


def find_item(app, value, course_id: str | None = None) -> str:
    """항목 → id: id, 과목 안의 제목(같음 → 번호 토큰 'LAB4' → 부분 제목 → 번호 없는 낱말), 말한 날짜(M/D)로 좁힘.
    같은 제목이 여럿이고 그중 하나만 살아 있으면(확인 전·일정·마감 없음) 그것. 그래도 여럿이면 Ambiguous."""
    from .extract import title_tokens
    raw = _as_text(value, "항목")
    view = app.store.view()
    hit = next((v for v in view if v["id"] == raw), None)
    if hit is not None:
        if course_id is not None and str(hit.get("course_id")) != str(course_id):
            raise ValueError(f"그 항목은 {course_short(app, course_id)} 과목의 것이 아니에요")
        return hit["id"]
    scope = [v for v in view if course_id is None or str(v.get("course_id")) == str(course_id)]
    days = {(int(m), int(d)) for m, d in _MD.findall(raw)}
    text = _MD.sub(" ", raw) if days else raw
    q = _key(text)
    mt = title_tokens(text)
    m_num = {t for t in mt if t[1] is not None}
    m_terms = {k for k, n in mt if n is None}

    def nums(v):
        return {t for t in title_tokens(v.get("title")) if t[1] is not None}

    def terms(v):
        return {k for k, n in title_tokens(v.get("title")) if n is None}

    exact = [v for v in scope if q and _key(v.get("title")) == q]
    numbered = [v for v in scope if m_num and nums(v) & m_num]
    part = [v for v in scope if len(q) >= 2 and (q in _key(v.get("title"))
                                                 or (len(_key(v.get("title"))) >= 3 and _key(v.get("title")) in q))]
    worded = [v for v in scope if m_terms and not nums(v) and terms(v) and terms(v) <= m_terms]
    for group in (exact, numbered, part, worded):
        g = _by_days(group, days)
        if len(g) > 1:
            alive = [v for v in g if v.get("status") in ACTIVE]
            if len(alive) == 1:
                g = alive
        if len(g) == 1:
            return g[0]["id"]
        if len(g) > 1:
            cands = [{"id": v["id"], "label": item_label(v)} for v in g[:8]]
            raise Ambiguous(f"‘{raw}’에 맞는 항목이 여럿이에요 — 하나를 골라 주세요(id나 날짜로): "
                            + " · ".join(f"{x['label']} [{x['id']}]" for x in cands), cands)
    where = f"{course_short(app, course_id)} 과목에서 " if course_id else ""
    raise ValueError(f"{where}‘{raw}’ 항목을 찾지 못했어요(제목·번호를 다시 확인하거나 동기화해 주세요)")


def _resolve_item(app, value, clean, cap):
    return find_item(app, value, course_of(cap, clean))


def _pick(raw: str, rows: list[tuple[str, str]], what: str, josa: str = "이") -> str:
    """(id, 이름) 목록에서 id·같은 이름·하나로 정해지는 부분 이름으로 고른다."""
    for rid, _ in rows:
        if rid == raw:
            return rid
    q = _key(raw)
    exact = [(i, n) for i, n in rows if q and _key(n) == q]
    part = [(i, n) for i, n in rows if len(q) >= 2 and q in _key(n)]
    for group in (exact, part):
        if len(group) == 1:
            return group[0][0]
        if len(group) > 1:
            cands = [{"id": i, "label": n} for i, n in group[:8]]
            raise Ambiguous(f"‘{raw}’에 맞는 {what}{josa} 여럿이에요 — 하나를 골라 주세요: "
                            + " · ".join(f"{x['label']}" for x in cands), cands)
    raise ValueError(f"‘{raw}’에 맞는 {what}{josa} 없어요")


def find_component(app, value, course_id: str | None) -> str:
    """평가 항목 → 저장된 이름(같은 이름 → 흔한 표현이 같은 이름('Midterm' = '중간고사') → 하나로 정해지는 부분 이름).
    여럿이면 Ambiguous. 맞는 것이 없거나 과목을 모르면 말한 그대로 돌려준다(성적 능력의 build가 계열·별칭으로 다시 보고 없으면 거절)."""
    raw = _as_text(value, "평가 항목")
    if course_id is None:
        return raw
    comps = [c for c in (((app.store.data.get("grades") or {}).get(str(course_id)) or {}).get("components") or [])
             if isinstance(c, dict) and c.get("name")]
    try:
        from .gradebook import _canon as canon
    except ImportError:   # pragma: no cover
        def canon(x):
            return str(x).lower()
    names = [str(c["name"]) for c in comps]
    for n in names:
        if n == raw or _key(n) == _key(raw):
            return n
    same = [n for n in names if canon(n) == canon(raw)]
    if len(same) == 1:
        return same[0]
    try:
        return _pick(raw, [(n, n) for n in names], "평가 항목")
    except Ambiguous:
        raise
    except ValueError:
        return raw


def _resolve_component(app, value, clean, cap):
    return find_component(app, value, course_of(cap, clean))


def _plans(app) -> list[dict]:
    from . import plan as planmod
    return planmod.state(app.store)


def _resolve_plan(app, value, clean, cap):
    raw = _as_text(value, "공부 계획")
    rows = [(p["id"], str(p.get("title") or p["id"])) for p in _plans(app)]
    if not rows:
        raise ValueError("넣어 둔 공부 계획이 없어요")
    return _pick(raw, rows, "공부 계획")


def _checklist_rows(app, iid: str) -> list[dict]:
    from .actions import _section
    box = (_section(app, "checklists") or {}).get(iid) or {}
    return [r for r in box.get("items") or [] if isinstance(r, dict) and isinstance(r.get("id"), str)]


def _resolve_checklist(app, value, clean, cap):
    raw = _as_text(value, "체크리스트 칸")
    iid = item_of(cap, clean)
    if iid is None:
        raise ValueError("체크리스트 칸을 찾으려면 항목(item)도 알려 주세요")
    rows = [(r["id"], str(r.get("text") or r["id"])) for r in _checklist_rows(app, iid)]
    if not rows:
        raise ValueError("그 항목에는 체크리스트 칸이 없어요")
    return _pick(raw, rows, "체크리스트 칸")


def _resolve_calendar(app, value, clean, cap):
    from . import gevents
    rows = [(c["id"], str(c.get("name") or c["id"])) for c in gevents.calendars(app.store)]
    if not rows:
        raise ValueError("가져온 구글 캘린더 목록이 아직 없어요(다른 일정 보기를 켜고 잠시 뒤 다시 해 주세요)")
    values = value if isinstance(value, list) else [value]
    out = []
    for v in values:
        cid = _pick(_as_text(v, "캘린더"), rows, "캘린더", "가")
        if cid not in out:
            out.append(cid)
    return out


def _resolve_download(app, value, clean, cap):
    raw = _as_text(value, "받은 파일")
    rows = [(str(k), str((m or {}).get("filename") or k)) for k, m in (app.store.data.get("downloads") or {}).items()
            if isinstance(m, dict)]
    if not rows:
        raise ValueError("받은 첨부 파일이 아직 없어요")
    return _pick(raw, rows, "받은 파일")


RESOLVERS.update(course=_resolve_course, item=_resolve_item, component=_resolve_component, plan=_resolve_plan,
                 checklist=_resolve_checklist, calendar=_resolve_calendar, download=_resolve_download)


# ================================================================ 값 검사

def _datetime_input(s: str) -> str:
    """'2026-11-20T23:59'·'2026-11-20 23:59'·'2026-11-20'(→ 23:59)·시간대 붙은 ISO → 로컬 'YYYY-MM-DDTHH:MM'."""
    from .timeutil import parse_iso
    t = s.strip().replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} \d", s.strip()) else s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        t += "T23:59"
    d = parse_iso(t) if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", t) else None
    if d is None:
        raise ValueError("시각은 YYYY-MM-DDTHH:MM 모양(로컬 시각)으로 주세요")
    return d.strftime("%Y-%m-%dT%H:%M")


def _date_input(s: str) -> str:
    from datetime import date
    t = s.strip()[:10]
    try:
        date.fromisoformat(t)
    except ValueError:
        raise ValueError("날짜는 YYYY-MM-DD 모양으로 주세요") from None
    return t


def _range_text(lo, hi) -> str:
    if lo is not None and hi is not None:
        return f"{lo}~{hi} 사이"
    return f"{lo} 이상" if lo is not None else f"{hi} 이하"


def _typed(name: str, spec: dict, v):
    t = spec.get("type")
    if t == "str":
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise ValueError(f"‘{name}’은(는) 글자여야 해요")
        v = str(v).strip()
        mx = spec.get("max")
        if mx and len(v) > mx:
            raise ValueError(f"‘{name}’은(는) {mx}자 이하여야 해요")
        if spec.get("format") == "datetime":
            v = _datetime_input(v)
        elif spec.get("format") == "date":
            v = _date_input(v)
        return v
    if t in ("int", "float"):
        if isinstance(v, bool):
            raise ValueError(f"‘{name}’은(는) 숫자여야 해요")
        if isinstance(v, str):
            try:
                v = float(v.strip().replace(",", "").rstrip("%"))
            except ValueError:
                raise ValueError(f"‘{name}’은(는) 숫자여야 해요") from None
        if not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"‘{name}’은(는) 숫자여야 해요")
        if t == "int":
            if float(v) != int(v):
                raise ValueError(f"‘{name}’은(는) 정수여야 해요")
            v = int(v)
        else:
            v = float(v)
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ValueError(f"‘{name}’은(는) {_range_text(lo, hi)}여야 해요")
        return v
    if t == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        raise ValueError(f"‘{name}’은(는) true 또는 false여야 해요")
    if t == "enum":
        opts = list(spec.get("enum") or [])
        if not isinstance(v, bool):
            for o in opts:
                if v == o or (isinstance(v, (str, int, float)) and str(v).strip().lower() == str(o).lower()):
                    return o
            k = _key(v)
            for o, label in (spec.get("labels") or {}).items():
                if k and k == _key(label) and o in opts:
                    return o
        return _enum_error(name, opts)
    if t == "list":
        if not isinstance(v, list):
            raise ValueError(f"‘{name}’은(는) 목록이어야 해요")
        mx = spec.get("max")
        if mx and len(v) > mx:
            raise ValueError(f"‘{name}’은(는) {mx}개 이하여야 해요")
        kind = spec.get("items")
        if kind == "str" and not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in v):
            raise ValueError(f"‘{name}’의 값은 모두 글자여야 해요")
        if kind == "dict" and not all(isinstance(x, dict) for x in v):
            raise ValueError(f"‘{name}’의 값은 모두 객체({{…}})여야 해요")
        return copy.deepcopy(v)
    if t == "dict":
        if not isinstance(v, dict):
            raise ValueError(f"‘{name}’은(는) 객체({{…}})여야 해요")
        return copy.deepcopy(v)
    return v


def _enum_error(name, opts):
    raise ValueError(f"‘{name}’은(는) {'·'.join(map(str, opts))} 중 하나여야 해요")


_ORDER = {"course": 0, "item": 1, "component": 2}


def _check_params(app, cap: Capability, params) -> dict:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params는 객체({이름: 값})여야 해요")
    params = dict(params)
    for k in list(params):   # 'course_id'·'item_id'처럼 경로 이름으로 줘도 같은 칸('course'·'item')으로
        if k not in cap.params and isinstance(k, str) and k.endswith("_id") and k[:-3] in cap.params \
                and k[:-3] not in params:
            params[k[:-3]] = params.pop(k)
    extra = [str(k) for k in params if k not in cap.params and k != "reason"]
    if extra:
        raise ValueError(f"이 능력이 받지 않는 값이에요: {', '.join(extra)[:80]} (받는 값: {', '.join(cap.params) or '없음'})")
    order = list(cap.params)
    names = sorted(order, key=lambda n: (_ORDER.get((cap.params[n] or {}).get("resolve"), 3), order.index(n)))
    clean: dict = {}
    for name in names:
        spec = cap.params[name] or {}
        v = params.get(name)
        if isinstance(v, str) and not v.strip():
            v = None
        if v is None and not (name in params and spec.get("nullable")):
            if spec.get("required", True):
                raise ValueError(f"‘{name}’({spec.get('desc') or name}) 값이 필요해요")
            if "default" in spec:
                clean[name] = copy.deepcopy(spec["default"])
            continue
        if v is None:
            clean[name] = None
            continue
        v = _typed(name, spec, v)
        r = spec.get("resolve")
        if r and r in RESOLVERS:
            v = RESOLVERS[r](app, v, clean, cap)
        clean[name] = v
    return clean


# ================================================================ 계획(바꾸지 않음)

def _empty_plan(cap_id) -> dict:
    return {"ok": False, "capability": cap_id, "title": None, "area": None, "tier": None, "params": {}, "summary": "",
            "changes": [], "external": False, "undoable": False, "error": None, "pane": None, "candidates": []}


def _summary(cap: Capability, changes: list[dict]) -> str:
    if not changes:
        return cap.title
    parts = [f"{c.get('label')} {c.get('before')} → {c.get('after')}" for c in changes[:4]]
    more = f" 외 {len(changes) - 4}개" if len(changes) > 4 else ""
    return (" · ".join(parts) + more)[:240]


def _txt(v) -> str:
    return "" if v is None else str(v)


def plan(app, cap_id: str, params: dict | None) -> dict:
    """검사·이름 → id·미리 보기. 아무것도 바꾸지 않는다."""
    _ensure_loaded()
    out = _empty_plan(cap_id)
    cap = REGISTRY.get(cap_id) if isinstance(cap_id, str) else None
    if cap is None:
        near = [i for i in REGISTRY if isinstance(cap_id, str) and cap_id and
                (cap_id.split(".")[0] == i.split(".")[0] or cap_id.split(".")[-1] in i)][:6]
        out["error"] = (f"모르는 능력이에요: {str(cap_id)[:40]}" + (f" (비슷한 것: {', '.join(near)})" if near else
                                                              " — list_capabilities()로 목록을 보세요"))
        return out
    out.update(title=cap.title, area=cap.area, tier=cap.tier, external=cap.external, undoable=cap.undoable,
               pane=cap.pane)
    if cap.tier == "refuse":
        out.update(error=cap.refuse or f"‘{cap.title}’은(는) 사용자가 직접 할 일이에요", summary=f"{cap.title} — 직접 해 주세요")
        return out
    try:
        clean = _check_params(app, cap, params)
        out["params"] = clean
        if cap.build is None:
            raise ValueError("이 능력은 실행할 수 없어요(본문을 만드는 법이 없음)")
        path, body = cap.build(app, clean)
        if not isinstance(path, str) or not isinstance(body, dict):
            raise ValueError("이 능력의 본문 모양이 올바르지 않아요")
        changes = list(cap.preview(app, clean) or []) if cap.preview else []
    except Ambiguous as e:
        out.update(error=str(e), candidates=list(e.candidates))
        return out
    except ApiError as e:
        out["error"] = str(e)
        return out
    except (ValueError, LookupError) as e:
        out["error"] = str(e) if str(e) else "값을 확인하지 못했어요"
        return out
    except Exception as e:  # noqa: BLE001 — 계획이 에이전트 루프를 멈추지 않게(내용·비밀값 없이 종류만)
        out["error"] = f"확인하지 못했어요({type(e).__name__})"
        return out
    changes = [{"label": _txt(c.get("label")), "before": _txt(c.get("before")), "after": _txt(c.get("after"))}
               for c in changes if isinstance(c, dict)]
    if changes and all(c["before"] == c["after"] for c in changes):
        out.update(changes=changes, error="이미 그 상태예요: " + " · ".join(f"{c['label']} {c['after']}" for c in changes)[:200])
        return out
    out.update(ok=True, changes=changes, summary=_summary(cap, changes))
    return out


# ================================================================ 실행·되돌리기

SNAPSHOT_SKIP = frozenset({
    # Canvas 원본과 거기서 만든 것(동기화가 통째로 바꾼다 — 항목은 manual·agent_items를 되돌린 뒤 refresh_items로 다시 만든다)
    "cache", "items", "extracted", "rules_version", "pdf_texts", "downloads", "materials_errors",
    # 실행·발견 기록
    "report", "runs", "last_sync", "seen", "last_export",
    # 대화·AI 기록(되돌리기 기록 자체가 여기에 남는다)
    "agent", "ai_runs", "ai_triage", "missed_cases",
    # 상태 계산이 스스로 쓰는 축하·소식 기록
    "celebrated", "medals", "celebrate_init", "review_peak", "news_seen", "reward_toasts",
    # 동기화·AI가 스스로 채우는 값(사용자 값은 grades·grade_links·grade_counts·item_links)
    "weights_auto", "dist_ai", "grade_links_ai", "grade_counts_ai",
    # 구글 반영 기록·가져온 다른 일정(보기 켜기·캘린더 고르기는 _SIDE로 경로를 불러 되돌림)·업데이트 확인 캐시
    "gcal", "gcal_events", "update",
    # 파일 모양 번호
    "version", "app_version",
})
CAL_TOP = frozenset({"user", "items", "manual", "agent_items", "course_prefs", "plans"})
CAL_SETTINGS = frozenset({"include_canvas", "weekly_summary", "review_reminder"})
REFRESH_TOP = frozenset({"manual", "agent_items"})

_MISSING = object()
_ABSENT = {"$absent": True}


def _canon(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _eq(a, b) -> bool:
    if a is _MISSING or b is _MISSING:
        return a is b
    return _canon(a) == _canon(b)


def _top_blank(v) -> bool:
    return v is _MISSING or v is None or v == {} or v == []


def _id_list(v) -> bool:
    if not isinstance(v, list):
        return False
    ids = [x.get("id") for x in v if isinstance(x, dict)]
    return len(ids) == len(v) and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids)


def _snapshot(store) -> dict:
    data = {k: v for k, v in store.data.items() if k not in SNAPSHOT_SKIP}
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _diff(before: dict, after: dict) -> list[tuple]:
    """→ [(path, before, after)]. path = [최상위] | [최상위, 하위 이름] | [최상위, {"id": …}]."""
    out = []
    for k in sorted(set(before) | set(after), key=str):
        b, a = before.get(k, _MISSING), after.get(k, _MISSING)
        if (_top_blank(b) and _top_blank(a)) or _eq(b, a):
            continue
        if (isinstance(b, dict) or isinstance(a, dict)) and (isinstance(b, dict) or _top_blank(b)) \
                and (isinstance(a, dict) or _top_blank(a)):
            bd = b if isinstance(b, dict) else {}
            ad = a if isinstance(a, dict) else {}
            for s in sorted(set(bd) | set(ad), key=str):
                bv, av = bd.get(s, _MISSING), ad.get(s, _MISSING)
                if not _eq(bv, av):
                    out.append(([k, s], bv, av))
        elif (_id_list(b) or _top_blank(b)) and (_id_list(a) or _top_blank(a)):
            bl = b if isinstance(b, list) else []
            al = a if isinstance(a, list) else []
            bm = {x["id"]: x for x in bl}
            am = {x["id"]: x for x in al}
            for i in [x["id"] for x in bl] + [x["id"] for x in al if x["id"] not in bm]:
                bv, av = bm.get(i, _MISSING), am.get(i, _MISSING)
                if not _eq(bv, av):
                    out.append(([k, {"id": i}], bv, av))
        else:
            out.append(([k], b, a))
    return out


def _label(path: list) -> str:
    return "/".join(p if isinstance(p, str) else "#" + str(p.get("id")) for p in path)


def _enc(v):
    return copy.deepcopy(_ABSENT) if v is _MISSING else v


def _dec(v):
    return _MISSING if isinstance(v, dict) and v == _ABSENT else v


def _get(data: dict, path: list):
    cur: Any = data
    for p in path:
        if isinstance(p, dict):
            if not isinstance(cur, list):
                return _MISSING
            cur = next((x for x in cur if isinstance(x, dict) and x.get("id") == p.get("id")), _MISSING)
        else:
            if not isinstance(cur, dict) or p not in cur:
                return _MISSING
            cur = cur[p]
        if cur is _MISSING:
            return _MISSING
    return cur


def _set(data: dict, path: list, value) -> None:
    if len(path) == 1:
        if value is _MISSING:
            data.pop(path[0], None)
        else:
            data[path[0]] = copy.deepcopy(value)
        return
    top, last = path
    box = data.get(top)
    if isinstance(last, dict):
        if not isinstance(box, list):
            if value is _MISSING:
                return
            box = data[top] = []
        idx = next((i for i, x in enumerate(box) if isinstance(x, dict) and x.get("id") == last.get("id")), None)
        if value is _MISSING:
            if idx is not None:
                del box[idx]
        elif idx is None:
            box.append(copy.deepcopy(value))
        else:
            box[idx] = copy.deepcopy(value)
        return
    if not isinstance(box, dict):
        if value is _MISSING:
            return
        box = data[top] = {}
    if value is _MISSING:
        box.pop(last, None)
    else:
        box[last] = copy.deepcopy(value)


def _valid_path(path) -> bool:
    if not isinstance(path, list) or not 1 <= len(path) <= 2:
        return False
    if not isinstance(path[0], str) or path[0] in SNAPSHOT_SKIP:
        return False
    if len(path) == 2:
        p = path[1]
        return isinstance(p, str) or (isinstance(p, dict) and set(p) == {"id"} and isinstance(p["id"], str))
    return True


def _touches_calendar(paths: list[list]) -> bool:
    for p in paths:
        if p[0] in CAL_TOP:
            return True
        if p[0] == "settings" and (len(p) == 1 or p[1] in CAL_SETTINGS):
            return True
    return False


# 'changed'를 경로 처리기가 이미 냈는지(이 스레드에서) — 두 번 내서 구글 반영이 두 번 돌지 않게
_emits = threading.local()


@router.on("changed")
def _note_changed(app, **_):
    if getattr(_emits, "watch", False):
        _emits.hit = True


def _call(app, path: str, body: dict) -> tuple:
    """화면과 같은 경로 처리기를 앱 안에서 부른다 → (응답, 처리기가 changed를 냈는지)."""
    from .actions import _call as call
    _emits.watch, _emits.hit = True, False
    try:
        res = call(app, path, body)
        return res, bool(getattr(_emits, "hit", False))
    finally:
        _emits.watch = False


def _now_iso() -> str:
    from .timeutil import iso, now_local
    return iso(now_local())


# 상태가 store.data 밖에 있는 능력: id → (지금 상태 읽기(app) -> JSON 값, 그 상태로 되돌리기(app, 값))
_SIDE: dict = {}
# 능력별 실행 뒤 문구: id → fn(app, params, 응답) -> str
_MESSAGES: dict = {}
# 능력별 응답에서 보여 줄 것: id → fn(응답) -> 값
_DATA: dict = {}


def run(app, planned: dict) -> dict:
    """확인한 계획을 실행한다 → {"ok", "message", "undo": 기록 | None, "data"}. 지금 상태로 다시 계획하고(그 사이 바뀌었으면 ValueError),
    실행 전 store.data를 기록해 두고 화면과 같은 경로를 부른다. 거절 등급·검사 실패는 ValueError, 경로의 ApiError는 그대로."""
    _ensure_loaded()
    if not isinstance(planned, dict):
        raise ValueError("실행할 계획이 없어요")
    cap = REGISTRY.get(planned.get("capability"))
    if cap is None:
        raise ValueError(f"모르는 능력이에요: {str(planned.get('capability'))[:40]}")
    if cap.tier == "refuse":
        raise ValueError(cap.refuse or f"‘{cap.title}’은(는) 사용자가 직접 할 일이에요")
    app.store.reload_if_changed()
    again = plan(app, cap.id, planned.get("params"))
    if not again["ok"]:
        raise ValueError(again["error"] or "지금 다시 확인해 보니 실행할 수 없어요")
    params = again["params"]
    path, body = cap.build(app, params)
    track = bool(cap.undoable)
    side = _SIDE.get(cap.id) if track else None
    before = _snapshot(app.store) if track else None
    side_before = side[0](app) if side else None
    res, emitted = _call(app, path, body)
    app.store.reload_if_changed()
    record = None
    paths: list = []
    if track:
        diff = _diff(before, _snapshot(app.store))
        paths = [p for p, _, _ in diff]
        keys = {_label(p): {"path": p, "before": _enc(b), "after": _enc(a)} for p, b, a in diff}
        side_rec = None
        if side:
            side_after = side[0](app)
            if not _eq(side_before, side_after):
                side_rec = {"before": side_before, "after": side_after}
        if keys or side_rec:
            record = {"cap": cap.id, "title": again["summary"] or cap.title, "keys": keys, "at": _now_iso()}
            if side_rec:
                record["side"] = side_rec
    if paths and _touches_calendar(paths) and not emitted:
        app.emit("changed")
    msg_fn = _MESSAGES.get(cap.id)
    try:
        message = msg_fn(app, params, res) if msg_fn else None
    except Exception:  # noqa: BLE001 — 문구 만들기 실패가 실행 결과를 가리지 않게
        message = None
    if not message:
        message = f"했어요 — {again['summary'] or cap.title}"
    data_fn = _DATA.get(cap.id)
    data = data_fn(res) if data_fn and isinstance(res, dict) else None
    return {"ok": True, "message": message, "undo": record, "data": data}


def _conflict() -> ApiError:
    return ApiError(409, "실행한 뒤에 그 칸이 다시 바뀌어서 되돌리지 않았어요(지금 값이 그대로 남아요)")


def _read_record(record) -> tuple:
    if not isinstance(record, dict):
        raise ValueError("되돌릴 기록이 없어요(실행하지 않았거나 이미 되돌렸어요)")
    cap = REGISTRY.get(record.get("cap"))
    if cap is None:
        raise ValueError("모르는 능력의 기록이라 되돌릴 수 없어요")
    if not cap.undoable:
        raise ValueError(f"‘{cap.title}’은(는) 되돌릴 수 없는 일이에요")
    raw = record.get("keys") or {}
    if not isinstance(raw, dict):
        raise ValueError("되돌릴 기록의 모양이 올바르지 않아요")
    entries = []
    for e in raw.values():
        if not isinstance(e, dict) or not _valid_path(e.get("path")) or "before" not in e or "after" not in e:
            raise ValueError("되돌릴 기록의 모양이 올바르지 않아요")
        entries.append((list(e["path"]), _dec(e["before"]), _dec(e["after"])))
    side = record.get("side")
    if side is not None and (not isinstance(side, dict) or "before" not in side or "after" not in side
                             or cap.id not in _SIDE):
        raise ValueError("되돌릴 기록의 모양이 올바르지 않아요")
    return cap, entries, side


def undo(app, record: dict) -> dict:
    """run()이 돌려준 기록으로 실행 전으로 → {"ok", "message"}. 칸마다 지금 == after인지 모두 먼저 보고, 하나라도 다르면 409(아무것도 안 바꿈)."""
    _ensure_loaded()
    cap, entries, side = _read_record(record)
    if not entries and not side:
        return {"ok": True, "message": "실행 전과 같아 되돌릴 것이 없어요"}
    store = app.store
    paths = [p for p, _, _ in entries]
    try:
        with store.locked():
            store.reload_if_changed()
            for p, _, after in entries:
                if not _eq(_get(store.data, p), after):
                    raise _conflict()
            if side and not _eq(_SIDE[cap.id][0](app), side["after"]):
                raise _conflict()
            if not side:
                _restore(store, entries)
                store.write()
        if side:
            _SIDE[cap.id][1](app, side["before"])   # 경로 처리기가 스스로 잠금·저장
            with store.locked():
                store.reload_if_changed()
                if _restore(store, entries):
                    store.write()
    except TimeoutError:
        raise ApiError(409, "다른 동기화가 저장 중이에요. 잠시 뒤 다시 눌러 주세요") from None
    if paths and _touches_calendar(paths):
        app.emit("changed")
    return {"ok": True, "message": f"되돌렸어요 — {str(record.get('title') or cap.title)[:200]}"}


def _restore(store, entries: list) -> bool:
    """after 그대로인 칸만 before로(잠금 안에서). 바꾼 것이 있으면 True. manual·agent_items를 바꾸면 항목을 다시 만든다."""
    changed, refresh = False, False
    for p, before, after in entries:
        cur = _get(store.data, p)
        if _eq(cur, before) or not _eq(cur, after):
            continue
        _set(store.data, p, before)
        changed = True
        refresh = refresh or p[0] in REFRESH_TOP
    if refresh:
        from . import core
        core.refresh_items(store)
    return changed


# ================================================================ 화면 상태 읽기(저장된 것만)

def _settings(app) -> dict:
    from .actions import _settings_now
    return _settings_now(app.store)


def _raw_settings(app) -> dict:
    s = app.store.data.get("settings")
    return s if isinstance(s, dict) else {}


def _section(app, key: str):
    from .actions import _section as section
    return section(app, key)


def _onoff(v) -> str:
    return "켜짐" if v else "꺼짐"


def _view_item(app, iid: str) -> dict:
    v = next((x for x in app.store.view() if x["id"] == iid), None)
    if v is None:
        raise ValueError("그 항목을 찾을 수 없어요(동기화 뒤 다시 해 주세요)")
    return v


def _course(app, cid) -> dict:
    c = next((x for x in _course_list(app.store) if str(x.get("id")) == str(cid)), None)
    if c is None:
        raise ValueError("그 과목을 찾을 수 없어요(동기화 뒤 다시 해 주세요)")
    return c


def P(type_: str, desc: str, required: bool = True, **extra) -> dict:
    """params 한 칸."""
    spec = {"type": type_, "required": required, "desc": desc, "resolve": extra.pop("resolve", None)}
    spec.update(extra)
    return spec


def _cap(id_, area, title, route, params=None, tier="direct", build=None, preview=None, undoable=True, external=False,
         refuse=None, pane=None, message=None, side=None, data=None) -> Capability:
    cap = register(Capability(id_, area, title, route, dict(params or {}), tier, build, preview, undoable, external,
                              refuse, pane))
    if message:
        _MESSAGES[id_] = message
    if side:
        _SIDE[id_] = side
    if data:
        _DATA[id_] = data
    return cap


# ================================================================ 능력 — 설정·알림

def _toggle(key: str, label: str):
    def build(app, p):
        return "/api/settings", {key: p["value"]}

    def preview(app, p):
        return [{"label": label, "before": _onoff(_settings(app).get(key)), "after": _onoff(p["value"])}]
    return build, preview


for _id, _area, _key_name, _title, _label_text in (
    ("settings.include_canvas", "settings", "include_canvas", "Canvas 과제도 캘린더에 넣기 켜기/끄기", "Canvas 과제도 캘린더에 넣기"),
    ("settings.weekly_summary", "settings", "weekly_summary", "주간 요약 일정 켜기/끄기", "주간 요약 일정"),
    ("settings.review_reminder", "settings", "review_reminder", "확인할 후보 알림 일정 켜기/끄기", "확인할 후보 알림"),
    ("settings.update_check", "settings", "update_check", "새 버전 자동 확인 켜기/끄기", "새 버전 자동 확인"),
    ("settings.auto_update", "settings", "auto_update", "켤 때 자동 업데이트 켜기/끄기", "켤 때 자동 업데이트"),
    ("settings.tutorial_done", "settings", "tutorial_done", "튜토리얼 다 본 것으로 표시(false = 다시 보이기)", "튜토리얼 다 봄"),
    ("notify.enabled", "notify", "notify", "백그라운드 알림(신규 마감·보상) 켜기/끄기", "백그라운드 알림"),
):
    _b, _p = _toggle(_key_name, _label_text)
    _cap(_id, _area, _title, ("POST", "/api/settings"), {"value": P("bool", "켜기(true)/끄기(false)")}, build=_b, preview=_p)


def _celebrations(app) -> dict:
    """축하·소식 카드(저장하지 않고 계산만 — 상태 칸은 처음 계산을 저장하므로 직접 부른다)."""
    from . import celebrate
    from .features.core import items as core_items, semester as core_semester
    ctx = StateContext(app)
    out, _ = celebrate.state(app.store, core_semester(ctx), core_items(ctx))
    return out or {}


def _seen_ids(app) -> list:
    c = _celebrations(app)
    ids = [n.get("id") for n in c.get("news") or [] if isinstance(n, dict)]
    ids += [r.get("id") for r in c.get("pending") or [] if isinstance(r, dict)]
    return [i for i in ids if isinstance(i, str)][:200]


def _b_seen(app, p):
    ids = _seen_ids(app)
    if not ids:
        raise ValueError("지금 읽음으로 할 소식·보상 카드가 없어요")
    return "/api/celebrations/seen", {"ids": ids}


def _p_seen(app, p):
    return [{"label": "소식·보상 카드", "before": f"{len(_seen_ids(app))}개 보임", "after": "읽음"}]


def _grade_ids(app) -> list:
    out = []
    for n in _celebrations(app).get("news") or []:
        if isinstance(n, dict) and n.get("kind") == "grades":
            out += [i for i in n.get("grade_ids") or [] if isinstance(i, str)]
    return out[:200]


def _b_snooze(app, p):
    ids = _grade_ids(app)
    if not ids:
        raise ValueError("지금 미룰 성적 소식이 없어요")
    return "/api/celebrations/snooze", {"ids": ids}


def _p_snooze(app, p):
    return [{"label": "성적 소식", "before": f"{len(_grade_ids(app))}개 보임", "after": "나중에 보기(두 번 미루면 큰 카드로 안 띄움)"}]


_cap("notify.mark_seen", "notify", "소식·보상 카드 모두 읽음으로", ("POST", "/api/celebrations/seen"), build=_b_seen,
     preview=_p_seen, undoable=False,
     message=lambda app, p, res: "소식·보상 카드를 읽음으로 했어요(받은 보상은 메달함에 있어요)")
_cap("notify.snooze_grades", "notify", "성적 소식 나중에 보기", ("POST", "/api/celebrations/snooze"), build=_b_snooze,
     preview=_p_snooze)


# ================================================================ 능력 — 마감·항목

_ITEM_PARAMS = {"item": P("str", "항목(제목·번호 'LAB4'·날짜 'M/D' 또는 id)", resolve="item"),
                "course": P("str", "과목(이름·짧은 이름·코드 — 항목을 좁힘)", required=False, resolve="course")}
DECISIONS = {"approved": "승인(일정에 넣기)", "rejected": "제외", "pending": "확인 전"}


def _b_item(field_name: str):
    def build(app, p):
        _view_item(app, p["item"])
        return f"/api/item/{p['item']}", {field_name: p[field_name]}
    return build


def _p_due(app, p):
    from .timeutil import parse_local_input, iso
    v = _view_item(app, p["item"])
    if p["due"] is None:
        after = _md(v.get("orig_due")) + "(원래 마감)" if v.get("orig_due") else _md(v.get("due"))
    else:
        after = _md(iso(parse_local_input(p["due"])))
    return [{"label": f"{item_label(v)} 마감", "before": _md(v.get("due")), "after": after}]


def _p_title(app, p):
    v = _view_item(app, p["item"])
    after = p["title"] if p["title"] is not None else (v.get("orig_title") or v.get("title"))
    return [{"label": f"{item_label(v)} 제목", "before": v.get("title"), "after": after}]


def _p_decision(app, p):
    v = _view_item(app, p["item"])
    return [{"label": item_label(v), "before": DECISIONS.get(v.get("decision"), v.get("decision")),
             "after": DECISIONS[p["decision"]]}]


def _sub_text(s) -> str:
    return "제출함" if s else "제출 안 함"


def _p_submitted(app, p):
    v = _view_item(app, p["item"])
    if p["submitted"] is None:
        raw = next((it for it in app.store.data.get("items") or [] if it.get("id") == p["item"]), {})
        after = f"Canvas 값({_sub_text(raw.get('canvas_submitted'))})"
        if v.get("submitted_source") != "user":
            after = _sub_text(v.get("submitted"))
    else:
        after = _sub_text(p["submitted"])
    return [{"label": f"{item_label(v)} 제출 표시", "before": _sub_text(v.get("submitted")), "after": after}]


_cap("items.set_due", "items", "마감 날짜·시각 고치기", ("POST", "/api/item/{item_id}"),
     {**_ITEM_PARAMS, "due": P("str", "마감(로컬 시각) — null이면 원래 마감으로", nullable=True, format="datetime")},
     build=_b_item("due"), preview=_p_due)
_cap("items.set_title", "items", "항목 제목 고치기", ("POST", "/api/item/{item_id}"),
     {**_ITEM_PARAMS, "title": P("str", "제목(200자 이하) — null이면 원래 제목으로", nullable=True, max=200)},
     build=_b_item("title"), preview=_p_title)
_cap("items.set_decision", "items", "항목 승인·제외·확인 전으로", ("POST", "/api/item/{item_id}"),
     {**_ITEM_PARAMS, "decision": P("enum", "승인·제외·확인 전", enum=list(DECISIONS), labels=DECISIONS)},
     build=_b_item("decision"), preview=_p_decision)
_cap("items.set_submitted", "items", "제출함 표시 고치기", ("POST", "/api/item/{item_id}"),
     {**_ITEM_PARAMS, "submitted": P("bool", "제출함(true)/안 함(false) — null이면 Canvas 값대로", nullable=True)},
     build=_b_item("submitted"), preview=_p_submitted)


def _b_manual(app, p):
    name = _course(app, p["course"]).get("name") if p.get("course") else ""
    body = {"course_name": name or "", "text": p["text"]}
    if p.get("posted_at"):
        body["posted_at"] = p["posted_at"]
    return "/api/manual", body


def _p_manual(app, p):
    n = len(app.store.data.get("manual") or [])
    where = course_short(app, p["course"]) if p.get("course") else "과목 미상"
    snippet = p["text"][:30] + ("…" if len(p["text"]) > 30 else "")
    return [{"label": "직접 입력 글", "before": f"{n}개", "after": f"{n + 1}개({where}: ‘{snippet}’)"}]


_cap("items.add_text", "items", "직접 입력 글 더하기(글 속 마감을 찾아 후보로)", ("POST", "/api/manual"),
     {"text": P("str", "강의 글·공지 내용(마감이 적힌 문장)", max=50000),
      "course": P("str", "과목", required=False, resolve="course"),
      "posted_at": P("str", "게시일(선택)", required=False, format="date")},
     build=_b_manual, preview=_p_manual,
     message=lambda app, p, res: "직접 입력 글을 더했어요. 찾은 마감은 확인함에 후보로 떠요")


# ================================================================ 능력 — 과목

MODES = {"normal": "성적 있음", "pass_fail": "P/NP", "hidden": "제외"}


def _b_mode(app, p):
    return f"/api/course/{p['course']}/mode", {"mode": p["mode"]}


def _p_mode(app, p):
    from .store import course_mode
    return [{"label": f"{course_short(app, p['course'])} 과목 종류", "before": MODES[course_mode(app.store.data, p["course"])],
             "after": MODES[p["mode"]]}]


_cap("courses.set_mode", "courses", "과목 종류 바꾸기(성적 있음·P/NP·제외)", ("POST", "/api/course/{course_id}/mode"),
     {"course": P("str", "과목", resolve="course"), "mode": P("enum", "과목 종류", enum=list(MODES), labels=MODES)},
     build=_b_mode, preview=_p_mode)


def _purposes() -> dict:
    from .maildraft import PURPOSE_LABEL
    return dict(PURPOSE_LABEL)


def _b_mail(app, p):
    if not p.get("course") and not p.get("item"):
        raise ValueError("어느 과목(course)이나 항목(item)에 대한 메일인지 알려 주세요")
    body = {"purpose": p["purpose"]}
    if p.get("course"):
        body["course_id"] = p["course"]
    if p.get("item"):
        body["item_id"] = p["item"]
    if p.get("note"):
        body["note"] = p["note"]
    return "/api/mail/draft", body


def _m_mail(app, p, res):
    d = (res or {}).get("draft") or {}
    to = ", ".join(f"{r.get('email')}({r.get('role')})" for r in d.get("to") or [] if isinstance(r, dict)) or "못 찾음(직접 넣어 주세요)"
    return (f"메일 초안을 만들었어요(보내지 않았어요 — 확인한 뒤 직접 보내 주세요).\n받는 사람: {to}\n제목: {d.get('subject')}\n\n"
            f"{d.get('body')}")


_cap("courses.mail_draft", "courses", "교수님·조교 문의 메일 초안 만들기(보내지 않음)", ("POST", "/api/mail/draft"),
     {"purpose": P("enum", "목적", enum=list(_purposes()), labels=_purposes()),
      "course": P("str", "과목", required=False, resolve="course"),
      "item": P("str", "항목(선택)", required=False, resolve="item"),
      "note": P("str", "덧붙일 말(선택)", required=False, max=1000)},
     build=_b_mail, undoable=False, message=_m_mail, data=lambda res: res.get("draft"))


# ================================================================ 능력 — 첨부 자료

def _b_auto_dl(app, p):
    return "/api/materials/settings", {"auto_download": p["value"]}


def _p_auto_dl(app, p):
    return [{"label": "신규 첨부 자동으로 받기", "before": _onoff(_raw_settings(app).get("auto_download", True)),
             "after": _onoff(p["value"])}]


def _folder_now(app) -> str:
    from .features.materials import folder_of
    return str(folder_of(app))


def _b_folder(app, p):
    if app.demo:
        raise ValueError("데모에서는 저장 폴더를 바꿀 수 없어요")
    f = p["folder"]
    if f is not None:
        if "\x00" in f or not Path(f).expanduser().is_absolute():
            raise ValueError("저장 폴더는 C:\\… 처럼 전체 경로로 적어 주세요")
        if Path(f).expanduser().is_file():
            raise ValueError("그 경로는 폴더가 아니라 파일이에요")
    return "/api/materials/settings", {"download_dir": f}


def _p_folder(app, p):
    from . import materials
    before = _folder_now(app) + ("" if _raw_settings(app).get("download_dir") else "(기본)")
    after = str(Path(p["folder"]).expanduser()) if p["folder"] else f"{materials.default_folder()}(기본)"
    return [{"label": "첨부 저장 폴더", "before": before, "after": after}]


def _b_open(app, p):
    return "/api/materials/open", ({"key": p["file"]} if p.get("file") else {})


def _m_open(app, p, res):
    if not p.get("file"):
        return "자료 폴더를 열었어요"
    m = (app.store.data.get("downloads") or {}).get(p["file"]) or {}
    return f"‘{m.get('filename') or '파일'}’을(를) 열었어요"


_cap("materials.auto_download", "materials", "신규 첨부 자동으로 받기 켜기/끄기", ("POST", "/api/materials/settings"),
     {"value": P("bool", "켜기(true)/끄기(false)")}, build=_b_auto_dl, preview=_p_auto_dl)
_cap("materials.set_folder", "materials", "첨부 저장 폴더 바꾸기", ("POST", "/api/materials/settings"),
     {"folder": P("str", "폴더 전체 경로 — null이면 기본(문서\\LMS Agent)", nullable=True, max=400)},
     build=_b_folder, preview=_p_folder)
_cap("materials.open", "materials", "자료 폴더(또는 받은 파일) 열기", ("POST", "/api/materials/open"),
     {"file": P("str", "받은 파일 이름(선택 — 없으면 폴더)", required=False, resolve="download")},
     build=_b_open, undoable=False, message=_m_open)


# ================================================================ 능력 — 앱(동기화·백그라운드·자동 실행·업데이트·캐릭터)

INTERVALS = [1, 3, 6, 12, 24]


def _b_sync(app, p):
    from .config import is_configured
    if not is_configured(app.cfg):
        raise ValueError("먼저 Canvas에 로그인해 주세요(설정 → 계정)")
    if getattr(app, "syncing", False):
        raise ValueError("이미 동기화하는 중이에요. 끝나면 다시 해 주세요")
    return "/api/sync", ({"full": True} if p.get("full") else {})


def _m_sync(app, p, res):
    rep = (res or {}).get("report") or {}
    head = "전체를 다시 확인했어요" if p.get("full") else "동기화했어요"
    return f"{head}(확인할 후보 {rep.get('needs_review', 0)}개)"


_cap("app.sync", "app", "지금 동기화(전체 다시 확인 선택)", ("POST", "/api/sync"),
     {"full": P("bool", "전체 다시 확인", required=False)}, build=_b_sync, undoable=False, message=_m_sync)


def _bg_now(app) -> dict:
    st = _section(app, "background") or {}
    on = bool(st.get("enabled"))
    return {"enabled": on, "interval_hours": st.get("interval_hours") if on else None}


def _bg_text(st: dict) -> str:
    return f"켜짐({st.get('interval_hours')}시간마다)" if st.get("enabled") else "꺼짐"


def _b_bg(app, p):
    if not getattr(app, "background_supported", False):
        raise ValueError("백그라운드 동기화는 Windows에서만 쓸 수 있어요(데모에서는 꺼져 있어요)")
    body = {"enabled": p["enabled"]}
    if p["enabled"] and p.get("interval_hours"):
        body["interval_hours"] = p["interval_hours"]
    return "/api/background", body


def _p_bg(app, p):
    now = _bg_now(app)
    hours = p.get("interval_hours") or (_section(app, "background") or {}).get("interval_hours") or 3
    after = {"enabled": p["enabled"], "interval_hours": hours}
    return [{"label": "백그라운드 동기화", "before": _bg_text(now), "after": _bg_text(after)}]


def _bg_restore(app, st):
    body = {"enabled": bool((st or {}).get("enabled"))}
    if body["enabled"] and (st or {}).get("interval_hours") in INTERVALS:
        body["interval_hours"] = st["interval_hours"]
    _call(app, "/api/background", body)


_cap("app.background", "app", "백그라운드 동기화 켜기/끄기·간격", ("POST", "/api/background"),
     {"enabled": P("bool", "켜기/끄기"), "interval_hours": P("enum", "간격(시간, 선택)", required=False, enum=INTERVALS)},
     build=_b_bg, preview=_p_bg, side=(_bg_now, _bg_restore),
     message=lambda app, p, res: (f"백그라운드 동기화를 켰어요({_bg_now(app).get('interval_hours')}시간마다)" if p["enabled"]
                                  else "백그라운드 동기화를 껐어요"))


def _auto_now(app) -> dict:
    st = _section(app, "autostart") or {}
    return {"enabled": bool(st.get("enabled")), "open_browser": st.get("open_browser", True) is not False}


def _auto_text(st: dict) -> str:
    if not st.get("enabled"):
        return "꺼짐"
    return "켜짐" + ("(브라우저도 열기)" if st.get("open_browser") else "(브라우저는 열지 않음)")


def _b_auto(app, p):
    if not getattr(app, "autostart_supported", False):
        raise ValueError("이 PC에서는 '컴퓨터를 켜면 앱 열기'를 쓸 수 없어요(데모에서는 꺼져 있어요)")
    body = {"enabled": p["enabled"]}
    if p.get("open_browser") is not None:
        body["open_browser"] = p["open_browser"]
    return "/api/autostart", body


def _p_auto(app, p):
    now = _auto_now(app)
    ob = p["open_browser"] if p.get("open_browser") is not None else now["open_browser"]
    return [{"label": "컴퓨터를 켜면 앱 열기", "before": _auto_text(now), "after": _auto_text({"enabled": p["enabled"], "open_browser": ob})}]


def _auto_restore(app, st):
    _call(app, "/api/autostart", {"enabled": bool((st or {}).get("enabled")),
                                  "open_browser": (st or {}).get("open_browser", True) is not False})


_cap("app.autostart", "app", "컴퓨터를 켜면 앱 열기(OS 등록) 켜기/끄기", ("POST", "/api/autostart"),
     {"enabled": P("bool", "켜기/끄기"), "open_browser": P("bool", "브라우저도 열기(선택)", required=False)},
     tier="confirm", build=_b_auto, preview=_p_auto, side=(_auto_now, _auto_restore),
     message=lambda app, p, res: ("컴퓨터를 켜면 앱이 열리게 했어요. 다음 로그인부터 적용돼요" if p["enabled"]
                                  else "컴퓨터를 켜도 앱이 저절로 열리지 않게 했어요"))


def _m_update_check(app, p, res):
    u = (res or {}).get("update") or {}
    if u.get("available"):
        return f"새 버전 v{u.get('latest')}이(가) 있어요(지금 v{u.get('current')}). '지금 업데이트'라고 하면 확인 카드를 띄울게요"
    if u.get("error"):
        return f"확인하지 못했어요: {u['error']}"
    return f"지금 쓰는 v{u.get('current')}이(가) 최신이에요"


def _b_update_apply(app, p):
    if app.demo:
        raise ValueError("데모에서는 업데이트하지 않아요. 실제 앱에서 해 주세요")
    u = app.updater
    if not u.available():
        raise ValueError("지금 쓰는 버전이 최신이에요(먼저 새 버전을 확인해 보세요)")
    pl = u.plan()
    if not pl.get("can_apply"):
        raise ValueError(pl.get("reason") or "이 설치 방식은 앱 안에서 업데이트할 수 없어요")
    return "/api/update/apply", {}


def _p_update_apply(app, p):
    st = app.updater.status(demo=app.demo)
    return [{"label": "앱 버전", "before": f"v{st.get('current')}", "after": f"v{st.get('latest')}"}]


def _b_update_restart(app, p):
    if app.demo:
        raise ValueError("데모에서는 다시 시작하지 않아요")
    u = app.updater
    if not (u.result and u.result.get("ok") and u.result.get("restart")) or u.phase != "idle":
        raise ValueError("다시 시작할 업데이트가 없어요")
    return "/api/update/restart", {}


def _b_const(path: str, body: dict | None = None):
    def build(app, p):
        return path, dict(body or {})
    return build


_cap("app.update_check", "app", "새 버전 확인", ("POST", "/api/update/check"), build=_b_const("/api/update/check"),
     undoable=False, message=_m_update_check)
_cap("app.update_apply", "app", "지금 업데이트(받아서 바꾸고 다시 켬)", ("POST", "/api/update/apply"), tier="confirm",
     build=_b_update_apply, preview=_p_update_apply, undoable=False,
     message=lambda app, p, res: "업데이트를 시작했어요. 끝나면 앱이 다시 켜져요(pip·git 설치면 설정 → 업데이트의 [다시 시작])")
_cap("app.update_restart", "app", "업데이트 뒤 다시 시작", ("POST", "/api/update/restart"), tier="confirm",
     build=_b_update_restart, undoable=False, message=lambda app, p, res: "다시 시작하고 있어요. 잠시 뒤 화면이 다시 연결돼요")
_cap("app.update_dismiss", "app", "업데이트 알림 닫기", ("POST", "/api/update/dismiss"),
     build=_b_const("/api/update/dismiss"), undoable=False, message=lambda app, p, res: "업데이트 알림을 닫았어요")
_cap("app.quit", "app", "앱 끄기", ("POST", "/api/app/quit"), tier="confirm", build=_b_const("/api/app/quit"),
     undoable=False, message=lambda app, p, res: (res or {}).get("message") or "앱을 껐어요")

MASCOT_CHOICES = {"auto": "고려대 호이·쿠이(자동)", "original": "오리지널 페이스메이커"}


def _p_mascot(app, p):
    c = (app.store.data.get("mascot") or {}).get("choice")
    return [{"label": "캐릭터", "before": MASCOT_CHOICES.get(c if c in MASCOT_CHOICES else "auto"),
             "after": MASCOT_CHOICES[p["choice"]]}]


_cap("app.mascot", "app", "캐릭터 바꾸기", ("POST", "/api/mascot/map"),
     {"choice": P("enum", "캐릭터", enum=list(MASCOT_CHOICES), labels=MASCOT_CHOICES)},
     build=lambda app, p: ("/api/mascot/map", {"choice": p["choice"]}), preview=_p_mascot)
_cap("app.mascot_folder", "app", "내 캐릭터 그림 폴더 열기", ("POST", "/api/mascot/open-folder"),
     build=_b_const("/api/mascot/open-folder"), undoable=False, message=lambda app, p, res: "캐릭터 폴더를 열었어요")


# ================================================================ 능력 — 구글 캘린더

def _google_ready(app, read: bool = False) -> None:
    g = getattr(app, "google", None)
    if not (g and g.available and g.connected):
        raise ValueError("구글 캘린더가 연결돼 있지 않아요. 설정 → 구글 캘린더에서 먼저 직접 연결해 주세요")
    if read and not g.read_granted:
        raise ValueError("다른 일정을 읽을 권한이 아직 없어요. 설정 → 구글 캘린더에서 직접 허락해 주세요(구글 동의는 사용자가 해요)")


def _b_push(app, p):
    _google_ready(app)
    return "/api/google/push", {}


def _p_push(app, p):
    st = app.google.status(app.store)
    want = st.get("desired") or {}
    return [{"label": "구글 'LMS 마감' 캘린더",
             "before": f"마지막 반영 {_md(st.get('last_push')) if st.get('last_push') else '없음'}",
             "after": f"지금 마감 {want.get('deadlines', 0)}개·요약 {want.get('summaries', 0)}개로 맞춤"}]


def _m_push(app, p, res):
    r = ((res or {}).get("google") or {}).get("last_result") or {}
    return (f"구글 캘린더에 반영했어요(추가 {r.get('created') or 0} · 바뀜 {r.get('updated') or 0} · 지움 {r.get('deleted') or 0}). "
            "일정은 앱 상태로 다시 만들어지므로 따로 되돌리지 않아요")


_cap("calendar.push", "calendar", "구글 캘린더에 지금 반영", ("POST", "/api/google/push"), build=_b_push, preview=_p_push,
     undoable=False, external=True, message=_m_push)


def _gread_now(app) -> bool:
    from . import gevents
    return gevents.enabled(app.store)


def _b_gread(app, p):
    if p["enabled"]:
        _google_ready(app, read=True)
    return "/api/google/read", {"enabled": p["enabled"]}


def _p_gread(app, p):
    return [{"label": "구글 캘린더의 다른 일정 보기", "before": _onoff(_gread_now(app)), "after": _onoff(p["enabled"])}]


def _gread_restore(app, on):
    if on:
        try:
            _google_ready(app, read=True)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
    res, _ = _call(app, "/api/google/read", {"enabled": bool(on)})
    if isinstance(res, dict) and res.get("need_consent"):
        raise ApiError(400, "다른 일정을 읽을 권한이 아직 없어요. 설정 → 구글 캘린더에서 직접 허락해 주세요")


def _m_gread(app, p, res):
    if isinstance(res, dict) and res.get("need_consent"):
        return "다른 일정을 읽을 권한이 아직 없어요. 설정 → 구글 캘린더에서 직접 허락해 주세요"
    return ("구글 캘린더의 다른 일정을 대시보드에 보여 줄게요(가져오는 중)" if p["enabled"]
            else "구글 캘린더의 다른 일정 보기를 껐어요(가져온 일정은 이 PC에서 지웠어요)")


_cap("calendar.read", "calendar", "구글 캘린더의 다른 일정 보기 켜기/끄기", ("POST", "/api/google/read"),
     {"enabled": P("bool", "켜기/끄기")}, build=_b_gread, preview=_p_gread, side=(_gread_now, _gread_restore),
     message=_m_gread)


def _need_reading(app) -> None:
    if not _gread_now(app):
        raise ValueError("먼저 '구글 캘린더의 다른 일정 보기'를 켜 주세요")
    _google_ready(app, read=True)


def _gsel_now(app) -> list:
    from . import gevents
    return gevents.selected_ids(app.store)


def _cal_names(app, ids) -> str:
    from . import gevents
    names = {c["id"]: c.get("name") or c["id"] for c in gevents.calendars(app.store)}
    return ", ".join(str(names.get(i, i)) for i in ids) or "없음"


def _b_gsel(app, p):
    _need_reading(app)
    return "/api/google/read/calendars", {"selected": list(p["calendars"])}


def _p_gsel(app, p):
    return [{"label": "대시보드에 보일 캘린더", "before": _cal_names(app, _gsel_now(app)), "after": _cal_names(app, p["calendars"])}]


def _gsel_restore(app, ids):
    _call(app, "/api/google/read/calendars", {"selected": list(ids or [])})


_cap("calendar.choose", "calendar", "대시보드에 보일 구글 캘린더 고르기", ("POST", "/api/google/read/calendars"),
     {"calendars": P("list", "보일 캘린더(이름이나 id 목록 — 전체를 이것으로)", resolve="calendar", items="str", max=50)},
     build=_b_gsel, preview=_p_gsel, side=(_gsel_now, _gsel_restore))


def _b_grefresh(app, p):
    _need_reading(app)
    return "/api/google/read/refresh", {}


_cap("calendar.refresh", "calendar", "구글 캘린더의 다른 일정 다시 가져오기", ("POST", "/api/google/read/refresh"),
     build=_b_grefresh, undoable=False, message=lambda app, p, res: "구글 캘린더의 다른 일정을 다시 가져오고 있어요")


# ================================================================ 능력 — 공부 계획

def _plan_body(app, p) -> dict:
    from . import plan as planmod
    if p.get("blocks"):
        blocks = [{k: b[k] for k in ("start", "end", "title", "course_id", "item_id") if k in b} for b in p["blocks"]]
        title = p.get("title") or (f"{course_short(app, p['course'])} 공부 계획" if p.get("course") else "공부 계획")
        body = {"title": title, "blocks": blocks}
        if p.get("course"):
            body["course_id"] = p["course"]
        if p.get("item"):
            body["item_id"] = p["item"]
    else:
        kw = {k: p[k] for k in ("days", "minutes", "hour") if p.get(k) is not None}
        made = planmod.make(app.store, course_id=p.get("course"), item_id=p.get("item"), **kw)
        if not made.get("blocks"):
            raise ValueError(" ".join(str(n) for n in made.get("notes") or []) or "공부 계획을 짜지 못했어요")
        body = {"title": p.get("title") or made.get("title") or "공부 계획",
                "blocks": [{k: b[k] for k in ("start", "end", "title") if k in b} for b in made["blocks"]
                           if isinstance(b, dict)]}
        for k in ("course_id", "item_id"):
            if made.get(k) is not None:
                body[k] = made[k]
    body["decided_by"] = "user"   # 사용자가 시킨 일(계약 28-0)
    planmod.validate(app.store, body)   # 지난 시각·겹침·길이 — ValueError(한국어)
    return body


def _b_plan_apply(app, p):
    return "/api/plan/apply", _plan_body(app, p)


def _p_plan_apply(app, p):
    from .timeutil import parse_iso
    body = _plan_body(app, p)
    n = len(_plans(app))
    first = min((parse_iso(b["start"]) for b in body["blocks"] if parse_iso(b.get("start"))), default=None)
    when = f" · 첫 칸 {first.month}/{first.day} {first.hour:02d}:{first.minute:02d}" if first else ""
    return [{"label": "공부 계획", "before": f"{n}개",
             "after": f"{n + 1}개(‘{body['title']}’ {len(body['blocks'])}칸{when})"}]


def _m_plan_apply(app, p, res):
    pid = (res or {}).get("plan_id")
    got = next((x for x in _plans(app) if x["id"] == pid), None)
    if got:
        return f"공부 계획 ‘{got.get('title')}’을(를) 캘린더에 넣었어요({len(got.get('blocks') or [])}칸)"
    return "공부 계획을 캘린더에 넣었어요"


_cap("plan.apply", "plan", "공부 계획 캘린더에 넣기(블록을 주거나 앱이 짬)", ("POST", "/api/plan/apply"),
     {"course": P("str", "과목(선택)", required=False, resolve="course"),
      "item": P("str", "시험·과제 항목(선택)", required=False, resolve="item"),
      "title": P("str", "계획 이름(선택)", required=False, max=120),
      "blocks": P("list", "공부 칸 [{start, end, title?}](선택 — 없으면 앱이 짬)", required=False, items="dict", max=30),
      "days": P("int", "며칠 동안(선택)", required=False, min=1, max=60),
      "minutes": P("int", "하루 몇 분(선택)", required=False, min=15, max=240),
      "hour": P("int", "시작 시(선택, 0~23)", required=False, min=0, max=23)},
     build=_b_plan_apply, preview=_p_plan_apply, message=_m_plan_apply)


def _p_plan_remove(app, p):
    got = next((x for x in _plans(app) if x["id"] == p["plan"]), None) or {}
    return [{"label": "공부 계획", "before": f"‘{got.get('title')}’ {len(got.get('blocks') or [])}칸", "after": "지움"}]


_cap("plan.remove", "plan", "공부 계획 지우기", ("POST", "/api/plan/remove"),
     {"plan": P("str", "지울 계획(이름 또는 id)", resolve="plan")},
     build=lambda app, p: ("/api/plan/remove", {"id": p["plan"]}), preview=_p_plan_remove,
     message=lambda app, p, res: "공부 계획을 지웠어요(구글 캘린더의 그 일정도 다음 반영 때 지워져요)")


# ================================================================ 능력 — 체크리스트

def _row(app, iid: str, eid: str) -> dict:
    return next((r for r in _checklist_rows(app, iid) if r["id"] == eid), {})


def _b_check_set(app, p):
    return f"/api/checklist/{p['item']}", {"op": "toggle", "id": p["entry"], "done": p["done"]}


def _p_check_set(app, p):
    r = _row(app, p["item"], p["entry"])
    return [{"label": f"체크리스트 ‘{r.get('text')}’", "before": "함" if r.get("done") else "안 함",
             "after": "함" if p["done"] else "안 함"}]


def _b_check_add(app, p):
    from .checklist import clean_text
    _view_item(app, p["item"])
    return f"/api/checklist/{p['item']}", {"op": "add", "text": clean_text(p["text"])}


def _p_check_add(app, p):
    n = len(_checklist_rows(app, p["item"]))
    return [{"label": f"{item_label(_view_item(app, p['item']))} 체크리스트", "before": f"{n}칸",
             "after": f"{n + 1}칸(+ ‘{p['text'][:40]}’)"}]


def _p_check_remove(app, p):
    r = _row(app, p["item"], p["entry"])
    return [{"label": f"체크리스트 ‘{r.get('text')}’", "before": "있음", "after": "지움"}]


_ENTRY = P("str", "체크리스트 칸(내용 일부 또는 id)", resolve="checklist")
_cap("checklist.set", "checklist", "체크리스트 칸 체크·해제", ("POST", "/api/checklist/{item_id}"),
     {**_ITEM_PARAMS, "entry": _ENTRY, "done": P("bool", "체크(true)/해제(false)")}, build=_b_check_set, preview=_p_check_set)
_cap("checklist.add", "checklist", "체크리스트에 칸 더하기", ("POST", "/api/checklist/{item_id}"),
     {**_ITEM_PARAMS, "text": P("str", "더할 칸 내용", max=200)}, build=_b_check_add, preview=_p_check_add)
_cap("checklist.remove", "checklist", "체크리스트 칸 지우기", ("POST", "/api/checklist/{item_id}"),
     {**_ITEM_PARAMS, "entry": _ENTRY},
     build=lambda app, p: (f"/api/checklist/{p['item']}", {"op": "remove", "id": p["entry"]}), preview=_p_check_remove)


# ================================================================ 능력 — 코치 카드

CARDS = {"briefing": "브리핑 카드", "retro": "회고 카드", "report": "학기 리포트"}


def _card(app, kind: str) -> dict:
    card = (_section(app, "coach") or {}).get(kind)
    if not isinstance(card, dict) or not card.get("id"):
        raise ValueError(f"지금 닫을 {CARDS[kind]}가 없어요")
    return card


_cap("coach.dismiss", "coach", "코치 카드 닫기(브리핑·회고는 그 주, 리포트는 그 학기)", ("POST", "/api/coach/dismiss"),
     {"card": P("enum", "카드", enum=list(CARDS), labels=CARDS)},
     build=lambda app, p: ("/api/coach/dismiss", {"id": _card(app, p["card"])["id"]}),
     preview=lambda app, p: [{"label": f"{CARDS[p['card']]} ‘{_card(app, p['card']).get('title') or ''}’",
                              "before": "보임", "after": "닫음"}])


# ================================================================ 거절(사용자가 직접) — 실행하지 않고 그 설정 칸을 열어 준다

for _id, _area, _title, _route, _pane, _why in (
    ("ai.enable", "ai", "AI 켜기·끄기", "/api/ai/settings", "ai",
     "AI 켜기·끄기는 에이전트가 자기 행동을 켜고 끄는 일이라 하지 않아요. 설정 → AI 도움에서 직접 바꿔 주세요."),
    ("ai.auto_triage", "ai", "동기화 뒤 AI 자동 정리 켜기·끄기", "/api/ai/settings", "ai",
     "AI 자동 정리 켜기·끄기도 AI 설정이라 에이전트가 하지 않아요. 설정 → AI 도움에서 직접 바꿔 주세요."),
    ("ai.provider", "ai", "AI 실행기·모델 바꾸기", "/api/ai/provider", "ai",
     "AI 실행기·모델 바꾸기는 에이전트 자신을 바꾸는 일이라 하지 않아요. 설정 → AI 도움에서 직접 골라 주세요."),
    ("ai.daily_limit", "ai", "하루 AI 사용 한도 바꾸기", "/api/settings", "ai",
     "하루 AI 사용 한도는 에이전트가 자기 한도를 바꾸지 않게 직접 정해 주세요(설정 → AI 도움)."),
    ("account.login", "account", "Canvas 토큰·로그인", "/api/account/login", "account",
     "Canvas 토큰·로그인은 개인정보라 에이전트가 하지 않아요. 설정 → 계정에서 직접 해 주세요."),
    ("account.logout", "account", "로그아웃", "/api/account/logout", "account",
     "로그아웃은 계정에 관한 일이라 에이전트가 하지 않아요. 설정 → 계정에서 직접 해 주세요."),
    ("account.wipe_data", "account", "이 PC의 데이터 지우기", "/api/account/logout", "account",
     "데이터 지우기는 되돌릴 수 없어 에이전트가 하지 않아요. 설정 → 계정(로그아웃할 때 기록까지 지우기)에서 직접 해 주세요."),
    ("account.student_id", "account", "학번 넣기·바꾸기", "/api/grades/settings", "grades",
     "학번은 개인정보라 에이전트가 넣거나 바꾸지 않아요. 설정 → 성적에서 직접 넣어 주세요."),
    ("calendar.connect", "calendar", "구글 캘린더 연결", "/api/google/connect", "google",
     "구글 연결은 구글 동의 화면에서 직접 허락해야 해요. 설정 → 구글 캘린더에서 연결해 주세요."),
    ("calendar.disconnect", "calendar", "구글 연결 해제", "/api/google/disconnect", "google",
     "구글 연결 해제는 에이전트가 하지 않아요. 설정 → 구글 캘린더에서 직접 해 주세요."),
):
    _cap(_id, _area, _title, ("POST", _route), tier="refuse", undoable=False, refuse=_why, pane=_pane)


EXCLUDED_ROUTES.update({
    "/api/agent/ask": "에이전트와의 대화 자체(묻기)",
    "/api/agent/apply": "에이전트 제안의 [실행] — 능력을 부르는 쪽",
    "/api/agent/undo": "에이전트 제안의 [되돌리기] — 능력을 부르는 쪽",
    "/api/agent/clear": "대화 기록 지우기 — 대화 창의 동작",
    "/api/setup": "예전 첫 연결 API(Canvas 토큰을 받음 — account.login처럼 사용자가 직접)",
    "/api/plan/preview": "미리 보기만(저장 안 함) — plan.apply가 블록이 없으면 안에서 짠다",
    "/api/ai/rescan": "AI 실행기 다시 찾기 — AI 설정 화면의 동작(사용자가 직접)",
    "/api/ai/test": "AI 연결 시험 — AI 설정 화면의 동작(사용자가 직접)",
    "/api/ai/triage": "AI를 다시 부르는 확인함 정리 — 에이전트가 AI라 items.set_decision으로 직접 정한다",
})


# ================================================================ 다른 모듈의 능력

_loaded = False


def _load_extensions() -> None:
    """다른 모듈이 더하는 능력(성적 — capabilities_grades.register_all)."""
    try:
        from . import capabilities_grades
        capabilities_grades.register_all()
    except ImportError:
        pass


def _ensure_loaded() -> None:
    """성적 능력을 처음 쓸 때 한 번 불러온다(서로 import하므로 모듈 끝이 아니라 처음 쓸 때)."""
    global _loaded
    if _loaded:
        return
    try:
        _load_extensions()
    except AttributeError:   # 그 모듈이 아직 다 불러와지지 않음(서로 import 중) — 다음에 다시
        return
    _loaded = True


try:   # REGISTRY를 바로 읽는 쪽도 성적 능력을 보게(실패하면 처음 쓸 때 다시 — 그때는 오류가 그대로 보인다)
    _ensure_loaded()
except Exception:  # noqa: BLE001
    pass
