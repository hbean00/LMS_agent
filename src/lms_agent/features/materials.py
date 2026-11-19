"""첨부 자료: 받은 파일 목록, 파일·폴더 열기, 자동 저장 설정. 계약 10장 "첨부 자료(materials) — B".

실제로 받는 일은 동기화가 materials.run(store.data, download_folder(app), …)으로 한다(받는 함수는 동기화가 넘김).
열기는 다운로드 폴더 안의 파일만, 실행 파일은 열지 않는다. 테스트는 App(..., opener=가짜)로 실제 프로그램을 띄우지 않는다.

받아 둔 파일 다시 읽기(계약 29-3): POST /api/materials/reread {key?} — key가 있으면 그 파일 하나, 없으면 읽을 수 있는 받아 둔 파일
모두(store.data["reprocess"]를 켜고 REREAD_BATCH개씩). 백그라운드 스레드가 app.lock(동기화와 같은 잠금)을 묶음마다 잡고 디스크에서
읽은 뒤 파일 잠금 안에서 합치고(받기 오류는 그대로), 항목을 다시 만든다(core.refresh_items) → "changed".
상태 state.materials.reprocess = {pending, running, total, done, at, finished_at, by, current, error}. 능력 materials.reread(direct, 되돌릴 수 없음).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from .. import capabilities as _caps
from .. import core, gradebook, grades, materials
from ..api import ApiError, router
from ..store import hidden_courses
from ..timeutil import iso, now_local

REREAD_BATCH = 20   # 화면에서 누른 다시 읽기가 app.lock을 한 번에 잡고 읽는 파일 수(동기화·다른 동작이 오래 기다리지 않게)
IMPORT_PREFIX = "import:"   # 성적 화면에서 가져온 파일의 pdf_texts key(manualscores — 다시 읽기는 manualscores.reread_imports)

ANNOUNCEMENT_POST = "board:announcements-"   # core._linked_files의 가상 게시판(board_id "announcements")
ANNOUNCEMENT_FILES = "공지 첨부 파일"

# 자료 화면의 분류(계약 24-4): 가상 게시판(공지·과제·강의계획서에 링크된 파일)은 post_key로, 그 밖은 게시판 이름의 낱말로.
# 과목마다 게시판 이름이 달라도(예: '과제 제출', 'Assignments', '실습 자료실') 같은 분류가 되게 특정 과목 이름에 기대지 않는다.
CATEGORIES = [("assignment", "과제"), ("announcement", "공지"), ("lecture", "강의자료"), ("syllabus", "강의계획서"),
              ("board", "기타 게시판")]
_VIRTUAL = {"board:announcements-": "announcement", "board:assignments-": "assignment", "board:syllabus-": "syllabus"}
_CAT_WORDS = [
    ("syllabus", re.compile(r"강의\s*계획서|syllabus", re.I)),
    ("announcement", re.compile(r"공지|알림|notice|announce", re.I)),
    ("assignment", re.compile(r"과제|숙제|레포트|리포트|보고서|실습|프로젝트|assignment|homework|\bhw\b|\blabs?\b|project|report", re.I)),
    ("lecture", re.compile(r"강의\s*자료|수업\s*자료|자료실|강의\s*노트|강의록|교안|lecture|\bslides?\b|\bmaterials?\b|\bnotes?\b", re.I)),
]


def category_of(post_key, board_title) -> str:
    pk = str(post_key or "")
    for prefix, cat in _VIRTUAL.items():
        if pk.startswith(prefix):
            return cat
    title = str(board_title or "")
    for cat, rx in _CAT_WORDS:
        if rx.search(title):
            return cat
    return "board"

# 앱에서 바로 열면 프로그램이 실행되는 형식. 게시판 첨부는 누구나 올릴 수 있으니 폴더에서 직접 확인하게 한다.
RUNNABLE = {".exe", ".com", ".bat", ".cmd", ".scr", ".pif", ".msi", ".msp", ".msc", ".cpl", ".lnk", ".url",
            ".reg", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".jar", ".appref-ms",
            ".application", ".gadget", ".inf", ".scf", ".sh", ".command", ".app", ".desktop"}


def os_open(path: Path) -> None:
    """OS 기본 프로그램으로 연다(셸을 거치지 않음)."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — 경로는 다운로드 폴더 안으로 확인된 것만 온다
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@router.on_init
def attach(app, options):
    app.materials_opener = options.get("opener") or os_open
    app.reread_guard = threading.Lock()
    app.reread_thread = None
    app.reread_current = None     # 다시 읽는 중인 파일 key, 모두면 "all"
    app.reread_error = None


