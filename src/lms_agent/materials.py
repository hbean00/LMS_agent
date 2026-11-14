"""첨부 자료 자동 저장: LearningX 게시판 글의 첨부(과제 PDF·강의 슬라이드)와 Canvas 공지·과제 설명·강의계획서에 링크된
파일(core._linked_files의 가상 게시판, 계약 24-3)을 내 PC 폴더에 받고, PDF는 글자를 뽑아 마감 찾기 출처(type "pdf")로 넘긴다.
표 파일(엑셀 xlsx·xls, CSV — sheets.kind_of)은 sheets.read로 칸을 읽어 같은 저장소(pdf_texts)에 "kind": "sheet" 기록으로 둔다
(글 = sheets.to_text, 표 = "sheets" — 성적 후보가 읽음, 계약 25-2).
계약 10장 "첨부 자료(materials) — B". 기록(downloads·pdf_texts)에는 어느 글에서 받았는지(board_id·post)와 canvas_file_id를 남긴다.

- plan(): 수집 결과(cache["boards"])에서 아직 안 받은 첨부만 고르고 안전한 저장 위치를 정한다(네트워크 없음).
  받아 둔 표 파일 중 아직 안 읽었거나 옛 규칙(sheet_version)으로 읽은 것은 디스크에서 다시 읽는 일("reread": True)로 더한다
  (한 번에 SHEET_REREAD_MAX개, 다운로드 폴더 안·크기 한도 안만) — v0.7.3 이전에 받아 둔 엑셀도 다음 동기화에서 읽힌다.
- fetch(): 실제로 받는다. 받는 함수(get_bytes)와 PDF 읽는 함수(pdf_reader)는 넘겨받는다(테스트는 가짜). 다시 읽기는 받지 않는다.
- apply()/run(): 결과를 state.json의 downloads·pdf_texts·materials_errors에 합친다.

안전 규칙: 모든 저장 위치는 다운로드 폴더 안이어야 한다(글 제목·파일 이름에 "..", 경로 구분자, Windows 예약 이름이
있어도 폴더 밖으로 나가지 않음). 사용자가 가진 파일은 덮어쓰지 않는다(이름이 겹치면 " (2)"). 쓰기는 .part 파일에
쓴 뒤 바꿔치기. 오류 문구에는 파일 이름만 넣는다(주소·verifier 값은 넣지 않는다).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import sheets as _sheets
from .learningx import safe_name
from .timeutil import iso, now_local, parse_iso

APP_FOLDER = "LMS Agent"
MAX_BYTES = 50_000_000            # 파일당 50MB
PDF_TEXT_LIMIT = 60_000           # pdf_texts에 남기는 글자 수
SHEET_VERSION = 1                 # 표 기록 만드는 판(이 모듈). 올리면 받아 둔 표 파일을 다음 동기화들에서 다시 읽는다
SHEET_READER = int(getattr(_sheets, "VERSION", 1) or 1)   # 표 읽기 판(sheets.VERSION) — 올라가도 다시 읽는다
SHEET_REREAD_MAX = 20             # 한 번의 동기화에서 디스크에서 다시 읽는 표 파일 수
SHEET_CELL_LIMIT = 20_000         # pdf_texts의 표 기록에 남기는 칸 수(파일마다 — state.json이 커지지 않게)
SHEET_CELL_CHARS = 200            # 칸마다 남기는 글자 수
SHEET_EXTS = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv")   # 받아 둔 파일 중 다시 읽어 볼 이름
# 칸마다 길이 제한. 그래도 폴더 경로가 길어 Windows 한도(파일 259자·폴더 247자)를 넘으면 dest_parts가 더 줄인다.
LIMITS = {"course": 40, "board": 30, "post": 50, "file": 80}
PATH_MAX, DIR_MAX = 259, 247
_SPARE = len(" (99)") + len(".part")   # 이름 겹침 번호와 임시 파일 꼬리
_FLOORS = (("post", 10), ("file", 20), ("board", 8), ("course", 8))   # 줄이는 순서와 최소 길이
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"


# ------------------------------------------------------------ 폴더

def _windows_documents() -> Path | None:
    """Windows의 '문서' 폴더(OneDrive로 옮겨졌어도 실제 위치). 못 찾으면 None."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(_FOLDERID_DOCUMENTS)
        guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except (OSError, AttributeError, ValueError):
        return None


