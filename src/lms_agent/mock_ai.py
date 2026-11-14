"""데모용 가짜 AI CLI. 데모의 [AI로 정리]가 실제 AI 서비스로 글을 보내지 않고도 하네스 전체(가리기·검증·반영·기록)를
그대로 지나가게 한다. agent_cli의 runner 자리에 들어간다: runner(args, input, timeout, cwd) → CompletedProcess.

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


def _parse_assist(text: str, mark: str = ASSIST_MARK) -> dict:
    after = text.split(mark, 1)[1].split("\n", 3)
    msg = after[1].strip()
    if len(after) > 2 and after[2].startswith(FOLLOW_PREFIX):   # 생략 질문 — 앞 대화와 합친 뜻으로 읽는다(계약 24-1)
        msg = after[2][len(FOLLOW_PREFIX):].strip().removesuffix(")").strip()
    state_part = text.split("지금 상태 / current state:", 1)[-1].split("찾은 글 / found texts", 1)[0]
    found_part = text.split("찾은 글 / found texts", 1)[-1].split("=== 입력 글 끝", 1)[0]
    courses: dict = {}
    total, cur, sect = None, None, None
    for line in state_part.splitlines():
        m = _TERM.match(line)
        if m:
            total = int(m.group(2))
        if line.startswith("## course_id "):
            cid, _, short = line[len("## course_id "):].partition(" · ")
            cur = courses.setdefault(cid.strip(), {"short": short.strip(), "items": [], "comps": [], "cands": []})
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
    snippets = []
    for block in found_part.split("\n### source_key: ")[1:]:
        head, _, body = block.partition("\n")
        meta, _, rest = body.partition("\n")
        f = dict(x.split(": ", 1) for x in meta.split(" | ") if ": " in x)
        snippets.append({"key": head.strip(), "course_id": f.get("course_id"), "posted": f.get("게시/posted", ""),
                         "title": f.get("제목/title", ""), "text": rest.strip()})
    return {"message": msg, "courses": courses, "snippets": snippets, "weeks_total": total}


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
    from . import links as lk
    from .agent import SCORE_WORDS, TOPICS, question_topics, tokens
    p = _parse_assist(text, ANSWER_MARK)
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


def runner(args, input, timeout, cwd) -> subprocess.CompletedProcess:
    """triage_review·assist·answer·chat만 흉내 낸다. 다른 작업(평가 비중·마감 찾기·평가 항목 연결·개수·학점 비율)은 '찾지
    못함'으로 답한다(데모는 규칙이 찾는다)."""
    if CHAT_MARK in (input or ""):   # chat(일반 대화 — 계약 24-2)
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