def _settings(app) -> dict:
    return app.store.data["settings"]


def folder_of(app) -> Path:
    """첨부를 저장하는 폴더(자동 저장을 꺼도 이미 받은 파일을 열 때 쓴다). 데모는 <home>/downloads."""
    if app.demo:
        return Path(app.home) / "downloads"
    d = _settings(app).get("download_dir")
    return Path(d) if d else materials.default_folder()


def download_folder(app) -> Path | None:
    """동기화가 첨부를 받을 폴더. 자동 저장을 껐으면 None."""
    if not _settings(app).get("auto_download", True):
        return None
    return folder_of(app)


def _notes(v, limit: int) -> list[str]:
    """받은 파일 기록의 메모(목록 또는 글 하나) → 글 목록(한도·칸마다 200자)."""
    items = v if isinstance(v, (list, tuple)) else [v] if v else []
    return [str(n)[:200] for n in items[:limit] if n]


@router.state_section("materials")
def materials_state(app, ctx):
    d = app.store.data
    shorts = ctx.get("materials_shorts", lambda: materials.short_names_of(d.get("cache")))
    items = []
    hidden = hidden_courses(d)
    for key, m in (d.get("downloads") or {}).items():
        path = m.get("path") or ""
        ann = str(m.get("post_key") or "").startswith(ANNOUNCEMENT_POST)
        item = {
            "key": key, "course_short": m.get("course_short") or shorts.get(str(m.get("course_id"))),
            # 공지 본문에 링크된 파일을 모은 가상 게시판은 화면에서 '공지 첨부 파일'(저장 폴더 이름 '공지'는 그대로: board_folder)
            "board_title": ANNOUNCEMENT_FILES if ann else m.get("board_title"), "board_folder": m.get("board_title"),
            "course_hidden": str(m.get("course_id")) in hidden,
            "post_title": m.get("post_title"), "filename": m.get("filename"),
            "size": m.get("size"), "posted_at": m.get("posted_at"), "downloaded_at": m.get("downloaded_at"),
            "pdf_ok": m.get("pdf_ok"), "post_key": m.get("post_key"), "exists": bool(path) and os.path.isfile(path),
            "category": category_of(m.get("post_key"), m.get("board_title")),
            # 표 파일(엑셀·CSV, 계약 25-2): 내용을 읽었는지와 못 읽은 이유(화면 꼬리표)
            "sheet_ok": m.get("sheet_ok"), "sheet_notes": list(m.get("sheet_notes") or [])[:3],
        }
        # 문서·zip(docx·pptx·hwp·hwpx·zip, 계약 27-2): 내용을 읽었는지와 못 읽은 이유(화면 꼬리표). PDF·표는 pdf_ok·sheet_ok가
        # 이미 말하므로 그 밖의 파일에만 더한다(칸이 없으면 '모름')
        if m.get("read_ok") is not None and m.get("pdf_ok") is None and m.get("sheet_ok") is None:
            item.update(read_ok=bool(m.get("read_ok")), read_notes=_notes(m.get("read_notes"), 3))
        items.append(item)
    items.sort(key=lambda x: (x["posted_at"] or x["downloaded_at"] or "", x["downloaded_at"] or ""), reverse=True)
    return {"folder": str(folder_of(app)), "auto_download": bool(_settings(app).get("auto_download", True)),
            "count": len(items), "items": items, "errors": list(d.get("materials_errors") or []),
            "categories": [{"id": c, "label": label} for c, label in CATEGORIES],
            "reprocess": reprocess_state(app)}


def _running(app) -> bool:
    t = getattr(app, "reread_thread", None)
    return bool(t is not None and t.is_alive())


