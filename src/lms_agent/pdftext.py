"""PDF에서 글자를 꺼낸다(표준 라이브러리만).

교수가 올린 과제 PDF(Lab3.pdf 같은)에 적힌 마감("Due: Nov. 18, 23:59", "마감: 11월 18일(수) 23:59")을
`extract.find_deadlines`로 찾기 위해 쓴다. 완벽할 필요는 없다. 대신 어떤 파일을 받아도 예외를 내거나
멈추지 않고(쪽수·크기·시간 제한), 못 읽었으면 못 읽었다고 notes에 적는다.

읽는 것
- 파일 구조: xref 표, xref 스트림, 객체 스트림(/ObjStm), 증분 저장(/Prev, /XRefStm).
  xref가 깨졌으면 `N 0 obj ... endobj`를 훑어 다시 만든다.
- 압축: FlateDecode(+PNG/TIFF 예측자), LZWDecode, ASCIIHexDecode, ASCII85Decode, RunLengthDecode.
  그림 압축(DCT/JPX/CCITT/JBIG2)은 풀지 않는다(그림은 세기만 한다).
- 글자 연산자: BT/ET, Tf, Td, TD, Tm, T*, Tj, TJ, ', ", Tc, Tw, Tz, TL, Ts, q/Q, cm,
  Form XObject(Do, 깊이 제한), 인라인 그림(BI/ID/EI)은 건너뛴다.
  같은 높이의 글 조각을 한 줄로 묶고 x 순서로 놓는다. 떨어진 자리(TJ 간격, x 간격)에 빈칸을 넣는다.
- 글자 코드 → 유니코드: /ToUnicode CMap(bfchar/bfrange, 1·2바이트 코드), Identity-H CID 글꼴,
  이름 붙은 CJK CMap(UniKS-UCS2-H, KSCms-UHC-H 등), 단순 글꼴의 /Encoding·/Differences
  (StandardEncoding, WinAnsiEncoding, MacRomanEncoding, 글리프 이름).
- 주석(FreeText, Text)의 글.

못 읽는 것: 암호화된 PDF(풀지 않음), 그림으로만 된 글자(OCR 없음),
글자 대응표(ToUnicode)가 없는 CID 글꼴의 글자.
"""
from __future__ import annotations

import base64
import bisect
import re
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from math import hypot

__all__ = ["PdfTextResult", "extract_text", "PAGE_SEP"]

PAGE_SEP = "\n\f\n"
_MAX_FORM_DEPTH = 8


@dataclass
class PdfTextResult:
    text: str = ""        # 쪽 사이는 "\n\f\n", 줄 사이는 "\n"
    pages: int = 0        # 문서 전체 쪽수(찾은 만큼). 실제로 읽은 쪽은 text의 쪽 수
    ok: bool = False      # 읽을 수 있는 글자가 하나도 없으면 False(스캔 그림, 암호화, 지원 안 함)
    notes: list[str] = field(default_factory=list)  # 사용자에게 보여 줄 한국어 설명


def extract_text(data: bytes, max_pages: int = 40, max_bytes: int = 30_000_000,
                 time_budget: float = 5.0) -> PdfTextResult:
    """PDF 바이트에서 글자를 꺼낸다. 어떤 입력에도 예외를 내지 않는다."""
    res = PdfTextResult()
    start = time.monotonic()
    fatal = False
    try:
        fatal = _extract(res, data, max(0, int(max_pages)), int(max_bytes),
                         float(time_budget), start)
    except _Timeout:
        res.notes.append(f"시간 제한({float(time_budget):g}초)에 걸려 읽기를 멈췄습니다")
    except Exception:  # noqa: BLE001 - 어떤 파일이 와도 죽지 않는다
        res.notes.append("PDF 구조가 깨져 있어 읽지 못했습니다")
    res.ok = _readable(res.text)
    if not res.ok and not fatal:
        res.notes.append("읽을 수 있는 글자가 없습니다(스캔한 그림 PDF일 수 있습니다)")
    return res


# ---------------------------------------------------------------- 본체

