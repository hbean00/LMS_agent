"""AI 작업 틀(harness): 규칙으로 못 한 일만, 사용자가 켠 경우에만, 사용자 PC의 AI CLI(기본 `claude`)에 맡긴다. 계약 10·12장.

구성
- 작업 목록(TASKS): 작업마다 목적, 지시문(한국어+영어, JSON만 답하게), 보낼 글 만들기(글자 수 예산·가리기),
  JSON 스키마(형식·범위 검사), 사후 검사(비중 합 ~100, 근거 문장이 보낸 글에 그대로 있는지, 날짜가 근거와 맞는지 …),
  재시도(검사 오류를 그대로 인용해 고쳐 달라고 한 번 더), 시간 제한.
  작업: grading_weights(평가 비중), deadline_from_text(마감), triage_review(확인함 후보 정리 — 계약 12장),
  map_score_item(점수·마감 → 평가 항목), count_items(세부 항목 개수 — 계약 14장), grade_distribution(학점 비율 — 16장),
  assist(에이전트에게 묻기 — 계약 20장, 앱 동작 제안 type "action" — 22-9), answer(과목 질문에 인용으로 답하기 — 22-2),
  chat(일반 대화 — 24-2: 페이스메이커 말투, 제안·인용 없음, '했다'는 말·과목 사실을 지어내지 않음). assist·answer·chat은
  같은 대화의 '이전 대화'(가림, 1,500자 이내 — 근거가 아님)를 함께 받는다(24-1).
- 실행(run_task): 고른 실행기로. claude는 `claude -p <고정된 짧은 지시> --output-format json` (+ 도구 끄기 `--tools ""`·
  세션 저장 안 함 — 옛 버전이 모르면 빼고 다시). 글은 **표준 입력(stdin)으로만** 보낸다(명령줄 인자 X). shell 없이, Windows에서는 창 없이
  (CREATE_NO_WINDOW), 빈 임시 폴더에서 실행해 CLI가 프로젝트 파일을 읽지 않게 한다. 호출한 쪽으로 예외를 던지지 않고
  AiResult{ok, data, error, attempts, duration_ms, cli, model}를 돌려준다.
- 가리기(redact): 보내기 전에 이메일·전화번호·학번(가린 학번 포함)·링크·토큰처럼 보이는 긴 문자열·알려 준 이름·편지 서명을
  지운다. 점수·토큰·이름은 보내지 않는다(성적 파일은 어떤 작업에도 넣지 않는다).
- 실행 기록: store.data["ai_runs"]에 작업·성공·시도 수·걸린 시간·보낸 글자 수·오류만(최근 50개). 지시문·글·답은 남기지 않는다.
- 정책(AiSession): settings.ai_cli가 true일 때만, 동기화 한 번에 최대 MAX_CALLS_PER_SYNC회, 하루 MAX_CALLS_PER_DAY회.
  AI 결과는 "by": "ai"로 표시되고 근거(검증된 원문 인용)와 함께 보인다. 사용자가 되돌릴 수 있다.
- 평가(run_evals): 실제 형식의 글로 같은 작업을 돌려 결과를 검사한다(가짜 실행기 또는 실제 CLI).
- 여러 CLI(계약 21장): 하네스는 하나, 실행기(adapter)는 갈아 끼운다 — ADAPTERS(claude·codex·gemini·opencode·copilot·
  ollama·custom). claude는 지금까지의 호출 그대로, 다른 CLI는 설치된 버전의 도움말(--help)을 읽어(probe) 실제로 있는
  비대화·도구 끄기 옵션만 쓴다(없으면 "확인 필요" — 옵션을 짐작해 실행하지 않음). 고르기: settings.ai_provider
  ("auto" = 찾은 것 중 안전한 첫 번째)·ai_custom·ai_model. 어느 CLI든 글은 stdin으로만, 셸 없이, 빈 임시 폴더에서,
  우리 환경 변수(LMS_AGENT_*·우리 .env·GOOGLE*)를 뺀 채로 띄운다.
- claude 경로: 환경 변수 LMS_AGENT_AI_CLI에 실행 파일 전체 경로(claude와 같은 인자를 받아야 함).
  화면·오류·기록에는 명령 이름(예: "claude")만 남기고 사용자 이름이 든 전체 경로는 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


_OUR_PREFIXES = ("LMS_AGENT_",)   # 우리 앱의 환경 변수(Canvas 토큰·구글 클라이언트·데이터 폴더 …)
_GOOGLE_PREFIX = "GOOGLE"         # 우리 구글 연결과 섞이지 않게 — 그 CLI가 자기 인증에 쓰는 이름(Adapter.own_env)만 남긴다


def child_env(keep=()) -> dict:
    """AI CLI에 넘길 환경 변수: 지금 환경에서 우리 것만 뺀다(계약 21-2). PATH·HOME·USERPROFILE·APPDATA 같은 것과
    그 CLI의 자기 인증(예: OPENAI_API_KEY, ~/.codex)은 그대로 둬야 CLI가 로그인 정보를 찾는다.
    빼는 것: LMS_AGENT_*, 우리 .env가 넣은 이름(config.DOTENV_KEYS — 토큰·시크릿·키), GOOGLE*(keep에 있는 이름은 남김)."""
    from .config import DOTENV_KEYS
    ours = {k.upper() for k in DOTENV_KEYS}
    keep_u = {k.upper() for k in keep or ()}
    out = {}
    for k, v in os.environ.items():
        u = k.upper()
        if u.startswith(_OUR_PREFIXES) or u in ours:
            continue
        if u.startswith(_GOOGLE_PREFIX) and u not in keep_u:
            continue
        out[k] = v
    return out


def _kill(p: subprocess.Popen) -> None:
    """시간 초과: 윈도에서는 claude.cmd → node 같은 자식까지 끝낸다."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    p.kill()
    p.communicate()


def _default_runner(args: list, input: str, timeout: float, cwd: str, env: dict | None = None) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=cwd, text=True, encoding="utf-8", errors="replace", creationflags=flags,
                         env=env if env is not None else child_env())
    try:
        out, err = p.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
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


# ---------------------------------------------------------------- 출력 읽기(계약 21-1): 어느 형식이든 모델 글 안의 첫 JSON 객체

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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_THINK = re.compile(r"(?is)<think>.*?</think>|^Thinking\.\.\..*?\.\.\.done thinking\.")


def _clean(s: str) -> str:
    """색 코드·생각 과정(<think>…</think>, ollama 'Thinking...') 지우기 — 그 안의 괄호를 답으로 읽지 않게."""
    return _THINK.sub(" ", _ANSI.sub("", s or ""))


def _payload_text(stdout: str) -> tuple[dict | None, dict]:
    """text: 출력 전체에서 첫 JSON 객체(codex exec·ollama run·copilot -p처럼 마지막 답만 찍는 CLI)."""
    return _first_json_object(_clean(stdout)), {}


def _payload_json(stdout: str) -> tuple[dict | None, dict]:
    """json: JSON 객체 하나. {"response"|"result": 모델 글, "stats"…}(gemini -o json 등)이면 그 글에서, 아니면 객체 자체가 답.
    {"error": …}만 있으면 CLI 자체 오류."""
    text = _clean(stdout).strip()
    try:
        outer = json.loads(text)
    except ValueError:
        return _first_json_object(text), {}
    if not isinstance(outer, dict):
        return None, {}
    meta: dict = {}
    stats = outer.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(outer.get("model"), str):
        meta["model"] = outer["model"]
    elif isinstance(models, dict) and models:
        meta["model"] = next(iter(models))
    for key in ("response", "result"):
        if isinstance(outer.get(key), str):
            return _first_json_object(_clean(outer[key])), meta
    err = outer.get("error")
    if err and len(outer) <= 3:
        msg = err.get("message") if isinstance(err, dict) else err
        raise AgentCliError(f"AI CLI가 오류를 돌려줬습니다: {str(msg)[:150]}")
    return outer, meta


_MSG_TYPES = re.compile(r"(?i)agent_message|assistant_message|^message$|^assistant$|^text$|^result$|^response$")


def _event_text(ev: dict) -> tuple[str | None, bool]:
    """JSON 줄 하나 → (모델 글, 답 메시지인지). codex `{"type":"item.completed","item":{"type":"agent_message","text"}}`,
    옛 codex `{"msg":{"type":"agent_message","message"}}`, opencode `{"type":"text","part":{"text"}}` 같은 모양."""
    for holder in (ev, ev.get("item"), ev.get("msg"), ev.get("part"), ev.get("data")):
        if not isinstance(holder, dict):
            continue
        kind = str(holder.get("type") or holder.get("item_type") or holder.get("role") or "")
        for key in ("text", "message", "content", "result", "response"):
            v = holder.get(key)
            if isinstance(v, str) and v.strip():
                return v, bool(_MSG_TYPES.search(kind)) or kind == ""
    return None, False


