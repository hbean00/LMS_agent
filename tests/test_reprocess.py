"""받아 둔 파일 다시 읽기(계약 29-3, 사용자 제보 2026-11-19: "업데이트가 되면서 이미 받은 파일들에 대해 성적 반영을 안 하는 문제").

리드 진단: 표·문서는 읽기 판이 있어 규칙이 바뀌면 다시 읽지만 PDF는 판이 없어 한 번 받은 PDF는 다시 읽지 않았고,
core._download_new가 materials.plan에 texts(pdf_texts)를 넘기지 않아 글 기록이 빠진 파일도 다시 읽지 않았다.
- PDF 읽기 판(pdftext.VERSION → 다운로드 기록의 pdf_version·pdf_reader), 판이 낮거나 글 기록이 없으면 디스크에서 다시 읽음.
- 앱 버전이 바뀌면(migrate) store.data["reprocess"] → 다음 동기화들에서 읽을 수 있는 받아 둔 파일을 모두(동기화당 200개) 다시 읽고 지움.
- POST /api/materials/reread {key?}(하나 또는 전부, 백그라운드), 능력 materials.reread, state.materials.reprocess.
가짜 Canvas(mock_canvas)·지어낸 학번·점수만 쓴다(네트워크 없음)."""
from __future__ import annotations   # 파이썬 3.9에서도 `X | None` 표기를 읽게(CI)

from pathlib import Path

import pytest

from lms_agent import capabilities, core, gradebook, materials, migrate, pdftext
from lms_agent.canvas import Canvas
from lms_agent.features import materials as feature
from lms_agent.mock_canvas import DEMO_STUDENT_ID, DEMO_TOKEN, start_mock_canvas, tiny_pdf
from lms_agent.server import App
from lms_agent.store import Store

SID = "2099123456"   # 가짜 학번


class _Bare:
    data = {"settings": {"auto_download": False}}


def pdf_reader():
    return core.sync_options(_Bare())["pdf_reader"]


def score_pdf(label="Quiz 3", mine=9) -> bytes:
    rows = [(f"20990000{i:02d}", s) for i, s in enumerate([6, 7, 8, 5, 10], 1)] + [(SID, mine)]
    return tiny_pdf([f"{label} Score", "Student ID Score (10)"] + [f"{a} {b}" for a, b in rows])


def _entry(path: Path, key="77:1", filename="Quiz3_score.pdf", posted="2026-11-10T10:00:00+09:00", **extra) -> dict:
    """v0.8.3까지 받아 둔 PDF 기록 모양(pdf_version·pdf_reader 없음, 글자를 읽었음)."""
    m = {"path": str(path), "filename": filename, "size": path.stat().st_size, "course_id": "77",
         "course_short": "디지털통신", "board_title": "공지", "post_title": "Quiz 3 결과",
         "post_key": "board:announcements-5", "board_id": "announcements", "post": 5, "canvas_file_id": 7001,
         "posted_at": posted, "downloaded_at": "2026-11-11T09:00:00+09:00", "pdf_ok": True, "pdf_notes": [],
         "read_ok": True, "read_notes": [], "read_kind": "pdf"}
    m.update(extra)
    return m


def _put(folder: Path, name: str, data: bytes) -> Path:
    p = folder / "디지털통신" / "공지" / "Quiz 3 결과" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _store(tmp_path: Path, downloads: dict, texts: dict | None = None) -> Store:
    s = Store(tmp_path / "state.json")
    s.data["settings"]["student_id"] = SID
    s.data["cache"] = {"fetched_at": "2026-11-12T10:00:00+09:00", "courses": [{"id": "77", "name": "디지털통신"}],
                       "assignments": [], "conversations": [], "announcements": [], "boards": [], "errors": [],
                       "pdf_texts": dict(texts or {})}
    s.data["downloads"] = downloads
    s.data["pdf_texts"] = dict(texts or {})
    return s


def _cands(store):
    return [c for c in gradebook.candidates(store) if c.get("source_type") == "pdf"]