def reprocess_state(app) -> dict:
    """state.materials.reprocess(계약 29-3): pending = 모두 다시 읽기가 남음(업데이트 뒤 또는 [모두 다시 읽기]), running = 지금
    백그라운드에서 읽는 중, total(모르면 아직 안 읽은 읽을 수 있는 파일 수로 어림)·done·at(시작 시각)·finished_at(끝난 시각, 남은 동안 None)·
    by("update" | "user")·current(읽는 중인 key 또는 "all")·error(마지막 실패 한 줄)."""
    d = app.store.data
    rp = d.get("reprocess") if isinstance(d.get("reprocess"), dict) else None
    running = _running(app)
    base = {"running": running, "current": getattr(app, "reread_current", None) if running else None,
            "error": getattr(app, "reread_error", None)}
    if rp and rp.get("pending"):
        total = rp.get("total")
        if not isinstance(total, int):
            mark = rp.get("at")
            hidden = hidden_courses(d)
            total = int(rp.get("done") or 0) + sum(
                1 for m in (d.get("downloads") or {}).values()
                if materials.readable(m) and m.get("reprocessed") != mark and str(m.get("course_id")) not in hidden)
        return {"pending": True, "total": total, "done": int(rp.get("done") or 0), "at": rp.get("at"),
                "finished_at": None, "by": rp.get("by") or "update", **base}
    last = d.get("reprocess_last") if isinstance(d.get("reprocess_last"), dict) else {}
    return {"pending": False, "total": last.get("total"), "done": int(last.get("done") or 0), "at": last.get("at"),
            "finished_at": last.get("finished_at"), "by": last.get("by"), **base}


@router.route("POST", r"/api/materials/reread")
def reread(app, body):
    """받아 둔 파일 다시 읽기: {"key"?} — key(받은 파일 key)면 그 파일, 없으면 읽을 수 있는 받아 둔 파일 모두. 다음 동기화를 기다리지 않고
    백그라운드에서(진행은 state.materials.reprocess). 응답은 새 state. 이미 도는 중이면 409."""
    key = body.get("key")
    if key is not None and not isinstance(key, str):
        raise ApiError(400, "key는 받은 파일의 키(글자)여야 해요")
    key = (key or "").strip() or None
    folder = folder_of(app)
    if key is not None and key.startswith(IMPORT_PREFIX):   # 성적 화면에서 가져온 파일(manualscores, 계약 29-1)
        if key not in (app.store.data.get("pdf_texts") or {}) and not any(
                isinstance(r, dict) and r.get("key") == key
                for rs in (app.store.data.get("score_imports") or {}).values() for r in (rs if isinstance(rs, list) else [])):
            raise ApiError(404, "가져온 성적 파일 목록에 없어요")
    elif key is not None:
        m = (app.store.data.get("downloads") or {}).get(key)
        if not isinstance(m, dict):
            raise ApiError(404, "받은 파일 목록에 없어요. 동기화한 뒤 다시 해 주세요")
        name = m.get("filename") or "파일"
        if not materials.readable(m):
            raise ApiError(400, f"'{name}'은(는) 글자를 읽는 형식(PDF·엑셀·CSV·워드·파워포인트·한글·zip)이 아니에요")
        if materials.on_disk(m, folder) is None:
            raise ApiError(404, f"'{name}' 파일이 다운로드 폴더에 없어요(지웠으면 다음 동기화 때 다시 받아요)")
    with app.reread_guard:
        if _running(app):
            raise ApiError(409, "받아 둔 파일을 이미 다시 읽는 중이에요. 끝나면 다시 눌러 주세요")
        if key is None:
            with app.store.locked():
                app.store.reload_if_changed()
                rp = app.store.data.get("reprocess")
                if not (isinstance(rp, dict) and rp.get("pending")):   # 업데이트 뒤 남은 것이 있으면 이어서
                    app.store.data["reprocess"] = {"pending": True, "total": None, "done": 0, "at": iso(now_local()),
                                                   "from": None, "to": None, "by": "user"}
                app.store.write()
        app.reread_error = None
        app.reread_current = key or "all"
        t = threading.Thread(target=_reread_job, args=(app, key, folder, app.session_gen), daemon=True,
                             name="materials-reread")
        app.reread_thread = t
        t.start()
    return app.state()