def default_folder() -> Path:
    """~/Documents/LMS Agent (Windows는 사용자의 '문서' 폴더). 문서 폴더가 없으면 ~/LMS Agent."""
    docs = _windows_documents() if sys.platform == "win32" else None
    if docs is None or not docs.is_dir():
        docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / APP_FOLDER


def inside(path, folder) -> bool:
    """path가 (링크를 따라간 실제 위치로) folder 안에 있는지. folder 자체는 False."""
    try:
        p, root = Path(path).resolve(), Path(folder).resolve()
        p.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return False
    return p != root


# ------------------------------------------------------------ 이름

def _unreserved(s: str) -> str:
    """'con', 'NUL.txt', 'com1.pdf'처럼 Windows가 장치로 보는 이름이면 앞에 '_'."""
    return "_" + s if s.split(".")[0].rstrip(" ").upper() in _RESERVED else s


def part_name(name, limit: int) -> str:
    """폴더 한 칸의 이름. learningx.safe_name(금지 문자·끝의 점·공백) + 예약 이름."""
    return _unreserved(safe_name(name, limit))


def file_name(name, limit: int = LIMITS["file"]) -> str:
    """파일 이름: 길면 확장자는 남기고 앞부분을 줄인다."""
    s = safe_name(name, 100_000)
    stem, ext = os.path.splitext(s)
    if len(ext) > 12 or not ext[1:].isalnum():
        stem, ext = s, ""
    stem = stem[:max(1, limit - len(ext))].rstrip(". ") or "_"
    return _unreserved(stem + ext)


def dest_parts(folder: Path, course, board, post, filename) -> tuple[str, str, str, str]:
    """<과목>/<게시판>/<글>/<파일> 네 칸의 안전한 이름. 전체 경로가 길면 글 제목 → 파일 이름 → 게시판 → 과목 순으로 줄인다."""
    lim = dict(LIMITS)
    for _ in range(3):
        c, b, p = part_name(course, lim["course"]), part_name(board, lim["board"]), part_name(post, lim["post"])
        f = file_name(filename, lim["file"])
        d = len(str(folder)) + 3 + len(c) + len(b) + len(p)
        over = max(d - DIR_MAX, d + 1 + len(f) + _SPARE - PATH_MAX)
        if over <= 0:
            break
        for k, floor in _FLOORS:
            cur = len({"post": p, "file": f, "board": b, "course": c}[k])
            cut = min(over, max(0, cur - floor))
            lim[k], over = cur - cut, over - cut
            if over <= 0:
                break
    return c, b, p, f  # 폴더 자체가 너무 길면 줄여도 넘을 수 있다 → 저장할 때 오류로 알린다


def _numbered(path: Path, n: int) -> Path:
    stem, ext = os.path.splitext(path.name)
    return path.with_name(f"{stem} ({n}){ext}")


def _norm(path) -> str:
    """겹침 비교용. 대소문자를 가리지 않는 파일 시스템(Windows·macOS·USB)도 있으니 항상 대소문자 무시."""
    return os.path.normcase(str(path)).casefold()


def _free_path(path: Path, taken: set) -> Path:
    """이미 있는 파일이나 이번에 쓰기로 한 경로와 겹치지 않는 이름("Lab3 (2).pdf")."""
    cand, n = path, 1
    while _norm(cand) in taken or os.path.lexists(cand):
        n += 1
        cand = _numbered(path, n)
    return cand


# ------------------------------------------------------------ 계획

def key_of(course_id, attachment_id) -> str:
    return f"{course_id}:{attachment_id}"


def _posted_at(post: dict) -> str | None:
    raw = post.get("created_at") or post.get("updated_at")
    return iso(parse_iso(raw)) or raw


def _have(old: dict | None, size, folder: Path) -> bool:
    """같은 크기로 받아 둔 파일이 (지금 폴더 안에) 그대로 있나."""
    return bool(old and old.get("size") == size and old.get("path")
                and inside(old["path"], folder) and os.path.isfile(old["path"]))


def _file_id(cid: str, att: dict):
    """같은 파일인지 볼 기준: (과목, Canvas 파일 id). 없으면 None(그 첨부 key로만 가린다)."""
    fid = att.get("canvas_file_id")
    return (cid, str(fid)) if fid not in (None, "") else None


