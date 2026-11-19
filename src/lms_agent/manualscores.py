"""직접 넣은 점수·성적 파일 가져오기(계약 29-1, 담당 S1). 경로는 features/manualscores.py.

직접 넣은 점수:
    store.data["manual_scores"][과목 id] = [{"id": "ms-<hex>", "label", "score", "max_score", "mean", "sd", "n", "rank",
                                              "high", "low", "component"(이름 | None), "note", "at", "by"("user"|"agent")}]
  - 점수 후보 공급자(gradebook.register_candidates): source_type "manual_score", source_title "직접 입력", key "manual:<id>",
    posted_at = at(고친 시각 — 같은 평가 항목·같은 이름의 파일 점수보다 나중이면 이것이 이긴다, gradebook.members_of).
  - component를 주면 그 평가 항목에 **사용자 연결**로(grade_links[과목]["manual:<id>"] — 화면의 '항목 고르기'와 같은 칸),
    없으면 이름 규칙(links.match)이 정한다. 지우면 사용자 연결도 지운다. 연결은 공통 되돌리기가 되돌린다(manual_scores·grade_links 칸).

성적 파일 가져오기:
  - POST /api/grades/<과목>/import {filename, data(base64)} → <데이터 폴더>/imports/<과목>/<해시12>-<안전한 이름>에 저장 →
    지금 읽기(PDF·문서·zip = docs.read_detail(materials._read_doc), 표 = materials.read_sheet_file) →
    pdf_texts["import:<과목>:<해시12>"](zip은 "<그 key>/<안 이름>", parent_key = 그 key) — 받은 첨부와 같은 모양
    (board_id "upload", source "upload", filename, course_id, posted_at = 가져온 시각)이라 점수표 해석·진단이 그대로 읽는다.
  - 가져온 기록 store.data["score_imports"][과목] = [{"key", "filename", "path", "size", "at", "kind", "ok", "notes", "records",
    "members"}] — 못 읽은 파일도 이유와 함께 남긴다(사용자가 지울 때까지). 같은 파일(같은 해시)을 다시 가져오면 다시 읽는다.
  - 지우기: pdf_texts의 그 기록(zip 속 파일 포함)·가져온 기록·디스크의 파일.
개인정보: 오류·메모에 파일 내용·경로를 넣지 않는다(파일 이름만).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
import os
import re
import secrets
from pathlib import Path

from . import gradebook
from .store import hidden_courses
from .timeutil import iso, now_local

SOURCE_TYPE = "manual_score"
SOURCE_TITLE = "직접 입력"
KEY_PREFIX = "manual:"
MAX_PER_COURSE = 200
MAX_LABEL = 60
MAX_NOTE = 200
BY = ("user", "agent")
NUM_FIELDS = (("score", "내 점수"), ("max_score", "만점"), ("mean", "평균"), ("sd", "표준편차"),
              ("high", "최고점"), ("low", "최저점"))
INT_FIELDS = (("n", "인원"), ("rank", "등수"))
FIELDS = ("label", "score", "max_score", "mean", "sd", "n", "rank", "high", "low", "component", "note")

IMPORT_MAX_BYTES = 20_000_000
IMPORT_EXTS = (".pdf", ".xlsx", ".xls", ".csv", ".tsv", ".docx", ".pptx", ".hwp", ".hwpx", ".zip")
SHEET_EXTS = (".xlsx", ".xls", ".csv", ".tsv")
IMPORT_DIR = "imports"
IMPORT_SOURCE = "upload"
IMPORT_PREFIX = "import:"
MAX_IMPORTS = 30
# base64 본문 상한(20MB 파일 → 약 26.7MB 글자) + JSON·파일 이름 여유. 서버(server.py)가 경로마다 본문 한도를 읽을 수 있으면 쓴다
IMPORT_BODY_MAX = 4 * math.ceil(IMPORT_MAX_BYTES / 3) + 64_000
_EXT_TEXT = "PDF·엑셀(xlsx·xls)·CSV·TSV·워드(docx)·파워포인트(pptx)·한글(hwp·hwpx)·zip"
_CID = re.compile(r"^[\w\-]{1,64}$")


# ================================================================ 공통

def _now() -> str:
    return iso(now_local())


def _courses(store) -> dict:
    return {str(c.get("id")): c for c in (store.data.get("cache") or {}).get("courses") or []
            if isinstance(c, dict) and c.get("id") is not None}


def _short(store, cid) -> str:
    from .core import short_course_name
    c = _courses(store).get(str(cid))
    return short_course_name((c or {}).get("name")) or str(cid)


def check_course(store, course_id) -> str:
    """성적 화면에 있는 과목 id(글자)만. 모르는 과목·제외한 과목은 한국어 ValueError."""
    cid = str(course_id or "").strip()
    if not _CID.match(cid) or cid not in _courses(store):
        raise ValueError("그 과목을 찾을 수 없어요(동기화한 뒤 다시 해 주세요)")
    if cid in hidden_courses(store.data):
        raise ValueError(f"'{_short(store, cid)}'은(는) 제외한 과목이라 성적 화면에 없어요 — 과목 종류를 먼저 바꿔 주세요")
    return cid


def _fmt(x) -> str:
    return f"{round(float(x), 2):g}"


def _number(v, what: str, lo: float = 0.0, positive: bool = False):
    """'9'·'9.5'·'87%'·'1,234'도 받는다. 빈 값은 None. 범위 밖·숫자 아님은 한국어 ValueError."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError(f"{what}은(는) 숫자여야 해요")
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        s = re.sub(r"\s*(?:점|%|pts?|points?)\s*$", "", s, flags=re.I)
        try:
            v = float(s)
        except ValueError:
            raise ValueError(f"{what}은(는) 숫자여야 해요: '{v.strip()[:20]}'") from None
    if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError(f"{what}은(는) 숫자여야 해요")
    x = float(v)
    if positive and x <= 0:
        raise ValueError(f"{what}은(는) 0보다 커야 해요")
    if x < lo:
        raise ValueError(f"{what}은(는) {lo:g} 이상이어야 해요")
    if x > 1_000_000:
        raise ValueError(f"{what}이(가) 너무 커요")
    return x