def _payload_lines(stdout: str) -> tuple[dict | None, dict]:
    """json-lines: 줄마다 JSON 사건. 마지막 답 메시지의 글에서 첫 JSON 객체(없으면 그 앞 메시지들). 답 없이 오류만이면 CLI 오류."""
    meta: dict = {}
    answers, others, errors = [], [], []
    for line in _clean(stdout).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if isinstance(ev.get("model"), str):
            meta["model"] = ev["model"]
        kind = str(ev.get("type") or "")
        if kind in ("error", "turn.failed") or (kind.endswith(".failed") and ev.get("error")):
            e = ev.get("error") if isinstance(ev.get("error"), (dict, str)) else ev
            errors.append(str((e.get("message") if isinstance(e, dict) else e) or kind))
            continue
        text, is_answer = _event_text(ev)
        if text:
            (answers if is_answer else others).append(text)
    for text in reversed(answers + others if not answers else answers):
        obj = _first_json_object(_clean(text))
        if obj is not None:
            return obj, meta
    if errors and not answers:
        raise AgentCliError(f"AI CLI가 오류를 돌려줬습니다: {errors[-1][:150]}")
    return None, meta


_PARSERS = {"claude-json": _payload, "json": _payload_json, "json-lines": _payload_lines, "text": _payload_text}
OUTPUTS = tuple(_PARSERS)


def parse_output(fmt: str, stdout: str) -> tuple[dict | None, dict]:
    """CLI 출력 → (답 JSON 객체 | None, {"model"}). 형식: claude-json·json·json-lines·text."""
    return _PARSERS.get(fmt, _payload_text)(stdout or "")


# ---------------------------------------------------------------- 실행기(adapter) — 계약 21장

@dataclass(frozen=True)
class Flag:
    """실행기 옵션 하나. 도움말(probe)에 find의 낱말이 모두 있을 때만 args를 붙인다."""
    args: tuple = ()            # 붙일 인자("{prompt}" = 고정 지시, "{model}" = 모델 이름). 비면 도움말 확인만
    find: tuple = ()            # 도움말에 있어야 하는 옵션 이름·값(낱말 단위)
    role: str = "extra"         # "run"(비대화 실행에 꼭 필요) · "safe"(도구·셸 끄기, 읽기 전용) · "extra"(있으면 씀)
    output: str | None = None   # 이 옵션을 쓰면 바뀌는 출력 형식
    what: str = ""              # 무엇을 하는 옵션인지(화면용)

    @property
    def label(self) -> str:
        return " ".join(a if a else '""' for a in self.args if a != "{prompt}") or self.what or " ".join(self.find)


@dataclass(frozen=True)
class Adapter:
    """AI CLI 하나를 띄우는 방법(계약 21-1). 옵션은 공개 문서 기준 후보이고, 실제로 쓸지는 probe가 정한다."""
    id: str
    name: str
    exe_names: tuple = ()
    output: str = "text"        # 기본 출력 형식(옵션이 바꿀 수 있음)
    base: tuple = ()            # 실행 파일 바로 뒤 인자(하위 명령 등)
    flags: tuple = ()
    help: tuple = ("--help",)   # probe가 읽을 도움말 명령
    prompt_mode: str = "stdin"  # "stdin": 고정 지시도 stdin 맨 앞에 · "arg": 고정 지시(강의 글 아님)만 인자로
    trusted: bool = False       # 하네스가 이미 시험한 호출(claude): probe 없이 쓰고, 모르는 옵션이면 빼고 다시
    no_tools: str = ""          # 원래 도구가 없는 실행기(ollama run) — 안전한 이유
    local: bool = False         # 글이 이 PC 밖으로 나가지 않음
    needs_model: bool = False   # settings.ai_model, 없으면 models 명령의 첫 번째
    models_cmd: tuple = ()
    own_env: tuple = ()         # 그 CLI가 자기 인증에 쓰는 GOOGLE* 이름(우리 .env가 넣은 것이 아니면 남김)
    site: str = ""
    notes: str = ""

    @property
    def no_tools_flags(self) -> list:
        return [a for f in self.flags if f.role == "safe" for a in f.args]

    @property
    def args(self) -> list:
        """문서 기준 후보 인자 전체(probe 전). 실제 인자는 probe 결과의 args."""
        return list(self.base) + [a for f in self.flags for a in f.args]


ADAPTERS: dict[str, Adapter] = {a.id: a for a in [
    Adapter("claude", "Claude Code", ("claude",), output="claude-json", prompt_mode="arg", trusted=True,
            base=("-p", "{prompt}", "--output-format", "json"),
            flags=(Flag(("--tools", ""), ("--tools",), "safe", what="도구 끄기"),
                   Flag(("--no-session-persistence",), ("--no-session-persistence",), what="세션 저장 안 함")),
            site="https://docs.claude.com/en/docs/claude-code/overview",
            notes="앱이 처음부터 써 온 호출이에요. 도구를 끈 채(--tools \"\") 답만 받아요."),
    Adapter("codex", "OpenAI Codex CLI", ("codex",), base=("exec",), help=("exec", "--help"),
            flags=(Flag((), ("stdin",), "run", what="글을 표준 입력으로 받음"),
                   Flag(("--sandbox", "read-only"), ("--sandbox", "read-only"), "safe",
                        what="읽기 전용 샌드박스(파일 쓰기·네트워크 막힘)"),
                   Flag(("--skip-git-repo-check",), ("--skip-git-repo-check",), what="빈 임시 폴더에서 실행"),
                   Flag(("--ephemeral",), ("--ephemeral",), what="세션 저장 안 함"),
                   Flag(("--color", "never"), ("--color", "never"), what="색 없이"),
                   Flag(("--json",), ("--json",), output="json-lines", what="JSON 줄로 출력")),
            site="https://developers.openai.com/codex/cli",
            notes="codex exec(비대화)로 실행해요. 승인을 물을 수 없는 모드라 읽기 전용 샌드박스 안에서만 움직여요."),
    Adapter("gemini", "Gemini CLI", ("gemini",),
            flags=(Flag((), ("stdin",), "run", what="표준 입력으로 받은 글로 비대화 실행"),
                   Flag(("--approval-mode", "default"), ("--approval-mode", "default"), "safe",
                        what="비대화 모드에서 셸·파일 쓰기 도구가 꺼짐(읽기 도구만)"),
                   Flag(("--output-format", "json"), ("--output-format", "json"), output="json", what="JSON으로 출력")),
            own_env=("GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI",
                     "GOOGLE_APPLICATION_CREDENTIALS"),
            site="https://github.com/google-gemini/gemini-cli",
            notes="표준 입력이 있으면 비대화로 한 번 답하고 끝나요. 승인이 필요한 셸·쓰기 도구는 이 모드에서 빠져요."),
    Adapter("opencode", "OpenCode", ("opencode",), base=("run",), help=("run", "--help"),
            flags=(Flag((), ("stdin",), "run", what="글을 표준 입력으로 받음"),
                   Flag(("--format", "json"), ("--format", "json"), output="json-lines", what="JSON 줄로 출력")),
            site="https://opencode.ai/docs/cli/",
            notes="도구를 끄는 명령줄 옵션이 알려져 있지 않아 쓰지 않아요. opencode.json의 permission으로 도구를 끈 뒤 "
                  "'직접 지정'으로 연결할 수 있어요."),
    Adapter("copilot", "GitHub Copilot CLI", ("copilot",), prompt_mode="arg", base=("-p", "{prompt}"),
            flags=(Flag((), ("--prompt",), "run", what="한 번 묻고 끝내는 비대화 실행(-p)"),
                   Flag((), ("stdin",), "run", what="글을 표준 입력으로 받음"),
                   Flag(("--deny-tool", "shell", "--deny-tool", "write"), ("--deny-tool",), "safe",
                        what="셸·파일 쓰기 도구 거부"),
                   Flag(("--no-color",), ("--no-color",), what="색 없이"),
                   Flag(("--silent",), ("--silent",), what="답만 출력"),
                   Flag(("--no-custom-instructions",), ("--no-custom-instructions",), what="폴더의 지시 파일 무시")),
            site="https://docs.github.com/copilot/how-tos/copilot-cli",
            notes="도구 허용 옵션(--allow-*) 없이, 셸·쓰기 도구는 거부한 채 실행해요."),
    Adapter("ollama", "Ollama(이 PC의 모델)", ("ollama",), base=("run", "{model}"), help=("run", "--help"),
            flags=(Flag((), ("ollama run",), "run", what="ollama run <모델>(표준 입력의 글로 한 번 답함)"),
                   Flag(("--hidethinking",), ("--hidethinking",), what="생각 과정 숨김"),
                   Flag(("--nowordwrap",), ("--nowordwrap",), what="줄바꿈 없이"),
                   Flag(("--format", "json"), ("--format",), what="JSON으로만 답")),
            no_tools="모델만 실행해요(도구 없음)", local=True, needs_model=True, models_cmd=("list",),
            site="https://ollama.com/download",
            notes="이 PC에서 도는 모델이라 글이 인터넷으로 나가지 않아요(이름이 -cloud로 끝나는 모델은 예외)."),
    Adapter("custom", "직접 지정", (), notes="실행 파일 경로와 인자를 직접 넣어요. 글은 항상 표준 입력으로 보내요."),
]}
AUTO_ORDER = ("claude", "codex", "gemini", "opencode", "copilot", "ollama")   # "auto"가 보는 순서(custom은 자동 선택 안 함)
PROBE_TIMEOUT = 10.0      # 도움말 한 번
PROBE_TTL = 600.0         # 같은 실행 파일(경로·수정 시각·크기)의 확인 결과를 다시 쓰는 시간
CUSTOM_WARNING = ("직접 지정한 CLI는 도구·셸을 끄는지 앱이 확인할 수 없어요. 인자에 도구를 끄는(읽기 전용) 옵션을 꼭 넣어 "
                  "주세요. 자동으로는 고르지 않아요.")
