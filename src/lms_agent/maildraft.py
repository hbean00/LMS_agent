"""문의 메일 초안(계약 22-7). 앱은 메일을 보내지 않는다 — 초안(받는 사람·제목·본문)만 만든다.

- 받는 사람: 그 과목 글(공지·게시판·메시지·강의계획서·과제 설명·첨부 글)에서 조교/TA/교수/professor/문의 같은 말 가까이에
  적힌 이메일 주소만, 그 문장 인용·글 제목과 함께. 항목의 제출 주소(email_to)가 있으면 그것이 먼저. 못 찾으면 비워 두고
  notes에 "주소를 못 찾았어요" — 주소를 지어내지 않는다.
- 이름·학번은 [이름]·[학번] 자리표시. 설정에 사용자 이름(user_name)이 있으면 이름만 채운다(학번은 늘 자리표시).
- 날짜는 앱의 다른 곳처럼 이 PC 시간대로 '11월 18일(수) 23:59'.
"""
from __future__ import annotations

import re

from .checklist import ADDR, clean_address
from .extract import same_task, split_sentences, title_tokens
from .timeutil import parse_iso

PURPOSES = ("extension", "absence", "grade", "submission", "question")
PURPOSE_LABEL = {"extension": "기한 연장 문의", "absence": "결석(공결) 문의", "grade": "성적 문의",
                 "submission": "제출 확인 문의", "question": "문의"}
NOTE_MAX = 1000
MAX_TO = 3
QUOTE_MAX = 300
NOT_FOUND = "주소를 못 찾았어요 — 강의계획서·공지에서 교수님·조교 연락처를 확인해 받는 사람을 직접 넣어 주세요."
_KO_WD = "월화수목금토일"

PROF = re.compile(r"(?i)교수|강사|담당\s*교원|(?<![a-z])(?:professor|prof|instructor|lecturer)(?![a-z])")
TA = re.compile(r"(?i)조교|튜터|(?<![a-z])(?:t\.a\.|ta|tas|teaching\s+assistants?|tutors?)(?![a-z])")
CONTACT = re.compile(r"(?i)문의|연락|질문|상담|(?<![a-z])(?:contact|questions?|inquir\w*|office\s+hours?)(?![a-z])")
_LOCAL_PROF = re.compile(r"(?i)(?:^|[._\-])(?:prof|professor|instructor)(?:$|[._\-\d])")
_LOCAL_TA = re.compile(r"(?i)(?:^|[._\-])(?:ta|tas|assistant|tutor)(?:$|[._\-\d])")
# 주소 바로 앞의 머리 줄('조교 연락처', 'TA:', 'Contact') — 짧고 숫자·주소가 없는 줄만 역할 말로 본다
_HEADING = re.compile(r"[^\d@]{1,30}")
_SKIP_ADDR = re.compile(r"(?i)no-?reply|do-?not-?reply|notifications?@|instructure|mailer-daemon")


# ---------------------------------------------------------------- 글·항목

def _sources(store) -> list[dict]:
    from . import agent
    return agent.sources(store)


def _courses(store) -> list[dict]:
    from .store import hidden_courses
    hidden = hidden_courses(store.data)
    return [c for c in ((store.data.get("cache") or {}).get("courses") or [])
            if isinstance(c, dict) and str(c.get("id")) not in hidden]


def _short(name) -> str:
    from .core import short_course_name
    return short_course_name(name) or "과목"


def due_text(due) -> str | None:
    d = parse_iso(due) if isinstance(due, str) else due
    if d is None:
        return None
    return f"{d.month}월 {d.day}일({_KO_WD[d.weekday()]}) {d:%H:%M}"


# ---------------------------------------------------------------- 받는 사람

def _role(context: str, addr: str) -> str | None:
    """주소 주변 말(주소 자체는 뺀 것)로 역할을, 없으면 주소 앞부분(prof·ta)으로."""
    if PROF.search(context):
        return "교수"
    if TA.search(context):
        return "조교"
    local = addr.split("@", 1)[0]
    if _LOCAL_PROF.search(local):
        return "교수"
    if _LOCAL_TA.search(local):
        return "조교"
    if CONTACT.search(context):
        return "문의처"
    return None


