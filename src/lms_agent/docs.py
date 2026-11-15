"""문서 파일(워드 docx·파워포인트 pptx·한글 hwpx·hwp)과 zip 묶음 속 파일에서 글자를 꺼낸다 — 계약 27-2. 표준 라이브러리만 쓴다.

read(name, data) -> (글자, notes) | None — 못 읽었으면 None(이유는 read_detail의 notes).
read_detail(name, data, pdf_reader=None) -> {"kind", "ok", "text", "notes", "members"(zip), "sheets"·"sheet_kind"(표)}
  - docx: word/document.xml(+ 머리말·꼬리말) — 문단마다 줄, 표는 행마다 칸을 빈칸으로 이은 줄('2021123456 88').
  - pptx: 슬라이드 순서대로 '[슬라이드 N]' 줄 + 글 상자·표의 글(숨긴 슬라이드는 뺌, 발표자 메모는 읽지 않음).
  - hwpx: Contents/section*.xml(content.hpf의 순서) — 문단·표·글 상자.
  - hwp(한글 5.x): OLE2(sheets._Cfb를 함께 씀) → FileHeader(압축·암호·배포용) → BodyText/Section*(raw deflate) → 레코드
    (HWPTAG_PARA_TEXT = UTF-16 글자, 조판 부호는 건너뜀 — 표는 CTRL_HEADER 'tbl '·LIST_HEADER의 칸 주소로 행을 만든다).
  - zip: 안의 pdf·xlsx·xls·csv·tsv·docx·pptx·hwp·hwpx를 **메모리에서만** 읽는다(디스크에 풀지 않음, 안의 경로는 버리고 이름만).
    한도: 파일 MAX_MEMBERS개, 풀린 크기 합 MAX_UNPACKED, 압축률 MAX_RATIO배(RATIO_FLOOR 넘는 파일) — 넘는 것은 건너뛰고 notes.
    zip 안의 zip은 열지 않는다. 결과의 members = 파일마다 {"name", "kind", "ok", "text", "notes", …}(따로 저장하라고).
  - 표의 빈 칸은 '-'(같은 열에 값이 있는 표에서만) — 점수표 읽기(scorefile)가 칸 위치를 잃지 않게(결시처럼 빈 값으로 본다).
  - pdf·표 파일도 받는다(zip 안 파일용): pdf는 pdf_reader(없으면 pdftext), 표는 sheets.
못 읽는 것(이유만 notes에): 그림(png·jpg…), 스캔 PDF, 암호·배포용·DRM 문서, 옛 형식(.doc·.ppt·한글 97 이하), zip이 아닌 압축.
이유 글에는 파일 내용·경로를 넣지 않는다. 매크로·외부 연결·포함된 개체는 실행하거나 따라가지 않는다. 어떤 입력에도 예외를 내지 않는다.
"""
from __future__ import annotations

import io
import posixpath
import re
import struct
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter

from . import sheets as _sheets

VERSION = 1  # 읽는 규칙이 바뀌면 올린다(받아 둔 문서를 다음 동기화에서 다시 읽는다 — materials.DOC_READER)

MAX_FILE = 50 * 1024 * 1024        # 받은 파일 크기
MAX_UNPACKED = 30 * 1024 * 1024    # zip·docx·pptx·hwpx 안에서 풀어 읽는 총량, hwp 구역을 푼 총량
MAX_MEMBERS = 50                   # zip 안에서 보는 파일 수
MAX_RATIO = 200                    # 압축률(풀린 크기 / 압축된 크기) — 넘으면 압축 폭탄으로 보고 읽지 않는다
RATIO_FLOOR = 1024 * 1024          # 이보다 작은 파일은 압축률이 커도 읽는다(빈 칸 많은 XML)
MAX_SLIDES = 500
MAX_SECTIONS = 200                 # hwp·hwpx 구역
TEXT_LIMIT = 200_000               # 파일마다 돌려주는 글자 수
ZIP_SECONDS = 30.0                 # zip 하나를 읽는 시간(PDF가 많을 때) — 넘으면 나머지 파일은 건너뜀
ZIP_SHEET_CELLS = 50_000           # zip 안 표 파일마다 돌려주는 칸 수

R_UNSUPPORTED = "지원하지 않는 형식이라 글자를 읽지 않았어요"
R_IMAGE = "그림 파일이라 글자를 읽을 수 없어요"
R_SCAN = "스캔한 PDF로 보여요(글자 없음)"
R_PDF_LOCKED = "암호가 걸린 PDF라 글자를 읽지 못했어요"
R_PDF_BROKEN = "PDF 파일이 깨져 있어 글자를 읽지 못했어요"
R_FILE_BIG = "파일이 너무 커서(50MB 넘음) 읽지 않았어요"
R_BIG = "파일 안의 내용이 너무 커서(압축을 풀면 30MB 넘음) 읽지 않았어요"
R_NO_TEXT = "문서에서 글자를 찾지 못했어요(그림만 있는 문서일 수 있어요)"
R_DOCX_BROKEN = "워드(.docx) 파일이 깨져 있어 읽지 못했어요 — 워드에서 다시 저장하거나 PDF로 올려 달라고 부탁해 주세요"
R_PPTX_BROKEN = "파워포인트(.pptx) 파일이 깨져 있어 읽지 못했어요 — 다시 저장하거나 PDF로 올려 달라고 부탁해 주세요"
R_OFFICE_LOCKED = "암호가 걸린 오피스 문서라 읽지 못했어요 — 암호를 풀어 다시 올리거나 PDF로 올려 달라고 부탁해 주세요"
R_DOC_OLD = "옛 워드(.doc) 파일은 읽지 못해요 — 워드에서 .docx나 PDF로 저장해 다시 올려 달라고 부탁해 주세요"
R_PPT_OLD = "옛 파워포인트(.ppt) 파일은 읽지 못해요 — .pptx나 PDF로 저장해 다시 올려 달라고 부탁해 주세요"
R_HWP_LOCKED = "암호가 걸린 한글 파일이에요 — 암호를 풀어 다시 저장하거나 PDF로 올려 달라고 부탁해 주세요"
R_HWP_DIST = "배포용(읽기 전용) 한글 문서라 글자를 읽을 수 없어요 — PDF로 올려 달라고 부탁해 주세요"
R_HWP_OLD = "옛 한글 형식(한글 97 이하)이라 읽지 못했어요 — 한글에서 .hwpx나 PDF로 저장해 다시 올려 달라고 부탁해 주세요"
R_HWP_BROKEN = "한글(.hwp) 파일이 깨져 있어 읽지 못했어요 — 한글에서 다시 저장하거나 PDF로 올려 달라고 부탁해 주세요"
R_HWPX_BROKEN = ("한글(.hwpx) 파일이 깨져 있거나 암호·배포용이라 읽지 못했어요 — "
                 "한글에서 다시 저장하거나 PDF로 올려 달라고 부탁해 주세요")
