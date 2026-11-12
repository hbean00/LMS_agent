"""pdftext: 표준 라이브러리만으로 PDF 글자 꺼내기.

PDF는 테스트 안에서 손으로 만든다(라이브러리 없음). 기준일 REF = 2026-11-12(목) 10:00 +09:00.
"""
import base64
import random
import time
import zlib
from datetime import datetime, timedelta, timezone

from lms_agent.extract import find_deadlines
from lms_agent.pdftext import PAGE_SEP, PdfTextResult, extract_text

KST = timezone(timedelta(hours=9))
REF = datetime(2026, 11, 12, 10, 0, tzinfo=KST)

CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
HELV = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"


# ---------------------------------------------------------------- 손으로 만드는 PDF

def build_pdf(objs, *, root=1, trailer_extra=b"", shift=0, startxref_shift=0, xref=True):
    """objs: {번호: 본문}. 본문이 (dict 안쪽, 데이터)면 스트림으로 쓰고 /Length를 붙인다.

    shift: xref에 적는 위치를 일부러 틀리게, xref=False: xref·trailer를 아예 쓰지 않는다.
    """
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for num in sorted(objs):
        offs[num] = len(out)
        body = objs[num]
        out += b"%d 0 obj\n" % num
        if isinstance(body, tuple):
            d, data = body
            out += b"<< " + d + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"
        else:
            out += body
        out += b"\nendobj\n"
    if xref:
        size = max(objs) + 1
        xoff = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % size
        for i in range(1, size):
            out += (b"%010d 00000 n \n" % (offs[i] + shift)) if i in offs else b"0000000000 65535 f \n"
        out += b"trailer\n<< /Size %d /Root %d 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (
            size, root, trailer_extra, xoff + startxref_shift)
    return bytes(out)


def text_doc(contents, *, font=HELV, extra=None, **kw):
    """1 카탈로그, 2 쪽 묶음(글꼴 자원을 물려줌), 3 글꼴, 4부터 쪽·내용. 첫 쪽은 자원을 직접 갖는다."""
    objs = {1: CATALOG, 3: font}
    kids = []
    for i, content in enumerate(contents):
        p, c = 4 + 2 * i, 5 + 2 * i
        kids.append(b"%d 0 R" % p)
        own = b" /Resources << /Font << /F1 3 0 R >> >>" if i == 0 else b""
        objs[p] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]%s /Contents %d 0 R >>" % (own, c)
        objs[c] = content if isinstance(content, tuple) else (b"", content)
    objs[2] = (b"<< /Type /Pages /Kids [" + b" ".join(kids)
               + b"] /Count %d /Resources << /Font << /F1 3 0 R >> >> >>" % len(kids))
    objs.update(extra or {})
    return build_pdf(objs, **kw)


def page_doc(content, *, resources=b"<< /Font << /F1 5 0 R >> >>", extra=None, page_extra=b"", **kw):
    """1 카탈로그, 2 쪽 묶음, 3 쪽, 4 내용, 5부터 extra."""
    objs = {1: CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources " + resources
               + page_extra + b" /Contents 4 0 R >>",
            4: content if isinstance(content, tuple) else (b"", content)}
    objs.update(extra or {})
    return build_pdf(objs, **kw)


def lines_of(r: PdfTextResult, page=0):
    return r.text.split(PAGE_SEP)[page].split("\n")


# ---------------------------------------------------------------- (1) Helvetica, Tj/TJ, 두 쪽

PAGE1 = rb"""BT /F1 12 Tf 72 600 Td (bottom line) Tj ET
BT
/F1 12 Tf
14 TL
72 720 Td
(Hello World) Tj
T*
[(A) 120 (WAY) -300 (team)] TJ
T*
(Due:) Tj
40 0 Td
(Nov. 18, 23:59) Tj
ET
BT /F1 12 Tf 72 650 Td (Caf\351 \(paren\) back\\slash \101) Tj ET
"""
PAGE2 = rb'''BT /F1 10 Tf 12 TL 72 700 Td (Second page) Tj (via quote) ' 1 0 (via dquote) " ET'''
PAGE1_LINES = ["Hello World", "AWAY team", "Due: Nov. 18, 23:59", "Café (paren) back\\slash A", "bottom line"]
PAGE2_LINES = ["Second page", "via quote", "via dquote"]