NOT_FOUND = ("AI CLI를 찾지 못했습니다(" + "·".join(AUTO_ORDER) + " 중 바로 쓸 수 있는 것이 없음). "
             "설치하거나 설정 → AI 도움에서 직접 지정하세요")


@dataclass(frozen=True)
class Plan:
    """실행 준비가 끝난 실행기: 실행 파일과 인자(글 없음). run_task·AiSession이 받는다."""
    id: str
    name: str
    exe: str
    output: str = "text"
    variants: tuple = ((),)     # 시도할 인자 목록들(claude: 새 옵션 포함 → 옛 버전이 모르면 뺀 것)
    prompt_mode: str = "stdin"
    safe: bool = False
    local: bool = False
    own_env: tuple = ()
    model: str | None = None

    @property
    def cli(self) -> str | None:
        return command_name(self.exe)

    def argv(self, prompt: str) -> list[list[str]]:
        """실제 명령줄들. 고정 지시만 들어갈 수 있다(prompt_mode "arg") — 강의 글·작업 지시는 늘 stdin."""
        line = _safe_prompt(prompt, self.exe)
        return [[self.exe] + [line if a == "{prompt}" else a for a in v] for v in self.variants]


def claude_plan(exe: str) -> Plan:
    """claude: 지금까지와 똑같은 호출(-p <고정 지시> --output-format json --tools "" --no-session-persistence, 모르면 뒤 둘 빼고)."""
    a = ADAPTERS["claude"]
    extra = tuple(x for f in a.flags for x in f.args)
    return Plan("claude", a.name, exe, a.output, (a.base + extra, a.base), a.prompt_mode, True)


def find_exe(adapter: Adapter) -> str | None:
    """PATH에서 그 실행기를 찾는다(claude는 detect — LMS_AGENT_AI_CLI 우선)."""
    if adapter.id == "claude":
        return detect()
    for n in adapter.exe_names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _has(text: str, token: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text, re.I) is not None