def _int(v, what: str, lo: int = 1, hi: int = 100_000):
    x = _number(v, what)
    if x is None:
        return None
    if abs(x - round(x)) > 1e-9:
        raise ValueError(f"{what}은(는) 정수여야 해요")
    x = int(round(x))
    if not lo <= x <= hi:
        raise ValueError(f"{what}은(는) {lo}~{hi} 사이여야 해요")
    return x


def _text(v, what: str, limit: int, required: bool = False) -> str | None:
    if v is None:
        s = ""
    elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
        s = re.sub(r"\s+", " ", str(v)).strip()
    else:
        raise ValueError(f"{what}은(는) 글자여야 해요")
    if not s:
        if required:
            raise ValueError(f"{what}을(를) 넣어 주세요")
        return None
    if len(s) > limit:
        raise ValueError(f"{what}은(는) {limit}자까지예요")
    return s


def component_names(store, cid) -> list[str]:
    comps = ((store.data.get("grades") or {}).get(str(cid)) or {}).get("components") or []
    return [str(c["name"]).strip() for c in comps if isinstance(c, dict) and str(c.get("name") or "").strip()]


def _component(store, cid, raw) -> str | None:
    """평가 항목 이름(대소문자·띄어쓰기 무시) → 저장된 이름. 빈 값은 None(이름 규칙이 정함)."""
    s = _text(raw, "평가 항목", 80)
    if s is None:
        return None
    names = component_names(store, cid)
    if not names:
        raise ValueError(f"{_short(store, cid)}에는 아직 평가 항목이 없어요 — 평가 항목을 먼저 넣거나 평가 항목을 비워 두세요")
    hit = gradebook._course_component(store, cid, s)
    if hit is None:
        raise ValueError(f"{_short(store, cid)}에 '{s[:40]}' 평가 항목이 없어요. 지금 평가 항목: {', '.join(names)}")
    return hit