def test_simple_helvetica_two_pages():
    r = extract_text(text_doc([PAGE1, PAGE2]))
    assert r.ok and r.pages == 2 and r.notes == []
    assert r.text == "\n".join(PAGE1_LINES) + PAGE_SEP + "\n".join(PAGE2_LINES)


def test_overprinted_shadow_is_read_once():
    content = (b"BT /F1 12 Tf 73 699 Td (Shadow text) Tj ET "
               b"BT /F1 12 Tf 72 700 Td (Shadow text) Tj ET")
    r = extract_text(page_doc(content, extra={5: HELV}))
    assert r.text == "Shadow text"


def test_rotated_text_stays_one_line():
    content = (b"BT /F1 12 Tf 0 1 -1 0 300 100 Tm (Rotated text) Tj ET "
               b"BT /F1 12 Tf 72 700 Td (Upright) Tj ET")
    r = extract_text(page_doc(content, extra={5: HELV}))
    assert sorted(r.text.split("\n")) == ["Rotated text", "Upright"]


# ---------------------------------------------------------------- (2) Type0 / Identity-H 한글 + 마감 찾기

LINE1 = "Lab3 마감: 11월 18일(수) 23:59"
LINE2 = ["Due:", "23:59", " on 11/18"]


def korean_doc():
    special = set("0123456789:() ")
    chars = sorted(set(LINE1 + "".join(LINE2)) - special)
    cid = {" ": 3, ":": 0x30, "(": 0x31, ")": 0x32}
    cid.update({d: 0x10 + i for i, d in enumerate("0123456789")})
    cid.update({ch: 0x40 + i for i, ch in enumerate(chars)})

    def enc(s):
        return b"<" + "".join("%04X" % cid[ch] for ch in s).encode() + b">"

    bfchar = b"".join(b"<%04X> <%s>\n" % (cid[ch], ch.encode("utf-16-be").hex().upper().encode())
                      for ch in chars + [" "])
    tounicode = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                 b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                 b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
                 b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
                 + b"%d beginbfchar\n" % (len(chars) + 1) + bfchar + b"endbfchar\n"
                 b"2 beginbfrange\n<0010> <0019> <0030>\n<0030> <0032> [<003A> <0028> <0029>]\nendbfrange\n"
                 b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    content = (b"BT /F1 12 Tf 72 720 Td " + enc(LINE1) + b" Tj 0 -16 Td ["
               + enc(LINE2[0]) + b" -400 " + enc(LINE2[1]) + b" 50 " + enc(LINE2[2]) + b"] TJ ET")
    type0 = (b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+MalgunGothic /Encoding /Identity-H "
             b"/DescendantFonts [6 0 R] /ToUnicode 7 0 R >>")
    cidfont = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+MalgunGothic "
               b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
               b"/DW 1000 /W [3 [300] 16 25 550 48 [300 330 330]] /CIDToGIDMap /Identity >>")
    return page_doc(content, extra={5: type0, 6: cidfont, 7: (b"", tounicode)})


def test_identity_h_korean_tounicode():
    r = extract_text(korean_doc())
    assert r.ok and r.notes == []
    assert lines_of(r) == [LINE1, "Due: 23:59 on 11/18"]


def test_deadline_found_in_korean_pdf():
    r = extract_text(korean_doc())
    found = find_deadlines(r.text, REF)
    assert any(f.due == datetime(2026, 11, 18, 23, 59, tzinfo=KST) for f in found), found


# ---------------------------------------------------------------- (3) 압축된 내용 스트림, 1바이트 ToUnicode, Differences

