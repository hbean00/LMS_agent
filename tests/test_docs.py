"""문서 파일(docx·pptx·hwpx·hwp)과 zip 속 파일 글자 읽기(계약 27-2): 형식마다 작은 진짜 파일을 코드로 만든다(가짜 학번만).
받기(materials.fetch) → pdf_texts → 성적 후보, 받아 둔 파일 다시 읽기, 압축 폭탄·개수 한도, 암호·깨진 파일의 이유."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

import io
import json
import struct
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from lms_agent import core, docs, gradebook, materials, scorefile
from lms_agent.mock_canvas import DEMO_STUDENT_ID, score_table_pdf, score_table_xlsx, tiny_pdf
from lms_agent.store import Store

SID = "2099123456"   # 가짜 학번
SCORES = [55, 72, 64, 90, 47, None, 81]     # 6번은 결시(빈 칸)


def zip_of(files, compression=zipfile.ZIP_DEFLATED) -> bytes:
    """[(이름, bytes | str)] → zip(날짜 고정)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, body in files:
            if isinstance(body, str):
                body = body.encode("utf-8")
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), body, compress_type=compression)
    return buf.getvalue()


# ---------------------------------------------------------------- docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def w_p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>' if text else "<w:p/>"


def w_tbl(rows) -> str:
    return ("<w:tbl><w:tblPr/><w:tblGrid/>" + "".join(
        "<w:tr>" + "".join(f"<w:tc><w:tcPr/>{w_p(str(c) if c is not None else '')}</w:tc>" for c in row) + "</w:tr>"
        for row in rows) + "</w:tbl>")


def docx_bytes(body: str, header: str | None = None) -> bytes:
    files = [
        ("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                                'content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.'
                                'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        ("_rels/.rels", f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{R}/officeDocument" '
                        'Target="word/document.xml"/></Relationships>'),
        ("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}" xmlns:r="{R}">'
                              f"<w:body>{body}<w:sectPr/></w:body></w:document>"),
    ]
    if header is not None:
        files += [("word/_rels/document.xml.rels", f'<Relationships xmlns="{PKG}"><Relationship Id="rId7" '
                                                   f'Type="{R}/header" Target="header1.xml"/></Relationships>'),
                  ("word/header1.xml", f'<w:hdr xmlns:w="{W}">{w_p(header)}</w:hdr>')]
    return zip_of(files)


def score_rows(scores=SCORES, mine=88):
    rows = [["번호", "학번", "이름", "기말고사 (100)"]]
    for i, s in enumerate(scores, 1):
        rows.append([i, f"20990000{i:02d}", "김*민", s])
    rows.append([len(scores) + 1, SID, "나*빈", mine])
    return rows


def score_docx() -> bytes:
    return docx_bytes(w_p("VLSI 기말고사 채점 결과") + w_tbl(score_rows()) + w_p("문의는 메일로"))


def test_docx_score_table_lines_are_read_by_score_parser():
    text, notes = docs.read("기말_성적.docx", score_docx())
    lines = text.splitlines()
    assert lines[0] == "VLSI 기말고사 채점 결과" and lines[-1] == "문의는 메일로"
    assert "번호 학번 이름 기말고사 (100)" in lines
    assert f"8 {SID} 나*빈 88" in lines and "6 2099000006 김*민 -" in lines   # 빈 칸은 '-'(칸 위치 유지)
    (r,) = scorefile.parse_score_results(text, SID, filename="기말_성적.docx")
    assert (r.score, r.max_score, r.n, r.rank, r.high, r.low) == (88, 100, 7, 2, 90, 47)
    assert r.mean == pytest.approx(sum(s for s in SCORES if s is not None) / 6 * 6 / 7 + 88 / 7)
    assert "2099000001" not in json.dumps(r.__dict__, ensure_ascii=False)     # 다른 학생 학번은 결과에 없다


def test_docx_runs_tabs_breaks_textboxes_merges_and_header():
    body = (
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        "<w:r><w:t>Report</w:t></w:r><w:r><w:tab/><w:t>due 11/18 23:59</w:t></w:r>"
        "<w:del><w:r><w:delText>OLD 11/11</w:delText></w:r></w:del>"
        '<w:r><w:instrText>HYPERLINK "https://x.example"</w:instrText></w:r>'
        "<w:hyperlink><w:r><w:t> (Canvas)</w:t></w:r></w:hyperlink><w:r><w:br/><w:t>둘째 줄</w:t></w:r>"
        "<w:r><w:drawing><w:txbxContent>" + w_p("글 상자 안내") + "</w:txbxContent></w:drawing></w:r>"
        "<mc:AlternateContent xmlns:mc='http://schemas.openxmlformats.org/markup-compatibility/2006'>"
        "<mc:Choice/><mc:Fallback><w:r><w:t>중복 글</w:t></w:r></mc:Fallback></mc:AlternateContent></w:p>"
        "<w:sdt><w:sdtContent>" + w_p("내용 컨트롤 글") + "</w:sdtContent></w:sdt>"
        "<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val='2'/></w:tcPr>" + w_p("Lab 1 Scores") + "</w:tc></w:tr>"
        "<w:tr><w:tc>" + w_p("A") + "</w:tc><w:tc>" + w_p("1") + "</w:tc></w:tr>"
        "<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc>" + w_p("2") + "</w:tc></w:tr></w:tbl>")
    text, _ = docs.read("안내.docx", docx_bytes(body, header="ASIC설계 2026"))
    assert text.splitlines() == ["Report due 11/18 23:59 (Canvas)", "둘째 줄", "글 상자 안내", "내용 컨트롤 글",
                                 "Lab 1 Scores", "A 1", "- 2", "ASIC설계 2026"]