# ================================================================ 직접 넣은 점수

def entries(store, cid) -> list[dict]:
    rows = (store.data.get("manual_scores") or {}).get(str(cid))
    return [e for e in rows if isinstance(e, dict) and e.get("id")] if isinstance(rows, list) else []


def candidate_key(entry_id: str) -> str:
    return KEY_PREFIX + str(entry_id)


def _validate(store, cid: str, row: dict) -> None:
    """채운 한 줄(row)의 값 사이 관계: 내 점수·평균·최고·최저 ≤ 만점(없으면 100), 최저 ≤ 최고, 등수 ≤ 인원."""
    mx = row.get("max_score")
    top = mx if mx is not None else 100.0
    for f, ko in (("score", "내 점수"), ("mean", "평균"), ("high", "최고점"), ("low", "최저점")):
        v = row.get(f)
        if v is not None and v > top + 1e-9:
            if mx is None:
                raise ValueError(f"{ko}({_fmt(v)})이(가) 100보다 커요 — 만점을 함께 넣어 주세요(만점이 없으면 100점 만점으로 봐요)")
            raise ValueError(f"{ko}({_fmt(v)})이(가) 만점({_fmt(mx)})보다 커요 — 만점을 확인해 주세요"
                             "(가산점이면 만점을 그만큼 올려 넣어 주세요)")
    if row.get("sd") is not None and row["sd"] > top + 1e-9:
        raise ValueError(f"표준편차({_fmt(row['sd'])})가 만점보다 커요 — 값을 확인해 주세요")
    if row.get("high") is not None and row.get("low") is not None and row["low"] > row["high"] + 1e-9:
        raise ValueError("최저점이 최고점보다 커요 — 값을 확인해 주세요")
    if row.get("rank") is not None and row.get("n") is not None and row["rank"] > row["n"]:
        raise ValueError(f"등수({row['rank']})가 인원({row['n']}명)보다 커요 — 값을 확인해 주세요")


def _fill(store, cid: str, body: dict, row: dict, partial: bool) -> dict:
    """본문의 칸을 검사해 row에 채운다(partial이면 본문에 있는 칸만 — null·빈 값은 지움)."""
    def given(k):
        return k in body if partial else True
    if given("label"):
        row["label"] = _text(body.get("label"), "점수 이름", MAX_LABEL, required=True)
    for f, ko in NUM_FIELDS:
        if given(f):
            row[f] = _number(body.get(f), ko, positive=(f == "max_score"))
    for f, ko in INT_FIELDS:
        if given(f):
            row[f] = _int(body.get(f), ko)
    if row.get("score") is None:
        raise ValueError("내 점수를 넣어 주세요")
    if given("component"):
        row["component"] = _component(store, cid, body.get("component"))
    if given("note"):
        row["note"] = _text(body.get("note"), "메모", MAX_NOTE)
    _validate(store, cid, row)
    return row


def _by(body: dict) -> str:
    by = body.get("by") or "user"
    if by not in BY:
        raise ValueError("by는 user 또는 agent여야 해요")
    return by


def _set_link(store, cid: str, entry_id: str, component: str | None) -> None:
    """직접 고른 평가 항목 = 사용자 연결(화면 '항목 고르기'와 같은 grade_links 칸). None이면 사용자 연결을 지워 이름 규칙으로."""
    links = store.data.setdefault("grade_links", {})
    key = candidate_key(entry_id)
    if component is None:
        slot = links.get(cid)
        if isinstance(slot, dict):
            slot.pop(key, None)
            if not slot:
                links.pop(cid, None)
        if not links:
            store.data.pop("grade_links", None)
        return
    links.setdefault(cid, {})[key] = {"component": component, "at": _now()}