def _quote(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s if len(s) <= QUOTE_MAX else s[:QUOTE_MAX - 1] + "…"


def find_contacts(texts: list[dict]) -> list[dict]:
    """texts: [{"plain"|"text", "title"|"source_title", "url"}] → [{"email", "role", "quote", "source_title", "url"}].
    주소가 든 문장(+ 바로 앞의 짧은 줄 — '조교 연락처' 같은 머리 줄)에 역할·문의 낱말이 있는 것만. 같은 주소는 처음 것."""
    out, seen = [], set()
    for t in texts:
        plain = t.get("plain") if t.get("plain") is not None else (t.get("text") or "")
        sents = split_sentences(plain)
        for i, s in enumerate(sents):
            for m in ADDR.finditer(s):
                addr = m.group(0)
                key = addr.lower()
                if key in seen or _SKIP_ADDR.search(addr):
                    continue
                prev = sents[i - 1] if i > 0 and _HEADING.fullmatch(sents[i - 1]) else ""
                context = ADDR.sub(" ", prev + " " + s)
                role = _role(context, addr)
                if role is None:
                    continue
                seen.add(key)
                out.append({"email": addr, "role": role, "quote": _quote(s),
                            "source_title": t.get("title") or t.get("source_title"), "url": t.get("url")})
    return out


def _item_address(item: dict, texts: list[dict]) -> dict | None:
    """항목의 제출 주소(email_to) — 근거 문장 또는 그 과목 글에서 인용을 찾아 붙인다."""
    addr = clean_address(item.get("email_to"))
    if not addr:
        return None
    for e in item.get("evidence") or []:
        if isinstance(e, dict) and addr.lower() in str(e.get("text") or "").lower():
            return {"email": addr, "role": "제출처", "quote": _quote(str(e.get("text"))),
                    "source_title": e.get("source_title"), "url": e.get("url")}
    for t in texts:
        for s in split_sentences(t.get("plain") or ""):
            if addr.lower() in s.lower():
                return {"email": addr, "role": "제출처", "quote": _quote(s), "source_title": t.get("title"),
                        "url": t.get("url")}
    return None     # 인용할 문장을 못 찾으면 넣지 않는다


def recipients(store, course_id, purpose: str, item: dict | None = None, srcs: list | None = None) -> list[dict]:
    srcs = _sources(store) if srcs is None else srcs
    mine = [s for s in srcs if course_id is not None and str(s.get("course_id")) == str(course_id)]
    out = []
    first = _item_address(item, mine) if item else None
    if first:
        out.append(first)
    order = ("교수", "조교", "문의처") if purpose == "absence" else ("조교", "교수", "문의처")
    found = find_contacts(mine)
    found.sort(key=lambda r: order.index(r["role"]))      # 안정 정렬: 같은 역할은 글 순서
    for r in found:
        if all(r["email"].lower() != o["email"].lower() for o in out):
            out.append(r)
    return out[:MAX_TO]


# ---------------------------------------------------------------- 초안

def _names(names) -> list[str]:
    if isinstance(names, str):
        names = [names]
    return [str(n).strip() for n in (names or []) if isinstance(n, str) and n.strip()]


def _clean_note(note) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("note(덧붙일 말)는 문자열이어야 해요")
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"덧붙일 말은 {NOTE_MAX}자 이하로 적어 주세요")
    return note or None


def _how(item: dict) -> str:
    m = item.get("method")
    if m == "email":
        addr = clean_address(item.get("email_to"))
        return f"이메일({addr})로" if addr else "이메일로"
    if m == "lms":
        return "LMS(Canvas)로"
    if m == "paper":
        return "출력해서 직접"
    return "[제출 방법]으로"


def _main(purpose: str, item: dict | None, note: str | None) -> list[str]:
    """용건 문단들. 사용자가 note를 주면 자리표시 대신 그 말을 넣는다."""
    title = item.get("title") if item else None
    when = due_text(item.get("due")) if item else None
    is_exam = bool(item) and item.get("kind") == "시험"
    q = f"'{title}'" if title else "'[과제 이름]'"
    if purpose == "extension":
        head = f"{q}(현재 마감 {when})의 제출 기한과 관련하여 연락드립니다." if when else \
            f"{q}의 제출 기한과 관련하여 연락드립니다."
        why = note or "[사정을 적어 주세요] 때문에 기한 안에 제출하기 어렵게 되었습니다."
        return [head + " " + why,
                "혹시 제출 기한을 [희망 날짜]까지 연장해 주실 수 있을지 여쭙고 싶습니다. "
                "연장이 어렵다면 늦게 제출할 때의 규정을 알려 주시면 그에 따르겠습니다."]
    if purpose == "absence":
        why = note or "[날짜] 수업에 [사유]로 출석하지 못하게 되어 미리 말씀드립니다."
        paras = [why + " 공결(출석 인정)을 받을 수 있는지, 받을 수 있다면 어떤 증빙 서류를 어떻게 내면 되는지 알려 주시면 감사하겠습니다."]
        if title:
            paras.append(f"그날 예정된 {q}({when})에 대해서도 어떻게 하면 될지 여쭙고 싶습니다." if when else
                         f"그날 예정된 {q}에 대해서도 어떻게 하면 될지 여쭙고 싶습니다.")
        return paras
    if purpose == "grade":
        head = f"{q}의 성적과 관련하여 문의드립니다." if title else "이번 학기 [평가 항목] 성적과 관련하여 문의드립니다."
        ask = note or "공개된 점수를 확인했는데, [궁금한 부분]에 대해 채점 기준이나 감점된 부분을 알 수 있을지 여쭙고 싶습니다."
        return [head + " " + ask]
    if purpose == "submission":
        obj = f"{q}" + (f"(마감 {when})" if when else "") + (" 답안을" if is_exam else " 과제를")
        how = _how(item) if item else "[제출 방법]으로"
        return [f"{obj} {how} 제출했는데, 제대로 접수되었는지 확인해 주실 수 있을지 여쭙고 싶습니다.",
                note or "제출한 때: [제출한 날짜·시각]"]
    head = f"{q}에 관하여 궁금한 점이 있어 연락드립니다." if title else "수업과 관련하여 궁금한 점이 있어 연락드립니다."
    return [head + " " + (note or "[질문 내용을 적어 주세요]")]