# ---------------------------------------------------------------- pptx

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def a_p(text):
    return f"<a:p><a:r><a:rPr lang='ko'/><a:t>{escape(text)}</a:t></a:r></a:p>"


def slide(*parts, show=True):
    return (f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"' + ("" if show else ' show="0"') + "><p:cSld><p:spTree>"
            + "".join(parts) + "</p:spTree></p:cSld></p:sld>")


def shape(*paras):
    return "<p:sp><p:txBody><a:bodyPr/>" + "".join(a_p(t) for t in paras) + "</p:txBody></p:sp>"


def a_tbl(rows):
    return ("<p:graphicFrame><a:graphic><a:graphicData><a:tbl>" + "".join(
        "<a:tr>" + "".join(f"<a:tc><a:txBody>{a_p(str(c)) if c is not None else '<a:p/>'}</a:txBody></a:tc>" for c in r)
        + "</a:tr>" for r in rows) + "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>")


def pptx_bytes(slides_in_order) -> bytes:
    """슬라이드 목록(보여 줄 순서). 파일 이름 번호는 일부러 거꾸로(slideN은 목록의 끝부터) — 순서는 sldIdLst를 따라야 한다."""
    n = len(slides_in_order)
    files = [("_rels/.rels", f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{R}/officeDocument" '
                             'Target="ppt/presentation.xml"/></Relationships>'),
             ("ppt/presentation.xml", f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
                                      + "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 10}"/>' for i in range(n))
                                      + "</p:sldIdLst></p:presentation>"),
             ("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{PKG}">' + "".join(
                 f'<Relationship Id="rId{i + 10}" Type="{R}/slide" Target="slides/slide{n - i}.xml"/>' for i in range(n))
              + "</Relationships>")]
    files += [(f"ppt/slides/slide{n - i}.xml", s) for i, s in enumerate(slides_in_order)]
    return zip_of(files)


def test_pptx_slides_in_presentation_order_with_tables_hidden_skipped():
    data = pptx_bytes([slide(shape("Quiz 1 결과", "평균 7.5")),
                       slide(shape("숨긴 슬라이드"), show=False),
                       slide(shape("점수표"), a_tbl([["학번", "Quiz 1 (10)"], [SID, 9], ["2099000001", None],
                                                     ["2099000002", 6]]))])
    r = docs.read_detail("quiz.pptx", data)
    assert r["ok"] and r["kind"] == "pptx"
    assert r["text"].splitlines() == ["[슬라이드 1]", "Quiz 1 결과", "평균 7.5", "[슬라이드 3]", "점수표",
                                      "학번 Quiz 1 (10)", f"{SID} 9", "2099000001 -", "2099000002 6"]
    assert r["notes"] == ["숨긴 슬라이드 1개는 읽지 않았어요"]


# ---------------------------------------------------------------- hwpx

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"


def hp_p(text):
    return f"<hp:p><hp:run><hp:t>{escape(text)}</hp:t></hp:run></hp:p>"


def hp_tbl(rows):
    cells = "".join(
        "<hp:tr>" + "".join(
            f'<hp:tc><hp:subList>{hp_p(str(c)) if c is not None else "<hp:p/>"}</hp:subList>'
            f'<hp:cellAddr colAddr="{ci}" rowAddr="{ri}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
            for ci, c in enumerate(r)) + "</hp:tr>" for ri, r in enumerate(rows))
    return f"<hp:p><hp:run><hp:tbl>{cells}</hp:tbl></hp:run></hp:p>"


def hwpx_bytes(sections, spine=None, manifest=None) -> bytes:
    spine = spine if spine is not None else list(range(len(sections)))
    hpf = ('<opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:manifest>'
           '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
           + "".join(f'<opf:item id="section{i}" href="Contents/section{i}.xml" media-type="application/xml"/>'
                     for i in range(len(sections)))
           + '</opf:manifest><opf:spine><opf:itemref idref="header"/>'
           + "".join(f'<opf:itemref idref="section{i}"/>' for i in spine) + "</opf:spine></opf:package>")
    files = [("mimetype", "application/hwp+zip"), ("version.xml", "<hv:HCFVersion/>"), ("Contents/content.hpf", hpf),
             ("Contents/header.xml", "<hh:head xmlns:hh='x'><hh:t>머리 정보</hh:t></hh:head>")]
    files += [(f"Contents/section{i}.xml", f'<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">{body}</hs:sec>')
              for i, body in enumerate(sections)]
    if manifest is not None:
        files.append(("META-INF/manifest.xml", manifest))
    return zip_of(files)


def test_hwpx_sections_in_spine_order_with_inline_tabs_and_tables():
    s0 = (f'<hp:p><hp:run><hp:secPr/><hp:t>과제<hp:tab/>마감<hp:lineBreak/>11/20 23:59</hp:t></hp:run></hp:p>'
          + hp_tbl([["학번", "Report (30)"], [SID, 27], ["2099000001", None], ["2099000002", 21]]))
    s1 = hp_p("둘째 구역이 먼저(spine 순서)")
    r = docs.read_detail("안내.hwpx", hwpx_bytes([s0, s1], spine=[1, 0]))
    assert r["ok"] and r["kind"] == "hwpx"
    assert r["text"].splitlines() == ["둘째 구역이 먼저(spine 순서)", "과제 마감", "11/20 23:59", "학번 Report (30)",
                                      f"{SID} 27", "2099000001 -", "2099000002 21"]
    assert "머리 정보" not in r["text"]                                          # header.xml은 본문이 아니다