def _find(store, cid: str, entry_id) -> dict:
    eid = str(entry_id or "").strip()
    if not eid:
        raise ValueError("고칠 점수의 id가 필요해요")
    e = next((x for x in entries(store, cid) if x["id"] == eid), None)
    if e is None:
        raise ValueError("그 점수를 찾을 수 없어요(이미 지웠을 수 있어요)")
    return e


def add(store, course_id, body: dict) -> dict:
    cid = check_course(store, course_id)
    rows = entries(store, cid)
    if len(rows) >= MAX_PER_COURSE:
        raise ValueError(f"직접 넣은 점수는 과목마다 {MAX_PER_COURSE}개까지예요 — 안 쓰는 점수를 지워 주세요")
    row = {"id": "ms-" + secrets.token_hex(5), "label": None, "score": None, "max_score": None, "mean": None, "sd": None,
           "n": None, "rank": None, "high": None, "low": None, "component": None, "note": None}
    _fill(store, cid, body, row, partial=False)
    row["at"], row["by"] = _now(), _by(body)
    store.data.setdefault("manual_scores", {})[cid] = rows + [row]
    if row["component"]:
        _set_link(store, cid, row["id"], row["component"])
    return row


def update(store, course_id, body: dict) -> dict:
    cid = check_course(store, course_id)
    old = _find(store, cid, body.get("id"))
    if not any(k in body for k in FIELDS):
        raise ValueError("바꿀 값이 없어요")
    row = _fill(store, cid, body, dict(old), partial=True)
    row["at"], row["by"] = _now(), _by(body)
    store.data["manual_scores"][cid] = [row if x["id"] == old["id"] else x for x in entries(store, cid)]
    if "component" in body:
        _set_link(store, cid, row["id"], row["component"])
    return row


def delete(store, course_id, body: dict) -> dict:
    cid = check_course(store, course_id)
    old = _find(store, cid, body.get("id"))
    rest = [x for x in entries(store, cid) if x["id"] != old["id"]]
    box = store.data.setdefault("manual_scores", {})
    if rest:
        box[cid] = rest
    else:
        box.pop(cid, None)
    if not box:
        store.data.pop("manual_scores", None)
    _set_link(store, cid, old["id"], None)
    return old


ACTIONS = {"add": add, "update": update, "delete": delete}


def apply(store, course_id, body: dict) -> dict:
    """POST /api/grades/<과목>/scores {"action": "add"|"update"|"delete", …} → 바꾼(지운) 점수 한 줄. 잘못되면 한국어 ValueError."""
    if not isinstance(body, dict):
        raise ValueError("본문이 올바르지 않아요")
    fn = ACTIONS.get(body.get("action"))
    if fn is None:
        raise ValueError("action은 add·update·delete 중 하나여야 해요")
    return fn(store, course_id, body)


def entry_text(e: dict) -> str:
    """'Lab 3 9/10' · 'Quiz 1 8점'."""
    s = f"{e.get('label') or '점수'} {_fmt(e['score'])}" if e.get("score") is not None else str(e.get("label") or "점수")
    if e.get("score") is not None:
        s += f"/{_fmt(e['max_score'])}" if e.get("max_score") is not None else "점"
    return s


def _sentence(e: dict) -> str:
    extra = []
    if e.get("mean") is not None:
        extra.append(f"평균 {_fmt(e['mean'])}")
    if e.get("sd") is not None:
        extra.append(f"표준편차 {_fmt(e['sd'])}")
    if e.get("rank") is not None:
        extra.append((f"{e['n']}명 중 " if e.get("n") else "") + f"{e['rank']}등")
    elif e.get("n") is not None:
        extra.append(f"{e['n']}명")
    return f"직접 입력: {entry_text(e)}" + (f" ({', '.join(extra)})" if extra else "")


