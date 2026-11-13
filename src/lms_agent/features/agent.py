"""에이전트에게 묻기(채팅) — 계약 20장. 일은 agent.py가 하고, 여기는 경로·상태 칸·백그라운드 작업만.

- POST /api/agent/ask {"message", "course_id"?} → {"job": 대화 id} 바로(AI는 수십 초 걸릴 수 있어 백그라운드 스레드).
  진행은 state.agent(busy, threads[].status). 한 번에 하나만(답하는 중이면 409).
- 작업은 파일에서 새로 읽은 사본으로 답을 만들고, 앱의 Store에 잠금을 잡고 다시 읽은 뒤 그 대화만 합쳐 저장한다.
- AI(settings.ai_cli)가 꺼져 있어도 같은 창이 규칙 검색으로 답한다(by "rules"). 테스트·데모는 App(..., ai_runner=가짜).
- 액션 제안(type "action", 계약 22-9)은 /api/agent/apply·undo에서 actions.run/undo로 실행한다. 액션은 화면 단추와 같은 API 경로를
  앱 안에서 부르므로(그 경로가 잠금을 잡는다) **잠금 밖에서** 실행하고, 결과만 파일 잠금 안에서 제안에 남긴다.
"""
from __future__ import annotations

import copy
import json
import threading

from .. import actions, agent, agent_cli
from ..api import ApiError, Response, router


@router.on_init
def attach(app, options):
    app.agent_jobs = {}                 # 대화 id → 스레드(이 프로세스에서 도는 것)
    app.agent_guard = threading.Lock()
    app.agent_action_guard = threading.Lock()   # 액션 실행·되돌리기는 한 번에 하나(두 번 눌러도 한 번만)


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


def _peek(app, tid: str, idx: int) -> dict:
    """그 제안의 지금 값(사본). 없는 대화 404, 없는 번호 400."""
    with app.store.locked():
        app.store.reload_if_changed()
        try:
            _, p = agent.proposal(app.store, tid, idx)
        except LookupError as e:
            raise ApiError(404, str(e)) from None
        return copy.deepcopy(p)


def _run_action(app, tid: str, idx: int, undo: bool) -> None:
    """액션 제안 실행·되돌리기(계약 22-9): 검사 → actions.run/undo(잠금 밖 — 그 API 경로가 스스로 잠금을 잡는다) → 결과를 잠금
    안에서 제안에 남김. 실패(ok false)는 결과를 남기고 400."""
    if not app.agent_action_guard.acquire(blocking=False):
        raise ApiError(409, "다른 동작을 실행하는 중이에요. 끝나면 다시 눌러 주세요")
    try:
        p = _peek(app, tid, idx)
        if undo:
            if not p.get("applied"):
                raise ApiError(409, "실행하지 않은 동작이에요")
            if not p.get("undoable"):
                raise ApiError(409, "되돌릴 수 없는 동작이에요")
        else:
            if p.get("applied"):
                raise ApiError(409, "이미 실행한 동작이에요")
            if p.get("client"):
                raise ApiError(400, "화면에서 바로 하는 동작이에요(서버에서 실행하지 않아요)")
            if p.get("rejected_reason"):
                raise ApiError(400, f"실행할 수 없는 동작이에요: {p['rejected_reason']}")
            again = agent.check_action(app.store, p, by=p.get("by") or "rules")   # 지금 상태로 다시 검사
            if again.get("rejected_reason"):
                raise ApiError(400, f"지금 다시 확인해 보니 실행할 수 없어요: {again['rejected_reason']}")
        status = 400
        try:
            res = actions.undo(app, p) if undo else actions.run(app, p)
            if not isinstance(res, dict):
                res = {"ok": False, "message": "동작 결과를 알 수 없어요"}
        except ApiError as e:            # 그 API 경로가 거절함(예: 동기화하는 중 409)
            res, status = {"ok": False, "message": str(e)}, e.status
        except ValueError as e:          # 그 경로의 검사(한국어 문구)
            res = {"ok": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001 — 실패도 대화에 남기고 화면에는 오류 문구만(내용·비밀값 없이)
            res, status = {"ok": False, "message": f"실행하지 못했어요({type(e).__name__})"}, 500
        with app.store.locked():
            app.store.reload_if_changed()
            agent.record_action(app.store, tid, idx, res, undo=undo)
            app.store.write()
        if not res.get("ok"):
            raise ApiError(status, str(res.get("message") or ("되돌리지 못했어요" if undo else "실행하지 못했어요"))[:300])
    finally:
        app.agent_action_guard.release()


@router.route("POST", r"/api/agent/apply")
def apply_proposal(app, body):
    """{"thread", "proposal": idx} → 다시 검증한 뒤 반영(액션이면 실행) → 새 state."""
    tid, idx = _index(body)
    if _peek(app, tid, idx).get("type") == "action":
        _run_action(app, tid, idx, undo=False)
        return app.state()
    p = _change(app, lambda: agent.apply(app.store, tid, idx, names=_names(app)))
    if p.get("type") in ("add_deadline", "fix_due"):
        app.emit("changed")   # 구글 캘린더 반영 등
    return app.state()


@router.route("POST", r"/api/agent/undo")
def undo_proposal(app, body):
    """{"thread", "proposal": idx} → 반영 전 값으로(액션이면 actions.undo) → 새 state."""
    tid, idx = _index(body)
    if _peek(app, tid, idx).get("type") == "action":
        _run_action(app, tid, idx, undo=True)
        return app.state()
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