def test_flate_content_and_one_byte_tounicode():
    word = "Report due 11/20"
    codes = {ch: i + 1 for i, ch in enumerate(sorted(set(word)))}
    tu = (b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n%d beginbfchar\n" % len(codes)
          + b"".join(b"<%02X> <%04X>\n" % (c, ord(ch)) for ch, c in codes.items()) + b"endbfchar\n")
    hexword = "".join("%02X" % codes[ch] for ch in word).encode()
    content = (b"/Filter /FlateDecode", zlib.compress(b"BT /F1 11 Tf 50 500 Td <" + hexword + b"> Tj ET"))
    font = (b"<< /Type /Font /Subtype /TrueType /BaseFont /ABCDEF+Calibri /FirstChar 1 /LastChar %d "
            b"/Widths [%s] /ToUnicode 6 0 R >>" % (len(codes), b" ".join(b"500" for _ in codes)))
    r = extract_text(page_doc(content, extra={5: font, 6: (b"", tu)}))
    assert r.ok and r.text == word and r.notes == []


def test_differences_and_glyph_names():
    font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+CMR10 /Encoding << /Type /Encoding "
            b"/BaseEncoding /WinAnsiEncoding /Differences [1 /D /u /e /colon /space /uni00E9 /f_f "
            b"/eacute /one.oldstyle /quoteright /g123] >> >>")
    r = extract_text(page_doc(b"BT /F1 12 Tf 72 700 Td <0102030405060708090A0B> Tj ET", extra={5: font}))
    assert r.text == "Due: éffé1’"
    assert any("글꼴 1개" in n for n in r.notes)     # /g123은 모르는 글리프


def test_other_filters_ascii85_asciihex_lzw():
    body = b"BT /F1 12 Tf 72 700 Td (Filters work) Tj ET"
    a85 = (b"/Filter [/ASCII85Decode /FlateDecode]", base64.a85encode(zlib.compress(body), adobe=True))
    ahx = (b"/Filter [/ASCIIHexDecode /FlateDecode]", zlib.compress(body).hex().encode() + b">")
    long_body = b"BT /F1 12 Tf 14 TL 72 760 Td " + b"".join(b"(line %d of lzw text) ' " % i for i in range(60)) + b"ET"
    lzw = (b"/Filter /LZWDecode", lzw_encode(long_body))
    for content, want in ((a85, "Filters work"), (ahx, "Filters work")):
        assert extract_text(page_doc(content, extra={5: HELV})).text == want
    r = extract_text(page_doc(lzw, extra={5: HELV}))
    assert r.text.split("\n") == ["line %d of lzw text" % i for i in range(60)]


def lzw_encode(data: bytes) -> bytes:
    """테스트용 LZW 인코더(EarlyChange 1). 코드 너비는 디코더가 표를 늘리는 시점에 맞춘다."""
    table = {bytes([i]): i for i in range(256)}
    codes = [256]
    w = b""
    for b in data:
        wc = w + bytes([b])
        if wc in table:
            w = wc
        else:
            codes.append(table[w])
            table[wc] = len(table) + 2
            w = bytes([b])
    codes += [table[w], 257]
    out = bytearray()
    bits, dec_len, first, acc, nacc = 9, 258, True, 0, 0
    for c in codes:
        acc = (acc << bits) | c
        nacc += bits
        while nacc >= 8:
            nacc -= 8
            out.append((acc >> nacc) & 255)
        acc &= (1 << nacc) - 1
        if c == 256:
            bits, dec_len, first = 9, 258, True
        elif first:
            first = False
        else:
            dec_len += 1
            if dec_len + 1 >= (1 << bits) and bits < 12:
                bits += 1
    if nacc:
        out.append((acc << (8 - nacc)) & 255)
    assert max(codes) >= 512, "코드 너비가 바뀌는 길이여야 한다"
    return bytes(out)


# ---------------------------------------------------------------- (4) xref 스트림 + 객체 스트림