def plan(boards: list[dict], downloads: dict, short_names: dict, folder: Path,
         texts: dict | None = None) -> list[dict]:
    """받아야 할 첨부 목록. 같은 key·같은 크기이고 파일이 (지금 폴더 안에) 그대로 있으면 건너뛴다.
    파일을 지웠거나, 교수가 파일을 바꿨거나(크기 다름), 저장 폴더를 바꿨으면 다시 받는다.
    한 파일이 여러 글에 붙거나 링크돼 있으면(같은 key, 또는 같은 과목·같은 Canvas 파일 id — 예: 공지와 과제 설명에 같은 PDF)
    한 번만 받는다: 이미 받아 둔 쪽이 있으면 그것, 없으면 게시판 순서상 처음 글(LearningX → 공지 → 과제 → 강의계획서).
    끝에 받아 둔 표 파일 다시 읽기(reread_jobs, texts = pdf_texts를 주면 표 기록이 빠진 것도)를 더한다."""
    folder = Path(folder)
    taken: set = set()
    jobs: list[dict] = []
    planned: set = set()
    covered: set = set()      # 이미 받았거나 이번에 받기로 한 (과목, Canvas 파일 id)
    for board in boards or []:
        cid = str(board.get("course_id") or "")
        for post in board.get("posts") or []:
            for att in post.get("attachments") or []:
                f = _file_id(cid, att) if cid and att.get("id") is not None else None
                if f and _have(downloads.get(key_of(cid, att["id"])), att.get("filesize"), folder):
                    covered.add(f)
    for board in boards or []:
        cid = str(board.get("course_id") or "")
        board_id = board.get("board_id")
        board_title = board.get("board_title") or f"게시판 {board_id}"
        course_short = short_names.get(cid) or f"과목 {cid}"
        for post in board.get("posts") or []:
            post_title = post.get("title") or f"글 {post.get('id')}"
            for att in post.get("attachments") or []:
                if att.get("id") is None or not cid:
                    continue
                key = key_of(cid, att["id"])
                size = att.get("filesize")
                if key in planned or _have(downloads.get(key), size, folder):
                    continue
                f = _file_id(cid, att)
                if f in covered:
                    continue
                filename = str(att.get("filename") or att.get("display_name") or f"첨부 {att['id']}")
                dest = folder.joinpath(*dest_parts(folder, course_short, board_title, post_title, filename))
                if not inside(dest, folder):  # 이름을 걸렀으니 여기 오면 폴더 안 링크가 밖을 가리키는 경우
                    continue
                dest = _free_path(dest, taken)
                taken.add(_norm(dest))
                planned.add(key)
                if f:
                    covered.add(f)
                jobs.append({
                    "key": key, "dest": str(dest), "root": str(folder), "filename": filename,
                    "size": size if isinstance(size, int) else None,
                    "course_id": cid, "course_short": course_short, "board_title": board_title,
                    "post_title": post_title, "post_key": f"board:{board_id}-{post.get('id')}",
                    "board_id": board_id, "post": post.get("id"), "canvas_file_id": att.get("canvas_file_id"),
                    "posted_at": _posted_at(post), "attachment": att,
                })
    return jobs + reread_jobs(downloads, folder, skip=planned, texts=texts)


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _sheet_due(key: str, m: dict, texts: dict | None) -> bool:
    """받아 둔 파일을 표로 (다시) 읽어야 하나: 이름이 표 파일이고, 아직 안 읽었거나 옛 판으로 읽었거나,
    읽었다는데 pdf_texts에 표 기록이 없거나 옛 판(texts를 줄 때만)."""
    name = str(m.get("filename") or m.get("path") or "").lower()
    if not name.endswith(SHEET_EXTS):
        return False
    if _int(m.get("sheet_version")) < SHEET_VERSION or _int(m.get("sheet_reader")) < SHEET_READER:
        return True
    if texts is not None and m.get("sheet_ok"):
        rec = texts.get(key)
        return (not isinstance(rec, dict) or rec.get("kind") != "sheet" or _int(rec.get("sheet_version")) < SHEET_VERSION
                or _int(rec.get("sheet_reader")) < SHEET_READER)
    return False