def draft(store, course_id, purpose: str, item_id=None, note=None, names=None, srcs: list | None = None) -> dict:
    """→ {"course_id", "purpose", "item_id", "to": [{"email", "quote", "source_title", "role", "url"}], "subject", "body",
    "notes"}. purpose가 틀리면 ValueError, 모르는 과목·항목은 LookupError. 저장하지 않고 보내지 않는다."""
    if purpose not in PURPOSES:
        raise ValueError("purpose는 extension, absence, grade, submission, question 중 하나여야 해요")
    note = _clean_note(note)
    item = None
    if item_id is not None:
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("item_id는 문자열이어야 해요")
        item = next((v for v in store.view() if v["id"] == item_id), None)
        if item is None:
            raise LookupError("항목을 찾을 수 없습니다. 동기화 후 다시 시도하세요")
        if course_id is not None and str(course_id) != str(item.get("course_id")):
            raise ValueError("항목과 과목이 맞지 않아요")
        course_id = item.get("course_id")
    course = None
    if course_id is not None:
        course = next((c for c in _courses(store) if str(c.get("id")) == str(course_id)), None)
        if course is None and item is None:
            raise LookupError("과목을 찾을 수 없습니다. 동기화 후 다시 시도하세요")
    cname = _short(course.get("name")) if course else (_short(item.get("course_name")) if item else "[과목명]")
    to = recipients(store, course_id, purpose, item, srcs) if course_id is not None else []
    who = _names(names)
    me = who[0] if who else "[이름]"
    role = next((r["role"] for r in to if r["role"] in ("교수", "조교")), None)
    if role is None and purpose == "absence":
        role = "교수"
    greet = f"{role}님, 안녕하세요." if role else "안녕하세요."
    title = item.get("title") if item else None
    subject = (f"[{cname}] " if course or item else "[과목명] ") + (f"{title} " if title else "") + \
        f"{PURPOSE_LABEL[purpose]} - [학번] {me}"
    body = "\n\n".join([greet + f"\n{cname} 수업을 듣고 있는 [학번] {me}입니다."] + _main(purpose, item, note) +
                       ["바쁘신 와중에 읽어 주셔서 감사합니다.\n" + f"{me} 올림"])
    notes = []
    if not to:
        notes.append(NOT_FOUND if course_id is not None else
                     "과목을 알려 주면 그 과목 글에서 교수님·조교 주소를 찾아 넣어요.")
    elif to[0]["role"] == "제출처" and purpose != "submission":
        notes.append(f"첫 번째 받는 사람({to[0]['email']})은 과제 제출 주소예요 — 문의 주소가 따로 있으면 바꿔 주세요.")
    notes.append("[ ]로 표시한 곳을 채운 뒤 직접 보내 주세요. 앱은 메일을 보내지 않아요.")
    return {"course_id": str(course_id) if course_id is not None else None, "purpose": purpose,
            "item_id": item["id"] if item else None, "to": to, "subject": subject, "body": body, "notes": notes}


# ---------------------------------------------------------------- 채팅에서 알아듣기

MAIL = re.compile(r"(?i)메일|(?<![a-z])e-?mail(?![a-z])")
ASK = re.compile(r"(?i)써\s*(?:줘|주|줄|봐|달|야)|쓰(?:기|는\s*법|고\s*싶|려(?:고|는|면)|자)|초안|작성|만들어|보낼|보내려|"
                 r"보내고\s*싶|어떻게\s*보내|문의\s*(?:하려|하고\s*싶|할|메일|이메일)|"
                 r"(?<![a-z])(?:draft|write|compose)(?![a-z])")