R_ZIP_BROKEN = "zip 파일이 깨져 있어 열지 못했어요"
R_ZIP_LOCKED = "암호가 걸린 zip 파일이라 열지 못했어요"
R_ZIP_EMPTY = "zip 안에 읽을 수 있는 파일(PDF·엑셀·CSV·워드·파워포인트·한글)이 없어요"
R_ZIP_NONE = "zip 안 파일을 하나도 읽지 못했어요"
R_ZIP_NESTED = "zip 안의 zip은 열지 않았어요"
R_ARCHIVE = "zip이 아닌 압축 파일(7z·rar·egg·alz 등)은 열지 못해요 — zip으로 올려 달라고 부탁해 주세요"
R_MEMBER_LOCKED = "암호가 걸린 파일이라 읽지 못했어요"
R_MEMBER_BROKEN = "압축이 깨져 있어 꺼내지 못했어요"
R_BOMB = "압축률이 비정상적으로 커서(압축 폭탄 의심) 읽지 않았어요"
R_SLOW = "시간이 오래 걸려 읽지 않았어요"
R_SHEET = "표 파일을 읽지 못했어요"

TEXT_EXTS = (".docx", ".docm", ".pptx", ".pptm", ".hwpx", ".hwp", ".zip")   # 글자를 읽는 문서·묶음(받아 둔 파일 다시 읽기)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".jpe", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".svg")
ARCHIVE_EXTS = (".7z", ".rar", ".egg", ".alz", ".tar", ".gz", ".tgz", ".bz2", ".xz")
_SHEET_EXTS = (".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb", ".xls", ".xlt", ".csv", ".tsv")
_BY_EXT = {".pdf": "pdf", ".docx": "docx", ".docm": "docx", ".dotx": "docx", ".pptx": "pptx", ".pptm": "pptx",
           ".ppsx": "pptx", ".potx": "pptx", ".hwpx": "hwpx", ".hwp": "hwp", ".zip": "zip",
           ".doc": "doc", ".dot": "doc", ".ppt": "ppt", ".pps": "ppt", ".pot": "ppt"}
_ZIP_KINDS = ("pdf", "sheet", "docx", "pptx", "hwp", "hwpx")   # zip 안에서 읽는 것
_REASON_ONLY = {"image": R_IMAGE, "doc": R_DOC_OLD, "ppt": R_PPT_OLD, "hwp3": R_HWP_OLD,
                "office_locked": R_OFFICE_LOCKED, "archive": R_ARCHIVE}
_BROKEN = {"docx": R_DOCX_BROKEN, "pptx": R_PPTX_BROKEN, "hwpx": R_HWPX_BROKEN, "hwp": R_HWP_BROKEN}

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_HWP_SIG = b"HWP Document File"
_WORD_CT = re.compile(rb"(?:wordprocessingml\.(?:document|template)|ms-word\.(?:document|template)\.macroenabled)"
                      rb"(?:\.main)?\+xml", re.I)
_PPT_CT = re.compile(rb"(?:presentationml\.(?:presentation|slideshow|template)|"
                     rb"ms-powerpoint\.(?:presentation|slideshow|template)\.macroenabled)\.main\+xml", re.I)


class DocError(ValueError):
    """문서를 읽지 못한 이유(한국어). 파일 내용·경로는 넣지 않는다."""


class _TooBig(Exception):
    pass


# ---------------------------------------------------------------- 바깥 API

def read(name: str, data: bytes, pdf_reader=None) -> tuple[str, list[str]] | None:
    """(글자, notes) 또는 None(못 읽음 — 이유는 read_detail(...)["notes"]). zip이면 읽은 파일마다 '[파일 이름]' 줄 다음 글."""
    r = read_detail(name, data, pdf_reader)
    return (r["text"], list(r["notes"])) if r["ok"] else None


def read_detail(name: str, data: bytes, pdf_reader=None, _nested: bool = False) -> dict:
    """{"kind": kind_of 값 | None, "ok": bool, "text": str, "notes": [str]} (+ zip: "members", 표: "sheets"·"sheet_kind").
    못 읽으면 ok False, text "", notes 첫 줄이 이유. pdf_reader(data) → str | (text, notes) | {"text", "notes", "ok"?}."""
    name = str(name or "")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return _fail(None, R_UNSUPPORTED)
    data = bytes(data)
    kind = kind_of(name, data)
    if len(data) > MAX_FILE:
        return _fail(kind, R_FILE_BIG)
    if kind is None:
        return _fail(None, R_UNSUPPORTED)
    if kind in _REASON_ONLY:
        return _fail(kind, _REASON_ONLY[kind])
    if kind == "pdf":
        return _read_pdf(data, pdf_reader)
    if kind == "sheet":
        return _read_sheet(name, data)
    if kind == "zip":
        return _fail(kind, R_ZIP_NESTED) if _nested else _read_zip(data, pdf_reader)
    notes: list[str] = []
    try:
        lines = _READERS[kind](data, notes)
    except DocError as e:
        return _fail(kind, str(e), notes)
    except (_TooBig, _sheets._TooBig, MemoryError):
        return _fail(kind, R_BIG, notes)
    except Exception:  # noqa: BLE001 - 깨진 파일(XML·zip·레코드·재귀 깊이)도 앱을 멈추지 않는다
        return _fail(kind, _BROKEN[kind], notes)
    text = _join(lines, notes)
    if not text.strip():
        return _fail(kind, R_NO_TEXT, notes)
    return {"kind": kind, "ok": True, "text": text, "notes": notes}


