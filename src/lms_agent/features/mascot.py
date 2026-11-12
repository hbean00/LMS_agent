"""캐릭터: 학기 마라톤을 함께 달리는 페이스메이커 그림.

장면(mood)마다 어떤 그림을 쓸지 이 순서로 정한다.
1. 내 캐릭터 폴더(<데이터 폴더>/characters/, 하위 폴더 한 단계까지)의 그림 — 누구나 자기 PC에서 바꿀 수 있다.
   장면별 파일은 그 폴더의 pack.json에 적는다. 없으면 파일 이름(run.gif, 축하.png …)으로 짐작한다.
2. 고려대 Canvas(주소가 korea.ac.kr로 끝남)나 데모면 앱에 들어 있는 고려대 공식 캐릭터 KU BUDDY 호이·쿠이 GIF
   (web/characters/ku/, 권리는 고려대학교 — 같은 폴더의 NOTICE.md).
3. 그 밖에는 화면(index.html)이 직접 그리는 오리지널 페이스메이커(SVG).
그림은 고치지 않고 그대로 보낸다(화면도 크기만 맞춰 보여 준다 — 원본 변형 금지 조건을 지키려고).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from importlib import resources
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from ..api import ApiError, Response, router
from ..config import data_dir

# 앱의 장면: run=평소(대시보드), start=출발 보상·튜토리얼, milestone=km 이정표, week_clear=한 주 완주, streak=연속 완주,
# inbox_zero=확인함 비우기, grade_a/b/c=성적 반응(c는 D·F도), wall=30km 벽·시험 몰린 주, busy=마감 많은 주,
# surprise=새 마감·마감 바뀜, rest=마감 없는 주, finish=마지막 주·완주
MOODS = ("run", "start", "milestone", "week_clear", "streak", "inbox_zero", "grade_a", "grade_b", "grade_c",
         "wall", "busy", "surprise", "rest", "finish")
TYPES = {".gif": "image/gif", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
PACK_FILE = "pack.json"
MAX_BYTES = 10_000_000
MAX_FILES = 300
KU_HOST = "korea.ac.kr"
KU_CREDIT = "캐릭터: 고려대학교 KU BUDDY 호이·쿠이"
CHOICES = ("auto", "original")   # auto: 위 순서대로, original: 고려대 캐릭터를 쓰지 않음(내 폴더 그림은 그대로)
# 사용자가 넣은 SVG라도 스크립트는 돌지 않게(주소창으로 바로 열어도)
IMG_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
GUESS = [  # 파일 이름(확장자 뺀 것)이 장면 이름과 같지 않을 때 낱말로 짐작
    ("finish", r"finish|goal|결승|완주|골인"),
    ("grade_a", r"grade_?a|축하|만세|최고|짱|기뻐|celebrat|hooray|yay"),
    ("grade_b", r"grade_?b|thumb|good|nice|굿|좋아|응원|화이팅|파이팅"),
    ("grade_c", r"grade_?c|comfort|hug|sad|위로|토닥|괜찮|슬퍼|눈물"),
    ("rest", r"rest|sleep|water|drink|휴식|쉬는|쉬기|낮잠|급수|커피"),
    ("surprise", r"surprise|wow|깜짝|놀람|헉"),
    ("busy", r"busy|바빠|바쁨|정신"),
    ("wall", r"wall|tired|힘들|지침|벽"),
    ("start", r"start|ready|출발|시작|안녕|hello"),
    ("run", r"run|jog|dash|달리|뛰|러닝"),
]


def os_open(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — 캐릭터 폴더만 연다
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@router.on_init
def attach(app, options):
    app.mascot_opener = options.get("opener") or os_open
    app.mascot_dir = Path(options["mascot_dir"]) if options.get("mascot_dir") else None


def folder_of(app) -> Path:
    if getattr(app, "mascot_dir", None):
        return app.mascot_dir
    return (Path(app.home) if app.demo else data_dir()) / "characters"


# ---------------------------------------------------------------- 앱에 들어 있는 고려대 캐릭터

def ku_file(mood: str):
    if mood not in MOODS:
        return None
    f = resources.files("lms_agent").joinpath(f"web/characters/ku/{mood}.gif")
    return f if f.is_file() else None


def ku_moods() -> list[str]:
    return [m for m in MOODS if ku_file(m) is not None]


def is_ku(app) -> bool:
    """고려대 Canvas에 연결했거나(주소가 korea.ac.kr로 끝남) 데모(고려대 Canvas를 본뜬 데이터)면 True."""
    if app.demo:
        return True
    host = (urlparse(str(app.cfg.get("canvas_url") or "")).hostname or "").lower()
    return host == KU_HOST or host.endswith("." + KU_HOST)


def _choice(app) -> str:
    c = (app.store.data.get("mascot") or {}).get("choice")
    return c if c in CHOICES else "auto"


# ---------------------------------------------------------------- 내 캐릭터 폴더

def list_files(folder: Path) -> list[str]:
    """그림 파일의 폴더 기준 경로('a.gif', '호이/run.gif'). 하위 폴더는 한 단계까지, 숨김·링크는 뺀다."""
    out = []

    def scan(d: Path, prefix: str, depth: int):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or len(out) >= MAX_FILES:
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if depth == 0:
                        scan(Path(e.path), e.name + "/", 1)
                    continue
                if e.is_file() and Path(e.name).suffix.lower() in TYPES:
                    out.append(prefix + e.name)
            except OSError:
                continue

    scan(folder, "", 0)
    return out


def read_pack(folder: Path) -> dict:
    try:
        data = json.loads((folder / PACK_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _guess(files: list[str]) -> dict:
    out: dict = {}
    stems = [(f, Path(f).stem.lower()) for f in files]
    for mood in MOODS:                     # 이름이 장면과 같으면(run.gif) 그대로
        hit = next((f for f, s in stems if s == mood), None)
        if hit:
            out[mood] = hit
    for mood, rx in GUESS:
        if mood in out:
            continue
        hit = next((f for f, s in stems if re.search(rx, s, re.I) and f not in out.values()), None)
        if hit:
            out[mood] = hit
    return out


def local_url(rel: str) -> str:
    return "/mascot/local/" + quote(rel)


def ku_url(mood: str) -> str:
    return f"/mascot/ku/{mood}.gif"


@router.state_section("mascot")
def mascot_state(app, ctx):
    folder = folder_of(app)
    files = list_files(folder)
    pack = read_pack(folder)
    mapped = {m: pack.get(m) for m in MOODS if isinstance(pack.get(m), str) and pack.get(m) in files}
    guessed = {} if any(m in pack for m in MOODS) else _guess(files)   # 한 번이라도 직접 고르면 짐작은 끈다
    local = {m: mapped.get(m) or guessed.get(m) for m in MOODS}
    choice = _choice(app)
    ku_on = choice == "auto" and is_ku(app)
    ku_have = set(ku_moods()) if ku_on else set()
    moods, used_ku = {}, False
    for m in MOODS:
        if local[m]:
            moods[m] = local_url(local[m])
        elif m in ku_have:
            moods[m] = ku_url(m)
            used_ku = True
        else:
            moods[m] = None
    name = pack.get("name") if isinstance(pack.get("name"), str) else None
    name = (name or "").strip()[:20] or None
    return {"pack": "local" if any(local.values()) else "ku" if used_ku else None,
            "name": name or ("호이·쿠이" if used_ku else None), "moods": moods,
            "local": local, "guessed": sorted(m for m in guessed if m not in mapped),
            "files": files, "folder": str(folder), "choice": choice, "ku": ku_on and bool(ku_have),
            "ku_school": is_ku(app), "credit": KU_CREDIT if used_ku else None}


def _image(data: bytes, ctype: str) -> Response:
    return Response(200, data, ctype, {"Content-Security-Policy": IMG_CSP})


@router.route("GET", r"/mascot/ku/(?P<mood>[a-z_]+)\.gif", auth=False)
def serve_ku(app, query, mood):
    f = ku_file(mood)
    if f is None:
        raise ApiError(404, "그림을 찾을 수 없습니다")
    return _image(f.read_bytes(), "image/gif")


@router.route("GET", r"/mascot/local/(?P<rel>.+)", auth=False)
def serve_local(app, query, rel):
    folder = folder_of(app)
    raw = unquote(rel)
    # 목록에 그대로 있는 경로만(../, \, 드라이브 문자, 두 단계 이상, 다른 확장자 불가)
    if not raw or "\\" in raw or ":" in raw or ".." in raw.split("/") or raw not in list_files(folder):
        raise ApiError(404, "그림을 찾을 수 없습니다")
    path = folder.joinpath(*raw.split("/"))
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ApiError(404, "그림 파일이 너무 큽니다(10MB 이하)")
        data = path.read_bytes()
    except OSError:
        raise ApiError(404, "그림을 찾을 수 없습니다") from None
    return _image(data, TYPES[path.suffix.lower()])


def _write_pack(folder: Path, pack: dict) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (PACK_FILE + ".tmp")
        tmp.write_text(json.dumps(pack, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, folder / PACK_FILE)
    except OSError:
        raise ApiError(500, "캐릭터 설정(pack.json)을 저장하지 못했습니다") from None


@router.route("POST", r"/api/mascot/map")
def map_mood(app, body):
    """{"mood": "grade_a", "file": "호이/만세.gif" | null} · {"name": "호이"} · {"choice": "auto"|"original"}."""
    folder = folder_of(app)
    pack = read_pack(folder)
    wrote = changed = False
    if "mood" in body or "file" in body:
        mood, file = body.get("mood"), body.get("file")
        if mood not in MOODS:
            raise ApiError(400, "장면 이름이 올바르지 않습니다")
        files = list_files(folder)
        if file is not None and (not isinstance(file, str) or file not in files):
            raise ApiError(400, "캐릭터 폴더에 없는 그림입니다. 폴더에 넣은 뒤 다시 고르세요")
        if not any(m in pack for m in MOODS):   # 처음 직접 고를 때는 지금 짐작한 것을 그대로 옮겨 적는다
            pack.update(_guess(files))
        pack[mood] = file
        wrote = True
    if "name" in body:
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise ApiError(400, "이름은 문자열이어야 합니다")
        pack["name"] = (name or "").strip()[:20] or None
        wrote = True
    if "choice" in body:
        if body["choice"] not in CHOICES:
            raise ApiError(400, "choice는 auto 또는 original이어야 합니다")
        app.store.data.setdefault("mascot", {})["choice"] = body["choice"]
        app.store.save()
        changed = True
    if not (wrote or changed):
        raise ApiError(400, "바꿀 내용(mood·file, name 또는 choice)이 없습니다")
    if wrote:
        _write_pack(folder, pack)
    return app.state()


@router.route("POST", r"/api/mascot/open-folder")
def open_folder(app, body):
    folder = folder_of(app)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        app.mascot_opener(folder)
    except OSError:
        raise ApiError(500, "캐릭터 폴더를 열지 못했습니다") from None
    return {"ok": True}
