"""에이전트 v2 — 도구를 쓰는 AI 루프(계약 26-1·26-2·26-4, v0.8.0, 담당 A).

사용자: "뭔가를 요청하면 일단 문서를 무조건 탐색한다", "잘못 기입된 일정·성적 제거를 요청했는데 다른 소리부터 한다" →
AI가 먼저 판단하고 필요할 때만 도구를 쓴다(docs/AGENT_V2.md).

흐름(run): 메시지 + 이전 대화 + 지금 상태 요약(과목·모든 항목 id·평가 항목 이름·비중 — 점수는 get_grades로만) → AI 작업 `agent`
  (agent_cli) 한 차례 → {"say", "calls": [{"tool", "args"}], "done", "citations"?}. 읽기 도구는 하네스가 바로 실행해 결과를 다음 차례
  입력에 붙이고(가림, 12,000자 안), 바꾸기 도구(propose_*)는 실행하지 않고 agent.check_proposal로 검사한 제안 카드만 만든다
  (지우기·고치기 넷은 agent_fix.check로 감). 최대 MAX_ROUNDS(3)차례 + 검사 실패 다시 묻기 1번 = agent.PER_ASK(4).
차례마다의 검사(Loop.validate): 마지막 답(say) 1~1,500자, 인용(citations)은 이번 묻기의 도구 결과에 그대로, '했어요·지웠어요'는 바로
  반영할 제안(auto_targets)이 있을 때만, 학점·'N점'은 whatif·get_grades 결과나 인용한 문장(또는 학생 말)에 있는 것만, say의 날짜는
  도구 결과·상태·제안에 있는 것만. 틀리면 오류를 인용해 다시 묻고(한 묻기에 한 번), 또 틀리면 실패 → agent.answer가 v0.7.3 갈래로 답한다.
바로 반영(26-4, 사용자 결정 B): AI가 auto를 냈고, 학생 말이 시키는 말(is_imperative)이고, 바꾸는 제안이 하나뿐이며 대상이 분명하고
  (_ambiguous), 검사를 통과했고, 종류가 AUTO_TYPES면 제안에 auto: true. 실제 반영은 agent.run_job이 답을 앱 Store에 합친 뒤 같은
  잠금 안에서 agent.apply로 하고 auto_applied: true를 붙인다(되돌리기는 지금처럼). 앱 동작(action)·업데이트는 바로 하지 않는다.
진행 표시: steps [{"round", "tool", "label", "ok", "note"}], progress(문구 — "마감 목록 보는 중…")는 progress 콜백으로 차례 사이마다.
AI에게 보내지 않는 것: 토큰·비밀값, 다른 학생 정보(점수표는 내 값·반 집계만), 구글 캘린더 개인 일정(gcal_events — 어떤 도구도 읽지
  않음), 이메일·학번·이름(agent_cli.redact로 가림).
말로 시킨 일(계약 28-3, v0.8.3): list_capabilities(area?)(읽기 — capabilities.catalog_text)와 act(capability, params, reason?, auto?)
  (제안 — agent.act_proposal → capabilities.plan: 검사·이름→id·전→후, 바꾸지 않음). 학생의 지시가 권한이라 근거 글이 필요 없다.
  plan이 막히면(모르는 이름·여럿이 맞음 등) 그 이유를 다음 차례 도구 결과로 AI에게 돌려준다(done이었어도 차례가 남았으면 이어 감).
  바로 하기: 등급 direct + 시키는 말(is_command — 바꿔줘·반영해줘·켜줘 …, 묻는 말 아님) + 이번 묻기의 바꾸기 제안 하나 + AI가
  auto: false를 내지 않음(애매한 지시를 구체적인 값으로 바꾼 것은 확인 카드) → auto. 실제 실행은 agent.run_job이 잠금 밖에서.
  confirm 등급은 [실행] 카드, refuse 등급(AI 켜기·끄기·한도·개인정보)은 거절 카드 — AI가 act 없이 '못 해요'라고만 해도 시킨 말이
  그런 일이면 하네스가 거절 카드(agent.refusal_for, [설정 열기])를 붙인다.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta

from . import agent, agent_cli
from .agent_cli import (_CLAIM_DONE, _GRADE_CLAIM, _POINTS, _Invalid, _num_norm, _num_tokens, _quoted, original_quote,
                        redact, unplace)
from .extract import find_dates
from .store import course_mode
from .timeutil import now_local, parse_iso

MAX_ROUNDS = 3
MAX_CALLS = agent_cli.AGENT_MAX_CALLS
SEARCH_TOOL_LIMIT = agent.SEARCH_LIMIT
LIST_MAX = 40
DETAIL_ITEMS = 12         # 상태 요약에 자세히 적는 과목별 항목 수(나머지는 id·제목·상태만)
OTHER_ITEMS = 60
RESULTS_KEEP = 16
SAY_MAX = 1_500
INVALID_PREFIX = "AI 답이 검사를 통과하지 못했습니다"
AUTO_TYPES = ("item_remove", "item_merge", "component_remove", "score_clear", "fix_due", "link_score", "set_count",
              "add_deadline")
EVIDENCE_TYPES = ("add_deadline", "fix_due", "link_score", "set_count", "set_weights", "set_dist")
FIX_TYPES = ("item_remove", "item_merge", "component_remove", "score_clear")
_WD = "월화수목금토일"

# 바꾸기 도구 → (제안 type, 받는 칸). 모든 propose의 args에 auto(bool) — 계약 26-2
PROPOSE = {
    "propose_item_remove": ("item_remove", ("item_id", "reason")),
    "propose_item_merge": ("item_merge", ("keep_id", "drop_id", "reason")),
    "propose_component_remove": ("component_remove", ("course_id", "component", "merge_into", "reason")),
    "propose_score_clear": ("score_clear", ("course_id", "component", "which", "key", "reason")),
    "propose_add_deadline": ("add_deadline", ("course_id", "title", "due", "kind", "method", "evidence", "source_key",
                                              "reason")),
    "propose_fix_due": ("fix_due", ("course_id", "item_id", "due", "evidence", "source_key", "reason")),
    "propose_link_score": ("link_score", ("course_id", "key", "component", "evidence", "source_key", "reason")),
    "propose_count": ("set_count", ("course_id", "component", "n", "evidence", "source_key", "reason")),
    "propose_weights": ("set_weights", ("course_id", "components", "evidence", "source_key", "reason")),
    "propose_dist": ("set_dist", ("course_id", "groups", "dist_kind", "cutoffs", "evidence", "source_key", "reason")),
    "propose_action": ("action", ("action", "params", "reason", "title")),
    "act": ("act", ("capability", "params", "reason", "auto")),   # 말로 시킨 일(계약 28-3) — 근거 글 없음
}
assert set(PROPOSE) == set(agent_cli.AGENT_PROPOSE_TOOLS)

# 도구 → (단계 이름, 진행 문구) — 화면의 단계 목록과 '…하는 중'
LABELS = {
    "search_texts": ("글 찾기", "공지·글 찾는 중…"),
    "list_items": ("마감 목록 보기", "마감 목록 보는 중…"),
    "get_item": ("항목 자세히 보기", "항목 보는 중…"),
    "get_grades": ("성적 보기", "성적 보는 중…"),
    "whatif": ("성적 계산", "성적 계산하는 중…"),
    "list_materials": ("자료 목록 보기", "자료 목록 보는 중…"),
    "get_settings": ("설정 보기", "설정 보는 중…"),
    "list_capabilities": ("할 수 있는 일 보기", "할 수 있는 일 보는 중…"),
    "act": ("직접 하기", "말한 일 확인하는 중…"),
    "draft_mail": ("메일 초안 쓰기", "메일 초안 쓰는 중…"),
    "make_plan": ("공부 계획 짜기", "공부 계획 짜는 중…"),
    "propose_item_remove": ("항목 빼기 제안", "제안 검사 중…"),
    "propose_item_merge": ("항목 합치기 제안", "제안 검사 중…"),
    "propose_component_remove": ("평가 항목 지우기 제안", "제안 검사 중…"),
    "propose_score_clear": ("점수 떼기 제안", "제안 검사 중…"),
    "propose_add_deadline": ("마감 추가 제안", "제안 검사 중…"),
    "propose_fix_due": ("마감 고치기 제안", "제안 검사 중…"),
    "propose_link_score": ("점수 연결 제안", "제안 검사 중…"),
    "propose_count": ("개수 제안", "제안 검사 중…"),
    "propose_weights": ("평가 비중 제안", "제안 검사 중…"),
    "propose_dist": ("학점 비율 제안", "제안 검사 중…"),
    "propose_action": ("앱 동작 제안", "제안 검사 중…"),
}
ASK_FIRST = "AI에게 묻는 중…"
ASK_AGAIN = "AI가 결과를 읽는 중…"
ACT_NOTE = {"direct": "바로 할 수 있음", "confirm": "확인 카드", "refuse": "하지 않는 일"}

# 시키는 말(계약 26-4): 지워·없애·삭제·빼·합쳐·고쳐·바꿔·추가·넣어·등록·연결·remove·delete·fix … — 조사·띄어쓰기와 상관없이,
# '지워야 할까?'·'삭제해도 돼?'·'빼고'·'왜 지웠어?'처럼 묻는 말·다른 뜻은 아니다(QUESTION이 이긴다).
_VERB = r"(?:지워|없애|빼|떼어|떼|합쳐|고쳐|바꿔|넣어|끊어|옮겨|(?:삭제|제거|추가|등록|연결|수정|변경|제외|정리|병합|해제)\s*(?:좀\s*)?해)"
IMPERATIVE = re.compile(
    _VERB + r"\s*(?:좀\s*)?(?:줘요|줘|주세요|주라|줄래요?|주실래요?|주시겠어요|주십시오|봐|버려|놔|둬|라|요)?(?![가-힣])"
    r"|(?:지우|합치|고치|바꾸|넣|없애|(?:삭제|제거|추가|등록|연결|수정|변경|제외|정리)\s*하)(?:자|세요|십시오)(?![가-힣])"
    r"|(?:삭제|제거|추가|등록|정리)\s*(?:부탁|요청)"
    r"|(?:삭제|제거|추가|등록)\s*[.!~]*\s*$"
    r"|(?<![a-z])(?:remove|delete|fix|merge|add|change|correct|link|unlink|drop|clear)(?![a-z])", re.I)
QUESTION = re.compile(
    r"왜|어떻게|무엇|뭐|언제|어디|할까|해야|돼야|될까|되나|나요|을까|ㄹ까|인가|가능|수\s*있|괜찮|맞아|맞나|"
    r"(?<![a-z])(?:should|why|how|what|whether)(?![a-z])|^\s*(?:is|are|do|does|did|was|were|will)(?![a-z])", re.I)
# '했어요'류(계약 26-1 — 반영한 제안이 없으면 '제안했어요'로): agent_cli._CLAIM_DONE + 지우기·고치기 말
_MORE_DONE = re.compile(
    r"(?:합쳤|없앴|고쳤|뺐|뗐|떼어\s*냈|연결했|정리했|수정했|변경했|제거했|병합했|삭제됐|지워졌|반영됐|제외됐)"
    r"(?:어요|습니다|어|다|음)(?![가-힣])(?!\s*[?？])", re.I)


def _flat(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _s(v, n: int = 300) -> str:
    return _flat(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _norm(s) -> str:
    """이름 비교용: 소문자, 공백·기호를 뺀 글자('LAB 2' = 'lab2')."""
    return re.sub(r"[^0-9a-z가-힣]+", "", str(s or "").lower())


def is_imperative(message: str) -> bool:
    """학생 말이 분명히 시키는 말인지(바로 반영 조건, 계약 26-4)."""
    msg = _flat(message)
    return bool(msg and IMPERATIVE.search(msg) and not QUESTION.search(msg))


def is_command(message: str) -> bool:
    """말로 시킨 일(act)을 바로 할 조건(계약 28-3): 시키는 말 — is_imperative의 말에 더해 '반영해줘·켜줘·꺼줘·동기화해줘·
    40%로 해줘'처럼 '…줘/주세요'로 시키는 말(actions._is_command) — 이고 묻는 말이 아님('바꿔도 돼?'·'바꿀 수 있어?'는 카드)."""
    from . import actions
    msg = _flat(message)
    if not msg or QUESTION.search(msg):
        return False
    return bool(IMPERATIVE.search(msg) or actions._is_command(msg))