def _reread_job(app, key: str | None, folder: Path, gen: int) -> None:
    try:
        changed = _reread_loop(app, key, folder, gen)
    except Exception as e:  # noqa: BLE001 — 화면이 '읽는 중'에 멈춰 있지 않게(내용·경로 없이 종류만)
        app.reread_error = f"받아 둔 파일을 다시 읽지 못했어요({type(e).__name__})"
        changed = False
    if changed:
        app.emit("changed")


def _reread_loop(app, key: str | None, folder: Path, gen: int) -> bool:
    reader = core.default_pdf_reader()
    changed = False
    while True:
        with app.lock:   # 동기화(do_sync)와 같은 잠금 — 동기화가 받는 중이면 끝날 때까지 기다린다
            if app.session_gen != gen:
                return changed
            app.store.reload_if_changed()
            d = app.store.data
            hidden = hidden_courses(d)
            mark = core._reprocess_mark(d)
            if key is not None and key.startswith(IMPORT_PREFIX):
                jobs = []   # 가져온 성적 파일(manualscores) — 아래 잠금 안에서 그 파일만 다시 읽는다
            elif key is not None:
                job = materials.reread_job(key, (d.get("downloads") or {}).get(key) or {}, folder)
                jobs = [job] if job is not None else []
            elif mark is None:
                return changed
            else:
                jobs = materials.reread_jobs(d.get("downloads") or {}, folder, texts=d.get("pdf_texts") or {},
                                             limit=REREAD_BATCH, pdf=reader is not None, everything=mark, hidden=hidden)
            updates, texts, _ = materials.fetch(jobs, _no_download, pdf_reader=reader) if jobs else ({}, {}, [])
            with app.store.locked():
                app.store.reload_if_changed()
                if app.session_gen != gen:   # 그 사이 로그아웃 — 지운 기록을 되살리지 않는다
                    return changed
                materials.apply(app.store.data, updates, texts, None)
                imported = 0
                if key is not None and key.startswith(IMPORT_PREFIX):
                    imported = core.reread_imports(app.store, reader, keys={key})
                rp = None
                if key is None:
                    before = int((app.store.data.get("reprocess") or {}).get("imports") or 0)
                    rp = core.reprocess_progress(app.store, folder, pdf=reader is not None, reread=set(updates),
                                                 pdf_reader=reader)   # 처음 묶음에서 가져온 성적 파일도(계약 29-1·29-3)
                    imported = int((rp or {}).get("imports") or 0) - before
                if updates or imported > 0:
                    _refresh_items(app.store)
                    changed = True
                app.store.write()
        if (key is not None or not jobs or not updates or rp is None or not rp.get("pending")
                or "reprocess" not in app.store.data):
            return changed


def _no_download(att):
    raise OSError("다시 읽기는 받지 않는다")


def _refresh_items(store) -> None:
    """다시 읽은 글로 항목(마감 찾기)을 다시 만든다 — 실행 기록·report는 그대로(core.refresh_items)."""
    cache = store.data.get("cache")
    if isinstance(cache, dict):
        cache["pdf_texts"] = dict(store.data.get("pdf_texts") or {})
        core.refresh_items(store)


def wait_reread(app, timeout: float = 30.0) -> bool:
    """테스트용: 다시 읽기 스레드가 끝날 때까지 기다린다. 끝났으면 True."""
    t = getattr(app, "reread_thread", None)
    if t is None:
        return True
    t.join(timeout)
    return not t.is_alive()


# ---------------------------------------------------------------- 능력(계약 28장) — 받아 둔 파일 다시 읽기

def _b_reread(app, p):
    return "/api/materials/reread", ({"key": p["file"]} if p.get("file") else {})


def _m_reread(app, p, res):
    if p.get("file"):
        m = (app.store.data.get("downloads") or {}).get(p["file"]) or {}
        return f"‘{m.get('filename') or '파일'}’을(를) 다시 읽기 시작했어요 — 끝나면 성적·마감에 반영돼요"
    return "받아 둔 파일을 모두 다시 읽기 시작했어요(백그라운드) — 끝나면 성적·마감에 반영돼요"