def _extract(res: PdfTextResult, data, max_pages, max_bytes, budget, start) -> bool:
    """res를 채운다. 더 읽을 수 없는 이유(암호화 등)를 notes에 적었으면 True."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        res.notes.append("PDF 내용이 바이트가 아닙니다")
        return True
    data = bytes(data)
    if len(data) > max_bytes:
        res.notes.append(f"파일이 너무 커서({len(data) / 1e6:.1f}MB) 읽지 않았습니다")
        return True
    if not data.strip():
        res.notes.append("빈 파일입니다")
        return True
    if data.find(b"%PDF", 0, 1024) < 0 and not _RE_SCAN.search(data):
        res.notes.append("PDF 파일이 아닙니다")
        return True

    doc = _Doc(data, start + max(0.0, budget), max(1, max_bytes))
    doc.load()
    if doc.trailer.get("Encrypt") is not None:
        res.notes.append("암호화된 PDF라 읽지 못했습니다")
        try:
            res.pages = len(doc.pages())
        except Exception:  # noqa: BLE001
            pass
        return True

    pages = doc.pages()
    res.pages = len(pages)
    if not pages:
        res.notes.append("PDF에서 쪽을 찾지 못했습니다")
        return False

    texts: list[str] = []
    image_pages: list[int] = []
    broken = 0
    timed_out = False
    for i, (page, resources) in enumerate(pages[:max_pages]):
        if time.monotonic() >= doc.deadline:
            timed_out = True
            break
        acc = _Acc()
        try:
            r = doc.resolve(resources)
            content = doc.contents(page)
            if content:
                doc.run(content, r if isinstance(r, dict) else {}, _GS0, 0, acc)
        except _Timeout:
            timed_out = True
        except Exception:  # noqa: BLE001
            broken += 1
        try:
            lines = _layout(acc.chunks)
        except Exception:  # noqa: BLE001
            lines = []
            broken += 1
        try:
            lines += doc.annotations(page)
        except _Timeout:
            timed_out = True
        except Exception:  # noqa: BLE001
            pass
        page_text = "\n".join(lines)
        texts.append(page_text)
        if acc.images and not page_text.strip():
            image_pages.append(i + 1)
        if timed_out:
            break
    res.text = PAGE_SEP.join(texts)

    n = res.pages
    if doc.repaired:
        res.notes.append("PDF 목차(xref)가 맞지 않아 파일 전체를 훑어 읽었습니다")
    if n > max_pages:
        res.notes.append(f"앞 {max_pages}쪽만 읽었습니다(전체 {n}쪽)")
    if timed_out:
        res.notes.append(f"시간 제한({budget:g}초)에 걸려 {len(texts)}쪽까지만 읽었습니다")
    if image_pages:
        shown = ", ".join(str(p) for p in image_pages[:10]) + (" 등" if len(image_pages) > 10 else "")
        res.notes.append(f"글자 대신 그림으로 된 페이지 {len(image_pages)}쪽({shown}번째 쪽)")
    bad = sum(1 for f in doc.fonts.values() if not f.fallback and f.missing > 0)
    if bad:
        res.notes.append(f"글꼴 {bad}개는 글자 대응표가 없어 일부 글자를 못 읽었습니다")
    if doc.unsupported:
        res.notes.append("읽을 수 없는 압축 방식이 있어 일부 내용을 건너뛰었습니다")
    if doc.too_big:
        res.notes.append("압축을 푼 내용이 너무 커서 일부를 건너뛰었습니다")
    if broken:
        res.notes.append(f"{broken}쪽은 내용이 깨져 있어 일부만 읽었습니다")
    return False


def _readable(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


class _Timeout(Exception):
    pass


# ---------------------------------------------------------------- 낱말(토큰)

class Name(str):
    """PDF 이름(/Type 같은). 키워드(연산자)는 그냥 str이다."""
    __slots__ = ()


class Ref:
    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int):
        self.num = num
        self.gen = gen

    def __repr__(self):
        return f"Ref({self.num}, {self.gen})"


class Stream:
    __slots__ = ("dict", "raw", "dec")

    def __init__(self, d: dict, raw: bytes):
        self.dict = d
        self.raw = raw
        self.dec = None


class _Mark:
    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s

    def __repr__(self):
        return self.s


_END = _Mark("EOF")
_AO, _AC, _DO, _DC = _Mark("["), _Mark("]"), _Mark("<<"), _Mark(">>")
_MISSING = _Mark("missing")

_WS_BYTES = b"\x00\t\n\x0c\r "
_WS_SET = frozenset(_WS_BYTES)
_NUMSTART = frozenset(b"+-.0123456789")
_RE_WS = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)+")
_RE_REG = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_RE_NAME = re.compile(rb"/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_RE_NUM = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_RE_HEX = re.compile(rb"<([0-9A-Fa-f\x00\t\n\x0c\r ]*)>")
_RE_PAREN = re.compile(rb"[()\\]")
_RE_ESC = re.compile(rb"\\([0-7]{1,3}|\r\n|[\s\S])")
_RE_NAMEHEX = re.compile(rb"#([0-9A-Fa-f]{2})")
_ESC_MAP = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
            b"\r\n": b"", b"\r": b"", b"\n": b""}


def _esc(m):
    g = m.group(1)
    if g[0] in b"01234567":
        return bytes([int(g, 8) & 0xFF])
    return _ESC_MAP.get(g, g)


def _unescape(raw: bytes) -> bytes:
    return _RE_ESC.sub(_esc, raw) if b"\\" in raw else raw


def _literal(d: bytes, pos: int):
    """d[pos]=='('인 문자열을 읽어 (바이트, 다음 위치)를 준다. 괄호 짝과 \\ 이스케이프를 따진다."""
    i = pos + 1
    depth = 0
    n = len(d)
    search = _RE_PAREN.search
    while True:
        m = search(d, i)
        if m is None:
            end = nxt = n
            break
        j = m.start()
        c = d[j]
        if c == 92:      # \
            i = j + 2
        elif c == 40:    # (
            depth += 1
            i = j + 1
        elif depth == 0:
            end, nxt = j, j + 1
            break
        else:
            depth -= 1
            i = j + 1
    return _unescape(d[pos + 1:end]), nxt


def _hex(h: bytes) -> bytes:
    h = h.translate(None, _WS_BYTES)
    if len(h) & 1:
        h += b"0"
    return bytes.fromhex(h.decode("ascii"))


def _name(raw: bytes) -> Name:
    if b"#" in raw:
        raw = _RE_NAMEHEX.sub(lambda m: bytes([int(m.group(1), 16)]), raw)
    return Name(raw.decode("latin-1"))


class _Lexer:
    """바이트에서 낱말을 하나씩 꺼낸다. 어떤 바이트가 와도 앞으로 나아간다."""
    __slots__ = ("d", "pos", "n", "start")

    def __init__(self, d: bytes, pos: int = 0):
        self.d = d
        self.pos = pos
        self.n = len(d)
        self.start = pos

    def next(self):
        d = self.d
        n = self.n
        pos = self.pos
        while True:
            m = _RE_WS.match(d, pos)
            if m:
                pos = m.end()
            if pos >= n:
                self.pos = self.start = n
                return _END
            c = d[pos]
            self.start = pos
            if c == 47:      # /
                m = _RE_NAME.match(d, pos)
                self.pos = m.end()
                return _name(m.group()[1:])
            if c == 40:      # (
                s, self.pos = _literal(d, pos)
                return s
            if c == 91:      # [
                self.pos = pos + 1
                return _AO
            if c == 93:      # ]
                self.pos = pos + 1
                return _AC
            if c == 60:      # <
                if d[pos + 1:pos + 2] == b"<":
                    self.pos = pos + 2
                    return _DO
                m = _RE_HEX.match(d, pos)
                if m:
                    self.pos = m.end()
                    return _hex(m.group(1))
                pos += 1
                continue
            if c == 62:      # >
                if d[pos + 1:pos + 2] == b">":
                    self.pos = pos + 2
                    return _DC
                pos += 1
                continue
            if c == 41 or c == 123 or c == 125:   # ) { }
                pos += 1
                continue
            m = _RE_REG.match(d, pos)
            tok = m.group()
            self.pos = m.end()
            if c in _NUMSTART and len(tok) <= 24 and _RE_NUM.fullmatch(tok):
                return float(tok) if b"." in tok else int(tok)
            return tok.decode("latin-1")


def _close(items: list):
    if items[0] is _DO:
        d = {}
        i, n = 1, len(items)
        while i < n:
            k = items[i]
            if type(k) is Name and i + 1 < n:
                d[k] = items[i + 1]
                i += 2
            else:
                i += 1
        return d
    return items[1:]


def _collapse(stack: list):
    while len(stack) > 1:
        v = _close(stack.pop())
        stack[-1].append(v)
    return _close(stack[0])


def _parse(lex: _Lexer):
    """객체 하나를 읽는다. 끝이면 _END, 맨 앞이 키워드면 그 str(endobj, stream 등)."""
    stack: list = []
    nxt = lex.next
    while True:
        start = lex.pos
        t = nxt()
        if t is _END:
            return _collapse(stack) if stack else _END
        tt = type(t)
        if t is _AO or t is _DO:
            if len(stack) >= 256:
                raise ValueError("너무 깊이 겹친 객체")
            stack.append([t])
            continue
        if t is _AC or t is _DC:
            if not stack:
                continue
            val = _close(stack.pop())
        elif tt is str:
            if t == "R":
                if stack:
                    cur = stack[-1]
                    if len(cur) >= 3 and type(cur[-1]) is int and type(cur[-2]) is int:
                        g = cur.pop()
                        cur.append(Ref(cur.pop(), g))
                continue
            if t == "true":
                val = True
            elif t == "false":
                val = False
            elif t == "null":
                val = None
            elif stack:      # 닫히지 않은 채 endobj/stream 등이 나왔다
                lex.pos = start
                return _collapse(stack)
            else:
                return t
        else:
            val = t
        if stack:
            stack[-1].append(val)
            continue
        if tt is int:        # 맨 위의 "5 0 R"
            save = lex.pos
            t2 = nxt()
            if type(t2) is int:
                t3 = nxt()
                if type(t3) is str and t3 == "R":
                    return Ref(val, t2)
            lex.pos = save
        return val


def _num(x, default):
    return float(x) if type(x) is int or type(x) is float else default


# ---------------------------------------------------------------- 압축 풀기

def _inflate(data: bytes, limit: int) -> bytes:
    for wbits in (15, -15):
        z = zlib.decompressobj(wbits)
        parts = []
        total = 0
        try:
            for i in range(0, len(data), 16384):
                p = z.decompress(data[i:i + 16384])
                if p:
                    parts.append(p)
                    total += len(p)
                    if total > limit:
                        break
                if z.eof:
                    break
            else:
                parts.append(z.flush())
            return b"".join(parts)
        except zlib.error:
            if parts:        # 끝이 깨진 스트림: 앞부분이라도 쓴다
                return b"".join(parts)
    return b""


def _lzw(data: bytes, early: int, limit: int, deadline: float) -> bytes:
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bits = 9
    buf = 0
    nbits = 0
    prev = None
    for k, byte in enumerate(data):
        if not k & 0xFFFF and time.monotonic() >= deadline:
            raise _Timeout()
        buf = (buf << 8) | byte
        nbits += 8
        while nbits >= bits:
            nbits -= bits
            code = (buf >> nbits) & ((1 << bits) - 1)
            buf &= (1 << nbits) - 1
            if code == 256:
                del table[258:]
                bits = 9
                prev = None
                continue
            if code == 257:
                return bytes(out)
            if prev is None:
                entry = table[code] if code < len(table) else b""
            elif code < len(table):
                entry = table[code]
                if len(table) < 4096:
                    table.append(prev + entry[:1])
            elif code == len(table):
                entry = prev + prev[:1]
                if len(table) < 4096:
                    table.append(entry)
            else:
                return bytes(out)
            out += entry
            if len(out) > limit:
                return bytes(out)
            prev = entry
            if len(table) + early >= (1 << bits) and bits < 12:
                bits += 1
    return bytes(out)


def _ahx(data: bytes) -> bytes:
    i = data.find(b">")
    if i >= 0:
        data = data[:i]
    h = re.sub(rb"[^0-9A-Fa-f]", b"", data)
    if len(h) & 1:
        h += b"0"
    return bytes.fromhex(h.decode("ascii"))


def _a85(data: bytes) -> bytes:
    d = data.translate(None, _WS_BYTES)
    if d.startswith(b"<~"):
        d = d[2:]
    i = d.find(b"~>")
    if i >= 0:
        d = d[:i]
    try:
        return base64.a85decode(d)
    except Exception:  # noqa: BLE001
        cut = len(d) - len(d) % 5
        try:
            return base64.a85decode(d[:cut])
        except Exception:  # noqa: BLE001
            return b""


def _rl(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        ln = data[i]
        i += 1
        if ln == 128:
            break
        if ln < 128:
            out += data[i:i + ln + 1]
            i += ln + 1
        elif i < n:
            out += bytes([data[i]]) * (257 - ln)
            i += 1
    return bytes(out)


def _unpredict(data: bytes, p: dict) -> bytes:
    pred = int(_num(p.get("Predictor"), 1))
    if pred <= 1:
        return data
    colors = max(1, int(_num(p.get("Colors"), 1)))
    bpc = max(1, int(_num(p.get("BitsPerComponent"), 8)))
    cols = max(1, int(_num(p.get("Columns"), 1)))
    bpp = max(1, colors * bpc // 8)
    row = (colors * bpc * cols + 7) // 8
    if row <= 0 or row > 1 << 20:
        return data
    if pred == 2:
        if bpc != 8:
            return data
        out = bytearray(data)
        for r in range(0, len(out), row):
            for j in range(r + bpp, min(r + row, len(out))):
                out[j] = (out[j] + out[j - bpp]) & 255
        return bytes(out)
    out = bytearray()
    prev = bytearray(row)
    step = row + 1
    for i in range(0, len(data), step):
        ft = data[i]
        cur = bytearray(data[i + 1:i + step])
        if len(cur) < row:
            cur.extend(bytes(row - len(cur)))
        if ft == 1:
            for j in range(bpp, row):
                cur[j] = (cur[j] + cur[j - bpp]) & 255
        elif ft == 2:
            cur = bytearray((a + b) & 255 for a, b in zip(cur, prev))
        elif ft == 3:
            for j in range(row):
                left = cur[j - bpp] if j >= bpp else 0
                cur[j] = (cur[j] + ((left + prev[j]) >> 1)) & 255
        elif ft == 4:
            for j in range(row):
                a = cur[j - bpp] if j >= bpp else 0
                b = prev[j]
                c = prev[j - bpp] if j >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                cur[j] = (cur[j] + pr) & 255
        out += cur
        prev = cur
    return bytes(out)


# ---------------------------------------------------------------- 파일 구조

_RE_HDR = re.compile(rb"[\x00\t\n\x0c\r ]*(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})[\x00\t\n\x0c\r ]+obj(?![A-Za-z])")
_RE_SCAN = re.compile(rb"(?<![0-9])(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})[\x00\t\n\x0c\r ]+obj(?![A-Za-z])")
_RE_STREAM = re.compile(rb"[\x00\t\n\x0c\r ]*stream(?:[ \t]*(?:\r\n|\n|\r))?")
_RE_STARTXREF = re.compile(rb"startxref[\x00\t\n\x0c\r ]+(\d{1,12})")
_RE_XREF = re.compile(rb"[\x00\t\n\x0c\r ]*xref")
_RE_XSUB = re.compile(rb"[\x00\t\n\x0c\r ]*(\d{1,10})[ \t]+(\d{1,10})[ \t]*(?:\r\n|\r|\n)")
_RE_XENT = re.compile(rb"[\x00\t\n\x0c\r ]*(\d{1,10})[ \t]+(\d{1,5})[ \t]+([nf])")
_RE_TRAILER = re.compile(rb"[\x00\t\n\x0c\r ]*trailer")
_RE_CATALOG = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/Catalog(?![A-Za-z])")
_RE_PAGE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/Page(?![A-Za-z])")
_RE_EI = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r ]|\Z)")
_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_GS0 = (_ID, None, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0)   # ctm, font, size, Tc, Tw, Th, TL, Ts


def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


class _Acc:
    """한 쪽에서 모은 글 조각과 그림 수."""
    __slots__ = ("chunks", "images", "active")

    def __init__(self):
        self.chunks: list = []
        self.images = 0
        self.active: set = set()


class _Doc:
    def __init__(self, data: bytes, deadline: float, limit: int):
        self.d = data
        self.deadline = deadline
        self.limit = limit                 # 스트림 하나를 풀었을 때 최대 크기
        self.budget = 2 * limit + (64 << 20)  # 전체로 풀 수 있는 크기
        self.xref: dict = {}               # num -> (0,) | (1, offset) | (2, objstm, index)
        self.cache: dict = {}
        self.trailer: dict = {}
        self.scan_off = None               # 훑어서 찾은 num -> offset
        self.scan_stm = None               # 훑어서 찾은 num -> (objstm, index)
        self.stms: dict = {}
        self.fonts: dict = {}
        self.fallback_font = _Font()
        self.repaired = False
        self.unsupported = 0
        self.too_big = False

    def tick(self):
        if time.monotonic() >= self.deadline:
            raise _Timeout()

    # ---- xref

    def load(self):
        ok = False
        try:
            i = self.d.rfind(b"startxref")
            if i >= 0:
                m = _RE_STARTXREF.match(self.d, i)
                if m:
                    self._xref_chain(int(m.group(1)))
            root = self.resolve(self.trailer.get("Root"))
            ok = isinstance(root, dict)
        except _Timeout:
            raise
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            self._rebuild()

    def _xref_chain(self, off: int):
        seen = set()
        todo = [off]
        while todo and len(seen) < 64:
            off = todo.pop(0)
            if off in seen or not 0 <= off < len(self.d):
                continue
            seen.add(off)
            tr = self._xref_section(off)
            if not tr:
                continue
            for k, v in tr.items():
                self.trailer.setdefault(k, v)
            xs, prev = tr.get("XRefStm"), tr.get("Prev")
            if type(xs) is int:
                todo.insert(0, xs)
            if type(prev) is int:
                todo.append(prev)

    def _xref_section(self, off: int):
        m = _RE_XREF.match(self.d, off)
        if m:
            return self._xref_table(m.end())
        obj = self._load_at(off, None)
        if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef":
            self._xref_stream(obj)
            return obj.dict
        return None

    def _xref_table(self, pos: int) -> dict:
        d, xref = self.d, self.xref
        while True:
            m = _RE_XSUB.match(d, pos)
            if not m:
                break
            first, count = int(m.group(1)), int(m.group(2))
            pos = m.end()
            for k in range(min(count, len(d) // 18 + 1)):
                e = _RE_XENT.match(d, pos)
                if not e:
                    break
                pos = e.end()
                num = first + k
                if num not in xref:
                    xref[num] = (1, int(e.group(1))) if e.group(3) == b"n" else (0,)
        m = _RE_TRAILER.match(d, pos)
        if not m:
            return {}
        tr = _parse(_Lexer(d, m.end()))
        return tr if isinstance(tr, dict) else {}

    def _xref_stream(self, s: Stream):
        data = self.decode(s)
        if not data:
            return
        R = self.resolve
        w = R(s.dict.get("W"))
        if not isinstance(w, list) or len(w) < 3:
            return
        w = [int(_num(R(x), -1)) for x in w[:3]]
        if any(x < 0 or x > 8 for x in w):
            return
        rec = sum(w)
        if not rec:
            return
        size = int(_num(R(s.dict.get("Size")), 0))
        index = R(s.dict.get("Index"))
        if not isinstance(index, list):
            index = [0, size]
        pos = 0
        xref = self.xref
        for i in range(0, len(index) - 1, 2):
            first, count = int(_num(R(index[i]), 0)), int(_num(R(index[i + 1]), 0))
            for k in range(max(0, count)):
                if pos + rec > len(data):
                    return
                f = []
                for x in w:
                    f.append(int.from_bytes(data[pos:pos + x], "big") if x else None)
                    pos += x
                t = f[0] if w[0] else 1
                num = first + k
                if num in xref:
                    continue
                if t == 1:
                    xref[num] = (1, f[1] or 0)
                elif t == 2:
                    xref[num] = (2, f[1] or 0, f[2] or 0)
                elif t == 0:
                    xref[num] = (0,)

    def _rebuild(self):
        """xref를 못 믿을 때: 파일을 훑어 trailer(또는 카탈로그)를 찾는다."""
        self._ensure_scan()
        self.repaired = True
        d = self.d
        tr = None
        pos = len(d)
        for _ in range(50):
            i = d.rfind(b"trailer", 0, pos)
            if i < 0:
                break
            try:
                t = _parse(_Lexer(d, i + 7))
            except ValueError:
                t = None
            if isinstance(t, dict) and "Root" in t:
                tr = t
                break
            pos = i
        if tr is None:
            for num, off in sorted(self.scan_off.items(), key=lambda kv: -kv[1]):
                if b"/XRef" in d[off:off + 1024]:
                    o = self._load_at(off, num)
                    if isinstance(o, Stream) and "Root" in o.dict:
                        tr = o.dict
                        break
        if tr is not None and not isinstance(self.resolve(tr.get("Root")), dict):
            enc = tr.get("Encrypt")
            tr = {"Encrypt": enc} if enc is not None else None
        if tr is None or "Root" not in tr:
            root = self._find_catalog()
            if root is not None:
                tr = dict(tr or {})
                tr["Root"] = root
        if tr:
            self.trailer.update(tr)

    def _ensure_scan(self):
        if self.scan_off is not None:
            return
        offs = {}
        for m in _RE_SCAN.finditer(self.d):
            offs[int(m.group(1))] = m.start()
        self.scan_off = offs
        self.tick()

    def _scan_stms(self):
        self.scan_stm = {}
        self._ensure_scan()
        d = self.d
        for num, off in sorted(self.scan_off.items(), key=lambda kv: kv[1]):
            if b"/ObjStm" not in d[off:off + 1024]:
                continue
            self.tick()
            info = self._stm(num)
            if info:
                for idx, (objnum, _o) in enumerate(info[2]):
                    self.scan_stm[objnum] = (num, idx)

    def _header_index(self):
        pairs = sorted((off, num) for num, off in self.scan_off.items())
        return [p[0] for p in pairs], [p[1] for p in pairs]

    def _objects_matching(self, rx) -> list:
        """rx가 나오는 자리를 품은 객체 번호(파일 순서), 그다음 객체 스트림 안의 객체."""
        self._ensure_scan()
        offs, nums = self._header_index()
        out, seen = [], set()
        for m in rx.finditer(self.d):
            k = bisect.bisect_right(offs, m.start()) - 1
            if k >= 0 and nums[k] not in seen:
                seen.add(nums[k])
                out.append(nums[k])
        if self.scan_stm is None:
            self._scan_stms()
        for num in self.scan_stm:
            if num not in seen:
                seen.add(num)
                out.append(num)
        return out

    def _find_catalog(self):
        for num in self._objects_matching(_RE_CATALOG):
            self.tick()
            o = self.get(num)
            if isinstance(o, dict) and o.get("Type") == "Catalog" and "Pages" in o:
                return Ref(num, 0)
        return None

    # ---- 객체

    def _load_at(self, off: int, num):
        self.tick()
        d = self.d
        if not 0 <= off < len(d):
            return _MISSING
        m = _RE_HDR.match(d, off)
        if m is None or (num is not None and int(m.group(1)) != num):
            return _MISSING
        lex = _Lexer(d, m.end())
        try:
            obj = _parse(lex)
        except ValueError:
            return _MISSING
        if obj is _END or type(obj) is str:
            return None
        if type(obj) is dict:
            sm = _RE_STREAM.match(d, lex.pos)
            if sm:
                start = sm.end()
                ln = obj.get("Length")
                if type(ln) is Ref:
                    ln = self.get(ln.num)
                end = -1
                if type(ln) is int and 0 <= ln and start + ln <= len(d):
                    if d[start + ln:start + ln + 40].lstrip(_WS_BYTES).startswith(b"endstream"):
                        end = start + ln
                if end < 0:
                    k = d.find(b"endstream", start)
                    end = k if k >= 0 else len(d)
                    if d[end - 2:end] == b"\r\n":
                        end -= 2
                    elif d[end - 1:end] in (b"\n", b"\r"):
                        end -= 1
                    end = max(end, start)
                return Stream(obj, d[start:end])
        return obj

    def get(self, num):
        c = self.cache
        if num in c:
            return c[num]
        c[num] = None        # 자기 자신을 다시 찾는 고리를 끊는다
        obj = _MISSING
        e = self.xref.get(num)
        try:
            if e and e[0] == 1:
                obj = self._load_at(e[1], num)
            elif e and e[0] == 2:
                obj = self._from_stm(e[1], e[2], num)
        except _Timeout:
            raise
        except Exception:  # noqa: BLE001
            obj = _MISSING
        if obj is _MISSING:
            try:
                obj = self._recover(num)
            except _Timeout:
                raise
            except Exception:  # noqa: BLE001
                obj = _MISSING
        if obj is _MISSING:
            obj = None
        c[num] = obj
        return obj

    def _recover(self, num):
        self._ensure_scan()
        off = self.scan_off.get(num)
        if off is not None:
            o = self._load_at(off, num)
            if o is not _MISSING:
                if self.xref:
                    self.repaired = True
                return o
        if self.scan_stm is None:
            self._scan_stms()
        e = self.scan_stm.get(num)
        if e:
            o = self._from_stm(e[0], e[1], num)
            if o is not _MISSING and self.xref:
                self.repaired = True
            return o
        return _MISSING

    def _stm(self, stmnum):
        if stmnum in self.stms:
            return self.stms[stmnum]
        self.stms[stmnum] = None
        info = None
        s = self.get(stmnum)
        if isinstance(s, Stream):
            data = self.decode(s)
            if data:
                R = self.resolve
                n = int(_num(R(s.dict.get("N")), 0))
                first = int(_num(R(s.dict.get("First")), 0))
                if not 0 < first <= len(data):
                    first = len(data)
                lex = _Lexer(data[:first])
                pairs = []
                for _ in range(min(max(n, 0), 1_000_000)):
                    a, b = lex.next(), lex.next()
                    if type(a) is not int or type(b) is not int:
                        break
                    pairs.append((a, b))
                info = (data, first, pairs, {p[0]: p[1] for p in pairs})
        self.stms[stmnum] = info
        return info

    def _from_stm(self, stmnum, idx, num):
        info = self._stm(stmnum)
        if not info:
            return _MISSING
        data, first, pairs, by_num = info
        if 0 <= idx < len(pairs) and pairs[idx][0] == num:
            off = pairs[idx][1]
        elif num in by_num:
            off = by_num[num]
        else:
            return _MISSING
        if not 0 <= first + off < len(data):
            return _MISSING
        try:
            obj = _parse(_Lexer(data, first + off))
        except ValueError:
            return _MISSING
        if obj is _END or type(obj) is str:
            return None
        return obj

    def resolve(self, x):
        n = 0
        while type(x) is Ref and n < 32:
            x = self.get(x.num)
            n += 1
        return None if type(x) is Ref else x

    def decode(self, s: Stream) -> bytes:
        if s.dec is not None:
            return s.dec
        R = self.resolve
        f = R(s.dict.get("Filter"))
        p = R(s.dict.get("DecodeParms"))
        filters = f if isinstance(f, list) else ([f] if f is not None else [])
        parms = p if isinstance(p, list) else [p]
        data = s.raw
        for i, f in enumerate(filters):
            f = R(f)
            pr = R(parms[i]) if i < len(parms) else None
            pr = pr if isinstance(pr, dict) else {}
            if f in ("FlateDecode", "Fl"):
                data = _unpredict(_inflate(data, self.limit), pr)
            elif f in ("LZWDecode", "LZW"):
                early = int(_num(pr.get("EarlyChange"), 1))
                data = _unpredict(_lzw(data, early, self.limit, self.deadline), pr)
            elif f in ("ASCIIHexDecode", "AHx"):
                data = _ahx(data)
            elif f in ("ASCII85Decode", "A85"):
                data = _a85(data)
            elif f in ("RunLengthDecode", "RL"):
                data = _rl(data)
            elif f == "Crypt":
                continue
            else:            # 그림 압축이거나 모르는 방식
                self.unsupported += 1
                data = b""
                break
            if len(data) > self.limit:
                self.too_big = True
                data = data[:self.limit]
        self.budget -= len(data)
        if self.budget < 0:
            self.too_big = True
            data = b""
        s.dec = data
        return data

    # ---- 쪽

    def pages(self) -> list:
        root = self.resolve(self.trailer.get("Root"))
        out = self._walk(root.get("Pages")) if isinstance(root, dict) else []
        if not out:
            out = self._scan_pages()
        return out

    def _walk(self, top) -> list:
        out, seen = [], set()
        stack = [(top, None)]
        while stack:
            ref, inherited = stack.pop()
            key = ("r", ref.num) if type(ref) is Ref else ("i", id(ref))
            if key in seen:
                continue
            seen.add(key)
            if not len(seen) % 1000:
                self.tick()
            if len(seen) > 200_000:
                break
            node = self.resolve(ref)
            if not isinstance(node, dict):
                continue
            res = node.get("Resources", inherited)
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") != "Page" and isinstance(kids, list):
                for k in reversed(kids):
                    stack.append((k, res))
            elif node.get("Type") == "Page" or "Contents" in node:
                out.append((node, res))
        return out

    def _scan_pages(self) -> list:
        out = []
        for num in self._objects_matching(_RE_PAGE):
            self.tick()
            o = self.get(num)
            if isinstance(o, dict) and o.get("Type") == "Page":
                out.append((o, self._inherit(o, "Resources")))
        return out

    def _inherit(self, node: dict, key: str):
        for _ in range(32):
            if key in node:
                return node[key]
            node = self.resolve(node.get("Parent"))
            if not isinstance(node, dict):
                return None
        return None

    def contents(self, page: dict) -> bytes:
        c = self.resolve(page.get("Contents"))
        if isinstance(c, Stream):
            return self.decode(c)
        if isinstance(c, list):
            parts = []
            for x in c[:10000]:
                x = self.resolve(x)
                if isinstance(x, Stream):
                    parts.append(self.decode(x))
            return b"\n".join(parts)
        return b""

    def annotations(self, page: dict) -> list:
        out = []
        an = self.resolve(page.get("Annots"))
        if not isinstance(an, list):
            return out
        for a in an[:500]:
            a = self.resolve(a)
            if isinstance(a, dict) and a.get("Subtype") in ("FreeText", "Text"):
                c = self.resolve(a.get("Contents"))
                if isinstance(c, bytes) and c.strip():
                    for line in _text_string(c).splitlines():
                        line = _clean(line)
                        if line:
                            out.append(line)
        return out

    def font(self, res, name):
        fonts = self.resolve(res.get("Font")) if isinstance(res, dict) else None
        if not isinstance(fonts, dict):
            return None
        ref = fonts.get(name)
        if ref is None:
            return None
        key = ("r", ref.num) if type(ref) is Ref else ("i", id(ref))
        f = self.fonts.get(key)
        if f is None:
            fd = self.resolve(ref)
            try:
                f = _Font(self, fd) if isinstance(fd, dict) else _Font()
            except _Timeout:
                raise
            except Exception:  # noqa: BLE001
                f = _Font()
            self.fonts[key] = f
        return f

    # ---- 내용 스트림

    def run(self, data: bytes, res: dict, gs: tuple, depth: int, acc: _Acc):
        """내용 스트림을 따라가며 글 조각(위치·크기·글자)을 acc.chunks에 모은다."""
        ctm, font, size, tc, tw, th, tl, rise = gs
        tm = tlm = _ID
        saved: list = []
        lex = _Lexer(data)
        nxt = lex.next
        ops: list = []
        stack: list = []
        n_ops = 0
        deadline = self.deadline
        chunks = acc.chunks
        fallback = self.fallback_font

        def show(s: bytes):
            nonlocal tm
            f = font if font is not None else fallback
            text, w, ng, nsp = f.decode(s)
            a, b, c, d, e, ff = tm
            A, B, C, D, E, F = ctm
            ma, mb = a * A + b * C, a * B + b * D
            mc, md = c * A + d * C, c * B + d * D
            x0 = e * A + ff * C + E + rise * mc
            y0 = e * B + ff * D + F + rise * md
            tx = (w * f.scale * size + ng * tc + nsp * tw) * th
            tm = (a, b, c, d, e + tx * a, ff + tx * b)
            if text:
                chunks.append((x0, y0, x0 + tx * ma, y0 + tx * mb,
                               abs(size) * hypot(mc, md) * f.vscale, ma, mb, text))

        while True:
            t = nxt()
            tt = type(t)
            if tt is not str:
                if t is _END:
                    break
                if t is _AO or t is _DO:
                    if len(stack) < 32:
                        stack.append([t])
                    continue
                if t is _AC or t is _DC:
                    if stack:
                        v = _close(stack.pop())
                        (stack[-1] if stack else ops).append(v)
                    continue
                (stack[-1] if stack else ops).append(t)
                continue
            n_ops += 1
            if not n_ops & 511 and time.monotonic() >= deadline:
                raise _Timeout()
            while stack:
                v = _close(stack.pop())
                (stack[-1] if stack else ops).append(v)
            k = _OPS.get(t)
            if k is None:
                if ops:
                    ops = []
                continue
            try:
                if k == 0:       # Tj
                    if ops and type(ops[-1]) is bytes:
                        show(ops[-1])
                elif k == 1:     # TJ
                    if ops and type(ops[-1]) is list:
                        for el in ops[-1]:
                            te = type(el)
                            if te is bytes:
                                show(el)
                            elif te is int or te is float:
                                dx = -el * 0.001 * size * th
                                a, b, c, d, e, ff = tm
                                tm = (a, b, c, d, e + dx * a, ff + dx * b)
                elif k == 2 or k == 3:   # Td TD
                    tx, ty = float(ops[-2]), float(ops[-1])
                    if k == 3:
                        tl = -ty
                    a, b, c, d, e, ff = tlm
                    tlm = tm = (a, b, c, d, e + tx * a + ty * c, ff + tx * b + ty * d)
                elif k == 4:     # Tm
                    if len(ops) >= 6:
                        tlm = tm = tuple(float(x) for x in ops[-6:])
                elif k in (5, 6, 7):     # T*  '  "
                    if k == 7:
                        tw, tc = float(ops[-3]), float(ops[-2])
                    a, b, c, d, e, ff = tlm
                    tlm = tm = (a, b, c, d, e - tl * c, ff - tl * d)
                    if k != 5 and ops and type(ops[-1]) is bytes:
                        show(ops[-1])
                elif k == 8:     # Tf
                    if len(ops) >= 2:
                        if type(ops[-1]) is int or type(ops[-1]) is float:
                            size = float(ops[-1])
                        font = self.font(res, ops[-2]) if type(ops[-2]) is Name else None
                elif k == 9:     # BT
                    tm = tlm = _ID
                elif k == 11:
                    tc = float(ops[-1])
                elif k == 12:
                    tw = float(ops[-1])
                elif k == 13:
                    th = float(ops[-1]) / 100.0
                elif k == 14:
                    tl = float(ops[-1])
                elif k == 15:
                    rise = float(ops[-1])
                elif k == 16:    # q
                    if len(saved) < 512:
                        saved.append((ctm, font, size, tc, tw, th, tl, rise))
                elif k == 17:    # Q
                    if saved:
                        ctm, font, size, tc, tw, th, tl, rise = saved.pop()
                elif k == 18:    # cm
                    if len(ops) >= 6:
                        ctm = _mul(tuple(float(x) for x in ops[-6:]), ctm)
                elif k == 19:    # Do
                    if ops and type(ops[-1]) is Name:
                        self._do(ops[-1], res, (ctm, font, size, tc, tw, th, tl, rise), depth, acc)
                elif k == 20:    # BI ... ID <그림 바이트> EI
                    t2 = nxt()
                    while t2 is not _END and not (type(t2) is str and t2[:2] == "ID"):
                        t2 = nxt()
                    if t2 is not _END:
                        p = lex.start + 2
                        if p < len(data) and data[p] in _WS_SET:
                            p += 1
                        m = _RE_EI.search(data, p)
                        lex.pos = m.end() if m else len(data)
                    acc.images += 1
            except _Timeout:
                raise
            except Exception:  # noqa: BLE001 - 연산자 하나가 이상해도 다음으로
                pass
            ops = []

    def _do(self, name, res, gs, depth, acc: _Acc):
        R = self.resolve
        xd = R(res.get("XObject")) if isinstance(res, dict) else None
        if not isinstance(xd, dict):
            return
        ref = xd.get(name)
        x = R(ref)
        if not isinstance(x, Stream):
            return
        sub = R(x.dict.get("Subtype"))
        if sub == "Image":
            acc.images += 1
            return
        if sub != "Form" or depth >= _MAX_FORM_DEPTH:
            return
        key = ref.num if type(ref) is Ref else id(x)
        if key in acc.active:
            return
        mat = _ID
        m = R(x.dict.get("Matrix"))
        if isinstance(m, list) and len(m) == 6:
            vals = [_num(R(v), None) for v in m]
            if None not in vals:
                mat = tuple(vals)
        sub_res = R(x.dict.get("Resources"))
        if not isinstance(sub_res, dict):
            sub_res = res
        data = self.decode(x)
        if not data:
            return
        acc.active.add(key)
        try:
            self.run(data, sub_res, (_mul(mat, gs[0]),) + gs[1:], depth + 1, acc)
        finally:
            acc.active.discard(key)


_OPS = {op: i for i, op in enumerate(
    ["Tj", "TJ", "Td", "TD", "Tm", "T*", "'", '"', "Tf", "BT", "ET",
     "Tc", "Tw", "Tz", "TL", "Ts", "q", "Q", "cm", "Do", "BI"])}


# ---------------------------------------------------------------- 줄 맞추기

def _layout(chunks: list) -> list[str]:
    """글 조각을 방향별로 나누고, 같은 높이끼리 한 줄로 묶어 위에서 아래로 놓는다."""
    if not chunks:
        return []
    groups: dict = {}
    for ch in chunks:
        ma, mb = ch[5], ch[6]
        if abs(ma) >= abs(mb):
            q = 0 if ma >= 0 else 2
        else:
            q = 1 if mb > 0 else 3
        groups.setdefault(q, []).append(ch)
    order = sorted(groups, key=lambda q: -sum(len(c[7]) for c in groups[q]))
    lines: list[str] = []
    for q in order:
        items = []
        for x0, y0, x1, y1, vs, _a, _b, text in groups[q]:
            if q == 0:
                X0, Y, X1 = x0, y0, x1
            elif q == 1:
                X0, Y, X1 = y0, -x0, y1
            elif q == 2:
                X0, Y, X1 = -x0, -y0, -x1
            else:
                X0, Y, X1 = -y0, x0, -y1
            if X1 < X0:
                X0, X1 = X1, X0
            items.append((Y, X0, X1, vs if vs > 0 else 1.0, text))
        lines += _lines(items)
    return lines


def _lines(items: list) -> list[str]:
    items.sort(key=lambda it: -it[0])
    rows: list = []      # [위, 아래, 기준 크기, 조각들]
    for it in items:
        y, s = it[0], it[3]
        top, bot = y + 0.75 * s, y - 0.25 * s
        if rows:
            r = rows[-1]
            if min(top, r[0]) - max(bot, r[1]) >= 0.5 * min(s, r[2]):
                r[3].append(it)
                if s > r[2]:
                    r[0], r[1], r[2] = top, bot, s
                continue
        rows.append([top, bot, s, [it]])
    out = []
    for r in rows:
        line = _join(r[3])
        if line:
            out.append(line)
    return out


def _join(members: list) -> str:
    members.sort(key=lambda it: it[1])
    parts: list[str] = []
    end = None
    seen: dict = {}
    for y, x0, x1, s, text in members:
        lst = seen.get(text)
        if lst is not None:      # 그림자·가짜 굵게: 같은 글자를 살짝 옮겨 두 번 그린 것
            tol = max(0.3 * (x1 - x0), 0.1 * s)
            if any(abs(x0 - px) < tol and abs(y - py) < 0.5 * s for px, py in lst):
                continue
            lst.append((x0, y))
        else:
            seen[text] = [(x0, y)]
        if end is not None and parts:
            gap = x0 - end
            if (gap > 0.15 * s or gap < -0.6 * s) and not parts[-1][-1:].isspace() \
                    and not text[:1].isspace():
                parts.append(" ")
        parts.append(text)
        end = x1 if end is None or x1 > end else end
    return _clean("".join(parts))


def _build_trans() -> dict:
    t: dict = {c: " " for c in range(32)}
    t.update({c: None for c in range(0x7F, 0xA0)})
    t.update({c: None for c in range(0xE000, 0xF900)})       # 사용자 정의 영역(글머리 기호 등)
    t.update({0xA0: " ", 0x3000: " ", 0x2028: " ", 0x2029: " ", 0xAD: None, 0xFEFF: None,
              0x200B: None, 0x200C: None, 0x200D: None, 0x2060: None, 0xFFFD: None,
              0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl",
              0xFB05: "st", 0xFB06: "st"})
    for c in range(0xFF01, 0xFF5F):                           # 전각 숫자·기호 → 반각("１１월" → "11월")
        t[c] = chr(c - 0xFEE0)
    return t


_TRANS = _build_trans()


def _clean(s: str) -> str:
    s = unicodedata.normalize("NFC", s.translate(_TRANS))
    return " ".join(s.split())


def _text_string(b: bytes) -> str:
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", "ignore")
    if b[:2] == b"\xff\xfe":
        return b[2:].decode("utf-16-le", "ignore")
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "ignore")
    return b.decode("latin-1")


# ---------------------------------------------------------------- CMap

class _CMap:
    __slots__ = ("map", "ranges", "space", "parent")

    def __init__(self):
        self.map: dict = {}      # 코드 -> 유니코드 str(ToUnicode) 또는 CID int(인코딩 CMap)
        self.ranges: list = []   # (lo, hi, 시작 바이트 | 시작 CID)
        self.space: list = []    # (바이트 수, lo, hi)
        self.parent = None       # usecmap 이름

    def lookup(self, code: int):
        v = self.map.get(code)
        if v is not None:
            return v
        for lo, hi, base in self.ranges:
            if lo <= code <= hi:
                return base + (code - lo) if type(base) is int else _inc_unicode(base, code - lo)
        return None


_CM_TOK = re.compile(
    rb"<([0-9A-Fa-f\x00\t\n\x0c\r ]*)>"
    rb"|(\[)|(\])"
    rb"|/([^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"
    rb"|(-?\d{1,12})"
    rb"|([A-Za-z][A-Za-z0-9]*)"
    rb"|\(((?:\\[\s\S]|[^\\)])*)\)"
    rb"|%[^\r\n]*")
_CM_MODES = {b"codespacerange", b"bfchar", b"bfrange", b"cidchar", b"cidrange"}


def _u16(b: bytes) -> str:
    if not b:
        return ""
    if len(b) == 1:
        return chr(b[0])
    if len(b) & 1:
        b = b"\x00" + b
    return b.decode("utf-16-be", "ignore")


def _inc_unicode(dst: bytes, j: int) -> str:
    v = int.from_bytes(dst, "big") + j
    try:
        b = v.to_bytes(len(dst), "big")
    except OverflowError:
        b = v.to_bytes(len(dst) + 1, "big")
    return _u16(b)


def _dst_unicode(x):
    if type(x) is bytes:
        return _u16(x)
    if type(x) is Name:
        return _glyph_unicode(str(x))
    return None


def _parse_cmap(data: bytes) -> _CMap:
    cm = _CMap()
    mode = None
    buf: list = []
    arr = None
    last = None
    for m in _CM_TOK.finditer(data):
        h, ao, ac, name, num, kw, lit = m.groups()
        if kw is not None:
            if kw.startswith(b"begin"):
                if kw[5:] in _CM_MODES:
                    mode, buf, arr = kw[5:], [], None
            elif kw.startswith(b"end"):
                if mode is not None and kw[3:] == mode:
                    _cm_apply(cm, mode, buf)
                mode, buf, arr = None, [], None
            elif kw == b"usecmap" and last is not None:
                cm.parent = last
            continue
        if ao is not None:
            arr = []
            continue
        if ac is not None:
            tok, arr = (arr if arr is not None else []), None
        elif h is not None:
            tok = _hex(h)
        elif lit is not None:
            tok = _unescape(lit)
        elif name is not None:
            tok = _name(name)
            last = str(tok)
        elif num is not None:
            tok = int(num)
        else:
            continue
        if arr is not None:
            if len(arr) < 65536:
                arr.append(tok)
        elif mode is not None and len(buf) < 600_000:
            buf.append(tok)
    return cm


def _cm_apply(cm: _CMap, mode: bytes, buf: list):
    def code(b):
        return int.from_bytes(b, "big") if type(b) is bytes and 0 < len(b) <= 4 else None

    if mode == b"codespacerange":
        for i in range(0, len(buf) - 1, 2):
            lo, hi = code(buf[i]), code(buf[i + 1])
            if lo is not None and hi is not None:
                cm.space.append((len(buf[i]), lo, hi))
        cm.space.sort()
    elif mode == b"bfchar":
        for i in range(0, len(buf) - 1, 2):
            src = code(buf[i])
            u = _dst_unicode(buf[i + 1])
            if src is not None and u is not None:
                cm.map[src] = u
    elif mode == b"bfrange":
        for i in range(0, len(buf) - 2, 3):
            lo, hi, dst = code(buf[i]), code(buf[i + 1]), buf[i + 2]
            if lo is None or hi is None or hi < lo:
                continue
            if type(dst) is list:
                for j, x in enumerate(dst[:hi - lo + 1]):
                    u = _dst_unicode(x)
                    if u is not None:
                        cm.map[lo + j] = u
            elif type(dst) is bytes and dst:
                if hi - lo < 256:
                    for j in range(hi - lo + 1):
                        cm.map[lo + j] = _inc_unicode(dst, j)
                else:
                    cm.ranges.append((lo, hi, dst))
    elif mode == b"cidchar":
        for i in range(0, len(buf) - 1, 2):
            src = code(buf[i])
            if src is not None and type(buf[i + 1]) is int:
                cm.map[src] = buf[i + 1]
    elif mode == b"cidrange":
        for i in range(0, len(buf) - 2, 3):
            lo, hi, cid = code(buf[i]), code(buf[i + 1]), buf[i + 2]
            if lo is not None and hi is not None and type(cid) is int and lo <= hi:
                cm.ranges.append((lo, hi, cid))


# ---------------------------------------------------------------- 글꼴

def _lead_cjk(b: int) -> bool:
    return b >= 0x81


def _lead_sjis(b: int) -> bool:
    return 0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC


def _cmap_codec(name: str):
    """이름 붙은 CJK CMap → (파이썬 코덱, 코드 자르는 방식)."""
    n = name
    if "UCS2" in n or "UTF16" in n:
        return "utf-16-be", "cid2"
    if n.startswith("KSC") or "UHC" in n:
        return ("johab" if "Johab" in n else "cp949"), "lead"
    if "RKSJ" in n:
        return "cp932", "sjis"
    if n.startswith(("GBK2K", "GB18030")):
        return "gb18030", "lead"
    if n.startswith(("GB", "GBK", "GBpc", "GBT")):
        return "gbk", "lead"
    if n.startswith(("B5", "ETen", "HKscs", "ETHK", "HKdl", "HKgccs", "HKm", "CNS")):
        return ("big5hkscs" if n.startswith(("HK", "ETHK")) else "cp950"), "lead"
    if n.startswith(("EUC-", "83pv", "90pv", "Ext-", "Add-")):
        return "euc_jp", "lead"
    return None, "cid2"


class _Font:
    """글자 코드를 유니코드와 너비로 바꾼다. fd가 없으면 ASCII만 읽는 대체 글꼴."""

    def __init__(self, doc: _Doc | None = None, fd: dict | None = None):
        self.missing = 0             # 유니코드를 모르는 글자 수
        self.cache: dict = {}
        self.scale = 0.001           # 글리프 너비 → 글자 공간
        self.vscale = 1.0
        self.tu = None
        self.mode = "simple"         # simple | cid2 | lead | sjis | space
        self.enc = _ASCII
        self.widths: dict = {}
        self.wranges: list = []
        self.dw = 500.0
        self.std = None
        self.codec = None
        self.cid = None
        self.space = None
        self.identity = False
        self.blank = False
        self.fallback = fd is None
        if fd is None:
            return
        R = doc.resolve
        sub = R(fd.get("Subtype"))
        base = R(fd.get("BaseFont"))
        base = str(base) if isinstance(base, str) else ""
        tu = R(fd.get("ToUnicode"))
        if isinstance(tu, Stream):
            data = doc.decode(tu)
            if data:
                cm = _parse_cmap(data)
                if cm.map or cm.ranges:
                    self.tu = cm
        if sub == "Type0":
            self._type0(doc, fd)
        else:
            self._simple(doc, fd, sub, base)

    def _type0(self, doc: _Doc, fd: dict):
        R = doc.resolve
        desc = R(fd.get("DescendantFonts"))
        if isinstance(desc, list) and desc:
            desc = R(desc[0])
        if not isinstance(desc, dict):
            desc = {}
        self.dw = _num(R(desc.get("DW")), 1000.0)
        self.widths, self.wranges = _parse_w(R(desc.get("W")), R)
        enc = R(fd.get("Encoding"))
        name = None
        if isinstance(enc, Stream):
            data = doc.decode(enc)
            cm = _parse_cmap(data) if data else None
            if cm is not None:
                self.cid = cm if (cm.map or cm.ranges) else None
                self.space = cm.space or None
                name = cm.parent
        elif isinstance(enc, str):
            name = str(enc)
        self.mode = "cid2"
        if name and not name.startswith("Identity"):
            self.codec, self.mode = _cmap_codec(name)
        if self.space:
            self.mode = "space"
        self.identity = self.cid is None and (not name or name.startswith("Identity"))

    def _simple(self, doc: _Doc, fd: dict, sub, base: str):
        R = doc.resolve
        if sub == "Type3":
            fm = R(fd.get("FontMatrix"))
            if isinstance(fm, list) and len(fm) >= 4:
                a, d = _num(R(fm[0]), 0.001), _num(R(fm[3]), 0.001)
                if a:
                    self.scale = a
                if d:
                    self.vscale = abs(d) / 0.001
        first = int(_num(R(fd.get("FirstChar")), 0))
        ws = R(fd.get("Widths"))
        if isinstance(ws, list):
            for i, x in enumerate(ws[:256]):
                v = _num(R(x), None)
                if v is not None:
                    self.widths[first + i] = v
        desc = R(fd.get("FontDescriptor"))
        mw = _num(R(desc.get("MissingWidth")), None) if isinstance(desc, dict) else None
        low = base.lower()
        if self.widths:
            self.dw = mw if mw is not None else 0.0
        elif "courier" in low:
            self.dw = 600.0
        else:                        # 기본 14 글꼴: Helvetica 너비로 어림한다
            self.std = _HELV
            self.dw = mw if mw is not None else 556.0
        if self.tu is None and any(k in low for k in ("wingding", "webding", "dingbat", "marlett")):
            self.blank = True        # 글머리 기호 글꼴
        enc = R(fd.get("Encoding"))
        table = None
        diffs = None
        if isinstance(enc, str):
            table = _NAMED_ENC.get(str(enc))
        elif isinstance(enc, dict):
            be = R(enc.get("BaseEncoding"))
            if isinstance(be, str):
                table = _NAMED_ENC.get(str(be))
            diffs = R(enc.get("Differences"))
        if table is None:
            table = _WINANSI if sub == "TrueType" else _STANDARD
        if isinstance(diffs, list):
            table = list(table)
            code = 0
            for x in diffs[:2048]:
                x = R(x)
                if type(x) is int or type(x) is float:
                    code = int(x)
                elif isinstance(x, str):
                    if 0 <= code < 256:
                        table[code] = _glyph_unicode(str(x))
                    code += 1
        self.enc = table

    def _codes(self, s: bytes) -> list:
        m = self.mode
        n = len(s)
        if m == "cid2":
            return [((s[i] << 8) | s[i + 1], 2) for i in range(0, n - 1, 2)]
        out = []
        i = 0
        if m in ("lead", "sjis"):
            lead = _lead_sjis if m == "sjis" else _lead_cjk
            while i < n:
                b = s[i]
                if i + 1 < n and lead(b):
                    out.append(((b << 8) | s[i + 1], 2))
                    i += 2
                else:
                    out.append((b, 1))
                    i += 1
            return out
        space = self.space
        short = space[0][0]
        while i < n:
            for nb, lo, hi in space:
                if i + nb <= n:
                    c = int.from_bytes(s[i:i + nb], "big")
                    if lo <= c <= hi:
                        break
            else:
                nb = short
                c = int.from_bytes(s[i:i + nb], "big")
            out.append((c, nb))
            i += nb
        return out

    def decode(self, s: bytes):
        """(글자, 너비 합(글리프 단위), 글리프 수, 1바이트 빈칸 수)."""
        cache = self.cache
        look = self._lookup
        parts = []
        w = 0.0
        miss = 0
        if self.mode == "simple":
            for c in s:
                e = cache.get(c)
                if e is None:
                    e = cache[c] = look(c, 1)
                u, cw = e
                w += cw
                if u is None:
                    miss += 1
                elif u:
                    parts.append(u)
            ng, nsp = len(s), s.count(32)
        else:
            codes = self._codes(s)
            ng, nsp = len(codes), 0
            for c, nb in codes:
                key = c | (nb << 32)
                e = cache.get(key)
                if e is None:
                    e = cache[key] = look(c, nb)
                u, cw = e
                w += cw
                if c == 32 and nb == 1:
                    nsp += 1
                if u is None:
                    miss += 1
                elif u:
                    parts.append(u)
        self.missing += miss
        return "".join(parts), w, ng, nsp

    def _lookup(self, code: int, nb: int):
        u = None
        if self.tu is not None:
            v = self.tu.lookup(code)
            if type(v) is str:
                u = v
        if u is None:
            if self.mode == "simple":
                if self.blank:
                    u = ""
                elif 0 <= code < 256:
                    u = self.enc[code]
            elif self.codec:
                try:
                    u = code.to_bytes(nb, "big").decode(self.codec) or None
                except Exception:  # noqa: BLE001
                    u = None
        if self.mode == "simple":
            cw = self.widths.get(code)
            if cw is None:
                std = self.std
                cw = std[code - 32] if std is not None and 32 <= code < 127 else self.dw
        else:
            cid = code if self.identity else (self.cid.lookup(code) if self.cid is not None else None)
            cw = None
            if type(cid) is int:
                cw = self.widths.get(cid)
                if cw is None:
                    for lo, hi, v in self.wranges:
                        if lo <= cid <= hi:
                            cw = v
                            break
            if cw is None:
                cw = self.dw
        return u, cw


def _parse_w(w, R):
    widths: dict = {}
    ranges: list = []
    if not isinstance(w, list):
        return widths, ranges
    i, n = 0, len(w)
    while i < n and len(widths) < 200_000:
        a = _num(R(w[i]), None)
        if a is None:
            i += 1
            continue
        b = R(w[i + 1]) if i + 1 < n else None
        if isinstance(b, list):
            for j, x in enumerate(b[:65536]):
                v = _num(R(x), None)
                if v is not None:
                    widths[int(a) + j] = v
            i += 2
        elif i + 2 < n:
            hi, v = _num(b, None), _num(R(w[i + 2]), None)
            if hi is not None and v is not None:
                ranges.append((int(a), int(hi), v))
            i += 3
        else:
            break
    return widths, ranges


# ---------------------------------------------------------------- 인코딩 표

def _ascii_table() -> list:
    t: list = [None] * 256
    for c in range(32, 127):
        t[c] = chr(c)
    return t


_ASCII = tuple(_ascii_table())


def _codepage_table(codec: str) -> tuple:
    t = _ascii_table()
    for c in range(128, 256):
        try:
            t[c] = bytes([c]).decode(codec)
        except UnicodeDecodeError:
            t[c] = None
    return tuple(t)


def _standard_table() -> tuple:
    t = _ascii_table()
    t[0x27], t[0x60] = "\u2019", "\u2018"
    upper = {
        0xA1: "¡", 0xA2: "¢", 0xA3: "£", 0xA4: "\u2044", 0xA5: "¥", 0xA6: "ƒ", 0xA7: "§",
        0xA8: "¤", 0xA9: "'", 0xAA: "\u201c", 0xAB: "«", 0xAC: "\u2039", 0xAD: "\u203a",
        0xAE: "\ufb01", 0xAF: "\ufb02", 0xB1: "\u2013", 0xB2: "\u2020", 0xB3: "\u2021",
        0xB4: "·", 0xB6: "¶", 0xB7: "\u2022", 0xB8: "\u201a", 0xB9: "\u201e", 0xBA: "\u201d",
        0xBB: "»", 0xBC: "\u2026", 0xBD: "\u2030", 0xBF: "¿", 0xC1: "`", 0xC2: "´",
        0xC3: "\u02c6", 0xC4: "\u02dc", 0xC5: "¯", 0xC6: "\u02d8", 0xC7: "\u02d9", 0xC8: "¨",
        0xCA: "\u02da", 0xCB: "¸", 0xCD: "\u02dd", 0xCE: "\u02db", 0xCF: "\u02c7",
        0xD0: "\u2014", 0xE1: "Æ", 0xE3: "ª", 0xE8: "Ł", 0xE9: "Ø", 0xEA: "Œ", 0xEB: "º",
        0xF1: "æ", 0xF5: "ı", 0xF8: "ł", 0xF9: "ø", 0xFA: "œ", 0xFB: "ß",
    }
    for k, v in upper.items():
        t[k] = v
    return tuple(t)


def _winansi_table() -> tuple:
    t = list(_codepage_table("cp1252"))
    t[0xA0], t[0xAD] = " ", "-"
    return tuple(t)


_STANDARD = _standard_table()
_WINANSI = _winansi_table()
_MACROMAN = _codepage_table("mac_roman")
_NAMED_ENC = {"WinAnsiEncoding": _WINANSI, "MacRomanEncoding": _MACROMAN,
              "StandardEncoding": _STANDARD, "PDFDocEncoding": _WINANSI,
              "MacExpertEncoding": _STANDARD}

# Helvetica 너비(코드 32~126). /Widths가 없는 기본 14 글꼴의 글자 끝 위치를 어림하는 데만 쓴다.
_HELV = tuple(float(x) for x in (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584").split())

_GLYPH_SPEC = (
    "space 20 exclam 21 quotedbl 22 numbersign 23 dollar 24 percent 25 ampersand 26 quotesingle 27 "
    "parenleft 28 parenright 29 asterisk 2a plus 2b comma 2c hyphen 2d period 2e slash 2f "
    "zero 30 one 31 two 32 three 33 four 34 five 35 six 36 seven 37 eight 38 nine 39 "
    "colon 3a semicolon 3b less 3c equal 3d greater 3e question 3f at 40 bracketleft 5b "
    "backslash 5c bracketright 5d asciicircum 5e underscore 5f grave 60 braceleft 7b bar 7c "
    "braceright 7d asciitilde 7e nbspace a0 nonbreakingspace a0 exclamdown a1 cent a2 "
    "sterling a3 currency a4 yen a5 brokenbar a6 section a7 dieresis a8 copyright a9 "
    "ordfeminine aa guillemotleft ab logicalnot ac sfthyphen ad registered ae macron af "
    "degree b0 plusminus b1 twosuperior b2 threesuperior b3 acute b4 mu b5 paragraph b6 "
    "periodcentered b7 middot b7 cedilla b8 onesuperior b9 ordmasculine ba guillemotright bb "
    "onequarter bc onehalf bd threequarters be questiondown bf AE c6 Eth d0 multiply d7 "
    "Oslash d8 Thorn de germandbls df ae e6 eth f0 divide f7 oslash f8 thorn fe "
    "dotlessi 131 Lslash 141 lslash 142 OE 152 oe 153 florin 192 circumflex 2c6 caron 2c7 "
    "breve 2d8 dotaccent 2d9 ring 2da ogonek 2db tilde 2dc hungarumlaut 2dd endash 2013 "
    "emdash 2014 quoteleft 2018 quoteright 2019 quotesinglbase 201a quotedblleft 201c "
    "quotedblright 201d quotedblbase 201e dagger 2020 daggerdbl 2021 bullet 2022 "
    "ellipsis 2026 perthousand 2030 guilsinglleft 2039 guilsinglright 203a fraction 2044 "
    "Euro 20ac trademark 2122 minus 2212 arrowleft 2190 arrowup 2191 arrowright 2192 "
    "arrowdown 2193 lessequal 2264 greaterequal 2265 notequal 2260 approxequal 2248 "
    "infinity 221e visiblespace 2423 fi fb01 fl fb02 ff fb00 ffi fb03 ffl fb04 dotlessj 237 "
    "Alpha 391 Beta 392 Gamma 393 Delta 394 Epsilon 395 Zeta 396 Eta 397 Theta 398 Iota 399 "
    "Kappa 39a Lambda 39b Mu 39c Nu 39d Xi 39e Omicron 39f Pi 3a0 Rho 3a1 Sigma 3a3 Tau 3a4 "
    "Upsilon 3a5 Phi 3a6 Chi 3a7 Psi 3a8 Omega 3a9 alpha 3b1 beta 3b2 gamma 3b3 delta 3b4 "
    "epsilon 3b5 zeta 3b6 eta 3b7 theta 3b8 iota 3b9 kappa 3ba lambda 3bb nu 3bd xi 3be "
    "omicron 3bf pi 3c0 rho 3c1 sigma1 3c2 sigma 3c3 tau 3c4 upsilon 3c5 phi 3c6 chi 3c7 "
    "psi 3c8 omega 3c9")
_GLYPHS = {k: chr(int(v, 16)) for k, v in zip(*[iter(_GLYPH_SPEC.split())] * 2)}
_ACCENTS = {"acute": "\u0301", "grave": "\u0300", "circumflex": "\u0302", "tilde": "\u0303",
            "dieresis": "\u0308", "ring": "\u030a", "cedilla": "\u0327", "caron": "\u030c",
            "macron": "\u0304", "breve": "\u0306", "ogonek": "\u0328", "dotaccent": "\u0307",
            "hungarumlaut": "\u030b", "commaaccent": "\u0326"}
_RE_UNI = re.compile(r"uni((?:[0-9A-Fa-f]{4})+)")
_RE_U = re.compile(r"u([0-9A-Fa-f]{4,6})")


def _glyph_unicode(name: str, depth: int = 0):
    """글리프 이름(/eacute, /uni00E9, /f_f, /one.oldstyle) → 유니코드. 모르면 None."""
    u = _GLYPHS.get(name)
    if u is not None:
        return u
    base = name.split(".", 1)[0]
    if not base:
        return None
    if "_" in base and depth == 0:
        parts = [_glyph_unicode(p, 1) for p in base.split("_")]
        return "".join(parts) if parts and None not in parts else None
    if base != name:
        u = _GLYPHS.get(base)
        if u is not None:
            return u
    if len(base) == 1:
        return base if base.isprintable() and base != " " else None
    m = _RE_UNI.fullmatch(base)
    if m:
        h = m.group(1)
        cps = [int(h[i:i + 4], 16) for i in range(0, len(h), 4)]
        return "".join(chr(c) for c in cps if not 0xD800 <= c <= 0xDFFF) or None
    m = _RE_U.fullmatch(base)
    if m:
        c = int(m.group(1), 16)
        return chr(c) if c <= 0x10FFFF and not 0xD800 <= c <= 0xDFFF else None
    if base[0].isascii() and base[0].isalpha() and base[1:] in _ACCENTS:
        return unicodedata.normalize("NFC", base[0] + _ACCENTS[base[1:]])
    return None