def claims_done(say: str):
    """'지웠어요·추가했어요·켰어요'처럼 앱이 무언가를 했다는 말(묻는 말은 아님)."""
    return _CLAIM_DONE.search(say or "") or _MORE_DONE.search(say or "")


class _ToolError(Exception):
    """도구 인자가 틀림(모르는 id 등) — 결과 글로 AI에게 알린다(다시 묻기 아님)."""


# ---------------------------------------------------------------- 바로 반영 조건(26-4)

def _same_task(a: dict, b: dict) -> bool:
    """같은 과제의 후보(묶음·중복·연결·대체)인지."""
    if a.get("id") == b.get("id"):
        return True
    if a.get("group") and a.get("group") == b.get("group"):
        return True
    refs = ("duplicate_of", "linked_to", "replaced_by")
    return b.get("id") in [a.get(k) for k in refs] or a.get("id") in [b.get(k) for k in refs]


def _named_items(ctx, text: str) -> list[dict]:
    """글에 제목이 나온 항목들(제외한 것 빼고) — 'LAB 2'는 'LAB2를'에도 맞고 'LAB20'에는 안 맞는다."""
    hay = _norm(text)
    out = []
    for v in ctx.items:
        if v.get("status") == "rejected":
            continue
        t = _norm(v.get("title"))
        if len(t) >= 2 and re.search(re.escape(t) + r"(?!\d)", hay):
            out.append(v)
    return out


def _ambiguous(ctx, texts: tuple[str, str], p: dict) -> str | None:
    """대상이 하나로 정해지지 않으면 그 이유(한국어). texts = (이번 말, 앞 차례 말·답)."""
    typ = p.get("type")
    now_text, prev_text = texts
    if typ in ("item_remove", "fix_due", "item_merge"):
        ids = [p.get("keep_id"), p.get("drop_id")] if typ == "item_merge" else [p.get("item_id")]
        targets = [ctx.by_id.get(str(i)) for i in ids]
        if any(t is None for t in targets):
            return "대상 항목을 찾지 못해서"
        named = _named_items(ctx, now_text) or _named_items(ctx, prev_text)
        said = set(agent.mentioned_courses(now_text, list(ctx.courses.values())))
        if said:   # 과목을 말했으면('자료구조 Quiz 1') 그 과목의 항목만 본다
            named = [v for v in named if str(v.get("course_id")) in said]
        if not any(_same_task(v, t) for v in named for t in targets):
            return "말한 제목과 대상 항목이 같은지 분명하지 않아서"
        if any(not any(_same_task(v, t) for t in targets) for v in named):
            return "같은 이름(또는 여러 이름)의 항목이 여럿이라"
        return None
    if typ in ("component_remove", "score_clear"):
        name = _norm(p.get("component"))
        hay = _norm(now_text) or ""
        if not name or (name not in hay and name not in _norm(prev_text)):
            return "말한 평가 항목 이름이 분명하지 않아서"
        if not agent.mentioned_courses(now_text, list(ctx.courses.values())):
            same = [cid for cid in ctx.courses if any(_norm(c.get("name")) == name for c in ctx.components(cid))]
            if len(same) > 1:
                return "같은 이름의 평가 항목이 여러 과목에 있어서"
        return None
    return None