# ================================================================ 버그 재현(고치기 전에는 실패)

def test_bug_downloaded_pdf_without_text_record_is_reread_by_the_sync_path(tmp_path):
    """받아 둔 PDF인데 pdf_texts 기록이 없다(옛 버전·지워진 기록) → 동기화(core._download_new)가 디스크에서 다시 읽는다."""
    folder = tmp_path / "dl"
    p = _put(folder, "Quiz3_score.pdf", score_pdf())
    store = _store(tmp_path, {"77:1": _entry(p)})
    assert _cands(store) == []                                      # 지금: 글 기록이 없어 성적 후보도 없음
    updates, texts, errors, reread = core._download_new(None, store.data["cache"], store, folder, pdf_reader())
    assert errors == [] and reread == {"77:1"} and "77:1" in texts  # 받지 않고(네트워크 없음) 디스크에서 읽음
    materials.apply(store.data, updates, texts, errors)
    m = store.data["downloads"]["77:1"]
    assert (m["pdf_ok"], m["pdf_version"], m["pdf_reader"]) == (True, materials.PDF_VERSION, materials.PDF_READER)
    assert m["downloaded_at"] == "2026-11-11T09:00:00+09:00" and m["path"] == str(p)   # 받은 기록은 그대로
    (c,) = _cands(store)
    assert (c["key"], c["score"], c["max_score"], c["n"]) == ("pdf:77:1", 9, 10, 6)


def test_bug_pdf_read_by_an_older_reader_is_reread(tmp_path):
    """옛 PDF 읽기(pdf_reader 판이 낮거나 없음)로 읽은 기록 → 다시 읽어 새 글로 바꾼다. 같은 판이면 다시 읽지 않는다."""
    folder = tmp_path / "dl"
    p = _put(folder, "Quiz3_score.pdf", score_pdf())
    old_text = {"77:1": {"text": "깨진 글자", "course_id": "77", "filename": "Quiz3_score.pdf",
                         "post_key": "board:announcements-5", "board_id": "announcements", "post": 5,
                         "posted_at": "2026-11-10T10:00:00+09:00"}}
    store = _store(tmp_path, {"77:1": _entry(p)}, old_text)
    assert _cands(store) == []
    updates, texts, errors, reread = core._download_new(None, store.data["cache"], store, folder, pdf_reader())
    assert reread == {"77:1"} and "Quiz 3 Score" in texts["77:1"]["text"]
    materials.apply(store.data, updates, texts, errors)
    assert [c["score"] for c in _cands(store)] == [9]
    # 이제 같은 판 — 할 일이 없다
    assert core._download_new(None, store.data["cache"], store, folder, pdf_reader()) == ({}, {}, [], set())
    # PDF 읽기 판이 오르면 다시
    store.data["downloads"]["77:1"]["pdf_reader"] = materials.PDF_READER - 1
    assert core._download_new(None, store.data["cache"], store, folder, pdf_reader())[3] == {"77:1"}


def test_pdftext_has_a_reader_version():
    assert isinstance(pdftext.VERSION, int) and pdftext.VERSION >= 1 and materials.PDF_READER == pdftext.VERSION


def test_pdf_reread_needs_a_pdf_reader_and_respects_folder(tmp_path):
    """PDF 읽는 함수가 없으면(옛 호출) PDF는 다시 읽기 일에 넣지 않는다. 폴더 밖·없는 파일도 아님."""
    folder = tmp_path / "dl"
    p = _put(folder, "Quiz3_score.pdf", score_pdf())
    out = _put(tmp_path / "elsewhere", "Quiz4_score.pdf", score_pdf("Quiz 4"))
    downloads = {"77:1": _entry(p), "77:2": _entry(out, "77:2", "Quiz4_score.pdf"),
                 "77:3": {**_entry(p, "77:3", "gone.pdf"), "path": str(folder / "없음.pdf")}}
    assert materials.reread_jobs(downloads, folder) == []
    assert [j["key"] for j in materials.reread_jobs(downloads, folder, texts={}, pdf=True)] == ["77:1"]


