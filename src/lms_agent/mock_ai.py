"""데모용 가짜 AI CLI. 데모의 [AI로 정리]가 실제 AI 서비스로 글을 보내지 않고도 하네스 전체(가리기·검증·반영·기록)를
그대로 지나가게 한다. agent_cli의 runner 자리에 들어간다: runner(args, input, timeout, cwd) → CompletedProcess.
[AI로 성적 찾기](score_read, 계약 29-2)는 가린 글의 '[내 줄]'에서 합계 열의 내 값을 그대로 고른다.

판단은 단순한 규칙이다(진짜 모델 아님): 같은 과제 후보가 여럿이면 가장 최근 근거를 승인하고 나머지는 제외,
마감 낱말이 있는 단독 후보는 승인, 그 밖에는 보류. 근거는 보낸 글에서 그대로 잘라 쓴다(하네스 검증을 통과해야 하므로).
이유는 정직하게: 상대 날짜('다음 주 금요일')면 게시일 기준으로 계산했다고 말하고, '분명히 적혀 있다'고 하지 않는다(계약 13-3).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date

from .extract import find_dates

DEMO_COMMAND = "claude"
_DEADLINE_WORD = re.compile(r"마감|까지|제출|연장|due|by\s", re.I)
_EVID = re.compile(r"^근거/evidence \d+ \(([^,]*), ([0-9-]*)\): (.*)$")


def _blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        if line.startswith("### id: "):
            out.append({"id": line[8:].strip(), "group": [], "evidence": []})
        elif out and line.startswith("같은 과제의 다른 후보"):
            out[-1]["group"] = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
        elif out:
            m = _EVID.match(line)
            if m:
                out[-1]["evidence"].append({"posted": m.group(2), "text": m.group(3)})
    return out


def _quote(text: str) -> str:
    """근거 원문에서 첫 문장(최대 80자) — 그대로 잘라야 검증을 통과한다."""
    first = re.split(r"(?<=[.!?。])\s", text.strip(), maxsplit=1)[0]
    return first[:80].strip() or text[:80]


def _single_reason(ev: dict) -> str:
    """단독 후보를 승인하는 이유. 날짜가 글에 적혀 있는지, 게시일 기준으로 계산한 상대 날짜인지 구분해 말한다."""
    try:
        ref = date.fromisoformat(ev["posted"][:10])
    except ValueError:
        ref = date.today()
    hits = find_dates(ev["text"], ref)
    if hits and all(h.relative for h in hits):
        return "상대 날짜라 게시일 기준으로 계산한 마감이에요 — 맞는지 한 번 봐 주세요"
    return "근거 글의 마감 날짜를 그대로 옮겼어요"


def _decide(text: str) -> dict:
    blocks = _blocks(text)
    by_id = {b["id"]: b for b in blocks}
    latest = {b["id"]: max((e["posted"] for e in b["evidence"]), default="") for b in blocks}
    decisions = []
    for b in blocks:
        if not b["evidence"]:
            continue
        ev = b["evidence"][0]["text"]
        group = [g for g in b["group"] if g in by_id]
        if group:
            newest = max([b["id"]] + group, key=lambda i: (latest.get(i, ""), i))
            if newest == b["id"]:
                decisions.append({"id": b["id"], "decision": "approved", "evidence": _quote(ev),
                                  "reason": "같은 과제 후보 중 가장 최근 글(연장·변경 공지)이라 이 마감을 골랐어요"})
            else:
                decisions.append({"id": b["id"], "decision": "rejected", "evidence": _quote(ev),
                                  "reason": "나중에 올라온 글이 마감을 바꿔서 이 후보는 뺐어요"})
        elif _DEADLINE_WORD.search(ev):
            decisions.append({"id": b["id"], "decision": "approved", "evidence": _quote(ev),
                              "reason": _single_reason(b["evidence"][0])})
        else:
            decisions.append({"id": b["id"], "decision": "keep", "evidence": _quote(ev),
                              "reason": "마감인지 확실하지 않아 직접 확인이 필요해요"})
    return {"decisions": decisions, "skipped": []}


# ---------------------------------------------------------------- assist(에이전트에게 묻기, 계약 20장)
# 보낸 글(찾은 글·지금 상태)만 읽고 규칙으로 답한다. 근거는 보낸 글에서 그대로 잘라 쓴다(하네스 검증을 통과해야 하므로).
# 이유는 정직하게: 무엇을 보고 그렇게 봤는지만 말하고, 상대 날짜는 계산했다고 한다.

ASSIST_MARK = "사용자 요청 / user request:"
ANSWER_MARK = "학생 질문 / student question:"   # agent_cli.ANSWER_MARK(계약 22-2 작업 answer)
CHAT_MARK = "학생이 건넨 말 / student message:"   # agent_cli.CHAT_MARK(계약 24-2 작업 chat)
FOLLOW_PREFIX = "(앞 대화와 이어서 읽은 뜻 / read with the previous turns:"   # agent_cli.FOLLOW_PREFIX(계약 24-1)
HISTORY_HEAD = "이전 대화 / previous turns"
_ITEM = re.compile(r"^- item_id (\S+) \| (.*?) \| (\S+) \| due (\S+) \| (\S+)")
_COMP = re.compile(r"^- (.+?) ([\d.]+)% \| 일정/dates: (\w+)")
_CAND = re.compile(r"^- key (\S+) \| 라벨/label: (.*?) \| 출처/source: (.*?) \| 연결/linked: (.*?)(?: \| 글/text: (.*))?$")
_WEEK = re.compile(r"\(주차/week (\d+)\)")
_TERM = re.compile(r"^학기 / term: (\d+)주차 / (\d+)주")


def _parse_state(state_part: str) -> tuple[dict, int | None]:
    """'지금 상태' 글 → ({course_id: {"short", "items", "comps", "cands"}}, 학기 주 수). assist·agent가 같은 모양을 보낸다."""
    courses: dict = {}
    total, cur, sect = None, None, None
    for line in state_part.splitlines():
        m = _TERM.match(line)
        if m:
            total = int(m.group(2))
        if line.startswith("## course_id "):
            cid, _, short = line[len("## course_id "):].partition(" · ")
            cur = courses.setdefault(cid.strip(), {"short": re.sub(r" \(P/NP\)$", "", short.strip()), "items": [],
                                                   "comps": [], "cands": []})
            sect = None
        elif cur is not None and line.startswith("평가 항목 / grading components"):
            sect = "comps"
        elif cur is not None and line.startswith("점수 후보"):
            sect = "cands"
        elif cur is not None and line.startswith("- item_id "):
            m = _ITEM.match(line)
            if m:
                cur["items"].append({"id": m.group(1), "title": m.group(2), "kind": m.group(3), "due": m.group(4),
                                     "status": m.group(5)})
        elif cur is not None and sect == "comps" and _COMP.match(line):
            m = _COMP.match(line)
            cur["comps"].append({"name": m.group(1), "weight": float(m.group(2)), "dates": m.group(3)})
        elif cur is not None and sect == "cands" and _CAND.match(line):
            m = _CAND.match(line)
            wk = _WEEK.search(m.group(3))
            cur["cands"].append({"key": m.group(1), "label": m.group(2), "source": m.group(3),
                                 "linked": not m.group(4).startswith("없음"), "text": m.group(5),
                                 "week": int(wk.group(1)) if wk else None})
    return courses, total


def _parse_found(found_part: str) -> list[dict]:
    """'### source_key: …' 덩어리들(찾은 글·search_texts 결과) → [{"key", "course_id", "posted", "title", "text"}]."""
    snippets = []
    for block in ("\n" + found_part).split("\n### source_key: ")[1:]:
        head, _, body = block.partition("\n")
        meta, _, rest = body.partition("\n")
        f = dict(x.split(": ", 1) for x in meta.split(" | ") if ": " in x)
        snippets.append({"key": head.strip(), "course_id": f.get("course_id"), "posted": f.get("게시/posted", ""),
                         "title": f.get("제목/title", ""), "text": rest.split("\n#### 차례 ", 1)[0].strip()})
    return snippets


def _parse_assist(text: str, mark: str = ASSIST_MARK) -> dict:
    after = text.split(mark, 1)[1].split("\n", 3)
    msg = after[1].strip()
    if len(after) > 2 and after[2].startswith(FOLLOW_PREFIX):   # 생략 질문 — 앞 대화와 합친 뜻으로 읽는다(계약 24-1)
        msg = after[2][len(FOLLOW_PREFIX):].strip().removesuffix(")").strip()
    state_part = text.split("지금 상태 / current state:", 1)[-1].split("찾은 글 / found texts", 1)[0]
    found_part = text.split("찾은 글 / found texts", 1)[-1].split("=== 입력 글 끝", 1)[0]
    courses, total = _parse_state(state_part)
    return {"message": msg, "courses": courses, "snippets": _parse_found(found_part), "weeks_total": total}


def _assist_deadlines(p: dict, fam_q: set) -> tuple[list, list]:
    from . import links as lk
    from .extract import find_time, split_sentences, title_hint
    props, said = [], []
    for sn in p["snippets"]:
        st = p["courses"].get(sn["course_id"] or "")
        if st is None:
            continue
        try:
            ref = date.fromisoformat(sn["posted"][:10])
        except ValueError:
            continue
        for sentence in split_sentences(sn["text"]):
            hits = [h for h in find_dates(sentence, ref) if not h.relative] or find_dates(sentence, ref)
            fam_s = lk.families(sentence) | lk.families(sn["title"])
            if not hits or (fam_q and not fam_q & fam_s):
                continue
            h = hits[0]
            if any(it["due"][:10] == h.day.isoformat() for it in st["items"]):
                said.append(f"{st['short']}의 {h.day.month}/{h.day.day} 일정은 이미 앱에 있어요.")
                break
            t, _ = find_time(sentence[h.end:])
            exam = bool(fam_s & {"quiz", "exam", "midterm", "final"}) and not fam_s & {"project", "report", "hw", "lab"}
            kind = "시험" if exam else "과제"
            fam_line = lk.families(sentence)
            comp = [c["name"] for c in st["comps"] if c["dates"] == "not_announced" and lk.families(c["name"]) & fam_line
                    and lk.families(c["name"]) & set(lk.TERM) <= fam_line and lk.is_exam_component(c["name"]) == exam]
            # 제목은 근거 문장에서만(질문의 낱말을 제목으로 쓰지 않는다)
            title = comp[0] if len(comp) == 1 else (title_hint(sentence, kind) or ("시험" if exam else "마감 후보"))
            hm = f"T{t:%H:%M}:00" if t else "T23:59:00"
            if h.relative:
                reason = "상대 날짜라 게시일 기준으로 계산한 날짜예요 — 맞는지 확인해 주세요"
            elif exam and not fam_s & {"quiz", "exam"}:
                m = next((re.search(lk.FAMILY_RX[f], sentence, re.I) for f in ("final", "midterm")
                          if re.search(lk.FAMILY_RX[f], sentence, re.I)), None)
                word = m.group(0) if m else "시험"
                reason = (f"글에 '{word}'만 적혀 있어 규칙이 시험 일정으로 읽지 못한 것 같아요(날짜는 글에 적힌 그대로)")
            else:
                reason = "글에 날짜가 적혀 있지만 마감 낱말이 없어 규칙이 놓친 것 같아요(날짜는 글에 적힌 그대로)"
            props.append({"type": "add_deadline", "course_id": sn["course_id"], "source_key": sn["key"],
                          "evidence": sentence.strip()[:300], "title": title, "kind": kind,
                          "due": h.day.isoformat() + hm, "reason": reason})
            said.append(f"{st['short']} '{sn['title']}' 글의 \"{sentence.strip()[:60]}\"에 날짜가 있어요 — {reason}.")
            break
        if len(props) >= 2:
            break
    return props, said


def _assist_scores(p: dict) -> tuple[list, list]:
    from . import links as lk
    from .extract import split_sentences
    props, said = [], []
    half = (p["weeks_total"] or 16) / 2
    for cid, st in p["courses"].items():
        loose = [c for c in st["cands"] if not c["linked"]]
        if st["cands"] and not st["comps"]:
            said.append(f"'{st['short']}'에는 평가 항목(비중)이 없어서 점수를 성적 계산에 넣을 곳이 없어요 — "
                        "강의 글에서 비중을 못 찾았어요. 비중이 적힌 글이 있으면 붙여 넣거나 성적 화면에서 직접 넣어 주세요.")
            continue
        for c in loose[:2]:
            fam = lk.families(c["label"]) | lk.families(c["source"])
            comps = [x["name"] for x in st["comps"] if lk.families(x["name"]) & fam - set(lk.TERM)]
            # 점수 문장이 든 찾은 글(같은 메시지·공지)의 다른 문장이 학기 시점을 말해 주는지
            sn = next((x for x in p["snippets"] if c["text"] and c["text"][:40] in x["text"]), None)
            term_line = next((ln for ln in split_sentences(sn["text"]) if lk.families(ln) & set(lk.TERM)), None)                 if sn else None
            said_term = lk.families(term_line or "") & set(lk.TERM)
            quote = None
            if len(comps) > 1 and len(said_term) == 1:
                term = next(iter(said_term))
                comps = [x for x in comps if term in lk.families(x)] or comps
                why = "같은 글에 학기 시점 낱말이 있어서"
                quote = term_line.strip()[:200]
            elif len(comps) > 1 and c["week"]:
                term = "midterm" if c["week"] <= half else "final"
                comps = [x for x in comps if term in lk.families(x)] or comps
                why = f"점수 글이 학기 {'앞' if term == 'midterm' else '뒤'}쪽({c['week']}주차)에 올라와 짐작하건대"
            else:
                why = "라벨의 낱말이 이 항목과 같아서"
            if len(comps) != 1:
                said.append(f"'{st['short']}'의 점수 '{c['label']}'은(는) 어느 평가 항목인지 글만으로는 모르겠어요.")
                continue
            quote = quote or (c["text"] or "")[:80].strip() or c["label"]
            props.append({"type": "link_score", "course_id": cid, "key": c["key"], "component": comps[0],
                          "evidence": quote, "reason": f"{why} '{comps[0]}'로 보여요 — 확인해 주세요"})
            said.append(f"'{st['short']}'의 점수 '{c['label']}'이(가) 평가 항목에 연결되지 않아 성적에 안 들어갔어요. "
                        f"{why} '{comps[0]}'에 연결하는 제안을 드려요.")
    return props, said


def _assist(text: str) -> dict:
    from . import links as lk
    from .agent import DATE_WORDS, SCORE_WORDS
    p = _parse_assist(text)
    fam_q = lk.families(p["message"])
    props, said = [], []
    if DATE_WORDS.search(p["message"]) or fam_q:
        a, b = _assist_deadlines(p, fam_q)
        props += a
        said += b
    if SCORE_WORDS.search(p["message"]):
        a, b = _assist_scores(p)
        props += a
        said += b
    if not said:
        said = ["보내 준 글에서 질문과 맞는 날짜나 점수 문장을 찾지 못했어요.",
                "과목 이름이나 다른 낱말(예: final, quiz, 과제 이름)로 다시 물어봐 주세요."]
    n = len(p["snippets"])
    head = f"찾은 글 {n}개를 읽었어요." if n else "이 질문과 관련된 글이 보내지지 않았어요."
    tail = ["[반영]을 누르기 전에 날짜와 항목이 맞는지 확인해 주세요."] if props else []
    return {"answer": " ".join([head] + said[:3] + tail), "proposals": props[:6]}


# ---------------------------------------------------------------- answer(과목 질문에 답하기, 계약 22-2)
# 보낸 '찾은 글'에서 질문의 주제(범위·형식 …)와 평가 낱말(기말·LAB …)이 맞는 문장 하나를 골라 그대로 인용한다(검증을 통과해야
# 하므로 한 글자도 바꾸지 않음). 답에는 그 문장과 글 제목만 쓴다(다른 숫자를 지어내지 않음). 맞는 문장이 없으면 찾지 못했다고 한다.

def _answer(text: str) -> dict:
    return _answer_from(_parse_assist(text, ANSWER_MARK))


def _answer_from(p: dict) -> dict:
    from . import links as lk
    from .agent import SCORE_WORDS, TOPICS, question_topics, tokens
    msg = p["message"]
    topics = question_topics(msg)
    fam_q = lk.families(msg)
    keys = [t.lower() for t in tokens(msg) if not lk.families(t) and not any(q.search(t) for q, _ in TOPICS.values())]
    best, best_score = None, 0.0
    for sn in p["snippets"]:          # 보낸 순서 = 검색 점수 순(같은 점수면 앞의 것)
        t_fam = lk.families(sn["title"])
        for line in sn["text"].split("\n"):   # 한 줄(문장 몇 개)을 그대로 인용
            s = line.strip()
            if len(s) < 8:
                continue
            hit = [t for t in topics if TOPICS[t][1].search(s)]
            if (topics and not hit) or (fam_q and not fam_q & (lk.families(s) | t_fam)):
                continue
            score = (2.0 * len(hit) + len(fam_q & lk.families(s)) + sum(k in s.lower() for k in keys)
                     + (1.0 if SCORE_WORDS.search(msg) and SCORE_WORDS.search(s) else 0.0))
            if not topics and not fam_q and score == 0:
                continue
            if score + 0.5 > best_score:
                best, best_score = (sn, s[:500]), score + 0.5
    if best is None:
        return {"answer": "보내 준 글에서 질문에 대한 내용을 찾지 못했어요. 과목 이름이나 다른 낱말로 다시 물어봐 주세요.",
                "citations": []}
    sn, quote = best
    return {"answer": f"'{sn['title']}' 글에 이렇게 적혀 있어요: “{quote}” 원문에서 한 번 더 확인해 주세요.",
            "citations": [{"source_key": sn["key"], "quote": quote}]}


# ---------------------------------------------------------------- chat(일반 대화, 계약 24-2)
# 진짜 모델이 아니므로 말의 갈래만 보고 정해진 문장으로 답한다. 날짜·점수·학점은 쓰지 않고(하네스 검사), 했다고 하지 않는다.
# 보낸 글에 '이전 대화'가 있으면 이어서 말한다고 밝힌다(대화를 이어받는지 데모·테스트에서 보이게).

_CRISIS = re.compile(r"죽고\s*싶|자살|자해|극단\s*적|살기\s*싫|사라지고\s*싶|suicid|kill\s+myself|self[-\s]?harm", re.I)
_HARM = re.compile(r"해킹|훔치|불법|대리\s*출석|커닝|컨닝|답\s*(?:좀\s*)?(?:알려|보내)|hack|cheat", re.I)
_HELLO = re.compile(r"^\s*(?:안녕|하이|헬로|반가|hi|hello|hey)", re.I)
_TIRED = re.compile(r"힘들|힘드|지쳤|지친|지쳐|피곤|우울|스트레스|응원|화이팅|파이팅|tired|stressed", re.I)


def _chat(text: str) -> dict:
    msg = text.split(CHAT_MARK, 1)[1].split("\n", 2)[1].strip()
    general = "모드 / mode: general" in text
    state = text.split("지금 상태 / current state", 1)[-1]
    week = next((ln.split(":", 1)[1].strip() for ln in state.splitlines() if ln.startswith("이번 주 마감 / this week:")), None)
    head = "앞 이야기에 이어서 말해 볼게요. " if HISTORY_HEAD in text else ""
    if _CRISIS.search(msg):
        answer = ("많이 힘들었겠어요. 혼자 견디지 않아도 돼요. 자살예방상담전화 109나 정신건강위기상담 1577-0199, "
                  "학교 학생상담센터에 꼭 이야기해 주세요. 위급하면 112나 119예요.")
    elif _HARM.search(msg):
        answer = ("그건 도와드릴 수 없어요 — 학교 규정이나 법을 어기는 일이라서요. 대신 정당하게 준비하는 방법이라면 "
                  "같이 고민해 볼게요.")
    elif general:
        answer = (head + "일반적으로는 개념을 작은 예로 직접 풀어 보는 게 가장 빨라요. 다만 그 과목에서는 다르게 정했을 수 "
                  "있으니 강의 글이나 교수님 안내를 꼭 확인해 주세요.")
    elif _HELLO.search(msg):
        answer = head + "안녕하세요! 오늘도 같이 달려 봐요." + (f" 이번 주 마감은 {week}예요." if week else "")
    elif _TIRED.search(msg):
        answer = (head + "많이 지쳤죠. 여기까지 온 것만으로도 잘 달리고 있어요. 오늘은 가장 가까운 것 하나만 같이 "
                  "끝내 봐요.")
    else:
        answer = (head + "저는 데모의 가짜 AI라 깊은 이야기는 못 하지만, 같이 달리는 페이스메이커로 여기 있을게요. "
                  "학기 이야기라면 '기말 범위 어디까지야?'처럼 물어봐 주세요.")
    return {"answer": answer}


# ---------------------------------------------------------------- agent(에이전트 v2 루프의 한 차례, 계약 26-1)
# 진짜 모델이 아니므로 단순한 규칙으로 '먼저 판단'한다: 인사·잡담·위험한 말 → 도구 없이 답(done), '중복·지워·없애 … 제목' → 1차례
# list_items → 2차례 propose_item_remove(평가 항목 이름이면 propose_component_remove, auto: true), 만약에 → whatif, 메일 → draft_mail,
# 계획 → make_plan, 앱 명령 → propose_action(규칙이 고름), 과목 질문 → search_texts → 인용으로 답하거나(question) 놓친 마감 제안
# (missing). 인용·근거는 보낸 도구 결과에서 그대로 자른다. '했어요'라고 하지 않는다(바로 반영은 앱이 정한다).

AGENT_MARK = "학생 요청 / student request (에이전트 / agent):"   # agent_cli.AGENT_MARK
AGENT_COURSE_PREFIX = "(앞 대화의 과목 / course from the previous turns:"   # agent_cli.AGENT_COURSE_PREFIX
AGENT_TOOLS_MARK = "도구 결과 / tool results"
_FIX_ASK = re.compile(r"중복|지워|지우|없애|삭제|제거|빼\s*(?:줘|주세요)|합쳐", re.I)
_TOOL_HEAD = re.compile(r"^(\d+) · (\w+)")
_LISTED = re.compile(r"\| course_id (\S+)")
_ANSWER_LINE = re.compile(r"^답 / answer: (.*)$", re.M)


def _mnorm(s) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(s or "").lower())


def _parse_agent(text: str) -> dict:
    body = text.split("=== 입력 글 시작 / INPUT START ===", 1)[-1]
    lines = body.split(AGENT_MARK, 1)[1].split("\n")
    typed = lines[1].strip()
    msg, course = typed, None
    for ln in lines[2:5]:
        if ln.startswith(FOLLOW_PREFIX):
            msg = ln[len(FOLLOW_PREFIX):].strip().removesuffix(")").strip()
        elif ln.startswith(AGENT_COURSE_PREFIX):
            course = ln[len(AGENT_COURSE_PREFIX):].strip().split(" ", 1)[0]
    cpart = body.split("과목 / courses (course_id: 이름):", 1)[-1].split("\n\n", 1)[0]
    courses = [{"id": m.group(1), "name": m.group(2).strip()} for m in re.finditer(r"^- (\S+): (.+)$", cpart, re.M)]
    state_part = body.split("지금 상태 / current state:", 1)[-1].split(AGENT_TOOLS_MARK, 1)[0]
    states, total = _parse_state(state_part)
    tools_part = body.split(AGENT_TOOLS_MARK, 1)[-1].split("=== 입력 글 끝", 1)[0]
    tools = []
    for block in tools_part.split("\n#### 차례 ")[1:]:
        head, _, rest = block.partition("\n")
        m = _TOOL_HEAD.match(head)
        if m:
            tools.append({"round": int(m.group(1)), "tool": m.group(2), "text": rest})
    return {"typed": typed, "message": msg, "course": course, "courses": courses, "states": states,
            "weeks_total": total, "tools": tools, "history": HISTORY_HEAD in body}


def _chat_reply(msg: str, history: bool) -> str:
    """도구 없이 하는 짧은 대화(chat 흉내와 같은 문장 — 날짜·점수·'했어요' 없음)."""
    head = "앞 이야기에 이어서 말해 볼게요. " if history else ""
    if _CRISIS.search(msg):
        return ("많이 힘들었겠어요. 혼자 견디지 않아도 돼요. 자살예방상담전화 109나 정신건강위기상담 1577-0199, "
                "학교 학생상담센터에 꼭 이야기해 주세요. 위급하면 112나 119예요.")
    if _HARM.search(msg):
        return "그건 도와드릴 수 없어요 — 학교 규정이나 법을 어기는 일이라서요. 대신 정당하게 준비하는 방법이라면 같이 고민해 볼게요."
    if _HELLO.search(msg):
        return head + "안녕하세요! 오늘도 같이 달려 봐요. 마감이나 과목 이야기는 언제든 물어봐 주세요."
    if _TIRED.search(msg):
        return head + "많이 지쳤죠. 여기까지 온 것만으로도 잘 달리고 있어요. 오늘은 가장 가까운 것 하나만 같이 끝내 봐요."
    return (head + "저는 데모의 가짜 AI라 깊은 이야기는 못 하지만, 같이 달리는 페이스메이커로 여기 있을게요. "
            "학기 이야기라면 '기말 범위 어디까지야?'처럼 물어봐 주세요.")


# ---- 말로 시킨 일(계약 28-3): 진짜 모델처럼 '먼저 판단'하되 규칙으로 —
# 하지 않는 일(AI 켜기·끄기·한도·개인정보) → 도구 없이 이유만(앱이 [설정 열기]를 붙인다), '구글 캘린더에 반영해줘' → act(구글 반영) 바로,
# '학점 비율 A 40%로 바꿔줘' → list_capabilities(grades) → act(grades.set_dist). 능력 id·params 이름은 capabilities.REGISTRY에서 읽는다
# (데모가 지금 능력 모양을 따라가게 — 과목은 학생 말에 나온 이름, 값은 말에 적힌 그대로).
_DIST_ASK = re.compile(r"(?:학점|성적)\s*(?:의\s*)?(?:비율|분포|배분|컷)|비율", re.I)
_LETTER_PCT = re.compile(r"(?<![A-Za-z])([A-D][+0]?)\s*(?:학점)?\s*(?:을|를|은|는|이|가)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:%|퍼센트|프로)")
# 절대평가 기준표('ASIC 절대평가 A+ 95 A0 90 B+ 85로 바꿔줘') — 글자별 최저 점수
_ABSOLUTE = re.compile(r"절대\s*평가|절대\s*기준|점수\s*기준")
_LETTER_SCORE = re.compile(r"(?<![A-Za-z])([A-D][+0]?)\s*(?:학점)?\s*(?:은|는|이|가)?\s*(\d{2,3}(?:\.\d+)?)\s*(?:점)?(?![\d%.])")
_WORD = re.compile(r"[A-Za-z]{2,}|[가-힣]{2,}")
# '구글 캘린더 일정 반영해줘'처럼 사이에 낱말이 껴도(actions._GPUSH는 '구글 캘린더에 반영'만)
_GOOGLE_PUSH = re.compile(r"(?:구글|google)[^.?!\n]{0,24}?(?:반영|올려|보내|푸시|push|맞춰)", re.I)


def _caps():
    try:
        from . import capabilities
        return capabilities.REGISTRY
    except Exception:  # noqa: BLE001
        return {}


def _cap_by_route(path: str, fallback: str) -> str:
    for c in _caps().values():
        if c.route and len(c.route) > 1 and c.route[1] == path and c.tier != "refuse":
            return c.id
    return fallback


def _course_named(p: dict, text: str) -> dict | None:
    """학생 말에 이름(두 글자 이상 낱말)이 나온 과목 하나(여럿·없음이면 None)."""
    words = [w.lower() for w in _WORD.findall(text)]
    hits = [c for c in p["courses"] if c["name"] and (_mnorm(c["name"]) in _mnorm(text)
                                                     or any(len(w) >= 2 and w in c["name"].lower() for w in words
                                                            if not _DIST_ASK.fullmatch(w)))]
    return hits[0] if len(hits) == 1 else None


def _fill(cap, values: dict) -> dict:
    """능력의 params 모양에 맞춰 값을 넣는다: 이름이 같은 칸 → resolve 종류(과목) → 종류(글·목록·객체)·enum 순."""
    out = {}
    for name, spec in (getattr(cap, "params", None) or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        typ, low = spec.get("type"), name.lower()
        if name in values:
            out[name] = values[name]
        elif spec.get("resolve") == "course" or low in ("course", "course_id", "course_name"):
            out[name] = values["course"]
        elif typ == "enum" and "relative" in (spec.get("enum") or []):
            out[name] = "relative"
        elif typ == "dict":
            out[name] = dict(values["letters"])
        elif typ == "list":
            out[name] = [{"grade": g, "pct": v} for g, v in values["letters"].items()]
        elif typ == "str" and spec.get("required"):
            out[name] = values["text"]
    return out


def _act_first(p: dict, done, ask) -> dict | None:
    from . import actions
    from .agent import refusal_for
    typed = p["typed"]
    ref = refusal_for(None, typed)
    if ref is not None:
        return done(f"{ref['refuse']} 아래 [설정 열기]를 누르면 그 칸이 바로 열려요.")
    if not actions._is_command(typed):
        return None
    if actions._GPUSH.search(typed.lower()) or _GOOGLE_PUSH.search(typed):
        cap = _cap_by_route("/api/google/push", "calendar.google_push")
        return done("구글 캘린더에 반영해 달라는 말로 알아들었어요. 앱이 확인하면 바로 하고, 결과는 아래 카드에 보여요.",
                    [{"tool": "act", "args": {"capability": cap, "params": {}, "reason": f"학생 요청: “{typed[:60]}”"}}])
    if _course_named(p, typed) and ((_DIST_ASK.search(typed) and _LETTER_PCT.search(typed))
                                    or (_ABSOLUTE.search(typed) and _LETTER_SCORE.search(typed))):
        return ask({"tool": "list_capabilities", "args": {"area": "grades"}})
    return None


def _act_next(p: dict) -> dict | None:
    """list_capabilities 다음 차례 → act(학점 비율), act가 막혔으면 그 이유로 답."""
    used = [t["tool"] for t in p["tools"]]
    blocked = next((t["text"] for t in p["tools"] if t["tool"] == "act" and t["text"].startswith("거절")), None)
    if blocked:
        why = blocked.split(":", 1)[-1].strip()[:200].rstrip(".")
        tail = " 과목·값을 조금 더 분명히 말해 주세요." if re.search(r"여럿|없어요|골라", why) else ""
        return {"say": f"말한 대로 하지 못했어요 — {why}.{tail}", "calls": [], "done": True}
    if "list_capabilities" not in used:
        return None
    typed = p["typed"]
    c = _course_named(p, typed)
    absolute = bool(_ABSOLUTE.search(typed)) and not _LETTER_PCT.search(typed)
    pairs = {m.group(1): float(m.group(2)) for m in (_LETTER_SCORE if absolute else _LETTER_PCT).finditer(typed)}
    cap = _caps().get("grades.set_dist")
    if c is None or not pairs:
        return None
    unit = "점 이상" if absolute else "%"
    text = " · ".join(f"{g} {v:g}{unit}" for g, v in pairs.items())
    what = "절대평가 기준을" if absolute else "학점 비율을"
    params = _fill(cap, {"course": c["name"], "letters": pairs, "text": text,
                         "kind": "absolute" if absolute else "relative"}) if cap else {"course_id": c["name"], "text": text}
    return {"say": f"{c['name']} {what} {text}로 바꿔 달라는 말로 알아들었어요. 앱이 확인하면 바로 하고, [되돌리기]로 언제든 "
                   "되돌릴 수 있어요.", "done": True,
            "calls": [{"tool": "act", "args": {"capability": "grades.set_dist", "params": params,
                                               "reason": f"학생 요청: “{typed[:60]}”"}}]}


def _agent_first(p: dict) -> dict:
    from . import actions, maildraft, plan, whatif
    from .agent import chat_intent, strong_academic
    typed, msg = p["typed"], p["message"]
    course = {"course_id": p["course"]} if p["course"] else {}
    done = lambda say, calls=(): {"say": say, "calls": list(calls), "done": True}   # noqa: E731
    ask = lambda *calls: {"say": "", "calls": list(calls), "done": False}   # noqa: E731
    if _CRISIS.search(typed) or _HARM.search(typed):
        return done(_chat_reply(typed, p["history"]))
    acted = _act_first(p, done, ask)
    if acted is not None:
        return acted
    if _FIX_ASK.search(typed):
        return ask({"tool": "list_items", "args": dict(course)})
    if whatif.parse(typed):
        return ask({"tool": "whatif", "args": {"question": msg, **course}})
    if maildraft.is_mail_request(typed):
        return ask({"tool": "draft_mail", "args": dict(course)})
    if plan._PLAN.search(typed) and not plan._NOT_PLAN.search(typed):
        return ask({"tool": "make_plan", "args": dict(course)})
    if actions._is_command(typed) and not strong_academic(typed, p["courses"]):
        return done("말한 앱 동작을 카드로 준비했어요 — 맞으면 [실행]을 눌러 주세요.",
                    [{"tool": "propose_action", "args": {"reason": "학생이 앱 동작을 시켰어요"}}])
    if chat_intent(typed, p["courses"]) or not strong_academic(msg, p["courses"]):
        return done(_chat_reply(typed, p["history"]))
    return ask({"tool": "search_texts", "args": {"query": msg, **course}})


def _agent_fix(p: dict, listed: list[dict]) -> dict:
    hay = _mnorm(p["typed"])
    for cid, st in p["states"].items():
        for c in st["comps"]:
            if len(_mnorm(c["name"])) >= 2 and _mnorm(c["name"]) in hay:
                return {"say": f"‘{c['name']}’ 평가 항목을 지워 달라는 말로 알아들었어요. 앱이 확인하면 바로 반영되고, "
                               "[되돌리기]로 언제든 되돌릴 수 있어요.", "done": True,
                        "calls": [{"tool": "propose_component_remove",
                                   "args": {"course_id": cid, "component": c["name"], "auto": True,
                                            "reason": "학생이 이 평가 항목을 지워 달라고 했어요"}}]}
    hits = [it for it in listed if len(_mnorm(it["title"])) >= 2 and _mnorm(it["title"]) in hay]
    if not hits:
        return {"say": "목록에서 어느 항목인지 찾지 못했어요. 항목 제목을 그대로 적어 다시 말해 주세요.", "calls": [],
                "done": True}
    dup = [it for it in hits if it.get("duplicate") or it["status"] == "duplicate"]
    it = (dup or hits)[-1]
    return {"say": f"‘{it['title']}’ 항목을 일정에서 빼 달라는 말로 알아들었어요. 앱이 확인하면 바로 반영되고, "
                   "[되돌리기]로 언제든 되돌릴 수 있어요.", "done": True,
            "calls": [{"tool": "propose_item_remove",
                       "args": {"item_id": it["id"], "auto": True, "reason": "학생이 이 항목을 지워 달라고 했어요"}}]}


def _agent(text: str) -> dict:
    from . import links as lk
    from .agent import DATE_WORDS, classify
    p = _parse_agent(text)
    if not p["tools"]:
        return _agent_first(p)
    acted = _act_next(p)
    if acted is not None:
        return acted
    used = {t["tool"] for t in p["tools"]}
    if "list_items" in used:
        listed = []
        for t in p["tools"]:
            if t["tool"] != "list_items":
                continue
            for line in t["text"].splitlines():
                m = _ITEM.match(line)
                if m:
                    listed.append({"id": m.group(1), "title": m.group(2), "due": m.group(4), "status": m.group(5),
                                   "duplicate": "duplicate_of" in line})
        return _agent_fix(p, listed)
    for tool, say in (("whatif", None), ("draft_mail", "메일 초안을 만들었어요 — 받는 사람과 본문을 확인하고 직접 보내 주세요"
                                                        "(앱은 보내지 않아요)."),
                      ("make_plan", "공부 계획 미리 보기를 짰어요 — 괜찮으면 [캘린더에 넣기]를 눌러 주세요.")):
        if tool in used:
            body = next(t["text"] for t in p["tools"] if t["tool"] == tool)
            m = _ANSWER_LINE.search(body)
            return {"say": say or (m.group(1).strip() if m else "계산 결과를 봐 주세요."), "calls": [], "done": True}
    snippets = [sn for t in p["tools"] if t["tool"] == "search_texts" for sn in _parse_found(t["text"])]
    pp = {"message": p["message"], "courses": p["states"], "snippets": snippets, "weeks_total": p["weeks_total"]}
    fam_q = lk.families(p["message"])
    if classify(p["message"]) == "missing" and (DATE_WORDS.search(p["message"]) or fam_q):
        props, said = _assist_deadlines(pp, fam_q)
        if props:
            head = f"찾은 글 {len(snippets)}개를 읽었어요."
            return {"say": " ".join([head] + said[:3] + ["[반영]을 누르기 전에 날짜와 항목이 맞는지 확인해 주세요."]),
                    "done": True, "calls": [{"tool": "propose_add_deadline", "args": {k: v for k, v in x.items() if k != "type"}}
                                            for x in props[:3]]}
    ans = _answer_from(pp)
    return {"say": ans["answer"], "calls": [], "done": True, "citations": ans["citations"]}


# ---------------------------------------------------------------- score_read(성적 파일에서 내 점수, 계약 29-2)
# 보낸 글(aiscores.build_input이 가린 것)의 '[내 줄 / MY ROW]'과 '앱이 계산한 열 통계'만 읽는다. 합계·총점 열(없으면 마지막 열)의
# '내 값'을 그 줄에서 그대로 고르고, 평균·표준편차는 앱 통계 값 그대로(하네스 검사를 통과해야 하므로 지어내지 않는다).

SCORE_MARK = "성적 파일(가린 글) / score file (masked):"   # aiscores.MARK
SCORE_ROW = "[내 줄 / MY ROW] "                            # aiscores.MY_ROW
_STAT = re.compile(r"^표 (\d+) · 열 '(.*)': n (\d+) · 평균/mean (\S+) · 표준편차/sd (\S+) · 최저/min \S+ · 최고/max \S+"
                   r"(?: · 내 값/my value (\S+) · 내 등수/my rank (\d+)/\d+)?")
_TOTALISH = re.compile(r"total|sum|합계|총점|총합|score|점수|final|최종", re.I)
_LABEL_LINE = "파일 이름에서 본 평가 이름 / label from the file name: "


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _score_read(text: str) -> dict:
    body = text.split("=== 입력 글 시작 / INPUT START ===", 1)[-1].split("=== 입력 글 끝", 1)[0]
    rows = [ln for ln in body.splitlines() if ln.startswith(SCORE_ROW)]
    if not rows:
        return {"scores": [], "note": "보낸 글에 내 줄이 없어요"}
    label = next((ln[len(_LABEL_LINE):].strip() for ln in body.splitlines() if ln.startswith(_LABEL_LINE)), None)
    stats = []
    for ln in body.splitlines():
        m = _STAT.match(ln)
        if m and m.group(6) is not None:
            stats.append({"name": m.group(2), "mean": _num(m.group(4)), "sd": _num(m.group(5)), "mine": _num(m.group(6))})
    nums = [float(x) for x in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", rows[0][len(SCORE_ROW):])]
    if stats:
        col = next((c for c in reversed(stats) if _TOTALISH.search(c["name"])), stats[-1])
        score, column, mean, sd = col["mine"], col["name"], col["mean"], col["sd"]
    elif nums:
        score, column, mean, sd = nums[-1], None, None, None
    else:
        return {"scores": [], "note": "내 줄에 숫자가 없어요"}
    item = {"label": label or column or "점수", "score": score, "evidence": rows[0][:400], "confidence": "medium",
            "component": None}
    if column:
        item["column"] = column
    if mean is not None:
        item["mean"] = mean
    if sd is not None:
        item["sd"] = sd
    return {"scores": [item], "note": "데모의 가짜 AI가 내 줄에서 읽었어요"}


def runner(args, input, timeout, cwd) -> subprocess.CompletedProcess:
    """agent·triage_review·assist·answer·chat·score_read만 흉내 낸다. 다른 작업(평가 비중·마감 찾기·평가 항목 연결·개수·학점 비율)은
    '찾지 못함'으로 답한다(데모는 규칙이 찾는다)."""
    if SCORE_MARK in (input or ""):   # score_read(성적 파일에서 내 점수 — 계약 29-2). 파일 글에 다른 표시가 섞여도 먼저
        model = json.dumps(_score_read(input), ensure_ascii=False)
    elif AGENT_MARK in (input or ""):   # agent(에이전트 v2 루프 한 차례 — 계약 26-1)
        model = json.dumps(_agent(input), ensure_ascii=False)
    elif CHAT_MARK in (input or ""):   # chat(일반 대화 — 계약 24-2)
        model = json.dumps(_chat(input), ensure_ascii=False)
    elif ANSWER_MARK in (input or ""):   # answer(과목 질문 — 계약 22-2)
        model = json.dumps(_answer(input), ensure_ascii=False)
    elif ASSIST_MARK in (input or ""):   # assist(에이전트에게 묻기)
        model = json.dumps(_assist(input), ensure_ascii=False)
    elif "### id: " in (input or ""):
        model = json.dumps(_decide(input), ensure_ascii=False)
    elif "### key: " in (input or ""):   # map_score_item: 모르면 null
        keys = re.findall(r"^### key: (.+)$", input, re.M)
        model = json.dumps({"links": [{"key": k.strip(), "component": None} for k in keys]}, ensure_ascii=False)
    elif '"cutoffs"' in (input or ""):   # grade_distribution(스키마에 cutoffs가 있다): 없음(null)
        model = json.dumps({"kind": None, "groups": [], "cutoffs": [], "evidence": ""}, ensure_ascii=False)
    elif '"count"' in (input or "") and '"links"' not in (input or ""):   # count_items: 근거 없으면 null
        model = json.dumps({"count": None}, ensure_ascii=False)
    else:
        model = json.dumps({"components": [], "deadlines": [], "evidence": ""}, ensure_ascii=False)
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": model,
                      "session_id": "demo", "modelUsage": {"demo-rules (가짜 AI)": {"inputTokens": 0}}},
                     ensure_ascii=False)
    return subprocess.CompletedProcess(args, 0, out, "")
