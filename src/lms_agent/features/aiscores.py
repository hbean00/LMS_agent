"""AI로 성적 찾기(계약 29-2, 담당 S2): 경로·능력. 일은 aiscores.py가 한다.

- POST /api/grades/ai-lookup {course_id?, key?} → 새 state + "job"(작업 요약) 바로. AI(settings.ai_cli)가 꺼져 있거나 학번이 없으면 400,
  모르는 과목·파일 404, 이미 도는 중이면 409. 대상이 없으면 스레드 없이 끝난 작업(total 0, message)으로 답한다.
  작업은 백그라운드 스레드에서 **파일에서 새로 읽은 사본**(Store(app.store.path))으로 AI를 부르고(잠금 밖 — 몇십 초), 끝나면
  앱의 Store에 파일 잠금을 잡고 다시 읽은 뒤 그 작업이 본 파일의 ai_scores 칸·새 AI 연결(grade_links_ai — 없던 것만)·AI 실행 기록만
  합쳐 저장한다(그 사이 사용자가 바꾼 것을 지우지 않게). 한 번에 aiscores.ROUTE_MAX개 파일, 하루 AI 한도 안.
  성적(후보·추정)만 바뀌고 일정은 그대로라 "changed"(구글 캘린더 반영)는 내지 않는다.
- 진행·결과: state.grades.ai_lookup = aiscores.lookup_state — gradetrace.state_fields가 성적 묶음에 넣는다(별도 상태 칸 없음).
- 능력 grades.ai_lookup(direct, 되돌리기 기록 없음 — 찾은 점수는 후보의 '넣지 않음'으로 뺀다).
"""
from __future__ import annotations

import threading
import uuid

from .. import agent_cli, aiscores
from .. import capabilities as caps
from ..api import ApiError, router
from ..timeutil import iso, now_local

ROUTE = "/api/grades/ai-lookup"


@router.on_init
def attach(app, options):
    app.ai_lookup_thread = None
    app.ai_lookup_guard = threading.Lock()


def _names(app) -> list:
    n = (getattr(app, "cfg", None) or {}).get("user_name")
    return [n] if n else []


def _clean(app, body) -> tuple[str | None, str | None]:
    """본문 검사 → (과목 id | None, 파일 key | None). 잘못되면 ApiError."""
    body = body if isinstance(body, dict) else {}
    cid, key = body.get("course_id"), body.get("key")
    if cid in ("", None):
        cid = None
    elif isinstance(cid, bool) or not isinstance(cid, (str, int)) or len(str(cid)) > 40:
        raise ApiError(400, "course_id(과목 id)가 올바르지 않아요")
    else:
        cid = str(cid)
        known = {str(c.get("id")) for c in (app.store.data.get("cache") or {}).get("courses") or [] if isinstance(c, dict)}
        if cid not in known:
            raise ApiError(404, "그 과목을 찾지 못했어요(먼저 동기화해 주세요)")
    if key in ("", None):
        key = None
    elif not isinstance(key, str) or len(key) > 300:
        raise ApiError(400, "key(성적 파일 key)가 올바르지 않아요")
    else:
        rec = (app.store.data.get("pdf_texts") or {}).get(key)
        if not isinstance(rec, dict):
            raise ApiError(404, "그 파일의 읽은 글이 없어요(받은 파일을 다시 읽거나 성적 파일을 가져와 주세요)")
        if cid is not None and str(rec.get("course_id")) != cid:
            raise ApiError(400, "그 파일은 고른 과목의 파일이 아니에요")
    return cid, key


