"""표 파일(엑셀 xlsx·xlsm, 옛 엑셀 xls, CSV·TSV) 읽기 — 계약 25-1. 표준 라이브러리만 쓴다.

값만 읽는다: 수식은 파일에 저장된 결과만, 매크로·외부 연결·서식은 실행하거나 따라가지 않는다.

read(data, filename) -> [{"name": 시트 이름, "rows": [{열 번호(0부터): 글자 | 숫자}, …]}, …]
  - 보이는 워크시트만 통합 문서 순서대로(숨긴 시트·차트·매크로 시트·빈 시트는 뺌).
  - 행은 행 번호 순서, 빈 칸·빈 행은 없다. 글자는 앞뒤 공백을 지운다. 숫자는 유한한 float(날짜 서식도 숫자 그대로).
    CSV는 숫자처럼 보여도 글자 그대로 둔다(숫자 판단은 쓰는 쪽).
  - 한도(시트 20·시트당 행 5,000·열 200)를 넘어 잘린 시트에는 "truncated": True가 붙는다(안 잘렸으면 키 없음).
read_with_notes(data, filename) -> (같은 결과, notes): 뺀 시트·자른 것·CSV 인코딩 같은 설명(시트 이름·내용은 넣지 않음).
못 읽으면 SheetError(ValueError의 하위 클래스): 한국어 이유만 — 파일 내용·경로는 넣지 않는다.
"""
from __future__ import annotations

import csv
import io
import math
import posixpath
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from array import array
from collections import Counter

VERSION = 1  # 읽는 규칙이 바뀌면 올린다(받아 둔 표 파일을 다시 읽을지 판단하는 데 쓸 수 있음)

MAX_SHEETS = 20
MAX_ROWS = 5000          # 시트마다 (빈 행은 세지 않음)
MAX_COLS = 200
MAX_FILE = 100 * 1024 * 1024       # 받은 파일 크기
MAX_UNPACKED = 50 * 1024 * 1024    # xlsx: zip 안에서 풀어 읽는 총량(압축 폭탄 막기)
MAX_STREAM = 50 * 1024 * 1024      # xls: 'Workbook' 스트림 크기

E_NOT_SHEET = "표 파일(엑셀·CSV)이 아니라서 읽지 못했어요"
E_FILE_BIG = "표 파일이 너무 커서(100MB 넘음) 읽지 않았어요"
E_BIG = "엑셀 파일 안의 내용이 너무 커서(압축을 풀면 50MB 넘음) 읽지 않았어요"
E_XLSX = "엑셀(.xlsx) 파일이 깨져 있어 읽지 못했어요 — 엑셀에서 열어 다시 저장해 올려 달라고 부탁해 주세요"
E_XLS = "옛 엑셀(.xls) 파일이 깨져 있어 읽지 못했어요 — 엑셀에서 .xlsx로 저장해 다시 올려 달라고 부탁해 주세요"
E_NO_WB = "엑셀 파일 안에서 통합 문서(시트 목록)를 찾지 못했어요 — 엑셀에서 .xlsx로 저장해 다시 올려 달라고 부탁해 주세요"
E_OLD = "옛 형식(엑셀 95 이하)이라 읽지 못했어요 — 엑셀에서 .xlsx로 저장해 다시 올려 달라고 부탁해 주세요"
E_LOCKED = "암호가 걸린 엑셀 파일이라 읽지 못했어요 — 암호를 풀고 .xlsx로 저장해 다시 올려 달라고 부탁해 주세요"
E_XLSB = "엑셀 바이너리 통합 문서(.xlsb)는 읽지 못해요 — 엑셀에서 .xlsx로 저장해 다시 올려 달라고 부탁해 주세요"
E_HTML = ("엑셀 이름이지만 실제로는 웹 페이지(HTML)·XML 표라 읽지 못했어요 — "
          "엑셀에서 열어 .xlsx로 저장해 다시 올려 달라고 부탁해 주세요")
E_CSV = "CSV 파일을 읽지 못했어요(글자로 된 표가 아님)"

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_XLSX_EXT = (".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb")
_XLS_EXT = (".xls", ".xlt")
_CSV_EXT = (".csv", ".tsv")
_MAIN_CT = re.compile(rb"(?:spreadsheetml|ms-excel)\.(?:sheet|template)(?:\.binary)?(?:\.macroenabled)?\.main", re.I)


class SheetError(ValueError):
    """표 파일을 읽지 못한 이유(한국어). 파일 내용·경로는 넣지 않는다."""


class _TooBig(Exception):
    pass


class _Eof(Exception):
    pass


# 깨진 파일에서 나올 수 있는 예외 → SheetError로 바꾼다(앱이 멈추지 않게).
_BROKEN = (struct.error, IndexError, KeyError, ValueError, LookupError, OverflowError, EOFError, OSError,
           RuntimeError, NotImplementedError, RecursionError, SyntaxError, zlib.error, zipfile.BadZipFile,
           zipfile.LargeZipFile, csv.Error, _Eof)


# ---------------------------------------------------------------- 바깥 API

