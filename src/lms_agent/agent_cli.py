"""AI 작업 틀(harness): 규칙으로 못 한 일만, 사용자가 켠 경우에만, 사용자 PC의 AI CLI(기본 `claude`)에 맡긴다. 계약 10·12장.

구성
- 작업 목록(TASKS): 작업마다 목적, 지시문(한국어+영어, JSON만 답하게), 보낼 글 만들기(글자 수 예산·가리기),
  JSON 스키마(형식·범위 검사), 사후 검사(비중 합 ~100, 근거 문장이 보낸 글에 그대로 있는지, 날짜가 근거와 맞는지 …),
  재시도(검사 오류를 그대로 인용해 고쳐 달라고 한 번 더), 시간 제한.
  작업: grading_weights(평가 비중), deadline_from_text(마감), triage_review(확인함 후보 정리 — 계약 12장),
  map_score_item(점수·마감 → 평가 항목), count_items(세부 항목 개수 — 계약 14장), grade_distribution(학점 비율 — 16장).
- 실행(run_task): `claude -p <고정된 짧은 지시> --output-format json` (+ 도구 끄기 `--tools ""`·세션 저장 안 함 — 옛 버전이
  모르면 빼고 다시). 글은 **표준 입력(stdin)으로만** 보낸다(명령줄 인자 X). shell 없이, Windows에서는 창 없이
  (CREATE_NO_WINDOW), 빈 임시 폴더에서 실행해 CLI가 프로젝트 파일을 읽지 않게 한다. 호출한 쪽으로 예외를 던지지 않고
  AiResult{ok, data, error, attempts, duration_ms, cli, model}를 돌려준다.
- 가리기(redact): 보내기 전에 이메일·전화번호·학번(가린 학번 포함)·링크·토큰처럼 보이는 긴 문자열·알려 준 이름·편지 서명을
  지운다. 점수·토큰·이름은 보내지 않는다(성적 파일은 어떤 작업에도 넣지 않는다).
- 실행 기록: store.data["ai_runs"]에 작업·성공·시도 수·걸린 시간·보낸 글자 수·오류만(최근 50개). 지시문·글·답은 남기지 않는다.
- 정책(AiSession): settings.ai_cli가 true일 때만, 동기화 한 번에 최대 MAX_CALLS_PER_SYNC회, 하루 MAX_CALLS_PER_DAY회.
  AI 결과는 "by": "ai"로 표시되고 근거(검증된 원문 인용)와 함께 보인다. 사용자가 되돌릴 수 있다.
- 평가(run_evals): 실제 형식의 글로 같은 작업을 돌려 결과를 검사한다(가짜 실행기 또는 실제 CLI).
- 다른 CLI: 환경 변수 LMS_AGENT_AI_CLI에 실행 파일 전체 경로(같은 인자를 받아야 함).
  화면·오류·기록에는 명령 이름(예: "claude")만 남기고 사용자 이름이 든 전체 경로는 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import distribution as _dist
from .extract import KO_WD, find_dates, html_to_text
from .grades import GRADES
from .store import group_key
from .timeutil import iso, local_tz, now_local, parse_iso
from .weights import WeightsFound, display_names, is_fixed

ENV_VAR = "LMS_AGENT_AI_CLI"
DEFAULT_COMMAND = "claude"
MAX_TEXT = 12_000
MAX_CALLS_PER_SYNC = 5
MAX_CALLS_PER_DAY = 30
RUN_LOG_KEEP = 50
TRIAGE_MAX_ITEMS = 30
_BATCH_UNSAFE = re.compile(r'["%^&|<>!\r\n]')   # .cmd 실행 파일은 cmd.exe가 인자를 다시 읽는다


class AgentCliError(Exception):
    """AI CLI를 못 찾음·시간 초과·실패·답 해석 실패. 메시지는 한국어, 전체 경로·글 내용은 넣지 않는다."""


class _Invalid(Exception):
    """답이 스키마·사후 검사를 통과하지 못함 → 오류를 인용해 다시 묻는다."""


# ---------------------------------------------------------------- CLI 찾기·실행

def detect() -> str | None:
    """쓸 수 있는 AI CLI 실행 파일 경로(없으면 None). LMS_AGENT_AI_CLI가 있으면 그것만 본다."""
    override = (os.environ.get(ENV_VAR) or "").strip().strip('"')
    if override:
        if os.path.isfile(override):
            return override
        return shutil.which(override)
    return shutil.which(DEFAULT_COMMAND)


def command_name(path: str | None) -> str | None:
    """화면에 보여 줄 이름: 'C:\\Users\\…\\npm\\claude.CMD' → 'claude'."""
    if not path:
        return None
    name = re.split(r"[\\/]", str(path).rstrip("\\/"))[-1]   # 윈도·맥·리눅스 경로 모두(Path는 지금 OS 규칙만 앎)
    return os.path.splitext(name)[0].lower() or None


def _scrub(msg: str, exe: str | None) -> str:
    """오류 문구에서 전체 경로·홈 폴더(사용자 이름)를 지운다."""
    msg = msg or ""
    if exe:
        msg = msg.replace(exe, command_name(exe) or "AI CLI")
    try:
        home = str(Path.home())
        if home and len(home) > 3:
            msg = re.sub(re.escape(home), "~", msg, flags=re.I)
    except RuntimeError:
        pass
    msg = re.sub(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s)]+", "~", msg, flags=re.I)   # 다른 OS 모양의 홈 경로도
    msg = re.sub(r"/(?:home|Users)/[^/\s]+", "~", msg)
    return re.sub(r"\s+", " ", msg).strip()[:200]


def _default_runner(args: list, input: str, timeout: float, cwd: str) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=cwd, text=True, encoding="utf-8", errors="replace", creationflags=flags)
    try:
        out, err = p.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # claude.cmd → node 자식까지 끝낸다
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, creationflags=flags)
        p.kill()
        p.communicate()
        raise
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def _safe_prompt(prompt: str, exe: str) -> str:
    p = re.sub(r"\s+", " ", prompt or "").strip()
    if exe.lower().endswith((".cmd", ".bat")):
        p = _BATCH_UNSAFE.sub(" ", p)
    return p


def _first_json_object(s: str) -> dict | None:
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", s or ""):
        try:
            obj, _ = dec.raw_decode(s, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _payload(stdout: str) -> tuple[dict | None, dict]:
    """claude --output-format json 출력 → (모델 글 안의 첫 JSON 객체 | None, {"model"}). CLI 자체 오류는 AgentCliError."""
    meta: dict = {}
    try:
        outer = json.loads(stdout)
    except ValueError:
        outer = None
    if isinstance(outer, list):  # 옛 버전: 메시지 목록, 마지막이 result
        outer = next((x for x in reversed(outer) if isinstance(x, dict) and x.get("type") == "result"), None)
    if isinstance(outer, dict) and "result" in outer:
        mu = outer.get("modelUsage")
        meta["model"] = outer.get("model") or (next(iter(mu), None) if isinstance(mu, dict) and mu else None)
        if outer.get("is_error") or str(outer.get("subtype", "")).startswith("error"):
            raise AgentCliError(f"AI CLI가 오류를 돌려줬습니다: {str(outer.get('result') or outer.get('subtype'))[:150]}")
        inner = outer.get("result")
        return (inner if isinstance(inner, dict) else _first_json_object(str(inner or ""))), meta
    if isinstance(outer, dict):
        return outer, meta
    return _first_json_object(stdout or ""), meta


def _call(exe: str, prompt: str, stdin: str, timeout: float, runner) -> tuple[dict | None, dict]:
    """CLI를 한 번 실행. 실패는 AgentCliError. 옛 버전이 모르는 옵션이면 빼고 한 번 더(호출 수에는 1회로 셈)."""
    run = runner or _default_runner
    name = command_name(exe)
    base = [exe, "-p", _safe_prompt(prompt, exe), "--output-format", "json"]
    variants = [base + ["--tools", "", "--no-session-persistence"], base]
    with tempfile.TemporaryDirectory(prefix="lms-agent-ai-") as cwd:
        for i, args in enumerate(variants):
            try:
                cp = run(args, input=stdin, timeout=timeout, cwd=cwd)
            except subprocess.TimeoutExpired:
                raise AgentCliError(f"AI CLI({name})가 {timeout:g}초 안에 답하지 않았습니다") from None
            except OSError as e:
                raise AgentCliError(f"AI CLI({name})를 실행하지 못했습니다: {_scrub(str(e), exe)}") from None
            if cp.returncode != 0:
                err = _scrub((cp.stderr or "") + " " + (cp.stdout or ""), exe)
                if i == 0 and re.search(r"unknown option|unrecognized|invalid option", err, re.I):
                    continue
                raise AgentCliError(f"AI CLI({name})가 실패했습니다(종료 코드 {cp.returncode}): {err[:150]}")
            try:
                return _payload(cp.stdout or "")
            except AgentCliError as e:
                raise AgentCliError(_scrub(str(e), exe)) from None
    raise AgentCliError(f"AI CLI({name})가 실패했습니다")


# ---------------------------------------------------------------- 보낼 글: 가리기·예산

_MASK = "*#●○■□xX＊"
_REDACT = [
    ("이메일", re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")),
    ("링크", re.compile(r"https?://\S+|www\.\S+", re.I)),
    ("토큰", re.compile(r"(?i)bearer\s+\S+|(?<![\w\-])[A-Za-z0-9_\-~]{32,}(?![\w\-])")),
    ("전화", re.compile(r"(?<!\w)(?:\+?82[\s\-]?)?0\d{1,2}[\s\-.)]\s?\d{3,4}[\s\-.]\d{4}(?!\d)")),
    # 학번: 6~12자리 숫자(가린 자리 * # ● x 포함). 숫자 2개 이상.
    ("학번", re.compile(rf"(?<![\w{_MASK}])(?=(?:[^\d\s]*\d){{2}})\d[\d{_MASK}]{{5,11}}(?![\w{_MASK}])")),
]
_SIGN_EN = re.compile(r"(?im)^([ \t]*(?:best\s+regards|kind\s+regards|regards|sincerely|thanks|thank\s+you)[,.!]?)"
                      r"[ \t]*[^\n]{1,40}$")
_SIGN_KO = re.compile(r"(?m)\S{2,10}[ \t]*(?:교수|조교|교수님)?[ \t]*(?:드림|올림)[ \t]*$")


def redact(text: str, names: list | None = None) -> tuple[str, list[str]]:
    """보내기 전에 개인 정보처럼 보이는 것을 [이메일]·[전화]·[학번]·[링크]·[토큰]·[이름]으로 바꾼다. (글, 알림)."""
    counts: dict[str, int] = {}
    s = text or ""
    for label, rx in _REDACT:
        s, n = rx.subn(f"[{label}]", s)
        if n:
            counts[label] = counts.get(label, 0) + n
    for nm in names or []:
        nm = str(nm or "").strip()
        if len(nm) >= 2:
            s, n = re.subn(re.escape(nm), "[이름]", s)
            if n:
                counts["이름"] = counts.get("이름", 0) + n
    s, n1 = _SIGN_EN.subn(lambda m: m.group(1) + " [이름]", s)
    s, n2 = _SIGN_KO.subn("[이름] 드림", s)
    if n1 + n2:
        counts["서명"] = n1 + n2
    notes = ["보내기 전에 가림: " + "·".join(f"{k} {v}개" for k, v in counts.items())] if counts else []
    return s, notes


def cap_text(text: str, limit: int = MAX_TEXT, focus: str | None = None) -> tuple[str, bool]:
    """보낼 글을 limit자 이하로. 길면 focus 정규식이 처음 맞는 곳 근처를 고른다. (글, 잘랐는지)."""
    text = text or ""
    if len(text) <= limit:
        return text, False
    m = re.search(focus, text, re.I) if focus else None
    start = max(0, min(len(text) - limit, (m.start() - 1500) if m else 0))
    return text[start:start + limit], True


def _plain(inputs: dict) -> str:
    t = inputs.get("text") or ""
    return html_to_text(t) if inputs.get("is_html") else t


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _quoted(quote: str, *texts: str) -> bool:
    """근거 문장이 글에 그대로 있는지(공백 차이만 무시)."""
    q = _flat(quote)
    return len(q) >= 4 and any(q in _flat(t) for t in texts)


# ---------------------------------------------------------------- 스키마 검사(작은 JSON Schema 부분집합)

def check_schema(value, schema: dict, path: str = "$") -> list[str]:
    """type·required·properties·items·maxItems·minLength·maxLength·minimum·maximum·enum. 오류 문구 목록."""
    t = schema.get("type")
    ok = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
          "number": isinstance(value, (int, float)) and not isinstance(value, bool),
          "integer": isinstance(value, int) and not isinstance(value, bool),
          "boolean": isinstance(value, bool)}.get(t, True)
    if not ok:
        return [f"{path}: {t} 형식이어야 합니다 / must be {t}"]
    errs: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {schema['enum']} 중 하나여야 합니다 / must be one of {schema['enum']}")
    if t == "object":
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: 빠졌습니다 / missing")
        for k, sub in schema.get("properties", {}).items():
            if k in value and value[k] is not None:
                errs += check_schema(value[k], sub, f"{path}.{k}")
    elif t == "array":
        if len(value) > schema.get("maxItems", 10 ** 6):
            errs.append(f"{path}: 항목이 {schema['maxItems']}개를 넘습니다 / too many items")
        for i, v in enumerate(value[:50]):
            errs += check_schema(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if len(value.strip()) < schema.get("minLength", 0):
            errs.append(f"{path}: 비어 있거나 너무 짧습니다 / empty or too short")
        if len(value) > schema.get("maxLength", 10 ** 6):
            errs.append(f"{path}: {schema['maxLength']}자를 넘습니다 / longer than {schema['maxLength']}")
    elif t in ("number", "integer"):
        if not (schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf"))):
            errs.append(f"{path}: {schema.get('minimum')}~{schema.get('maximum')} 사이여야 합니다 / out of range")
    return errs


# ---------------------------------------------------------------- 작업 정의

@dataclass
class AiTask:
    name: str
    purpose: str
    instructions: str                                    # stdin 맨 앞에 붙는 지시(한국어+영어)
    schema: dict
    build_input: Callable[[dict], tuple[str, list]]      # inputs → (보낼 글, 알림). inputs에 준비물을 남겨도 된다
    post_validate: Callable[[dict, str, dict, bool], Any]  # (답, 보낸 글, inputs, 마지막 시도?) → data. 틀리면 _Invalid
    max_chars: int = MAX_TEXT
    max_attempts: int = 2
    timeout: float = 90.0


PROMPT = ("You are one data-extraction step inside the LMS Agent app. Follow the instructions at the top of "
          "standard input exactly and reply with exactly one JSON object and nothing else. Do not use any tools.")

_GRADING_FOCUS = (r"평가\s*(?:방법|기준|비율|요소|항목)|성적\s*(?:평가|산출|반영)|배점|비중|반영\s*비율|"
                  r"grading|evaluation|assessment|weight")
_DATE_FOCUS = r"\d{1,2}\s*[/.월]\s*\d{1,2}|due|deadline|마감|까지"
_DIST_FOCUS = r"학점\s*(?:비율|분포|부여)|상대\s*평가|절대\s*평가|grade\s*(?:distribution|scale|guideline)|curv|A\s*\+|A0"


def _text_builder(focus: str):
    def build(inputs: dict) -> tuple[str, list]:
        text, notes = redact(_plain(inputs), inputs.get("names"))
        text, cut = cap_text(text, MAX_TEXT, focus)
        if cut:
            notes.append(f"글이 길어 관련 부분 {MAX_TEXT:,}자만 보냄")
        title = _flat(str(inputs.get("title") or ""))[:120]
        if title:
            title, _ = redact(title, inputs.get("names"))
        head = f"제목 / title: {title}\n" if title else ""
        if inputs.get("posted_at"):
            head += f"게시 시각 / posted at: {inputs['posted_at']}\n"
        return head + text, notes
    return build


def _weights_validate(obj: dict, sent: str, inputs: dict, final: bool):
    comps = obj.get("components") or []
    if not comps:
        return None
    out: dict[str, dict] = {}
    for c in comps:
        name = _flat(str(c.get("name")))[:40]
        w = float(c["weight"])
        if w == 0:
            continue
        out.setdefault(name.lower(), {"name": name, "weight": 0.0})["weight"] += w
    total = round(sum(c["weight"] for c in out.values()), 2)
    if not out or abs(total - 100) > 1:
        raise _Invalid(f"비중 합이 {total:g}%입니다. 합이 100이 되는 평가 비중 목록만 답하세요 "
                       f"/ weights sum to {total:g}, they must sum to 100")
    evidence = _flat(str(obj.get("evidence") or ""))
    if not _quoted(evidence, sent):
        raise _Invalid("evidence는 입력 글에 있는 문장을 한 글자도 바꾸지 말고 그대로 복사해야 합니다 "
                       "/ evidence must be copied verbatim from the input text")
    merged: dict[str, dict] = {}
    for c, name in zip(out.values(), display_names([c["name"] for c in out.values()])):
        m = merged.setdefault(name.lower(), {"name": name, "weight": 0.0, "fixed": is_fixed(name)})
        m["weight"] = round(m["weight"] + c["weight"], 2)
    comps_out = list(merged.values())
    return WeightsFound(comps_out, total, evidence[:240], "medium", ["AI CLI로 찾음 — 맞는지 확인하세요"])


def parse_due(s: str) -> tuple[datetime | None, list[str]]:
    """AI가 준 마감 문자열 → 로컬 시간 datetime. 'YYYY-MM-DD'만 있으면 23:59로."""
    s = (s or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = datetime.fromisoformat(s)
            return d.replace(hour=23, minute=59, tzinfo=local_tz()), ["시간 없음 → 23:59로 둠"]
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None, []
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz()), []


def _as_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    return parse_iso(v) if isinstance(v, str) else None


def _deadline_validate(obj: dict, sent: str, inputs: dict, final: bool):
    items = obj.get("deadlines") or []
    ref = _as_dt(inputs.get("posted_at")) or now_local()
    out, dropped = [], []
    for it in items:
        title = _flat(str(it.get("title")))[:120]
        evidence = _flat(str(it.get("evidence")))
        due, notes = parse_due(str(it.get("due")))
        if not _quoted(evidence, sent):
            dropped.append(f"'{title}': evidence가 입력 글에 그대로 없습니다 / evidence not found verbatim in input")
        elif due is None:
            dropped.append(f"'{title}': due가 ISO 8601 날짜가 아닙니다 / due is not ISO 8601")
        elif not (ref - timedelta(days=60) <= due <= ref + timedelta(days=400)):
            dropped.append(f"'{title}': due가 게시일과 너무 멉니다 / due is implausibly far from the post date")
        else:
            out.append({"title": title, "due": iso(due), "evidence": evidence[:300], "by": "ai",
                        "notes": ["AI CLI로 찾음 — 근거 문장이 원문에 있는 것만"] + notes})
    if dropped and not final:
        raise _Invalid("; ".join(dropped[:4]))
    if items and not out:
        raise _Invalid("; ".join(dropped[:4]))
    return out


# ---- triage_review (계약 12장)

def _group_key(v: dict) -> str | None:
    """같은 과제 후보 묶음 — 화면(store.view의 group)과 같은 규칙 하나(store.group_key). 묶음이 없으면 None."""
    return group_key(v)


def _triage_build(inputs: dict) -> tuple[str, list]:
    """후보마다 {id, 과목, 제목, 마감, 근거 원문, notes, conflict}만, 가려서. 검사에 쓸 준비물을 inputs["_prep"]에 남긴다."""
    names = inputs.get("names")
    items = list(inputs.get("items") or [])[:TRIAGE_MAX_ITEMS]
    groups: dict = {}
    for v in inputs.get("all_items") or items:
        k = _group_key(v)
        if k is not None:
            groups.setdefault(k, []).append(v["id"])
    prep, parts, notes = {}, [], []
    if len(inputs.get("items") or []) > TRIAGE_MAX_ITEMS:
        notes.append(f"후보가 많아 {TRIAGE_MAX_ITEMS}개만 보냄")
    redacted_any = 0
    budget = MAX_TEXT
    for v in items:
        ev = []
        for e in (v.get("evidence") or [])[:4]:
            t, n = redact(str(e.get("text") or ""), names)
            redacted_any += bool(n)
            ev.append({"text": t[:600], "type": e.get("source_type"), "posted_at": e.get("posted_at")})
        title, n = redact(str(v.get("title") or ""), names)
        redacted_any += bool(n)
        gid = _group_key(v)
        group = [x for x in groups.get(gid, []) if x != v["id"]] if gid is not None else []
        lines = [f"### id: {v['id']}",
                 f"과목/course: {v.get('course_short') or ''} | 제목/title: {title} | 마감/due: {v.get('due') or '없음/none'}"]
        if v.get("conflict"):
            lines.append(f"충돌/conflict: {redact(str(v['conflict']), names)[0]}")
        if group:
            lines.append("같은 과제의 다른 후보(이 중 최대 하나만 approved) / same task, approve at most one: " + ", ".join(group))
        if v.get("notes"):
            lines.append("추정/notes: " + "; ".join(redact(str(x), names)[0] for x in v["notes"][:5]))
        for i, e in enumerate(ev, 1):
            lines.append(f"근거/evidence {i} ({e['type'] or ''}, {str(e['posted_at'] or '')[:10]}): {e['text']}")
        block = "\n".join(lines)
        if len(block) > budget:
            notes.append("글자 수 예산을 넘어 일부 후보는 다음에 보냄")
            break
        budget -= len(block)
        parts.append(block)
        prep[v["id"]] = {"evidence": ev, "due": v.get("due"), "group": gid, "item": v}
    if redacted_any:
        notes.append("보내기 전에 이메일·학번·이름 등을 가림")
    inputs["_prep"] = prep
    inputs["_groups"] = groups
    return "\n\n".join(parts), notes


_CLAIMS_EXPLICIT = re.compile(r"(?i)분명|명시|적혀\s*있|explicit|stated")
_NEG_AFTER = re.compile(r"^[^.,;]{0,6}?(?:지\s*않|않|없|아니)")
_NEG_BEFORE = re.compile(r"(?i)(?:\bnot|n't|never)\s+(?:\w+\s+)?$")
_SOURCE_DAY = {"announcement": "공지일", "inbox": "메시지 날짜", "board": "게시일"}


def claims_explicit(reason: str) -> bool:
    """이유가 '날짜가 분명히/명시적으로 적혀 있다'고 주장하는지('분명하지 않아'·'not stated' 같은 부정은 빼고)."""
    for m in _CLAIMS_EXPLICIT.finditer(reason or ""):
        if _NEG_AFTER.match(reason[m.end():m.end() + 10]) or _NEG_BEFORE.search(reason[max(0, m.start() - 16):m.start()]):
            continue
        return True
    return False


def date_basis(evidence: list[dict], due: datetime | None) -> dict | None:
    """마감 날짜가 근거의 어떤 날짜 표현에서 왔는지. 근거에 적힌 날짜(±1일)가 있으면 {"relative": False},
    상대 날짜('다음 주 금요일', 'next Friday')만 맞으면 {"relative": True, "phrase", "posted_at", "source_type"}, 없으면 None."""
    if due is None:
        return None
    rel = None
    for e in evidence or []:
        text = str(e.get("text") or "")
        posted = _as_dt(e.get("posted_at"))
        for h in find_dates(text, (posted or due).date()):
            if abs((h.day - due.date()).days) > 1:
                continue
            if not h.relative:
                return {"relative": False}
            if rel is None:
                rel = {"relative": True, "phrase": _flat(text[h.start:h.end]), "posted_at": iso(posted),
                       "source_type": e.get("source_type") or e.get("type")}
    return rel


def relative_note(basis: dict, due: datetime) -> str:
    """‘다음 주 금요일’ → 공지일 11/12(목) 기준 11/20(금)"""
    target = f"{due.month}/{due.day}({KO_WD[due.weekday()]})"
    posted = _as_dt(basis.get("posted_at"))
    if posted is None:
        return f"‘{basis['phrase']}’ → {target}로 계산"
    label = _SOURCE_DAY.get(basis.get("source_type") or "", "게시일")
    return f"‘{basis['phrase']}’ → {label} {posted.month}/{posted.day}({KO_WD[posted.weekday()]}) 기준 {target}"


_PLACEHOLDER = re.compile(r"\[(이메일|링크|토큰|전화|학번|이름)\]")


def _label_rx(label: str, edge: bool) -> str:
    if label == "이름":
        return r"\S+" if edge else r".+?"
    rx = dict(_REDACT)[label]
    p = rx.pattern[4:] if rx.pattern.startswith("(?i)") else rx.pattern
    return ("(?i:" if rx.pattern.startswith("(?i)") or rx.flags & re.I else "(?:") + p + ")"


def unplace(s: str) -> str:
    """'[이메일]' 같은 자리 표시에서 괄호를 뗀다(화면에 자리 표시가 나오지 않게)."""
    return _PLACEHOLDER.sub(lambda m: m.group(1), s or "")


def original_quote(quote: str, evidence: list[dict], names: list | None = None) -> str:
    """AI가 가린 글에서 인용한 문장 → 이 PC에 있는 원문(가리지 않은) 문장. 계약 13-3.
    가린 근거 안에서 인용을 찾고, 자리 표시가 아닌 조각들을 순서대로 원문에 맞춘다. 조각 맞추기가 안 되면
    그 근거 원문 문장 전체, 어느 근거에도 없으면(드묾) 자리 표시 괄호만 뗀 인용."""
    q = _flat(quote)
    for e in (evidence or [])[:4]:
        orig = _flat(str(e.get("text") or ""))
        red = _flat(redact(str(e.get("text") or ""), names)[0][:600])
        if not q or q not in red:
            continue
        if not _PLACEHOLDER.search(q):
            if q in orig:
                return q
        parts = _PLACEHOLDER.split(q)
        rx = ""
        for i, part in enumerate(parts):
            if i % 2 == 0:
                rx += re.escape(part).replace(r"\ ", r"\s*")
            else:
                rx += _label_rx(part, edge=(i == 1 and not parts[0]) or (i == len(parts) - 2 and not parts[-1]))
        try:
            m = re.search(rx, orig)
        except re.error:
            m = None
        return m.group(0) if m and m.group(0).strip() else orig
    return unplace(q)


def _due_supported(due: datetime, ev: list[dict]) -> bool:
    """마감 날짜가 근거 원문에 적힌 날짜와 ±1일 안에 있는지."""
    for e in ev:
        ref = _as_dt(e.get("posted_at")) or due
        for h in find_dates(e["text"], ref.date()):
            if abs((h.day - due.date()).days) <= 1:
                return True
    return False


def _triage_validate(obj: dict, sent: str, inputs: dict, final: bool):
    prep: dict = inputs.get("_prep") or {}
    groups: dict = inputs.get("_groups") or {}
    approved_before = {k: [i for i in ids if i not in prep and _approved(inputs, i)] for k, ids in groups.items()}
    errors, soft, out, seen = [], [], [], set()
    for d in obj.get("decisions") or []:
        iid = str(d.get("id"))
        p = prep.get(iid)
        if p is None:
            errors.append({"id": iid, "why": "확인할 후보 목록에 없는 id / unknown id"})
            continue
        if iid in seen:
            errors.append({"id": iid, "why": "같은 id에 결정이 둘 / duplicate decision"})
            continue
        seen.add(iid)
        decision = d.get("decision")
        reason = _flat(str(d.get("reason") or ""))
        evidence = _flat(str(d.get("evidence") or ""))
        if len(reason) > 120 or not reason:
            errors.append({"id": iid, "why": "reason은 1~120자 한 줄 / reason must be one line, at most 120 chars"})
            continue
        if not _quoted(evidence, *[e["text"] for e in p["evidence"]]):
            errors.append({"id": iid, "why": "evidence가 그 후보의 근거 원문에 그대로 없음 / evidence not verbatim in this item's evidence"})
            continue
        changes = {}
        if decision in ("approved", "rejected"):
            changes["decision"] = decision
        if d.get("title") and decision != "rejected":
            t = unplace(_flat(str(d["title"])))[:120]   # 가린 자리 표시가 제목으로 들어가지 않게
            if t:
                changes["title"] = t
        if d.get("due") and decision != "rejected":
            due, _ = parse_due(str(d["due"]))
            if due is None:
                soft.append({"id": iid, "why": "마감 변경 무시: 날짜 형식이 아님 / due not ISO 8601"})
            elif not _due_supported(due, p["evidence"]):
                soft.append({"id": iid, "why": "마감 변경 무시: 근거에 적힌 날짜와 ±1일 넘게 다름 / due not within ±1 day of the evidence"})
            elif (_as_dt(p["due"]) or None) != due:
                changes["due"] = due.strftime("%Y-%m-%dT%H:%M")
        basis = None
        if changes.get("decision") == "approved":
            due_dt = parse_due(changes["due"])[0] if changes.get("due") else _as_dt(p["due"])
            basis = date_basis(p["evidence"], due_dt)
            if basis and basis["relative"] and claims_explicit(reason):
                errors.append({"id": iid, "why": f"근거는 상대 날짜('{basis['phrase']}')인데 이유가 날짜가 분명히 적혀 있다고 함 — "
                                                 "계산한 날짜라고 밝혀야 함 / the evidence only has a relative date; "
                                                 "do not claim the date is explicitly stated"})
                continue
        out.append({"id": iid, "decision": decision, "changes": changes, "reason": reason, "evidence": evidence[:300],
                     "group": p["group"], "date_basis": basis if basis and basis["relative"] else None})
    # 충돌 묶음: 이미 승인된 것 + 이번 승인 ≤ 1
    by_group: dict = {}
    for o in out:
        if o["changes"].get("decision") == "approved" and o["group"] is not None:
            by_group.setdefault(o["group"], []).append(o)
    bad_ids = set()
    for g, lst in by_group.items():
        if len(lst) + len(approved_before.get(g, [])) > 1:
            for o in lst:
                bad_ids.add(o["id"])
                errors.append({"id": o["id"], "why": "같은 과제 후보를 둘 이상 승인함 — 하나만 / approved more than one item of the same task"})
    out = [o for o in out if o["id"] not in bad_ids]
    if errors and not final:
        raise _Invalid("; ".join(f"{e['id']}: {e['why']}" for e in errors[:6]))
    return {"decisions": out, "skipped": errors + soft}


def _approved(inputs: dict, item_id: str) -> bool:
    for v in inputs.get("all_items") or []:
        if v["id"] == item_id:
            return v.get("decision") == "approved"
    return False


# ---- map_score_item·count_items (계약 14-2·14-4)

MAP_MAX_THINGS = 20


def _norm_name(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _map_build(inputs: dict) -> tuple[str, list]:
    """평가 항목 이름 목록 + 연결할 것마다 {key, 라벨, 출처, 글}(가려서). 검사 준비물은 inputs["_prep"]."""
    names = inputs.get("names")
    comps = [str(c) for c in inputs.get("components") or [] if str(c).strip()]
    prep, parts, notes, budget, red = {}, [], [], MAX_TEXT, 0
    for t in list(inputs.get("things") or [])[:MAP_MAX_THINGS]:
        label, n1 = redact(_flat(str(t.get("label") or ""))[:120], names)
        title, n2 = redact(_flat(str(t.get("title") or ""))[:160], names)
        text, n3 = redact(_flat(str(t.get("text") or ""))[:600], names)
        red += bool(n1 or n2 or n3)
        block = f"### key: {t['key']}\n라벨/label: {label}\n출처/source: {title}\n글/text: {text}"
        if len(block) > budget:
            notes.append("글자 수 예산을 넘어 일부는 다음에 보냄")
            break
        budget -= len(block)
        parts.append(block)
        prep[str(t["key"])] = "\n".join((label, title, text))
    if red:
        notes.append("보내기 전에 이메일·학번·이름 등을 가림")
    inputs["_prep"] = prep
    inputs["_names"] = {_norm_name(c): c for c in comps}
    head = "평가 항목 / grading components: " + " | ".join(comps)
    return head + "\n\n" + "\n\n".join(parts), notes


def _map_validate(obj: dict, sent: str, inputs: dict, final: bool):
    prep, names = inputs.get("_prep") or {}, inputs.get("_names") or {}
    out, errors, seen = [], [], set()
    for d in obj.get("links") or []:
        key = str(d.get("key"))
        if key not in prep:
            errors.append(f"{key}: 목록에 없는 key / unknown key")
            continue
        if key in seen:
            continue
        seen.add(key)
        comp = d.get("component")
        if comp is None or (isinstance(comp, str) and not comp.strip()):
            out.append({"key": key, "component": None, "evidence": None})
            continue
        name = names.get(_norm_name(comp))
        if name is None:
            errors.append(f"{key}: component는 평가 항목 목록의 이름 그대로여야 합니다 / component must be one of the listed names")
            continue
        ev = _flat(str(d.get("evidence") or ""))
        if not _quoted(ev, prep[key]):
            errors.append(f"{key}: evidence가 그 항목의 글에 그대로 없음 / evidence not verbatim in that item's text")
            continue
        out.append({"key": key, "component": name, "evidence": unplace(ev)[:300]})
    if errors and not final:
        raise _Invalid("; ".join(errors[:6]))
    return {"links": out, "skipped": errors}


def _count_build(inputs: dict) -> tuple[str, list]:
    lines = [redact(_flat(str(x)), inputs.get("names"))[0][:300] for x in inputs.get("lines") or []]
    text, cut = cap_text("\n".join(lines), MAX_TEXT)
    notes = [f"글이 길어 {MAX_TEXT:,}자만 보냄"] if cut else []
    low = inputs.get("low")
    head = (f"평가 항목 / component: {_flat(str(inputs.get('component') or ''))[:60]} "
            f"(계열 / kind: {inputs.get('family') or ''})\n"
            + (f"이미 본 번호로 최소 {low}개 / at least {low} already seen\n" if low else ""))
    return head + text, notes


def _count_validate(obj: dict, sent: str, inputs: dict, final: bool):
    n = obj.get("count")
    if n is None:
        return {"count": None, "evidence": None}
    low = inputs.get("low") or 1
    if n < low:
        raise _Invalid(f"count {n}는 이미 본 최소 개수 {low}보다 작습니다 / count is below the {low} already seen")
    ev = _flat(str(obj.get("evidence") or ""))
    if not _quoted(ev, sent):
        raise _Invalid("evidence는 입력 글에서 한 글자도 바꾸지 말고 그대로 복사해야 합니다 / evidence must be verbatim")
    return {"count": int(n), "evidence": unplace(ev)[:300]}


# ---- grade_distribution (계약 16장)

_GRADE_IN = re.compile(r"(?<![A-Za-z0-9])[A-DF](?:\+|0)?(?![A-Za-z0-9])")


def _dist_validate(obj: dict, sent: str, inputs: dict, final: bool):
    """AI가 준 학점 묶음·기준표를 규칙과 같은 방법(distribution.from_groups)으로 풀어 검사한다:
    근거는 보낸 글에 그대로 있고 학점 낱말이 있어야 하며, 묶음은 이어진 학점·겹치지 않음·pct가 [low, high] 안,
    합은 100(범위 안에서 맞출 수 있으면 통과), 기준표는 학점 순서대로 내려가야 한다."""
    kind = obj.get("kind")
    if kind in (None, "none"):
        return None
    evidence = _flat(str(obj.get("evidence") or ""))
    if not _quoted(evidence, sent):
        raise _Invalid("evidence는 입력 글에 있는 문장을 한 글자도 바꾸지 말고 그대로 복사해야 합니다 "
                       "/ evidence must be copied verbatim from the input text")
    if not _GRADE_IN.search(evidence):
        raise _Invalid("evidence에 학점(A+, B0 등)이 없습니다 / evidence must contain the letter grades")
    try:
        if kind == "absolute":
            cut = {c["grade"]: float(c["min_score"]) for c in obj.get("cutoffs") or []}
            df = _dist.from_groups("absolute", cutoffs=cut, sentence=unplace(evidence))
        else:
            df = _dist.from_groups("relative", groups=obj.get("groups") or [], sentence=unplace(evidence))
    except ValueError as e:
        raise _Invalid(str(e)) from None
    df.confidence = "medium"
    df.notes = list(df.notes) + ["AI CLI로 찾음 — 맞는지 확인하세요"]
    return df


TASKS: dict[str, AiTask] = {}


def register(task: AiTask) -> AiTask:
    TASKS[task.name] = task
    return task


register(AiTask(
    name="grading_weights",
    purpose="강의계획서·공지에서 평가 항목별 비중(%)을 찾는다(규칙으로 못 찾았을 때)",
    instructions=(
        "작업: 아래 강의 글에서 성적 평가 항목과 비중(%)만 뽑으세요. 글에 없는 것은 지어내지 마세요.\n"
        "Task: extract ONLY the grading components and their weights (percent) from the course text below. "
        "Never invent anything that is not in the text.\n"
        "- 학점 분포(A+ 15% 등), 출석률 기준(80% 이상), 감점 비율, '상위 30%'는 비중이 아닙니다 "
        "/ grade distributions, attendance thresholds, late penalties and percentiles are NOT weights.\n"
        "- 비중이 바뀌었다는 글이면 바뀐 최신 목록을 답하세요 / if the text updates a policy, return the updated list.\n"
        "- 이름은 글에 적힌 그대로(예: 'Midterm project', '중간고사') / keep component names as written.\n"
        "- evidence: 근거 문장을 입력 글에서 한 글자도 바꾸지 말고 그대로 복사 / copy the evidence verbatim from the input.\n"
        "- 비중이 없으면 {\"components\": [], \"evidence\": \"\"} / if there are no weights, return empty components.\n"
        "- 다른 정보(이름, 학번, 연락처, 점수)는 넣지 마세요 / include no other data."),
    schema={"type": "object", "required": ["components"], "properties": {
        "components": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "required": ["name", "weight"], "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 60},
                "weight": {"type": "number", "minimum": 0, "maximum": 100}}}},
        "evidence": {"type": "string", "maxLength": 600}}},
    build_input=_text_builder(_GRADING_FOCUS),
    post_validate=_weights_validate,
))

register(AiTask(
    name="deadline_from_text",
    purpose="규칙으로 마감을 못 찾았지만 날짜처럼 보이는 글에서 제출 마감을 찾는다",
    instructions=(
        "작업: 아래 강의 글에서 과제·보고서·시험의 제출 마감(또는 시험 시각)만 뽑으세요. 글에 없는 것은 지어내지 마세요.\n"
        "Task: extract ONLY submission deadlines (or exam times) from the course text below. Never invent anything.\n"
        "- due: ISO 8601 날짜·시각(예: 2026-11-18T23:59:00+09:00). 연도가 없으면 게시 시각 기준으로 가장 가까운 앞날 "
        "/ due must be ISO 8601; if the year is missing, use the nearest future date after the post time.\n"
        "- 시간이 없으면 23:59 / if no time is given use 23:59. '자정'·'midnight'은 그날 23:59.\n"
        "- evidence: 마감이 적힌 문장을 입력 글에서 한 글자도 바꾸지 말고 그대로 복사 / copy the evidence verbatim.\n"
        "- 휴강·보강·수업 일정·지난 날짜 안내는 마감이 아닙니다 / class cancellations and schedules are not deadlines.\n"
        "- 마감이 없으면 {\"deadlines\": []} / if none, return an empty list. 다른 정보는 넣지 마세요."),
    schema={"type": "object", "required": ["deadlines"], "properties": {
        "deadlines": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "required": ["title", "due", "evidence"], "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "due": {"type": "string", "minLength": 10, "maxLength": 40},
                "evidence": {"type": "string", "minLength": 4, "maxLength": 400}}}}}},
    build_input=_text_builder(_DATE_FOCUS),
    post_validate=_deadline_validate,
))

register(AiTask(
    name="triage_review",
    purpose="확인함의 미결정 후보를 승인·제외·보류하고 필요하면 제목·마감을 고친다(근거 원문으로 검증된 것만 반영)",
    instructions=(
        "작업: 아래는 학생의 확인함에 있는 마감 후보들입니다. 후보마다 approved(일정에 넣기) / rejected(마감 아님·잘못 찾음) / "
        "keep(판단 어려움) 중 하나를 고르세요.\n"
        "Task: below are deadline candidates waiting in a student's review inbox. For each, decide approved "
        "(put on the calendar), rejected (not a real deadline / wrong) or keep (unsure).\n"
        "- 근거 원문에 마감이 분명히 적혀 있을 때만 approved. 애매하면 keep / approve only when the evidence clearly states it.\n"
        "- '다음 주 금요일'·'next Friday' 같은 상대 날짜는 게시 시각 기준으로 계산한 것이므로, reason에 계산했다고 밝히고 "
        "'분명히 적혀 있다'·'명시'라고 쓰지 마세요 / for relative dates say you computed it from the post date; "
        "never claim the date is explicitly stated.\n"
        "- 같은 과제의 후보가 여럿이면 최대 하나만 approved — 보통 가장 최근 글(연장 공지 등) / approve at most one per task, "
        "usually the most recent notice (e.g. an extension).\n"
        "- due를 고칠 때는 근거 원문에 적힌 날짜 그대로(ISO 8601) / only change due to a date written in the evidence.\n"
        "- evidence: 그 후보의 근거 원문에서 한 글자도 바꾸지 말고 그대로 복사 / copy evidence verbatim from that item's evidence.\n"
        "- reason: 한국어 한 줄, 120자 이내 / one Korean line, at most 120 characters.\n"
        "- 목록에 없는 id를 만들지 마세요 / never invent ids."),
    schema={"type": "object", "required": ["decisions"], "properties": {
        "decisions": {"type": "array", "maxItems": TRIAGE_MAX_ITEMS, "items": {
            "type": "object", "required": ["id", "decision", "reason", "evidence"], "properties": {
                "id": {"type": "string", "minLength": 1, "maxLength": 80},
                "decision": {"type": "string", "enum": ["approved", "rejected", "keep"]},
                "due": {"type": "string", "maxLength": 40},
                "title": {"type": "string", "maxLength": 200},
                "reason": {"type": "string", "maxLength": 400},
                "evidence": {"type": "string", "maxLength": 800}}}}}},
    build_input=_triage_build,
    post_validate=_triage_validate,
))

register(AiTask(
    name="map_score_item",
    purpose="규칙으로 못 정한 점수·마감이 어느 평가 항목(강의계획서의 비중 항목)에 해당하는지 고른다",
    instructions=(
        "작업: 아래 '연결할 것'마다 평가 항목 목록에서 해당하는 항목 이름을 하나 고르세요. 모르면 component를 null로.\n"
        "Task: for each item below, pick the ONE grading component from the list that it belongs to, or null if unsure.\n"
        "- component는 목록의 이름을 글자 그대로 / component must be copied exactly from the list.\n"
        "- 예: 'Lab0' 점수표는 실습 과제라 'Homework assignments'(w/ short reports)일 수 있음 / e.g. lab scores often count "
        "as homework assignments.\n"
        "- evidence: 그 항목의 라벨·출처·글에서 판단 근거를 한 글자도 바꾸지 말고 그대로 복사 / copy the evidence verbatim "
        "from that item's label, source or text.\n"
        "- 목록에 없는 key를 만들지 마세요 / never invent keys. 다른 정보는 넣지 마세요."),
    schema={"type": "object", "required": ["links"], "properties": {
        "links": {"type": "array", "maxItems": MAP_MAX_THINGS, "items": {
            "type": "object", "required": ["key", "component"], "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 300},
                "component": {"type": "string", "maxLength": 80},
                "evidence": {"type": "string", "maxLength": 400}}}}}},
    build_input=_map_build,
    post_validate=_map_validate,
))

register(AiTask(
    name="count_items",
    purpose="규칙으로 명시·단서를 못 찾았을 때 실습·과제 같은 세부 항목이 학기에 모두 몇 개인지 찾는다",
    instructions=(
        "작업: 아래 강의 글 줄들에서 이 평가 항목(예: 실습 Lab)이 학기 전체에 모두 몇 개인지 찾으세요. "
        "글에 근거가 없으면 count를 null로. 추측하지 마세요.\n"
        "Task: from the course text lines below, find how many of these items (e.g. labs) there are in the whole term. "
        "If the text does not say, return null. Do not guess.\n"
        "- 'Lab 0~6'은 7개, 'Next labs: A → B → C'는 지금 번호 뒤에 3개가 더 있다는 뜻 / 'Lab 0~6' means 7; "
        "'Next labs: A → B → C' means three more after the current one.\n"
        "- evidence: 근거 줄을 입력 글에서 한 글자도 바꾸지 말고 그대로 복사 / copy the evidence line verbatim.\n"
        "- 다른 정보는 넣지 마세요 / include no other data."),
    schema={"type": "object", "required": ["count"], "properties": {
        "count": {"type": "integer", "minimum": 1, "maximum": 30},
        "evidence": {"type": "string", "maxLength": 400}}},
    build_input=_count_build,
    post_validate=_count_validate,
))


register(AiTask(
    name="grade_distribution",
    purpose="규칙으로 못 읽은 강의 글에서 학점 비율(상대평가) 또는 점수 기준(절대평가)을 찾는다",
    instructions=(
        "작업: 아래 강의 글에서 학점 비율 / grade distribution을 찾으세요. 글에 없는 것은 지어내지 마세요.\n"
        "Task: find the letter-grade policy in the course text below: either a relative distribution (share of "
        "students per grade) or absolute score cutoffs. Never invent anything that is not in the text.\n"
        "- 상대평가(relative): groups = [{grades: [\"A+\",\"A0\"], pct 또는 low·high, cumulative}] — 학점 이름은 "
        "A+ A0 B+ B0 C+ C0 D+ D0 F만. 'A'는 A+와 A0, 'B 이상 70%'는 cumulative true, '40~50%'는 low 40·high 50, "
        "'30% 이내'는 high 30 / grade names must be from A+ A0 B+ B0 C+ C0 D+ D0 F; 'A' means A+ and A0; "
        "'B or above 70%' is cumulative; ranges go to low/high; caps go to high.\n"
        "- 절대평가(absolute): cutoffs = [{grade, min_score}] (100점 기준 최소 점수) / minimum total score out of 100.\n"
        "- 평가 비중(중간고사 30%), 출석 기준(66% 이상 출석), 점수 통계는 학점 비율이 아닙니다 / grading weights, "
        "attendance thresholds and score statistics are NOT a grade distribution.\n"
        "- P/NP(Pass/Fail)만 있거나 학점 비율이 없으면 {\"kind\": \"none\"} / if there is none, kind none.\n"
        "- evidence: 근거 문장을 입력 글에서 한 글자도 바꾸지 말고 그대로 복사 / copy the evidence verbatim.\n"
        "- 다른 정보(이름, 학번, 연락처, 점수)는 넣지 마세요 / include no other data."),
    schema={"type": "object", "required": ["kind"], "properties": {
        "kind": {"type": "string", "enum": ["relative", "absolute", "none"]},
        "groups": {"type": "array", "maxItems": 9, "items": {
            "type": "object", "required": ["grades"], "properties": {
                "grades": {"type": "array", "maxItems": 9, "items": {"type": "string", "enum": list(GRADES)}},
                "pct": {"type": "number", "minimum": 0, "maximum": 100},
                "low": {"type": "number", "minimum": 0, "maximum": 100},
                "high": {"type": "number", "minimum": 0, "maximum": 100},
                "cumulative": {"type": "boolean"}}}},
        "cutoffs": {"type": "array", "maxItems": 9, "items": {
            "type": "object", "required": ["grade", "min_score"], "properties": {
                "grade": {"type": "string", "enum": list(GRADES)},
                "min_score": {"type": "number", "minimum": 0, "maximum": 100}}}},
        "evidence": {"type": "string", "maxLength": 600}}},
    build_input=_text_builder(_DIST_FOCUS),
    post_validate=_dist_validate,
))


# ---------------------------------------------------------------- 실행

@dataclass
class AiResult:
    ok: bool
    task: str
    data: Any = None
    error: str | None = None
    attempts: int = 0          # 실제로 CLI를 부른 횟수
    duration_ms: int = 0
    input_chars: int = 0
    cli: str | None = None     # 명령 이름만(예: "claude")
    model: str | None = None   # CLI가 알려 주면
    notes: list = field(default_factory=list)


def run_task(name: str, inputs: dict, runner=None, command: str | None = None, store=None,
             max_attempts: int | None = None) -> AiResult:
    """작업 하나를 실행한다. 예외를 던지지 않는다. store를 주면 실행 기록(내용 없음)을 남긴다."""
    task = TASKS.get(name)
    started = time.monotonic()
    if task is None:
        return AiResult(False, name, error=f"모르는 AI 작업: {name}")
    if max_attempts is not None:
        task = replace(task, max_attempts=max(1, min(max_attempts, task.max_attempts)))
    exe = command or detect()
    res = AiResult(False, name, cli=command_name(exe))
    if not exe:
        res.error = f"AI CLI({DEFAULT_COMMAND})를 찾지 못했습니다. 설치하거나 {ENV_VAR}에 경로를 넣으세요"
        _log(store, res)
        return res
    inputs = dict(inputs)
    try:
        sent, notes = task.build_input(inputs)
    except Exception as e:  # noqa: BLE001 — 입력 만들기 실패도 결과로 돌려준다
        res.error = f"보낼 글을 만들지 못했습니다: {type(e).__name__}"
        _log(store, res)
        return res
    res.notes, res.input_chars = list(notes), len(sent)
    correction = ""
    for attempt in range(1, task.max_attempts + 1):
        final = attempt == task.max_attempts
        stdin = (task.instructions + "\n\n응답 JSON 스키마 / response JSON schema:\n"
                 + json.dumps(task.schema, ensure_ascii=False) + "\n\n" + correction
                 + "=== 입력 글 시작 / INPUT START ===\n" + sent + "\n=== 입력 글 끝 / INPUT END ===\n")
        res.attempts = attempt
        try:
            obj, meta = _call(exe, PROMPT, stdin, task.timeout, runner)
            res.model = meta.get("model") or res.model
            if obj is None:
                raise _Invalid("답에서 JSON 객체를 찾지 못했습니다 / no JSON object found in the reply")
            errs = check_schema(obj, task.schema)
            if errs:
                raise _Invalid("; ".join(errs[:4]))
            res.data = task.post_validate(obj, sent, inputs, final)
            res.ok, res.error = True, None
            break
        except AgentCliError as e:
            res.error = str(e)
            break  # CLI 자체 문제는 다시 물어도 같다
        except _Invalid as e:
            res.error = f"AI 답이 검사를 통과하지 못했습니다: {str(e)[:200]}"
            correction = ("!!! 이전 답이 검사를 통과하지 못했습니다 / your previous answer failed validation: "
                          + str(e)[:400] + "\n같은 스키마의 JSON 객체 하나만 다시 답하세요 / reply again with exactly one "
                          "JSON object matching the schema.\n\n")
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            res.error = f"AI 답이 검사를 통과하지 못했습니다: {type(e).__name__}"
            correction = ("!!! 이전 답의 형식이 틀렸습니다 / your previous answer had the wrong shape. "
                          "스키마를 정확히 지키세요 / follow the schema exactly.\n\n")
    res.duration_ms = int((time.monotonic() - started) * 1000)
    _log(store, res)
    return res


def _log(store, res: AiResult) -> None:
    """실행 기록(최근 RUN_LOG_KEEP개): 내용(지시·글·답)은 남기지 않는다."""
    if store is None:
        return
    runs = store.data.setdefault("ai_runs", [])
    runs.append({"at": iso(now_local()), "task": res.task, "ok": res.ok, "attempts": res.attempts,
                 "duration_ms": res.duration_ms, "input_chars": res.input_chars, "cli": res.cli,
                 "model": res.model, "error": res.error})
    del runs[:-RUN_LOG_KEEP]


def calls_today(store) -> int:
    today = now_local().date().isoformat()
    return sum(int(r.get("attempts") or 0) for r in (store.data.get("ai_runs") or [])
               if str(r.get("at") or "").startswith(today))


def recent_runs(store, n: int = 5) -> list[dict]:
    return [dict(r) for r in reversed((store.data.get("ai_runs") or [])[-n:])]


class AiSession:
    """동기화 한 번 동안 쓰는 AI 호출 창구: settings.ai_cli가 켜져 있을 때만, 동기화당·하루 호출 한도 안에서.
    동기화 안의 모든 AI 작업(평가 비중·마감·확인함 정리)이 같은 세션을 나눠 쓰면 한도가 함께 적용된다."""

    def __init__(self, store, runner=None, command: str | None = None, enabled: bool | None = None,
                 max_calls: int = MAX_CALLS_PER_SYNC, max_per_day: int = MAX_CALLS_PER_DAY):
        self.store, self.runner, self.command = store, runner, command
        self.max_calls, self.max_per_day = max_calls, max_per_day
        self.enabled = bool((store.data.get("settings") or {}).get("ai_cli")) if enabled is None else bool(enabled)
        self.calls = 0

    def blocked(self) -> str | None:
        """지금 부를 수 없으면 그 이유(한국어)."""
        if not self.enabled:
            return "AI CLI 사용이 꺼져 있습니다(설정에서 켤 수 있음)"
        if self.calls >= self.max_calls:
            return f"이번 동기화의 AI 호출 한도({self.max_calls}회)를 다 썼습니다"
        if calls_today(self.store) >= self.max_per_day:
            return f"오늘 AI 호출 한도({self.max_per_day}회)를 다 썼습니다"
        return None

    def run(self, name: str, inputs: dict) -> AiResult:
        why = self.blocked()
        if why:
            return AiResult(False, name, error=why)
        left = min(self.max_calls - self.calls, self.max_per_day - calls_today(self.store))
        res = run_task(name, inputs, runner=self.runner, command=self.command, store=self.store, max_attempts=left)
        self.calls += res.attempts
        return res


# ---------------------------------------------------------------- 확인함 정리(계약 12장)

def review_items(store) -> tuple[list[dict], list[dict]]:
    """AI가 다룰 수 있는 후보(확인 대기·미결정·사용자가 손대지 않음)와 전체 항목."""
    view = store.view()
    todo = [v for v in view if v["status"] == "review" and v["decision"] == "pending"
            and v.get("decided_by") != "user" and not v.get("edited")]
    return todo, view


def auto_triage(store, runner=None, session: AiSession | None = None, names: list | None = None,
                command: str | None = None) -> dict:
    """확인함 후보를 AI로 정리해 검증된 결정만 store.update_user(..., by="ai", reason=…)로 반영한다.
    settings.ai_cli가 꺼져 있으면 아무것도 하지 않는다. 결과는 store.data["ai_triage"]에도 남는다(state.ai.last_triage).
    리더: 동기화 뒤 settings.ai_auto_triage가 켜져 있으면 `agent_cli.auto_triage(store, session=같은 세션)` 후 저장.
    반환: {"at", "ok", "applied": n, "skipped": [{"id","why"}], "error", "attempts",
           "pending_left": 아직 확인 대기 수, "user_reverted": 사용자가 되돌려 AI가 건드리지 않은 수}(계약 13-3)"""
    session = session or AiSession(store, runner=runner, command=command)
    out = {"at": iso(now_local()), "ok": False, "applied": 0, "skipped": [], "error": None, "attempts": 0}

    def finish() -> dict:
        out.update(queue_counts(store))
        out["skipped"] = [{**s, "why": unplace(str(s.get("why") or ""))} for s in out["skipped"]][:50]
        store.data["ai_triage"] = out
        return out

    todo, view = review_items(store)
    if not todo:
        out.update(ok=True, error=None)
        out["note"] = "확인할 후보가 없습니다"
        return finish()
    why = session.blocked()
    if why:
        out["error"] = why
        return finish()
    from .core import short_course_name  # 순환 import 피하기
    items = [{**v, "course_short": v.get("course_short") or short_course_name(v.get("course_name"))} for v in todo]
    res = session.run("triage_review", {"items": items, "all_items": view, "names": names})
    out["attempts"] = res.attempts
    if not res.ok:
        out["error"] = res.error
        return finish()
    data = res.data or {"decisions": [], "skipped": []}
    skipped = list(data.get("skipped") or [])
    # AI가 생각하는 동안 사용자가 결정했을 수 있다 → 지금 다시 확인
    now_todo = {v["id"]: v for v in review_items(store)[0]}
    applied = 0
    for d in data.get("decisions") or []:
        v = now_todo.get(d["id"])
        if v is None:
            skipped.append({"id": d["id"], "why": "그 사이 사용자가 결정함 — 건드리지 않음"})
            continue
        if not d["changes"]:
            skipped.append({"id": d["id"], "why": f"보류(keep): {d['reason']}"})
            continue
        try:
            store.update_user(d["id"], d["changes"], by="ai", reason=ai_reason_text(d, v, names))
            applied += 1
        except (KeyError, ValueError) as e:
            skipped.append({"id": d["id"], "why": f"반영 실패: {e}"})
    out.update(ok=True, applied=applied, skipped=skipped)
    return finish()


AI_REASON_MAX = 300   # store.update_user가 자르는 길이


def ai_reason_text(d: dict, item: dict, names: list | None = None) -> str:
    """화면에 남길 AI 이유: 이유(자리 표시 없이) + 상대 날짜면 계산 + 가리지 않은 원문 인용(계약 13-3)."""
    reason = unplace(d.get("reason") or "")
    basis = d.get("date_basis")
    if basis:
        due = parse_due(d["changes"]["due"])[0] if d["changes"].get("due") else _as_dt(item.get("due"))
        if due is not None:
            reason += " · " + relative_note(basis, due)
    quote = original_quote(d.get("evidence") or "", item.get("evidence") or [], names)
    room = max(40, min(150, AI_REASON_MAX - len(reason) - len(' — 근거: ""')))
    if len(quote) > room:
        quote = quote[:room - 1] + "…"
    return f"{reason} — 근거: \"{quote}\""


def queue_counts(store) -> dict:
    """확인함에 남은 것: pending_left(아직 확인 대기), user_reverted(사용자가 결정을 되돌려 pending — AI가 건드리지 않음)."""
    review = [v for v in store.view() if v["status"] == "review"]
    return {"pending_left": len(review),
            "user_reverted": sum(1 for v in review if v["decision"] == "pending" and v.get("decided_by") == "user"
                                 and v.get("decided_at"))}


# ---------------------------------------------------------------- 예전 진입점(한 번 묻기)

def ask_json(prompt: str, text: str, schema_hint: str, timeout: float = 90, runner=None,
             command: str | None = None) -> dict:
    """AI CLI에 한 번 묻고 답의 JSON 객체를 돌려준다(검사·재시도 없음). 글은 가린 뒤 stdin으로만, 최대 MAX_TEXT자.
    실패하면 AgentCliError(한국어). 앱 안에서는 검사·재시도·기록이 있는 run_task / AiSession을 쓴다."""
    exe = command or detect()
    if not exe:
        raise AgentCliError(f"AI CLI({DEFAULT_COMMAND})를 찾지 못했습니다. 설치하거나 {ENV_VAR}에 경로를 넣으세요")
    body, _ = redact(text)
    body, cut = cap_text(body)
    stdin = (f"응답 형식(JSON 객체 하나만, 다른 글 없이) / reply with one JSON object: {schema_hint}\n\n"
             + ("(글이 길어 일부만 보냄)\n" if cut else "")
             + "=== 입력 글 시작 / INPUT START ===\n" + body + "\n=== 입력 글 끝 / INPUT END ===\n")
    try:
        obj, _ = _call(exe, prompt, stdin, timeout, runner)
    except AgentCliError as e:
        raise AgentCliError(str(e) + (f" (글이 길어 {MAX_TEXT:,}자만 보냈습니다)" if cut else "")) from None
    if obj is None:
        raise AgentCliError("AI CLI의 답에서 JSON을 찾지 못했습니다")
    return obj


def weights_via_cli(text: str, is_html: bool = False, runner=None, timeout: float = 90,
                    command: str | None = None, store=None) -> WeightsFound | None:
    """grading_weights 작업 한 번(검사·재시도 포함). 비중이 없다는 답이면 None, 실패하면 AgentCliError."""
    res = run_task("grading_weights", {"text": text, "is_html": is_html}, runner=runner, command=command, store=store)
    if not res.ok:
        raise AgentCliError(res.error or "AI CLI 실패")
    if res.data is not None:
        res.data.notes += res.notes
    return res.data


# ---------------------------------------------------------------- 평가 묶음(실제 형식의 글)

def _ev(text, typ="announcement", posted="2026-11-09T10:00:00+09:00"):
    return {"text": text, "source_type": typ, "posted_at": posted}


_TRIAGE_ITEMS = [
    {"id": "f-lab4-a", "origin": "found", "course_id": "101", "course_name": "ASIC설계", "course_short": "ASIC설계",
     "title": "LAB4 보고서", "due": "2026-11-18T23:59:00+09:00", "status": "review", "decision": "pending",
     "linked_to": "canvas-101-1012", "conflict": "같은 과제에 다른 마감(11/20 23:59)이 적힌 근거가 있습니다",
     "notes": [], "evidence": [_ev("LAB4 보고서 마감: 11월 18일(수) 23:59")]},
    {"id": "f-lab4-b", "origin": "found", "course_id": "101", "course_name": "ASIC설계", "course_short": "ASIC설계",
     "title": "LAB4 보고서", "due": "2026-11-20T23:59:00+09:00", "status": "review", "decision": "pending",
     "linked_to": "canvas-101-1012", "conflict": "같은 과제에 다른 마감(11/18 23:59)이 적힌 근거가 있습니다",
     "notes": ["'자정' → 23:59로 둠"],
     "evidence": [_ev("LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다. 메일로 제출해 주세요.", "inbox",
                      "2026-11-12T20:00:00+09:00")]},
    {"id": "f-holiday", "origin": "found", "course_id": "102", "course_name": "디지털신호처리", "course_short": "디지털신호처리",
     "title": "휴강", "due": "2026-11-15T23:59:00+09:00", "status": "review", "decision": "pending",
     "linked_to": None, "conflict": None, "notes": ["시간 미기재 → 23:59로 둠"],
     "evidence": [_ev("11월 15일 휴강입니다. 보강은 추후 공지합니다.")]},
]

EVAL_CASES = [
    {"name": "updated_grading_policy", "task": "grading_weights",
     "inputs": {"title": "Updated Grading Policy", "is_html": True, "text": (
         "<p>Dear All,</p><p>I have attached in the updated grading policy, which includes the midterm quiz (5%) "
         "and the final quiz (5%). For each midterm week and final week, we will have a short quiz session that "
         "covers the lecture materials.</p><ul><li>Homework assignments: 30% (w/ short reports)</li>"
         "<li>Midterm project: 25% (w/ midterm report)</li><li>Midterm quiz: 5%</li>"
         "<li>Final project: 25% (w/ final report)</li><li>Final quiz: 5%</li><li>Attendance: 10%</li></ul>"
         "<p>Best Regards, Prof. Example</p>")},
     "expect": {"weights": {"homework assignments": 30, "midterm project": 25, "midterm quiz": 5,
                            "final project": 25, "final quiz": 5, "attendance": 10}}},
    {"name": "korean_syllabus_prose", "task": "grading_weights",
     "inputs": {"title": "강의계획서", "text": "수업 운영: 매주 실습 후 보고서 제출.\n성적 평가는 출석과 과제, 시험을 종합한다. "
                "출석은 10, 과제는 20, 중간은 30, 기말은 40의 비율로 반영한다. 출석률 3/4 미만은 F."},
     "expect": {"weights": {"출석": 10, "과제": 20, "중간고사": 30, "기말고사": 40}}},
    {"name": "no_weights", "task": "grading_weights",
     "inputs": {"title": "휴강 안내", "text": "11월 20일 수업은 휴강합니다. 보강은 추후 공지합니다."},
     "expect": {"weights": None}},
    {"name": "asic_lab3_due", "task": "deadline_from_text",
     "inputs": {"title": "과제: Lab3 Assignment", "posted_at": "2026-11-10T09:00:00+09:00", "is_html": True,
                "text": "<p>Lab3 Assignment is uploaded. Please submit your report and Verilog code to the "
                        "Canvas assignment box (due : 23:59 on 11/18).</p>"},
     "expect": {"deadlines": [{"month": 11, "day": 18, "hour": 23, "minute": 59, "evidence_has": "11/18"}]}},
    {"name": "asic_lab4_email_due", "task": "deadline_from_text",
     "inputs": {"title": "LAB4 보고서 제출 안내", "posted_at": "2026-11-09T10:00:00+09:00",
                "text": "LAB4 보고서는 조교 이메일(asic.ta@example.ac.kr)로 제출합니다. 마감: 11월 18일(수) 자정"},
     "expect": {"deadlines": [{"month": 11, "day": 18, "evidence_has": "11월 18일"}]}},
    {"name": "korean_prose_distribution", "task": "grade_distribution",
     "inputs": {"title": "강의계획서", "text": "성적은 상대평가로 매깁니다. 수강생의 상위 35% 정도까지 A를 주고, 그다음 "
                "40% 정도에 B를 주며, 나머지는 C 이하로 줍니다. 출석 3/4 미만은 F."},
     "expect": {"dist_groups": {"A+": 17.5, "A0": 17.5, "B+": 20, "B0": 20}}},
    {"name": "pass_fail_only", "task": "grade_distribution",
     "inputs": {"title": "Syllabus", "text": "Grading: this course is graded on a P/NP (Pass/Non-pass) basis. "
                "Attendance over 66% is required."},
     "expect": {"dist_groups": None}},
    {"name": "triage_extension_and_holiday", "task": "triage_review",
     "inputs": {"items": _TRIAGE_ITEMS, "all_items": _TRIAGE_ITEMS},
     "expect": {"triage": {"not": {"f-lab4-a": "approved", "f-holiday": "approved"},
                           "at_most_one_approved": ["f-lab4-a", "f-lab4-b"]}}},
]


_EVAL_TZ = timezone(timedelta(hours=9))


def _check_expect(case: dict, res: AiResult) -> str | None:
    exp = case["expect"]
    if not res.ok:
        return res.error or "실패"
    if "weights" in exp:
        want = exp["weights"]
        if want is None:
            return None if res.data is None else "비중이 없어야 하는데 찾음"
        if res.data is None:
            return "비중을 못 찾음"
        got = {c["name"].lower(): c["weight"] for c in res.data.components}
        return None if got == {k.lower(): float(v) for k, v in want.items()} else f"다름: {got}"
    if "dist_groups" in exp:
        want = exp["dist_groups"]
        if want is None:
            return None if res.data is None else "학점 비율이 없어야 하는데 찾음"
        if res.data is None or res.data.kind != "relative":
            return "학점 비율을 못 찾음"
        bad = {k: res.data.dist.get(k) for k, v in want.items() if abs((res.data.dist.get(k) or 0) - v) > 0.6}
        return None if not bad else f"다름: {bad}"
    if "deadlines" in exp:
        got = res.data or []
        for w in exp["deadlines"]:
            hit = False
            for g in got:
                d = parse_iso(g["due"])
                d = d.astimezone(_EVAL_TZ) if d else None   # 평가 문장의 마감은 한국 시각(CI는 UTC에서 돈다)
                if (d and d.month == w["month"] and d.day == w["day"]
                        and all(getattr(d, k) == w[k] for k in ("hour", "minute") if k in w)
                        and w.get("evidence_has", "") in g["evidence"]):
                    hit = True
            if not hit:
                return f"{w['month']}/{w['day']} 마감을 못 찾음({len(got)}개 찾음)"
    if "triage" in exp:
        decided = {d["id"]: d["changes"].get("decision", "keep") for d in (res.data or {}).get("decisions", [])}
        for iid, bad in exp["triage"].get("not", {}).items():
            if decided.get(iid) == bad:
                return f"{iid}를 {bad}로 결정함"
        group = exp["triage"].get("at_most_one_approved") or []
        if sum(decided.get(i) == "approved" for i in group) > 1:
            return "같은 과제 후보를 둘 이상 승인함"
    return None


def run_evals(runner=None, command: str | None = None, cases: list | None = None) -> dict:
    """평가 묶음을 돌린다(runner=None이면 실제 CLI — 호출 한도·기록과 무관한 점검용).
    `python -m lms_agent ai-eval`에서 부르기 좋게 결과만 돌려준다(입력·답 내용 없음).
    결과: {"cli", "passed", "total", "cases": [{"name","task","passed","ok","error","problem","attempts","duration_ms"}]}"""
    out = []
    for case in cases or EVAL_CASES:
        res = run_task(case["task"], case["inputs"], runner=runner, command=command)
        problem = _check_expect(case, res)
        out.append({"name": case["name"], "task": case["task"], "passed": problem is None, "ok": res.ok,
                    "error": res.error, "problem": problem, "attempts": res.attempts, "duration_ms": res.duration_ms})
    return {"cli": command_name(command or detect()), "passed": sum(c["passed"] for c in out), "total": len(out),
            "cases": out}