def score_candidates(store, names: dict | None = None) -> list[dict]:
    """점수 후보 공급자(gradebook.CANDIDATE_PROVIDERS) — 직접 넣은 점수 하나마다 후보 하나."""
    from .core import short_course_name
    names = names if names is not None else {}
    by_str = {str(k): v for k, v in names.items()}
    out = []
    for cid, rows in sorted((store.data.get("manual_scores") or {}).items()):
        if not isinstance(rows, list):
            continue
        for e in rows:
            if not isinstance(e, dict) or not e.get("id") or e.get("score") is None:
                continue
            notes = [str(e["note"])] if e.get("note") else []
            if e.get("by") == "agent":
                notes.append("에이전트에게 말해서 넣은 점수예요")
            out.append({"course_id": str(cid), "course_short": short_course_name(by_str.get(str(cid))) or None,
                        "source_type": SOURCE_TYPE, "source_title": SOURCE_TITLE, "posted_at": e.get("at"), "url": None,
                        "label": e.get("label"), "score": e.get("score"), "max_score": e.get("max_score"),
                        "mean": e.get("mean"), "sd": e.get("sd"), "high": e.get("high"), "low": e.get("low"),
                        "n": e.get("n"), "rank": e.get("rank"), "sentence": _sentence(e), "notes": notes,
                        "key": candidate_key(e["id"]), "file_kind": None, "manual_id": e["id"], "by": e.get("by")})
    return out


gradebook.register_candidates(score_candidates)


# ================================================================ 성적 파일 가져오기

def imports_root(home) -> Path:
    return Path(home) / IMPORT_DIR


def safe_filename(name) -> str:
    """사용자가 고른 파일 이름 → 저장해도 되는 이름(폴더 부분·금지 문자·예약 이름·'..' 없음, 확장자는 남김)."""
    from .materials import file_name
    s = str(name or "").replace("\\", "/").split("/")[-1].replace("\x00", "")
    s = file_name(s, 80)
    return "_" if s in (".", "..") else s


def _ext(name: str) -> str:
    return os.path.splitext(str(name or ""))[1].lower()


def _decode(raw) -> bytes:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("파일 내용(data)이 비어 있어요")
    s = raw.strip()
    if s.startswith("data:"):   # FileReader.readAsDataURL 그대로 보냈을 때
        s = s.split(",", 1)[1] if "," in s else ""
    s = re.sub(r"\s+", "", s)
    if len(s) > IMPORT_BODY_MAX:
        raise ValueError(f"{IMPORT_MAX_BYTES // 1_000_000}MB보다 큰 파일은 가져올 수 없어요")
    try:
        data = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("파일 내용(base64)을 읽지 못했어요 — 다시 골라 주세요") from None
    if not data:
        raise ValueError("빈 파일이에요")
    if len(data) > IMPORT_MAX_BYTES:
        raise ValueError(f"{IMPORT_MAX_BYTES // 1_000_000}MB보다 큰 파일은 가져올 수 없어요")
    return data


def import_records(store, cid: str) -> list[dict]:
    rows = (store.data.get("score_imports") or {}).get(str(cid))
    return [r for r in rows if isinstance(r, dict) and r.get("key")] if isinstance(rows, list) else []