def kind_of(data: bytes, filename: str = "") -> str | None:
    """"xlsx" | "xls" | "csv" | None. 내용(매직 바이트)을 먼저 보고, 다음 파일 이름 확장자.
    docx·pptx 같은 다른 zip, 한글(hwp)·doc 같은 다른 OLE2 문서, PDF·그림은 None."""
    try:
        data = _bytes(data)
    except SheetError:
        return None
    ext = _ext(filename)
    if _is_zip(data):
        if _zip_is_workbook(data):
            return "xlsx"
        return "xlsx" if ext in _XLSX_EXT + _XLS_EXT else None
    if data[:8] == _OLE:
        names = _ole_names(data)
        if names & {"workbook", "book"}:
            return "xls"
        if "encryptedpackage" in names and ext in _XLSX_EXT:
            return "xlsx"  # 암호 걸린 xlsx(OLE2로 감싼 것) — read가 이유를 알려 준다
        if ext in _XLSX_EXT:
            return "xlsx"
        return "xls" if ext in _XLS_EXT else None
    if ext in _XLSX_EXT:
        return "xlsx"  # 이름만 엑셀(웹 페이지·글자 표 등) — read가 읽거나 이유를 알려 준다
    if ext in _XLS_EXT:
        return "xls"
    if ext in _CSV_EXT and _looks_text(data):
        return "csv"
    return None


def read(data: bytes, filename: str = "") -> list[dict]:
    return read_with_notes(data, filename)[0]


def read_with_notes(data: bytes, filename: str = "") -> tuple[list[dict], list[str]]:
    data = _bytes(data)
    if len(data) > MAX_FILE:
        raise SheetError(E_FILE_BIG)
    kind = kind_of(data, filename)
    if kind is None:
        raise SheetError(E_NOT_SHEET)
    notes: list[str] = []
    if _is_zip(data):
        broken, fn = E_XLSX, lambda: _read_xlsx(data, notes)
    elif data[:8] == _OLE:
        broken, fn = E_XLS, lambda: _read_xls(data, notes)
    elif kind == "csv":
        broken, fn = E_CSV, lambda: _read_csv(data, filename, notes, prefer_tab=_ext(filename) == ".tsv")
    else:
        broken, fn = (E_XLSX if kind == "xlsx" else E_XLS), lambda: _read_disguised(data, filename, notes, kind)
    try:
        sheets = fn()
    except SheetError:
        raise
    except (_TooBig, MemoryError):
        raise SheetError(E_BIG) from None
    except _BROKEN:
        raise SheetError(broken) from None
    if not sheets:
        notes.append("읽을 내용이 있는 시트가 없음")
    return sheets, notes


def to_text(sheets: list[dict], limit: int = 60000) -> str:
    """검색·마감 찾기·체크리스트용 글: 시트마다 `[시트 이름]` 줄, 다음 행마다 칸을 탭으로 이은 줄(정수인 숫자는 정수로).
    limit 글자를 넘으면 그 앞의 줄까지만(줄 하나가 limit보다 길면 그 줄을 자름). JSON에서 되살린 행(키가 글자)도 받는다."""
    cap = None if limit is None else max(0, int(limit))
    out: list[str] = []
    total = 0
    for line in _text_lines(sheets):
        add = len(line) + (1 if out else 0)
        if cap is not None and total + add > cap:
            if not out:
                out.append(line[:cap])
            break
        out.append(line)
        total += add
    return "\n".join(out)


def _text_lines(sheets):
    for sh in sheets or []:
        if not isinstance(sh, dict):
            continue
        yield "[" + _flat(str(sh.get("name") or "시트")) + "]"
        for r in sh.get("rows") or []:
            row = _int_keys(r)
            if row:
                yield "\t".join(_cell_text(row.get(c)) for c in range(min(max(row), 10 * MAX_COLS) + 1))


_FLAT = {9: " ", 10: " ", 13: " "}


def _flat(s: str) -> str:
    return s.translate(_FLAT)


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        return str(int(v)) if v.is_integer() and abs(v) < 1e16 else repr(v)
    if isinstance(v, int):
        return str(v)
    return _flat(str(v))


def _int_keys(r) -> dict:
    if not isinstance(r, dict):
        return {}
    out = {}
    for k, v in r.items():
        try:
            k = int(k)
        except (TypeError, ValueError):
            continue
        if k >= 0:
            out[k] = v
    return out


# ---------------------------------------------------------------- 공통

def _bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise SheetError(E_NOT_SHEET)


def _base(filename: str) -> str:
    # 윈도·맥·리눅스 어느 모양의 경로가 와도 마지막 이름만(지금 OS 규칙에 기대지 않는다).
    return re.split(r"[\\/]", str(filename or ""))[-1].strip()


def _ext(filename: str) -> str:
    base = _base(filename).lower()
    i = base.rfind(".")
    return base[i:] if i >= 0 else ""


def _stem(filename: str) -> str:
    base = _base(filename)
    i = base.rfind(".")
    return base[:i] if i > 0 else base


def _is_zip(data: bytes) -> bool:
    # 'PK'만 보면 "PK,이름"으로 시작하는 CSV도 zip으로 오해한다 — 머리글 서명 4바이트까지.
    return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _looks_text(data: bytes) -> bool:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    return b"\x00" not in data[:8192] and not data.startswith(b"%PDF")