def kind_by_name(name: str) -> str | None:
    """파일 이름(확장자)만으로: "pdf"|"sheet"|"docx"|"pptx"|"hwpx"|"hwp"|"zip"|"image"|"doc"|"ppt"|"archive"|None."""
    ext = _ext(name)
    if ext in _BY_EXT:
        return _BY_EXT[ext]
    if ext in _SHEET_EXTS:
        return "sheet"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return None


def kind_of(name: str, data: bytes) -> str | None:
    """내용(매직 바이트)을 먼저, 다음 이름. 읽는 것: "pdf"|"sheet"|"docx"|"pptx"|"hwpx"|"hwp"|"zip",
    이유만: "image"|"doc"|"ppt"|"hwp3"|"office_locked"|"archive". 모르는 것 None. 예외를 내지 않는다."""
    try:
        data = bytes(data or b"")
        if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
            return _zip_kind(name, data)
        if data[:8] == _OLE:
            return _ole_kind(name, data)
        if data[:1024].find(b"%PDF") >= 0:
            return "pdf"
        if data.startswith(_HWP_SIG):
            return "hwp3"          # OLE2가 아닌 한글 = 한글 97(3.x) 이하
        if _is_image(data):
            return "image"
        if _is_archive(data):
            return "archive"
        if _sheets.kind_of(data, name):
            return "sheet"
    except Exception:  # noqa: BLE001 - 판별만 한다
        pass
    return kind_by_name(name)


def pdf_notes(ok, notes) -> list[str]:
    """PDF 읽기 결과의 notes → 진단용: 못 읽었으면 첫 줄에 짧은 이유(스캔·암호·깨짐), 다음 원래 notes."""
    notes = [str(n) for n in (notes or [])]
    if ok:
        return notes
    joined = " ".join(notes)
    if "암호" in joined:
        reason = R_PDF_LOCKED
    elif any(w in joined for w in ("깨져", "아닙니다", "빈 파일", "쪽을 찾지", "읽지 못했습니다")):
        reason = R_PDF_BROKEN
    elif any(w in joined for w in ("너무 커서", "시간 제한")):
        return notes
    else:
        reason = R_SCAN
    return [reason] + [n for n in notes if n != reason]


# ---------------------------------------------------------------- 공통

def _fail(kind, reason: str, notes=()) -> dict:
    return {"kind": kind, "ok": False, "text": "", "notes": [reason] + [n for n in notes if n != reason]}


def _ext(name: str) -> str:
    base = re.split(r"[\\/]", str(name or ""))[-1].strip().lower()
    i = base.rfind(".")
    return base[i:] if i >= 0 else ""


def _is_image(d: bytes) -> bool:
    return (d[:8] == b"\x89PNG\r\n\x1a\n" or d[:3] == b"\xff\xd8\xff" or d[:6] in (b"GIF87a", b"GIF89a")
            or d[:4] in (b"II*\x00", b"MM\x00*") or (d[:4] == b"RIFF" and d[8:12] == b"WEBP")
            or (d[4:8] == b"ftyp" and d[8:12] in (b"heic", b"heix", b"mif1", b"heif", b"avif")))


def _is_archive(d: bytes) -> bool:
    return (d[:6] in (b"7z\xbc\xaf\x27\x1c", b"\xfd7zXZ\x00") or d[:6] == b"Rar!\x1a\x07" or d[:2] == b"\x1f\x8b"
            or d[:4] in (b"EGGA", b"ALZ\x01"))


_WS = re.compile(r"[ \t\r\f\v  -​　]+")
_CTL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _lines(text: str) -> list[str]:
    out = []
    for ln in _CTL.sub("", text or "").split("\n"):
        ln = _WS.sub(" ", ln).strip()
        if ln:
            out.append(ln)
    return out


def _cell(texts) -> str:
    return " ".join(ln for t in texts for ln in _lines(t))


def _table_lines(rows) -> list[str]:
    """rows = [[(열, 차지하는 열 수, 글)…]…] → 행마다 칸을 빈칸으로 이은 줄. 빈 칸은 '-'(그 열에 값이 있는 표에서만 —
    칸 사이 여백용 빈 열은 빼고), 모든 칸이 빈 행은 뺀다. 합친 칸(여러 열)은 글만."""
    used = {c for row in rows for c, s, t in row if t and s <= 1}
    out = []
    for row in rows:
        if not any(t for _, _, t in row):
            continue
        out.append(" ".join(t if t else "-" for c, s, t in row if t or (s <= 1 and c in used)))
    return out


def _join(lines, notes: list) -> str:
    out, total = [], 0
    for ln in lines:
        if total + len(ln) + 1 > TEXT_LIMIT:
            notes.append(f"글이 길어 앞의 {TEXT_LIMIT:,}자만 읽었어요")
            break
        out.append(ln)
        total += len(ln) + 1
    return "\n".join(out)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(el, name: str, default=None):
    for k, v in el.attrib.items():
        if k == name or k.endswith("}" + name):
            return v
    return default


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _xml(blob: bytes):
    """XML 한 부분. DTD·엔티티는 거절(엔티티 폭탄 — 오피스·한글 문서는 쓰지 않는다)."""
    flat = blob.replace(b"\x00", b"") if b"\x00" in blob[:200] else blob
    if b"<!DOCTYPE" in flat or b"<!ENTITY" in flat:
        raise ValueError("dtd")
    return ET.fromstring(blob)


def _zread(z: zipfile.ZipFile, info: zipfile.ZipInfo, budget: list, locked: str = R_OFFICE_LOCKED) -> bytes:
    """부분 하나를 푼다. 남은 한도(budget[0])를 넘으면 _TooBig — 적힌 크기로 먼저 막고, 실제로 푼 양으로 한 번 더."""
    if info.flag_bits & 0x1:
        raise DocError(locked)
    if info.file_size > budget[0]:
        raise _TooBig()
    try:
        with z.open(info) as f:
            blob = f.read(budget[0] + 1)
    except RuntimeError:   # zip 항목 암호
        raise DocError(locked) from None
    if len(blob) > budget[0]:
        raise _TooBig()
    budget[0] -= len(blob)
    return blob