def auto_targets(ctx, message: str, texts: tuple[str, str], proposals: list[dict]) -> tuple[set, dict]:
    """바로 반영할 제안 번호(최대 하나)와, AI가 auto를 냈지만 바로 반영하지 않는 이유 {번호: 이유}."""
    want = [i for i, p in enumerate(proposals) if p.get("_auto")]
    if not want:
        return set(), {}
    # 말로 시킨 일(act, 계약 28-3)은 '반영해줘·켜줘'도 시키는 말(is_command), 그 밖은 지금처럼 is_imperative
    told = {i for i in want if (is_command(message) if proposals[i].get("type") == "act" else is_imperative(message))}
    if not told:
        return set(), {i: ("분명히 시킨 말(바꿔줘·반영해줘·켜줘…)일 때만 바로 해요" if proposals[i].get("type") == "act"
                           else "분명히 시킨 말(지워줘·고쳐줘·추가해줘…)일 때만 바로 반영해요") for i in want}
    changes = [i for i, p in enumerate(proposals)
               if not p.get("rejected_reason") and p.get("type") not in ("note", "action") and agent.can_apply(p)]
    ok = [i for i in want if i in changes and i in told
          and (proposals[i].get("type") in AUTO_TYPES
               or (proposals[i].get("type") == "act" and proposals[i].get("tier") == "direct"))]
    reasons = {i: "검사를 통과하지 못했거나 바로 반영하는 종류가 아니라서" for i in want if i not in ok}
    if len(changes) != 1 or len(ok) != 1:
        return set(), {i: reasons.get(i) or "바꿀 대상이 여럿이라" for i in want}
    i = ok[0]
    why = _ambiguous(ctx, texts, proposals[i])
    if why:
        return set(), {**reasons, i: why}
    return {i}, reasons


# ---------------------------------------------------------------- 지금 상태 요약(AI 첫 입력)

def _due_key(v: dict):
    d = parse_iso(v.get("due"))
    return (d is None, d.timestamp() if d else 0, str(v.get("title") or ""))


def item_line(ctx, v: dict, course: bool = False) -> str:
    """항목 한 줄: '- item_id X | 제목 | 종류 | due ISO | 상태' + 출처·중복·연결·평가 항목(mock_ai도 같은 모양을 읽는다)."""
    title = _flat(v.get("title")).replace("|", "/")[:120]
    row = (f"- item_id {v['id']} | {title} | {v.get('kind') or '-'} | due {v.get('due') or '없음/none'} | "
           f"{v.get('status') or '-'}")
    if course:
        cid = str(v.get("course_id"))
        row += f" | course_id {cid} · {ctx.shorts.get(cid) or '-'}"
    row += f" | origin {v.get('origin') or '-'}"
    for k in ("duplicate_of", "linked_to", "replaced_by"):
        if v.get(k):
            row += f" | {k} {v[k]}"
    if v.get("component"):
        row += f" | 평가 항목/component: {v['component']}"
    if v.get("submitted"):
        row += " | 제출함/submitted"
    ev = (v.get("evidence") or [{}])[0] if v.get("evidence") else {}
    if course and ev.get("source_title"):
        row += f" | 출처/source: {_flat(ev.get('source_title'))[:60]}"
    return row


# 상태 요약을 예산(agent_cli.AGENT_STATE_BUDGET) 안에 넣는 단계: (자세히 적는 항목 수, 나머지 항목 제목 길이 — 0이면 id만)
_LEVELS = ((DETAIL_ITEMS, 30), (6, 16), (3, 0), (0, 0))


def state_summary(ctx, budget: int | None = None) -> str:
    """과목마다 다가오는 마감(자세히)과 모든 항목 id, 평가 항목 이름·비중·일정. 점수·점수 후보·구글 개인 일정은 넣지 않는다.
    예산을 넘으면 자세히 적는 항목을 줄이고 나머지는 id만 남긴다(모든 과목·항목 id가 들어가게)."""
    budget = agent_cli.AGENT_STATE_BUDGET if budget is None else budget
    text = ""
    for detail, title_len in _LEVELS:
        text = _state(ctx, detail, title_len)
        if len(text) <= budget:
            break
    return text


def _state(ctx, detail: int, title_len: int) -> str:
    sem = ctx.sem or {}
    now = ctx.now
    lines = [f"학기 / term: {sem.get('week_now')}주차 / {sem.get('weeks_total')}주",
             f"오늘 / today: {now:%Y-%m-%d}({_WD[now.weekday()]}) {now:%H:%M}"]
    floor = now - timedelta(days=7)
    for cid in ctx.courses:
        mode = course_mode(ctx.store.data, cid)
        lines.append(f"\n## course_id {cid} · {ctx.shorts.get(cid)}" + (" (P/NP)" if mode == "pass_fail" else ""))
        mine = sorted([v for v in ctx.items if str(v.get("course_id")) == cid], key=_due_key)
        live = [v for v in mine if v.get("status") not in ("rejected", "replaced")
                and (parse_iso(v.get("due")) is None or parse_iso(v.get("due")) >= floor)]
        if not mine:
            lines.append("항목 / items: 없음 / none")
        else:
            lines.append(f"항목 / items ({len(mine)}개 — 자세한 것은 list_items·get_item):")
            lines += [item_line(ctx, v) for v in live[:detail]]
            more = [v for v in mine if v not in live[:detail]]
            if more and title_len:
                lines.append("그 밖의 항목 / other items (item_id: 제목·상태): " + "; ".join(
                    f"{v['id']}: {_flat(v.get('title')).replace('|', '/')[:title_len]}·{v.get('status')}"
                    for v in more[:OTHER_ITEMS])
                    + (f"; …외 {len(more) - OTHER_ITEMS}개" if len(more) > OTHER_ITEMS else ""))
            elif more:
                lines.append("그 밖의 항목 id / other item ids: " + ", ".join(v["id"] for v in more))
        comps = ctx.components(cid)
        if comps:
            lines.append("평가 항목 / grading components (점수는 get_grades):")
            for c in comps:
                lines.append(f"- {c.get('name')} {float(c.get('weight') or 0):g}% | 일정/dates: "
                             f"{agent._date_status(ctx, cid, str(c.get('name')))}")
        else:
            lines.append("평가 항목 / grading components: 없음(비중을 못 찾음) / none found")
    return "\n".join(lines)


# ---------------------------------------------------------------- 루프

def _day(v) -> date | None:
    if not isinstance(v, str) or not v.strip():
        return None
    d = parse_iso(v.strip())
    if d is not None:
        return d.date()
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


def _clip(v):
    if isinstance(v, str):
        return v[:600]
    if isinstance(v, (list, dict)):
        return copy.deepcopy(v)
    return v


_MSG_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(obj, out: set | None = None) -> set:
    """params 안의 숫자 값(int·float만 — id·이름 같은 글자는 빼고) → '50'·'92.5' 같은 글자 모양."""
    out = set() if out is None else out
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(f"{float(obj):g}")
    elif isinstance(obj, dict):   # 글자 값(과목·항목 id, 이름)은 세지 않는다 — 바꿀 '값'만
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, out)
    return out


def _explicit_numbers(params, message: str) -> bool:
    """바꿀 값에 숫자가 있고 그 숫자가 모두 학생의 말에 있으면 True(학생이 값을 직접 말함)."""
    nums = _numbers(params if isinstance(params, (dict, list)) else {})
    said = {f"{float(x):g}" for x in _MSG_NUM.findall(str(message or ""))}
    return bool(nums) and nums <= said


