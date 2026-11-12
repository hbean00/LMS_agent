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