def _read(data: bytes, name: str, key: str, cid: str, at: str, pdf_reader=None) -> tuple[dict, dict]:
    """→ (읽기 결과 {"kind", "ok", "notes", "members"}, pdf_texts에 넣을 {key: 기록}). 받은 첨부 읽기(materials)와 같은 함수·모양."""
    from . import materials as mt
    job = {"key": key, "course_id": cid, "filename": name, "post_key": None, "board_id": IMPORT_SOURCE, "post": None,
           "canvas_file_id": None, "posted_at": at}
    members = []
    if _ext(name) in SHEET_EXTS:
        got = mt.read_sheet_file(data, name) or {"kind": None, "ok": False, "text": "", "sheets": [],
                                                 "notes": ["표 파일 형식이 아니어서 읽지 않았어요"]}
        recs = {key: mt._sheet_record(job, got)} if got.get("ok") else {}
        kind, ok, notes = "sheet", bool(got.get("ok")), list(got.get("notes") or [])
    else:
        got = mt._read_doc(data, name, pdf_reader)
        kind = got.get("kind")
        notes = [str(n) for n in got.get("notes") or []]
        if kind == "pdf":
            text = str(got.get("text") or "")
            recs = {key: {"text": text[:mt.PDF_TEXT_LIMIT], "course_id": cid, "filename": name, "post_key": None,
                          "board_id": IMPORT_SOURCE, "post": None, "canvas_file_id": None, "posted_at": at}} \
                if got.get("ok") and text.strip() else {}
            ver = _pdf_version()
            if ver is not None:
                for r in recs.values():
                    r["pdf_version"] = ver
        else:
            recs, extra = mt._doc_records(job, got)
            notes += [str(n) for n in extra]
        ok = bool(got.get("ok") and recs)
        if kind == "zip":
            keys = {r.get("member"): k for k, r in recs.items()}
            members = [{"name": str(m.get("name") or "")[:120], "ok": bool(m.get("ok")) and m.get("name") in keys,
                        "note": str((m.get("notes") or [""])[0] or "")[:200]}
                       for m in (got.get("members") or [])[:50] if isinstance(m, dict)]
    for r in recs.values():
        r.update(source=IMPORT_SOURCE, import_key=key, imported_at=at)
    if not ok and not notes:
        notes = ["파일에서 글자를 찾지 못했어요"]
    return {"kind": kind or _ext(name).lstrip("."), "ok": ok, "notes": [n[:200] for n in notes][:10],
            "members": members}, recs


def _pdf_version():
    try:
        from . import pdftext
    except ImportError:   # pragma: no cover
        return None
    v = getattr(pdftext, "VERSION", None)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _drop_texts(store, key: str) -> None:
    texts = store.data.get("pdf_texts")
    if not isinstance(texts, dict):
        return
    for k in [k for k, r in texts.items()
              if k == key or k.startswith(key + "/") or (isinstance(r, dict) and r.get("parent_key") == key)]:
        texts.pop(k, None)


def _too_many(store, cid: str, key: str) -> bool:
    return len([r for r in import_records(store, cid) if r["key"] != key]) >= MAX_IMPORTS


def _too_many_msg() -> str:
    return f"가져온 파일은 과목마다 {MAX_IMPORTS}개까지예요 — 안 쓰는 파일을 지워 주세요"


def prepare_import(store, home, course_id, body: dict, pdf_reader=None) -> dict:
    """가져오기 앞부분(저장소는 바꾸지 않음 — 잠금 밖에서): 검사 → 디스크에 저장 → 지금 읽기. 잘못되면 한국어 ValueError.
    → {"cid", "key", "rec"(가져온 기록), "recs"(pdf_texts에 넣을 것)} — commit_import에 넘긴다."""
    cid = check_course(store, course_id)
    if not isinstance(body, dict):
        raise ValueError("본문이 올바르지 않아요")
    raw_name = body.get("filename")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("파일 이름(filename)이 필요해요")
    name = safe_filename(raw_name.strip()[:255])
    ext = _ext(name)
    if ext not in IMPORT_EXTS:
        raise ValueError(f"이 형식({ext or '확장자 없음'})은 가져올 수 없어요 — {_EXT_TEXT} 파일만 돼요")
    data = _decode(body.get("data"))
    digest = hashlib.sha256(data).hexdigest()[:12]
    key = f"{IMPORT_PREFIX}{cid}:{digest}"
    if _too_many(store, cid, key):
        raise ValueError(_too_many_msg())
    root = imports_root(home)
    dest = root / cid / f"{digest}-{name}"
    from .materials import _write_atomic, inside
    if not inside(dest, root):
        raise ValueError("이 파일 이름으로는 저장할 수 없어요 — 이름을 바꿔 다시 골라 주세요")
    try:
        _write_atomic(dest, data)
    except OSError:
        raise ValueError("파일을 이 PC에 저장하지 못했어요(데이터 폴더 권한·남은 공간을 확인해 주세요)") from None
    at = _now()
    info, recs = _read(data, name, key, cid, at, pdf_reader)
    rec = {"key": key, "filename": name, "path": str(dest), "size": len(data), "at": at, "kind": info["kind"],
           "ok": info["ok"], "notes": info["notes"], "records": sorted(recs), "members": info["members"]}
    return {"cid": cid, "key": key, "rec": rec, "recs": recs}


