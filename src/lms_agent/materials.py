"""첨부 자료 자동 저장: LearningX 게시판 글의 첨부(과제 PDF·강의 슬라이드)와 Canvas 공지·과제 설명·강의계획서에 링크된
파일(core._linked_files의 가상 게시판, 계약 24-3)을 내 PC 폴더에 받고, PDF는 글자를 뽑아 마감 찾기 출처(type "pdf")로 넘긴다.
계약 10장 "첨부 자료(materials) — B". 기록(downloads·pdf_texts)에는 어느 글에서 받았는지(board_id·post)와 canvas_file_id를 남긴다.

- plan(): 수집 결과(cache["boards"])에서 아직 안 받은 첨부만 고르고 안전한 저장 위치를 정한다(네트워크 없음).
- fetch(): 실제로 받는다. 받는 함수(get_bytes)와 PDF 읽는 함수(pdf_reader)는 넘겨받는다(테스트는 가짜).
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

from .learningx import safe_name
from .timeutil import iso, now_local, parse_iso

APP_FOLDER = "LMS Agent"
MAX_BYTES = 50_000_000            # 파일당 50MB
PDF_TEXT_LIMIT = 60_000           # pdf_texts에 남기는 글자 수
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


def plan(boards: list[dict], downloads: dict, short_names: dict, folder: Path) -> list[dict]:
    """받아야 할 첨부 목록. 같은 key·같은 크기이고 파일이 (지금 폴더 안에) 그대로 있으면 건너뛴다.
    파일을 지웠거나, 교수가 파일을 바꿨거나(크기 다름), 저장 폴더를 바꿨으면 다시 받는다.
    한 파일이 여러 글에 붙거나 링크돼 있으면(같은 key, 또는 같은 과목·같은 Canvas 파일 id — 예: 공지와 과제 설명에 같은 PDF)
    한 번만 받는다: 이미 받아 둔 쪽이 있으면 그것, 없으면 게시판 순서상 처음 글(LearningX → 공지 → 과제 → 강의계획서)."""
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
    return jobs


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


def fetch(jobs: list[dict], get_bytes: Callable, max_bytes: int = MAX_BYTES, pdf_reader: Callable | None = None,
          now: datetime | None = None) -> tuple[dict, dict, list[str]]:
    """jobs(plan 결과)를 받아 저장한다. 한 파일이 실패해도 나머지는 계속.
    → (downloads에 합칠 것, pdf_texts에 합칠 것, 한국어 오류 목록)."""
    at = iso(now or now_local())
    limit_mb = f"{max_bytes / 1_000_000:g}MB"
    updates: dict = {}
    texts: dict = {}
    errors: list[str] = []
    taken: set = set()
    for job in jobs:
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
    """한 번에: cache["boards"] → plan → fetch → apply. 저장(store.save)은 부르는 쪽이 한다."""
    cache = data.get("cache") or {}
    jobs = plan(cache.get("boards") or [], data.get("downloads") or {}, short_names_of(cache), Path(folder))
    updates, texts, errors = fetch(jobs, get_bytes, max_bytes=max_bytes, pdf_reader=pdf_reader, now=now)
    apply(data, updates, texts, errors)
    return {"planned": len(jobs), "downloaded": len(updates), "pdf_texts": len(texts), "errors": errors}
