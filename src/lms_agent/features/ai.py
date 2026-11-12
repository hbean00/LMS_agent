"""AI CLI 에이전트: 설정, 상태, 확인함 정리(triage). 계약 10·12장.

- 사용자가 settings.ai_cli를 켜야만 AI를 부른다(켜면 강의 글 일부가 그 AI 서비스로 간다 — 화면에서 안내).
- 테스트·데모는 App(..., ai_runner=가짜, ai_command="claude")로 실제 CLI를 띄우지 않는다.
"""
from __future__ import annotations

import time

from .. import agent_cli
from ..api import ApiError, router

_DETECT = {"at": 0.0, "path": None}


def _detect(app) -> str | None:
    if getattr(app, "ai_command", None):
        return app.ai_command
    if time.monotonic() - _DETECT["at"] > 30:   # PATH 찾기는 30초에 한 번만
        _DETECT.update(at=time.monotonic(), path=agent_cli.detect())
    return _DETECT["path"]


@router.on_init
def attach(app, options):
    app.ai_runner = options.get("ai_runner")     # 테스트용 가짜 실행기(None이면 실제 CLI)
    app.ai_command = options.get("ai_command")   # 테스트용 명령(None이면 PATH에서 찾음)


@router.state_section("ai")
def ai_state(app, ctx):
    st = app.store.data.get("settings") or {}
    runs = app.store.data.get("ai_runs") or []
    last = runs[-1] if runs else None
    exe = _detect(app)
    return {"available": bool(exe), "command": agent_cli.command_name(exe) or agent_cli.DEFAULT_COMMAND,
            "enabled": bool(st.get("ai_cli")), "auto_triage": bool(st.get("ai_auto_triage")),
            "last_error": (last or {}).get("error") if last and not last.get("ok") else None,
            "calls_today": agent_cli.calls_today(app.store),
            "limits": {"per_sync": agent_cli.MAX_CALLS_PER_SYNC, "per_day": agent_cli.MAX_CALLS_PER_DAY,
                       "max_chars": agent_cli.MAX_TEXT},
            "recent_runs": agent_cli.recent_runs(app.store, 5),
            "last_triage": app.store.data.get("ai_triage")}


@router.route("POST", r"/api/ai/settings")
def ai_settings(app, body):
    """{"ai_cli"?: bool, "ai_auto_triage"?: bool}"""
    for key in ("ai_cli", "ai_auto_triage"):
        if key in body:
            app.store.data["settings"][key] = bool(body[key])
    app.store.save()
    return app.state()


@router.route("POST", r"/api/ai/triage")
def ai_triage(app, body):
    """확인함 후보를 AI로 정리(검증된 결정만 반영, by="ai"). 결과는 state.ai.last_triage와 응답의 "triage"."""
    if not (app.store.data.get("settings") or {}).get("ai_cli"):
        raise ApiError(400, "AI CLI 사용이 꺼져 있습니다. 설정에서 먼저 켜 주세요")
    with app.lock:  # 동기화가 항목을 다시 만드는 중에는 기다린다
        result = agent_cli.auto_triage(app.store, runner=app.ai_runner, command=app.ai_command)
        app.store.save()
    if result.get("applied"):
        app.emit("changed")
    out = app.state()
    out["triage"] = result
    return out