class _Sheet:
    """시트 하나를 모은다: 빈 칸·빈 행 없음, 한도(행·열)를 넘으면 truncated."""

    def __init__(self, name):
        self.name = str(name or "").strip() or "Sheet"
        self.rows: dict[int, dict] = {}
        self.truncated = False
        self.last = -1

    def add(self, r: int, c: int, v) -> bool:
        """칸 하나를 넣는다. False = 행 한도가 차서 이 시트는 더 읽지 않아도 된다."""
        if isinstance(v, str):
            v = v.replace("\x00", "").strip()
            if not v:
                return True
        elif not isinstance(v, float) or not math.isfinite(v):
            return True
        if r < 0 or c < 0:
            return True
        if c >= MAX_COLS:
            self.truncated = True
            return True
        row = self.rows.get(r)
        if row is None:
            if len(self.rows) >= MAX_ROWS:
                self.truncated = True
                return r < self.last  # 행은 번호 순서로 오므로 마지막 행을 넘으면 그만 읽는다
            row = self.rows[r] = {}
            self.last = max(self.last, r)
        row[c] = v
        return True

    def add_row(self, r: int, cells) -> bool:
        for c, v in cells:
            if not self.add(r, c, v):
                return False
        return True

    def result(self) -> dict:
        out = {"name": self.name, "rows": [{c: row[c] for c in sorted(row)} for _, row in sorted(self.rows.items())]}
        if self.truncated:
            out["truncated"] = True
        return out


def _note_sheets(notes: list, n: Counter) -> None:
    if n["hidden"]:
        notes.append(f"숨긴 시트 {n['hidden']}개는 읽지 않음")
    if n["other"]:
        notes.append(f"차트·매크로 시트 {n['other']}개는 뺌")
    if n["empty"]:
        notes.append(f"빈 시트 {n['empty']}개는 뺌")
    if n["broken"]:
        notes.append(f"구조가 깨진 시트 {n['broken']}개는 읽지 않음")
    if n["big"]:
        notes.append(f"내용이 너무 큰 시트 {n['big']}개는 읽지 않음")
    if n["more"]:
        notes.append(f"시트가 {MAX_SHEETS}개를 넘어 앞의 {MAX_SHEETS}개만 읽음")
    if n["trunc"]:
        notes.append(f"시트 {n['trunc']}개는 한도(행 {MAX_ROWS:,}개·열 {MAX_COLS}개)를 넘는 부분을 잘라 냄")
    if n["nofv"]:
        notes.append(f"저장된 값이 없는 수식 칸 {n['nofv']}개는 뺌(엑셀에서 열어 저장하면 값이 생김)")


# ---------------------------------------------------------------- xlsx·xlsm (zip + XML)

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _zip_parts(z: zipfile.ZipFile) -> dict:
    """부분 이름(소문자, '/' 구분) → ZipInfo. OPC 부분 이름은 대소문자를 가리지 않는다."""
    out = {}
    for info in z.infolist():
        name = info.filename.replace("\\", "/").lstrip("/")
        if name and not name.endswith("/"):
            out.setdefault(name.lower(), info)
    return out


def _zip_is_workbook(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = _zip_parts(z)
            if "xl/workbook.xml" in parts or "xl/workbook.bin" in parts:
                return True
            ct = parts.get("[content_types].xml")
            if ct is None or ct.file_size > 1024 * 1024:
                return False
            with z.open(ct) as f:
                return bool(_MAIN_CT.search(f.read(1024 * 1024)))
    except Exception:  # 판별만 한다: 깨진 zip은 '엑셀 아님'
        return False


def _zread(z: zipfile.ZipFile, info: zipfile.ZipInfo, budget: list) -> bytes:
    """부분 하나를 푼다. 남은 한도(budget[0])를 넘으면 _TooBig — 적힌 크기로 먼저 막고, 실제로 푼 양으로 한 번 더."""
    if info.file_size > budget[0]:
        raise _TooBig()
    try:
        with z.open(info) as f:
            blob = f.read(budget[0] + 1)
    except RuntimeError:  # zip 항목 암호
        raise SheetError(E_LOCKED) from None
    if len(blob) > budget[0]:
        raise _TooBig()
    budget[0] -= len(blob)
    if b"<!DOCTYPE" in blob or b"<!ENTITY" in blob:  # 엑셀은 DTD를 쓰지 않는다(엔티티 폭탄 막기)
        raise SheetError(E_XLSX)
    return blob


def _target(src: str, target: str) -> str:
    target = (target or "").replace("\\", "/")
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(src), target))


def _rels(z, parts, budget, src: str) -> dict:
    d, base = posixpath.split(src)
    info = parts.get(posixpath.join(d, "_rels", base + ".rels").lower())
    if info is None:
        return {}
    out = {}
    for r in ET.fromstring(_zread(z, info, budget)):
        if _local(r.tag) != "Relationship" or (r.get("TargetMode") or "").lower() == "external":
            continue
        out[r.get("Id")] = ((r.get("Type") or "").rsplit("/", 1)[-1].lower(), _target(src, r.get("Target") or ""))
    return out


