"""에이전트에게 묻기(채팅) — 계약 20장.

규칙이 놓친 것을 사용자가 말로 짚으면("기말고사 날짜가 안 보여요", "성적이 안 들어와요") 이 PC에 모아 둔 글
(공지·게시판·메시지·강의계획서·과제 설명·첨부 PDF 글·직접 붙여 넣은 글)에서 관련 문단을 찾고(search),
AI가 켜져 있으면 AI CLI 작업 `assist`(agent_cli)에 보내 근거 있는 제안을 받는다. 하네스가 검증한 제안만 반영할 수 있고
(check_proposal), 반영한 것은 decided_by "ai"·이유·되돌리기가 남는다(apply/undo). AI가 꺼져 있으면 같은 창이 규칙 검색으로
답한다(by "rules"). 특정 과목·문장에 맞추지 않는다: 평가 낱말 계열(links.families), 날짜(extract.find_dates),
점수 문장(grades.find_scores)처럼 일반 구조로 점수를 매긴다.

저장 칸(store.data):
- "agent": {"threads": [대화, 최근 20개]} — 대화 = {id, at, message, course_id, status, answer, proposals, searched,
  results, error, by, finished_at}. 제안의 "_"로 시작하는 칸(되돌리기 기록)은 화면에 내보내지 않는다.
- "agent_items": [반영한 마감 기록] — core.build_items가 함께 합친다(다음 동기화에도 남게, 규칙이 같은 마감을 찾으면 하나로).
- "missed_cases": [규칙이 놓친 사례(가린 문장)] — 사용자가 원할 때만 파일로 저장(자동으로 보내지 않음).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import core
from . import links as lk
from .extract import deadline_kind, find_dates, html_to_text
from .store import hidden_courses
from .timeutil import iso, now_local, parse_iso

MAX_THREADS = 20
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
         "of", "for", "in", "on", "to", "and", "or", "do", "does", "don't", "doesn't", "show", "shown", "please"}


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
    toks = [t.lower() for t in tokens(message) if not lk.families(t) and not DATE_WORDS.search(t)
            and not SCORE_WORDS.search(t)]
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
TYPES = ("add_deadline", "fix_due", "link_score", "set_count", "set_weights", "set_dist", "note")
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


# ---------------------------------------------------------------- 한 번 묻기

def answer(store, message: str, course_id=None, ai_enabled: bool = False, runner=None, command: str | None = None,
           names: list | None = None, session=None) -> dict:
    """질문 하나에 답한다(잠금 밖에서, 몇십 초 걸릴 수 있음). store는 이 작업만 쓰는 사본(Store(path))이 좋다 —
    AI 실행 기록(ai_runs)만 이 store에 더해진다. 결과: 대화 칸(status, answer, proposals, searched, results, error, by …)."""
    ctx = Ctx(store)
    results, searched = _search(store, message, course_id, items=ctx.items)
    cids = scope_of(ctx, message, course_id, results)
    out = {"status": "done", "searched": searched, "results": results, "error": None, "by": "rules",
           "answer": "", "proposals": [], "attempts": 0, "notes": []}
    if ai_enabled:
        from .agent_cli import AiSession
        session = session or AiSession(store, runner=runner, command=command, enabled=True, max_calls=2)
        text, originals = state_text(ctx, cids)
        originals += [r["snippet"] for r in results]
        inputs = {"message": message, "names": names, "state_text": text,
                  "courses": [{"id": cid, "short": ctx.shorts[cid]} for cid in ctx.courses],
                  "snippets": [{"source_key": r["source_key"], "course_id": r["course_id"], "type": r["type"],
                                "title": r["title"], "posted_at": r["posted_at"], "text": r["snippet"]} for r in results],
                  "check": lambda p, why: check_proposal(ctx, p, "ai", why, originals, names)}
        res = session.run("assist", inputs)
        out["attempts"] = res.attempts
        if res.ok:
            out.update(by="ai", answer=res.data["answer"], proposals=res.data["proposals"], notes=list(res.notes))
            out["finished_at"] = iso(now_local())
            return out
        out["error"] = res.error
    out["answer"], out["proposals"] = rules_answer(ctx, message, results, searched, cids, out["error"], ai_enabled)
    out["finished_at"] = iso(now_local())
    return out


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


def new_thread(store, message: str, course_id: str | None) -> dict:
    """대화 하나를 'running'으로 만든다(부르는 쪽이 잠금·저장). 최근 MAX_THREADS개만 남긴다."""
    import uuid
    th = {"id": "t-" + uuid.uuid4().hex[:10], "at": iso(now_local()), "message": message, "course_id": course_id,
          "status": "running", "answer": None, "proposals": [], "searched": 0, "results": [], "error": None,
          "by": None, "finished_at": None}
    box = _box(store)
    box["threads"].append(th)
    del box["threads"][:-MAX_THREADS]
    return th


def run_job(target, thread_id: str, message: str, course_id=None, ai_enabled: bool = False, runner=None,
            command: str | None = None, names: list | None = None) -> dict:
    """백그라운드 작업 하나: 파일에서 새로 읽은 사본으로 답을 만들고(잠금 밖), target(앱의 Store)에 잠금을 잡고 다시 읽은 뒤
    그 대화와 AI 실행 기록만 합쳐 저장한다(그 사이 다른 프로세스가 저장한 것을 지우지 않게). 그 사이 대화가 지워졌으면 버린다."""
    from .agent_cli import RUN_LOG_KEEP
    from .store import Store
    work = Store(target.path)
    before = list(work.data.get("ai_runs") or [])
    try:
        result = answer(work, message, course_id, ai_enabled=ai_enabled, runner=runner, command=command, names=names)
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


def public_threads(store, active: set | None = None) -> list[dict]:
    """state.agent.threads: 오래된 것부터(대화 순서). '_'로 시작하는 칸은 빼고, 이 프로세스·다른 프로세스 어디에서도 끝나지
    않은 채 오래된 'running'(앱이 꺼졌던 경우)은 오류로 보여 준다."""
    now = now_local()
    out = []
    for t in threads(store):
        if not isinstance(t, dict):
            continue
        t2 = {k: v for k, v in t.items() if not str(k).startswith("_")}
        t2["proposals"] = [{**{k: v for k, v in p.items() if not str(k).startswith("_")},
                            "can_apply": bool(not p.get("rejected_reason") and not p.get("applied")
                                              and p.get("type") != "note")}
                           for p in t.get("proposals") or [] if isinstance(p, dict)]
        at = parse_iso(t.get("at"))
        if (t2.get("status") == "running" and (active is None or t2.get("id") not in active)
                and at and (now - at).total_seconds() > STALE_SECONDS):
            t2.update(status="error", error="답을 받지 못했어요(앱이 다시 켜졌을 수 있어요). 다시 물어봐 주세요")
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
    if not p.get("applied") or not p.get("_undo"):
        raise AgentConflict("반영하지 않은 제안이에요")
    _APPLY[p["type"]][1](store, p["_undo"])
    p.update(applied=False, undone_at=iso(now_local()), result=None)
    p.pop("_undo", None)
    ref = f"{thread_id}#{idx}"
    store.data["missed_cases"] = [c for c in store.data.get("missed_cases") or [] if c.get("_ref") != ref]
    return p


def missed_cases_export(store) -> list[dict]:
    """[파일로 저장]용(계약 20-5): {at, type, source_type, evidence(가린 문장), rule_version, by}."""
    return [{k: v for k, v in c.items() if not str(k).startswith("_")}
            for c in store.data.get("missed_cases") or [] if isinstance(c, dict)]