def xref_stream_doc():
    members = {
        1: CATALOG,
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        4: HELV,
    }
    body, head = b"", []
    for num, src in members.items():
        head.append(b"%d %d" % (num, len(body)))
        body += src + b"\n"
    head = b" ".join(head) + b"\n"
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    pos = {}
    content = zlib.compress(b"BT /F1 12 Tf 72 700 Td (Object stream works) Tj ET")
    pos[5] = len(out)
    out += (b"5 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content)
            + content + b"\nendstream\nendobj\n")
    stm = zlib.compress(head + body)
    pos[6] = len(out)
    out += (b"6 0 obj\n<< /Type /ObjStm /N 4 /First %d /Length %d /Filter /FlateDecode >>\nstream\n"
            % (len(head), len(stm)) + stm + b"\nendstream\nendobj\n")
    xoff = len(out)
    rows = [(0, 0, 65535)] + [(2, 6, i) for i in range(4)] + [(1, pos[5], 0), (1, pos[6], 0), (1, xoff, 0)]
    raw = b"".join(bytes([t]) + a.to_bytes(4, "big") + b.to_bytes(2, "big") for t, a, b in rows)
    prev, pred = bytes(7), bytearray()          # PNG Up 예측자(Word·PowerPoint가 쓰는 방식)
    for i in range(0, len(raw), 7):
        row = raw[i:i + 7]
        pred += b"\x02" + bytes((x - p) & 255 for x, p in zip(row, prev))
        prev = row
    xs = zlib.compress(bytes(pred))
    out += (b"7 0 obj\n<< /Type /XRef /Size 8 /W [1 4 2] /Root 1 0 R /Filter /FlateDecode "
            b"/DecodeParms << /Predictor 12 /Columns 7 >> /Length %d >>\nstream\n" % len(xs)
            + xs + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xoff)
    return bytes(out)


def test_xref_stream_and_object_stream():
    r = extract_text(xref_stream_doc())
    assert r.ok and r.text == "Object stream works"
    assert r.notes == []        # 훑어 읽기(복구)로 넘어가지 않았다


# ---------------------------------------------------------------- (5) Form XObject

def test_form_xobject_nested_and_self_reference():
    form1 = (b"/Type /XObject /Subtype /Form /BBox [0 0 300 300] /Matrix [1 0 0 1 36 300] "
             b"/Resources << /Font << /F1 5 0 R >> /XObject << /Fm2 7 0 R /Fm1 6 0 R >> >>",
             b"BT /F1 10 Tf 0 0 Td (Inside form) Tj ET /Fm2 Do /Fm1 Do")
    form2 = (b"/Type /XObject /Subtype /Form /BBox [0 0 300 300]",
             b"BT /F1 10 Tf 0 -20 Td (Nested form) Tj ET")
    content = b"q 2 0 0 2 0 0 cm /Fm1 Do Q BT /F1 12 Tf 72 300 Td (After form) Tj ET"
    pdf = page_doc(content, resources=b"<< /Font << /F1 5 0 R >> /XObject << /Fm1 6 0 R >> >>",
                   extra={5: HELV, 6: form1, 7: form2})
    r = extract_text(pdf)
    assert r.text.split("\n") == ["Inside form", "Nested form", "After form"]


# ---------------------------------------------------------------- (6) 깨진 xref

def test_wrong_xref_offsets_still_extract():
    r = extract_text(text_doc([PAGE1, PAGE2], shift=7, startxref_shift=3))
    assert r.ok and r.pages == 2
    assert lines_of(r) == PAGE1_LINES
    assert any("목차" in n for n in r.notes)


def test_missing_xref_and_trailer_still_extract():
    r = extract_text(text_doc([PAGE1, PAGE2], xref=False))
    assert r.ok and r.pages == 2
    assert lines_of(r, 1) == PAGE2_LINES


# ---------------------------------------------------------------- (7) 암호화

def test_encrypted_pdf_is_reported():
    enc = b"<< /Filter /Standard /V 1 /R 2 /O <" + b"00" * 32 + b"> /U <" + b"00" * 32 + b"> /P -44 >>"
    pdf = text_doc([PAGE1], extra={20: enc},
                   trailer_extra=b"/Encrypt 20 0 R /ID [<0123456789ABCDEF> <0123456789ABCDEF>] ")
    r = extract_text(pdf)
    assert not r.ok and r.text == "" and r.pages == 1
    assert r.notes == ["암호화된 PDF라 읽지 못했습니다"]


# ---------------------------------------------------------------- (8) 쓰레기·잘린 파일