_XESC = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _xtext(s):
    if s is None:
        return None
    return _XESC.sub(lambda m: chr(int(m.group(1), 16)), s) if "_x" in s else s


def _rich(el) -> str:
    """<si>·<is>의 글자: <t>와 <r><t>만(발음 표시 <rPh>는 뺌)."""
    if el is None:
        return ""
    parts = []
    for ch in el:
        n = _local(ch.tag)
        if n == "t":
            parts.append(ch.text or "")
        elif n == "r":
            parts.extend(t.text or "" for t in ch if _local(t.tag) == "t")
    return _xtext("".join(parts))


_REF = re.compile(r"\$?([A-Za-z]{1,3})?\$?([0-9]{1,7})?$")
_ROWNUM = re.compile(r"[0-9]{1,7}")


def _ref(ref):
    """'AB12' → (열 27, 행 11). 없거나 이상하면 None."""
    m = _REF.match((ref or "").strip())
    if not m:
        return None, None
    col = row = None
    if m.group(1):
        n = 0
        for ch in m.group(1).upper():
            n = n * 26 + ord(ch) - 64
        col = n - 1
    if m.group(2) and int(m.group(2)) > 0:
        row = int(m.group(2)) - 1
    return col, row


def _read_xlsx(data: bytes, notes: list) -> list:
    z = zipfile.ZipFile(io.BytesIO(data))
    with z:
        parts = _zip_parts(z)
        budget = [MAX_UNPACKED]
        wb_path = next((p for t, p in _rels(z, parts, budget, "").values() if t == "officedocument"), "")
        if wb_path.lower() not in parts:
            ct = parts.get("[content_types].xml")
            wb_path = "xl/workbook.xml"
            if ct is not None:
                for el in ET.fromstring(_zread(z, ct, budget)):
                    if _local(el.tag) == "Override" and _MAIN_CT.search((el.get("ContentType") or "").encode()):
                        wb_path = _target("", el.get("PartName") or "")
                        break
        if wb_path.lower().endswith(".bin") or (wb_path.lower() not in parts and "xl/workbook.bin" in parts):
            raise SheetError(E_XLSB)
        info = parts.get(wb_path.lower())
        if info is None:
            raise SheetError(E_NO_WB)
        wb = ET.fromstring(_zread(z, info, budget))
        rels = _rels(z, parts, budget, wb_path)
        wb_dir = posixpath.dirname(wb_path)

        sst_path = next((p for t, p in rels.values() if t == "sharedstrings"), posixpath.join(wb_dir, "sharedStrings.xml"))
        sst_info = parts.get(sst_path.lower())
        sst = _xlsx_sst(_zread(z, sst_info, budget)) if sst_info is not None else []

        sheets_el = next((el for el in wb if _local(el.tag) == "sheets"), None)
        entries = [el for el in (sheets_el if sheets_el is not None else []) if _local(el.tag) == "sheet"]
        out, n = [], Counter()
        for pos, el in enumerate(entries):
            if (el.get("state") or "").lower() in ("hidden", "veryhidden"):
                n["hidden"] += 1
                continue
            rid = next((v for k, v in el.attrib.items() if k == "id" or k.endswith("}id")), None)
            typ, path = rels.get(rid, ("worksheet", posixpath.join(wb_dir, f"worksheets/sheet{pos + 1}.xml")))
            if typ != "worksheet" and (typ != "" or "worksheets/" not in path.lower()):
                n["other"] += 1  # chartsheet·dialogsheet·xlMacrosheet …
                continue
            if len(out) >= MAX_SHEETS:
                n["more"] += 1
                break
            info = parts.get(path.lower())
            if info is None:
                n["broken"] += 1
                continue
            try:
                blob = _zread(z, info, budget)
            except _TooBig:
                n["big"] += 1
                continue
            sh = _Sheet(el.get("name") or f"Sheet{pos + 1}")
            try:
                _xlsx_rows(blob, sst, sh, n)
            except ET.ParseError:
                n["broken"] += 1
                continue
            if not sh.rows:
                n["empty"] += 1
                continue
            n["trunc"] += sh.truncated
            out.append(sh.result())
    _note_sheets(notes, n)
    if not out and n["big"]:
        raise _TooBig()
    if not out and n["broken"]:
        raise SheetError(E_XLSX)
    return out


def _xlsx_sst(blob: bytes) -> list:
    out, root = [], None
    for ev, el in ET.iterparse(io.BytesIO(blob), events=("start", "end")):
        if ev == "start":
            if root is None:
                root = el
            continue
        if _local(el.tag) == "si":
            out.append(_rich(el))
            el.clear()
            if root is not None and el is not root:
                try:
                    root.remove(el)
                except ValueError:
                    pass
    return out