def commit_import(store, prepared: dict) -> dict:
    """가져오기 뒷부분(잠금 안에서): pdf_texts·가져온 기록에 넣는다 → 가져온 기록 한 줄(경로 빼고). 저장은 부르는 쪽이."""
    cid, key, rec = prepared["cid"], prepared["key"], prepared["rec"]
    if _too_many(store, cid, key):   # 그 사이 다른 화면에서 더 가져왔다
        try:
            os.remove(rec["path"])
        except OSError:
            pass
        raise ValueError(_too_many_msg())
    _drop_texts(store, key)
    store.data.setdefault("pdf_texts", {}).update(prepared["recs"])
    old = [r for r in import_records(store, cid) if r["key"] != key]
    store.data.setdefault("score_imports", {})[cid] = old + [rec]
    return {k: v for k, v in rec.items() if k != "path"}


def import_file(store, home, course_id, body: dict, pdf_reader=None) -> dict:
    """가져오기 한 번에(prepare_import → commit_import). 저장(store.save)은 부르는 쪽이 한다."""
    return commit_import(store, prepare_import(store, home, course_id, body, pdf_reader))


def delete_import(store, home, course_id, body: dict) -> dict:
    """POST /api/grades/<과목>/import/delete {key}: 읽은 글(zip 속 파일 포함)·가져온 기록·디스크 파일을 지운다."""
    cid = check_course(store, course_id)
    key = str((body or {}).get("key") or "").strip()
    if not key.startswith(f"{IMPORT_PREFIX}{cid}:"):
        raise ValueError("지울 가져온 파일(key)을 알려 주세요")
    rows = import_records(store, cid)
    rec = next((r for r in rows if r["key"] == key), None)
    texts = store.data.get("pdf_texts") or {}
    if rec is None and key not in texts and not any(isinstance(r, dict) and r.get("parent_key") == key for r in texts.values()):
        raise ValueError("그 가져온 파일을 찾을 수 없어요(이미 지웠을 수 있어요)")
    _drop_texts(store, key)
    rest = [r for r in rows if r["key"] != key]
    box = store.data.setdefault("score_imports", {})
    if rest:
        box[cid] = rest
    else:
        box.pop(cid, None)
    if not box:
        store.data.pop("score_imports", None)
    path = (rec or {}).get("path")
    if path:
        from .materials import inside
        try:
            if inside(path, imports_root(home)) and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass   # 파일이 열려 있어도 기록은 지운다(다음에 다시 가져오면 덮어씀)
    return {k: v for k, v in (rec or {"key": key}).items() if k != "path"}


def reread_imports(store, home, keys=None, pdf_reader=None) -> int:
    """가져온 파일을 디스크에서 다시 읽는다(읽기 규칙이 바뀐 뒤 — 받아 둔 파일 다시 읽기가 부를 수 있다). keys가 없으면 전부.
    → 다시 읽은 수. 파일이 없거나 <데이터 폴더>/imports 밖이면 건너뛴다. 저장은 부르는 쪽이."""
    from .materials import inside
    root = imports_root(home)
    done = 0
    for cid in list((store.data.get("score_imports") or {})):
        for rec in import_records(store, cid):
            if keys is not None and rec["key"] not in keys:
                continue
            path = rec.get("path")
            try:
                if not path or not inside(path, root) or not os.path.isfile(path) or os.path.getsize(path) > IMPORT_MAX_BYTES:
                    continue
                data = Path(path).read_bytes()
            except OSError:
                continue
            info, recs = _read(data, rec.get("filename") or Path(path).name, rec["key"], str(cid), rec.get("at") or _now(),
                               pdf_reader)
            _drop_texts(store, rec["key"])
            store.data.setdefault("pdf_texts", {}).update(recs)
            rec.update(kind=info["kind"], ok=info["ok"], notes=info["notes"], records=sorted(recs), members=info["members"])
            done += 1
    return done