def reread_jobs(downloads: dict, folder: Path, skip=(), texts: dict | None = None,
                limit: int = SHEET_REREAD_MAX) -> list[dict]:
    """받아 둔 표 파일을 디스크에서 다시 읽는 일(네트워크 없음): 최근 글의 파일부터 limit개. 파일이 지금 다운로드 폴더 안에
    그대로 있고 MAX_BYTES 이하일 때만. skip = 이번에 받기로 한 key(받으면서 읽는다)."""
    folder = Path(folder)
    out: list[dict] = []

    def newest(kv):
        m = kv[1]
        dt = parse_iso(m.get("posted_at")) or parse_iso(m.get("downloaded_at"))
        return -dt.timestamp() if dt else 0.0
    for key, m in sorted(((k, m) for k, m in (downloads or {}).items() if isinstance(m, dict)), key=newest):
        if len(out) >= limit:
            break
        if key in skip or not _sheet_due(key, m, texts):
            continue
        path = m.get("path")
        try:
            if not path or not inside(path, folder) or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
                continue
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append({"key": key, "reread": True, "dest": str(path), "root": str(folder),
                    "filename": str(m.get("filename") or Path(path).name), "size": size,
                    "course_id": str(m.get("course_id") or ""), "post_key": m.get("post_key"),
                    "board_id": m.get("board_id"), "post": m.get("post"), "canvas_file_id": m.get("canvas_file_id"),
                    "posted_at": m.get("posted_at"), "entry": dict(m)})
    return out


# ------------------------------------------------------------ 받기

def _read_pdf(pdf_reader: Callable, data: bytes) -> tuple[str, list[str], bool | None]:
    """pdf_reader(data)의 결과를 (글자, 메모, 성공 여부)로. 결과는 str, (text, notes), {"text", "notes", "ok"?} 중 하나."""
    res = pdf_reader(data)
    ok = None
    if isinstance(res, dict):
        text, notes, ok = res.get("text"), res.get("notes"), res.get("ok")
    elif isinstance(res, (tuple, list)):
        text, notes = (list(res) + [None, None])[:2]
    else:
        text, notes = res, None
    text = text if isinstance(text, str) else ""
    notes = [str(n) for n in (notes or [])] if isinstance(notes, (list, tuple)) else ([str(notes)] if notes else [])
    return text, notes, ok


def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with open(part, "wb") as f:
            f.write(data)
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def _limit_sheets(got: list) -> tuple[list[dict], list[str]]:
    """sheets.read 결과 → 저장할 표(칸 SHEET_CELL_LIMIT개·칸마다 SHEET_CELL_CHARS자까지, 열 번호는 JSON처럼 글자 "0")와 메모."""
    out, notes, left, cut = [], [], SHEET_CELL_LIMIT, False
    for sh in got or []:
        if not isinstance(sh, dict):
            continue
        extra = sh.get("notes")
        notes += [str(n)[:200] for n in (extra if isinstance(extra, (list, tuple)) else [extra] if extra else [])]
        rows = []
        for r in sh.get("rows") or []:
            if left <= 0:
                cut = True
                break
            row = {}
            for c, v in (r.items() if isinstance(r, dict) else []):
                if isinstance(v, str):
                    v = v[:SHEET_CELL_CHARS]
                elif isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                row[str(c)] = v
            left -= len(row)
            rows.append(row)
        out.append({"name": str(sh.get("name") or "")[:100], "rows": rows})
    if cut:
        notes.append(f"표가 커서 앞부분(칸 {SHEET_CELL_LIMIT:,}개)만 남겼습니다")
    return out, notes


def read_sheet_file(data: bytes, name: str) -> dict | None:
    """표 파일이면 {"kind", "ok", "notes", "text", "sheets"}, 아니면 None. 읽지 못한 이유는 notes(파일 내용·주소 없음)."""
    try:
        kind = _sheets.kind_of(data, name)
    except Exception:
        kind = None
    if kind is None:
        return None
    try:
        if hasattr(_sheets, "read_with_notes"):
            got, extra = _sheets.read_with_notes(data, name)   # 뺀 시트·자른 것·인코딩 메모(내용 없음)
        else:
            got, extra = _sheets.read(data, name), []
    except ValueError as e:     # sheets가 만든 한국어 이유(옛 형식·암호 등 — 파일 내용·경로 없음)
        return {"kind": kind, "ok": False, "notes": [str(e)[:200] or "표 파일을 읽지 못했습니다"], "text": "", "sheets": []}
    except Exception:
        return {"kind": kind, "ok": False, "notes": ["표 파일을 읽지 못했습니다"], "text": "", "sheets": []}
    stored, notes = _limit_sheets(got)
    notes = [str(n)[:200] for n in extra or []] + notes
    try:
        text = _sheets.to_text(got, PDF_TEXT_LIMIT)
    except Exception:
        text = ""
    ok = any(sh["rows"] for sh in stored)
    if not ok and not notes:
        notes = ["표 파일에서 칸을 찾지 못했습니다(빈 파일)"]
    return {"kind": kind, "ok": ok, "notes": notes, "text": (text or "")[:PDF_TEXT_LIMIT], "sheets": stored}