class Loop:
    """한 번 묻기의 루프 상태: 도구 결과·단계·제안·찾은 글·원문 조각."""

    def __init__(self, store, message: str, eff: str | None, course_id, history, names, session, now, progress,
                 intent, app=None):
        self.store, self.message = store, message
        self.app = agent.PlanApp(app, store)   # act의 capabilities.plan이 읽는 앱(store는 이 작업의 사본)
        self.eff = eff or message
        self.course_id = str(course_id) if course_id is not None else None
        self.history = list(history or [])
        self.names = names
        self.session = session
        self.now = now or now_local()
        self.progress = progress
        self.intent = intent
        self.ctx = agent.Ctx(store)
        if self.course_id not in self.ctx.courses:
            self.course_id = None
        self.state_text = state_summary(self.ctx)
        self.tool_results: list[dict] = []
        self.steps: list[dict] = []
        self.kept: list[dict] = []
        self.results: list[dict] = []
        self.searched = 0
        self.originals: list[str] = []
        self.grade_texts: list[str] = []
        self.extras: dict = {}
        self.used: set = set()
        self.notes: list[str] = []
        self.attempts = 0
        self.retry_left = True
        self._sec = None
        last = self.history[-1] if self.history else {}
        self.naming = (_flat(f"{message} {self.eff}"), _flat(f"{last.get('message') or ''} {last.get('answer') or ''}"))

    # ------------------------------------------------------------ 진행·입력

    def _tell(self, text: str | None) -> None:
        if callable(self.progress):
            try:
                self.progress(text, copy.deepcopy(self.steps))
            except Exception:  # noqa: BLE001 — 진행 표시가 고장 나도 답은 만든다
                pass

    def _inputs(self, rnd: int) -> dict:
        return {"message": self.message, "followup": self.eff if _flat(self.eff) != _flat(self.message) else None,
                "names": self.names,
                "course_context": {"id": self.course_id, "short": self.ctx.shorts.get(self.course_id)}
                if self.course_id else None,
                "courses": [{"id": cid, "short": self.ctx.shorts[cid]} for cid in self.ctx.courses],
                "history": [{"message": h.get("message"), "answer": h.get("answer"), "kind": h.get("kind")}
                            for h in self.history],
                "state_text": self.state_text, "tool_results": self.tool_results, "round": rnd,
                "max_rounds": MAX_ROUNDS, "final": rnd >= MAX_ROUNDS, "validate": self.validate}

    # ------------------------------------------------------------ 실행

    def go(self) -> dict:
        for rnd in range(1, MAX_ROUNDS + 1):
            self._tell(ASK_FIRST if rnd == 1 else ASK_AGAIN)
            res = self.session.run("agent", self._inputs(rnd), max_attempts=2 if self.retry_left else 1)
            self.attempts += res.attempts
            self.notes += [n for n in res.notes if n not in self.notes]
            if res.attempts > 1:
                self.retry_left = False
            if not res.ok:
                failure = "invalid" if str(res.error or "").startswith(INVALID_PREFIX) else "cli"
                return {"ok": False, "error": res.error or "AI가 답하지 못했어요", "failure": failure,
                        "steps": self.steps, "attempts": self.attempts}
            data = res.data
            self._take(rnd, data)
            if data["final"]:
                return self._done(rnd, data)
        return {"ok": False, "error": "차례 한도", "failure": "invalid", "steps": self.steps, "attempts": self.attempts}

    def _take(self, rnd: int, data: dict) -> None:
        """검사를 통과한 차례: 제안 단계를 남기고, 읽기 도구를 실행해 결과를 다음 차례에 붙인다."""
        for call in data["calls"]:
            tool, args = call["tool"], call["args"]
            label, doing = LABELS.get(tool, (tool, "도구 쓰는 중…"))
            if tool in PROPOSE:
                props = call["props"]
                bad = [p for p in props if p.get("rejected_reason")]
                note = ("거절: " + _flat(bad[0]["rejected_reason"])[:60]) if bad else ("통과" if props else "없음")
                if tool == "act" and props:   # '직접 하기 · 학점 비율 바꾸기' — 등급대로 짧게
                    p = props[0]
                    label = f"{label} · {_flat(p.get('title'))[:40]}" if p.get("title") and not bad else label
                    note = note if bad else ACT_NOTE.get(p.get("tier"), "통과")
                self.steps.append({"round": rnd, "tool": tool, "label": label, "ok": bool(props) and not bad,
                                   "note": note})
                self.tool_results.append({"round": rnd, "tool": tool, "args": _short_args(args),
                                          "text": "\n".join(_prop_text(p) for p in props) or "제안 없음 / none"})
                continue
            self._tell(doing)
            try:
                text, note = getattr(self, "t_" + tool)(args)
                ok = True
            except _ToolError as e:
                text, note, ok = f"오류 / error: {e}", _flat(e)[:60], False
            except Exception as e:  # noqa: BLE001 — 도구 하나가 고장 나도 루프는 계속(내용·비밀값 없이)
                text, note, ok = f"오류 / error: 도구를 실행하지 못했어요({type(e).__name__})", "실행 못 함", False
            self.used.add(tool)
            self.tool_results.append({"round": rnd, "tool": tool, "args": _short_args(args), "text": text})
            self.steps.append({"round": rnd, "tool": tool, "label": label, "ok": ok, "note": note})
        self.kept += data["kept"]

    def _done(self, rnd: int, data: dict) -> dict:
        props = self.kept
        auto, why = auto_targets(self.ctx, self.message, self.naming, props)
        for i, p in enumerate(props):
            wanted = bool(p.pop("_auto", False))
            p["auto"] = i in auto
            p["auto_applied"] = False
            if wanted and i not in auto:
                button = "[실행]" if p.get("type") == "act" else "[반영]"
                p["notes"] = list(p.get("notes") or []) + [f"바로 반영하지 않았어요({why.get(i) or '조건이 맞지 않아서'}) "
                                                           f"— 맞으면 {button}을 눌러 주세요"]
            elif p.get("type") == "act" and p.get("tier") == "direct" and not wanted and not p.get("rejected_reason"):
                p["notes"] = list(p.get("notes") or []) + ["말한 것을 이렇게 알아들었어요 — 전→후가 맞으면 [실행]을 눌러 주세요"]
        # 하지 않는 일(AI 켜기·끄기·한도·개인정보)을 시켰는데 AI가 '못 해요'라고만 했으면 [설정 열기] 거절 카드를 붙인다(계약 28-3)
        if all(p.get("type") == "note" for p in props):
            ref = agent.refusal_for(self.store, self.message, by="ai")
            if ref is not None:
                props.append(ref)
        if rnd >= MAX_ROUNDS and not data["done"]:
            self.notes.append("도구는 한 번 묻기에 3차례까지만 써요 — 여기까지 찾은 것으로 답했어요")
        kind = self._kind(props)
        out = {"kind": kind, "answer": data["say"], "proposals": props, "citations": self._citations(data["citations"]),
               "results": self.results[:RESULTS_KEEP], "searched": self.searched, "by": "ai", "error": None,
               "attempts": self.attempts, "notes": list(self.notes), "steps": self.steps}
        for k in ("whatif", "draft", "plan"):
            if k in self.extras:
                out[k] = self.extras[k]
        if kind == "chat":
            out["intent"] = self.intent or "other"
        if agent.CRISIS_WORDS.search(self.message or ""):
            out["answer"] = agent._ensure_help(out["answer"])
        self._tell(None)
        return {"ok": True, "out": out, "steps": self.steps, "attempts": self.attempts}

    def _kind(self, props: list[dict]) -> str:
        """차례 kind(화면이 그리는 모양 — 계약 22-1): 지우기·고치기 fix, 말로 시킨 일만 act, 다른 바꾸기 missing, 앱 동작 action, 메일 mail, 계획 plan,
        만약에 whatif, 글을 찾았으면 그 말의 갈래(missing|question), 다른 읽기만 했으면 question, 도구 없이 답했으면 chat."""
        types = {p.get("type") for p in props}
        if types & set(FIX_TYPES):
            return "fix"
        if "act" in types and not types - {"act", "action", "note"}:   # 말로 시킨 일만(계약 28-3)
            return "act"
        if types - {"action", "note"}:
            return "missing"
        if "action" in types:
            return "action"
        for k, kind in (("draft", "mail"), ("plan", "plan"), ("whatif", "whatif")):
            if k in self.extras:
                return kind
        if "search_texts" in self.used:
            return agent.classify(self.eff)
        return "question" if self.used else "chat"

    def _citations(self, good: list[dict]) -> list[dict]:
        """검사를 통과한 인용(가린 글) → 화면용 인용(원문 문장 + 글 정보, 계약 22-2 모양)."""
        out = []
        for c in good:
            q = c["quote"]
            order = ([r for r in self.results if r.get("source_key") == c["source_key"]]
                     + [r for r in self.results if r.get("source_key") != c["source_key"]])
            home = next((r for r in order if q in _flat(redact(str(r.get("snippet") or ""), self.names)[0])), None)
            if home is not None:
                cite = agent.citation(home, original_quote(q, [{"text": home.get("snippet") or ""}], self.names))
            else:   # 검색 결과가 아닌 도구 결과(항목 근거 등)의 문장
                cite = {"source_key": c["source_key"] or None, "course_id": None, "course_short": None, "type": None,
                        "title": None, "posted_at": None, "url": None,
                        "quote": agent._original(q, self.originals, self.names)[:agent.QUOTE_MAX]}
            if not any(x["source_key"] == cite["source_key"] and x["quote"] == cite["quote"] for x in out):
                out.append(cite)
        return out

    # ------------------------------------------------------------ 차례 검사(agent_cli._agent_validate가 부른다)

    def validate(self, obj: dict, inputs: dict, final_attempt: bool) -> dict:
        rnd = int(inputs.get("round") or 1)
        say = _flat(unplace(str(obj.get("say") or "")))
        calls = [c for c in obj.get("calls") or [] if isinstance(c, dict)][:MAX_CALLS]
        done = bool(obj.get("done"))
        if not calls and not done:
            if not say:
                raise _Invalid("calls도 say도 없어요 — 도구를 부르거나 done: true와 say로 답하세요 "
                               "/ call tools or answer with done true")
            done = True
        if len(say) > SAY_MAX:
            raise _Invalid(f"say는 {SAY_MAX:,}자 이하로 / say must be at most {SAY_MAX} characters")
        out_calls, now_props, errs, act_errs = [], [], [], 0
        for i, c in enumerate(calls):
            tool = str(c.get("tool") or "")
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            rec = {"tool": tool, "args": args, "props": []}
            if tool in PROPOSE:
                rec["props"] = self._propose(tool, args, inputs)
                bad = [f"calls[{i}] {tool}: {p['rejected_reason']}" for p in rec["props"] if p.get("rejected_reason")]
                errs += bad
                act_errs += len(bad) if tool == "act" else 0
                now_props += rec["props"]
            out_calls.append(rec)
        # act가 막혔으면(모르는 이름·후보 여럿 등, 계약 28-3) done이었어도 차례가 남았으면 이어 간다 — 막힌 이유가 다음 차례의
        # 도구 결과로 AI에게 간다(다시 묻기 한 번을 쓰지 않음)
        if act_errs and done and rnd < MAX_ROUNDS:
            done = False
        final = done or rnd >= MAX_ROUNDS
        if final and not say:
            raise _Invalid("마지막 답(say)이 비어 있어요 — done: true와 한국어 say로 답하세요 / say is required")
        if errs and final and not final_attempt:
            raise _Invalid("; ".join(errs[:6]))
        kept = now_props if final else [p for p in now_props if not p.get("rejected_reason")]
        good: list[dict] = []
        if final:
            good = self._check_citations(obj.get("citations") or [], inputs)
            allp = self.kept + kept
            auto, _ = auto_targets(self.ctx, self.message, self.naming, allp)
            claim = claims_done(say)
            if claim and not auto:
                raise _Invalid(f"바로 반영한 제안이 없는데 했다고 하면 안 돼요('{claim.group(0)}') — '제안했어요·[반영]을 누르면 "
                               "돼요'처럼 말하세요(auto는 분명히 시킨 말에 대상이 하나일 때만) / never claim a change was made")
            bad = self._grade_claims(say, good)
            if bad:
                raise _Invalid("성적 숫자·학점은 whatif(또는 get_grades) 결과나 인용한 문장에 있는 것만 쓰세요: " + ", ".join(bad[:5])
                               + " / grade letters and scores must come from the whatif tool")
            bad = self._unsupported_dates(say, inputs, allp)
            if bad:
                raise _Invalid("say의 날짜가 도구 결과·지금 상태에 없어요: " + ", ".join(bad[:5])
                               + " / every date in say must appear in the tool results or the state")
        return {"say": say, "done": done, "final": final, "calls": out_calls, "kept": kept, "citations": good}

    def _propose(self, tool: str, args: dict, inputs: dict) -> list[dict]:
        """propose_* 하나 → 검사한 제안들(실행하지 않음). 근거가 필요한 종류는 근거가 이번 도구 결과·상태 글에 그대로 있어야 한다."""
        typ, keys = PROPOSE[tool]
        if typ == "act":
            return [self._act(args)]
        if typ == "action" and not _s(args.get("action")):
            return self._action_from_message(args)
        raw: dict = {"type": typ}
        for k in keys:
            if k in args and args[k] is not None:
                raw[k] = _clip(args[k])
        if typ == "action":
            raw["params"] = copy.deepcopy(args["params"]) if isinstance(args.get("params"), dict) else {}
        want = args.get("auto") is True and typ in AUTO_TYPES
        if typ in FIX_TYPES:
            raw["auto"] = want
        why = None
        if typ in EVIDENCE_TYPES:
            ev = _flat(raw.get("evidence"))
            if not _quoted(ev, inputs.get("_tool_texts") or "", inputs.get("_state_sent") or ""):
                why = ("근거(evidence)가 이번 묻기의 도구 결과에 그대로 없어요 — search_texts 등으로 찾은 문장을 그대로 "
                       "/ evidence is not verbatim in this ask's tool results")
        q = agent.check_proposal(self.ctx, raw, "ai", why, self.originals, self.names)
        if typ in FIX_TYPES:   # 다시 검사(agent.apply)가 같은 칸을 읽게
            for k in keys:
                if k != "reason" and q.get(k) in (None, "") and raw.get(k) not in (None, ""):
                    q[k] = raw[k]
        q["_auto"] = want
        return [q]

    def _act(self, args: dict) -> dict:
        """act(capability, params, reason?, auto?) → 제안(계약 28-3). 학생의 말이 권한이라 근거 글을 보지 않는다(이유가 없으면 학생의 말).
        AI가 auto: false를 냈으면(애매한 지시를 구체적인 값으로 바꾼 것) 바로 하지 않고 [실행] 카드. 등급 direct·검사 통과만 바로 할 후보."""
        reason = _s(args.get("reason"), 300) or f"요청: “{_flat(self.message)[:80]}”"
        p = agent.act_proposal(self.app, args.get("capability"), args.get("params"), reason=reason, by="ai")
        # AI의 auto: false는 'AI가 지어낸 값이 있을 때'만 따른다(리드, 실제 AI 확인: 'A 50%로 바꿔줘'를 카드로 냄) —
        # 바꿀 값의 숫자가 모두 학생의 말에 있으면 분명히 시킨 것이라 바로 할 후보
        ai_auto = args.get("auto") is not False or _explicit_numbers(args.get("params"), self.message)
        p["_auto"] = ai_auto and p.get("tier") == "direct" and not p.get("rejected_reason")
        if not p.get("rejected_reason"):   # 답의 학점·날짜 검사가 미리 보기(전→후)의 값을 쓸 수 있게
            self.grade_texts.append(" ".join([_flat(p.get("summary"))] + [
                f"{c.get('label')} {c.get('before')} {c.get('after')}" for c in p.get("changes") or []]))
        return p

    def _action_from_message(self, args: dict) -> list[dict]:
        """propose_action에 action이 없으면 앱의 규칙(actions.handle)이 학생 말에서 동작을 고른다."""
        from . import actions
        try:
            res = actions.handle(self.store, self.message, self.course_id)
        except Exception:  # noqa: BLE001
            res = None
        raws = [p for p in (res or {}).get("proposals") or [] if isinstance(p, dict)]
        why = _s(args.get("reason")) or None
        if not raws:
            return [agent.check_action(self.store, {"type": "action", "action": "", "reason": why or "-"}, by="ai",
                                       why="어떤 앱 동작인지 알아듣지 못했어요 — '앱 동작' 목록의 action·params로 / unknown action",
                                       shorts=self.ctx.shorts)]
        out = []
        for p in raws[:3]:
            q = agent.check_action(self.store, {**p, "reason": why or p.get("reason") or "학생이 시킨 앱 동작"}, by="ai",
                                   shorts=self.ctx.shorts)
            q["_auto"] = False
            out.append(q)
        return out

    def _check_citations(self, cits: list, inputs: dict) -> list[dict]:
        texts = _flat(inputs.get("_tool_texts") or "")
        good, problems = [], []
        for i, c in enumerate(cits[:4]):
            if not isinstance(c, dict):
                continue
            q = _flat(str(c.get("quote") or ""))
            if len(q) < agent_cli.ANSWER_QUOTE_MIN:
                problems.append(f"citations[{i}]: 인용이 너무 짧아요 / quote is too short")
            elif q not in texts:
                problems.append(f"citations[{i}]: quote가 이번 묻기의 도구 결과에 그대로 없어요 — 한 글자도 바꾸지 말고 "
                                "복사하세요 / quote is not verbatim in this ask's tool results")
            elif not any(g["quote"] == q for g in good):
                good.append({"source_key": _s(c.get("source_key"), 200), "quote": q[:agent_cli.ANSWER_QUOTE_MAX]})
        if problems:
            raise _Invalid("; ".join(problems))
        return good

    def _grade_claims(self, say: str, good: list[dict]) -> list[str]:
        allowed = _flat(" ".join(self.grade_texts + [c["quote"] for c in good] + [self.message, self.eff]))
        squeezed = allowed.replace(" ", "")
        bad = [m.group(0) for m in _GRADE_CLAIM.finditer(say) if m.group(0).replace(" ", "") not in squeezed]
        nums = _num_tokens(allowed)
        bad += [m.group(0) for m in _POINTS.finditer(say) if _num_norm(m.group(1)) not in nums]
        return list(dict.fromkeys(bad))

    def _unsupported_dates(self, say: str, inputs: dict, props: list[dict]) -> list[str]:
        allowed = " ".join([inputs.get("_tool_texts") or "", inputs.get("_state_sent") or "", self.message, self.eff]
                           + self.grade_texts + [str(p.get("due") or "") for p in props])
        today = self.now.date()
        days = {(today.month, today.day)} | {(h.day.month, h.day.day) for h in find_dates(allowed, today)}
        return list(dict.fromkeys(_flat(say[h.start:h.end]) for h in find_dates(say, today)
                                  if not h.relative and (h.day.month, h.day.day) not in days))

    # ------------------------------------------------------------ 읽기 도구(26-2) — 기존 코드를 감싼다

    def _course(self, value, required: bool = False) -> str | None:
        if value in (None, ""):
            if required:
                if self.course_id:
                    return self.course_id
                raise _ToolError("course_id가 필요해요 / course_id is required")
            return None
        cid = str(value)
        if cid not in self.ctx.courses:
            raise _ToolError("과목 목록에 없는 course_id예요(제외한 과목 포함) / unknown or hidden course_id")
        return cid

    def _section(self) -> dict:
        if self._sec is None:
            from . import whatif
            self._sec, _ = whatif._context(self.store, self.now)
        return self._sec

    def t_search_texts(self, args: dict):
        query = _s(args.get("query"), 200) or self.eff
        cid = self._course(args.get("course_id"))
        results, n = agent._search(self.store, query, cid, limit=SEARCH_TOOL_LIMIT, items=self.ctx.items)
        self.searched = max(self.searched, n)
        blocks = []
        for r in results:
            if not any(x["source_key"] == r["source_key"] and x["snippet"] == r["snippet"] for x in self.results):
                self.results.append(r)
            self.originals.append(r["snippet"])
            blocks.append(f"### source_key: {r['source_key']}\ncourse_id: {r.get('course_id') or '-'} | 종류/type: "
                          f"{r.get('type')} | 게시/posted: {str(r.get('posted_at') or '')[:10]} | 제목/title: "
                          f"{_flat(r.get('title'))[:120]}\n{r['snippet']}")
        if not blocks:
            return f"(이 PC의 글 {n}개에서 찾은 문단 없음 / nothing found)", "0개"
        return "\n".join(blocks), f"{len(results)}개"

    def t_list_items(self, args: dict):
        cid = self._course(args.get("course_id"))
        status = _s(args.get("status"), 20)
        frm, to = _day(args.get("from")), _day(args.get("to"))
        query = _s(args.get("query"), 100)
        qn, qt = _norm(query), [t.lower() for t in agent.tokens(query)]
        rows = []
        for v in self.ctx.items:
            if cid is not None and str(v.get("course_id")) != cid:
                continue
            if status and v.get("status") != status:
                continue
            d = parse_iso(v.get("due"))
            if (frm or to) and d is None:
                continue
            if (frm and d.date() < frm) or (to and d.date() > to):
                continue
            if query:
                title = str(v.get("title") or "")
                if not ((qn and qn in _norm(title)) or any(t in title.lower() for t in qt)):
                    continue
            rows.append(v)
        rows.sort(key=_due_key)
        head = f"{len(rows)}개" + (f" 중 {LIST_MAX}개" if len(rows) > LIST_MAX else "")
        # 과목을 좁혔는데(이어받은 과목 등) 학생 말에 나온 제목이 다른 과목에 있으면 함께 알려 준다(리드 — 데모 확인: 'LAB 6 일정 지워줘'가
        # 앞 대화의 과목에서만 찾다가 '못 찾음'이 됨. 검색(_search)이 이어받은 과목에 없으면 모든 과목에서 다시 찾는 것과 같은 뜻)
        seen = {v["id"] for v in rows}
        others = [v for v in _named_items(self.ctx, self.message)
                  if cid is not None and v["id"] not in seen and str(v.get("course_id")) != cid][:LIST_MAX]
        if not rows and not others:
            return "항목 없음 / no items", "0개"
        body = "\n".join(item_line(self.ctx, v, course=True) for v in rows[:LIST_MAX]) or "(이 과목에는 없음 / none in this course)"
        if others:
            head += f" + 다른 과목 {len(others)}개"
            body += ("\n학생 말에 나온 제목의 다른 과목 항목 / items in other courses named in the message:\n"
                     + "\n".join(item_line(self.ctx, v, course=True) for v in others))
        return f"항목 {head} / items:\n" + body, head

    def t_get_item(self, args: dict):
        iid = _s(args.get("item_id"), 160)
        v = self.ctx.by_id.get(iid)
        if v is None:
            raise _ToolError("항목 목록에 없는 item_id예요(제외한 과목의 항목은 볼 수 없어요) / unknown item_id")
        lines = [item_line(self.ctx, v, course=True),
                 f"결정 / decision: {v.get('decision')} (by {v.get('decided_by') or '-'})"
                 + (f" — 이유/reason: {_flat(v.get('ai_reason'))[:200]}" if v.get("ai_reason") else "")]
        if v.get("edited"):
            lines.append(f"학생이 고침 / edited (원래 제목/orig title: {_flat(v.get('orig_title'))[:80] or '-'}, "
                         f"원래 마감/orig due: {v.get('orig_due') or '-'})")
        if v.get("conflict"):
            lines.append(f"충돌 / conflict: {_flat(v['conflict'])[:200]}")
        for a in (v.get("alternatives") or [])[:5]:
            if isinstance(a, dict):
                lines.append(f"같은 과제의 다른 후보 / alternative: {a.get('id')} | due {a.get('due')} | {a.get('status')}")
        for n in (v.get("notes") or [])[:5]:
            lines.append(f"메모 / note: {_flat(n)[:160]}")
        for e in (v.get("evidence") or [])[:6]:
            if not isinstance(e, dict):
                continue
            lines.append(f"근거 / evidence ({e.get('source_type')}, {str(e.get('posted_at') or '')[:10]}, "
                         f"'{_flat(e.get('source_title'))[:80]}'): {_flat(e.get('text'))[:400]}")
            self.originals.append(str(e.get("text") or ""))
        return "\n".join(lines), _flat(v.get("title"))[:40]

    def t_get_grades(self, args: dict):
        from . import whatif
        cid = self._course(args.get("course_id"), required=True)
        c = next((x for x in self._section().get("courses") or [] if str(x.get("course_id")) == cid), None)
        if c is None:
            return f"course_id {cid}: 성적 정보 없음 / no grade data", "없음"
        n = whatif._n
        cfg = c.get("config") or {}
        comps = cfg.get("components") or []
        lines = [f"## course_id {cid} · {c.get('short_name')} | 종류/mode: {c.get('mode')}"]
        if c.get("canvas_score") is not None:
            lines.append(f"Canvas 총점 / Canvas score: {n(c['canvas_score'])}")
        lines.append("평가 항목 / components:" if comps else "평가 항목 / components: 없음 / none")
        for x in comps:
            sc, mx = x.get("score"), x.get("max_score")
            row = (f"- {x.get('name')} {float(x.get('weight') or 0):g}% | 점수/score: "
                   f"{'-' if sc is None else n(sc)}{'' if mx is None else '/' + n(mx)}"
                   f" | 평균/mean: {'-' if x.get('mean') is None else n(x['mean'])}"
                   f" | 표준편차/sd: {'-' if x.get('sd') is None else n(x['sd'])} | auto: {bool(x.get('auto'))}")
            if x.get("source"):
                row += f" | 출처/source: {_flat(x['source'])[:80]}"
            lines.append(row)
        est = c.get("estimate")
        if est:
            lines.append(f"추정 / estimate: {whatif._est_value(est)}")
            rng = whatif._est_range(est)
            if rng:
                lines.append(f"범위 / range: {rng}")
        elif c.get("error"):
            lines.append(f"추정 못 함 / no estimate: {_flat(c['error'])[:160]}")
        ds = c.get("dist_source") or {}
        lines.append(f"학점 비율 / distribution: {_flat(ds.get('summary'))[:160] or '-'} (출처/basis: {c.get('letter_basis')})")
        links = self.ctx.links(cid) if self.ctx.components(cid) else {}
        cands = [x for x in self.ctx.cands if str(x.get("course_id")) == cid][:15]
        if cands:
            lines.append("점수 후보 / score candidates:")
        for x in cands:
            ln = links.get(x.get("key")) or {}
            linked = f"{ln['component']} ({ln.get('by')})" if ln.get("component") else "없음/none"
            row = (f"- key {x['key']} | 라벨/label: {x.get('label') or '-'} | 출처/source: {x.get('source_type')} "
                   f"'{_flat(x.get('source_title'))[:60]}' {str(x.get('posted_at') or '')[:10]} | 연결/linked: {linked}")
            if x.get("source_type") != "pdf" and x.get("sentence"):   # 점수표(반 전체)는 줄을 넣지 않는다 — 라벨·출처만
                row += f" | 글/text: {_flat(x['sentence'])[:200]}"
                self.originals.append(str(x["sentence"]))
            self.originals += [str(t) for t in (x.get("label"), x.get("source_title")) if t]
            lines.append(row)
        text = "\n".join(lines)
        self.grade_texts.append(text)
        return text, f"평가 항목 {len(comps)}개"

    def t_whatif(self, args: dict):
        from . import whatif
        question = _s(args.get("question"), 300) or self.eff
        cid = self._course(args.get("course_id")) or self.course_id
        res = whatif.handle(self.store, question, cid, self.now)
        if not isinstance(res, dict):
            return ("성적 '만약에' 질문으로 읽지 못했어요 — 예: '기말에서 몇 점 받아야 A?', '중간 70점이면 학점은?' "
                    "/ not a what-if question"), "읽지 못함"
        w = res.get("whatif") or {}
        lines = [f"답 / answer: {_flat(res.get('answer'))}"]
        lines += [f"- {_flat(x)}" for x in w.get("lines") or []]
        lines += [f"- {_flat(r.get('label'))}: {_flat(r.get('value'))}" for r in w.get("rows") or [] if isinstance(r, dict)]
        lines += [f"근거 / basis: {_flat(b)}" for b in (w.get("basis") or [])[:6]]
        text = "\n".join(lines)
        self.grade_texts.append(text)
        self.extras["whatif"] = w
        return text, str(w.get("kind") or "계산")

    def t_list_materials(self, args: dict):
        from .features.materials import category_of
        cid = self._course(args.get("course_id"))
        cat = _s(args.get("category"), 20)
        rows = []
        for m in (self.store.data.get("downloads") or {}).values():
            if not isinstance(m, dict):
                continue
            c = str(m.get("course_id"))
            if c in self.ctx.hidden or (cid is not None and c != cid):
                continue
            category = category_of(m.get("post_key"), m.get("board_title"))
            if cat and category != cat:
                continue
            rows.append((m, category))
        rows.sort(key=lambda x: str(x[0].get("posted_at") or x[0].get("downloaded_at") or ""), reverse=True)
        if not rows:
            return "받은 자료 없음 / no materials", "0개"
        lines = [f"- {_flat(m.get('filename'))[:80]} | course_id {m.get('course_id')} · "
                 f"{self.ctx.shorts.get(str(m.get('course_id'))) or '-'} | 분류/category: {category} | 글/post: "
                 f"{_flat(m.get('post_title'))[:60]} | {str(m.get('posted_at') or '')[:10]}"
                 + (" | 글자 읽음/text read" if m.get("pdf_ok") or m.get("sheet_ok") or m.get("read_ok") else "")
                 for m, category in rows[:30]]
        return f"자료 {len(rows)}개 / materials:\n" + "\n".join(lines), f"{len(rows)}개"

    def t_get_settings(self, args: dict):
        from . import actions
        view = actions._settings_now(self.store)
        raw = self.store.data.get("settings") or {}
        on = lambda b: "켜짐/on" if b else "꺼짐/off"   # noqa: E731
        lines = [f"- {k} ({label}): {on(view.get(k))}" for k, label in actions.SETTING_NAMES.items()]
        lines.append(f"- auto_download (신규 첨부 자동으로 받기): {on(raw.get('auto_download', True))}")
        lines.append(f"- ai_cli (AI 도움): {on(raw.get('ai_cli'))}")
        lim = agent_cli.daily_limit(self.store)
        lines.append(f"- 하루 AI 한도 / daily AI limit: {'제한 없음/unlimited' if lim is None else f'{lim}회'} "
                     f"(오늘 {agent_cli.calls_today(self.store)}회 씀)")
        lines.append(f"- 학번 / student id: {'넣음/set' if raw.get('student_id') else '안 넣음/not set'}")   # 값은 보내지 않음
        g = self.store.data.get("gcal") or {}
        lines.append(f"- 구글 캘린더 반영 / Google calendar push: {('마지막 ' + str(g.get('at'))[:16]) if g.get('at') else '기록 없음/none'}")
        lines.append(f"- 마지막 동기화 / last sync: {str(self.store.data.get('last_sync') or '없음/none')[:16]}")
        modes = [f"{self.ctx.shorts.get(cid)}={course_mode(self.store.data, cid)}" for cid in self.ctx.courses
                 if course_mode(self.store.data, cid) != "normal"]
        if modes:
            lines.append("- 과목 종류 / course modes: " + ", ".join(modes))
        return "\n".join(lines), f"{len(lines)}개"

    def t_list_capabilities(self, args: dict):
        """앱이 할 수 있는 일(계약 28-1 catalog_text) — area를 주면 그 영역의 자세한 모양(params·등급)."""
        from . import capabilities
        area = _s(args.get("area"), 20) or None
        if area is not None and area not in capabilities.AREAS:
            raise _ToolError("모르는 영역이에요 — 영역/areas: " + ", ".join(capabilities.AREAS))
        text = _flat_lines(capabilities.catalog_text(area))
        if not text:
            return "(이 영역에서 할 수 있는 일이 아직 없어요 / no capabilities)", "0개"
        n = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("- "))
        return text, (f"{n}개" if n else (area or "전체"))

    def t_draft_mail(self, args: dict):
        from . import maildraft
        cid = self._course(args.get("course_id")) or self.course_id
        res = maildraft.handle(self.store, self.eff, cid, self.names)
        if not isinstance(res, dict):
            purpose = _s(args.get("purpose"), 20) or maildraft.purpose_of(self.eff)
            if purpose not in maildraft.PURPOSE_LABEL:
                purpose = "question"
            iid = _s(args.get("item_id"), 160) or None
            if iid and iid not in self.ctx.by_id:
                raise _ToolError("항목 목록에 없는 item_id예요 / unknown item_id")
            res = {"answer": f"{maildraft.PURPOSE_LABEL[purpose]} 메일 초안이에요.",
                   "draft": maildraft.draft(self.store, cid, purpose, iid, None, self.names)}
        d = res.get("draft") or {}
        self.extras["draft"] = d
        to = ", ".join(str(t.get("email")) for t in d.get("to") or [] if isinstance(t, dict)) or "(못 찾음 / not found)"
        text = (f"답 / answer: {_flat(res.get('answer'))}\n받는 사람 / to: {to}\n제목 / subject: {_flat(d.get('subject'))}\n"
                f"본문 / body:\n{str(d.get('body') or '')[:1500]}")
        return text, _flat(d.get("subject"))[:40] or "초안"

    def t_make_plan(self, args: dict):
        from . import plan
        cid = self._course(args.get("course_id")) or self.course_id
        res = plan.handle(self.store, self.eff, cid, self.now)
        if not isinstance(res, dict):
            kw = {k: args[k] for k in ("days", "minutes", "hour")
                  if isinstance(args.get(k), int) and not isinstance(args.get(k), bool)}
            iid = _s(args.get("item_id"), 160) or None
            try:
                p = plan.make(self.store, course_id=cid, item_id=iid, now=self.now, **kw)
            except (ValueError, LookupError) as e:
                raise _ToolError(_flat(e)[:200]) from None
            res = {"answer": "공부 계획 미리 보기예요.", "plan": p}
        p = res.get("plan") or {}
        self.extras["plan"] = p
        blocks = [b for b in p.get("blocks") or [] if isinstance(b, dict)]
        text = (f"답 / answer: {_flat(res.get('answer'))}\n"
                + "\n".join(f"- {b.get('start')} ~ {b.get('end')} {_flat(b.get('title'))}" for b in blocks[:14]))
        return text, f"블록 {len(blocks)}개"


