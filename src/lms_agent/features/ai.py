"""AI CLI 에이전트: 설정, 상태, 확인함 정리(triage), 실행기 고르기·연결 시험. 계약 10·12·21장.

- 사용자가 settings.ai_cli를 켜야만 AI를 부른다(켜면 강의 글 일부가 고른 AI 서비스로 간다 — 화면에서 안내).
- 실행기(claude·codex·gemini·opencode·copilot·ollama·custom)는 settings.ai_provider("auto" 기본)·ai_custom·ai_model로
  고른다. 동기화·확인함 정리·에이전트에게 묻기가 모두 같은 설정을 따른다(agent_cli.resolve).
- 테스트·데모는 App(..., ai_runner=가짜, ai_command="claude")로 실제 CLI를 띄우지 않는다. ai_command를 넣으면 PATH를
  보지 않고 claude 하나만 찾은 것으로 보여 준다(다른 CLI는 '없음').
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid

from .. import agent_cli
from ..api import ApiError, router
from ..timeutil import iso, now_local

FIND_TTL = 30.0   # 화면 상태 계산의 PATH 찾기는 30초에 한 번만(동기화·실행은 매번 새로)


@router.on_init
def attach(app, options):
    app.ai_runner = options.get("ai_runner")     # 테스트용 가짜 실행기(None이면 실제 CLI)
    app.ai_command = options.get("ai_command")   # 테스트용 claude 명령(None이면 PATH에서 찾음)
    app.ai_find = {}                             # 실행기 id → (시각, 경로)
    app.ai_test = None                           # [연결 시험] 진행·결과(이 프로세스 메모리에만)
    app.ai_test_thread = None
    app.ai_guard = threading.Lock()


def _settings(app) -> dict:
    return app.store.data.get("settings") or {}


def _discover(app) -> bool:
    return not getattr(app, "ai_command", None)


def _finder(app):
    cache = app.ai_find

    def find(adapter):
        hit = cache.get(adapter.id)
        if hit and time.monotonic() - hit[0] < FIND_TTL:
            return hit[1]
        path = agent_cli.find_exe(adapter)
        cache[adapter.id] = (time.monotonic(), path)
        return path
    return find


def entries(app, probe_now: bool = False, settings: dict | None = None) -> list[dict]:
    """state.ai.providers의 바탕(내부 칸 포함). probe_now면 확인하지 않은 CLI의 도움말을 읽는다."""
    return agent_cli.providers(settings if settings is not None else _settings(app), claude_exe=app.ai_command,
                               discover=_discover(app), probe_now=probe_now, finder=_finder(app))


def current(app) -> dict | None:
    """지금 설정이면 쓸 실행기(새로 확인하지 않음) — 화면 표시용."""
    return agent_cli.pick(entries(app), _settings(app))


def available(app) -> bool:
    return current(app) is not None


def _custom_public(st: dict) -> dict:
    c = agent_cli.custom_settings(st)
    if not c:
        return {"set": False, "name": None, "args": [], "output": "text", "stdin": True}
    return {"set": True, "name": agent_cli.command_name(c["path"]),
            "args": [agent_cli._scrub(str(a), None) if str(a) else "" for a in c.get("args") or []],
            "output": c.get("output") if c.get("output") in agent_cli.OUTPUTS else "text", "stdin": True}


def _test_public(app) -> dict | None:
    t = app.ai_test
    if not t:
        return None
    return {**t, "cases": [dict(c) for c in t.get("cases") or []]}


@router.state_section("ai")
def ai_state(app, ctx):
    st = _settings(app)
    runs = app.store.data.get("ai_runs") or []
    last = runs[-1] if runs else None
    ents = entries(app)
    want = st.get("ai_provider") or "auto"
    cur = agent_cli.pick(ents, st)
    plan = cur.get("_plan") if cur else None
    reason = None
    if cur is None:
        by = {e["id"]: e for e in ents}
        if want == "auto" and any(e["found"] and e["status"] == "needs_check" and not e["probed"] for e in ents):
            reason = "찾은 AI CLI를 아직 확인하지 않았어요 — [다시 찾기]로 도움말을 읽어 확인해요"
        else:
            reason = agent_cli.why_not(by.get(want), want)
    return {"available": bool(cur), "command": (plan.cli if plan else None) or agent_cli.DEFAULT_COMMAND,
            "enabled": bool(st.get("ai_cli")), "auto_triage": bool(st.get("ai_auto_triage")),
            "last_error": (last or {}).get("error") if last and not last.get("ok") else None,
            "calls_today": agent_cli.calls_today(app.store),
            "limits": {"per_sync": agent_cli.MAX_CALLS_PER_SYNC, "per_day": agent_cli.MAX_CALLS_PER_DAY,
                       "max_chars": agent_cli.MAX_TEXT},
            "recent_runs": agent_cli.recent_runs(app.store, 5),
            "last_triage": app.store.data.get("ai_triage"),
            # 계약 21-3: 실행기 목록(이름만, 경로 없음)과 지금 쓰는 것
            "providers": [agent_cli.public(e) for e in ents],
            "provider": cur["id"] if cur else None,
            "provider_name": (f"{cur['name']} · {cur['path_hint']}" if cur["id"] == "custom" and cur.get("path_hint")
                              else cur["name"]) if cur else None,
            "selected": want,
            "local": bool(cur and cur.get("local")),
            "unavailable_reason": reason,
            "model": st.get("ai_model") or None,
            "custom": _custom_public(st),
            "auto_order": list(agent_cli.AUTO_ORDER),
            "test": _test_public(app),
            "test_cases": len(agent_cli.EVAL_CASES)}


@router.route("POST", r"/api/ai/settings")
def ai_settings(app, body):
    """{"ai_cli"?: bool, "ai_auto_triage"?: bool}"""
    for key in ("ai_cli", "ai_auto_triage"):
        if key in body:
            app.store.data["settings"][key] = bool(body[key])
    app.store.save()
    return app.state()


# ---------------------------------------------------------------- 실행기 고르기(계약 21-3)

_LINE_BAD = re.compile(r"[\x00\r\n]")


def _expand_home(arg: str) -> str:
    """화면에는 홈 폴더를 '~'로 보여 주므로(경로 숨김), 저장할 때 맨 앞·'=' 뒤의 '~'를 되돌린다."""
    home = os.path.expanduser("~")
    return re.sub(r"(^|=)~(?=[\\/]|$)", lambda m: m.group(1) + home, arg)


def _check_custom(c, old) -> dict | None:
    """{"path"?, "args"?: [한 줄에 하나], "output"?, "stdin"?} → 저장할 모양. 빠진 칸은 예전 값. None이면 지움."""
    if c is None:
        return None
    if not isinstance(c, dict):
        raise ApiError(400, "custom은 {path, args, output} 모양이어야 해요")
    old = old if isinstance(old, dict) else {}
    path = c.get("path")
    path = str(path).strip().strip('"').strip() if path not in (None, "") else str(old.get("path") or "")
    if not path:
        raise ApiError(400, "실행 파일 경로(또는 PATH에 있는 이름)를 넣어 주세요")
    if len(path) > 500 or _LINE_BAD.search(path):
        raise ApiError(400, "실행 파일 경로가 너무 길거나 줄바꿈이 있어요")
    args = c.get("args", old.get("args") or [])
    if isinstance(args, str):
        args = args.splitlines()
    if not isinstance(args, list):
        raise ApiError(400, "인자는 한 줄에 하나씩 넣어 주세요")
    clean = []
    for a in args:
        a = str(a if a is not None else "").strip()
        if not a:
            continue
        a = "" if a in ('""', "''") else _expand_home(a)   # 빈 문자열 인자는 "" 로 적는다
        if len(a) > 300 or _LINE_BAD.search(a):
            raise ApiError(400, "인자 하나가 너무 길거나 줄바꿈이 있어요")
        clean.append(a)
    if len(clean) > 40:
        raise ApiError(400, "인자는 40개까지 넣을 수 있어요")
    if path.lower().endswith((".cmd", ".bat")) and any(agent_cli._BATCH_UNSAFE.search(a) for a in clean):
        raise ApiError(400, ".cmd·.bat 실행 파일에는 인자에 \" % ^ & | < > ! 문자를 쓸 수 없어요(명령 창이 다시 읽어요)")
    output = c.get("output", old.get("output") or "text")
    if output not in agent_cli.OUTPUTS:
        raise ApiError(400, "출력 형식은 " + "·".join(agent_cli.OUTPUTS) + " 중 하나예요")
    if c.get("stdin") is False:
        raise ApiError(400, "글은 항상 표준 입력(stdin)으로 보내요 — 명령줄에 강의 글을 넣지 않아요")
    return {"path": path, "args": clean, "stdin": True, "output": output}


def _check_model(m) -> str | None:
    if m in (None, ""):
        return None
    m = str(m).strip()
    if not m:
        return None
    if not agent_cli._MODEL_NAME.match(m):
        raise ApiError(400, "모델 이름은 영문·숫자와 . : / @ + - _ 만 쓸 수 있어요(예: llama3.2:3b)")
    return m


@router.route("POST", r"/api/ai/provider")
def ai_provider(app, body):
    """{"provider"?: "auto"|id, "custom"?: {path?, args?, output?}|null, "model"?: str|null} → 확인 뒤 저장 → 새 state.
    직접 고른 실행기는 지금 쓸 수 있어야 저장한다(못 쓰면 400과 이유 — 아무것도 바꾸지 않음)."""
    st = app.store.data["settings"]
    new = {k: st.get(k) for k in ("ai_provider", "ai_custom", "ai_model")}
    if "custom" in body:
        new["ai_custom"] = _check_custom(body.get("custom"), st.get("ai_custom"))
    if "model" in body:
        new["ai_model"] = _check_model(body.get("model"))
    if "provider" in body:
        want = body.get("provider")
        if not isinstance(want, str) or (want != "auto" and want not in agent_cli.ADAPTERS):
            raise ApiError(400, "모르는 AI 실행기예요")
        new["ai_provider"] = want
    want = new.get("ai_provider") or "auto"
    if want == "custom" and not new.get("ai_custom"):
        raise ApiError(400, "직접 지정할 실행 파일을 먼저 넣어 주세요")

    def merged(base: dict) -> dict:
        for k, v in new.items():
            if v is None:
                base.pop(k, None)
            else:
                base[k] = v
        return base

    if want != "auto":   # 직접 고른 것은 지금 쓸 수 있어야(도움말 확인까지) 저장한다
        e = agent_cli._entry(want, merged(dict(st)), app.ai_command, _discover(app), True, None, _finder(app))
        if e["status"] != "ready":
            raise ApiError(400, agent_cli.why_not(e, want))
    merged(st)
    app.store.save()
    return app.state()


@router.route("POST", r"/api/ai/rescan")
def ai_rescan(app, body):
    """찾은 CLI의 도움말을 (다시) 읽어 상태를 새로 정한다(몇 초). 화면의 [다시 찾기]·설정 창을 처음 열 때."""
    agent_cli.clear_probe_cache()
    app.ai_find.clear()
    entries(app, probe_now=True)
    return app.state()


# ---------------------------------------------------------------- 연결 시험(데모 문장만, 계약 21-3)

def _run_test(app, job: dict, plan) -> None:
    def progress(done, total, case):
        job["cases"] = job["cases"] + [{k: case.get(k) for k in ("name", "label", "task", "passed", "ok", "problem",
                                                                  "error", "attempts", "duration_ms")}]
        job.update(done=done, total=total, passed=sum(1 for c in job["cases"] if c["passed"]))
    try:
        r = agent_cli.run_evals(runner=getattr(app, "ai_runner", None), provider=plan, progress=progress)
        job.update(status="done" if not r.get("error") else "error", passed=r["passed"], total=r["total"],
                   duration_ms=r["duration_ms"], error=r.get("error"))
    except Exception as e:  # noqa: BLE001 — 화면이 '시험 중'에 멈춰 있지 않게
        job.update(status="error", error=f"연결 시험을 끝내지 못했어요({type(e).__name__})")
    job["finished_at"] = iso(now_local())


@router.route("POST", r"/api/ai/test")
def ai_test(app, body):
    """{"provider"?} → 그 실행기로 평가 묶음(run_evals — 데모 문장, 강의 글 아님)을 백그라운드에서 돌린다.
    진행·결과는 state.ai.test {status, provider, done, total, passed, cases[], duration_ms, error}. 응답은 새 state + "job"."""
    st = _settings(app)
    want = body.get("provider") or st.get("ai_provider") or "auto"
    if not isinstance(want, str) or (want != "auto" and want not in agent_cli.ADAPTERS):
        raise ApiError(400, "모르는 AI 실행기예요")
    with app.ai_guard:
        if app.ai_test_thread is not None and app.ai_test_thread.is_alive():
            raise ApiError(409, "연결 시험이 이미 도는 중이에요. 끝나면 다시 눌러 주세요")
        plan, why = agent_cli.select(st, claude_exe=app.ai_command, discover=_discover(app), want=want)
        if plan is None:
            raise ApiError(400, why)
        job = {"id": "x-" + uuid.uuid4().hex[:10], "status": "running", "provider": plan.id, "name": plan.name,
               "cli": plan.cli, "local": plan.local, "started_at": iso(now_local()), "finished_at": None, "done": 0,
               "total": len(agent_cli.EVAL_CASES), "passed": 0, "cases": [], "duration_ms": 0, "error": None}
        app.ai_test = job
        t = threading.Thread(target=_run_test, args=(app, job, plan), daemon=True)
        app.ai_test_thread = t
        t.start()
    out = app.state()
    out["job"] = job["id"]
    return out


def wait_test(app, timeout: float = 30.0) -> bool:
    """테스트용: 연결 시험이 끝날 때까지 기다린다. 끝났으면 True."""
    t = app.ai_test_thread
    if t is None:
        return True
    t.join(timeout)
    return not t.is_alive()


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
