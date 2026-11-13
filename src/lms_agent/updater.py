"""업데이트: 새 버전 확인(하루 한 번)과 설치 방식별 한 번 누르기 업데이트. 계약 19장.

- 확인: GitHub 공개 Releases API(`GET /repos/hbean00/LMS_agent/releases/latest`) 하나만 부른다. 보내는 것은 고정된
  User-Agent뿐이다(이름·토큰·학교 정보 없음). 결과는 state.json의 "update" 칸에 두고, 하루가 지나기 전에는 다시 묻지 않는다.
  인터넷이 없거나 GitHub이 막아도(비로그인 60회/시간) 조용히 넘어가고 몇 시간 뒤 다시 묻는다.
- 설치 방식(how): "exe"(PyInstaller 실행 파일) · "pip"(site-packages) · "git"(소스 폴더 위에 .git) · "unknown".
- 적용은 사용자가 누를 때만(또는 settings.auto_update를 켰으면 앱을 켤 때 exe·pip만):
  exe = 같은 릴리스의 SHA256SUMS.txt와 맞는지 확인 → 실행 중인 파일을 .old로 바꾸고 새 파일을 같은 자리에 → 다시 시작
        (같은 경로라 작업 스케줄러의 백그라운드 동기화도 그대로 새 버전을 쓴다). .old는 다음에 켤 때 지운다.
  pip = 릴리스의 wheel을 같은 방식으로 확인한 뒤 `python -m pip install`(셸 없이) → 다시 시작
  git = 작업 폴더가 깨끗하면 `git pull --ff-only` → 다시 시작. 아니면 할 명령만 보여 준다
  unknown = 릴리스 주소와 명령만 보여 준다.
- 주소는 https만, 리다이렉트는 GitHub 다운로드 호스트로만 따라간다. 체크섬이 맞지 않으면 아무것도 실행·교체하지 않는다.
- 업데이트 전에 state.json·config.json을 그 옆에 `*.before-<새 버전>.json`으로 복사해 둔다.

테스트는 fetch(가짜 다운로드)·runner(가짜 subprocess)·how·exe_path를 넘겨 네트워크·실제 설치를 건드리지 않는다.
주의: exe를 바꾼 뒤에는 새 모듈을 import하지 않는다 — PyInstaller 단일 파일은 아직 안 읽은 모듈을 실행 파일
경로에서 그때그때 읽는데, 그 경로에 이제 새 파일이 있다. 그래서 다시 시작에 쓰는 모듈은 모두 여기 맨 위에서 불러 둔다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, net
from .config import CONFIG_FILE, STATE_FILE
from .timeutil import iso, now_local, parse_iso

REPO = "hbean00/LMS_agent"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
GIT_URL = f"https://github.com/{REPO}"
USER_AGENT = "lms-agent-update-check"      # 누구인지 알 수 있는 값은 넣지 않는다
# 릴리스 파일은 github.com/…/releases/download/… 에서 GitHub의 파일 서버로 넘어간다
ALLOWED_HOSTS = frozenset({"api.github.com", "github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
SUMS_NAME = "SHA256SUMS.txt"
EXE_ASSETS = {"win32": "lms-agent-windows.exe", "darwin": "lms-agent-macos", "linux": "lms-agent-linux"}
WHEEL_RE = re.compile(r"^lms_agent-[0-9][0-9A-Za-z.+_-]*-py3-none-any\.whl$")
HOWS = ("exe", "pip", "git", "unknown")
CHECK_EVERY = timedelta(hours=24)
RETRY_AFTER_ERROR = timedelta(hours=3)
MAX_JSON = 1_000_000
MAX_SUMS = 64_000
MAX_ASSET = 300_000_000
NOTES_MAX = 1500


class UpdateError(Exception):
    """화면에 그대로 보여 줄 한국어 문장. kind: offline | rate_limited | no_release | blocked | http | bad | failed | dirty."""

    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- 버전

_VER = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.+]?([0-9A-Za-z.\-+]*))?\s*$")


def parse_version(s) -> tuple | None:
    """'v0.6.0' → (0, 6, 0, 1, ''). 넷째 값은 정식(1)·미리 보기(0, 예: 0.6.0-rc1) — 미리 보기가 정식보다 작다."""
    m = _VER.match(str(s or ""))
    if not m:
        return None
    major, minor, patch, extra = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4) or ""
    return (major, minor, patch, 0 if extra else 1, extra)


def is_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def clean_version(s) -> str | None:
    """'v0.6.0' → '0.6.0'(파일 이름에 써도 되는 글자만)."""
    if not parse_version(s):
        return None
    return re.sub(r"[^0-9A-Za-z.\-]", "", str(s).strip().lstrip("v"))


# ---------------------------------------------------------------- 설치 방식

def package_dir() -> Path:
    return Path(__file__).resolve().parent


def install_type(frozen: bool | None = None, pkg: Path | None = None) -> str:
    """exe(PyInstaller) · pip(site-packages 안) · git(…/src/lms_agent 위에 .git) · unknown."""
    if getattr(sys, "frozen", False) if frozen is None else frozen:
        return "exe"
    pkg = Path(pkg) if pkg is not None else package_dir()
    if any(p.lower() in ("site-packages", "dist-packages") for p in pkg.parts):
        return "pip"
    if git_root(pkg) is not None:
        return "git"
    return "unknown"


def git_root(pkg: Path | None = None) -> Path | None:
    """소스에서 돌 때 저장소 폴더(…/src/lms_agent → …). .git은 폴더(보통) 또는 파일(git worktree)."""
    pkg = Path(pkg) if pkg is not None else package_dir()
    if pkg.parent.name != "src":
        return None
    root = pkg.parent.parent
    return root if (root / ".git").exists() else None


# ---------------------------------------------------------------- 내려받기 (GitHub만)

def check_url(url: str) -> str:
    p = urlparse(str(url or ""))
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise UpdateError("GitHub이 아닌 주소라 내려받지 않았어요", "blocked")
    return url


class _GitHubOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """리다이렉트는 https + GitHub 호스트로만 따라간다."""
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req, timeout: float):
    """실제 네트워크. Canvas·구글과 같은 공통 HTTPS 설정(net.py, 계약 17장 — 운영체제·사용자 루트 인증서)을 쓴다.
    테스트(conftest)는 이 함수를 막아 둔다."""
    return urllib.request.build_opener(net.https_handler(), _GitHubOnlyRedirects).open(req, timeout=timeout)


def http_fetch(url: str, max_bytes: int, timeout: float = 20.0) -> bytes:
    """GET 한 번 → 본문. 실패는 UpdateError(kind로 구분). 토큰·쿠키는 보내지 않는다."""
    check_url(url)
    accept = "application/vnd.github+json" if urlparse(url).hostname == "api.github.com" else "application/octet-stream"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with _open(req, timeout) as resp:
            check_url(resp.geturl())
            data = resp.read(max_bytes + 1)
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
        e.close()
        if e.code == 404:
            raise UpdateError("아직 공개된 버전이 없어요", "no_release") from None
        if e.code == 429 or (e.code == 403 and remaining == "0"):
            raise UpdateError("GitHub이 잠시 확인을 막았어요. 몇 시간 뒤 다시 확인할게요", "rate_limited") from None
        raise UpdateError(f"GitHub 응답 오류({e.code})라 확인하지 못했어요", "http") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        if net.is_cert_error(reason):   # 백신·네트워크가 HTTPS를 가로채는 경우: Canvas와 같은 고치는 방법을 알려 준다
            raise UpdateError(net.cert_help(urlparse(url).hostname or "", reason, "GitHub(업데이트 확인)"), "offline") from None
        if isinstance(reason, ssl.SSLError):
            raise UpdateError("GitHub과 보안 연결(인증서)을 확인하지 못해 멈췄어요", "offline") from None
        raise UpdateError("인터넷에 연결되지 않아 새 버전을 확인하지 못했어요", "offline") from None
    if len(data) > max_bytes:
        raise UpdateError("받은 파일이 너무 커서 멈췄어요", "bad")
    return data


# ---------------------------------------------------------------- 릴리스 정보·체크섬

def parse_release(data) -> dict:
    """releases/latest 응답 → 캐시에 둘 칸. 모양이 이상하면 UpdateError(bad)."""
    if not isinstance(data, dict):
        raise UpdateError("GitHub 응답을 읽지 못했어요", "bad")
    tag = data.get("tag_name")
    latest = clean_version(tag)
    if not latest:
        raise UpdateError("릴리스 버전 이름을 읽지 못했어요", "bad")
    assets = []
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        name, url = a.get("name"), a.get("browser_download_url")
        if isinstance(name, str) and isinstance(url, str) and url.startswith("https://"):
            assets.append({"name": name, "url": url, "size": a.get("size") if isinstance(a.get("size"), int) else None})
    page = data.get("html_url")
    if not (isinstance(page, str) and page.startswith("https://github.com/")):
        page = f"{RELEASES_PAGE}/tag/{tag}"
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    notes = body.strip()
    if len(notes) > NOTES_MAX:
        notes = notes[:NOTES_MAX].rstrip() + "…"
    return {"latest": latest, "tag": str(tag), "notes_url": page, "notes": notes or None,
            "published_at": data.get("published_at") if isinstance(data.get("published_at"), str) else None,
            "assets": assets}


def parse_sums(text: str) -> dict:
    """`sha256sum` 형식('<64자리 16진수>  <파일 이름>', 이진 표시 '*' 허용) → {파일 이름: 소문자 16진수}."""
    out = {}
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*([0-9A-Fa-f]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[Path(m.group(2).replace("\\", "/")).name] = m.group(1).lower()
    return out


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_ok(actual_hex: str, expected_hex: str | None) -> bool:
    if not expected_hex or not re.fullmatch(r"[0-9a-f]{64}", str(expected_hex).lower()):
        return False
    return hmac.compare_digest(str(actual_hex).lower(), str(expected_hex).lower())


# ---------------------------------------------------------------- 백업·교체·다시 시작

def backup_data(home: Path, version: str, overwrite: bool = True) -> list[Path]:
    """state.json·config.json을 옆에 state.before-<version>.json 처럼 복사한다. 만든 파일 목록."""
    v = clean_version(version) or "unknown"
    out = []
    for name in (STATE_FILE, CONFIG_FILE):
        src = Path(home) / name
        if not src.is_file():
            continue
        dst = src.with_name(f"{src.stem}.before-{v}{src.suffix}")
        if dst.exists() and not overwrite:
            continue
        shutil.copy2(src, dst)   # 권한(0600)도 같이 — config.json에는 (암호화된) 토큰이 있다
        out.append(dst)
    return out


def swap_executable(exe: Path, new: Path) -> Path:
    """실행 중인 exe를 <이름>.old로 옮기고 new를 그 자리에 둔다(Windows도 실행 중인 파일의 이름은 바꿀 수 있다).
    두 번째 단계가 실패하면 원래대로 돌려놓고 OSError를 그대로 낸다. 옮긴 옛 파일 경로를 돌려준다."""
    exe, new = Path(exe), Path(new)
    old = exe.with_name(exe.name + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:      # 지난번 옛 파일이 아직 쓰이고 있으면 다른 이름으로
            old = exe.with_name(f"{exe.name}.old-{os.getpid()}")
    if os.name != "nt":
        try:
            os.chmod(new, exe.stat().st_mode | 0o111)
        except OSError:
            pass
    last = None
    for _ in range(5):       # 바이러스 검사 등이 잠깐 잡고 있을 수 있다
        try:
            os.replace(exe, old)
            break
        except OSError as e:
            last = e
            time.sleep(0.3)
    else:
        raise last
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)
        raise
    return old


def leftover_files(exe: Path) -> list[Path]:
    exe = Path(exe)
    try:
        return sorted(p for p in exe.parent.iterdir()
                      if p.name == exe.name + ".new" or p.name == exe.name + ".old" or p.name.startswith(exe.name + ".old-"))
    except OSError:
        return []


def cleanup_executable(exe: Path, tries: int = 1, wait: float = 1.0, files: list | None = None) -> list[Path]:
    """지난 업데이트가 남긴 <이름>.old·.new를 지운다. 옛 프로세스가 아직 끝나는 중이면(Windows) 몇 번 더 해 본다.
    다시 해 볼 때도 처음에 찾은 파일만 지운다(그 사이 새로 받기 시작한 .new를 지우지 않게)."""
    removed = []
    left = list(files) if files is not None else leftover_files(exe)
    for i in range(max(1, tries)):
        still = []
        for p in left:
            try:
                p.unlink()
                removed.append(p)
            except FileNotFoundError:
                pass
            except OSError:
                still.append(p)
        left = still
        if not left:
            break
        if i + 1 < tries:
            time.sleep(wait)
    return removed


def clean_env(env: dict | None = None) -> dict:
    """다시 시작할 프로세스의 환경. PyInstaller가 자식에게 넘기는 값(_MEIPASS2·_PYI_*)을 지워 새 exe가 처음부터 풀게 한다."""
    out = {k: v for k, v in (os.environ if env is None else env).items()
           if not (k.startswith("_MEI") or k.startswith("_PYI_"))}
    out["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return out


def restart_argv(how: str, port: int | None, exe_path: Path | None = None, extra: list | None = None) -> list[str]:
    """새 버전으로 다시 켤 명령. 화면이 이미 열려 있으니 기본은 같은 포트 + 브라우저를 새로 열지 않음."""
    args = list(extra) if extra is not None else (["serve", "--no-browser"] + (["--port", str(port)] if port else []))
    if how == "exe":
        return [str(exe_path or sys.executable)] + args
    return [sys.executable, "-m", "lms_agent"] + args


def _spawn_detached(argv: list[str], flags: int) -> None:
    """Windows: 이 프로세스가 끝나도 살아남게 띄운다. 띄운 쪽이 '닫히면 함께 끝내는' 작업(Job)에 들어 있을 수 있어
    먼저 거기서 떨어져 나가 보고(CREATE_BREAKAWAY_FROM_JOB), 허락되지 않으면 그냥 띄운다."""
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    try:
        subprocess.Popen(argv, env=clean_env(), close_fds=True, creationflags=flags | breakaway)
    except OSError:
        subprocess.Popen(argv, env=clean_env(), close_fds=True, creationflags=flags)


def relaunch(argv: list[str]) -> None:
    """지금 프로세스를 끝내고 argv로 다시 켠다. POSIX는 exec(같은 터미널·같은 PID), Windows는 새 콘솔 창.
    돌아오지 않는다(실패하면 OSError)."""
    if os.name == "nt":
        _spawn_detached(argv, getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        os._exit(0)
    os.execve(argv[0], argv, clean_env())


def windows_finish_script(new: Path, exe: Path, argv: list[str]) -> str:
    """실행 중인 exe의 이름을 바꾸지 못했을 때(Windows): 이 프로세스가 끝나기를 기다렸다가 바꾸고 다시 켜는 PowerShell.
    경로에 한글이 있어도 되게 -EncodedCommand(UTF-16)로 넘긴다. 돌려주는 값은 그 인자."""
    def q(s):
        return "'" + str(s).replace("'", "''") + "'"
    args = ", ".join(q(a) for a in argv[1:]) or "''"
    script = (f"$src={q(new)}; $dst={q(exe)}; "
              "for ($i = 0; $i -lt 90; $i++) { try { Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop; "
              f"Start-Process -FilePath $dst -ArgumentList @({args}); break }} catch {{ Start-Sleep -Seconds 1 }} }}")
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def relaunch_after_exit(new: Path, exe: Path, argv: list[str]) -> None:
    """Windows에서 교체가 막혔을 때: 숨은 PowerShell이 이 프로세스가 끝난 뒤 바꾸고 다시 켠다. 돌아오지 않는다."""
    _spawn_detached(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                     "-EncodedCommand", windows_finish_script(new, exe, argv)], getattr(subprocess, "CREATE_NO_WINDOW", 0))
    os._exit(0)


def _run(args: list[str], timeout: float = 300, cwd: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=flags, timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL)


def _tail(r: subprocess.CompletedProcess, n: int = 300) -> str:
    text = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
    return (text[-1] if text else "")[-n:]


# ---------------------------------------------------------------- 상태와 적용

class Updater:
    """state.json의 "update" 칸(캐시)과 지금 하는 일(phase)을 맡는다. 앱(features/update.py)과 CLI가 함께 쓴다."""

    def __init__(self, store, home: Path, fetch=None, how: str | None = None, exe_path: Path | None = None,
                 root: Path | None = None, runner=None, now=None, platform: str | None = None, python: str | None = None):
        self.store = store
        self.home = Path(home)
        self.fetch = fetch or http_fetch
        self.how = how if how in HOWS else install_type()
        self.exe_path = Path(exe_path or sys.executable)
        self.root = Path(root) if root is not None else git_root()
        self.runner = runner or _run
        self.now = now or now_local
        self.platform = platform or sys.platform
        self.python = python or sys.executable
        self.phase = "idle"          # idle | checking | applying | restarting
        self.result: dict | None = None   # 마지막 적용 결과 {"ok", "message", "restart"}(메모리에만)
        self.auto = False                 # 저절로 확인하는 실행인지(cli serve의 start_background_checks가 켬)
        self._mutex = threading.Lock()

    # ---- 캐시
    def cached(self) -> dict:
        c = self.store.data.get("update")
        return c if isinstance(c, dict) else {}

    def settings(self) -> dict:
        s = self.store.data.get("settings")
        return s if isinstance(s, dict) else {}

    def check_enabled(self) -> bool:
        return bool(self.settings().get("update_check", True))

    def auto_enabled(self) -> bool:
        return bool(self.settings().get("auto_update", False))

    def _save(self, changes: dict) -> None:
        st = self.store
        try:
            with st.locked():
                st.reload_if_changed()
                cur = dict(st.data.get("update") or {}) if isinstance(st.data.get("update"), dict) else {}
                cur.update(changes)
                st.data["update"] = cur
                st.write()
        except (TimeoutError, OSError):   # 저장을 못 해도 이번 실행 동안은 메모리에 둔다
            cur = dict(self.cached())
            cur.update(changes)
            st.data["update"] = cur

    def due(self) -> bool:
        """하루에 한 번(실패했으면 몇 시간 뒤 다시)."""
        if not self.check_enabled():
            return False
        c = self.cached()
        last = parse_iso(c.get("checked_at"))
        if last is None:
            return True
        wait = RETRY_AFTER_ERROR if c.get("error") else CHECK_EVERY
        now = self.now()
        return now - last >= wait or last > now + timedelta(minutes=5)   # 시계가 뒤로 간 경우도

    def _begin(self, phase: str) -> bool:
        with self._mutex:
            if self.phase != "idle":
                return False
            self.phase = phase
            return True

    def _end(self) -> None:
        with self._mutex:
            self.phase = "idle"

    def check(self) -> dict:
        """GitHub에 최신 버전을 묻고 캐시에 둔다. 다른 확인·적용 중이면 묻지 않고 지금 캐시를 돌려준다."""
        if not self._begin("checking"):
            return self.cached()
        try:
            try:
                info = parse_release(json.loads(self.fetch(API_LATEST, MAX_JSON).decode("utf-8")))
                changes = {**info, "error": None}
            except UpdateError as e:
                if e.kind == "no_release":
                    changes = {"latest": None, "tag": None, "assets": [], "notes": None, "notes_url": None, "error": None}
                else:
                    changes = {"error": str(e)}     # 지난번에 알아 둔 최신 버전은 그대로 둔다
            except (ValueError, UnicodeDecodeError):
                changes = {"error": "GitHub 응답을 읽지 못했어요"}
            changes["checked_at"] = iso(self.now())
            self._save(changes)
        finally:
            self._end()
        return self.cached()

    # ---- 계획
    def latest(self) -> str | None:
        return self.cached().get("latest")

    def available(self) -> bool:
        return is_newer(self.latest(), __version__)

    def _asset(self, name_or_re) -> dict | None:
        for a in self.cached().get("assets") or []:
            name = a.get("name") or ""
            if (name == name_or_re) if isinstance(name_or_re, str) else bool(name_or_re.match(name)):
                return a
        return None

    def manual_command(self) -> str | None:
        """직접 할 명령. exe는 명령 대신 릴리스 페이지(notes_url)에서 받는다 → None."""
        if self.how == "exe":
            return None
        if self.how == "git" and self.root is not None:
            return f'git -C "{self.root}" pull --ff-only'
        tag = self.cached().get("tag") or ("v" + self.latest() if self.latest() else "")
        return f"python -m pip install -U git+{GIT_URL}" + (f"@{tag}" if tag else "")

    def _manual_hint(self) -> str:
        cmd = self.manual_command()
        return f"직접 하려면: {cmd}" if cmd else "릴리스 페이지에서 새 파일을 받아 지금 파일과 바꿔 주세요"

    def plan(self) -> dict:
        """한 번 누르기로 될지, 안 되면 왜인지와 직접 할 명령."""
        reason = None
        if self.how == "exe":
            want = EXE_ASSETS.get(self.platform)
            if not want:
                reason = "이 운영체제용 실행 파일은 릴리스에 없어요"
            elif not self._asset(want):
                reason = f"이번 릴리스에 이 운영체제용 실행 파일({want})이 없어요"
            elif not self._asset(SUMS_NAME):
                reason = "이번 릴리스에 확인값 파일(SHA256SUMS.txt)이 없어 자동으로 바꾸지 않아요"
        elif self.how == "pip":
            if not self._asset(WHEEL_RE):
                reason = "이번 릴리스에 설치 파일(wheel)이 없어요"
            elif not self._asset(SUMS_NAME):
                reason = "이번 릴리스에 확인값 파일(SHA256SUMS.txt)이 없어 자동으로 설치하지 않아요"
        elif self.how == "git":
            if self.root is None or not shutil.which("git"):
                reason = "git 명령을 찾지 못했어요"
        else:
            reason = "설치 방식을 알 수 없어 자동으로 바꾸지 않아요"
        return {"can_apply": reason is None, "reason": reason, "command": self.manual_command()}

    def stuck(self) -> bool:
        """지난번에 이 버전으로 바꿨는데 아직 옛 버전이 돈다 → 자동 업데이트를 되풀이하지 않는다."""
        applied = self.cached().get("applied") or {}
        return applied.get("version") == self.latest() and self.available()

    def status(self, demo: bool = False) -> dict:
        c = self.cached()
        available = self.available()
        plan = self.plan() if available else {"can_apply": False, "reason": None, "command": None}
        applied = c.get("applied") if isinstance(c.get("applied"), dict) else {}
        updated = None
        if applied.get("version") == __version__ and not applied.get("seen") and applied.get("from") != __version__:
            updated = {"from": applied.get("from"), "to": applied.get("version"), "at": applied.get("at")}
        data_from = self.store.data.get("app_version")
        reason = "데모에서는 업데이트하지 않아요. 실제 앱에서 해 주세요" if demo and available else plan["reason"]
        # 이 실행에서 적용했다면(result) 그 결과를 보여 준다. 다시 켰는데도 옛 버전이면 그때 알린다
        stuck = available and self.result is None and self.phase == "idle" and self.stuck()
        return {
            "current": __version__,
            "latest": c.get("latest"),
            "available": available,
            "notes_url": c.get("notes_url") or RELEASES_PAGE,
            "notes": c.get("notes"),
            "published_at": c.get("published_at"),
            "how": self.how,
            "can_apply": bool(plan["can_apply"] and not demo),
            "reason": reason,
            "command": plan["command"],
            "checked_at": c.get("checked_at"),
            "error": "지난번 업데이트가 적용되지 않았어요. 다시 해 보거나 직접 받아 주세요" if stuck else c.get("error"),
            "phase": self.phase,
            # 곧 저절로 확인한다(켠 직후 등): 화면은 이 값이 참인 동안 /api/update를 몇 초마다 다시 읽는다
            "pending": bool(self.auto and not demo and self.due()),
            "result": self.result,
            "dismissed": bool(available and c.get("dismissed") == c.get("latest")),
            "updated": updated,
            "data_newer": data_from if is_newer(data_from, __version__) else None,
        }

    def dismiss(self) -> None:
        """배너 닫기: 방금 업데이트 알림이면 다시 안 보이게, 새 버전 알림이면 그보다 새 버전이 나올 때까지."""
        c = self.cached()
        applied = c.get("applied") if isinstance(c.get("applied"), dict) else None
        if applied and applied.get("version") == __version__ and not applied.get("seen"):
            self._save({"applied": {**applied, "seen": True}})
        elif self.available():
            self._save({"dismissed": c.get("latest")})
        if self.result and self.result.get("ok") is False:
            self.result = None

    # ---- 적용
    def _expected(self, asset_name: str) -> str:
        sums_asset = self._asset(SUMS_NAME)
        if not sums_asset:
            raise UpdateError("확인값 파일(SHA256SUMS.txt)이 없어 업데이트하지 않았어요", "bad")
        try:
            sums = parse_sums(self.fetch(check_url(sums_asset["url"]), MAX_SUMS).decode("utf-8", "replace"))
        except UpdateError as e:
            raise UpdateError("확인값 파일을 받지 못했어요: " + str(e), e.kind) from None
        expected = sums.get(asset_name)
        if not expected:
            raise UpdateError(f"확인값 파일에 이 파일({asset_name})의 값이 없어 업데이트하지 않았어요", "bad")
        return expected

    def _download_verified(self, asset: dict) -> tuple[bytes, str]:
        expected = self._expected(asset["name"])
        data = self.fetch(check_url(asset["url"]), MAX_ASSET)
        if not checksum_ok(sha256_hex(data), expected):
            raise UpdateError("내려받은 파일이 확인값(SHA-256)과 달라 업데이트를 멈췄어요. 아무것도 바꾸지 않았어요", "bad")
        return data, expected

    def prepare(self) -> dict:
        """적용 1단계(오래 걸림): 받기·확인·백업, pip·git은 설치까지. exe는 교체 직전에서 멈추고 finish()를 돌려준다.
        결과 {"ok", "message", "restart", "finish"?}. 실패는 {"ok": False, "message"}(예외를 내지 않음)."""
        latest = self.latest()
        if not self.available():
            return {"ok": False, "message": "지금 쓰는 버전이 최신이에요", "restart": False}
        plan = self.plan()
        if not plan["can_apply"]:
            return {"ok": False, "message": f"{plan['reason']}. {self._manual_hint()}", "restart": False}
        try:
            if self.how == "exe":
                return self._prepare_exe(latest)
            if self.how == "pip":
                return self._apply_pip(latest)
            return self._apply_git(latest)
        except UpdateError as e:
            return {"ok": False, "message": str(e), "restart": False}
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return {"ok": False, "message": f"업데이트하지 못했어요({type(e).__name__}). {self._manual_hint()}",
                    "restart": False}

    def _mark_applied(self, latest: str) -> None:
        self._save({"applied": {"version": latest, "from": __version__, "at": iso(self.now()), "how": self.how}})

    def _prepare_exe(self, latest: str) -> dict:
        asset = self._asset(EXE_ASSETS[self.platform])
        data, expected = self._download_verified(asset)
        exe = self.exe_path
        new = exe.with_name(exe.name + ".new")
        try:
            new.write_bytes(data)
        except OSError:
            raise UpdateError(f"이 폴더에 쓸 수 없어 바꾸지 못했어요({exe.parent}). 릴리스 페이지에서 받아 바꿔 주세요", "failed") from None
        if not checksum_ok(sha256_file(new), expected):   # 디스크에 쓴 것도 한 번 더
            new.unlink()
            raise UpdateError("저장한 파일이 확인값과 달라 업데이트를 멈췄어요", "bad")
        backup_data(self.home, latest)
        self._mark_applied(latest)

        def finish() -> None:
            swap_executable(exe, new)

        return {"ok": True, "message": f"v{latest} 준비를 마쳤어요. 다시 켤게요", "restart": True, "finish": finish,
                "new": new}

    def _apply_pip(self, latest: str) -> dict:
        asset = self._asset(WHEEL_RE)
        data, expected = self._download_verified(asset)
        tmp = Path(tempfile.mkdtemp(prefix="lms-agent-update-"))
        try:
            whl = tmp / Path(asset["name"]).name
            whl.write_bytes(data)
            if not checksum_ok(sha256_file(whl), expected):
                raise UpdateError("저장한 파일이 확인값과 달라 업데이트를 멈췄어요", "bad")
            backup_data(self.home, latest)
            r = self.runner([self.python, "-m", "pip", "install", "--upgrade", "--no-deps", "--no-input",
                             "--disable-pip-version-check", str(whl)], timeout=300)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if r.returncode != 0:
            raise UpdateError(f"pip 설치가 실패했어요: {_tail(r)} — {self._manual_hint()}", "failed")
        self._mark_applied(latest)
        return {"ok": True, "message": f"v{latest} 설치를 마쳤어요. 다시 시작하면 새 버전이 열려요", "restart": True}

    def _apply_git(self, latest: str) -> dict:
        git = shutil.which("git") or "git"
        root = str(self.root)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}   # 비밀번호를 묻느라 멈추지 않게
        r = self.runner([git, "-C", root, "status", "--porcelain", "--untracked-files=no"], timeout=30, env=env)
        if r.returncode != 0:
            raise UpdateError(f"git 상태를 확인하지 못했어요: {_tail(r)}", "failed")
        if (r.stdout or "").strip():
            raise UpdateError(f"소스 폴더에 고친 파일이 있어 자동으로 받지 않았어요. 정리한 뒤 {self._manual_hint()}", "dirty")
        backup_data(self.home, latest)
        r = self.runner([git, "-C", root, "pull", "--ff-only"], timeout=180, env=env)
        if r.returncode != 0:
            raise UpdateError(f"git pull이 실패했어요: {_tail(r)}", "failed")
        self._mark_applied(latest)
        return {"ok": True, "message": "새 코드를 받았어요(git pull). 다시 시작하면 새 버전이 열려요", "restart": True}

    def start(self, fn) -> bool:
        """적용을 백그라운드 스레드로. 이미 확인·적용 중이면 False."""
        if not self._begin("applying"):
            return False
        self.result = None
        threading.Thread(target=fn, daemon=True).start()
        return True

    def finish_phase(self, result: dict) -> None:
        self.result = {k: result.get(k) for k in ("ok", "message", "restart")}
        self._end()

    # ---- 켤 때
    def startup_cleanup(self, background: bool = True) -> None:
        """exe면 지난 업데이트가 남긴 파일을 지운다: 받다 만 .new는 바로, 옛 실행 파일 .old는 옛 프로세스가
        끝나기를 기다리며(Windows는 끝나기 전에는 못 지운다) 백그라운드에서."""
        if self.how != "exe":
            return
        files = leftover_files(self.exe_path)
        cleanup_executable(self.exe_path, files=[p for p in files if p.name.endswith(".new")])
        olds = [p for p in files if not p.name.endswith(".new")]
        if olds and background:
            threading.Thread(target=cleanup_executable, args=(self.exe_path, 20, 1.5, olds), daemon=True).start()
        elif olds:
            cleanup_executable(self.exe_path, files=olds)

    def auto_update_at_start(self, out=print, argv_tail: list | None = None) -> bool:
        """settings.auto_update가 켜져 있고(exe·pip만) 새 버전이 있으면 켜기 전에 적용하고 다시 켠다(돌아오지 않음).
        안 했으면 False. 실패해도 앱은 평소처럼 켜진다."""
        if not (self.auto_enabled() and self.check_enabled() and self.how in ("exe", "pip")):
            return False
        if self.due():
            self.check()
        if not self.available() or self.stuck() or not self.plan()["can_apply"]:
            return False
        out(f"새 버전(v{self.latest()})을 설치하고 다시 켜요(설정의 '켤 때 자동 업데이트')…")
        if not self._begin("applying"):
            return False
        try:
            r = self.prepare()
        finally:
            self._end()
        if not r.get("ok"):
            out(f"자동 업데이트를 건너뛰었어요: {r['message']}")
            return False
        argv = restart_argv(self.how, None, self.exe_path, extra=list(argv_tail if argv_tail is not None else sys.argv[1:]))
        if r.get("finish"):
            try:
                r["finish"]()
            except OSError as e:
                if os.name == "nt" and isinstance(e, PermissionError):   # 잠겨 이름을 못 바꾸면: 끝난 뒤 바꾸고 다시 켠다
                    out("실행 파일이 잠겨 있어요. 이 창이 닫히면 바꾸고 다시 켤게요")
                    relaunch_after_exit(r["new"], self.exe_path, argv)
                    return True
                out(f"실행 파일을 바꾸지 못했어요({e}). 이번에는 지금 버전으로 켜요")
                try:
                    Path(r["new"]).unlink()
                except OSError:
                    pass
                return False
        relaunch(argv)
        return True   # 테스트에서 relaunch를 바꿨을 때만 여기 온다