def _short_args(args: dict) -> dict:
    """단계·입력 머리에 보일 인자(긴 글은 자르고, 근거 문장은 뺌)."""
    out = {}
    for k, v in (args or {}).items():
        if k in ("evidence", "components", "groups", "cutoffs", "params"):
            continue
        out[k] = v[:80] if isinstance(v, str) else v
    return out


def _flat_lines(text) -> str:
    """여러 줄 글의 줄마다 공백만 정리(줄은 남김)."""
    return "\n".join(ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()).strip()


def _prop_text(p: dict) -> str:
    """제안 검사 결과(다음 차례 입력) — 통과/거절과 그 이유. act는 등급·전→후까지(계약 28-3)."""
    if p.get("rejected_reason"):
        cands = "; ".join(f"{_flat(c.get('label'))} (id {c.get('id')})" for c in p.get("candidates") or [])[:300]
        return (f"거절 / rejected: {_flat(p['rejected_reason'])[:300]}"
                + (f" | 후보/candidates: {cands} — 하나를 골라 다시 act" if cands else ""))
    if p.get("type") == "act":
        if p.get("tier") == "refuse":
            return (f"하지 않는 일 / refused: {_flat(p.get('refuse'))[:200]} — say로 이유를 말하세요(앱이 설정 칸을 여는 단추를 붙여요) "
                    "/ tell the student why; the app adds a button to open the setting")
        rows = "; ".join(f"{_flat(c.get('label'))}: {c.get('before')} → {c.get('after')}" for c in p.get("changes") or [])[:400]
        tier = {"direct": "direct(분명히 시킨 말이면 앱이 바로 하고 [되돌리기])", "confirm": "confirm(학생이 [실행]을 눌러야 함)"}
        return (f"검사 통과 / passed: {_flat(p.get('summary') or p.get('title'))[:200]} | 등급/tier: "
                f"{tier.get(p.get('tier'), p.get('tier'))}" + (f" | 전→후/changes: {rows}" if rows else "")
                + (" | 되돌릴 수 없음/not undoable" if not p.get("undoable") else ""))
    tail = " — auto 요청(앱이 마지막에 바로 반영할지 정함)" if p.get("_auto") else ""
    return f"검사 통과 / passed: {_flat(p.get('summary') or p.get('title') or p.get('type'))[:200]}{tail}"