def _rels(z, parts: dict, budget: list, src: str) -> dict:
    """부분 src의 관계 {Id: (종류, 대상 부분 이름)} — 바깥 주소(TargetMode External)는 따라가지 않는다."""
    d, base = posixpath.split(src)
    info = parts.get(posixpath.join(d, "_rels", base + ".rels").lower())
    if info is None:
        return {}
    out = {}
    for r in _xml(_zread(z, info, budget)):
        if _local(r.tag) != "Relationship" or (r.get("TargetMode") or "").lower() == "external":
            continue
        out[r.get("Id")] = ((r.get("Type") or "").rsplit("/", 1)[-1].lower(), _sheets._target(src, r.get("Target") or ""))
    return out


def _main_part(z, parts, budget, default: str) -> str:
    main = next((p for t, p in _rels(z, parts, budget, "").values() if t == "officedocument"), "")
    return main if main.lower() in parts else default


# ---------------------------------------------------------------- 판별

def _zip_kind(name: str, data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = _sheets._zip_parts(z)
            if "word/document.xml" in parts:
                return "docx"
            if "ppt/presentation.xml" in parts:
                return "pptx"
            mt = parts.get("mimetype")
            if mt is not None and mt.file_size < 200 and not mt.flag_bits & 1:
                with z.open(mt) as f:
                    if f.read(200).strip().lower().startswith(b"application/hwp+zip"):
                        return "hwpx"
            if "contents/content.hpf" in parts or any(re.fullmatch(r"contents/section\d+\.xml", p) for p in parts):
                return "hwpx"
            if _sheets._zip_is_workbook(data):
                return "sheet"
            ct = parts.get("[content_types].xml")
            if ct is not None and ct.file_size < 1024 * 1024 and not ct.flag_bits & 1:
                with z.open(ct) as f:
                    blob = f.read(1024 * 1024)
                if _WORD_CT.search(blob):
                    return "docx"
                if _PPT_CT.search(blob):
                    return "pptx"
    except Exception:  # noqa: BLE001 - 깨진 zip: 이름으로
        pass
    k = kind_by_name(name)
    return k if k in ("docx", "pptx", "hwpx", "sheet") else "zip"


def _ole_kind(name: str, data: bytes) -> str | None:
    ext = _ext(name)
    try:
        tree = _ole_tree(_sheets._Cfb(data), data)
    except Exception:  # noqa: BLE001 - 깨진 OLE2: 이름으로
        tree = None
    if tree is not None:
        if "fileheader" in tree:
            return "hwp"
        if "workbook" in tree or "book" in tree:
            return "sheet"
        if "encryptedpackage" in tree or "encryptioninfo" in tree:
            return "sheet" if ext in _SHEET_EXTS else "office_locked"
        if "worddocument" in tree:
            return "doc"
        if "powerpoint document" in tree:
            return "ppt"
    k = kind_by_name(name)
    return k if k in ("hwp", "sheet", "doc", "ppt") else None


# ---------------------------------------------------------------- PDF·표(zip 안 파일용)

def _default_pdf(data: bytes) -> dict:
    from .pdftext import extract_text
    r = extract_text(data)
    return {"text": r.text, "notes": list(r.notes), "ok": r.ok}


def _read_pdf(data: bytes, pdf_reader) -> dict:
    try:
        res = (pdf_reader or _default_pdf)(data)
        ok = None
        if isinstance(res, dict):
            text, notes, ok = res.get("text"), res.get("notes"), res.get("ok")
        elif isinstance(res, (tuple, list)):
            text, notes = (list(res) + [None, None])[:2]
        else:
            text, notes = res, None
        text = text if isinstance(text, str) else ""
        notes = [str(n) for n in notes] if isinstance(notes, (list, tuple)) else ([str(notes)] if notes else [])
    except Exception:  # noqa: BLE001
        text, notes, ok = "", ["PDF를 읽지 못했습니다"], False
    ok = bool(text.strip()) if ok is None else bool(ok and text.strip())
    return {"kind": "pdf", "ok": ok, "text": text[:TEXT_LIMIT] if ok else "", "notes": pdf_notes(ok, notes)}


def _read_sheet(name: str, data: bytes) -> dict:
    try:
        got, notes = _sheets.read_with_notes(data, name)
        text = _sheets.to_text(got, TEXT_LIMIT)
    except ValueError as e:     # sheets의 한국어 이유(옛 형식·암호 등 — 파일 내용·경로 없음)
        return _fail("sheet", str(e)[:200] or R_SHEET)
    except Exception:  # noqa: BLE001
        return _fail("sheet", R_SHEET)
    ok = any(sh.get("rows") for sh in got if isinstance(sh, dict))
    out = {"kind": "sheet", "ok": ok, "text": text if ok else "", "notes": [str(n) for n in notes],
           "sheets": got if ok else [], "sheet_kind": _sheets.kind_of(data, name)}
    if not ok and not out["notes"]:
        out["notes"] = ["표 파일에서 칸을 찾지 못했어요(빈 파일)"]
    return out


# ---------------------------------------------------------------- docx (word/document.xml)

_W_SKIP = {"pPr", "rPr", "delText", "instrText", "del", "moveFrom", "Fallback", "sectPr", "tblPr", "trPr", "tcPr",
           "tblGrid", "fldData", "bookmarkStart", "bookmarkEnd", "proofErr", "lastRenderedPageBreak"}
_W_WRAP = {"sdt", "sdtContent", "customXml", "ins", "moveTo", "smartTag"}


def _read_docx(data: bytes, notes: list) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = _sheets._zip_parts(z)
        budget = [MAX_UNPACKED]
        main = _main_part(z, parts, budget, "word/document.xml")
        info = parts.get(main.lower())
        if info is None:
            raise DocError(R_DOCX_BROKEN)
        lines: list[str] = []
        _w_blocks(_xml(_zread(z, info, budget)), lines)
        extra: list[str] = []
        for t, p in _rels(z, parts, budget, main).values():   # 머리말·꼬리말(있으면 끝에, 같은 줄은 한 번)
            if t in ("header", "footer") and p.lower() in parts:
                try:
                    _w_blocks(_xml(_zread(z, parts[p.lower()], budget)), extra)
                except (ET.ParseError, ValueError):
                    continue
        seen = set(lines)
        lines += [ln for ln in dict.fromkeys(extra) if ln not in seen]
    return lines


def _w_blocks(el, out: list) -> None:
    for ch in el:
        n = _local(ch.tag)
        if n == "p":
            _w_para(ch, out)
        elif n == "tbl":
            out.extend(_table_lines(_w_rows(ch)))
        elif n not in _W_SKIP:
            _w_blocks(ch, out)


def _w_para(p, out: list) -> None:
    buf: list = []
    extra: list = []
    _w_inline(p, buf, extra)
    out.extend(_lines("".join(buf)))
    for x in extra:   # 글 상자(txbxContent) 안의 문단·표는 그 뒤의 줄로
        if _local(x.tag) == "tbl":
            out.extend(_table_lines(_w_rows(x)))
        else:
            _w_blocks(x, out)


def _w_inline(el, buf: list, extra: list) -> None:
    for ch in el:
        n = _local(ch.tag)
        if n in _W_SKIP:
            continue
        if n == "t":
            buf.append(ch.text or "")
        elif n in ("tab", "ptab"):
            buf.append(" ")
        elif n in ("br", "cr"):
            buf.append("\n")
        elif n == "noBreakHyphen":
            buf.append("-")
        elif n in ("txbxContent", "tbl"):
            extra.append(ch)
        else:
            _w_inline(ch, buf, extra)


def _kids(el, name: str):
    """el의 자식 중 이름이 name인 것(내용 컨트롤·변경 추적 같은 감싸개 안까지)."""
    for ch in el:
        n = _local(ch.tag)
        if n == name:
            yield ch
        elif n in _W_WRAP:
            yield from _kids(ch, name)


def _w_rows(tbl) -> list:
    rows = []
    for tr in _kids(tbl, "tr"):
        trpr = next((x for x in tr if _local(x.tag) == "trPr"), None)
        before = next((x for x in (trpr if trpr is not None else []) if _local(x.tag) == "gridBefore"), None)
        col, row = (_int(_attr(before, "val"), 0) if before is not None else 0), []
        for tc in _kids(tr, "tc"):
            pr = next((x for x in tc if _local(x.tag) == "tcPr"), None)
            span, cont = 1, False
            for x in (pr if pr is not None else []):
                if _local(x.tag) == "gridSpan":
                    span = max(1, min(_int(_attr(x, "val"), 1), 1000))
                elif _local(x.tag) == "vMerge":
                    cont = (_attr(x, "val") or "continue") != "restart"   # 위 칸과 합쳐진 칸(빈 칸)
            lines: list = []
            _w_blocks(tc, lines)
            row.append((col, span, "" if cont else _cell(lines)))
            col += span
        rows.append(row)
    return rows


# ---------------------------------------------------------------- pptx (ppt/slides/slideN.xml)

def _read_pptx(data: bytes, notes: list) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = _sheets._zip_parts(z)
        budget = [MAX_UNPACKED]
        main = _main_part(z, parts, budget, "ppt/presentation.xml")
        order: list[str] = []
        if main.lower() in parts:
            pres = _xml(_zread(z, parts[main.lower()], budget))
            rels = _rels(z, parts, budget, main)
            lst = next((el for el in pres if _local(el.tag) == "sldIdLst"), None)
            for el in (lst if lst is not None else []):
                rid = next((v for k, v in el.attrib.items() if k.endswith("}id")), None)
                t, p = rels.get(rid, ("", ""))
                if t == "slide" and p.lower() in parts and p.lower() not in order:
                    order.append(p.lower())
        if not order:   # 목록이 없거나 깨졌으면 파일 이름 번호 순서
            nums = [(int(m.group(1)), p) for p in parts for m in [re.fullmatch(r"ppt/slides/slide(\d+)\.xml", p)] if m]
            order = [p for _, p in sorted(nums)]
        if not order:
            raise DocError(R_PPTX_BROKEN)
        lines: list[str] = []
        n = Counter()
        for i, p in enumerate(order, 1):
            if i > MAX_SLIDES:
                n["more"] = len(order) - MAX_SLIDES
                break
            try:
                root = _xml(_zread(z, parts[p], budget))
            except (ET.ParseError, ValueError):
                n["broken"] += 1
                continue
            if (root.get("show") or "").lower() in ("0", "false"):
                n["hidden"] += 1
                continue
            body: list = []
            _a_blocks(root, body)
            if body:
                lines.append(f"[슬라이드 {i}]")
                lines.extend(body)
    if n["broken"] and n["broken"] == min(len(order), MAX_SLIDES):
        raise DocError(R_PPTX_BROKEN)
    if n["hidden"]:
        notes.append(f"숨긴 슬라이드 {n['hidden']}개는 읽지 않았어요")
    if n["broken"]:
        notes.append(f"깨진 슬라이드 {n['broken']}개는 읽지 못했어요")
    if n["more"]:
        notes.append(f"슬라이드가 많아 앞의 {MAX_SLIDES}개만 읽었어요")
    return lines


def _a_blocks(el, out: list) -> None:
    for ch in el:
        n = _local(ch.tag)
        if n == "Fallback":
            continue
        if n == "p":
            buf: list = []
            _a_inline(ch, buf)
            out.extend(_lines("".join(buf)))
        elif n == "tbl":
            out.extend(_table_lines(_a_rows(ch)))
        else:
            _a_blocks(ch, out)


def _a_inline(el, buf: list) -> None:
    for ch in el:
        n = _local(ch.tag)
        if n in ("pPr", "rPr", "endParaRPr", "Fallback"):
            continue
        if n == "t":
            buf.append(ch.text or "")
        elif n == "br":
            buf.append("\n")
        else:
            _a_inline(ch, buf)


def _a_rows(tbl) -> list:
    rows = []
    for tr in (x for x in tbl if _local(x.tag) == "tr"):
        row, col = [], 0
        for tc in (x for x in tr if _local(x.tag) == "tc"):
            if (tc.get("hMerge") or "").lower() in ("1", "true"):   # 왼쪽 칸에 합쳐진 칸
                col += 1
                continue
            span = max(1, min(_int(tc.get("gridSpan"), 1), 1000))
            lines: list = []
            _a_blocks(tc, lines)
            cont = (tc.get("vMerge") or "").lower() in ("1", "true")
            row.append((col, span, "" if cont else _cell(lines)))
            col += span
        rows.append(row)
    return rows


# ---------------------------------------------------------------- hwpx (OWPML: Contents/section*.xml)

_HP_SKIP = {"secPr", "linesegarray", "Fallback"}


def _read_hwpx(data: bytes, notes: list) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = _sheets._zip_parts(z)
        budget = [MAX_UNPACKED]
        man = parts.get("meta-inf/manifest.xml")
        if man is not None and b"encryption-data" in _zread(z, man, budget, R_HWP_LOCKED):
            raise DocError(R_HWP_LOCKED)
        paths: list[str] = []
        hpf = parts.get("contents/content.hpf") or next((parts[p] for p in parts if p.endswith(".hpf")), None)
        if hpf is not None:
            try:
                root = _xml(_zread(z, hpf, budget, R_HWP_LOCKED))
                items = {el.get("id"): el.get("href") or "" for el in root.iter() if _local(el.tag) == "item"}
                base = posixpath.dirname(hpf.filename.replace("\\", "/"))
                for el in root.iter():
                    href = items.get(el.get("idref")) if _local(el.tag) == "itemref" else None
                    if not href or not re.search(r"section\d+\.xml$", href, re.I):
                        continue
                    for cand in (href.lstrip("/"), posixpath.join(base, href)):
                        cand = posixpath.normpath(cand).lower()
                        if cand in parts and cand not in paths:
                            paths.append(cand)
                            break
            except (ET.ParseError, ValueError):
                paths = []
        if not paths:
            nums = [(int(m.group(1)), p) for p in parts for m in [re.fullmatch(r"contents/section(\d+)\.xml", p)] if m]
            paths = [p for _, p in sorted(nums)]
        if not paths:
            raise DocError(R_HWPX_BROKEN)
        paths = paths[:MAX_SECTIONS]
        lines: list[str] = []
        broken = 0
        for p in paths:
            try:
                _hp_blocks(_xml(_zread(z, parts[p], budget, R_HWP_LOCKED)), lines)
            except (ET.ParseError, ValueError):
                broken += 1
    if broken == len(paths):
        raise DocError(R_HWPX_BROKEN)
    if broken:
        notes.append(f"깨진 구역 {broken}개는 읽지 못했어요")
    return lines


def _hp_blocks(el, out: list) -> None:
    for ch in el:
        n = _local(ch.tag)
        if n == "p":
            _hp_para(ch, out)
        elif n == "tbl":
            out.extend(_table_lines(_hp_rows(ch)))
        elif n not in _HP_SKIP:
            _hp_blocks(ch, out)


def _hp_para(p, out: list) -> None:
    buf: list = []
    extra: list = []
    _hp_inline(p, buf, extra)
    out.extend(_lines("".join(buf)))
    for x in extra:   # 문단 안의 표·글 상자·머리말 등(subList)은 그 뒤의 줄로
        if _local(x.tag) == "tbl":
            out.extend(_table_lines(_hp_rows(x)))
        else:
            _hp_blocks(x, out)


def _hp_inline(el, buf: list, extra: list) -> None:
    for ch in el:
        n = _local(ch.tag)
        if n == "t":
            buf.append(ch.text or "")
            for sub in ch:   # <hp:t>글<hp:tab/>글<hp:lineBreak/>…</hp:t>
                s = _local(sub.tag)
                if s in ("tab", "fwSpace", "nbSpace"):
                    buf.append(" ")
                elif s == "lineBreak":
                    buf.append("\n")
                elif s == "hyphen":
                    buf.append("-")
                buf.append(sub.tail or "")
        elif n in ("tbl", "subList"):
            extra.append(ch)
        elif n not in _HP_SKIP:
            _hp_inline(ch, buf, extra)


def _hp_rows(tbl) -> list:
    grid: dict = {}
    for r, tr in enumerate(x for x in tbl if _local(x.tag) == "tr"):
        col = 0
        for tc in (x for x in tr if _local(x.tag) == "tc"):
            addr = next((x for x in tc if _local(x.tag) == "cellAddr"), None)
            span_el = next((x for x in tc if _local(x.tag) == "cellSpan"), None)
            c = _int(addr.get("colAddr"), col) if addr is not None else col
            row = _int(addr.get("rowAddr"), r) if addr is not None else r
            span = max(1, min(_int(span_el.get("colSpan"), 1) if span_el is not None else 1, 1000))
            lines: list = []
            _hp_blocks(tc, lines)
            grid.setdefault(row, []).append((c, span, _cell(lines)))
            col = c + span
    return [sorted(cells, key=lambda x: x[0]) for _, cells in sorted(grid.items())]


# ---------------------------------------------------------------- hwp (한글 5.x: OLE2 + 레코드)

_TAG_PARA_TEXT, _TAG_CTRL_HEADER, _TAG_LIST_HEADER, _TAG_TABLE = 67, 71, 72, 77   # HWPTAG_BEGIN(16) + 51·55·56·61
_CTRL_TABLE = b" lbt"          # 조판 부호 id 'tbl '(4글자를 거꾸로 담은 UINT32)
_HWP_CHAR = frozenset((0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31))   # 1글자짜리 조판 부호 — 나머지(1~31)는 8글자(16바이트)
_SECTION = re.compile(r"bodytext/section(\d+)")
_LOCKED_BITS = 0x02 | 0x10 | 0x100 | 0x400     # 암호·DRM·공인 인증서 암호화·공인 인증서 DRM
_DIST_BIT = 0x04                               # 배포용 문서(본문은 ViewText에 암호화)


def _ole_tree(cfb, data: bytes) -> dict:
    """OLE2 디렉터리를 나무로 따라가 스트림마다 {"저장소/스트림"(소문자): sheets._Cfb.read가 받는 항목}."""
    major = struct.unpack_from("<H", data, 0x1A)[0]
    dir_start = struct.unpack_from("<I", data, 0x30)[0]
    blob = cfb._regular(dir_start, None)
    ents = []
    for off in range(0, min(len(blob), 128 * 50_000) - 127, 128):
        e = blob[off:off + 128]
        nlen = min(struct.unpack_from("<H", e, 64)[0], 64)
        left, right, child = struct.unpack_from("<III", e, 68)
        start, size = struct.unpack_from("<IQ", e, 116)
        if major == 3:
            size &= 0xFFFFFFFF   # v3는 아래 32비트만 쓴다
        ents.append((e[:max(0, nlen - 2)].decode("utf-16-le", "replace"), e[66], left, right, child, start, size))
    if not ents or ents[0][1] != 5:
        raise DocError(R_HWP_BROKEN)
    out: dict = {}
    seen = {0}
    todo = [(ents[0][4], "")]
    while todo:
        did, prefix = todo.pop()
        stack = [did]
        while stack:   # 형제는 빨강-검정 나무(왼쪽·오른쪽) — 순서는 상관없이 모두
            d = stack.pop()
            if d > _sheets._MAXREG or d >= len(ents) or d in seen:
                continue
            seen.add(d)
            name, typ, left, right, child, start, size = ents[d]
            stack += [left, right]
            path = prefix + name.lower()
            if typ == 2:
                out.setdefault(path, (name, 2, start, size))
            elif typ == 1 and prefix.count("/") < 8:
                todo.append((child, path + "/"))
    return out


def _read_hwp(data: bytes, notes: list) -> list[str]:
    try:
        cfb = _sheets._Cfb(data)
        tree = _ole_tree(cfb, data)
        fh = tree.get("fileheader")
        head = cfb.read(fh) if fh is not None else b""
    except (_sheets._TooBig, DocError):
        raise
    except Exception:  # noqa: BLE001 - sheets의 '엑셀' 이유 대신 한글 이유
        raise DocError(R_HWP_BROKEN) from None
    if len(head) < 40 or not head.startswith(_HWP_SIG):
        raise DocError(R_HWP_BROKEN)
    version, props = struct.unpack_from("<II", head, 32)
    if version >> 24 < 5:
        raise DocError(R_HWP_OLD)
    if props & _LOCKED_BITS:
        raise DocError(R_HWP_LOCKED)
    if props & _DIST_BIT:
        raise DocError(R_HWP_DIST)
    secs = sorted((int(m.group(1)), ent) for path, ent in tree.items() for m in [_SECTION.fullmatch(path)] if m)
    if not secs:
        raise DocError(R_HWP_DIST if any(p.startswith("viewtext/") for p in tree) else R_HWP_BROKEN)
    secs = secs[:MAX_SECTIONS]
    budget = [MAX_UNPACKED]
    lines: list[str] = []
    broken = 0
    for _, ent in secs:
        try:
            raw = cfb.read(ent)
            stream = _inflate(raw, budget) if props & 0x01 else _take(raw, budget)
            lines += _hwp_lines(stream)
        except (_TooBig, _sheets._TooBig):
            raise _TooBig() from None
        except Exception:  # noqa: BLE001 - 구역 하나가 깨져도 나머지는 읽는다
            broken += 1
    if broken == len(secs):
        raise DocError(R_HWP_BROKEN)
    if broken:
        notes.append(f"깨진 구역 {broken}개는 읽지 못했어요")
    return lines


def _take(raw: bytes, budget: list) -> bytes:
    if len(raw) > budget[0]:
        raise _TooBig()
    budget[0] -= len(raw)
    return raw


def _inflate(raw: bytes, budget: list) -> bytes:
    """구역 스트림 풀기(raw deflate, 머리글 있는 zlib도) — 한도(budget[0])를 넘게 풀지 않는다(압축 폭탄)."""
    for wbits in (-15, 15):
        d = zlib.decompressobj(wbits)
        try:
            out = d.decompress(raw, budget[0] + 1)
        except zlib.error:
            continue
        if len(out) > budget[0] or d.unconsumed_tail:
            raise _TooBig()
        budget[0] -= len(out)
        return out
    raise DocError(R_HWP_BROKEN)


def _hwp_records(b: bytes):
    """(tag, level, 본문) 차례로. 머리글 32비트 = tag 10비트 · level 10비트 · 크기 12비트(0xFFF면 다음 32비트가 크기)."""
    pos, n = 0, len(b)
    while pos + 4 <= n:
        h = struct.unpack_from("<I", b, pos)[0]
        pos += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, h >> 20
        if size == 0xFFF:
            if pos + 4 > n:
                return
            size = struct.unpack_from("<I", b, pos)[0]
            pos += 4
        yield tag, level, b[pos:pos + size]
        pos += size


def _hwp_text(body: bytes) -> str:
    """HWPTAG_PARA_TEXT → 글자. 탭은 빈칸, 줄 바꿈은 줄, 묶음·고정폭 빈칸은 빈칸, 하이픈은 '-', 그 밖의 조판 부호(표·그림·필드…)는 건너뜀."""
    n = len(body) // 2
    if not n:
        return ""
    codes = struct.unpack_from(f"<{n}H", body)
    out = []
    i = 0
    while i < n:
        c = codes[i]
        if c >= 32:
            out.append(c)
            i += 1
        elif c in _HWP_CHAR:
            if c == 10:
                out.append(10)
            elif c == 24:
                out.append(0x2D)
            elif c in (30, 31):
                out.append(0x20)
            i += 1
        else:
            if c == 9:
                out.append(0x20)
            i += 8
    return struct.pack(f"<{len(out)}H", *out).decode("utf-16-le", "replace") if out else ""


def _hwp_lines(stream: bytes) -> list[str]:
    """구역 하나 → 줄. 표(CTRL_HEADER 'tbl ' 아래의 TABLE·LIST_HEADER(칸)·문단)는 칸 주소(열·행)로 행을 만든다.
    표 안의 표는 바깥 칸의 글로 붙인다. 레벨이 표의 레벨 이하로 돌아오면 그 표가 끝난 것."""
    out: list[str] = []
    stack: list[dict] = []

    def close() -> None:
        t = stack.pop()
        grid: dict = {}
        for col, row, span, texts in t["cells"]:
            grid.setdefault(row, []).append((col, span, _cell(texts)))
        lines = _table_lines([sorted(cells, key=lambda x: x[0]) for _, cells in sorted(grid.items())])
        if stack and stack[-1]["cur"] is not None:
            stack[-1]["cur"][3].append(" ".join(lines))
        else:
            out.extend(lines)

    for tag, level, body in _hwp_records(stream):
        while stack and level <= stack[-1]["level"]:
            close()
        if tag == _TAG_CTRL_HEADER and body[:4] == _CTRL_TABLE and len(stack) < 8:
            stack.append({"level": level, "cells": [], "cur": None, "cols": 0, "table": False})
            continue
        if tag == _TAG_PARA_TEXT:
            text = _hwp_text(body)
            holder = next((t["cur"] for t in reversed(stack) if t["cur"] is not None), None)   # 표 안의 캡션은 바깥 칸에
            if holder is not None:
                holder[3].append(text)
            else:
                out.extend(_lines(text))
            continue
        if not stack:
            continue
        t = stack[-1]
        if tag == _TAG_TABLE and level == t["level"] + 1:
            t["table"] = True
            if len(body) >= 8:
                t["rows"], t["cols"] = struct.unpack_from("<HH", body, 4)
        elif tag == _TAG_LIST_HEADER and level == t["level"] + 1 and t["table"]:   # 표 앞의 LIST_HEADER는 캡션
            # 문단 리스트 헤더(문단 수 2·? 2·속성 4바이트) 다음 셀 속성: 열 주소·행 주소·열 병합 수(UINT16) …
            k, cols = len(t["cells"]), t["cols"]
            col, row, span = struct.unpack_from("<HHH", body, 8) if len(body) >= 14 else (-1, -1, 0)
            if span < 1 or col < 0 or (cols and col + span > cols) or (t.get("rows") and row >= t["rows"]):
                col, row, span = k % (cols or 1), k // (cols or 1), 1   # 주소를 못 믿으면 칸 순서대로
            cell = [col, row, max(1, span), []]
            t["cells"].append(cell)
            t["cur"] = cell
    while stack:
        close()
    return out


_READERS = {"docx": _read_docx, "pptx": _read_pptx, "hwpx": _read_hwpx, "hwp": _read_hwp}


# ---------------------------------------------------------------- zip (안의 파일을 메모리에서만)

_JUNK = (".ds_store", "thumbs.db", "desktop.ini")


def _member_name(info: zipfile.ZipInfo) -> str:
    """안의 이름(경로는 버림). UTF-8 표시가 없는 이름은 zipfile이 cp437로 풀어 둔 것 — UTF-8·CP949(한국어 윈도)로 다시."""
    raw = info.filename
    if not info.flag_bits & 0x800:
        try:
            b = raw.encode("cp437")
        except UnicodeEncodeError:
            b = None
        if b is not None:
            for enc in ("utf-8", "cp949"):
                try:
                    raw = b.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
    base = re.split(r"[\\/]", raw)[-1]
    return "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()[:120]


def _cap_sheets(shs: list, cap: int) -> list:
    """zip 안 표 파일의 칸을 앞에서부터 cap개까지만 남긴다(안 파일이 여럿일 때 메모리가 커지지 않게 — 저장은 materials가 더 줄임)."""
    out, left = [], cap
    for sh in shs:
        if left <= 0:
            break
        rows = []
        for row in sh.get("rows") or []:
            if left <= 0:
                break
            rows.append(row)
            left -= len(row)
        out.append({**sh, "rows": rows})
    return out


def _unique(name: str, taken: set) -> str:
    cand, n = name, 1
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    while cand.lower() in taken:
        n += 1
        cand = f"{stem} ({n})" + (f".{ext}" if ext else "")
    taken.add(cand.lower())
    return cand


def _read_zip(data: bytes, pdf_reader) -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except Exception:  # noqa: BLE001
        return _fail("zip", R_ZIP_BROKEN)
    members: list[dict] = []
    notes: list[str] = []
    with z:
        files = []
        for info in infos:
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                continue
            name = _member_name(info)
            if not name or "__macosx/" in info.filename.replace("\\", "/").lower() or name.startswith("._") \
                    or name.lower() in _JUNK:
                continue
            files.append((info, name))
        if not files:
            return {**_fail("zip", R_ZIP_EMPTY), "members": []}
        if all(info.flag_bits & 0x1 for info, _ in files):
            return {**_fail("zip", R_ZIP_LOCKED), "members": []}
        if len(files) > MAX_MEMBERS:
            notes.append(f"파일이 {len(files)}개라 앞의 {MAX_MEMBERS}개만 봤어요")
            files = files[:MAX_MEMBERS]
        budget = [MAX_UNPACKED]
        taken: set = set()
        deadline = time.monotonic() + ZIP_SECONDS
        for info, name in files:
            name = _unique(name, taken)
            k = kind_by_name(name)
            reason = None
            if k == "zip":
                reason = R_ZIP_NESTED
            elif k not in _ZIP_KINDS:
                reason = _REASON_ONLY.get(k, R_UNSUPPORTED)
            elif info.flag_bits & 0x1:
                reason = R_MEMBER_LOCKED
            elif info.file_size > budget[0]:
                reason = R_BIG
            elif info.file_size > RATIO_FLOOR and info.file_size > MAX_RATIO * max(1, info.compress_size):
                reason = R_BOMB
            elif time.monotonic() > deadline:
                reason = R_SLOW
            if reason is None:
                try:
                    with z.open(info) as f:
                        blob = f.read(budget[0] + 1)
                except Exception:  # noqa: BLE001 - 깨진 압축·CRC·암호
                    blob, reason = None, R_MEMBER_BROKEN
                if blob is not None and len(blob) > budget[0]:
                    blob, reason, budget[0] = None, R_BIG, 0    # 적힌 크기를 속인 파일 — 그 뒤도 읽지 않는다
                elif blob is not None and len(blob) > RATIO_FLOOR and len(blob) > MAX_RATIO * max(1, info.compress_size):
                    blob, reason = None, R_BOMB
            if reason is not None:
                members.append({"name": name, "kind": k, "ok": False, "text": "", "notes": [reason]})
                continue
            budget[0] -= len(blob)
            r = read_detail(name, blob, pdf_reader, _nested=True)
            blob = None
            if r.get("sheets"):
                r["sheets"] = _cap_sheets(r["sheets"], ZIP_SHEET_CELLS)
            members.append({"name": name, **r})
    ok = [m for m in members if m["ok"]]
    fails = Counter(m["notes"][0] for m in members if not m["ok"] and m["notes"])
    tail = [f"{why} — {n}개" for why, n in fails.most_common()]
    if not ok:
        first = R_ZIP_EMPTY if all(m["kind"] not in _ZIP_KINDS for m in members) else R_ZIP_NONE
        return {**_fail("zip", first, notes + tail), "members": members}
    notes.append(f"zip 안 파일 {len(members)}개 중 {len(ok)}개를 읽었어요")
    text_notes: list = []
    text = _join([f"[{m['name']}]\n{m['text']}" for m in ok], text_notes)
    return {"kind": "zip", "ok": True, "text": text, "notes": notes + tail + text_notes, "members": members}
