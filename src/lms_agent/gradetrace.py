"""성적 파일 진단(계약 27-3): 성적이 될 수 있는 파일·출처마다 어디까지 왔고 **어디서 멈췄는지**.

사용자 질문: "PDF를 다운 못 받는 것인지, 받았는데 성적을 산출 못하는 것인지". 과목마다 파일 형식·성적 양식이 다르므로
파일(또는 Canvas 성적부 점수)마다 단계를 차례로 본다:

    찾음(found) → 받음(downloaded) → 읽음(read) → 점수표(score_table) → 내 줄(my_row) → 후보(candidate) → 연결(linked) → 추정(estimate)

- 찾음: Canvas·LearningX 글의 첨부(공지·과제·강의계획서에 링크·첨부 칸 파일 = 가상 게시판, core._linked_files), 받은 파일(downloads),
  첨부에서 뽑은 글(pdf_texts — PDF·표·문서·zip 속 파일), Canvas 성적부 점수(assignments[].submission.score → canvas_grade 후보).
  Canvas가 첨부 정보를 주지 않은 글(cache.errors의 '…의 첨부를 확인하지 못했습니다')은 found=False 줄.
- 받음·읽음: downloads 기록(pdf_ok·sheet_ok·read_ok와 그 notes), materials_errors(파일 이름별 이유), 자동 저장 설정, 크기 한도.
- 점수표·내 줄: 성적 후보가 쓰는 것과 같은 판정(gradebook._is_score_rec, 없으면 scorefile.is_score_file / is_score_sheet).
  내 줄은 학번이 그 글·표에 있는지 **참/거짓만** 본다(가린 학번이 여러 줄과 맞으면 본인을 고르지 않음 — scorefile과 같은 규칙).
- 후보·연결·추정: gradebook.score_file_candidates(전부)와 성적 화면 묶음(gradebook.section — 연결·세부 점수·추정)을 그대로 읽는다.

행은 읽기만 한다(아무것도 저장하지 않음). 개인정보: 학번·다른 학생의 행·첨부 주소(확인값 포함)는 넣지 않는다.
파일 이름·글 제목에 내 학번이 섞여 있으면 가린다. 후보에는 내 점수와 반 집계(평균·표준편차·인원)만 — 성적 화면과 같다.
제외한 과목(hidden)은 기본으로 빼고 수만 센다(show_all이면 '제외한 과목' 줄로 넣는다 — 성적 화면처럼 추정에는 들어가지 않음).

화면은 state.grades.trace(행 목록)·state.grades.trace_summary, 터미널은 `lms-agent grades-check`(report_text)가 같은 행을 쓴다.

AI로 철저히 찾기(계약 29-2, aiscores): 줄마다 "ai": {"status": "found"|"none"|"error"|"pending"|None, "results", "at", "note"}.
규칙이 못 읽은 파일을 AI가 읽었으면 그 결과 후보(source_type "ai_score")로 연결·추정까지 이어 가고 메모 "AI가 찾음",
AI가 켜져 있고 아직 묻지 않은 대상이면 멈춘 줄에 "AI로 찾아볼 수 있어요". state.grades.ai_lookup(aiscores.lookup_state)도 여기서 더한다.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from . import aiscores, gradebook, scorefile
from .store import course_mode, hidden_courses

STAGES = (("found", "찾음"), ("downloaded", "받음"), ("read", "읽음"), ("score_table", "점수표"),
          ("my_row", "내 줄"), ("candidate", "후보"), ("linked", "연결"), ("estimate", "추정"))
STAGE_LABEL = dict(STAGES)
_FILE_ONLY = ("downloaded", "read", "score_table", "my_row")   # Canvas 성적부 점수에는 해당 없음

SOURCE_KO = {"announcement": "공지", "assignment": "과제", "syllabus": "강의계획서", "board": "게시판",
             "canvas_grade": "Canvas 성적부", "upload": "직접 가져온 파일", "unknown": "출처 모름"}
_VIRTUAL = {"announcements": "announcement", "assignments": "assignment", "syllabus": "syllabus"}

# 점수표일 수 있는 이름(파일 이름·글 제목) — scorefile._SCORE_NAME과 같은 낱말. 진단에 넣을 줄을 고르는 데만 쓴다
_SCOREISH = re.compile(r"score|점수|성적|grade|result|결과|채점", re.I)
SHEET_EXTS = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv")
DOC_EXTS = (".docx", ".doc", ".pptx", ".ppt", ".hwp", ".hwpx", ".zip")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".heic", ".webp", ".tif", ".tiff")
_MASK = set("*#●○■□xX＊")
_PUNCT = ",;:()[]{}|\"'<>"
_GENERIC_NOTES = ("표준편차는 표본표준편차", "다른 학생의 행·학번은")

DOWNLOAD_STOP = {
    "hidden": "제외한 과목이라 받지 않아요",
    "off": "첨부 자동 저장이 꺼져 있어 받지 않았어요 — 설정 > 첨부 자료에서 켜세요",
    "size": "파일 크기 한도({limit})를 넘어 받지 않았어요",
    "error": "내려받지 못했어요(Canvas가 학생에게 막았거나 네트워크 문제일 수 있어요)",
    "save": "받았지만 이 PC에 저장하지 못했어요(폴더 권한·경로 길이를 확인하세요)",
    "outside": "저장 위치가 다운로드 폴더 밖이라 받지 않았어요",
    "missing": "받아 둔 파일이 지워졌어요 — 다음 동기화 때 다시 받아요",
    "pending": "아직 받지 않았어요 — 다음 동기화 때 받아요",
}
READ_STOP = {
    "scanned": "글자를 못 읽었어요(스캔본·그림으로 된 파일일 수 있어요)",
    "image": "그림 파일이라 글자를 읽지 못해요",
    "encrypted": "암호가 걸려 있어 읽지 못했어요",
    "unsupported": "이 형식({ext})은 아직 읽지 못해요",
    "unreadable": "파일 내용을 읽지 못했어요",
    "pending": "받았지만 아직 읽지 않았어요 — 다음 동기화 때 읽어요",
}
STOP = {
    "not_found": "Canvas가 이 글의 첨부 파일 정보를 주지 않았어요(학생에게 닫힌 파일일 수 있어요)",
    "not_table": "점수표가 아니라고 봤어요(학번과 점수가 있는 줄이 5개보다 적어요)",
    "not_table_sheet": "점수표가 아니라고 봤어요(학번 열과 점수 열을 찾지 못했어요)",
    "no_sid": "학번을 넣지 않아 내 줄을 찾지 않았어요",
    "name_only": "파일 이름은 점수표 같지만 학번·점수 줄을 찾지 못했어요(점수표가 아닐 수 있어요)",
    "masked_many": "가린 학번과 맞는 줄이 여러 개라 내 줄을 고르지 않았어요",
    "no_row": "내 학번 줄이 없어요(다른 분반 파일일 수 있어요)",
    "no_result": "내 줄은 있지만 점수를 계산하지 못했어요(칸 수가 다르거나 인원이 너무 적어요)",
    "empty_cell": "내 칸이 비어 있어요(점수 없음)",
    "absent": "내 칸이 '{mark}'(결시·미응시 표시)라 점수가 없어요 — 0점으로 세지 않아요",
    "letter_only": "점수 대신 학점 글자('{letter}')만 있어요 — 학점 글자는 점수가 아니에요",
    "no_canvas_cand": "Canvas 성적부 점수가 점수 후보에 없어요",
    "no_course": "어느 과목 파일인지 몰라 평가 항목에 연결하지 않았어요",
    "hidden": "제외한 과목이라 성적에 넣지 않아요",
    "not_listed": "점수 후보가 너무 많아 성적 화면 목록에서 밀렸어요",
    "unknown_course": "과목 목록에 없는 과목이라 연결하지 않았어요",
    "no_components": "이 과목의 평가 항목(비중)이 아직 없어요 — 성적 화면에서 넣어 주세요",
    "user_excluded": "성적에 넣지 않기로 했어요(직접 정함)",
    "unlinked": "평가 항목에 연결되지 않았어요 — 성적 화면에서 고르세요",
    "pass_fail": "P/NP 과목이라 학점 추정을 하지 않아요",
    "fixed": "'{comp}' 항목은 점수를 직접 넣어 고정해 두어 이 점수를 쓰지 않아요",
    "superseded": "같은 이름의 더 최근 점수가 대신 쓰였어요",
    "not_member": "평가 항목의 세부 점수에 들어가지 않았어요",
    "no_estimate": "추정을 계산하지 못했어요",
}
AI_NOTE = {   # 계약 29-2
    "found": "AI가 찾음 — 규칙이 못 읽은 파일을 AI가 읽었어요(맞지 않으면 성적 화면에서 '넣지 않음')",
    "pending": "AI로 찾아볼 수 있어요 — 성적 화면의 [AI로 성적 찾기](동기화 뒤에도 몇 개씩 자동으로)",
    "none": "AI도 내 점수를 찾지 못했어요",
    "error": "AI로 찾지 못했어요",
}
_AI_NONE = {"status": None, "results": [], "at": None, "note": None}

_MEMO: dict = {}
_MEMO_MAX = 2000


# ---------------------------------------------------------------- 작은 도우미

def _ext(name) -> str:
    return Path(str(name or "")).suffix.lower()


def _scoreish(s) -> bool:
    return bool(s) and bool(_SCOREISH.search(str(s)))


def _clean_url(u) -> str | None:
    """글 주소에서 쿼리·조각을 뗀다(첨부 주소의 verifier 같은 값이 섞이지 않게). http(s)만."""
    if not isinstance(u, str) or not u:
        return None
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def scrub(s, sid: str | None):
    """글에 섞인 내 학번을 가린다(화면에 보이는 것과 같은 모양 2026****02)."""
    if not isinstance(s, str) or not s or not sid:
        return s
    return re.sub(re.escape(sid), gradebook.mask_student_id(sid) or "****", s, flags=re.I)


def _masked_match(tok: str, sid: str) -> bool:
    """가린 학번(2026****02)의 보이는 자리가 내 학번과 맞는지 — scorefile의 규칙과 같다."""
    if len(tok) != len(sid) or not any(ch in _MASK for ch in tok):
        return False
    visible = [(a, b) for a, b in zip(tok, sid) if a not in _MASK]
    return len(visible) >= len(sid) / 2 and all(a.lower() == b.lower() for a, b in visible)


def _is_sheet_rec(rec: dict) -> bool:
    return rec.get("kind") == "sheet" and isinstance(rec.get("sheets"), list)


def _rec_kind(rec: dict, filename: str) -> str:
    k = str(rec.get("kind") or "")
    if k == "sheet":
        return "sheet"
    if k.startswith("zip"):
        return "zip"
    if k == "doc":
        return "doc"
    return _kind_of_name(filename)


def _kind_of_name(name) -> str:
    e = _ext(name)
    if e == ".pdf":
        return "pdf"
    if e in SHEET_EXTS:
        return "sheet"
    if e == ".zip":
        return "zip"
    if e in DOC_EXTS:
        return "doc"
    if e in IMAGE_EXTS:
        return "image"
    return "other"


def _source_of(board_id=None, post_key=None) -> str:
    if board_id in _VIRTUAL:
        return _VIRTUAL[board_id]
    pk = str(post_key or "")
    for bid, src in _VIRTUAL.items():
        if pk.startswith(f"board:{bid}-"):
            return src
    return "board" if (board_id is not None or pk.startswith("board:")) else "unknown"


def _stages(stop: str | None, na=()) -> list[dict]:
    out, hit = [], False
    for sid_, label in STAGES:
        if sid_ in na:
            st = "na"
        elif hit:
            st = "todo"
        elif sid_ == stop:
            st, hit = "stop", True
        else:
            st = "done"
        out.append({"id": sid_, "label": label, "state": st})
    return out


# ---------------------------------------------------------------- 글·표 한 개 살피기(점수표인지, 내 학번이 있는지)

def _scan_text(text: str, sid: str) -> tuple[bool, int]:
    """(내 학번이 그대로 있는지, 가린 학번이 맞는 줄 수). 값은 참/거짓·개수만 — 줄 내용은 남기지 않는다."""
    low, masked = sid.lower(), 0
    for line in text.splitlines():
        hit = False
        for t in line.split():
            t = t.strip(_PUNCT)
            if not t:
                continue
            if t.lower() == low:
                return True, masked
            if not hit and _masked_match(t, sid):
                hit = True
        masked += hit
    return False, masked


def _scan_sheets(sheets: list, sid: str) -> tuple[bool, int]:
    low, masked = sid.lower(), 0
    for sh in sheets or []:
        for row in (sh.get("rows") if isinstance(sh, dict) else None) or []:
            hit = False
            for v in (row.values() if isinstance(row, dict) else []):
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    try:
                        s = str(int(v)) if float(v).is_integer() else ""
                    except (OverflowError, ValueError):
                        s = ""
                elif isinstance(v, str):
                    s = v.strip()
                else:
                    continue
                if not s:
                    continue
                if s.lower() == low:
                    return True, masked
                if not hit and _masked_match(s, sid):
                    hit = True
            masked += hit
    return False, masked


def _is_score_table(rkey: str, rec: dict) -> bool:
    """성적 후보가 쓰는 판정 그대로(gradebook._is_score_rec), 없으면 같은 규칙으로."""
    f = getattr(gradebook, "_is_score_rec", None)
    if callable(f):
        try:
            return bool(f(rkey, rec))
        except Exception:
            pass
    fn = rec.get("filename") or ""
    if _is_sheet_rec(rec):
        return bool(scorefile.is_score_sheet(rec.get("sheets"), fn))
    return bool(scorefile.is_score_file(fn, rec.get("text") or ""))


def _analyze(rkey: str, rec: dict, sid: str | None) -> dict:
    """{"score_table", "shape"(이름을 빼고 모양만으로도 점수표인지), "exact", "masked"} — 같은 글이면 기억한 값."""
    text = rec.get("text") if isinstance(rec.get("text"), str) else ""
    sheet = _is_sheet_rec(rec)
    k = (rkey, sid, rec.get("filename"), len(text), hash(text), sheet,
         len(rec.get("sheets") or []) if sheet else 0)
    got = _MEMO.get(k)
    if got is not None:
        return got
    st = _is_score_table(rkey, rec)
    if sheet:
        try:
            shape = bool(scorefile.is_score_sheet(rec.get("sheets"), None))
        except Exception:
            shape = False
    else:
        shape = bool(scorefile.is_score_file(None, text))
    exact, masked = (False, 0)
    if sid and st:
        exact, masked = _scan_sheets(rec.get("sheets"), sid) if sheet else _scan_text(text, sid)
    got = {"score_table": st, "shape": shape, "exact": exact, "masked": masked}
    if len(_MEMO) >= _MEMO_MAX:
        _MEMO.clear()
    _MEMO[k] = got
    return got


# ---------------------------------------------------------------- 파일 묶기: 첨부 + 받은 기록 + 뽑은 글

def _units(store) -> list[dict]:
    """같은 파일(과목 + Canvas 파일 id, 없으면 첨부 key)을 하나로: 여러 글에 붙은 같은 파일은 한 번만 받으므로(materials.plan)."""
    d = store.data
    cache = d.get("cache") or {}
    downloads = d.get("downloads") or {}
    texts = d.get("pdf_texts") or {}
    units: list[dict] = []
    by_ident: dict = {}
    by_key: dict = {}

    def ident(cid, x, key):
        fid = x.get("canvas_file_id") if isinstance(x, dict) else None
        return ("f", str(cid), str(fid)) if fid not in (None, "") else ("k", key)

    for b in cache.get("boards") or []:
        cid = str(b.get("course_id") or "")
        if not cid:
            continue
        for p in b.get("posts") or []:
            for a in p.get("attachments") or []:
                if not isinstance(a, dict) or a.get("id") is None:
                    continue
                key = f"{cid}:{a['id']}"
                if key in by_key:
                    continue
                idt = ident(cid, a, key)
                u = by_ident.get(idt)
                if u is None:
                    u = {"key": key, "cid": cid, "att": a, "post": p, "board": b, "dl": None, "dl_key": None, "recs": [],
                         "keys": []}
                    by_ident[idt] = u
                    units.append(u)
                u["keys"].append(key)
                u.setdefault("posts", []).append((key, a, p, b))
                by_key[key] = u
    for key, m in downloads.items():
        if not isinstance(m, dict):
            continue
        cid = str(m.get("course_id") or str(key).split(":")[0] or "")
        u = by_key.get(key) or by_ident.get(ident(cid, m, key))
        if u is None or u["dl"] is not None:
            u = {"key": key, "cid": cid or None, "att": None, "post": None, "board": None, "dl": None, "dl_key": None,
                 "recs": [], "keys": []}
            units.append(u)
        u["dl"], u["dl_key"], u["key"] = m, key, key
        for k2, a, p, b in u.get("posts") or []:   # 받은 글의 첨부를 대표로(글 제목·주소)
            if k2 == key:
                u["att"], u["post"], u["board"] = a, p, b
        if key not in u["keys"]:
            u["keys"].append(key)
        by_key[key] = u
    known = sorted(by_key, key=len, reverse=True)
    for rkey, rec in texts.items():
        if not isinstance(rec, dict):
            continue
        u = by_key.get(rkey) or by_key.get(str(rec.get("parent_key") or ""))   # zip 속 파일: parent_key = 첨부 key(계약 27-2)
        if u is None:   # parent_key가 없는 옛 기록: 가장 긴 첨부 key(뒤가 글자·숫자가 아닌 곳에서 끊김 — '<key>/<안 이름>')
            u = next((by_key[k] for k in known if rkey.startswith(k) and len(rkey) > len(k) and not rkey[len(k)].isalnum()),
                     None)
        if u is None:
            cid = rec.get("course_id")
            u = {"key": rkey, "cid": str(cid) if cid is not None else None, "att": None, "post": None, "board": None,
                 "dl": None, "dl_key": None, "recs": [], "keys": [rkey]}
            units.append(u)
            by_key[rkey] = u
        u["recs"].append((rkey, rec))
    return units


# ---------------------------------------------------------------- 받음·읽음 판정

def _errors_by_name(store) -> dict:
    """materials_errors("'<파일 이름>': <이유>") → {파일 이름: 이유 코드}."""
    out = {}
    for e in (store.data.get("materials_errors") or []):
        m = re.match(r"^'(.*)': (.*)$", str(e or ""), re.S)
        if not m:
            continue
        why = m.group(2)
        code = ("size" if "커서" in why else "outside" if "폴더 밖" in why else "save" if "저장하지" in why else "error")
        out[m.group(1)] = code
    return out


def _download_state(u: dict, ctx: "_Ctx"):
    """True | 이유 코드(hidden·off·size·error·save·outside·missing·pending)."""
    m = u.get("dl")
    if m is not None:
        path = m.get("path")
        if not u["recs"] and path:
            try:
                if not Path(path).is_file():
                    return "missing"
            except OSError:
                return "missing"
        return True
    if u["recs"]:
        return True   # 받은 기록이 없는 글 기록(예전 판) — 글은 이미 있다
    if u.get("cid") in ctx.hidden:
        return "hidden"
    if not ctx.auto_download:
        return "off"
    name = _filename(u)
    if name in ctx.errors:
        return ctx.errors[name]
    size = (u.get("att") or {}).get("filesize")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size > ctx.max_bytes:
        return "size"
    return "pending"


def _notes_of(m: dict, *fields) -> list[str]:
    out = []
    for f in fields:
        v = m.get(f)
        for n in (v if isinstance(v, (list, tuple)) else [v] if v else []):
            s = " ".join(str(n).split())[:160]
            if s and s not in out:
                out.append(s)
    return out


def _classify(notes: list[str], default: str) -> str:
    t = " ".join(notes)
    if "암호" in t:
        return "encrypted"
    if re.search(r"스캔|그림|이미지|글자를 찾지", t):
        return "scanned"
    if re.search(r"지원|형식", t):
        return "unsupported"
    return default


def _read_failure(u: dict) -> tuple[str, list[str]]:
    """받았는데 글 기록이 없는 파일: (이유 코드, 메모)."""
    m = u.get("dl") or {}
    name = _filename(u)
    ext = _ext(name)
    if m.get("pdf_ok") is False:
        notes = _notes_of(m, "pdf_notes")
        return _classify(notes, "scanned"), notes
    if m.get("sheet_ok") is False:
        notes = _notes_of(m, "sheet_notes")
        return _classify(notes, "unreadable"), notes
    if m.get("read_ok") is False:
        notes = _notes_of(m, "read_notes")
        return _classify(notes, "unreadable"), notes
    if ext in IMAGE_EXTS:
        return "image", []
    if ext == ".pdf" or ext in SHEET_EXTS:
        return "pending", []
    if ext in DOC_EXTS:
        try:
            from . import docs as _docs   # noqa: F401 — 문서·zip 읽기(계약 27-2)가 있으면 다시 읽기로 읽힌다
            return "pending", []
        except ImportError:
            return "unsupported", []
    return "unsupported", []


def _filename(u: dict) -> str:
    m, a = u.get("dl") or {}, u.get("att") or {}
    rec = u["recs"][0][1] if u.get("recs") else {}
    return str(m.get("filename") or a.get("filename") or a.get("display_name") or rec.get("filename")
               or f"첨부 {a.get('id') or u.get('key')}")


# ---------------------------------------------------------------- 맥락(한 번만 만드는 표)

class _Ctx:
    def __init__(self, store, grades: dict):
        from .materials import MAX_BYTES
        d = store.data
        cache = d.get("cache") or {}
        self.store = store
        self.sid = (d.get("settings") or {}).get("student_id") or None
        self.hidden = hidden_courses(d)
        self.auto_download = bool((d.get("settings") or {}).get("auto_download", True))
        self.max_bytes = MAX_BYTES
        self.errors = _errors_by_name(store)
        self.courses = {str(c.get("course_id")): c for c in grades.get("courses") or [] if isinstance(c, dict)}
        from .core import short_course_name
        self.order = {}
        self.names = {}
        for i, c in enumerate(cache.get("courses") or []):
            self.order[str(c.get("id"))] = i
            self.names[str(c.get("id"))] = short_course_name(c.get("name"))
        for cid, c in self.courses.items():
            self.names.setdefault(cid, c.get("short_name"))
        self.shown = {c["key"]: c for c in grades.get("candidates") or [] if isinstance(c, dict) and c.get("key")}
        # Canvas 성적부와 같은 점수라 성적부 후보 하나로 합쳐진 파일·글 후보(gradebook.merge_canvas_duplicates, 계약 27-1)
        self.merged_into = {m["key"]: c for c in self.shown.values() for m in c.get("merged") or []
                            if isinstance(m, dict) and m.get("key")}
        try:
            files = gradebook.score_file_candidates(store)   # 100개로 자르지 않은 전부(제외한 과목 포함)
        except Exception:
            files = [c for c in self.shown.values() if str(c.get("key", "")).startswith("pdf:")]
        texts = d.get("pdf_texts") or {}
        self.by_rec: dict = {}
        for c in files:
            k = str(c.get("key") or "")
            if not k.startswith("pdf:"):
                continue
            s = k[4:]
            cut = [i for i, ch in enumerate(s) if ch == "#"]
            rkey = s if s in texts else next((s[:i] for i in reversed(cut) if s[:i] in texts), None)
            if rkey is not None:
                self.by_rec.setdefault(rkey, []).append(c)
        self.members: dict = {}
        for cid, c in self.courses.items():
            slot = self.members.setdefault(cid, {})
            for comp in (c.get("config") or {}).get("components") or []:
                if isinstance(comp, dict) and comp.get("name"):
                    slot[comp["name"]] = comp
        # AI가 찾은 점수 후보(계약 29-2) — 출처 파일(pdf_texts key)별
        self.ai_on = bool((d.get("settings") or {}).get("ai_cli"))
        self.ai_by_rec: dict = {}
        for c in self.shown.values():
            if c.get("source_type") == aiscores.SOURCE_TYPE and c.get("ai_source"):
                self.ai_by_rec.setdefault(str(c["ai_source"]), []).append(c)

    def short(self, cid) -> str | None:
        return self.names.get(str(cid)) if cid is not None else None


# ---------------------------------------------------------------- 줄 만들기

def _base(u: dict, ctx: _Ctx) -> dict:
    m, a, p, b = u.get("dl") or {}, u.get("att") or {}, u.get("post") or {}, u.get("board") or {}
    rec0 = u["recs"][0][1] if u.get("recs") else {}
    cid = u.get("cid")
    name = _filename(u)
    post_key = m.get("post_key") or (f"board:{b.get('board_id')}-{p.get('id')}" if b else None)
    # 성적 화면에서 직접 가져온 파일(계약 29-1, pdf_texts "import:…" — source·board_id "upload")
    upload = rec0.get("source") == "upload" or rec0.get("board_id") == "upload" or str(u.get("key") or "").startswith("import:")
    path = m.get("path")
    exists = False
    if path:
        try:
            exists = Path(path).is_file()
        except OSError:
            exists = False
    return {"id": "f:" + str(u["key"]), "course_id": cid, "course_short": ctx.short(cid),
            "course_hidden": bool(cid) and cid in ctx.hidden,
            "file": scrub(name, ctx.sid), "kind": _kind_of_name(name),
            "source": "upload" if upload else _source_of(b.get("board_id") if b else None, post_key),
            "post_title": scrub(m.get("post_title") or p.get("title"), ctx.sid) or None,
            "posted_at": (m.get("posted_at") or p.get("created_at") or p.get("updated_at") or rec0.get("posted_at")
                          or rec0.get("imported_at")),
            "url": _clean_url(p.get("post_url")),
            "material_key": u.get("dl_key"), "material_exists": exists,
            "found": True, "downloaded": None, "read": None, "score_table": None, "my_row": None,
            "candidate": None, "linked": None, "link_by": None, "in_estimate": False,
            "stage": None, "stop": None, "notes": [], "relevant": False}


def _finish(row: dict, stage: str | None, stop: str | None, na=()) -> dict:
    row["stage"], row["stop"] = stage, stop
    row["stages"] = _stages(stage, na)
    return row


def _cand_info(c: dict) -> dict:
    return {"key": c.get("key"), "label": c.get("label") or c.get("source_title"), "score": c.get("score"),
            "max_score": c.get("max_score"), "mean": c.get("mean"), "sd": c.get("sd"), "n": c.get("n")}


def _cand_notes(c: dict, sid) -> list[str]:
    out = []
    for n in c.get("notes") or []:
        s = " ".join(str(n).split())
        if s and not s.startswith(_GENERIC_NOTES):
            out.append(scrub(s[:160], sid))
    return out[:3]


def _after_candidate(row: dict, c: dict, ctx: _Ctx, na=()) -> dict:
    """후보가 있는 줄: 연결 → 추정."""
    row["candidate"] = _cand_info(c)
    if row["candidate"]["label"]:
        row["candidate"]["label"] = scrub(str(row["candidate"]["label"]), ctx.sid)
    row["id"] = row["id"] + "#" + str(c.get("key"))
    row["relevant"] = True
    row["notes"] = (row["notes"] + _cand_notes(c, ctx.sid))[:4]
    if c.get("score") is None:   # 점수 없는 결과(계약 29-3 — scorefile.result_extras): 애매함·결시·학점 글자만
        if c.get("ambiguous"):
            row["my_row"], row["candidate"] = False, None
            return _finish(row, "my_row", STOP["masked_many"], na)
        if c.get("absent"):
            return _finish(row, "candidate", STOP["absent"].format(mark=scrub(str(c["absent"]), ctx.sid)[:20]), na)
        if c.get("letter"):
            return _finish(row, "candidate", STOP["letter_only"].format(letter=str(c["letter"])[:4]), na)
        return _finish(row, "candidate", STOP["empty_cell"], na)
    cid = str(c.get("course_id")) if c.get("course_id") is not None else None
    if cid is None:
        return _finish(row, "linked", STOP["no_course"], na)
    if cid in ctx.hidden:
        return _finish(row, "linked", STOP["hidden"], na)
    course = ctx.courses.get(cid)
    if course is None:
        return _finish(row, "linked", STOP["unknown_course"], na)
    shown = ctx.shown.get(c.get("key"))
    if shown is None and c.get("key") in ctx.merged_into:   # 같은 점수의 Canvas 성적부 후보를 따라간다(추정에는 한 번만)
        shown = ctx.merged_into[c["key"]]
        row["notes"].insert(0, f"Canvas 성적부 '{scrub(str(shown.get('label') or ''), ctx.sid)}'와 같은 점수라 하나로 셌어요"
                               "(추정에는 한 번만 들어가요)")
    if shown is None:
        return _finish(row, "linked", STOP["not_listed"], na)
    comps = ctx.members.get(cid) or {}
    if not comps:
        return _finish(row, "linked", STOP["no_components"], na)
    comp, by = shown.get("component"), shown.get("link_by")
    row["link_by"] = by
    if not comp:
        return _finish(row, "linked", STOP["user_excluded"] if by == "user" else STOP["unlinked"], na)
    row["linked"] = scrub(str(comp), ctx.sid)
    cfg = comps.get(comp) or {}
    mems = [m for m in cfg.get("members") or [] if isinstance(m, dict)]
    if not any(m.get("key") == shown.get("key") for m in mems):
        lab = re.sub(r"\s+", "", str(shown.get("label") or shown.get("source_title") or "")).lower()
        same = any(re.sub(r"\s+", "", str(m.get("label") or "")).lower() == lab for m in mems)
        return _finish(row, "estimate", STOP["superseded"] if same else STOP["not_member"], na)
    if cfg.get("fixed"):
        return _finish(row, "estimate", STOP["fixed"].format(comp=row["linked"]), na)
    if course.get("pass_fail") or course_mode(ctx.store.data, cid) == "pass_fail":
        return _finish(row, "estimate", STOP["pass_fail"], na)
    if not course.get("estimate"):
        err = course.get("error")
        return _finish(row, "estimate", STOP["no_estimate"] + (f": {scrub(str(err), ctx.sid)[:120]}" if err else ""), na)
    row["in_estimate"] = True
    return _finish(row, None, None, na)


def _record_rows(base: dict, rkey: str, rec: dict, ctx: _Ctx, zip_name: str | None) -> list[dict]:
    row = dict(base, notes=list(base["notes"]))
    fn = str(rec.get("filename") or base["file"] or "")
    kind = _rec_kind(rec, fn)
    ks = str(rec.get("kind") or "")
    inner = rec.get("member") or (ks[4:] if ks.startswith("zip:") else None)   # zip 속 파일 이름(표는 kind "sheet")
    if inner:
        inner = str(inner)
        row["file"] = scrub(f"{rec.get('zip_name') or zip_name or fn} › {inner}", ctx.sid)
        kind = _kind_of_name(inner) if _kind_of_name(inner) != "other" else ("sheet" if _is_sheet_rec(rec) else "zip")
    row["kind"] = kind
    row["id"] = "f:" + rkey
    row["downloaded"], row["read"] = True, "ok"
    info = _analyze(rkey, rec, ctx.sid)
    row["score_table"] = info["score_table"]
    named = _scoreish(fn) or _scoreish(inner) or _scoreish(base.get("post_title"))
    row["relevant"] = bool(info["score_table"] or kind == "sheet" or named or base.get("source") == "upload")
    try:
        row["ai"] = aiscores.entry_status(ctx.store, rkey, rec, ctx.ai_on)
    except Exception:  # noqa: BLE001 — AI 칸은 덤(진단 줄은 그대로)
        row["ai"] = dict(_AI_NONE)
    cands = ctx.by_rec.get(rkey) or []
    ai_cands = ctx.ai_by_rec.get(rkey) or []
    if ai_cands and not any(c.get("score") is not None for c in cands):   # 규칙이 못 읽은 것을 AI가 찾음
        row["my_row"] = True
        row["relevant"] = True
        out = []
        for c in ai_cands:
            r = _after_candidate(dict(row, notes=list(row["notes"])), c, ctx)
            r["notes"] = [AI_NOTE["found"]] + [n for n in r["notes"] if n != AI_NOTE["found"]][:3]
            r["found_by"] = "ai"
            out.append(r)
        return out
    if cands:
        row["my_row"] = True
        return [_after_candidate(dict(row, notes=list(row["notes"])), c, ctx) for c in cands]
    if not info["score_table"]:
        return [_finish(row, "score_table", STOP["not_table_sheet"] if _is_sheet_rec(rec) else STOP["not_table"])]
    if not ctx.sid:
        return [_finish(row, "my_row", STOP["no_sid"])]
    if info["exact"] or info["masked"] == 1:
        row["my_row"] = True
        return [_finish(row, "candidate", STOP["no_result"])]
    row["my_row"] = False
    if info["masked"] > 1:
        return [_finish(row, "my_row", STOP["masked_many"])]
    return [_finish(row, "my_row", STOP["name_only"] if not info["shape"] else STOP["no_row"])]


def _unit_rows(u: dict, ctx: _Ctx) -> list[dict]:
    base = _base(u, ctx)
    named = _scoreish(base["file"]) or _scoreish(base.get("post_title")) or _ext(base["file"]) in SHEET_EXTS
    dl = _download_state(u, ctx)
    base["downloaded"] = dl
    if dl is not True:
        base["relevant"] = named
        stop = DOWNLOAD_STOP.get(dl, DOWNLOAD_STOP["pending"])
        if dl == "size":
            stop = stop.format(limit=f"{ctx.max_bytes / 1_000_000:g}MB")
        return [_finish(base, "downloaded", stop)]
    if not u["recs"]:
        code, notes = _read_failure(u)
        base["read"] = code
        base["notes"] = [scrub(n, ctx.sid) for n in notes[:2]]
        base["relevant"] = named
        stop = READ_STOP.get(code, READ_STOP["unreadable"])
        if code == "unsupported":
            stop = stop.format(ext=_ext(base["file"]) or "확장자 없음")
        elif code == "unreadable" and notes:
            stop = f"{stop}: {scrub(notes[0], ctx.sid)[:120]}"
        return [_finish(base, "read", stop)]
    if u.get("dl") and not base["material_exists"] and u["dl"].get("path"):
        base["notes"].append("받아 둔 파일이 지워졌어요(읽은 내용은 남아 있어요) — 다음 동기화 때 다시 받아요")
    rows = []
    for rkey, rec in u["recs"]:
        rows += _record_rows(base, rkey, rec, ctx, base["file"])
    return rows + _zip_member_rows(u, base, ctx)


def _zip_member_rows(u: dict, base: dict, ctx: _Ctx) -> list[dict]:
    """일부만 읽은 zip에서 못 읽은 안 파일(받은 기록의 zip_members — ok가 아니고 글 기록이 없는 것)도 한 줄씩."""
    out = []
    for zm in (u.get("dl") or {}).get("zip_members") or []:
        if not isinstance(zm, dict) or zm.get("ok") or zm.get("key"):
            continue
        name = str(zm.get("name") or "파일")
        notes = _notes_of(zm, "notes")
        ext = _ext(name)
        code = "image" if ext in IMAGE_EXTS else _classify(notes, "unsupported" if not notes else "unreadable")
        row = dict(base, notes=[scrub(n, ctx.sid) for n in notes[:2]], id=f"f:{u['key']}/{name}",
                   file=scrub(f"{base['file']} › {name}", ctx.sid), kind=_kind_of_name(name), read=code,
                   relevant=_scoreish(name) or ext in SHEET_EXTS or _scoreish(base.get("post_title")))
        stop = READ_STOP.get(code, READ_STOP["unreadable"])
        if code == "unsupported":
            stop = stop.format(ext=ext or "확장자 없음")
        elif code == "unreadable" and notes:
            stop = f"{stop}: {scrub(notes[0], ctx.sid)[:120]}"
        out.append(_finish(row, "read", stop))
    return out


def _canvas_rows(store, ctx: _Ctx) -> list[dict]:
    """Canvas 성적부 점수(과제의 submission.score) → canvas_grade 후보(계약 27-1)."""
    out = []
    for a in (store.data.get("cache") or {}).get("assignments") or []:
        if not isinstance(a, dict) or a.get("grading_type") == "not_graded":
            continue   # 채점하지 않는 과제·면제(excused)는 후보가 없는 게 맞다(실패로 보이지 않게, gradebook과 같은 규칙)
        sub = a.get("submission") if isinstance(a.get("submission"), dict) else {}
        score = sub.get("score")
        if score is None or isinstance(score, bool) or not isinstance(score, (int, float, str)) or sub.get("excused"):
            continue
        try:
            float(score)
        except (TypeError, ValueError):
            continue
        cid = str(a.get("course_id")) if a.get("course_id") is not None else None
        key = f"canvas:{a.get('id')}"
        row = {"id": "c:" + str(a.get("id")), "course_id": cid, "course_short": ctx.short(cid),
               "course_hidden": bool(cid) and cid in ctx.hidden,
               "file": scrub(" ".join(str(a.get("name") or "과제").split()), ctx.sid), "kind": "canvas_grade",
               "source": "canvas_grade", "post_title": None,
               "posted_at": sub.get("graded_at") or a.get("due_at") or a.get("updated_at"),
               "url": _clean_url(a.get("html_url")), "material_key": None, "material_exists": False,
               "found": True, "downloaded": None, "read": None, "score_table": None, "my_row": None,
               "candidate": None, "linked": None, "link_by": None, "in_estimate": False,
               "stage": None, "stop": None, "notes": [], "relevant": True}
        c = ctx.shown.get(key)
        if c is None:   # 성적 화면 후보에 없음(제외한 과목은 성적 화면에서 빠진다)
            out.append(_finish(row, "candidate", STOP["hidden"] if cid in ctx.hidden else STOP["no_canvas_cand"],
                               _FILE_ONLY))
            continue
        out.append(_after_candidate(row, c, ctx, _FILE_ONLY))
    return out


_NOT_FOUND = (re.compile(r"^(?P<what>공지|과제) '(?P<t>.*)'의 첨부를 확인하지 못했습니다(?P<e>.*)$", re.S),
              re.compile(r"^'(?P<t>.*)' 강의계획서의 첨부를 확인하지 못했습니다(?P<e>.*)$", re.S))


def _not_found_rows(store, ctx: _Ctx) -> list[dict]:
    """Canvas가 첨부 정보를 주지 않은 글(동기화 기록 cache.errors) — 파일 이름도 모른다."""
    cache = store.data.get("cache") or {}
    ann = {str(n.get("title") or ""): str(n.get("context_code") or "").replace("course_", "")
           for n in cache.get("announcements") or []}
    asg = {" ".join(str(a.get("name") or "").split()): str(a.get("course_id")) for a in cache.get("assignments") or []}
    crs = {str(c.get("name") or ""): str(c.get("id")) for c in cache.get("courses") or []}
    out, seen = [], set()
    for e in cache.get("errors") or []:
        s = str(e or "")
        for i, rx in enumerate(_NOT_FOUND):
            m = rx.match(s)
            if not m:
                continue
            title = m.group("t")
            if i == 1:
                src, cid = "syllabus", crs.get(title)
                title = "강의계획서"
            elif m.group("what") == "공지":
                src, cid = "announcement", ann.get(title)
            else:
                src, cid = "assignment", asg.get(title)
            if (src, cid, title) in seen:
                break
            seen.add((src, cid, title))
            closed = re.search(r"\((?:401|403)\)", m.group("e") or "")
            row = {"id": f"n:{src}:{cid}:{len(out)}", "course_id": cid, "course_short": ctx.short(cid),
                   "course_hidden": bool(cid) and cid in ctx.hidden, "file": "첨부 파일(이름 모름)", "kind": "other",
                   "source": src, "post_title": scrub(title, ctx.sid), "posted_at": None, "url": None,
                   "material_key": None, "material_exists": False,
                   "found": False, "downloaded": None, "read": None, "score_table": None, "my_row": None,
                   "candidate": None, "linked": None, "link_by": None, "in_estimate": False,
                   "stage": None, "stop": None, "notes": ["Canvas가 권한 없음(401·403)으로 답했어요"] if closed else [],
                   "relevant": _scoreish(title)}
            out.append(_finish(row, "found", STOP["not_found"]))
            break
    return out


# ---------------------------------------------------------------- 공개 함수

def _section(store) -> dict:
    from .core import course_summaries
    return gradebook.section(store, course_summaries((store.data.get("cache") or {})))


def _collect(store, grades: dict | None) -> list[dict]:
    """모든 줄(제외한 과목·관계없어 보이는 것까지, "relevant" 표시와 함께), 과목 순 → 최근 글 먼저."""
    if grades is None:
        grades = _section(store)
    ctx = _Ctx(store, grades)
    rows: list[dict] = []
    for u in _units(store):
        rows += _unit_rows(u, ctx)
    rows += _canvas_rows(store, ctx)
    rows += _not_found_rows(store, ctx)
    for r in rows:   # AI 칸(계약 29-2): 모든 줄에, 멈춘 줄에는 AI가 할 수 있는 것·한 것을 한 줄
        ai = r.get("ai")
        if not isinstance(ai, dict):
            r["ai"] = ai = dict(_AI_NONE)
        st = ai.get("status")
        if not r.get("stage") or r.get("found_by") == "ai" or st not in AI_NOTE:
            continue
        if st == "pending" and not ctx.ai_on:
            continue
        note = AI_NOTE[st] + (f": {scrub(str(ai['note']), ctx.sid)[:120]}" if st in ("none", "error") and ai.get("note")
                              else "")
        if note not in r["notes"]:
            r["notes"] = [note] + list(r["notes"])[:3]
        if st in ("pending", "none", "error"):
            r["relevant"] = True

    def order(r):
        return (ctx.order.get(str(r.get("course_id")), 10_000), r["kind"] == "canvas_grade",
                -_ts(r.get("posted_at")), str(r.get("file") or ""))
    rows.sort(key=order)
    return rows


def _pick(rows: list[dict], show_all: bool) -> tuple[list[dict], int]:
    """(보여 줄 줄, 기본 진단에서 뺀 제외한 과목의 줄 수). show_all이 아니면 관계있어 보이고 제외하지 않은 과목만."""
    out, hidden = [], 0
    for r in rows:
        if r["course_hidden"] and r["relevant"]:
            hidden += 1
        if not show_all and (not r["relevant"] or r["course_hidden"]):
            continue
        x = {k: v for k, v in r.items() if k != "relevant"}
        if x["course_hidden"]:
            x["notes"] = ["제외한 과목이에요(성적 화면·추정에서 빠져요)"] + list(x["notes"])
        out.append(x)
    return out, (0 if show_all else hidden)


def trace(store, grades: dict | None = None, show_all: bool = False) -> list[dict]:
    """성적이 될 수 있는 파일·출처마다 한 줄(한 파일에 평가가 여럿이면 평가마다). grades = gradebook.section 결과(없으면 만든다).

    줄: {"id", "course_id", "course_short", "course_hidden", "file", "kind"("pdf"|"sheet"|"doc"|"zip"|"image"|"other"|"canvas_grade"),
         "source"("announcement"|"assignment"|"syllabus"|"board"|"canvas_grade"|"upload"(직접 가져온 파일)|"unknown"), "post_title", "posted_at",
         "url"(글 주소 — 쿼리 없음, 첨부 주소 아님), "material_key"(받은 파일 key — 자료 열기), "material_exists",
         "found": bool, "downloaded": True | "hidden"|"off"|"size"|"error"|"save"|"outside"|"missing"|"pending" | None,
         "read": "ok" | "scanned"|"image"|"encrypted"|"unsupported"|"unreadable"|"pending" | None,
         "score_table": bool | None, "my_row": bool | None(학번 없음·단계 전),
         "candidate": {"key", "label", "score", "max_score", "mean", "sd", "n"} | None,
         "linked": 평가 항목 이름 | None, "link_by": "rules"|"ai"|"user"|None, "in_estimate": bool,
         "stage": 처음 멈춘 단계 id | None(추정까지 다 됨), "stop": 그 단계의 한국어 한 줄 | None,
         "stages": [{"id", "label", "state": "done"|"stop"|"todo"|"na"}], "notes": [한국어 메모…],
         "ai": {"status": "found"|"none"|"error"|"pending"|None, "results": [{key, label, score, max_score, mean, sd,
                confidence, evidence(화면용 — 학번은 가린 모양)}], "at", "note"}(계약 29-2), "found_by": "ai"(AI가 찾은 후보의 줄)}
    None 값은 '그 단계까지 가지 않음'(또는 Canvas 성적부 점수처럼 해당 없음 — stages의 "na").
    show_all=False면 성적과 관계있어 보이는 것만(점수 낱말 이름·점수표 글·표 파일·후보가 있는 것·Canvas 성적부 점수)과
    제외하지 않은 과목만. show_all이면 받은 모든 첨부와 제외한 과목(메모 '제외한 과목이에요')까지."""
    return _pick(_collect(store, grades), show_all)[0]


def _ts(v) -> float:
    from .timeutil import parse_iso
    d = parse_iso(v) if isinstance(v, str) else None
    try:
        return d.timestamp() if d else 0.0
    except (OverflowError, OSError, ValueError):
        return 0.0


def summary(rows: list[dict], hidden: int = 0) -> dict:
    """{"total", "in_estimate", "stopped": {단계 id: 수}, "hidden_skipped"}."""
    stopped: dict = {}
    for r in rows:
        if r.get("stage"):
            stopped[r["stage"]] = stopped.get(r["stage"], 0) + 1
    return {"total": len(rows), "in_estimate": sum(1 for r in rows if r.get("in_estimate")),
            "stopped": {s: stopped[s] for s, _ in STAGES if s in stopped}, "hidden_skipped": hidden}


def state_fields(store, grades: dict) -> dict:
    """state.grades에 더하는 칸: {"trace": [줄…], "trace_summary": {...}, "ai_lookup": aiscores.lookup_state(계약 29-2)}.
    무거운 것(글·표 살피기)은 글마다 기억하므로 /api/state마다 다시 만들어도 가볍다(점수표 판정·학번 찾기는 같은 글이면 다시 하지 않음).
    진단이 실패해도 ai_lookup은 남고, AI 상태가 실패해도 진단은 남는다(어느 쪽도 성적 화면을 깨지 않게)."""
    try:
        rows, hid = _pick(_collect(store, grades), False)
        out = {"trace": rows, "trace_summary": summary(rows, hid)}
    except Exception:  # noqa: BLE001 — features/grades의 대체 모양과 같게
        out = {"trace": [], "trace_summary": {"total": 0, "in_estimate": 0, "stopped": {}, "hidden_skipped": 0,
                                              "error": "성적 파일 진단을 만들지 못했어요"}}
    try:
        out["ai_lookup"] = aiscores.lookup_state(store)
    except Exception:  # noqa: BLE001
        out["ai_lookup"] = {"enabled": bool((store.data.get("settings") or {}).get("ai_cli")), "student_id_set": False,
                            "running": False, "job": None, "last_at": None, "done": 0, "found": 0, "pending": 0,
                            "errors": [], "per_sync": aiscores.AUTO_PER_SYNC}
    return out


# ---------------------------------------------------------------- 터미널 글(lms-agent grades-check)

_MARK = {"done": "O", "stop": "X", "todo": "-", "na": "/"}
_MY_VALUE = re.compile(r"내\s*(?:점수\s*)?[-+]?\d")   # '다른 열: Prelab 내 17 / 평균 16.4' 같은 내 점수 메모(--scores일 때만)


def _paste_safe(s, sid) -> str:
    """붙여 보내도 되는 글: 내 학번을 가리고, 7자리 넘는 숫자(학번·전화번호일 수 있음)도 가린다."""
    s = scrub(str(s or ""), sid)
    return re.sub(r"\d{7,}", lambda m: "#" * len(m.group()), s)


def _fmt(x) -> str:
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return "-"


def report_text(store, show_all: bool = False, show_scores: bool = False, version: str | None = None) -> str:
    """`lms-agent grades-check`가 찍는 글. 학번·다른 학생 정보·토큰·주소는 없다(점수는 --scores일 때만)."""
    rows, hid = _pick(_collect(store, None), show_all)
    sid = (store.data.get("settings") or {}).get("student_id")
    auto = bool((store.data.get("settings") or {}).get("auto_download", True))
    last = str(store.data.get("last_sync") or "")[:16].replace("T", " ") or "없음"
    out = [f"성적 파일 진단{f' (LMS Agent v{version})' if version else ''}",
           f"학번: {'넣음(내 줄 찾기에만 씀 — 여기에는 나오지 않아요)' if sid else '안 넣음'} · "
           f"첨부 자동 저장: {'켜짐' if auto else '꺼짐'} · 마지막 동기화: {last}",
           "표시: O 지나감 · X 여기서 멈춤 · - 아직 · / 해당 없음"]
    if not rows:
        out.append("")
        out.append("성적이 될 만한 파일·출처를 아직 찾지 못했어요. 동기화한 뒤 다시 실행해 보세요."
                   + ("" if show_all else " 모든 첨부를 보려면 --all"))
    else:
        head = f"파일·출처 {len(rows)}개"
        if hid:
            head += f" (제외한 과목의 {hid}개는 뺐어요 — 모두 보려면 --all)"
        elif not show_all:
            head += " (성적과 관계있어 보이는 것만 — 모두 보려면 --all)"
        out += ["", head]
    for i, r in enumerate(rows, 1):
        src = SOURCE_KO.get(r.get("source"), "출처")
        where = f" — {src} '{_paste_safe(r['post_title'], sid)}'" if r.get("post_title") else f" — {src}"
        out.append("")
        out.append(f"{i}. [{_paste_safe(r.get('course_short') or '과목 모름', sid)}] {_paste_safe(r.get('file'), sid)}{where}")
        out.append("   " + "  ".join(f"{s['label']} {_MARK[s['state']]}" for s in r.get("stages") or []))
        c = r.get("candidate")
        if c:
            if show_scores:
                bits = [f"{_fmt(c.get('score'))}" + (f"/{_fmt(c.get('max_score'))}" if c.get("max_score") else "")]
                if c.get("mean") is not None:
                    bits.append(f"평균 {_fmt(c.get('mean'))}")
                if c.get("sd") is not None:
                    bits.append(f"표준편차 {_fmt(c.get('sd'))}")
            else:
                have = [w for w, k in (("내 점수", "score"), ("평균", "mean"), ("표준편차", "sd")) if c.get(k) is not None]
                bits = [("·".join(have) + " 있음") if have else "값 없음"]
            if c.get("n"):
                bits.append(f"{c['n']}명")
            line = f"   후보: {_paste_safe(c.get('label') or '이름 없음', sid)} ({', '.join(bits)})"
            if r.get("linked"):
                by = {"rules": "규칙", "ai": "AI", "user": "직접"}.get(r.get("link_by"), "")
                line += f" → {_paste_safe(r['linked'], sid)}" + (f"({by})" if by else "")
            out.append(line)
        if r.get("stop"):
            out.append(f"   멈춤: {_paste_safe(r['stop'], sid)}")
        elif r.get("in_estimate"):
            out.append("   결과: 추정에 반영됐어요")
        notes = [n for n in r.get("notes") or [] if show_scores or not _MY_VALUE.search(str(n))]
        for n in notes[:2]:
            out.append(f"   메모: {_paste_safe(n, sid)}")
    if rows:
        s = summary(rows)
        parts = [f"{STAGE_LABEL[k]} {v}" for k, v in s["stopped"].items()]
        out += ["", f"요약: 추정에 반영 {s['in_estimate']}개"
                + (f" · 멈춤 {sum(s['stopped'].values())}개({' · '.join(parts)})" if parts else "")]
    out.append("이 글에는 학번·다른 학생 정보·토큰·주소가 들어 있지 않아요"
               + ("." if show_scores else "(내 점수도 숫자로 넣지 않았어요 — 넣으려면 --scores)."))
    return "\n".join(out)