def _sheet_record(job: dict, got: dict) -> dict:
    """pdf_texts의 표 기록(PDF 기록과 같은 칸 + 표)."""
    return {"text": got["text"], "sheets": got["sheets"], "kind": "sheet", "sheet_kind": got["kind"],
            "sheet_version": SHEET_VERSION, "sheet_reader": SHEET_READER,
            "course_id": job.get("course_id"), "filename": job.get("filename"),
            "post_key": job.get("post_key"), "board_id": job.get("board_id"), "post": job.get("post"),
            "canvas_file_id": job.get("canvas_file_id"), "posted_at": job.get("posted_at")}


def _apply_sheet(entry: dict, got: dict, at: str) -> None:
    entry.update(sheet_ok=bool(got["ok"]), sheet_notes=list(got["notes"]), sheet_kind=got["kind"],
                 sheet_version=SHEET_VERSION, sheet_reader=SHEET_READER, sheet_read_at=at)


def _reread(job: dict, max_bytes: int, at: str) -> tuple[dict, dict | None] | None:
    """받아 둔 표 파일을 디스크에서 다시 읽는다 → (downloads 기록, 표 기록 | None). 파일이 없거나 폴더 밖·너무 크면 None(그대로 둠)."""
    path = Path(job["dest"])
    try:
        if not inside(path, job["root"]) or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    entry = dict(job.get("entry") or {})
    got = _sheet_or_marker(data, job.get("filename") or path.name)
    _apply_sheet(entry, got, at)
    return entry, (_sheet_record(job, got) if got["ok"] else None)


def _sheet_or_marker(data: bytes, name: str) -> dict | None:
    """read_sheet_file + 이름만 표 파일(.xlsx 등)인데 내용이 아닌 것은 '읽지 않음' 표시(다음 동기화에서 다시 보지 않게)."""
    got = read_sheet_file(data, name)
    if got is None and str(name or "").lower().endswith(SHEET_EXTS):
        got = {"kind": None, "ok": False, "notes": ["표 파일 형식이 아니어서 읽지 않았습니다"], "text": "", "sheets": []}
    return got