def _xlsx_rows(blob: bytes, sst: list, sh: _Sheet, n: Counter) -> None:
    parent, prev = None, -1
    for ev, el in ET.iterparse(io.BytesIO(blob), events=("start", "end")):
        tag = _local(el.tag)
        if ev == "start":
            if tag == "sheetData":
                parent = el
            continue
        if tag != "row":
            continue
        r = (el.get("r") or "").strip()
        idx = int(r) - 1 if _ROWNUM.fullmatch(r) and int(r) > 0 else None
        cells, nxt, ref_row = [], 0, None
        for c in el:
            if _local(c.tag) != "c":
                continue
            col, rr = _ref(c.get("r"))
            if col is None:
                col = nxt
            if ref_row is None:
                ref_row = rr
            nxt = col + 1
            v = _xlsx_value(c, sst, n)
            if v is not None:
                cells.append((col, v))
        if idx is None:
            idx = ref_row if ref_row is not None else prev + 1
        prev = idx
        go = sh.add_row(idx, cells)
        el.clear()
        if parent is not None:
            try:
                parent.remove(el)
            except ValueError:
                pass
        if not go:
            break


def _xlsx_value(c, sst: list, n: Counter):
    t = (c.get("t") or "n").strip()
    v, is_, has_f = None, None, False
    for ch in c:
        name = _local(ch.tag)
        if name == "v":
            v = ch.text
        elif name == "is":
            is_ = ch
        elif name == "f":
            has_f = True
    if t == "inlineStr":
        return _rich(is_) if is_ is not None else _xtext(v)
    if v is None:
        if has_f and t != "str":
            n["nofv"] += 1  # 계산된 값이 저장되지 않은 수식(일부 프로그램이 만든 파일)
        return None
    if t == "s":
        try:
            i = int(v)
        except ValueError:
            return None
        return sst[i] if 0 <= i < len(sst) else None
    if t in ("str", "d"):
        return _xtext(v)
    if t in ("b", "e"):
        return None  # 불리언·오류(#DIV/0! 등)는 뺌
    try:
        x = float(v)
    except ValueError:
        return _xtext(v)  # 숫자 칸인데 숫자가 아니면 글자로 둔다
    return x if math.isfinite(x) else None


# ---------------------------------------------------------------- xls (OLE2 복합 문서 + BIFF8)

_MAXREG = 0xFFFFFFFA  # 이보다 크면 FREESECT·ENDOFCHAIN·FATSECT·DIFSECT
_U32 = next(t for t in "IL" if array(t).itemsize == 4)