def test_garbage_never_raises():
    rng = random.Random(0)
    junk = [
        b"", b"   ", b"%PDF-1.4\n", bytes(rng.randrange(256) for _ in range(5000)),
        b"%PDF-1.7\n1 0 obj\n" + b"[" * 100_000,
        b"%PDF-1.7\n1 0 obj\n" + b"<<" * 50_000,
        b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [2 0 R 1 0 R] >> endobj\ntrailer << /Root 1 0 R >>",
        b"%PDF-1.4\n1 0 obj << /Length 99999999999999999999999999 >> stream\nabc\nendstream endobj",
        b"%PDF-1.4\n4 0 obj << /Length 4 0 R >> stream\n" + b"x" * 100 + b"\nendstream endobj",
        b"%PDF-1.4\nstartxref\n999999999\n%%EOF",
        b"%PDF-1.4\nxref\n0 99999999\ntrailer << /Root 1 0 R /Prev 9 >>\nstartxref\n9\n%%EOF",
        b"%PDF-1.4\n" + b"9" * 10_000 + b" 0 obj",
    ]
    for data in junk:
        t0 = time.monotonic()
        r = extract_text(data)
        assert isinstance(r, PdfTextResult)
        assert not r.ok and r.notes, (data[:40], r)
        assert time.monotonic() - t0 < 3
    assert not extract_text(None).ok            # 바이트가 아니어도 죽지 않는다
    assert not extract_text("문자열").ok


def test_truncated_and_bitflipped_files_never_raise():
    good = text_doc([(b"/Filter /FlateDecode", zlib.compress(PAGE1)), PAGE2])
    t0 = time.monotonic()
    for cut in range(0, len(good), 17):
        assert isinstance(extract_text(good[:cut], time_budget=2), PdfTextResult)
    rng = random.Random(1)
    for _ in range(150):
        b = bytearray(good)
        for _ in range(rng.randrange(1, 6)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        assert isinstance(extract_text(bytes(b), time_budget=2), PdfTextResult)
    assert time.monotonic() - t0 < 60


# ---------------------------------------------------------------- (9) 그림뿐인 쪽, 인라인 그림

def test_image_only_page_is_not_ok():
    img = (b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
           b"/BitsPerComponent 8 /Filter /DCTDecode", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xd9")
    pdf = page_doc(b"q 612 0 0 792 0 0 cm /Im1 Do Q", resources=b"<< /XObject << /Im1 5 0 R >> >>",
                   extra={5: img})
    r = extract_text(pdf)
    assert not r.ok and r.pages == 1
    assert any("그림" in n for n in r.notes)


def test_inline_image_bytes_are_skipped():
    content = (b"q 10 0 0 10 0 0 cm BI /W 4 /H 4 /CS /G /BPC 8 ID \x00(]\xff[) Tj >>EI\x01 EI Q "
               b"BT /F1 12 Tf 72 700 Td (After inline) Tj ET")
    r = extract_text(page_doc(content, extra={5: HELV}))
    assert r.ok and r.text == "After inline" and r.notes == []


# ---------------------------------------------------------------- 주석, 제한

def test_freetext_annotation_is_read():
    contents = b"<FEFF" + "마감 11/20 18:00".encode("utf-16-be").hex().upper().encode() + b">"
    annot = b"<< /Type /Annot /Subtype /FreeText /Rect [0 0 100 20] /Contents " + contents + b" >>"
    pdf = page_doc(b"BT /F1 12 Tf 72 700 Td (Lab 4) Tj ET", extra={5: HELV, 6: annot},
                   page_extra=b" /Annots [6 0 R]")
    assert extract_text(pdf).text == "Lab 4\n마감 11/20 18:00"


def test_limits_pages_bytes_time():
    pdf = text_doc([PAGE1, PAGE2])
    r = extract_text(pdf, max_pages=1)
    assert r.pages == 2 and PAGE_SEP not in r.text and lines_of(r) == PAGE1_LINES
    assert any("앞 1쪽만" in n for n in r.notes)

    r = extract_text(pdf, max_bytes=100)
    assert not r.ok and any("너무 커서" in n for n in r.notes)

    t0 = time.monotonic()
    r = extract_text(pdf, time_budget=0)
    assert time.monotonic() - t0 < 1 and any("시간 제한" in n for n in r.notes)