def ready_error(store) -> str | None:
    """지금 AI로 찾을 수 없는 이유(한국어) | None."""
    st = store.data.get("settings") or {}
    if not st.get("ai_cli"):
        return "AI가 꺼져 있어요 — 설정 → AI 도움에서 켜면 AI로 성적 파일을 찾아볼 수 있어요"
    if not st.get("student_id"):
        return "학번을 먼저 넣어 주세요(설정 → 성적) — 점수표에서 내 줄을 찾는 데만 써요"
    return None


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _run(app, job: dict, cid, key, names) -> None:
    """백그라운드: 사본으로 AI → 앱 Store에 그 파일 칸만 합쳐 저장."""
    from ..store import Store
    store = app.store
    try:
        work = Store(store.path)
        before_runs = list(work.data.get("ai_runs") or [])
        before_links = {c: dict(v) for c, v in (work.data.get("grade_links_ai") or {}).items() if isinstance(v, dict)}
        session = agent_cli.AiSession(work, runner=getattr(app, "ai_runner", None), command=getattr(app, "ai_command", None),
                                      max_calls=2 * aiscores.ROUTE_MAX)

        def step(t, entry):   # 진행(화면이 'N개 중 M개'를 보인다)
            cur = aiscores.job_of(store) or job
            status = "error" if entry.get("error") else ("found" if entry.get("results") else "none")
            aiscores.update_job(store, done=cur.get("done", 0) + 1, current=t.get("filename"),
                                found=cur.get("found", 0) + (status == "found"),
                                none=cur.get("none", 0) + (status == "none"),
                                failed=cur.get("failed", 0) + (status == "error"))
        res = aiscores.lookup(work, session, course_id=cid, key=key, names=names, limit=aiscores.ROUTE_MAX,
                              explicit=True, by="user", on_step=step)
        new_runs = [r for r in work.data.get("ai_runs") or [] if r not in before_runs]
        with store.locked():
            store.reload_if_changed()
            box = store.data.setdefault("ai_scores", {})
            for k in res["keys"]:
                e = (work.data.get("ai_scores") or {}).get(k)
                if isinstance(e, dict):
                    box[k] = e
            for c, slot in (work.data.get("grade_links_ai") or {}).items():
                for k, v in (slot or {}).items():
                    if k not in before_links.get(c, {}):
                        store.data.setdefault("grade_links_ai", {}).setdefault(c, {}).setdefault(k, v)
            if new_runs:
                runs = store.data.setdefault("ai_runs", [])
                runs.extend(r for r in new_runs if r not in runs)
                del runs[:-agent_cli.RUN_LOG_KEEP]
            store.write()
        msg = (f"파일 {res['total']}개 중 {len(res['keys'])}개를 봤어요 — 점수를 찾은 파일 {res['found']}개"
               + (f", 멈춘 이유: {res['error']}" if res.get("error") else ""))
        aiscores.update_job(store, status="done", finished_at=iso(now_local()), total=res["total"],
                            done=len(res["keys"]), found=res["found"], none=res["none"], failed=res["failed"],
                            error=res.get("error"), message=msg, current=None)
    except Exception as e:  # noqa: BLE001 — 화면이 '찾는 중'에 멈춰 있지 않게(내용·비밀값 없이 종류만)
        aiscores.update_job(store, status="error", finished_at=iso(now_local()), error=f"찾지 못했어요({type(e).__name__})",
                            message="AI로 성적 찾기가 중간에 멈췄어요", current=None)


@router.route("POST", ROUTE, priority=1)   # /api/grades/<course_id>(성적 저장)보다 먼저
def ai_lookup(app, body):
    """{course_id?, key?} → AI로 성적 파일 찾기(백그라운드). 응답: 새 state + "job"."""
    why = ready_error(app.store)
    if why:
        raise ApiError(400, why)
    cid, key = _clean(app, body)
    names = _names(app)
    with app.ai_lookup_guard:
        t = getattr(app, "ai_lookup_thread", None)
        cur = aiscores.job_of(app.store)
        if (t is not None and t.is_alive()) or (cur and cur.get("status") == "running"):
            raise ApiError(409, "AI로 성적을 찾는 중이에요. 끝나면 다시 눌러 주세요")
        todo = [x for x in aiscores.targets(app.store, cid, key, names) if aiscores.needs(app.store, x, explicit=True)]
        job = {"id": "s-" + uuid.uuid4().hex[:10], "status": "running", "course_id": cid, "key": key,
               "started_at": iso(now_local()), "finished_at": None, "total": len(todo), "done": 0, "found": 0, "none": 0,
               "failed": 0, "error": None, "message": None, "current": None}
        if not todo:
            if key is not None and aiscores.rules_found(key, (app.store.data.get("pdf_texts") or {}).get(key) or {},
                                                        str(app.store.data["settings"].get("student_id"))):
                msg = "이 파일은 규칙이 이미 내 점수를 읽었어요 — AI로 다시 찾지 않아요"
            elif aiscores.targets(app.store, cid, key, names):
                msg = "AI가 이미 다 찾아봤어요(파일이 바뀌면 다시 찾아요)"
            else:
                msg = "AI로 찾아볼 성적 파일이 없어요(규칙이 다 읽었거나 내 학번 줄이 있는 점수 파일이 없어요)"
            job.update(status="done", finished_at=job["started_at"], message=msg)
            aiscores.set_job(app.store, job)
        else:
            aiscores.set_job(app.store, job)
            th = threading.Thread(target=_run, args=(app, job, cid, key, names), daemon=True)
            app.ai_lookup_thread = th
            th.start()
    out = app.state()
    out["job"] = _public(aiscores.job_of(app.store) or job)
    return out


