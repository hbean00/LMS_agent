"""설정 파일과 데이터 폴더 위치."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "lms-agent"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"


def data_dir() -> Path:
    """사용자별 데이터 폴더. LMS_AGENT_HOME 환경 변수로 바꿀 수 있다."""
    env = os.environ.get("LMS_AGENT_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def load_dotenv(*paths: Path) -> list[Path]:
    """KEY=VALUE 형식의 .env를 읽어 환경 변수에 넣는다(이미 있는 값은 덮지 않음). 값은 출력하지 않는다."""
    loaded = []
    for p in paths:
        try:
            text = Path(p).read_text("utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        loaded.append(Path(p))
    return loaded


def google_client() -> tuple[str | None, str | None]:
    """구글 OAuth 클라이언트(데스크톱 앱). 현재 폴더·데이터 폴더의 .env 또는 환경 변수에서."""
    load_dotenv(Path.cwd() / ".env", data_dir() / ".env")
    return os.environ.get("LMS_AGENT_GOOGLE_CLIENT_ID"), os.environ.get("LMS_AGENT_GOOGLE_CLIENT_SECRET")


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def load_config(home: Path | None = None) -> dict:
    home = home or data_dir()
    cfg: dict = {}
    path = home / CONFIG_FILE
    if path.exists():
        try:
            cfg = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            cfg = {}
    if os.environ.get("LMS_AGENT_CANVAS_URL"):
        cfg["canvas_url"] = os.environ["LMS_AGENT_CANVAS_URL"]
    if os.environ.get("LMS_AGENT_TOKEN"):
        cfg["token"] = os.environ["LMS_AGENT_TOKEN"]
    if cfg.get("canvas_url"):
        cfg["canvas_url"] = normalize_url(cfg["canvas_url"])
    return cfg


def save_config(cfg: dict, home: Path | None = None) -> Path:
    home = home or data_dir()
    home.mkdir(parents=True, exist_ok=True)
    path = home / CONFIG_FILE
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(path, 0o600)  # 토큰이 들어 있으므로 본인만 읽게
    except OSError:
        pass
    return path


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("canvas_url") and cfg.get("token"))