def _u32s(blob: bytes) -> array:
    a = array(_U32)
    a.frombytes(blob[: len(blob) // 4 * 4])
    if sys.byteorder == "big":
        a.byteswap()
    return a


class _Cfb:
    """OLE2(Compound File Binary) 읽기: 머리글 → DIFAT(+추가 DIFAT 섹터) → FAT → 디렉터리 → (작은 스트림은) mini FAT."""

    def __init__(self, data: bytes):
        if len(data) < 512 or data[:8] != _OLE:
            raise SheetError(E_XLS)
        major, bom, sshift, mshift = struct.unpack_from("<4H", data, 0x1A)
        if bom != 0xFFFE or sshift not in (9, 12) or not 0 < mshift < sshift:
            raise SheetError(E_XLS)
        self.data, self.ss, self.ms = data, 1 << sshift, 1 << mshift
        ss = self.ss
        self.nsec = max(0, -(-(len(data) - ss) // ss))
        n_fat, dir_start, _, _, self.mfat_start, _, difat_start, _ = struct.unpack_from("<8I", data, 0x2C)
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        s, hops = difat_start, 0
        while s <= _MAXREG:
            hops += 1
            if s >= self.nsec or hops > self.nsec:
                raise SheetError(E_XLS)
            vals = _u32s(self._sector(s, full=True))
            difat.extend(vals[:-1])
            s = vals[-1]
        fat = array(_U32)
        for x in [x for x in difat if x <= _MAXREG][:n_fat]:
            if x >= self.nsec:
                raise SheetError(E_XLS)
            fat.extend(_u32s(self._sector(x, full=True)))
        self.fat = fat
        blob = self._regular(dir_start, None)
        self.entries = []
        for off in range(0, len(blob) - 127, 128):
            e = blob[off:off + 128]
            typ = e[66]
            if typ not in (1, 2, 5):  # 저장소·스트림·루트만
                continue
            nlen = min(struct.unpack_from("<H", e, 64)[0], 64)
            name = e[:max(0, nlen - 2)].decode("utf-16-le", "replace")
            start, size = struct.unpack_from("<IQ", e, 116)
            if major == 3:
                size &= 0xFFFFFFFF  # v3는 아래 32비트만 쓴다
            self.entries.append((name, typ, start, size))
        self._mini = None
        self._minifat = None

    def _sector(self, i: int, full: bool = False) -> bytes:
        off = (i + 1) * self.ss
        blob = self.data[off:off + self.ss]
        if full and len(blob) < self.ss:
            blob += b"\xff" * (self.ss - len(blob))
        return blob

    @staticmethod
    def _chain(start: int, table, need):
        out, seen, s = [], set(), start
        while s <= _MAXREG:
            if s >= len(table) or s in seen:
                raise SheetError(E_XLS)  # 범위 밖·고리
            seen.add(s)
            out.append(s)
            if need is not None and len(out) >= need:
                break
            s = table[s]
        return out

    def _regular(self, start: int, size):
        need = None if size is None else -(-size // self.ss)
        if need == 0:
            return b""
        parts = []
        for k in self._chain(start, self.fat, need):
            if k >= self.nsec:
                raise SheetError(E_XLS)
            parts.append(self._sector(k))
        blob = b"".join(parts)
        return blob if size is None else blob[:size]

    def names(self) -> set:
        return {e[0].lower() for e in self.entries if e[1] == 2}

    def find(self, name: str):
        return next((e for e in self.entries if e[1] == 2 and e[0].lower() == name), None)

    def read(self, ent) -> bytes:
        _, _, start, size = ent
        if size > MAX_STREAM:
            raise _TooBig()
        if size == 0:
            return b""
        if size >= 4096:  # 표준의 mini stream 경계(머리글 값은 언제나 4096)
            return self._regular(start, size)
        if self._mini is None:
            root = next((e for e in self.entries if e[1] == 5), None)
            if root is None:
                raise SheetError(E_XLS)
            if root[3] > MAX_STREAM:
                raise _TooBig()
            self._mini = self._regular(root[2], root[3])
            self._minifat = _u32s(self._regular(self.mfat_start, None))
        parts = []
        for k in self._chain(start, self._minifat, -(-size // self.ms)):
            off = k * self.ms
            if off >= len(self._mini):
                raise SheetError(E_XLS)
            parts.append(self._mini[off:off + self.ms])
        return b"".join(parts)[:size]


def _ole_names(data: bytes) -> set:
    try:
        return _Cfb(data).names()
    except Exception:  # 판별만 한다: 깨진 OLE2는 이름 없음
        return set()


class _Segs:
    """레코드 본문 + 뒤따르는 CONTINUE 본문들을 이어 읽는다. 글자 배열이 CONTINUE로 넘어가면
    새 레코드 첫 바이트가 그 뒤 글자의 압축 여부(fHighByte)다(그 밖의 칸이 넘어갈 때는 그런 바이트가 없음)."""

    def __init__(self, segs):
        self.segs, self.i, self.p = segs, 0, 0

    def take(self, n: int) -> bytes:
        out = b""
        while n > 0:
            if self.i >= len(self.segs):
                raise _Eof()
            seg = self.segs[self.i]
            avail = len(seg) - self.p
            if avail <= 0:
                self.i, self.p = self.i + 1, 0
                continue
            k = min(avail, n)
            out += seg[self.p:self.p + k]
            self.p += k
            n -= k
        return out

    def skip(self, n: int) -> None:
        while n > 0:
            if self.i >= len(self.segs):
                raise _Eof()
            avail = len(self.segs[self.i]) - self.p
            if avail <= 0:
                self.i, self.p = self.i + 1, 0
                continue
            k = min(avail, n)
            self.p += k
            n -= k

    def chars(self, cch: int, high: int) -> str:
        parts = []
        while cch > 0:
            if self.i >= len(self.segs):
                raise _Eof()
            seg = self.segs[self.i]
            avail = len(seg) - self.p
            if avail <= 0:
                self.i += 1
                if self.i >= len(self.segs) or not self.segs[self.i]:
                    raise _Eof()
                high, self.p = self.segs[self.i][0] & 1, 1
                continue
            if high:
                k = min(avail // 2, cch)
                if k == 0:  # 홀수 바이트 하나가 남음 — 건너뜀
                    self.p = len(seg)
                    continue
                parts.append(seg[self.p:self.p + 2 * k].decode("utf-16-le", "replace"))
                self.p += 2 * k
            else:
                k = min(avail, cch)
                parts.append(seg[self.p:self.p + k].decode("latin-1"))
                self.p += k
            cch -= k
        return "".join(parts)


def _records(stream: bytes, pos: int):
    """(레코드 종류, [본문, CONTINUE 본문…]) 차례로."""
    n = len(stream)
    while pos + 4 <= n:
        rt, ln = struct.unpack_from("<HH", stream, pos)
        segs = [stream[pos + 4:pos + 4 + ln]]
        pos += 4 + ln
        while pos + 4 <= n and stream[pos] == 0x3C and stream[pos + 1] == 0:
            ln2 = struct.unpack_from("<H", stream, pos + 2)[0]
            segs.append(stream[pos + 4:pos + 4 + ln2])
            pos += 4 + ln2
        yield rt, segs


def _short_str(b: bytes, off: int) -> str:
    """ShortXLUnicodeString(글자 수 1바이트)."""
    if off + 2 > len(b):
        return ""
    cch, fl = b[off], b[off + 1]
    raw = b[off + 2:off + 2 + (2 * cch if fl & 1 else cch)]
    return raw.decode("utf-16-le", "replace") if fl & 1 else raw.decode("latin-1")


def _xl_str(segs, off: int):
    """XLUnicodeString(글자 수 2바이트) — LABEL·RSTRING·STRING. CONTINUE로 이어져도 읽는다."""
    rd = _Segs(segs)
    try:
        rd.skip(off)
        cch = struct.unpack("<H", rd.take(2))[0]
        return rd.chars(cch, rd.take(1)[0] & 1)
    except _Eof:
        return None


def _sst(segs) -> list:
    rd, out = _Segs(segs), []
    try:
        rd.skip(4)
        total = struct.unpack("<I", rd.take(4))[0]
        for _ in range(total):
            cch = struct.unpack("<H", rd.take(2))[0]
            fl = rd.take(1)[0]
            runs = struct.unpack("<H", rd.take(2))[0] if fl & 8 else 0
            ext = struct.unpack("<I", rd.take(4))[0] if fl & 4 else 0
            out.append(rd.chars(cch, fl & 1))
            rd.skip(4 * runs + ext)
    except _Eof:
        pass  # 잘린 SST: 읽은 데까지
    return out


def _rk(b: bytes, off: int) -> float:
    u = struct.unpack_from("<I", b, off)[0]
    if u & 2:
        x = float(struct.unpack_from("<i", b, off)[0] >> 2)
    else:
        x = struct.unpack("<d", struct.pack("<Q", (u & 0xFFFFFFFC) << 32))[0]
    return x / 100 if u & 1 else x


def _read_xls(data: bytes, notes: list) -> list:
    cfb = _Cfb(data)
    names = cfb.names()
    if names & {"encryptedpackage", "encryptioninfo"}:
        raise SheetError(E_LOCKED)  # 암호 걸린 xlsx는 OLE2로 감싸여 온다
    ent = cfb.find("workbook") or cfb.find("book")
    if ent is None:
        raise SheetError(E_NO_WB)
    return _biff(cfb.read(ent), notes)


def _biff(stream: bytes, notes: list) -> list:
    if len(stream) < 8:
        raise SheetError(E_XLS)
    rt, ln = struct.unpack_from("<HH", stream, 0)
    if rt in (0x0009, 0x0209, 0x0409):  # BIFF2·3·4
        raise SheetError(E_OLD)
    if rt != 0x0809:
        raise SheetError(E_XLS)
    if (struct.unpack_from("<H", stream, 4)[0] if ln >= 2 else 0) != 0x0600:  # BIFF5·7(엑셀 5.0·95) 등
        raise SheetError(E_OLD)
    bound, sst = [], []
    for rt, segs in _records(stream, 4 + ln):
        b = segs[0]
        if rt == 0x002F:  # FILEPASS
            raise SheetError(E_LOCKED)
        if rt == 0x0085 and len(b) >= 8:  # BOUNDSHEET
            off, state, dt = struct.unpack_from("<IBB", b, 0)
            bound.append((off, state & 3, dt, _short_str(b, 6)))
        elif rt == 0x00FC:  # SST
            sst = _sst(segs)
        elif rt == 0x000A:
            break
    out, n = [], Counter()
    for pos, (off, state, dt, name) in enumerate(bound):
        if state:
            n["hidden"] += 1
            continue
        if dt != 0:  # 매크로·차트·VBA 모듈
            n["other"] += 1
            continue
        if len(out) >= MAX_SHEETS:
            n["more"] += 1
            break
        sh = _Sheet(name or f"Sheet{pos + 1}")
        res = _biff_sheet(stream, off, sst, sh)
        if res != "ok":
            n[res] += 1
            continue
        if not sh.rows:
            n["empty"] += 1
            continue
        n["trunc"] += sh.truncated
        out.append(sh.result())
    _note_sheets(notes, n)
    if not out and n["broken"]:
        raise SheetError(E_XLS)
    return out


def _biff_sheet(stream: bytes, off: int, sst: list, sh: _Sheet) -> str:
    """시트 하나(BOF … EOF). 'ok' | 'other'(워크시트 아님) | 'broken'. 시트 안 차트(BOF…EOF 중첩)는 건너뛴다."""
    if off + 8 > len(stream):
        return "broken"
    rt, ln = struct.unpack_from("<HH", stream, off)
    if rt != 0x0809 or ln < 4:
        return "broken"
    if struct.unpack_from("<H", stream, off + 6)[0] != 0x0010:
        return "other"
    depth, pending = 1, None
    for rt, segs in _records(stream, off + 4 + ln):
        if rt == 0x0809:
            depth += 1
            continue
        if rt == 0x000A:
            depth -= 1
            if depth == 0:
                return "ok"
            continue
        if depth > 1:
            continue
        b, go = segs[0], True
        if rt == 0x0207:  # STRING: 바로 앞 수식의 글자 결과
            if pending is not None:
                s = _xl_str(segs, 0)
                if s is not None:
                    go = sh.add(pending[0], pending[1], s)
            pending = None
        elif rt in (0x04BC, 0x0221, 0x0236):  # SHRFMLA·ARRAY·TABLE: 수식과 STRING 사이에 올 수 있음
            pass
        else:
            pending = None
            if rt == 0x00FD and len(b) >= 10:  # LABELSST
                r, c, _, i = struct.unpack_from("<HHHI", b)
                if i < len(sst):
                    go = sh.add(r, c, sst[i])
            elif rt == 0x0203 and len(b) >= 14:  # NUMBER
                r, c = struct.unpack_from("<HH", b)
                go = sh.add(r, c, struct.unpack_from("<d", b, 6)[0])
            elif rt == 0x027E and len(b) >= 10:  # RK
                r, c = struct.unpack_from("<HH", b)
                go = sh.add(r, c, _rk(b, 6))
            elif rt == 0x00BD and len(b) >= 6:  # MULRK
                r, c0 = struct.unpack_from("<HH", b)
                for k in range((len(b) - 6) // 6):
                    go = sh.add(r, c0 + k, _rk(b, 4 + 6 * k + 2))
                    if not go:
                        break
            elif rt == 0x0006 and len(b) >= 14:  # FORMULA: 저장된 결과만
                r, c = struct.unpack_from("<HH", b)
                if b[12:14] == b"\xff\xff":
                    if b[6] == 0:  # 글자 결과 → 뒤따르는 STRING
                        pending = (r, c)
                    # 1 불리언·2 오류·3 빈 글자는 뺌
                else:
                    go = sh.add(r, c, struct.unpack_from("<d", b, 6)[0])
            elif rt in (0x0204, 0x00D6) and len(b) >= 9:  # LABEL·RSTRING
                r, c = struct.unpack_from("<HH", b)
                s = _xl_str(segs, 6)
                if s is not None:
                    go = sh.add(r, c, s)
            # BOOLERR·BLANK·MULBLANK와 서식 레코드는 무시
        if not go:
            return "ok"
    return "broken"  # EOF 없이 끝남


# ---------------------------------------------------------------- CSV·TSV

_DELIMS = {",": "쉼표", "\t": "탭", ";": "세미콜론"}


def _decode(data: bytes):
    """(글, 인코딩 이름, 확실한지)."""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace"), "UTF-8", True
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "UTF-16", True
    if b"\x00" in data:
        raise SheetError(E_CSV)
    try:
        return data.decode("utf-8"), "UTF-8", True
    except UnicodeDecodeError as e:
        if e.start >= len(data) - 3 and e.reason == "unexpected end of data":
            return data[:e.start].decode("utf-8", "replace"), "UTF-8", True  # 끝에서 잘린 글자 하나
    try:
        return data.decode("cp949"), "CP949(EUC-KR)", True
    except UnicodeDecodeError:
        pass
    a, b = data.decode("utf-8", "replace"), data.decode("cp949", "replace")
    return (a, "UTF-8", False) if a.count("�") <= b.count("�") else (b, "CP949(EUC-KR)", False)


def _sniff(text: str, prefer_tab: bool) -> str:
    """쉼표·탭·세미콜론 중 같은 칸 수(2개 이상)인 행이 가장 많은 것. 같으면 앞의 후보(.tsv면 탭 먼저)."""
    sample = "\n".join(text[:65536].splitlines()[:100])
    order = ("\t", ",", ";") if prefer_tab else (",", "\t", ";")
    best, best_score = order[0], (0, 0)
    for d in order:
        try:
            counts = [len(r) for r in csv.reader(io.StringIO(sample, newline=""), delimiter=d) if any(x.strip() for x in r)]
        except csv.Error:
            continue
        common = Counter(c for c in counts if c > 1).most_common(1)
        score = (common[0][1], common[0][0]) if common else (0, 0)
        if score > best_score:
            best, best_score = d, score
    return best


def _read_csv(data: bytes, filename: str, notes: list, prefer_tab: bool = False) -> list:
    text, enc, sure = _decode(data)
    if "\x00" in text:
        raise SheetError(E_CSV)
    delim = _sniff(text, prefer_tab)
    sh = _Sheet(_stem(filename) or "CSV")
    for r, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim)):
        if not sh.add_row(r, enumerate(row)):
            break
    notes.append(f"CSV를 {enc}·{_DELIMS[delim]} 구분으로 읽음")
    if not sure:
        notes.append("글자 인코딩이 확실하지 않아 일부 글자가 깨졌을 수 있음")
    if sh.truncated:
        notes.append(f"한도(행 {MAX_ROWS:,}개·열 {MAX_COLS}개)를 넘는 부분을 잘라 냄")
    return [sh.result()] if sh.rows else []


def _read_disguised(data: bytes, filename: str, notes: list, kind: str) -> list:
    """이름은 엑셀인데 zip·OLE2가 아닌 것: 웹 페이지·XML이면 이유를, .xls 이름의 글자 표(포털의 '엑셀 내려받기')면 CSV처럼 읽는다."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        head = data[2:2048].decode("utf-16-le" if data[:1] == b"\xff" else "utf-16-be", "ignore")
    else:
        head = (data[3:2048] if data[:3] == b"\xef\xbb\xbf" else data[:2048]).decode("latin-1")
    if head.lstrip(" \t\r\n\x00")[:1] == "<":
        raise SheetError(E_HTML)
    if kind != "xls" or not _looks_text(data):  # 글자 표로 읽어 주는 것은 .xls 이름일 때만(포털 내려받기 관행)
        raise SheetError(E_XLSX if kind == "xlsx" else E_XLS)
    sheets = _read_csv(data, filename, notes, prefer_tab=True)
    notes.insert(0, "엑셀 이름이지만 글자로 된 표(CSV·TSV)라 그렇게 읽음")
    return sheets