def run(store, message: str, *, eff: str | None = None, course_id=None, history=None, names=None, session=None,
        now: datetime | None = None, progress=None, intent: str | None = None, app=None) -> dict:
    """한 번 묻기의 루프(잠금 밖에서, store는 작업용 사본). 결과:
    성공 {"ok": True, "out": 차례 칸(kind, answer, proposals, citations, results, searched, by "ai", error None, attempts, notes,
          steps, whatif?/draft?/plan?/intent?), "steps", "attempts"}
    실패 {"ok": False, "error": 이유(한국어), "failure": "invalid"(검사 2번 실패)|"cli"(실행기 없음·오류), "steps", "attempts"}.
    session = agent_cli.AiSession(max_calls=agent.PER_ASK) — 하루 한도·묻기 한도는 그 세션이 지킨다. progress(문구|None, steps).
    app: act의 capabilities.plan이 읽을 앱(agent.PlanApp으로 감싸 store만 이 사본 — 없으면 store만 가진 앱)."""
    loop = Loop(store, message, eff, course_id, history, names, session, now, progress, intent, app=app)
    return loop.go()


__all__ = ["MAX_ROUNDS", "AUTO_TYPES", "PROPOSE", "LABELS", "is_imperative", "is_command", "claims_done",
           "auto_targets", "state_summary", "item_line", "run", "Loop"]