def wait(app, timeout: float = 30.0) -> bool:
    """테스트용: [AI로 성적 찾기] 작업이 끝날 때까지 기다린다. 끝났으면 True."""
    t = getattr(app, "ai_lookup_thread", None)
    if t is None:
        return True
    t.join(timeout)
    app.store.reload_if_changed()
    return not t.is_alive()


# ---------------------------------------------------------------- 능력(계약 28장)

def _cap_build(app, p: dict) -> tuple:
    why = ready_error(app.store)
    if why:
        raise ValueError(why)
    body = {}
    if p.get("course") is not None:
        body["course_id"] = p["course"]
    if p.get("key") is not None:
        if not isinstance((app.store.data.get("pdf_texts") or {}).get(p["key"]), dict):
            raise ValueError("그 성적 파일을 찾지 못했어요 — 성적 화면의 '성적 파일 진단'에서 파일을 골라 주세요")
        body["key"] = p["key"]
    return ROUTE, body


def _cap_preview(app, p: dict) -> list:
    _cap_build(app, p)
    todo = [x for x in aiscores.targets(app.store, p.get("course"), p.get("key"), _names(app))
            if aiscores.needs(app.store, x, explicit=True)]
    if not todo:
        raise ValueError("AI로 찾아볼 성적 파일이 없어요(규칙이 다 읽었거나, 이미 찾아봤거나, 내 학번 줄이 있는 점수 파일이 없어요)")
    where = caps.course_short(app, p["course"]) if p.get("course") else "모든 과목"
    return [{"label": f"AI로 성적 찾기({where})", "before": f"규칙이 못 읽은 파일 {len(todo)}개",
             "after": f"AI가 최대 {min(len(todo), aiscores.ROUTE_MAX)}개를 읽음(끝나면 'AI가 찾음' 후보)"}]


def _cap_message(app, p: dict, res) -> str:
    job = (res or {}).get("job") if isinstance(res, dict) else None
    if isinstance(job, dict) and job.get("status") == "done":
        return str(job.get("message") or "AI로 찾아볼 파일이 없어요")
    return ("AI로 성적 파일을 찾기 시작했어요 — 끝나면 성적 화면에 'AI가 찾음' 후보로 보여요. 맞지 않으면 그 후보를 "
            "'넣지 않음'으로 빼면 돼요")


caps.register(caps.Capability(
    "grades.ai_lookup", "grades", "AI로 성적 파일 찾기(규칙이 못 읽은 점수표에서 내 점수 — 다른 학생 줄은 보내지 않음)",
    ("POST", ROUTE),
    {"course": caps.P("str", "과목(없으면 모든 과목)", required=False, resolve="course"),
     "key": caps.P("str", "성적 파일 key(성적 파일 진단의 파일 — 없으면 그 과목 전부)", required=False, max=300)},
    tier="direct", build=_cap_build, preview=_cap_preview, undoable=False))
caps._MESSAGES["grades.ai_lookup"] = _cap_message
