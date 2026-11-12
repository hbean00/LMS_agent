"""첨부 자료: 받은 파일 목록, 파일·폴더 열기, 자동 저장 설정. 계약 10장 "첨부 자료(materials) — B".

실제로 받는 일은 동기화가 materials.run(store.data, download_folder(app), …)으로 한다(받는 함수는 동기화가 넘김).
열기는 다운로드 폴더 안의 파일만, 실행 파일은 열지 않는다. 테스트는 App(..., opener=가짜)로 실제 프로그램을 띄우지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .. import materials
from ..api import ApiError, router
from ..store import hidden_courses

ANNOUNCEMENT_POST = "board:announcements-"   # core._announcement_files의 가상 게시판(board_id "announcements")
ANNOUNCEMENT_FILES = "공지 첨부 파일"

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


@router.state_section("materials")
def materials_state(app, ctx):
    d = app.store.data
    shorts = ctx.get("materials_shorts", lambda: materials.short_names_of(d.get("cache")))
    items = []
    hidden = hidden_courses(d)
    for key, m in (d.get("downloads") or {}).items():
        path = m.get("path") or ""
        ann = str(m.get("post_key") or "").startswith(ANNOUNCEMENT_POST)
        items.append({
            "key": key, "course_short": m.get("course_short") or shorts.get(str(m.get("course_id"))),
            # 공지 본문에 링크된 파일을 모은 가상 게시판은 화면에서 '공지 첨부 파일'(저장 폴더 이름 '공지'는 그대로: board_folder)
            "board_title": ANNOUNCEMENT_FILES if ann else m.get("board_title"), "board_folder": m.get("board_title"),
            "course_hidden": str(m.get("course_id")) in hidden,
            "post_title": m.get("post_title"), "filename": m.get("filename"),
            "size": m.get("size"), "posted_at": m.get("posted_at"), "downloaded_at": m.get("downloaded_at"),
            "pdf_ok": m.get("pdf_ok"), "post_key": m.get("post_key"), "exists": bool(path) and os.path.isfile(path),
        })
    items.sort(key=lambda x: (x["posted_at"] or x["downloaded_at"] or "", x["downloaded_at"] or ""), reverse=True)
    return {"folder": str(folder_of(app)), "auto_download": bool(_settings(app).get("auto_download", True)),
            "count": len(items), "items": items, "errors": list(d.get("materials_errors") or [])}


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