def test_hwpx_encrypted_or_broken_explains():
    locked = hwpx_bytes([hp_p("x")], manifest="<odf:manifest><odf:file-entry><odf:encryption-data/>"
                                               "</odf:file-entry></odf:manifest>")
    assert docs.read("a.hwpx", locked) is None
    assert docs.read_detail("a.hwpx", locked)["notes"][0] == docs.R_HWP_LOCKED
    broken = zip_of([("mimetype", "application/hwp+zip"), ("Contents/section0.xml", b"\x93\x11garbage")])
    assert docs.read_detail("b.hwpx", broken)["notes"][0] == docs.R_HWPX_BROKEN


# ---------------------------------------------------------------- hwp (OLE2 + 레코드)

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENDC, FREE, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF


def make_cfb(files) -> bytes:
    """[("저장소/스트림" 경로, bytes)] → OLE2 복합 문서(v3, 512바이트 섹터). 저장소(폴더)도 만든다.
    형제는 오른쪽 링크 사슬(올바른 이진 나무), 4096바이트보다 작은 스트림은 mini stream에."""
    ss = 512
    secs, fat = [], []

    def put(blob, pad=b"\0"):
        n = max(1, -(-len(blob) // ss))
        start = len(secs)
        for k in range(n):
            secs.append(blob[k * ss:(k + 1) * ss].ljust(ss, pad))
            fat.append(start + k + 1 if k < n - 1 else ENDC)
        return start

    nodes = [{"name": "Root Entry", "type": 5, "kids": []}]
    for path, data in files:
        parent = nodes[0]
        for st in path.split("/")[:-1]:
            found = next((nodes[i] for i in parent["kids"] if nodes[i]["name"] == st and nodes[i]["type"] == 1), None)
            if found is None:
                nodes.append({"name": st, "type": 1, "kids": []})
                parent["kids"].append(len(nodes) - 1)
                found = nodes[-1]
            parent = found
        nodes.append({"name": path.split("/")[-1], "type": 2, "data": data, "kids": []})
        parent["kids"].append(len(nodes) - 1)
    mini, minifat = bytearray(), []
    for n in nodes:
        if n["type"] != 2:
            n["start"], n["size"] = 0, 0
        elif not n["data"]:
            n["start"], n["size"] = ENDC, 0
        elif len(n["data"]) < 4096:
            start, k = len(mini) // 64, -(-len(n["data"]) // 64)
            minifat.extend(start + j + 1 if j < k - 1 else ENDC for j in range(k))
            mini += n["data"].ljust(k * 64, b"\0")
            n["start"], n["size"] = start, len(n["data"])
        else:
            n["start"], n["size"] = put(n["data"]), len(n["data"])
    nodes[0]["start"] = put(bytes(mini)) if mini else ENDC
    nodes[0]["size"] = len(mini)
    mfat_start = put(struct.pack(f"<{len(minifat)}I", *minifat), pad=b"\xff") if minifat else ENDC
    n_mfat = -(-len(minifat) * 4 // ss)
    right = {a: b for n in nodes for a, b in zip(n["kids"], n["kids"][1:])}

    def dirent(i, n):
        nm = n["name"].encode("utf-16-le") + b"\0\0"
        child = n["kids"][0] if n["kids"] else NOSTREAM
        return (nm.ljust(64, b"\0") + struct.pack("<HBB", len(nm), n["type"], 1)
                + struct.pack("<III", NOSTREAM, right.get(i, NOSTREAM), child) + b"\0" * 36
                + struct.pack("<IQ", n["start"], n["size"]))

    dir_start = put(b"".join(dirent(i, n) for i, n in enumerate(nodes)))
    per, nf = ss // 4, 1
    while len(secs) + nf > nf * per:
        nf += 1
    fat_ids = list(range(len(secs), len(secs) + nf))
    fat += [FATSECT] * nf
    fat += [FREE] * (nf * per - len(fat))
    blob = struct.pack(f"<{len(fat)}I", *fat)
    secs += [blob[k * ss:(k + 1) * ss] for k in range(nf)]
    hdr = bytearray(512)
    hdr[0:8] = OLE
    struct.pack_into("<5H", hdr, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<8I", hdr, 0x2C, nf, dir_start, 0, 4096, mfat_start, n_mfat, ENDC, 0)
    struct.pack_into("<109I", hdr, 0x4C, *(fat_ids + [FREE] * (109 - nf)))
    return bytes(hdr) + b"".join(secs)


def rec(tag, level, body=b""):
    if len(body) >= 0xFFF:   # 큰 레코드: 크기 칸 0xFFF + 32비트 크기
        return struct.pack("<II", tag | level << 10 | 0xFFF << 20, len(body)) + body
    return struct.pack("<I", tag | level << 10 | len(body) << 20) + body


def utf16(s):
    return s.encode("utf-16-le")


TAB = struct.pack("<8H", 9, 0, 0, 0, 0, 0, 0, 9)                    # 탭(8글자 조판 부호)
TBL_CHAR = struct.pack("<8H", 11, 0x6C62, 0x7420, 0, 0, 0, 0, 11)   # 표·그리기 개체(8글자)
END = utf16("\r")


def para(level, body: bytes):
    return rec(66, level, b"\0" * 22) + rec(67, level + 1, body + END)


def table(level, rows):
    out = para(level, TBL_CHAR)
    out += rec(71, level + 1, b" lbt" + b"\0" * 40)                                  # CTRL_HEADER 'tbl '
    out += rec(72, level + 2, b"\0" * 34)                                            # 캡션(TABLE 앞) — 칸이 아니다
    out += rec(67, level + 3, utf16("표 1 캡션") + END)
    out += rec(77, level + 2, struct.pack("<IHH", 0, len(rows), len(rows[0])) + b"\0" * 20)   # TABLE
    for ri, row in enumerate(rows):
        for ci, c in enumerate(row):
            if c == "__merged__":
                continue   # 합쳐져 없는 칸
            span = 2 if c == "__span__" else 1
            out += rec(72, level + 2, struct.pack("<HHIHHHH", 1, 0, 0, ci, ri, span, 1) + b"\0" * 18)
            out += rec(66, level + 2, b"\0" * 22)
            out += rec(67, level + 3, (utf16(str(c)) if c not in (None, "__span__") else b"") + END)
    return out


def file_header(props=0x01, version=0x05000300):
    return b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", version, props) + b"\0" * 216


def deflate(b):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


def hwp_bytes(stream: bytes, props=0x01, extra=()) -> bytes:
    body = deflate(stream) if props & 1 else stream
    return make_cfb([("FileHeader", file_header(props)), ("DocInfo", deflate(b"") if props & 1 else b""),
                     ("BodyText/Section0", body), ("PrvText", utf16("미리 보기"))] + list(extra))


HWP_SECTION = (para(0, utf16("기말고사 성적 안내") + TAB + utf16("(VLSI)"))
               + para(0, utf16("첫 줄") + utf16("\n") + utf16("둘째 줄"))
               + table(0, [["학번", "이름", "기말 (100)"], [SID, "나*빈", 88], ["2099000001", "김*민", None],
                           ["2099000002", "이*수", 64], ["2099000003", "박*진", 71]])
               + para(0, utf16("끝")))


def test_hwp_compressed_paragraphs_controls_and_table():
    r = docs.read_detail("성적.hwp", hwp_bytes(HWP_SECTION))
    assert r["ok"] and r["kind"] == "hwp", r["notes"]
    assert r["text"].splitlines() == ["기말고사 성적 안내 (VLSI)", "첫 줄", "둘째 줄", "표 1 캡션", "학번 이름 기말 (100)",
                                      f"{SID} 나*빈 88", "2099000001 김*민 -", "2099000002 이*수 64",
                                      "2099000003 박*진 71", "끝"]
    (res,) = scorefile.parse_score_results(r["text"], SID, filename="성적.hwp")
    assert (res.score, res.max_score, res.n) == (88, 100, 3)


def test_hwp_uncompressed_big_record_regular_sectors_and_nested_tables():
    long_text = "가나다라마" * 900          # 4,500글자 = 9,000바이트 → 크기 칸 0xFFF + 32비트 크기, 4096바이트 넘는 스트림
    inner = table(2, [["안", "쪽"]])
    nested = (para(0, TBL_CHAR) + rec(71, 1, b" lbt") + rec(77, 2, struct.pack("<IHH", 0, 1, 2))
              + rec(72, 2, struct.pack("<HHIHHHH", 1, 0, 0, 0, 0, 1, 1)) + rec(66, 2, b"") + rec(67, 3, utf16("바깥") + END)
              + rec(72, 2, struct.pack("<HHIHHHH", 1, 0, 0, 1, 0, 1, 1)) + rec(66, 2, b"") + rec(67, 3, TBL_CHAR + END)
              + inner)
    stream = para(0, utf16(long_text)) + nested + para(0, utf16("끝"))
    r = docs.read_detail("큰.hwp", hwp_bytes(stream, props=0))
    assert r["ok"], r["notes"]
    lines = r["text"].splitlines()
    assert lines[0] == long_text and lines[-1] == "끝"
    assert lines[1] == "바깥 표 1 캡션 안 쪽"                    # 안쪽 표는 바깥 칸의 글로 붙는다


def test_hwp_locked_distribution_old_and_broken_explain():
    assert docs.read("a.hwp", hwp_bytes(HWP_SECTION, props=0x03)) is None
    assert docs.read_detail("a.hwp", hwp_bytes(HWP_SECTION, props=0x03))["notes"][0] == docs.R_HWP_LOCKED
    assert docs.read_detail("a.hwp", hwp_bytes(HWP_SECTION, props=0x11))["notes"][0] == docs.R_HWP_LOCKED   # DRM
    dist = make_cfb([("FileHeader", file_header(0x05)), ("ViewText/Section0", b"\x01" * 300)])
    assert docs.read_detail("배포.hwp", dist)["notes"][0] == docs.R_HWP_DIST
    assert docs.read_detail("옛.hwp", b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\0" * 200)["notes"] == \
        [docs.R_HWP_OLD]
    assert docs.read_detail("깨짐.hwp", OLE + b"\x00" * 100)["notes"][0] == docs.R_HWP_BROKEN
    bad_stream = make_cfb([("FileHeader", file_header(0x01)), ("BodyText/Section0", b"not deflate \xff" * 40)])
    assert docs.read_detail("깨짐.hwp", bad_stream)["notes"][0] == docs.R_HWP_BROKEN
    assert docs.read_detail("x.hwp", make_cfb([("WordDocument", b"x" * 10)]))["notes"] == [docs.R_DOC_OLD]


def test_hwp_section_bomb_is_bounded(monkeypatch):
    monkeypatch.setattr(docs, "MAX_UNPACKED", 10_000)
    big = para(0, utf16("가" * 20_000))
    assert docs.read_detail("큰.hwp", hwp_bytes(big))["notes"][0] == docs.R_BIG


# ---------------------------------------------------------------- zip

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40


def korean_name_zip(inner_name: str, body: bytes, placeholder: str) -> bytes:
    """윈도 한국어 압축기처럼 UTF-8 표시 없이 CP949로 쓴 이름(같은 길이의 ASCII 이름을 써 두고 바이트를 바꾼다)."""
    raw = inner_name.encode("cp949")
    assert len(raw) == len(placeholder)
    return zip_of([(placeholder, body)]).replace(placeholder.encode(), raw)


def test_zip_reads_nested_pdf_sheet_doc_and_explains_the_rest():
    files = [("성적/Lab0_score.pdf", score_table_pdf("Lab0")), ("성적/Quiz2_성적.xlsx", score_table_xlsx()),
             ("성적/", b""), ("__MACOSX/성적/._Lab0_score.pdf", b"junk"), ("photo.png", PNG),
             ("inner.zip", zip_of([("a.csv", "학번,점수\n1,2\n")])), ("readme.txt", "hello"),
             ("other/Lab0_score.pdf", tiny_pdf(["Lab0 second copy"]))]
    r = docs.read_detail("성적 모음.zip", zip_of(files))
    assert r["ok"] and r["kind"] == "zip"
    by = {m["name"]: m for m in r["members"]}
    assert list(by) == ["Lab0_score.pdf", "Quiz2_성적.xlsx", "photo.png", "inner.zip", "readme.txt",
                        "Lab0_score (2).pdf"]                                        # 경로는 버리고 겹치면 번호
    assert by["Lab0_score.pdf"]["kind"] == "pdf" and "Lab0 Score" in by["Lab0_score.pdf"]["text"]
    assert by["Quiz2_성적.xlsx"]["kind"] == "sheet" and by["Quiz2_성적.xlsx"]["sheets"][0]["name"] == "Quiz2"
    assert by["photo.png"]["notes"] == [docs.R_IMAGE] and by["inner.zip"]["notes"] == [docs.R_ZIP_NESTED]
    assert by["readme.txt"]["notes"] == [docs.R_UNSUPPORTED] and by["Lab0_score (2).pdf"]["ok"]
    assert r["notes"][0] == "zip 안 파일 6개 중 3개를 읽었어요"
    assert r["text"].startswith("[Lab0_score.pdf]\n") and "[Quiz2_성적.xlsx]" in r["text"]
    # read()는 같은 글을 한 번에
    assert docs.read("성적 모음.zip", zip_of(files))[0] == r["text"]


def test_zip_member_names_in_cp949_are_decoded():
    r = docs.read_detail("묶음.zip", korean_name_zip("sub/기말_성적.docx", score_docx(), "sub/abcd_efgh.docx"))
    (m,) = r["members"]
    assert m["name"] == "기말_성적.docx" and m["ok"] and m["kind"] == "docx"


def test_zip_bomb_ratio_budget_and_member_limits(monkeypatch):
    bomb = b"\0" * (3 * 1024 * 1024)                              # 3MB가 몇 KB로 — 압축률 수백 배
    r = docs.read_detail("a.zip", zip_of([("bomb.csv", bomb), ("ok.csv", "학번,점수\n2099000001,3\n")]))
    by = {m["name"]: m for m in r["members"]}
    assert by["bomb.csv"]["notes"] == [docs.R_BOMB] and by["ok.csv"]["ok"]
    stored = docs.read_detail("b.zip", zip_of([("bomb.csv", bomb)], compression=zipfile.ZIP_STORED))
    assert stored["members"][0]["ok"] is False                     # 압축 안 된 3MB는 폭탄이 아니지만 CSV로는 못 읽음
    assert stored["members"][0]["notes"] != [docs.R_BOMB]

    monkeypatch.setattr(docs, "MAX_UNPACKED", 60)                  # 풀린 크기 합 한도
    r = docs.read_detail("c.zip", zip_of([(f"s{i}.csv", "학번,점수\n2099000001,3\n") for i in range(4)]))
    assert [m["ok"] for m in r["members"]] == [True, True, False, False]
    assert r["members"][2]["notes"] == [docs.R_BIG] and f"{docs.R_BIG} — 2개" in r["notes"]
    monkeypatch.undo()

    many = zip_of([(f"s{i:02d}.csv", f"학번,점수\n20990000{i:02d},{i}\n") for i in range(docs.MAX_MEMBERS + 10)])
    r = docs.read_detail("d.zip", many)
    assert len(r["members"]) == docs.MAX_MEMBERS and r["members"][-1]["name"] == f"s{docs.MAX_MEMBERS - 1:02d}.csv"
    assert r["notes"][0] == f"파일이 {docs.MAX_MEMBERS + 10}개라 앞의 {docs.MAX_MEMBERS}개만 봤어요"


def _set_encrypted(data: bytes) -> bytes:
    """zip의 모든 항목에 '암호 걸림' 표시(일반 비트 0)를 켠다(파이썬 zipfile은 암호 zip을 만들지 못함)."""
    b = bytearray(data)
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        pos = b.find(sig)
        while pos >= 0:
            b[pos + off] |= 1
            pos = b.find(sig, pos + 4)
    return bytes(b)


def test_encrypted_corrupt_and_unsupported_inputs_explain_without_content():
    secret = "2099123456 88 SECRET"
    cases = {
        "a.zip": (_set_encrypted(zip_of([("x.pdf", tiny_pdf([secret]))])), docs.R_ZIP_LOCKED),
        "b.zip": (b"PK\x03\x04" + b"\x00" * 50, docs.R_ZIP_BROKEN),
        "c.zip": (zip_of([("d/", b""), ("note.txt", secret)]), docs.R_ZIP_EMPTY),
        "d.docx": (score_docx()[:300], docs.R_DOCX_BROKEN),
        "e.docx": (docx_bytes('<!DOCTYPE x [<!ENTITY a "aaaa">]>' + w_p(secret)), docs.R_DOCX_BROKEN),
        "f.docx": (make_cfb([("EncryptionInfo", b"x" * 10), ("EncryptedPackage", b"y" * 100)]), docs.R_OFFICE_LOCKED),
        "g.pptx": (b"PK\x03\x04broken", docs.R_PPTX_BROKEN),
        "h.png": (PNG, docs.R_IMAGE),
        "i.jpg": (b"\xff\xd8\xff\xe0" + b"\0" * 20, docs.R_IMAGE),
        "j.pdf": (tiny_pdf([]), docs.R_SCAN),                                   # 글자 없는 PDF(스캔본처럼)
        "k.pdf": (b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << /Encrypt 2 0 R /Root 1 0 R >>\n%%EOF", docs.R_PDF_LOCKED),
        "l.ppt": (make_cfb([("PowerPoint Document", b"x" * 10)]), docs.R_PPT_OLD),
        "m.7z": (b"7z\xbc\xaf\x27\x1c" + b"\0" * 30, docs.R_ARCHIVE),
        "n.bin": (b"\x00\x01\x02 random", docs.R_UNSUPPORTED),
        "o.docx": (docx_bytes(""), docs.R_NO_TEXT),
    }
    for name, (data, reason) in cases.items():
        assert docs.read(name, data) is None, name
        r = docs.read_detail(name, data)
        assert r["ok"] is False and r["text"] == "" and r["notes"][0] == reason, (name, r["notes"])
        dumped = json.dumps(r, ensure_ascii=False)
        assert "2099123456" not in dumped and name not in dumped, name      # 이유에 내용·파일 이름 없음
    assert docs.read_detail("x", "not bytes")["notes"] == [docs.R_UNSUPPORTED]
    assert docs.read_detail("big.docx", b"PK\x03\x04" + b"\0" * 10)["kind"] == "docx"


def test_file_size_limit(monkeypatch):
    monkeypatch.setattr(docs, "MAX_FILE", 100)
    assert docs.read_detail("a.docx", score_docx())["notes"] == [docs.R_FILE_BIG]


# ---------------------------------------------------------------- 받기·성적 후보(materials)

def att(i, filename, data):
    return {"id": i, "filename": filename, "filesize": len(data), "canvas_file_id": 7000 + i,
            "url": f"https://lms.example/files/{7000 + i}/download?verifier=SECRET"}


def board(atts, course_id="77"):
    return {"course_id": course_id, "tool_id": None, "board_id": "announcements", "board_title": "공지", "source": "canvas",
            "posts": [{"id": 5, "title": "성적 안내", "content": "", "created_at": "2026-11-10T01:00:00Z",
                       "updated_at": "2026-11-10T01:00:00Z", "attachments": atts}]}


def fake_get(contents):
    calls = []

    def get(a):
        calls.append(a["id"])
        return contents[a["id"]]
    get.calls = calls
    return get


def _store(tmp_path, recs: dict, sid=SID):
    s = Store(tmp_path / "state.json")
    s.data["cache"] = {"courses": [{"id": "77", "name": "VLSI 설계"}], "assignments": [], "announcements": [],
                       "conversations": []}
    s.data["pdf_texts"] = recs
    s.data["settings"]["student_id"] = sid
    return s


def _pdf_cands(store):
    return [c for c in gradebook.candidates(store) if c.get("source_type") == "pdf"]


def test_fetch_docx_to_pdf_texts_to_score_candidate(tmp_path):
    data = score_docx()
    bs = [board([att(1, "기말_성적.docx", data), att(2, "사진.png", PNG)])]
    jobs = materials.plan(bs, {}, {"77": "VLSI 설계"}, tmp_path)
    now = datetime(2026, 11, 15, 9, 0).astimezone()
    updates, texts, errors = materials.fetch(jobs, fake_get({1: data, 2: PNG}), now=now)
    assert errors == [] and sorted(texts) == ["77:1"]
    m = updates["77:1"]
    assert (m["read_ok"], m["read_kind"], m["doc_kind"], m["doc_version"], m["pdf_ok"]) == \
        (True, "docx", "docx", materials.DOC_VERSION, None)
    assert "sheet_ok" not in m and m["doc_read_at"] == m["downloaded_at"]
    t = texts["77:1"]
    assert (t["kind"], t["doc_kind"], t["filename"], t["course_id"], t["board_id"], t["post"], t["canvas_file_id"]) == \
        ("doc", "docx", "기말_성적.docx", "77", "announcements", 5, 7001)
    assert f"8 {SID} 나*빈 88" in t["text"].splitlines()
    img = updates["77:2"]
    assert (img["read_ok"], img["read_kind"], img["read_notes"]) == (False, "image", [docs.R_IMAGE])
    s = _store(tmp_path, texts)
    (c,) = _pdf_cands(s)
    assert (c["key"], c["label"], c["score"], c["max_score"], c["n"], c["rank"], c["file_kind"]) == \
        ("pdf:77:1", "기말고사", 88, 100, 7, 2, "pdf")                               # 이름은 열 머리글에서
    assert "2099000001" not in json.dumps(c, ensure_ascii=False)
    # 마감 찾기 출처(core.pdf_source)에도 그대로 들어간다
    cache = {"pdf_texts": texts, "boards": bs}
    srcs = core.pdf_source(cache, [], now)
    assert [s["title"] for s in srcs] == ["첨부 '기말_성적.docx'"]
    # 받아 둔 파일·기록이 최신이면 다음 계획에 다시 읽기가 없다
    assert materials.plan(bs, updates, {"77": "VLSI 설계"}, tmp_path, texts=texts) == []


def test_fetch_zip_stores_members_separately_and_both_score_tables_become_candidates(tmp_path):
    z = zip_of([("Lab0_score.pdf", score_table_pdf("Lab0")), ("Quiz2_성적.xlsx", score_table_xlsx()),
                ("photo.png", PNG)])
    bs = [board([att(1, "성적 모음.zip", z)])]
    jobs = materials.plan(bs, {}, {"77": "VLSI 설계"}, tmp_path)
    updates, texts, errors = materials.fetch(jobs, fake_get({1: z}))               # PDF 읽기는 pdftext(기본)
    assert errors == []
    assert sorted(texts) == ["77:1/Lab0_score.pdf", "77:1/Quiz2_성적.xlsx"] and "77:1" not in texts
    pdf, sheet = texts["77:1/Lab0_score.pdf"], texts["77:1/Quiz2_성적.xlsx"]
    assert (pdf["kind"], pdf["member_kind"], pdf["filename"], pdf["zip_name"], pdf["parent_key"]) == \
        ("zip:Lab0_score.pdf", "pdf", "Lab0_score.pdf", "성적 모음.zip", "77:1")
    assert (sheet["kind"], sheet["sheet_kind"], sheet["member_kind"], sheet["parent_key"]) == ("sheet", "xlsx", "xlsx", "77:1")
    assert all(isinstance(k, str) for r in sheet["sheets"][0]["rows"] for k in r)
    assert pdf["canvas_file_id"] == 7001 and (pdf["board_id"], pdf["post"]) == ("announcements", 5)
    m = updates["77:1"]
    assert (m["read_ok"], m["read_kind"]) == (True, "zip")
    assert [(z["name"], z["kind"], z["ok"], z["key"]) for z in m["zip_members"]] == [
        ("Lab0_score.pdf", "pdf", True, "77:1/Lab0_score.pdf"), ("Quiz2_성적.xlsx", "sheet", True, "77:1/Quiz2_성적.xlsx"),
        ("photo.png", "image", False, None)]
    assert m["zip_members"][2]["notes"] == [docs.R_IMAGE]
    s = _store(tmp_path, texts, sid=DEMO_STUDENT_ID)
    cands = sorted(_pdf_cands(s), key=lambda c: c["key"])
    assert [(c["key"], c["label"], c["score"], c["file_kind"]) for c in cands] == [
        ("pdf:77:1/Lab0_score.pdf", "Lab0", 44, "pdf"), ("pdf:77:1/Quiz2_성적.xlsx", "Quiz 2", 9, "sheet")]
    # 다시 받은 zip에서 빠진 안 파일의 옛 기록은 지운다
    data = {"downloads": {}, "pdf_texts": dict(texts)}
    materials.apply(data, {"77:1": m}, {"77:1/Lab0_score.pdf": pdf}, [])
    assert sorted(data["pdf_texts"]) == ["77:1/Lab0_score.pdf"]


# ---------------------------------------------------------------- 받아 둔 파일 다시 읽기

def _old_entry(path: Path, key="77:1", filename=None, posted="2026-11-10T10:00:00+09:00"):
    """v0.8.1까지 받아 둔 기록 모양(문서 칸·읽음 표시 없음)."""
    return {"path": str(path), "filename": filename or path.name, "size": path.stat().st_size, "course_id": "77",
            "course_short": "VLSI 설계", "board_title": "공지", "post_title": "성적 안내",
            "post_key": "board:announcements-5", "board_id": "announcements", "post": 5, "canvas_file_id": 7001,
            "posted_at": posted, "downloaded_at": "2026-11-11T09:00:00+09:00", "pdf_ok": None, "pdf_notes": []}


def _put(folder: Path, name: str, data: bytes) -> Path:
    p = folder / "VLSI 설계" / "공지" / "성적 안내" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_reread_already_downloaded_docs_and_zip_without_downloading(tmp_path, monkeypatch):
    folder = tmp_path / "dl"
    d = _put(folder, "기말_성적.docx", score_docx())
    z = _put(folder, "모음.zip", zip_of([("Lab0_score.pdf", score_table_pdf("Lab0")), ("a.hwp", hwp_bytes(HWP_SECTION))]))
    h = _put(folder, "안내.pptx", b"PK\x03\x04 not really")
    outside = _put(tmp_path / "elsewhere", "밖.docx", score_docx())
    data = {"cache": {"courses": [{"id": "77", "name": "VLSI 설계"}], "boards": []},
            "downloads": {"77:1": _old_entry(d), "77:2": _old_entry(z, "77:2"), "77:3": _old_entry(h, "77:3"),
                          "77:4": _old_entry(outside, "77:4")},
            "pdf_texts": {}, "settings": {}}
    get = fake_get({})
    report = materials.run(data, folder, get)
    assert get.calls == [] and report == {"planned": 0, "downloaded": 0, "pdf_texts": 3, "errors": [], "reread": 3}
    assert sorted(data["pdf_texts"]) == ["77:1", "77:2/Lab0_score.pdf", "77:2/a.hwp"]
    m1, m2, m3, m4 = (data["downloads"][k] for k in ("77:1", "77:2", "77:3", "77:4"))
    assert m1["read_ok"] is True and m1["downloaded_at"] == "2026-11-11T09:00:00+09:00" and m1["path"] == str(d)
    assert data["pdf_texts"]["77:2/a.hwp"]["kind"] == "zip:a.hwp" and "기말고사 성적 안내" in data["pdf_texts"]["77:2/a.hwp"]["text"]
    assert m2["read_ok"] is True and [x["key"] for x in m2["zip_members"]] == ["77:2/Lab0_score.pdf", "77:2/a.hwp"]
    assert (m3["read_ok"], m3["read_notes"][0]) == (False, docs.R_PPTX_BROKEN)       # 못 읽은 이유를 남기고 다시 시도 안 함
    assert "read_ok" not in m4 and "doc_version" not in m4                           # 다운로드 폴더 밖은 읽지 않는다
    s = _store(tmp_path, data["pdf_texts"])
    assert [c["score"] for c in _pdf_cands(s) if c["key"] == "pdf:77:1"] == [88]
    # 두 번째는 할 일이 없다(못 읽은 pptx도 다시 보지 않음)
    assert materials.run(data, folder, get) == {"planned": 0, "downloaded": 0, "pdf_texts": 0, "errors": []}
    # 읽은 기록이 지워졌으면 다시 읽는다(zip은 안 파일 하나라도)
    data["pdf_texts"].pop("77:2/a.hwp")
    assert materials.run(data, folder, get)["reread"] == 1 and "77:2/a.hwp" in data["pdf_texts"]
    # 읽기 판이 오르면 모두 다시
    monkeypatch.setattr(materials, "DOC_READER", materials.DOC_READER + 1)
    assert materials.run(data, folder, get)["reread"] == 3


def test_old_entries_get_read_flags_without_reading_disk(tmp_path):
    folder = tmp_path / "dl"
    p = _put(folder, "x.pdf", b"%PDF-")
    downloads = {
        "77:1": {**_old_entry(p), "pdf_ok": False, "pdf_notes": ["PDF에서 글자를 찾지 못했습니다(스캔본일 수 있습니다)"]},
        "77:2": {**_old_entry(p, "77:2", "y.pdf"), "pdf_ok": True, "pdf_notes": ["2쪽"]},
        "77:3": {**_old_entry(p, "77:3", "Quiz.xlsx"), "sheet_ok": True, "sheet_notes": []},
        "77:4": {**_old_entry(p, "77:4", "사진.JPG")},
        "77:5": {**_old_entry(p, "77:5", "lecture.mp4")},
        "77:6": {**_old_entry(p, "77:6", "z.pdf")},             # PDF 읽기 없이 받은 것 — 모름
        "77:7": {**_old_entry(p, "77:7", "a.hwp")},             # 다시 읽기 차례 — 모름
    }
    materials.backfill_read_flags(downloads)
    assert (downloads["77:1"]["read_ok"], downloads["77:1"]["read_notes"][0]) == (False, docs.R_SCAN)
    assert (downloads["77:2"]["read_ok"], downloads["77:2"]["read_notes"]) == (True, ["2쪽"])
    assert (downloads["77:3"]["read_ok"], downloads["77:3"]["read_kind"]) == (True, "sheet")
    assert (downloads["77:4"]["read_ok"], downloads["77:4"]["read_notes"]) == (False, [docs.R_IMAGE])
    assert downloads["77:5"]["read_notes"] == [docs.R_UNSUPPORTED]
    assert "read_ok" not in downloads["77:6"] and "read_ok" not in downloads["77:7"]
    assert docs.pdf_notes(False, ["암호화된 PDF라 읽지 못했습니다"])[0] == docs.R_PDF_LOCKED
    assert docs.pdf_notes(False, ["PDF 구조가 깨져 있어 읽지 못했습니다"])[0] == docs.R_PDF_BROKEN


def test_fresh_pdf_gets_read_flags(tmp_path):
    bs = [board([att(1, "scan.pdf", b"%PDF-1.4 x"), att(2, "ok.pdf", b"%PDF-1.4 y")])]
    jobs = materials.plan(bs, {}, {"77": "VLSI 설계"}, tmp_path)
    updates, texts, _ = materials.fetch(jobs, fake_get({1: b"%PDF-1.4 x", 2: b"%PDF-1.4 y"}),
                                        pdf_reader=lambda d: "Lab 1 due 11/18" if d.endswith(b"y") else "")
    assert (updates["77:1"]["read_ok"], updates["77:1"]["read_kind"], updates["77:1"]["read_notes"][0]) == \
        (False, "pdf", docs.R_SCAN)
    assert "스캔본" in updates["77:1"]["pdf_notes"][0]                              # 옛 칸은 그대로
    assert (updates["77:2"]["read_ok"], updates["77:2"]["read_notes"]) == (True, []) and list(texts) == ["77:2"]