def test_scanned_pdf_reread_once_per_reader_version(tmp_path):
    folder = tmp_path / "dl"
    p = _put(folder, "scan.pdf", b"%PDF-1.4\n%%EOF\n")
    store = _store(tmp_path, {"77:1": _entry(p, filename="scan.pdf", pdf_ok=False, read_ok=False)})
    updates, texts, errors, reread = core._download_new(None, store.data["cache"], store, folder, pdf_reader())
    assert reread == {"77:1"} and texts == {}
    materials.apply(store.data, updates, texts, errors)
    assert store.data["downloads"]["77:1"]["pdf_ok"] is False and store.data["downloads"]["77:1"]["pdf_notes"]
    assert core._download_new(None, store.data["cache"], store, folder, pdf_reader())[3] == set()


# ================================================================ 실제 동기화 경로(가짜 Canvas)

@pytest.fixture()
def env(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    store.data["settings"]["student_id"] = DEMO_STUDENT_ID
    yield mock, base, store
    mock.shutdown()


def _score_keys(store) -> set:
    return {c["key"] for c in _cands(store)}


def test_sync_rereads_files_that_an_older_version_downloaded(env, tmp_path):
    """v0.8.3이 받아 둔 상태(PDF 판 없음, 글 기록 일부 빠짐) → 다음 동기화는 받지 않고 디스크에서 다시 읽어 성적 후보가 돌아온다."""
    mock, base, store = env
    dl = tmp_path / "dl"
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    want = _score_keys(store)
    assert want                                                     # 데모 점수표(Lab0·Lab1 PDF, Quiz 2 엑셀)
    pdfs = [k for k, m in store.data["downloads"].items() if m.get("read_kind") == "pdf"]
    assert pdfs and all(store.data["downloads"][k]["pdf_reader"] == pdftext.VERSION for k in pdfs)
    for k in pdfs:                                                  # 옛 버전 모양으로 되돌린다
        for f in ("pdf_version", "pdf_reader", "pdf_read_at"):
            store.data["downloads"][k].pop(f, None)
        store.data["pdf_texts"].pop(k, None)
    store.save()
    assert not (_score_keys(store) >= want)
    before = len(mock.requests)
    r = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    assert r["downloaded"] == 0
    assert not [p for p in mock.requests[before:] if "/files/" in p and "download" in p]   # 다시 받지 않았다
    assert _score_keys(store) >= want


def test_app_version_change_rereads_every_readable_file_then_clears(env, tmp_path, monkeypatch):
    mock, base, store = env
    dl = tmp_path / "dl"
    core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    readable = {k for k, m in store.data["downloads"].items() if materials.readable(m)}
    assert readable
    # 업데이트 뒤 처음 읽기: 옛 버전이 저장한 파일 → migrate가 reprocess를 켠다
    store.data["app_version"] = "0.8.3"
    store.save()
    monkeypatch.setattr(migrate, "__version__", "0.8.4")
    data = dict(store.data)
    migrate.migrate(data, current="0.8.4")
    rp = data["reprocess"]
    assert rp["pending"] is True and rp["done"] == 0 and rp["at"] and rp.get("from") == "0.8.3"
    again = dict(data)
    migrate.migrate(again, current="0.8.4")                         # 여러 번 돌아도 같은 결과
    assert again["reprocess"] == rp
    store.data.update(reprocess=rp, app_version="0.8.4")
    store.save()
    r = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    assert r["downloaded"] == 0 and r.get("reprocessed") == len(readable)
    assert "reprocess" not in store.data                            # 끝나면 지움
    last = store.data["reprocess_last"]
    assert (last["total"], last["done"]) == (len(readable), len(readable)) and last["finished_at"]
    assert all(store.data["downloads"][k].get("reprocessed") == rp["at"] for k in readable)
    # 다음 동기화는 다시 읽지 않는다
    r2 = core.sync(Canvas(base, DEMO_TOKEN), store, downloads_to=dl, pdf_reader=pdf_reader())
    assert not r2.get("reprocessed")


def test_new_store_and_store_without_downloads_get_no_reprocess(tmp_path):
    d = {"app_version": "0.8.3", "items": [], "settings": {}}
    migrate.migrate(d, current="0.8.4")
    assert "reprocess" not in d
    d = {"app_version": "0.8.4", "downloads": {"1:1": {"filename": "a.pdf"}}}
    migrate.migrate(d, current="0.8.4")                             # 같은 버전 — 켜지 않는다
    assert "reprocess" not in d
    d = {"app_version": "0.8.3", "downloads": {"1:1": {"filename": "a.pdf"}}}
    migrate.migrate(d, current="0.8.4")
    assert d["reprocess"]["pending"] is True


def test_reprocess_batches_200_per_sync(tmp_path, monkeypatch):
    folder = tmp_path / "dl"
    monkeypatch.setattr(materials, "REPROCESS_MAX", 3)
    data = score_pdf()
    downloads = {}
    for i in range(7):
        p = _put(folder, f"Quiz{i}_score.pdf", data)
        downloads[f"77:{i}"] = _entry(p, f"77:{i}", p.name, posted=f"2026-10-{1 + i:02d}T10:00:00+09:00",
                                      pdf_version=materials.PDF_VERSION, pdf_reader=materials.PDF_READER)
    texts = {k: {"text": "x", "course_id": "77", "filename": m["filename"]} for k, m in downloads.items()}
    store = _store(tmp_path, downloads, texts)
    store.data["reprocess"] = {"pending": True, "total": None, "done": 0, "at": "2026-11-19T09:00:00+09:00"}
    seen = []
    for _ in range(3):
        updates, texts_, errors, reread = core._download_new(None, store.data["cache"], store, folder, pdf_reader())
        materials.apply(store.data, updates, texts_, errors)
        core.reprocess_progress(store, folder, pdf=True, reread=reread)
        seen.append(len(reread))
    assert seen == [3, 3, 1] and "reprocess" not in store.data
    assert store.data["reprocess_last"]["done"] == 7 and store.data["reprocess_last"]["total"] == 7


# ================================================================ 화면 경로·능력·상태

class _NoSched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


def _app(tmp_path, downloads, texts=None) -> App:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    store = _store(home, downloads, texts)
    store.data["settings"]["download_dir"] = str(tmp_path / "dl")
    store.save()
    return App(store, home, cfg={"canvas_url": "https://lms.example.test", "token": "test-token"},
               scheduler=_NoSched())


def _post(app, path, body):
    for route, groups in __import__("lms_agent.api", fromlist=["router"]).router.match("POST", path):
        res = route.handler(app, dict(body), **groups)
        if res is not None:
            return res
    raise AssertionError("경로 없음")


def test_reread_route_one_file_and_all_in_background(tmp_path):
    folder = tmp_path / "dl"
    p1 = _put(folder, "Quiz3_score.pdf", score_pdf())
    p2 = _put(folder, "Quiz4_score.pdf", score_pdf("Quiz 4", mine=7))
    downloads = {"77:1": _entry(p1, pdf_version=materials.PDF_VERSION, pdf_reader=materials.PDF_READER),
                 "77:2": _entry(p2, "77:2", "Quiz4_score.pdf", pdf_version=materials.PDF_VERSION,
                                pdf_reader=materials.PDF_READER)}
    app = _app(tmp_path, downloads)
    st = app.state()["materials"]["reprocess"]
    assert st["pending"] is False and st["running"] is False
    out = _post(app, "/api/materials/reread", {"key": "77:1"})
    assert out["materials"]["reprocess"]["running"] in (True, False)
    assert feature.wait_reread(app, 30)
    assert set(app.store.data["pdf_texts"]) == {"77:1"}
    assert [c["score"] for c in _cands(app.store)] == [9]
    _post(app, "/api/materials/reread", {})
    assert feature.wait_reread(app, 30)
    assert set(app.store.data["pdf_texts"]) == {"77:1", "77:2"}
    st = app.state()["materials"]["reprocess"]
    assert (st["pending"], st["running"], st["total"], st["done"]) == (False, False, 2, 2) and st["finished_at"]
    assert sorted(c["score"] for c in _cands(app.store)) == [7, 9]


def test_reread_route_errors(tmp_path):
    folder = tmp_path / "dl"
    p = _put(folder, "lecture.mp4", b"\x00\x00\x00\x18ftypmp42")
    app = _app(tmp_path, {"77:9": _entry(p, "77:9", "lecture.mp4", pdf_ok=None, read_ok=False, read_kind="video")})
    with pytest.raises(Exception) as e:
        _post(app, "/api/materials/reread", {"key": "없는 키"})
    assert getattr(e.value, "status", None) == 404
    with pytest.raises(Exception) as e:
        _post(app, "/api/materials/reread", {"key": "77:9"})
    assert getattr(e.value, "status", None) == 400
    with pytest.raises(Exception) as e:
        _post(app, "/api/materials/reread", {"key": 5})
    assert getattr(e.value, "status", None) == 400


def test_reread_all_also_rereads_imported_score_files_and_keeps_import_keys(tmp_path):
    """성적 화면에서 가져온 파일(pdf_texts "import:" 기록, 계약 29-1)도 [모두 다시 읽기]·업데이트 뒤 다시 읽기에서 다시 읽는다.
    받은 파일 다시 읽기가 import: 기록을 지우지 않는다."""
    import base64
    folder = tmp_path / "dl"
    p = _put(folder, "Quiz3_score.pdf", score_pdf())
    app = _app(tmp_path, {"77:1": _entry(p, pdf_version=materials.PDF_VERSION, pdf_reader=materials.PDF_READER)})
    _post(app, "/api/grades/77/import", {"filename": "Quiz5_score.pdf",
                                         "data": base64.b64encode(score_pdf("Quiz 5", mine=8)).decode()})
    keys = [k for k in app.store.data["pdf_texts"] if k.startswith("import:")]
    assert keys
    _post(app, "/api/materials/reread", {})                                 # 받은 파일 다시 읽기 — import: 기록은 그대로
    assert feature.wait_reread(app, 30)
    assert all(k in app.store.data["pdf_texts"] for k in keys) and "77:1" in app.store.data["pdf_texts"]
    assert app.store.data["reprocess_last"]["imports"] >= 1
    for k in keys:                                                          # 옛 판으로 읽힌 것처럼 글 기록이 빠졌다
        app.store.data["pdf_texts"].pop(k)
    app.store.save()
    _post(app, "/api/materials/reread", {"key": keys[0]})                    # 가져온 파일 하나만
    assert feature.wait_reread(app, 30) and keys[0] in app.store.data["pdf_texts"]
    assert sorted(c["score"] for c in _cands(app.store) if c.get("score") is not None) == [8, 9]


def test_reread_capability_registered_direct_not_undoable(tmp_path):
    cap = capabilities.REGISTRY["materials.reread"]
    assert (cap.tier, cap.undoable, cap.area, cap.route) == ("direct", False, "materials", ("POST", "/api/materials/reread"))
    assert "/api/materials/reread" not in capabilities.uncovered_routes()
    folder = tmp_path / "dl"
    p = _put(folder, "Quiz3_score.pdf", score_pdf())
    app = _app(tmp_path, {"77:1": _entry(p)})
    plan = capabilities.plan(app, "materials.reread", {"file": "Quiz3_score.pdf"})
    assert plan["ok"] and plan["params"]["file"] == "77:1"
    res = capabilities.run(app, plan)
    assert res["ok"] and res["undo"] is None
    assert feature.wait_reread(app, 30) and "77:1" in app.store.data["pdf_texts"]