def _help_text(args: list, timeout: float = PROBE_TIMEOUT) -> tuple[int, str]:
    """도움말 읽기: 셸 없이, 표준 입력 없이(묻는 화면이 떠도 바로 끝남), 빈 임시 폴더에서, 우리 환경 변수를 뺀 채로.
    → (종료 코드, stdout+stderr)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="lms-agent-probe-") as cwd:
        p = subprocess.Popen(list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=cwd, text=True, encoding="utf-8", errors="replace", creationflags=flags,
                             env=child_env())
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(p)
            raise
    return p.returncode, (out or "") + "\n" + (err or "")


_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?)")
_MODEL_NAME = re.compile(r"^[\w.:/@+-]{1,120}$")
_MODEL_ROW = re.compile(r"^\s*(\S+)\s+[0-9a-f]{12}\b")   # `ollama list`: NAME  ID(16진 12자리)  SIZE  MODIFIED


def _models(text: str) -> list[str]:
    """`ollama list` 표 → 모델 이름들(머리줄·오류 줄은 ID 칸이 없어 빠진다)."""
    out = []
    for line in _clean(text).splitlines():
        m = _MODEL_ROW.match(line)
        if m and _MODEL_NAME.match(m.group(1)):
            out.append(m.group(1))
    return out


def _is_local_model(model: str | None) -> bool:
    """ollama: 이름이 -cloud/:cloud로 끝나는 모델이나 OLLAMA_HOST가 다른 컴퓨터면 글이 이 PC 밖으로 간다."""
    if model and re.search(r"(?i)[-:]cloud$", model):
        return False
    host = re.sub(r"^\w+://", "", (os.environ.get("OLLAMA_HOST") or "").strip().lower()).split("/")[0]
    if host.startswith("["):
        host = host[1:].split("]")[0]
    elif host.count(":") == 1:
        host = host.split(":")[0]
    return host in ("", "localhost", "127.0.0.1", "::1", "0.0.0.0")


def probe(adapter_id: str, path: str, help_runner=None, model: str | None = None) -> dict:
    """실행기 하나의 도움말을 읽어 실제로 있는 옵션만 고른다(계약 21-1). 필요한 비대화·도구 끄기 옵션이 없으면 "needs_check".
    help_runner(args) → (종료 코드, 글) — 테스트는 가짜 도움말을 넣는다. 결과에 전체 경로는 넣지 않는다.
    → {status, safe, confirmed[], missing[], args[](실행 파일 뒤), output, version, models[], model, local, note, error}"""
    a = ADAPTERS[adapter_id]
    run = help_runner or _help_text
    out = {"status": "needs_check", "safe": False, "confirmed": [], "missing": [], "args": [], "output": a.output,
           "version": None, "models": [], "model": None, "local": a.local, "note": None, "error": None}

    def read(args) -> str | None:
        try:
            _, text = run([path, *args])
        except subprocess.TimeoutExpired:
            out["error"] = f"도움말이 {PROBE_TIMEOUT:g}초 안에 나오지 않았어요"
            return None
        except OSError as e:
            out["error"] = f"실행하지 못했어요: {_scrub(str(e), path)}"
            return None
        return _clean(text or "")

    text = read(a.help)
    if text is None:
        out["note"] = out["error"]
        return out
    vtext = read(("--version",))
    m = _VERSION.search(vtext or "")
    out["version"] = m.group(1) if m else None
    out["error"] = None
    args = [x for x in a.base]
    run_ok, safe_hits, safe_need = True, 0, 0
    for f in a.flags:
        hit = all(_has(text, t) for t in f.find)
        if f.role == "safe":
            safe_need += 1
        if hit:
            out["confirmed"].append(f.label)
            args += list(f.args)
            if f.output:
                out["output"] = f.output
            safe_hits += f.role == "safe"
        elif f.role in ("run", "safe"):
            out["missing"].append(f.what or f.label)
            run_ok = run_ok and f.role != "run"
    safe = bool(a.no_tools) or (safe_need > 0 and safe_hits == safe_need)
    if not a.no_tools and not safe_need:
        out["missing"].append("도구를 끄는 옵션(알려진 것이 없음)")
    out["safe"] = safe
    if a.needs_model:
        listing = read(a.models_cmd) if a.models_cmd else None
        out["models"] = _models(listing or "")[:50]
        want = (model or "").strip() or None
        if want and want not in out["models"] and f"{want}:latest" not in out["models"]:   # 'llama3.2' = llama3.2:latest
            out["missing"].append(f"모델 '{want}'(먼저 받아 두세요: ollama pull {want})")
            want = None
            run_ok = False
        chosen = want or (out["models"][0] if out["models"] else None)
        if chosen is None and run_ok:
            out["missing"].append("받아 둔 모델(ollama가 켜져 있는지, ollama pull <모델>로 받았는지 확인)")
            run_ok = False
        out["model"] = chosen
        out["local"] = a.local and _is_local_model(chosen)
        args = [chosen if x == "{model}" else x for x in args]
    out["args"] = args
    if run_ok and safe:
        out["status"] = "ready"
        out["note"] = a.no_tools or None
    else:
        out["note"] = "도움말에서 확인하지 못한 것: " + ", ".join(out["missing"]) if out["missing"] else None
    return out


_probe_cache: dict = {}
_probe_lock = threading.Lock()


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def _stamp(path: str):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def cached_probe(adapter_id: str, path: str, model: str | None = None, help_runner=None, run: bool = True) -> dict | None:
    """probe 결과를 같은 실행 파일(경로·수정 시각·크기)·모델이면 PROBE_TTL 동안 다시 쓴다. run=False면 저장된 것만(없으면 None).
    help_runner를 직접 주면(테스트) 저장하지 않는다."""
    if help_runner is not None:
        return probe(adapter_id, path, help_runner, model) if run else None
    key = (adapter_id, path, _stamp(path), model or "")
    with _probe_lock:
        hit = _probe_cache.get(key)
    if hit and time.monotonic() - hit[0] < PROBE_TTL:
        return hit[1]
    if not run:
        return None
    res = probe(adapter_id, path, None, model)
    with _probe_lock:
        _probe_cache[key] = (time.monotonic(), res)
    return res


def _resolve_custom(path: str) -> str | None:
    """직접 지정한 실행 파일: 경로(구분자가 있으면)는 그 파일이 있어야, 이름만이면 PATH에서."""
    p = str(path or "").strip().strip('"').strip()
    if not p:
        return None
    if re.search(r"[\\/]", p):
        return p if os.path.isfile(p) else None
    return shutil.which(p)


def custom_settings(settings: dict) -> dict | None:
    c = (settings or {}).get("ai_custom")
    return c if isinstance(c, dict) and str(c.get("path") or "").strip() else None


def _entry(aid: str, settings: dict, claude_exe: str | None = None, discover: bool = True, probe_now: bool = False,
           help_runner=None, finder=None) -> dict:
    """실행기 하나의 상태(계약 21-3 state.ai.providers의 한 칸). '_'로 시작하는 칸(실행 파일·Plan)은 화면에 내보내지 않는다."""
    a = ADAPTERS[aid]
    e = {"id": aid, "name": a.name, "found": False, "path_hint": None, "safe": False, "status": "missing",
         "version": None, "local": a.local, "probed": False, "auto": aid in AUTO_ORDER, "flags": [], "missing": [],
         "note": None, "notes": a.notes or None, "site": a.site or None}
    if aid == "custom":
        c = custom_settings(settings)
        e["warning"] = CUSTOM_WARNING
        if not c:
            e["note"] = "아직 지정하지 않았어요"
            return e
        args = [str(x) for x in c.get("args") or []]
        out = c.get("output") if c.get("output") in OUTPUTS else "text"
        e.update(path_hint=command_name(c["path"]), output=out, args=[_scrub(x, None) for x in args])
        exe = _resolve_custom(c["path"])
        if not exe:
            e["note"] = "실행 파일을 찾지 못했어요"
            return e
        e.update(found=True, status="ready", note="도구를 끄는지 앱이 확인하지 않은 실행기예요")
        e["_plan"] = Plan("custom", command_name(exe) or a.name, exe, out, (tuple(args),), "stdin", False, False)
        return e
    exe = claude_exe if (aid == "claude" and claude_exe) else ((finder or find_exe)(a) if discover else None)
    if not exe:
        e["note"] = "설치되지 않았거나 PATH에서 찾지 못했어요"
        return e
    e.update(found=True, path_hint=command_name(exe))
    if a.trusted:
        e.update(status="ready", safe=True, flags=[f.label for f in a.flags], note=None)
        e["_plan"] = claude_plan(exe)
        return e
    model = (str(settings.get("ai_model") or "").strip() or None) if a.needs_model else None
    r = cached_probe(aid, exe, model, help_runner, run=probe_now)
    if r is None:
        e.update(status="needs_check", note="아직 확인하지 않았어요 — 도움말을 읽어 쓸 옵션을 확인해요")
        return e
    e.update(probed=True, status=r["status"], safe=r["safe"], version=r["version"], flags=r["confirmed"],
             missing=r["missing"], local=r["local"], note=r["note"] or r["error"], output=r["output"])
    if a.needs_model:
        e.update(model=r["model"], models=r["models"][:20])
    if r["status"] == "ready":
        e["_plan"] = Plan(aid, a.name, exe, r["output"], (tuple(r["args"]),), a.prompt_mode, r["safe"], r["local"],
                          a.own_env, r["model"])
    return e


def public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def providers(settings: dict | None = None, claude_exe: str | None = None, discover: bool = True,
              probe_now: bool = False, help_runner=None, finder=None) -> list[dict]:
    """찾은 실행기와 상태(AUTO_ORDER + custom). probe_now면 아직 확인하지 않은 것의 도움말을 함께(동시에) 읽는다.
    claude_exe: 테스트·데모가 넣은 claude(그때는 discover=False로 PATH를 보지 않는다)."""
    settings = settings or {}
    ids = list(AUTO_ORDER) + ["custom"]
    if not probe_now:
        return [_entry(i, settings, claude_exe, discover, False, help_runner, finder) for i in ids]
    with ThreadPoolExecutor(max_workers=len(ids)) as ex:
        futs = [ex.submit(_entry, i, settings, claude_exe, discover, True, help_runner, finder) for i in ids]
        return [f.result() for f in futs]


def pick(entries: list[dict], settings: dict | None = None, want: str | None = None) -> dict | None:
    """고른 실행기(state 계산용 — 새로 확인하지 않음). auto: AUTO_ORDER에서 쓸 수 있고 안전한 첫 번째. custom은 직접 고를 때만."""
    want = want or (settings or {}).get("ai_provider") or "auto"
    by = {e["id"]: e for e in entries}
    if want == "auto":
        return next((by[i] for i in AUTO_ORDER if i in by and by[i]["status"] == "ready" and by[i]["safe"]
                     and by[i].get("_plan")), None)
    e = by.get(want)
    return e if e and e["status"] == "ready" and e.get("_plan") else None


def why_not(entry: dict | None, want: str) -> str:
    """고른 실행기를 못 쓰는 이유(한국어, 경로 없음)."""
    if want == "auto":
        return NOT_FOUND
    if entry is None:
        return f"모르는 AI 실행기예요: {str(want)[:40]}"
    if entry["status"] == "missing":
        if entry["id"] == "custom":
            return "직접 지정한 AI CLI의 실행 파일을 찾지 못했습니다. 설정 → AI 도움에서 경로를 확인하세요"
        return (f"선택한 AI CLI({entry['name']})를 찾지 못했습니다. 설치했는지 확인하거나 설정 → AI 도움에서 "
                "다른 것을 고르세요")
    miss = ", ".join(entry.get("missing") or []) or entry.get("note") or "필요한 옵션"
    return f"{entry['name']}: 비대화 실행·도구 끄기 옵션을 도움말에서 확인하지 못해 쓰지 않았습니다(확인 못 한 것: {miss})"


def select(settings: dict | None = None, claude_exe: str | None = None, discover: bool = True, help_runner=None,
           want: str | None = None, finder=None) -> tuple[Plan | None, str | None]:
    """설정대로 실행기 하나를 고른다(필요하면 도움말을 읽어 확인) → (Plan, None) 또는 (None, 이유).
    auto는 AUTO_ORDER 순서로 보다가 쓸 수 있고 안전한 첫 번째에서 멈춘다(그 뒤 것은 띄우지 않음).
    직접 고른 것을 못 쓰면 다른 실행기로 몰래 바꾸지 않는다(글이 사용자가 고르지 않은 서비스로 가지 않게)."""
    settings = settings or {}
    want = want or settings.get("ai_provider") or "auto"
    if want == "auto":
        for aid in AUTO_ORDER:
            e = _entry(aid, settings, claude_exe, discover, True, help_runner, finder)
            if e["status"] == "ready" and e["safe"] and e.get("_plan"):
                return e["_plan"], None
        return None, NOT_FOUND
    if want not in ADAPTERS:
        return None, why_not(None, want)
    e = _entry(want, settings, claude_exe, discover, True, help_runner, finder)
    if e["status"] == "ready" and e.get("_plan"):
        return e["_plan"], None
    return None, why_not(e, want)


def resolve(provider=None, command: str | None = None, store=None, settings: dict | None = None) -> tuple[Plan | None, str | None]:
    """run_task·AiSession·run_evals가 쓰는 실행기 정하기.
    provider: Plan(이미 고름) · 실행기 id · None(설정대로). command: claude 실행 파일(예전 인자 — 테스트·데모가 넣으면
    PATH를 보지 않고 claude로 쓴다; 설정에서 custom을 골랐으면 custom)."""
    if isinstance(provider, Plan):
        return provider, None
    if settings is None:
        settings = (store.data.get("settings") if store is not None else None) or {}
    want = provider if isinstance(provider, str) and provider else None
    return select(settings, claude_exe=command, discover=not command, want=want)


def _call(target, prompt: str, stdin: str, timeout: float, runner) -> tuple[dict | None, dict]:
    """CLI를 한 번 실행. target: Plan 또는 claude 실행 파일 경로(예전 방식). 실패는 AgentCliError.
    앞 인자 목록을 모른다고 하면(옛 버전) 다음 목록으로 한 번 더(호출 수에는 1회로 셈). 글은 늘 stdin."""
    plan = target if isinstance(target, Plan) else claude_plan(target)
    exe, name = plan.exe, plan.cli
    variants = plan.argv(prompt)
    if plan.prompt_mode == "stdin":
        stdin = prompt + "\n\n" + stdin
    env = None if runner else child_env(plan.own_env)
    with tempfile.TemporaryDirectory(prefix="lms-agent-ai-") as cwd:
        for i, args in enumerate(variants):
            try:
                cp = (runner(args, input=stdin, timeout=timeout, cwd=cwd) if runner
                      else _default_runner(args, input=stdin, timeout=timeout, cwd=cwd, env=env))
            except subprocess.TimeoutExpired:
                raise AgentCliError(f"AI CLI({name})가 {timeout:g}초 안에 답하지 않았습니다") from None
            except OSError as e:
                raise AgentCliError(f"AI CLI({name})를 실행하지 못했습니다: {_scrub(str(e), exe)}") from None
            if cp.returncode != 0:
                err = _scrub((cp.stderr or "") + " " + (cp.stdout or ""), exe)
                if i + 1 < len(variants) and re.search(r"unknown option|unrecognized|invalid option", err, re.I):
                    continue
                raise AgentCliError(f"AI CLI({name})가 실패했습니다(종료 코드 {cp.returncode}): {err[:150]}")
            try:
                return parse_output(plan.output, cp.stdout or "")
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


# ---- assist: 에이전트에게 묻기(계약 20-3)

ASSIST_MAX_PROPOSALS = 6
ASSIST_STATE_BUDGET = 4_000
ASSIST_TYPES = ["add_deadline", "fix_due", "link_score", "set_count", "set_weights", "set_dist", "note", "action"]
ASSIST_MARK = "사용자 요청 / user request:"
ANSWER_MARK = "학생 질문 / student question:"
ACTIONS_MARK = "앱 동작 / app actions (type action — action = id, params = 모양 그대로):"
CATALOG_MAX = 2_000
# 이어지는 대화(계약 24-1): 생략 질문이면 앞 대화와 합친 뜻을 메시지 바로 아래 줄에, 이전 대화는 따로 한 덩어리(가림, 1,500자 이내)
FOLLOW_PREFIX = "(앞 대화와 이어서 읽은 뜻 / read with the previous turns:"
HISTORY_MARK = ("이전 대화 / previous turns (흐름 참고용 — 근거가 아니에요, 인용하지 말고 이전 답의 숫자·날짜를 사실로 쓰지 마세요 "
                "/ context only: not evidence, never quote it or reuse its numbers and dates as facts):")
HISTORY_MAX = 1_500
HISTORY_MSG_MAX = 200
HISTORY_ANSWER_MAX = 300


def history_block(inputs: dict) -> tuple[str, bool]:
    """inputs["history"] = [{"message", "answer", "kind"}](오래된 것부터) → ('이전 대화' 덩어리, 가린 것이 있는지).
    가린 뒤 HISTORY_MAX자 이내 — 넘치면 최근 차례부터 남긴다. 차례가 없으면 ('', False)."""
    names = inputs.get("names")
    turns = [h for h in inputs.get("history") or [] if isinstance(h, dict)]
    if not turns:
        return "", False
    room = HISTORY_MAX - len(HISTORY_MARK) - 1
    blocks, red = [], False
    for h in reversed(turns):
        msg, n1 = redact(_flat(str(h.get("message") or ""))[:HISTORY_MSG_MAX], names)
        ans, n2 = redact(_flat(str(h.get("answer") or ""))[:HISTORY_ANSWER_MAX], names)
        red = red or bool(n1 or n2)
        block = f"- 학생 / student: {msg}\n  앱 / app ({h.get('kind') or '-'}): {ans or '(답 없음 / no answer)'}"
        if len(block) + 1 > room:
            if not blocks:
                blocks.append(block[:max(0, room)])
            break
        blocks.append(block)
        room -= len(block) + 1
    return (HISTORY_MARK + "\n" + "\n".join(reversed(blocks))) if blocks else "", red


def _bundle(inputs: dict, mark: str, extra: str = "") -> tuple[str, list]:
    """메시지(mark 아래, 생략 질문이면 합친 뜻 한 줄 더) + 과목 목록 + extra(예: 앱 동작 목록) + 이전 대화 + 지금 상태 요약 +
    찾은 글(source_key마다 한 덩어리), 모두 가려서 12,000자 안에. 보낸 글 덩어리는 inputs["_sent_snippets"](원문)·
    inputs["_sent_texts"]([(source_key, 가린 글)])에, 이전 대화 덩어리는 inputs["_history_text"]에 남는다(근거 검사에서 뺀다)."""
    names = inputs.get("names")
    notes, red = [], 0
    msg, n = redact(_flat(str(inputs.get("message") or ""))[:500], names)
    red += bool(n)
    head = [mark, msg]
    fu = _flat(str(inputs.get("followup") or ""))
    if fu and fu != _flat(str(inputs.get("message") or "")):
        fu, n = redact(fu[:500], names)
        red += bool(n)
        head.append(f"{FOLLOW_PREFIX} {fu})")
    head += ["", "과목 / courses (course_id: 이름):"]
    for c in list(inputs.get("courses") or [])[:40]:
        head.append(f"- {c.get('id')}: {redact(_flat(str(c.get('short') or '')), names)[0][:80]}")
    if extra:
        head += ["", extra]
    hist, n = history_block(inputs)
    red += bool(n)
    inputs["_history_text"] = hist
    state, n = redact(str(inputs.get("state_text") or ""), names)
    red += bool(n)
    state, cut = cap_text(state, ASSIST_STATE_BUDGET)
    if cut:
        notes.append(f"상태 요약이 길어 {ASSIST_STATE_BUDGET:,}자만 보냄")
    out = ("\n".join(head) + ("\n\n" + hist if hist else "") + "\n\n지금 상태 / current state:\n" + (state or "(없음 / none)")
           + "\n\n찾은 글 / found texts (근거는 여기서 그대로 인용 / quote evidence verbatim from here):\n")
    budget = MAX_TEXT - len(out)
    sent, texts = [], []
    for sn in inputs.get("snippets") or []:
        text, n = redact(str(sn.get("text") or ""), names)
        title, n2 = redact(_flat(str(sn.get("title") or ""))[:120], names)
        red += bool(n or n2)
        block = (f"\n### source_key: {sn.get('source_key')}\n"
                 f"course_id: {sn.get('course_id') or '-'} | 종류/type: {sn.get('type')} | "
                 f"게시/posted: {str(sn.get('posted_at') or '')[:10]} | 제목/title: {title}\n{text}\n")
        if len(block) > budget:
            notes.append(f"글자 수 예산({MAX_TEXT:,}자)을 넘어 일부 글은 보내지 않음")
            break
        budget -= len(block)
        out += block
        sent.append(sn)
        texts.append((str(sn.get("source_key") or ""), text))
    if not sent:
        out += "(찾은 글 없음 / no matching texts)\n"
    if red:
        notes.append("보내기 전에 이메일·학번·이름 등을 가림")
    inputs["_sent_snippets"] = sent
    inputs["_sent_texts"] = texts
    return out, notes


def _catalog() -> str:
    """앱 동작 목록(actions.catalog_text, 계약 22-9) — 없거나 고장 나면 빈 글(assist는 동작 제안 없이 돈다)."""
    try:
        from . import actions
        text = str(actions.catalog_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return text[:CATALOG_MAX]


def _assist_build(inputs: dict) -> tuple[str, list]:
    """사용자 요청 + 과목 목록 + 앱 동작 목록 + 지금 상태 요약 + 찾은 글, 모두 가려서 12,000자 안에(_bundle)."""
    cat = _catalog()
    extra = (ACTIONS_MARK + "\n" + redact(cat, inputs.get("names"))[0]) if cat else ""
    return _bundle(inputs, ASSIST_MARK, extra)


def _assist_validate(obj: dict, sent: str, inputs: dict, final: bool):
    """답(answer)과 제안들. 근거가 보낸 글에 그대로 없으면 거절 — 나머지 검사(과목·항목·후보·날짜 ±1일·중복·비중 합·학점 비율)는
    inputs["check"](제안, 이유)가 이 PC의 지금 상태로 한다(agent.check_proposal). 마지막 시도가 아니면 오류를 인용해 다시 묻고,
    마지막 시도면 통과 못 한 제안도 rejected_reason과 함께 돌려준다(화면에 단추 없이 보임)."""
    answer = unplace(_flat(str(obj.get("answer") or "")))[:1200]
    if len(answer) < 2:
        raise _Invalid("answer가 비어 있습니다 / answer is empty")
    check = inputs.get("check")
    hist = inputs.get("_history_text") or ""
    source = sent.replace(hist, "") if hist else sent   # 이전 대화(앱의 지난 답)는 근거가 아니다(계약 24-1)
    out, errors = [], []
    for i, p in enumerate((obj.get("proposals") or [])[:ASSIST_MAX_PROPOSALS]):
        if not isinstance(p, dict):
            continue
        ev = _flat(str(p.get("evidence") or ""))
        why = None
        if (p.get("type") not in ("note", "action") or ev) and not _quoted(ev, source):   # 앱 동작은 근거 글 없이도(22-9)
            why = "근거(evidence)가 보낸 글에 그대로 없어요 / evidence is not verbatim in the input"
        q = check(p, why) if check else {**p, "rejected_reason": why}
        if q.get("rejected_reason"):
            errors.append(f"proposals[{i}] {p.get('type')}: {q['rejected_reason']}")
        out.append(q)
    if errors and not final:
        raise _Invalid("; ".join(errors[:6]))
    return {"answer": answer, "proposals": out}


register(AiTask(
    name="assist",
    purpose="사용자가 채팅으로 짚은 문제(안 보이는 마감·시험 날짜, 안 들어오는 성적 등)를 이 PC의 글에서 찾아 근거와 함께 제안한다",
    instructions=(
        "작업: 학생이 LMS Agent 앱에 물었습니다(아래 '사용자 요청'). 앱의 규칙이 놓친 것을 아래 '찾은 글'과 '지금 상태'에서만 찾아 "
        "한국어로 답하고, 근거가 있으면 앱에 반영할 제안을 주세요. 글에 없는 것은 지어내지 마세요. 읽지 않은 글을 읽었다고 하지 마세요.\n"
        "Task: a student asked the LMS Agent app the 'user request' below. Using ONLY the 'found texts' and 'current state' "
        "below, answer in Korean and propose fixes the app can apply, each backed by evidence. Never invent anything and "
        "never claim to have read anything that is not below.\n"
        "- answer: 한국어 2~6문장 — 무엇을 찾았는지(어느 글), 왜 규칙이 못 찾았을 것 같은지, 확인할 점 / 2-6 Korean sentences.\n"
        "- proposals[].type: add_deadline(새 마감·시험: title, due, kind 과제|시험, method?), fix_due(item_id, due), "
        "link_score(key = 점수 후보 key, component = 평가 항목 이름), set_count(component, n = 학기 전체 개수), "
        "set_weights(components = [{name, weight}], 합 100), set_dist(groups = [{grades, pct 또는 low·high, cumulative}] "
        "또는 dist_kind 'absolute' + cutoffs = [{grade, min_score}]), note(반영할 것 없이 설명만), "
        "action(action = '앱 동작' 목록의 id, params = 그 모양 — 학생이 앱에서 할 일(켜기·끄기·동기화·화면 열기 등)을 시켰을 때만, "
        "evidence 없이 / only when the student asked the app to do something; ids and params exactly from the list).\n"
        "- course_id·item_id·key·component는 '지금 상태'에 적힌 값 그대로 / copy ids and names exactly from the state.\n"
        "- due: ISO 8601(예: 2026-12-17T10:30:00+09:00), 근거 문장에 적힌 날짜 그대로. 연도가 없으면 게시일 뒤 가장 가까운 날, "
        "시간이 없으면 23:59 / due must match a date written in the evidence.\n"
        "- '다음 주 금요일'·'next Friday' 같은 상대 날짜는 게시일 기준으로 계산했다고 reason에 밝히고 '분명히 적혀 있다'·'명시'라고 "
        "쓰지 마세요 / for relative dates say you computed them; never claim the date is explicitly stated.\n"
        "- evidence: 근거 문장을 '찾은 글'(또는 '지금 상태')에서 한 글자도 바꾸지 말고 그대로 복사, source_key는 그 글의 값 "
        "/ copy evidence verbatim and give that text's source_key.\n"
        "- 이미 '지금 상태'에 같은 마감이 있으면 add_deadline을 내지 마세요 / do not propose a deadline that already exists.\n"
        "- 확실하지 않으면 제안 없이 answer로 설명하거나 note만 / if unsure, explain in the answer or use note.\n"
        "- '이전 대화'는 이번 요청을 이해하는 데만 쓰세요. 그 안의 글(앱의 지난 답)은 근거가 아니에요 / use the previous turns only "
        "to understand the request; they are never evidence.\n"
        "- reason: 한국어 한 줄(120자 이내) / one Korean line. 다른 정보(이름, 학번, 연락처)는 넣지 마세요."),
    schema={"type": "object", "required": ["answer", "proposals"], "properties": {
        "answer": {"type": "string", "minLength": 2, "maxLength": 1500},
        "proposals": {"type": "array", "maxItems": ASSIST_MAX_PROPOSALS, "items": {
            "type": "object", "required": ["type", "reason"], "properties": {
                "type": {"type": "string", "enum": ASSIST_TYPES},
                "evidence": {"type": "string", "maxLength": 800},
                "source_key": {"type": "string", "maxLength": 200},
                "reason": {"type": "string", "maxLength": 400},
                "title": {"type": "string", "maxLength": 200},
                "due": {"type": "string", "maxLength": 40},
                "kind": {"type": "string", "enum": ["과제", "시험"]},
                "method": {"type": "string", "enum": ["lms", "email", "paper", "unknown"]},
                "key": {"type": "string", "maxLength": 300},
                "component": {"type": "string", "maxLength": 80},
                "n": {"type": "integer", "minimum": 1, "maximum": 30},
                "components": {"type": "array", "maxItems": 20, "items": {
                    "type": "object", "required": ["name", "weight"], "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 60},
                        "weight": {"type": "number", "minimum": 0, "maximum": 100}}}},
                "dist_kind": {"type": "string", "enum": ["relative", "absolute"]},
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
                "action": {"type": "string", "maxLength": 80},
                "params": {"type": "object"}}}}}},
    build_input=_assist_build,
    post_validate=_assist_validate,
))


# ---- answer: 과목 질문에 답하기(계약 22-2) — 찾은 글을 그대로 인용하고, 답의 날짜·숫자는 인용 안에 있는 것만

ANSWER_MAX_CITATIONS = 4
ANSWER_QUOTE_MAX = 500
ANSWER_QUOTE_MIN = 8
_NOT_FOUND = re.compile(
    r"찾지\s*못|못\s*찾|찾을\s*수\s*없|확인(?:할\s*수\s*없|되지\s*않)|(?:나와|적혀|쓰여|언급되어|언급돼)\s*있지\s*않|언급(?:이|은)\s*없|"
    r"없는\s*것\s*같|(?<![a-z])(?:not\s+found|(?:couldn'?t|could\s+not|cannot|can'?t)\s+find|no\s+mention|"
    r"not\s+mentioned|does(?:n'?t|\s+not)\s+say)(?![a-z])", re.I)
_NUM = re.compile(r"\d+(?:[.:]\d+)*")


def _num_norm(t: str) -> str:
    return re.sub(r"(?<!\d)0+(?=\d)", "", t)


def _num_tokens(text: str) -> set:
    """글의 숫자들(앞의 0을 뗀 것) — '10:30'은 '10:30'·'10'·'30', '60.2'는 '60.2'·'60'·'2'."""
    out = set()
    for m in _NUM.finditer(text or ""):
        out.add(_num_norm(m.group(0)))
        out.update(_num_norm(x) for x in re.split(r"[.:]", m.group(0)) if x)
    return out


def _unsupported(answer: str, quotes: list[tuple[str, str | None]], inputs: dict, titles: list[str]) -> list[str]:
    """답에 있는 날짜·숫자 중 인용(quotes = [(인용, 게시 시각)]) 안에 없는 것. 날짜는 같은 월·일이거나 같은 말이면 있음(인용이
    'Dec. 15', 답이 '12월 15일'이어도 됨). 질문·과목 이름·인용한 글 제목의 숫자('LAB4')는 써도 된다."""
    joined = _flat(" ".join(q for q, _ in quotes))
    allowed_text = joined + " " + _flat(" ".join(titles))
    days = set()
    for q, posted in quotes:
        ref = (_as_dt(posted) or now_local()).date()
        for h in find_dates(q, ref):
            days.add((h.day.month, h.day.day))
    allowed = (_num_tokens(allowed_text) | _num_tokens(str(inputs.get("message") or ""))
               | _num_tokens(str(inputs.get("followup") or "")))   # 생략 질문이 이어받은 사용자 낱말('LAB4')
    allowed |= _num_tokens(" ".join(str(c.get("short") or "") for c in inputs.get("courses") or []))
    allowed |= {str(x) for d in days for x in d}
    bad, chars = [], list(answer)
    for h in find_dates(answer, now_local().date()):
        phrase = _flat(answer[h.start:h.end])
        if (h.day.month, h.day.day) not in days and phrase not in allowed_text:
            bad.append(phrase)
        for i in range(h.start, h.end):
            chars[i] = " "
    for m in _NUM.finditer("".join(chars)):
        if _num_norm(m.group(0)) not in allowed:
            bad.append(m.group(0))
    return list(dict.fromkeys(bad))


def _answer_build(inputs: dict) -> tuple[str, list]:
    """학생 질문 + 과목 목록 + 지금 상태 요약 + 찾은 글(assist와 같은 묶음, 가려서 12,000자 안에)."""
    return _bundle(inputs, ANSWER_MARK)


def _answer_validate(obj: dict, sent: str, inputs: dict, final: bool):
    """답과 인용. 인용(quote)은 보낸 '찾은 글'에 그대로 있어야 하고(아니면 다시 묻기, 마지막 시도면 그 인용을 뺌), 인용이 하나도
    없으면 '글에서 찾지 못했어요' 같은 답만 된다. 답의 날짜·숫자는 남은 인용 안에 있어야 한다(아니면 다시 묻기, 마지막 시도면 실패 →
    규칙 답). 결과 {"answer", "citations": [{"source_key", "quote"(가린 글의 인용)}]} — 원문 인용으로 바꾸기는 agent가 한다."""
    answer = unplace(_flat(str(obj.get("answer") or "")))[:1200]
    if len(answer) < 2:
        raise _Invalid("answer가 비어 있습니다 / answer is empty")
    texts = inputs.get("_sent_texts") or []
    meta = {str(sn.get("source_key") or ""): sn for sn in inputs.get("_sent_snippets") or []}
    good, problems = [], []
    for i, c in enumerate((obj.get("citations") or [])[:ANSWER_MAX_CITATIONS]):
        if not isinstance(c, dict):
            continue
        q, key = _flat(str(c.get("quote") or "")), str(c.get("source_key") or "")
        if len(q) < ANSWER_QUOTE_MIN:
            problems.append(f"citations[{i}]: 인용이 너무 짧습니다 / quote is too short")
            continue
        home = (next((k for k, t in texts if k == key and q in _flat(t)), None)
                or next((k for k, t in texts if q in _flat(t)), None))
        if home is None:
            problems.append(f"citations[{i}]: quote가 '찾은 글'에 그대로 없습니다 — 한 글자도 바꾸지 말고 복사하세요 "
                            "/ quote is not verbatim in the found texts")
            continue
        if not any(g["source_key"] == home and g["quote"] == q for g in good):
            good.append({"source_key": home, "quote": q[:ANSWER_QUOTE_MAX]})
    if problems and not final:
        raise _Invalid("; ".join(problems[:4]))
    if not good and not _NOT_FOUND.search(answer):
        raise _Invalid("인용(citations)이 없으면 '글에서 찾지 못했어요'라고만 답할 수 있습니다 "
                       "/ without citations, only say that the texts do not answer it")
    quotes = [(g["quote"], (meta.get(g["source_key"]) or {}).get("posted_at")) for g in good]
    titles = [str((meta.get(g["source_key"]) or {}).get("title") or "") for g in good]
    bad = _unsupported(answer, quotes, inputs, titles)
    if bad:
        raise _Invalid("답의 날짜·숫자가 인용한 문장에 없습니다: " + ", ".join(bad[:6])
                       + " — 인용한 문장에 있는 날짜·숫자만 쓰세요 / every date and number in the answer must appear in a "
                         "cited quote")
    return {"answer": answer, "citations": good}


register(AiTask(
    name="answer",
    purpose="과목에 대한 학생의 질문(범위·제출 형식·규정·장소 등)에 이 PC의 글을 그대로 인용해 답한다(제안 없음)",
    instructions=(
        "작업: 학생이 LMS Agent 앱에 과목에 대해 물었습니다(아래 '학생 질문'). 아래 '찾은 글'에 적힌 내용만으로 한국어로 답하고, "
        "답의 근거가 된 문장을 그대로 인용하세요. 글에 없는 것은 지어내지 말고, 글이 답하지 않으면 '글에서 찾지 못했어요'라고 "
        "답하세요.\n"
        "Task: a student asked the LMS Agent app a question about a course (the 'student question' below). Answer in "
        "Korean using ONLY the 'found texts' below and quote the sentences your answer is based on. Never invent "
        "anything; if the texts do not answer it, say so ('글에서 찾지 못했어요').\n"
        "- answer: 한국어 2~6문장 / 2-6 Korean sentences.\n"
        "- citations: 근거 1~4개. quote는 '찾은 글'에서 한 글자도 바꾸지 말고 그대로 복사(500자 이내), source_key는 그 글의 값 "
        "/ 1-4 citations; copy each quote verbatim from the found texts (max 500 chars) with that text's source_key.\n"
        "- 답에 쓰는 날짜·시각·숫자(점수·%·쪽수·주차 등)는 인용한 문장에 있는 것만. 날짜를 계산하거나 숫자를 더하지 말고, "
        "글 개수 같은 다른 숫자도 쓰지 마세요 / every date, time and number in the answer must appear in a cited quote; "
        "never compute dates or add numbers of your own.\n"
        "- '지금 상태'는 참고용이에요 — 인용하지 마세요 / the current state is context only; never cite it.\n"
        "- '이전 대화'는 이번 질문을 이해하는 데만 쓰고, 이전 답의 날짜·숫자를 근거로 쓰지 마세요 / use the previous turns only to "
        "understand the question; never cite them or reuse their dates and numbers.\n"
        "- 찾지 못했으면 citations는 빈 목록 / if not found, return an empty citations list.\n"
        "- [이메일] 같은 자리 표시는 그대로 두고, 다른 개인 정보(이름, 학번, 연락처)는 넣지 마세요 / keep placeholders as they "
        "are and add no personal data."),
    schema={"type": "object", "required": ["answer", "citations"], "properties": {
        "answer": {"type": "string", "minLength": 2, "maxLength": 1500},
        "citations": {"type": "array", "maxItems": ANSWER_MAX_CITATIONS, "items": {
            "type": "object", "required": ["source_key", "quote"], "properties": {
                "source_key": {"type": "string", "maxLength": 200},
                "quote": {"type": "string", "minLength": 1, "maxLength": ANSWER_QUOTE_MAX}}}}}},
    build_input=_answer_build,
    post_validate=_answer_validate,
))


# ---- chat: 일반 대화(계약 24-2) — 학기 마라톤을 같이 달리는 페이스메이커 말투. 제안·인용 없음, 했다고 하지 않음, 과목 사실을 지어내지 않음

CHAT_MARK = "학생이 건넨 말 / student message:"
CHAT_GENERAL = ("모드 / mode: general — 학업 질문인데 이 PC의 강의 글에 답이 없었어요. 일반 지식으로만 짧게 설명하고, 그 과목에서는 "
                "다르게 정했을 수 있으니 강의 글·교수님 안내를 확인하라고 덧붙이세요 / the course texts do not answer this academic "
                "question: give a short general-knowledge explanation and remind them the course may differ.")
CHAT_APP = ("앱이 할 수 있는 일 / what the app can do (안내만 — 했다고 하지 않기 / mention only, never claim it was done): "
            "과목 질문에 글을 인용해 답하기, 놓친 마감 찾기, 성적 '만약에' 계산, 문의 메일 초안, 공부 계획, 설정 바꾸기·동기화 "
            "(실행은 학생이 [실행]·[반영] 단추로).")
CHAT_MAX = 1_200
CHAT_MAX_SENTENCES = 8
CHAT_STATE_BUDGET = 800
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")
# 앱 동작을 했다는 말(계약 24-2: 실행은 [실행] 단추로만) — 묻는 말('잘 보냈어요?')은 빼고
_CLAIM_DONE = re.compile(
    r"(?:켰|껐|꺼\s*(?:뒀|놨|드렸)|켜\s*(?:뒀|놨|드렸)|바꿨|바꿔\s*(?:뒀|놨|드렸)|동기화했|반영했|추가했|등록했|삭제했|지웠|"
    r"보냈|전송했|실행했|설정했|저장했|예약했|넣어\s*(?:뒀|놨|드렸)|넣었|만들어\s*(?:뒀|놨|드렸)|제출했|업데이트했|승인했|제외했)"
    r"(?:어요|습니다|어|다|음)(?![가-힣])(?!\s*[?？])"
    r"|(?<![a-z])i(?:'ve|\s+have)?\s+(?:turned|switched|enabled|disabled|synced|added|deleted|removed|sent|scheduled|"
    r"saved|updated|submitted|approved|set\s+up)(?![a-z])", re.I)
_GRADE_CLAIM = re.compile(r"(?<![A-Za-z0-9])[A-D](?:\+|0)(?![A-Za-z0-9])|(?<![A-Za-z0-9])[A-DF]\s*학점")
_POINTS = re.compile(r"(\d+(?:\.\d+)?)\s*점(?![심수])")


def _chat_build(inputs: dict) -> tuple[str, list]:
    """학생이 건넨 말 + (일반 지식 모드면 그 줄) + 짧은 지금 상태(시각·주차·이번 주 마감 수·페이스, 성적 없음) + 이전 대화 +
    앱이 할 수 있는 일, 모두 가려서."""
    names = inputs.get("names")
    notes, red = [], False
    msg, n = redact(_flat(str(inputs.get("message") or ""))[:500], names)
    red = red or bool(n)
    lines = [CHAT_MARK, msg]
    if inputs.get("mode") == "general":
        lines += ["", CHAT_GENERAL]
    state, n = redact(str(inputs.get("state_text") or ""), names)
    red = red or bool(n)
    state, _ = cap_text(state, CHAT_STATE_BUDGET)
    lines += ["", "지금 상태 / current state (참고용, 성적 없음 / context only, no grades):", state or "(없음 / none)"]
    hist, n = history_block(inputs)
    red = red or bool(n)
    if hist:
        lines += ["", hist]
    lines += ["", CHAT_APP]
    if red:
        notes.append("보내기 전에 이메일·학번·이름 등을 가림")
    return "\n".join(lines), notes


def _chat_unsupported(answer: str, inputs: dict) -> list[str]:
    """답에 지어낸 과목 사실로 보이는 것: 학생 말·지금 상태에 없는 (상대 날짜가 아닌) 날짜, 학점 글자(A+·B0·'C 학점'), 'N점'."""
    allowed = _flat(str(inputs.get("message") or "") + " " + str(inputs.get("state_text") or ""))
    today = now_local().date()
    days = {(today.month, today.day)} | {(h.day.month, h.day.day) for h in find_dates(allowed, today)}
    bad = []
    for h in find_dates(answer, today):
        if not h.relative and (h.day.month, h.day.day) not in days:
            bad.append(_flat(answer[h.start:h.end]))
    squeezed = allowed.replace(" ", "")
    for m in _GRADE_CLAIM.finditer(answer):
        if m.group(0).replace(" ", "") not in squeezed:
            bad.append(m.group(0))
    allowed_nums = _num_tokens(allowed)
    for m in _POINTS.finditer(answer):
        if _num_norm(m.group(1)) not in allowed_nums:
            bad.append(m.group(0))
    return list(dict.fromkeys(bad))


def _chat_validate(obj: dict, sent: str, inputs: dict, final: bool):
    """답 하나(제안·인용 없음). 고칠 수 있는 것(1,200자·8문장·링크)은 마지막 시도가 아니면 다시 묻고 마지막이면 잘라 낸다.
    앱 동작을 했다는 말, 학생 말·상태에 없는 날짜·학점·점수는 마지막 시도까지 어기면 실패(→ 규칙 답)."""
    answer = unplace(_flat(str(obj.get("answer") or "")))
    if len(answer) < 2:
        raise _Invalid("answer가 비어 있습니다 / answer is empty")
    claim = _CLAIM_DONE.search(answer)
    if claim:
        raise _Invalid(f"앱 동작을 했다고 하면 안 됩니다('{claim.group(0)}') — 실행은 학생이 [실행] 단추로 해요 "
                       "/ never claim you performed an app action")
    bad = _chat_unsupported(answer, inputs)
    if bad:
        raise _Invalid("과목 사실(날짜·학점·점수)을 지어내면 안 됩니다: " + ", ".join(bad[:5])
                       + " / never invent course facts (dates, grades, scores)")
    problems = []
    if _URL.search(answer):
        problems.append("링크를 넣지 마세요 / no links")
    sentences = [s for s in _SENT_SPLIT.split(answer) if s.strip()]
    if len(sentences) > CHAT_MAX_SENTENCES:
        problems.append(f"{CHAT_MAX_SENTENCES}문장 이하로 답하세요 / at most {CHAT_MAX_SENTENCES} sentences")
    if len(answer) > CHAT_MAX:
        problems.append(f"{CHAT_MAX:,}자 이하로 답하세요 / at most {CHAT_MAX} characters")
    if problems and not final:
        raise _Invalid("; ".join(problems))
    answer = _flat(_URL.sub("", " ".join(sentences[:CHAT_MAX_SENTENCES])))
    if len(answer) > CHAT_MAX:
        answer = answer[:CHAT_MAX - 1].rstrip() + "…"
    return {"answer": answer}


register(AiTask(
    name="chat",
    purpose="학업 밖의 말(인사·고민·일반 지식 …)이나 강의 글에 답이 없는 질문에 페이스메이커 말투로 짧게 답한다(제안·인용 없음)",
    instructions=(
        "작업: 학생이 LMS Agent 앱의 채팅에 말을 건넸습니다(아래 '학생이 건넨 말'). 당신은 학기 마라톤을 같이 달리는 페이스메이커 "
        "친구예요. 한국어로 짧고 따뜻하게, 이전 대화의 흐름을 이어서 답하세요.\n"
        "Task: a student said something in the LMS Agent app's chat (the 'student message' below). You are a pacemaker "
        "friend running the semester marathon with them. Reply in Korean, short and warm, continuing the previous turns.\n"
        f"- answer: 한국어 2~{CHAT_MAX_SENTENCES}문장, {CHAT_MAX:,}자 이내 / 2-{CHAT_MAX_SENTENCES} Korean sentences, at most "
        f"{CHAT_MAX} characters.\n"
        "- '이전 대화'와 '지금 상태'는 흐름을 잇는 참고용이에요. 이전 답의 숫자·날짜를 사실처럼 되풀이하지 마세요 / previous turns "
        "and the current state are context only; do not restate numbers or dates from earlier answers as facts.\n"
        "- 과목 사실(마감·시험 날짜·점수·학점·범위·과제 내용)은 지어내지 마세요. 필요하면 그 과목을 물어보게 안내하세요(예: '기말 범위 "
        "어디까지야?') / never invent course facts (deadlines, exam dates, scores, grades, scope); suggest asking about the "
        "course instead.\n"
        "- 앱이 할 수 있는 일은 안내해도 되지만, 했다고 말하지 마세요 — 실행은 학생이 앱의 [실행]·[반영] 단추로만 해요 / you may "
        "mention what the app can do, but never claim you did anything; only the student's buttons do things.\n"
        "- 제안·인용·링크를 넣지 마세요 / no proposals, citations or links.\n"
        "- 위험한 요청(자해·자살, 남을 해치는 일, 불법, 해킹, 시험 부정·표절·대리 출석 등)은 정중히 거절하세요. 위기로 보이면 도움 "
        "받을 곳을 꼭 안내하세요: 자살예방상담전화 109, 정신건강위기상담 1577-0199, 학교 학생상담센터, 위급하면 112·119 / politely "
        "decline harmful requests (self-harm, hurting others, illegal acts, hacking, cheating); if it looks like a crisis, "
        "always point to help (Korea: 109, 1577-0199, campus counseling center, 112/119 in an emergency).\n"
        "- 건강·법률·돈처럼 전문가가 필요한 일은 일반적인 이야기만 하고 전문가와 상담하라고 권하세요 / for medical, legal or "
        "financial matters give general information only and suggest a professional.\n"
        "- 개인 정보(이름·학번·연락처)를 묻거나 넣지 말고, [이메일] 같은 자리 표시는 그대로 두세요 / never ask for or add personal "
        "data; keep placeholders as they are.\n"
        "- 'mode: general'이면 학업 질문인데 강의 글에 답이 없었다는 뜻이에요. 일반 지식으로 짧게 설명하고 그 과목에서는 다를 수 "
        "있다고 덧붙이세요. 머리말('강의 글에는 없어서…')은 앱이 붙여요 / in general mode, explain briefly from general "
        "knowledge and note the course may differ; the app adds the header."),
    schema={"type": "object", "required": ["answer"], "properties": {
        "answer": {"type": "string", "minLength": 2, "maxLength": 1500}}},
    build_input=_chat_build,
    post_validate=_chat_validate,
    max_chars=4_000,
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
    provider: str | None = None   # 실행기 id(계약 21장, 예: "codex")


def run_task(name: str, inputs: dict, runner=None, command: str | None = None, store=None,
             max_attempts: int | None = None, provider=None) -> AiResult:
    """작업 하나를 실행한다. 예외를 던지지 않는다. store를 주면 실행 기록(내용 없음)을 남긴다.
    실행기: provider(Plan·id) > command(claude 실행 파일) > store의 settings.ai_provider(기본 auto) — resolve()."""
    task = TASKS.get(name)
    started = time.monotonic()
    if task is None:
        return AiResult(False, name, error=f"모르는 AI 작업: {name}")
    if max_attempts is not None:
        task = replace(task, max_attempts=max(1, min(max_attempts, task.max_attempts)))
    plan, why = resolve(provider, command, store)
    res = AiResult(False, name, cli=plan.cli if plan else None, provider=plan.id if plan else None)
    if plan is None:
        res.error = why
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
            obj, meta = _call(plan, PROMPT, stdin, task.timeout, runner)
            res.model = meta.get("model") or plan.model or res.model
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
                 max_calls: int = MAX_CALLS_PER_SYNC, max_per_day: int = MAX_CALLS_PER_DAY, provider=None):
        self.store, self.runner, self.command = store, runner, command
        self.provider = provider   # Plan·id·None(None이면 store의 settings.ai_provider대로 — 계약 21-3)
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
        res = run_task(name, inputs, runner=self.runner, command=self.command, store=self.store, max_attempts=left,
                       provider=self.provider)
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
                command: str | None = None, provider=None) -> dict:
    """확인함 후보를 AI로 정리해 검증된 결정만 store.update_user(..., by="ai", reason=…)로 반영한다.
    settings.ai_cli가 꺼져 있으면 아무것도 하지 않는다. 결과는 store.data["ai_triage"]에도 남는다(state.ai.last_triage).
    리더: 동기화 뒤 settings.ai_auto_triage가 켜져 있으면 `agent_cli.auto_triage(store, session=같은 세션)` 후 저장.
    반환: {"at", "ok", "applied": n, "skipped": [{"id","why"}], "error", "attempts",
           "pending_left": 아직 확인 대기 수, "user_reverted": 사용자가 되돌려 AI가 건드리지 않은 수}(계약 13-3)"""
    session = session or AiSession(store, runner=runner, command=command, provider=provider)
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
    plan, why = resolve(None, command)
    if plan is None:
        raise AgentCliError(why)
    body, _ = redact(text)
    body, cut = cap_text(body)
    stdin = (f"응답 형식(JSON 객체 하나만, 다른 글 없이) / reply with one JSON object: {schema_hint}\n\n"
             + ("(글이 길어 일부만 보냄)\n" if cut else "")
             + "=== 입력 글 시작 / INPUT START ===\n" + body + "\n=== 입력 글 끝 / INPUT END ===\n")
    try:
        obj, _ = _call(plan, prompt, stdin, timeout, runner)
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


EVAL_LABELS = {   # 화면의 [연결 시험] 목록에 보일 이름
    "updated_grading_policy": "평가 비중 · 영어 공지 목록", "korean_syllabus_prose": "평가 비중 · 한국어 문장",
    "no_weights": "평가 비중 · 없는 글", "asic_lab3_due": "마감 · 영어 게시판 글",
    "asic_lab4_email_due": "마감 · 한국어 '자정'", "korean_prose_distribution": "학점 비율 · 한국어 문장",
    "pass_fail_only": "학점 비율 · P/NP 과목", "triage_extension_and_holiday": "확인함 정리 · 연장·휴강",
}


def run_evals(runner=None, command: str | None = None, cases: list | None = None, provider=None,
              settings: dict | None = None, progress=None) -> dict:
    """평가 묶음을 돌린다(runner=None이면 실제 CLI — 호출 한도·기록과 무관한 점검용). 데모 문장만 보낸다(강의 글 아님).
    `python -m lms_agent ai-eval`·화면의 [연결 시험](계약 21-3)에서 부르기 좋게 결과만 돌려준다(입력·답 내용 없음).
    provider: Plan·실행기 id·None(settings대로, 기본 auto). progress(done, total, case_result)는 사례마다.
    결과: {"cli", "provider", "passed", "total", "duration_ms", "error",
           "cases": [{"name","label","task","passed","ok","error","problem","attempts","duration_ms"}]}"""
    cases = cases or EVAL_CASES
    started = time.monotonic()
    plan, why = resolve(provider, command, settings=settings or {})
    out: list[dict] = []
    if plan is None:
        return {"cli": None, "provider": None, "passed": 0, "total": len(cases), "cases": [], "error": why,
                "duration_ms": 0}
    for case in cases:
        res = run_task(case["task"], case["inputs"], runner=runner, provider=plan)
        problem = _check_expect(case, res)
        out.append({"name": case["name"], "label": EVAL_LABELS.get(case["name"], case["name"]), "task": case["task"],
                    "passed": problem is None, "ok": res.ok, "error": res.error, "problem": problem,
                    "attempts": res.attempts, "duration_ms": res.duration_ms})
        if progress is not None:
            progress(len(out), len(cases), out[-1])
    return {"cli": plan.cli, "provider": plan.id, "passed": sum(c["passed"] for c in out), "total": len(out),
            "cases": out, "error": None, "duration_ms": int((time.monotonic() - started) * 1000)}