_caps.register(_caps.Capability(
    "materials.reread", "materials", "받아 둔 파일 다시 읽기(성적·마감 다시 찾기)", ("POST", "/api/materials/reread"),
    {"file": _caps.P("str", "받은 파일 이름(선택 — 없으면 받아 둔 파일 모두)", required=False, resolve="download")},
    tier="direct", build=_b_reread, undoable=False))
getattr(_caps, "_MESSAGES", {})["materials.reread"] = _m_reread

# 공지·게시판 본문(HTML 표)의 점수표도 첨부 점수표처럼 성적 후보로(계약 29-3 ⑥ — 리드의 후보 공급자 연결)
gradebook.register_candidates(grades.body_table_candidates)


@router.route("POST", r"/api/materials/open")
def open_material(app, body):
    folder = folder_of(app)
    key = body.get("key")
    if key in (None, ""):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            app.materials_opener(folder)
        except OSError:
            raise ApiError(500, "다운로드 폴더를 열지 못했습니다") from None
        return {"ok": True}
    m = (app.store.data.get("downloads") or {}).get(str(key))
    if not m or not m.get("path"):
        raise ApiError(404, "받은 파일 목록에 없습니다. 동기화 후 다시 시도하세요")
    path = Path(m["path"])
    name = m.get("filename") or path.name
    if not materials.inside(path, folder):
        raise ApiError(403, f"'{name}'은(는) 다운로드 폴더 밖에 있어 열지 않습니다")
    if not path.is_file():
        raise ApiError(404, f"'{name}' 파일이 없습니다(지워졌으면 다음 동기화 때 다시 받습니다)")
    if path.suffix.lower() in RUNNABLE:
        raise ApiError(400, f"'{name}'은(는) 실행 파일이라 앱에서 열지 않습니다. 폴더에서 직접 확인하세요")
    try:
        app.materials_opener(path)
    except OSError:
        raise ApiError(500, f"'{name}'을(를) 열지 못했습니다(연결된 프로그램이 없을 수 있습니다)") from None
    return {"ok": True}


def _apply_settings(app, body: dict) -> bool:
    """auto_download·download_dir을 확인해 settings에 넣는다. 바뀐 것이 있으면 True. 잘못된 값은 400."""
    new: dict = {}
    if "auto_download" in body:
        if not isinstance(body["auto_download"], bool):
            raise ApiError(400, "auto_download는 true 또는 false여야 합니다")
        new["auto_download"] = body["auto_download"]
    if "download_dir" in body:
        raw = body["download_dir"]
        if raw is not None and not isinstance(raw, str):
            raise ApiError(400, "저장 폴더는 문자열이어야 합니다")
        raw = (raw or "").strip()
        if not raw:
            new["download_dir"] = None  # 기본(문서\LMS Agent)으로
        elif app.demo:
            raise ApiError(400, "데모 모드에서는 저장 폴더를 바꿀 수 없습니다")
        else:
            if len(raw) > 400 or "\x00" in raw:
                raise ApiError(400, "저장 폴더 경로가 올바르지 않습니다")
            p = Path(raw).expanduser()
            if not p.is_absolute():
                raise ApiError(400, "저장 폴더는 C:\\… 처럼 전체 경로로 적어 주세요")
            if p.exists() and not p.is_dir():
                raise ApiError(400, "그 경로는 폴더가 아니라 파일입니다")
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ApiError(400, "그 위치에 폴더를 만들 수 없습니다(권한·드라이브를 확인하세요)") from None
            new["download_dir"] = str(p.resolve())
    _settings(app).update(new)  # 다 확인한 뒤에 한꺼번에(하나라도 틀리면 아무것도 안 바뀜)
    return bool(new)


@router.route("POST", r"/api/materials/settings")
def materials_settings(app, body):
    if _apply_settings(app, body):
        app.store.save()
    return app.state()


@router.route("POST", r"/api/settings", priority=1)
def settings_passthrough(app, body):
    """계약대로 POST /api/settings에도 auto_download·download_dir을 받는다. 확인·반영만 하고
    None을 돌려 기본 설정 처리(features/core.py)로 넘긴다 — 그쪽이 저장하고 새 state를 준다."""
    _apply_settings(app, body)
    return None