_PURPOSE_RX = [
    ("absence", re.compile(r"(?i)결석|공결|출석\s*인정|불참|병결|빠지|빠져|(?<![a-z])(?:absen\w*|miss(?:ed|ing)?\s+(?:the\s+)?class)(?![a-z])")),
    ("extension", re.compile(r"(?i)연장|미뤄|미룰|늦게|기한\s*(?:을|좀)?\s*늘|(?<![a-z])(?:extension|extend|more\s+time)(?![a-z])")),
    ("grade", re.compile(r"(?i)성적|점수|채점|학점|감점|(?<![a-z])(?:grades?|scores?|marks?|grading)(?![a-z])")),
    ("submission", re.compile(r"(?i)제출\s*확인|제출했|냈는데|냈어|접수|들어갔|(?<![a-z])(?:submitted|submission\s+(?:check|confirm\w*))(?![a-z])")),
]
_STATUS_RANK = {"scheduled": 0, "review": 1, "no_deadline": 2, "duplicate": 3, "replaced": 4, "rejected": 5}


# 놓친 것을 알리는 말('안 보여요'·'안 들어와요') — 메일을 곁들여 말해도(제 메일 …) 메일 쓰기 요청이 아니다(계약 22-1의 5번 갈래)
MISSING = re.compile(r"안\s*보여|안\s*보이|안\s*들어와|안\s*들어오|빠졌|빠져\s*있|없어요|놓친|못\s*찾|이상해|안\s*나와|안\s*떠")
DRAFT = re.compile(r"(?i)초안|작성|써\s*(?:줘|주)|(?<![a-z])(?:draft|write|compose)(?![a-z])")


def is_mail_request(message: str) -> bool:
    """메일/이메일 + 쓰기 말(써 줘·작성·초안·보내려고·문의 메일). 놓친 것을 알리는 말이면 '초안·작성·써 줘'가 있을 때만."""
    m = message or ""
    if not (MAIL.search(m) and ASK.search(m)):
        return False
    return not MISSING.search(m) or bool(DRAFT.search(m))


def purpose_of(message: str) -> str:
    for p, rx in _PURPOSE_RX:
        if rx.search(message or ""):
            return p
    return "question"


def _pick_item(message: str, items: list[dict], course_ids: list[str] | None) -> dict | None:
    if not title_tokens(message):
        return None
    cands = [v for v in items if (not course_ids or str(v.get("course_id")) in course_ids)
             and same_task(message, v.get("title") or "")]
    if not cands:
        return None

    def rank(v):
        d = parse_iso(v.get("due"))
        return (_STATUS_RANK.get(v.get("status"), 9), 0 if v.get("newest") else 1,
                d.timestamp() if d else float("inf"))
    return min(cands, key=rank)


def handle(store, message: str, course_id=None, names=None) -> dict | None:
    """"LAB4 연장 문의 메일 써줘" → {"answer", "draft"}. 메일 쓰기 요청이 아니면 None(계약 22-1·22-7)."""
    if not is_mail_request(message):
        return None
    from . import agent
    purpose = purpose_of(message)
    courses = _courses(store)
    if course_id is not None and not any(str(c.get("id")) == str(course_id) for c in courses):
        course_id = None
    cids = [str(course_id)] if course_id is not None else agent.mentioned_courses(message, courses)
    items = store.view()
    item = _pick_item(message, items, cids)
    if item is not None:
        course_id = item.get("course_id")
    elif cids:
        course_id = cids[0]
    elif len(courses) == 1:
        course_id = courses[0].get("id")
    else:
        course_id = None
    srcs = _sources(store)
    d = draft(store, course_id, purpose, item["id"] if item else None, None, names, srcs=srcs)
    what = f"'{item['title']}' " if item else ""
    if d["course_id"] is None:
        listing = ", ".join(_short(c.get("name")) for c in courses[:8])
        answer = (f"{PURPOSE_LABEL[purpose]} 메일 초안을 만들었어요. 어느 과목인지 알려 주면 그 과목 글에서 받는 사람 주소를 찾아 넣어요"
                  + (f"(과목: {listing})." if listing else "."))
    else:
        cname = _short(next((c.get("name") for c in courses if str(c.get("id")) == str(d["course_id"])), None)
                       or (item or {}).get("course_name"))
        answer = f"{cname} {what}{PURPOSE_LABEL[purpose]} 메일 초안이에요."
        if d["to"]:
            first = d["to"][0]
            src = f" — '{first['source_title']}'에 적힌 주소" if first.get("source_title") else ""
            more = f" 외 {len(d['to']) - 1}명" if len(d["to"]) > 1 else ""
            answer += f" 받는 사람: {first['email']}({first['role']}){more}{src}."
        else:
            answer += " 받는 사람 주소는 그 과목 글에서 못 찾았어요 — 직접 넣어 주세요."
    answer += " 보내지는 않았어요. 고친 뒤 복사하거나 메일 앱으로 열어 보내세요."
    return {"answer": answer, "draft": d}