def fetch(jobs: list[dict], get_bytes: Callable, max_bytes: int = MAX_BYTES, pdf_reader: Callable | None = None,
          now: datetime | None = None) -> tuple[dict, dict, list[str]]:
    """jobs(plan 결과)를 받아 저장한다. 한 파일이 실패해도 나머지는 계속.
    PDF는 pdf_reader로 글자를, 표 파일(엑셀·CSV)은 sheets로 칸을 읽는다. 다시 읽기 일("reread")은 받지 않고 디스크에서 읽는다
    (downloads 기록은 그대로 두고 sheet_* 칸만 바뀜 — downloaded_at도 그대로).
    → (downloads에 합칠 것, pdf_texts에 합칠 것, 한국어 오류 목록)."""
    at = iso(now or now_local())
    limit_mb = f"{max_bytes / 1_000_000:g}MB"
    updates: dict = {}
    texts: dict = {}
    errors: list[str] = []
    taken: set = set()
    for job in jobs:
        if job.get("reread"):
            done = _reread(job, max_bytes, at)
            if done is not None:
                updates[job["key"]] = done[0]
                if done[1] is not None:
                    texts[job["key"]] = done[1]
            continue
        name = job.get("filename") or "첨부"
        if job.get("size") is not None and job["size"] > max_bytes:
            errors.append(f"'{name}': {limit_mb}보다 커서 받지 않았습니다")
            continue
        dest = Path(job["dest"])
        if not inside(dest, job["root"]):
            errors.append(f"'{name}': 저장 위치가 다운로드 폴더 밖이라 받지 않았습니다")
            continue
        try:
            data = get_bytes(job["attachment"])
        except Exception:  # 네트워크·권한·주소 만료 등. 원인 문구에는 주소가 섞일 수 있어 넣지 않는다
            errors.append(f"'{name}': 내려받지 못했습니다")
            continue
        if not isinstance(data, (bytes, bytearray)):
            errors.append(f"'{name}': 내려받지 못했습니다")
            continue
        if len(data) > max_bytes:
            errors.append(f"'{name}': {limit_mb}보다 커서 받지 않았습니다")
            continue
        try:
            if os.path.lexists(dest) or _norm(dest) in taken:  # 계획 뒤에 생긴 파일도 덮어쓰지 않는다
                dest = _free_path(dest, taken)
            if not inside(dest, job["root"]):
                raise OSError("outside")
            _write_atomic(dest, bytes(data))
        except Exception:
            errors.append(f"'{name}': 저장하지 못했습니다")
            continue
        taken.add(_norm(dest))
        place = {"board_id": job.get("board_id"), "post": job.get("post"), "canvas_file_id": job.get("canvas_file_id")}
        entry = {
            "path": str(dest), "filename": name,
            "size": job["size"] if job.get("size") is not None else len(data),
            "course_id": job["course_id"], "course_short": job.get("course_short"),
            "board_title": job["board_title"], "post_title": job["post_title"], "post_key": job["post_key"],
            **place, "posted_at": job.get("posted_at"), "downloaded_at": at, "pdf_ok": None, "pdf_notes": [],
        }
        is_pdf = name.lower().endswith(".pdf") or bytes(data[:5]) == b"%PDF-"
        if is_pdf and pdf_reader is not None:
            try:
                text, notes, ok = _read_pdf(pdf_reader, bytes(data))
            except Exception:
                text, notes, ok = "", ["PDF를 읽지 못했습니다"], False
            ok = bool(text.strip()) if ok is None else bool(ok and text.strip())
            if not ok and not notes:
                notes = ["PDF에서 글자를 찾지 못했습니다(스캔본일 수 있습니다)"]
            entry["pdf_ok"], entry["pdf_notes"] = ok, notes
            if ok:
                texts[job["key"]] = {"text": text[:PDF_TEXT_LIMIT], "course_id": job["course_id"], "filename": name,
                                     "post_key": job["post_key"], **place, "posted_at": job.get("posted_at")}
        elif not is_pdf:
            got = _sheet_or_marker(bytes(data), name)
            if got is not None:
                _apply_sheet(entry, got, at)
                if got["ok"]:
                    texts[job["key"]] = _sheet_record({**job, "filename": name}, got)
        updates[job["key"]] = entry
    return updates, texts, errors


# ------------------------------------------------------------ state.json에 합치기

def short_names_of(cache: dict | None) -> dict:
    from .core import short_course_name  # core가 이 모듈을 불러도 순환되지 않게 안에서
    return {str(c["id"]): short_course_name(c.get("name")) for c in (cache or {}).get("courses") or []}


def apply(data: dict, updates: dict, texts: dict, errors: list[str]) -> None:
    """fetch 결과를 store.data에 합친다. 다시 받은 PDF에서 글자를 못 찾았으면 예전 글자는 지운다."""
    downloads = data.setdefault("downloads", {})
    pdf_texts = data.setdefault("pdf_texts", {})
    for key, entry in updates.items():
        downloads[key] = entry
        if key not in texts:
            pdf_texts.pop(key, None)
    pdf_texts.update(texts)
    data["materials_errors"] = list(errors)


def run(data: dict, folder: Path, get_bytes: Callable, pdf_reader: Callable | None = None,
        max_bytes: int = MAX_BYTES, now: datetime | None = None) -> dict:
    """한 번에: cache["boards"] → plan(+ 받아 둔 표 파일 다시 읽기) → fetch → apply. 저장(store.save)은 부르는 쪽이 한다.
    보고: planned·downloaded = 이번에 내려받은 것, pdf_texts = 이번에 만든 글·표 기록, reread = 다시 읽은 표 파일(있을 때만)."""
    cache = data.get("cache") or {}
    jobs = plan(cache.get("boards") or [], data.get("downloads") or {}, short_names_of(cache), Path(folder),
                texts=data.get("pdf_texts") or {})
    updates, texts, errors = fetch(jobs, get_bytes, max_bytes=max_bytes, pdf_reader=pdf_reader, now=now)
    apply(data, updates, texts, errors)
    again = {j["key"] for j in jobs if j.get("reread")}
    report = {"planned": len(jobs) - len(again), "downloaded": len(set(updates) - again), "pdf_texts": len(texts),
              "errors": errors}
    if again & set(updates):
        report["reread"] = len(again & set(updates))
    return report
