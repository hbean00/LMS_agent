"""바뀐 점(계약 19-6): 앱에 함께 들어 있는 `data/changelog.json`(버전마다 한국어 요약). 업데이트가 끝나면 화면이
지난 버전 다음부터 지금 버전까지의 바뀐 점을 보여 주고(state.update.changes), 설정 → 업데이트에서 최근 바뀐 점(state.update.changelog)을 본다.
인터넷 없이도 보이고, 문구는 사용자가 읽을 말로 적는다(커밋 메시지가 아님)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).parent / "data" / "changelog.json"
RECENT = 6


def _key(v) -> tuple:
    parts = []
    for p in str(v or "").lstrip("v").split("-")[0].split("."):
        parts.append(int(p) if p.isdigit() else 0)
    return tuple(parts + [0] * (3 - len(parts)))


@lru_cache(maxsize=1)
def entries() -> tuple:
    """버전이 높은 것부터. 파일이 없거나 깨졌으면 빈 목록(화면은 '바뀐 점' 링크만 숨긴다)."""
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict) or not e.get("version"):
            continue
        items = [str(x)[:300] for x in e.get("items") or [] if str(x).strip()][:10]
        out.append({"version": str(e["version"]), "date": e.get("date"), "title": str(e.get("title") or "")[:80],
                    "items": items})
    return tuple(sorted(out, key=lambda e: _key(e["version"]), reverse=True))


def between(old, new) -> list[dict]:
    """old보다 높고 new 이하인 버전들(높은 것부터) — 0.7.3 → 0.8.2로 건너뛴 사용자는 0.8.0·0.8.1·0.8.2를 모두 본다."""
    if not new:
        return []
    lo, hi = _key(old) if old else (0, 0, 0), _key(new)
    return [dict(e) for e in entries() if lo < _key(e["version"]) <= hi]


def recent(current, n: int = RECENT) -> list[dict]:
    """지금 버전 이하의 최근 n개."""
    hi = _key(current)
    return [dict(e) for e in entries() if _key(e["version"]) <= hi][:n]
