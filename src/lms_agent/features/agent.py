"""에이전트에게 묻기(채팅) — 계약 20장. 일은 agent.py가 하고, 여기는 경로·상태 칸·백그라운드 작업만.

- POST /api/agent/ask {"message", "course_id"?} → {"job": 대화 id} 바로(AI는 수십 초 걸릴 수 있어 백그라운드 스레드).
  진행은 state.agent(busy, threads[].status). 한 번에 하나만(답하는 중이면 409).
- 작업은 파일에서 새로 읽은 사본으로 답을 만들고, 앱의 Store에 잠금을 잡고 다시 읽은 뒤 그 대화만 합쳐 저장한다.
- AI(settings.ai_cli)가 꺼져 있어도 같은 창이 규칙 검색으로 답한다(by "rules"). 테스트·데모는 App(..., ai_runner=가짜).
"""
from __future__ import annotations

import json
import threading

from .. import agent, agent_cli
from ..api import ApiError, Response, router


@router.on_init
def attach(app, options):
    app.agent_jobs = {}                 # 대화 id → 스레드(이 프로세스에서 도는 것)
    app.agent_guard = threading.Lock()


def _names(app) -> list:
    n = (app.cfg or {}).get("user_name")
    return [n] if n else []


def busy(app) -> bool:
    return any(t.is_alive() for t in app.agent_jobs.values())


@router.state_section("agent")
def agent_state(app, ctx):
    st = app.store.data.get("settings") or {}
    from .ai import available
    active = {k for k, t in app.agent_jobs.items() if t.is_alive()}
    return {"enabled": bool(st.get("ai_cli")), "available": available(app), "busy": bool(active),
            "threads": agent.public_threads(app.store, active),
            "calls_today": agent_cli.calls_today(app.store),
            "limits": {"per_day": agent_cli.MAX_CALLS_PER_DAY, "per_ask": 2, "timeout": 90,
                       "max_chars": agent_cli.MAX_TEXT, "max_message": agent.MAX_MESSAGE},
            "missed_cases": len(app.store.data.get("missed_cases") or [])}


@router.route("POST", r"/api/agent/ask")
def ask(app, body):
    msg, cid = agent.check_message(body.get("message"), body.get("course_id"), app.store)   # ValueError → 400
    with app.agent_guard:
        if busy(app):
            raise ApiError(409, "이전 질문에 답하는 중이에요. 끝나면 다시 물어봐 주세요")
        with app.store.locked():
            app.store.reload_if_changed()
            th = agent.new_thread(app.store, msg, cid)
            app.store.write()
        ai_on = bool((app.store.data.get("settings") or {}).get("ai_cli"))
        # 실행기는 작업이 읽은 설정대로(settings.ai_provider, 계약 21-3). ai_command는 테스트·데모가 넣은 claude
        t = threading.Thread(target=agent.run_job, daemon=True, args=(app.store, th["id"], msg, cid),
                             kwargs={"ai_enabled": ai_on, "runner": getattr(app, "ai_runner", None),
                                     "command": getattr(app, "ai_command", None), "names": _names(app)})
        app.agent_jobs = {k: v for k, v in app.agent_jobs.items() if v.is_alive()}
        app.agent_jobs[th["id"]] = t
        t.start()
    return {"job": th["id"], "agent": agent_state(app, None)}


def wait(app, job_id: str, timeout: float = 30.0) -> bool:
    """테스트용: 그 작업이 끝날 때까지 기다린다. 끝났으면 True."""
    t = app.agent_jobs.get(job_id)
    if t is None:
        return True
    t.join(timeout)
    app.store.reload_if_changed()
    return not t.is_alive()


def _index(body) -> tuple[str, int]:
    tid, idx = body.get("thread"), body.get("proposal")
    if not isinstance(tid, str) or not tid:
        raise ApiError(400, "thread(대화 id)가 필요해요")
    if isinstance(idx, bool) or not isinstance(idx, int):
        raise ApiError(400, "proposal(제안 번호, 0부터)이 필요해요")
    return tid, idx


def _change(app, fn) -> dict:
    """반영·되돌리기: 동기화가 항목을 다시 만드는 중이면 기다렸다가(최대 30초), 파일 잠금 안에서 다시 읽고 바꾼 뒤 저장."""
    if not app.lock.acquire(timeout=30):
        raise ApiError(409, "동기화하는 중이에요. 잠시 뒤 다시 눌러 주세요")
    try:
        with app.store.locked():
            app.store.reload_if_changed()
            try:
                p = fn()
            except LookupError as e:
                raise ApiError(404, str(e)) from None
            except agent.AgentConflict as e:
                raise ApiError(409, str(e)) from None
            app.store.write()
    finally:
        app.lock.release()
    return p


@router.route("POST", r"/api/agent/apply")
def apply_proposal(app, body):
    """{"thread", "proposal": idx} → 다시 검증한 뒤 반영 → 새 state."""
    tid, idx = _index(body)
    p = _change(app, lambda: agent.apply(app.store, tid, idx, names=_names(app)))
    if p.get("type") in ("add_deadline", "fix_due"):
        app.emit("changed")   # 구글 캘린더 반영 등
    return app.state()


@router.route("POST", r"/api/agent/undo")
def undo_proposal(app, body):
    """{"thread", "proposal": idx} → 반영 전 값으로 → 새 state."""
    tid, idx = _index(body)
    p = _change(app, lambda: agent.undo(app.store, tid, idx))
    if p.get("type") in ("add_deadline", "fix_due"):
        app.emit("changed")
    return app.state()


@router.route("POST", r"/api/agent/clear")
def clear(app, body):
    """대화 기록 지우기(반영한 마감·연결은 그대로 — 항목 화면에서 되돌릴 수 있다). 돌던 작업의 답은 버려진다."""
    with app.store.locked():
        app.store.reload_if_changed()
        agent._box(app.store)["threads"] = []
        app.store.write()
    return app.state()


@router.route("GET", r"/api/agent/missed-cases")
def missed_cases(app, query):
    """설정 → AI의 '놓친 사례 N개 · [파일로 저장]'(계약 20-5): 가린 문장만 든 JSON 파일. 사용자가 원할 때만, 자동으로 보내지 않는다."""
    data = {"app": "lms-agent", "missed_cases": agent.missed_cases_export(app.store)}
    return Response(200, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
                    "application/json; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="lms-agent-missed-cases.json"'})
