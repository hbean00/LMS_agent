"""에이전트에게 묻기(채팅) — 계약 20장·22-1·22-2.

메시지를 갈래(kind)로 나눈다(22-1, answer): 명령(actions) → 메일 초안(maildraft) → 공부 계획(plan) → 성적 '만약에'(whatif)
→ 놓친 것(missing, 20장) → 그 밖의 질문(question, 22-2). 앞의 넷은 각 모듈의 handle이 맡고, 첫 번째로 None이 아닌 결과를 대화에 합친다.

놓친 것(missing): 규칙이 놓친 것을 사용자가 말로 짚으면("기말고사 날짜가 안 보여요", "성적이 안 들어와요") 이 PC에 모아 둔 글
(공지·게시판·메시지·강의계획서·과제 설명·첨부 PDF 글·직접 붙여 넣은 글)에서 관련 문단을 찾고(search),
AI가 켜져 있으면 AI CLI 작업 `assist`(agent_cli)에 보내 근거 있는 제안을 받는다. 하네스가 검증한 제안만 반영할 수 있고
(check_proposal), 반영한 것은 decided_by "ai"·이유·되돌리기가 남는다(apply/undo). AI가 꺼져 있으면 같은 창이 규칙 검색으로
답한다(by "rules"). 특정 과목·문장에 맞추지 않는다: 평가 낱말 계열(links.families), 날짜(extract.find_dates),
점수 문장(grades.find_scores)처럼 일반 구조로 점수를 매긴다.

질문(question): 같은 검색(질문 낱말 — 범위·형식·규정·출석·장소 … — 로 점수 보강)으로 찾은 문단을 그대로 인용해 답한다(citations).
AI가 켜져 있으면 작업 `answer`(인용이 보낸 글에 그대로 있고 답의 날짜·숫자가 인용 안에 있는 것만), 아니면 규칙으로 문단 1~3개 인용.

일반 대화(chat, 계약 24-2): 인사·감사·힘듦·시각·도움말·일반 이야기처럼 학업과 무관한 말, 또는 학업 낱말이 없는데 관련 글도 없는 말.
AI가 켜져 있으면 작업 `chat`(페이스메이커 말투, 제안·인용 없음, 과목 사실·'했다'는 말을 지어내지 않음), 아니면 규칙 답(rules_chat).
학업 질문인데 글을 못 찾으면 question('찾지 못했어요') 그대로 두고, AI가 켜져 있으면 끝에 일반 지식 설명을 덧붙인다(general).

이어지는 대화(conversation, 계약 24-1): 차례(thread)마다 conversation id. 같은 대화의 앞 차례(최대 6)를 history로 받아
과목(course_context)을 이어받고, 짧은 생략 질문('그럼 중간은?')은 앞 질문의 주제 낱말을 합쳐(followup) 갈래·검색에 쓴다.
AI 입력에는 '이전 대화'(가림, 1,500자 이내)를 더하되 이전 답은 근거가 아니다(인용 검증은 찾은 글에서만).

저장 칸(store.data):
- "agent": {"threads": [차례, 최근 MAX_THREADS개]} — 차례 = {id, at, message, course_id, conversation, status, kind, answer,
  proposals, citations, searched, results, error, by, finished_at, carried, course_context, …갈래별 칸(whatif·draft·plan·general)}.
  제안의 "_"로 시작하는 칸(되돌리기 기록)은 화면에 내보내지 않는다.
- "agent_items": [반영한 마감 기록] — core.build_items가 함께 합친다(다음 동기화에도 남게, 규칙이 같은 마감을 찾으면 하나로).
- "missed_cases": [규칙이 놓친 사례(가린 문장)] — 사용자가 원할 때만 파일로 저장(자동으로 보내지 않음).
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta

from . import core
from . import links as lk
from .extract import deadline_kind, find_dates, html_to_text
from .store import hidden_courses
from .timeutil import iso, local_tz, now_local, parse_iso

MAX_THREADS = 80          # 남기는 차례 수(계약 24-1, 오래된 것부터 지움)
MAX_CONVERSATIONS = 20    # state.agent.conversations에 보이는 대화 수
HISTORY_TURNS = 6         # 답할 때 넘기는 같은 대화의 앞 차례 수
PER_ASK = 2               # 한 번 묻기에 쓰는 AI 호출(질문 답 + 일반 지식 덧붙이기를 합쳐)
MAX_MESSAGE = 500
SEARCH_LIMIT = 8
SNIPPET_MAX = 500

# 낱말 묶음(언어 무관하게 뜻으로) — 평가 낱말은 links.families(lab·hw·report·quiz·midterm·final·project·attendance·exam)
DATE_WORDS = re.compile(r"날짜|언제|일정|일시|마감|기한|데드라인|며칠|몇\s*시|시간|요일|공지\s*안|date|when|due|deadline|"
                        r"schedule|time", re.I)
SCORE_WORDS = re.compile(r"점수|성적|학점|평균|표준\s*편차|등수|순위|채점|score|grade|marks?|mean|average|std|result",
                         re.I)
_SUFFIXES = sorted(["이에요", "예요", "입니다", "습니다", "해요", "네요", "어요", "아요", "에서", "으로", "이랑", "까지",
                    "부터", "하고", "에게", "한테", "이요", "는", "은", "이", "가", "을", "를", "의", "에", "도", "만",
                    "로", "요", "와", "과"], key=len, reverse=True)
_STOP = {"안", "왜", "좀", "또", "꼭", "더", "제", "내", "저", "나", "뭐", "무엇", "어디", "지금", "아직", "이거", "그거",
         "이번", "문제", "있", "있습", "없", "없어", "보이", "보여", "보이지", "들어오", "들어와", "들어오지", "나와", "나오",
         "나오지", "찾아", "찾", "알려", "주세", "확인", "과목", "수업", "강의", "어떻게", "해주", "됐", "되", "돼", "안돼",
         "같아", "같은", "대한", "관련", "있는", "없는", "the", "a", "an", "is", "are", "was", "not", "no", "i", "my",
         "me", "where", "why", "what", "how", "can", "cannot", "see", "find", "missing", "problem", "there", "it",
         "of", "for", "in", "on", "to", "and", "or", "do", "does", "don't", "doesn't", "show", "shown", "please",
         "어디까지", "어디까지야", "뭐야", "뭔가", "뭐예", "알려줘", "알려주", "있어", "있나", "있나요", "되나", "되나요",
         "하나", "하나요", "해야", "인가", "인가요", "언제야", "몇", "어때", "어떤", "will", "be", "should", "about"}

# 묻기 갈래(계약 22-1): 놓친 것을 짚는 말 — '안 보여요'·'안 들어와요'·'빠졌어요'·'없어요'·'놓친'·'못 찾'·'이상해' …
MISSING_WORDS = re.compile(
    r"안\s*(?:보|들어|나와|나오|떠|뜨|잡혀|잡히)|빠졌|빠져\s*있|빠진|없어요|없네|없습니다|없는데|놓친|놓쳤|못\s*찾|못\s*읽|"
    r"이상해|이상하|이상한|누락|틀렸|틀린|잘못\s*(?:들어|나와|나오|됐|되어|돼)|"
    r"(?<![a-z])(?:missing|not\s+(?:showing|shown|there)|can'?t\s+(?:find|see)|cannot\s+(?:find|see)|"
    r"don'?t\s+see|do\s+not\s+see|doesn'?t\s+show|where\s+can\s+i\s+find)(?![a-z])", re.I)

# 질문 낱말(계약 22-2): 주제마다 (질문 쪽 낱말, 글 쪽 낱말). 질문 쪽에 있으면 글 쪽이 맞는 문단에 점수를 더 준다.
_TOPIC_SRC = {
    "scope": (r"범위|어디까지|진도|출제|챕터|단원|(?<![a-z])(?:scope|coverage|covers?|covered|chapters?|ch\.)(?![a-z])",
              r"범위|진도|출제|챕터|단원|강의\s*자료|복습|(?<![a-z])(?:scope|coverage|covers?|covered|covering|chapters?|"
              r"review|lecture\s+(?:slides|materials|notes))(?![a-z])|(?:from|through|until|up\s+to)\s+(?:week|chapter|"
              r"lecture|ch\.?)\s*\d+|\d+\s*(?:주차|장|단원|챕터)\s*(?:부터|까지|~|-)"),
    "format": (r"형식|양식|포맷|파일\s*(?:명|이름|형식)|확장자|분량|몇\s*(?:쪽|페이지|장)|어떻게\s*(?:제출|내)|"
               r"제출\s*(?:방법|형식|양식|처|할\s*곳)|어디(?:로|에)\s*(?:제출|내)|(?<![a-z])(?:format|file\s*(?:name|type|"
               r"format)|naming|page\s*limit|how\s+to\s+submit|submission\s+format)(?![a-z])",
               r"형식|양식|포맷|파일\s*(?:명|이름)|확장자|분량|첨부|제출|업로드|\d+\s*(?:쪽|페이지|매|pages?|words?)|"
               r"(?<![a-z])(?:format|file\s*name|naming|pdf|zip|docx?|hwp|pptx?|attach(?:ed|ment)?|upload|"
               r"submit(?:ted)?|submission)(?![a-z])"),
    "policy": (r"감점|지각\s*(?:제출|하면|시)|늦게|늦으면|규정|정책|부정\s*행위|표절|재시험|연장\s*(?:되|가능|해\s*주|신청)|"
               r"(?<![a-z])(?:late|penalt(?:y|ies)|polic(?:y|ies)|rules?|cheat(?:ing)?|plagiari[sz]m|make-?up|"
               r"extensions?)(?![a-z])",
               r"감점|지각|늦게|늦은|규정|정책|부정\s*행위|표절|재시험|연장|불인정|인정하지\s*않|받지\s*않|(?<!\d)0\s*점|"
               r"(?<![a-z])(?:late|penalt(?:y|ies)|deduct(?:ed|ion)?|polic(?:y|ies)|rules?|cheat(?:ing)?|plagiari[sz]m|"
               r"make-?up|extensions?|not\s+accepted)(?![a-z])"),
    "attendance": (r"출석|출결|결석|공결|(?<![a-z])(?:attendance|absen(?:t|ce|ces))(?![a-z])",
                   r"출석|출결|결석|공결|호명|(?<![a-z])(?:attendance|absen(?:t|ce|ces))(?![a-z])"),
    "room": (r"시험장|강의실|고사장|장소|어디서|어디에서|어느\s*(?:건물|방|교실|강의실)|대면|비대면|"
             r"(?<![a-z])(?:room|location|venue|building|zoom|in\s+person)(?![a-z])|"
             r"where\s+(?:is|will)\s+(?:the\s+)?(?:exam|final|midterm|quiz|test|class|lecture)",
             r"시험장|강의실|고사장|장소|공학관|(?<!\d)\d{2,4}\s*호(?!\d)|대면|비대면|온라인|"
             r"(?<![a-z])(?:room|location|venue|building|hall|zoom|online|in\s+person|classroom)(?![a-z])"),
    "materials": (r"오픈\s*북|클로즈드\s*북|계산기|지참|준비물|가져가|가져와|치트\s*시트|노트북|"
                  r"(?<![a-z])(?:open[\s-]*book|closed[\s-]*book|calculators?|bring|cheat[\s-]*sheets?|laptops?|"
                  r"allowed)(?![a-z])",
                  r"오픈\s*북|클로즈드\s*북|계산기|지참|준비물|치트\s*시트|노트북|허용|A4|"
                  r"(?<![a-z])(?:open[\s-]*book|closed[\s-]*book|calculators?|bring|cheat[\s-]*sheets?|laptops?|"
                  r"allowed|permitted)(?![a-z])"),
    "grading": (r"비중|배점|반영\s*비율|평가\s*(?:방법|기준|비율|항목)|학점\s*(?:비율|분포|기준)|상대\s*평가|절대\s*평가|"
                r"(?<![a-z])(?:weights?|weighting|grading|grade\s+(?:distribution|scale|cutoffs?)|curve)(?![a-z])",
                r"비중|배점|반영|평가|학점|\d+(?:\.\d+)?\s*%|"
                r"(?<![a-z])(?:weights?|grading|grades?|distribution|curve)(?![a-z])"),
    "contact": (r"연락처|이메일\s*주소|메일\s*주소|오피스\s*아워|면담|상담|"
                r"(?<![a-z])(?:contact|office\s*hours?|e-?mail\s+address)(?![a-z])",
                r"@|연락|이메일|메일|오피스\s*아워|면담|상담|(?<![a-z])(?:contact|office\s*hours?|e-?mail)(?![a-z])"),
}
TOPICS = {k: (re.compile(q, re.I), re.compile(p, re.I)) for k, (q, p) in _TOPIC_SRC.items()}
TOPIC_LABELS = {"scope": "범위", "format": "제출 형식·방법", "policy": "규정(감점·지각·연장 등)", "attendance": "출석",
                "room": "장소", "materials": "준비물·시험 규칙", "grading": "평가 비중·학점 기준", "contact": "연락처·면담"}
TOPIC_WEIGHT = 4.0     # 질문 주제가 문단에 있으면(제목에만 있으면 TOPIC_TITLE_WEIGHT)
TOPIC_TITLE_WEIGHT = 1.5


def question_topics(message: str) -> list[str]:
    """질문이 묻는 주제(범위·형식·규정·출석·장소·준비물·평가 비중·연락처) — 계약 22-2."""
    return [k for k, (q, _) in TOPICS.items() if q.search(message or "")]


def topic_in(topic: str, text) -> bool:
    """그 주제의 글 쪽 낱말이 text에 있는지."""
    return bool(TOPICS[topic][1].search(str(text or "")))


def classify(message: str) -> str:
    """'missing'(놓친 것을 짚음 — 20장 assist·규칙) 또는 'question'(22-2). 명령·메일·계획·만약에는 각 모듈의 handle이 먼저 본다.
    - 놓친 것을 짚는 말(MISSING_WORDS)이 있으면 missing — 단 범위·형식 같은 질문 주제만 있고 앱이 고칠 수 있는 것(날짜·점수·평가
      비중)이 없으면 question('제출 형식이 안 보여요'는 글을 인용해 답하는 게 맞다).
    - 질문 주제가 있으면 question.
    - 날짜를 묻는 말('LAB4 마감 언제야?')은 missing — 마감 찾기 흐름이라야 규칙이 놓친 날짜를 [반영]으로 넣을 수 있다.
    - 그 밖은 question."""
    msg = message or ""
    topics = question_topics(msg)
    fixable = bool(DATE_WORDS.search(msg) or SCORE_WORDS.search(msg) or "grading" in topics)
    if MISSING_WORDS.search(msg):
        return "missing" if (not topics or fixable) else "question"
    if topics:
        return "question"
    if DATE_WORDS.search(msg):
        return "missing"
    return "question"


# ---------------------------------------------------------------- 낱말

def _strip(tok: str) -> str:
    if re.fullmatch(r"[가-힣]+", tok):
        for suf in _SUFFIXES:
            if tok.endswith(suf) and len(tok) - len(suf) >= 2:
                return tok[:-len(suf)]
    return tok


def tokens(message: str) -> list[str]:
    """질문의 낱말(조사·말끝을 떼고, 흔한 말은 뺌). 영어·숫자가 붙은 낱말('LAB4')은 그대로."""
    out = []
    for t in re.findall(r"[A-Za-z][A-Za-z0-9+#\-]*|[가-힣]+|\d+[A-Za-z가-힣]*", message or ""):
        t = _strip(t)
        if len(t) < 2 or t.lower() in _STOP or t.isdigit():
            continue
        if t not in out:
            out.append(t)
    return out


def _keywords(toks: list[str]) -> list[str]:
    """평가·날짜·점수 낱말이 아닌 나머지(과제 이름·특정 낱말)."""
    return [t for t in toks if not lk.families(t) and not DATE_WORDS.search(t) and not SCORE_WORDS.search(t)]


def course_variants(c: dict) -> list[str]:
    short = core.short_course_name(c.get("name"))
    out = {short, re.sub(r"\([^()]*\)", "", short).strip(), str(c.get("course_code") or "")}
    return [v for v in out if len(v) >= 2]


def mentioned_courses(message: str, courses: list[dict]) -> list[str]:
    """질문에 이름(짧은 이름·괄호 뗀 이름·과목 코드, 또는 그 안의 두 글자 이상 낱말)이 나온 과목 id들."""
    msg = (message or "").lower()
    # 평가·날짜·점수 낱말과 질문 주제 낱말 자체('형식', '범위')는 과목 이름 조각으로 보지 않는다('형식언어'는 그대로)
    toks = [t.lower() for t in tokens(message) if not lk.families(t) and not DATE_WORDS.search(t)
            and not SCORE_WORDS.search(t) and not any(q.fullmatch(t) for q, _ in TOPICS.values())]
    out = []
    for c in courses:
        names = [v.lower() for v in course_variants(c)]
        if any(v in msg for v in names) or any(t in v for t in toks for v in names):
            out.append(str(c["id"]))
    return out


# ---------------------------------------------------------------- 글 모으기

def _cache_with_pdfs(store) -> dict:
    """cache + 이 PC의 pdf_texts(동기화 뒤 받은 것까지)."""
    cache = dict(store.data.get("cache") or {})
    pdfs = dict(cache.get("pdf_texts") or {})
    for k, v in (store.data.get("pdf_texts") or {}).items():
        pdfs.setdefault(k, v)
    cache["pdf_texts"] = pdfs
    return cache


def _score_file(pdfs: dict, src: dict) -> bool:
    """점수표 PDF(반 전체 학번·점수)는 검색·AI에 넣지 않는다."""
    if src.get("type") != "pdf":
        return False
    from . import scorefile
    rec = pdfs.get(src["id"]) or {}
    return scorefile.is_score_file(rec.get("filename") or "", src.get("text") or "")


def sources(store, now: datetime | None = None) -> list[dict]:
    """검색할 글(core._sources 전체 + pdf_texts) — 제외한 과목·점수표 PDF는 뺀다. 글마다 plain(태그 없는 글)·key."""
    now = now or now_local()
    hidden = hidden_courses(store.data)
    out = []
    cache = _cache_with_pdfs(store)
    for s in core._sources(cache, store.data.get("manual", []), now):
        if s.get("course_id") is not None and str(s["course_id"]) in hidden:
            continue
        if _score_file(cache["pdf_texts"], s):
            continue
        s = dict(s)
        s["key"] = f"{s['type']}:{s['id']}"
        s["plain"] = html_to_text(s["text"]) if s.get("is_html") else (s.get("text") or "")
        out.append(s)
    return out


def _paragraphs(plain: str) -> list[tuple[int, str]]:
    """(줄 번호, 문단). 짧은 줄(표의 한 칸·제목 줄)은 앞 줄을 붙여 맥락을 남긴다."""
    lines = [ln.strip() for ln in (plain or "").split("\n")]
    lines = [ln for ln in lines if ln]
    out = []
    for i, ln in enumerate(lines):
        text = ln
        if len(ln) < 40 and i > 0 and len(lines[i - 1]) < 120:
            text = lines[i - 1] + "\n" + ln
        out.append((i, text[:SNIPPET_MAX]))
    return out


def _dates_of(text: str, posted: datetime) -> list[dict]:
    return [{"date": h.day.isoformat(), "phrase": re.sub(r"\s+", " ", text[h.start:h.end]).strip(),
             "relative": h.relative} for h in find_dates(text, posted.date())]


def _known_days(items: list[dict]) -> dict:
    """과목별로 이미 항목이 있는 날짜(제외 안 한 것)."""
    out: dict = {}
    for it in items or []:
        d = parse_iso(it.get("due"))
        if d is not None and it.get("status") != "rejected":
            out.setdefault(str(it.get("course_id")), set()).add(d.date().isoformat())
    return out


def search(store, message: str, course_id=None, limit: int = SEARCH_LIMIT, items: list | None = None) -> list[dict]:
    """질문과 가장 관련 있는 문단들(계약 20-2). 점수: 같은 평가 낱말 계열(기말↔final, 과제↔homework …), 날짜를 묻는 질문이면
    날짜가 있는 문단(아직 항목이 없는 날짜면 더), 점수를 묻는 질문이면 점수 문장, 남은 낱말, 이름이 나온 과목.
    course_id를 주면 그 과목 글만. 결과: [{"source_key", "type", "title", "posted_at", "course_id", "course_short",
    "snippet", "dates": [{"date", "phrase", "relative"}], "score", "url"}] — 점수 높은 순, 같은 글에서 최대 2개."""
    results, _ = _search(store, message, course_id, limit, items)
    return results


def _search(store, message, course_id=None, limit=SEARCH_LIMIT, items=None) -> tuple[list[dict], int]:
    now = now_local()
    srcs = sources(store, now)
    if course_id is not None:
        srcs = [s for s in srcs if str(s.get("course_id")) == str(course_id)]
    courses = [c for c in (store.data.get("cache") or {}).get("courses", [])
               if str(c["id"]) not in hidden_courses(store.data)]
    named = set(mentioned_courses(message, courses)) if course_id is None else set()
    shorts = {str(c["id"]): core.short_course_name(c["name"]) for c in courses}
    toks = tokens(message)
    fam_q = lk.families(message)
    keys = [k.lower() for k in _keywords(toks)]
    want_date = bool(DATE_WORDS.search(message or ""))
    want_score = bool(SCORE_WORDS.search(message or ""))
    topics = question_topics(message)
    known = _known_days(items if items is not None else store.view())
    from .grades import find_scores
    scored = []
    for s in srcs:
        title = str(s.get("title") or "")
        posted = s.get("posted_at") if isinstance(s.get("posted_at"), datetime) else now
        t_fam = lk.families(title)
        cid = str(s["course_id"]) if s.get("course_id") is not None else None
        for _, par in _paragraphs(s["plain"]):
            low = par.lower()
            fam_p = lk.families(par)
            # 공지는 주제를 제목에 쓴다('기말고사 공지' + 본문 '날짜: 12월 15일') — 제목의 주제도 본문과 같은 무게
            score = 3.0 * len(fam_q & (fam_p | t_fam))
            dates = _dates_of(par, posted)
            # 질문이 무엇(기말·퀴즈·LAB…)을 가리키면, 그 주제가 이 글(제목·본문)에 있거나 문단이 마감·시험 문장일 때만
            # 날짜 가산 — 강의 슬라이드 첫 장의 날짜('강사 이름 2026.11.26')가 위로 올라오지 않게
            relevant = (not fam_q) or bool(fam_q & (fam_p | t_fam)) or bool(deadline_kind(par))
            if dates:
                if (want_date or fam_q) and relevant:
                    score += 3.0 if want_date else 1.0
                    if any(d["date"] >= now.date().isoformat() for d in dates):
                        score += 1.0
                    if any(d["date"] not in known.get(cid, set()) for d in dates):
                        score += 2.0   # 아직 항목이 없는 날짜 — 사용자가 '안 보인다'고 한 것일 수 있다
                else:
                    score += 0.5
            if want_score:
                if find_scores(par):
                    score += 4.0
                elif SCORE_WORDS.search(par):
                    score += 2.0
                if SCORE_WORDS.search(title):   # 'Lab0, Lab1 Score Announcement'처럼 제목이 점수 글이라고 말할 때
                    score += 3.0
            for tp in topics:   # 질문 낱말(범위·형식·규정·출석·장소 …, 계약 22-2): 그 주제가 적힌 문단이 먼저
                if topic_in(tp, par):
                    score += TOPIC_WEIGHT
                elif topic_in(tp, title):
                    score += TOPIC_TITLE_WEIGHT
            for k in keys:
                if k in low:
                    score += 2.0
                elif k in title.lower():
                    score += 1.0
            if score <= 0:
                continue
            if cid in named:
                score += 4.0
            scored.append((score, posted, s, par, dates, cid))
    scored.sort(key=lambda x: (-x[0], -x[1].timestamp()))
    out, per = [], {}
    for score, posted, s, par, dates, cid in scored:
        if per.get(s["key"], 0) >= 2 or any(r["source_key"] == s["key"] and (par in r["snippet"] or r["snippet"] in par)
                                            for r in out):
            continue
        per[s["key"]] = per.get(s["key"], 0) + 1
        out.append({"source_key": s["key"], "type": s["type"], "title": s.get("title"), "posted_at": iso(posted),
                    "course_id": cid, "course_short": shorts.get(cid) if cid else None, "snippet": par,
                    "dates": dates, "score": round(score, 1), "url": s.get("url")})
        if len(out) >= limit:
            break
    return out, len(srcs)


# ---------------------------------------------------------------- 지금 상태(검증·AI 입력이 함께 쓴다)

KINDS = ("과제", "시험")
METHODS = ("lms", "email", "paper", "unknown")
TYPES = ("add_deadline", "fix_due", "link_score", "set_count", "set_weights", "set_dist", "note", "action")
_KO_WD = "월화수목금토일"


class Ctx:
    """한 번의 묻기·반영에서 쓰는 지금 상태(제외한 과목은 뺌): 과목, 항목(평가 항목 연결까지), 점수 후보, 성적 설정, 글."""

    def __init__(self, store):
        from . import gradebook
        self.store = store
        self.now = now_local()
        cache = store.data.get("cache") or {}
        self.hidden = hidden_courses(store.data)
        self.courses = {str(c["id"]): c for c in cache.get("courses", []) if str(c["id"]) not in self.hidden}
        self.shorts = {cid: core.short_course_name(c["name"]) for cid, c in self.courses.items()}
        view = store.view()
        self.sem = core.semester_info(cache, view)
        core.annotate(view, self.sem, cache)
        gradebook.link_items(store, view, self.sem)
        self.items = view
        self.by_id = {v["id"]: v for v in view}
        self.cands = gradebook.candidates(store)
        self.grades = store.data.get("grades") or {}
        self._srcs = None

    def sources(self) -> dict:
        if self._srcs is None:
            self._srcs = {s["key"]: s for s in sources(self.store, self.now)}
        return self._srcs

    def components(self, cid) -> list[dict]:
        return [c for c in (self.grades.get(str(cid)) or {}).get("components") or [] if isinstance(c, dict)]

    def component(self, cid, name) -> str | None:
        from .gradebook import _course_component
        return _course_component(self.store, cid, name)

    def links(self, cid) -> dict:
        from .gradebook import link_candidates
        mine = [c for c in self.cands if str(c.get("course_id")) == str(cid)]
        return link_candidates(self.store, cid, self.components(cid), mine) if self.components(cid) else {}


def _flat(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _md(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}({_KO_WD[dt.weekday()]})" + ("" if (dt.hour, dt.minute) == (23, 59) else f" {dt:%H:%M}")


def same_deadline(a: dict, b: dict) -> bool:
    """같은 과목의 같은 마감인지(중복 거절·규칙 항목과 하나로 합치기): 같은 날이고, 같은 과제(extract.same_task)이거나
    마감 시각까지 같은 같은 종류, 또는 둘 다 시험이고 시험 계열(quiz·exam)이 겹침(번호가 다르면 다른 것)."""
    from .extract import same_task, title_tokens
    if str(a.get("course_id")) != str(b.get("course_id")):
        return False
    da, db = parse_iso(a.get("due")), parse_iso(b.get("due"))
    if da is None or db is None or da.date() != db.date():
        return False
    ta, tb = str(a.get("title") or ""), str(b.get("title") or "")
    if same_task(ta, tb) or _flat(ta).lower() == _flat(tb).lower():
        return True
    na = {(k, n) for k, n in title_tokens(ta) if n}
    nb = {(k, n) for k, n in title_tokens(tb) if n}
    if any(k == k2 and n != n2 for k, n in na for k2, n2 in nb):
        return False
    if da == db and a.get("kind") == b.get("kind"):
        return True
    return (a.get("kind") == b.get("kind") == "시험"
            and bool(lk.families(ta) & lk.families(tb) & {"quiz", "exam"}))


def _evidence_home(ctx: Ctx, p: dict, quote: str) -> tuple[dict | None, str | None]:
    """근거가 있는 곳: (글, 원문 인용). source_key의 글 → 다른 글 → 점수 후보 문장 순. 없으면 (None, None)."""
    q = _flat(quote)
    if len(q) < 4:
        return None, None
    srcs = ctx.sources()
    order = [srcs[p["source_key"]]] if p.get("source_key") in srcs else []
    order += [s for k, s in srcs.items() if k != p.get("source_key")]
    for s in order:
        if q in _flat(s["plain"]):
            return s, q
    for c in ctx.cands:
        for t in (c.get("sentence"), c.get("label"), c.get("source_title")):
            if t and q in _flat(t):
                return {"key": None, "type": c.get("source_type"), "title": c.get("source_title"),
                        "posted_at": parse_iso(c.get("posted_at")), "course_id": c.get("course_id"), "url": c.get("url"),
                        "plain": str(t)}, q
    return None, None


def _original(quote: str, originals: list | None, names: list | None) -> str:
    """AI가 가린 글에서 인용한 문장 → 이 PC의 원문 문장(계약 13-3: 화면에 [이메일] 같은 자리 표시가 나오지 않게)."""
    from .agent_cli import original_quote, redact, unplace
    q = _flat(quote)
    for t in originals or []:
        if q and q in _flat(redact(str(t), names)[0]):
            return _flat(original_quote(q, [{"text": t}], names))
    return _flat(unplace(q))


def _due(ctx: Ctx, p: dict, quote: str, home: dict | None, errs: list, key: str = "due"):
    from . import agent_cli
    due, notes = agent_cli.parse_due(str(p.get(key) or ""))
    if due is None:
        errs.append("마감(due)이 ISO 8601 날짜가 아니에요 / due is not ISO 8601")
        return None, notes, None
    posted = (home or {}).get("posted_at") if isinstance((home or {}).get("posted_at"), datetime) else ctx.now
    ev = [{"text": quote, "posted_at": iso(posted), "source_type": (home or {}).get("type")}]
    if not (posted - timedelta(days=60) <= due <= posted + timedelta(days=400)):
        errs.append("마감이 글의 게시일과 너무 멀어요 / due is implausibly far from the post date")
    elif not agent_cli._due_supported(due, ev):
        errs.append("마감 날짜가 근거 문장의 날짜와 ±1일 넘게 달라요 / due is not within ±1 day of a date in the evidence")
    basis = agent_cli.date_basis(ev, due)
    return due, notes, (basis if basis and basis.get("relative") else None)


_STATUS_TAG = {"rejected": " · 제외함", "review": " · 확인함", "scheduled": " · 일정", "replaced": " · 대체됨",
               "duplicate": " · 겹침"}


def check_proposal(ctx: Ctx, p: dict, by: str = "ai", why: str | None = None, originals: list | None = None,
                   names: list | None = None) -> dict:
    """제안 하나를 검사해 화면용 모양으로(계약 20-3). 통과하지 못하면 rejected_reason(한국어)이 붙고 [반영] 단추가 없다.
    why: 하네스가 먼저 찾은 문제(근거가 보낸 글에 그대로 없음 등)."""
    from . import agent_cli
    p = p if isinstance(p, dict) else {}
    typ = str(p.get("type") or "")
    if typ == "action":   # 앱 동작(계약 22-9) — 근거 글 대신 actions.check가 id·params를 검사한다
        return check_action(ctx.store, p, by=by, why=why, shorts=ctx.shorts)
    errs: list[str] = [why] if why else []
    if typ not in TYPES:
        errs.append(f"모르는 제안 종류 '{typ[:20]}' / unknown proposal type")
    quote = _original(p.get("evidence") or "", originals, names) if p.get("evidence") else ""
    home, quote2 = _evidence_home(ctx, p, quote) if quote else (None, None)
    if quote and home is None and not why:
        errs.append("근거 문장이 이 PC의 글에 없어요(글이 바뀌었을 수 있어요) / evidence not found in the stored texts")
    if typ != "note" and not quote:
        errs.append("근거(evidence)가 없어요 / evidence is required")
    cid = p.get("course_id")
    if cid in (None, ""):
        cid = str(home["course_id"]) if home and home.get("course_id") is not None else None
    else:
        cid = str(cid)
    if cid is not None and cid not in ctx.courses:
        errs.append("과목 목록에 없는 과목이에요(제외한 과목 포함) / unknown or hidden course_id")
    elif cid is None and typ != "note":
        errs.append("과목(course_id)이 없어요 / course_id is required")
    elif home and home.get("course_id") is not None and cid is not None and str(home["course_id"]) != cid:
        errs.append(f"근거 글은 다른 과목({ctx.shorts.get(str(home['course_id']), home['course_id'])})이에요 "
                    "/ the evidence belongs to another course")
    reason = _flat(agent_cli.unplace(str(p.get("reason") or "")))[:300]
    if not reason:
        errs.append("이유(reason)가 없어요 / reason is required")
    posted = home.get("posted_at") if home and isinstance(home.get("posted_at"), datetime) else None
    out = {"type": typ, "by": by, "course_id": cid, "course_short": ctx.shorts.get(cid) if cid else None,
           "evidence": quote2 or quote or None, "source_key": (home or {}).get("key") or p.get("source_key"),
           "source_type": (home or {}).get("type"), "source_title": (home or {}).get("title"),
           "posted_at": iso(posted), "url": (home or {}).get("url"), "reason": reason, "summary": "",
           "rejected_reason": None, "applied": False, "applied_at": None, "undone_at": None, "result": None,
           "notes": [], "date_basis": None}
    short = f"[{out['course_short']}] " if out["course_short"] else ""
    check = _CHECKS.get(typ)
    if check is not None:
        check(ctx, p, out, errs, quote, home, short)
    elif typ == "note":
        out["summary"] = "설명만(반영할 것 없음)"
    out["rejected_reason"] = "; ".join(dict.fromkeys(e for e in errs if e))[:400] or None
    return out


def _check_add(ctx, p, out, errs, quote, home, short):
    from . import agent_cli
    title = _flat(agent_cli.unplace(str(p.get("title") or "")))[:120]
    kind = p.get("kind") if p.get("kind") in KINDS else "과제"
    method = p.get("method") if p.get("method") in METHODS else "unknown"
    if not title:
        errs.append("제목(title)이 없어요 / title is required")
    due, notes, basis = _due(ctx, p, quote, home, errs) if quote else (None, [], None)
    out.update(title=title, kind=kind, method=method, due=iso(due), notes=notes, date_basis=basis)
    if due is None:
        return
    if basis and agent_cli.claims_explicit(out["reason"]):
        errs.append(f"근거는 상대 날짜('{basis['phrase']}')인데 이유가 날짜가 적혀 있다고 해요 — 계산한 날짜라고 밝혀야 해요 "
                    "/ relative date: do not claim it is explicitly stated")
    me = {"course_id": out["course_id"], "due": iso(due), "title": title, "kind": kind}
    dup = next((v for v in ctx.items if same_deadline(me, v)), None)
    if dup is not None:
        errs.append(f"이미 있어요: {dup['title']} {_md(parse_iso(dup['due']))}{_STATUS_TAG.get(dup['status'], '')}")
    out["summary"] = f"{short}{title} · {_md(due)} · {kind} 추가"


def _check_fix(ctx, p, out, errs, quote, home, short):
    from . import agent_cli
    it = ctx.by_id.get(str(p.get("item_id") or ""))
    if it is None:
        errs.append("항목 목록에 없는 item_id예요 / unknown item_id")
    elif out["course_id"] is not None and str(it.get("course_id")) != out["course_id"]:
        errs.append("그 항목은 다른 과목이에요 / item belongs to another course")
    due, notes, basis = _due(ctx, p, quote, home, errs) if quote else (None, [], None)
    out.update(item_id=str(p.get("item_id") or ""), due=iso(due), date_basis=basis,
               item_title=it["title"] if it else None)
    if it is None or due is None:
        return
    old = parse_iso(it.get("due"))
    if old == due:
        errs.append("지금 마감과 같아요 / same as the current due")
    if basis and agent_cli.claims_explicit(out["reason"]):
        errs.append(f"근거는 상대 날짜('{basis['phrase']}')인데 이유가 날짜가 적혀 있다고 해요 / relative date claimed explicit")
    out["summary"] = f"{short}{it['title']} 마감 {_md(old) if old else '없음'} → {_md(due)}"


def _check_link(ctx, p, out, errs, quote, home, short):
    cid, key = out["course_id"], str(p.get("key") or "")
    cand = next((c for c in ctx.cands if c.get("key") == key and str(c.get("course_id")) == str(cid)), None)
    comp = ctx.component(cid, p.get("component")) if cid else None
    if cand is None:
        errs.append("그 과목의 점수 후보에 없는 key예요 / unknown candidate key")
    if comp is None:
        errs.append("그 과목에 없는 평가 항목이에요 / component is not in this course")
    out.update(key=key, component=comp or p.get("component"), score_label=(cand or {}).get("label"))
    if cand is None or comp is None:
        return
    ln = ctx.links(cid).get(key) or {}
    if ln.get("by") == "user":
        errs.append("직접 연결한 것이 있어요 — 사용자 연결이 이겨요 / the user already linked it")
    elif ln.get("component") == comp:
        errs.append(f"이미 '{comp}'에 연결돼 있어요 / already linked")
    out["summary"] = f"{short}'{cand.get('label') or cand.get('source_title')}' 점수 → {comp}"


def _check_count(ctx, p, out, errs, quote, home, short):
    cid = out["course_id"]
    comp = ctx.component(cid, p.get("component")) if cid else None
    n = p.get("n")
    out.update(component=comp or p.get("component"), n=n)
    if comp is None:
        errs.append("그 과목에 없는 평가 항목이에요 / component is not in this course")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= lk.MAX_COUNT:
        errs.append(f"개수(n)는 1~{lk.MAX_COUNT} 정수여야 해요 / n must be an integer 1..{lk.MAX_COUNT}")
        return
    if comp is None:
        return
    cnt = _current_count(ctx, cid, comp)
    if cnt and cnt.get("by") == "user":
        errs.append("직접 정한 개수가 있어요 / the user already set the count")
    elif cnt and cnt.get("basis") in ("stated", "hint") and cnt.get("by") == "rules":
        errs.append(f"규칙이 이미 '{_flat(cnt.get('evidence'))[:60]}'에서 {cnt.get('n')}개로 찾았어요")
    elif cnt and isinstance(cnt.get("low"), int) and n < cnt["low"]:
        errs.append(f"이미 본 번호로 최소 {cnt['low']}개예요 / below the {cnt['low']} already seen")
    out["summary"] = f"{short}{comp} 모두 {n}개"


def _check_weights(ctx, p, out, errs, quote, home, short):
    comps, total = [], 0.0
    for c in p.get("components") or []:
        name = _flat(c.get("name"))[:40] if isinstance(c, dict) else ""
        w = c.get("weight") if isinstance(c, dict) else None
        if not name or isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 <= w <= 100:
            errs.append("평가 항목마다 이름과 0~100 비중이 필요해요 / each component needs a name and a 0..100 weight")
            break
        comps.append({"name": name, "weight": float(w)})
        total += float(w)
    if not comps or len(comps) > 20:
        errs.append("평가 항목이 1~20개여야 해요 / need 1..20 components")
    elif abs(total - 100) > 1:
        errs.append(f"비중 합이 {total:g}%예요 — 100이어야 해요 / weights sum to {total:g}, must be 100")
    if len({c["name"].lower() for c in comps}) != len(comps):
        errs.append("같은 이름의 항목이 둘이에요 / duplicate component names")
    if out["course_id"] and _user_weights(ctx, out["course_id"]):
        errs.append("직접 넣은 평가 비중이 있어요 — 덮지 않아요 / the user already set the weights")
    out["components"] = comps
    out["summary"] = f"{short}평가 비중 " + " · ".join(f"{c['name']} {c['weight']:g}%" for c in comps[:8])


def _check_dist(ctx, p, out, errs, quote, home, short):
    from . import agent_cli
    from . import distribution as dm
    from .store import course_mode
    df = None
    absolute = p.get("dist_kind") == "absolute"
    try:
        if absolute:
            cut = {str(c.get("grade")): float(c.get("min_score")) for c in p.get("cutoffs") or [] if isinstance(c, dict)}
            df = dm.from_groups("absolute", cutoffs=cut, sentence=quote)
        else:
            df = dm.from_groups("relative", groups=p.get("groups") or [], sentence=quote)
    except (ValueError, TypeError, AttributeError) as e:
        errs.append(str(e)[:200])
    if quote and not agent_cli._GRADE_IN.search(quote):
        errs.append("근거에 학점(A+, B0 등)이 없어요 / evidence must contain the letter grades")
    cid = out["course_id"]
    if (ctx.grades.get(str(cid)) or {}).get("dist"):
        errs.append("직접 넣은 학점 비율이 있어요 — 덮지 않아요 / the user already set the distribution")
    if cid and course_mode(ctx.store.data, cid) == "pass_fail":
        errs.append("P/NP 과목이에요 — 학점 비율이 필요 없어요 / pass/fail course")
    out.update(dist_kind="absolute" if absolute else "relative", groups=p.get("groups") or [],
               cutoffs=p.get("cutoffs") or [], dist=df.as_dict() if df is not None else None)
    if df is not None:
        out["summary"] = f"{short}학점 비율 {dm.summary(df)}"


_CHECKS = {"add_deadline": _check_add, "fix_due": _check_fix, "link_score": _check_link, "set_count": _check_count,
           "set_weights": _check_weights, "set_dist": _check_dist}


def check_action(store, p: dict, by: str = "rules", why: str | None = None, shorts: dict | None = None) -> dict:
    """액션 제안(계약 22-9)을 화면용 모양으로: actions.check(store, p)가 id·params를 검사(모르는 것·금지 목록은 rejected_reason).
    결과 칸: 제안 공통 칸(근거 칸은 None) + action, params, title, client, undoable. 서버 동작만 [실행] 가능(client는 화면이 직접)."""
    from . import actions
    from .agent_cli import unplace
    p = p if isinstance(p, dict) else {}
    raw = {"type": "action", "action": str(p.get("action") or "")[:80],
           "params": copy.deepcopy(p["params"]) if isinstance(p.get("params"), dict) else {}}
    for k in ("title", "reason"):
        if p.get(k):
            raw[k] = str(p[k])
    try:
        q = actions.check(store, dict(raw))
        if not isinstance(q, dict):
            raise TypeError("check는 dict를 돌려줘야 해요")
    except Exception as e:  # noqa: BLE001 — 확인하지 못한 동작은 실행 단추 없이 보여 준다
        q = {**raw, "rejected_reason": f"동작을 확인하지 못했어요({type(e).__name__})"}
    entry = next((c for c in getattr(actions, "CATALOG", None) or []
                  if isinstance(c, dict) and c.get("id") == (q.get("action") or raw["action"])), {})

    def pick(key):   # check 결과 → handle이 준 값 → 목록(CATALOG) 순
        for src in (q, p, entry):
            if key in src and src[key] is not None:
                return src[key]
        return None

    errs = [why] if why else []
    if q.get("rejected_reason"):
        errs.append(_flat(q["rejected_reason"]))
    reason = _flat(unplace(str(p.get("reason") or q.get("reason") or "")))[:300]
    if not reason and by == "ai":
        errs.append("이유(reason)가 없어요 / reason is required")
    params = q.get("params") if isinstance(q.get("params"), dict) else raw["params"]
    title = _flat(pick("title") or raw["action"])[:120]
    cid = params.get("course_id")
    cid = str(cid) if cid not in (None, "") else None
    out = {"type": "action", "by": by, "action": str(q.get("action") or raw["action"]), "params": params,
           "title": title, "summary": title, "client": bool(pick("client")), "undoable": bool(pick("undoable")),
           "course_id": cid, "course_short": (shorts or {}).get(cid) if cid else None, "evidence": None,
           "source_key": None, "source_type": None, "source_title": None, "posted_at": None, "url": None,
           "reason": reason, "rejected_reason": "; ".join(dict.fromkeys(e for e in errs if e))[:400] or None,
           "applied": False, "applied_at": None, "undone_at": None, "result": None,
           "notes": [str(n) for n in q.get("notes") or [] if n][:5], "date_basis": None}
    return out


def _current_count(ctx: Ctx, cid, comp: str) -> dict | None:
    from . import gradebook
    lcomps = gradebook._lcomps(ctx.grades.get(str(cid)))
    target = next((c for c in lcomps if c.name == comp), None)
    if target is None:
        return None
    mine = [c for c in ctx.cands if str(c.get("course_id")) == str(cid)]
    obs = gradebook._observations(ctx.store, str(cid), mine, ctx.items)
    try:
        texts = gradebook._course_texts(ctx.store).get(str(cid), [])
    except KeyError:   # 다른 스레드가 같은 기억을 바꾸는 중(드묾) — 반영할 때 다시 검사한다
        texts = []
    return gradebook._count_for(ctx.store, str(cid), target, lcomps, obs, texts)


def _user_weights(ctx: Ctx, cid) -> bool:
    from .gradebook import _is_user_edited
    return _is_user_edited(ctx.grades.get(str(cid)))


# ---------------------------------------------------------------- 지금 상태 요약(AI에 보냄, 가리기는 agent_cli가)

MAX_SCOPE = 6


def _date_status(ctx: Ctx, cid: str, name: str) -> str:
    rows = [v for v in ctx.items if str(v.get("course_id")) == cid and v.get("component") == name
            and v.get("status") in ("review", "scheduled") and parse_iso(v.get("due"))]
    if not rows:
        return "not_announced"
    return "announced" if any(parse_iso(v["due"]) >= ctx.now for v in rows) else "past"


def scope_of(ctx: Ctx, message: str, course_id, results: list[dict]) -> list[str]:
    """요약에 넣을 과목: 지정한 과목 → 이름이 나온 과목 → 찾은 글의 과목 → 모든 과목(최대 MAX_SCOPE)."""
    if course_id is not None:
        return [str(course_id)] if str(course_id) in ctx.courses else []
    named = mentioned_courses(message, list(ctx.courses.values()))
    if named:
        return named[:MAX_SCOPE]
    seen = [r["course_id"] for r in results if r.get("course_id") in ctx.courses]
    out = list(dict.fromkeys(seen))
    for cid in ctx.courses:
        if len(out) >= MAX_SCOPE:
            break
        if cid not in out:
            out.append(cid)
    return out[:MAX_SCOPE]


def state_text(ctx: Ctx, cids: list[str]) -> tuple[str, list[str]]:
    """과목마다 항목(제목·마감·상태·평가 항목), 평가 항목·비중·일정, 점수 후보와 연결 상태, 학점 비율 출처. 점수표 PDF의 줄은 넣지
    않는다(라벨·출처만). (글, 원문 조각들 — AI가 가린 인용을 원문으로 되돌릴 때 쓴다)."""
    from . import distribution as dm
    sem = ctx.sem
    lines = [f"학기 / term: {sem.get('week_now')}주차 / {sem.get('weeks_total')}주 (개강 {sem.get('start')})",
             f"오늘 / today: {ctx.now.date().isoformat()}"]
    originals: list[str] = []
    for cid in cids:
        lines.append(f"\n## course_id {cid} · {ctx.shorts.get(cid)}")
        mine = [v for v in ctx.items if str(v.get("course_id")) == cid]
        lines.append("항목 / items:" if mine else "항목 / items: 없음 / none")
        for v in mine[:25]:
            lines.append(f"- item_id {v['id']} | {v['title']} | {v.get('kind')} | due {v.get('due') or '없음/none'} | "
                         f"{v['status']}" + (f" | 평가 항목/component: {v['component']}" if v.get("component") else ""))
        cfg = ctx.grades.get(cid) or {}
        comps = ctx.components(cid)
        if comps:
            src = f" (출처/source: {_flat(cfg.get('auto_source'))[:80]})" if cfg.get("auto_source") else ""
            lines.append(f"평가 항목 / grading components{src}:")
            for c in comps:
                lines.append(f"- {c.get('name')} {float(c.get('weight') or 0):g}% | 일정/dates: "
                             f"{_date_status(ctx, cid, str(c.get('name')))}")
        else:
            lines.append("평가 항목 / grading components: 없음(비중을 못 찾음) / none found")
        links = ctx.links(cid)
        cands = [c for c in ctx.cands if str(c.get("course_id")) == cid][:15]
        if cands:
            lines.append("점수 후보 / score candidates:")
        for c in cands:
            ln = links.get(c.get("key")) or {}
            wk = core.week_of(sem, c.get("posted_at")) if sem.get("weeks") else None
            linked = f"{ln['component']} ({ln.get('by')})" if ln.get("component") else \
                f"없음/none — {ln.get('reason') or '평가 항목이 없어요'}"
            row = (f"- key {c['key']} | 라벨/label: {c.get('label') or '-'} | 출처/source: {c.get('source_type')} "
                   f"'{c.get('source_title') or ''}' {str(c.get('posted_at') or '')[:10]}"
                   + (f" (주차/week {wk})" if wk else "") + f" | 연결/linked: {linked}")
            if c.get("source_type") != "pdf" and c.get("sentence"):
                row += f" | 글/text: {_flat(c['sentence'])[:200]}"
                originals.append(str(c["sentence"]))
            lines.append(row)
            originals += [str(x) for x in (c.get("label"), c.get("source_title")) if x]
        eff = dm.effective(cfg)
        how = {"user": "직접 정함 / set by user", "syllabus": f"강의 글 / from course text "
               f"({(eff.get('auto') or {}).get('source_title') or ''})", "default": "기본값 / default"}[eff["by"]]
        lines.append(f"학점 비율 / grade distribution: {how}")
    return "\n".join(lines), originals


# ---------------------------------------------------------------- 규칙으로 답하기(AI 꺼짐·실패)

def _exam_intent(fam: set) -> bool:
    return bool(fam & {"quiz", "exam"}) or bool(fam & {"midterm", "final"} and not fam & {"project", "report", "hw", "lab"})


_TASK_FAMS = {"project", "report", "hw", "lab"}
GENERIC_TITLE = "마감 후보"


def guess_title(ctx: Ctx, cid, sentence: str, source_title: str | None = None) -> tuple[str, str, bool]:
    """근거 문장 자체에서 (제목, 종류, 추정인지). 질문의 낱말은 쓰지 않는다 — 'LAB4 보고서 마감 연장' 문장에 '기말고사'를 물었다고
    'Final quiz'를 붙이지 않게. 종류는 문장의 낱말로(과제 낱말 → 과제, 시험·중간·기말 → 시험). 제목은 ① 문장이 그 평가 항목을
    가리킬 때만(학기 시점 낱말이 같고, 같은 계열이거나 같은 종류의 시험 항목) '날짜 미공지' 항목 이름 ② 문장의 과제·시험 이름
    (extract.title_hint) ③ 문장의 학기 시점 낱말 + 시험('기말 시험', 'Final exam') ④ 글 제목의 이름 ⑤ '마감 후보'(추정)."""
    from .extract import title_hint
    fs = lk.families(sentence) or lk.families(source_title)
    kind = "과제" if fs & _TASK_FAMS else "시험" if _exam_intent(fs) else "과제"
    picks = []
    for c in ctx.components(cid) if cid else []:
        name = str(c.get("name") or "")
        cf = lk.families(name)
        terms = cf & set(lk.TERM)
        if not cf & fs or not terms <= fs or (fs & set(lk.TERM)) - terms:
            continue   # 'Final quiz'는 문장에 final·기말이 있을 때만, 'Midterm project'는 midterm이 있을 때만
        if (kind == "시험") != lk.is_exam_component(name):
            continue
        if not (cf - set(lk.TERM)) & fs and kind != "시험":
            continue   # 과제는 같은 계열(실습·보고서·프로젝트…)이 문장에 있어야 한다
        if _date_status(ctx, str(cid), name) == "not_announced":
            picks.append(name)
    if len(picks) == 1:
        return picks[0], kind, False
    hint = title_hint(sentence, kind)
    if hint:
        return hint, kind, False
    if kind == "시험":
        for f in ("final", "midterm"):
            m = re.search(lk.FAMILY_RX[f], sentence, re.I)
            if m:
                w = m.group(0)
                return (f"{w} 시험" if re.search(r"[가-힣]", w) else f"{w.title()} exam"), kind, True
    hint = title_hint(source_title or "", kind)
    if hint:
        return hint, kind, True
    return ("시험 후보" if kind == "시험" else GENERIC_TITLE), kind, True


def _rules_proposals(ctx: Ctx, message: str, results: list[dict]) -> list[dict]:
    """날짜를 묻는(또는 평가 낱말이 있는) 질문이면, 날짜가 있는 문단마다 [마감 후보로 넣기] 제안 하나(최대 3개).
    날짜는 문단의 날짜 표현(extract.find_dates)·시각(find_time)으로, 사용자가 확인하고 넣는다(확인함 후보로)."""
    from .extract import _choose, find_time, split_sentences
    if not (DATE_WORDS.search(message or "") or lk.families(message)):
        return []
    out = []
    fq = lk.families(message)
    for r in results:
        if len(out) >= 3:
            break
        posted = parse_iso(r.get("posted_at")) or ctx.now
        par = r["snippet"]
        if r.get("course_id") not in ctx.courses:
            continue
        t_fam = lk.families(r.get("title"))
        pick = None
        for line in reversed(par.split(chr(10))):     # 이 줄 먼저(앞 줄은 맥락으로 붙인 것)
            for sentence in split_sentences(line):
                if fq and not fq & (lk.families(sentence) | t_fam):
                    continue   # '기말고사'를 물었는데 평가 낱말이 다른 문장(관람·휴강·다른 과제 줄)은 제안하지 않는다
                hits = [h for h in find_dates(sentence, posted.date()) if h.day >= ctx.now.date() - timedelta(days=1)]
                if hits:
                    pick = (sentence, hits)
                    break
            if pick:
                break
        if pick is None:
            continue
        sentence, hits = pick
        h = _choose(sentence, hits)
        nxt = min((x.start for x in hits if x.start >= h.end), default=len(sentence))
        t, notes = find_time(sentence[h.end:nxt])
        if t is None:
            t, notes = datetime.min.time().replace(hour=23, minute=59), ["시간 미기재 → 23:59로 둠"]
        title, kind, guessed = guess_title(ctx, r["course_id"], sentence, r.get("title"))
        if guessed:
            notes = notes + ["제목은 추정이에요 — 맞는 이름으로 고쳐 주세요"]
        due = datetime.combine(h.day, t, tzinfo=posted.tzinfo)
        p = {"type": "add_deadline", "course_id": r["course_id"], "evidence": sentence, "source_key": r["source_key"],
             "title": title, "due": iso(due), "kind": kind,
             "reason": "규칙 검색으로 찾은 날짜예요 — 날짜·제목이 맞는지 확인하고 넣어 주세요"}
        q = check_proposal(ctx, p, by="rules", originals=[par])
        q["notes"] = list(dict.fromkeys((q.get("notes") or []) + notes))
        if not any(x.get("summary") == q.get("summary") for x in out):
            out.append(q)
    return out


def diagnose(ctx: Ctx, message: str, cids: list[str]) -> list[str]:
    """규칙으로 짚을 수 있는 원인(한국어 문장들): 점수를 넣을 평가 항목이 없음·연결 못 함·학번 필요, 날짜 미공지 시험 항목."""
    from . import gradebook
    out = []
    if SCORE_WORDS.search(message or ""):
        for cid in cids:
            mine = [c for c in ctx.cands if str(c.get("course_id")) == cid]
            if not mine:
                continue
            short = ctx.shorts.get(cid)
            if not ctx.components(cid):
                out.append(f"'{short}'에는 평가 항목(비중)이 없어서 점수 {len(mine)}개('{mine[0].get('label') or '점수'}' 등)를 "
                           "성적 계산에 넣을 곳이 없어요 — 강의 글에서 평가 비중을 못 찾았어요(성적 화면에서 직접 넣을 수 있어요).")
                continue
            links = ctx.links(cid)
            loose = [c for c in mine if not (links.get(c.get("key")) or {}).get("component")]
            for c in loose[:3]:
                why = (links.get(c.get("key")) or {}).get("reason") or "어느 항목인지 정하지 못함"
                out.append(f"'{short}'의 점수 '{c.get('label') or c.get('source_title')}'은(는) 평가 항목에 연결되지 않았어요"
                           f"({_flat(why)[:80]}).")
            if not loose:
                out.append(f"'{short}'의 점수 {len(mine)}개는 모두 평가 항목에 연결돼 있어요.")
        waiting, unmatched = gradebook.score_files(ctx.store)
        if waiting:
            out.append(f"받아 둔 점수표 PDF {waiting}개가 있어요 — 학번을 넣으면(설정 → 성적) 내 점수를 찾아요.")
        if unmatched:
            out.append("학번으로 내 줄을 못 찾은 점수표: " + ", ".join(unmatched[:3]) + " — 학번을 확인해 주세요.")
    if DATE_WORDS.search(message or "") or _exam_intent(lk.families(message)):
        fq = lk.families(message)
        for cid in cids:
            for c in ctx.components(cid):
                name = str(c.get("name") or "")
                if (lk.is_exam_component(name) and (not fq or lk.families(name) & fq)
                        and _date_status(ctx, cid, name) == "not_announced"):
                    out.append(f"'{ctx.shorts.get(cid)}' {name}({float(c.get('weight') or 0):g}%)는 아직 날짜가 있는 일정이 없어요.")
    return out[:6]


def rules_answer(ctx: Ctx, message: str, results: list[dict], searched: int, cids: list[str],
                 ai_error: str | None = None, ai_enabled: bool = False) -> tuple[str, list[dict]]:
    props = _rules_proposals(ctx, message, results)
    parts = []
    if results:
        parts.append(f"이 PC의 글 {searched}개에서 관련 문단 {len(results)}개를 찾았어요(규칙 검색).")
        if any(not p["rejected_reason"] for p in props):
            parts.append("날짜가 있는 문단은 [마감 후보로 넣기]로 확인함에 넣을 수 있어요 — 날짜·제목을 꼭 확인해 주세요.")
        known = [p for p in props if str(p.get("rejected_reason") or "").startswith("이미 있어요")]
        if known:
            parts.append("이미 있는 마감: " + "; ".join(p["rejected_reason"].split(": ", 1)[-1] for p in known[:2]) + ".")
    else:
        parts.append("관련된 글을 찾지 못했어요. 과목 이름이나 다른 낱말(예: final, quiz, 과제 이름)로 다시 물어봐 주세요.")
    parts += diagnose(ctx, message, cids)
    if ai_error:
        parts.append(f"AI에게 묻지 못해 규칙 검색으로 답했어요: {ai_error}")
    elif not ai_enabled:
        parts.append("설정 → AI에서 AI CLI를 켜면 찾은 글을 AI가 읽고 근거와 함께 바로 반영할 수 있는 제안을 드려요.")
    return " ".join(parts), props


# ---------------------------------------------------------------- 과목 질문에 답하기(22-2)

QUOTE_MAX = 500
MAX_CITATIONS = 3
MIN_AI_SCORE = 1.0     # 검색 점수가 이보다 큰 문단이 하나도 없으면(날짜 가산 0.5뿐) AI에게 보낼 글이 없는 것
EXCERPT = 120


def relevant(message: str, r: dict) -> bool:
    """검색 결과 문단이 질문에 답이 될 만한지(규칙 모드 인용): 질문이 평가 낱말(기말·LAB…)을 말하면 그 낱말이 문단이나 글 제목에
    (번호가 있으면 — 'LAB4' — 같은 번호, 다른 번호만 있는 글은 아님), 질문 주제(범위·형식…)가 있으면 그 주제가 문단이나 글 제목에
    있어야 한다. 둘 다 없는 질문은 남은 낱말·점수 문장·날짜(묻는 것일 때) 중 하나가 문단에 있어야 한다(날짜만 있는 글은 아님)."""
    from .extract import title_tokens
    fam_q = lk.families(message)
    text, title = str(r.get("snippet") or ""), str(r.get("title") or "")
    if fam_q and not fam_q & (lk.families(text) | lk.families(title)):
        return False
    want = {(k, n) for k, n in title_tokens(message) if n}
    if want:
        got = {(k, n) for k, n in title_tokens(text) | title_tokens(title) if n}
        if not want & got and {k for k, _ in want} & {k for k, _ in got}:
            return False   # 'LAB4'를 물었는데 'Lab6' 글
    topics = question_topics(message)
    if topics:
        return any(topic_in(t, text) or topic_in(t, title) for t in topics)
    if fam_q:
        return True
    low = (text + " " + title).lower()
    keys = [k.lower() for k in _keywords(tokens(message)) if not any(q.search(k) for q, _ in TOPICS.values())]
    if any(k in low for k in keys):
        return True
    if SCORE_WORDS.search(message or "") and SCORE_WORDS.search(text):
        return True
    return bool(DATE_WORDS.search(message or "") and r.get("dates"))


def citation(r: dict, quote: str | None = None) -> dict:
    """검색 결과 하나 → 인용 칸(계약 22-2). quote는 그 글의 문장 그대로(QUOTE_MAX자 이내)."""
    q = str(quote if quote is not None else r.get("snippet") or "").strip()
    return {"source_key": r.get("source_key"), "course_id": r.get("course_id"), "course_short": r.get("course_short"),
            "type": r.get("type"), "title": r.get("title"), "posted_at": r.get("posted_at"), "url": r.get("url"),
            "quote": q[:QUOTE_MAX]}


def _excerpt(message: str, quote: str) -> str:
    """답 글에 넣을 짧은 인용(질문 주제가 처음 나오는 곳 근처, EXCERPT자)."""
    q = _flat(quote)
    if len(q) <= EXCERPT:
        return q
    pos = min((m.start() for t in question_topics(message) for m in [TOPICS[t][1].search(q)] if m), default=0)
    start = max(0, min(pos - 40, len(q) - EXCERPT))
    return ("…" if start else "") + q[start:start + EXCERPT].strip() + "…"


def question_rules_answer(message: str, results: list[dict], searched: int, ai_error: str | None = None,
                          ai_enabled: bool = False, named: set | None = None) -> tuple[str, list[dict]]:
    """규칙으로 답하기(AI 꺼짐·실패): 관련 높은 문단 1~3개를 그대로 인용. 못 찾으면 그렇다고 말한다(지어내지 않음).
    named: 질문에 이름이 나온 과목 id들 — 있으면 그 과목 글만 인용. (답, citations)."""
    rel = [r for r in results if relevant(message, r) and (not named or r.get("course_id") in named)]
    picks = []
    if rel:
        best = rel[0].get("score") or 0
        picks = [r for r in rel if (r.get("score") or 0) >= best * 0.5][:MAX_CITATIONS]
    cites = [citation(r) for r in picks]
    labels = [TOPIC_LABELS[t] for t in question_topics(message)]
    if cites:
        lines = [f"[{c['course_short'] or '과목 미상'}] '{_flat(c['title'])[:60]}': “{_excerpt(message, c['quote'])}”"
                 for c in cites]
        parts = [f"이 글들에 이렇게 적혀 있어요(규칙 검색, 이 PC의 글 {searched}개 중): " + " / ".join(lines),
                 "원문에서 한 번 더 확인해 주세요."]
    elif results:
        what = f"'{'·'.join(labels)}'에 대한 내용" if labels else "질문에 맞는 내용"
        parts = [f"{what}이 적힌 글을 이 PC의 글 {searched}개에서 찾지 못했어요.",
                 "비슷한 낱말이 있는 글은 검색 결과에 있어요 — 과목 이름이나 다른 낱말로 다시 물어봐 주세요."]
    else:
        parts = [f"이 질문과 관련된 글을 이 PC의 글 {searched}개에서 찾지 못했어요.",
                 "과목 이름이나 다른 낱말(예: 기말, LAB4, 범위, 제출 형식)로 다시 물어봐 주세요."]
    if ai_error:
        parts.append(f"AI에게 묻지 못해 규칙 검색으로 답했어요: {ai_error}")
    elif not ai_enabled:
        parts.append("설정 → AI에서 AI CLI를 켜면 찾은 글을 AI가 읽고 인용과 함께 답해 드려요.")
    return " ".join(parts), cites


def _ai_citations(cits: list[dict], results: list[dict], names: list | None) -> list[dict]:
    """AI가 가린 글에서 인용한 것 → 이 PC의 원문 인용(자리 표시 없이) + 글 정보."""
    from .agent_cli import original_quote, redact
    out = []
    for c in cits or []:
        q = _flat(c.get("quote"))
        same = [r for r in results if r.get("source_key") == c.get("source_key")]
        home = next((r for r in same if q in _flat(redact(str(r.get("snippet") or ""), names)[0])), None)
        home = home or (same[0] if same else None)
        if home is None:
            continue
        orig = original_quote(q, [{"text": home.get("snippet") or ""}], names)
        cite = citation(home, orig)
        if not any(x["source_key"] == cite["source_key"] and x["quote"] == cite["quote"] for x in out):
            out.append(cite)
    return out


def _ai_inputs(ctx: Ctx, message: str, results: list[dict], cids: list[str], names, message_typed: str | None = None,
               history: list | None = None) -> tuple[dict, list]:
    """assist·answer 작업이 함께 쓰는 글 묶음(계약 20-3·22-2·24-1): 메시지, 과목 목록, 지금 상태 요약, 찾은 문단, 이전 대화.
    message는 갈래·검색에 쓴 말(생략 질문이면 앞 질문과 합친 뜻), message_typed는 사용자가 친 그대로 — 다르면 AI에 둘 다 보낸다.
    (inputs, 원문 조각들)."""
    text, originals = state_text(ctx, cids)
    originals += [r["snippet"] for r in results]
    typed = message_typed if message_typed is not None else message
    inputs = {"message": typed, "followup": message if _flat(message) != _flat(typed) else None, "names": names,
              "state_text": text, "courses": [{"id": cid, "short": ctx.shorts[cid]} for cid in ctx.courses],
              "snippets": [{"source_key": r["source_key"], "course_id": r["course_id"], "type": r["type"],
                            "title": r["title"], "posted_at": r["posted_at"], "text": r["snippet"]} for r in results],
              "history": [{"message": h.get("message"), "answer": h.get("answer"), "kind": h.get("kind")}
                          for h in _history_list(history)]}
    return inputs, originals


def _answer_question(ctx: Ctx, out: dict, message: str, results: list[dict], searched: int, ai_enabled: bool, runner,
                     command, names, session, cids: list[str], message_typed: str | None = None,
                     history: list | None = None) -> dict:
    """question 갈래(22-2): AI가 켜져 있고 찾은 글이 있으면 작업 `answer`, 아니면(또는 실패하면) 규칙으로 문단을 인용.
    질문 낱말과 겹치는 것 없이 날짜만 있는 문단(점수 MIN_AI_SCORE 이하)뿐이면 AI를 부르지 않는다(하루 한도를 아낀다).
    message는 갈래·검색에 쓴 말(생략 질문이면 합친 뜻), message_typed는 친 그대로(AI에 함께 보냄)."""
    if ai_enabled and any((r.get("score") or 0) > MIN_AI_SCORE for r in results):
        from .agent_cli import AiSession
        session = session or AiSession(ctx.store, runner=runner, command=command, enabled=True, max_calls=PER_ASK)
        inputs, _ = _ai_inputs(ctx, message, results, cids, names, message_typed, history)
        res = session.run("answer", inputs)
        out["attempts"] = res.attempts
        if res.ok:
            out.update(by="ai", answer=res.data["answer"], citations=_ai_citations(res.data["citations"], results, names),
                       notes=out["notes"] + list(res.notes), finished_at=iso(now_local()))
            return out
        out["error"] = res.error
    named = set(cids) if mentioned_courses(message, list(ctx.courses.values())) else None   # 과목을 말했으면 그 과목 글만
    out["answer"], out["citations"] = question_rules_answer(message, results, searched, out["error"], ai_enabled, named)
    out["finished_at"] = iso(now_local())
    return out


# ---------------------------------------------------------------- 일반 대화인지(계약 24-2)

# 학업 말(이것이 있으면 일반 대화로 보지 않는다 — 지금까지처럼 놓친 것·질문 갈래). 평가 낱말(links.families)·질문 주제·점수 낱말·
# 과목 이름은 따로 본다. '공부'는 넣지 않는다('공부 팁 알려줘'는 일반 대화, '공부 계획'은 plan 갈래가 먼저 받는다).
ACADEMIC_WORDS = re.compile(
    r"과제|숙제|레포트|리포트|보고서|시험|퀴즈|중간\s*고사|기말\s*고사|강의|수업|교수|조교|학점|성적|점수|출석|결석|공지|계획서|"
    r"실라버스|과목|전공|학기|주차|실습|프로젝트|발표|제출|마감|기한|데드라인|휴강|보강|수강|교재|교과서|챕터|단원|범위|진도|평가|비중|"
    r"배점|확인함|캘린더|일정|스케[줄쥴]|"
    r"(?<![a-z])(?:lms|canvas|syllabus|homeworks?|assignments?|exams?|quiz(?:zes)?|midterms?|finals?|lectures?|classes|"
    r"courses?|professor|prof|labs?|projects?|reports?|deadlines?|due|grades?|scores?|semester|schedule|submit\w*)(?![a-z])",
    re.I)
_DATE_ASK = re.compile(r"마감|기한|데드라인|언제까지|며칠까지|(?<![a-z])(?:due|deadline)(?![a-z])", re.I)
CRISIS_WORDS = re.compile(
    r"죽고\s*싶|자살|자해|극단\s*적\s*(?:인\s*)?선택|살기\s*싫|살고\s*싶지\s*않|사라지고\s*싶|그만\s*살고\s*싶|"
    r"(?<![a-z])(?:suicid\w*|kill\s+myself|self[-\s]?harm|end\s+my\s+life|want\s+to\s+die)(?![a-z])", re.I)
CHEER_WORDS = re.compile(
    r"힘들|힘드|지쳤|지친|지쳐|피곤|졸려|우울|스트레스|불안해|막막|번아웃|하기\s*싫|못\s*하겠|포기하고\s*싶|그만두고\s*싶|멘붕|"
    r"응원|격려|위로|힘\s*(?:내|줘|좀|이\s*안)|화이팅|파이팅|"
    r"(?<![a-z])(?:tired|exhausted|stressed|burn(?:ed|t)?\s*out|anxious|motivat\w*|cheer\s+me)(?![a-z])", re.I)
GREETING_WORDS = re.compile(
    r"^\s*(?:안녕|하이|헬로|반가워|반갑|좋은\s*(?:아침|오후|저녁|밤)|굿\s*모닝|굿\s*나잇|잘\s*자|ㅎㅇ|여보세요|"
    r"hi|hello|hey|good\s+(?:morning|afternoon|evening|night))(?![a-z])", re.I)
THANKS_WORDS = re.compile(r"고마워|고맙|감사(?:해|합|드려|드립)|땡큐|ㄱㅅ|ㄳ|(?<![a-z])(?:thanks|thank\s+you|thx)(?![a-z])", re.I)
TIME_WORDS = re.compile(
    r"(?:오늘|지금|현재)\s*(?:이|은|는)?\s*(?:며칠|몇\s*(?:일|월|시|분)|무슨\s*(?:요일|날)|몇\s*요일|날짜|요일|시간|시각)|"
    r"^\s*몇\s*시(?:야|예요|에요|지|니|냐|인가요?|일까)?\s*[?？]?\s*$|무슨\s*요일|"
    r"(?<![a-z])(?:what\s+(?:time|day|date)\s+is\s+it|what'?s\s+the\s+(?:time|date)|today'?s\s+date)(?![a-z])", re.I)
HELP_WORDS = re.compile(
    r"(?:뭘|뭐|무엇을|무얼|어떤\s*걸)\s*(?:할\s*수|도와|해\s*줄\s*수)|할\s*수\s*있는\s*(?:게|것|일)|도움말|"
    r"(?:넌|너는|너)\s*(?:누구|뭐)|누구(?:야|세요|니)|(?<![a-z])(?:what\s+can\s+you\s+do|who\s+are\s+you)(?![a-z])", re.I)
GENERAL_WORDS = re.compile(
    r"날씨|추천|맛집|메뉴|뭐\s*먹|점심|저녁|영화|드라마|노래|음악|게임|운동|여행|연애|친구|고민|심심|농담|재밌는|"
    r"팁|조언|노하우|요령|하는\s*법|어떻게\s*(?:하면|해야|할까|하지)|"
    r"(?<![a-z])(?:weather|recommend\w*|jokes?|movies?|music|songs?|tips?|advice|how\s+to)(?![a-z])", re.I)
CHAT_INTENTS = ("crisis", "cheer", "time", "greeting", "thanks", "help", "general", "other")


def _visible_courses(store) -> list[dict]:
    hidden = hidden_courses(store.data)
    return [c for c in (store.data.get("cache") or {}).get("courses", []) if str(c["id"]) not in hidden]


def strong_academic(message: str, courses=()) -> bool:
    """학업 말인지(계약 24-2): 평가 낱말(기말·LAB…)·질문 주제(범위·형식…)·점수 낱말·학업 낱말(과제·시험·강의·마감…)·과목 이름.
    '날짜·언제·시간' 같은 말만 있는 것('지금 몇 시야?')과 '안 보여요'만 있는 것('할 게 없어요')은 학업 말이 아니다."""
    msg = message or ""
    return bool(lk.families(msg) or question_topics(msg) or SCORE_WORDS.search(msg) or ACADEMIC_WORDS.search(msg)
                or _DATE_ASK.search(msg) or (courses and mentioned_courses(msg, list(courses))))


def chat_intent(message: str, courses=()) -> str | None:
    """처음부터 일반 대화인 말의 갈래(계약 24-2) — crisis(위기)·cheer(힘듦·응원)·time(오늘 날짜·시각)·greeting·thanks·help(뭘 할 수
    있어?)·general(날씨·추천·고민·팁 …). 아니면 None(놓친 것·질문 갈래로; 관련 글이 없으면 그때 'other'로 일반 대화).
    위기 말은 언제나 먼저. 힘듦은 '언제·마감·안 보여요·범위' 같은 물음이 없을 때만('과제 너무 힘들어'는 응원, '힘든데 LAB4 마감
    언제야?'는 마감 찾기). 나머지는 학업 말(strong_academic)이 없을 때만."""
    msg = message or ""
    if CRISIS_WORDS.search(msg):
        return "crisis"
    asks = bool(MISSING_WORDS.search(msg) or question_topics(msg) or _DATE_ASK.search(msg) or re.search(r"언제|몇\s*시", msg))
    if CHEER_WORDS.search(msg) and not asks:
        return "cheer"
    if strong_academic(msg, courses):
        return None
    for kind, rx in (("time", TIME_WORDS), ("greeting", GREETING_WORDS), ("thanks", THANKS_WORDS), ("help", HELP_WORDS),
                     ("general", GENERAL_WORDS)):
        if rx.search(msg):
            return kind
    return None


def _grounded(message: str, r: dict) -> bool:
    """학업 말이 없는 메시지에 그 문단이 정말 관련 있는지: relevant()이면서 날짜만 맞은 것이 아님(평가 낱말·주제·남은 낱말·점수 문장)."""
    if not relevant(message, r):
        return False
    if lk.families(message) or question_topics(message):
        return True
    low = (str(r.get("snippet") or "") + " " + str(r.get("title") or "")).lower()
    keys = [k.lower() for k in _keywords(tokens(message)) if not any(q.search(k) for q, _ in TOPICS.values())]
    return any(k in low for k in keys) or bool(SCORE_WORDS.search(message or "") and SCORE_WORDS.search(low))


# ---------------------------------------------------------------- 이어지는 대화(계약 24-1): 앞 차례를 이어받기

FOLLOW_MAX = 25           # 생략 질문으로 볼 최대 길이
FOLLOW_LEAD = re.compile(
    r"\s*(?:그럼|그러면|그렇다면|그건|그거|그게|그것은|그것|거기(?:는|서|선)?|그\s*과목(?:은|는)?|그\s*수업(?:은|는)?|"
    r"그\s*과제(?:는|도)?|그\s*시험(?:은|도)?|또|그리고|그\s*다음(?:은)?|그다음(?:은)?|다른\s*(?:건|거|것)(?:은|는|도)?|"
    r"나머지(?:는|도)?|그\s*밖에(?:는)?|"
    r"(?:and|also|then|what\s+about|how\s+about)(?![a-z]))[\s,.]*", re.I)
FOLLOW_TAIL = re.compile(r"(?:은|는|도|건|이랑|랑|하고)\s*[?？]*\s*$")
MORE_WORDS = re.compile(r"^\s*(?:또|다른\s*(?:건|거|것)|나머지|그\s*밖)|(?<![a-z])(?:else|other|more)(?![a-z])", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#\-]*|[가-힣]+|\d+[A-Za-z가-힣]*")
_GRADE_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-DF](?:\+|0)?(?![A-Za-z0-9])")
# 물음 낱말(무엇을 하라는지·묻는지) — 생략 질문에서 '주제'가 아니라 '물음'으로 남긴다(메일·계획·만약에·명령 낱말 포함)
_ASK_WORDS = re.compile(
    r"메일|이메일|써|쓰|초안|작성|문의|연장|결석|공결|공부|계획|일정|스케[줄쥴]|세워|짜|받아야|받으려|받으면|받을|몇|만약|이면|라면|"
    r"어디|언제|뭐|무엇|어떻게|왜|알려|해줘|해\s*줘|줘|주세|보여|찾아|켜|꺼|바꿔|열어|동기화|반영|날짜|시간|"
    r"(?<![a-z])(?:when|what|where|how|why|which|email|mail|draft|plan|tell|show|find)(?![a-z])", re.I)


def is_followup(message: str) -> bool:
    """앞 차례에 기대는 짧은 말인지: 25자 이하이고 '그럼/그러면/그건/거기/그 과목/또/그리고/다른 건…'으로 시작하거나,
    낱말 두 개 이하에 '…은?/…는?/…도?'로 끝남('중간은?', 'LAB3도?')."""
    msg = _flat(message)
    if not msg or len(msg) > FOLLOW_MAX:
        return False
    if FOLLOW_LEAD.match(msg) and FOLLOW_LEAD.match(msg).end() > 0:
        return True
    return bool(FOLLOW_TAIL.search(msg)) and len(tokens(msg)) <= 2


def _strip_leads(message: str) -> str:
    msg = _flat(message)
    for _ in range(4):
        m = FOLLOW_LEAD.match(msg)
        if not m or m.end() == 0:
            break
        msg = msg[m.end():]
    return msg.strip()


def _parts(text: str, courses) -> tuple[list[str], str]:
    """메시지 → (주제 낱말, 물음 글). 주제 = 평가 낱말(LAB4·기말…)과 물음 낱말이 아닌 이름 같은 낱말(감상문 …),
    물음 글 = 주제·과목 이름 조각·이어 말하는 낱말(그럼…)을 뺀 나머지 글 그대로('범위 어디까지야?', '연장 문의 메일 써줘')."""
    variants = [v.lower() for c in courses for v in course_variants(c)]
    subject, spans = [], []
    for m in _TOKEN.finditer(text or ""):
        raw = m.group(0)
        t = _strip(raw)
        low = t.lower()
        if FOLLOW_LEAD.fullmatch(raw) or FOLLOW_LEAD.fullmatch(t):
            spans.append(m.span())
            continue
        if lk.families(t):
            subject.append(t)
            spans.append(m.span())
            continue
        if len(t) < 2 or t[0].isdigit() or _GRADE_TOKEN.fullmatch(t):
            continue
        if any(low == v or low in v for v in variants):
            spans.append(m.span())   # 과목 이름은 따로 이어받는다(course_id)
            continue
        if (low in _STOP or DATE_WORDS.search(t) or SCORE_WORDS.search(t) or MISSING_WORDS.search(t)
                or any(q.search(t) for q, _ in TOPICS.values()) or _ASK_WORDS.search(t)):
            continue
        subject.append(t)
        spans.append(m.span())
    ask, last = [], 0
    for a, b in spans:
        ask.append(text[last:a])
        last = b
    ask.append(text[last:])
    return subject, _flat(" ".join(ask))


def _has_ask(ask: str) -> bool:
    return bool(re.search(r"[가-힣A-Za-z0-9]{2,}", ask or ""))


def merge_followup(message: str, previous: str, courses=()) -> str:
    """생략 질문 + 앞 질문 → 갈래·검색에 쓸 말(계약 24-1). 이번 말에 주제가 없으면 앞 질문의 주제를, 물음이 없으면 앞 질문의 물음을
    더한다('그럼 중간은?' + 'ASIC 기말 범위 어디까지야?' → '중간은? 범위 어디까지야?', '그건 언제야?' + 'LAB4 제출 형식이 뭐야?' →
    '언제야? LAB4'). 이번 말에 학점 글자('그럼 B+는?')가 있으면 앞 물음의 학점 글자는 뺀다."""
    cur = _strip_leads(message)
    cur_subject, cur_ask = _parts(cur, courses)
    prev_subject, prev_ask = _parts(previous or "", courses)
    if _GRADE_TOKEN.search(cur):
        prev_ask = _flat(_GRADE_TOKEN.sub(" ", prev_ask))
    out = [] if not re.search(r"[가-힣A-Za-z0-9]", cur) else [cur]
    if not cur_subject and prev_subject:
        out.append(" ".join(dict.fromkeys(prev_subject)))
    if not _has_ask(cur_ask) and _has_ask(prev_ask):
        out.append(prev_ask)
    return _flat(" ".join(out)) or _flat(message)


def _history_list(history) -> list[dict]:
    """answer()가 받는 앞 차례들(오래된 것부터, 최대 HISTORY_TURNS) — 모양이 다른 것은 버린다."""
    out = []
    for h in history or []:
        if isinstance(h, dict) and isinstance(h.get("message"), str):
            out.append(h)
    return out[-HISTORY_TURNS:]


def followup(message: str, course_id, history: list, courses: list, intent: str | None = None) -> dict:
    """앞 차례에서 이어받을 것(계약 24-1) → {"message": 갈래·검색에 쓸 말, "course_id": 쓸 과목, "followup": 생략 질문인지,
    "more": '또 다른 건?'인지, "prev": 앞 차례|None, "carried": {"from", "course_id", "course_short", "message"}|None}.
    - 앞 차례 = 같은 대화에서 가장 최근의 일반 대화(chat)가 아닌 차례. 일반 대화(intent)면 아무것도 이어받지 않는다.
    - 과목: 이번 메시지에 과목 이름이 없고 course_id도 없으면 앞 차례의 과목(course_context)을 쓴다.
    - 주제: 생략 질문(is_followup)이면 앞 차례의 '읽은 뜻'과 합친다(merge_followup)."""
    out = {"message": message, "course_id": course_id, "followup": False, "more": False, "prev": None, "carried": None}
    if intent:
        return out
    prev = next((h for h in reversed(_history_list(history)) if h.get("kind") != "chat"), None)
    if prev is None:
        return out
    out["prev"] = prev
    carried = {}
    known = {str(c["id"]): c for c in courses}
    pc = prev.get("course_id")
    if course_id is None and pc is not None and str(pc) in known and not mentioned_courses(message, courses):
        out["course_id"] = carried["course_id"] = str(pc)
    if is_followup(message):
        out["followup"] = True
        out["more"] = bool(MORE_WORDS.search(message))
        eff = merge_followup(message, str(prev.get("effective") or prev.get("message") or ""), courses)
        if eff != _flat(message):
            out["message"] = carried["message"] = eff
    if carried:
        cid = carried.get("course_id")
        out["carried"] = {"from": prev.get("id"), "course_id": cid,
                          "course_short": core.short_course_name(known[cid].get("name")) if cid else None,
                          "message": carried.get("message")}
    return out


def _drop_carried_course(fu: dict) -> None:
    fu["course_id"] = None
    if fu.get("carried"):
        fu["carried"].update(course_id=None, course_short=None)
        if not fu["carried"].get("message"):
            fu["carried"] = None


def course_context(out: dict, course_id, message: str, courses: list) -> str | None:
    """이 차례가 다룬 과목(다음 차례가 이어받는다): 정한·이어받은 과목 → 메시지에 이름이 하나만 나온 과목 → 답(인용·제안·만약에·
    메일·계획)이 모두 한 과목이면 그 과목. 아니면 None."""
    if course_id is not None:
        return str(course_id)
    named = mentioned_courses(message, courses)
    if len(named) == 1:
        return named[0]
    seen = []
    for c in out.get("citations") or []:
        seen.append(c.get("course_id"))
    for p in out.get("proposals") or []:
        if isinstance(p, dict) and not p.get("rejected_reason"):
            seen.append(p.get("course_id"))
    for k in ("whatif", "draft", "plan"):
        if isinstance(out.get(k), dict):
            seen.append(out[k].get("course_id"))
    ids = {str(x) for x in seen if x not in (None, "")}
    return ids.pop() if len(ids) == 1 else None


def _prev_cited(prev: dict | None) -> list[tuple[str, str]]:
    return [(str(k), _flat(q)) for k, q in (prev or {}).get("cited") or [] if q]


# ---------------------------------------------------------------- 일반 대화 답(계약 24-2)

_WD_FULL = "월화수목금토일"
CHAT_EXAMPLES = ("'기말 범위 어디까지야?'", "'과제 연장 문의 메일 써줘'")
HELP_LINES = ("과목 질문에 답하기('기말 범위 어디까지야?')", "놓친 마감 찾기('기말고사 날짜가 안 보여요')",
              "성적 '만약에'('기말에서 몇 점 받아야 A?')", "문의 메일 초안('LAB4 연장 문의 메일 써줘')",
              "공부 계획('기말 전 2주 공부 계획 세워줘')", "앱 설정('컴퓨터 켤 때 자동 실행해줘')")
AI_OFF_CHAT = "학업 밖 이야기는 설정 → AI에서 AI 도움을 켜면 같이 나눌 수 있어요."
CRISIS_HELP = ("지금 위험하다고 느끼면 바로 112나 119에 연락해 주세요. 24시간 자살예방상담전화 109, 정신건강위기상담 "
               "1577-0199, 학교 학생상담센터에서 이야기를 들어 줄 거예요.")
GENERAL_HEAD = "강의 글에는 없어서 일반적인 설명이에요"


def pace_summary(store, now: datetime | None = None) -> dict:
    """일반 대화에 쓰는 짧은 지금 상태(읽기만, 계약 24-2): 이번 주(월~일) 마감 수·남은 수·확인 전 수, 가장 가까운 남은 마감,
    학기 주차, km·다음 이정표·연속 완주(celebrate — 성적 숫자 없음). 계산이 안 되면 빈 칸."""
    now = now or now_local()
    out = {"week_now": None, "weeks_total": None, "week_total": 0, "week_left": 0, "week_review": 0, "next": None,
           "km": None, "pace": None, "synced": store.data.get("cache") is not None}
    if not out["synced"]:
        return out
    cache = store.data.get("cache") or {}
    try:
        from . import coach
        view = store.view()
        sem = core.semester_info(cache, view, today=now.date())
        items = core.annotate(view, sem, cache)
        out["week_now"], out["weeks_total"] = sem.get("week_now"), sem.get("weeks_total")
        lv = coach.live(items)
        monday = now.date() - timedelta(days=now.weekday())
        start = datetime.combine(monday, datetime.min.time(), tzinfo=now.tzinfo)
        end = start + timedelta(days=7)
        week = [v for v in lv if start <= parse_iso(v["due"]) < end]
        out["week_total"] = len(week)
        out["week_left"] = sum(1 for v in week if not v.get("submitted") and parse_iso(v["due"]) >= now)
        out["week_review"] = sum(1 for v in week if v.get("status") == "review")
        nxt = next((v for v in lv if parse_iso(v["due"]) >= now and not v.get("submitted")), None)
        if nxt:
            out["next"] = {"title": nxt.get("title"), "course_short": core.short_course_name(nxt.get("course_name")),
                           "due": nxt.get("due")}
        try:
            from . import celebrate
            cel = celebrate.state(store, sem, items, now)[0]   # 읽기만(저장할 것은 버린다)
            out["km"] = cel.get("km")
            out["pace"] = coach._pace_text(cel)
        except Exception:  # noqa: BLE001 — 페이스는 덤이다
            pass
    except Exception:  # noqa: BLE001 — 상태 요약이 없어도 대화는 한다
        return out
    return out


def chat_state_text(pace: dict, now: datetime) -> str:
    """AI 작업 chat에 보내는 짧은 상태(계약 24-2): 지금 시각·주차·이번 주 마감 수·페이스. 제목·성적·개인 정보 없음."""
    lines = [f"오늘 / now: {now:%Y-%m-%d}({_WD_FULL[now.weekday()]}) {now:%H:%M}"]
    if pace.get("week_now") and pace.get("weeks_total"):
        lines.append(f"학기 / term: {pace['week_now']}주차 / {pace['weeks_total']}주")
    if pace.get("synced"):
        lines.append(f"이번 주 마감 / this week: {pace['week_total']}개 · 남은 것 {pace['week_left']}개"
                     + (f" · 확인 전 {pace['week_review']}개" if pace.get("week_review") else ""))
    else:
        lines.append("이번 주 마감 / this week: 아직 동기화 전이라 모름 / not synced yet")
    if pace.get("pace"):
        lines.append(f"페이스 / pace: {pace['pace']}")
    return "\n".join(lines)


def _clock(now: datetime) -> str:
    h = now.hour
    half = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    return f"{now.year}년 {now.month}월 {now.day}일 {_WD_FULL[now.weekday()]}요일 {half} {h12}시 {now.minute}분"


def _next_text(pace: dict) -> str | None:
    nxt = pace.get("next")
    due = parse_iso((nxt or {}).get("due"))
    if not nxt or due is None:
        return None
    where = f"[{nxt['course_short']}] " if nxt.get("course_short") else ""
    return f"{where}{nxt.get('title')}({_md(due)})"


def rules_chat(intent: str, pace: dict, now: datetime, ai_enabled: bool = False, ai_error: str | None = None,
               found_nothing: bool = False) -> str:
    """AI 없이 하는 일반 대화 답(계약 24-2): 인사 → 인사 + 이번 주 할 일·km, 감사 → 짧게, 힘듦 → 응원 + 다음 이정표,
    시각 → 지금 시각, 도움말 → 할 수 있는 것, 위기 → 도움 받을 곳, 그 밖 → AI를 켜면 같이 나눌 수 있다는 안내 + 예시 2개."""
    nxt = _next_text(pace)
    pace_line = f"{pace['pace']}." if pace.get("pace") else ""
    if intent == "crisis":
        return "많이 힘든 마음이 느껴져요. 혼자 견디지 않아도 돼요. " + CRISIS_HELP + " 마감은 잠시 미뤄도 괜찮아요 — 당신이 먼저예요."
    if intent == "greeting":
        if not pace.get("synced"):
            todo = "아직 동기화 전이라 마감을 몰라요 — 동기화하면 같이 챙길게요."
        elif pace.get("week_left"):
            todo = f"이번 주 마감 {pace['week_total']}개 중 {pace['week_left']}개 남았어요" + (
                f" — 가장 가까운 건 {nxt}예요." if nxt else ".")
        elif pace.get("week_total"):
            todo = f"이번 주 마감 {pace['week_total']}개는 모두 끝냈어요. 페이스 좋아요!"
        else:
            todo = "이번 주는 남은 마감이 없어요." + (f" 다음은 {nxt}예요." if nxt else "")
        return " ".join(x for x in ("안녕하세요! 오늘도 같이 달려 봐요.", todo, pace_line) if x)
    if intent == "thanks":
        return "천만에요! 필요하면 언제든 불러 주세요." + (f" {nxt}까지 같이 페이스 맞춰 갈게요." if nxt else "")
    if intent == "cheer":
        parts = ["많이 지쳤군요. 여기까지 달려온 것만으로도 충분히 잘하고 있어요.", pace_line,
                 f"오늘은 가장 가까운 {nxt} 하나만 붙잡아 봐요." if nxt else "오늘은 잠깐 쉬어 가도 괜찮아요.",
                 "물 한 잔 마시고 5분만 쉬었다가 다시 같이 가요."]
        return " ".join(x for x in parts if x)
    if intent == "time":
        week = f" 학기 {pace['week_now']}주차예요." if pace.get("week_now") else ""
        return f"지금은 {_clock(now)}이에요.{week}"
    if intent == "help":
        out = "저는 이 PC에 모은 강의 글로 이런 걸 도와드려요: " + ", ".join(HELP_LINES) + "."
        return out + ("" if ai_enabled else " " + AI_OFF_CHAT)
    head = "이 말에 맞는 강의 글은 찾지 못했어요. " if found_nothing else ""
    if ai_enabled and ai_error:
        head += f"지금은 AI에게 묻지 못했어요({ai_error}). "
    elif not ai_enabled:
        head += AI_OFF_CHAT + " "
    return head + "이런 건 바로 도와드릴 수 있어요: " + ", ".join(CHAT_EXAMPLES) + "."


def _ensure_help(answer: str) -> str:
    """위기 말에 대한 AI 답에 도움 받을 곳이 없으면 덧붙인다(하네스가 보장)."""
    if "109" in answer or "1577-0199" in answer:
        return answer
    return (answer.rstrip() + " " + CRISIS_HELP).strip()


def _session_left(session) -> str | None:
    """이 묻기에서 AI를 더 부를 수 없으면 그 이유(한국어)."""
    if session is None:
        return "AI CLI 사용이 꺼져 있습니다(설정에서 켤 수 있음)"
    if session.calls >= session.max_calls:
        return f"이번 질문에 쓸 AI 호출({session.max_calls}회)을 다 썼어요"
    return session.blocked()


def _chat_ai(store, message: str, mode: str, intent: str, session, names, history, now, pace) -> tuple:
    """AI 작업 chat 한 번 → (답 | None, 오류 | None, 시도 수, 알림)."""
    why = _session_left(session)
    if why:
        return None, why, 0, []
    inputs = {"message": message, "names": names, "mode": mode, "intent": intent,
              "state_text": chat_state_text(pace, now),
              "history": [{"message": h.get("message"), "answer": h.get("answer"), "kind": h.get("kind")}
                          for h in _history_list(history)]}
    res = session.run("chat", inputs)
    if not res.ok:
        return None, res.error, res.attempts, list(res.notes)
    ans = res.data["answer"]
    if intent == "crisis" or CRISIS_WORDS.search(message or ""):
        ans = _ensure_help(ans)
    return ans, None, res.attempts, list(res.notes)


def _chat(store, out: dict, message: str, intent: str, ai_enabled: bool, session, names, history, now,
          found_nothing: bool = False) -> dict:
    """일반 대화 갈래(kind "chat"): AI가 켜져 있으면 작업 chat, 아니면(또는 실패하면) 규칙 답. 인용·제안·검색 결과 없음."""
    pace = pace_summary(store, now)
    out.update(kind="chat", intent=intent, proposals=[], citations=[], results=[], by="rules")
    if ai_enabled:
        ans, err, n, notes = _chat_ai(store, message, "chat", intent, session, names, history, now, pace)
        out["attempts"] = int(out.get("attempts") or 0) + n
        out["notes"] = list(out.get("notes") or []) + notes
        if ans is not None:
            out.update(by="ai", answer=ans, error=None)
            return out
        out["error"] = err
    out["answer"] = rules_chat(intent, pace, now, ai_enabled, out.get("error"), found_nothing)
    return out


def _general_addendum(store, out: dict, message: str, session, names, history, now) -> None:
    """학업 질문인데 강의 글에 답이 없을 때(인용 0, AI 켜짐): 작업 chat(mode general)으로 일반 지식 설명을 끝에 덧붙인다.
    out["general"] = {"answer", "by": "ai"}, out["answer"] 끝에 '강의 글에는 없어서 일반적인 설명이에요 — …'. 실패하면 그대로."""
    pace = pace_summary(store, now)
    ans, err, n, notes = _chat_ai(store, message, "general", "general", session, names, history, now, pace)
    out["attempts"] = int(out.get("attempts") or 0) + n
    out["notes"] = list(out.get("notes") or []) + notes
    if ans is None:
        if n:
            out["notes"].append(f"일반 지식 설명은 덧붙이지 못했어요({_flat(err)[:120]})")
        return
    out["general"] = {"answer": ans, "by": "ai"}
    out["answer"] = f"{out.get('answer') or ''}\n\n{GENERAL_HEAD} — {ans}".strip()


# ---------------------------------------------------------------- 묻기 갈래(22-1): 다른 모듈의 handle

_KEEP = ("id", "at", "message", "course_id", "status", "kind", "finished_at", "searched", "results", "attempts",
         "conversation", "carried", "course_context")


def _handles(store, message: str, course_id, names, now, action_course=None):
    """갈래 handle들(모양 고정: handle(store, message, course_id, …)). 이어받은 과목은 메일·계획·만약에에 넘기고, 명령(actions)에는
    직접 고른 과목이나 생략 질문('그 과목 P/NP로 바꿔줘')일 때만 넘긴다(action_course)."""
    from . import actions, maildraft, plan, whatif
    return (("action", lambda: actions.handle(store, message, action_course)),
            ("mail", lambda: maildraft.handle(store, message, course_id, names)),
            ("plan", lambda: plan.handle(store, message, course_id, now)),
            ("whatif", lambda: whatif.handle(store, message, course_id, now)))


def _merge_handled(store, kind: str, res: dict, notes: list) -> dict:
    """handle 결과 {"answer", ...추가 칸}을 대화 칸으로. 20장 칸(answer·by·proposals …)은 유지하고, 대화의 틀(id·kind·status …)은
    handle이 바꾸지 못한다. 액션 제안은 check_action으로 같은 모양·검사를 거친다."""
    shorts = {str(c["id"]): core.short_course_name(c.get("name"))
              for c in (store.data.get("cache") or {}).get("courses", [])}
    out = {"status": "done", "kind": kind, "by": "rules", "answer": "", "proposals": [], "citations": [], "searched": 0,
           "results": [], "error": None, "attempts": 0, "notes": list(notes)}
    for k, v in res.items():
        if k in _KEEP or str(k).startswith("_"):
            continue
        out[k] = v
    out["answer"] = str(res.get("answer") or "")
    out["by"] = res.get("by") if res.get("by") in ("ai", "rules") else "rules"
    extra = [str(n) for n in res.get("notes") or [] if n] if isinstance(res.get("notes"), list) else []
    out["notes"] = list(notes) + extra
    props = []
    raw = res.get("proposals") if isinstance(res.get("proposals"), list) else []
    for p in raw:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "action":
            p = check_action(store, p, by=p["by"] if p.get("by") in ("ai", "rules") else out["by"], shorts=shorts)
        else:
            p = {"rejected_reason": None, "applied": False, "applied_at": None, "undone_at": None, "result": None,
                 "notes": [], **p}
        props.append(p)
    out["proposals"] = props
    out["citations"] = res.get("citations") if isinstance(res.get("citations"), list) else []
    return out


# ---------------------------------------------------------------- 한 번 묻기

def _finish(out: dict, fu: dict, message: str, courses: list) -> dict:
    """차례 칸 마무리: 이어받은 것(carried)·이 차례의 과목(course_context)·끝난 시각."""
    out["carried"] = fu.get("carried")
    out["course_context"] = None if out.get("kind") == "chat" else course_context(out, fu.get("course_id"),
                                                                                  fu.get("message") or message, courses)
    if fu.get("carried") and fu["carried"].get("course_short"):
        out["notes"] = list(out.get("notes") or []) + [f"앞 대화의 과목 '{fu['carried']['course_short']}'에서 찾았어요"]
    out["finished_at"] = iso(now_local())
    return out


def answer(store, message: str, course_id=None, ai_enabled: bool = False, runner=None, command: str | None = None,
           names: list | None = None, session=None, history: list | None = None) -> dict:
    """질문 하나에 답한다(잠금 밖에서, 몇십 초 걸릴 수 있음). store는 이 작업만 쓰는 사본(Store(path))이 좋다 —
    AI 실행 기록(ai_runs)만 이 store에 더해진다. 결과: 차례 칸(status, kind, answer, proposals, citations, searched, results,
    error, by, carried, course_context …).
    history: 같은 대화의 앞 차례들(오래된 것부터, 최대 6) [{"message", "answer", "kind", "course_id", "effective"?, "cited"?, "id"?}]
    — 과목·주제를 이어받는다(계약 24-1, followup).
    갈래(계약 22-1·24-2): actions → maildraft → plan → whatif의 handle 중 첫 번째로 None이 아닌 것 → 처음부터 일반 대화인 말
    (chat_intent) → missing(놓친 것 — 20장) / question(22-2) → 학업 말이 없고 관련 글도 없으면 chat. AI 호출은 한 번 묻기에
    PER_ASK(2)회까지(질문 답과 일반 지식 덧붙이기가 함께 쓴다)."""
    notes: list[str] = []
    now = now_local()
    hist = _history_list(history)
    courses = _visible_courses(store)
    intent = chat_intent(message, courses)
    fu = followup(message, course_id, hist, courses, intent)
    eff = fu["message"]
    if ai_enabled and session is None:
        from .agent_cli import AiSession
        session = AiSession(store, runner=runner, command=command, enabled=True, max_calls=PER_ASK)
    act_course = fu["course_id"] if (course_id is not None or fu["followup"]) else None
    for kind, fn in _handles(store, eff, fu["course_id"], names, now, act_course):
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001 — 한 갈래가 고장 나도 다음 갈래로(계약 22-1)
            notes.append(f"'{kind}' 갈래에서 오류가 나 다음 갈래로 넘어갔어요({type(e).__name__})")
            continue
        if res is None:
            continue
        if not isinstance(res, dict):
            notes.append(f"'{kind}' 갈래의 답 모양이 달라 다음 갈래로 넘어갔어요")
            continue
        return _finish(_merge_handled(store, kind, res, notes), fu, message, courses)
    base = {"status": "done", "searched": 0, "results": [], "error": None, "by": "rules", "answer": "", "proposals": [],
            "citations": [], "attempts": 0, "notes": notes}
    if intent:
        return _finish(_chat(store, {**base, "kind": "chat"}, message, intent, ai_enabled, session, names, hist, now),
                       fu, message, courses)
    kind = classify(eff)
    ctx = Ctx(store)
    results, searched = _search(store, eff, fu["course_id"], items=ctx.items)
    if fu["course_id"] is not None and course_id is None and not any(relevant(eff, r) for r in results):
        # 이어받은 과목에 관련 글이 없으면 모든 과목에서 다시 찾는다(다른 과목 이야기로 넘어간 경우)
        again, n_again = _search(store, eff, None, items=ctx.items)
        if any(relevant(eff, r) for r in again):
            results, searched = again, n_again
            _drop_carried_course(fu)
    if fu["more"]:   # '또 다른 건?' — 앞 차례에서 인용한 문단은 빼고
        seen = _prev_cited(fu["prev"])
        results = [r for r in results if not any(k == r["source_key"] and q and q in _flat(r["snippet"]) for k, q in seen)]
    academic = strong_academic(eff, courses) or course_id is not None
    out = {**base, "kind": kind, "searched": searched, "results": results}
    if not academic and not any(_grounded(eff, r) for r in results):
        out["searched"] = searched
        return _finish(_chat(store, out, message, "other", ai_enabled, session, names, hist, now, found_nothing=True),
                       fu, message, courses)
    cids = scope_of(ctx, eff, fu["course_id"], results)
    if kind == "question":
        out = _answer_question(ctx, out, eff, results, searched, ai_enabled, runner, command, names, session, cids,
                               message_typed=message, history=hist)
        if not out["citations"]:
            if not academic:   # 학업 말이 없고 인용할 글도 없음 → 일반 대화
                out.update(answer="", error=None)
                return _finish(_chat(store, out, message, "other", ai_enabled, session, names, hist, now,
                                     found_nothing=True), fu, message, courses)
            if ai_enabled:     # 학업 질문인데 글에 없음 → '찾지 못했어요' 끝에 일반 지식 설명(계약 24-2)
                _general_addendum(store, out, message, session, names, hist, now)
        return _finish(out, fu, message, courses)
    if ai_enabled:
        inputs, originals = _ai_inputs(ctx, eff, results, cids, names, message_typed=message, history=hist)
        inputs["check"] = lambda p, why: check_proposal(ctx, p, "ai", why, originals, names)
        res = session.run("assist", inputs)
        out["attempts"] = res.attempts
        if res.ok:
            out.update(by="ai", answer=res.data["answer"], proposals=res.data["proposals"], notes=notes + list(res.notes))
            return _finish(out, fu, message, courses)
        out["error"] = res.error
    out["answer"], out["proposals"] = rules_answer(ctx, eff, results, searched, cids, out["error"], ai_enabled)
    return _finish(out, fu, message, courses)


# ---------------------------------------------------------------- 대화 기록·작업

def _box(store) -> dict:
    box = store.data.get("agent")
    if not isinstance(box, dict):
        box = store.data["agent"] = {}
    if not isinstance(box.get("threads"), list):
        box["threads"] = []
    return box


def threads(store) -> list[dict]:
    return _box(store)["threads"]


def find_thread(store, thread_id) -> dict | None:
    return next((t for t in threads(store) if isinstance(t, dict) and t.get("id") == thread_id), None)


def check_message(message, course_id, store) -> tuple[str, str | None]:
    """요청 검사 → (메시지, 과목 id). 잘못되면 ValueError(한국어)."""
    msg = _flat(message if isinstance(message, str) else "")
    if not msg:
        raise ValueError("물어볼 내용을 적어 주세요")
    if len(msg) > MAX_MESSAGE:
        raise ValueError(f"질문은 {MAX_MESSAGE}자 이하로 적어 주세요")
    if course_id in (None, ""):
        return msg, None
    cid = str(course_id)
    known = {str(c["id"]) for c in (store.data.get("cache") or {}).get("courses", [])}
    if cid not in known:
        raise ValueError("그런 과목이 없어요")
    if cid in hidden_courses(store.data):
        raise ValueError("제외한 과목이에요 — 과목 화면에서 다시 보이게 한 뒤 물어봐 주세요")
    return msg, cid


def conversation_of(t: dict) -> str | None:
    """차례가 속한 대화 id. conversation 칸이 없는 옛 차례(v0.7.1까지)는 그 차례 id가 곧 대화 id(혼자인 대화)."""
    if not isinstance(t, dict):
        return None
    conv = t.get("conversation")
    return conv if isinstance(conv, str) and conv else t.get("id")


def conversation_ids(store) -> set:
    return {c for c in (conversation_of(t) for t in threads(store)) if c}


def new_thread(store, message: str, course_id: str | None, conversation: str | None = None) -> dict:
    """차례 하나를 'running'으로 만든다(부르는 쪽이 잠금·저장). conversation이 이미 있는 대화 id면 그 대화에 잇고, 없거나 모르는
    id면 신규 대화(c-<hex>)를 연다(계약 24-1). 최근 MAX_THREADS개만 남긴다(오래된 것부터 지움)."""
    import uuid
    conv = conversation if isinstance(conversation, str) and conversation in conversation_ids(store) else None
    th = {"id": "t-" + uuid.uuid4().hex[:10], "at": iso(now_local()), "message": message, "course_id": course_id,
          "conversation": conv or "c-" + uuid.uuid4().hex[:10],
          "status": "running", "kind": None, "answer": None, "proposals": [], "citations": [], "searched": 0,
          "results": [], "error": None, "by": None, "finished_at": None, "carried": None, "course_context": None}
    box = _box(store)
    box["threads"].append(th)
    del box["threads"][:-MAX_THREADS]
    return th


def _turn(t: dict) -> dict:
    """앞 차례 하나 → answer(history=…)가 받는 모양."""
    carried = t.get("carried") if isinstance(t.get("carried"), dict) else {}
    cited = [(c.get("source_key"), c.get("quote")) for c in t.get("citations") or [] if isinstance(c, dict)]
    return {"id": t.get("id"), "message": str(t.get("message") or ""), "answer": str(t.get("answer") or ""),
            "kind": t.get("kind") or "missing", "course_id": t.get("course_context") or t.get("course_id"),
            "effective": carried.get("message") or str(t.get("message") or ""), "cited": cited}


def history_for(store, thread_id: str, limit: int = HISTORY_TURNS) -> list[dict]:
    """그 차례보다 앞선 같은 대화의 끝난 차례들(오래된 것부터, 최대 limit) — 오류·답하는 중인 차례는 뺀다."""
    me = find_thread(store, thread_id)
    if me is None:
        return []
    conv = conversation_of(me)
    out = []
    for t in threads(store):
        if t is me:
            break
        if isinstance(t, dict) and conversation_of(t) == conv and t.get("status") == "done":
            out.append(_turn(t))
    return out[-limit:]


def conversations(store, active: set | None = None) -> list[dict]:
    """state.agent.conversations(계약 24-1): [{"id", "title"(첫 질문 40자), "started_at", "last_at", "turns", "legacy",
    "running"}] — 최근 먼저, 최대 MAX_CONVERSATIONS. legacy = conversation 칸이 없는 옛 차례 하나(id = 그 차례 id)."""
    legacy = {t.get("id") for t in threads(store) if isinstance(t, dict) and not t.get("conversation")}
    groups: dict = {}
    order: dict = {}
    for i, t in enumerate(public_threads(store, active)):
        conv = t.get("conversation")
        g = groups.setdefault(conv, {"id": conv, "title": None, "started_at": t.get("at"), "last_at": t.get("at"),
                                     "turns": 0, "legacy": conv in legacy, "running": False})
        if g["title"] is None:
            msg = _flat(t.get("message"))
            g["title"] = msg if len(msg) <= 40 else msg[:39] + "…"
        g["turns"] += 1
        g["running"] = g["running"] or t.get("status") == "running"
        order[conv] = i   # 차례는 오래된 것부터 쌓이므로, 같은 시각이면 마지막 차례가 뒤에 있는 대화가 최근
        last = max((x for x in (t.get("finished_at"), t.get("at")) if parse_iso(x)), key=lambda x: parse_iso(x),
                   default=None)
        if last and (parse_iso(g["last_at"]) is None or parse_iso(last) > parse_iso(g["last_at"])):
            g["last_at"] = last
    floor = datetime(1970, 1, 1, tzinfo=local_tz())   # 시각을 모르는 차례(datetime.min은 시간대 계산에서 넘칠 수 있음)
    out = sorted(groups.values(), key=lambda g: (parse_iso(g["last_at"]) or floor, order[g["id"]]), reverse=True)
    return out[:MAX_CONVERSATIONS]


def clear_conversation(store, conversation: str) -> bool:
    """그 대화의 차례만 지운다(부르는 쪽이 잠금·저장). 반영한 마감·연결은 그대로(항목 화면에서 되돌릴 수 있다). 없으면 False."""
    box = _box(store)
    keep = [t for t in box["threads"] if conversation_of(t) != conversation]
    if len(keep) == len(box["threads"]):
        return False
    box["threads"] = keep
    return True


def run_job(target, thread_id: str, message: str, course_id=None, ai_enabled: bool = False, runner=None,
            command: str | None = None, names: list | None = None, history: list | None = None) -> dict:
    """백그라운드 작업 하나: 파일에서 새로 읽은 사본으로 답을 만들고(잠금 밖), target(앱의 Store)에 잠금을 잡고 다시 읽은 뒤
    그 차례와 AI 실행 기록만 합쳐 저장한다(그 사이 다른 프로세스가 저장한 것을 지우지 않게). 그 사이 차례가 지워졌으면 버린다.
    history를 주지 않으면 사본에서 같은 대화의 앞 차례를 읽는다(history_for)."""
    from .agent_cli import RUN_LOG_KEEP
    from .store import Store
    work = Store(target.path)
    before = list(work.data.get("ai_runs") or [])
    try:
        if history is None:
            history = history_for(work, thread_id)
        result = answer(work, message, course_id, ai_enabled=ai_enabled, runner=runner, command=command, names=names,
                        history=history)
    except Exception as e:  # noqa: BLE001 — 화면이 '답하는 중'에 멈춰 있지 않게
        result = {"status": "error", "error": f"답을 만들지 못했어요({type(e).__name__})", "answer": None,
                  "proposals": [], "by": "rules", "finished_at": iso(now_local())}
    new_runs = [r for r in work.data.get("ai_runs") or [] if r not in before]
    with target.locked():
        target.reload_if_changed()
        th = find_thread(target, thread_id)
        if th is not None:
            th.update(result)
        if new_runs:
            runs = target.data.setdefault("ai_runs", [])
            runs.extend(r for r in new_runs if r not in runs)
            del runs[:-RUN_LOG_KEEP]
        target.write()
    return result


STALE_SECONDS = 300


def can_apply(p: dict) -> bool:
    """[반영]/[실행] 단추: 검사를 통과했고 아직 반영 안 함. 액션은 서버 동작만(client 동작은 화면이 직접 — 계약 22-9)."""
    if p.get("rejected_reason") or p.get("applied"):
        return False
    if p.get("type") == "action":
        return not p.get("client")
    return p.get("type") in _APPLY


def public_threads(store, active: set | None = None) -> list[dict]:
    """state.agent.threads: 오래된 것부터(대화 순서). '_'로 시작하는 칸은 빼고, 이 프로세스·다른 프로세스 어디에서도 끝나지
    않은 채 오래된 'running'(앱이 꺼졌던 경우)은 오류로 보여 준다. kind가 없는 옛 차례는 'missing'(계약 22-1),
    conversation이 없는 옛 차례는 conversation = 그 차례 id(혼자인 대화, 계약 24-1)."""
    now = now_local()
    out = []
    for t in threads(store):
        if not isinstance(t, dict):
            continue
        t2 = {k: v for k, v in t.items() if not str(k).startswith("_")}
        t2["conversation"] = conversation_of(t)
        t2.setdefault("carried", None)
        t2.setdefault("course_context", None)
        t2["proposals"] = [{**{k: v for k, v in p.items() if not str(k).startswith("_")},
                            "client": bool(p.get("client")), "can_apply": can_apply(p)}
                           for p in t.get("proposals") or [] if isinstance(p, dict)]
        if not isinstance(t2.get("citations"), list):
            t2["citations"] = []
        at = parse_iso(t.get("at"))
        if (t2.get("status") == "running" and (active is None or t2.get("id") not in active)
                and at and (now - at).total_seconds() > STALE_SECONDS):
            t2.update(status="error", error="답을 받지 못했어요(앱이 다시 켜졌을 수 있어요). 다시 물어봐 주세요")
        if not t2.get("kind"):   # 옛 대화(kind 없음)·답하기 전에 멈춘 대화는 'missing', 답하는 중이면 아직 모름(None)
            t2["kind"] = None if t2.get("status") == "running" else "missing"
        out.append(t2)
    return out


# ---------------------------------------------------------------- 반영·되돌리기(20-4)

MISSED_KEEP = 200


class AgentConflict(Exception):
    """이미 반영했음·그 사이 바뀜 같은 충돌(HTTP 409)."""


_RAW_KEYS = ("type", "course_id", "evidence", "source_key", "reason", "title", "due", "kind", "method", "item_id", "key",
             "component", "n", "components", "groups", "dist_kind", "cutoffs")


def reason_text(p: dict) -> str:
    """item·연결에 남길 이유: 이유 + (상대 날짜면) 계산 + 원문 인용(계약 13-3과 같은 모양)."""
    from .agent_cli import relative_note
    reason = p.get("reason") or "에이전트에게 묻기로 반영"
    due = parse_iso(p.get("due"))
    if p.get("date_basis") and due is not None:
        reason += " · " + relative_note(p["date_basis"], due)
    quote = _flat(p.get("evidence"))
    room = max(40, min(150, 300 - len(reason) - 12))
    if len(quote) > room:
        quote = quote[:room - 1] + "…"
    return f"{reason} — 근거: \"{quote}\"" if quote else reason


def _src(p: dict) -> dict:
    return {"title": p.get("source_title"), "type": p.get("source_type"), "posted_at": p.get("posted_at"),
            "posted_raw": p.get("posted_at")}


def _apply_add(store, ctx, p, th):
    import copy
    from .core import _hid
    due = parse_iso(p["due"])
    rid = "a-" + _hid(p["course_id"], iso(due), p["title"])
    who = "AI" if p["by"] == "ai" else "규칙 검색"
    notes = [f"'에이전트에게 묻기'로 찾음({who}) — 근거 문장이 원문에 있는 것만"] + list(p.get("notes") or [])
    if p["by"] == "rules":
        notes.append("날짜·제목을 확인하고 승인해 주세요")
    rec = {"id": rid, "course_id": p["course_id"], "course_name": (ctx.courses.get(p["course_id"]) or {}).get("name"),
           "title": p["title"], "due": iso(due), "kind": p.get("kind") or "과제", "method": p.get("method") or "unknown",
           "confidence": "medium", "notes": notes, "found_by": p["by"], "reason": p.get("reason"),
           "thread": th["id"], "at": iso(now_local()), "ref": p.get("_ref"),
           "evidence": [{"text": p["evidence"], "source_type": p.get("source_type") or "manual",
                         "source_title": p.get("source_title"), "url": p.get("url"), "posted_at": p.get("posted_at"),
                         "found_by": p["by"]}]}
    recs = [r for r in store.data.get("agent_items") or [] if isinstance(r, dict) and r.get("id") != rid]
    store.data["agent_items"] = recs + [rec]
    user_before = copy.deepcopy(store.data["user"].get(rid))
    core.refresh_items(store)
    if p["by"] == "ai":   # 사용자가 [반영]을 눌렀으므로 일정에 넣는다 — AI가 찾았다는 표시와 이유(되돌릴 수 있게)
        store.update_user(rid, {"decision": "approved"}, by="ai", reason=reason_text(p))
    # 반영 직후의 사용자 칸 — 그 뒤 사용자가 제목·마감·결정을 바꿨으면 되돌리지 않는다(사용자 쪽이 이김)
    return ({"item_id": rid, "user_before": user_before, "user_after": copy.deepcopy(store.data["user"].get(rid))},
            {"item_id": rid})


def _undo_add(store, u):
    rid = u["item_id"]
    if "user_after" in u and store.data["user"].get(rid) != u["user_after"]:
        raise AgentConflict("그 사이 이 항목을 직접 바꿔서 되돌리지 않았어요")
    store.data["agent_items"] = [r for r in store.data.get("agent_items") or [] if r.get("id") != rid]
    if u.get("user_before") is None:
        store.data["user"].pop(rid, None)
    else:
        store.data["user"][rid] = u["user_before"]
    core.refresh_items(store)


def _apply_fix(store, ctx, p, th):
    import copy
    iid = p["item_id"]
    before = copy.deepcopy(store.data["user"].get(iid))
    due = parse_iso(p["due"])
    store.update_user(iid, {"due": due.strftime("%Y-%m-%dT%H:%M")}, by="ai" if p["by"] == "ai" else "user",
                      reason=reason_text(p) if p["by"] == "ai" else None)
    store.data["user"][iid]["agent_ref"] = p.get("_ref")   # 항목 카드의 [되돌리기]가 /api/agent/undo를 부르게
    return {"item_id": iid, "before": before, "after": copy.deepcopy(store.data["user"].get(iid))}, {"item_id": iid}


def _undo_fix(store, u):
    if store.data["user"].get(u["item_id"]) != u["after"]:
        raise AgentConflict("그 사이 이 항목을 직접 바꿔서 되돌리지 않았어요")
    if u["before"] is None:
        store.data["user"].pop(u["item_id"], None)
    else:
        store.data["user"][u["item_id"]] = u["before"]


def _slot(store, name: str, cid: str) -> dict:
    return store.data.setdefault(name, {}).setdefault(str(cid), {})


def _apply_link(store, ctx, p, th):
    import copy
    slot = _slot(store, "grade_links_ai", p["course_id"])
    before = copy.deepcopy(slot.get(p["key"]))
    slot[p["key"]] = {"component": p["component"], "evidence": p["evidence"], "at": iso(now_local()), "error": None,
                      "reason": f"에이전트에게 묻기 · {p.get('reason')} — 근거: \"{_flat(p['evidence'])[:120]}\"",
                      "chat": True}
    return {"name": "grade_links_ai", "cid": p["course_id"], "k": p["key"], "before": before,
            "after": copy.deepcopy(slot[p["key"]])}, {}


def _apply_count(store, ctx, p, th):
    import copy
    k = lk.norm(p["component"])
    slot = _slot(store, "grade_counts_ai", p["course_id"])
    before = copy.deepcopy(slot.get(k))
    slot[k] = {"n": p["n"], "evidence": p["evidence"], "source_title": p.get("source_title"), "hash": None,
               "at": iso(now_local()), "error": None, "chat": True}
    return {"name": "grade_counts_ai", "cid": p["course_id"], "k": k, "before": before,
            "after": copy.deepcopy(slot[k])}, {}


def _undo_slot(store, u):
    slot = _slot(store, u["name"], u["cid"])
    if slot.get(u["k"]) != u["after"]:
        raise AgentConflict("그 사이 다른 값으로 바뀌어 되돌리지 않았어요")
    if u["before"] is None:
        slot.pop(u["k"], None)
    else:
        slot[u["k"]] = u["before"]


def _apply_weights(store, ctx, p, th):
    import copy
    from . import gradebook
    from .weights import WeightsFound, is_fixed
    grades = store.data.setdefault("grades", {})
    cfg = grades.get(p["course_id"])
    before = copy.deepcopy(cfg)
    comps = [{"name": c["name"], "weight": c["weight"], "fixed": is_fixed(c["name"])} for c in p["components"]]
    wf = WeightsFound(comps, round(sum(c["weight"] for c in comps), 2), p["evidence"], "medium",
                      ["'에이전트에게 묻기'로 AI가 찾음 — 맞는지 확인하세요"])
    line = gradebook._apply(store, p["course_id"], cfg, wf, _src(p), "ai", iso(now_local()), p.get("course_short") or "")
    if line is None:
        raise ValueError("지금과 같은 평가 비중이에요 — 바꿀 것이 없어요")
    grades[p["course_id"]]["auto_chat"] = {"thread": th["id"], "at": iso(now_local())}
    return {"cid": p["course_id"], "key": None, "before": before, "after": copy.deepcopy(grades[p["course_id"]])}, {}


def _apply_dist(store, ctx, p, th):
    import copy
    from . import gradebook
    from .distribution import DistFound
    grades = store.data.setdefault("grades", {})
    cfg = grades.get(p["course_id"])
    before = copy.deepcopy(cfg)
    if cfg is None:
        cfg = grades[p["course_id"]] = {"components": [], "dist": None, "class_size": None}
    df = DistFound(**p["dist"])
    df.notes = list(df.notes) + ["'에이전트에게 묻기'로 AI가 찾음 — 맞는지 확인하세요"]
    rec = gradebook._dist_record(df, _src(p), "ai", iso(now_local()))
    rec["chat"] = True
    cfg["dist_auto"] = rec
    return {"cid": p["course_id"], "key": "dist_auto", "before": before, "after": copy.deepcopy(cfg)}, {}


def _undo_cfg(store, u):
    grades = store.data.setdefault("grades", {})
    if grades.get(u["cid"]) != u["after"]:
        raise AgentConflict("그 사이 성적 설정이 바뀌어 되돌리지 않았어요(성적 화면에서 고쳐 주세요)")
    if u["before"] is None:
        grades.pop(u["cid"], None)
    else:
        grades[u["cid"]] = u["before"]


_APPLY = {"add_deadline": (_apply_add, _undo_add), "fix_due": (_apply_fix, _undo_fix),
          "link_score": (_apply_link, _undo_slot), "set_count": (_apply_count, _undo_slot),
          "set_weights": (_apply_weights, _undo_cfg), "set_dist": (_apply_dist, _undo_cfg)}


def proposal(store, thread_id, idx) -> tuple[dict, dict]:
    """(대화, 제안) — 없는 대화 LookupError, 없는 번호 ValueError."""
    return _proposal(store, thread_id, idx)


def _proposal(store, thread_id, idx) -> tuple[dict, dict]:
    th = find_thread(store, thread_id)
    if th is None:
        raise LookupError("대화를 찾을 수 없어요(지워졌을 수 있어요)")
    props = th.get("proposals") or []
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(props):
        raise ValueError("그런 제안이 없어요")
    return th, props[idx]


def apply(store, thread_id, idx, names: list | None = None) -> dict:
    """제안을 지금 상태로 다시 검증한 뒤 반영한다(부르는 쪽이 잠금·저장). 반영한 제안(화면용)을 돌려준다.
    없는 대화 LookupError, 이미 반영 AgentConflict, 반영할 수 없음 ValueError(한국어)."""
    from .extract import RULES_VERSION
    th, p = _proposal(store, thread_id, idx)
    if p.get("applied"):
        raise AgentConflict("이미 반영한 제안이에요")
    if p.get("type") == "action":   # 앱 동작은 화면의 [실행](features/agent.py → actions.run)으로만 — 계약 22-9
        raise ValueError("앱 동작은 앱 화면의 채팅에서 [실행]을 눌러 주세요")
    if p.get("type") == "note" or p.get("type") not in _APPLY:
        raise ValueError("설명만 있는 제안이라 반영할 것이 없어요")
    if p.get("rejected_reason"):
        raise ValueError(f"검증을 통과하지 못한 제안이에요: {p['rejected_reason']}")
    ctx = Ctx(store)
    again = check_proposal(ctx, {k: p.get(k) for k in _RAW_KEYS}, by=p.get("by") or "ai",
                           originals=[p.get("evidence") or ""], names=names)
    if again["rejected_reason"]:
        raise ValueError(f"지금 다시 확인해 보니 반영할 수 없어요: {again['rejected_reason']}")
    again["notes"] = list(dict.fromkeys((again.get("notes") or []) + (p.get("notes") or [])))
    again["_ref"] = {"thread": thread_id, "proposal": idx}
    undo, result = _APPLY[p["type"]][0](store, ctx, again, th)
    now = iso(now_local())
    p.update(applied=True, applied_at=now, undone_at=None, result=result, _undo=undo)
    cases = store.data.setdefault("missed_cases", [])
    from .agent_cli import redact
    cases.append({"at": now, "type": p["type"], "source_type": p.get("source_type"),
                  "evidence": redact(p.get("evidence") or "", names)[0][:300], "rule_version": RULES_VERSION,
                  "by": p.get("by"), "_ref": f"{thread_id}#{idx}"})
    del cases[:-MISSED_KEEP]
    return p


def undo(store, thread_id, idx) -> dict:
    """반영 전 값으로 되돌린다(항목은 지우고, 연결·개수·비중·학점 비율은 이전 값). 그 사이 직접 바꿨으면 AgentConflict."""
    th, p = _proposal(store, thread_id, idx)
    if p.get("type") == "action":
        raise ValueError("앱 동작은 앱 화면의 채팅에서 [되돌리기]를 눌러 주세요")
    if not p.get("applied") or not p.get("_undo"):
        raise AgentConflict("반영하지 않은 제안이에요")
    _APPLY[p["type"]][1](store, p["_undo"])
    p.update(applied=False, undone_at=iso(now_local()), result=None)
    p.pop("_undo", None)
    ref = f"{thread_id}#{idx}"
    store.data["missed_cases"] = [c for c in store.data.get("missed_cases") or [] if c.get("_ref") != ref]
    return p


def record_action(store, thread_id, idx, res: dict, undo: bool = False) -> dict | None:
    """actions.run/undo의 결과({"ok", "message", "undo"})를 그 액션 제안에 남긴다(부르는 쪽이 잠금·저장, 계약 22-9).
    실행: 성공이면 applied·applied_at·result{ok, message}·_undo(되돌릴 값, 화면에 안 나감). 되돌리기: 성공이면 applied false·
    undone_at. 실패면 result만(다시 누를 수 있게). 그 사이 대화·제안이 지워졌으면 None."""
    try:
        _, p = _proposal(store, thread_id, idx)
    except (LookupError, ValueError):
        return None
    if p.get("type") != "action":
        return None
    ok = bool(res.get("ok"))
    msg = _flat(res.get("message"))[:300] or None
    now = iso(now_local())
    p["result"] = {"ok": ok, "message": msg}
    if ok and not undo:
        p.update(applied=True, applied_at=now, undone_at=None, _undo=res.get("undo"))
    elif ok:
        p.update(applied=False, undone_at=now)
        p.pop("_undo", None)
    return p


def missed_cases_export(store) -> list[dict]:
    """[파일로 저장]용(계약 20-5): {at, type, source_type, evidence(가린 문장), rule_version, by}."""
    return [{k: v for k, v in c.items() if not str(k).startswith("_")}
            for c in store.data.get("missed_cases") or [] if isinstance(c, dict)]
