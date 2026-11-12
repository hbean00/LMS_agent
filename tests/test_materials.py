"""첨부 자료(계약 10장 B): 받을 것 고르기, 안전한 저장 위치, 이름 겹침, 받기·PDF 글자, 화면 API(목록·열기·설정)."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from lms_agent import materials
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.features import materials as feature  # noqa: E402 — 등록(FEATURES에 들어가기 전에도 시험되게)

SECRET = "verifier=SECRETVALUE"
PDF = b"%PDF-1.4 fake"


def att(i, filename, size=None, data=None):
    return {"id": i, "filename": filename, "filesize": size if size is not None else len(data or PDF),
            "canvas_file_id": 6000000 + i, "url": f"https://lms.example/files/{6000000 + i}/download?{SECRET}"}


def board(posts, title="과제", course_id="95079", board_id=83605):
    return {"course_id": course_id, "tool_id": 5, "board_id": board_id, "board_title": title, "posts": posts}


def post(pid, title, attachments, created="2026-11-10T01:00:00Z"):
    return {"id": pid, "title": title, "content": "<p>본문</p>", "created_at": created, "updated_at": created,
            "attachments": attachments}


SHORTS = {"95079": "ASIC설계(영강)", "95080": "회로이론"}


def fake_get(contents):
    """attachment id → bytes(또는 예외). 받은 순서를 calls에 남긴다."""
    calls = []

    def get(a):
        calls.append(a["id"])
        v = contents[a["id"]]
        if isinstance(v, Exception):
            raise v
        return v
    get.calls = calls
    return get


# ------------------------------------------------------------ plan

def test_plan_skips_same_size_and_redownloads_deleted_or_changed(tmp_path):
    bs = [board([post(1, "Lab3 Assignment", [att(10001, "Lab3.pdf")])])]
    (job,) = materials.plan(bs, {}, SHORTS, tmp_path)
    assert job["key"] == "95079:10001"
    assert Path(job["dest"]) == tmp_path / "ASIC설계(영강)" / "과제" / "Lab3 Assignment" / "Lab3.pdf"
    assert job["post_key"] == "board:83605-1" and job["posted_at"].startswith("2026-11-10")

    updates, _, errors = materials.fetch([job], fake_get({10001: PDF}))
    assert not errors and Path(updates[job["key"]]["path"]).read_bytes() == PDF
    assert materials.plan(bs, updates, SHORTS, tmp_path) == []                     # 같은 id·크기, 파일 있음 → 건너뜀

    bs[0]["posts"][0]["attachments"][0]["filesize"] = 999                           # 교수가 파일을 바꿈
    (changed,) = materials.plan(bs, updates, SHORTS, tmp_path)
    assert Path(changed["dest"]).name == "Lab3 (2).pdf"                             # 있던 파일은 덮어쓰지 않는다
    bs[0]["posts"][0]["attachments"][0]["filesize"] = len(PDF)

    os.remove(updates[job["key"]]["path"])                                          # 사용자가 지움 → 다시 받는다
    (again,) = materials.plan(bs, updates, SHORTS, tmp_path)
    assert again["dest"] == job["dest"]

    other = tmp_path.parent / (tmp_path.name + "-new")                              # 저장 폴더를 바꾸면 새 폴더에 받는다
    (moved,) = materials.plan(bs, {job["key"]: {**updates[job["key"]], "path": job["dest"]}}, SHORTS, other)
    assert materials.inside(moved["dest"], other)


@pytest.mark.parametrize("title", ["..\\..\\x", "../../x", "..", ".", "con", "CON.txt", "nul", "com1", "LPT9.pdf",
                                   "a/b", "a\\b", "C:\\Windows\\System32", "/etc/passwd", "trailing dots...",
                                   "space ", "   ", "", "<>:\"|?*", "탭\t줄\n바꿈", "x" * 300])
def test_destination_is_safe_and_inside(tmp_path, title):
    root = tmp_path / "root"
    bs = [board([post(1, title, [att(1, title or "f")])], title=title, course_id="7")]
    (job,) = materials.plan(bs, {}, {"7": title}, root)
    dest = Path(job["dest"])
    assert materials.inside(dest, root)
    rel = dest.relative_to(root).parts
    assert len(rel) == 4                                                             # 과목/게시판/글/파일 — 칸이 늘지 않음
    for p in rel:
        assert p not in ("", ".", "..") and not p.endswith((".", " ")) and p == p.strip()
        assert not any(c in p for c in '<>:"/\\|?*\t\n')
        assert p.split(".")[0].upper() not in ("CON", "NUL", "COM1", "LPT9"), p
    assert len(str(dest)) + materials._SPARE <= materials.PATH_MAX                    # Windows 경로 한도
    assert len(str(dest.parent)) <= materials.DIR_MAX
    updates, _, errors = materials.fetch([job], fake_get({1: b"data"}))
    assert not errors and Path(updates[job["key"]]["path"]).read_bytes() == b"data"


def test_long_download_folder_still_fits(tmp_path):
    root = tmp_path.joinpath(*(["깊은 폴더 이름"] * 12))
    assert len(str(root)) > 150
    bs = [board([post(1, "아주 긴 글 제목 " * 10, [att(1, "강의자료 " * 30 + ".pdf")])], title="강의자료실 " * 10)]
    (job,) = materials.plan(bs, {}, {"95079": "과목 이름 " * 10}, root)
    dest = Path(job["dest"])
    assert len(str(dest)) + materials._SPARE <= materials.PATH_MAX and dest.name.endswith(".pdf")
    assert len(str(dest.parent)) <= materials.DIR_MAX
    updates, _, errors = materials.fetch([job], fake_get({1: PDF}))
    assert not errors and Path(updates[job["key"]]["path"]).read_bytes() == PDF


def test_file_name_keeps_extension_when_long():
    name = materials.file_name("아주 긴 강의자료 제목 " * 20 + ".pptx")
    assert name.endswith(".pptx") and len(name) <= materials.LIMITS["file"]
    assert materials.file_name("CON.pdf") == "_CON.pdf"
    assert materials.file_name("report.pdf.") == "report.pdf"


def test_collisions_get_numbered(tmp_path):
    d = tmp_path / "ASIC설계(영강)" / "과제" / "공지"
    bs = [board([post(1, "공지", [att(1, "Lab.pdf"), att(2, "Lab.pdf")]),            # 같은 글에 같은 이름
                 post(2, "공지", [att(3, "Lab.pdf")])])]                             # 같은 제목의 다른 글
    d.mkdir(parents=True)
    (d / "Lab.pdf").write_bytes(b"user's own")                                       # 사용자가 둔 파일
    jobs = materials.plan(bs, {}, SHORTS, tmp_path)
    assert [Path(j["dest"]).name for j in jobs] == ["Lab (2).pdf", "Lab (3).pdf", "Lab (4).pdf"]
    updates, _, errors = materials.fetch(jobs, fake_get({1: b"a", 2: b"b", 3: b"c"}))
    assert not errors and [Path(m["path"]).read_bytes() for m in updates.values()] == [b"a", b"b", b"c"]
    assert (d / "Lab.pdf").read_bytes() == b"user's own"

    other = [board([post(1, "글", [att(1, "Lab.pdf"), att(2, "lab.PDF")])])]        # 대소문자만 다름(Windows에선 같은 파일)
    assert [Path(j["dest"]).name for j in materials.plan(other, {}, SHORTS, tmp_path / "x")] ==         ["Lab.pdf", "lab (2).PDF"]


def test_link_inside_folder_pointing_outside_is_rejected(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "ASIC설계(영강)", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 PC에서는 링크를 만들 수 없습니다")
    bs = [board([post(1, "Lab", [att(1, "a.pdf")])])]
    assert materials.plan(bs, {}, SHORTS, root) == []
    forged = {"key": "95079:1", "dest": str(root / "ASIC설계(영강)" / "a.pdf"), "root": str(root),
              "filename": "a.pdf", "size": 1, "attachment": {"id": 1}}
    updates, _, errors = materials.fetch([forged], fake_get({1: b"x"}))
    assert updates == {} and "폴더 밖" in errors[0] and not list(outside.iterdir())


# ------------------------------------------------------------ fetch

def test_fetch_writes_records_pdf_and_survives_failure(tmp_path):
    bs = [board([post(1, "Lab3 Assignment", [att(1, "Lab3.pdf"), att(2, "broken.pdf"), att(3, "slides.pptx")]),
                 post(2, "스캔", [att(4, "scan.pdf")])])]
    jobs = materials.plan(bs, {}, SHORTS, tmp_path)
    get = fake_get({1: PDF, 2: OSError(f"HTTP 403 https://lms.example/x?{SECRET}"), 3: b"PK\x03\x04", 4: PDF + b"scan"})
    read = []

    def reader(data):
        read.append(data)
        return ("Lab3 due 23:59 on 11/18 " + "가" * 70_000, ["2쪽"]) if data == PDF else ""

    now = datetime(2026, 11, 13, 9, 0).astimezone()
    updates, texts, errors = materials.fetch(jobs, get, pdf_reader=reader, now=now)

    assert get.calls == [1, 2, 3, 4]                                                 # 하나가 실패해도 나머지는 계속
    assert errors == ["'broken.pdf': 내려받지 못했습니다"]                           # 파일 이름만(주소·verifier 없음)
    assert sorted(updates) == ["95079:1", "95079:3", "95079:4"]
    m = updates["95079:1"]
    assert Path(m["path"]).read_bytes() == PDF and m["size"] == len(PDF)
    assert m["pdf_ok"] is True and m["pdf_notes"] == ["2쪽"] and m["downloaded_at"].startswith("2026-11-13T09:00")
    assert (m["course_id"], m["board_title"], m["post_title"], m["post_key"]) == \
        ("95079", "과제", "Lab3 Assignment", "board:83605-1")
    assert updates["95079:3"]["pdf_ok"] is None                                      # PDF가 아니면 읽지 않는다
    assert updates["95079:4"]["pdf_ok"] is False and "스캔본" in updates["95079:4"]["pdf_notes"][0]
    assert len(read) == 2
    t = texts["95079:1"]
    assert len(t["text"]) == materials.PDF_TEXT_LIMIT and t["text"].startswith("Lab3 due")
    assert (t["course_id"], t["filename"], t["post_key"]) == ("95079", "Lab3.pdf", "board:83605-1")
    assert t["posted_at"].startswith("2026-11-10")
    assert list(texts) == ["95079:1"]
    assert not list(tmp_path.rglob("*.part"))                                        # 임시 파일이 남지 않는다


def test_fetch_size_limit_reader_crash_and_bad_payload(tmp_path):
    bs = [board([post(1, "자료", [att(1, "big.zip", size=60_000_000), att(2, "liar.zip", size=10),
                                  att(3, "crash.pdf"), att(4, "none.bin")])])]
    jobs = materials.plan(bs, {}, SHORTS, tmp_path)
    get = fake_get({1: b"", 2: b"x" * 2_000, 3: PDF, 4: None})

    def reader(data):
        raise RuntimeError("bad xref")

    updates, texts, errors = materials.fetch(jobs, get, max_bytes=1_000, pdf_reader=reader)
    assert get.calls == [2, 3, 4]                                                   # 크기를 알면 받기 전에 거른다
    assert errors == ["'big.zip': 0.001MB보다 커서 받지 않았습니다", "'liar.zip': 0.001MB보다 커서 받지 않았습니다",
                      "'none.bin': 내려받지 못했습니다"]
    assert list(updates) == ["95079:3"] and texts == {}
    assert updates["95079:3"]["pdf_ok"] is False and updates["95079:3"]["pdf_notes"] == ["PDF를 읽지 못했습니다"]
    assert not (tmp_path / "ASIC설계(영강)" / "과제" / "자료" / "big.zip").exists()


def test_fetch_does_not_overwrite_file_created_after_plan(tmp_path):
    (job,) = materials.plan([board([post(1, "Lab", [att(1, "a.pdf")])])], {}, SHORTS, tmp_path)
    Path(job["dest"]).parent.mkdir(parents=True)
    Path(job["dest"]).write_bytes(b"mine")
    updates, _, errors = materials.fetch([job], fake_get({1: PDF}))
    assert not errors and Path(job["dest"]).read_bytes() == b"mine"
    assert Path(updates[job["key"]]["path"]).name == "a (2).pdf"


def test_run_and_apply_into_store_data(tmp_path):
    data = {"cache": {"courses": [{"id": "95079", "name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반"}],
                      "boards": [board([post(1, "Lab3", [att(1, "Lab3.pdf")])])]},
            "pdf_texts": {"95079:1": {"text": "old"}}, "settings": {}}
    report = materials.run(data, tmp_path, fake_get({1: PDF}), pdf_reader=lambda d: "새 마감 11/18")
    assert report == {"planned": 1, "downloaded": 1, "pdf_texts": 1, "errors": []}
    assert data["downloads"]["95079:1"]["course_short"] == "ASIC설계(영강)"
    assert data["pdf_texts"]["95079:1"]["text"] == "새 마감 11/18" and data["materials_errors"] == []
    assert materials.run(data, tmp_path, fake_get({}))["planned"] == 0               # 두 번째는 받을 것 없음

    materials.apply(data, {"95079:1": {**data["downloads"]["95079:1"], "pdf_ok": False}}, {}, ["'x': 내려받지 못했습니다"])
    assert "95079:1" not in data["pdf_texts"]                                        # 다시 받았는데 글자가 없으면 옛 글자도 지움
    assert data["materials_errors"] == ["'x': 내려받지 못했습니다"]


def test_default_folder():
    f = materials.default_folder()
    assert f.is_absolute() and f.name == "LMS Agent"


# ------------------------------------------------------------ 화면 API

class Opener:
    def __init__(self):
        self.opened = []

    def __call__(self, path):
        self.opened.append(Path(path))


class NoScheduler:
    """실제 작업 스케줄러를 건드리지 않게(데모가 아닌 App)."""

    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


def serve(app):
    httpd = start_server(app, 0)
    serve_in_thread(httpd)

    def post_(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(),
                                     method="POST",
                                     headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    return httpd, post_


@pytest.fixture()
def demo_app(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    opener = Opener()
    app = App(Store(home / "state.json"), home, cfg={}, demo=True, opener=opener)
    folder = feature.download_folder(app)
    assert folder == home / "downloads"
    bs = [board([post(1, "Lab3 Assignment", [att(1, "Lab3.pdf")], created="2026-11-10T01:00:00Z"),
                 post(2, "Lab4 Assignment", [att(2, "Lab4.pdf"), att(3, "tool.exe")], created="2026-11-12T01:00:00Z")])]
    app.store.data["cache"] = {"courses": [{"id": "95079", "name": "ASIC설계(영강)"}], "boards": bs}
    materials.run(app.store.data, folder, fake_get({1: PDF, 2: PDF, 3: b"MZ"}), pdf_reader=lambda d: "마감 11/18")
    app.store.data["materials_errors"] = ["'x.pdf': 내려받지 못했습니다"]
    app.store.save()
    httpd, post_ = serve(app)
    yield app, post_, opener
    httpd.shutdown()


def test_state_section(demo_app):
    app, post_, _ = demo_app
    st = app.state()["materials"]
    assert st["folder"] == str(app.home / "downloads") and st["auto_download"] is True
    assert st["count"] == 3 and st["errors"] == ["'x.pdf': 내려받지 못했습니다"]
    assert [i["filename"] for i in st["items"]][:2] in (["Lab4.pdf", "tool.exe"], ["tool.exe", "Lab4.pdf"])
    assert st["items"][-1]["filename"] == "Lab3.pdf"                                 # 최신 글 먼저
    first = st["items"][-1]
    assert first == {"key": "95079:1", "course_short": "ASIC설계(영강)", "board_title": "과제",
                     "post_title": "Lab3 Assignment", "filename": "Lab3.pdf", "size": len(PDF),
                     "posted_at": first["posted_at"], "downloaded_at": first["downloaded_at"], "pdf_ok": True,
                     "post_key": "board:83605-1", "exists": True}
    assert "path" not in first                                                       # 전체 경로는 화면에 주지 않는다


def test_open_file_and_folder_only_inside(demo_app, tmp_path):
    app, post_, opener = demo_app
    assert post_("/api/materials/open", {"key": "95079:1"}) == (200, {"ok": True})
    assert opener.opened[-1] == Path(app.store.data["downloads"]["95079:1"]["path"])
    assert post_("/api/materials/open", {}) == (200, {"ok": True})
    assert opener.opened[-1] == app.home / "downloads"

    code, err = post_("/api/materials/open", {"key": "nope:1"})
    assert code == 404 and "목록에 없습니다" in err["error"]
    code, err = post_("/api/materials/open", {"key": "95079:3"})
    assert code == 400 and "실행 파일" in err["error"]

    outside = tmp_path / "secret.txt"                                                # state.json이 조작돼 밖을 가리켜도
    outside.write_text("x")
    app.store.data["downloads"]["95079:9"] = {"path": str(outside), "filename": "secret.txt"}
    app.store.data["downloads"]["95079:8"] = {"path": str(app.home / "downloads" / ".." / "state.json"),
                                              "filename": "state.json"}
    app.store.save()
    for k in ("95079:9", "95079:8"):
        code, err = post_("/api/materials/open", {"key": k})
        assert code == 403 and "폴더 밖" in err["error"]
    os.remove(app.store.data["downloads"]["95079:2"]["path"])
    code, err = post_("/api/materials/open", {"key": "95079:2"})
    assert code == 404 and "다시 받습니다" in err["error"]
    assert len(opener.opened) == 2
    assert {i["key"]: i["exists"] for i in app.state()["materials"]["items"]}["95079:2"] is False


def test_settings_in_demo(demo_app):
    app, post_, _ = demo_app
    code, st = post_("/api/materials/settings", {"auto_download": False})
    assert code == 200 and st["materials"]["auto_download"] is False
    assert feature.download_folder(app) is None
    code, err = post_("/api/materials/settings", {"auto_download": "yes"})
    assert code == 400
    code, err = post_("/api/materials/settings", {"download_dir": str(app.home / "elsewhere")})
    assert code == 400 and "데모" in err["error"]
    code, st = post_("/api/settings", {"auto_download": True, "tutorial_done": True})   # 계약: /api/settings에도
    assert code == 200 and st["materials"]["auto_download"] is True and st["settings"]["tutorial_done"] is True
    assert Store(app.home / "state.json").data["settings"]["auto_download"] is True       # 저장됨


def test_settings_download_dir_validation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    app = App(Store(home / "state.json"), home, cfg={}, demo=False, opener=Opener(), scheduler=NoScheduler())
    httpd, post_ = serve(app)
    try:
        code, st = post_("/api/materials/settings", {})
        assert code == 200 and Path(st["materials"]["folder"]).name == "LMS Agent"        # 기본 = 문서\LMS Agent
        for bad in ("relative\\dir", "dir", 5, "x" * 500):
            code, err = post_("/api/materials/settings", {"download_dir": bad})
            assert code == 400, bad
        a_file = tmp_path / "file.txt"
        a_file.write_text("x")
        code, err = post_("/api/materials/settings", {"download_dir": str(a_file), "auto_download": False})
        assert code == 400 and "파일" in err["error"]
        assert app.store.data["settings"].get("auto_download", True) is True              # 틀리면 아무것도 안 바뀜

        target = tmp_path / "새 폴더" / "자료"
        code, st = post_("/api/materials/settings", {"download_dir": str(target)})
        assert code == 200 and target.is_dir() and Path(st["materials"]["folder"]) == target.resolve()
        assert feature.download_folder(app) == target.resolve()
        assert Store(home / "state.json").data["settings"]["download_dir"] == str(target.resolve())
        code, err = post_("/api/settings", {"download_dir": "nope"})                     # /api/settings도 같은 확인
        assert code == 400
        code, st = post_("/api/settings", {"download_dir": None})
        assert code == 200 and Path(st["materials"]["folder"]).name == "LMS Agent"
    finally:
        httpd.shutdown()