# ================================================================ 화면 상태(state.manual_scores)

def _is_table(rkey: str, rec: dict) -> bool:
    f = getattr(gradebook, "_is_score_rec", None)
    try:
        if callable(f):
            return bool(f(rkey, rec))
        from . import scorefile
        return bool(scorefile.is_score_file(rec.get("filename") or "", rec.get("text") or ""))
    except Exception:  # noqa: BLE001 — 상태 화면은 덤 하나로 깨지지 않게
        return False


def _import_status(store, rec: dict, cands: list[dict]) -> tuple[str, str]:
    """(상태, 한국어 한 줄): found | read_failed | no_sid | not_table | no_row."""
    if not rec.get("ok"):
        return "read_failed", str((rec.get("notes") or ["파일을 읽지 못했어요"])[0])
    if cands:
        return "found", f"점수 {len(cands)}개를 찾았어요"
    if not (store.data.get("settings") or {}).get("student_id"):
        return "no_sid", "학번을 넣으면 이 파일에서 내 줄을 찾아요"
    texts = store.data.get("pdf_texts") or {}
    tables = [k for k in rec.get("records") or [] if isinstance(texts.get(k), dict) and _is_table(k, texts[k])]
    if not tables:
        return "not_table", "점수표로 보이지 않아요(학번과 점수가 있는 줄을 찾지 못했어요) — 점수를 직접 넣거나 AI로 찾아 보세요"
    return "no_row", "점수표는 맞지만 내 학번 줄을 찾지 못했어요 — 학번이 맞는지 확인해 주세요"


def _cand_view(c: dict) -> dict:
    return {k: c.get(k) for k in ("key", "label", "score", "max_score", "mean", "sd", "n", "rank", "component",
                                  "link_by", "link_reason")}


def state(store, grades: dict | None) -> dict:
    """state.manual_scores = {"courses": {과목: [점수 + key·component(지금 연결)·link_by]}, "imports": {과목: [가져온 파일 +
    status·reason·candidates]}, "limits": {max_bytes, exts, max_imports, max_per_course}}. grades = state.grades 묶음(연결을 읽음)."""
    cands = [c for c in (grades or {}).get("candidates") or [] if isinstance(c, dict) and c.get("key")]
    by_key = {c["key"]: c for c in cands}
    courses = {}
    for cid in (store.data.get("manual_scores") or {}):
        out = []
        for e in entries(store, cid):
            c = by_key.get(candidate_key(e["id"])) or {}
            merged = next((x for x in cands for m in x.get("merged") or []
                           if isinstance(m, dict) and m.get("key") == candidate_key(e["id"])), None)
            out.append({**e, "key": candidate_key(e["id"]), "linked": (c or merged or {}).get("component"),
                        "link_by": (c or merged or {}).get("link_by"), "merged_into": (merged or {}).get("key")})
        if out:
            courses[str(cid)] = out
    imports = {}
    for cid in (store.data.get("score_imports") or {}):
        out = []
        for rec in import_records(store, cid):
            mine = [_cand_view(c) for c in cands if str(c["key"]).startswith("pdf:" + rec["key"])
                    and (len(c["key"]) == len("pdf:" + rec["key"]) or c["key"][len("pdf:" + rec["key"])] in "#/")]
            status, reason = _import_status(store, rec, mine)
            out.append({**{k: v for k, v in rec.items() if k != "path"}, "candidates": mine, "status": status,
                        "reason": reason})
        if out:
            imports[str(cid)] = out
    return {"courses": courses, "imports": imports,
            "limits": {"max_bytes": IMPORT_MAX_BYTES, "exts": list(IMPORT_EXTS), "max_imports": MAX_IMPORTS,
                       "max_per_course": MAX_PER_COURSE}}
