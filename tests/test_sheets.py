"""표 파일 읽기(계약 25-1): xlsx·xlsm, 옛 xls(OLE2 + BIFF8), CSV·TSV.

바이너리 예시 파일을 밖에서 가져오지 않는다 — xlsx는 zipfile로, xls는 아래의 작은 OLE2·BIFF8 생성기로 만든다.
"""
import io
import json
import posixpath
import random
import struct
import zipfile

import pytest

from lms_agent import sheets
from lms_agent.sheets import kind_of, read, read_with_notes, to_text


# ---------------------------------------------------------------- xlsx 만들기

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_XLSM = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def ws(body):
    return f'{DECL}<worksheet xmlns="{NS_MAIN}" xmlns:r="{NS_R}"><sheetData>{body}</sheetData></worksheet>'


def zip_files(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def rels(items):
    body = "".join(f'<Relationship Id="{i}" Type="{RT}{t}" Target="{tg}"{extra}/>' for i, t, tg, extra in items)
    return f'{DECL}<Relationships xmlns="{NS_PR}">{body}</Relationships>'


SST = (f'{DECL}<sst xmlns="{NS_MAIN}" count="6" uniqueCount="6">'
       "<si><t>학번</t></si>"
       "<si><t>이름</t></si>"
       '<si><r><rPr><b/></rPr><t>중간</t></r><r><t xml:space="preserve">고사 </t></r><rPh sb="0" eb="1"><t>ちゅう</t></rPh></si>'
       "<si><t>홍길동</t></si>"
       "<si><t>줄_x000D_바꿈</t></si>"
       "<si><t>비밀</t></si>"
       "</sst>")

SHEET1 = ws(
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>'
    '<c r="D1" t="inlineStr"><is><t>기말</t></is></c><c r="E1" t="inlineStr"><is><t>합계</t></is></c></row>'
    # 숫자로 저장된 학번, 수식의 저장된 값
    '<row r="2"><c r="A2"><v>2023123456</v></c><c r="B2" t="s"><v>3</v></c><c r="C2"><v>85.5</v></c>'
    '<c r="D2" s="1"><v>90</v></c><c r="E2"><f>C2+D2</f><v>175.5</v></c></row>'
    # r 없는 칸, 값 없는 수식, 불리언·오류, 글자 결과 수식
    '<row r="4"><c><v>2023000001</v></c><c t="inlineStr"><is><r><t>김</t></r><r><t>철수</t></r></is></c>'
    '<c r="D4"><v>70</v></c><c r="E4"><f>C4+D4</f></c><c r="F4" t="b"><v>1</v></c>'
    '<c r="G4" t="e"><v>#DIV/0!</v></c><c r="H4" t="str"><f>"A"&amp;"B"</f><v>AB</v></c></row>'
    '<row r="5"><c r="A5" t="inlineStr"><is><t>   </t></is></c></row>'  # 공백뿐 → 빈 행
    '<row><c r="B6" t="s"><v>4</v></c><c r="C6"><v>INF</v></c><c r="D6"><v>1.5E2</v></c></row>'  # 행 r 없음
    '<row r="7"><c r="I7" t="s"><v>99</v></c></row>'  # 없는 공유 문자열 → 빈 행
)
SHEET2 = ws('<row r="1"><c r="A1" t="s"><v>5</v></c></row>')  # 숨긴 시트
SHEET3 = ws('<row r="1"><c r="A1" t="inlineStr"><is><t>평균</t></is></c><c r="B1"><v>80.25</v></c></row>')
SHEET4 = ws("")  # 빈 시트

XLSX_ROWS = [
    {0: "학번", 1: "이름", 2: "중간고사", 3: "기말", 4: "합계"},
    {0: 2023123456.0, 1: "홍길동", 2: 85.5, 3: 90.0, 4: 175.5},
    {0: 2023000001.0, 1: "김철수", 3: 70.0, 7: "AB"},
    {1: "줄\r바꿈", 3: 150.0},
]


def xlsx_files(wb_path="xl/workbook.xml", main_ct=CT_SHEET):
    d, base = posixpath.split(wb_path)
    wb = (f'{DECL}<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_R}"><sheets>'
          '<sheet name="성적" sheetId="1" r:id="rId1"/>'
          '<sheet name="숨김" sheetId="2" state="hidden" r:id="rId2"/>'
          '<sheet name="차트" sheetId="3" r:id="rId3"/>'
          '<sheet name="요약" sheetId="4" r:id="rId4"/>'
          '<sheet name="빈 시트" sheetId="5" r:id="rId5"/>'
          "</sheets></workbook>")
    return {
        "[Content_Types].xml": (
            f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/{wb_path}" ContentType="{main_ct}"/></Types>'),
        "_rels/.rels": rels([("rId1", "officeDocument", wb_path, "")]),
        wb_path: wb,
        f"{d}/_rels/{base}.rels": rels([
            ("rId1", "worksheet", "worksheets/sheet1.xml", ""),
            ("rId2", "worksheet", "worksheets/sheet2.xml", ""),
            ("rId3", "chartsheet", "chartsheets/sheet1.xml", ""),
            ("rId4", "worksheet", f"/{d}/worksheets/sheet3.xml", ""),  # 절대 경로 대상
            ("rId5", "worksheet", "worksheets/sheet4.xml", ""),
            ("rId6", "sharedStrings", "sharedStrings.xml", ""),
            ("rId7", "externalLink", "file:///C:/secret/other.xlsx", ' TargetMode="External"'),
        ]),
        f"{d}/sharedStrings.xml": SST,
        f"{d}/worksheets/sheet1.xml": SHEET1,
        f"{d}/worksheets/sheet2.xml": SHEET2,
        f"{d}/worksheets/sheet3.xml": SHEET3,
        f"{d}/worksheets/sheet4.xml": SHEET4,
        f"{d}/chartsheets/sheet1.xml": f'{DECL}<chartsheet xmlns="{NS_MAIN}"/>',
    }


def xlsx_bytes(**kw):
    return zip_files(xlsx_files(**kw))


def one_sheet_xlsx(sheet_xml):
    return zip_files({
        "[Content_Types].xml": f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": rels([("rId1", "officeDocument", "xl/workbook.xml", "")]),
        "xl/workbook.xml": (f'{DECL}<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_R}"><sheets>'
                            '<sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": rels([("rId1", "worksheet", "worksheets/sheet1.xml", "")]),
        "xl/worksheets/sheet1.xml": sheet_xml,
    })


def patch_size(blob, member, size):
    """zip 중앙 디렉터리에 적힌 '푼 크기'를 바꾼다(거짓 머리글 흉내)."""
    b = bytearray(blob)
    pos = b.find(b"PK\x01\x02")
    while pos >= 0:
        n = struct.unpack_from("<H", b, pos + 28)[0]
        if bytes(b[pos + 46:pos + 46 + n]).decode() == member:
            struct.pack_into("<I", b, pos + 24, size)
            return bytes(b)
        pos = b.find(b"PK\x01\x02", pos + 4)
    raise AssertionError(member)


# ---------------------------------------------------------------- OLE2(CFB) + BIFF8 만들기

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENDC, FREE, FATSECT, DIFSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFC, 0xFFFFFFFF


def make_cfb(streams, shift=9, difat_in_sector=False):
    """[(이름, bytes)] → OLE2 복합 문서. 4096바이트보다 작은 스트림은 표준대로 mini stream에 넣는다.
    difat_in_sector=True면 FAT 섹터 목록을 머리글 대신 추가 DIFAT 섹터에 둔다(그 경로 시험용)."""
    ss = 1 << shift
    secs, fat = [], []

    def put(blob, pad=b"\0"):
        n = max(1, -(-len(blob) // ss))
        start = len(secs)
        for k in range(n):
            secs.append(blob[k * ss:(k + 1) * ss].ljust(ss, pad))
            fat.append(start + k + 1 if k < n - 1 else ENDC)
        return start

    mini, minifat, ents = bytearray(), [], []
    for name, data in streams:
        if not data:
            ents.append((name, ENDC, 0))
        elif len(data) < 4096:
            start, n = len(mini) // 64, -(-len(data) // 64)
            minifat.extend(start + k + 1 if k < n - 1 else ENDC for k in range(n))
            mini += data.ljust(n * 64, b"\0")
            ents.append((name, start, len(data)))
        else:
            ents.append((name, put(data), len(data)))
    root_start = put(bytes(mini)) if mini else ENDC
    mfat_start = put(struct.pack(f"<{len(minifat)}I", *minifat), pad=b"\xff") if minifat else ENDC
    n_mfat = -(-len(minifat) * 4 // ss)

    def dirent(name, typ, start, size, right=NOSTREAM, child=NOSTREAM):
        nm = name.encode("utf-16-le") + b"\0\0"
        return (nm.ljust(64, b"\0") + struct.pack("<HBB", len(nm), typ, 1) + struct.pack("<III", NOSTREAM, right, child)
                + b"\0" * 36 + struct.pack("<IQ", start, size))

    d = dirent("Root Entry", 5, root_start, len(mini), child=1 if ents else NOSTREAM)
    for i, (name, start, size) in enumerate(ents):
        d += dirent(name, 2, start, size, right=i + 2 if i + 1 < len(ents) else NOSTREAM)
    dir_start = put(d)

    per, nd, nf = ss // 4, (1 if difat_in_sector else 0), 1
    while len(secs) + nf + nd > nf * per:
        nf += 1
    fat_ids = list(range(len(secs), len(secs) + nf))
    fat += [FATSECT] * nf + [DIFSECT] * nd
    difat_id = len(secs) + nf if nd else ENDC
    fat += [FREE] * (nf * per - len(fat))
    fat_blob = struct.pack(f"<{len(fat)}I", *fat)
    secs += [fat_blob[k * ss:(k + 1) * ss] for k in range(nf)]
    if nd:
        secs.append(struct.pack(f"<{per}I", *(fat_ids + [FREE] * (per - 1 - nf) + [ENDC])))
    head_difat = [FREE] * 109 if nd else fat_ids + [FREE] * (109 - nf)
    hdr = bytearray(512)
    hdr[0:8] = OLE
    struct.pack_into("<5H", hdr, 0x18, 0x3E, 3 if shift == 9 else 4, 0xFFFE, shift, 6)
    struct.pack_into("<8I", hdr, 0x2C, nf, dir_start, 0, 4096, mfat_start, n_mfat, difat_id, nd)
    struct.pack_into("<109I", hdr, 0x4C, *head_difat)
    return bytes(hdr).ljust(ss, b"\0") + b"".join(secs)


def rec(rt, body=b""):
    return struct.pack("<HH", rt, len(body)) + body


def bof(dt, vers=0x0600):
    return rec(0x0809, struct.pack("<HHHHII", vers, dt, 0x0DBB, 0x07CC, 0x41, 0x06))


EOF_ = rec(0x000A)


def _wide(s):
    return any(ord(ch) > 255 for ch in s)


def enc(s, wide):
    return s.encode("utf-16-le") if wide else s.encode("latin-1")


def short_str(s):
    return struct.pack("<BB", len(s), int(_wide(s))) + enc(s, _wide(s))


def xl_str(s):
    return struct.pack("<HB", len(s), int(_wide(s))) + enc(s, _wide(s))


def boundsheet(off, name, hidden=0, dt=0):
    return rec(0x0085, struct.pack("<IBB", off, hidden, dt) + short_str(name))


def labelsst(r, c, i):
    return rec(0x00FD, struct.pack("<HHHI", r, c, 15, i))


def number(r, c, x):
    return rec(0x0203, struct.pack("<HHHd", r, c, 15, x))


def rk_int(n):
    return ((n << 2) | 2) & 0xFFFFFFFF


def rk_x100(n):
    return ((n << 2) | 3) & 0xFFFFFFFF


def rk_float(x):
    bits = struct.unpack("<Q", struct.pack("<d", x))[0]
    assert bits & 0x3FFFFFFFF == 0
    return bits >> 32


def rk(r, c, v):
    return rec(0x027E, struct.pack("<HHHI", r, c, 15, v))


def mulrk(r, c0, vals):
    return rec(0x00BD, struct.pack("<HH", r, c0) + b"".join(struct.pack("<HI", 15, v) for v in vals)
               + struct.pack("<H", c0 + len(vals) - 1))


def formula_num(r, c, x):
    return rec(0x0006, struct.pack("<HHHd", r, c, 15, x) + struct.pack("<HIH", 0, 0, 0))


def formula_special(r, c, typ, val=0):  # 0 글자(→ STRING) · 1 불리언 · 2 오류 · 3 빈 글자
    return rec(0x0006, struct.pack("<HHH", r, c, 15) + bytes([typ, 0, val, 0, 0, 0]) + b"\xff\xff"
               + struct.pack("<HIH", 0, 0, 0))


def label(r, c, s):
    return rec(0x0204, struct.pack("<HHH", r, c, 15) + xl_str(s))


def rstring(r, c, s):
    return rec(0x00D6, struct.pack("<HHH", r, c, 15) + xl_str(s) + struct.pack("<HHH", 1, 0, 1))


def sst_head(s, rich=0, ext=b"", wide=None):
    wide = _wide(s) if wide is None else wide
    h = struct.pack("<HB", len(s), int(wide) | (8 if rich else 0) | (4 if ext else 0))
    return h + (struct.pack("<H", rich) if rich else b"") + (struct.pack("<I", len(ext)) if ext else b"")


def sst_full(s, rich=0, ext=b""):
    return sst_head(s, rich, ext) + enc(s, _wide(s)) + b"\x00\x00\x01\x00" * rich + ext


SST_STRINGS = ["학번", "이름", "Midterm Score", "홍길동", "ABC가나", "굵게", "확장", "비밀", "경계"]


def sst_record():
    """CONTINUE로 나눈 SST: 압축 글자 중간, 넓은 글자 중간, 압축→넓은 글자로 바뀜, 서식 run 중간, 머리글 바로 뒤에서 끊김."""
    runs = b"\x00\x00\x01\x00\x01\x00\x02\x00"
    segs = [
        struct.pack("<II", 20, len(SST_STRINGS)) + sst_full("학번") + sst_full("이름")
        + sst_head("Midterm Score") + b"Midterm",
        b"\x00" + b" Score" + sst_head("홍길동") + enc("홍", True),
        b"\x01" + enc("길동", True) + sst_head("ABC가나", wide=False) + b"ABC",
        b"\x01" + enc("가나", True) + sst_head("굵게", rich=2) + enc("굵게", True) + runs[:3],
        runs[3:] + sst_full("확장", ext=b"E" * 10) + sst_full("비밀") + sst_head("경계"),
        b"\x01" + enc("경계", True),
    ]
    return rec(0x00FC, segs[0]) + b"".join(rec(0x003C, s) for s in segs[1:])


def workbook_stream(sheet_list, sst=b"", first=None, extra=b""):
    """sheet_list: [(이름, 숨김, 종류(dt), 시트 레코드들)] → BIFF8 'Workbook' 스트림."""
    def globals_(offsets):
        g = (first if first is not None else bof(0x0005)) + extra + rec(0x0042, struct.pack("<H", 1200))
        for (name, hidden, dt, _), off in zip(sheet_list, offsets):
            g += boundsheet(off, name, hidden, dt)
        return g + sst + EOF_
    g = globals_([0] * len(sheet_list))
    offs, pos = [], len(g)
    for *_, body in sheet_list:
        offs.append(pos)
        pos += len(body)
    return globals_(offs) + b"".join(body for *_, body in sheet_list)


MAIN_SHEET = (
    bof(0x0010) + rec(0x0200, struct.pack("<IIHHH", 0, 10, 0, 6, 0)) + rec(0x0208, b"\0" * 16)
    + labelsst(0, 0, 0) + labelsst(0, 1, 1) + labelsst(0, 2, 2) + labelsst(0, 3, 8)
    + number(1, 0, 2023123456.0) + labelsst(1, 1, 3) + rk(1, 2, rk_int(85)) + rk(1, 3, rk_x100(9025))
    + mulrk(2, 0, [rk_float(0.5), rk_int(-3), rk_x100(1234)])
    + formula_num(3, 0, 175.5)
    + formula_special(3, 1, 0) + rec(0x04BC, b"\0" * 10) + rec(0x0207, xl_str("합계"))
    + formula_special(3, 2, 1, 1)  # 불리언 결과 → 뺌
    + formula_special(3, 3, 0)     # 글자 결과인데 STRING이 안 옴 → 뺌
    + number(3, 4, 1.0)
    + rec(0x0205, struct.pack("<HHHBB", 4, 0, 15, 1, 0))  # BOOLERR
    + rec(0x0201, struct.pack("<HHH", 4, 1, 15)) + rec(0x00BE, struct.pack("<6H", 4, 1, 15, 15, 15, 3))  # BLANK·MULBLANK
    + labelsst(5, 1, 4) + labelsst(5, 2, 5) + labelsst(5, 3, 6) + labelsst(5, 5, 99)
    + number(5, 4, float("nan"))
    + bof(0x0020) + number(6, 0, 999.0) + EOF_  # 시트 안에 묻힌 차트 → 건너뜀
    + EOF_
)
NOTES_SHEET = (
    bof(0x0010) + label(0, 0, "메모") + rstring(1, 0, "Rich")
    + formula_special(2, 0, 0) + rec(0x0207, struct.pack("<HB", 8, 0) + b"Hello ") + rec(0x003C, b"\x01" + enc("세계", True))
    + EOF_
)
XLS_SHEETS = [
    ("성적", 0, 0, MAIN_SHEET),
    ("숨김", 1, 0, bof(0x0010) + labelsst(0, 0, 7) + EOF_),
    ("Chart1", 0, 2, bof(0x0020) + EOF_),
    ("Sheet3", 0, 0, bof(0x0010) + EOF_),
    ("Notes", 0, 0, NOTES_SHEET),
]
XLS_MAIN_ROWS = [
    {0: "학번", 1: "이름", 2: "Midterm Score", 3: "경계"},
    {0: 2023123456.0, 1: "홍길동", 2: 85.0, 3: 90.25},
    {0: 0.5, 1: -3.0, 2: 12.34},
    {0: 175.5, 1: "합계", 4: 1.0},
    {1: "ABC가나", 2: "굵게", 3: "확장"},
]


def xls_bytes(pad_to=0, **kw):
    wb = workbook_stream(XLS_SHEETS, sst_record())
    wb += b"\0" * max(0, pad_to - len(wb))  # 마지막 EOF 뒤 채움(엑셀도 4096바이트 이상으로 맞춤)
    return make_cfb([("Workbook", wb), ("\x05SummaryInformation", b"\0" * 200)], **kw)


# ---------------------------------------------------------------- xlsx

def test_xlsx_all_visible_sheets_in_order_with_values():
    data = xlsx_bytes()
    assert kind_of(data, "성적.xlsx") == "xlsx"
    got, notes = read_with_notes(data, "성적.xlsx")
    assert [s["name"] for s in got] == ["성적", "요약"]
    assert got[0]["rows"] == XLSX_ROWS
    assert got[1]["rows"] == [{0: "평균", 1: 80.25}]
    assert isinstance(got[0]["rows"][1][0], float)  # 숫자 학번은 숫자로
    assert all(set(s) == {"name", "rows"} for s in got)  # 안 잘렸으면 truncated 키 없음
    joined = " / ".join(notes)
    assert "숨긴 시트 1개" in joined and "차트·매크로 시트 1개" in joined and "빈 시트 1개" in joined
    assert "저장된 값이 없는 수식 칸 1개" in joined
    assert "비밀" not in to_text(got)


def test_xlsx_to_text():
    assert to_text(read(xlsx_bytes(), "a.xlsx")) == (
        "[성적]\n학번\t이름\t중간고사\t기말\t합계\n2023123456\t홍길동\t85.5\t90\t175.5\n"
        "2023000001\t김철수\t\t70\t\t\t\tAB\n\t줄 바꿈\t\t150\n[요약]\n평균\t80.25")


def test_xlsx_detected_by_content_not_name():
    data = xlsx_bytes()
    assert kind_of(data, "") == "xlsx"
    assert kind_of(data, "점수.csv") == "xlsx"
    assert kind_of(data, "C:\\Users\\me\\점수.XLSX") == "xlsx"
    assert read(data, "점수.csv")[0]["rows"] == XLSX_ROWS


def test_xlsm_values_only():
    files = xlsx_files(main_ct=CT_XLSM)
    files["xl/vbaProject.bin"] = b"\xd0\xcf\x11\xe0 macro bytes"
    data = zip_files(files)
    assert kind_of(data, "a.xlsm") == "xlsx"
    assert read(data, "a.xlsm")[0]["rows"] == XLSX_ROWS


def test_xlsx_nonstandard_workbook_path_and_missing_package_rels():
    files = xlsx_files(wb_path="xl/book2.xml")
    data = zip_files(files)
    assert kind_of(data, "") == "xlsx"  # xl/workbook.xml이 없어도 [Content_Types]의 main으로
    assert read(data)[0]["rows"] == XLSX_ROWS
    del files["_rels/.rels"]
    assert read(zip_files(files))[0]["rows"] == XLSX_ROWS


def test_docx_and_plain_zip_are_not_sheets():
    docx = zip_files({
        "[Content_Types].xml": (
            f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        "_rels/.rels": rels([("rId1", "officeDocument", "word/document.xml", "")]),
        "word/document.xml": "<w:document/>",
        "word/embeddings/Microsoft_Excel_Worksheet.xlsx": xlsx_bytes(),  # 문서 안에 엑셀이 묻혀 있어도
    })
    assert kind_of(docx, "보고서.docx") is None
    assert kind_of(docx, "") is None
    with pytest.raises(ValueError):
        read(docx, "보고서.docx")
    plain = zip_files({"a.txt": "hello"})
    assert kind_of(plain, "data.zip") is None


def test_xlsb_explains():
    data = zip_files({
        "[Content_Types].xml": "<Types/>",
        "_rels/.rels": rels([("rId1", "officeDocument", "xl/workbook.bin", "")]),
        "xl/workbook.bin": b"\x83\x01\x00",
    })
    assert kind_of(data, "a.xlsb") == "xlsx"
    with pytest.raises(ValueError, match=r"\.xlsb"):
        read(data, "a.xlsb")


def test_xlsx_limits(monkeypatch):
    monkeypatch.setattr(sheets, "MAX_ROWS", 2)
    got, notes = read_with_notes(xlsx_bytes())
    assert got[0]["rows"] == XLSX_ROWS[:2] and got[0]["truncated"] is True
    assert "truncated" not in got[1]
    assert any("잘라 냄" in n for n in notes)
    monkeypatch.setattr(sheets, "MAX_ROWS", 5000)
    monkeypatch.setattr(sheets, "MAX_COLS", 3)
    got = read(xlsx_bytes())
    assert got[0]["rows"][0] == {0: "학번", 1: "이름", 2: "중간고사"} and got[0]["truncated"] is True
    monkeypatch.setattr(sheets, "MAX_COLS", 200)
    monkeypatch.setattr(sheets, "MAX_SHEETS", 1)
    got, notes = read_with_notes(xlsx_bytes())
    assert [s["name"] for s in got] == ["성적"]
    assert any("앞의 1개만" in n for n in notes)


def test_xlsx_lying_zip_header_is_not_unpacked():
    # 중앙 디렉터리에 아주 큰 크기가 적힌 시트: 풀기 전에 막는다.
    data = patch_size(xlsx_bytes(), "xl/worksheets/sheet1.xml", 0x7FFFFFF0)
    got, notes = read_with_notes(data)
    assert [s["name"] for s in got] == ["요약"]
    assert any("너무 큰 시트 1개" in n for n in notes)
    bomb = patch_size(one_sheet_xlsx(ws("")), "xl/worksheets/sheet1.xml", 0x7FFFFFF0)
    with pytest.raises(ValueError, match="너무 커서"):
        read(bomb)
    # 작게 적고 실제로는 더 큰 것: zipfile이 적힌 크기에서 끊고 CRC가 안 맞아 ValueError.
    small = patch_size(xlsx_bytes(), "xl/worksheets/sheet1.xml", 40)
    with pytest.raises(ValueError):
        read(small)


def test_xlsx_zip_bomb_budget(monkeypatch):
    big = ws('<row r="1"><c r="A1"><v>1</v></c></row>' + " " * 200_000)  # 잘 눌리는 공백 200KB
    data = one_sheet_xlsx(big)
    assert len(data) < 20_000
    monkeypatch.setattr(sheets, "MAX_UNPACKED", 50_000)
    with pytest.raises(ValueError, match="너무 커서"):
        read(data)
    monkeypatch.setattr(sheets, "MAX_UNPACKED", 50 * 1024 * 1024)
    assert read(data)[0]["rows"] == [{0: 1.0}]


def test_xlsx_rejects_dtd_entities():
    evil = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
            f'<worksheet xmlns="{NS_MAIN}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&lol2;</t></is></c>'
            "</row></sheetData></worksheet>")
    with pytest.raises(ValueError):
        read(one_sheet_xlsx(evil))


def test_xlsx_malformed_inputs_raise_value_error():
    data = xlsx_bytes()
    for bad in (data[: len(data) // 2], data[:100], b"PK\x03\x04" + b"\0" * 50, b"not a zip at all"):
        with pytest.raises(ValueError):
            read(bad, "성적.xlsx")
    files = xlsx_files()
    files["xl/worksheets/sheet1.xml"] = "<worksheet><sheetData><row>"  # 깨진 XML 시트 하나
    got, notes = read_with_notes(zip_files(files))
    assert [s["name"] for s in got] == ["요약"] and any("깨진 시트 1개" in n for n in notes)
    del files["xl/workbook.xml"]
    with pytest.raises(ValueError, match="통합 문서"):
        read(zip_files(files), "a.xlsx")


def test_errors_never_include_path_or_content():
    for data in (b"\x00garbage-secret-content\x01", b"PK\x03\x04secret", OLE + b"secret" * 100):
        with pytest.raises(ValueError) as e:
            read(data, "C:\\비밀폴더\\학생명단.xls")
        msg = str(e.value)
        assert "비밀폴더" not in msg and "학생명단" not in msg and "secret" not in msg
        # 안쪽 예외(바이트 조각이 담길 수 있음)를 이어 붙이지 않는다
        assert e.value.__cause__ is None and (e.value.__context__ is None or e.value.__suppress_context__)


# ---------------------------------------------------------------- xls (BIFF8)

@pytest.mark.parametrize("variant", [
    {},                                   # 작은 스트림 → mini stream·mini FAT
    {"pad_to": 6000},                     # 4096바이트 이상 → 보통 섹터
    {"pad_to": 6000, "shift": 12},        # 4096바이트 섹터(v4)
    {"pad_to": 6000, "difat_in_sector": True},  # FAT 목록이 추가 DIFAT 섹터에
    {"shift": 12, "difat_in_sector": True},
])
def test_xls_round_trip(variant):
    data = xls_bytes(**variant)
    assert kind_of(data, "성적.xls") == "xls"
    assert kind_of(data, "") == "xls" and kind_of(data, "a.xlsx") == "xls" and kind_of(data, "a.csv") == "xls"
    got, notes = read_with_notes(data, "성적.xls")
    assert [s["name"] for s in got] == ["성적", "Notes"]
    assert got[0]["rows"] == XLS_MAIN_ROWS
    assert got[1]["rows"] == [{0: "메모"}, {0: "Rich"}, {0: "Hello 세계"}]
    joined = " / ".join(notes)
    assert "숨긴 시트 1개" in joined and "차트·매크로 시트 1개" in joined and "빈 시트 1개" in joined
    text = to_text(got)
    assert "2023123456\t홍길동\t85\t90.25" in text and "비밀" not in text and "999" not in text


def test_xls_limits(monkeypatch):
    monkeypatch.setattr(sheets, "MAX_ROWS", 2)
    got = read(xls_bytes())
    assert got[0]["rows"] == XLS_MAIN_ROWS[:2] and got[0]["truncated"] is True
    monkeypatch.setattr(sheets, "MAX_ROWS", 5000)
    monkeypatch.setattr(sheets, "MAX_COLS", 2)
    assert read(xls_bytes())[0]["rows"][0] == {0: "학번", 1: "이름"}


def test_xls_biff5_and_older_explain():
    biff5 = rec(0x0809, struct.pack("<HHHH", 0x0500, 0x0005, 0x0DBB, 0x07CC)) + EOF_
    for stream_name in ("Book", "Workbook"):
        with pytest.raises(ValueError, match=r"옛 형식.*\.xlsx로 저장해 다시 올려"):
            read(make_cfb([(stream_name, biff5)]), "옛성적.xls")
    biff2 = rec(0x0009, struct.pack("<HH", 2, 0x10)) + EOF_
    with pytest.raises(ValueError, match="옛 형식"):
        read(make_cfb([("Book", biff2)]), "x.xls")


def test_xls_password_protected():
    wb = workbook_stream(XLS_SHEETS[:1], sst_record(), extra=rec(0x002F, struct.pack("<H", 1) + b"\0" * 52))
    with pytest.raises(ValueError, match="암호"):
        read(make_cfb([("Workbook", wb)]), "성적.xls")
    # 암호 걸린 xlsx는 OLE2(EncryptionInfo·EncryptedPackage)로 감싸 온다
    enc_xlsx = make_cfb([("EncryptionInfo", b"\x04\x00\x04\x00" + b"\0" * 100), ("EncryptedPackage", b"\0" * 300)])
    assert kind_of(enc_xlsx, "성적.xlsx") == "xlsx"
    assert kind_of(enc_xlsx, "") is None
    with pytest.raises(ValueError, match="암호"):
        read(enc_xlsx, "성적.xlsx")


def test_other_ole_documents_are_not_sheets():
    hwp = make_cfb([("FileHeader", b"HWP Document File" + b"\0" * 100), ("BodyText", b"\0" * 100)])
    assert kind_of(hwp, "과제.hwp") is None
    assert kind_of(hwp, "") is None
    assert kind_of(hwp, "이상한.xls") == "xls"
    with pytest.raises(ValueError, match="통합 문서"):
        read(hwp, "이상한.xls")
    with pytest.raises(ValueError):
        read(hwp, "과제.hwp")


def test_xls_malformed_raise_value_error():
    data = xls_bytes(pad_to=6000)
    bad_bom = bytearray(data)
    bad_bom[0x1C] = 0
    for bad in (data[: len(data) // 2], data[:600], bytes(bad_bom), OLE + b"\0" * 504):
        with pytest.raises(ValueError):
            read(bad, "a.xls")
    # FAT 고리: Workbook 스트림의 둘째 섹터가 첫 섹터를 가리키게
    loop = bytearray(data)
    fat_sector = struct.unpack_from("<I", loop, 0x4C)[0]
    dir_start = struct.unpack_from("<I", loop, 0x30)[0]
    wb_start = struct.unpack_from("<I", loop, (dir_start + 1) * 512 + 128 + 116)[0]
    struct.pack_into("<I", loop, (fat_sector + 1) * 512 + 4 * (wb_start + 1), wb_start)
    with pytest.raises(ValueError):
        read(bytes(loop), "a.xls")


@pytest.mark.parametrize("which", ["xls", "xlsx"])
def test_fuzz_never_crashes(which):
    base = xls_bytes(pad_to=5000) if which == "xls" else xlsx_bytes()
    rng = random.Random(20261114)
    for i in range(300):
        b = bytearray(base)
        if i % 3 == 0:
            b = b[: rng.randrange(len(b))]
        else:
            for _ in range(rng.randint(1, 8)):
                b[rng.randrange(len(b))] = rng.getrandbits(8)
        data = bytes(b)
        kind_of(data, "f." + which)
        try:
            out = read(data, "f." + which)
        except ValueError:
            continue
        assert isinstance(out, list)
        to_text(out)


# ---------------------------------------------------------------- CSV·TSV

def test_csv_utf8_bom_strings_trimmed_empty_skipped():
    data = "\ufeff학번,이름,점수\n2023123456,홍길동,85.5\n,,\n2023000001, 김철수 ,90\n".encode("utf-8")
    assert kind_of(data, "점수.csv") == "csv"
    got, notes = read_with_notes(data, "점수.csv")
    assert got == [{"name": "점수", "rows": [
        {0: "학번", 1: "이름", 2: "점수"},
        {0: "2023123456", 1: "홍길동", 2: "85.5"},  # 숫자처럼 보여도 글자 그대로
        {0: "2023000001", 1: "김철수", 2: "90"},
    ]}]
    assert any("UTF-8" in n and "쉼표" in n for n in notes)


def test_csv_cp949_korean_headers():
    data = "학번,이름,중간고사\r\n2023123456,홍길동,80\r\n".encode("cp949")
    got, notes = read_with_notes(data, "C:\\Users\\me\\Downloads\\디지털통신 성적.csv")
    assert got[0]["name"] == "디지털통신 성적"
    assert got[0]["rows"] == [{0: "학번", 1: "이름", 2: "중간고사"}, {0: "2023123456", 1: "홍길동", 2: "80"}]
    assert any("CP949" in n for n in notes)


def test_csv_tab_and_semicolon_and_quotes():
    tsv = "학번\t이름\t비고\n1\t홍길동\t지각, 결석\n2\t김철수\t\n".encode("utf-8")
    want = [{0: "학번", 1: "이름", 2: "비고"}, {0: "1", 1: "홍길동", 2: "지각, 결석"}, {0: "2", 1: "김철수"}]
    assert kind_of(tsv, "a.tsv") == "csv"
    assert read(tsv, "a.tsv")[0]["rows"] == want
    assert read(tsv, "a.csv")[0]["rows"] == want  # 이름이 csv여도 탭으로 추측
    semi = "학번;이름;점수\n1;가;85,5\n2;나;90,0\n".encode("utf-8")
    assert read(semi, "a.csv")[0]["rows"][1] == {0: "1", 1: "가", 2: "85,5"}
    quoted = '"a,b",c\r"줄1\n줄2",d\r'.encode("utf-8")  # 옛 맥 줄바꿈(\r) + 따옴표 안 줄바꿈
    assert read(quoted, "q.csv")[0]["rows"] == [{0: "a,b", 1: "c"}, {0: "줄1\n줄2", 1: "d"}]


def test_csv_utf16_and_truncated_utf8_and_pk_header():
    u16 = "\ufeff학번\t점수\r\n1\t2\r\n".encode("utf-16-le")
    assert kind_of(u16, "a.csv") == "csv"
    assert read(u16, "a.csv")[0]["rows"] == [{0: "학번", 1: "점수"}, {0: "1", 1: "2"}]
    cut = "학번,이름\n1,홍길동".encode("utf-8")[:-1]  # 마지막 글자 중간에서 잘림
    assert read(cut, "a.csv")[0]["rows"][0] == {0: "학번", 1: "이름"}
    pk = "PK,이름\n1,가\n".encode("utf-8")  # 'PK'로 시작하는 CSV는 zip이 아니다
    assert kind_of(pk, "a.csv") == "csv"
    assert read(pk, "a.csv")[0]["rows"][0] == {0: "PK", 1: "이름"}


def test_csv_binary_and_empty():
    with pytest.raises(ValueError):
        read(b"a,b\n\x00\x01\x02", "a.csv")
    got, notes = read_with_notes(b"", "a.csv")
    assert got == [] and "읽을 내용이 있는 시트가 없음" in notes


def test_csv_limits(monkeypatch):
    monkeypatch.setattr(sheets, "MAX_ROWS", 2)
    got = read("\n".join(f"{i},{i}" for i in range(10)).encode(), "a.csv")
    assert got[0]["rows"] == [{0: "0", 1: "0"}, {0: "1", 1: "1"}] and got[0]["truncated"] is True


def test_disguised_xls():
    html = b"<html><body><table><tr><td>1</td></tr></table></body></html>"
    for data in (html, b"\xef\xbb\xbf  " + html, "\ufeff<html><table></table></html>".encode("utf-16-le")):
        assert kind_of(data, "성적.xls") == "xls"
        with pytest.raises(ValueError, match="HTML"):
            read(data, "성적.xls")
    tsv = "학번\t점수\r\n2023123456\t85\r\n".encode("cp949")  # 포털의 '엑셀 내려받기'가 주는 글자 표
    got, notes = read_with_notes(tsv, "성적.xls")
    assert got[0]["rows"] == [{0: "학번", 1: "점수"}, {0: "2023123456", 1: "85"}]
    assert "글자로 된 표" in notes[0]
    with pytest.raises(ValueError):
        read(b"\x00\x01\x02garbage\x00", "성적.xls")
    with pytest.raises(ValueError):
        read(b"plain text but named xlsx", "성적.xlsx")  # 글자 표로 읽어 주는 것은 .xls 이름일 때만


# ---------------------------------------------------------------- kind_of 아닌 것들

def test_kind_of_negatives():
    pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    assert kind_of(pdf, "강의계획서.pdf") is None
    assert kind_of(pdf, "a.csv") is None
    assert kind_of(pdf, "") is None
    assert kind_of(b"\x89PNG\r\n\x1a\n" + b"\0" * 50, "그림.png") is None
    rng = random.Random(7)
    noise = bytes(rng.getrandbits(8) for _ in range(2000))
    assert kind_of(noise, "") is None and kind_of(noise, "a.bin") is None
    assert kind_of(b"", "") is None
    assert kind_of(None, "a.xlsx") is None and kind_of("not bytes", "a.csv") is None
    assert kind_of(b"hello", "notes.txt") is None
    with pytest.raises(ValueError):
        read(pdf, "강의계획서.pdf")


def test_bytes_like_inputs():
    data = xlsx_bytes()
    assert read(bytearray(data)) == read(memoryview(data)) == read(data)


def test_file_size_limit(monkeypatch):
    monkeypatch.setattr(sheets, "MAX_FILE", 100)
    with pytest.raises(ValueError, match="너무 커서"):
        read(xlsx_bytes(), "a.xlsx")


# ---------------------------------------------------------------- to_text

def test_to_text_numbers_flattening_and_json_round_trip():
    sh = [{"name": "", "rows": [{0: 2023123456.0, 1: 85.5, 2: 90.0, 4: "a\tb\nc"}, {}, {1: -0.0, 2: 1e20}]}]
    assert to_text(sh) == "[시트]\n2023123456\t85.5\t90\t\ta b c\n\t0\t1e+20"
    back = json.loads(json.dumps(read(xlsx_bytes())))  # state.json에 저장했다 되살리면 열 번호가 글자
    assert to_text(back) == to_text(read(xlsx_bytes()))


def test_to_text_limit_cuts_at_line():
    got = read(xlsx_bytes())
    full = to_text(got)
    assert to_text(got, limit=len(full)) == full
    first2 = "[성적]\n학번\t이름\t중간고사\t기말\t합계"
    assert to_text(got, limit=len(first2)) == first2
    assert to_text(got, limit=len(first2) - 1) == "[성적]"
    assert to_text(got, limit=3) == "[성적"
    assert to_text(got, limit=0) == ""
